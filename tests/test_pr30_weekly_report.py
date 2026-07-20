"""Tests for PR-30 — weekly KPI report sidecar + --send opt-in + deal staleness.

Builds B-SW-REPORT + B-SW-DEAL-STALE + B-GW-DASHBOARD.

Coverage:
  * `KPISnapshot` TypedDict shape + week_starting/window/measurement_basis
  * `DealStalenessRule` env-override + evaluate semantics
  * `compute_active_deals` applies staleness, tracks stale_count
  * `compute_persona_funnels` aggregates cumulative cadence funnel
  * `PersonaFunnel.acceptance_rate` / `response_rate` math
  * Prior-period compare from previous Attio snapshot
  * `Weekly KPI Snapshot` sidecar write always (incl. dry-run)
  * Resend failure → `resend_delivery_failed` queue + sidecar
    survives
  * `run_weekly_report` returns KPISnapshot dict
  * Registry + manifest invariants
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

from models.pipeline import DealStage, PipelineStage
from workflows.escalation_schemas import (
    ESCALATION_SCHEMAS,
    ESCALATION_TYPES_SET,
    ResendDeliveryFailedPayload,
)
from workflows.weekly_report import (
    DEAL_STALENESS_DAYS_DEFAULT,
    DealStalenessRule,
    KPISnapshot,
    PersonaFunnel,
    _compute_prior_period_compare,
    _load_prior_snapshot,
    _monday_of,
    _open_resend_delivery_failed_row,
    _parse_deal_activity,
    _patch_resend_message_id,
    _supersede_existing_snapshot,
    _upsert_kpi_snapshot,
    compute_active_deals,
    compute_kpis,
    compute_persona_funnels,
    run_weekly_report,
)

# -- DealStalenessRule ---------------------------------------------------


class TestDealStalenessRule:
    def test_default_threshold(self):
        assert DealStalenessRule().days_threshold == DEAL_STALENESS_DAYS_DEFAULT

    def test_from_env_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("OUTBOUND_DEAL_STALENESS_DAYS", raising=False)
        rule = DealStalenessRule.from_env()
        assert rule.days_threshold == DEAL_STALENESS_DAYS_DEFAULT

    def test_from_env_override(self, monkeypatch):
        monkeypatch.setenv("OUTBOUND_DEAL_STALENESS_DAYS", "45")
        rule = DealStalenessRule.from_env()
        assert rule.days_threshold == 45

    def test_from_env_invalid_falls_back_to_default(self, monkeypatch, caplog):
        monkeypatch.setenv("OUTBOUND_DEAL_STALENESS_DAYS", "not-an-int")
        with caplog.at_level("WARNING", logger="workflows.weekly_report"):
            rule = DealStalenessRule.from_env()
        assert rule.days_threshold == DEAL_STALENESS_DAYS_DEFAULT
        assert any(
            "OUTBOUND_DEAL_STALENESS_DAYS" in r.message
            for r in caplog.records
        )

    def test_evaluate_unknown_on_none(self):
        rule = DealStalenessRule(days_threshold=21)
        assert rule.evaluate(None, date.today()) == "unknown"

    def test_evaluate_fresh_below_threshold(self):
        rule = DealStalenessRule(days_threshold=21)
        today = date.today()
        assert rule.evaluate(today - timedelta(days=5), today) == "fresh"

    def test_evaluate_stale_at_threshold(self):
        rule = DealStalenessRule(days_threshold=21)
        today = date.today()
        # >=21d is stale (inclusive).
        assert rule.evaluate(today - timedelta(days=21), today) == "stale"

    def test_evaluate_stale_above_threshold(self):
        rule = DealStalenessRule(days_threshold=21)
        today = date.today()
        assert rule.evaluate(today - timedelta(days=90), today) == "stale"


# -- _parse_deal_activity -----------------------------------------------


TODAY = date(2026, 7, 2)


class TestParseDealActivity:
    """`_parse_deal_activity` delegates to the Follow-up Radar's resolver
    (PR-218).

    The pre-radar version read `last_activity_at`/`updated_at` — keys
    `AttioClient.parse_deal` never emitted — so EVERY deal resolved
    "unknown" (silent bug). Precedence now mirrors
    `followup_radar._deal_recency`: verified touch > person-interaction
    join > created_at > None.
    """

    def test_verified_touch_wins_even_over_newer_interaction(self):
        d = {
            "last_verified_touch": "2026-03-20",
            "associated_people": ["p1"],
            "created_at": "2026-01-01",
        }
        interactions = {"p1": date(2026, 6, 30)}
        assert _parse_deal_activity(d, interactions, TODAY) == date(2026, 3, 20)

    def test_interaction_join_when_no_verified_touch(self):
        d = {"associated_people": ["p1", "p2"], "created_at": "2026-01-01"}
        interactions = {"p1": date(2026, 5, 1), "p2": date(2026, 6, 2)}
        assert _parse_deal_activity(d, interactions, TODAY) == date(2026, 6, 2)

    def test_falls_back_to_created_at(self):
        d = {"created_at": "2026-01-01"}
        assert _parse_deal_activity(d, {}, TODAY) == date(2026, 1, 1)

    def test_returns_none_when_nothing_datable(self):
        assert _parse_deal_activity({}, {}, TODAY) is None

    def test_handles_iso_datetime_created_at(self):
        d = {"created_at": "2026-05-22T10:30:00Z"}
        assert _parse_deal_activity(d, {}, TODAY) == date(2026, 5, 22)

    def test_unparseable_verified_touch_falls_to_created_at(self):
        d = {"last_verified_touch": "garbage", "created_at": "2026-01-01"}
        assert _parse_deal_activity(d, {}, TODAY) == date(2026, 1, 1)

    def test_future_verified_touch_tomorrow_clamps_to_today(self):
        # UTC skew: a stamp dated 'tomorrow' is legitimate and clamps.
        d = {"last_verified_touch": (TODAY + timedelta(days=1)).isoformat()}
        assert _parse_deal_activity(d, {}, TODAY) == TODAY

    def test_far_future_verified_touch_ignored(self):
        # Hand-edited future stamp must not hide staleness — falls through.
        d = {"last_verified_touch": "2027-07-02", "created_at": "2026-01-01"}
        assert _parse_deal_activity(d, {}, TODAY) == date(2026, 1, 1)

    def test_agrees_with_radar_resolver(self):
        # The whole point of the fix: the weekly report and the radar must
        # resolve the SAME deal to the SAME recency date.
        from workflows.followup_radar import resolve_deal_recency

        deals = [
            {"last_verified_touch": "2026-03-20", "created_at": "2026-01-01"},
            {"associated_people": ["p1"], "created_at": "2026-01-01"},
            {"created_at": "2026-01-01"},
            {},
        ]
        interactions = {"p1": date(2026, 6, 2)}
        for d in deals:
            assert _parse_deal_activity(d, interactions, TODAY) == (
                resolve_deal_recency(d, interactions, TODAY)[0]
            )


# -- compute_active_deals + DealStalenessRule integration ---------------


class TestComputeActiveDeals:
    def test_in_progress_deal_with_stale_verified_touch(self):
        today = date(2026, 5, 22)
        deals = [{
            "name": "Stale Co",
            "value": 50_000,
            "currency": "USD",
            "country": "MX",
            "stage": DealStage.IN_PROGRESS.value,
            "last_verified_touch": (today - timedelta(days=60)).isoformat(),
        }]
        result = compute_active_deals(deals, today=today)
        assert result["in_progress_count"] == 1
        assert result["stale_count"] == 1
        assert result["in_progress"][0]["stale_status"] == "stale"

    def test_in_progress_deal_with_fresh_verified_touch(self):
        today = date(2026, 5, 22)
        deals = [{
            "name": "Active Co",
            "value": 50_000,
            "stage": DealStage.IN_PROGRESS.value,
            "last_verified_touch": (today - timedelta(days=5)).isoformat(),
        }]
        result = compute_active_deals(deals, today=today)
        assert result["stale_count"] == 0
        assert result["in_progress"][0]["stale_status"] == "fresh"

    def test_old_deal_with_no_touch_data_is_stale_by_age(self):
        # Behavior change vs the phantom-key bug: created_at (deal age) now
        # counts as tier-3 recency, so an untouched old deal reads STALE
        # instead of silently "unknown".
        today = date(2026, 5, 22)
        deals = [{
            "name": "Aging Co",
            "value": 25_000,
            "stage": DealStage.IN_PROGRESS.value,
            "created_at": (today - timedelta(days=90)).isoformat(),
        }]
        result = compute_active_deals(deals, today=today)
        assert result["in_progress"][0]["stale_status"] == "stale"
        assert result["stale_count"] == 1

    def test_interactions_join_freshens_deal(self):
        today = date(2026, 5, 22)
        deals = [{
            "name": "Joined Co",
            "value": 25_000,
            "stage": DealStage.IN_PROGRESS.value,
            "associated_people": ["p1"],
            "created_at": (today - timedelta(days=90)).isoformat(),
        }]
        interactions = {"p1": today - timedelta(days=3)}
        result = compute_active_deals(deals, today=today, interactions=interactions)
        assert result["in_progress"][0]["stale_status"] == "fresh"
        # Without the join data the same deal falls back to created_at.
        result = compute_active_deals(deals, today=today)
        assert result["in_progress"][0]["stale_status"] == "stale"

    def test_in_progress_deal_with_no_activity_metadata(self):
        deals = [{
            "name": "Mystery Co",
            "value": 25_000,
            "stage": DealStage.IN_PROGRESS.value,
        }]
        result = compute_active_deals(deals, today=date(2026, 5, 22))
        assert result["in_progress"][0]["stale_status"] == "unknown"
        assert result["stale_count"] == 0

    def test_blank_value_fallback_to_acv(self):
        deals = [{
            "name": "NoVal Co",
            "value": None,
            "stage": DealStage.IN_PROGRESS.value,
        }]
        result = compute_active_deals(deals, today=date(2026, 5, 22))
        assert result["blank_value_count"] == 1
        assert result["in_progress"][0]["value_is_fallback"] is True

    def test_staleness_threshold_via_rule_override(self):
        today = date(2026, 5, 22)
        rule = DealStalenessRule(days_threshold=10)
        deals = [{
            "name": "Edge Co",
            "value": 30_000,
            "stage": DealStage.IN_PROGRESS.value,
            "last_verified_touch": (today - timedelta(days=11)).isoformat(),
        }]
        result = compute_active_deals(deals, today=today, staleness_rule=rule)
        # Threshold tightened to 10d → 11d-old deal is stale.
        assert result["in_progress"][0]["stale_status"] == "stale"

    def test_parse_deal_output_resolves_staleness_end_to_end(self):
        # Regression guard for the original bug: a REAL parse_deal dict
        # (not a hand-built one) must resolve to a known staleness.
        today = date(2026, 5, 22)
        record = {
            "id": {"record_id": "rec_e2e"},
            "created_at": "2026-01-01T09:00:00Z",
            "values": {
                "name": [{"value": "E2E Co"}],
                "stage": [{"status": {"title": DealStage.IN_PROGRESS.value}}],
                "last_verified_touch": [{"value": (today - timedelta(days=2)).isoformat()}],
            },
        }
        from clients.attio import AttioClient
        result = compute_active_deals([AttioClient.parse_deal(record)], today=today)
        assert result["in_progress"][0]["stale_status"] == "fresh"
        assert result["unknown_count"] == 0

    def test_non_in_progress_stages_not_staleness_evaluated(self):
        deals = [
            {"stage": DealStage.LEAD.value, "value": 10_000},
            {"stage": DealStage.LOST.value, "value": 5_000},
        ]
        result = compute_active_deals(deals, today=date(2026, 5, 22))
        assert result["in_progress_count"] == 0
        assert result["lead_count"] == 1
        assert result["lost_count"] == 1


# -- compute_persona_funnels --------------------------------------------


class TestComputePersonaFunnels:
    def _entry(self, persona: str, stage: str) -> dict:
        # parse_entry reads `entry_values` (Attio list-entry shape),
        # not `values`. Stage uses the option-title form.
        return {
            "entry_values": {
                "persona": [{"value": persona}],
                "stage": [{"status": {"title": stage}}],
            },
        }

    def test_funnel_is_cumulative_through_stages(self):
        entries = [
            self._entry("operations_leaders", PipelineStage.QUALIFIED.value),
            self._entry("operations_leaders", PipelineStage.RESPONDED.value),
            self._entry("operations_leaders", PipelineStage.DM2_SENT.value),
        ]
        funnels = compute_persona_funnels(entries)
        assert len(funnels) == 1
        ops = funnels[0]
        assert ops.persona == "operations_leaders"
        # All 3 prospects are at-least-DM1.
        assert ops.dm1_sent == 3
        # 2 are at-least-DM2 (RESPONDED + QUALIFIED + DM2_SENT all >= dm2).
        assert ops.dm2_sent == 3
        # Only QUALIFIED qualifies.
        assert ops.qualified == 1

    def test_multiple_personas_sorted(self):
        entries = [
            self._entry("digitalization_champions", PipelineStage.DM1_SENT.value),
            self._entry("operations_leaders", PipelineStage.RESPONDED.value),
        ]
        funnels = compute_persona_funnels(entries)
        # Sorted alphabetically.
        assert [f.persona for f in funnels] == [
            "digitalization_champions", "operations_leaders",
        ]

    def test_persona_funnel_acceptance_rate(self):
        pf = PersonaFunnel(
            persona="ops", invited=10, accepted=4,
            dm1_sent=4, dm2_sent=2, dm3_sent=1,
            responded=1, qualified=0,
        )
        assert pf.acceptance_rate == 0.4

    def test_persona_funnel_response_rate(self):
        # Post-fold (__post_init__ invariant): counters must be
        # monotone non-increasing. Adjusted fixture has dm3 >= responded.
        pf = PersonaFunnel(
            persona="ops", invited=10, accepted=8,
            dm1_sent=8, dm2_sent=6, dm3_sent=4,
            responded=2, qualified=0,
        )
        # 2 / (8 + 6 + 4) = 2/18 = 1/9
        assert pf.response_rate == pytest.approx(2 / 18)

    def test_persona_funnel_rejects_monotone_violation(self):
        # type-design QA fold: __post_init__ enforces the cumulative
        # cadence invariant. Constructing with responded > dm3_sent
        # would silently misrepresent the funnel pre-fold.
        with pytest.raises(ValueError, match="monotone non-increasing"):
            PersonaFunnel(
                persona="ops", invited=10, accepted=4,
                dm1_sent=4, dm2_sent=2, dm3_sent=1,
                responded=2, qualified=0,
            )

    def test_acceptance_rate_zero_when_no_invites(self):
        pf = PersonaFunnel("ops", 0, 0, 0, 0, 0, 0, 0)
        assert pf.acceptance_rate == 0.0
        assert pf.response_rate == 0.0


# -- _monday_of helper --------------------------------------------------


class TestMondayOf:
    def test_friday(self):
        assert _monday_of(date(2026, 5, 22)) == date(2026, 5, 18)

    def test_monday_returns_self(self):
        assert _monday_of(date(2026, 5, 18)) == date(2026, 5, 18)


# -- Prior-period compare -----------------------------------------------


class TestPriorPeriodCompare:
    def test_returns_none_when_no_prior(self):
        assert _compute_prior_period_compare({"x": 1.0}, None) is None

    def test_computes_delta_per_key(self):
        prior = {"totals": {"qualified": 5.0, "calls_booked": 2.0}}
        current = {"qualified": 7.0, "calls_booked": 1.0}
        compare = _compute_prior_period_compare(current, prior)
        assert compare is not None
        assert compare["qualified"]["delta"] == 2.0
        assert compare["calls_booked"]["delta"] == -1.0

    def test_skips_non_numeric_prior(self):
        prior = {"totals": {"qualified": "not-numeric"}}
        assert _compute_prior_period_compare({"qualified": 5.0}, prior) is None


@pytest.fixture
def kpi_dir(tmp_path, monkeypatch):
    """Point the filesystem sidecar at a tmp dir so tests never write
    into the repo's reports/weekly-kpi/ cache."""
    d = tmp_path / "weekly-kpi"
    monkeypatch.setattr("workflows.weekly_report.REPORTS_DIR", d)
    return d


