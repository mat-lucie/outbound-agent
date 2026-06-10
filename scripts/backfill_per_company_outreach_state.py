#!/usr/bin/env python3
"""PR-13 backfill: populate Companies per-company outreach state from historical DMs.

Per plan §9 line 1079: after PR-13 (where daily_check::run_dm_sequencing
and run_connection_requests began incrementally populating `last_outreach_at`,
`last_outreach_person_id`, `last_outreach_step`, `last_outreach_experiment_id`
on each CONFIRMED send), rows from BEFORE that PR landed don't have these
attrs populated.

This backfill script examines all historical LinkedIn Outreach list entries
and stamps the Company record with the most-advanced outreach state for each
prospect cohort.

For each Company:
  1. Find all LinkedIn Outreach entries linked to it
  2. Sort by (dm_step DESC, last_contact_date DESC)
  3. Take the most-advanced entry
  4. Extract: last_outreach_at, last_outreach_person_id, last_outreach_step,
     last_outreach_experiment_id
  5. Write to Company IF currently NULL (idempotent: §9.4)

# Idempotency contract (§9.4)

Second consecutive run: Companies already at populated state read
`prior_val` and apply_update returns False (skip).  Resulting Migration Run row:
``rows_modified=0, rows_skipped_idempotent=N, rows_failed=0``.

# Usage

    python3 scripts/backfill_per_company_outreach_state.py [--dry-run] [--apply] [--limit 2000] [--verbose]

--dry-run and --apply are mutually exclusive (exit 2 if neither/both).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clients.attio import AttioClient
from workflows.daily_check_helpers import _get_all_entries_parsed
from workflows.migration_run_writer import MigrationRunWriter

SCRIPT_VERSION = "pr-13-v1"
WRITER_MODULE = "scripts.backfill_per_company_outreach_state"

# dm_step is stored on linkedin_outreach as an integer (0-3); parse_entry
# returns the int verbatim. The companies.last_outreach_step select attribute
# uses string option titles. _STEP_LABEL maps the integer to the select
# option for the write payload.
_STEP_LABEL = {
    0: "CONNECTION_SENT",
    1: "DM1",
    2: "DM2",
    3: "DM3",
}


def main():
    parser = argparse.ArgumentParser(
        description="PR-13 backfill: per-company outreach state from DM history"
    )
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate the backfill without writing to Attio"
    )
    mode_group.add_argument(
        "--apply",
        action="store_true",
        help="Actually write to Attio"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max companies to process (None = all, capped at 2000 auto-pagination)"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print detailed progress"
    )

    args = parser.parse_args()
    dry_run = args.dry_run
    company_limit = args.limit or 2000

    # Initialize Attio client
    attio = AttioClient()

    # Fetch all companies and entries
    if args.verbose:
        print(f"[{SCRIPT_VERSION}] Fetching companies (limit={company_limit})...")
    companies = attio.search_companies(filter_=None, limit=company_limit)

    if args.verbose:
        print(f"[{SCRIPT_VERSION}] Fetching all LinkedIn Outreach entries...")
    all_entries = _get_all_entries_parsed(attio)

    # Build per-company index: company_id -> list[entry].
    # PR-13 fix-up: parse_entry returns the person's record_id (as `record_id`),
    # not a company_id. Resolve the person→company reference via bulk
    # people-fetch; mirrors scripts/diagnose_throttle_violations_20260525.py.
    person_ids = {e.get("record_id") for e in all_entries if e.get("record_id")}
    if args.verbose:
        print(f"[{SCRIPT_VERSION}] Resolving person→company refs for {len(person_ids)} persons...")
    persons = attio.bulk_fetch_persons_by_record_ids(person_ids)
    # QA fix-up (C1): bulk_fetch may silently drop persons (deleted record,
    # rate-limit truncation, etc.). Surface the gap before it becomes silent
    # data loss: those persons' entries will not contribute to any company
    # bucket below, so their companies will look history-less.
    missing_persons = person_ids - set(persons.keys())
    if missing_persons:
        print(
            f"[{SCRIPT_VERSION}] WARNING: bulk_fetch returned {len(missing_persons)} of "
            f"{len(person_ids)} requested persons missing — their entries will not be "
            f"indexed and their companies may be silently under-stamped.",
            file=sys.stderr,
        )
        if args.verbose:
            sample = sorted(missing_persons)[:10]
            print(
                f"  sample missing person_ids: {sample}{' …' if len(missing_persons) > 10 else ''}",
                file=sys.stderr,
            )
    person_to_companies: dict[str, list[str]] = {}
    for rid, record in persons.items():
        values = (record or {}).get("values", {})
        company_refs = values.get("company") or []
        if not isinstance(company_refs, list):
            continue
        person_to_companies[rid] = [
            ref.get("target_record_id")
            for ref in company_refs
            if isinstance(ref, dict) and ref.get("target_record_id")
        ]

    entry_by_company: dict[str, list[dict]] = {}
    for entry in all_entries:
        person_record_id = entry.get("record_id")
        if not person_record_id:
            continue
        for company_id in person_to_companies.get(person_record_id, ()):
            entry_by_company.setdefault(company_id, []).append(entry)

    if args.verbose:
        print(f"[{SCRIPT_VERSION}] Built index: {len(entry_by_company)} companies with outreach history")

    # Open migration writer per §3.13 contract. The writer takes
    # `script_name` (file/module basename) + `script_version` (the
    # per-script version tag); `writer_module` is NOT part of its API.
    with MigrationRunWriter(
        script_name=WRITER_MODULE,
        script_version=SCRIPT_VERSION,
        dry_run=args.dry_run,
        attio=attio,
    ) as run:
        processed = 0
        for company in companies:
            # Attio API returns `id` as a nested object: {"workspace_id":..., "object_id":..., "record_id":"<uuid>"}.
            # The test mocks previously used a string here, masking this for the live script.
            company_id = company.get("id", {}).get("record_id")
            processed += 1

            run.examine()

            # QA fix-up (C2): defend against a malformed Attio response where
            # `id.record_id` is missing. Without this guard `company_id=None`
            # would propagate into entry_by_company.get(None, []) (always empty)
            # → silently bucketed as "no_outreach_history" with the wrong reason.
            if not company_id:
                run.skip_excluded(reason="missing_record_id")
                if args.verbose:
                    print(
                        f"  [{processed}] company missing id.record_id (raw id={company.get('id')!r}), skipping",
                        file=sys.stderr,
                    )
                continue

            # Check if already populated (idempotent)
            values = company.get("values", {})
            if values.get("last_outreach_at"):
                run.skip_idempotent()
                if args.verbose:
                    print(f"  [{processed}] {company_id}: already populated, skipping")
                continue

            # Check if this company has any outreach history
            entries_for_company = entry_by_company.get(company_id, [])
            if not entries_for_company:
                run.skip_excluded(reason="no_outreach_history")
                if args.verbose:
                    print(f"  [{processed}] {company_id}: no outreach entries, skipping")
                continue

            # Find the most-advanced entry by (dm_step DESC, last_contact_date DESC).
            # dm_step is an int; treat missing/None as -1 so any real step beats it.
            def sort_key(entry: dict) -> tuple:
                step = entry.get("dm_step")
                step_rank = step if isinstance(step, int) else -1
                last_contact = entry.get("last_contact_date") or ""
                return (step_rank, last_contact)

            best_entry = max(entries_for_company, key=sort_key)

            # Extract the 4 attrs. parse_entry returns:
            #   - `record_id` (person record_id; parent_record_id on the list entry)
            #   - `dm_step` as an int (0=CONNECTION_SENT … 3=DM3)
            #   - `last_contact_date` as a date-only string (YYYY-MM-DD)
            # Companies.last_outreach_at is a timestamp; Companies.last_outreach_step
            # is a select with string options. Convert before writing.
            dm_step_int = best_entry.get("dm_step")
            last_contact_date = best_entry.get("last_contact_date")

            # Genuinely incomplete state — no dm_step OR no last_contact_date.
            # Skip rather than write half-state (e.g. a Prospect-stage entry
            # that was added but never sent).
            if not isinstance(dm_step_int, int) or not last_contact_date:
                run.skip_excluded(reason="incomplete_outreach_state")
                if args.verbose:
                    print(
                        f"  [{processed}] {company_id}: incomplete state "
                        f"(dm_step={dm_step_int!r}, last_contact_date={last_contact_date!r}), skipping"
                    )
                continue

            # QA fix-up (C3): dm_step is an int but not in {0,1,2,3} — anomaly,
            # not "incomplete". Audit as a failure so future DM4 or corrupt -1
            # sentinels don't silently disappear into the incomplete-state bucket.
            last_outreach_step = _STEP_LABEL.get(dm_step_int)
            if last_outreach_step is None:
                run.mark_failed(
                    record_id=company_id,
                    error=f"dm_step out of range: {dm_step_int!r} not in {sorted(_STEP_LABEL)}",
                )
                print(
                    f"  ERROR [{processed}] {company_id}: dm_step={dm_step_int!r} out of range",
                    file=sys.stderr,
                )
                continue

            # Promote date-only last_contact_date (YYYY-MM-DD) to ISO timestamp
            # at midnight UTC. last_outreach_at is a timestamp attribute.
            last_outreach_at = (
                last_contact_date
                if "T" in last_contact_date
                else f"{last_contact_date}T00:00:00Z"
            )
            last_outreach_person_id = best_entry.get("record_id")
            last_outreach_experiment_id = best_entry.get("experiment_id") or ""

            # Build the update payload (matching daily_check.py:232-240 exactly)
            attrs_to_write = {
                "last_outreach_at": last_outreach_at,
                "last_outreach_person_id": [
                    {
                        "target_object": "people",
                        "target_record_id": last_outreach_person_id,
                    }
                ] if last_outreach_person_id else [],
                "last_outreach_step": last_outreach_step,
                "last_outreach_experiment_id": last_outreach_experiment_id,
            }

            # Write (or simulate)
            if dry_run:
                run.mark_modified(record_id=company_id, object="companies")
                if args.verbose:
                    print(
                        f"  [{processed}] {company_id}: would write "
                        f"step={last_outreach_step} person={last_outreach_person_id}"
                    )
            else:
                try:
                    attio.update_company(company_id, attrs_to_write)
                    run.mark_modified(record_id=company_id, object="companies")
                    if args.verbose:
                        print(
                            f"  [{processed}] {company_id}: wrote "
                            f"step={last_outreach_step} person={last_outreach_person_id}"
                        )
                except Exception as exc:
                    run.mark_failed(record_id=company_id, error=str(exc))
                    print(
                        f"  ERROR [{processed}] {company_id}: {type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )

        # Print summary
        print(
            f"\n[{SCRIPT_VERSION}] Migration complete:\n"
            f"  Examined: {run.rows_examined}\n"
            f"  Modified: {run.rows_modified}\n"
            f"  Skipped (idempotent): {run.rows_skipped_idempotent}\n"
            f"  Skipped (excluded):   {run.rows_skipped_excluded}\n"
            f"  Failed: {run.rows_failed}"
        )

        if dry_run:
            print("  (dry-run: no writes executed)")


if __name__ == "__main__":
    main()
