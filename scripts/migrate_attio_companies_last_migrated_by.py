#!/usr/bin/env python3
"""Add the `last_migrated_by` provenance attribute to the companies object (#18).

# Why

Plan §3.13 requires every migrated record to carry `last_migrated_by` -> its
Migration Run row. The attribute was only ever created on the
`linkedin_outreach` list (F-PR-3.7), because that was the only migration
target at the time. The Wave-2 per-company throttle made `companies` a
migration target too: scripts/backfill_per_company_outreach_state.py calls
`mark_modified(object="companies")`, so MigrationRunWriter._stamp_back_pointers
PATCHes `companies.last_migrated_by` -- an attribute that did not exist, so
all 247 back-pointer stamps failed (forensic-only: the primary writes and the
Migration Run row were intact).

This script creates `companies.last_migrated_by` (record_reference ->
migration_run), mirroring the linkedin_outreach attribute, so company-targeted
migrations record their provenance back-pointer going forward.

# Running

    # Preview (default behaviour):
    python scripts/migrate_attio_companies_last_migrated_by.py
    python scripts/migrate_attio_companies_last_migrated_by.py --dry-run

    # Live deploy (writes to production Attio):
    python scripts/migrate_attio_companies_last_migrated_by.py --apply

DEFAULTS TO DRY-RUN. An explicit `--apply` flag is required to write to
production Attio. Idempotent: a second `--apply` run skips (the attribute
already exists). This script does NOT re-stamp the historical 247 companies
whose back-pointers failed before the fix -- that is an optional follow-up.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clients.attio import AttioClient  # noqa: E402
from scripts._attio_migration_helpers import ensure_attribute  # noqa: E402
from workflows.migration_run_writer import MigrationRunWriter  # noqa: E402

SCRIPT_VERSION = "1.0"
TARGET_OBJECT = "companies"
ATTR_SLUG = "last_migrated_by"
REFERENCED_OBJECT = "migration_run"
ATTR_DESCRIPTION = (
    "Provenance pointer to the Migration Run that last modified this company. "
    "Mirrors linkedin_outreach.last_migrated_by per plan section 3.13."
)


def deploy(attio: object, *, dry_run: bool, run: MigrationRunWriter) -> str:
    """Create companies.last_migrated_by if missing.

    Returns the ensure_attribute action: ``created`` | ``would_create`` |
    ``skipped`` (idempotent re-run). The schema mutation is logged on the
    Migration Run row via ``object="schema"`` -- the sentinel that skips the
    back-pointer PATCH (there is no target record to stamp).
    """
    run.examine()
    action = ensure_attribute(
        attio,
        "object",
        TARGET_OBJECT,
        ATTR_SLUG,
        "record_reference",
        is_required=False,
        description=ATTR_DESCRIPTION,
        referenced_object=REFERENCED_OBJECT,
        dry_run=dry_run,
    )
    if action == "skipped":
        run.skip_idempotent()
    else:
        run.mark_modified(record_id=f"{TARGET_OBJECT}.{ATTR_SLUG}", object="schema")
    return action


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--apply", action="store_true",
        help="Live deploy: write the attribute to production Attio. Default is dry-run.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Explicit dry-run (default; equivalent to omitting --apply).",
    )
    args = parser.parse_args(argv)

    # Default is dry-run. Live deploy requires explicit --apply.
    dry_run = not args.apply

    try:
        attio = AttioClient()
    except KeyError:
        print("error: ATTIO_API_KEY env var not set", file=sys.stderr)
        return 2

    action = "failed"
    with MigrationRunWriter(
        script_name=Path(__file__).name,
        script_version=SCRIPT_VERSION,
        rollback_script_path=None,  # additive attr; Attio attr removal is operator-manual-only
        dry_run=dry_run,
        attio=attio,
    ) as run:
        try:
            action = deploy(attio, dry_run=dry_run, run=run)
        except Exception as exc:
            run.mark_failed(record_id=f"{TARGET_OBJECT}.{ATTR_SLUG}", error=exc)
            print(
                f"attribute {TARGET_OBJECT}.{ATTR_SLUG} failed: {exc}",
                file=sys.stderr,
            )
            action = "failed"

    print(json.dumps({
        "dry_run": dry_run,
        "object": TARGET_OBJECT,
        "attribute": ATTR_SLUG,
        "referenced_object": REFERENCED_OBJECT,
        "action": action,
    }, indent=2))
    return 0 if action != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
