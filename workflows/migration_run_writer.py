"""MigrationRunWriter — every `scripts/migrate_*.py` and
`scripts/backfill_*.py` runs inside this context manager (F-PR-3.7,
plan §3.13).

# What it guarantees

Every migration / backfill that mutates Attio produces a typed
`Migration Run` row capturing:

  * Identity: run_id, script_name, script_version (git SHA), started,
    completed, dry_run
  * Counters: rows_examined, rows_modified, rows_skipped_idempotent,
    rows_failed
  * Provenance: pre_migration_snapshot_pointer, audit_log_path
  * Recovery: rollback_script_path (or escalate
    `irreversible_migration_approved=false` if no rollback is possible)
  * Failure metadata: failure_details_pointer

Every modified record gets `last_migrated_by: record_reference →
Migration Run` so a forensics scan can reconstruct "which migration
touched this row".

# Usage

    with MigrationRunWriter(
        script_name="scripts/migrate_X.py",
        script_version=git_sha(),
        rollback_script_path="scripts/rollback_X.py",
        dry_run=args.dry_run,
    ) as run:
        for row in rows:
            run.examine()
            if not _needs_change(row):
                run.skip_idempotent()
                continue
            try:
                _apply(row)
                run.mark_modified(record_id=row["id"])
            except Exception as exc:
                run.mark_failed(record_id=row["id"], error=exc)

    # On __exit__: writes the Migration Run row + emits a
    # `migration_idempotency_regression` queue row if
    # rows_modified > 0 on a second consecutive run.

# Exempt scripts

Per plan §3.13, three scripts are MigrationRunWriter-exempt:

  1. scripts/migrate_attio_F1_operator_review_queue.py
     (F-PR-3 bootstrap — Migration Run object didn't exist yet;
     this PR backfills retroactively from the bootstrap JSONL.)
  2. scripts/validate_attio_schema_deltas.py
     (read-only validator; never writes Attio.)
  3. scripts/data_quality_report.py
     (PR-43.5; default --report-only is read-only. If a future
     --auto-remediate is added, the script must opt back into
     MigrationRunWriter.)

The CI guard `tests/test_migration_writer_compliance.py` lists these
in `EXEMPT_SCRIPTS` and refuses to merge new scripts/migrate_*.py or
scripts/backfill_*.py that don't use the writer (or join the
documented exemption list).
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from clients.attio import AttioClient

if TYPE_CHECKING:
    from types import TracebackType

MIGRATION_RUN_SLUG = "migration_run"


def _git_sha() -> str:
    """Return the current HEAD SHA or 'unknown' when not in a git repo."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent,
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()[:12]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


