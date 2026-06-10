#!/usr/bin/env python3
"""Create Reclassification Run + Migration Run Attio objects, and the
two new linkedin_outreach attrs (last_classified_by, last_migrated_by).
F-PR-3.7 bootstrap migration.

# Why this script is special

This migration creates the very objects (Reclassification Run +
Migration Run) that downstream migrations will use to log themselves.
It must therefore run BEFORE any other migration uses
`MigrationRunWriter` — but it ALSO uses MigrationRunWriter itself,
to demonstrate the contract and to be self-logging.

The chicken-and-egg is resolved by ordering:
  1. This script's `_ensure_objects()` phase creates the objects
     WITHOUT using MigrationRunWriter (the objects don't exist yet).
  2. AFTER objects exist, _ensure_attributes() runs INSIDE a
     MigrationRunWriter context. The writer can now succeed because
     `migration_run` exists.
  3. F-PR-3 bootstrap retroactive backfill (a separate script,
     scripts/backfill_F1_bootstrap_to_migration_run.py) reads
     F-PR-3's JSONL and creates a Migration Run row for it.

# Idempotency

  * Re-running this script after objects exist is a no-op
    (rows_skipped_idempotent == rows_examined, rows_modified == 0).
  * Attribute type mismatch fails loud per F-PR-3's pattern.

# Reversibility

Object deletion is operator-manual-only (Attio API limitation).
The rollback_script_path argument in MigrationRunWriter is omitted;
rollback_status='irreversible' is recorded. If rollback is needed:

  1. Operator deletes the two objects via Attio UI.
  2. Manually removes the two attrs (linkedin_outreach.last_classified_by,
     linkedin_outreach.last_migrated_by) via Attio UI.
  3. Re-runs this script to recreate.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clients.attio import AttioClient  # noqa: E402
from scripts._attio_migration_helpers import (  # noqa: E402
    ensure_attribute,
    ensure_object,
)
from workflows.migration_run_writer import MigrationRunWriter  # noqa: E402

# Attribute spec shape: (slug, type, options, is_required, description).
# Descriptions are required by the Attio v2 API — defaults derived from
# slug when None.
OBJECTS = [
    {
        "slug": "reclassification_run",
        "singular": "Reclassification Run",
        "plural": "Reclassification Runs",
        "attributes": [
            ("run_id", "text", None, True, "Unique id per reclassifier run."),
            ("run_at", "datetime", None, True, "When the reclassification run was started."),
            ("classifier_module", "text", None, True, "Python module that owned the run (e.g. workflows.industry_classifier)."),
            ("classifier_version", "text", None, True, "Classifier semver / SHA at run time."),
            ("model_used", "text", None, True, "LLM model id (e.g. claude-haiku-4.5)."),
            ("input_attr", "text", None, True, "Source attribute the classifier consumed."),
            ("output_attr", "text", None, True, "Target attribute the classifier wrote."),
            ("batch_size", "number", None, True, "Number of records selected for this batch."),
            ("success_count", "number", None, True, "Records the classifier scored confidently."),
            ("abstain_count", "number", None, True, "Records the classifier deferred on."),
            ("error_count", "number", None, True, "Records the classifier could not score."),
            ("confidence_threshold", "number", None, False, "Min confidence required to apply the classifier's label."),
            ("audit_log_pointer", "text", None, False, "Path / URI to the per-record audit log for forensics."),
            ("cost_usd_actual", "currency", None, False, "Actual LLM spend for this run (USD)."),
            ("dry_run", "checkbox", None, True, "True when the run was a dry-run (no writes)."),
            ("failure_details_pointer", "long_text", None, False, "JSON / pointer to per-failure details."),
            ("agent_host", "text", None, False, "Hostname that ran the classifier (multi-machine debugging)."),
        ],
    },
    {
        "slug": "migration_run",
        "singular": "Migration Run",
        "plural": "Migration Runs",
        "attributes": [
            ("run_id", "text", None, True, "Unique id per migration run."),
            ("script_name", "text", None, True, "Path to the migration script."),
            ("script_version", "text", None, True, "Script semver / SHA at run time."),
            ("started", "datetime", None, True, "When the migration started."),
            ("completed", "datetime", None, True, "When the migration finished."),
            ("dry_run", "checkbox", None, True, "True when the run was a dry-run (no writes)."),
            ("rows_examined", "number", None, True, "Rows the migration looked at."),
            ("rows_modified", "number", None, True, "Rows the migration changed."),
            ("rows_skipped_idempotent", "number", None, True, "Rows skipped because already at target state."),
            ("rows_failed", "number", None, True, "Rows the migration failed on."),
            ("failure_details_pointer", "long_text", None, False, "JSON / pointer to per-failure details."),
            ("audit_log_path", "text", None, False, "Path to the per-row audit log."),
            ("rollback_script_path", "text", None, False, "Path to the rollback script (when reversible)."),
            (
                "rollback_status",
                "select",
                ["ready", "applied", "failed", "irreversible"],
                True,
                "Whether rollback is possible and its current state.",
            ),
            ("pre_migration_snapshot_pointer", "text", None, False, "Path to the pre-migration data snapshot."),
            ("agent_host", "text", None, False, "Hostname that ran the migration."),
        ],
    },
]

# linkedin_outreach attrs that point to a Reclassification/Migration Run row.
# `linkedin_outreach` is an Attio LIST (parent_object=people), NOT an object —
# attributes are registered via /lists/{list_id}/attributes. The list_id is
# the same ATTIO_LIST_ID used everywhere else in the codebase.
LINKEDIN_OUTREACH_PROVENANCE_ATTRS = [
    ("last_classified_by", "record_reference", "reclassification_run",
     "Provenance pointer to the Reclassification Run that last set this row's verdict."),
    ("last_migrated_by", "record_reference", "migration_run",
     "Provenance pointer to the Migration Run that last modified this row."),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        attio = AttioClient()
    except KeyError:
        print("error: ATTIO_API_KEY env var not set", file=sys.stderr)
        return 2

    # ---- Phase 1: create the two Attio objects (no MigrationRunWriter
    # yet — migration_run doesn't exist until this phase finishes). ----
    object_actions: list[dict] = []
    objects_failed = 0
    for obj in OBJECTS:
        try:
            action, create_body = ensure_object(
                attio, obj["slug"], obj["singular"], obj["plural"],
                dry_run=args.dry_run,
            )
            entry: dict = {"target": "object", "slug": obj["slug"], "action": action}
            if create_body:
                entry["create_body"] = create_body
            object_actions.append(entry)
        except Exception as exc:
            objects_failed += 1
            object_actions.append({
                "target": "object", "slug": obj["slug"],
                "action": "failed", "error": str(exc),
            })
            print(f"object {obj['slug']} create failed: {exc}", file=sys.stderr)

    if objects_failed:
        # Don't try Phase 2 if Phase 1 didn't succeed — MigrationRunWriter
        # needs migration_run to exist.
        print(
            f"phase 1 failed; phase 2 (attributes) skipped. "
            f"{objects_failed} object(s) failed.",
            file=sys.stderr,
        )
        return 1

    list_id = os.environ.get("ATTIO_LIST_ID", "")
    if not list_id:
        print(
            "warning: ATTIO_LIST_ID not set; linkedin_outreach provenance "
            "attrs will be skipped. Re-run with ATTIO_LIST_ID set to "
            "register last_classified_by + last_migrated_by on the list.",
            file=sys.stderr,
        )

    # ---- Phase 2: create attributes inside MigrationRunWriter. ----
    # migration_run now exists, so we can self-log.
    with MigrationRunWriter(
        script_name=Path(__file__).name,
        rollback_script_path=None,  # objects are operator-manual-only
        dry_run=args.dry_run,
        attio=attio,
    ) as run:
        # Object-level attributes
        for obj in OBJECTS:
            for slug, type_, options, is_required, description in obj["attributes"]:
                run.examine()
                try:
                    action = ensure_attribute(
                        attio, "object", obj["slug"], slug, type_,
                        options=options, is_required=is_required,
                        description=description, dry_run=args.dry_run,
                    )
                    if action == "skipped":
                        run.skip_idempotent()
                    else:
                        run.mark_modified(record_id=f"{obj['slug']}.{slug}", object="schema")
                except Exception as exc:
                    run.mark_failed(
                        record_id=f"{obj['slug']}.{slug}", error=exc,
                    )
                    print(
                        f"attribute {obj['slug']}.{slug} failed: {exc}",
                        file=sys.stderr,
                    )

        # linkedin_outreach provenance attrs (LIST endpoint, not /objects/).
        if list_id:
            for slug, type_, ref_obj, description in LINKEDIN_OUTREACH_PROVENANCE_ATTRS:
                run.examine()
                try:
                    action = ensure_attribute(
                        attio, "list", list_id, slug, type_,
                        is_required=False,
                        description=description,
                        referenced_object=ref_obj,
                        dry_run=args.dry_run,
                    )
                    if action == "skipped":
                        run.skip_idempotent()
                    else:
                        run.mark_modified(
                            record_id=f"linkedin_outreach.{slug}",
                            object="schema",
                        )
                except Exception as exc:
                    run.mark_failed(
                        record_id=f"linkedin_outreach.{slug}", error=exc,
                    )
                    print(
                        f"attribute linkedin_outreach.{slug} failed: {exc}",
                        file=sys.stderr,
                    )

    print(
        f"F-PR-3.7 migration complete: "
        f"objects_examined={len(OBJECTS)}, "
        f"objects_failed={objects_failed}, "
        f"attributes_examined={run.rows_examined}, "
        f"attributes_modified={run.rows_modified}, "
        f"attributes_skipped_idempotent={run.rows_skipped_idempotent}, "
        f"attributes_failed={run.rows_failed}"
    )
    return 1 if run.rows_failed > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
