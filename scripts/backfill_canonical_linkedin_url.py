#!/usr/bin/env python3
"""PR-9a value backfill: populate canonical_linkedin_url for all LinkedIn Outreach entries.

Round-3 blocker B2 fix. PR-9.5 dedup-join depends on canonical_linkedin_url
being populated BEFORE it runs. This script bridges that gap.

# What it does

For each LinkedIn Outreach list entry:
  1. Reads the parent Person record's `linkedin` field (via person record fetch).
  2. Computes `canonical_linkedin_url` using clients/attio.py::_canonical_linkedin_url()
     (URL-decoded, no www., no trailing slash, lowercase).
  3. If the entry's canonical_linkedin_url is already set to the same value → skip
     (idempotent: rows_skipped_idempotent += 1).
  4. If the entry's canonical_linkedin_url differs OR is empty → PATCH via
     AttioWriter(WriteIntent(updates={"canonical_linkedin_url": value})).
  5. Entries with no resolvable linkedin URL on the parent Person record are
     logged with rows_failed=0 but skipped with a warning (§0 #9: explicit
     None, not silent fallback; the PR-9.5 dedup-join will simply exclude
     those rows).

# Idempotency contract (§9.4)

Second consecutive run: canonical form is deterministic from the Person's
`linkedin` field → computes same value → skips every row →
  rows_modified=0, rows_skipped_idempotent=N, rows_failed=0.

# Scale

~6500 rows as of 2026-05-21. Fetches all entries once (auto-paginated),
then bulk-prefetches the Person records (8-concurrent per RecordCache
preload pattern), then patches in serial. Estimated wall time ~3-5 min.

# Usage

    python3 scripts/backfill_canonical_linkedin_url.py [--dry-run] [--verbose]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clients.attio import AttioClient, _canonical_linkedin_url  # noqa: E402
from clients.attio_writer import AttioWriter, WriteIntent  # noqa: E402
from workflows.migration_run_writer import MigrationRunWriter  # noqa: E402
from workflows.record_cache import RecordCache, preload_pipeline_persons  # noqa: E402

SCRIPT_VERSION = "pr-9a-v1"
WRITER_MODULE = "scripts.backfill_canonical_linkedin_url"
LINKEDIN_OUTREACH_LIST_ID_ENV = "ATTIO_LIST_ID"


def _get_list_id() -> str:
    list_id = os.environ.get(LINKEDIN_OUTREACH_LIST_ID_ENV, "").strip()
    if list_id:
        return list_id
    raise RuntimeError(
        f"LinkedIn Outreach list ID not set. "
        f"Export {LINKEDIN_OUTREACH_LIST_ID_ENV}=<uuid> and retry."
    )


def _compute_canonical(person_linkedin_url: str) -> str | None:
    """Return the canonical form of a person's linkedin URL, or None if not computable."""
    if not person_linkedin_url:
        return None
    result = _canonical_linkedin_url(person_linkedin_url)
    return result or None


