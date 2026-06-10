"""Tests for models/experiment.py."""

from unittest.mock import MagicMock

import pytest

from models.experiment import (
    Experiment,
    ExperimentStatus,
    MultipleRunningExperimentsError,
    append_experiment,
    get_active_experiments,
    get_baseline_rate,
    get_baseline_step_rate,
    get_current_experiment_id,
    load_experiments,
    upsert_experiment,
)


def _make_exp(**kwargs) -> Experiment:
    defaults = dict(
        experiment_id="exp-001",
        started="2026-04-01",
        completed="",
        cohort_size=50,
        dm_response_rate=0.12,
        baseline_rate=0.08,
        status="running",
        variable="messages.json:dm1:operations_leaders:es",
        description="Test variant",
    )
    defaults.update(kwargs)
    # Convert string status to ExperimentStatus enum
    defaults["status"] = ExperimentStatus(defaults["status"])
    return Experiment(**defaults)


# ── load_experiments ──────────────────────────────────────────────────────────

def test_load_experiments_empty_file(tmp_path):
    tsv = tmp_path / "experiments.tsv"
    tsv.write_text(
        "experiment_id\tstarted\tcompleted\tcohort_size\tdm_response_rate"
        "\tbaseline_rate\tstatus\tvariable\tdescription\n"
    )
    result = load_experiments(tsv)
    assert result == []


def test_load_experiments_missing_file(tmp_path):
    result = load_experiments(tmp_path / "nonexistent.tsv")
    assert result == []


def test_load_experiments_with_rows(tmp_path):
    tsv = tmp_path / "experiments.tsv"
    tsv.write_text(
        "experiment_id\tstarted\tcompleted\tcohort_size\tdm_response_rate"
        "\tbaseline_rate\tstatus\tvariable\tdescription\n"
        "baseline-v0\t2026-01-01\t2026-02-01\t100\t0.08\t0.0\twon"
        "\tbaseline\tInitial baseline\n"
        "exp-001\t2026-04-01\t\t50\t0.12\t0.08\trunning"
        "\tmessages.json:dm1:operations_leaders:es\tTest variant\n"
    )
    result = load_experiments(tsv)
    assert len(result) == 2
    assert result[0].experiment_id == "baseline-v0"
    assert result[0].cohort_size == 100
    assert result[0].dm_response_rate == pytest.approx(0.08)
    assert result[0].status == "won"
    assert result[1].experiment_id == "exp-001"
    assert result[1].completed == ""
    assert result[1].status == "running"


# ── append_experiment ─────────────────────────────────────────────────────────

def test_append_experiment_creates_file(tmp_path):
    tsv = tmp_path / "experiments.tsv"
    exp = _make_exp()
    append_experiment(exp, tsv)
    result = load_experiments(tsv)
    assert len(result) == 1
    assert result[0].experiment_id == "exp-001"


def test_append_experiment_adds_to_existing(tmp_path):
    tsv = tmp_path / "experiments.tsv"
    exp1 = _make_exp(experiment_id="exp-001")
    exp2 = _make_exp(experiment_id="exp-002", status="lost")
    append_experiment(exp1, tsv)
    append_experiment(exp2, tsv)
    result = load_experiments(tsv)
    assert len(result) == 2
    assert result[1].experiment_id == "exp-002"
    assert result[1].status == "lost"


def test_append_experiment_preserves_types(tmp_path):
    tsv = tmp_path / "experiments.tsv"
    exp = _make_exp(cohort_size=77, dm_response_rate=0.155, baseline_rate=0.09)
    append_experiment(exp, tsv)
    result = load_experiments(tsv)
    assert result[0].cohort_size == 77
    assert result[0].dm_response_rate == pytest.approx(0.155)
    assert result[0].baseline_rate == pytest.approx(0.09)


# ── get_active_experiments ────────────────────────────────────────────────────

