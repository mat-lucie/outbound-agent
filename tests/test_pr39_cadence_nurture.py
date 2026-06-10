"""PR-39 tests: CADENCE_BY_LANE, compute_nurture_re_eligible_at,
is_re_engagement_suppressed (M9 four-signal belt-and-suspenders),
derive_cadence_lane, backfill candidate selection + single-entry
backfill dispatch."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from workflows.cadence import (
    CADENCE_BY_LANE,
    NEGATIVE_RESPONSE_CLASSIFICATIONS,
    SCORING_LANE_TO_CADENCE_LANE,
    _parse_date,
    classify_suppression,
    compute_nurture_re_eligible_at,
    derive_cadence_lane,
    is_re_engagement_suppressed,
)
from workflows.gtm.nurture_backfill import (
    POST_DM3_STAGES,
    backfill_one,
    select_candidates,
)

# ────────────────────────────────────────────────────────────────────────
# Constants invariants
# ────────────────────────────────────────────────────────────────────────


class TestCadenceConstants:
    def test_three_lanes_defined(self):
        assert set(CADENCE_BY_LANE) == {"enterprise", "target", "legacy"}

    def test_lane_order_is_conservative(self):
        # enterprise >= target >= legacy (longer cooldowns for higher-value lanes)
        assert CADENCE_BY_LANE["enterprise"] >= CADENCE_BY_LANE["target"]
        assert CADENCE_BY_LANE["target"] >= CADENCE_BY_LANE["legacy"]

    def test_all_lanes_positive(self):
        for days in CADENCE_BY_LANE.values():
            assert days > 0

    def test_scoring_lane_promotion_covers_known_lanes(self):
        # Same three scoring_lane strings as quality_gate.score_prospect emits.
        assert set(SCORING_LANE_TO_CADENCE_LANE) == {
            "enterprise_mode", "target_company_mode", "legacy",
        }
        # Each promotion lands on a valid cadence lane.
        for cadence in SCORING_LANE_TO_CADENCE_LANE.values():
            assert cadence in CADENCE_BY_LANE


# ────────────────────────────────────────────────────────────────────────
# derive_cadence_lane
# ────────────────────────────────────────────────────────────────────────


class TestDeriveCadenceLane:
    @pytest.mark.parametrize("scoring,expected", [
        ("enterprise_mode", "enterprise"),
        ("target_company_mode", "target"),
        ("legacy", "legacy"),
    ])
    def test_known_promotions(self, scoring, expected):
        assert derive_cadence_lane(scoring) == expected

    @pytest.mark.parametrize("scoring", [None, "", "garbage", "some_legacy_string"])
    def test_unknown_lane_defaults_to_enterprise(self, scoring):
        # Conservative fall-through: unknown lanes get the LONGEST cooldown
        # (enterprise = 90 business days). Defaulting to "legacy" (45d)
        # would give unstamped rows the most-aggressive re-engagement
        # schedule, inverting the §3.1 fail-safe-by-default posture.
        assert derive_cadence_lane(scoring) == "enterprise"


# ────────────────────────────────────────────────────────────────────────
# is_re_engagement_suppressed (M9 four-signal OR-merge)
# ────────────────────────────────────────────────────────────────────────


class TestIsReEngagementSuppressed:
    def test_clean_entry_not_suppressed(self):
        assert not is_re_engagement_suppressed({
            "stage": "DM3 Sent",
            "response_classification": None,
            "suppress_re_engagement": None,
        })

    def test_explicit_suppress_flag_truthy(self):
        assert is_re_engagement_suppressed({"suppress_re_engagement": True})

    def test_negative_response_classification(self):
        for cls in NEGATIVE_RESPONSE_CLASSIFICATIONS:
            assert is_re_engagement_suppressed({"response_classification": cls})

    def test_not_interested_stage(self):
        assert is_re_engagement_suppressed({"stage": "Not Interested"})

    def test_defensive_hold_stage(self):
        assert is_re_engagement_suppressed({"stage": "Defensive Hold"})

    def test_or_merge_any_signal_alone(self):
        # Only one signal set — still suppresses. Three signals tested
        # (flag, classification, stage_not_interested); the 4th
        # (stage_defensive_hold) is exercised by test_defensive_hold_stage.
        entries = [
            {"suppress_re_engagement": True, "stage": "DM3 Sent",
             "response_classification": "positive"},
            {"suppress_re_engagement": False, "stage": "Not Interested",
             "response_classification": "positive"},
            {"suppress_re_engagement": False, "stage": "DM3 Sent",
             "response_classification": "negative"},
        ]
        for e in entries:
            assert is_re_engagement_suppressed(e), e

    def test_hard_no_no_longer_in_classifications(self):
        # PR-39 dropped `hard_no` from the suppression set after the QA
        # fan-out flagged it as a phantom signal (no caller in this repo
        # ever emits that token — workflows.response_classifier.VALID_CLASSIFICATIONS
        # is {positive, question, neutral, negative, defensive}).
        assert "hard_no" not in NEGATIVE_RESPONSE_CLASSIFICATIONS
        # Symmetric with workflows.cross_channel_suppression — both writers
        # must agree on the §3.1 predicate.
        from workflows.cross_channel_suppression import (
            NEGATIVE_RESPONSE_CLASSIFICATIONS as ccs_neg,
        )
        assert ccs_neg == NEGATIVE_RESPONSE_CLASSIFICATIONS

    def test_positive_classification_not_suppressed(self):
        # IMPORTANT: positive replies do NOT suppress re-engagement at the
        # cadence layer — they route through a different funnel path.
        assert not is_re_engagement_suppressed({
            "stage": "DM3 Sent", "response_classification": "positive",
        })


# ────────────────────────────────────────────────────────────────────────
# compute_nurture_re_eligible_at
# ────────────────────────────────────────────────────────────────────────


class TestComputeNurtureReEligibleAt:
    def test_clean_entry_returns_target_date(self):
        from models.business_calendar import add_business_days
        target = compute_nurture_re_eligible_at(
            dm3_sent_at=date(2026, 5, 1),
            cadence_lane="target",
            entry_attrs={"stage": "DM3 Sent"},
        )
        # target lane = 60 BUSINESS days (Sat/Sun skipped).
        assert target == add_business_days(date(2026, 5, 1), 60)

    def test_target_lands_on_weekday(self):
        # Friday + N business days never lands on a weekend — sanity-check
        # the business-day math vs the previous calendar-day implementation.
        friday = date(2026, 5, 1)  # Friday
        assert friday.weekday() == 4
        for lane in ("enterprise", "target", "legacy"):
            target = compute_nurture_re_eligible_at(
                dm3_sent_at=friday,
                cadence_lane=lane,  # type: ignore[arg-type]
                entry_attrs={"stage": "DM3 Sent"},
            )
            assert target is not None
            assert target.weekday() < 5  # Mon-Fri

    def test_suppressed_entry_returns_none(self):
        target = compute_nurture_re_eligible_at(
            dm3_sent_at=date(2026, 5, 1),
            cadence_lane="legacy",
            entry_attrs={"suppress_re_engagement": True, "stage": "DM3 Sent"},
        )
        assert target is None

    def test_missing_dm3_anchor_returns_none(self):
        target = compute_nurture_re_eligible_at(
            dm3_sent_at=None,
            cadence_lane="enterprise",
            entry_attrs={"stage": "DM3 Sent"},
        )
        assert target is None

    @pytest.mark.parametrize("input_raw,expected", [
        ("2026-05-01", date(2026, 5, 1)),
        ("2026-05-01T12:00:00Z", date(2026, 5, 1)),  # Attio datetime form
    ])
    def test_string_anchor_parsed(self, input_raw, expected):
        from models.business_calendar import add_business_days
        target = compute_nurture_re_eligible_at(
            dm3_sent_at=input_raw,
            cadence_lane="legacy",
            entry_attrs={"stage": "DM3 Sent"},
        )
        # legacy lane = 45 business days
        assert target == add_business_days(expected, 45)

    def test_malformed_anchor_returns_none(self):
        # Loud-from-suppression gate is upstream of the parse-error path;
        # a corrupted dm3_sent_at field returns None rather than crashing
        # the daily-check loop.
        target = compute_nurture_re_eligible_at(
            dm3_sent_at="not-a-date",
            cadence_lane="legacy",
            entry_attrs={"stage": "DM3 Sent"},
        )
        assert target is None

    def test_unknown_lane_falls_back_to_enterprise_floor(self):
        # An unknown lane should not crash — fall back to the ENTERPRISE
        # floor (longest cooldown). Defaulting to legacy (shortest) would
        # invert the §3.1 fail-safe direction.
        from models.business_calendar import add_business_days
        target = compute_nurture_re_eligible_at(
            dm3_sent_at=date(2026, 5, 1),
            cadence_lane="garbage",  # type: ignore[arg-type]
            entry_attrs={"stage": "DM3 Sent"},
        )
        assert target == add_business_days(date(2026, 5, 1), CADENCE_BY_LANE["enterprise"])


class TestClassifySuppression:
    """Per-signal attribution lets the backfill counters double as a
    health check on the OR-merge doctrine — if one signal silently breaks
    the others still fire on the same volumes."""

    def test_no_signal_returns_none(self):
        assert classify_suppression({"stage": "DM3 Sent"}) is None

    def test_flag_attribution(self):
        assert classify_suppression({
            "suppress_re_engagement": True, "stage": "DM3 Sent",
        }) == "flag"

    def test_classification_attribution(self):
        assert classify_suppression({
            "response_classification": "negative", "stage": "DM3 Sent",
        }) == "classification"

    def test_stage_not_interested_attribution(self):
        assert classify_suppression({"stage": "Not Interested"}) == "stage_not_interested"

    def test_stage_defensive_hold_attribution(self):
        assert classify_suppression({"stage": "Defensive Hold"}) == "stage_defensive_hold"

    def test_deterministic_priority_when_multiple_signals_fire(self):
        # flag > classification > stage. Multiple signals → flag wins so
        # re-runs map to the same reason code (counter consistency).
        assert classify_suppression({
            "suppress_re_engagement": True,
            "response_classification": "negative",
            "stage": "Not Interested",
        }) == "flag"


# ────────────────────────────────────────────────────────────────────────
# Backfill candidate selection + dispatch
# ────────────────────────────────────────────────────────────────────────


def _make_raw_entry(
    *,
    stage: str = "DM3 Sent",
    nurture_re_eligible_at: str | None = None,
    dm3_sent_at: str | None = "2026-05-01",
    scoring_lane: str | None = "legacy",
    suppress: bool = False,
    response_classification: str | None = None,
) -> dict:
    """Build the raw Attio entry envelope shape parse_entry expects."""
    def _v(v):
        return [{"value": v}] if v is not None else []

    return {
        "entry_id": "e-1",
        "parent_record_id": "p-1",
        "entry_values": {
            "stage": [{"status": {"title": stage}}] if stage else [],
            "nurture_re_eligible_at": _v(nurture_re_eligible_at),
            "dm3_sent_at": _v(dm3_sent_at),
            "scoring_lane": _v(scoring_lane),
            "suppress_re_engagement": _v(suppress) if suppress else [],
            "response_classification": _v(response_classification),
        },
    }


class TestSelectCandidates:
    def test_only_post_dm3_stages_included(self):
        e_pre = _make_raw_entry(stage="DM1 Sent")
        e_post = _make_raw_entry(stage="DM3 Sent")
        out = select_candidates([e_pre, e_post])
        assert len(out) == 1

    def test_already_set_entries_skipped(self):
        e_done = _make_raw_entry(nurture_re_eligible_at="2026-06-15")
        e_pending = _make_raw_entry(nurture_re_eligible_at=None)
        out = select_candidates([e_done, e_pending])
        assert len(out) == 1

    def test_post_dm3_stages_constant_matches_pipeline_terminal_set(self):
        # Sanity: the constant covers the 6 post-DM3 stages the spec calls out.
        assert {
            "DM3 Sent", "Nurture", "Responded", "Defensive Hold",
            "Call Booked", "Qualified",
        } <= POST_DM3_STAGES


class TestBackfillOne:
    def test_dry_run_returns_would_patch(self):
        e = _make_raw_entry()
        writer = MagicMock()
        out = backfill_one(e, writer=writer, list_id="L", dry_run=True)
        assert out == "would_patch"
        writer.apply.assert_not_called()

    def test_live_apply_patches_with_writer(self):
        e = _make_raw_entry()
        writer = MagicMock()
        out = backfill_one(e, writer=writer, list_id="L", dry_run=False)
        assert out == "patched"
        writer.apply.assert_called_once()
        intent = writer.apply.call_args.args[0]
        assert intent.object == "linkedin_outreach"
        assert intent.is_list_entry is True
        assert "nurture_re_eligible_at" in intent.updates

    def test_flag_signal_per_signal_attribution(self):
        e = _make_raw_entry(suppress=True)
        writer = MagicMock()
        out = backfill_one(e, writer=writer, list_id="L", dry_run=False)
        assert out == "skipped_flag"
        writer.apply.assert_not_called()

    def test_classification_signal_per_signal_attribution(self):
        e = _make_raw_entry(response_classification="negative")
        writer = MagicMock()
        out = backfill_one(e, writer=writer, list_id="L", dry_run=False)
        assert out == "skipped_classification"
        writer.apply.assert_not_called()

    def test_not_interested_stage_per_signal_attribution(self):
        e = _make_raw_entry(stage="Not Interested")
        writer = MagicMock()
        out = backfill_one(e, writer=writer, list_id="L", dry_run=False)
        assert out == "skipped_stage_not_interested"
        writer.apply.assert_not_called()

    def test_defensive_hold_stage_per_signal_attribution(self):
        e = _make_raw_entry(stage="Defensive Hold")
        writer = MagicMock()
        out = backfill_one(e, writer=writer, list_id="L", dry_run=False)
        assert out == "skipped_stage_defensive_hold"
        writer.apply.assert_not_called()

    def test_missing_dm3_anchor_skipped(self):
        e = _make_raw_entry(dm3_sent_at=None)
        writer = MagicMock()
        out = backfill_one(e, writer=writer, list_id="L", dry_run=False)
        assert out == "skipped_no_dm3_anchor"
        writer.apply.assert_not_called()

    def test_corrupt_dm3_anchor_distinct_outcome(self, capsys):
        # A post-DM3 entry with a garbage dm3_sent_at is a data-quality
        # alarm — should NOT lump in with the legitimate
        # skipped_no_dm3_anchor bucket (which is just unstamped legacy rows).
        e = _make_raw_entry(dm3_sent_at="not-a-date")
        writer = MagicMock()
        out = backfill_one(e, writer=writer, list_id="L", dry_run=False)
        assert out == "skipped_corrupt_dm3_anchor"
        writer.apply.assert_not_called()
        # Loud signal also goes to stderr so the operator sees it.
        captured = capsys.readouterr()
        assert "corrupt dm3_sent_at" in captured.err


# ────────────────────────────────────────────────────────────────────────
# _parse_date robustness
# ────────────────────────────────────────────────────────────────────────


class TestParseDate:
    @pytest.mark.parametrize("raw,expected", [
        ("2026-05-01", date(2026, 5, 1)),
        ("2026-05-01T12:00:00Z", date(2026, 5, 1)),
        (None, None),
        ("", None),
        ("garbage", None),
        ("2026-13-01", None),  # out-of-range month
    ])
    def test_robust_parsing(self, raw, expected):
        assert _parse_date(raw) == expected