class TestLoadPriorSnapshot:
    def test_returns_none_when_file_absent(self, kpi_dir, caplog):
        # Expected cold/gap case: prior week's file does not exist →
        # return None with NO warning/error log noise.
        with caplog.at_level("WARNING", logger="workflows.weekly_report"):
            assert _load_prior_snapshot(date(2026, 5, 18)) is None
        assert caplog.records == []

    def test_reads_prior_week_file(self, kpi_dir):
        # _load_prior_snapshot reads <week_starting - 7d>.json and
        # returns the parsed kpi_snapshot_json dict.
        kpi_dir.mkdir(parents=True, exist_ok=True)
        snapshot_blob = {"totals": {"qualified": 3.0}}
        prior_week = (date(2026, 5, 18) - timedelta(days=7)).isoformat()
        (kpi_dir / f"{prior_week}.json").write_text(
            json.dumps({"kpi_snapshot_json": json.dumps(snapshot_blob)}),
        )
        result = _load_prior_snapshot(date(2026, 5, 18))
        assert result == snapshot_blob

    def test_corrupt_file_propagates(self, kpi_dir):
        # A corrupt JSON file must NOT be swallowed (would hide data
        # damage) — JSONDecodeError propagates.
        kpi_dir.mkdir(parents=True, exist_ok=True)
        prior_week = (date(2026, 5, 18) - timedelta(days=7)).isoformat()
        (kpi_dir / f"{prior_week}.json").write_text("{not valid json")
        with pytest.raises(json.JSONDecodeError):
            _load_prior_snapshot(date(2026, 5, 18))