def test_get_active_experiments_filters_running(tmp_path):
    tsv = tmp_path / "experiments.tsv"
    append_experiment(_make_exp(experiment_id="a", status="running"), tsv)
    append_experiment(_make_exp(experiment_id="b", status="won"), tsv)
    append_experiment(_make_exp(experiment_id="c", status="running"), tsv)
    result = get_active_experiments(tsv)
    assert len(result) == 2
    assert all(e.status == "running" for e in result)
    assert {e.experiment_id for e in result} == {"a", "c"}


def test_get_active_experiments_empty_when_none_running(tmp_path):
    tsv = tmp_path / "experiments.tsv"
    append_experiment(_make_exp(status="won"), tsv)
    assert get_active_experiments(tsv) == []


# ── get_baseline_rate ─────────────────────────────────────────────────────────

def test_get_baseline_rate_returns_zero_for_empty(tmp_path):
    tsv = tmp_path / "experiments.tsv"
    tsv.write_text(
        "experiment_id\tstarted\tcompleted\tcohort_size\tdm_response_rate"
        "\tbaseline_rate\tstatus\tvariable\tdescription\n"
    )
    assert get_baseline_rate(tsv) == pytest.approx(0.0)


def test_get_baseline_rate_uses_baseline_v0_fallback(tmp_path):
    tsv = tmp_path / "experiments.tsv"
    append_experiment(
        _make_exp(experiment_id="baseline-v0", status="running", dm_response_rate=0.08),
        tsv,
    )
    assert get_baseline_rate(tsv) == pytest.approx(0.08)


def test_get_baseline_rate_prefers_most_recent_won(tmp_path):
    tsv = tmp_path / "experiments.tsv"
    append_experiment(
        _make_exp(experiment_id="baseline-v0", status="won", dm_response_rate=0.08),
        tsv,
    )
    append_experiment(
        _make_exp(experiment_id="exp-001", status="won", dm_response_rate=0.15),
        tsv,
    )
    assert get_baseline_rate(tsv) == pytest.approx(0.15)


def test_get_baseline_rate_missing_file(tmp_path):
    assert get_baseline_rate(tmp_path / "nonexistent.tsv") == pytest.approx(0.0)


# ── get_baseline_rate: 0.0-with-populated-steps guard ────────────────────────
# (fix/audit-learning-loop-stats)


def test_get_baseline_rate_refuses_zero_when_steps_populated(tmp_path, capsys):
    """The real baseline-v0 bug: dm_response_rate column is 0.0 but per-step
    columns are populated. get_baseline_rate must NOT return 0.0 — it must
    recover a DM1-based aggregate so non-step experiments aren't graded vs
    0.0 + 2pp.
    """
    tsv = tmp_path / "experiments.tsv"
    append_experiment(
        _make_exp(
            experiment_id="baseline-v0",
            status="won",
            dm_response_rate=0.0,  # the bug: overall column empty/zero
            dm1_response_rate=0.0425531914893617,  # real per-step data present
            dm2_response_rate=0.024390243902439025,
            dm3_response_rate=0.21052631578947367,
        ),
        tsv,
    )
    rate = get_baseline_rate(tsv)
    # DM1-based aggregate, NOT 0.0.
    assert rate == pytest.approx(0.0425531914893617)
    assert rate > 0.0
    # The recovery is surfaced to the operator.
    assert "dm_response_rate=0.0" in capsys.readouterr().err


def test_get_baseline_rate_keeps_zero_when_no_steps_populated(tmp_path):
    """A genuine 0.0 with NO per-step data is left as 0.0 (not a read bug)."""
    tsv = tmp_path / "experiments.tsv"
    append_experiment(
        _make_exp(
            experiment_id="baseline-v0",
            status="won",
            dm_response_rate=0.0,
            dm1_response_rate=None,
            dm2_response_rate=None,
            dm3_response_rate=None,
        ),
        tsv,
    )
    assert get_baseline_rate(tsv) == pytest.approx(0.0)


def test_get_baseline_rate_nonzero_overall_unchanged(tmp_path):
    """A populated overall column is returned as-is (guard only fires on 0.0)."""
    tsv = tmp_path / "experiments.tsv"
    append_experiment(
        _make_exp(
            experiment_id="baseline-v0",
            status="won",
            dm_response_rate=0.11,
            dm1_response_rate=0.04,
        ),
        tsv,
    )
    assert get_baseline_rate(tsv) == pytest.approx(0.11)


