"""Phase timers — coarse latency evidence for the send-dms latency work.

Covers: record_phase accumulation, render output, the None-safe helper,
and that preload_pipeline_persons splits its two latency buckets
(parallel person fetch vs serial company resolve).
"""

import os
from datetime import date
from unittest.mock import MagicMock, patch

from tests.test_daily_check_helpers import _StubAttio
from workflows.metrics import (
    DailyRunMetrics,
    phase_timer,
    record_phase_or_skip,
)
from workflows.record_cache import RecordCache, preload_pipeline_persons


class TestRecordPhase:
    def test_record_phase_accumulates_repeat_names(self):
        m = DailyRunMetrics()
        m.record_phase("pb_send_loop", 1.5)
        m.record_phase("pb_send_loop", 2.0)
        assert m.phase_seconds["pb_send_loop"] == 3.5

    def test_render_includes_phase_timings_when_present(self):
        m = DailyRunMetrics()
        m.record_phase("dm_list_scan", 12.34)
        out = m.render()
        assert "phase timings:" in out
        assert "dm_list_scan: 12.3s" in out

    def test_render_omits_phase_block_when_empty(self):
        m = DailyRunMetrics()
        assert "phase timings:" not in m.render()

    def test_phase_seconds_survives_to_dict(self):
        m = DailyRunMetrics()
        m.record_phase("queue_build", 0.5)
        assert m.to_dict()["phase_seconds"] == {"queue_build": 0.5}

    def test_record_phase_or_skip_none_metrics_is_noop(self):
        # Must not raise — workflow call sites pass metrics=None in
        # standalone/debug runs.
        record_phase_or_skip(None, "anything", phase_timer())

    def test_record_phase_or_skip_records_elapsed(self):
        m = DailyRunMetrics()
        record_phase_or_skip(m, "list_scan_preload_ids", phase_timer())
        assert "list_scan_preload_ids" in m.phase_seconds
        assert m.phase_seconds["list_scan_preload_ids"] >= 0.0


class TestPreloadPhaseSplit:
    """preload_pipeline_persons times the pool fetch and the serial
    company-resolve loop separately — the two have different remedies."""

    def _stub(self):
        records = {"rid-1": {"id": {"record_id": "rid-1"}, "values": {}}}
        info = {
            "rid-1": (
                "Name", "Co", "https://linkedin.com/in/x", None, "Title",
            ),
        }
        return _StubAttio(records, info)

    def test_preload_records_both_buckets(self):
        attio = self._stub()
        m = DailyRunMetrics()
        cache = RecordCache(attio)  # type: ignore[arg-type]
        primed = preload_pipeline_persons(
            attio, cache, {"rid-1"}, metrics=m,  # type: ignore[arg-type]
        )
        assert primed == 1
        assert "preload_person_fetch_parallel" in m.phase_seconds
        assert "preload_company_resolve_serial" in m.phase_seconds

    def test_preload_none_metrics_still_works(self):
        attio = _StubAttio({}, {})
        cache = RecordCache(attio)  # type: ignore[arg-type]
        assert preload_pipeline_persons(
            attio, cache, {"rid-1"}, metrics=None,  # type: ignore[arg-type]
        ) == 0

    def test_preload_fetch_failure_still_times_fetch_bucket(self):
        attio = _StubAttio({}, {})
        m = DailyRunMetrics()
        cache = RecordCache(attio)  # type: ignore[arg-type]
        with patch.object(
            attio, "bulk_fetch_persons_by_record_ids",
            side_effect=RuntimeError("boom"),
        ):
            assert preload_pipeline_persons(
                attio, cache, {"rid-1"}, metrics=m,  # type: ignore[arg-type]
            ) == 0
        # finally-block: the failed fetch's elapsed time is still recorded.
        assert "preload_person_fetch_parallel" in m.phase_seconds
        assert "preload_company_resolve_serial" not in m.phase_seconds


class TestRunDmSequencingSeams:
    """End-to-end pin on the daily_check.py timer seams: a dry-run drive of
    run_dm_sequencing must land all three pre-send phase keys. Guards against
    a future early return slipping between a phase_timer() start and its
    record_phase_or_skip (the risk is pairing, not the measured values).
    """

    def test_dry_run_records_pre_send_phases(self):
        from tests.test_integration import _attio_with_full_schema
        from tests.test_run_dm_sequencing_trim import _entry, _fake_daily_run
        from workflows.daily_check import run_dm_sequencing

        env = {
            "ATTIO_LIST_ID": "list-001", "ATTIO_API_KEY": "fake",
            "PHANTOMBUSTER_API_KEY": "fake", "PB_LI_SESSION_COOKIE": "fake-cookie",
            "PB_LI_USER_AGENT": "TestAgent/1.0", "GSHEET_AUTOCONNECT_ID": "fake-sheet-id",
        }
        entries = [_entry("a0", "Accepted", "2026-05-18", dm_step=0)]
        cache = MagicMock()
        cache.get.return_value = (
            "Name", "Co", "https://www.linkedin.com/in/a0", "manufacturing", "Director",
        )
        m = DailyRunMetrics()
        with patch.dict(os.environ, env), \
                patch("workflows.daily_check._get_all_entries_with_raw",
                      return_value=([], entries)), \
                patch("workflows.daily_check.write_prospects_to_sheet",
                      return_value="https://sheet/x"), \
                patch("workflows.daily_check._pb_session_args", return_value={}), \
                patch("workflows.daily_check.date") as mock_date:
            mock_date.today.return_value = date(2026, 5, 20)
            mock_date.fromisoformat = date.fromisoformat
            attio = _attio_with_full_schema()
            pb = MagicMock()
            pb.download_result_csv.return_value = ""
            run_dm_sequencing(
                attio, pb, "sender-id",
                daily_run=_fake_daily_run(remaining=30),
                dry_run=True, auto_confirm=True, cache=cache,
                metrics=m,
            )
        assert {"dm_list_scan", "dedup_index_build", "queue_build"} <= set(
            m.phase_seconds
        )