# -- Sidecar write ------------------------------------------------------


class TestUpsertKpiSnapshot:
    def _snapshot(self) -> KPISnapshot:
        return {
            "window": "2026-05-16..2026-05-22",
            "measurement_basis": "all_time_snapshot_asof_2026-05-22",
            "prior_period_compare": None,
            "week_starting": "2026-05-18",
            "totals": {"qualified": 3.0},
            "stage_counts": {"QUALIFIED": 3},
            "persona_funnels": [],
            "active_deals": {"in_progress_count": 0},
            "stale_threshold_source": "default",
            "notes": "",
            "outreach_volume": {"totals": {}, "per_day": {}, "window_days": 7,
                                "days_with_rows": 0, "malformed_reads": 0,
                                "note": "", "coverage_warning": "",
                                "source": "crm_daily_run_ledger"},
        }

    def test_writes_file_at_week_path(self, kpi_dir):
        path = _upsert_kpi_snapshot(
            self._snapshot(),
            persona_funnels=[],
            active_deals={"in_progress_count": 0},
            sent_to="operator@example.com",
            resend_message_id="",
        )
        expected = kpi_dir / "2026-05-18.json"
        assert path == str(expected)
        assert expected.exists()

    def test_same_week_rerun_supersedes_first_and_writes_new(self, kpi_dir):
        # Fix-5 (audit): a second run for the same week must NOT silently
        # destroy the first run's data. The first file is copied aside as
        # <week>_superseded_1.json; the canonical <week>.json gets the new
        # data. week-over-week delta logic reads only <week>.json.
        first = _upsert_kpi_snapshot(
            self._snapshot(),
            persona_funnels=[],
            active_deals={"in_progress_count": 0},
            sent_to="first@example.com",
            resend_message_id="",
        )
        second = _upsert_kpi_snapshot(
            self._snapshot(),
            persona_funnels=[],
            active_deals={"in_progress_count": 0},
            sent_to="second@example.com",
            resend_message_id="",
        )
        assert first == second  # same canonical path
        # Canonical file has the new (second-run) data.
        record = json.loads((kpi_dir / "2026-05-18.json").read_text())
        assert record["report_email_sent_to"] == "second@example.com"
        # First-run audit copy preserved.
        superseded = kpi_dir / "2026-05-18_superseded_1.json"
        assert superseded.exists()
        old_record = json.loads(superseded.read_text())
        assert old_record["report_email_sent_to"] == "first@example.com"

    def test_file_contents_match_record_shape(self, kpi_dir):
        path = _upsert_kpi_snapshot(
            self._snapshot(),
            persona_funnels=[
                PersonaFunnel("ops", 10, 4, 4, 2, 1, 1, 0),
            ],
            active_deals={"in_progress_count": 2, "stale_count": 1},
            sent_to="operator@example.com",
            resend_message_id="msg-1",
        )
        record = json.loads(open(path).read())
        for key in (
            "week_starting", "kpi_snapshot_json", "persona_funnels_json",
            "active_deals_json", "measurement_basis", "report_email_sent_to",
            "report_resend_message_id",
        ):
            assert key in record, f"missing record key {key}"
        assert record["week_starting"] == "2026-05-18"
        assert record["report_email_sent_to"] == "operator@example.com"
        assert record["report_resend_message_id"] == "msg-1"
        json.loads(record["kpi_snapshot_json"])
        funnels = json.loads(record["persona_funnels_json"])
        assert len(funnels) == 1
        assert funnels[0]["persona"] == "ops"

    def test_no_escalation_when_object_absent(self, kpi_dir):
        # The whole point of the move: the filesystem sidecar never
        # touches Attio, so a missing weekly_kpi_snapshot object can
        # never open an attio_schema_missing queue row again.
        with patch("workflows.weekly_report.escalate") as mock_escalate:
            _upsert_kpi_snapshot(
                self._snapshot(),
                persona_funnels=[],
                active_deals={},
                sent_to="",
                resend_message_id="",
            )
        mock_escalate.assert_not_called()

    def test_post_send_patch_updates_file(self, kpi_dir):
        # The upsert returns a real path; the post-send patch reads it,
        # sets report_resend_message_id, and atomically writes back.
        path = _upsert_kpi_snapshot(
            self._snapshot(),
            persona_funnels=[],
            active_deals={},
            sent_to="operator@example.com",
            resend_message_id="",
        )
        _patch_resend_message_id(path, "msg-abc-123")
        record = json.loads(open(path).read())
        assert record["report_resend_message_id"] == "msg-abc-123"
        # Other fields untouched.
        assert record["report_email_sent_to"] == "operator@example.com"