def test_get_baseline_rate_warns_when_won_source_below_maturity_floor(tmp_path, capsys):
    """A 'won' baseline source with cohort_size < the maturity floor may have
    been prior-dominated; get_baseline_rate must warn so the operator doesn't
    chain a fragile baseline forward unnoticed.
    """
    tsv = tmp_path / "experiments.tsv"
    append_experiment(
        _make_exp(
            experiment_id="exp-thin-win",
            status="won",
            cohort_size=8,  # < maturity floor (15)
            dm_response_rate=0.18,
        ),
        tsv,
    )
    get_baseline_rate(tsv)
    err = capsys.readouterr().err
    assert "prior-dominated" in err
    assert "exp-thin-win" in err


def test_get_baseline_rate_no_warn_when_won_source_mature(tmp_path, capsys):
    """A 'won' source at/above the maturity floor emits no prior-dominated warning."""
    tsv = tmp_path / "experiments.tsv"
    append_experiment(
        _make_exp(
            experiment_id="exp-fat-win",
            status="won",
            cohort_size=40,  # >= maturity floor
            dm_response_rate=0.18,
        ),
        tsv,
    )
    get_baseline_rate(tsv)
    assert "prior-dominated" not in capsys.readouterr().err


# ── get_current_experiment_id ─────────────────────────────────────────────────

def test_get_current_experiment_id_returns_single_running(tmp_path):
    """Exactly one running experiment → returns its experiment_id."""
    tsv = tmp_path / "experiments.tsv"
    append_experiment(_make_exp(experiment_id="exp-001", status="running"), tsv)
    assert get_current_experiment_id(tsv) == "exp-001"


def test_get_current_experiment_id_multiple_running_raises(tmp_path):
    """Two or more running experiments → raises MultipleRunningExperimentsError
    (§0 invariant #9: no silent fallback to 'first' entry)."""
    tsv = tmp_path / "experiments.tsv"
    append_experiment(_make_exp(experiment_id="exp-001", status="running"), tsv)
    append_experiment(_make_exp(experiment_id="exp-002", status="running"), tsv)
    with pytest.raises(MultipleRunningExperimentsError) as exc_info:
        get_current_experiment_id(tsv)
    # Error message must include all running experiment_ids for operator triage.
    msg = str(exc_info.value)
    assert "exp-001" in msg
    assert "exp-002" in msg


def test_get_current_experiment_id_returns_none_when_no_running(tmp_path):
    """Zero running experiments → returns None (§0 invariant #9: explicit
    'no current experiment' signal, NOT 'baseline-v0' fallback)."""
    tsv = tmp_path / "experiments.tsv"
    append_experiment(_make_exp(experiment_id="exp-001", status="won"), tsv)
    assert get_current_experiment_id(tsv) is None


def test_get_current_experiment_id_empty_file(tmp_path):
    """Empty TSV → returns None (no running experiments)."""
    tsv = tmp_path / "experiments.tsv"
    tsv.write_text(
        "experiment_id\tstarted\tcompleted\tcohort_size\tdm_response_rate"
        "\tbaseline_rate\tstatus\tvariable\tdescription\n"
    )
    assert get_current_experiment_id(tsv) is None


# ── deduplication (append-only TSV) ──────────────────────────────────────────

def test_get_active_experiments_deduplicates_after_verdict(tmp_path):
    """When a verdict row is appended, the experiment should no longer be 'running'."""
    tsv = tmp_path / "experiments.tsv"
    # Original running row
    append_experiment(_make_exp(experiment_id="exp-001", status="running"), tsv)
    # Verdict row appended later
    append_experiment(_make_exp(experiment_id="exp-001", status="won"), tsv)
    active = get_active_experiments(tsv)
    assert len(active) == 0


