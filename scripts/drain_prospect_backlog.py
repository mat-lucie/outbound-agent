#!/usr/bin/env python3
"""Drain the already-connected/invited backlog out of the PROSPECT pool.

# Why

The daily invite slice is sorted oldest-prospect-first, and the oldest
prospects are the ones most likely already connected/invited (manually, or by
prior runs that never recorded it). Their CRM stage was never advanced, so
they re-rank to the top of the queue every day; the pre-invite degree check
correctly rejects them, and ~0 fresh invites go out. The daily only processes
~25/run (bounded by the connection cap), so a large backlog takes weeks to
self-heal — during which new prospects (which sort LAST) never get reached.

This drains the WHOLE invite-eligible PROSPECT pool in one pass: it runs the
exact same degree check the daily uses (`pre_invite_check._pre_invite_degree_check`)
over every eligible prospect, letting it reclassify them in Attio —
  1st-degree   → ACCEPTED
  pending      → CONNECTION_SENT
  Out of Network → UNREACHABLE
  2nd/3rd      → stays PROSPECT (genuinely invitable)
— and sends NO invites. Afterwards the daily slice finally surfaces fresh
prospects. The headline number is how many remain genuinely-invitable: your
true daily-invite supply.

# Reuse, not reimplementation

The degree→stage classification (the risky part) is `_pre_invite_degree_check`
verbatim — same scrape, same Pattern-A/pending/OON flips, same §3.15
AttioWriter writes, same dry-run contract. This script only orchestrates:
gather the eligible pool, chunk it (one PB launch per chunk), and tally.

Unlike the daily it does NOT apply the §3.8 company throttle or same-company
dedup — those gate *contacting* a company, but a drain only reads degree, so
every eligible prospect must be checked.

# Usage

    python3 scripts/drain_prospect_backlog.py --dry-run          # scrape + preview, no flips
    python3 scripts/drain_prospect_backlog.py --limit 25         # canary: first chunk only
    python3 scripts/drain_prospect_backlog.py                    # full sweep
    python3 scripts/drain_prospect_backlog.py --chunk-size 25    # tune PB launch size
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clients.attio import AttioClient  # noqa: E402
from clients.phantombuster import PhantomBusterClient  # noqa: E402
from models.business_calendar import operator_today  # noqa: E402
from models.pipeline import PipelineStage, is_invite_eligible, is_send_eligible  # noqa: E402
from workflows.daily_check import _get_all_entries_parsed  # noqa: E402
from workflows.pre_invite_check import _pre_invite_degree_check  # noqa: E402
from workflows.record_cache import RecordCache, preload_pipeline_persons  # noqa: E402

QUALITY_FLOOR = 60  # mirror daily_check Part A invite-slice gate
_LANE_RANK = {"enterprise_mode": 0, "target_company_mode": 1, "legacy": 2}


def gather_eligible_prospects(parsed_entries: list[dict], today: date) -> list[dict]:
    """Filter + sort PROSPECT entries exactly as the daily invite slice does.

    Same gates: stage==PROSPECT, quality_score >= 60, invite-eligible
    (quarantine elapsed), send-eligible (not archaeology-stamped). Same
    lane/score/age ordering, so `--limit` canaries the same rows the daily
    would hit first.
    """
    pool = [
        attrs
        for attrs in parsed_entries
        if attrs.get("stage") == PipelineStage.PROSPECT.value
        and attrs.get("quality_score") is not None
        and int(attrs["quality_score"]) >= QUALITY_FLOOR
        and is_invite_eligible(attrs, today)
        and is_send_eligible(attrs)
    ]
    pool.sort(key=lambda a: (
        _LANE_RANK.get(a.get("scoring_lane") or "legacy", 3),
        -int(a.get("quality_score") or 0),
        a.get("created_at") or a.get("entry_created_at") or "",
    ))
    return pool


def build_send_rows(prospects: list[dict], cache: RecordCache) -> list[dict]:
    """Build the minimal `to_send_data` rows `_pre_invite_degree_check` reads.

    Resolves each prospect's LinkedIn URL from the Person record (via cache);
    rows with no URL are dropped (can't degree-check them). `message` is blank
    — a drain never sends an invite, so no note is rendered, and the §3.8
    company throttle / same-company dedup are deliberately NOT applied.
    """
    rows = []
    for attrs in prospects:
        name, company, linkedin_url, _industry, title = cache.get(attrs["record_id"])
        if not linkedin_url:
            continue
        rows.append({
            "linkedInUrl": linkedin_url,
            "message": "",  # no invite is sent during a drain
            "entry_id": attrs["entry_id"],
            "record_id": attrs.get("record_id", ""),
            "current_stage": attrs["stage"],
            "name": name,
            "company": company,
            "title": title,
            "invite_eligible_after": attrs.get("invite_eligible_after"),
            "experiment_id": attrs.get("experiment_id"),
            "experiment_id_frozen_at": attrs.get("experiment_id_frozen_at"),
        })
    return rows


def _chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def drain(
    rows: list[dict],
    *,
    degree_check,
    chunk_size: int,
    limit: int | None = None,
    on_chunk=None,
) -> dict:
    """Run `degree_check` over `rows` in chunks; tally fresh vs drained.

    `degree_check(chunk) -> (still_to_invite, already_connected)` is the
    injected reclassifier (production: `_pre_invite_degree_check`, which flips
    1st/pending/OON in Attio as a side effect and returns the remaining
    genuinely-invitable rows as `still_to_invite`). No invites are ever sent —
    `still_to_invite` is only counted. `--limit` caps rows examined (canary).

    Failure handling (the tally must never lie — this is a measurement tool):
    - `_pre_invite_degree_check` is fail-safe: on PB failure / empty CSV / dead
      cookie it RETURNS ``([], [])`` and drops the chunk untouched. A non-empty
      chunk yielding zero fresh AND zero 1st-degree therefore did NOT resolve —
      it is counted as **failed**, never as drained, so a failed sweep can't
      report as a successful one. (A genuinely all-pending/OON chunk also returns
      ``([], [])``; flagging it failed only under-counts `drained` — the safe
      direction — and a re-run won't re-flag it because those rows are no longer
      PROSPECT and drop out of the pool.)
    - A *raising* `degree_check` (ConfigError, legacy PB timeout, Attio write
      error) is caught per-chunk so a mid-sweep failure can't abort after
      already committing earlier chunks' flips; the chunk is marked failed.

    `on_chunk(index, total, info)` is an optional progress callback.
    """
    if limit is not None:
        rows = rows[:limit]
    chunks = list(_chunked(rows, chunk_size))
    total = len(chunks)
    examined = fresh = connected_1st = drained = 0
    failed_chunks = failed_rows = 0
    for idx, chunk in enumerate(chunks, 1):
        try:
            still, connected = degree_check(chunk)
        except Exception as exc:  # noqa: BLE001 — per-chunk isolation
            failed_chunks += 1
            failed_rows += len(chunk)
            if on_chunk:
                on_chunk(
                    idx,
                    total,
                    {
                        "failed": True,
                        "rows": len(chunk),
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
            continue
        if chunk and not still and not connected:
            # Dropped batch (fail-safe) or all-pending/OON — unverifiable here;
            # treat as failed so it's never silently counted as drained.
            failed_chunks += 1
            failed_rows += len(chunk)
            if on_chunk:
                on_chunk(idx, total, {"failed": True, "rows": len(chunk)})
            continue
        examined += len(chunk)
        fresh += len(still)
        connected_1st += len(connected)
        drained += len(chunk) - len(still)
        if on_chunk:
            on_chunk(idx, total, {
                "rows": len(chunk), "fresh": len(still),
                "drained": len(chunk) - len(still),
            })
    return {
        "examined": examined,            # rows in successfully-scraped chunks
        "fresh": fresh,                  # genuinely invitable, left at PROSPECT
        "connected_1st": connected_1st,  # flipped to ACCEPTED
        "drained": drained,              # reclassified out of PROSPECT (1st/pending/OON)
        "failed_chunks": failed_chunks,  # chunks that dropped / raised — NOT drained
        "failed_rows": failed_rows,
    }


@click.command()
@click.option("--dry-run", is_flag=True, help="Scrape + preview buckets; no Attio flips.")
@click.option("--limit", type=int, default=None, help="Cap rows examined (canary, e.g. 25).")
@click.option("--chunk-size", type=int, default=25, help="Profiles per PB launch (default 25).")
def main(dry_run: bool, limit: int | None, chunk_size: int) -> None:
    list_id = os.environ.get("ATTIO_LIST_ID", "").strip()
    if not list_id:
        click.echo("ERROR: ATTIO_LIST_ID unset.", err=True)
        sys.exit(2)
    profile_scraper_id = os.environ.get("PB_PROFILE_SCRAPER_ID") or None
    sales_nav_profile_scraper_id = os.environ.get("PB_SALES_NAV_PROFILE_SCRAPER_ID") or None
    backend = os.environ.get("PRE_INVITE_DEGREE_CHECK_BACKEND", "regular").strip()

    today = operator_today()

    # dry-run is a faithful preview ONLY on the sales_nav backend with a
    # sandbox sheet. On `regular` (or sales_nav without GSHEET_DRYRUN_ID) the
    # degree check degrades to skip-and-preview — every stale row is reported as
    # "fresh / would-invite" with degree UNRESOLVED, so the numbers invert. Warn
    # loudly instead of letting a skimming operator trust them.
    if dry_run and (backend != "sales_nav" or not os.environ.get("GSHEET_DRYRUN_ID")):
        click.echo(
            "  WARNING DEGRADED DRY-RUN: backend is not sales_nav (or GSHEET_DRYRUN_ID "
            "unset) — the degree scrape is skipped, so 'fresh' will read as the "
            "whole pool and 'drained' as 0. These numbers are NOT reliable; use a "
            "wet `--limit 25` canary to preview real reclassification.",
            err=True,
        )

    # Sales Nav dead-cookie pre-flight (mirrors cli.py daily). A wet sweep
    # launches ~1 PB scrape per chunk; a dead cookie would burn them all and —
    # with each chunk dropping to the fail-safe ([], []) path — report nothing.
    # One pre-flight up front fails fast instead.
    #
    # skip_parallel=True: skip the regular-cookie (SN Inbox Scraper) check. The
    # daily runs it because Phase 0.5 reads the inbox via PB_LI_SESSION_COOKIE,
    # but the drain ONLY does Sales Nav degree scrapes (PB_LI_SALES_NAV_SESSION_
    # COOKIE) — it never touches the regular cookie, so a dead regular cookie
    # must not block the drain. (It still needs rotating for the daily.)
    if backend == "sales_nav" and not dry_run:
        click.echo("--- Pre-flight: Sales Nav health check (degree-scrape only) ---")
        from scripts.validate_sales_nav_health import quick_check
        rc, summary = quick_check(skip_parallel=True)
        click.echo(summary)
        if rc == 1:
            click.echo(
                "Sales Nav pre-flight FAILED — rotate cookies OR set "
                "PRE_INVITE_DEGREE_CHECK_BACKEND=regular, then re-run.",
                err=True,
            )
            sys.exit(2)

    with AttioClient() as attio:
        pb = PhantomBusterClient()
        parsed = _get_all_entries_parsed(attio)
        pool = gather_eligible_prospects(parsed, today)

        cache = RecordCache(attio)
        preload_pipeline_persons(attio, cache, {a["record_id"] for a in pool if a.get("record_id")})
        rows = build_send_rows(pool, cache)

        click.echo(
            f"Drain PROSPECT backlog ({'dry-run' if dry_run else 'live'}, backend={backend}): "
            f"{len(pool)} eligible PROSPECT(s), {len(rows)} with a resolvable URL"
            f"{f'; canary --limit {limit}' if limit is not None else ''}. "
            f"Chunk size {chunk_size} (one PB launch each). No invites are sent."
        )

        def degree_check(chunk: list[dict]) -> tuple[list[dict], list[dict]]:
            return _pre_invite_degree_check(
                chunk, pb, profile_scraper_id, attio, list_id,
                sales_nav_profile_scraper_id=sales_nav_profile_scraper_id,
                today=today, dry_run=dry_run,
            )

        def on_chunk(idx: int, total: int, info: dict) -> None:
            # Per-chunk progress over a multi-minute, ~10-launch sweep.
            if info.get("failed"):
                click.echo(f"  chunk {idx}/{total}: FAILED ({info['rows']} rows unresolved)"
                           + (f" — {info['error']}" if info.get("error") else ""))
            else:
                click.echo(f"  chunk {idx}/{total}: examined={info['rows']} "
                           f"drained={info['drained']} fresh={info['fresh']}")

        report = drain(
            rows, degree_check=degree_check, chunk_size=chunk_size,
            limit=limit, on_chunk=on_chunk,
        )

    click.echo(
        f"\nDrain complete ({'dry-run' if dry_run else 'live'}):\n"
        f"  examined={report['examined']}\n"
        f"  drained (reclassified out of PROSPECT: 1st/pending/OON)={report['drained']}\n"
        f"    of which 1st-degree->ACCEPTED={report['connected_1st']}\n"
        f"  fresh invitable left at PROSPECT={report['fresh']}  <-- your true daily-invite supply\n"
        f"  failed chunks={report['failed_chunks']} ({report['failed_rows']} rows unresolved)"
    )
    if report["failed_chunks"] > 0:
        click.echo(
            f"\nWARNING: {report['failed_chunks']} chunk(s) failed to scrape — those rows "
            f"may not have been reclassified — verify before re-running. The 'drained'/'fresh' "
            f"counts above cover only the scraped rows. Re-run until 'failed chunks=0' for a "
            f"complete drain.",
            err=True,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
