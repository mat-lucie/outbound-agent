"""Tests for workflows/learn.py."""

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

from models.experiment import Experiment, ExperimentStatus, append_experiment
from workflows.learn import (
    _parse_step_from_variable,
    evaluate_experiments,
    measure_cohorts,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_entry(
    stage: str,
    experiment_id: str = "exp-001",
    dm_step: int | None = None,
    dm1_sent_at: str | None = "2026-05-01T12:00:00",
    dm2_sent_at: str | None = "2026-05-03T12:00:00",
    dm3_sent_at: str | None = "2026-05-06T12:00:00",
    experiment_id_frozen_at: str | None = "prospect",
) -> dict:
    """Return a parsed Attio entry dict.

    PR-10: dm{N}_sent_at and experiment_id_frozen_at fields added with
    eligibility defaults so existing tests continue exercising the per-step
    math path. Tests specifically checking exclusion behaviour set these to
    None / 'legacy_pure_unknown' explicitly.
    """
    return {
        "entry_id": "eid-001",
        "record_id": "rid-001",
        "stage": stage,
        "persona": None,
        "language": None,
        "dm_step": dm_step,
        "quality_score": None,
        "last_contact_date": None,
        "experiment_id": experiment_id,
        "dm1_sent_at": dm1_sent_at,
        "dm2_sent_at": dm2_sent_at,
        "dm3_sent_at": dm3_sent_at,
        "experiment_id_frozen_at": experiment_id_frozen_at,
    }


def _make_attio_mock(entries: list[dict]) -> MagicMock:
    """Build a mock AttioClient that returns pre-parsed entries from parse_entry."""
    attio = MagicMock()
    # query_list_entries returns raw entries; parse_entry returns the same dicts here
    # since we're already using parsed format, we set up parse_entry to be a pass-through
    raw = [{"_raw": e} for e in entries]
    attio.query_list_entries.return_value = raw
    attio.parse_entry.side_effect = lambda e: e["_raw"]
    return attio


def _make_experiment(exp_id: str, status: str, started: str, dm_response_rate: float = 0.08) -> Experiment:
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


# ── measure_cohorts ───────────────────────────────────────────────────────────

def test_measure_cohorts_empty_entries():
    attio = _make_attio_mock([])
    with patch("workflows.learn.load_experiments", return_value=[]):
        results = measure_cohorts(attio)
    assert results == []


def test_measure_cohorts_groups_by_experiment_id():
    entries = [
        _make_entry("DM1 Sent", "exp-001"),
        _make_entry("DM1 Sent", "exp-001"),
        _make_entry("DM1 Sent", "exp-002"),
    ]
    attio = _make_attio_mock(entries)
    with patch("workflows.learn.load_experiments", return_value=[]):
        results = measure_cohorts(attio)

    by_id = {r["experiment_id"]: r for r in results}
    assert set(by_id.keys()) == {"exp-001", "exp-002"}
    assert by_id["exp-001"]["cohort_size"] == 2
    assert by_id["exp-002"]["cohort_size"] == 1


def test_measure_cohorts_counts_dmed_correctly():
    """DM'd = DM1+, Responded, Call Booked, Qualified, Not Interested. Not Prospect/Connection Sent/Accepted."""
    entries = [
        _make_entry("Prospect", "exp-001"),
        _make_entry("Connection Sent", "exp-001"),
        _make_entry("Accepted", "exp-001"),
        _make_entry("DM1 Sent", "exp-001"),
        _make_entry("DM2 Sent", "exp-001"),
        _make_entry("DM3 Sent", "exp-001"),
        _make_entry("Responded", "exp-001"),
        _make_entry("Call Booked", "exp-001"),
        _make_entry("Qualified", "exp-001"),
        _make_entry("Not Interested", "exp-001"),
    ]
    attio = _make_attio_mock(entries)
    with patch("workflows.learn.load_experiments", return_value=[]):
        results = measure_cohorts(attio)

    r = results[0]
    assert r["dmed"] == 7  # DM1, DM2, DM3, Responded, Call Booked, Qualified, Not Interested
    assert r["responded"] == 3  # Responded, Call Booked, Qualified


def test_measure_cohorts_dm_response_rate():
    entries = [
        _make_entry("DM1 Sent", "exp-001"),  # dmed, not responded
        _make_entry("DM2 Sent", "exp-001"),  # dmed, not responded
        _make_entry("Responded", "exp-001"),  # dmed + responded
        _make_entry("Responded", "exp-001"),  # dmed + responded
    ]
    attio = _make_attio_mock(entries)
    with patch("workflows.learn.load_experiments", return_value=[]):
        results = measure_cohorts(attio)

    r = results[0]
    assert r["dmed"] == 4
    assert r["responded"] == 2
    assert r["dm_response_rate"] == pytest.approx(0.5)


def test_measure_cohorts_zero_dmed_gives_zero_rate():
    entries = [
        _make_entry("Prospect", "exp-001"),
        _make_entry("Connection Sent", "exp-001"),
    ]
    attio = _make_attio_mock(entries)
    with patch("workflows.learn.load_experiments", return_value=[]):
        results = measure_cohorts(attio)

    r = results[0]
    assert r["dmed"] == 0
    assert r["dm_response_rate"] == pytest.approx(0.0)


def test_measure_cohorts_unreachable_dmed_counts_in_denominator_not_numerator():
    """Wave-2-A: an UNREACHABLE row that received DMs (dm_step>=1) before being
    parked stays in the DM'd denominator but is NOT a response.

    Regression guard: omitting UNREACHABLE dropped delivered-to non-responders
    from the denominator, which inflated dm_response_rate (and the per-step
    denominators). The row must sit in the DM1+DM2 denominators (dm_step=2),
    never DM3, and never as a reply.
    """
    entries = [
        _make_entry("Responded", "exp-001", dm_step=2),    # dmed + responded@2
        _make_entry("Unreachable", "exp-001", dm_step=2),  # dmed, NOT a response
    ]
    attio = _make_attio_mock(entries)
    with patch("workflows.learn.load_experiments", return_value=[]):
        results = measure_cohorts(attio)

    r = results[0]
    assert r["dmed"] == 2  # both genuinely received >=1 DM
    assert r["responded"] == 1  # UNREACHABLE is an undeliverability terminal, not a reply
    assert r["dm_response_rate"] == pytest.approx(0.5)
    # Per-step: dm_step=2 row sits in DM1 + DM2 denominators, not DM3.
    assert r["dm1_received"] == 2
    assert r["dm2_received"] == 2
    assert r["dm3_received"] == 0
    assert r["dm1_replies"] == 0
    assert r["dm2_replies"] == 1  # the Responded row replied at exactly step 2
    assert r["dm3_replies"] == 0


def test_measure_cohorts_unreachable_dm_step_zero_or_none_excluded_from_dmed():
    """An UNREACHABLE row parked before DM1 was ever delivered (dm_step=0 — e.g.
    InMail-required on the first DM, or an Out-of-Network invite that was never
    accepted) never received a DM and must stay OUT of the DM'd denominator.
    dm_step=None (unknown step) is excluded for the same reason.
    """
    entries = [
        _make_entry("DM1 Sent", "exp-001", dm_step=1),         # genuinely dmed
        _make_entry("Unreachable", "exp-001", dm_step=0),      # never delivered
        _make_entry("Unreachable", "exp-001", dm_step=None),   # unknown step
    ]
    attio = _make_attio_mock(entries)
    with patch("workflows.learn.load_experiments", return_value=[]):
        results = measure_cohorts(attio)

    r = results[0]
    assert r["dmed"] == 1  # only the DM1 Sent row
    assert r["responded"] == 0


def test_measure_cohorts_handles_slug_dm_step_without_crashing():
    """Production dm_step is a select-type slug ("dm1".."dm3" / "invite" /
    "connection_note"), NOT an int — an OON park keeps whatever slug the row
    carried. The cohort math must coerce it, never raise, and bucket UNREACHABLE
    rows by their real DM step. Regression guard for the `slug >= n` TypeError.
    """
    entries = [
        _make_entry("DM1 Sent", "exp-001", dm_step="dm1"),       # dmed, no reply
        _make_entry("DM2 Sent", "exp-001", dm_step="dm2"),       # dmed, no reply
        _make_entry("Responded", "exp-001", dm_step="dm2"),      # reply @ step 2
        _make_entry("Unreachable", "exp-001", dm_step="dm2"),    # dmed via DM2, not a reply
        _make_entry("Unreachable", "exp-001", dm_step="invite"), # never DM'd → excluded
    ]
    attio = _make_attio_mock(entries)
    with patch("workflows.learn.load_experiments", return_value=[]):
        results = measure_cohorts(attio)

    r = results[0]
    assert r["dmed"] == 4  # DM1, DM2, Responded, Unreachable(dm2); "invite" excluded
    assert r["responded"] == 1
    assert r["dm1_received"] == 4  # all four dmed rows resolve to dm_step>=1
    assert r["dm2_received"] == 3  # the three dm2 rows
    assert r["dm3_received"] == 0
    assert r["dm2_replies"] == 1  # Responded@dm2 — slug `== n` matches only via coercion


def test_measure_cohorts_mature_when_enough_dmed_and_old_enough():
    # fix/audit-learning-loop-stats: maturity now ALSO requires cadence
    # completeness (>= 16 days running OR every member cadence-settled). These
    # DM1-Sent entries are NOT settled (dm_step default None), so the cohort
    # must be >= 16 days old to mature. 20 days clears all three gates:
    # dmed (20 >= 15), min-days (20 >= 7), cadence (20 >= 16).
    today = date.today()
    started = (today - timedelta(days=20)).isoformat()

    entries = [_make_entry("DM1 Sent", "exp-001") for _ in range(20)]
    attio = _make_attio_mock(entries)
    experiments = [_make_experiment("exp-001", "running", started)]

    with patch("workflows.learn.load_experiments", return_value=experiments):
        results = measure_cohorts(attio)

    r = results[0]
    assert r["is_mature"] is True
    assert r["days_running"] == 20


def test_measure_cohorts_immature_when_mid_cadence_despite_dmed_floor():
    """fix/audit-learning-loop-stats: a cohort past the DM'd + min-days floors
    but still mid-cadence (< 16 days, members not settled) is NOT mature.

    This is the bug the cadence gate closes: the OLD gate would have frozen a
    verdict at day 7-10, before DM3 (~day 16) landed.
    """
    today = date.today()
    started = (today - timedelta(days=10)).isoformat()  # past 7d floor, < 16d cadence

    # 20 DM'd, all still at DM1 (dm_step None / not settled) → mid-cadence.
    entries = [_make_entry("DM1 Sent", "exp-001") for _ in range(20)]
    attio = _make_attio_mock(entries)
    experiments = [_make_experiment("exp-001", "running", started)]

    with patch("workflows.learn.load_experiments", return_value=experiments):
        results = measure_cohorts(attio)

    assert results[0]["is_mature"] is False


def test_measure_cohorts_mature_when_all_members_cadence_settled():
    """A young cohort (< 16 days) IS mature if every DM'd member is settled —
    reached DM3 or hit a terminal/responded stage — so no member awaits a DM3.
    """
    today = date.today()
    started = (today - timedelta(days=8)).isoformat()  # < 16 days

    # All 20 members at dm_step=3 (reached the end of the cadence).
    entries = [_make_entry("DM3 Sent", "exp-001", dm_step=3) for _ in range(20)]
    attio = _make_attio_mock(entries)
    experiments = [_make_experiment("exp-001", "running", started)]

    with patch("workflows.learn.load_experiments", return_value=experiments):
        results = measure_cohorts(attio)

    assert results[0]["is_mature"] is True


def test_measure_cohorts_immature_too_few_dmed():
    today = date.today()
    started = (today - timedelta(days=10)).isoformat()

    entries = [_make_entry("DM1 Sent", "exp-001") for _ in range(10)]  # only 10, need 15
    attio = _make_attio_mock(entries)
    experiments = [_make_experiment("exp-001", "running", started)]

    with patch("workflows.learn.load_experiments", return_value=experiments):
        results = measure_cohorts(attio)

    assert results[0]["is_mature"] is False


def test_measure_cohorts_immature_not_old_enough():
    today = date.today()
    started = (today - timedelta(days=3)).isoformat()  # only 3 days, need 7

    entries = [_make_entry("DM1 Sent", "exp-001") for _ in range(20)]
    attio = _make_attio_mock(entries)
    experiments = [_make_experiment("exp-001", "running", started)]

    with patch("workflows.learn.load_experiments", return_value=experiments):
        results = measure_cohorts(attio)

    assert results[0]["is_mature"] is False


def test_measure_cohorts_not_in_tsv_treated_as_mature():
    """Experiments not in TSV default to days_running=_CADENCE_COMPLETE_MIN_DAYS
    (mature if dmed >= 15). fix/audit-learning-loop-stats raised the historical
    default from the 7-day floor to the 16-day cadence span so the new
    cadence-completeness gate also passes by age for historical cohorts.
    """
    entries = [_make_entry("DM1 Sent", "exp-historical") for _ in range(20)]
    attio = _make_attio_mock(entries)

    with patch("workflows.learn.load_experiments", return_value=[]):
        results = measure_cohorts(attio)

    r = results[0]
    assert r["is_mature"] is True  # 20 dmed >= 15, days_running defaults to 16 (cadence span)


# ── evaluate_experiments ──────────────────────────────────────────────────────

def _mature_cohort(exp_id: str, dm_response_rate: float, dmed: int = 20) -> dict:
    responded = round(dm_response_rate * dmed)
    return {
        "experiment_id": exp_id,
        "cohort_size": 30,
        "dmed": dmed,
        "responded": responded,
        "dm_response_rate": dm_response_rate,
        "is_mature": True,
        "days_running": 10,
    }


def _immature_cohort(exp_id: str) -> dict:
    return {
        "experiment_id": exp_id,
        "cohort_size": 5,
        "dmed": 5,
        "responded": 0,
        "dm_response_rate": 0.0,
        "is_mature": False,
        "days_running": 2,
    }


def test_evaluate_experiments_won_verdict(tmp_path):
    tsv = tmp_path / "experiments.tsv"
    append_experiment(_make_experiment("baseline-v0", "won", "2026-01-01", dm_response_rate=0.08), tsv)
    append_experiment(_make_experiment("exp-001", "running", "2026-04-01"), tsv)

    cohorts = [_mature_cohort("exp-001", dm_response_rate=0.12)]  # 0.12 > 0.08 + 0.02
    results = evaluate_experiments(cohorts, experiments_path=tsv)

    assert len(results) == 1
    assert results[0]["verdict"] == "won"
    assert results[0]["experiment_id"] == "exp-001"


def test_evaluate_experiments_lost_verdict(tmp_path):
    tsv = tmp_path / "experiments.tsv"
    append_experiment(_make_experiment("baseline-v0", "won", "2026-01-01", dm_response_rate=0.08), tsv)
    append_experiment(_make_experiment("exp-001", "running", "2026-04-01"), tsv)

    cohorts = [_mature_cohort("exp-001", dm_response_rate=0.04)]  # 0.04 < 0.08 - 0.02
    results = evaluate_experiments(cohorts, experiments_path=tsv)

    assert len(results) == 1
    assert results[0]["verdict"] == "lost"


def test_evaluate_experiments_inconclusive_verdict(tmp_path):
    tsv = tmp_path / "experiments.tsv"
    append_experiment(_make_experiment("baseline-v0", "won", "2026-01-01", dm_response_rate=0.08), tsv)
    append_experiment(_make_experiment("exp-001", "running", "2026-04-01"), tsv)

    # 0.085 is within ±0.02 of 0.08
    cohorts = [_mature_cohort("exp-001", dm_response_rate=0.085)]
    results = evaluate_experiments(cohorts, experiments_path=tsv)

    assert len(results) == 1
    assert results[0]["verdict"] == "inconclusive"


def test_evaluate_experiments_skips_non_running(tmp_path):
    tsv = tmp_path / "experiments.tsv"
    append_experiment(_make_experiment("exp-won", "won", "2026-01-01", dm_response_rate=0.15), tsv)
    append_experiment(_make_experiment("exp-lost", "lost", "2026-02-01", dm_response_rate=0.03), tsv)

    cohorts = [
        _mature_cohort("exp-won", dm_response_rate=0.15),
        _mature_cohort("exp-lost", dm_response_rate=0.03),
    ]
    results = evaluate_experiments(cohorts, experiments_path=tsv)

    assert results == []


def test_evaluate_experiments_no_mature_cohorts(tmp_path):
    tsv = tmp_path / "experiments.tsv"
    append_experiment(_make_experiment("exp-001", "running", "2026-04-01"), tsv)

    cohorts = [_immature_cohort("exp-001")]
    results = evaluate_experiments(cohorts, experiments_path=tsv)

    assert results == []


def test_evaluate_experiments_result_includes_rates(tmp_path):
    tsv = tmp_path / "experiments.tsv"
    append_experiment(_make_experiment("baseline-v0", "won", "2026-01-01", dm_response_rate=0.10), tsv)
    append_experiment(_make_experiment("exp-001", "running", "2026-04-01"), tsv)

    cohorts = [_mature_cohort("exp-001", dm_response_rate=0.15)]
    results = evaluate_experiments(cohorts, experiments_path=tsv)

    r = results[0]
    assert r["dm_response_rate"] == pytest.approx(0.15)
    assert r["baseline_rate"] == pytest.approx(0.10)
    assert r["cohort_size"] == 30


def test_evaluate_experiments_empty_tsv(tmp_path):
    """No experiments in TSV → no verdicts (baseline_rate=0.0, but no running exps)."""
    tsv = tmp_path / "experiments.tsv"
    tsv.write_text(
        "experiment_id\tstarted\tcompleted\tcohort_size\tdm_response_rate"
        "\tbaseline_rate\tstatus\tvariable\tdescription\n"
    )
    cohorts = [_mature_cohort("exp-001", dm_response_rate=0.12)]
    results = evaluate_experiments(cohorts, experiments_path=tsv)
    assert results == []


# ── per-step rates ────────────────────────────────────────────────────────────

def test_per_step_rates_mixed_dm_steps():
    """Hand-computed per-step rates against a known dm_step distribution.

    Cohort:
      - 3 entries at "DM1 Sent" with dm_step=1 (no reply yet)
      - 2 entries at "Responded" with dm_step=1 (replied after DM1)
      - 4 entries at "DM2 Sent" with dm_step=2 (no reply, mid-sequence)
      - 1 entry at "Responded" with dm_step=2 (replied after DM2)
      - 5 entries at "DM3 Sent" with dm_step=3 (no reply)
      - 1 entry at "Responded" with dm_step=3 (replied after DM3)

    dm{N}_response_rate is the Bayesian posterior mean, not the raw ratio.
    Reference values (recalibrated priors — fix/audit-learning-loop-stats):
      dm1: n_obs=16, n_suc=2, prior(1,22) → post(3,36) → mean=3/39≈0.07692
      dm2: n_obs=11, n_suc=1, prior(1,22) → post(2,32) → mean=2/34≈0.05882
      dm3: n_obs=6,  n_suc=1, prior(4,15) → post(5,20) → mean=5/25=0.20000
    """
    entries = []
    entries += [_make_entry("DM1 Sent", dm_step=1) for _ in range(3)]
    entries += [_make_entry("Responded", dm_step=1) for _ in range(2)]
    entries += [_make_entry("DM2 Sent", dm_step=2) for _ in range(4)]
    entries += [_make_entry("Responded", dm_step=2)]
    entries += [_make_entry("DM3 Sent", dm_step=3) for _ in range(5)]
    entries += [_make_entry("Responded", dm_step=3)]

    attio = _make_attio_mock(entries)
    with patch("workflows.learn.load_experiments", return_value=[]):
        results = measure_cohorts(attio)

    r = results[0]
    # Denominators (n_received) and numerators (n_replies) are raw counts, unchanged.
    # Denominators: dm1=16 (everyone), dm2=11 (dm_step>=2), dm3=6 (dm_step>=3)
    # Numerators:   dm1=2 (replied with dm_step==1), dm2=1, dm3=1
    assert r["dm1_received"] == 16
    assert r["dm1_replies"] == 2
    # dm1 posterior mean: alpha=1+2=3, beta=22+(16-2)=36 → 3/39 ≈ 0.07692
    assert r["dm1_response_rate"] == pytest.approx(3.0 / 39.0, rel=1e-5)

    assert r["dm2_received"] == 11
    assert r["dm2_replies"] == 1
    # dm2 posterior mean: (1+1)/(1+22+11) = 2/34 ≈ 0.05882
    assert r["dm2_response_rate"] == pytest.approx(2.0 / 34.0, rel=1e-5)

    assert r["dm3_received"] == 6
    assert r["dm3_replies"] == 1
    # dm3 posterior mean: (4+1)/(4+15+6) = 5/25 = 0.20
    assert r["dm3_response_rate"] == pytest.approx(5.0 / 25.0, rel=1e-5)


def test_per_step_rates_zero_received_collapses_to_prior():
    """Empty per-step denominator must collapse to the prior without ZeroDivisionError.

    The Bayesian posterior collapses to the prior when n_observed=0 — the
    correct behaviour is the prior mean, not zero. Recalibrated priors:
    dm2 prior=(1,22) → mean=1/23≈0.04348; dm3 prior=(4,15) → mean=4/19≈0.21053.
    """
    entries = [_make_entry("DM1 Sent", dm_step=1)]
    attio = _make_attio_mock(entries)
    with patch("workflows.learn.load_experiments", return_value=[]):
        results = measure_cohorts(attio)

    r = results[0]
    assert r["dm2_received"] == 0
    # Prior mean for dm2: 1.0 / (1.0 + 22.0) ≈ 0.04348
    assert r["dm2_response_rate"] == pytest.approx(1.0 / 23.0, rel=1e-5)
    assert r["dm3_received"] == 0
    # Prior mean for dm3: 4.0 / (4.0 + 15.0) ≈ 0.21053
    assert r["dm3_response_rate"] == pytest.approx(4.0 / 19.0, rel=1e-5)


def test_per_step_rates_skips_none_dm_step():
    """Entries with dm_step=None are excluded from per-step counts."""
    entries = [
        _make_entry("DM1 Sent", dm_step=None),  # excluded entirely
        _make_entry("Responded", dm_step=None),  # excluded entirely
        _make_entry("DM1 Sent", dm_step=1),
        _make_entry("Responded", dm_step=1),
    ]
    attio = _make_attio_mock(entries)
    with patch("workflows.learn.load_experiments", return_value=[]):
        results = measure_cohorts(attio)

    r = results[0]
    # Only 2 entries have non-null dm_step; both qualify for dm1_received
    assert r["dm1_received"] == 2
    assert r["dm1_replies"] == 1
    # Posterior mean, not raw ratio (recalibrated prior).
    # dm1: n_obs=2, n_suc=1, prior(1,22) → post(2,23) → mean=2/25=0.08
    assert r["dm1_response_rate"] == pytest.approx(2.0 / 25.0, rel=1e-5)


# ── _parse_step_from_variable ─────────────────────────────────────────────────

def test_parse_step_from_variable_recognized_steps():
    assert _parse_step_from_variable("messages.json:dm1:operations_leaders:es") == "dm1"
    assert _parse_step_from_variable("messages.json:dm2:digital:en") == "dm2"
    assert _parse_step_from_variable("messages.json:dm3:exec:pt") == "dm3"


def test_parse_step_from_variable_unrecognized_returns_none():
    assert _parse_step_from_variable("messages.json:connection_note:exec:es") is None
    assert _parse_step_from_variable("quality_gate.py:scoring_weights") is None
    assert _parse_step_from_variable("") is None
    assert _parse_step_from_variable("foo") is None
    assert _parse_step_from_variable("a:b") is None  # 'b' is not a recognized step


def test_evaluate_experiments_connection_note_routes_to_overall(tmp_path):
    """connection_note experiments use the overall rate per spec — they're
    upstream of all DM replies, so per-step attribution doesn't apply."""
    tsv = tmp_path / "experiments.tsv"
    append_experiment(
        _make_experiment("baseline-v0", "won", "2026-01-01", dm_response_rate=0.10),
        tsv,
    )
    append_experiment(
        _make_step_experiment(
            "exp-conn", "running", "2026-04-01",
            "messages.json:connection_note:operations_leaders:es",
        ),
        tsv,
    )

    cohorts = [_step_routed_cohort("exp-conn", dm1_rate=0.04, dm_response_rate=0.13)]
    results = evaluate_experiments(cohorts, experiments_path=tsv)

    assert len(results) == 1
    r = results[0]
    assert r["step"] == "overall"
    assert r["rate_metric"] == "dm_response_rate"
    assert r["verdict"] == "won"  # 0.13 > 0.10 + 0.02


# ── evaluate_experiments — step-aware verdict routing ────────────────────────

def _step_routed_cohort(exp_id: str, dm1_rate: float, dm_response_rate: float = 0.10) -> dict:
    """Cohort dict with hand-set per-step rates for verdict routing tests."""
    return {
        "experiment_id": exp_id,
        "cohort_size": 30,
        "dmed": 25,
        "responded": int(dm_response_rate * 25),
        "dm_response_rate": dm_response_rate,
        "is_mature": True,
        "days_running": 10,
        "dm1_response_rate": dm1_rate,
        "dm2_response_rate": 0.05,
        "dm3_response_rate": 0.10,
        "dm1_received": 25,
        "dm2_received": 15,
        "dm3_received": 8,
        "dm1_replies": int(dm1_rate * 25),
        "dm2_replies": 1,
        "dm3_replies": 1,
    }


def _make_step_experiment(exp_id: str, status: str, started: str, variable: str) -> Experiment:
    return Experiment(
        experiment_id=exp_id,
        started=started,
        completed="",
        cohort_size=50,
        dm_response_rate=0.10,
        baseline_rate=0.10,
        status=ExperimentStatus(status),
        variable=variable,
        description="test",
    )


def test_evaluate_experiments_routes_dm1_variable_to_per_step_rate(tmp_path):
    """A DM1-targeted experiment is judged against the DM1 baseline + DM1 cohort rate."""
    tsv = tmp_path / "experiments.tsv"
    # Baseline-v0 has overall=0.10 but per-step DM1=0.04 (lower).
    baseline = Experiment(
        experiment_id="baseline-v0",
        started="2026-01-01",
        completed="2026-01-01",
        cohort_size=50,
        dm_response_rate=0.10,
        baseline_rate=0.0,
        status=ExperimentStatus("won"),
        variable="none",
        description="baseline",
        dm1_response_rate=0.04,
        dm2_response_rate=0.05,
        dm3_response_rate=0.20,
    )
    append_experiment(baseline, tsv)
    append_experiment(
        _make_step_experiment("exp-dm1", "running", "2026-04-01",
                              "messages.json:dm1:operations_leaders:es"),
        tsv,
    )

    # Cohort DM1 rate 0.08 vs baseline DM1 rate 0.04 → +4pp → won.
    cohorts = [_step_routed_cohort("exp-dm1", dm1_rate=0.08)]
    results = evaluate_experiments(cohorts, experiments_path=tsv)

    assert len(results) == 1
    r = results[0]
    assert r["verdict"] == "won"
    assert r["step"] == "dm1"
    assert r["rate_metric"] == "dm1_response_rate"
    assert r["step_rate"] == pytest.approx(0.08)
    assert r["step_baseline_rate"] == pytest.approx(0.04)


def test_evaluate_experiments_non_step_variable_routes_to_overall(tmp_path):
    """Scoring experiments (variable doesn't target dm1/dm2/dm3) use the overall rate."""
    tsv = tmp_path / "experiments.tsv"
    append_experiment(
        _make_experiment("baseline-v0", "won", "2026-01-01", dm_response_rate=0.10),
        tsv,
    )
    append_experiment(
        _make_step_experiment("exp-scoring", "running", "2026-04-01",
                              "quality_gate.py:scoring_weights"),
        tsv,
    )

    # Cohort overall=0.15 vs baseline overall=0.10 → +5pp → won via overall.
    cohorts = [_step_routed_cohort("exp-scoring", dm1_rate=0.04, dm_response_rate=0.15)]
    results = evaluate_experiments(cohorts, experiments_path=tsv)

    assert len(results) == 1
    r = results[0]
    assert r["verdict"] == "won"
    assert r["step"] == "overall"
    assert r["rate_metric"] == "dm_response_rate"
    assert r["step_rate"] == pytest.approx(0.15)
    assert r["step_baseline_rate"] == pytest.approx(0.10)


def test_evaluate_experiments_malformed_variable_falls_back_to_overall(tmp_path):
    """Empty / single-segment / unknown-step variables must not raise."""
    tsv = tmp_path / "experiments.tsv"
    append_experiment(
        _make_experiment("baseline-v0", "won", "2026-01-01", dm_response_rate=0.10),
        tsv,
    )
    for bad_variable in ("", "foo", "messages.json:bogus:exec:es"):
        append_experiment(
            _make_step_experiment(
                f"exp-{abs(hash(bad_variable))}", "running", "2026-04-01", bad_variable),
            tsv,
        )

    cohorts = [
        _step_routed_cohort(f"exp-{abs(hash(b))}", dm1_rate=0.04, dm_response_rate=0.13)
        for b in ("", "foo", "messages.json:bogus:exec:es")
    ]
    results = evaluate_experiments(cohorts, experiments_path=tsv)
    assert len(results) == 3
    for r in results:
        assert r["step"] == "overall"
        assert r["verdict"] == "won"  # 0.13 > 0.10 + 0.02


def test_evaluate_experiments_preserves_overall_baseline_in_dict(tmp_path):
    """Verdict's `baseline_rate` key continues to mean overall baseline (TSV contract)."""
    tsv = tmp_path / "experiments.tsv"
    append_experiment(
        Experiment(
            experiment_id="baseline-v0", started="2026-01-01", completed="2026-01-01",
            cohort_size=50, dm_response_rate=0.10, baseline_rate=0.0,
            status=ExperimentStatus("won"), variable="none", description="baseline",
            dm1_response_rate=0.04, dm2_response_rate=0.05, dm3_response_rate=0.20,
        ),
        tsv,
    )
    append_experiment(
        _make_step_experiment("exp-dm1", "running", "2026-04-01",
                              "messages.json:dm1:operations_leaders:es"),
        tsv,
    )

    cohorts = [_step_routed_cohort("exp-dm1", dm1_rate=0.08)]
    results = evaluate_experiments(cohorts, experiments_path=tsv)

    r = results[0]
    # The verdict's `baseline_rate` is overall (0.10), independent of the routed step's baseline.
    assert r["baseline_rate"] == pytest.approx(0.10)
    # `step_baseline_rate` carries the routed baseline (DM1 = 0.04).
    assert r["step_baseline_rate"] == pytest.approx(0.04)


class TestMeasureCohortsSoftDeleteFilter:
    """§3.11 soft-delete filter — `measure_cohorts` must drop union-merge
    losers so dmed/responded denominators aren't double-counted against
    the same underlying prospect. The winner already inherits the cadence
    + response_classification math via the union-merge delta.
    """

    def test_merged_into_losers_excluded_from_cohort_math(self):
        # Winner and loser both carry `exp-soft` cohort identity + DM1
        # state. Without the filter, dmed would count 2; with it, only 1
        # (the winner) — the loser is soft-deleted and must not inflate
        # the denominator.
        winner = _make_entry("DM1 Sent", "exp-soft", dm_step=1)
        loser = _make_entry("DM1 Sent", "exp-soft", dm_step=1)
        loser["merged_into"] = {"target_object": "people",
                                "target_record_id": "rid-winner"}
        attio = _make_attio_mock([winner, loser])
        out = measure_cohorts(attio)
        # One row for `exp-soft` (winner only); loser was filtered.
        match = [r for r in out if r["experiment_id"] == "exp-soft"]
        assert len(match) == 1
        assert match[0]["dmed"] == 1, (
            "Soft-deleted loser must NOT inflate dmed denominator"
        )
