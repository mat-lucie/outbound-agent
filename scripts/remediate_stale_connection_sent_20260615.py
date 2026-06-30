"""One-time remediation: park permanently-stale CONNECTION_SENT rows.

Why: CONNECTION_SENT rows whose ``last_contact_date`` is older than
``STALE_CONNECTION_SENT_ESCALATE_DAYS`` (45) are permanently beyond the
``ACCEPTANCE_CHECK_WINDOW_DAYS`` (14) acceptance-detection window — Phase 0
never re-checks them, so they will never flip to ACCEPTED. Yet they stay at
CONNECTION_SENT, get scanned on every daily run, and re-open one aggregated
``stale_connection_sent`` operator-review row per day (see
``workflows.daily_check.detect_accepted_connections``). This script moves
them to the terminal ``UNREACHABLE`` stage so they leave the scan +
escalation loop.

Why UNREACHABLE (not a new "Lapsed" stage): UNREACHABLE is the existing
*undeliverability* terminal — "the prospect never declined, we simply can't
reach them via the automated channel" (models/pipeline.py). A 45-day-stale
invite fits exactly: the invite was delivered, never accepted, and the
automated path has run out of moves. It is deliberately kept out of the
learning-loop / response metrics and gates the row out of every send + invite
loop (rank 90). It also leaves an operator escape hatch — a prospect who later
accepts can still be moved forward to RESPONDED/CALL_BOOKED without a
monotonicity regression. Inventing a "Lapsed" stage would touch the enum,
STAGE_RANK, every send/invite gate, the CRM select options, and the
CI rank guard — far too much surface for a one-time cleanup.

Idempotent + safe to re-run: the candidate set is recomputed from the LIVE
CRM stage each run, so a row already parked at UNREACHABLE is never picked
up again. A live-stage re-fetch immediately before writing drops any row that
advanced (e.g. a concurrent daily run flipped it to ACCEPTED) so the park
never regresses real progress. Dry-run is the default and touches nothing.

Usage:
  python3 scripts/remediate_stale_connection_sent_20260615.py            # dry-run (default)
  python3 scripts/remediate_stale_connection_sent_20260615.py --apply    # write UNREACHABLE
  ... [--older-than-days N]   # override the 45-day threshold

Operator-run only: --apply writes live CRM stage changes. Do NOT auto-run
the live mode.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import click
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import os  # noqa: E402

import httpx  # noqa: E402

from clients.attio import AttioClient  # noqa: E402
from clients.attio_writer import AttioError, AttioWriter, WriteIntent  # noqa: E402
from clients.attio_writer_registry import UnauthorizedAttioWriteError  # noqa: E402
from models.pipeline import PipelineStage  # noqa: E402
from workflows.daily_check import STALE_CONNECTION_SENT_ESCALATE_DAYS  # noqa: E402
from workflows.daily_check_helpers import _get_all_entries_parsed  # noqa: E402
from workflows.migration_run_writer import MigrationRunWriter  # noqa: E402
from workflows.record_cache import RecordCache, preload_pipeline_persons  # noqa: E402

# This script writes `stage` on linkedin_outreach list entries; it is
# registered in clients/attio_writer_registry.WRITE_OWNER_REGISTRY under
# ("linkedin_outreach", "stage") so AttioWriter's §3.15 owner check passes.
WRITER_MODULE = "scripts.remediate_stale_connection_sent_20260615"


def _select_stale(all_parsed: list[dict], cutoff_iso: str) -> list[dict]:
    """CONNECTION_SENT entries whose last_contact_date is strictly < cutoff.

    Rows with no last_contact_date are NOT selected: without a send date we
    cannot prove the invite is older than the window, and the conservative
    direction is to leave them in the daily scan rather than terminalize
    them on a missing timestamp.
    """
    stale = []
    for attrs in all_parsed:
        if attrs.get("stage") != PipelineStage.CONNECTION_SENT.value:
            continue
        last_sent = (attrs.get("last_contact_date") or "")[:10]
        if not last_sent or last_sent >= cutoff_iso:
            continue
        stale.append(attrs)
    return stale


@click.command()
@click.option("--apply", "apply_changes", is_flag=True, help="Write UNREACHABLE stage changes (default: dry-run).")
@click.option(
    "--older-than-days",
    default=STALE_CONNECTION_SENT_ESCALATE_DAYS,
    show_default=True,
    help="Park CONNECTION_SENT rows whose last_contact_date is older than this many days.",
)
def main(apply_changes: bool, older_than_days: int) -> None:
    mode = "APPLY" if apply_changes else "DRY-RUN (no writes)"
    cutoff_iso = (date.today() - timedelta(days=older_than_days)).isoformat()
    click.echo(f"=== Stale CONNECTION_SENT → UNREACHABLE remediation — {mode} ===")
    click.echo(f"  Threshold: last_contact_date < {cutoff_iso} (>{older_than_days} days old)")

    list_id = os.environ.get("ATTIO_LIST_ID", "")

    with AttioClient() as attio, MigrationRunWriter(
        script_name=f"scripts/{Path(__file__).name}",
        rollback_script_path=None,
        dry_run=not apply_changes,
        attio=attio,
    ) as run:
        cache = RecordCache(attio)

        all_parsed = _get_all_entries_parsed(attio)
        candidates = _select_stale(all_parsed, cutoff_iso)

        record_ids = {c["record_id"] for c in candidates if c.get("record_id")}
        preload_pipeline_persons(attio, cache, record_ids)

        click.echo(f"\nStale CONNECTION_SENT rows to park: {len(candidates)}")
        for c in candidates:
            name, _, _, _, _ = cache.get(c["record_id"])
            click.echo(
                f"  - {name or '?'} (record_id={c.get('record_id')!r}, "
                f"entry_id={c.get('entry_id')!r}, sent {c.get('last_contact_date', '?')})"
            )

        parked = 0
        skipped_drifted = 0
        skipped_no_entry = 0
        failures: list[dict] = []

        if not candidates:
            click.echo("  Nothing to do — no stale CONNECTION_SENT rows.")
        elif not apply_changes:
            for _ in candidates:
                run.examine()
                run.skip_excluded(reason="dry-run — stage writes gated by --apply")
            click.echo(f"\nDRY-RUN: would park {len(candidates)} row(s). Re-run with --apply.")
        else:
            # Re-fetch live stages immediately before writing so a row a
            # concurrent daily run advanced (CONNECTION_SENT → ACCEPTED, etc.)
            # is dropped instead of regressed. The §3.15 monotonicity gate
            # would NOT catch this (UNREACHABLE rank 90 >= any active rank),
            # so the guard MUST live here.
            live_stage = {
                a["entry_id"]: a.get("stage", "")
                for a in _get_all_entries_parsed(attio)
                if a.get("entry_id")
            }
            writer = AttioWriter(attio=attio)
            for c in candidates:
                run.examine()
                entry_id = c.get("entry_id")
                if not entry_id:
                    skipped_no_entry += 1
                    run.skip_excluded(reason="no entry_id on candidate row")
                    continue
                current = live_stage.get(entry_id, c.get("stage", ""))
                if current != PipelineStage.CONNECTION_SENT.value:
                    skipped_drifted += 1
                    run.skip_excluded(
                        reason=f"stage drifted to {current!r} since fetch — not parking"
                    )
                    click.echo(
                        f"  ⚠ skip (drifted to {current!r}): "
                        f"record_id={c.get('record_id')!r} entry_id={entry_id!r}",
                        err=True,
                    )
                    continue
                try:
                    writer.apply(WriteIntent(
                        object="linkedin_outreach",
                        record_id=entry_id,
                        updates={"stage": PipelineStage.UNREACHABLE.value},
                        prior_values={"stage": PipelineStage.CONNECTION_SENT.value},
                        writer_module=WRITER_MODULE,
                        is_list_entry=True,
                        list_id=list_id,
                        companion_record_id=c.get("record_id"),
                    ))
                    parked += 1
                    run.mark_modified(record_id=entry_id, object="list_entry")
                    click.echo(
                        f"  ✓ parked UNREACHABLE: record_id={c.get('record_id')!r} "
                        f"entry_id={entry_id!r} (sent {c.get('last_contact_date', '?')})"
                    )
                except (AttioError, UnauthorizedAttioWriteError,
                        httpx.HTTPStatusError, httpx.RequestError,
                        ConnectionError, TimeoutError) as exc:
                    # Park-fail leaves the row at CONNECTION_SENT — it simply
                    # stays in the daily scan and re-escalates, which is the
                    # safe direction. Record loudly; never swallow silently.
                    # (AttioMonotonicityViolation subclasses AttioError, so a
                    # surprise stage regression is caught here too.)
                    failures.append({
                        "record_id": str(c.get("record_id")),
                        "entry_id": str(entry_id),
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                    run.mark_failed(record_id=entry_id, error=exc)
                    click.echo(
                        f"  ❌ park failed: record_id={c.get('record_id')!r} "
                        f"entry_id={entry_id!r} ({type(exc).__name__}: {exc})",
                        err=True,
                    )

            click.echo(
                f"\nParked {parked}/{len(candidates)} "
                f"(drifted-skipped {skipped_drifted}, no-entry-id {skipped_no_entry}, "
                f"failed {len(failures)})."
            )

        report = {
            "run_date": date.today().isoformat(),
            "mode": "apply" if apply_changes else "dry-run",
            "older_than_days": older_than_days,
            "cutoff": cutoff_iso,
            "candidates": [
                {
                    "record_id": str(c.get("record_id")),
                    "entry_id": str(c.get("entry_id")),
                    "last_contact_date": c.get("last_contact_date"),
                }
                for c in candidates
            ],
            "parked": parked,
            "skipped_drifted": skipped_drifted,
            "skipped_no_entry": skipped_no_entry,
            "failures": failures,
        }
        out = Path(__file__).resolve().parent.parent / "exports" / (
            f"stale-connection-sent-remediation-{date.today().isoformat()}-"
            f"{'apply' if apply_changes else 'dry-run'}.json"
        )
        out.parent.mkdir(exist_ok=True)
        try:
            out.write_text(json.dumps(report, indent=2))
            click.echo(f"Report: {out}")
        except OSError as exc:
            click.echo(
                f"❌ data ops complete; report write failed ({exc}); "
                "counts above are authoritative",
                err=True,
            )


if __name__ == "__main__":
    main()
