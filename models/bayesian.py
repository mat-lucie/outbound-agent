"""Bayesian posterior typed contract for per-step rate estimation.

PR-10 ships the `BayesianPosterior` TypedDict that `_per_step_rates` returns
per cohort+step. Consumers (evaluate_experiments, weekly_brain, PR-11+) read
alpha/beta/mean/ci_low_95/ci_high_95 for decision-making and n_observed/
n_successes for provenance.

Prior calibration (recalibrated — fix/audit-learning-loop-stats)
----------------------------------------------------------------
Empirical-Bayes priors are set PER STEP, each centered on that step's
EMPIRICALLY-OBSERVED baseline-v0 rate (experiments.tsv baseline-v0 row),
with weakly-informative strength (effective sample size S = α + β):

  dm1: prior_alpha=1.0, prior_beta=22.0  → prior mean ≈ 4.35%  (S=23)
  dm2: prior_alpha=1.0, prior_beta=22.0  → prior mean ≈ 4.35%  (S=23)
  dm3: prior_alpha=4.0, prior_beta=15.0  → prior mean ≈ 21.05% (S=19)

Derivation (baseline-v0 per-step rates: dm1=4.26%, dm2=2.44%, dm3=21.05%):
  - dm1: target 4.26% → Beta(1, 22) gives 1/23 = 4.35%, closest clean
    integer prior at strength ≈ 22 (err 0.09pp).
  - dm2: target 2.44%. The integer-α floor is α=1, and Beta(1, 22) = 4.35%
    is the lowest clean mean reachable at S ≈ 22 (a lower mean would need
    S ≈ 41, which is STRONGER than we want). We deliberately accept the
    slight over-estimate (4.35% vs 2.44%) rather than over-strengthen the
    prior — dm2 still sits in the same low-single-digit regime, far below
    the old 6% / 9% mis-anchors. Documented so a future calibrator with
    real dm2 volume can tighten it.
  - dm3: target 21.05% → Beta(4, 15) gives 4/19 = 21.05%, EXACT at S=19.

WHY THIS CHANGED: the old priors were anchored on a back-calculated 5-9%
"$/week table" belief, NOT on observed data. The old dm1 prior Beta(2,20)
had mean 9.1% — more than 2× the real 4.26% dm1 baseline. Combined with the
old mean-vs-(baseline+2pp) verdict rule, that inflated prior produced a
47.9% true-null false-positive rate at the n=15 maturity floor (a single
k=1 reply read "Won"). Centering each prior on its own empirical baseline,
plus the CI-based verdict rule (see workflows/learn.py::evaluate_experiments),
drops the n=15 true-null FP rate to ≈0.3%.

All three priors keep S ≤ 23 (equal-or-weaker than the old Σ=22 dm1 prior)
so they damp noise for small cohorts without overpowering real signal in
mature cohorts (n ≥ 20).

Denominator exclusion (§3.10 / PR-9b invariants — both conditions are
independent; either alone excludes a row):
  (a) dmN_sent_at IS NULL — we don't know when this DM was sent; treating the
      outcome as observed would manufacture a denominator we don't have.
  (b) experiment_id_frozen_at == 'legacy_pure_unknown' — measurement-excluded
      archaeology rows; confidence is below the 0.8 threshold required to
      include them in cohort math.

`is_send_eligible` is NOT applied here — that filter gates the SEND path, not
the MEASUREMENT path.
"""

from __future__ import annotations

from typing import TypedDict


class BayesianPosterior(TypedDict):
    """Bayesian Beta posterior for a single cohort × DM step.

    Fields
    ------
    alpha : float
        Posterior alpha = prior_alpha + n_successes.
    beta : float
        Posterior beta = prior_beta + (n_observed - n_successes).
    mean : float
        Posterior mean = alpha / (alpha + beta).
    ci_low_95 : float
        2.5th percentile of the posterior Beta distribution (95% CI lower).
    ci_high_95 : float
        97.5th percentile of the posterior Beta distribution (95% CI upper).
    n_observed : int
        Denominator: entries eligible for this step (dmN_sent_at IS NOT NULL
        AND experiment_id_frozen_at != 'legacy_pure_unknown').
    n_successes : int
        Numerator: entries that responded at exactly this step.
    """

    alpha: float
    beta: float
    mean: float
    ci_low_95: float
    ci_high_95: float
    n_observed: int
    n_successes: int