def _read_existing_canonical(entry: dict) -> str | None:
    """Read the existing canonical_linkedin_url value from a list entry, if any."""
    values = entry.get("entry_values", {})
    canonical_data = values.get("canonical_linkedin_url", [])
    if canonical_data and isinstance(canonical_data, list):
        item = canonical_data[0]
        if isinstance(item, dict):
            return item.get("value") or None
        return item if isinstance(item, str) else None
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    try:
        attio = AttioClient()
    except KeyError:
        print("error: ATTIO_API_KEY env var not set", file=sys.stderr)
        return 2

    try:
        list_id = _get_list_id()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        f"PR-9a canonical_linkedin_url backfill "
        f"({'dry-run' if args.dry_run else 'live'}) — fetching all entries..."
    )

    # ---- Step 1: Fetch all list entries ----
    all_entries = attio.query_list_entries(list_id=list_id, limit=100_000)
    print(f"  Fetched {len(all_entries)} linkedin_outreach entries.")

    if not all_entries:
        print("  No entries found — nothing to backfill.")
        return 0

    # ---- Step 2: Bulk-prefetch parent Person records ----
    record_ids: set[str] = set()
    for entry in all_entries:
        rid = (
            entry.get("parent_record_id")
            or entry.get("id", {}).get("record_id", "")
        )
        if rid:
            record_ids.add(rid)

    cache = RecordCache(attio)
    primed = preload_pipeline_persons(attio, cache, record_ids)
    print(f"  Primed {primed} person records (out of {len(record_ids)} unique parent IDs).")

    # ---- Step 3: Iterate entries and backfill ----
    writer = AttioWriter(attio=attio)

    no_linkedin_count = 0
    already_correct_count = 0

    with MigrationRunWriter(
        script_name=Path(__file__).name,
        script_version=SCRIPT_VERSION,
        rollback_script_path=None,  # value write is effectively irreversible
        dry_run=args.dry_run,
        attio=attio,
    ) as run:
        for entry in all_entries:
            run.examine()

            entry_id: str = (
                entry.get("entry_id")
                or entry.get("id", {}).get("entry_id", "")
            )
            record_id: str = (
                entry.get("parent_record_id")
                or entry.get("id", {}).get("record_id", "")
            )

            if not entry_id or not record_id:
                run.mark_failed(
                    record_id=entry_id or "unknown",
                    error="Missing entry_id or parent_record_id",
                )
                if args.verbose:
                    print("  WARN: entry missing ID fields, skipping.", file=sys.stderr)
                continue

            # Get person's linkedin URL
            _, _, linkedin_url, _, _ = cache.get(record_id)
            computed = _compute_canonical(linkedin_url)

            if computed is None:
                # §0 #9: explicit None, not silent fallback.
                # These rows will be excluded from PR-9.5 canonical-join.
                no_linkedin_count += 1
                run.skip_idempotent()
                if args.verbose:
                    print(
                        f"  SKIP entry_id={entry_id}: "
                        f"no linkedin URL on parent person (record_id={record_id})"
                    )
                continue

            # Check existing value for idempotency
            existing = _read_existing_canonical(entry)
            if existing == computed:
                already_correct_count += 1
                run.skip_idempotent()
                if args.verbose:
                    print(f"  SKIP entry_id={entry_id}: already set to {computed!r}")
                continue

            # Write the new canonical URL
            if args.dry_run:
                run.mark_modified(record_id=entry_id, object="schema")
                if args.verbose or run.rows_modified <= 10:
                    print(
                        f"  [dry-run] WOULD SET entry_id={entry_id}: "
                        f"{existing!r} → {computed!r}"
                    )
            else:
                try:
                    writer.apply(WriteIntent(
                        object="linkedin_outreach",
                        record_id=entry_id,
                        updates={"canonical_linkedin_url": computed},
                        prior_values={"canonical_linkedin_url": existing},
                        writer_module=WRITER_MODULE,
                        is_list_entry=True,
                        list_id=list_id,
                    ))
                    # Use object="schema" so MigrationRunWriter skips the
                    # back-pointer PATCH. List entry_ids are not valid
                    # object record_ids — patching /objects/linkedin_outreach/records/{entry_id}
                    # would always 404. The Migration Run row still captures the correct
                    # rows_modified count; the back-pointer is a forensics convenience only.
                    run.mark_modified(record_id=entry_id, object="schema")
                    if args.verbose:
                        print(
                            f"  SET entry_id={entry_id}: "
                            f"{existing!r} → {computed!r}"
                        )
                except Exception as exc:
                    run.mark_failed(record_id=entry_id, error=exc)
                    print(
                        f"  FAIL entry_id={entry_id}: {exc}",
                        file=sys.stderr,
                    )

    print(
        f"\nPR-9a canonical_linkedin_url backfill complete "
        f"({'dry-run' if args.dry_run else 'live'}):\n"
        f"  rows_examined={run.rows_examined}\n"
        f"  rows_modified={run.rows_modified}\n"
        f"  rows_skipped_idempotent={run.rows_skipped_idempotent} "
        f"(of which {already_correct_count} already had correct value, "
        f"{no_linkedin_count} had no linkedin URL)\n"
        f"  rows_failed={run.rows_failed}"
    )
    if run.rows_failed > 0:
        print(
            f"\nWARNING: {run.rows_failed} row(s) failed. "
            f"Run again to retry, or inspect failure_details in the Migration Run row.",
            file=sys.stderr,
        )
    return 1 if run.rows_failed > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
