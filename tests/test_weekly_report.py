"""Tests for workflows/weekly_report.py — deals aggregation + HTML rendering."""

from datetime import date

import pytest

from clients.attio import AttioClient
from workflows.weekly_report import (
    TARGETS,
    build_report_html,
    compute_active_deals,
    compute_kpis,
)


def _outreach_entry(stage: str, dm_step: int | None = None) -> dict:
    """Build a raw Attio list-entry fixture for compute_kpis (which parses via
    AttioClient.parse_entry). Only stage + dm_step matter for KPI math."""
    values: dict = {"stage": [{"status": {"title": stage}}]}
    if dm_step is not None:
        values["dm_step"] = [{"value": dm_step}]
    return {"entry_values": values}


class TestComputeKpisUnreachable:
    """Wave-2-A: UNREACHABLE rows that received >=1 DM belong in the DM'd /
    past-connection denominators (they genuinely earned them) but never in the
    response numerator. dm_step=0 rows (never delivered) stay fully excluded."""

    def test_unreachable_dmed_in_response_denominator_not_numerator(self):
        entries = [
            _outreach_entry("Responded", dm_step=1),    # responder
            _outreach_entry("DM1 Sent", dm_step=1),     # dmed, no response
            _outreach_entry("Unreachable", dm_step=2),  # dmed (>=1), NOT a response
        ]
        kpis = compute_kpis(entries)
        # total_dms = responded(1) + dm1(1) + unreachable_dmed(1) = 3
        # response numerator = responded(1) → 1/3
        assert kpis["response_rate"] == pytest.approx(33.3, abs=0.1)

    def test_unreachable_dm_step_zero_excluded_from_denominators(self):
        """A dm_step=0 UNREACHABLE row never received a DM — it must not change
        response_rate vs. a cohort without it at all."""
        with_zero = compute_kpis([
            _outreach_entry("DM1 Sent", dm_step=1),
            _outreach_entry("Responded", dm_step=1),
            _outreach_entry("Unreachable", dm_step=0),
        ])
        without = compute_kpis([
            _outreach_entry("DM1 Sent", dm_step=1),
            _outreach_entry("Responded", dm_step=1),
        ])
        assert with_zero["response_rate"] == without["response_rate"]

    def test_handles_select_slug_dm_step_without_crashing(self):
        """Production dm_step is a select-type attribute — parse_entry returns
        the option title slug ("dm2" / "invite"), not an int. compute_kpis must
        coerce it via dm_step_int, never raise on `slug >= 1`."""
        def sel(stage: str, slug: str | None = None) -> dict:
            values: dict = {"stage": [{"status": {"title": stage}}]}
            if slug is not None:
                values["dm_step"] = [{"attribute_type": "select",
                                      "option": {"title": slug}}]
            return {"entry_values": values}

        entries = [
            sel("Responded", "dm1"),       # responder
            sel("DM1 Sent", "dm1"),        # dmed, no response
            sel("Unreachable", "dm2"),     # dmed via DM2 → in total_dms
            sel("Unreachable", "invite"),  # never DM'd → excluded
        ]
        kpis = compute_kpis(entries)
        # total_dms = responded(1) + dm1(1) + unreachable_dmed(1) = 3
        assert kpis["response_rate"] == pytest.approx(33.3, abs=0.1)

    def test_persona_funnel_folds_unreachable_dmed_into_cumulative_buckets(self):
        """compute_persona_funnels must fold a DM'd-then-UNREACHABLE row into the
        cumulative dm{N}_sent buckets up to its real step, but NEVER into
        responded — so the per-persona response_rate de-inflates consistently
        with the compute_kpis fix. A dm_step=0 UNREACHABLE row is fully excluded.
        """
        from workflows.weekly_report import compute_persona_funnels

        entries = [
            _outreach_entry("Responded", dm_step=3),    # cumulative dm1+dm2+dm3, responded
            _outreach_entry("DM1 Sent", dm_step=1),     # dm1 only
            _outreach_entry("Unreachable", dm_step=2),  # dm1+dm2, NOT a response
            _outreach_entry("Unreachable", dm_step=0),  # never DM'd → fully excluded
        ]
        funnels = compute_persona_funnels(entries)
        assert len(funnels) == 1
        f = funnels[0]
        assert f.dm1_sent == 3  # Responded + DM1 + Unreachable(dm2)
        assert f.dm2_sent == 2  # Responded + Unreachable(dm2)
        assert f.dm3_sent == 1  # Responded only
        assert f.responded == 1  # UNREACHABLE is never a response
        # Cumulative funnel invariant preserved.
        assert f.invited >= f.accepted >= f.dm1_sent >= f.dm2_sent >= f.dm3_sent >= f.responded


