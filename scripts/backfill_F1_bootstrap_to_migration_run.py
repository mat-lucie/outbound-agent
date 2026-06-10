#!/usr/bin/env python3
"""Retroactive Migration Run backfill for F-PR-3's bootstrap JSONL.
F-PR-3.7's seam with F-PR-3.

# Why this script exists

F-PR-3's bootstrap migration ran BEFORE the Migration Run object
existed (chicken-and-egg). It logged each run to
`~/.outbound-agent/audit/F-PR-3-bootstrap-{date}.jsonl` with the §9
four-field idempotency contract.

Now that F-PR-3.7 has created the Migration Run object, this script
reads every F-PR-3 bootstrap JSONL line and creates one Migration Run
row per line, faithfully preserving the run_id / counters / actions.

# Idempotency

The backfill is keyed by `run_id` from the JSONL. Re-running this
script is a no-op for any Migration Run row that already exists with
the same run_id.

# When to run

Run ONCE after F-PR-3.7's migrate_attio_F2 script completes successfully.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clients.attio import AttioClient  # noqa: E402
from workflows.migration_run_writer import MIGRATION_RUN_SLUG  # noqa: E402

AUDIT_DIR = Path.home() / ".outbound-agent" / "audit"


def _existing_migration_run_ids(attio: AttioClient) -> set[str]:
    """Return run_ids of every Migration Run row currently in Attio."""
    body = {"limit": 1000}
    data = attio._request(
        "POST", f"/objects/{MIGRATION_RUN_SLUG}/records/query", json=body
    )
    seen: set[str] = set()
    for record in data.get("data", []):
        values = record.get("values", {}) or {}
        run_id = values.get("run_id")
        if isinstance(run_id, list) and run_id:
            run_id = run_id[0].get("value") if isinstance(run_id[0], dict) else run_id[0]
        if isinstance(run_id, str):
            seen.add(run_id)
    return seen


def _backfill_one_line(attio: AttioClient, line: dict, dry_run: bool) -> str:
    """Create a Migration Run row from a single JSONL line. Returns
    action ∈ {"created", "would_create", "failed"}."""
    attrs = {
        "run_id": line["run_id"],
        "script_name": line.get("script_name", "scripts/migrate_attio_F1_operator_review_queue.py"),
        "script_version": line.get("script_version", "F-PR-3"),
        "started": line.get("started"),
        "completed": line.get("completed"),
        "dry_run": line.get("dry_run", False),
        "rows_examined": line.get("rows_examined", 0),
        "rows_modified": line.get("rows_modified", 0),
        "rows_skipped_idempotent": line.get("rows_skipped_idempotent", 0),
        "rows_failed": line.get("rows_failed", 0),
        "audit_log_path": line.get("audit_log_path"),
        "rollback_status": "irreversible"
            if line.get("rollback_strategy") == "operator_manual"
            else "ready",
    }
    # Only populate failure_details_pointer with the bootstrap actions
    # when the bootstrap actually had failures. Storing the full actions
    # list there for successful runs would misclassify successful
    # create-actions as failures (data-steward-QA-build3.7 finding #4).
    actions = line.get("actions") or []
    if line.get("rows_failed", 0) > 0 and actions:
        failed_actions = [a for a in actions if a.get("action") == "failed"]
        if failed_actions:
            attrs["failure_details_pointer"] = json.dumps(
                {"failed_actions": failed_actions}, default=str
            )

    if dry_run:
        return "would_create"

    body = {"data": {"values": attrs}}
    try:
        attio._request("POST", f"/objects/{MIGRATION_RUN_SLUG}/records", json=body)
    except Exception as exc:
        print(f"failed to create Migration Run for run_id={line['run_id']}: {exc}",
              file=sys.stderr)
        return "failed"
    return "created"


def _iter_bootstrap_lines(audit_dir: Path, malformed_counter: list[int]):
    """Yield every JSONL line across every F-PR-3-bootstrap-*.jsonl file.

    `malformed_counter` is a single-element list used as a mutable
    counter (so the caller can read the count after iteration completes
    — engineer-QA-build3.7 #3)."""
    if not audit_dir.exists():
        return
    for path in sorted(audit_dir.glob("F-PR-3-bootstrap-*.jsonl")):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    malformed_counter[0] += 1
                    print(
                        f"warning: malformed line in {path}: {exc}",
                        file=sys.stderr,
                    )
                    continue


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--audit-dir", type=Path, default=AUDIT_DIR)
    args = parser.parse_args(argv)

    try:
        attio = AttioClient()
    except KeyError:
        print("error: ATTIO_API_KEY env var not set", file=sys.stderr)
        return 2

    existing_ids = _existing_migration_run_ids(attio)

    created = 0
    skipped_idempotent = 0
    would_create = 0
    failed = 0
    malformed_counter = [0]
    for line in _iter_bootstrap_lines(args.audit_dir, malformed_counter):
        run_id = line.get("run_id")
        if not run_id:
            failed += 1
            print(f"line missing run_id, skipped: {line}", file=sys.stderr)
            continue
        if run_id in existing_ids:
            skipped_idempotent += 1
            continue
        action = _backfill_one_line(attio, line, dry_run=args.dry_run)
        if action == "created":
            created += 1
            existing_ids.add(run_id)
        elif action == "would_create":
            would_create += 1
        else:
            failed += 1

    malformed = malformed_counter[0]
    print(
        f"F-PR-3.7 retroactive backfill: created={created}, "
        f"skipped_idempotent={skipped_idempotent}, "
        f"would_create={would_create}, failed={failed}, "
        f"malformed_lines={malformed}"
    )
    # Malformed JSONL lines are a real data-integrity signal — exit
    # non-zero so operator sees the audit-log gap.
    return 1 if (failed > 0 or malformed > 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
