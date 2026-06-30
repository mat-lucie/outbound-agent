"""Phase C — reconciliation sweep for the PROSPECT-but-already-pending leak.

Finds ``linkedin_outreach`` list entries stuck at stage=PROSPECT that LinkedIn
already shows as ``hasPendingInvitation`` (we invited them on a prior run but
the advance never persisted — the daily re-selection leak) and flips them to
CONNECTION_SENT so they leave the invite pool and Phase 0 watches for an
acceptance.

This is the one-shot backlog-clearer that complements the in-loop fixes:
- Phase A (``pre_invite_check`` heal path) flips pending rows the daily run
  happens to re-select, and
- Phase B (``compute_invite_outcome``) stops new rows from leaking by advancing
  authoritatively at send time.
This sweep clears whatever backlog accumulated before those landed, without
waiting for each row to be re-selected by chance.

Pipeline-leakage red line (NEVER ghost-advance an un-invited prospect): a row
is flipped ONLY when a FRESH Sales Nav re-scrape returns
``hasPendingInvitation="true"``. Any other value — blank, 2nd/3rd degree,
unknown, or a missing CSV row — leaves the row at PROSPECT untouched. Writes go
through ``_attio_advance_with_escalation`` (AttioWriter: §3.15 registry check,
stage monotonicity, DLQ + operator-review row on failure).

Operator-invoked only (``cli.py reconcile-pending-invites``). Always run with
``--dry-run`` first and review the projected candidate list before a wet run.

Dry-run semantics (fork convention): ``--dry-run`` is fully side-effect free —
it resolves and previews the PROSPECT *candidates* but does NOT launch a Sales
Nav scrape (and therefore cannot project per-row flips, which require a live
``hasPendingInvitation`` signal). This mirrors the fork's ``_pre_invite_degree_check``
dry-run (no PB launch, no sheet write, no cache mutation). The wet path scrapes
and flips. (Upstream's dry-run scrapes via a sandbox sheet; the fork has no
sandbox-sheet path, so dry-run is preview-only here.)
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import click

from models.business_calendar import operator_today
from models.pipeline import PipelineStage
from workflows import recheck_cache
from workflows.daily_check_helpers import (
    _dedupe_by_linkedin_url,
    _get_all_entries_parsed,
    _normalize_linkedin_url,
)
from workflows.pre_invite_check import (
    _IMMUTABLE_FROZEN_AT_VALUES,
    _launch_sales_nav_scrape,
)
from workflows.record_cache import RecordCache

if TYPE_CHECKING:
    from datetime import date

    from clients.attio import AttioClient
    from clients.phantombuster import PhantomBusterClient
    from workflows.audit import AuditLogger

# Registered in clients/attio_writer_registry.py as an authorized writer of
# linkedin_outreach.stage / .last_contact_date / .experiment_id_frozen_at.
_WRITER_MODULE = (
    "workflows.pending_invite_reconciliation.run_pending_invite_reconciliation"
)


def run_pending_invite_reconciliation(
    *,
    attio: AttioClient,
    pb: PhantomBusterClient,
    sales_nav_profile_scraper_id: str,
    list_id: str,
    today: date | None = None,
    dry_run: bool = True,
    force_rescrape: bool = False,
    batch_size: int = 25,
    audit_logger: AuditLogger | None = None,
) -> dict:
    """Re-scrape PROSPECT rows and flip the genuinely-pending ones forward.

    Args:
        sales_nav_profile_scraper_id: PB Sales Nav Profile Scraper agent id —
            the only backend that reports ``hasPendingInvitation``.
        today: operator-day override (defaults to ``operator_today()``).
        dry_run: when True, previews the PROSPECT candidate set and writes
            nothing — and (fork convention) does NOT launch a scrape, so no
            per-row flips are projected (``hasPendingInvitation`` is only known
            after a live scrape). The wet path scrapes and flips.
        force_rescrape: bypass the recheck-cache TTL skip and scrape every
            selected PROSPECT row regardless of when it was last checked.
        batch_size: cap on profiles scraped this run (PB cost bound). Excess
            rows are logged and left for a follow-up run — never silently
            dropped.

    Returns a summary dict: examined, candidates, scraped, flipped,
    left_at_prospect, failed, skipped_recently_checked, skipped_over_batch.
    """
    today_iso = (today or operator_today()).isoformat()
    summary = {
        "examined": 0,
        "candidates": 0,
        "scraped": 0,
        "flipped": 0,
        "left_at_prospect": 0,
        "failed": 0,
        "skipped_recently_checked": 0,
        "skipped_over_batch": 0,
        "dry_run": dry_run,
    }

    entries = _get_all_entries_parsed(attio)
    summary["examined"] = len(entries)
    prospects = [
        e for e in entries if e.get("stage") == PipelineStage.PROSPECT.value
    ]
    if not prospects:
        click.echo("No PROSPECT rows to reconcile.")
        return summary

    # Resolve each PROSPECT entry's LinkedIn URL via the same cache the daily
    # run uses, then collapse duplicate records so every entry_id for a
    # prospect flips in lock-step (mirrors _build_invite_send_data → dedupe).
    cache = RecordCache(attio)
    rows: list[dict] = []
    for attrs in prospects:
        record_id = attrs.get("record_id")
        if not record_id:
            continue
        _, _, linkedin_url, _, _ = cache.get(record_id)
        if not linkedin_url:
            continue
        rows.append({**attrs, "linkedInUrl": linkedin_url})
    deduped, _dropped = _dedupe_by_linkedin_url(rows)
    summary["candidates"] = len(deduped)
    if not deduped:
        click.echo("No PROSPECT rows with a resolvable LinkedIn URL.")
        return summary

    # TTL skip: don't re-scrape profiles the daily degree-check touched within
    # the recheck window unless --force. partition() returns (cached, to_check).
    by_url = {_normalize_linkedin_url(r["linkedInUrl"]): r for r in deduped}
    if force_rescrape:
        to_check_urls = [r["linkedInUrl"] for r in deduped]
    else:
        cached, to_check_urls = recheck_cache.partition(
            [r["linkedInUrl"] for r in deduped]
        )
        summary["skipped_recently_checked"] = len(cached)

    if len(to_check_urls) > batch_size:
        summary["skipped_over_batch"] = len(to_check_urls) - batch_size
        click.echo(
            f"  ⚠ {summary['skipped_over_batch']} candidate(s) over the "
            f"--batch-size={batch_size} cap left for a follow-up run."
        )
        to_check_urls = to_check_urls[:batch_size]

    if not to_check_urls:
        click.echo(
            f"Nothing to scrape (candidates={summary['candidates']}, "
            f"skipped_recently_checked={summary['skipped_recently_checked']}). "
            f"Use --force to bypass the recheck-cache TTL."
        )
        return summary

    # Fork convention: a dry-run is fully side-effect free (no PB launch, no
    # sheet write, no cache mutation). hasPendingInvitation is only known after
    # a live scrape, so dry-run previews the candidate set and cannot project
    # per-row flips. Re-run with --wet to scrape + flip.
    if dry_run:
        click.echo(
            f"  [DRY RUN] {len(to_check_urls)} PROSPECT candidate(s) would be "
            f"scraped via Sales Nav; only those returning hasPendingInvitation="
            f"true would flip to CONNECTION_SENT. No scrape/write performed."
        )
        for url in to_check_urls:
            click.echo(f"    [DRY RUN] candidate: {url}")
        click.echo(
            f"Reconciliation (DRY RUN — no scrape, no writes): "
            f"{summary['candidates']} candidate(s), "
            f"{len(to_check_urls)} would be scraped. Re-run with --wet to apply."
        )
        if audit_logger is not None:
            audit_logger.event("pending_invite_reconciliation", **summary)
        return summary

    click.echo(
        f"Reconciliation: {summary['candidates']} PROSPECT candidate(s), "
        f"scraping {len(to_check_urls)} via Sales Nav..."
    )
    _container, degree_lookup, extras = _launch_sales_nav_scrape(
        pb, sales_nav_profile_scraper_id, to_check_urls,
    )
    summary["scraped"] = len(to_check_urls)

    # Prime the recheck cache with observed degrees (mirrors the daily
    # degree-check) so a follow-up run can TTL-skip these.
    if degree_lookup:
        recheck_cache.record_many(
            {url: degree_lookup.get(_normalize_linkedin_url(url)) for url in to_check_urls}
        )

    # Deferred import: avoid an import cycle (daily_check imports pre_invite_check
    # which this module also imports). Resolved at call time, like the in-function
    # imports in pre_invite_check.
    from workflows.daily_check import _attio_advance_with_escalation

    for url in to_check_urls:
        norm = _normalize_linkedin_url(url)
        row = by_url.get(norm)
        if row is None:
            continue
        has_pending = extras.get(norm, {}).get("hasPendingInvitation") == "true"
        if not has_pending:
            # No positive already-invited signal → NEVER flip (pipeline-leakage
            # red line). Leaves the row at PROSPECT for the normal daily flow.
            summary["left_at_prospect"] += 1
            continue

        flip_attrs: dict = {
            "stage": PipelineStage.CONNECTION_SENT.value,
            "last_contact_date": today_iso,
        }
        # Re-stamp connection_sent only when the row carried an experiment AND
        # its prior frozen_at is not terminal/immutable — same guard as the
        # pre_invite_check pending flip. Preserve experiment_id (a prior-run
        # event; never back-fill the currently-running cohort).
        prior_experiment_id = row.get("experiment_id")
        prior_frozen_at = row.get("experiment_id_frozen_at")
        if (
            prior_experiment_id is not None
            and prior_frozen_at not in _IMMUTABLE_FROZEN_AT_VALUES
        ):
            flip_attrs["experiment_id_frozen_at"] = "connection_sent"

        # Flip every list entry for this prospect (duplicates included).
        row_ok = True
        for entry_id in row.get("entry_ids") or [row.get("entry_id")]:
            if not entry_id:
                continue
            ok = _attio_advance_with_escalation(
                attio=attio,
                entry_id=entry_id,
                entry_attributes=flip_attrs,
                list_id=list_id,
                linkedin_url=url,
                today=today_iso,
                step_label="pending_invite_reconciliation",
                writer_module=_WRITER_MODULE,
                prior_stage=PipelineStage.PROSPECT.value,
                person_record_id=row.get("record_id"),
                audit_logger=audit_logger,
            )
            if not ok:
                row_ok = False
        if row_ok:
            summary["flipped"] += 1
            click.echo(
                f"  ✓ flipped PROSPECT→CONNECTION_SENT: "
                f"record_id={row.get('record_id')!r} url={url}"
            )
        else:
            # _attio_advance_with_escalation already opened the attio_write_failed
            # DLQ row + echoed; the row stays at PROSPECT to retry.
            summary["failed"] += 1

    click.echo(
        f"Reconciliation done: "
        f"flipped {summary['flipped']}, left_at_prospect "
        f"{summary['left_at_prospect']}, failed {summary['failed']} "
        f"(scraped {summary['scraped']}/{summary['candidates']})."
    )
    if audit_logger is not None:
        audit_logger.event("pending_invite_reconciliation", **summary)
    return summary
