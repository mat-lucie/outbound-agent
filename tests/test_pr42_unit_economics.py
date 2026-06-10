"""PR-42 tests — LaneEconomics, evaluate_lane_breach tier logic,
escalation invocation with deterministic idempotency keys."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

from workflows.unit_economics import (
    CONSECUTIVE_BREACH_THRESHOLD,
    LANE_CEILING_USD,
    WRITER_MODULE,
    BreachVerdict,
    LaneEconomics,
    _idempotency_key_for,
    evaluate_lane_breach,
    fetch_prior_week_breached,
    open_breach_escalation,
)

# ────────────────────────────────────────────────────────────────────────
# Constants invariants
# ────────────────────────────────────────────────────────────────────────


class TestLaneCeilingDefaults:
    def test_three_lanes_defined(self):
        assert set(LANE_CEILING_USD) == {"enterprise", "target", "legacy"}

    def test_canonical_defaults(self):
        # Plan §5 PR-42 (Round-4): enterprise/target/legacy = $400 / $150 / $200
        assert LANE_CEILING_USD["enterprise"] == 400.0
        assert LANE_CEILING_USD["target"] == 150.0
        assert LANE_CEILING_USD["legacy"] == 200.0

    def test_consecutive_breach_threshold_is_two(self):
        assert CONSECUTIVE_BREACH_THRESHOLD == 2


# ────────────────────────────────────────────────────────────────────────
# LaneEconomics dataclass
# ────────────────────────────────────────────────────────────────────────


class TestLaneEconomics:
    def test_cost_per_positive_computed(self):
        econ = LaneEconomics(
            lane="target", week_starting=date(2026, 5, 18),
            sends=10, positives=2, spend_usd=300.0,
        )
        assert econ.cost_per_positive == 150.0

    def test_cost_per_positive_zero_positives(self):
        econ = LaneEconomics(
            lane="legacy", week_starting=date(2026, 5, 18),
            sends=10, positives=0, spend_usd=500.0,
        )
        assert econ.cost_per_positive is None

    def test_is_frozen(self):
        econ = LaneEconomics(
            lane="enterprise", week_starting=date(2026, 5, 18),
            sends=1, positives=1, spend_usd=10.0,
        )
        with pytest.raises(Exception):  # FrozenInstanceError
            econ.sends = 99  # type: ignore[misc]


# ────────────────────────────────────────────────────────────────────────
# evaluate_lane_breach — tier logic
# ────────────────────────────────────────────────────────────────────────


class TestEvaluateLaneBreachClear:
    def test_idle_lane_is_clear(self):
        """spend=0 + positives=0 = legitimate idle (no activity this week)."""
        econ = LaneEconomics(
            lane="target", week_starting=date(2026, 5, 18),
            sends=0, positives=0, spend_usd=0.0,
        )
        v = evaluate_lane_breach(econ)
        assert v.tier == "clear"
        assert v.observed_cpl is None
        assert "idle" in v.rationale.lower()

    def test_spend_without_positives_is_warning_not_clear(self):
        """spend>0 + positives=0 is a unit-economics breach signal —
        burned money without learning anything. Distinct from PR-17
        starvation (which tracks prospect inventory)."""
        econ = LaneEconomics(
            lane="target", week_starting=date(2026, 5, 18),
            sends=10, positives=0, spend_usd=1_000.0,
        )
        v = evaluate_lane_breach(econ)
        assert v.tier == "warning"
        assert v.observed_cpl is None
        assert "zero positives" in v.rationale.lower()

    def test_under_ceiling_is_clear(self):
        # target ceiling = $150; CPL = $100
        econ = LaneEconomics(
            lane="target", week_starting=date(2026, 5, 18),
            sends=10, positives=2, spend_usd=200.0,
        )
        v = evaluate_lane_breach(econ)
        assert v.tier == "clear"
        assert v.observed_cpl == 100.0

    def test_exactly_at_ceiling_is_clear(self):
        # Inclusive: CPL == ceiling does NOT breach. Strict > on the breach side.
        econ = LaneEconomics(
            lane="legacy", week_starting=date(2026, 5, 18),
            sends=10, positives=1, spend_usd=200.0,
        )
        v = evaluate_lane_breach(econ)
        assert v.tier == "clear"


class TestEvaluateLaneBreachWarning:
    def test_single_week_breach_is_warning(self):
        # target ceiling = $150; CPL = $300; prior_week_breached=False
        econ = LaneEconomics(
            lane="target", week_starting=date(2026, 5, 18),
            sends=10, positives=1, spend_usd=300.0,
            prior_week_breached=False,
        )
        v = evaluate_lane_breach(econ)
        assert v.tier == "warning"
        assert v.observed_cpl == 300.0
        assert v.ceiling == 150.0
        assert "advisory" in v.rationale.lower()


class TestEvaluateLaneBreachAlarm:
    def test_two_consecutive_breaches_is_alarm(self):
        econ = LaneEconomics(
            lane="enterprise", week_starting=date(2026, 5, 18),
            sends=10, positives=1, spend_usd=1000.0,  # CPL=1000 > 400
            prior_week_breached=True,                  # second week
        )
        v = evaluate_lane_breach(econ)
        assert v.tier == "alarm"
        assert "P1" in v.rationale

    def test_alarm_only_when_prior_breached(self):
        # Same CPL > ceiling, but no prior breach → warning, not alarm.
        econ = LaneEconomics(
            lane="enterprise", week_starting=date(2026, 5, 18),
            sends=10, positives=1, spend_usd=1000.0,
            prior_week_breached=False,
        )
        v = evaluate_lane_breach(econ)
        assert v.tier == "warning"


class TestCeilingOverride:
    def test_override_replaces_default(self):
        # Default target ceiling = $150; CPL = $200 would normally breach.
        # Override to $300 (operator-tuned via configuration_decision queue).
        econ = LaneEconomics(
            lane="target", week_starting=date(2026, 5, 18),
            sends=10, positives=1, spend_usd=200.0,
        )
        v = evaluate_lane_breach(econ, ceiling_override=300.0)
        assert v.tier == "clear"
        assert v.ceiling == 300.0


# ────────────────────────────────────────────────────────────────────────
# Idempotency key
# ────────────────────────────────────────────────────────────────────────


class TestIdempotencyKey:
    def test_key_pins_tier_lane_week(self):
        v = BreachVerdict(
            tier="alarm", lane="target",
            observed_cpl=200.0, ceiling=150.0, rationale="r",
        )
        key = _idempotency_key_for(v, week_starting=date(2026, 5, 18))
        assert key == "alarm|target|2026-05-18"

    def test_different_lanes_distinct_keys(self):
        a = BreachVerdict(tier="warning", lane="target", observed_cpl=200.0, ceiling=150.0, rationale="")
        b = BreachVerdict(tier="warning", lane="legacy", observed_cpl=200.0, ceiling=150.0, rationale="")
        ka = _idempotency_key_for(a, week_starting=date(2026, 5, 18))
        kb = _idempotency_key_for(b, week_starting=date(2026, 5, 18))
        assert ka != kb

    def test_different_tiers_distinct_keys(self):
        warn = BreachVerdict(tier="warning", lane="target", observed_cpl=200.0, ceiling=150.0, rationale="")
        alarm = BreachVerdict(tier="alarm", lane="target", observed_cpl=200.0, ceiling=150.0, rationale="")
        # Distinct keys means an upgrade from warning -> alarm opens a NEW
        # operator row instead of re-opening the warning. That's intentional:
        # alarm and warning are different escalation slugs in ESCALATION_TYPES.
        kw = _idempotency_key_for(warn, week_starting=date(2026, 5, 18))
        ka = _idempotency_key_for(alarm, week_starting=date(2026, 5, 18))
        assert kw != ka


# ────────────────────────────────────────────────────────────────────────
# Escalation invocation
# ────────────────────────────────────────────────────────────────────────


class TestOpenBreachEscalation:
    def test_clear_verdict_no_escalation(self):
        # Idle (spend=0, positives=0) — clear, no escalation.
        econ = LaneEconomics(
            lane="target", week_starting=date(2026, 5, 18),
            sends=0, positives=0, spend_usd=0.0,
        )
        verdict = evaluate_lane_breach(econ)
        assert verdict.tier == "clear"
        with patch("workflows.unit_economics.escalate") as m_escalate:
            out = open_breach_escalation(verdict, econ=econ)
        assert out is None
        m_escalate.assert_not_called()

    def test_warning_escalates_with_warning_slug(self):
        econ = LaneEconomics(
            lane="target", week_starting=date(2026, 5, 18),
            sends=10, positives=1, spend_usd=300.0,
            prior_week_breached=False,
        )
        verdict = evaluate_lane_breach(econ)
        assert verdict.tier == "warning"
        with patch("workflows.unit_economics.escalate") as m_escalate:
            m_escalate.return_value = {"id": {"record_id": "row-warn"}}
            out = open_breach_escalation(verdict, econ=econ)
        assert out["id"]["record_id"] == "row-warn"
        kwargs = m_escalate.call_args.kwargs
        assert kwargs["type"] == "unit_economics_alarm_warning"
        assert kwargs["idempotency_key"] == "warning|target|2026-05-18"
        payload = kwargs["payload"]
        assert payload["observed_cost_per_positive"] == 300.0
        assert payload["ceiling_usd"] == 150.0
        assert payload["opened_by"] == WRITER_MODULE
        assert payload["prior_week_breached"] is False

    def test_alarm_escalates_with_alarm_slug(self):
        econ = LaneEconomics(
            lane="enterprise", week_starting=date(2026, 5, 18),
            sends=10, positives=1, spend_usd=600.0,
            prior_week_breached=True,
        )
        verdict = evaluate_lane_breach(econ)
        assert verdict.tier == "alarm"
        with patch("workflows.unit_economics.escalate") as m_escalate:
            m_escalate.return_value = {"id": {"record_id": "row-alarm"}}
            out = open_breach_escalation(verdict, econ=econ)
        assert out["id"]["record_id"] == "row-alarm"
        kwargs = m_escalate.call_args.kwargs
        assert kwargs["type"] == "unit_economics_alarm"
        assert kwargs["idempotency_key"] == "alarm|enterprise|2026-05-18"
        assert kwargs["payload"]["prior_week_breached"] is True


# ────────────────────────────────────────────────────────────────────────
# fetch_prior_week_breached — lifts prior-breached signal out of
# caller's hands. Source of truth is the persisted queue row.
# ────────────────────────────────────────────────────────────────────────


class TestFetchPriorWeekBreached:
    def test_returns_false_when_no_prior_row(self):
        from unittest.mock import MagicMock
        attio = MagicMock()
        attio._request.return_value = {"data": []}
        assert fetch_prior_week_breached(
            attio, lane="target", this_week_starting=date(2026, 5, 25),
        ) is False

    def test_returns_true_when_prior_warning_exists(self):
        from unittest.mock import MagicMock
        attio = MagicMock()
        # First call (warning lookup) returns a hit
        attio._request.return_value = {"data": [{"id": {"record_id": "r1"}}]}
        out = fetch_prior_week_breached(
            attio, lane="target", this_week_starting=date(2026, 5, 25),
        )
        assert out is True
        # Filter uses the prior Monday's date (week-7 = 2026-05-18)
        kwargs = attio._request.call_args_list[0].kwargs
        assert kwargs["json"]["filter"]["idempotency_key"]["$eq"] == "warning|target|2026-05-18"

    def test_returns_true_when_prior_alarm_exists(self):
        from unittest.mock import MagicMock
        attio = MagicMock()
        # Warning lookup misses; alarm lookup hits
        attio._request.side_effect = [
            {"data": []},                                # warning miss
            {"data": [{"id": {"record_id": "r2"}}]},     # alarm hit
        ]
        assert fetch_prior_week_breached(
            attio, lane="enterprise", this_week_starting=date(2026, 5, 25),
        ) is True


# ────────────────────────────────────────────────────────────────────────
# Warning-supersede-on-alarm — alarm path triggers a PATCH on the
# matching warning row so operators see one current row per breach.
# ────────────────────────────────────────────────────────────────────────


class TestWarningSupersedeOnAlarm:
    def test_alarm_supersedes_existing_warning(self):
        from unittest.mock import MagicMock
        # Two responses queued: (1) escalate() result, (2) warning lookup
        # returns one row, (3) PATCH returns empty.
        attio = MagicMock()
        attio._request.side_effect = [
            # warning lookup
            {"data": [{"id": {"record_id": "warning-row-1"}}]},
            # patch response
            {"data": {}},
        ]

        econ = LaneEconomics(
            lane="enterprise", week_starting=date(2026, 5, 18),
            sends=10, positives=1, spend_usd=1_000.0,
            prior_week_breached=True,
        )
        verdict = evaluate_lane_breach(econ)
        with patch("workflows.unit_economics.escalate") as m_escalate:
            m_escalate.return_value = {"id": {"record_id": "row-alarm"}}
            open_breach_escalation(verdict, econ=econ, attio=attio)

        # Two calls: warning lookup + PATCH
        assert attio._request.call_count == 2
        last_call = attio._request.call_args_list[-1]
        # PATCH on the warning row
        assert last_call.args[0] == "PATCH"
        assert "operator_review_queue/records/warning-row-1" in last_call.args[1]
        assert last_call.kwargs["json"]["data"]["values"]["superseded_by_alarm"] is True

    def test_warning_lookup_failure_does_not_block_alarm(self, capsys):
        """A failure in the supersede note must not propagate — the
        alarm itself already wrote successfully."""
        from unittest.mock import MagicMock
        attio = MagicMock()
        attio._request.side_effect = RuntimeError("attio 500 on lookup")

        econ = LaneEconomics(
            lane="enterprise", week_starting=date(2026, 5, 18),
            sends=10, positives=1, spend_usd=1_000.0,
            prior_week_breached=True,
        )
        verdict = evaluate_lane_breach(econ)
        with patch("workflows.unit_economics.escalate") as m_escalate:
            m_escalate.return_value = {"id": {"record_id": "row-alarm"}}
            # Must NOT raise — supersede is best-effort.
            out = open_breach_escalation(verdict, econ=econ, attio=attio)
        assert out["id"]["record_id"] == "row-alarm"
        captured = capsys.readouterr()
        assert "mark_warning_superseded failed" in captured.err

    def test_warning_path_does_not_attempt_supersede(self):
        """Warning escalation must NOT call the supersede helper —
        nothing to supersede yet (this IS the first breach)."""
        from unittest.mock import MagicMock
        attio = MagicMock()
        econ = LaneEconomics(
            lane="target", week_starting=date(2026, 5, 18),
            sends=10, positives=1, spend_usd=300.0,
            prior_week_breached=False,
        )
        verdict = evaluate_lane_breach(econ)
        with patch("workflows.unit_economics.escalate") as m_escalate:
            m_escalate.return_value = {"id": {"record_id": "row-warn"}}
            open_breach_escalation(verdict, econ=econ, attio=attio)
        # Only escalate was invoked; no warning-lookup PATCH on attio.
        attio._request.assert_not_called()
