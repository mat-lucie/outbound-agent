#!/usr/bin/env python3
"""Create the Attio `Data Quality Report` object + its attributes
(bootstrap migration).

Wraps in `MigrationRunWriter` per §3.13 — every schema-mutating script
must produce a Migration Run audit row. Schema-level changes use
`object='schema'` so `last_migrated_by` back-pointers skip (no target
record exists).

# Idempotency

Re-runs are no-ops:
  * Object create: if `data_quality_report` already exists, skip.
  * Attribute create: per attribute, if it exists with the same type,
    skip; if different type, fail loud (Attio type migrations are
    irreversible — operator must resolve via UI).

# Reversibility

Attio object deletion is UI-only; `rollback_script_path=None`. The
Migration Run row captures the create body so a manual rollback has
the exact args needed to recreate.

# Running

    python scripts/migrate_attio_data_quality_report.py
    python scripts/migrate_attio_data_quality_report.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from clients.attio import AttioClient  # noqa: E402
from scripts._attio_migration_helpers import (  # noqa: E402
    ensure_attribute,
    ensure_object,
)
from workflows.migration_run_writer import MigrationRunWriter  # noqa: E402

OBJECT_SLUG = "data_quality_report"
OBJECT_PLURAL = "Data Quality Reports"
OBJECT_SINGULAR = "Data Quality Report"

# Spec: (slug, type, description, is_required). Mirrors the
# scripts.data_quality_report.write_report attribute set + the schema
# manifest entries in docs/attio_schema_deltas.yaml.
ATTRIBUTES: list[tuple[str, str, str, bool]] = [
    ("run_id", "text", "Unique id per DQR run.", True),
    ("generated_at", "datetime", "When the DQR was computed.", True),
    ("period_start", "date", "Inclusive start of the report window.", True),
    ("period_end", "date", "Inclusive end of the report window.", True),
    ("cohort_tagging_regression_count", "number",
     "P0 alarm count — open queue rows of this type.", True),
    ("write_owner_invariant_violated_count", "number",
     "P0 alarm count.", True),
    ("migration_idempotency_regression_count", "number",
     "P0 alarm count.", True),
    ("manual_reply_classification_gap_count", "number",
     "P1 alarm count. Threshold synced with §10 halt (M7).", True),
    ("nurture_silent_skipped_count_7d", "number",
     "Sum from Daily Run rows in the window.", True),
    ("nurture_count_parse_errors_7d", "number",
     "Daily Run rows whose nurture_silent_skipped_count failed int() parse.", True),
    ("pipeline_starvation_open_count", "number",
     "Open queue rows of type='pipeline_starvation' (PR-43).", True),
    ("back_pointer_failures_count_7d", "number",
     "Migration Run rows whose failure_details_pointer mentions back-pointer failures.", True),
    ("legacy_archaeology_pool_count", "number",
     "LinkedIn Outreach entries whose experiment_id_frozen_at is a legacy_* sentinel.", True),
    ("p0_alarms_fired", "text",
     "Comma-joined slugs of P0 alarms that fired this run.", False),
    ("p1_alarms_fired", "text",
     "Comma-joined slugs of P1 alarms that fired this run.", False),
    ("report_text", "long_text",
     "Pre-rendered human-readable report.", False),
]


# Schema-mutation helpers live in scripts/_attio_migration_helpers — single
# source of truth for the v2 API contract (type mapping, required POST
# fields, select-option backfill, etc.).


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would change without writing to Attio.",
    )
    args = parser.parse_args(argv)

    try:
        attio = AttioClient()
    except KeyError:
        print("error: ATTIO_API_KEY env var not set", file=sys.stderr)
        return 2

    with attio, MigrationRunWriter(
        attio=attio,
        script_name="migrate_attio_data_quality_report",
        script_version="PR-43.5-v1",
        rollback_script_path=None,
        dry_run=args.dry_run,
    ) as run:
        # ---- Object ----
        run.examine()
        obj_action, _create_body = ensure_object(
            attio, OBJECT_SLUG, OBJECT_SINGULAR, OBJECT_PLURAL,
            dry_run=args.dry_run,
        )
        print(f"object {OBJECT_SLUG}: {obj_action}")
        if obj_action == "skipped":
            run.skip_idempotent()
        else:
            run.mark_modified(record_id=OBJECT_SLUG, object="schema")

        # ---- Attributes ----
        for slug, type_, description, is_required in ATTRIBUTES:
            run.examine()
            try:
                action = ensure_attribute(
                    attio, "object", OBJECT_SLUG, slug, type_,
                    is_required=is_required, description=description,
                    dry_run=args.dry_run,
                )
            except Exception as exc:
                run.mark_failed(record_id=f"{OBJECT_SLUG}.{slug}", error=exc)
                print(f"attribute {slug}: FAILED ({exc})", file=sys.stderr)
                continue
            print(f"attribute {slug}: {action}")
            if action == "skipped":
                run.skip_idempotent()
            else:
                run.mark_modified(
                    record_id=f"{OBJECT_SLUG}.{slug}", object="schema",
                )

    # Surface failure to CI / shell `&&` chains.
    return 1 if run.rows_failed else 0


if __name__ == "__main__":
    sys.exit(main())
