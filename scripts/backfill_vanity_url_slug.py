#!/usr/bin/env python3
"""PR-14 value backfill: populate vanity_url_slug for all LinkedIn Outreach entries.

# What it does

For each LinkedIn Outreach list entry:
  1. Read the entry's existing `canonical_linkedin_url`
     (populated by PR-9a's value backfill — this script depends on
     that prerequisite per §11 sequence).
  2. Compute `vanity_url_slug` via `clients/attio.py::_vanity_url_slug`
     (e.g. `https://linkedin.com/in/mateo-lt-12345` → `mateo-lt-12345`).
  3. If the entry's `vanity_url_slug` is already set to the same value → skip
     (idempotent: rows_skipped_idempotent += 1).
  4. If the entry's `vanity_url_slug` differs OR is empty → PATCH via
     AttioWriter.
  5. Entries with no `canonical_linkedin_url` (PR-9a couldn't resolve
     the parent Person's LinkedIn URL) are skipped with rows_skipped
     and a warning — §0 #9: explicit empty, not silent fallback. PR-9.5
     dedup-join already excludes those rows from grouping anyway.

# Idempotency contract (§9.4)

Second consecutive run: the canonical URL is unchanged → same vanity
slug → skips every row →
  rows_modified=0, rows_skipped_idempotent=N, rows_failed=0.

# §3.15 multi-writer registry

`vanity_url_slug` is registered (in F-PR-3.7's registry) as multi-writer:
  - `workflows.weekly_prospect._build_prospect_entry_attrs` (PROSPECT-commit
    new entries — when a fresh row is committed weekly)
  - `scripts.backfill_vanity_url_slug` (this script — one-shot historical
    backfill against existing rows)

Both writers must agree on the canonical form. Both call
`clients/attio.py::_vanity_url_slug` so the output is deterministic from
`canonical_linkedin_url`.

# Scale

~6500 rows as of 2026-05-22 (current LinkedIn Outreach list count).
This script does NOT need to fetch parent Person records — the
canonical URL already lives on each entry. Estimated wall time ~2-3 min.

# Usage

    python3 scripts/backfill_vanity_url_slug.py [--dry-run] [--verbose]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clients.attio import AttioClient, _vanity_url_slug  # noqa: E402
from clients.attio_writer import AttioWriter, WriteIntent  # noqa: E402
from workflows.migration_run_writer import MigrationRunWriter  # noqa: E402

SCRIPT_VERSION = "pr-14-v1"
WRITER_MODULE = "scripts.backfill_vanity_url_slug"
LINKEDIN_OUTREACH_LIST_ID_ENV = "ATTIO_LIST_ID"


def _get_list_id() -> str:
    list_id = os.environ.get(LINKEDIN_OUTREACH_LIST_ID_ENV, "").strip()
    if list_id:
        return list_id
    raise RuntimeError(
        f"LinkedIn Outreach list ID not set. "
        f"Export {LINKEDIN_OUTREACH_LIST_ID_ENV}=<uuid> and retry."
    )


def _read_existing_text_attr(entry: dict, slug: str) -> str:
    """Read a text attribute from a list entry's entry_values block.

    Returns '' when the attribute is unset, null, or empty-string-stored.
    """
    values = entry.get("entry_values", {}) or {}
    data = values.get(slug, [])
    if not (data and isinstance(data, list)):
        return ""
    first = data[0]
    if isinstance(first, dict):
        return str(first.get("value", "") or "")
    return str(first or "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compute + log changes without writing to Attio.",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print one line per processed entry.",
    )
    args = parser.parse_args()

    list_id = _get_list_id()
    attio = AttioClient()

    # ---- Step 1: Fetch all LinkedIn Outreach entries (auto-paginated) ----
    print("PR-14 vanity_url_slug backfill")
    print(f"  list_id={list_id}, dry_run={args.dry_run}")
    print("  Fetching all LinkedIn Outreach entries...")
    all_entries = attio.query_list_entries(list_id=list_id)
    print(f"  Loaded {len(all_entries)} entries.")

    # ---- Step 2: Iterate entries and backfill ----
    writer = AttioWriter(attio=attio)
    no_canonical_count = 0
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
            if not entry_id:
                run.mark_failed(
                    record_id="unknown",
                    error="Missing entry_id",
                )
                if args.verbose:
                    print("  WARN: entry missing entry_id, skipping.", file=sys.stderr)
                continue

            # Read PR-9a's `canonical_linkedin_url` from the entry itself.
            canonical = _read_existing_text_attr(entry, "canonical_linkedin_url")
            if not canonical:
                # §0 #9: explicit empty, not silent fallback. Either
                # PR-9a couldn't resolve the parent Person's URL, OR
                # this entry pre-dates PR-9a's backfill. Skip and let
                # the operator triage by re-running PR-9a backfill.
                no_canonical_count += 1
                run.skip_idempotent()
                if args.verbose:
                    print(
                        f"  SKIP entry_id={entry_id}: canonical_linkedin_url "
                        f"is unset — PR-9a backfill prerequisite."
                    )
                continue

            computed = _vanity_url_slug(canonical)
            if not computed:
                # The canonical URL exists but isn't a /in/ profile URL
                # (company page, group URL, etc.). Mark as failed for
                # operator visibility — a non-profile URL on a LinkedIn
                # Outreach entry is data corruption.
                run.mark_failed(
                    record_id=entry_id,
                    error=f"canonical_linkedin_url={canonical!r} is not a /in/ profile URL",
                )
                if args.verbose:
                    print(
                        f"  FAIL entry_id={entry_id}: non-profile URL "
                        f"({canonical!r})",
                        file=sys.stderr,
                    )
                continue

            existing = _read_existing_text_attr(entry, "vanity_url_slug")
            if existing == computed:
                already_correct_count += 1
                run.skip_idempotent()
                if args.verbose:
                    print(f"  SKIP entry_id={entry_id}: already set to {computed!r}")
                continue

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
                        updates={"vanity_url_slug": computed},
                        prior_values={"vanity_url_slug": existing},
                        writer_module=WRITER_MODULE,
                        is_list_entry=True,
                        list_id=list_id,
                    ))
                    # Use object="schema" so MigrationRunWriter skips the
                    # back-pointer PATCH per the PR-9a backfill precedent
                    # (list entry_ids aren't valid object record_ids).
                    run.mark_modified(record_id=entry_id, object="schema")
                    if args.verbose:
                        print(
                            f"  SET entry_id={entry_id}: "
                            f"{existing!r} → {computed!r}"
                        )
                except Exception as exc:  # noqa: BLE001
                    run.mark_failed(record_id=entry_id, error=exc)
                    print(
                        f"  FAIL entry_id={entry_id}: {exc}",
                        file=sys.stderr,
                    )

    print(
        f"\nPR-14 vanity_url_slug backfill complete "
        f"({'dry-run' if args.dry_run else 'live'}):\n"
        f"  rows_examined={run.rows_examined}\n"
        f"  rows_modified={run.rows_modified}\n"
        f"  rows_skipped_idempotent={run.rows_skipped_idempotent} "
        f"(of which {already_correct_count} already had correct value, "
        f"{no_canonical_count} had no canonical_linkedin_url)\n"
        f"  rows_failed={run.rows_failed}"
    )
    return 1 if run.rows_failed > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