# -- Resend failure → queue row ----------------------------------------


class TestResendFailureEscalation:
    def test_open_resend_delivery_failed_row_payload(self):
        from workflows import weekly_report as wr
        attio = MagicMock()
        with patch.object(wr, "escalate") as mock_escalate:
            _open_resend_delivery_failed_row(
                attio,
                recipient="operator@example.com",
                week_starting="2026-05-18",
                resend_error="RuntimeError: rate limited",
            )
        kwargs = mock_escalate.call_args.kwargs
        assert kwargs["type"] == "resend_delivery_failed"
        assert kwargs["idempotency_key"] == "weekly_report|2026-05-18"
        payload = kwargs["payload"]
        assert payload["recipient_email"] == "operator@example.com"
        assert payload["kpi_snapshot_week_starting"] == "2026-05-18"
        assert "RuntimeError" in payload["resend_error_code"]


# -- run_weekly_report end-to-end --------------------------------------


class TestRunWeeklyReport:
    def _attio_with_entries(self, entries: list[dict], deals: list[dict]) -> MagicMock:
        attio = MagicMock()
        attio.query_list_entries.return_value = entries
        attio.search_deals.return_value = deals
        # Use raw deals through parse_deal — let parse_deal be a passthrough.
        attio.parse_deal.side_effect = lambda d: d
        return attio

    def test_dry_run_writes_sidecar_skips_email(self, monkeypatch, kpi_dir):
        monkeypatch.setenv("ATTIO_LIST_ID", "LIST")
        attio = self._attio_with_entries([], [])
        from clients.attio import AttioClient
        with patch.object(AttioClient, "parse_deal", side_effect=lambda d: d), \
             patch.object(AttioClient, "parse_entry",
                          side_effect=lambda e: {"persona": "ops", "stage": "PROSPECT"}):
            result = run_weekly_report(
                attio, resend=None, report_email="operator@example.com",
                dry_run=True, today=date(2026, 5, 22),
            )
        # Sidecar JSON written to the filesystem.
        record = json.loads((kpi_dir / "2026-05-18.json").read_text())
        assert record["report_email_sent_to"] == ""
        assert record["report_resend_message_id"] == ""
        assert record["week_starting"] == "2026-05-18"
        assert "window" in result
        assert "measurement_basis" in result
        # Fix-4 (audit): measurement_basis is now honest — all-time snapshot label.
        assert result["measurement_basis"] == "all_time_snapshot_asof_2026-05-22"

    def test_send_mode_writes_sidecar_and_emails(self, monkeypatch, kpi_dir):
        monkeypatch.setenv("ATTIO_LIST_ID", "LIST")
        attio = self._attio_with_entries([], [])
        resend = MagicMock()
        resend.send_email.return_value = {"id": "msg-abc-123"}
        from clients.attio import AttioClient
        with patch.object(AttioClient, "parse_deal", side_effect=lambda d: d), \
             patch.object(AttioClient, "parse_entry",
                          side_effect=lambda e: {"persona": "ops", "stage": "PROSPECT"}):
            run_weekly_report(
                attio, resend=resend, report_email="operator@example.com",
                dry_run=False, today=date(2026, 5, 22),
            )
        resend.send_email.assert_called_once()
        # Post-send patch backfilled the resend message id into the file.
        record = json.loads((kpi_dir / "2026-05-18.json").read_text())
        assert record["report_resend_message_id"] == "msg-abc-123"
        assert record["report_email_sent_to"] == "operator@example.com"

    def test_no_attio_schema_missing_escalation(self, monkeypatch, kpi_dir):
        # Regression guard for the whole task: the filesystem sidecar
        # must never open an attio_schema_missing queue row, even on a
        # clean send-mode run (the by-design weekly noise is gone).
        monkeypatch.setenv("ATTIO_LIST_ID", "LIST")
        attio = self._attio_with_entries([], [])
        resend = MagicMock()
        resend.send_email.return_value = {"id": "msg-abc-123"}
        from clients.attio import AttioClient
        from workflows import weekly_report as wr
        with patch.object(AttioClient, "parse_deal", side_effect=lambda d: d), \
             patch.object(AttioClient, "parse_entry",
                          side_effect=lambda e: {"persona": "ops", "stage": "PROSPECT"}), \
             patch.object(wr, "escalate") as mock_escalate:
            run_weekly_report(
                attio, resend=resend, report_email="operator@example.com",
                dry_run=False, today=date(2026, 5, 22),
            )
        schema_missing = [
            c for c in mock_escalate.call_args_list
            if c.kwargs.get("type") == "attio_schema_missing"
        ]
        assert schema_missing == [], (
            "filesystem sidecar must never emit attio_schema_missing"
        )

    def test_sidecar_written_before_email_send(self, monkeypatch, kpi_dir):
        # Durability contract: sidecar lands BEFORE the Resend attempt
        # so a Resend outage cannot lose the snapshot
        # (pr-test-analyzer I-2 + silent-failure I-1 convergence).
        monkeypatch.setenv("ATTIO_LIST_ID", "LIST")
        order_log: list[str] = []
        attio = self._attio_with_entries([], [])
        resend = MagicMock()

        def _send(**kw):
            # The sidecar file must exist by the time Resend is hit.
            order_log.append("send_email")
            order_log.append(
                "file_exists" if (kpi_dir / "2026-05-18.json").exists()
                else "file_missing"
            )
            return {"id": "msg"}

        resend.send_email.side_effect = _send
        from clients.attio import AttioClient
        with patch.object(AttioClient, "parse_deal", side_effect=lambda d: d), \
             patch.object(AttioClient, "parse_entry",
                          side_effect=lambda e: {"persona": "ops", "stage": "PROSPECT"}):
            run_weekly_report(
                attio, resend=resend, report_email="operator@example.com",
                dry_run=False, today=date(2026, 5, 22),
            )
        assert order_log == ["send_email", "file_exists"]

    def test_resend_failure_opens_queue_row_sidecar_intact(self, monkeypatch, kpi_dir):
        monkeypatch.setenv("ATTIO_LIST_ID", "LIST")
        attio = self._attio_with_entries([], [])
        resend = MagicMock()
        resend.send_email.side_effect = RuntimeError("rate limited")
        from clients.attio import AttioClient
        from workflows import weekly_report as wr
        with patch.object(AttioClient, "parse_deal", side_effect=lambda d: d), \
             patch.object(AttioClient, "parse_entry",
                          side_effect=lambda e: {"persona": "ops", "stage": "PROSPECT"}), \
             patch.object(wr, "escalate") as mock_escalate:
            run_weekly_report(
                attio, resend=resend, report_email="operator@example.com",
                dry_run=False, today=date(2026, 5, 22),
            )
        # Queue row written.
        mock_escalate.assert_called_once()
        assert mock_escalate.call_args.kwargs["type"] == "resend_delivery_failed"
        # Sidecar intact: the JSON file was written before the send.
        assert (kpi_dir / "2026-05-18.json").exists()


