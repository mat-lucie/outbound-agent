"""Task 7: tests for per-persona cohort breakdown and defensive veto verdict."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

from models.experiment import Experiment, ExperimentStatus, append_experiment
from workflows.learn import (
    BASELINE_DEFENSIVE_RATE,
    evaluate_experiments,
    measure_cohorts,
)


def _entry(
    stage: str,
    experiment_id: str = "exp-v1",
    persona: str = "digitalization_champions",
    classification: str | None = None,
) -> dict:
    return {
        "entry_id": "eid",
        "record_id": "rid",
        "stage": stage,
        "persona": persona,
        "language": "es",
        "dm_step": None,
        "quality_score": None,
        "last_contact_date": None,
        "experiment_id": experiment_id,
        "response_classification": classification,
        "last_response_text": None,
    }


def _mock_attio(entries: list[dict]) -> MagicMock:
    attio = MagicMock()
    raw = [{"_data": e} for e in entries]
    attio.query_list_entries.return_value = raw
    attio.parse_entry.side_effect = lambda e: e["_data"]
    return attio


def _exp(
    exp_id: str,
    status: str,
    started: str,
    dm_response_rate: float = 0.08,
) -> Experiment:
    return Experiment(
        experiment_id=exp_id,
        started=started,
        completed="",
        cohort_size=50,
        dm_response_rate=dm_response_rate,
        baseline_rate=0.08,
        status=ExperimentStatus(status),
        variable="",
        description="test",
    )


# ── measure_cohorts classification_breakdown + defensive_rate ───────


class TestClassificationBreakdown:
    def test_empty_entries_return_empty_breakdown(self):
        attio = _mock_attio([])
        with patch("workflows.learn.load_experiments", return_value=[]):
            results = measure_cohorts(attio)
        assert results == []

    def test_classification_breakdown_counts_each_bucket(self):
        entries = [
            _entry("DM1 Sent", classification="none"),
            _entry("Responded", classification="positive"),
            _entry("Responded", classification="question"),
            _entry("Not Interested", classification="negative"),
            _entry("Responded", classification="defensive"),
            _entry("Responded", classification="neutral"),
        ]
        attio = _mock_attio(entries)
        with patch("workflows.learn.load_experiments", return_value=[]):
            results = measure_cohorts(attio)
        cohort = results[0]
        breakdown = cohort["classification_breakdown"]
        assert breakdown["positive"] == 1
        assert breakdown["question"] == 1
        assert breakdown["negative"] == 1
        assert breakdown["defensive"] == 1
        assert breakdown["neutral"] == 1
        assert breakdown["none"] == 1

    def test_defensive_rate_computed_correctly(self):
        entries = [
            _entry("DM1 Sent", classification="none"),
            _entry("Responded", classification="positive"),
            _entry("Responded", classification="defensive"),
        ]
        attio = _mock_attio(entries)
        with patch("workflows.learn.load_experiments", return_value=[]):
            results = measure_cohorts(attio)
        assert results[0]["defensive_rate"] == pytest.approx(1 / 3)

    def test_none_classification_for_unset_entries(self):
        """Entries without response_classification set must bucket as 'none'."""
        entries = [_entry("DM1 Sent"), _entry("DM2 Sent"), _entry("DM3 Sent")]
        attio = _mock_attio(entries)
        with patch("workflows.learn.load_experiments", return_value=[]):
            results = measure_cohorts(attio)
        breakdown = results[0]["classification_breakdown"]
        assert breakdown["none"] == 3
        assert breakdown["defensive"] == 0
        assert results[0]["defensive_rate"] == 0.0

    def test_unknown_classification_bucketed_as_none(self):
        """A classification we don't recognize falls into 'none' rather than crashing."""
        entries = [_entry("DM1 Sent", classification="weird_new_label")]
        attio = _mock_attio(entries)
        with patch("workflows.learn.load_experiments", return_value=[]):
            results = measure_cohorts(attio)
        breakdown = results[0]["classification_breakdown"]
        assert breakdown["none"] == 1

    def test_non_dmed_stages_excluded_from_breakdown(self):
        """Prospect/Connection Sent/Accepted shouldn't contribute to the breakdown."""
        entries = [
            _entry("Prospect", classification="defensive"),  # excluded
            _entry("Connection Sent", classification="defensive"),  # excluded
            _entry("DM1 Sent", classification="positive"),  # included
        ]
        attio = _mock_attio(entries)
        with patch("workflows.learn.load_experiments", return_value=[]):
            results = measure_cohorts(attio)
        breakdown = results[0]["classification_breakdown"]
        assert breakdown["positive"] == 1
        assert breakdown["defensive"] == 0


