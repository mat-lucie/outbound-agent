"""Tests for the score-threshold ROC calibration."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from workflows.escalation_schemas import ESCALATION_SCHEMAS
from workflows.threshold_calibration import (
    ABSTAIN_THRESHOLD,
    _candidate_thresholds,
    _collect_labeled_pairs,
    _select_cost_weighted,
    _select_max_youden,
    _sweep,
    calibrate_score_threshold,
)


@pytest.fixture
def mock_attio():
    client = MagicMock()
    posted: list[dict] = []
    client.posted = posted  # type: ignore[attr-defined]

    def _request(method: str, path: str, json: dict | None = None, **_):
        if path.endswith("/records/query"):
            return {"data": []}
        if path.endswith("/records") and method == "POST":
            posted.append({"path": path, "body": json or {}})
            return {"data": {"id": {"record_id": f"rec-{len(posted)}"}}}
        return {"data": {}}

    client._request.side_effect = _request
    return client


def _labeled(stage: str, score: int, classification: str | None = None) -> dict:
    return {
        "stage": stage,
        "quality_score": score,
        "response_classification": classification,
    }


# =====================================================================
# Outcome labeling
# =====================================================================

class TestLabeling:
    def test_responded_question_classification_excluded(self):
        """Responded + classification='question' is an in-flight
        handoff, not a verdict — must be excluded from the cohort
        (NOT silently labeled as outcome=0)."""
        pairs, _ = _collect_labeled_pairs(
            MagicMock(),
            attio_query=lambda _a: [_labeled("Responded", 60, "question")],
        )
        assert pairs == []

    def test_responded_neutral_classification_excluded(self):
        pairs, _ = _collect_labeled_pairs(
            MagicMock(),
            attio_query=lambda _a: [_labeled("Responded", 60, "neutral")],
        )
        assert pairs == []

    def test_responded_none_classification_excluded(self):
        pairs, _ = _collect_labeled_pairs(
            MagicMock(),
            attio_query=lambda _a: [_labeled("Responded", 60, None)],
        )
        assert pairs == []

    def test_responded_negative_classification_is_negative(self):
        pairs, _ = _collect_labeled_pairs(
            MagicMock(),
            attio_query=lambda _a: [_labeled("Responded", 60, "negative")],
        )
        assert pairs == [(60, 0)]

    def test_non_numeric_score_counted_separately(self):
        """Non-numeric quality_score must be counted in
        n_dropped_invalid_score — silently dropping is the §0 #9
        failure mode this PR exists to fix."""
        pairs, n_dropped = _collect_labeled_pairs(
            MagicMock(),
            attio_query=lambda _a: [
                {"stage": "Qualified", "quality_score": "high"},
                {"stage": "Qualified", "quality_score": 75},
                {"stage": "Qualified", "quality_score": "N/A"},
            ],
        )
        assert pairs == [(75, 1)]
        assert n_dropped == 2

    def test_bool_score_is_dropped_not_coerced(self):
        """bool is an int subclass; without an isinstance(bool) guard
        True/False would coerce to 1/0 and corrupt the cohort."""
        pairs, n_dropped = _collect_labeled_pairs(
            MagicMock(),
            attio_query=lambda _a: [
                {"stage": "Qualified", "quality_score": True},
                {"stage": "Qualified", "quality_score": False},
            ],
        )
        assert pairs == []
        assert n_dropped == 2

    def test_qualified_is_positive(self):
        pairs, _ = _collect_labeled_pairs(
            MagicMock(),
            attio_query=lambda _a: [_labeled("Qualified", 78)],
        )
        assert pairs == [(78, 1)]

    def test_call_booked_is_positive(self):
        pairs, _ = _collect_labeled_pairs(
            MagicMock(),
            attio_query=lambda _a: [_labeled("Call Booked", 65)],
        )
        assert pairs == [(65, 1)]

    def test_responded_positive_classification_is_positive(self):
        pairs, _ = _collect_labeled_pairs(
            MagicMock(),
            attio_query=lambda _a: [_labeled("Responded", 60, "positive")],
        )
        assert pairs == [(60, 1)]

    def test_responded_defensive_is_negative(self):
        pairs, _ = _collect_labeled_pairs(
            MagicMock(),
            attio_query=lambda _a: [_labeled("Responded", 60, "defensive")],
        )
        assert pairs == [(60, 0)]

    def test_not_interested_is_negative(self):
        pairs, _ = _collect_labeled_pairs(
            MagicMock(),
            attio_query=lambda _a: [_labeled("Not Interested", 55)],
        )
        assert pairs == [(55, 0)]

    def test_in_flight_stages_excluded(self):
        """A row still in DM1_Sent / Prospect has no label yet — must
        NOT pollute the ROC table."""
        pairs, _ = _collect_labeled_pairs(
            MagicMock(),
            attio_query=lambda _a: [
                _labeled("DM1 Sent", 70),
                _labeled("Prospect", 50),
                _labeled("Connection Sent", 60),
            ],
        )
        assert pairs == []

    def test_missing_score_excluded(self):
        pairs, _ = _collect_labeled_pairs(
            MagicMock(),
            attio_query=lambda _a: [
                {"stage": "Qualified", "quality_score": None},
            ],
        )
        assert pairs == []


# =====================================================================
# ROC sweep + selection
# =====================================================================

class TestSweep:
    def test_thresholds_are_40_to_80_step_5(self):
        assert _candidate_thresholds() == [40, 45, 50, 55, 60, 65, 70, 75, 80]

    def test_sweep_produces_one_row_per_threshold(self):
        pairs = [(80, 1), (50, 0)] * 30
        rows = _sweep(pairs)
        assert len(rows) == 9
        assert [r["threshold"] for r in rows] == _candidate_thresholds()

    def test_perfect_separator_has_youden_1(self):
        """All positives at 80, all negatives at 40 → threshold=45..80
        gives perfect classification → youden=1.0."""
        pairs = [(80, 1)] * 25 + [(40, 0)] * 25
        rows = _sweep(pairs)
        # threshold=45..80 should be perfect.
        for r in rows:
            if 45 <= r["threshold"] <= 80:
                assert r["youden"] == 1.0
        # threshold=40 includes all → TPR=1, TNR=0, youden=0.
        t40 = next(r for r in rows if r["threshold"] == 40)
        assert t40["youden"] == 0.0


class TestSelectMaxYouden:
    def test_picks_first_max_threshold_on_tie(self):
        # All positives at 70, all negatives at 50. Thresholds 55..70
        # all classify perfectly. Python's max() is left-stable; the
        # FIRST tied row wins.
        pairs = [(70, 1)] * 25 + [(50, 0)] * 25
        rows = _sweep(pairs)
        sel = _select_max_youden(rows)
        assert sel["youden"] == 1.0
        # Strict tie-break: 55 is the lowest threshold with Youden=1.0
        # and wins via list-order stability.
        assert sel["threshold"] == 55


class TestSelectCostWeighted:
    def test_value_dominant_picks_strictly_lower_threshold_than_cost_dominant(self):
        # Data spread that creates an observable trade-off: positives
        # spread across [50, 60, 70]; negatives spread across [45, 55,
        # 65, 75] so different thresholds give different (TPR, TNR)
        # combinations rather than collapsing to a degenerate optimum.
        pairs = (
            [(50, 1)] * 8 + [(60, 1)] * 12 + [(70, 1)] * 20 +
            [(45, 0)] * 25 + [(55, 0)] * 15 + [(65, 0)] * 10 + [(75, 0)] * 5
        )
        rows = _sweep(pairs)
        low = _select_cost_weighted(
            rows, cost_per_send=0.01, value_per_positive=1000.0,
        )
        high = _select_cost_weighted(
            rows, cost_per_send=1000.0, value_per_positive=0.01,
        )
        # Strict ordering: high-value picks a strictly lower threshold
        # than high-cost. A trivial pass (both at 50) is rejected.
        assert low["threshold"] < high["threshold"], (
            f"value-dominant picked t={low['threshold']}, "
            f"cost-dominant picked t={high['threshold']} — trade-off "
            f"not observable; check the data spread"
        )


# =====================================================================
# Abstain
# =====================================================================

class TestAbstain:
    def test_below_threshold_abstains(self, mock_attio):
        pairs = [_labeled("Qualified", 70) for _ in range(ABSTAIN_THRESHOLD - 1)]
        out = calibrate_score_threshold(
            mock_attio,
            cost_per_send=1.0,
            value_per_positive=50.0,
            today=date(2026, 5, 22),
            attio_query=lambda _a: pairs,
        )
        assert out["status"] == "abstain"
        assert out["n_labeled"] == ABSTAIN_THRESHOLD - 1
        assert out["reason"] == "insufficient_labeled_data"

    def test_abstain_opens_configuration_decision_row(self, mock_attio):
        pairs = [_labeled("Qualified", 70) for _ in range(10)]
        calibrate_score_threshold(
            mock_attio,
            cost_per_send=1.0,
            value_per_positive=50.0,
            today=date(2026, 5, 22),
            attio_query=lambda _a: pairs,
        )
        # The queue-row create POST contains the configuration_decision type.
        body = mock_attio.posted[0]["body"]
        attrs = body.get("data", {}).get("values", {})
        assert attrs.get("type") == "configuration_decision"

    def test_exactly_at_threshold_proceeds(self, mock_attio):
        """Boundary: n_labeled == ABSTAIN_THRESHOLD must NOT abstain.
        The check is `<`, not `<=`."""
        pairs = [
            _labeled("Qualified", 70) for _ in range(ABSTAIN_THRESHOLD // 2)
        ] + [
            _labeled("Not Interested", 50)
            for _ in range(ABSTAIN_THRESHOLD - ABSTAIN_THRESHOLD // 2)
        ]
        assert len(pairs) == ABSTAIN_THRESHOLD
        out = calibrate_score_threshold(
            mock_attio,
            cost_per_send=1.0,
            value_per_positive=50.0,
            today=date(2026, 5, 22),
            attio_query=lambda _a: pairs,
        )
        assert out["status"] == "ok"


class TestClassImbalanceAbstain:
    """A near-one-class cohort produces meaningless ROC math (one
    class always achieves 1.0 rate; the other always 0.0). Abstain
    must fire before the operator sees a degenerate-but-confident
    recommendation."""

    def test_all_positives_abstains(self, mock_attio):
        pairs = [_labeled("Qualified", 75) for _ in range(60)]
        out = calibrate_score_threshold(
            mock_attio,
            cost_per_send=1.0, value_per_positive=50.0,
            today=date(2026, 5, 22),
            attio_query=lambda _a: pairs,
        )
        assert out["status"] == "abstain"
        assert out["reason"] == "class_imbalance"

    def test_all_negatives_abstains(self, mock_attio):
        pairs = [_labeled("Not Interested", 45) for _ in range(60)]
        out = calibrate_score_threshold(
            mock_attio,
            cost_per_send=1.0, value_per_positive=50.0,
            today=date(2026, 5, 22),
            attio_query=lambda _a: pairs,
        )
        assert out["status"] == "abstain"
        assert out["reason"] == "class_imbalance"

    def test_too_few_positives_abstains(self, mock_attio):
        # 55 labeled rows but only 2 positives → degenerate.
        pairs = (
            [_labeled("Qualified", 75) for _ in range(2)] +
            [_labeled("Not Interested", 45) for _ in range(53)]
        )
        out = calibrate_score_threshold(
            mock_attio,
            cost_per_send=1.0, value_per_positive=50.0,
            today=date(2026, 5, 22),
            attio_query=lambda _a: pairs,
        )
        assert out["status"] == "abstain"
        assert out["reason"] == "class_imbalance"


class TestIdempotency:
    """Two calls on the same `today` must produce the same
    idempotency key. The downstream escalate() dedup ensures only
    one row lands."""

    def test_same_today_produces_same_idempotency_key(self, mock_attio):
        pairs = (
            [_labeled("Qualified", 75) for _ in range(30)] +
            [_labeled("Not Interested", 45) for _ in range(30)]
        )
        out1 = calibrate_score_threshold(
            mock_attio,
            cost_per_send=1.0, value_per_positive=50.0,
            today=date(2026, 5, 22),
            attio_query=lambda _a: pairs,
        )
        out2 = calibrate_score_threshold(
            mock_attio,
            cost_per_send=1.0, value_per_positive=50.0,
            today=date(2026, 5, 22),
            attio_query=lambda _a: pairs,
        )
        assert out1["queue_row_key"] == out2["queue_row_key"]


# =====================================================================
# Happy-path queue row
# =====================================================================

class TestQueueRowShape:
    def test_ok_row_carries_full_roc_table(self, mock_attio):
        pairs = (
            [_labeled("Qualified", 75) for _ in range(30)] +
            [_labeled("Not Interested", 45) for _ in range(30)]
        )
        out = calibrate_score_threshold(
            mock_attio,
            cost_per_send=1.0,
            value_per_positive=50.0,
            today=date(2026, 5, 22),
            attio_query=lambda _a: pairs,
        )
        assert out["status"] == "ok"
        assert len(out["roc_table"]) == 9
        assert out["max_youden"]["threshold"] in _candidate_thresholds()
        body = mock_attio.posted[0]["body"]
        attrs = body.get("data", {}).get("values", {})
        assert attrs.get("type") == "threshold_recommendation"


# =====================================================================
# Schema registration
# =====================================================================

class TestSchemaRegistered:
    def test_threshold_recommendation_payload_registered(self):
        assert "threshold_recommendation" in ESCALATION_SCHEMAS

    def test_payload_required_fields_validation(self):
        from workflows.escalation import _validate_payload_against_typeddict
        from workflows.escalation_schemas import EscalationSchemaError

        with pytest.raises(EscalationSchemaError):
            _validate_payload_against_typeddict(
                "threshold_recommendation", {"today": "2026-05-22"},
            )


# =====================================================================
# Pure-Attio (zero LLM cost) — sanity check by introspection
# =====================================================================

class TestZeroLlmCost:
    def test_no_anthropic_import_in_threshold_calibration(self):
        import workflows.threshold_calibration as tc

        with open(tc.__file__) as f:
            source = f.read()
        assert "anthropic" not in source.lower(), (
            "ROC calibration must be pure Attio + numpy math — "
            "no LLM dispatch"
        )


# =====================================================================
# Script entry point (scripts/threshold_calibration.py)
# =====================================================================


class TestEnvFloatFailLoud:
    def test_garbage_string_raises_systemexit(self, monkeypatch):
        from scripts.threshold_calibration import _env_float

        monkeypatch.setenv("OUTBOUND_FAKE_FLOAT", "O.5")  # letter O, not zero
        with pytest.raises(SystemExit) as exc:
            _env_float("OUTBOUND_FAKE_FLOAT", 0.50)
        assert exc.value.code == 2

    def test_negative_value_raises(self, monkeypatch):
        from scripts.threshold_calibration import _env_float

        monkeypatch.setenv("OUTBOUND_FAKE_FLOAT", "-1.0")
        with pytest.raises(SystemExit) as exc:
            _env_float("OUTBOUND_FAKE_FLOAT", 0.50)
        assert exc.value.code == 2

    def test_nan_raises(self, monkeypatch):
        from scripts.threshold_calibration import _env_float

        monkeypatch.setenv("OUTBOUND_FAKE_FLOAT", "nan")
        with pytest.raises(SystemExit) as exc:
            _env_float("OUTBOUND_FAKE_FLOAT", 0.50)
        assert exc.value.code == 2

    def test_unset_returns_default(self, monkeypatch):
        from scripts.threshold_calibration import _env_float

        monkeypatch.delenv("OUTBOUND_FAKE_FLOAT", raising=False)
        assert _env_float("OUTBOUND_FAKE_FLOAT", 0.50) == 0.50

    def test_valid_float_returns_parsed(self, monkeypatch):
        from scripts.threshold_calibration import _env_float

        monkeypatch.setenv("OUTBOUND_FAKE_FLOAT", "1.25")
        assert _env_float("OUTBOUND_FAKE_FLOAT", 0.50) == 1.25


class TestRenderSummary:
    def test_abstain_render_includes_reason_and_n_dropped(self):
        from scripts.threshold_calibration import _render_summary

        out = {
            "status": "abstain",
            "reason": "class_imbalance",
            "n_labeled": 60,
            "n_dropped_invalid_score": 2,
            "queue_row_key": "k-1",
        }
        text = _render_summary(out)
        assert "ABSTAIN" in text
        assert "class_imbalance" in text
        assert "n_labeled=60" in text
        assert "n_dropped_invalid_score=2" in text
        assert "k-1" in text

    def test_ok_render_includes_max_youden_and_cost_weighted(self):
        from scripts.threshold_calibration import _render_summary

        out = {
            "status": "ok",
            "n_labeled": 80,
            "n_dropped_invalid_score": 0,
            "max_youden": {
                "threshold": 60, "tpr": 0.9, "tnr": 0.8, "youden": 0.7,
            },
            "cost_weighted": {
                "threshold": 65, "tpr": 0.85, "tnr": 0.85,
            },
            "queue_row_key": "k-2",
        }
        text = _render_summary(out)
        assert "max-Youden threshold: 60" in text
        assert "cost-weighted threshold: 65" in text
        assert "NOT changed" in text


class TestDryRunHonest:
    """--dry-run must actually compute the ROC table from the real
    cohort but skip the queue-row write. A "would compute" stub is the
    silent-failure pattern §0 #9 prohibits."""

    def test_dry_run_attio_intercepts_post_but_passes_reads(self):
        from scripts.threshold_calibration import _DryRunAttio

        inner = MagicMock()
        inner._request = MagicMock(
            side_effect=lambda method, path, json=None, **_: (
                {"data": []} if "query" in path
                else {"data": {"id": {"record_id": "rec-x"}}}
            ),
        )
        wrapped = _DryRunAttio(inner)
        # Query passes through.
        wrapped._request("POST", "/objects/x/records/query")
        # Write is intercepted.
        wrapped._request("POST", "/objects/x/records", json={"payload": 1})
        assert len(wrapped.intercepted_posts) == 1
        assert wrapped.intercepted_posts[0]["body"] == {"payload": 1}