# -- deal_recency_degraded visibility (PR-219) ---------------------------


class TestDealRecencyDegradedVisibility:
    """The person-interaction join degradation must reach the durable
    record (kpi_snapshot_json sidecar) and the emailed report — not just
    the CLI scrollback. Deals here HAVE associated_people so the join
    actually executes (empty id sets short-circuit before the fetch).
    """

    IN_PROGRESS_DEAL = {
        "name": "Acme Foods",
        "stage": DealStage.IN_PROGRESS.value,
        "associated_people": ["p1"],
        "created_at": "2026-01-01",
        "value": 50_000,
    }

    def _attio(self, deals: list[dict]) -> MagicMock:
        attio = MagicMock()
        attio.query_list_entries.return_value = []
        attio.search_deals.return_value = deals
        return attio

    def _run(self, attio, *, resend=None, dry_run=True):
        from clients.attio import AttioClient
        with patch.object(AttioClient, "parse_deal", side_effect=lambda d: d), \
             patch.object(AttioClient, "parse_entry",
                          side_effect=lambda e: {"persona": "ops", "stage": "PROSPECT"}):
            return run_weekly_report(
                attio, resend=resend, report_email="operator@example.com",
                dry_run=dry_run, today=date(2026, 5, 22),
            )

    def test_degraded_join_lands_in_snapshot_and_sidecar(self, monkeypatch, kpi_dir):
        monkeypatch.setenv("ATTIO_LIST_ID", "LIST")
        attio = self._attio([dict(self.IN_PROGRESS_DEAL)])
        attio.bulk_fetch_persons_by_record_ids.side_effect = RuntimeError("boom")

        result = self._run(attio, dry_run=True)

        # The join executed and degraded.
        attio.bulk_fetch_persons_by_record_ids.assert_called_once()
        assert result["deal_recency_degraded"], (
            "degraded join must be recorded in the KPI snapshot"
        )
        # Durable week-over-week record carries the degradation trace.
        record = json.loads((kpi_dir / "2026-05-18.json").read_text())
        sidecar_snapshot = json.loads(record["kpi_snapshot_json"])
        assert sidecar_snapshot["deal_recency_degraded"] == result["deal_recency_degraded"]

    def test_degraded_join_banner_in_report_email(self, monkeypatch, kpi_dir):
        monkeypatch.setenv("ATTIO_LIST_ID", "LIST")
        attio = self._attio([dict(self.IN_PROGRESS_DEAL)])
        attio.bulk_fetch_persons_by_record_ids.side_effect = RuntimeError("boom")
        resend = MagicMock()
        resend.send_email.return_value = {"id": "msg-1"}

        self._run(attio, resend=resend, dry_run=False)

        html = resend.send_email.call_args.kwargs["html"]
        assert "Deal staleness may be overstated" in html
        assert "person-interaction join" in html

    def test_clean_join_no_degradation_recorded_or_rendered(self, monkeypatch, kpi_dir):
        monkeypatch.setenv("ATTIO_LIST_ID", "LIST")
        attio = self._attio([dict(self.IN_PROGRESS_DEAL)])
        attio.bulk_fetch_persons_by_record_ids.return_value = {
            "p1": {"values": {"last_interaction": [
                {"interacted_at": "2026-05-20T10:00:00.000000000Z"},
            ]}},
        }
        resend = MagicMock()
        resend.send_email.return_value = {"id": "msg-1"}

        result = self._run(attio, resend=resend, dry_run=False)

        assert result["deal_recency_degraded"] == []
        html = resend.send_email.call_args.kwargs["html"]
        assert "Deal staleness may be overstated" not in html


