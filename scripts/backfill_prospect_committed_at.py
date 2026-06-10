#!/usr/bin/env python3
"""Backfill `prospect_committed_at` + `invite_eligible_after` on existing
LinkedIn Outreach entries (§3.13 Migration Run protocol).

# Why this exists

Both attributes are a contract on every NEW PROSPECT-stage entry written
by `workflows.weekly_prospect._build_prospect_entry_attrs`. The several
thousand entries that pre-date that contract have no
`prospect_committed_at`. Without a backfill,
`workflows.starvation.evaluate_pipeline_starvation` would mis-report
`most_recent_commit=None` indefinitely, and any future caller that
pivots on `prospect_committed_at` would silently skip the legacy cohort.

# Fallback rules (best-effort, never null)

For each entry without `prospect_committed_at`:

  1. If `last_contact_date` is set → use that as the proxy for the
     PROSPECT-commit moment. It's the closest non-null signal we have
     for "this row entered the funnel around then". Accuracy: ±1 day.
  2. Else, use the entry's `created_at` timestamp (always non-null
     from Attio).

`invite_eligible_after` for backfilled rows = the proxy date itself.
That means `is_invite_eligible(entry, today_local)` returns True for
every backfilled row from day one — legacy prospects retain their
existing invite eligibility. The quarantine only applies to genuinely
fresh commits going forward.

# Idempotency

Rows whose `prospect_committed_at` is already set are counted via
`run.skip_idempotent()` and not touched. Re-running the script is a
no-op (verified by `rows_modified == 0` on the second run, per §9.4).

# Rollback

`rollback_script_path=None` — the backfill is best-effort and forward-
only. If a row's proxy date turns out to be wrong, the operator can
overwrite it manually; the §3.13 contract still holds because the
`last_migrated_by` back-pointer attributes any future audit query to
this Migration Run.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from clients.attio import AttioClient  # noqa: E402
from workflows.migration_run_writer import MigrationRunWriter  # noqa: E402


def _parse_proxy_date(raw: str | None) -> date | None:
    """Parse Attio's date / datetime strings into a date. Returns None on
    missing / unparseable input."""
    if not raw:
        return None
    text = str(raw)[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _entry_attrs(entry: dict) -> dict:
    """Flatten an Attio list-entry into the {slug: value} shape the
    backfill needs. Pulls from `entry_values` which is how list-entry
    attributes are returned by `/lists/.../entries/query`.
    """
    values = entry.get("entry_values") or entry.get("values") or {}
    flat: dict = {}
    for slug, items in values.items():
        if isinstance(items, list) and items:
            v = items[0]
            if isinstance(v, dict):
                # Multi-shape: text→value, date→value, etc.
                flat[slug] = v.get("value") or v.get("status", {}).get("title")
            else:
                flat[slug] = v
        elif isinstance(items, str | int | float | bool):
            flat[slug] = items
    return flat


def _backfill_prospect_committed_at(
    attio: AttioClient, run: MigrationRunWriter, *, list_id: str,
) -> dict:
    """Walk every entry in the LinkedIn Outreach list, fill in
    `prospect_committed_at` + `invite_eligible_after` when missing.
    Returns a summary dict."""
    entries = attio.query_list_entries(list_id=list_id, limit=50000)

    summary = {
        "scanned": 0,
        "needed_backfill": 0,
        "filled_from_last_contact_date": 0,
        "filled_from_created_at": 0,
    }

    for entry in entries:
        run.examine()
        summary["scanned"] += 1
        flat = _entry_attrs(entry)
        if flat.get("prospect_committed_at"):
            run.skip_idempotent()
            continue
        summary["needed_backfill"] += 1

        proxy_date: date | None = _parse_proxy_date(flat.get("last_contact_date"))
        if proxy_date is not None:
            summary["filled_from_last_contact_date"] += 1
        else:
            proxy_date = _parse_proxy_date(entry.get("created_at"))
            if proxy_date is not None:
                summary["filled_from_created_at"] += 1

        if proxy_date is None:
            # No usable signal — leave it alone rather than guess. Counts
            # as failed so the operator can audit the gap.
            run.mark_failed(
                record_id=entry.get("id", {}).get("entry_id", "?"),
                error=ValueError(
                    "no last_contact_date or created_at available; "
                    "cannot backfill prospect_committed_at without a proxy"
                ),
            )
            continue

        entry_id = entry.get("id", {}).get("entry_id", "")
        # Proxy time is noon-UTC of the proxy date so it sorts after any
        # legitimately backdated commit but stays semantically "that day".
        committed_at_iso = datetime(
            proxy_date.year, proxy_date.month, proxy_date.day, 12, 0, 0,
        ).isoformat() + "Z"
        # Backfilled rows are released from quarantine immediately:
        # invite_eligible_after = proxy date itself so is_invite_eligible
        # returns True from day one.
        invite_eligible_after_iso = proxy_date.isoformat()

        if run.dry_run:
            run.mark_modified(record_id=entry_id, object="schema")
            continue

        try:
            attio.update_list_entry(
                entry_id=entry_id,
                entry_attributes={
                    "prospect_committed_at": committed_at_iso,
                    "invite_eligible_after": invite_eligible_after_iso,
                },
                list_id=list_id,
            )
        except Exception as exc:
            run.mark_failed(record_id=entry_id, error=exc)
            continue

        # Note: the back-pointer (last_migrated_by) targets the parent
        # `people` record, not the list-entry. The list-entry's
        # record_id is the actual person row. Look it up from
        # parent_record.
        parent = entry.get("parent_record_id") or entry.get(
            "parent", {},
        ).get("record_id", "") or entry_id
        run.mark_modified(record_id=parent, object="people")

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be backfilled without writing to Attio.",
    )
    parser.add_argument(
        "--list-id",
        default=os.environ.get("ATTIO_LIST_ID", ""),
        help="LinkedIn Outreach list id (default: ATTIO_LIST_ID env).",
    )
    args = parser.parse_args(argv)

    if not args.list_id:
        print(
            "error: --list-id required (or set ATTIO_LIST_ID env)",
            file=sys.stderr,
        )
        return 2

    with AttioClient() as attio, MigrationRunWriter(
        attio=attio,
        script_name="backfill_prospect_committed_at",
        script_version="PR-43-v1",
        rollback_script_path=None,
        dry_run=args.dry_run,
    ) as run:
        summary = _backfill_prospect_committed_at(
            attio, run, list_id=args.list_id,
        )

    print(
        f"backfill summary (dry_run={args.dry_run}): {summary}; "
        f"rows_examined={run.rows_examined} "
        f"rows_modified={run.rows_modified} "
        f"rows_skipped_idempotent={run.rows_skipped_idempotent} "
        f"rows_failed={run.rows_failed}",
    )
    # Non-zero exit on any per-row failure so CI / shell `&&` chains
    # don't proceed on a partial backfill. Lesson from the pytest-pipe
    # masking incident: a quiet exit-0 on broken data lets downstream
    # steps ship state they shouldn't trust.
    return 1 if run.rows_failed else 0


if __name__ == "__main__":
    sys.exit(main())