def _deal_record(name: str, stage: str, value: float | None = None,
                 currency: str = "USD", country: str | None = None) -> dict:
    """Build an Attio-shaped deal record fixture."""
    rec: dict = {
        "id": {"record_id": f"rec_{name.replace(' ', '_').lower()}"},
        "values": {
            "name": [{"value": name}],
            "stage": [{"status": {"title": stage}}],
            "associated_company": [{"target_record_id": "co_x"}],
            "owner": [{"referenced_actor_id": "actor_x"}],
        },
    }
    if value is not None:
        rec["values"]["value"] = [{"currency_value": value, "currency_code": currency}]
    if country is not None:
        rec["values"]["country"] = [{"country_code": country}]
    return rec


class TestParseDeal:
    def test_parses_full_record(self):
        rec = _deal_record("Sigma", "In Progress", value=200_000, country="MX")
        out = AttioClient.parse_deal(rec)
        assert out["name"] == "Sigma"
        assert out["stage"] == "In Progress"
        assert out["value"] == 200_000
        assert out["currency"] == "USD"
        assert out["country"] == "MX"
        assert out["company_id"] == "co_x"
        assert out["owner_id"] == "actor_x"
        assert out["record_id"] == "rec_sigma"

    def test_parses_blank_value(self):
        rec = _deal_record("Tajin", "In Progress")
        out = AttioClient.parse_deal(rec)
        assert out["value"] is None
        assert out["currency"] is None

    def test_parses_blank_country(self):
        rec = _deal_record("Creditex", "In Progress", value=100_000)
        out = AttioClient.parse_deal(rec)
        assert out["country"] is None

    def test_handles_empty_values(self):
        rec = {"id": {"record_id": "rec_empty"}, "values": {}}
        out = AttioClient.parse_deal(rec)
        assert out["name"] is None
        assert out["stage"] == ""
        assert out["value"] is None