# -- Registry + manifest invariants -------------------------------------


class TestCliReportCommand:
    """pr-test-analyzer I-3 fold: CLI `--send` opt-in behavior tested
    via Click runner. Default IS dry-run; explicit --send is required.
    """

    def test_default_is_dry_run(self):
        from click.testing import CliRunner

        from cli import cli

        runner = CliRunner()
        with patch("clients.attio.AttioClient") as mock_attio_cls, \
             patch("clients.resend_client.ResendClient") as mock_resend_cls, \
             patch("workflows.weekly_report.run_weekly_report") as mock_run:
            mock_attio_cls.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_attio_cls.return_value.__exit__ = MagicMock(return_value=False)
            result = runner.invoke(cli, ["report"])

        assert result.exit_code == 0, result.output
        # Banner appears on dry-run default.
        assert "[DRY-RUN MODE]" in result.output
        # Resend client NOT instantiated when dry-run.
        mock_resend_cls.assert_not_called()
        # run_weekly_report invoked with dry_run=True.
        assert mock_run.call_args.kwargs["dry_run"] is True

    def test_send_flag_disables_dry_run(self):
        from click.testing import CliRunner

        from cli import cli

        runner = CliRunner()
        with patch("clients.attio.AttioClient") as mock_attio_cls, \
             patch("clients.resend_client.ResendClient") as mock_resend_cls, \
             patch("workflows.weekly_report.run_weekly_report") as mock_run:
            mock_attio_cls.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_attio_cls.return_value.__exit__ = MagicMock(return_value=False)
            result = runner.invoke(cli, ["report", "--send"])

        assert result.exit_code == 0, result.output
        # No dry-run banner.
        assert "[DRY-RUN MODE]" not in result.output
        # Resend client IS instantiated.
        mock_resend_cls.assert_called_once()
        # run_weekly_report invoked with dry_run=False.
        assert mock_run.call_args.kwargs["dry_run"] is False


class TestRegistryInvariants:
    def test_resend_delivery_failed_typeddict_registered(self):
        assert ESCALATION_SCHEMAS.get("resend_delivery_failed") is (
            ResendDeliveryFailedPayload
        )

    def test_resend_delivery_failed_slug_in_set(self):
        assert "resend_delivery_failed" in ESCALATION_TYPES_SET

    def test_writer_registry_pins_weekly_kpi_snapshot(self):
        from clients.attio_writer_registry import WRITE_OWNER_REGISTRY
        for slug in (
            "week_starting", "kpi_snapshot_json", "persona_funnels_json",
            "active_deals_json", "measurement_basis", "report_email_sent_to",
            "report_resend_message_id",
        ):
            assert WRITE_OWNER_REGISTRY[("weekly_kpi_snapshot", slug)] == (
                "workflows.weekly_report.run_weekly_report"
            )

    def test_manifest_declares_weekly_kpi_snapshot(self):
        from pathlib import Path

        import yaml

        manifest_path = (
            Path(__file__).parent.parent / "docs" / "attio_schema_deltas.yaml"
        )
        manifest = yaml.safe_load(manifest_path.read_text())
        new_objects = {obj["object"] for obj in manifest.get("new_objects", [])}
        assert "weekly_kpi_snapshot" in new_objects

        kpi_attrs = {
            a["slug"]
            for a in manifest.get("attributes", [])
            if a["object"] == "weekly_kpi_snapshot"
        }
        assert kpi_attrs == {
            "week_starting", "kpi_snapshot_json", "persona_funnels_json",
            "active_deals_json", "measurement_basis", "report_email_sent_to",
            "report_resend_message_id",
        }


# ── Audit fixes ──────────────────────────────────────────────────────────────