class TestExitCodes:
    """The script's exit-code contract: 0 on both ok and abstain;
    non-zero reserved for tooling failure (env var typo, missing key)."""

    def test_main_exits_0_on_ok(self, monkeypatch, capsys):
        from scripts.threshold_calibration import main

        # Stub the calibrate function so the test runs without Attio.
        def _stub_calibrate(*_a, **_k):
            return {
                "status": "ok",
                "n_labeled": 80,
                "n_dropped_invalid_score": 0,
                "max_youden": {"threshold": 60, "tpr": 0.9, "tnr": 0.8, "youden": 0.7},
                "cost_weighted": {"threshold": 65, "tpr": 0.85, "tnr": 0.85},
                "queue_row_key": "k-2",
            }
        monkeypatch.setattr(
            "scripts.threshold_calibration.calibrate_score_threshold",
            _stub_calibrate,
        )
        monkeypatch.setattr(
            "clients.attio.AttioClient.__init__", lambda self: None,
        )
        monkeypatch.setattr(
            "clients.attio.AttioClient.__enter__", lambda self: self,
        )
        monkeypatch.setattr(
            "clients.attio.AttioClient.__exit__", lambda self, *a: False,
        )
        rc = main([])
        assert rc == 0

    def test_main_exits_0_on_abstain(self, monkeypatch):
        from scripts.threshold_calibration import main

        def _stub_calibrate(*_a, **_k):
            return {
                "status": "abstain",
                "reason": "insufficient_labeled_data",
                "n_labeled": 12,
                "n_dropped_invalid_score": 0,
                "queue_row_key": "k-abstain",
            }
        monkeypatch.setattr(
            "scripts.threshold_calibration.calibrate_score_threshold",
            _stub_calibrate,
        )
        monkeypatch.setattr(
            "clients.attio.AttioClient.__init__", lambda self: None,
        )
        monkeypatch.setattr(
            "clients.attio.AttioClient.__enter__", lambda self: self,
        )
        monkeypatch.setattr(
            "clients.attio.AttioClient.__exit__", lambda self, *a: False,
        )
        rc = main([])
        assert rc == 0

    def test_main_exits_2_on_missing_attio_key(self, monkeypatch):
        from scripts.threshold_calibration import main

        def _broken_init(self):
            raise KeyError("ATTIO_API_KEY")
        monkeypatch.setattr(
            "clients.attio.AttioClient.__init__", _broken_init,
        )
        rc = main([])
        assert rc == 2