class TestComputeActiveDeals:
    def test_aggregates_by_stage(self):
        deals = [
            AttioClient.parse_deal(_deal_record("A", "In Progress", value=50_000)),
            AttioClient.parse_deal(_deal_record("B", "In Progress")),
            AttioClient.parse_deal(_deal_record("C", "Lead")),
            AttioClient.parse_deal(_deal_record("D", "Lost")),
        ]
        result = compute_active_deals(deals)

        assert result["in_progress_count"] == 2
        assert result["lead_count"] == 1
        assert result["lost_count"] == 1
        assert result["deals_by_stage"] == {
            "In Progress": 2, "Lead": 1, "Lost": 1,
        }

    def test_explicit_value_used_when_set(self):
        deals = [AttioClient.parse_deal(_deal_record("A", "In Progress", value=200_000))]
        result = compute_active_deals(deals)
        assert result["in_progress_value"] == 200_000
        assert result["blank_value_count"] == 0
        assert result["in_progress"][0]["value_is_fallback"] is False

    def test_acv_fallback_for_blank_value(self):
        deals = [AttioClient.parse_deal(_deal_record("A", "In Progress"))]
        result = compute_active_deals(deals)
        assert result["in_progress_value"] == TARGETS["acv"]
        assert result["blank_value_count"] == 1
        assert result["in_progress"][0]["value_is_fallback"] is True

    def test_mixed_explicit_and_fallback(self):
        deals = [
            AttioClient.parse_deal(_deal_record("Big", "In Progress", value=500_000)),
            AttioClient.parse_deal(_deal_record("Blank1", "In Progress")),
            AttioClient.parse_deal(_deal_record("Blank2", "In Progress")),
        ]
        result = compute_active_deals(deals)
        # 500K + 135K + 135K = 770K
        assert result["in_progress_value"] == 500_000 + 2 * TARGETS["acv"]
        assert result["blank_value_count"] == 2

    def test_in_progress_sorted_by_value_desc(self):
        deals = [
            AttioClient.parse_deal(_deal_record("Small", "In Progress", value=50_000)),
            AttioClient.parse_deal(_deal_record("Big", "In Progress", value=500_000)),
            AttioClient.parse_deal(_deal_record("Mid", "In Progress", value=200_000)),
        ]
        result = compute_active_deals(deals)
        names = [d["name"] for d in result["in_progress"]]
        assert names == ["Big", "Mid", "Small"]

    def test_empty_deals(self):
        result = compute_active_deals([])
        assert result["in_progress_count"] == 0
        assert result["in_progress_value"] == 0
        assert result["lead_count"] == 0
        assert result["lost_count"] == 0


class TestBuildReportHtml:
    def _kpis(self) -> dict:
        return {
            "total_prospects": 100,
            "stage_counts": {"Prospect": 50, "Connection Sent": 30, "Accepted": 20},
            "connection_acceptance_rate": 30.0,
            "response_rate": 12.5,
            "responded": 5,
            "calls_booked": 2,
            "qualified": 0,
            "not_interested": 1,
            "pipeline_value": 0,
        }

    def test_renders_active_deals_section_with_blanks(self):
        deals = [
            AttioClient.parse_deal(_deal_record("Sigma", "In Progress")),
            AttioClient.parse_deal(_deal_record("Tajin", "In Progress")),
            AttioClient.parse_deal(_deal_record("X", "Lead")),
        ]
        active = compute_active_deals(deals)
        html = build_report_html(self._kpis(), active, date(2026, 5, 8))

        assert "Active Deals" in html
        assert "Sigma" in html
        assert "Tajin" in html
        assert "ACV fallback" in html
        assert "missing" in html  # hygiene banner
        assert "1 leads" in html

    def test_renders_active_deals_with_explicit_values(self):
        deals = [
            AttioClient.parse_deal(_deal_record("BigDeal", "In Progress", value=300_000)),
        ]
        active = compute_active_deals(deals)
        html = build_report_html(self._kpis(), active, date(2026, 5, 8))

        assert "BigDeal" in html
        assert "$300,000" in html
        assert "ACV fallback" not in html  # no blanks → no banner
        assert "missing" not in html

    def test_headline_pipeline_value_uses_deals(self):
        deals = [
            AttioClient.parse_deal(_deal_record("A", "In Progress", value=300_000)),
        ]
        active = compute_active_deals(deals)
        html = build_report_html(self._kpis(), active, date(2026, 5, 8))

        # Pipeline Value row reflects deals total, not kpis['pipeline_value']
        assert "$300,000" in html

    def test_no_active_deals_renders_empty_state(self):
        active = compute_active_deals([])
        html = build_report_html(self._kpis(), active, date(2026, 5, 8))
        assert "No deals currently in progress" in html

    def test_lost_count_shown(self):
        deals = [
            AttioClient.parse_deal(_deal_record("A", "In Progress", value=100_000)),
            AttioClient.parse_deal(_deal_record("B", "Lost")),
            AttioClient.parse_deal(_deal_record("C", "Lost")),
        ]
        active = compute_active_deals(deals)
        html = build_report_html(self._kpis(), active, date(2026, 5, 8))
        assert "2 lost" in html