def _outreach_entry(stage: str, dm_step: int | None = None, merged_into: str | None = None) -> dict:
    """Build a raw Attio list-entry fixture for testing.

    `merged_into` simulates the soft-delete pointer set on dedup losers.
    """
    values: dict = {"stage": [{"status": {"title": stage}}]}
    if dm_step is not None:
        values["dm_step"] = [{"value": dm_step}]
    if merged_into is not None:
        # AttioClient._extract_value for a record-reference returns the
        # target_record_id string when the list is non-empty.
        values["merged_into"] = [{"target_record_id": merged_into}]
    return {"entry_values": values}


class TestAuditFix1Truncation:
    """Fix-1: query_list_entries limit must be 50_000 (not 500)."""

    def test_run_weekly_report_uses_limit_50000(self, monkeypatch, kpi_dir):
        """Assert that run_weekly_report passes limit=50_000 to query_list_entries."""
        monkeypatch.setenv("ATTIO_LIST_ID", "LIST")
        from unittest.mock import MagicMock, patch

        from clients.attio import AttioClient

        attio = MagicMock()
        attio.query_list_entries.return_value = []
        attio.search_deals.return_value = []
        with patch.object(AttioClient, "parse_deal", side_effect=lambda d: d), \
             patch.object(AttioClient, "parse_entry",
                          side_effect=lambda e: {"persona": "ops", "stage": "PROSPECT",
                                                 "merged_into": None}):
            run_weekly_report(attio, resend=None, dry_run=True, today=date(2026, 5, 22))

        call_kwargs = attio.query_list_entries.call_args
        assert call_kwargs.kwargs.get("limit") == 50_000, (
            "query_list_entries must use limit=50_000 to avoid silent truncation"
        )


class TestAuditFix2MergedIntoFilter:
    """Fix-2: entries with merged_into set must be excluded before KPI computation."""

    def test_merged_into_entries_excluded_from_compute_kpis(self):
        """A prospect with merged_into set is a soft-deleted dedup loser.
        Including it in compute_kpis would double-count the same person.
        After fix-2 the filter happens in run_weekly_report before calling
        compute_kpis. Test via the integration path in run_weekly_report."""
        from unittest.mock import MagicMock, patch

        from clients.attio import AttioClient

        # One real entry + one dedup loser pointing at it.
        real_entry = _outreach_entry("DM1 Sent", dm_step=1)
        loser_entry = _outreach_entry("DM1 Sent", dm_step=1, merged_into="rec_winner_123")

        attio = MagicMock()
        attio.query_list_entries.return_value = [real_entry, loser_entry]
        attio.search_deals.return_value = []

        captured: list[dict] = []

        original_compute_kpis = __import__(
            "workflows.weekly_report", fromlist=["compute_kpis"]
        ).compute_kpis

        def _spy_compute_kpis(entries: list) -> dict:
            captured.extend(entries)
            return original_compute_kpis(entries)

        with patch.object(AttioClient, "parse_deal", side_effect=lambda d: d):
            import workflows.weekly_report as wr
            with patch.object(wr, "compute_kpis", side_effect=_spy_compute_kpis):
                run_weekly_report(attio, resend=None, dry_run=True, today=date(2026, 5, 22))

        # Only the real entry (no merged_into) must reach compute_kpis.
        assert len(captured) == 1, (
            f"merged_into loser must be filtered; got {len(captured)} entries"
        )

    def test_merged_into_present_changes_count(self):
        """Direct unit test: parse_entry returns merged_into; filtering on it
        changes the entry count passed to compute_kpis."""
        real = _outreach_entry("DM1 Sent", dm_step=1)
        loser = _outreach_entry("DM1 Sent", dm_step=1, merged_into="rec_winner_123")

        # Without filter — both entries counted.
        kpis_with_loser = compute_kpis([real, loser])
        # With filter (as run_weekly_report now does).
        from clients.attio import AttioClient
        filtered = [e for e in [real, loser] if not AttioClient.parse_entry(e).get("merged_into")]
        kpis_filtered = compute_kpis(filtered)

        assert kpis_with_loser["total_prospects"] == 2
        assert kpis_filtered["total_prospects"] == 1


class TestAuditFix3NotInterestedDenominator:
    """Fix-3: NOT_INTERESTED belongs in the total_dms denominator (and in
    compute_persona_funnels dm1_sent), mirroring learn.py _DMED_STAGES."""

    def test_not_interested_in_total_dms_denominator(self):
        """A NOT_INTERESTED prospect received DMs — they must reduce response_rate.
        Before the fix, the denominator excluded them, inflating the rate."""
        ni_stage = PipelineStage.NOT_INTERESTED.value
        entries = [
            _outreach_entry("Responded", dm_step=1),  # response numerator
            _outreach_entry("DM1 Sent", dm_step=1),   # denominator only
            _outreach_entry(ni_stage),                 # denominator only (fix)
        ]
        kpis = compute_kpis(entries)
        # response numerator = 1; denominator = responded(1) + dm1(1) + ni(1) = 3
        assert kpis["response_rate"] == pytest.approx(33.3, abs=0.1)

    def test_not_interested_without_fix_would_inflate_rate(self):
        """Prove the fix matters: without NOT_INTERESTED in the denominator,
        the rate would be 1/2 = 50%; with the fix it is 1/3 ≈ 33.3%."""
        ni_stage = PipelineStage.NOT_INTERESTED.value
        entries = [
            _outreach_entry("Responded", dm_step=1),
            _outreach_entry("DM1 Sent", dm_step=1),
            _outreach_entry(ni_stage),
        ]
        kpis = compute_kpis(entries)
        # Would be 50.0 without the fix.
        assert kpis["response_rate"] != pytest.approx(50.0, abs=0.5)

    def test_persona_funnel_not_interested_in_dm1_sent(self):
        """compute_persona_funnels must count NOT_INTERESTED in dm1_sent so the
        per-persona response_rate denominator matches compute_kpis semantics."""
        ni_stage = PipelineStage.NOT_INTERESTED.value
        entries = [
            _outreach_entry("Responded", dm_step=1),
            _outreach_entry(ni_stage),   # should appear in dm1_sent
        ]
        funnels = compute_persona_funnels(entries)
        assert len(funnels) == 1
        f = funnels[0]
        # Both Responded and NOT_INTERESTED contributed to dm1_sent.
        assert f.dm1_sent == 2, (
            "NOT_INTERESTED must be counted in dm1_sent "
            f"(got dm1_sent={f.dm1_sent})"
        )
        # Only Responded is a response.
        assert f.responded == 1