# ── measure_cohorts per_persona ─────────────────────────────────────


class TestPerPersonaBreakdown:
    def test_per_persona_split_by_persona(self):
        entries = [
            _entry("DM1 Sent", persona="digitalization_champions", classification="positive"),
            _entry("DM1 Sent", persona="digitalization_champions", classification="defensive"),
            _entry("DM1 Sent", persona="operations_leaders", classification="neutral"),
        ]
        attio = _mock_attio(entries)
        with patch("workflows.learn.load_experiments", return_value=[]):
            results = measure_cohorts(attio)
        per = results[0]["per_persona"]
        assert "digitalization_champions" in per
        assert "operations_leaders" in per
        assert per["digitalization_champions"]["dmed"] == 2
        assert per["digitalization_champions"]["defensive_rate"] == 0.5
        assert per["operations_leaders"]["dmed"] == 1
        assert per["operations_leaders"]["defensive_rate"] == 0.0

    def test_per_persona_has_response_rate(self):
        entries = [
            _entry("DM1 Sent", persona="execs", classification="none"),
            _entry("Responded", persona="execs", classification="positive"),
        ]
        attio = _mock_attio(entries)
        with patch("workflows.learn.load_experiments", return_value=[]):
            results = measure_cohorts(attio)
        execs = results[0]["per_persona"]["execs"]
        assert execs["dm_response_rate"] == pytest.approx(0.5)

    def test_unknown_persona_bucketed(self):
        entries = [_entry("DM1 Sent", persona=None, classification="positive")]
        attio = _mock_attio(entries)
        with patch("workflows.learn.load_experiments", return_value=[]):
            results = measure_cohorts(attio)
        per = results[0]["per_persona"]
        assert "unknown" in per


# ── evaluate_experiments defensive veto ─────────────────────────────


