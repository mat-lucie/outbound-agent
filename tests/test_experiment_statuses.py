"""Test the ExperimentStatus enum, including rejected_defensive."""

import tempfile
from pathlib import Path

from models.experiment import (
    Experiment,
    ExperimentStatus,
    append_experiment,
    load_experiments,
)


def test_rejected_defensive_is_valid_status():
    assert "rejected_defensive" in {s.value for s in ExperimentStatus}


def test_existing_statuses_still_valid():
    """Regression: the five existing statuses must not have been dropped."""
    valid_values = {s.value for s in ExperimentStatus}
    for s in ["running", "won", "lost", "rejected", "insufficient_data"]:
        assert s in valid_values, f"Status '{s}' missing from ExperimentStatus"


def test_append_and_load_rejected_defensive_experiment():
    """Appending an experiment with rejected_defensive status must round-trip."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".tsv", delete=False) as tf:
        tsv_path = Path(tf.name)
    try:
        exp = Experiment(
            experiment_id="exp-003-digi-v3",
            started="2026-04-15",
            completed="2026-04-29",
            cohort_size=20,
            dm_response_rate=0.22,
            baseline_rate=0.18,
            status=ExperimentStatus.REJECTED_DEFENSIVE,
            variable="dm1_v3",
            description="V3 social proof bridge for digitalization_champions",
        )
        append_experiment(exp, path=tsv_path)
        loaded = load_experiments(path=tsv_path)
        assert len(loaded) == 1
        assert loaded[0].status == ExperimentStatus.REJECTED_DEFENSIVE
        assert loaded[0].experiment_id == "exp-003-digi-v3"
        assert loaded[0].cohort_size == 20
    finally:
        tsv_path.unlink(missing_ok=True)