def test_get_current_experiment_id_skips_completed_experiments(tmp_path):
    """After a verdict, completed experiment should not be returned as current."""
    tsv = tmp_path / "experiments.tsv"
    append_experiment(_make_exp(experiment_id="exp-001", status="running"), tsv)
    append_experiment(_make_exp(experiment_id="exp-001", status="won"), tsv)
    append_experiment(_make_exp(experiment_id="exp-002", status="running"), tsv)
    assert get_current_experiment_id(tsv) == "exp-002"


# ── per-step schema: load + write ─────────────────────────────────────────────

LEGACY_HEADER = (
    "experiment_id\tstarted\tcompleted\tcohort_size\tdm_response_rate"
    "\tbaseline_rate\tstatus\tvariable\tdescription\n"
)
NEW_HEADER = (
    "experiment_id\tstarted\tcompleted\tcohort_size\tdm_response_rate"
    "\tdm1_response_rate\tdm2_response_rate\tdm3_response_rate"
    "\tbaseline_rate\tstatus\tvariable\tdescription\n"
)


def test_load_experiments_legacy_row_returns_none_for_per_step(tmp_path):
    """A TSV that predates the per-step columns must parse cleanly with None fields."""
    tsv = tmp_path / "experiments.tsv"
    tsv.write_text(
        LEGACY_HEADER
        + "baseline-v0\t2026-01-01\t2026-02-01\t100\t0.08\t0.0\twon\tnone\thistorical\n"
    )
    result = load_experiments(tsv)
    assert len(result) == 1
    r = result[0]
    assert r.experiment_id == "baseline-v0"
    assert r.dm_response_rate == pytest.approx(0.08)
    assert r.dm1_response_rate is None
    assert r.dm2_response_rate is None
    assert r.dm3_response_rate is None


def test_load_experiments_empty_per_step_cells_parse_as_none(tmp_path):
    """New schema with empty per-step cells (pre-backfill state) parses to None — not 0.0."""
    tsv = tmp_path / "experiments.tsv"
    tsv.write_text(
        NEW_HEADER
        + "baseline-v0\t2026-01-01\t2026-02-01\t100\t0.08\t\t\t\t0.0\twon\tnone\thistorical\n"
    )
    result = load_experiments(tsv)
    assert result[0].dm1_response_rate is None
    assert result[0].dm2_response_rate is None
    assert result[0].dm3_response_rate is None


def test_load_experiments_with_per_step_rates_parses_floats(tmp_path):
    tsv = tmp_path / "experiments.tsv"
    tsv.write_text(
        NEW_HEADER
        + "baseline-v0\t2026-01-01\t2026-02-01\t100\t0.143\t0.041\t0.022\t0.286\t0.0\twon\tnone\thistorical\n"
    )
    result = load_experiments(tsv)
    r = result[0]
    assert r.dm1_response_rate == pytest.approx(0.041)
    assert r.dm2_response_rate == pytest.approx(0.022)
    assert r.dm3_response_rate == pytest.approx(0.286)


def test_append_then_load_round_trip_preserves_per_step(tmp_path):
    """Round-trip: write Experiment with per-step rates, read it back identical."""
    tsv = tmp_path / "experiments.tsv"
    exp = _make_exp(
        experiment_id="exp-round-trip",
        dm1_response_rate=0.05,
        dm2_response_rate=0.025,
        dm3_response_rate=0.30,
    )
    append_experiment(exp, tsv)
    loaded = load_experiments(tsv)
    assert len(loaded) == 1
    r = loaded[0]
    assert r.dm1_response_rate == pytest.approx(0.05)
    assert r.dm2_response_rate == pytest.approx(0.025)
    assert r.dm3_response_rate == pytest.approx(0.30)


def test_append_serializes_none_per_step_as_empty_string(tmp_path):
    """An Experiment with default-None per-step fields writes empty cells, parses back as None."""
    tsv = tmp_path / "experiments.tsv"
    exp = _make_exp(experiment_id="exp-none-fields")  # defaults to None for per-step
    append_experiment(exp, tsv)
    raw = tsv.read_text()
    # Three consecutive tab-separated empty fields between dm_response_rate and baseline_rate.
    assert "\t\t\t\t" in raw
    loaded = load_experiments(tsv)
    assert loaded[0].dm1_response_rate is None
    assert loaded[0].dm2_response_rate is None
    assert loaded[0].dm3_response_rate is None