class TestDefensiveVeto:
    def _run(self, tmp_path, exp_id, dm_response_rate, defensive_rate):
        tsv = tmp_path / "experiments.tsv"
        append_experiment(
            _exp("baseline-v0", "won", "2026-01-01", dm_response_rate=0.08), tsv
        )
        append_experiment(_exp(exp_id, "running", "2026-04-01"), tsv)

        cohort = {
            "experiment_id": exp_id,
            "cohort_size": 30,
            "dmed": 20,
            "responded": round(dm_response_rate * 20),
            "dm_response_rate": dm_response_rate,
            "is_mature": True,
            "days_running": 10,
            "defensive_rate": defensive_rate,
            "classification_breakdown": {},
            "per_persona": {},
        }
        return evaluate_experiments([cohort], experiments_path=tsv)

    def test_high_defensive_rate_overrides_won(self, tmp_path):
        """A winning response rate can't save a cohort that's triggering reactance."""
        results = self._run(
            tmp_path,
            "exp-v3",
            dm_response_rate=0.30,  # normally a huge win
            defensive_rate=0.25,  # > 2 * 0.10
        )
        assert len(results) == 1
        assert results[0]["verdict"] == "rejected_defensive"

    def test_defensive_rate_below_threshold_keeps_won_verdict(self, tmp_path):
        results = self._run(
            tmp_path,
            "exp-v1",
            dm_response_rate=0.12,
            defensive_rate=0.05,  # < 0.20
        )
        assert results[0]["verdict"] == "won"

    def test_defensive_rate_below_threshold_keeps_lost_verdict(self, tmp_path):
        results = self._run(
            tmp_path,
            "exp-v1",
            dm_response_rate=0.03,
            defensive_rate=0.05,
        )
        assert results[0]["verdict"] == "lost"

    def test_verdict_includes_defensive_rate_and_baseline(self, tmp_path):
        results = self._run(
            tmp_path,
            "exp-v1",
            dm_response_rate=0.10,
            defensive_rate=0.08,
        )
        r = results[0]
        assert r["defensive_rate"] == pytest.approx(0.08)
        assert r["baseline_defensive_rate"] == pytest.approx(BASELINE_DEFENSIVE_RATE)

    def test_defensive_rate_at_exactly_2x_baseline_does_not_veto(self, tmp_path):
        """Boundary pin: the veto fires on STRICTLY greater than 2× baseline,
        not greater-or-equal. A variant at exactly 0.20 (with baseline 0.10)
        still counts as "won" if the response rate beats baseline.
        """
        results = self._run(
            tmp_path,
            "exp-boundary",
            dm_response_rate=0.15,
            defensive_rate=BASELINE_DEFENSIVE_RATE * 2,  # exactly 0.20
        )
        assert results[0]["verdict"] == "won"

    def test_defensive_rate_just_above_threshold_vetos(self, tmp_path):
        results = self._run(
            tmp_path,
            "exp-just-over",
            dm_response_rate=0.15,
            defensive_rate=BASELINE_DEFENSIVE_RATE * 2 + 0.001,
        )
        assert results[0]["verdict"] == "rejected_defensive"

    def test_missing_defensive_rate_defaults_to_zero(self, tmp_path):
        """Legacy cohort rows without defensive_rate shouldn't crash."""
        tsv = tmp_path / "experiments.tsv"
        append_experiment(
            _exp("baseline-v0", "won", "2026-01-01", dm_response_rate=0.08), tsv
        )
        append_experiment(_exp("exp-legacy", "running", "2026-04-01"), tsv)

        legacy_cohort = {
            "experiment_id": "exp-legacy",
            "cohort_size": 30,
            "dmed": 20,
            "responded": 3,
            "dm_response_rate": 0.15,
            "is_mature": True,
            "days_running": 10,
            # No defensive_rate — legacy shape
        }
        results = evaluate_experiments([legacy_cohort], experiments_path=tsv)
        assert results[0]["verdict"] == "won"
        assert results[0]["defensive_rate"] == 0.0


# ── Maturity check preserved ─────────────────────────────────────────


class TestMaturityPreserved:
    def test_immature_cohorts_still_skipped(self, tmp_path):
        tsv = tmp_path / "experiments.tsv"
        append_experiment(_exp("exp-immature", "running", "2026-04-01"), tsv)

        cohort = {
            "experiment_id": "exp-immature",
            "cohort_size": 5,
            "dmed": 5,
            "responded": 0,
            "dm_response_rate": 0.0,
            "is_mature": False,
            "days_running": 2,
            "defensive_rate": 0.9,  # huge defensive rate — still should NOT emit verdict
        }
        results = evaluate_experiments([cohort], experiments_path=tsv)
        assert results == []

    def test_maturity_unchanged_for_existing_logic(self):
        # fix/audit-learning-loop-stats: DM1-Sent entries (dm_step None) are not
        # cadence-settled, so the cohort must be >= 16 days old to pass the
        # cadence-completeness gate. 20 days clears all three gates.
        today = date.today()
        started = (today - timedelta(days=20)).isoformat()
        entries = [_entry("DM1 Sent") for _ in range(20)]
        attio = _mock_attio(entries)
        with patch(
            "workflows.learn.load_experiments",
            return_value=[_exp("exp-v1", "running", started)],
        ):
            results = measure_cohorts(attio)
        assert results[0]["is_mature"] is True
        assert results[0]["days_running"] == 20
