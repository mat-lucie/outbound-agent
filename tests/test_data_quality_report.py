"""Tests for the Data Quality Report aggregator."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from models.data_quality_report import (
    EXIT_OK,
    EXIT_P0,
    EXIT_P1,
    P0_ALARM_SLUGS,
    P1_ALARM_SLUGS,
    DataQualityHalt,
    DQRMetrics,
    DQRReport,
    assert_no_open_p0_alarms,
    exit_code_for_severity,
)
from scripts.data_quality_report import (
    build_report,
    render_report,
    write_report,
)

# --- Fixture --------------------------------------------------------

@pytest.fixture
def mock_attio():
    """Mock AttioClient that takes route dispatch from a closure dict.

    Tests set `client.routes[<path-substring>] = handler_fn` to control
    what each Attio call returns. Defaults: every query returns [].
    """
    client = MagicMock()
    posted: list[dict] = []
    client.posted = posted  # type: ignore[attr-defined]
    client.routes: dict = {}  # type: ignore[attr-defined]

    def _request(method: str, path: str, json: dict | None = None, **_):
        for substr, handler in client.routes.items():
            if substr in path:
                return handler(method, path, json or {})
        # Default: every records/query returns empty.
        if "/records/query" in path:
            return {"data": []}
        if path.endswith("/records") and method == "POST":
            posted.append({"path": path, "body": json or {}})
            return {"data": {"id": {"record_id": f"rec-{len(posted)}"}}}
        return {"data": {}}

    client._request.side_effect = _request

    # query_list_entries used by legacy_archaeology collector.
    client.query_list_entries.return_value = []

    return client


def _route_count(handler_returns_count_for: dict[str, int]):
    """Build a route handler that returns N empty records depending on
    which queue-row `type` filter the query asks for.

    `handler_returns_count_for[slug] = N` → N records for that filter.
    Any other type → 0.
    """
    def _handle(method, path, body):
        type_filter = (
            body.get("filter", {}).get("type", {}).get("$eq")
        )
        n = handler_returns_count_for.get(type_filter, 0)
        return {"data": [{"id": {"record_id": f"r{i}"}} for i in range(n)]}
    return _handle


# --- Helpers --------------------------------------------------------

def _vals(**kwargs) -> dict:
    """Build an Attio `values` dict from kwargs in the [{value: X}] shape."""
    return {k: [{"value": v}] for k, v in kwargs.items() if v is not None}


# ====================================================================
# build_report — full happy + alarm paths
# ====================================================================

class TestBuildReport:
    def test_clean_run_no_alarms(self, mock_attio, monkeypatch):
        # ATTIO_LIST_ID must be set — `_collect_legacy_archaeology_count`
        # raises without it (no silent zero).
        monkeypatch.setenv("ATTIO_LIST_ID", "lst-x")
        report = build_report(mock_attio, today=date(2026, 5, 22))
        assert report.exit_severity() == "clean"
        assert report.p0_alarms_fired == []
        assert report.p1_alarms_fired == []
        m = report.metrics
        assert m.cohort_tagging_regression_count == 0
        assert m.write_owner_invariant_violated_count == 0
        assert m.migration_idempotency_regression_count == 0
        assert m.manual_reply_classification_gap_count == 0
        assert m.nurture_silent_skipped_count_7d == 0
        assert m.pipeline_starvation_open_count == 0
        assert m.back_pointer_failures_count_7d == 0
        assert m.legacy_archaeology_pool_count == 0

    def test_p0_cohort_tagging_regression_fires(self, mock_attio, monkeypatch):
        """A single open queue row of type='cohort_tagging_regression'
        → P0 fires → severity=p0 → exit code 70."""
        monkeypatch.setenv("ATTIO_LIST_ID", "lst-x")
        mock_attio.routes["/operator_review_queue/records/query"] = _route_count({
            "cohort_tagging_regression": 1,
        })
        report = build_report(mock_attio, today=date(2026, 5, 22))
        assert report.exit_severity() == "p0"
        assert "cohort_tagging_regression" in report.p0_alarms_fired
        assert report.metrics.cohort_tagging_regression_count == 1
        assert exit_code_for_severity(report.exit_severity()) == EXIT_P0

    def test_p0_write_owner_invariant_fires(self, mock_attio, monkeypatch):
        monkeypatch.setenv("ATTIO_LIST_ID", "lst-x")
        mock_attio.routes["/operator_review_queue/records/query"] = _route_count({
            "write_owner_invariant_violated": 3,
        })
        report = build_report(mock_attio, today=date(2026, 5, 22))
        assert "write_owner_invariant_violated" in report.p0_alarms_fired
        assert report.metrics.write_owner_invariant_violated_count == 3

    def test_p0_migration_idempotency_fires(self, mock_attio, monkeypatch):
        monkeypatch.setenv("ATTIO_LIST_ID", "lst-x")
        mock_attio.routes["/operator_review_queue/records/query"] = _route_count({
            "migration_idempotency_regression": 2,
        })
        report = build_report(mock_attio, today=date(2026, 5, 22))
        assert "migration_idempotency_regression" in report.p0_alarms_fired

    def test_p1_manual_reply_gap_fires_at_threshold(self, mock_attio, monkeypatch):
        """P1 manual_reply_classification_gap fires when the open-row
        count meets the threshold (synced with §10 halt-on-10 per M7).
        Below threshold → does not fire."""
        monkeypatch.setenv("ATTIO_LIST_ID", "lst-x")
        monkeypatch.setenv("OUTBOUND_MANUAL_REPLY_GAP_THRESHOLD", "10")
        # 10 open rows → threshold MET → P1 fires.
        mock_attio.routes["/operator_review_queue/records/query"] = _route_count({
            "manual_reply_classification_gap": 10,
        })
        report = build_report(mock_attio, today=date(2026, 5, 22))
        assert report.exit_severity() == "p1"
        assert "manual_reply_classification_gap" in report.p1_alarms_fired
        assert exit_code_for_severity(report.exit_severity()) == EXIT_P1

    def test_p1_below_threshold_does_not_fire(self, mock_attio, monkeypatch):
        monkeypatch.setenv("ATTIO_LIST_ID", "lst-x")
        monkeypatch.setenv("OUTBOUND_MANUAL_REPLY_GAP_THRESHOLD", "10")
        mock_attio.routes["/operator_review_queue/records/query"] = _route_count({
            "manual_reply_classification_gap": 9,  # below
        })
        report = build_report(mock_attio, today=date(2026, 5, 22))
        assert report.exit_severity() == "clean"
        assert report.p1_alarms_fired == []
        assert report.metrics.manual_reply_classification_gap_count == 9

    def test_p0_dominates_p1(self, mock_attio, monkeypatch):
        """When both P0 and P1 fire, severity = p0 (the dominant rule).
        Exit code = 70."""
        monkeypatch.setenv("ATTIO_LIST_ID", "lst-x")
        monkeypatch.setenv("OUTBOUND_MANUAL_REPLY_GAP_THRESHOLD", "5")
        mock_attio.routes["/operator_review_queue/records/query"] = _route_count({
            "cohort_tagging_regression": 1,
            "manual_reply_classification_gap": 5,
        })
        report = build_report(mock_attio, today=date(2026, 5, 22))
        assert report.exit_severity() == "p0"
        assert "cohort_tagging_regression" in report.p0_alarms_fired
        assert "manual_reply_classification_gap" in report.p1_alarms_fired
        assert exit_code_for_severity(report.exit_severity()) == EXIT_P0

    def test_nurture_silent_skipped_summed_from_daily_runs(self, mock_attio, monkeypatch):
        monkeypatch.setenv("ATTIO_LIST_ID", "lst-x")
        def _daily_runs(method, path, body):
            return {"data": [
                {"values": _vals(nurture_silent_skipped_count=3)},
                {"values": _vals(nurture_silent_skipped_count=2)},
                {"values": _vals(nurture_silent_skipped_count=7)},
            ]}
        mock_attio.routes["/daily_run/records/query"] = _daily_runs
        report = build_report(mock_attio, today=date(2026, 5, 22))
        assert report.metrics.nurture_silent_skipped_count_7d == 12

    def test_back_pointer_failures_counted_from_migration_runs(self, mock_attio, monkeypatch):
        """Migration Run rows where failure_details_pointer mentions a
        back-pointer failure count toward the observability metric."""
        monkeypatch.setenv("ATTIO_LIST_ID", "lst-x")
        def _migration_runs(method, path, body):
            return {"data": [
                {"values": _vals(
                    failure_details_pointer='[{"kind":"back_pointer_failure"}]',
                )},
                {"values": _vals(
                    failure_details_pointer='[{"kind":"other"}]',
                )},
                {"values": _vals(failure_details_pointer=None)},
                {"values": _vals(
                    failure_details_pointer='[{"kind":"back_pointer","record":"r1"}]',
                )},
            ]}
        mock_attio.routes["/migration_run/records/query"] = _migration_runs
        report = build_report(mock_attio, today=date(2026, 5, 22))
        assert report.metrics.back_pointer_failures_count_7d == 2

    def test_legacy_archaeology_counted_from_list_entries(self, mock_attio, monkeypatch):
        """LinkedIn Outreach entries with archaeology sentinel
        experiment_id_frozen_at count toward the observability metric."""
        monkeypatch.setenv("ATTIO_LIST_ID", "lst-x")
        mock_attio.query_list_entries.return_value = [
            {"entry_values": {
                "experiment_id_frozen_at": [{"value": "legacy_inferred_by_archaeology"}],
            }},
            {"entry_values": {
                "experiment_id_frozen_at": [{"value": "legacy_pure_unknown"}],
            }},
            {"entry_values": {
                "experiment_id_frozen_at": [{"value": "exp-2026-05-01-cell-1"}],  # not legacy
            }},
            {"entry_values": {}},  # no attr
        ]
        report = build_report(mock_attio, today=date(2026, 5, 22))
        assert report.metrics.legacy_archaeology_pool_count == 2


# ====================================================================
# Render
# ====================================================================

class TestRenderReport:
    def test_render_shows_severity(self):
        report = DQRReport(
            run_id="abc", generated_at="2026-05-22T00:00:00+00:00",
            period_start="2026-05-15", period_end="2026-05-22",
            metrics=DQRMetrics(cohort_tagging_regression_count=1),
            p0_alarms_fired=["cohort_tagging_regression"],
        )
        text = render_report(report)
        assert "severity: p0" in text
        assert "cohort_tagging_regression_count: 1" in text
        assert "abc" in text


# ====================================================================
# Consumer-side halt gate
# ====================================================================

class TestAssertNoOpenP0Alarms:
    def test_raises_data_quality_halt_when_p0_open(self):
        counts = {
            "cohort_tagging_regression": 2,
            "write_owner_invariant_violated": 0,
            "migration_idempotency_regression": 0,
        }
        with pytest.raises(DataQualityHalt) as exc:
            assert_no_open_p0_alarms(lambda slug: counts.get(slug, 0))
        assert "cohort_tagging_regression" in exc.value.alarm_slugs
        # P1 slugs must NOT be in the raised alarm list.
        for p1_slug in P1_ALARM_SLUGS:
            assert p1_slug not in exc.value.alarm_slugs

    def test_returns_none_when_clean(self):
        assert assert_no_open_p0_alarms(lambda _slug: 0) is None

    def test_collects_all_firing_p0(self):
        with pytest.raises(DataQualityHalt) as exc:
            assert_no_open_p0_alarms(lambda _slug: 1)
        assert set(exc.value.alarm_slugs) == set(P0_ALARM_SLUGS)


# ====================================================================
# Severity ↔ exit code mapping
# ====================================================================

class TestExitCodeMapping:
    def test_clean_to_zero(self):
        assert exit_code_for_severity("clean") == EXIT_OK

    def test_p1_to_one(self):
        assert exit_code_for_severity("p1") == EXIT_P1

    def test_p0_to_seventy(self):
        assert exit_code_for_severity("p0") == EXIT_P0

    def test_unknown_severity_raises(self):
        with pytest.raises(KeyError):
            exit_code_for_severity("garbage")  # type: ignore[arg-type]


# ====================================================================
# EXEMPT_SCRIPTS pinning — the script is read-only and must stay listed
# ====================================================================

class TestExemptScripts:
    def test_data_quality_report_is_exempt(self):
        from tests.test_migration_writer_compliance import EXEMPT_SCRIPTS

        assert "scripts/data_quality_report.py" in EXEMPT_SCRIPTS

    def test_migrate_attio_data_quality_report_is_not_exempt(self):
        from tests.test_migration_writer_compliance import EXEMPT_SCRIPTS

        assert "scripts/migrate_attio_data_quality_report.py" not in EXEMPT_SCRIPTS


# ====================================================================
# ATTIO_LIST_ID hard-fail (no silent zero on misconfigured deployment)
# ====================================================================

class TestAttioListIdRequired:
    def test_missing_list_id_raises_runtime_error(self, mock_attio, monkeypatch):
        monkeypatch.delenv("ATTIO_LIST_ID", raising=False)
        with pytest.raises(RuntimeError, match="ATTIO_LIST_ID"):
            build_report(mock_attio, today=date(2026, 5, 22))


# ====================================================================
# Nurture parse-error tracking (no silent swallow)
# ====================================================================

class TestNurtureParseErrors:
    def test_malformed_count_increments_separate_counter(
        self, mock_attio, monkeypatch,
    ):
        monkeypatch.setenv("ATTIO_LIST_ID", "lst-x")
        def _daily_runs(method, path, body):
            return {"data": [
                {"values": _vals(nurture_silent_skipped_count=3)},
                {"values": _vals(nurture_silent_skipped_count="not-a-number")},
                {"values": _vals(nurture_silent_skipped_count=7)},
                {"values": _vals(nurture_silent_skipped_count="garbage")},
            ]}
        mock_attio.routes["/daily_run/records/query"] = _daily_runs
        report = build_report(mock_attio, today=date(2026, 5, 22))
        assert report.metrics.nurture_silent_skipped_count_7d == 10
        assert report.metrics.nurture_count_parse_errors_7d == 2


# ====================================================================
# Slug-vocabulary subset assertion fires at import time
# ====================================================================

class TestSlugVocabulary:
    def test_p0_slugs_are_in_escalation_types(self):
        from workflows.escalation_schemas import ESCALATION_TYPES_SET

        for slug in P0_ALARM_SLUGS:
            assert slug in ESCALATION_TYPES_SET

    def test_p1_slugs_are_in_escalation_types(self):
        from workflows.escalation_schemas import ESCALATION_TYPES_SET

        for slug in P1_ALARM_SLUGS:
            assert slug in ESCALATION_TYPES_SET


# ====================================================================
# DQRReport rejects unknown slugs at construction
# ====================================================================

class TestDQRReportSlugValidation:
    def test_rejects_unknown_p0_slug(self):
        with pytest.raises(ValueError, match="unknown slug"):
            DQRReport(
                run_id="x", generated_at="2026-05-22T00:00:00Z",
                period_start="2026-05-15", period_end="2026-05-22",
                metrics=DQRMetrics(),
                p0_alarms_fired=["not_a_real_slug"],
            )

    def test_rejects_unknown_p1_slug(self):
        with pytest.raises(ValueError, match="unknown slug"):
            DQRReport(
                run_id="x", generated_at="2026-05-22T00:00:00Z",
                period_start="2026-05-15", period_end="2026-05-22",
                metrics=DQRMetrics(),
                p1_alarms_fired=["banana"],
            )


# ====================================================================
# write_report — payload shape + idempotency-on-re-run
# ====================================================================

class TestWriteReport:
    def _report(self, *, p0=(), p1=()) -> DQRReport:
        return DQRReport(
            run_id="rid-1", generated_at="2026-05-22T12:00:00+00:00",
            period_start="2026-05-15", period_end="2026-05-22",
            metrics=DQRMetrics(cohort_tagging_regression_count=len(p0)),
            p0_alarms_fired=list(p0),
            p1_alarms_fired=list(p1),
        )

    def test_writes_one_row_per_call(self, mock_attio):
        report = self._report()
        write_report(mock_attio, report)
        write_report(mock_attio, report)
        dqr_posts = [p for p in mock_attio.posted if "data_quality_report" in p["path"]]
        assert len(dqr_posts) == 2

    def test_payload_contains_all_required_attrs(self, mock_attio):
        report = self._report(p0=["cohort_tagging_regression"])
        write_report(mock_attio, report)
        attrs = mock_attio.posted[0]["body"]["data"]["values"]
        for key in [
            "run_id", "generated_at", "period_start", "period_end",
            "cohort_tagging_regression_count",
            "write_owner_invariant_violated_count",
            "migration_idempotency_regression_count",
            "manual_reply_classification_gap_count",
            "nurture_silent_skipped_count_7d",
            "nurture_count_parse_errors_7d",
            "pipeline_starvation_open_count",
            "back_pointer_failures_count_7d",
            "legacy_archaeology_pool_count",
            "p0_alarms_fired", "p1_alarms_fired", "report_text",
        ]:
            assert key in attrs, f"write_report missing attr: {key}"

    def test_empty_alarms_serialize_as_none_sentinel(self, mock_attio):
        report = self._report()
        write_report(mock_attio, report)
        attrs = mock_attio.posted[0]["body"]["data"]["values"]
        assert attrs["p0_alarms_fired"] == "none"
        assert attrs["p1_alarms_fired"] == "none"

    def test_missing_record_id_raises(self, mock_attio):
        # Override the default route handler to return a 2xx with no id.
        def _no_id(method, path, body):
            return {"data": {}}
        mock_attio.routes["/data_quality_report/records"] = _no_id
        report = self._report()
        with pytest.raises(RuntimeError, match="record_id"):
            write_report(mock_attio, report)


# ====================================================================
# DataQualityHalt message carries recovery instructions
# ====================================================================

class TestDataQualityHaltMessage:
    def test_message_includes_recovery_path(self):
        halt = DataQualityHalt(["cohort_tagging_regression"])
        msg = str(halt)
        assert "Attio" in msg
        assert "Operator Review Queue" in msg
        assert "resolved" in msg or "dismissed" in msg


# ====================================================================
# Render places severity + firing slugs at the top
# ====================================================================

class TestRenderHeader:
    def test_p0_appears_in_first_two_lines(self):
        report = DQRReport(
            run_id="rid-x", generated_at="2026-05-22T00:00:00Z",
            period_start="2026-05-15", period_end="2026-05-22",
            metrics=DQRMetrics(cohort_tagging_regression_count=1),
            p0_alarms_fired=["cohort_tagging_regression"],
        )
        lines = render_report(report).splitlines()
        assert "severity: p0" in lines[0]
        assert "P0 BLOCKED" in lines[1]


# ====================================================================
# parse_entry round-trip for experiment_id_frozen_at
# ====================================================================

class TestParseEntryExperimentIdFrozenAt:
    def test_extracted_for_archaeology_sentinel(self):
        from clients.attio import AttioClient

        raw = {
            "id": {"entry_id": "e1", "record_id": "r1"},
            "entry_values": {
                "stage": [{"status": {"title": "Prospect"}}],
                "experiment_id_frozen_at": [{"value": "legacy_pure_unknown"}],
            },
        }
        parsed = AttioClient.parse_entry(raw)
        assert parsed["experiment_id_frozen_at"] == "legacy_pure_unknown"

    def test_missing_attr_returns_none(self):
        from clients.attio import AttioClient

        raw = {
            "id": {"entry_id": "e2", "record_id": "r2"},
            "entry_values": {
                "stage": [{"status": {"title": "Prospect"}}],
            },
        }
        parsed = AttioClient.parse_entry(raw)
        assert parsed["experiment_id_frozen_at"] is None