# ── get_baseline_step_rate ────────────────────────────────────────────────────

def test_get_baseline_step_rate_returns_baseline_v0_per_step(tmp_path):
    tsv = tmp_path / "experiments.tsv"
    exp = _make_exp(
        experiment_id="baseline-v0",
        status="won",
        variable="none",
        dm_response_rate=0.10,
        dm1_response_rate=0.04,
        dm2_response_rate=0.025,
        dm3_response_rate=0.30,
    )
    append_experiment(exp, tsv)
    assert get_baseline_step_rate("dm1", tsv) == pytest.approx(0.04)
    assert get_baseline_step_rate("dm2", tsv) == pytest.approx(0.025)
    assert get_baseline_step_rate("dm3", tsv) == pytest.approx(0.30)


def test_get_baseline_step_rate_prefers_won_with_matching_step(tmp_path):
    """A more recent won experiment that targets dm1 should override baseline-v0."""
    tsv = tmp_path / "experiments.tsv"
    append_experiment(
        _make_exp(
            experiment_id="baseline-v0", status="won", variable="none",
            dm_response_rate=0.10,
            dm1_response_rate=0.04, dm2_response_rate=0.02, dm3_response_rate=0.20,
        ),
        tsv,
    )
    append_experiment(
        _make_exp(
            experiment_id="exp-dm1-winner", status="won",
            variable="messages.json:dm1:operations_leaders:es",
            dm_response_rate=0.12,
            dm1_response_rate=0.075,
        ),
        tsv,
    )
    assert get_baseline_step_rate("dm1", tsv) == pytest.approx(0.075)
    # DM2 baseline still comes from baseline-v0 (the dm1-winner doesn't touch dm2)
    assert get_baseline_step_rate("dm2", tsv) == pytest.approx(0.02)


def test_get_baseline_step_rate_falls_back_to_overall_when_per_step_missing(tmp_path):
    """Pre-backfill: baseline-v0 has no per-step rates → fall back to overall baseline."""
    tsv = tmp_path / "experiments.tsv"
    tsv.write_text(
        LEGACY_HEADER
        + "baseline-v0\t2026-01-01\t2026-02-01\t100\t0.143\t0.0\twon\tnone\thistorical\n"
    )
    # No per-step in the legacy row, no won-for-step row → falls back to overall (0.143).
    assert get_baseline_step_rate("dm1", tsv) == pytest.approx(0.143)
    assert get_baseline_step_rate("dm2", tsv) == pytest.approx(0.143)
    assert get_baseline_step_rate("dm3", tsv) == pytest.approx(0.143)


def test_get_baseline_step_rate_unknown_step_returns_overall(tmp_path):
    """Calling with an unknown step (e.g. 'connection_note') falls back to overall baseline."""
    tsv = tmp_path / "experiments.tsv"
    append_experiment(
        _make_exp(
            experiment_id="baseline-v0", status="won", variable="none",
            dm_response_rate=0.10,
            dm1_response_rate=0.04,
        ),
        tsv,
    )
    assert get_baseline_step_rate("connection_note", tsv) == pytest.approx(0.10)


def test_get_baseline_step_rate_missing_file(tmp_path):
    assert get_baseline_step_rate("dm1", tmp_path / "nonexistent.tsv") == pytest.approx(0.0)



# ── Wave-2-A: TSV-primary read (no Attio, no WARN) ────────────────────────────