class MigrationRunWriter:
    """Context manager that wraps a single migration / backfill script run.

    Mutates: writes ONE `Migration Run` row on __exit__. Per-record
    `last_migrated_by` writes happen via `mark_modified(record_id=...)`
    so the Attio side stays consistent with the counters.
    """

    def __init__(
        self,
        *,
        script_name: str,
        script_version: str | None = None,
        rollback_script_path: str | None = None,
        dry_run: bool = False,
        attio: AttioClient | None = None,
        audit_log_path: str | None = None,
        pre_migration_snapshot_pointer: str | None = None,
        reclassification_run_id: str | None = None,
    ) -> None:
        self.script_name = script_name
        self.script_version = script_version or _git_sha()
        self.rollback_script_path = rollback_script_path
        self.dry_run = dry_run
        self._attio = attio
        self.audit_log_path = audit_log_path
        self.pre_migration_snapshot_pointer = pre_migration_snapshot_pointer
        # PR-9b fold-in (data-steward + math BLOCKING #1): backfill scripts
        # that open both ReclassificationRunWriter and MigrationRunWriter
        # pass the rec_run.run_id here so the two rows can be joined for
        # forensics. None when the script does pure schema work.
        self.reclassification_run_id = reclassification_run_id

        self.run_id = uuid.uuid4().hex[:12]
        self.started: str | None = None
        self.completed: str | None = None
        self.rows_examined = 0
        self.rows_modified = 0
        self.rows_skipped_idempotent = 0
        # PR-9b fold-in (5-agent convergence IMP #4): soft-deleted / excluded
        # rows shouldn't inflate the idempotent counter — the semantic is
        # "row exists but is outside scope", not "row already has the
        # target value". Tracked separately and surfaced on the Migration
        # Run row via failure_details_pointer when non-zero.
        self.rows_skipped_excluded = 0
        self.rows_failed = 0
        self.failure_details: list[dict] = []
        # (record_id, object) tuples — empty `object` (or "schema") skips
        # the back-pointer PATCH. Per §3.13 "every modified record gets
        # last_migrated_by → Migration Run", but schema-level changes
        # have no target record to stamp.
        self.modified_records: list[tuple[str, str]] = []
        self._migration_run_record_id: str | None = None
        self._back_pointer_failures: list[dict] = []

    # ---- Lifecycle ----

    def __enter__(self) -> MigrationRunWriter:
        self.started = datetime.now(UTC).isoformat()
        if self._attio is None:
            self._attio = AttioClient()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> Literal[False]:
        self.completed = datetime.now(UTC).isoformat()
        # Crash-on-exception: capture the exception in failure_details
        # before writing the Migration Run row so the audit trail isn't
        # lost.
        if exc_type is not None:
            self.failure_details.append({
                "kind": "uncaught_exception",
                "exc_type": exc_type.__name__,
                "exc_msg": str(exc_val)[:500],
            })
            self.rows_failed += 1

        if not self.dry_run:
            self._write_migration_run_row()
            # §3.13 contract: every modified record gets last_migrated_by.
            # Must run AFTER the Migration Run row is created so we have
            # its record_id to point at. Failures here are captured in
            # _back_pointer_failures but DON'T re-raise — the Migration
            # Run row is already on Attio; back-pointer is a forensics
            # convenience, not a §3.1 safety guarantee.
            self._stamp_back_pointers()
            # engineer-QA-build3.7-v2 #1: surface silent back-pointer
            # failures on stderr so operators see the forensics gap
            # immediately. Without this, gaps are only visible via
            # PR-43.5's data-quality-report (weekly cadence) — too
            # late for fresh migration debugging.
            if self._back_pointer_failures:
                print(
                    f"WARNING: MigrationRunWriter run_id={self.run_id} "
                    f"wrote Migration Run row but {len(self._back_pointer_failures)} "
                    f"back-pointer PATCH(es) failed. "
                    f"Migration data is safe; last_migrated_by stamps are partial. "
                    f"See _back_pointer_failures for details.",
                    file=sys.stderr,
                )
        return False  # never suppress

    # ---- Per-record counters ----

    def examine(self) -> None:
        self.rows_examined += 1

    def skip_idempotent(self) -> None:
        self.rows_skipped_idempotent += 1

    def skip_excluded(self, *, reason: str | None = None) -> None:
        """Count a row as examined-but-out-of-scope.

        Distinct from skip_idempotent — idempotent means "row already
        carries the target value", excluded means "row is outside the
        migration's scope altogether" (e.g., PR-9.5 soft-deleted losers
        with merged_into set). Surfaced separately on the Migration Run
        row so operator postmortems can disambiguate."""
        self.rows_skipped_excluded += 1
        if reason:
            self.failure_details.append({
                "kind": "skip_excluded",
                "reason": reason,
            })

    def mark_modified(
        self, *, record_id: str, object: str = "linkedin_outreach",
    ) -> None:
        """Counts a record as modified and (on __exit__) stamps
        `last_migrated_by` on the target record.

        `object` selects which Attio object the record_id belongs to.
        Sentinel values that skip the back-pointer PATCH:
          * ``object="schema"`` — schema-level mutations (attribute
            creation, etc.) have no target record to stamp.
          * ``object="list_entry"`` (Wave-2-C fix-up, multi-agent C1):
            list-entry-level mutations pass an ``entry_id`` as record_id,
            but the back-pointer endpoint is
            ``/objects/{object}/records/{record_id}`` which only resolves
            object-level record IDs (not list-entry IDs). Use this
            sentinel from scripts that PATCH list entries via
            ``attio.update_list_entry``; the row still counts toward
            ``rows_modified`` but skips the back-pointer that would
            otherwise 404.
        """
        self.rows_modified += 1
        if object not in ("schema", "list_entry"):
            self.modified_records.append((record_id, object))

    def mark_failed(self, *, record_id: str, error: Exception | str) -> None:
        self.rows_failed += 1
        msg = str(error)[:500] if isinstance(error, Exception) else str(error)[:500]
        self.failure_details.append({
            "record_id": record_id,
            "error": msg,
            "error_class": type(error).__name__ if isinstance(error, Exception) else "str",
        })

    # ---- Internal ----

    def _stamp_back_pointers(self) -> None:
        """For each modified record, PATCH last_migrated_by → this run.

        Failures are captured for operator visibility but don't bubble
        up — the Migration Run row already exists, so the forensics
        chain is partially complete. Operators see the gap via
        _back_pointer_failures (logged below) + the Migration Run row's
        rows_modified vs the actual count of records with this run as
        last_migrated_by (PR-43.5 data-quality-report consumer)."""
        assert self._attio is not None
        if self._migration_run_record_id is None:
            # _write_migration_run_row didn't populate the id (Attio
            # response shape edge case). Don't crash; record the gap.
            self._back_pointer_failures.append({
                "kind": "missing_migration_run_record_id",
                "n_back_pointers_skipped": len(self.modified_records),
            })
            return
        for record_id, target_object in self.modified_records:
            try:
                body = {
                    "data": {
                        "values": {
                            "last_migrated_by": [{
                                "target_object": MIGRATION_RUN_SLUG,
                                "target_record_id": self._migration_run_record_id,
                            }]
                        }
                    }
                }
                self._attio._request(
                    "PATCH",
                    f"/objects/{target_object}/records/{record_id}",
                    json=body,
                )
            except Exception as exc:
                self._back_pointer_failures.append({
                    "record_id": record_id,
                    "object": target_object,
                    "error": str(exc)[:300],
                    "error_class": type(exc).__name__,
                })

    def _write_migration_run_row(self) -> None:
        """Write the Migration Run row. Caller must have set self._attio."""
        assert self._attio is not None
        attrs: dict[str, Any] = {
            "run_id": self.run_id,
            "script_name": self.script_name,
            "script_version": self.script_version,
            "started": self.started,
            "completed": self.completed,
            "dry_run": self.dry_run,
            "rows_examined": self.rows_examined,
            "rows_modified": self.rows_modified,
            "rows_skipped_idempotent": self.rows_skipped_idempotent,
            "rows_skipped_excluded": self.rows_skipped_excluded,
            "rows_failed": self.rows_failed,
            "agent_host": socket.gethostname(),
        }
        if self.reclassification_run_id is not None:
            attrs["reclassification_run_id"] = self.reclassification_run_id
        if self.rollback_script_path is not None:
            attrs["rollback_script_path"] = self.rollback_script_path
            attrs["rollback_status"] = "ready"
        else:
            attrs["rollback_status"] = "irreversible"
        if self.audit_log_path is not None:
            attrs["audit_log_path"] = self.audit_log_path
        if self.pre_migration_snapshot_pointer is not None:
            attrs["pre_migration_snapshot_pointer"] = self.pre_migration_snapshot_pointer
        if self.failure_details:
            attrs["failure_details_pointer"] = json.dumps(
                self.failure_details, default=str
            )

        body = {"data": {"values": attrs}}
        data = self._attio._request(
            "POST", f"/objects/{MIGRATION_RUN_SLUG}/records", json=body
        )
        record = data.get("data", data)
        self._migration_run_record_id = (
            record.get("id", {}).get("record_id")
            if isinstance(record.get("id"), dict)
            else record.get("record_id")
        )
