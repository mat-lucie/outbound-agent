"""PR-10: Bayesian per-step rates + BayesianPosterior contract + diacritic fix.

Tests cover:
1. BayesianPosterior TypedDict — field presence and numeric correctness
2. _per_step_rates — exact posterior values for worked examples
3. Denominator exclusion fixtures — dual-condition logic
4. _normalize_name diacritic fix — José, Müller, Łukasz, Ñoño
5. evaluate_experiments interface preservation
6. Soft-delete filter still present after rewrite (§3.11 regression guard)
7. _compute_posterior defensive validation (silent-failure CRIT-1)
8. prior_dominated verdict flag (silent-failure CRIT-2 + §0 #9)
9. Pre-migration row warning surfaced (silent-failure CRIT-3)
10. _normalize_name strips zero-width joiner (silent-failure HIGH-5)

All numeric assertions use exact reference values computed offline from
scipy.stats.beta with the priors declared in models/bayesian.py:
  dm1: prior (2.0, 20.0), dm2: prior (1.0, 15.0), dm3: prior (1.0, 18.0)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from models.bayesian import FROZEN_AT_EXCLUDED, STEP_PRIORS, BayesianPosterior
from models.experiment import Experiment, ExperimentStatus, append_experiment
from workflows.detect_responses import _normalize_name
from workflows.learn import (
    _compute_posterior,
    _is_denominator_excluded,
    _is_prior_dominated,
    _per_step_rates,
    evaluate_experiments,
    measure_cohorts,
)

# Test constant — pull a representative excluded value from the frozenset.
# Using a literal here would couple the test to a specific string; using
# `next(iter(FROZEN_AT_EXCLUDED))` keeps the test honest to whatever the
# set contains.
EXCLUDED_FROZEN_AT_EXAMPLE = next(iter(FROZEN_AT_EXCLUDED))

# ── Fixtures & helpers ────────────────────────────────────────────────────────


def _make_entry(
    stage: str,
    dm_step: int | None = 1,
    experiment_id: str = "exp-001",
    dm1_sent_at: str | None = "2026-05-01T12:00:00",
    dm2_sent_at: str | None = None,
    dm3_sent_at: str | None = None,
    experiment_id_frozen_at: str | None = "prospect",
) -> dict:
    """Build a minimal parsed Attio entry for per-step rate tests."""
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
    attio = MagicMock()
    raw = [{"_raw": e} for e in entries]
    attio.query_list_entries.return_value = raw
    attio.parse_entry.side_effect = lambda e: e["_raw"]
    return attio


def _make_experiment(
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


# ── §1: BayesianPosterior TypedDict ──────────────────────────────────────────


class TestBayesianPosteriorContract:
    """Verify that BayesianPosterior carries exactly the 7 required fields."""

    def test_required_fields_present(self):
        # Construct a posterior manually to confirm all 7 keys
        posterior = BayesianPosterior(
            alpha=5.0,
            beta=27.0,
            mean=0.15625,
            ci_low_95=0.054,
            ci_high_95=0.298,
            n_observed=10,
            n_successes=3,
        )
        assert set(posterior.keys()) == {
            "alpha", "beta", "mean", "ci_low_95", "ci_high_95",
            "n_observed", "n_successes",
        }

    def test_field_types(self):
        posterior = _compute_posterior(n_observed=10, n_successes=3, step_n=1)
        assert isinstance(posterior["alpha"], float)
        assert isinstance(posterior["beta"], float)
        assert isinstance(posterior["mean"], float)
        assert isinstance(posterior["ci_low_95"], float)
        assert isinstance(posterior["ci_high_95"], float)
        assert isinstance(posterior["n_observed"], int)
        assert isinstance(posterior["n_successes"], int)

    def test_n_observed_and_n_successes_are_provenance(self):
        posterior = _compute_posterior(n_observed=10, n_successes=3, step_n=1)
        assert posterior["n_observed"] == 10
        assert posterior["n_successes"] == 3


# ── §2: _compute_posterior — exact numeric assertions ────────────────────────


class TestComputePosteriorNumerics:
    """Worked-example fixtures with EXACT reference values.

    Reference values computed offline (recalibrated priors):
      dm1 priors (1.0, 22.0):
        n_observed=10, n_successes=3 → alpha=4.0, beta=29.0
        mean = 4/33 = 0.12121212
        ci = scipy_beta.interval(0.95, 4.0, 29.0) = (0.03513065, 0.25022695)

      dm2 priors (1.0, 22.0):
        n_observed=8, n_successes=1 → alpha=2.0, beta=29.0
        mean = 2/31 = 0.06451613
        ci = scipy_beta.interval(0.95, 2.0, 29.0) = (0.00817813, 0.17216946)

      dm3 priors (4.0, 15.0), zero observations:
        n_observed=0, n_successes=0 → alpha=4.0, beta=15.0
        mean = 4/19 ≈ 0.21052632
    """

    def test_dm1_worked_example(self):
        # dm1 prior (1.0, 22.0): post(1+3, 22+7) = (4.0, 29.0); mean = 4/33.
        p = _compute_posterior(n_observed=10, n_successes=3, step_n=1)
        assert p["alpha"] == pytest.approx(4.0)
        assert p["beta"] == pytest.approx(29.0)
        assert p["mean"] == pytest.approx(4.0 / 33.0, rel=1e-6)
        # 95% CI from offline computation
        assert p["ci_low_95"] == pytest.approx(0.03513065, rel=1e-4)
        assert p["ci_high_95"] == pytest.approx(0.25022695, rel=1e-4)

    def test_dm2_worked_example(self):
        # dm2 prior (1.0, 22.0): post(1+1, 22+7) = (2.0, 29.0); mean = 2/31.
        p = _compute_posterior(n_observed=8, n_successes=1, step_n=2)
        assert p["alpha"] == pytest.approx(2.0)
        assert p["beta"] == pytest.approx(29.0)
        assert p["mean"] == pytest.approx(2.0 / 31.0, rel=1e-6)
        assert p["ci_low_95"] == pytest.approx(0.00817813, rel=1e-4)
        assert p["ci_high_95"] == pytest.approx(0.17216946, rel=1e-4)

    def test_dm3_zero_observations_collapses_to_prior(self):
        p = _compute_posterior(n_observed=0, n_successes=0, step_n=3)
        prior_a, prior_b = STEP_PRIORS[3]
        assert p["alpha"] == pytest.approx(prior_a)
        assert p["beta"] == pytest.approx(prior_b)
        assert p["mean"] == pytest.approx(prior_a / (prior_a + prior_b), rel=1e-6)

    def test_posterior_mean_between_zero_and_one(self):
        for n_obs in (0, 1, 5, 20, 100):
            for n_suc in range(min(n_obs + 1, 5)):
                for step in (1, 2, 3):
                    p = _compute_posterior(n_obs, n_suc, step)
                    assert 0.0 < p["mean"] < 1.0, (
                        f"mean out of range for step={step}, n_obs={n_obs}, n_suc={n_suc}"
                    )

    def test_ci_low_less_than_mean_less_than_ci_high(self):
        p = _compute_posterior(n_observed=15, n_successes=2, step_n=1)
        assert p["ci_low_95"] < p["mean"] < p["ci_high_95"]

    def test_ci_width_shrinks_with_more_data(self):
        """More observations → tighter CI (Bayesian posterior concentration)."""
        p_few = _compute_posterior(n_observed=5, n_successes=1, step_n=1)
        p_many = _compute_posterior(n_observed=100, n_successes=10, step_n=1)
        width_few = p_few["ci_high_95"] - p_few["ci_low_95"]
        width_many = p_many["ci_high_95"] - p_many["ci_low_95"]
        assert width_many < width_few

    def test_prior_values_match_spec(self):
        """STEP_PRIORS must match the recalibrated values documented in
        models/bayesian.py (centered on each step's empirical baseline-v0 rate).
        """
        assert STEP_PRIORS[1] == (1.0, 22.0)  # mean 1/23 ≈ 4.35% (baseline 4.26%)
        assert STEP_PRIORS[2] == (1.0, 22.0)  # mean 1/23 ≈ 4.35% (baseline 2.44%, α-floor)
        assert STEP_PRIORS[3] == (4.0, 15.0)  # mean 4/19 = 21.05% (baseline 21.05%)


# ── §3: Denominator exclusion fixtures ───────────────────────────────────────


class TestDenominatorExclusion:
    """§3.10 + PR-9b dual exclusion invariants.

    Both conditions independently exclude a row from the denominator:
      (a) dmN_sent_at IS NULL
      (b) experiment_id_frozen_at == 'legacy_pure_unknown'
    """

    # ── _is_denominator_excluded unit tests ──────────────────────────────────

    def test_null_sent_at_excluded(self):
        """Row with dmN_sent_at=None MUST be excluded from denominator (PR-9b)."""
        entry = _make_entry("DM1 Sent", dm_step=1, dm1_sent_at=None,
                            experiment_id_frozen_at="prospect")
        assert _is_denominator_excluded(entry, 1) is True

    def test_nonnull_sent_at_eligible(self):
        """Row with dmN_sent_at populated and good frozen_at IS included."""
        entry = _make_entry("DM1 Sent", dm_step=1,
                            dm1_sent_at="2026-05-01T12:00:00",
                            experiment_id_frozen_at="prospect")
        assert _is_denominator_excluded(entry, 1) is False

    def test_legacy_pure_unknown_excluded(self):
        """Row with frozen_at='legacy_pure_unknown' is excluded regardless of sent_at."""
        entry = _make_entry("DM1 Sent", dm_step=1,
                            dm1_sent_at="2026-05-01T12:00:00",
                            experiment_id_frozen_at=EXCLUDED_FROZEN_AT_EXAMPLE)
        assert _is_denominator_excluded(entry, 1) is True

    def test_dual_exclusion_independent_sent_at_nonnull_frozen_unknown(self):
        """dm1_sent_at NOT NULL but frozen_at='legacy_pure_unknown' → STILL excluded.

        This is the key dual-condition test: the two exclusions are OR-ed, not AND-ed.
        """
        entry = _make_entry("DM1 Sent", dm_step=1,
                            dm1_sent_at="2026-05-01T12:00:00",
                            experiment_id_frozen_at=EXCLUDED_FROZEN_AT_EXAMPLE)
        assert _is_denominator_excluded(entry, 1) is True

    def test_archaeology_inferred_included(self):
        """frozen_at='legacy_inferred_by_archaeology' (confidence ≥0.8) IS included."""
        entry = _make_entry("DM1 Sent", dm_step=1,
                            dm1_sent_at="2026-05-01T12:00:00",
                            experiment_id_frozen_at="legacy_inferred_by_archaeology")
        assert _is_denominator_excluded(entry, 1) is False

    def test_frozen_at_none_with_sent_at_present_is_included(self):
        """frozen_at=None (pre-archaeology rows) with sent_at set → included."""
        entry = _make_entry("DM1 Sent", dm_step=1,
                            dm1_sent_at="2026-05-01T12:00:00",
                            experiment_id_frozen_at=None)
        assert _is_denominator_excluded(entry, 1) is False

    def test_step_key_reads_correct_sent_at_field(self):
        """Step N exclusion reads dm{N}_sent_at, not a generic field."""
        # dm1_sent_at is set but dm2_sent_at is None → dm2 step excluded
        entry = {
            "dm_step": 2,
            "stage": "DM2 Sent",
            "dm1_sent_at": "2026-05-01T12:00:00",
            "dm2_sent_at": None,
            "dm3_sent_at": None,
            "experiment_id_frozen_at": "prospect",
        }
        assert _is_denominator_excluded(entry, 1) is False  # dm1 eligible
        assert _is_denominator_excluded(entry, 2) is True   # dm2 not eligible
        assert _is_denominator_excluded(entry, 3) is True   # dm3 not eligible

    # ── _per_step_rates integration tests ────────────────────────────────────

    def test_null_sent_at_excluded_from_denominator_in_per_step_rates(self):
        """Entries with dmN_sent_at=None must not inflate n_observed."""
        entries = [
            # Eligible: dm1_sent_at set, prospect frozen_at
            _make_entry("DM1 Sent", dm_step=1,
                        dm1_sent_at="2026-05-01T12:00:00",
                        experiment_id_frozen_at="prospect"),
            # Excluded: dm1_sent_at=None
            _make_entry("DM1 Sent", dm_step=1,
                        dm1_sent_at=None,
                        experiment_id_frozen_at="prospect"),
        ]
        result = _per_step_rates(entries)
        assert result["dm1_received"] == 1, (
            "Row with null dm1_sent_at must not count in n_observed"
        )

    def test_legacy_pure_unknown_excluded_from_denominator(self):
        """Entries with frozen_at='legacy_pure_unknown' must not count in n_observed."""
        entries = [
            # Eligible
            _make_entry("DM1 Sent", dm_step=1,
                        dm1_sent_at="2026-05-01T12:00:00",
                        experiment_id_frozen_at="prospect"),
            # Excluded: legacy_pure_unknown
            _make_entry("DM1 Sent", dm_step=1,
                        dm1_sent_at="2026-05-01T12:00:00",
                        experiment_id_frozen_at=EXCLUDED_FROZEN_AT_EXAMPLE),
        ]
        result = _per_step_rates(entries)
        assert result["dm1_received"] == 1, (
            "Row with legacy_pure_unknown must not count in n_observed"
        )

    def test_dual_exclusion_independent_in_per_step_rates(self):
        """A row with sent_at NOT NULL but frozen_at='legacy_pure_unknown' is still excluded."""
        entries = [
            # Eligible
            _make_entry("DM1 Sent", dm_step=1,
                        dm1_sent_at="2026-05-01T12:00:00",
                        experiment_id_frozen_at="prospect"),
            # Excluded: both conditions satisfied (belt-and-suspenders check)
            _make_entry("DM1 Sent", dm_step=1,
                        dm1_sent_at="2026-05-01T12:00:00",
                        experiment_id_frozen_at=EXCLUDED_FROZEN_AT_EXAMPLE),
        ]
        result = _per_step_rates(entries)
        assert result["dm1_received"] == 1

    def test_archaeology_inferred_included_in_denominator(self):
        """frozen_at='legacy_inferred_by_archaeology' IS counted in n_observed."""
        entries = [
            _make_entry("DM1 Sent", dm_step=1,
                        dm1_sent_at="2026-05-01T12:00:00",
                        experiment_id_frozen_at="legacy_inferred_by_archaeology"),
            _make_entry("DM1 Sent", dm_step=1,
                        dm1_sent_at="2026-05-02T12:00:00",
                        experiment_id_frozen_at="prospect"),
        ]
        result = _per_step_rates(entries)
        assert result["dm1_received"] == 2

    def test_is_send_eligible_filter_not_applied_to_per_step_rates(self):
        """send-eligibility does NOT gate measurement math (§3.10 invariant).

        We simulate a row that would fail is_send_eligible (e.g., frozen_at=None
        which is legal pre-archaeology) but has a valid sent_at — it must be counted.
        The measurement denominator uses ONLY the dual exclusion, not any send-eligibility
        flag. We verify by constructing an entry with frozen_at=None (send-eligibility
        unknown) that should still appear in n_observed because sent_at is set.
        """
        entry = _make_entry("DM1 Sent", dm_step=1,
                            dm1_sent_at="2026-05-01T12:00:00",
                            experiment_id_frozen_at=None)  # might fail is_send_eligible
        result = _per_step_rates([entry])
        assert result["dm1_received"] == 1, (
            "Entry with sent_at populated must count even if frozen_at=None"
        )

    # ── Exact numeric worked example via _per_step_rates ─────────────────────

    def test_per_step_rates_exact_numerics_dm1(self):
        """Worked example: 10 eligible dm1 entries, 3 successes → known posterior.

        Reference (recalibrated dm1 prior):
          prior_alpha=1.0, prior_beta=22.0
          post_alpha=4.0, post_beta=29.0
          mean=4/33=0.12121212
          ci = scipy_beta.interval(0.95, 4.0, 29.0) = (0.03513065, 0.25022695)
        """
        # 7 DM1 Sent (no reply) + 3 Responded (reply at dm1)
        entries = []
        entries += [
            _make_entry("DM1 Sent", dm_step=1,
                        dm1_sent_at="2026-05-01T12:00:00",
                        experiment_id_frozen_at="prospect")
            for _ in range(7)
        ]
        entries += [
            _make_entry("Responded", dm_step=1,
                        dm1_sent_at="2026-05-01T12:00:00",
                        experiment_id_frozen_at="prospect")
            for _ in range(3)
        ]

        result = _per_step_rates(entries)
        p = result["dm1_posterior"]

        assert p["n_observed"] == 10
        assert p["n_successes"] == 3
        assert p["alpha"] == pytest.approx(4.0)
        assert p["beta"] == pytest.approx(29.0)
        assert p["mean"] == pytest.approx(4.0 / 33.0, rel=1e-6)
        assert p["ci_low_95"] == pytest.approx(0.03513065, rel=1e-4)
        assert p["ci_high_95"] == pytest.approx(0.25022695, rel=1e-4)
        # Flat scalar matches posterior mean
        assert result["dm1_response_rate"] == pytest.approx(p["mean"], rel=1e-9)

    def test_per_step_rates_zero_eligible_collapses_to_prior(self):
        """When all dm1 entries are excluded, n_observed=0 → posterior = prior."""
        # All have null sent_at → excluded
        entries = [
            _make_entry("DM1 Sent", dm_step=1, dm1_sent_at=None,
                        experiment_id_frozen_at="prospect")
            for _ in range(5)
        ]
        result = _per_step_rates(entries)
        p = result["dm1_posterior"]
        prior_a, prior_b = STEP_PRIORS[1]
        assert p["n_observed"] == 0
        assert p["alpha"] == pytest.approx(prior_a)
        assert p["beta"] == pytest.approx(prior_b)
        assert p["mean"] == pytest.approx(prior_a / (prior_a + prior_b), rel=1e-6)

    def test_per_step_rates_none_dm_step_still_excluded(self):
        """dm_step=None entries are excluded from ALL step denominators (pre-existing)."""
        entries = [
            _make_entry("DM1 Sent", dm_step=None,
                        dm1_sent_at="2026-05-01T12:00:00",
                        experiment_id_frozen_at="prospect"),
        ]
        result = _per_step_rates(entries)
        assert result["dm1_received"] == 0
        assert result["dm2_received"] == 0
        assert result["dm3_received"] == 0


# ── §4: _normalize_name diacritic fix ────────────────────────────────────────


class TestNormalizeName:
    """§4.1 diacritic fix via NFKD + strip combining marks.

    Existing behaviour (lowercase, collapse spaces) must be preserved.
    New: accented characters fold to ASCII base letters.
    """

    # Pre-existing behaviour preserved
    def test_lowercase(self):
        assert _normalize_name("Carlos Lopez") == "carlos lopez"

    def test_collapse_whitespace(self):
        assert _normalize_name("  Carlos   Lopez  ") == "carlos lopez"

    def test_uppercase_normalized(self):
        assert _normalize_name("VANESSA Solis") == "vanessa solis"

    # Diacritic stripping (new in PR-10)
    def test_jose_matches_jose(self):
        """José → jose (acute accent stripped)."""
        assert _normalize_name("José") == _normalize_name("Jose")

    def test_muller_matches_muller(self):
        """Müller → muller (umlaut stripped)."""
        assert _normalize_name("Müller") == _normalize_name("Muller")

    def test_inigo_matches_inigo(self):
        """Íñigo → inigo (tilde and acute stripped)."""
        assert _normalize_name("Íñigo") == _normalize_name("Inigo")

    def test_nono_matches_nono(self):
        """Ñoño → nono."""
        assert _normalize_name("Ñoño") == _normalize_name("Nono")

    def test_garcia_hernandez(self):
        assert _normalize_name("García Hernández") == _normalize_name("Garcia Hernandez")

    def test_polish_l_with_stroke_documents_known_gap(self):
        """Ł (U+0141) does NOT decompose under NFKD — documents the known gap.

        Stroke-marked characters encoded as single code points (Ł, Đ, Ø)
        don't decompose; this test exists to document that gap so a future
        attempt at "improving" _normalize_name doesn't accidentally fix it
        and break the regression suite. When a Polish-cohort customer
        actually shows up, replace this with an explicit-mapping fix.
        """
        # NFKD doesn't decompose Ł (no combining mark) but it's still lowercased
        normalized = _normalize_name("Łukasz")
        # At minimum: lowercased and stripped
        assert normalized == normalized.lower().strip()
        # Document the gap explicitly: Łukasz does NOT currently match Lukasz.
        # When this assertion fails it means someone shipped a stroke-fix —
        # remove this test and add positive coverage for Łukasz == Lukasz.
        assert _normalize_name("Łukasz") != _normalize_name("Lukasz"), (
            "Stroke-fix shipped — update _normalize_name docstring and "
            "convert this test to a positive-match assertion."
        )

    def test_zero_width_joiner_stripped(self):
        """LinkedIn occasionally pastes names with U+200D (ZWJ) embedded.

        silent-failure HIGH-5: a name like 'José‍' (with a trailing ZWJ)
        must normalize the same as 'José'. The Cf category strip in
        _normalize_name covers ZWJ, ZWNJ (U+200C), word joiner (U+2060),
        and other invisible format characters.
        """
        with_zwj = "José‍"  # José + zero-width joiner
        without_zwj = "José"
        assert _normalize_name(with_zwj) == _normalize_name(without_zwj)

    def test_idempotent(self):
        """Normalizing an already-normalized string returns the same string."""
        name = "Andrés Núñez"
        once = _normalize_name(name)
        twice = _normalize_name(once)
        assert once == twice

    def test_sao_paulo(self):
        assert _normalize_name("São Paulo") == _normalize_name("Sao Paulo")


# ── §5: evaluate_experiments interface preservation ──────────────────────────


class TestEvaluateExperimentsInterfacePreserved:
    """Existing callers of evaluate_experiments must not break after PR-10.

    Key backward-compat invariants:
    - verdict dict still has: experiment_id, verdict, dm_response_rate,
      baseline_rate, defensive_rate, baseline_defensive_rate, cohort_size,
      dmed, responded, step, rate_metric, step_rate, step_baseline_rate,
      dm1/2/3_response_rate
    - Won/lost/inconclusive/rejected_defensive verdicts still fire correctly
    - PR-10 adds dm{N}_posterior keys but does not remove any existing keys
    """

    def _mature_cohort(self, exp_id: str, dm_response_rate: float, dmed: int = 20) -> dict:
        responded = round(dm_response_rate * dmed)
        return {
            "experiment_id": exp_id,
            "cohort_size": 30,
            "dmed": dmed,
            "responded": responded,
            "dm_response_rate": dm_response_rate,
            "is_mature": True,
            "days_running": 10,
            "defensive_rate": 0.0,
            "dm1_response_rate": dm_response_rate,
            "dm2_response_rate": dm_response_rate * 0.5,
            "dm3_response_rate": dm_response_rate * 0.3,
            # dm{N}_received populated so the cohort isn't flagged prior-
            # dominated solely on the absence of either posterior or scalar.
            "dm1_received": dmed,
            "dm2_received": dmed,
            "dm3_received": dmed,
            "dm1_posterior": None,
            "dm2_posterior": None,
            "dm3_posterior": None,
        }

    def test_won_verdict_preserved(self, tmp_path):
        tsv = tmp_path / "experiments.tsv"
        append_experiment(_make_experiment("baseline-v0", "won", "2026-01-01", 0.08), tsv)
        append_experiment(_make_experiment("exp-001", "running", "2026-04-01"), tsv)

        cohorts = [self._mature_cohort("exp-001", dm_response_rate=0.12)]
        results = evaluate_experiments(cohorts, experiments_path=tsv)

        assert len(results) == 1
        assert results[0]["verdict"] == "won"

    def test_lost_verdict_preserved(self, tmp_path):
        tsv = tmp_path / "experiments.tsv"
        append_experiment(_make_experiment("baseline-v0", "won", "2026-01-01", 0.08), tsv)
        append_experiment(_make_experiment("exp-001", "running", "2026-04-01"), tsv)

        cohorts = [self._mature_cohort("exp-001", dm_response_rate=0.04)]
        results = evaluate_experiments(cohorts, experiments_path=tsv)

        assert results[0]["verdict"] == "lost"

    def test_inconclusive_verdict_preserved(self, tmp_path):
        tsv = tmp_path / "experiments.tsv"
        append_experiment(_make_experiment("baseline-v0", "won", "2026-01-01", 0.08), tsv)
        append_experiment(_make_experiment("exp-001", "running", "2026-04-01"), tsv)

        cohorts = [self._mature_cohort("exp-001", dm_response_rate=0.085)]
        results = evaluate_experiments(cohorts, experiments_path=tsv)

        assert results[0]["verdict"] == "inconclusive"

    def test_rejected_defensive_verdict_preserved(self, tmp_path):
        tsv = tmp_path / "experiments.tsv"
        append_experiment(_make_experiment("baseline-v0", "won", "2026-01-01", 0.08), tsv)
        append_experiment(_make_experiment("exp-001", "running", "2026-04-01"), tsv)

        cohort = self._mature_cohort("exp-001", dm_response_rate=0.12)
        cohort["defensive_rate"] = 0.25  # > 2× BASELINE_DEFENSIVE_RATE (0.10)
        results = evaluate_experiments([cohort], experiments_path=tsv)

        assert results[0]["verdict"] == "rejected_defensive"

    def test_existing_dict_keys_all_present(self, tmp_path):
        """All pre-PR-10 verdict dict keys must still be present."""
        tsv = tmp_path / "experiments.tsv"
        append_experiment(_make_experiment("baseline-v0", "won", "2026-01-01", 0.08), tsv)
        append_experiment(_make_experiment("exp-001", "running", "2026-04-01"), tsv)

        cohorts = [self._mature_cohort("exp-001", dm_response_rate=0.12)]
        results = evaluate_experiments(cohorts, experiments_path=tsv)
        r = results[0]

        required_keys = {
            "experiment_id", "verdict", "dm_response_rate", "baseline_rate",
            "defensive_rate", "baseline_defensive_rate", "cohort_size", "dmed",
            "responded", "step", "rate_metric", "step_rate", "step_baseline_rate",
            "dm1_response_rate", "dm2_response_rate", "dm3_response_rate",
        }
        assert required_keys.issubset(r.keys()), (
            f"Missing keys: {required_keys - r.keys()}"
        )

    def test_pr10_adds_posterior_keys_to_verdict(self, tmp_path):
        """PR-10 adds dm{N}_posterior keys — must be present in verdict dict."""
        tsv = tmp_path / "experiments.tsv"
        append_experiment(_make_experiment("baseline-v0", "won", "2026-01-01", 0.08), tsv)
        append_experiment(_make_experiment("exp-001", "running", "2026-04-01"), tsv)

        cohorts = [self._mature_cohort("exp-001", dm_response_rate=0.12)]
        results = evaluate_experiments(cohorts, experiments_path=tsv)
        r = results[0]

        assert "dm1_posterior" in r
        assert "dm2_posterior" in r
        assert "dm3_posterior" in r

    def test_immature_cohort_still_skipped(self, tmp_path):
        tsv = tmp_path / "experiments.tsv"
        append_experiment(_make_experiment("exp-001", "running", "2026-04-01"), tsv)

        cohort = self._mature_cohort("exp-001", dm_response_rate=0.12)
        cohort["is_mature"] = False
        results = evaluate_experiments([cohort], experiments_path=tsv)

        assert results == []

    def test_non_running_experiments_still_skipped(self, tmp_path):
        tsv = tmp_path / "experiments.tsv"
        append_experiment(_make_experiment("exp-won", "won", "2026-01-01", 0.15), tsv)

        cohorts = [self._mature_cohort("exp-won", dm_response_rate=0.15)]
        results = evaluate_experiments(cohorts, experiments_path=tsv)

        assert results == []


# ── §6: Soft-delete filter regression guard ──────────────────────────────────


class TestSoftDeleteFilterPreservedAfterRewrite:
    """§3.11 merged_into filter must still be present after PR-10's rewrite.

    If this test fails, the rewrite accidentally dropped the soft-delete filter
    from measure_cohorts.
    """

    def test_merged_into_losers_excluded_from_cohort_math(self):
        winner = _make_entry("DM1 Sent", dm_step=1, experiment_id="exp-soft")
        loser = _make_entry("DM1 Sent", dm_step=1, experiment_id="exp-soft")
        loser["merged_into"] = {"target_object": "people",
                                "target_record_id": "rid-winner"}

        attio = _make_attio_mock([winner, loser])
        with patch("workflows.learn.load_experiments", return_value=[]):
            out = measure_cohorts(attio)

        match = [r for r in out if r["experiment_id"] == "exp-soft"]
        assert len(match) == 1
        assert match[0]["dmed"] == 1, (
            "Soft-deleted loser must NOT inflate dmed denominator (§3.11 regression)"
        )

    def test_merged_into_truthy_excluded(self):
        """Any truthy merged_into value (not just a dict) should be excluded."""
        winner = _make_entry("DM1 Sent", dm_step=1, experiment_id="exp-x")
        loser = _make_entry("DM1 Sent", dm_step=1, experiment_id="exp-x")
        loser["merged_into"] = "rid-winner"  # string truthy value

        attio = _make_attio_mock([winner, loser])
        with patch("workflows.learn.load_experiments", return_value=[]):
            out = measure_cohorts(attio)

        match = [r for r in out if r["experiment_id"] == "exp-x"]
        assert match[0]["dmed"] == 1


# ── §7: measure_cohorts emits posterior keys ─────────────────────────────────


class TestMeasureCohortsPosteriorKeys:
    """measure_cohorts results must include dm{N}_posterior keys after PR-10."""

    def test_posterior_keys_present_in_cohort_result(self):
        entries = [
            _make_entry("DM1 Sent", dm_step=1,
                        dm1_sent_at="2026-05-01T12:00:00",
                        experiment_id_frozen_at="prospect"),
        ]
        attio = _make_attio_mock(entries)
        with patch("workflows.learn.load_experiments", return_value=[]):
            results = measure_cohorts(attio)

        r = results[0]
        assert "dm1_posterior" in r
        assert "dm2_posterior" in r
        assert "dm3_posterior" in r

    def test_posterior_is_bayesian_posterior_typed_dict(self):
        entries = [
            _make_entry("DM1 Sent", dm_step=1,
                        dm1_sent_at="2026-05-01T12:00:00",
                        experiment_id_frozen_at="prospect"),
        ]
        attio = _make_attio_mock(entries)
        with patch("workflows.learn.load_experiments", return_value=[]):
            results = measure_cohorts(attio)

        p = results[0]["dm1_posterior"]
        assert isinstance(p, dict)
        assert set(p.keys()) == {
            "alpha", "beta", "mean", "ci_low_95", "ci_high_95",
            "n_observed", "n_successes",
        }

    def test_dm_response_rate_scalar_matches_posterior_mean(self):
        """dm1_response_rate scalar must equal dm1_posterior['mean'] (backward compat)."""
        entries = [
            _make_entry("Responded", dm_step=1,
                        dm1_sent_at="2026-05-01T12:00:00",
                        experiment_id_frozen_at="prospect"),
            _make_entry("DM1 Sent", dm_step=1,
                        dm1_sent_at="2026-05-02T12:00:00",
                        experiment_id_frozen_at="prospect"),
        ]
        attio = _make_attio_mock(entries)
        with patch("workflows.learn.load_experiments", return_value=[]):
            results = measure_cohorts(attio)

        r = results[0]
        assert r["dm1_response_rate"] == pytest.approx(r["dm1_posterior"]["mean"], rel=1e-9)


# ── §8: _compute_posterior defensive validation (silent-failure CRIT-1) ──────


class TestComputePosteriorDefensiveValidation:
    """Malformed calls to _compute_posterior must fail loud, not produce
    numerically-valid-but-semantically-wrong posteriors that silently
    propagate into verdicts.
    """

    def test_unknown_step_n_raises(self):
        with pytest.raises(ValueError, match="unknown step_n"):
            _compute_posterior(n_observed=10, n_successes=3, step_n=4)  # type: ignore[arg-type]

    def test_step_n_zero_raises(self):
        with pytest.raises(ValueError, match="unknown step_n"):
            _compute_posterior(n_observed=10, n_successes=3, step_n=0)  # type: ignore[arg-type]

    def test_n_successes_exceeds_n_observed_raises(self):
        with pytest.raises(ValueError, match="n_successes .* > n_observed"):
            _compute_posterior(n_observed=5, n_successes=10, step_n=1)

    def test_negative_n_observed_raises(self):
        with pytest.raises(ValueError, match="must be non-negative"):
            _compute_posterior(n_observed=-1, n_successes=0, step_n=1)

    def test_negative_n_successes_raises(self):
        with pytest.raises(ValueError, match="must be non-negative"):
            _compute_posterior(n_observed=10, n_successes=-1, step_n=1)

    def test_boundary_zero_zero_is_valid(self):
        """n_obs=0, n_suc=0 must NOT raise — collapses to prior cleanly."""
        p = _compute_posterior(n_observed=0, n_successes=0, step_n=1)
        prior_a, prior_b = STEP_PRIORS[1]
        assert p["alpha"] == prior_a
        assert p["beta"] == prior_b

    def test_boundary_n_successes_equals_n_observed_is_valid(self):
        """All-success cohort is unusual but valid (e.g. 1/1 from a tiny seed)."""
        p = _compute_posterior(n_observed=5, n_successes=5, step_n=1)
        prior_a, prior_b = STEP_PRIORS[1]
        assert p["alpha"] == prior_a + 5
        assert p["beta"] == prior_b  # no failures added


# ── §9: prior_dominated verdict flag (silent-failure CRIT-2 + §0 #9) ─────────


class TestPriorDominatedVerdictFlag:
    """Verdicts on thinly-observed cohorts must carry prior_dominated=True
    so the operator can distinguish "won with n=0" from "won with n=200".
    """

    def _mature_cohort_with_n(
        self,
        exp_id: str,
        n_obs_per_step: tuple[int, int, int],
        dm_response_rate: float = 0.12,
    ) -> dict:
        """Build a cohort with explicit n_observed per step."""
        def post(n_obs: int, rate: float = 0.10) -> dict:
            n_suc = round(n_obs * rate)
            return {
                "alpha": 2.0 + n_suc,
                "beta": 20.0 + (n_obs - n_suc),
                "mean": (2.0 + n_suc) / (22.0 + n_obs),
                "ci_low_95": 0.01,
                "ci_high_95": 0.30,
                "n_observed": n_obs,
                "n_successes": n_suc,
            }

        n1, n2, n3 = n_obs_per_step
        return {
            "experiment_id": exp_id,
            "cohort_size": 30,
            "dmed": max(n_obs_per_step),
            "responded": 2,
            "dm_response_rate": dm_response_rate,
            "is_mature": True,
            "days_running": 10,
            "defensive_rate": 0.0,
            "dm1_response_rate": dm_response_rate,
            "dm2_response_rate": dm_response_rate * 0.5,
            "dm3_response_rate": dm_response_rate * 0.3,
            "dm1_received": n1,
            "dm2_received": n2,
            "dm3_received": n3,
            "dm1_posterior": post(n1, dm_response_rate),
            "dm2_posterior": post(n2, dm_response_rate * 0.5),
            "dm3_posterior": post(n3, dm_response_rate * 0.3),
        }

    def test_zero_observation_cohort_flagged_prior_dominated(self, tmp_path):
        tsv = tmp_path / "experiments.tsv"
        append_experiment(_make_experiment("baseline-v0", "won", "2026-01-01", 0.08), tsv)
        append_experiment(_make_experiment("exp-001", "running", "2026-04-01"), tsv)

        cohort = self._mature_cohort_with_n("exp-001", n_obs_per_step=(0, 0, 0))
        results = evaluate_experiments([cohort], experiments_path=tsv)

        assert len(results) == 1
        assert results[0]["prior_dominated"] is True

    def test_high_observation_cohort_not_flagged(self, tmp_path):
        tsv = tmp_path / "experiments.tsv"
        append_experiment(_make_experiment("baseline-v0", "won", "2026-01-01", 0.08), tsv)
        append_experiment(_make_experiment("exp-001", "running", "2026-04-01"), tsv)

        cohort = self._mature_cohort_with_n("exp-001", n_obs_per_step=(100, 100, 100))
        results = evaluate_experiments([cohort], experiments_path=tsv)

        assert results[0]["prior_dominated"] is False

    def test_one_thin_step_flags_whole_verdict(self, tmp_path):
        """The weakest step sets the bar — dm3 with n=2 flags the cohort
        even when dm1/dm2 are fat."""
        tsv = tmp_path / "experiments.tsv"
        append_experiment(_make_experiment("baseline-v0", "won", "2026-01-01", 0.08), tsv)
        append_experiment(_make_experiment("exp-001", "running", "2026-04-01"), tsv)

        cohort = self._mature_cohort_with_n("exp-001", n_obs_per_step=(100, 100, 2))
        results = evaluate_experiments([cohort], experiments_path=tsv)

        assert results[0]["prior_dominated"] is True

    def test_prior_dominated_warning_emitted(self, tmp_path, capsys):
        tsv = tmp_path / "experiments.tsv"
        append_experiment(_make_experiment("baseline-v0", "won", "2026-01-01", 0.08), tsv)
        append_experiment(_make_experiment("exp-thin", "running", "2026-04-01"), tsv)

        cohort = self._mature_cohort_with_n("exp-thin", n_obs_per_step=(0, 0, 0))
        evaluate_experiments([cohort], experiments_path=tsv)
        captured = capsys.readouterr()
        assert "prior-dominated verdict" in captured.err
        assert "exp-thin" in captured.err

    def test_is_prior_dominated_unit_zero_obs(self):
        cohort = {
            "dm1_posterior": {"n_observed": 0},
            "dm2_posterior": {"n_observed": 0},
            "dm3_posterior": {"n_observed": 0},
        }
        assert _is_prior_dominated(cohort) is True

    def test_is_prior_dominated_unit_threshold_boundary(self):
        """n_observed == SMALL_N_THRESHOLD-1 IS dominated; == SMALL_N_THRESHOLD is NOT."""
        from models.bayesian import SMALL_N_THRESHOLD
        thin = {
            "dm1_posterior": {"n_observed": SMALL_N_THRESHOLD - 1},
            "dm2_posterior": {"n_observed": 100},
            "dm3_posterior": {"n_observed": 100},
        }
        assert _is_prior_dominated(thin) is True

        ok = {
            "dm1_posterior": {"n_observed": SMALL_N_THRESHOLD},
            "dm2_posterior": {"n_observed": SMALL_N_THRESHOLD},
            "dm3_posterior": {"n_observed": SMALL_N_THRESHOLD},
        }
        assert _is_prior_dominated(ok) is False

    def test_is_prior_dominated_falls_back_to_received_when_posterior_absent(self):
        """Pre-PR-10 cohorts without dm{N}_posterior use dm{N}_received."""
        cohort_pre_pr10 = {
            "dm1_received": 100,
            "dm2_received": 100,
            "dm3_received": 2,  # thin
        }
        assert _is_prior_dominated(cohort_pre_pr10) is True


# ── §10: _is_denominator_excluded migration-gap warning (CRIT-3) ─────────────


class TestDenominatorExclusionMigrationWarning:
    """Distinguishing 'key absent' from 'value None' — a row with the
    experiment_id_frozen_at key absent indicates an incomplete migration
    rather than a pre-archaeology row. The function must surface the gap
    (warning) but preserve "included" semantics so denominators don't
    silently halve.
    """

    def test_frozen_at_key_absent_emits_warning(self, capsys):
        """Key absent (not present-with-value-None) → warning to stderr."""
        entry = {
            "record_id": "rid-pre-migration",
            "dm_step": 1,
            "stage": "DM1 Sent",
            "dm1_sent_at": "2026-05-01T12:00:00",
            # NOTE: no 'experiment_id_frozen_at' key at all
        }
        result = _is_denominator_excluded(entry, 1)
        captured = capsys.readouterr()
        # Preserves prior behavior: row is INCLUDED.
        assert result is False
        # But the gap is surfaced.
        assert "pre-migration" in captured.err.lower() or "absent" in captured.err.lower()
        assert "rid-pre-migration" in captured.err

    def test_frozen_at_key_present_with_none_no_warning(self, capsys):
        """frozen_at=None (key present, value None) is a pre-archaeology row,
        NOT a pre-migration row — no warning expected."""
        entry = {
            "record_id": "rid-pre-archaeology",
            "dm_step": 1,
            "stage": "DM1 Sent",
            "dm1_sent_at": "2026-05-01T12:00:00",
            "experiment_id_frozen_at": None,
        }
        result = _is_denominator_excluded(entry, 1)
        captured = capsys.readouterr()
        assert result is False
        assert "pre-migration" not in captured.err.lower()

    def test_falsy_sent_at_empty_string_excluded(self):
        """sent_at='' (empty string) is treated as missing — silent-failure MED-7."""
        entry = {
            "dm_step": 1,
            "stage": "DM1 Sent",
            "dm1_sent_at": "",
            "experiment_id_frozen_at": "prospect",
        }
        assert _is_denominator_excluded(entry, 1) is True


# ── §11: evaluate_experiments — drop silent 0.0 fallback (HIGH-4) ────────────


class TestEvaluateExperimentsSilentFallback:
    """A missing rate-metric must skip the verdict (with a warning), not
    default to 0.0 which silently produces a 'lost' verdict.
    """

    def test_missing_step_rate_skips_verdict_with_warning(self, tmp_path, capsys):
        tsv = tmp_path / "experiments.tsv"
        append_experiment(_make_experiment("baseline-v0", "won", "2026-01-01", 0.08), tsv)
        append_experiment(
            Experiment(
                experiment_id="exp-step",
                status=ExperimentStatus.RUNNING,
                started="2026-04-01",
                completed="",
                dm_response_rate=0.0,
                baseline_rate=0.08,
                cohort_size=10,
                variable="messages.json:dm1:operations_leaders:es",
                description="test",
            ),
            tsv,
        )

        # Cohort missing dm1_posterior AND dm1_response_rate → must skip
        cohort = {
            "experiment_id": "exp-step",
            "cohort_size": 30,
            "dmed": 20,
            "responded": 0,
            "dm_response_rate": 0.0,
            "is_mature": True,
            "days_running": 10,
            "defensive_rate": 0.0,
            # NOTE: dm1_response_rate absent, dm1_posterior absent
            "dm2_response_rate": 0.0,
            "dm3_response_rate": 0.0,
            "dm1_received": 0,
            "dm2_received": 0,
            "dm3_received": 0,
        }
        results = evaluate_experiments([cohort], experiments_path=tsv)
        captured = capsys.readouterr()
        assert results == []
        assert "missing" in captured.err.lower()
        assert "exp-step" in captured.err


# ── §12: Partial-exclusion accounting (pr-test MED-1) ────────────────────────


class TestPartialExclusionAccounting:
    """A cohort with mixed eligible / excluded entries must report n_observed
    and n_successes that match only the eligible subset — a successful
    response on an excluded row must NOT be counted.
    """

    def test_partial_exclusion_with_excluded_success(self):
        """5 eligible (2 success) + 3 excluded (1 of which is a success):
        n_observed=5, n_successes=2 — the excluded success is dropped.
        """
        entries: list[dict] = []
        # 5 eligible: 2 responded, 3 sent (no response)
        entries.append(_make_entry("Responded", dm_step=1,
                                   dm1_sent_at="2026-05-01T10:00:00",
                                   experiment_id_frozen_at="prospect"))
        entries.append(_make_entry("Responded", dm_step=1,
                                   dm1_sent_at="2026-05-01T11:00:00",
                                   experiment_id_frozen_at="prospect"))
        entries.append(_make_entry("DM1 Sent", dm_step=1,
                                   dm1_sent_at="2026-05-01T12:00:00",
                                   experiment_id_frozen_at="prospect"))
        entries.append(_make_entry("DM1 Sent", dm_step=1,
                                   dm1_sent_at="2026-05-01T13:00:00",
                                   experiment_id_frozen_at="prospect"))
        entries.append(_make_entry("DM1 Sent", dm_step=1,
                                   dm1_sent_at="2026-05-01T14:00:00",
                                   experiment_id_frozen_at="prospect"))
        # 3 excluded (legacy_pure_unknown): one of which is a success
        entries.append(_make_entry("Responded", dm_step=1,
                                   dm1_sent_at="2026-05-01T15:00:00",
                                   experiment_id_frozen_at=EXCLUDED_FROZEN_AT_EXAMPLE))
        entries.append(_make_entry("DM1 Sent", dm_step=1,
                                   dm1_sent_at="2026-05-01T16:00:00",
                                   experiment_id_frozen_at=EXCLUDED_FROZEN_AT_EXAMPLE))
        entries.append(_make_entry("DM1 Sent", dm_step=1,
                                   dm1_sent_at="2026-05-01T17:00:00",
                                   experiment_id_frozen_at=EXCLUDED_FROZEN_AT_EXAMPLE))

        result = _per_step_rates(entries)
        assert result["dm1_received"] == 5
        assert result["dm1_replies"] == 2
        # Posterior reflects eligible-only counts
        prior_a, prior_b = STEP_PRIORS[1]
        assert result["dm1_posterior"]["alpha"] == pytest.approx(prior_a + 2)
        assert result["dm1_posterior"]["beta"] == pytest.approx(prior_b + 3)


# ── §13: CI-based verdicts (fix/audit-learning-loop-stats) ───────────────────


class TestCIBasedVerdicts:
    """The verdict is decided by the posterior 95% CI vs the baseline, NOT the
    posterior mean ± 2pp.

      Won  iff ci_low_95  > baseline   (pure superiority)
      Lost iff ci_high_95 < baseline
      else Inconclusive

    All reference numbers use the recalibrated dm1 prior (1, 22) and the
    empirical dm1 baseline 4.26% (baseline-v0). Math is shown per case.
    """

    def _tsv_with_dm1_baseline(self, tmp_path, dm1_baseline: float):
        """baseline-v0 row (status=won) carrying a dm1 step rate. The step
        baseline for dm1 resolves to this value via get_baseline_step_rate."""
        tsv = tmp_path / "experiments.tsv"
        append_experiment(
            Experiment(
                experiment_id="baseline-v0",
                started="2026-01-01",
                completed="2026-03-01",
                cohort_size=100,
                dm_response_rate=0.0,  # overall column intentionally 0 (the bug)
                baseline_rate=0.0,
                status=ExperimentStatus.WON,
                variable="",
                description="historical baseline",
                dm1_response_rate=dm1_baseline,
                dm2_response_rate=0.024,
                dm3_response_rate=0.21,
            ),
            tsv,
        )
        append_experiment(
            Experiment(
                experiment_id="messages.json:dm1:ops_leaders:en:v2-2026-01-15",
                started="2026-04-01",
                completed="",
                cohort_size=200,
                dm_response_rate=0.0,
                baseline_rate=dm1_baseline,
                status=ExperimentStatus.RUNNING,
                variable="messages.json:dm1:ops_leaders:en:v2-2026-01-15",
                description="ci test",
            ),
            tsv,
        )
        return tsv

    def _cohort_with_dm1(self, n_obs: int, n_suc: int) -> dict:
        """Build a mature cohort whose dm1 posterior is computed for real from
        (n_obs, n_suc). dm2/dm3 are padded n_obs so the cohort isn't flagged
        prior-dominated (we want to exercise the CI verdict, not the flag)."""
        p1 = _compute_posterior(n_obs, n_suc, 1)
        p2 = _compute_posterior(max(n_obs, 10), 0, 2)
        p3 = _compute_posterior(max(n_obs, 10), 0, 3)
        return {
            "experiment_id": "messages.json:dm1:ops_leaders:en:v2-2026-01-15",
            "cohort_size": 200,
            "dmed": n_obs,
            "responded": n_suc,
            "dm_response_rate": n_suc / n_obs if n_obs else 0.0,
            "is_mature": True,
            "days_running": 30,
            "defensive_rate": 0.0,
            "dm1_response_rate": p1["mean"],
            "dm2_response_rate": p2["mean"],
            "dm3_response_rate": p3["mean"],
            "dm1_received": p1["n_observed"],
            "dm2_received": p2["n_observed"],
            "dm3_received": p3["n_observed"],
            "dm1_posterior": p1,
            "dm2_posterior": p2,
            "dm3_posterior": p3,
        }

    def test_strong_signal_wins_via_ci(self, tmp_path):
        """n=100, k=30 → post(31,92), ci_low=0.1796 > baseline 0.0426 → Won."""
        tsv = self._tsv_with_dm1_baseline(tmp_path, 0.0425531914893617)
        cohort = self._cohort_with_dm1(n_obs=100, n_suc=30)
        results = evaluate_experiments([cohort], experiments_path=tsv)
        assert results[0]["verdict"] == "won"
        # The CI lower bound (not the mean) drove it.
        assert results[0]["step_ci_low_95"] > results[0]["step_baseline_rate"]

    def test_clear_null_loses_via_ci(self, tmp_path):
        """n=100, k=0 → post(1,122), ci_high=0.0298 < baseline 0.0426 → Lost."""
        tsv = self._tsv_with_dm1_baseline(tmp_path, 0.0425531914893617)
        cohort = self._cohort_with_dm1(n_obs=100, n_suc=0)
        results = evaluate_experiments([cohort], experiments_path=tsv)
        assert results[0]["verdict"] == "lost"
        assert results[0]["step_ci_high_95"] < results[0]["step_baseline_rate"]

    def test_thin_signal_is_inconclusive_via_ci(self, tmp_path):
        """The headline fix: a thin cohort the mean rule calls 'won' the CI
        rule correctly calls 'inconclusive'.

        n=15, k=2 → post(1+2, 22+13) = (3, 35).
          mean    = 3/38 = 0.0789  → mean rule: 0.0789 > 0.0426+0.02=0.0626 → 'won'
          ci_low  = 0.0170, ci_high = 0.1819 (95% CI of Beta(3, 35))
          CI rule: ci_low (0.0170) NOT > baseline (0.0426) and ci_high (0.1819)
          NOT < baseline → the interval straddles the baseline → 'inconclusive'.

        This is exactly the false-positive the CI rule eliminates: two replies
        out of fifteen is not statistically distinguishable from the 4.26%
        baseline, yet the old mean rule would have promoted the variant.
        """
        tsv = self._tsv_with_dm1_baseline(tmp_path, 0.0425531914893617)
        cohort = self._cohort_with_dm1(n_obs=15, n_suc=2)
        results = evaluate_experiments([cohort], experiments_path=tsv)
        r = results[0]
        assert r["verdict"] == "inconclusive"
        # The legacy mean-based verdict is emitted alongside and DISAGREES —
        # this is the transition-comparability signal for the operator.
        assert r["mean_verdict"] == "won", (
            "The old mean-based rule would have (wrongly) called this a win; "
            "the mean_verdict field must surface that disagreement."
        )

    def test_n15_k1_false_positive_eliminated(self):
        """Direct numeric proof the n=15 true-null FP collapses, comparing the
        full OLD system against the full NEW system.

        At the true null (variant rate == dm1 baseline 4.26%), enumerate every
        k in 0..15, weight by Binomial(15, baseline), and sum P(verdict==won).

          OLD system: OLD prior Beta(2, 20) + mean-vs-(baseline+0.02) rule.
            Fires for k>=1 → ~47.9% true-null false-positive rate.
          NEW system: recalibrated prior Beta(1, 22) + ci_low-vs-baseline rule.
            Requires k>=4 → ~0.3%.

        The before/after must use each system's OWN prior — that is the actual
        regression that shipped, not a half-swapped hybrid.
        """
        from scipy.stats import beta as _beta
        from scipy.stats import binom as _binom

        baseline = 0.0425531914893617
        n = 15
        old_a, old_b = 2.0, 20.0  # the prior this change replaces
        new_a, new_b = STEP_PRIORS[1]  # (1.0, 22.0)
        assert (new_a, new_b) == (1.0, 22.0)

        fp_old = 0.0
        fp_new = 0.0
        for k in range(n + 1):
            pk = _binom.pmf(k, n, baseline)
            # OLD: mean of Beta(2+k, 20+n-k) vs baseline+2pp.
            old_mean = (old_a + k) / (old_a + old_b + n)
            if old_mean > baseline + 0.02:
                fp_old += pk
            # NEW: 2.5th pct of Beta(1+k, 22+n-k) vs baseline (pure superiority).
            ci_low = _beta.ppf(0.025, new_a + k, new_b + (n - k))
            if ci_low > baseline:
                fp_new += pk

        # Old system: ~47.9% true-null false positives.
        assert fp_old == pytest.approx(0.4791, abs=0.005)
        # New system: < 1% — two orders of magnitude better.
        assert fp_new < 0.01
        assert fp_new == pytest.approx(0.0031, abs=0.001)

    def test_mean_verdict_emitted_for_comparability(self, tmp_path):
        """Every verdict dict carries mean_verdict + CI bounds for transition."""
        tsv = self._tsv_with_dm1_baseline(tmp_path, 0.0425531914893617)
        cohort = self._cohort_with_dm1(n_obs=100, n_suc=30)
        r = evaluate_experiments([cohort], experiments_path=tsv)[0]
        assert "mean_verdict" in r
        assert "step_ci_low_95" in r
        assert "step_ci_high_95" in r
        assert r["step_ci_low_95"] is not None
        assert r["step_ci_high_95"] is not None