class TestAuditFix4HonestLabels:
    """Fix-4: measurement_basis must not claim '7d_window' when no filter is applied."""

    def test_measurement_basis_is_all_time_snapshot(self, monkeypatch, kpi_dir):
        """run_weekly_report must emit 'all_time_snapshot_asof_<date>' not '7d_window_…'."""
        from unittest.mock import MagicMock, patch

        from clients.attio import AttioClient

        monkeypatch.setenv("ATTIO_LIST_ID", "LIST")
        attio = MagicMock()
        attio.query_list_entries.return_value = []
        attio.search_deals.return_value = []
        with patch.object(AttioClient, "parse_deal", side_effect=lambda d: d), \
             patch.object(AttioClient, "parse_entry",
                          side_effect=lambda e: {"persona": "ops", "stage": "PROSPECT",
                                                 "merged_into": None}):
            result = run_weekly_report(
                attio, resend=None, dry_run=True, today=date(2026, 5, 22),
            )

        assert "7d_window" not in result["measurement_basis"], (
            "measurement_basis must not claim windowing; no date filter is applied"
        )
        assert result["measurement_basis"] == "all_time_snapshot_asof_2026-05-22"

    def test_persona_funnel_html_header_is_cumulative(self):
        """HTML heading must say 'cumulative, all-time', NOT 'this week'."""
        from workflows.weekly_report import _persona_funnel_table_html

        pf = PersonaFunnel("ops", 5, 4, 4, 2, 1, 1, 0)
        html = _persona_funnel_table_html([pf])
        assert "this week" not in html.lower(), (
            "HTML persona funnel header must not claim 'this week'"
        )
        assert "cumulative" in html.lower()

    def test_sidecar_measurement_basis_matches_snapshot(self, kpi_dir):
        """The sidecar JSON file must record the honest measurement_basis label."""
        snap: KPISnapshot = {
            "window": "2026-05-16..2026-05-22",
            "measurement_basis": "all_time_snapshot_asof_2026-05-22",
            "prior_period_compare": None,
            "week_starting": "2026-05-18",
            "totals": {},
            "stage_counts": {},
            "persona_funnels": [],
            "active_deals": {},
            "stale_threshold_source": "default",
            "notes": "",
            "outreach_volume": {"totals": {}, "per_day": {}, "window_days": 7,
                                "days_with_rows": 0, "malformed_reads": 0,
                                "note": "", "coverage_warning": "",
                                "source": "crm_daily_run_ledger"},
        }
        path = _upsert_kpi_snapshot(
            snap, persona_funnels=[], active_deals={}, sent_to="", resend_message_id="",
        )
        record = json.loads(open(path).read())
        assert "7d_window" not in record["measurement_basis"]
        assert record["measurement_basis"] == "all_time_snapshot_asof_2026-05-22"


class TestAuditFix5SupersedeOnRewrite:
    """Fix-5: same-week re-run must not silently destroy the first run's audit trail."""

    def test_no_prior_file_is_noop(self, kpi_dir):
        """_supersede_existing_snapshot on a non-existent path does nothing."""
        kpi_dir.mkdir(parents=True, exist_ok=True)
        path = kpi_dir / "2026-05-18.json"
        # No error, no side effects.
        _supersede_existing_snapshot(path)
        assert not path.exists()
        assert list(kpi_dir.iterdir()) == []

    def test_existing_file_is_renamed_aside(self, kpi_dir):
        """First run's file is renamed to <week>_superseded_1.json."""
        kpi_dir.mkdir(parents=True, exist_ok=True)
        path = kpi_dir / "2026-05-18.json"
        path.write_text('{"run": 1}')
        _supersede_existing_snapshot(path)
        # Original path is gone.
        assert not path.exists()
        superseded = kpi_dir / "2026-05-18_superseded_1.json"
        assert superseded.exists()
        assert json.loads(superseded.read_text())["run"] == 1

    def test_second_supersede_increments_n(self, kpi_dir):
        """A third run produces _superseded_2.json (n increments to avoid collisions)."""
        kpi_dir.mkdir(parents=True, exist_ok=True)
        path = kpi_dir / "2026-05-18.json"

        # First run → canonical file.
        path.write_text('{"run": 1}')
        _supersede_existing_snapshot(path)
        # Second run → second canonical file (simulate _upsert_kpi_snapshot writing it).
        path.write_text('{"run": 2}')
        _supersede_existing_snapshot(path)

        assert (kpi_dir / "2026-05-18_superseded_1.json").exists()
        assert (kpi_dir / "2026-05-18_superseded_2.json").exists()

    def test_week_over_week_delta_reads_canonical_not_superseded(self, kpi_dir):
        """_load_prior_snapshot reads <week>.json (the canonical latest run),
        NOT the superseded copies, so week-over-week deltas are correct."""
        from workflows.weekly_report import _compute_prior_period_compare, _load_prior_snapshot

        kpi_dir.mkdir(parents=True, exist_ok=True)
        # Simulate: two runs in week of 2026-05-11 (prior to 2026-05-18).
        prior_week = "2026-05-11"
        canon = kpi_dir / f"{prior_week}.json"
        # First run data (will become superseded).
        first_snapshot = {"totals": {"qualified": 2.0}}
        # Second (corrected) run data — this is canonical.
        second_snapshot = {"totals": {"qualified": 5.0}}

        canon.write_text(json.dumps({"kpi_snapshot_json": json.dumps(first_snapshot)}))
        # Supersede the first run (move it aside).
        _supersede_existing_snapshot(canon)
        # Write the second (corrected) run as canonical.
        canon.write_text(json.dumps({"kpi_snapshot_json": json.dumps(second_snapshot)}))

        loaded = _load_prior_snapshot(date(2026, 5, 18))
        assert loaded == second_snapshot, (
            "_load_prior_snapshot must read the canonical (latest) file, "
            "not the superseded copy"
        )
        compare = _compute_prior_period_compare({"qualified": 7.0}, loaded)
        assert compare["qualified"]["prior"] == 5.0  # from second (canonical) run
        assert compare["qualified"]["delta"] == 2.0
