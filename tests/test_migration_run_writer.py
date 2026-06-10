"""Tests for workflows/migration_run_writer.py (F-PR-3.7)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from workflows.migration_run_writer import MigrationRunWriter


@pytest.fixture
def mock_attio():
    client = MagicMock()
    # POST /objects/migration_run/records returns the row we just created
    # (so the writer can grab its record_id). PATCH /records/{id} for
    # back-pointer writes returns an empty success body.
    def _request(method, path, json=None, **kwargs):
        if method == "POST" and path.endswith("/objects/migration_run/records"):
            return {
                "data": {
                    "id": {"record_id": "rec_migration_run_abc"},
                    "values": {},
                }
            }
        if method == "PATCH":
            return {"data": {"id": {"record_id": "rec_target"}, "values": {}}}
        return {"data": {}}

    client._request.side_effect = _request
    return client


def _migration_run_create_body(attio: MagicMock) -> dict:
    """Find the single POST that created the Migration Run row and
    return its `values` dict. After fold-in the writer also issues
    back-pointer PATCHes, so .call_args (the LAST call) is now a
    PATCH — tests must filter to the create call explicitly."""
    for call in attio._request.call_args_list:
        if call[0][:2] == ("POST", "/objects/migration_run/records"):
            return call[1]["json"]["data"]["values"]
    raise AssertionError("no Migration Run create call recorded")


class TestLifecycle:
    def test_writes_one_migration_run_row_on_exit(self, mock_attio):
        with MigrationRunWriter(
            script_name="scripts/migrate_test.py",
            script_version="abc123",
            rollback_script_path="scripts/rollback_test.py",
            attio=mock_attio,
        ) as run:
            run.examine()
            run.mark_modified(record_id="rec_x")
        # Exactly one Migration Run create + exactly one back-pointer PATCH.
        creates = [
            c for c in mock_attio._request.call_args_list
            if c[0][:2] == ("POST", "/objects/migration_run/records")
        ]
        assert len(creates) == 1
        body = creates[0][1]["json"]["data"]["values"]
        assert body["script_name"] == "scripts/migrate_test.py"
        assert body["script_version"] == "abc123"
        assert body["rollback_script_path"] == "scripts/rollback_test.py"
        assert body["rollback_status"] == "ready"
        assert body["rows_examined"] == 1
        assert body["rows_modified"] == 1

    def test_dry_run_does_not_write(self, mock_attio):
        with MigrationRunWriter(
            script_name="x", dry_run=True, attio=mock_attio,
        ) as run:
            run.examine()
            run.mark_modified(record_id="rec_x")
        mock_attio._request.assert_not_called()

    def test_no_rollback_script_marks_irreversible(self, mock_attio):
        with MigrationRunWriter(
            script_name="x", rollback_script_path=None, attio=mock_attio,
        ):
            pass
        body = _migration_run_create_body(mock_attio)
        assert body["rollback_status"] == "irreversible"

    def test_run_id_is_12_hex(self, mock_attio):
        with MigrationRunWriter(script_name="x", attio=mock_attio) as run:
            assert len(run.run_id) == 12
            assert all(c in "0123456789abcdef" for c in run.run_id)


class TestCounters:
    def test_examine_increments(self, mock_attio):
        with MigrationRunWriter(script_name="x", attio=mock_attio) as run:
            run.examine()
            run.examine()
            run.examine()
        body = _migration_run_create_body(mock_attio)
        assert body["rows_examined"] == 3

    def test_mixed_outcomes(self, mock_attio):
        with MigrationRunWriter(script_name="x", attio=mock_attio) as run:
            for _ in range(5):
                run.examine()
            run.mark_modified(record_id="rec_a")
            run.mark_modified(record_id="rec_b")
            run.skip_idempotent()
            run.skip_idempotent()
            run.mark_failed(record_id="rec_c", error=ValueError("fail"))
        body = _migration_run_create_body(mock_attio)
        assert body["rows_examined"] == 5
        assert body["rows_modified"] == 2
        assert body["rows_skipped_idempotent"] == 2
        assert body["rows_failed"] == 1
        # Failure details captured.
        details = json.loads(body["failure_details_pointer"])
        assert any(d.get("record_id") == "rec_c" for d in details)

    def test_idempotency_second_run_contract(self, mock_attio):
        """§9 done-criterion: a re-run should leave rows_modified=0 and
        rows_skipped_idempotent==rows_examined. This is a behavioral
        contract that callers must honor — the writer just records what
        the caller reports."""
        with MigrationRunWriter(script_name="x", attio=mock_attio) as run:
            for _ in range(10):
                run.examine()
                run.skip_idempotent()
        body = _migration_run_create_body(mock_attio)
        assert body["rows_examined"] == body["rows_skipped_idempotent"] == 10
        assert body["rows_modified"] == 0


class TestExceptionHandling:
    def test_uncaught_exception_recorded_then_reraised(self, mock_attio):
        with pytest.raises(RuntimeError, match="boom"), MigrationRunWriter(
            script_name="x", attio=mock_attio
        ) as run:
            run.examine()
            raise RuntimeError("boom")
        # Migration Run row STILL written so the partial state is auditable.
        body = _migration_run_create_body(mock_attio)
        assert body["rows_failed"] >= 1
        details = json.loads(body["failure_details_pointer"])
        assert any(d.get("exc_type") == "RuntimeError" for d in details)

    def test_attio_write_failure_propagates(self, mock_attio):
        # If Attio itself rejects the Migration Run write, propagate
        # rather than silently swallowing it.
        mock_attio._request.side_effect = ConnectionError("attio down")
        with pytest.raises(ConnectionError), MigrationRunWriter(script_name="x", attio=mock_attio):
            pass


class TestBackPointerStamping:
    """§3.13 contract: every modified record gets last_migrated_by →
    Migration Run. The PATCH must fire AFTER the Migration Run row is
    created (so we have its record_id to point at). data-steward-QA-
    build3.7 #1 + engineer-QA-build3.7 #1 — both blockers for build3.7."""

    def _patches(self, attio):
        return [
            c for c in attio._request.call_args_list
            if c[0][0] == "PATCH"
        ]

    def test_back_pointer_patch_fires_per_modified_record(self, mock_attio):
        with MigrationRunWriter(script_name="x", attio=mock_attio) as run:
            run.examine()
            run.examine()
            run.mark_modified(record_id="rec_a")
            run.mark_modified(record_id="rec_b")
        patches = self._patches(mock_attio)
        assert len(patches) == 2
        # Each PATCH targets linkedin_outreach by default.
        paths = {c[0][1] for c in patches}
        assert paths == {
            "/objects/linkedin_outreach/records/rec_a",
            "/objects/linkedin_outreach/records/rec_b",
        }
        # Body shape: last_migrated_by points at the Migration Run row.
        for call in patches:
            body = call[1]["json"]["data"]["values"]
            assert body["last_migrated_by"][0]["target_object"] == "migration_run"
            assert body["last_migrated_by"][0]["target_record_id"] == \
                "rec_migration_run_abc"

    def test_back_pointer_honors_object_param(self, mock_attio):
        """mark_modified(object="companies") patches a Companies record."""
        with MigrationRunWriter(script_name="x", attio=mock_attio) as run:
            run.examine()
            run.mark_modified(record_id="rec_company", object="companies")
        patches = self._patches(mock_attio)
        assert len(patches) == 1
        assert patches[0][0][1] == "/objects/companies/records/rec_company"

    def test_schema_object_skips_back_pointer(self, mock_attio):
        """Schema-level mutations (attribute creation) count toward
        rows_modified but DON'T trigger a back-pointer PATCH — there is
        no target record to stamp."""
        with MigrationRunWriter(script_name="x", attio=mock_attio) as run:
            run.examine()
            run.mark_modified(record_id="linkedin_outreach.new_attr", object="schema")
        assert self._patches(mock_attio) == []
        body = mock_attio._request.call_args_list[-1][1]["json"]["data"]["values"]
        # rows_modified still incremented.
        assert body["rows_modified"] == 1

    def test_list_entry_object_skips_back_pointer(self, mock_attio):
        """Wave-2-C fix-up (multi-agent C1): list-entry-level mutations
        pass an entry_id as record_id, but the back-pointer endpoint
        ``/objects/{object}/records/{record_id}`` only resolves
        object-level record IDs. The ``list_entry`` sentinel counts
        toward rows_modified but skips the back-pointer that would
        otherwise 404 on the entry_id."""
        with MigrationRunWriter(script_name="x", attio=mock_attio) as run:
            run.examine()
            run.mark_modified(record_id="ent_abc123", object="list_entry")
        assert self._patches(mock_attio) == []
        body = mock_attio._request.call_args_list[-1][1]["json"]["data"]["values"]
        assert body["rows_modified"] == 1

    def test_back_pointer_failure_does_not_re_raise(self, mock_attio, capsys):
        """If the per-record PATCH fails, the Migration Run row stays
        (it was created before the back-pointer attempt). Failures are
        captured in _back_pointer_failures for operator visibility AND
        a stderr WARNING fires so the operator sees the gap before
        PR-43.5's weekly data-quality-report (engineer-QA-v2 #1)."""
        # Set up: Migration Run create succeeds, PATCH fails.
        original_side_effect = mock_attio._request.side_effect

        def fail_on_patch(method, path, json=None, **kwargs):
            if method == "PATCH":
                raise ConnectionError("attio temporarily unavailable")
            return original_side_effect(method, path, json=json, **kwargs)

        mock_attio._request.side_effect = fail_on_patch
        with MigrationRunWriter(script_name="x", attio=mock_attio) as run:
            run.examine()
            run.mark_modified(record_id="rec_a")
        # Migration Run row was created despite the PATCH failure.
        creates = [
            c for c in mock_attio._request.call_args_list
            if c[0][:2] == ("POST", "/objects/migration_run/records")
        ]
        assert len(creates) == 1
        # Failure captured.
        assert len(run._back_pointer_failures) == 1
        assert run._back_pointer_failures[0]["record_id"] == "rec_a"
        # Stderr warning emitted so the operator sees the gap immediately.
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert run.run_id in captured.err
        assert "back-pointer PATCH" in captured.err


