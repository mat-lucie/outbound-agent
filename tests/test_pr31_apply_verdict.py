"""PR-31 / Wave-2-A tests: all-4-verdict persistence to experiments.tsv +
REJECTED_DEFENSIVE auto-pause.

Covers ``workflows.learn.apply_verdict`` and the ``to_experiment_verdict``
converter. ``experiments.tsv`` is the system of record (Wave-2-A — the Attio
``experiment`` object was descoped); verdict status writes go to the TSV via
``models.experiment.upsert_experiment``. The ``operator_review_queue`` Attio
object IS live, so the REJECTED_DEFENSIVE escalation is preserved.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from models.experiment import (
    Experiment,
    ExperimentStatus,
    append_experiment,
    load_experiments,
)
from workflows.learn import (
    APPLY_VERDICT_WRITER_MODULE,
    ExperimentNotFoundError,
    apply_verdict,
    to_experiment_verdict,
)

# ────────────────────────────────────────────────────────────────────────
# to_experiment_verdict
# ────────────────────────────────────────────────────────────────────────


class TestToExperimentVerdict:
    def test_won(self):
        assert to_experiment_verdict("won") == "WON"

    def test_lost(self):
        assert to_experiment_verdict("lost") == "LOST"

    def test_inconclusive_maps_to_rejected_null(self):
        # Renaming per Round-4 D10: the evaluator's "inconclusive" is the
        # REJECTED_NULL terminal status.
        assert to_experiment_verdict("inconclusive") == "REJECTED_NULL"

    def test_rejected_defensive(self):
        assert to_experiment_verdict("rejected_defensive") == "REJECTED_DEFENSIVE"

    def test_unknown_returns_none(self):
        # §0 #9: silent skip is correct for unrecognized verdicts. Caller
        # must handle None — the cli.py learn flow echoes a "Skipped"
        # breadcrumb when this happens.
        assert to_experiment_verdict("running") is None
        assert to_experiment_verdict("superseded") is None
        assert to_experiment_verdict("") is None
        assert to_experiment_verdict("WON") is None  # case-sensitive


# ────────────────────────────────────────────────────────────────────────
# apply_verdict — TSV persistence
# ────────────────────────────────────────────────────────────────────────


def _running_exp(experiment_id: str = "exp-001") -> Experiment:
    return Experiment(
        experiment_id=experiment_id,
        started="2026-05-01",
        completed="",
        cohort_size=0,
        dm_response_rate=0.0,
        baseline_rate=0.04,
        status=ExperimentStatus.RUNNING,
        variable="messages.json:dm1:operations_leaders:es",
        description="running variant",
    )


@pytest.fixture
def tsv_with_running(tmp_path):
    """Return (path, attio_mock) with one running exp-001 row in the TSV."""
    tsv = tmp_path / "experiments.tsv"
    append_experiment(_running_exp("exp-001"), tsv)
    attio = MagicMock()
    return tsv, attio


def _row_for(path, experiment_id: str) -> Experiment:
    rows = [e for e in load_experiments(path) if e.experiment_id == experiment_id]
    assert rows, f"no row for {experiment_id}"
    return rows[-1]  # last-row-wins


class TestApplyVerdictPersistsToTsv:
    def test_won_writes_status_to_tsv(self, tsv_with_running):
        tsv, attio = tsv_with_running
        result = apply_verdict("exp-001", "WON", attio=attio, experiments_path=tsv)

        assert result["status_written"] == ExperimentStatus.WON.value == "won"
        assert result["queue_opened"] is False
        assert result["prior_status"] == "running"  # sourced from the TSV row

        # Exactly one row, status flipped — no duplicate appended.
        rows = load_experiments(tsv)
        assert len(rows) == 1
        assert rows[0].status == ExperimentStatus.WON

    def test_lost_writes_status_to_tsv(self, tsv_with_running):
        tsv, attio = tsv_with_running
        result = apply_verdict("exp-001", "LOST", attio=attio, experiments_path=tsv)
        assert result["status_written"] == "lost"
        assert _row_for(tsv, "exp-001").status == ExperimentStatus.LOST
        assert len(load_experiments(tsv)) == 1

    def test_rejected_null_writes_status_no_queue(self, tsv_with_running):
        tsv, attio = tsv_with_running
        result = apply_verdict(
            "exp-001", "REJECTED_NULL", attio=attio, experiments_path=tsv,
        )
        assert result["status_written"] == "rejected_null"
        assert result["queue_opened"] is False
        assert _row_for(tsv, "exp-001").status == ExperimentStatus.REJECTED_NULL
        assert len(load_experiments(tsv)) == 1

    @patch("workflows.escalation.escalate")
    def test_rejected_defensive_writes_status_to_tsv(
        self, mock_escalate, tsv_with_running
    ):
        tsv, attio = tsv_with_running
        mock_escalate.return_value = {"id": {"record_id": "queue-1"}}
        result = apply_verdict(
            "exp-001", "REJECTED_DEFENSIVE", attio=attio, experiments_path=tsv,
        )
        assert result["status_written"] == "rejected_defensive"
        assert result["queue_opened"] is True
        assert _row_for(tsv, "exp-001").status == ExperimentStatus.REJECTED_DEFENSIVE
        # Single row — no duplicate.
        assert len(load_experiments(tsv)) == 1

    def test_metrics_forwarded_to_persisted_row(self, tsv_with_running):
        """The freshly-measured cohort metrics (passed via `metrics`) land on
        the persisted terminal row rather than the running row's placeholders."""
        tsv, attio = tsv_with_running
        metrics = {
            "cohort_size": 42,
            "dm_response_rate": 0.18,
            "baseline_rate": 0.04,
            "dm1_response_rate": 0.18,
        }
        apply_verdict(
            "exp-001", "WON", attio=attio, experiments_path=tsv, metrics=metrics,
        )
        row = _row_for(tsv, "exp-001")
        assert row.cohort_size == 42
        assert row.dm_response_rate == pytest.approx(0.18)
        assert row.dm1_response_rate == pytest.approx(0.18)


