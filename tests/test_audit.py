"""Tests for workflows/audit.py — F-PR-2 per-run JSONL audit log."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from workflows.audit import (
    AuditLogger,
    _audit_path,
    _find_audit_path,
    audit_stats,
    iter_events,
    list_runs,
    new_run_id,
    record_ids_for_events,
    resume_from_audit,
)


@pytest.fixture
def audit_tmp(tmp_path, monkeypatch):
    """Redirect AUDIT_DIR to an isolated tmp_path for each test."""
    monkeypatch.setattr("workflows.audit.AUDIT_DIR", tmp_path)
    return tmp_path


class TestNewRunId:
    def test_returns_12_char_hex(self):
        rid = new_run_id()
        assert len(rid) == 12
        assert all(c in "0123456789abcdef" for c in rid)

    def test_unique_per_call(self):
        ids = {new_run_id() for _ in range(100)}
        # uuid4 hex-12 collision is astronomically unlikely.
        assert len(ids) == 100


class TestAuditPath:
    def test_path_shape(self, audit_tmp):
        path = _audit_path("abc123", date(2026, 5, 21))
        assert path == audit_tmp / "run-2026-05-21-abc123.jsonl"

    def test_default_date_is_today(self, audit_tmp):
        path = _audit_path("abc123")
        assert path.name.startswith(f"run-{date.today().isoformat()}-")


class TestAuditLogger:
    def test_context_manager_writes_run_started_and_completed(self, audit_tmp):
        with AuditLogger(run_id="t0", workflow="daily_check") as log:
            log.event("noop")
        events = list(iter_events("t0"))
        assert [e["event"] for e in events] == ["run_started", "noop", "run_completed"]

    def test_context_manager_writes_run_crashed_on_exception(self, audit_tmp):
        with pytest.raises(ValueError, match="boom"), AuditLogger(run_id="t1", workflow="daily_check") as log:
            log.event("midway")
            raise ValueError("boom")
        events = list(iter_events("t1"))
        assert events[-1]["event"] == "run_crashed"
        assert events[-1]["exc_type"] == "ValueError"
        assert events[-1]["exc_msg"] == "boom"

    def test_sequence_is_monotonic(self, audit_tmp):
        with AuditLogger(run_id="t2", workflow="x") as log:
            log.event("a")
            log.event("b")
            log.event("c")
        events = list(iter_events("t2"))
        seqs = [e["seq"] for e in events]
        assert seqs == sorted(seqs)
        assert seqs == list(range(1, len(seqs) + 1))

    def test_event_outside_context_raises(self, audit_tmp):
        log = AuditLogger(run_id="t3", workflow="x")
        with pytest.raises(RuntimeError, match="outside `with` context"):
            log.event("oops")

    def test_event_after_exit_raises(self, audit_tmp):
        with AuditLogger(run_id="t4", workflow="x") as log:
            pass
        with pytest.raises(RuntimeError):
            log.event("too_late")

    def test_payload_kwargs_are_persisted(self, audit_tmp):
        with AuditLogger(run_id="t5", workflow="x") as log:
            log.event("dm_sent", record_id="rec_abc", step="DM1", count=3)
        events = list(iter_events("t5"))
        dm = next(e for e in events if e["event"] == "dm_sent")
        assert dm["record_id"] == "rec_abc"
        assert dm["step"] == "DM1"
        assert dm["count"] == 3

    def test_non_json_serializable_falls_back_to_str(self, audit_tmp):
        """`default=str` lets dates serialize without ad-hoc coercion at every call site."""
        with AuditLogger(run_id="t6", workflow="x") as log:
            log.event("dated", when=date(2026, 5, 21), now=datetime(2026, 5, 21, 12, tzinfo=UTC))
        events = list(iter_events("t6"))
        ev = next(e for e in events if e["event"] == "dated")
        assert ev["when"] == "2026-05-21"
        # str(datetime) uses space, not 'T'. Either is acceptable —
        # we just need the timestamp to round-trip through json+str.
        assert ev["now"].startswith("2026-05-21")
        assert "12:00:00" in ev["now"]

    def test_each_line_flushed_for_crash_safety(self, audit_tmp):
        """Flush-per-event means a kill -9 between events leaves prior events on disk."""
        log = AuditLogger(run_id="t7", workflow="x")
        log.__enter__()
        try:
            log.event("first")
            # Without explicit close: file should still have run_started + first.
            path = _find_audit_path("t7")
            content = path.read_text()
            lines = [line for line in content.split("\n") if line]
            assert len(lines) >= 2  # run_started + first
            assert json.loads(lines[0])["event"] == "run_started"
            assert json.loads(lines[1])["event"] == "first"
        finally:
            log.__exit__(None, None, None)

    def test_default_run_id_is_generated(self, audit_tmp):
        with AuditLogger(workflow="x") as log:
            assert len(log.run_id) == 12

    def test_dry_run_flag_recorded(self, audit_tmp):
        with AuditLogger(run_id="t8", workflow="x", dry_run=True):
            pass
        events = list(iter_events("t8"))
        assert events[0]["dry_run"] is True

    def test_workflow_name_in_run_started(self, audit_tmp):
        with AuditLogger(run_id="t9", workflow="weekly_prospect"):
            pass
        events = list(iter_events("t9"))
        assert events[0]["workflow"] == "weekly_prospect"


class TestResumeFromAudit:
    def test_completed_run_status(self, audit_tmp):
        with AuditLogger(run_id="r1", workflow="x") as log:
            log.event("touch", record_id="A")
            log.event("touch", record_id="B")
        manifest = resume_from_audit("r1")
        assert manifest["status"] == "completed"
        assert manifest["record_ids_seen"] == {"A", "B"}
        assert manifest["record_ids_failed"] == set()
        assert manifest["last_event"] == "run_completed"
        assert manifest["crash_exc_type"] is None

    def test_crashed_run_status_carries_exc_info(self, audit_tmp):
        with pytest.raises(RuntimeError), AuditLogger(run_id="r2", workflow="x") as log:
            log.event("touch", record_id="A")
            raise RuntimeError("kaboom")
        manifest = resume_from_audit("r2")
        assert manifest["status"] == "crashed"
        assert manifest["crash_exc_type"] == "RuntimeError"
        assert manifest["crash_exc_msg"] == "kaboom"
        # Record A WAS touched before the crash → in record_ids_seen
        # so the resume run knows to evaluate it.
        assert "A" in manifest["record_ids_seen"]

    def test_in_progress_run(self, audit_tmp):
        # Simulate an audit file that lacks the closing run_completed line
        # (e.g., kill -9 between events).
        log = AuditLogger(run_id="r3", workflow="x")
        log.__enter__()
        log.event("touch", record_id="A")
        # Don't call __exit__ — close fh manually to mimic abnormal termination.
        fh = log._fh
        assert fh is not None
        fh.close()
        log._fh = None
        manifest = resume_from_audit("r3")
        assert manifest["status"] == "in_progress"
        assert manifest["last_seq"] >= 2  # run_started + touch
        assert manifest["crash_exc_type"] is None

    def test_unknown_run_id_raises(self, audit_tmp):
        with pytest.raises(FileNotFoundError, match="no audit log for run_id"):
            resume_from_audit("nonexistent")

    def test_error_events_go_to_failed_set(self, audit_tmp):
        with AuditLogger(run_id="r4", workflow="x") as log:
            log.event("touch", record_id="A")
            log.event("error", record_id="B")  # B failed, must NOT be in seen
        manifest = resume_from_audit("r4")
        assert "A" in manifest["record_ids_seen"]
        assert "A" not in manifest["record_ids_failed"]
        assert "B" in manifest["record_ids_failed"]
        assert "B" not in manifest["record_ids_seen"]

    def test_silent_skip_events_are_in_seen_not_completed(self, audit_tmp):
        """§3.18 nurture_silent_skip carries record_id but is NOT a completion.
        F-PR-2's contract: record_ids_seen counts touches; callers narrow
        via record_ids_for_events with their workflow-specific completion set."""
        with AuditLogger(run_id="r5", workflow="x") as log:
            log.event("dm_sent", record_id="A")
            log.event("nurture_silent_skip", record_id="B")
        manifest = resume_from_audit("r5")
        assert manifest["record_ids_seen"] == {"A", "B"}
        # The caller filters to actual completions.
        actually_completed = record_ids_for_events("r5", {"dm_sent"})
        assert actually_completed == {"A"}


class TestListRuns:
    def test_empty_dir_returns_empty(self, audit_tmp):
        assert list_runs() == []

    def test_returns_newest_first(self, audit_tmp):
        with AuditLogger(run_id="aaa", workflow="x"):
            pass
        with AuditLogger(run_id="bbb", workflow="x"):
            pass
        paths = list_runs()
        # bbb wrote second so its mtime is newer.
        assert paths[0].name.endswith("-bbb.jsonl")
        assert paths[1].name.endswith("-aaa.jsonl")

    def test_filters_by_since_days(self, audit_tmp):
        # Create one file then artificially backdate its mtime to 10 days ago.
        with AuditLogger(run_id="old", workflow="x"):
            pass
        old_path = next(audit_tmp.glob("*-old.jsonl"))
        import os
        ts = datetime.now(UTC).timestamp() - 10 * 86400
        os.utime(old_path, (ts, ts))
        with AuditLogger(run_id="new", workflow="x"):
            pass
        within_7d = list_runs(since_days=7)
        assert len(within_7d) == 1
        assert within_7d[0].name.endswith("-new.jsonl")


class TestIterEvents:
    def test_yields_in_order(self, audit_tmp):
        with AuditLogger(run_id="i1", workflow="x") as log:
            log.event("a")
            log.event("b")
            log.event("c")
        events = list(iter_events("i1"))
        names = [e["event"] for e in events]
        assert names == ["run_started", "a", "b", "c", "run_completed"]

    def test_skips_empty_lines(self, audit_tmp):
        with AuditLogger(run_id="i2", workflow="x"):
            pass
        path = _find_audit_path("i2")
        # Append a blank line — defensive against partial-write crashes.
        with open(path, "a") as f:
            f.write("\n\n")
        # iter_events tolerates blank lines; no exception.
        events = list(iter_events("i2"))
        assert all("event" in e for e in events)

    def test_malformed_line_raises(self, audit_tmp):
        with AuditLogger(run_id="i3", workflow="x"):
            pass
        path = _find_audit_path("i3")
        with open(path, "a") as f:
            f.write("not valid json\n")
        with pytest.raises(json.JSONDecodeError):
            list(iter_events("i3"))


class TestAuditStats:
    def test_empty_dir(self, audit_tmp):
        stats = audit_stats()
        assert stats["run_count"] == 0
        assert stats["event_count_total"] == 0
        assert stats["event_count_by_type"] == {}

    def test_aggregates_completed_and_crashed(self, audit_tmp):
        with AuditLogger(run_id="ok1", workflow="x") as log:
            log.event("custom_a")
        with pytest.raises(RuntimeError), AuditLogger(run_id="boom1", workflow="x") as log:
            log.event("custom_b")
            raise RuntimeError("die")
        stats = audit_stats()
        assert stats["run_count"] == 2
        assert stats["completed_count"] == 1
        assert stats["crashed_count"] == 1
        assert stats["event_count_by_type"]["custom_a"] == 1
        assert stats["event_count_by_type"]["custom_b"] == 1
        assert stats["event_count_by_type"]["run_started"] == 2
        assert stats["malformed_line_count"] == 0

    def test_corrupt_line_doesnt_kill_rollup(self, audit_tmp):
        """A single malformed JSONL line in one file must not halt the
        cross-run aggregation. (iter_events is loud-by-design; audit_stats
        is resilient-by-design.)"""
        with AuditLogger(run_id="okA", workflow="x") as log:
            log.event("custom_a")
        # Inject garbage into a separate file.
        with AuditLogger(run_id="badA", workflow="x") as log:
            log.event("custom_b")
        bad_path = _find_audit_path("badA")
        with open(bad_path, "a") as f:
            f.write("not valid json\nstill not valid\n")
        stats = audit_stats()
        assert stats["run_count"] == 2
        assert stats["malformed_line_count"] == 2
        # custom_a + custom_b still aggregated despite the bad lines.
        assert stats["event_count_by_type"]["custom_a"] == 1
        assert stats["event_count_by_type"]["custom_b"] == 1


class TestRecordIdsForEvents:
    """The §3.18 / §3.10 consumer contract: a workflow's resume path
    narrows record_ids_seen to a specific completion-event vocabulary
    via this helper."""

    def test_returns_only_matching_event_record_ids(self, audit_tmp):
        with AuditLogger(run_id="rie1", workflow="x") as log:
            log.event("dm_sent", record_id="A")
            log.event("dm_sent", record_id="B")
            log.event("nurture_silent_skip", record_id="C")
            log.event("dry_run_would_send", record_id="D")
        sent = record_ids_for_events("rie1", {"dm_sent"})
        assert sent == {"A", "B"}

    def test_supports_multi_event_filter(self, audit_tmp):
        with AuditLogger(run_id="rie2", workflow="x") as log:
            log.event("dm_sent", record_id="A")
            log.event("connection_request_sent", record_id="B")
            log.event("nurture_silent_skip", record_id="C")
        completed = record_ids_for_events(
            "rie2", {"dm_sent", "connection_request_sent"}
        )
        assert completed == {"A", "B"}

    def test_empty_filter_returns_empty(self, audit_tmp):
        with AuditLogger(run_id="rie3", workflow="x") as log:
            log.event("dm_sent", record_id="A")
        assert record_ids_for_events("rie3", set()) == set()

    def test_skips_events_without_record_id(self, audit_tmp):
        with AuditLogger(run_id="rie4", workflow="x") as log:
            log.event("dm_sent", record_id="A")
            log.event("dm_sent")  # no record_id payload
        assert record_ids_for_events("rie4", {"dm_sent"}) == {"A"}


class TestCliCommands:
    """The 3 audit CLI commands: replay-run, audit-tail, audit-list,
    audit-stats. Operator-facing surfaces — exit codes matter."""

    def test_replay_run_prints_manifest(self, audit_tmp):
        from click.testing import CliRunner

        from cli import cli

        with AuditLogger(run_id="clirun1", workflow="x") as log:
            log.event("dm_sent", record_id="A")

        result = CliRunner().invoke(cli, ["replay-run", "clirun1"])
        assert result.exit_code == 0, result.output
        manifest = json.loads(result.output)
        assert manifest["run_id"] == "clirun1"
        assert manifest["status"] == "completed"
        assert "A" in manifest["record_ids_seen"]

    def test_replay_run_unknown_run_id_returns_nonzero(self, audit_tmp):
        from click.testing import CliRunner

        from cli import cli

        result = CliRunner().invoke(cli, ["replay-run", "nonexistent"])
        assert result.exit_code != 0
        assert "no audit log" in result.output.lower() or "nonexistent" in result.output

    def test_audit_tail_unknown_run_id_is_click_error(self, audit_tmp):
        """Engineer-QA #1: audit-tail must wrap FileNotFoundError as
        ClickException so the operator sees a clean error, not a Python
        traceback."""
        from click.testing import CliRunner

        from cli import cli

        result = CliRunner().invoke(cli, ["audit-tail", "--run-id", "nonexistent"])
        assert result.exit_code != 0
        # No Python traceback in the operator's terminal:
        assert "Traceback" not in result.output

    def test_audit_list_shows_recent_runs(self, audit_tmp):
        from click.testing import CliRunner

        from cli import cli

        with AuditLogger(run_id="listrun1", workflow="x"):
            pass
        with AuditLogger(run_id="listrun2", workflow="x"):
            pass

        result = CliRunner().invoke(cli, ["audit-list"])
        assert result.exit_code == 0, result.output
        assert "listrun1" in result.output
        assert "listrun2" in result.output
        assert "completed" in result.output

    def test_audit_list_surfaces_crash_message(self, audit_tmp):
        """Salesman-QA #1: the crashed-run summary should expose
        exc_type/exc_msg so the operator can decide rerun vs intervene
        without chasing the JSONL by hand."""
        from click.testing import CliRunner

        from cli import cli

        with pytest.raises(RuntimeError), AuditLogger(run_id="crashrun", workflow="x") as log:
            log.event("dm_sent", record_id="A")
            raise RuntimeError("LinkedIn rate limit hit")

        result = CliRunner().invoke(cli, ["audit-list"])
        assert result.exit_code == 0, result.output
        assert "crashed" in result.output
        assert "RuntimeError" in result.output
        assert "LinkedIn rate limit" in result.output