class TestSnapshotPointer:
    def test_snapshot_pointer_recorded_when_provided(self, mock_attio):
        with MigrationRunWriter(
            script_name="x",
            attio=mock_attio,
            pre_migration_snapshot_pointer="s3://snapshots/2026-05-21.tar.gz",
        ):
            pass
        body = _migration_run_create_body(mock_attio)
        assert body["pre_migration_snapshot_pointer"] == \
            "s3://snapshots/2026-05-21.tar.gz"

    def test_audit_log_path_recorded_when_provided(self, mock_attio):
        with MigrationRunWriter(
            script_name="x",
            attio=mock_attio,
            audit_log_path="/var/log/migration.log",
        ):
            pass
        body = _migration_run_create_body(mock_attio)
        assert body["audit_log_path"] == "/var/log/migration.log"


# ── PR-9b fold-in additions ─────────────────────────────────────────────────


class TestReclassificationRunIdCrossReference:
    """PR-9b fold-in BLOCKING #1 (data-steward + math 2-agent convergence):
    backfill scripts that open both writers pass the rec_run.run_id into
    MigrationRunWriter so the Migration Run row carries a real Attio
    cross-reference, not just a stderr log line."""

    def test_reclassification_run_id_persisted_to_migration_run_row(self, mock_attio):
        with MigrationRunWriter(
            script_name="scripts/backfill_per_step_timestamps.py",
            attio=mock_attio,
            reclassification_run_id="rec_run_abc123",
        ):
            pass
        body = _migration_run_create_body(mock_attio)
        assert body["reclassification_run_id"] == "rec_run_abc123", (
            "Migration Run row must carry the rec_run.run_id when supplied — "
            "this is the cross-reference forensics consumers join on."
        )

    def test_reclassification_run_id_absent_when_not_supplied(self, mock_attio):
        """Pure schema-work migrations don't open a Reclassification Run
        and pass None — the field must NOT appear on the row in that case."""
        with MigrationRunWriter(
            script_name="scripts/migrate_schema_only.py",
            attio=mock_attio,
        ):
            pass
        body = _migration_run_create_body(mock_attio)
        assert "reclassification_run_id" not in body, (
            "reclassification_run_id must be ABSENT (not None/empty-string) "
            "when no rec_run is paired with the migration."
        )


class TestSkipExcluded:
    """PR-9b fold-in (5-agent convergence IMP #4): soft-deleted /
    out-of-scope rows are tracked separately from idempotent rows so
    the Migration Run row reflects the correct semantic."""

    def test_skip_excluded_increments_dedicated_counter(self, mock_attio):
        with MigrationRunWriter(script_name="x", attio=mock_attio) as run:
            run.examine()
            run.skip_excluded(reason="soft_deleted_loser")
            run.examine()
            run.skip_excluded(reason="soft_deleted_loser")
        body = _migration_run_create_body(mock_attio)
        assert body["rows_skipped_excluded"] == 2
        # And idempotent counter is NOT inflated by exclusions.
        assert body["rows_skipped_idempotent"] == 0

    def test_skip_excluded_reason_recorded_in_failure_details(self, mock_attio):
        with MigrationRunWriter(script_name="x", attio=mock_attio) as run:
            run.examine()
            run.skip_excluded(reason="soft_deleted_loser")
        body = _migration_run_create_body(mock_attio)
        details = json.loads(body["failure_details_pointer"])
        assert any(
            d.get("kind") == "skip_excluded" and d.get("reason") == "soft_deleted_loser"
            for d in details
        )