class TestApplyVerdictEscalationOrdering:
    @patch("workflows.escalation.escalate")
    def test_rejected_defensive_opens_queue_idempotent_on_id(
        self, mock_escalate, tsv_with_running
    ):
        tsv, attio = tsv_with_running
        mock_escalate.return_value = {"id": {"record_id": "queue-1"}}

        apply_verdict(
            "exp-001", "REJECTED_DEFENSIVE", attio=attio, experiments_path=tsv,
        )

        mock_escalate.assert_called_once()
        call_kwargs = mock_escalate.call_args.kwargs
        assert call_kwargs["type"] == "variant_paused_defensive"
        assert call_kwargs["idempotency_key"] == "exp-001"
        assert call_kwargs["payload"]["experiment_id"] == "exp-001"
        assert call_kwargs["payload"]["reason"] == "defensive_rate_exceeded_threshold"
        assert call_kwargs["payload"]["auto_paused_by"] == APPLY_VERDICT_WRITER_MODULE
        # prior_status in the payload is sourced from the TSV row.
        assert call_kwargs["payload"]["prior_status"] == "running"
        assert call_kwargs["attio"] is attio

    @patch("workflows.escalation.escalate")
    def test_escalate_failure_skips_status_write(
        self, mock_escalate, tsv_with_running
    ):
        """C1: escalate BEFORE status write. When escalate raises, the verdict
        status must NOT be persisted to the TSV — otherwise the variant is
        marked rejected_defensive with no operator queue row, and the next
        learn run sees status != running, skips the cohort, and the defensive
        variant keeps sending with no queue row. Next cycle retries cleanly
        because the variant is still status=running."""
        tsv, attio = tsv_with_running
        mock_escalate.side_effect = RuntimeError("attio 503 on queue insert")

        with pytest.raises(RuntimeError, match="attio 503"):
            apply_verdict(
                "exp-001", "REJECTED_DEFENSIVE",
                attio=attio, experiments_path=tsv,
            )

        # Status write was NOT attempted — the TSV row is still 'running'.
        assert _row_for(tsv, "exp-001").status == ExperimentStatus.RUNNING
        assert len(load_experiments(tsv)) == 1


class TestApplyVerdictErrors:
    def test_experiment_not_found_raises(self, tmp_path):
        tsv = tmp_path / "experiments.tsv"
        append_experiment(_running_exp("exp-present"), tsv)
        attio = MagicMock()

        with pytest.raises(ExperimentNotFoundError) as exc_info:
            apply_verdict("nonexistent", "WON", attio=attio, experiments_path=tsv)

        assert exc_info.value.experiment_id == "nonexistent"
        # No write occurred — the present row is untouched.
        assert _row_for(tsv, "exp-present").status == ExperimentStatus.RUNNING

    def test_not_a_value_error(self):
        """Subclass of RuntimeError so ``except ValueError`` blocks elsewhere
        don't silently swallow the §3.1-adjacent failure."""
        assert issubclass(ExperimentNotFoundError, RuntimeError)
        assert not issubclass(ExperimentNotFoundError, ValueError)