class CohortStepRates(TypedDict):
    """Per-step rate bundle returned by `_per_step_rates`.

    The flat scalar keys (dm{N}_response_rate / dm{N}_received / dm{N}_replies)
    are preserved for backward compatibility with pre-PR-10 callers; the
    `dm{N}_posterior` keys hold the full BayesianPosterior dicts.
    """

    dm1_posterior: BayesianPosterior
    dm2_posterior: BayesianPosterior
    dm3_posterior: BayesianPosterior
    dm1_response_rate: float
    dm2_response_rate: float
    dm3_response_rate: float
    dm1_received: int
    dm2_received: int
    dm3_received: int
    dm1_replies: int
    dm2_replies: int
    dm3_replies: int


class VerdictDict(TypedDict, total=False):
    """Verdict dict returned by `evaluate_experiments`.

    `total=False` because not all keys are guaranteed on every code path
    (e.g. dm{N}_posterior is None on pre-PR-10 cohort inputs). Consumers
    should defensively check for None on the posterior keys.
    """

    experiment_id: str
    verdict: str
    prior_dominated: bool
    dm_response_rate: float
    baseline_rate: float
    defensive_rate: float
    baseline_defensive_rate: float
    cohort_size: int
    dmed: int
    responded: int
    step: str
    rate_metric: str
    step_rate: float
    step_baseline_rate: float
    # CI-based verdict surfacing (fix/audit-learning-loop-stats). The CI bounds
    # the verdict was decided on; None on the overall / pre-PR-10 routing where
    # no per-step posterior exists. `mean_verdict` is the legacy mean-vs-±2pp
    # verdict emitted alongside for transition comparability.
    step_ci_low_95: float | None
    step_ci_high_95: float | None
    mean_verdict: str
    dm1_response_rate: float | None
    dm2_response_rate: float | None
    dm3_response_rate: float | None
    dm1_posterior: BayesianPosterior | None
    dm2_posterior: BayesianPosterior | None
    dm3_posterior: BayesianPosterior | None


# Per-step empirical-Bayes priors. Keyed by DM step number (1, 2, 3).
# (prior_alpha, prior_beta) encodes the population-wide rate belief before
# seeing any cohort data. Effective sample size = prior_alpha + prior_beta.
# Recalibrated to the empirical baseline-v0 per-step rates (see module
# docstring for the full derivation). Each (alpha, beta) is centered on the
# step's observed baseline at weakly-informative strength S = alpha + beta.
STEP_PRIORS: dict[int, tuple[float, float]] = {
    1: (1.0, 22.0),  # dm1 prior mean = 1/23 ≈ 4.35% (baseline 4.26%, S=23)
    2: (1.0, 22.0),  # dm2 prior mean = 1/23 ≈ 4.35% (baseline 2.44%, α-floor, S=23)
    3: (4.0, 15.0),  # dm3 prior mean = 4/19 = 21.05% (baseline 21.05% exact, S=19)
}

# Rows with a frozen_at value in this set are excluded from measurement
# denominators. §3.10 invariant: confidence < 0.8 → label = legacy_pure_unknown
# → excluded. frozenset over str because membership is checked per-row in hot
# loops; the immutable type also prevents accidental in-flight mutation.
FROZEN_AT_EXCLUDED: frozenset[str] = frozenset({"legacy_pure_unknown"})

# Cohorts with fewer observations than this on ANY step are flagged as
# prior-dominated — the posterior mean is closer to the prior than to the
# observed data and the verdict should not be trusted in isolation. §0 #9
# "surface uncertainty" invariant: a 0-obs cohort's "won"/"lost" verdict
# must be distinguishable from a 100-obs cohort's "won"/"lost".
SMALL_N_THRESHOLD: int = 5