def test_load_experiments_default_reads_tsv_no_warn(tmp_path, monkeypatch, capsys):
    """Default read path (no `path`, no `attio`) reads experiments.tsv directly
    with NO Attio probe and NO per-read WARN. The Attio experiment object was
    descoped (Wave-2-A) — the WARN must STOP firing for the expected case."""
    tsv = tmp_path / "experiments.tsv"
    append_experiment(_make_exp(experiment_id="exp-tsv-only", status="running"), tsv)
    monkeypatch.setattr("models.experiment.DEFAULT_TSV_PATH", tsv)

    # No AttioClient construction allowed — if the read path tried to build
    # one, this import-time stub would make it explode. It must NOT be touched.
    import clients.attio as attio_mod

    def _boom(*a, **k):  # pragma: no cover — asserts it's never called
        raise AssertionError("load_experiments must NOT construct an AttioClient")

    monkeypatch.setattr(attio_mod, "AttioClient", _boom)

    result = load_experiments()
    assert [e.experiment_id for e in result] == ["exp-tsv-only"]

    captured = capsys.readouterr()
    assert "WARN" not in captured.err
    assert "Attio" not in captured.err


def test_load_experiments_attio_kwarg_is_ignored(tmp_path, monkeypatch):
    """The `attio` kwarg is retained for signature compatibility but is a
    no-op — the read always comes from the TSV, the mock is never touched."""
    tsv = tmp_path / "experiments.tsv"
    append_experiment(_make_exp(experiment_id="exp-from-tsv"), tsv)
    monkeypatch.setattr("models.experiment.DEFAULT_TSV_PATH", tsv)

    attio = MagicMock()
    result = load_experiments(attio=attio)
    assert [e.experiment_id for e in result] == ["exp-from-tsv"]
    attio.assert_not_called()
    assert not attio.method_calls


def test_load_experiments_missing_tsv_returns_empty_no_warn(
    tmp_path, monkeypatch, capsys
):
    """A missing TSV on the default path returns [] with no alarm."""
    monkeypatch.setattr(
        "models.experiment.DEFAULT_TSV_PATH", tmp_path / "does-not-exist.tsv"
    )
    assert load_experiments() == []
    captured = capsys.readouterr()
    assert "WARN" not in captured.err


# ── Wave-2-A: upsert_experiment (in-place verdict persistence) ────────────────


def test_upsert_experiment_appends_when_new(tmp_path):
    tsv = tmp_path / "experiments.tsv"
    append_experiment(_make_exp(experiment_id="exp-a", status="running"), tsv)

    was_update = upsert_experiment(_make_exp(experiment_id="exp-b", status="won"), tsv)
    assert was_update is False

    rows = load_experiments(tsv)
    assert [e.experiment_id for e in rows] == ["exp-a", "exp-b"]


def test_upsert_experiment_updates_in_place_no_duplicate(tmp_path):
    """Upserting an existing id replaces the row — no duplicate appended."""
    tsv = tmp_path / "experiments.tsv"
    append_experiment(_make_exp(experiment_id="exp-a", status="running"), tsv)
    append_experiment(_make_exp(experiment_id="exp-b", status="running"), tsv)

    was_update = upsert_experiment(_make_exp(experiment_id="exp-a", status="won"), tsv)
    assert was_update is True

    rows = load_experiments(tsv)
    # Still exactly two rows, exp-a's status flipped.
    assert [e.experiment_id for e in rows] == ["exp-a", "exp-b"]
    by_id = {e.experiment_id: e for e in rows}
    assert by_id["exp-a"].status == ExperimentStatus.WON
    assert by_id["exp-b"].status == ExperimentStatus.RUNNING


def test_upsert_experiment_replaces_last_matching_row(tmp_path):
    """When an id appears twice (append-only verdict history), upsert replaces
    the LAST matching row — consistent with read-side last-row-wins dedup."""
    tsv = tmp_path / "experiments.tsv"
    append_experiment(_make_exp(experiment_id="exp-a", status="running"), tsv)
    append_experiment(_make_exp(experiment_id="exp-a", status="rejected"), tsv)

    upsert_experiment(_make_exp(experiment_id="exp-a", status="won"), tsv)

    rows = load_experiments(tsv)
    assert [e.experiment_id for e in rows] == ["exp-a", "exp-a"]
    # First row preserved as 'running'; last row updated to 'won'.
    assert rows[0].status == ExperimentStatus.RUNNING
    assert rows[1].status == ExperimentStatus.WON