class TestApplyVerdictWriterEnforcement:
    EXPECTED_WRITER_MODULE = "workflows.learn.apply_verdict"

    def test_writer_module_constant_value(self):
        assert APPLY_VERDICT_WRITER_MODULE == self.EXPECTED_WRITER_MODULE

    def test_writer_module_matches_registry_entry(self):
        """Registry symmetry canary: the experiment status writer entry is
        retained for symmetry even though the write now goes to the TSV."""
        from clients.attio_writer_registry import WRITE_OWNER_REGISTRY
        registered = WRITE_OWNER_REGISTRY[("experiment", "status")]
        assert isinstance(registered, list)
        assert self.EXPECTED_WRITER_MODULE in registered


class TestImportSurface:
    def test_module_exposes_public_api(self):
        from workflows import learn

        assert hasattr(learn, "ExperimentVerdict")
        assert hasattr(learn, "apply_verdict")
        assert hasattr(learn, "to_experiment_verdict")
        assert hasattr(learn, "ExperimentNotFoundError")
        assert hasattr(learn, "APPLY_VERDICT_WRITER_MODULE")


# ────────────────────────────────────────────────────────────────────────
# cli learn — single TSV write (no double-write / no tail mirror)
# ────────────────────────────────────────────────────────────────────────


class TestCliLearnSingleWrite:
    def test_cli_learn_writes_each_verdict_exactly_once(self, tmp_path, monkeypatch):
        """C3 + §3: cli `learn` must persist each terminal verdict to the TSV
        exactly once. The old tail WON/LOST mirror (a second append_experiment)
        was deleted — a double-write would duplicate rows because
        append_experiment is append-only, not upsert."""
        from click.testing import CliRunner

        import models.experiment as exp_mod
        from cli import cli

        tsv = tmp_path / "experiments.tsv"
        # Two running experiments; the learn run will resolve a WON and a LOST.
        append_experiment(_running_exp("exp-win"), tsv)
        append_experiment(_running_exp("exp-lose"), tsv)
        monkeypatch.setattr(exp_mod, "DEFAULT_TSV_PATH", tsv)

        fake_verdicts = [
            {
                "experiment_id": "exp-win", "verdict": "won", "step": "overall",
                "rate_metric": "dm_response_rate", "step_rate": 0.2,
                "step_baseline_rate": 0.04, "cohort_size": 30,
                "dm_response_rate": 0.2, "baseline_rate": 0.04,
                "dmed": 30, "responded": 6, "prior_dominated": False,
                "dm1_response_rate": 0.2, "dm2_response_rate": None,
                "dm3_response_rate": None,
            },
            {
                "experiment_id": "exp-lose", "verdict": "lost", "step": "overall",
                "rate_metric": "dm_response_rate", "step_rate": 0.01,
                "step_baseline_rate": 0.04, "cohort_size": 30,
                "dm_response_rate": 0.01, "baseline_rate": 0.04,
                "dmed": 30, "responded": 0, "prior_dominated": False,
                "dm1_response_rate": 0.01, "dm2_response_rate": None,
                "dm3_response_rate": None,
            },
        ]

        with (
            patch("clients.attio.AttioClient") as mock_client_cls,
            patch("workflows.learn.measure_cohorts", return_value=[]),
            patch("workflows.learn.evaluate_experiments", return_value=fake_verdicts),
        ):
            mock_client_cls.return_value.__enter__.return_value = MagicMock()
            result = CliRunner().invoke(cli, ["learn"])

        assert result.exit_code == 0, result.output

        rows = load_experiments(tsv)
        ids = [e.experiment_id for e in rows]
        # Exactly one row per experiment — NO duplicate from a tail mirror.
        assert ids.count("exp-win") == 1
        assert ids.count("exp-lose") == 1
        by_id = {e.experiment_id: e for e in rows}
        assert by_id["exp-win"].status == ExperimentStatus.WON
        assert by_id["exp-lose"].status == ExperimentStatus.LOST
