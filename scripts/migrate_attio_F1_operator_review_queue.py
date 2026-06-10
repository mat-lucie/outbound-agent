#!/usr/bin/env python3
"""Create the Attio `Operator Review Queue` object + register all
escalation types as select options. (F-PR-3 bootstrap migration.)

# Bootstrap-migration carve-out

This script is **MigrationRunWriter-exempt** because the `Migration
Run` Attio object doesn't exist yet — F-PR-3.7 creates it. To preserve
the section 9 idempotency invariant despite the exemption, this script
logs each run to:

    ~/.outbound-agent/audit/F-PR-3-bootstrap-{date}.jsonl

with the four-field idempotency contract:

    rows_examined, rows_modified, rows_skipped_idempotent, rows_failed

F-PR-3.7's executor MUST backfill a `Migration Run` row from this
JSONL as its first action (per plan section 3.13 bootstrap carve-out).

# Idempotency

Running this script twice in a row is a no-op on the second run:

  * Object create: if `operator_review_queue` already exists, skip.
  * Attribute create: per attribute, if it exists with the same type,
    skip; if it exists with a DIFFERENT type, fail loud (don't try to
    migrate the type — that's an irreversible Attio schema change).
  * Select option register: if the option already exists, skip; if
    missing, add. The 79-slug enum is the source of truth.

# Reversibility

Object deletion in Attio is a manual UI action only — there is no API
to drop an object. Per plan section 4 F-PR-3 / Round-4 D28, this
script declares `rollback_strategy='operator_manual'`. If rollback is
needed, the operator deletes the object via Attio UI; the bootstrap
JSONL at the path above retains the create-arguments for recreate.

# Running

    python scripts/migrate_attio_F1_operator_review_queue.py
    python scripts/migrate_attio_F1_operator_review_queue.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

# Add project root to path for imports when run as `python scripts/...`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clients.attio import AttioClient  # noqa: E402
from scripts._attio_migration_helpers import (  # noqa: E402
    ensure_attribute,
    ensure_object,
)
from workflows.escalation_schemas import (  # noqa: E402
    CONFIGURATION_DECISION_KEYS,
    ESCALATION_TYPES,
)

OBJECT_SLUG = "operator_review_queue"
OBJECT_PLURAL = "Operator Review Queue"
OBJECT_SINGULAR = "Operator Review Item"

# Attribute spec for the queue object.
# Shape: (slug, type, options_or_None, description, is_required).
ATTRIBUTES: list[tuple[str, str, list[str] | None, str, bool]] = [
    ("type", "select", list(ESCALATION_TYPES),
     "The escalation type (79 typed slugs).", True),
    ("decision_key", "select", list(CONFIGURATION_DECISION_KEYS),
     "Only set when type='configuration_decision' (refactor-QA Rec #3).", False),
    ("idempotency_key", "text", None,
     "Caller-provided dedup key.", True),
    ("uniqueness_key", "text", None,
     "Computed: type|decision_key|idempotency_key. Indexed for fast dedup lookup.", True),
    ("payload_json", "long_text", None,
     "JSON payload (TypedDict-validated at write time).", True),
    ("status", "select",
     ["open", "resolved", "dismissed", "auto_resolved_default"],
     "Lifecycle status.", True),
    ("opened_at", "datetime", None,
     "When the agent first opened the row.", True),
    ("resolved_at", "datetime", None,
     "When the operator (or default-on-expiry resolver) closed the row.", False),
    ("deadline", "date", None,
     "Optional default-on-expiry deadline.", False),
    ("decision_json", "long_text", None,
     "Operator's recorded decision payload (when status=resolved).", False),
    ("decision_source", "text", None,
     "How status flipped: 'agent_opened' | 'operator' | 'expired_default_*'.", True),
    ("agent_host", "text", None,
     "Hostname that opened the row (for multi-machine debugging).", False),
]

AUDIT_DIR = Path.home() / ".outbound-agent" / "audit"


def _audit_path() -> Path:
    return AUDIT_DIR / f"F-PR-3-bootstrap-{date.today().isoformat()}.jsonl"


def _new_run_id() -> str:
    return uuid.uuid4().hex[:12]


# Schema-mutation helpers are extracted into scripts/_attio_migration_helpers
# so every bootstrap migration agrees on the Attio v2 contract (type
# mapping, required POST fields, currency/record-reference config shape,
# select-option backfill). See that module for the full v2 drift inventory.


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing to Attio.",
    )
    args = parser.parse_args(argv)

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    audit_path = _audit_path()
    run_id = _new_run_id()
    started = datetime.now(UTC).isoformat()

    rows_examined = 0
    rows_modified = 0
    rows_skipped_idempotent = 0
    rows_failed = 0
    actions: list[dict] = []

    try:
        attio = AttioClient()
    except KeyError:
        print("error: ATTIO_API_KEY env var not set", file=sys.stderr)
        return 2

    # ---- Object ----
    try:
        obj_action, obj_create_body = ensure_object(
            attio, OBJECT_SLUG, OBJECT_SINGULAR, OBJECT_PLURAL,
            dry_run=args.dry_run,
        )
        action_entry: dict = {
            "target": "object", "slug": OBJECT_SLUG, "action": obj_action,
        }
        # Capture the full create body so a manual rollback (Attio object
        # deletion is UI-only) has the exact args needed to recreate.
        if obj_create_body:
            action_entry["create_body"] = obj_create_body
        actions.append(action_entry)
        rows_examined += 1
        if obj_action.startswith("created") or obj_action.startswith("would_create"):
            rows_modified += 1
        elif obj_action == "skipped":
            rows_skipped_idempotent += 1
    except Exception as exc:
        rows_failed += 1
        actions.append({"target": "object", "slug": OBJECT_SLUG,
                        "action": "failed", "error": str(exc)})
        print(f"object create failed: {exc}", file=sys.stderr)

    # ---- Attributes ----
    for slug, type_, options, description, is_required in ATTRIBUTES:
        rows_examined += 1
        try:
            attr_action = ensure_attribute(
                attio, "object", OBJECT_SLUG, slug, type_,
                options=options, is_required=is_required,
                description=description, dry_run=args.dry_run,
            )
            actions.append({
                "target": "attribute", "slug": slug, "type": type_,
                "action": attr_action,
            })
            if attr_action == "skipped":
                rows_skipped_idempotent += 1
            else:
                rows_modified += 1
        except Exception as exc:
            rows_failed += 1
            actions.append({"target": "attribute", "slug": slug,
                            "action": "failed", "error": str(exc)})
            print(f"attribute {slug} failed: {exc}", file=sys.stderr)

    completed = datetime.now(UTC).isoformat()

    # ---- Bootstrap JSONL (F-PR-3.7 retroactively reads this) ----
    # `audit_log_path` lets F-PR-3.7 record the source path on the Migration
    # Run row rather than reconstructing it from path conventions
    # (data-steward-QA-build3 #2).
    line = {
        "run_id": run_id,
        "script_name": Path(__file__).name,
        "script_version": "F-PR-3",
        "started": started,
        "completed": completed,
        "dry_run": args.dry_run,
        "rollback_strategy": "operator_manual",
        "audit_log_path": str(audit_path),
        "rows_examined": rows_examined,
        "rows_modified": rows_modified,
        "rows_skipped_idempotent": rows_skipped_idempotent,
        "rows_failed": rows_failed,
        "actions": actions,
    }
    with open(audit_path, "a") as fh:
        fh.write(json.dumps(line, default=str) + "\n")

    print(json.dumps({
        "run_id": run_id,
        "rows_examined": rows_examined,
        "rows_modified": rows_modified,
        "rows_skipped_idempotent": rows_skipped_idempotent,
        "rows_failed": rows_failed,
        "audit_log": str(audit_path),
    }, indent=2))

    return 1 if rows_failed > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
