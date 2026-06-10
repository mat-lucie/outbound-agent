"""Cohort measurement and experiment evaluation for A/B learning.

PR-10 rewrites `_per_step_rates` to return `BayesianPosterior` typed dicts
per cohort+step, and updates `evaluate_experiments` to consume the posterior
mean for verdict decisions.

PR-22 adds two things:

  1. `COHORT_MEASUREMENT_START_DATE` — operator-facing anchor. Data quality
     before this date is archaeology-inferred; `legacy_inferred_by_archaeology`
     rows (confidence ≥ 0.8) are INCLUDED in denominators (§3.10 hybrid).

  2. Measure-side dedup in `measure_cohorts` — deduplicates by
     `canonical_linkedin_url` BEFORE grouping by `experiment_id`. This
     prevents double-counting the same prospect across multiple LinkedIn
     Outreach entries that survived PR-9.5's physical-record dedup
     (residual pattern or new entries for the same URL). This is the
     measurement-time safety net; PR-9.5 handled physical-record dedup.

Denominator exclusion (§3.10 / PR-9b invariants — both conditions independently
exclude a row from the measurement denominator):
  (a) dmN_sent_at IS NULL — we don't know when this DM was sent; counting the
      entry would manufacture a denominator we cannot verify.
  (b) experiment_id_frozen_at == 'legacy_pure_unknown' — below the 0.8
      archaeology confidence threshold; measurement-excluded per §3.10.

Note: `legacy_inferred_by_archaeology` (confidence ≥ 0.8) IS included in
denominators — PR-10's `FROZEN_AT_EXCLUDED` only excludes `legacy_pure_unknown`.
This is the §3.10 archaeology hybrid contract.

The `is_send_eligible` filter is NOT applied here — that filter gates the SEND
path, not the MEASUREMENT path.
"""

import sys
from datetime import date, datetime
from typing import Literal, cast

try:
    from scipy.stats import beta as scipy_beta
except ImportError as exc:  # pragma: no cover — surfaces dep failure early
    raise ImportError(
        "scipy is required for Bayesian posterior computation (PR-10). "
        "Install with: pip install 'scipy>=1.13' "
        "(declared in pyproject.toml [project.dependencies])."
    ) from exc

from models.bayesian import (
    FROZEN_AT_EXCLUDED,
    SMALL_N_THRESHOLD,
    STEP_PRIORS,
    BayesianPosterior,
    CohortStepRates,
    VerdictDict,
)
from models.experiment import (
    Experiment,
    ExperimentStatus,
    get_baseline_rate,
    get_baseline_step_rate,
    load_experiments,
    upsert_experiment,
)
from models.pipeline import STAGE_RANK, PipelineStage, dm_step_int

# PR-22: data-quality anchor for operator-facing reports.
#
# OPERATOR-FACING DOCUMENTATION ANCHOR ONLY. This is NOT a date-filter on
# rows. Pre/post split is enforced via `experiment_id_frozen_at` enum
# values: `legacy_pure_unknown` is excluded via `FROZEN_AT_EXCLUDED`;
# `legacy_inferred_by_archaeology` is included. A date-based filter would
# be redundant with — and could quietly diverge from — the frozen_at gate.
#
# Data quality before this date is archaeology-inferred (see
# scripts/backfill_experiment_id_archaeology.py). Rows stamped
# `legacy_inferred_by_archaeology` (confidence >= 0.8) are INCLUDED
# in measurement denominators per the §3.10 archaeology hybrid.
# `legacy_pure_unknown` rows (confidence < 0.8) are EXCLUDED via
# FROZEN_AT_EXCLUDED in models.bayesian.
COHORT_MEASUREMENT_START_DATE = "2026-05-22"

_STEP_KEYS = {"dm1", "dm2", "dm3"}


def _parse_step_from_variable(variable: str) -> str | None:
    """Extract the DM step from an experiment's variable string.

    Variable format: 'messages.json:<step>:<persona>:<lang>' (or similar).
    Returns 'dm1' / 'dm2' / 'dm3' when the second segment is a recognized DM step,
    otherwise None (covers connection_note, scoring experiments, malformed strings).
    """
    if not variable:
        return None
    parts = variable.split(":")
    if len(parts) < 2:
        return None
    step = parts[1]
    return step if step in _STEP_KEYS else None

# Stages that count as "DM'd" (received at least DM1).
#
# UNREACHABLE (Wave-2-A) is included but is SPECIAL: a prospect can be parked
# at UNREACHABLE *after* receiving DM1/DM2 (a later step hit InMail-required /
# OON), keeping its dm_step. Such a row genuinely received DMs and must stay in
# the denominator — dropping it removed delivered-to non-responders and inflated
# dm_response_rate. But a row parked at UNREACHABLE before any DM was delivered
# (dm_step=0/None — InMail-required on DM1, or an OON invite never accepted)
# never received a DM. `_counts_as_dmed` applies a dm_step>=1 guard to
# UNREACHABLE only; every other DM'd stage keeps its existing stage-only
# semantics. UNREACHABLE is deliberately kept OUT of `_RESPONDED_STAGES` — it is
# an undeliverability terminal, not a reply.
_DMED_STAGES = {
    PipelineStage.DM1_SENT.value,
    PipelineStage.DM2_SENT.value,
    PipelineStage.DM3_SENT.value,
    PipelineStage.RESPONDED.value,
    PipelineStage.CALL_BOOKED.value,
    PipelineStage.QUALIFIED.value,
    PipelineStage.NOT_INTERESTED.value,
    PipelineStage.UNREACHABLE.value,
}

# Stages that count as "responded"
_RESPONDED_STAGES = {
    PipelineStage.RESPONDED.value,
    PipelineStage.CALL_BOOKED.value,
    PipelineStage.QUALIFIED.value,
}


def _counts_as_dmed(entry: dict) -> bool:
    """Whether an entry received at least DM1, for cohort DM denominators.

    Stage membership in `_DMED_STAGES` is sufficient for every stage EXCEPT
    UNREACHABLE, which additionally requires dm_step>=1 (see the `_DMED_STAGES`
    note): an UNREACHABLE row parked before DM1 delivery never received a DM and
    must not enter the denominator. `dm_step` is a select-type slug
    ("dm1"/"invite"/"connection_note"/...) — an OON park keeps whatever slug the
    row carried — so it is coerced via `dm_step_int` (a raw `slug >= 1` would
    TypeError). dm_step that resolves to 0 (dm0/connection_note/invite/None)
    stays excluded.

    Note: this is the AGGREGATE-denominator gate and deliberately applies no
    `dmN_sent_at`-NULL check; `_per_step_rates` applies that PR-9b gate
    separately for the per-step denominators, so aggregate `dmed` can exceed the
    summed per-step `dmN_received` for a row whose send timestamp is NULL.
    """
    stage = entry.get("stage")
    if stage not in _DMED_STAGES:
        return False
    if stage == PipelineStage.UNREACHABLE.value:
        return dm_step_int(entry.get("dm_step")) >= 1
    return True

_MATURITY_MIN_DMED = 15
_MATURITY_MIN_DAYS = 7

# Cadence-complete maturity (fix/audit-learning-loop-stats).
#
# The OLD gate ((≥7 days) AND (≥15 DM'd)) froze the verdict at first maturity
# — typically ~day 7 — BEFORE the cohort's DM3 (~day 16) had even been sent.
# A verdict struck mid-cadence measures an incomplete funnel and never
# re-measures (apply_verdict writes a terminal status). We add a cadence-
# completeness requirement so a cohort is only graded once its members have
# actually run through the cadence.
#
# Cadence span: DM1 lands ~1d after accept, DM2 +5d after DM1, DM3 +10d after
# DM2 (workflows.dm_sequencer.DM_TIMING). So the last DM (DM3) is sent ~16
# days after the cohort's first contact. We require the experiment to have
# been running at least this long OR every DM'd member to be "cadence-
# settled" (reached DM3, hit a terminal stage, or responded) — whichever is
# satisfied first. The ≥15-DM'd floor is unchanged.
#
# Derivation: 1 (accept→DM1) + 5 (DM1→DM2) + 10 (DM2→DM3) = 16 calendar days.
_CADENCE_COMPLETE_MIN_DAYS = 16

# Stages that count as "cadence-settled" without needing dm_step==3: a member
# that responded or hit a terminal undeliverable/disposition has left the
# cadence and will receive no further DM, so its funnel contribution is final.
# (UNREACHABLE / NOT_INTERESTED are terminal; the _RESPONDED_STAGES are final
# by definition. DM3_SENT is covered by the dm_step==3 check below.)
_CADENCE_TERMINAL_STAGES = _RESPONDED_STAGES | {
    PipelineStage.NOT_INTERESTED.value,
    PipelineStage.UNREACHABLE.value,
}

# Classification buckets used in per-cohort breakdowns. "none" is the default
# bucket for entries where response_classification is unset (Attio default).
# PR-20 fold-in (math-QA): `manual_unclassified` is its own bucket so cohort
# reports can distinguish "pending operator review" from "true non-respondent"
# (the prior collapse into "none" hid that signal). Operators reclassify
# manual_unclassified rows to a real classification once they triage; the
# next learn run picks up the new value automatically.
_CLASSIFICATION_BUCKETS = [
    "positive",
    "question",
    "neutral",
    "negative",
    "defensive",
    "manual_unclassified",
    "none",
]

# Baseline defensive-rate threshold. A variant whose defensive rate exceeds
# 2× this is rejected even if its response rate would otherwise qualify as a
# win. Hardcoded pending one real cohort of data to calibrate against.
BASELINE_DEFENSIVE_RATE = 0.10


def _empty_breakdown() -> dict:
    return {b: 0 for b in _CLASSIFICATION_BUCKETS}


def _count_classification(entries: list[dict]) -> dict:
    breakdown = _empty_breakdown()
    for entry in entries:
        cls = entry.get("response_classification") or "none"
        if cls in breakdown:
            breakdown[cls] += 1
        else:
            breakdown["none"] += 1
    return breakdown


def _is_denominator_excluded(entry: dict, step_n: int) -> bool:
    """Return True when an entry must be excluded from the step-N denominator.

    Dual condition — either alone is sufficient to exclude:
      (a) The corresponding dmN_sent_at is missing/empty (PR-9b invariant:
          NULL means "we don't know when this was sent", not "it was never
          sent"). Falsy values — None, "", 0 — all count as missing.
      (b) experiment_id_frozen_at IS IN `FROZEN_AT_EXCLUDED` (§3.10 invariant:
          archaeology confidence < 0.8 → measurement-excluded).

    Label vs. behavior caveat (PR-22 fold-in): a row stamped
    `legacy_inferred_by_archaeology` is labeled "MEASUREMENT-INCLUDED" by the
    §3.10 archaeology hybrid, but the PR-9b NULL-sent_at gate still applies
    independently. So a high-confidence archaeology row whose `dm{N}_sent_at`
    is NULL is INCLUDED-by-frozen_at-label but EXCLUDED-by-sent_at-gate. The
    two gates are checked separately and either alone is sufficient to exclude.
    This is consistent with §3.10a: archaeology infers cohort identity, not
    send timestamps.

    Migration safety: when the `experiment_id_frozen_at` key is ABSENT from
    the entry (vs. present-but-None), it indicates an incomplete migration —
    the row predates the legacy_pure_unknown archaeology pass. We emit a
    structured warning and INCLUDE the row (preserves prior behavior) so a
    silent migration gap doesn't quietly halve the denominator. Key absent ≠
    value None: pre-archaeology rows have the key with value None; pre-
    migration rows lack the key entirely.

    Note: is_send_eligible is NOT checked here. That filter gates the SEND
    path only and must not be applied to the measurement denominator (§3.10).
    The caller (`_per_step_rates`) pre-filters entries by `dm_step >= step_n`,
    so this function only sees candidates the caller already verified are
    nominally eligible for the step.
    """
    sent_at_key = f"dm{step_n}_sent_at"
    sent_at = entry.get(sent_at_key)
    if not sent_at:
        # Catches None, "", and falsy values. Per PR-9b: NULL/empty means
        # "we don't know when this DM was sent" and the row must be excluded.
        return True
    if "experiment_id_frozen_at" not in entry:
        # Key absent — pre-archaeology / pre-migration row. Surface the gap
        # but preserve "included" semantics so an incomplete migration
        # doesn't silently change cohort sizes.
        print(
            f"WARNING: row record_id={entry.get('record_id')!r} has "
            f"{sent_at_key} populated but 'experiment_id_frozen_at' key is "
            "absent — possibly pre-migration; included in denominator. "
            "Run archaeology backfill (§3.10) to close this gap.",
            file=sys.stderr,
        )
        return False
    return entry.get("experiment_id_frozen_at") in FROZEN_AT_EXCLUDED


def _compute_posterior(
    n_observed: int,
    n_successes: int,
    step_n: Literal[1, 2, 3],
) -> BayesianPosterior:
    """Compute the Beta posterior for a cohort × step.

    Uses empirical-Bayes calibrated priors from `models.bayesian.STEP_PRIORS`.
    Posterior: Beta(prior_alpha + n_successes, prior_beta + n_observed - n_successes).
    95% CI from the 2.5th and 97.5th percentiles of the posterior distribution.

    When n_observed == 0 the posterior collapses to the prior (no data observed).
    This is the correct Bayesian behaviour — we don't manufacture zeros.

    Defensive validation raises `ValueError` rather than silently producing
    garbage (silent-failure CRIT-1). A malformed call from a buggy caller
    must fail loud; a Beta(α<0, β<0) or unknown-step posterior would
    otherwise emit numerically-valid but semantically-wrong rates that
    propagate into the verdict.
    """
    if step_n not in STEP_PRIORS:
        raise ValueError(
            f"_compute_posterior: unknown step_n={step_n!r}; "
            f"valid: {sorted(STEP_PRIORS)}"
        )
    if n_observed < 0 or n_successes < 0:
        raise ValueError(
            f"_compute_posterior: counts must be non-negative; "
            f"got n_observed={n_observed}, n_successes={n_successes}"
        )
    if n_successes > n_observed:
        raise ValueError(
            f"_compute_posterior: n_successes ({n_successes}) > "
            f"n_observed ({n_observed}) for step_n={step_n}"
        )
    prior_alpha, prior_beta = STEP_PRIORS[step_n]
    post_alpha = prior_alpha + n_successes
    post_beta = prior_beta + (n_observed - n_successes)
    post_mean = post_alpha / (post_alpha + post_beta)
    ci_low, ci_high = scipy_beta.interval(0.95, post_alpha, post_beta)
    return BayesianPosterior(
        alpha=post_alpha,
        beta=post_beta,
        mean=post_mean,
        ci_low_95=float(ci_low),
        ci_high_95=float(ci_high),
        n_observed=n_observed,
        n_successes=n_successes,
    )


def _per_step_rates(dmed_entries: list[dict]) -> CohortStepRates:
    """Compute per-DM-step Bayesian posteriors from a cohort's DM'd entries.

    For each step N in {1, 2, 3}:
      n_observed  = entries eligible for step N:
                    dm_step >= N AND dmN_sent_at IS NOT NULL
                    AND experiment_id_frozen_at != 'legacy_pure_unknown'
      n_successes = entries that responded at exactly step N
                    (stage in _RESPONDED_STAGES AND dm_step == N)
                    subject to the same dual exclusion as the denominator

    Returns a dict with three keys — 'dm1', 'dm2', 'dm3' — each holding a
    `BayesianPosterior` typed dict. Also emits flat scalar keys for backward
    compatibility with callers that read dm{N}_response_rate / dm{N}_received /
    dm{N}_replies directly:
      dm{N}_response_rate : posterior mean (replaces raw ratio)
      dm{N}_received      : n_observed (denominator)
      dm{N}_replies       : n_successes (numerator)
      dm{N}_posterior     : BayesianPosterior typed dict

    The denominator excludes entries where:
      (a) dmN_sent_at IS NULL — PR-9b invariant
      (b) experiment_id_frozen_at == 'legacy_pure_unknown' — §3.10 invariant
    Both conditions independently exclude; a row meeting either is excluded.

    dm_step that resolves to 0 (None / dm0 / connection_note / invite) is
    excluded from ALL step denominators (pre-existing behaviour, unaffected by
    the dual exclusion above). `dm_step` is a select-type slug, so it is coerced
    via `dm_step_int` before the numeric comparison — a raw `slug >= n` would
    TypeError, and a raw `slug == n` would silently never match (undercounting
    replies).
    """
    result: dict = {}
    for n in (1, 2, 3):
        # Candidates: dm_step is set AND dm_step >= N
        candidates = [
            e for e in dmed_entries
            if dm_step_int(e.get("dm_step")) >= n
        ]

        # Apply dual denominator exclusion (either condition independently excludes)
        eligible = [e for e in candidates if not _is_denominator_excluded(e, n)]
        n_observed = len(eligible)

        # Successes: responded at exactly step N (same eligibility filter on
        # success side — a success on an excluded row is also excluded)
        n_successes = sum(
            1 for e in eligible
            if e.get("stage") in _RESPONDED_STAGES and dm_step_int(e.get("dm_step")) == n
        )

        # step_n is a hard-coded literal from (1, 2, 3) — cast is safe.
        posterior = _compute_posterior(n_observed, n_successes, cast("Literal[1, 2, 3]", n))

        result[f"dm{n}_posterior"] = posterior
        # Flat scalars for backward compatibility
        result[f"dm{n}_response_rate"] = posterior["mean"]
        result[f"dm{n}_received"] = n_observed
        result[f"dm{n}_replies"] = n_successes

    # Cast: keys are statically known to match CohortStepRates' shape, but the
    # dict was built via f-string keys which mypy can't track. Runtime shape
    # is asserted by tests/test_pr10_bayesian_math.py.
    return cast("CohortStepRates", result)


def _persona_summary(p_entries: list[dict]) -> dict:
    p_dmed = len(p_entries)
    p_responded = sum(1 for e in p_entries if e.get("stage") in _RESPONDED_STAGES)
    p_breakdown = _count_classification(p_entries)
    p_defensive = p_breakdown.get("defensive", 0)
    return {
        "dmed": p_dmed,
        "responded": p_responded,
        "dm_response_rate": p_responded / p_dmed if p_dmed > 0 else 0.0,
        "defensive_rate": p_defensive / p_dmed if p_dmed > 0 else 0.0,
        "classification_breakdown": p_breakdown,
    }


def _dedup_by_canonical_url(entries: list[dict]) -> list[dict]:
    """PR-22 measure-side dedup (B-AW-DEDUP-006).

    Deduplicate entries by `canonical_linkedin_url` before cohort grouping.
    When two entries share the same canonical URL, keep the one with higher
    `stage_rank` (the entry furthest along the funnel) — it carries the most
    informative state for cohort math.

    Entries with no `canonical_linkedin_url` (None or empty string) are
    included as-is; we cannot dedup without a key.

    This is the MEASUREMENT-TIME safety net. Physical-record dedup was handled
    by PR-9.5's `scripts.attio_dedup`. This layer catches residual patterns
    where the same LinkedIn URL has two separate live (non-merged) list entries.
    """
    def _rank(entry: dict) -> int:
        stage_val = entry.get("stage")
        if not stage_val:
            return -1
        try:
            stage = PipelineStage(stage_val) if isinstance(stage_val, str) else stage_val
            return STAGE_RANK.get(stage, -1)
        except ValueError:
            return -1

    def _is_preferred(candidate: dict, incumbent: dict) -> bool:
        """Return True when `candidate` should replace `incumbent`.

        Primary key: higher stage_rank wins (the entry furthest along the
        funnel carries the most cohort-math information).

        Tie-break (equal stage_rank): prefer the entry with the LATER
        last_contact_date — closer to "actively worked" state. Final
        tie-break: lexicographically smaller record_id — purely structural,
        ensures reproducibility across runs. Without these tie-breaks the
        dedup output depended on iteration order and was non-deterministic.
        """
        c_rank = _rank(candidate)
        i_rank = _rank(incumbent)
        if c_rank != i_rank:
            return c_rank > i_rank
        c_lcd = candidate.get("last_contact_date") or ""
        i_lcd = incumbent.get("last_contact_date") or ""
        if c_lcd != i_lcd:
            return c_lcd > i_lcd
        c_id = candidate.get("record_id") or ""
        i_id = incumbent.get("record_id") or ""
        return c_id < i_id

    seen: dict[str, dict] = {}  # canonical_url → best entry seen so far
    no_url: list[dict] = []

    for entry in entries:
        url = entry.get("canonical_linkedin_url")
        if not url or not str(url).strip():
            no_url.append(entry)
            continue
        normalized = str(url).strip().rstrip("/").lower()
        prior = seen.get(normalized)
        if prior is None or _is_preferred(entry, prior):
            seen[normalized] = entry

    # Preserve insertion order: seen-by-URL first (in first-encounter order),
    # then the no-URL entries.
    result = list(seen.values()) + no_url
    return result


def measure_cohorts(attio) -> list[dict]:
    """Query all Attio list entries and measure cohort metrics.

    Returns one row per experiment_id. Each row carries:
    - Top-level counts (dmed, responded, dm_response_rate, cohort_size)
    - Maturity flags (is_mature, days_running)
    - classification_breakdown: dict of classification → count across dmed
    - defensive_rate: defensive count divided by dmed count
    - per_persona: dict of persona → same metrics, narrowed to that persona
    - dm{N}_posterior: BayesianPosterior per step (PR-10)
    - dm{N}_response_rate: posterior mean per step (PR-10, backward-compat scalar)
    - dm{N}_received / dm{N}_replies: eligible n_observed / n_successes (PR-10)
    """
    raw_entries = attio.query_list_entries(limit=5000)
    parsed = [attio.parse_entry(e) for e in raw_entries]
    # §3.11 soft-delete filter: drop loser entries whose union-merge
    # winner already carries the cohort identity + cadence math. Including
    # them double-counts dmed/responded denominators against the same
    # underlying prospect.
    parsed = [entry for entry in parsed if not entry.get("merged_into")]

    # PR-22 measure-side dedup (B-AW-DEDUP-006): deduplicate by
    # canonical_linkedin_url BEFORE grouping by experiment_id.
    # PR-9.5 handled physical-record dedup; this is the measurement-time
    # safety net for residual duplicate patterns (same URL, two live
    # LinkedIn Outreach entries). When two entries share a canonical URL,
    # prefer the one with higher stage_rank (further along the funnel) as
    # it is the most informative for cohort math. Entries with no
    # canonical_linkedin_url are included as-is (cannot dedup without key).
    parsed = _dedup_by_canonical_url(parsed)

    # Load experiments once for maturity timing. Missing-file is legitimate
    # (fresh project with no TSV yet). A malformed TSV, permission error, or
    # CSV parse failure is a real bug and must halt the learn loop rather
    # than silently grant every cohort default maturity.
    experiments_by_id: dict[str, str] = {}  # experiment_id → started date string
    try:
        for exp in load_experiments():
            if exp.experiment_id not in experiments_by_id:
                experiments_by_id[exp.experiment_id] = exp.started
    except FileNotFoundError:
        pass

    # Group by experiment_id
    # Rows with NULL experiment_id (PROSPECT-committed during the no-experiment
    # window introduced in PR-21) accumulate in the "unknown" bucket. They
    # cannot mature into a verdict because `days_running` is anchored to 0 —
    # there is no TSV/Attio Experiment row for the "unknown" pseudo-id — so
    # they never reach the maturity threshold and never receive a verdict.
    # PR-22 archaeology will retroactively assign frozen_at
    # ("legacy_pure_unknown" or "legacy_inferred_by_archaeology") to these
    # rows so they exit the "unknown" bucket into a real experiment cohort.
    groups: dict[str, list[dict]] = {}
    for entry in parsed:
        exp_id = entry.get("experiment_id") or "unknown"
        groups.setdefault(exp_id, []).append(entry)

    today = date.today()
    results = []

    for exp_id, entries in groups.items():
        dmed_entries = [e for e in entries if _counts_as_dmed(e)]
        dmed = len(dmed_entries)
        responded = sum(1 for e in dmed_entries if e.get("stage") in _RESPONDED_STAGES)
        dm_response_rate = responded / dmed if dmed > 0 else 0.0

        # PR-22 disclosure: count rows whose cohort identity was inferred
        # by archaeology rather than committed live by PR-21. Operators
        # need this to interpret a high "DM'd" count — e.g. a cohort with
        # 50 DM'd but 40 archaeology-inferred is mostly historical
        # attribution, not a freshly-running experiment.
        archaeology_inferred_count = sum(
            1 for e in entries
            if e.get("experiment_id_frozen_at") == "legacy_inferred_by_archaeology"
        )

        breakdown = _count_classification(dmed_entries)
        defensive_count = breakdown.get("defensive", 0)
        defensive_rate = defensive_count / dmed if dmed > 0 else 0.0

        per_step = _per_step_rates(dmed_entries)

        # Per-persona breakdown
        personas_seen: dict[str, list[dict]] = {}
        for entry in dmed_entries:
            persona = entry.get("persona") or "unknown"
            personas_seen.setdefault(persona, []).append(entry)
        per_persona = {
            persona: _persona_summary(p_entries)
            for persona, p_entries in personas_seen.items()
        }

        # Determine days running
        days_running = 0
        started_str = experiments_by_id.get(exp_id)
        if started_str:
            try:
                started_date = datetime.strptime(started_str, "%Y-%m-%d").date()
                days_running = (today - started_date).days
            except ValueError:
                pass
        else:
            # Not in TSV — treat as historical (mature by default). Use the
            # cadence-complete span so the cadence-completeness gate also
            # passes by age (a historical cohort has long since run its
            # cadence), not just the old 7-day floor.
            days_running = _CADENCE_COMPLETE_MIN_DAYS

        # Cadence-completeness (fix/audit-learning-loop-stats): a cohort is
        # measurable only once its members have actually run through the
        # cadence. Two ways to satisfy it (whichever comes first):
        #   (a) the experiment has been running >= the full cadence span
        #       (~16 days, so even a member that accepted on day 0 has had its
        #       DM3 window open), OR
        #   (b) every DM'd member is cadence-settled — reached DM3 (dm_step==3)
        #       or hit a terminal/responded stage — so no member is still
        #       waiting on a future DM3.
        cadence_settled = all(
            dm_step_int(e.get("dm_step")) >= 3
            or e.get("stage") in _CADENCE_TERMINAL_STAGES
            for e in dmed_entries
        )
        cadence_complete = days_running >= _CADENCE_COMPLETE_MIN_DAYS or cadence_settled

        is_mature = (
            dmed >= _MATURITY_MIN_DMED
            and days_running >= _MATURITY_MIN_DAYS
            and cadence_complete
        )

        results.append({
            "experiment_id": exp_id,
            "cohort_size": len(entries),
            "dmed": dmed,
            "responded": responded,
            "dm_response_rate": dm_response_rate,
            "is_mature": is_mature,
            "days_running": days_running,
            "classification_breakdown": breakdown,
            "defensive_rate": defensive_rate,
            "per_persona": per_persona,
            "archaeology_inferred_count": archaeology_inferred_count,
            **per_step,
        })

    return results


def _is_prior_dominated(cohort: dict) -> bool:
    """Return True when ANY per-step posterior has fewer than
    SMALL_N_THRESHOLD observations.

    A prior-dominated cohort's verdict reflects the prior more than the
    data. §0 #9 invariant: surface this to the operator rather than treat
    a "won" with n=0 as equivalent to a "won" with n=100.

    We check every step (dm1, dm2, dm3) — even when the experiment only
    targets one step the operator still needs to see the full funnel. The
    flag goes True if ANY step is thin so the weakest signal sets the
    confidence bar.
    """
    for step in (1, 2, 3):
        posterior = cohort.get(f"dm{step}_posterior")
        if posterior is None:
            # Pre-PR-10 cohort — fall back to the flat scalar's denominator
            # surrogate (dm{N}_received). When neither is present we can't
            # claim the cohort is well-observed, so flag it.
            n_obs = cohort.get(f"dm{step}_received")
            if n_obs is None or n_obs < SMALL_N_THRESHOLD:
                return True
            continue
        if posterior.get("n_observed", 0) < SMALL_N_THRESHOLD:
            return True
    return False


def _mean_based_verdict(rate: float, baseline: float) -> str:
    """Legacy mean-vs-(baseline ± 2pp) verdict.

    Retained for two reasons (fix/audit-learning-loop-stats):
      1. Emitted alongside the CI-based verdict in the verdict dict
         (mean_verdict) so operators can compare old vs new semantics during
         the transition. The two will disagree at low n — that is the point:
         the old rule cried "won" on noise, the CI rule says "inconclusive".
      2. Used as the fallback verdict when no posterior CI is available
         (overall / non-step routing).

    Does NOT apply the defensive veto — that override is handled by the
    caller before this runs.
    """
    if rate > baseline + 0.02:
        return "won"
    if rate < baseline - 0.02:
        return "lost"
    return "inconclusive"


def evaluate_experiments(
    cohort_results: list[dict],
    experiments_path=None,
) -> list[VerdictDict]:
    """Evaluate mature running experiment cohorts and assign verdicts.

    Verdicts (fix/audit-learning-loop-stats: CI-based rule):
      - rejected_defensive: defensive_rate > 2× BASELINE_DEFENSIVE_RATE
        (overrides every other verdict — a reactance-triggering variant
        is killed even if its raw response rate would look like a win)
      - won: posterior 95% CI lower bound > step_baseline_rate (pure superiority)
      - lost: posterior 95% CI upper bound < step_baseline_rate
      - inconclusive: CI straddles the baseline
      Fallback (no CI — overall/non-step routing): mean-vs-(baseline±2pp).
      Legacy mean_verdict is emitted alongside for transition comparability.

    The `step_rate` used for verdict decisions is the Bayesian posterior mean
    from `dm{N}_posterior["mean"]` when the experiment targets a specific DM
    step. This is identical to the `dm{N}_response_rate` scalar emitted by
    `_per_step_rates` for backward compatibility — callers reading
    `step_rate` from the verdict dict get the posterior mean regardless.

    Routing: when an experiment's `variable` parses to a DM step (e.g.,
    'messages.json:dm1:operations_leaders:es'), the verdict uses that
    step's per-step rate from the cohort and that step's baseline from
    `get_baseline_step_rate`. Otherwise the verdict uses the overall rate.

    Verdict-confidence surfacing (§0 #9, silent-failure CRIT-2):
      - `prior_dominated: bool` — True when any per-step n_observed is
        below SMALL_N_THRESHOLD. Operators must NOT treat a prior-
        dominated "won"/"lost" the same as a converged one. A structured
        warning is emitted to stderr identifying the cohort.

    Returned dict keys:
      - dm_response_rate / baseline_rate: ALWAYS the overall figures.
        Preserved for the TSV row that gets persisted (the TSV's
        `dm_response_rate` column is the funnel KPI, not the routed rate).
      - step / rate_metric / step_rate / step_baseline_rate: the routing
        used for the verdict. Read these for "what drove the verdict."
      - dm1_response_rate / dm2_response_rate / dm3_response_rate:
        per-step rates from the cohort, passed through for the TSV write.
      - dm{N}_posterior: BayesianPosterior typed dicts, passed through for
        downstream consumers (PR-11+).
      - prior_dominated: confidence flag (PR-10 addition).
    """
    experiments = load_experiments(experiments_path)

    # Build a lookup of experiment_id → most recent experiment (last row wins)
    exp_by_id: dict[str, Experiment] = {}
    for e in experiments:
        exp_by_id[e.experiment_id] = e

    overall_baseline = get_baseline_rate(experiments_path)

    verdicts: list[VerdictDict] = []
    for cohort in cohort_results:
        if not cohort["is_mature"]:
            continue

        exp_id = cohort["experiment_id"]
        exp: Experiment | None = exp_by_id.get(exp_id)

        # Only evaluate running experiments
        if exp is None or exp.status != ExperimentStatus.RUNNING:
            continue

        # Route the verdict to the per-step rate when the experiment targets a
        # specific DM step; otherwise use the overall rate (connection_note,
        # scoring experiments, anything else).
        step = _parse_step_from_variable(exp.variable)
        # CI-based verdict (fix/audit-learning-loop-stats): the verdict is
        # decided by the posterior 95% CI bounds, not the posterior mean. We
        # capture the CI bounds here when a per-step posterior is available so
        # the verdict block below can compare them against the baseline.
        verdict_ci_low: float | None = None
        verdict_ci_high: float | None = None
        if step is not None:
            rate_metric = f"{step}_response_rate"
            # Prefer the posterior mean from dm{N}_posterior when available;
            # fall back to the flat scalar for cohorts computed before PR-10.
            posterior_key = f"{step}_posterior"
            if posterior_key in cohort and cohort[posterior_key] is not None:
                verdict_rate = cohort[posterior_key]["mean"]
                verdict_ci_low = cohort[posterior_key].get("ci_low_95")
                verdict_ci_high = cohort[posterior_key].get("ci_high_95")
            else:
                # silent-failure HIGH-4: don't default-to-0.0. A missing
                # rate metric is a data gap, not a "lost" verdict. Surface
                # the gap and skip the verdict so the operator notices.
                verdict_rate = cohort.get(rate_metric)
                if verdict_rate is None:
                    print(
                        f"WARNING: missing {rate_metric!r} for "
                        f"experiment_id={exp_id!r}; skipping verdict. "
                        "Cohort lacks both posterior and flat scalar — "
                        "check measure_cohorts output.",
                        file=sys.stderr,
                    )
                    continue
            verdict_baseline = get_baseline_step_rate(step, experiments_path)
        else:
            rate_metric = "dm_response_rate"
            verdict_rate = cohort["dm_response_rate"]
            verdict_baseline = overall_baseline

        defensive_rate = cohort.get("defensive_rate", 0.0)

        # ── Verdict decision (fix/audit-learning-loop-stats) ──────────────
        #
        # CI-BASED RULE (replaces the old mean-vs-(baseline±2pp) rule):
        #   Won  iff posterior 95% CI lower bound  > baseline
        #   Lost iff posterior 95% CI upper bound  < baseline
        #   else Inconclusive
        #
        # We use PURE SUPERIORITY (compare against `baseline`, not
        # `baseline + 0.02`). The ±2pp business buffer is intentionally left
        # OUT of the CI test: the CI already encodes uncertainty, so a
        # variant whose entire 95% credible interval sits above the baseline
        # is a real win regardless of a fixed 2pp cushion. Folding +2pp into
        # the CI test would make the threshold uncertainty-blind again. The
        # 2pp figure is preserved only in the legacy mean-based verdict
        # emitted alongside (the `mean_verdict` key) for transition
        # comparability.
        #
        # WHY: the old mean-vs-threshold rule had a 47.9% true-null false-
        # positive rate at the n=15 maturity floor (k=1 reply → "Won").
        # The CI rule drops that to ≈0.3% — at current volumes most verdicts
        # correctly read Inconclusive until enough signal accumulates.
        #
        # FALLBACK: when no per-step posterior CI is available (the overall /
        # non-step routing, or a pre-PR-10 cohort), we cannot run the CI test
        # and fall back to the legacy mean-vs-(baseline±2pp) rule so those
        # paths keep a defined verdict rather than silently going Inconclusive.
        mean_verdict = _mean_based_verdict(verdict_rate, verdict_baseline)
        if defensive_rate > BASELINE_DEFENSIVE_RATE * 2:
            verdict = "rejected_defensive"
        elif verdict_ci_low is not None and verdict_ci_high is not None:
            # CI-based rule (pure superiority against the baseline).
            if verdict_ci_low > verdict_baseline:
                verdict = "won"
            elif verdict_ci_high < verdict_baseline:
                verdict = "lost"
            else:
                verdict = "inconclusive"
        else:
            # No CI available — legacy mean-based fallback (±2pp buffer).
            verdict = mean_verdict

        # §0 #9 confidence surfacing: a thinly-observed cohort's verdict
        # is dominated by the prior. Emit a structured warning so the
        # operator notices BEFORE acting on the verdict.
        prior_dominated = _is_prior_dominated(cohort)
        if prior_dominated:
            step_counts = {
                f"dm{s}": (
                    (cohort.get(f"dm{s}_posterior") or {}).get("n_observed")
                    if cohort.get(f"dm{s}_posterior") is not None
                    else cohort.get(f"dm{s}_received")
                )
                for s in (1, 2, 3)
            }
            print(
                f"WARNING: prior-dominated verdict for experiment_id={exp_id!r} "
                f"(verdict={verdict!r}, step_n_observed={step_counts}). "
                f"At least one step has n_observed < {SMALL_N_THRESHOLD}; "
                "the posterior reflects the prior more than the data.",
                file=sys.stderr,
            )

        # Verdict dict preserves the existing contract:
        #   - dm_response_rate = cohort's overall rate
        #   - baseline_rate    = overall baseline (what gets persisted to TSV)
        # And adds new fields exposing the routing used for the verdict:
        #   - step / rate_metric / step_rate / step_baseline_rate
        # PR-10 additions:
        #   - dm{N}_posterior: BayesianPosterior dicts passed through for PR-11+
        #   - prior_dominated: confidence flag (§0 #9)
        verdicts.append(cast("VerdictDict", {
            "experiment_id": exp_id,
            "verdict": verdict,
            "prior_dominated": prior_dominated,
            "dm_response_rate": cohort["dm_response_rate"],
            "baseline_rate": overall_baseline,
            "defensive_rate": defensive_rate,
            "baseline_defensive_rate": BASELINE_DEFENSIVE_RATE,
            "cohort_size": cohort["cohort_size"],
            "dmed": cohort["dmed"],
            "responded": cohort["responded"],
            "step": step or "overall",
            "rate_metric": rate_metric,
            "step_rate": verdict_rate,
            "step_baseline_rate": verdict_baseline,
            # CI-based verdict transition surfacing (fix/audit-learning-loop-stats):
            #   - step_ci_low_95 / step_ci_high_95: the posterior CI bounds the
            #     verdict was decided on (None on overall / pre-PR-10 routing).
            #   - mean_verdict: what the OLD mean-vs-±2pp rule would have said,
            #     emitted for operator comparability during the transition.
            "step_ci_low_95": verdict_ci_low,
            "step_ci_high_95": verdict_ci_high,
            "mean_verdict": mean_verdict,
            "dm1_response_rate": cohort.get("dm1_response_rate"),
            "dm2_response_rate": cohort.get("dm2_response_rate"),
            "dm3_response_rate": cohort.get("dm3_response_rate"),
            "dm1_posterior": cohort.get("dm1_posterior"),
            "dm2_posterior": cohort.get("dm2_posterior"),
            "dm3_posterior": cohort.get("dm3_posterior"),
        }))

    return verdicts


# ============================================================
# PR-31 — All-4-verdict persistence + REJECTED_DEFENSIVE auto-pause
# ============================================================
#
# Per plan §5 PR-31 (Round-4 D10): the four terminal experiment verdicts
# (WON / LOST / REJECTED_NULL / REJECTED_DEFENSIVE) persist to the
# experiment's ``status`` column. REJECTED_DEFENSIVE additionally opens a
# ``variant_paused_defensive`` Operator Review Queue row idempotent on
# ``experiment_id``.
#
# Wave-2-A (2026-06-01): `experiments.tsv` is the system of record. The
# Attio `experiment` object was descoped (never deployed, 404 live), so
# verdict persistence writes the status to the TSV via
# `models.experiment.upsert_experiment` rather than PATCHing Attio. The
# ``operator_review_queue`` Attio object IS live, so the
# REJECTED_DEFENSIVE escalation is preserved unchanged.
#
# The literal verdict values are uppercase per the plan; the underlying
# ``ExperimentStatus`` enum stores lowercase strings. The boundary mapping
# lives in ``_VERDICT_TO_STATUS`` below.

ExperimentVerdict = Literal["WON", "LOST", "REJECTED_NULL", "REJECTED_DEFENSIVE"]

APPLY_VERDICT_WRITER_MODULE = "workflows.learn.apply_verdict"

# evaluate_experiments emits the lowercase "inconclusive" name; PR-31
# canonicalizes it to REJECTED_NULL on the Attio side (matches the
# F-PR-6 select option). All other returned verdicts map 1:1.
_EVAL_VERDICT_TO_EXPERIMENT_VERDICT: dict[str, ExperimentVerdict] = {
    "won": "WON",
    "lost": "LOST",
    "inconclusive": "REJECTED_NULL",
    "rejected_defensive": "REJECTED_DEFENSIVE",
}

_VERDICT_TO_STATUS: dict[ExperimentVerdict, ExperimentStatus] = {
    "WON": ExperimentStatus.WON,
    "LOST": ExperimentStatus.LOST,
    "REJECTED_NULL": ExperimentStatus.REJECTED_NULL,
    "REJECTED_DEFENSIVE": ExperimentStatus.REJECTED_DEFENSIVE,
}


def to_experiment_verdict(eval_verdict: str) -> ExperimentVerdict | None:
    """Convert ``evaluate_experiments`` lowercase verdict string to the
    typed ``ExperimentVerdict`` literal accepted by ``apply_verdict``.

    Returns ``None`` for verdicts that are not terminal (currently only
    "inconclusive" is renamed; other emitted values map directly).
    Callers MUST handle ``None`` — silent skip is correct for unrecognized
    states (§0 #9: missing data → None, not a default-value fabrication).
    """
    return _EVAL_VERDICT_TO_EXPERIMENT_VERDICT.get(eval_verdict)


class ExperimentNotFoundError(RuntimeError):
    """Raised by ``apply_verdict`` when no row for a given ``experiment_id``
    exists in ``experiments.tsv``. Subclass of RuntimeError (not ValueError)
    to avoid silent swallow by broad ``except ValueError`` blocks elsewhere
    in the codebase."""

    def __init__(self, experiment_id: str) -> None:
        self.experiment_id = experiment_id
        super().__init__(
            f"Experiment {experiment_id!r} not found in experiments.tsv. "
            "Ensure the experiment cohort was registered (append_experiment) "
            "and the experiment_id is correct."
        )


def _find_experiment_record(experiment_id: str, experiments_path=None) -> "Experiment | None":
    """Return the current ``Experiment`` row for ``experiment_id`` from the TSV.

    `experiments.tsv` is the system of record (Wave-2-A). Applies the same
    last-row-wins dedup that read-side consumers use, so the returned row
    reflects the experiment's current status (e.g. a later ``rejected`` row
    supersedes the original ``running`` row). Returns ``None`` when no row
    matches.
    """
    latest: Experiment | None = None
    for e in load_experiments(experiments_path):
        if e.experiment_id == experiment_id:
            latest = e
    return latest


def apply_verdict(
    experiment_id: str,
    verdict: ExperimentVerdict,
    *,
    attio,
    writer=None,
    experiments_path=None,
    metrics: "VerdictDict | dict | None" = None,
) -> dict:
    """Persist ``Experiment.status = <verdict>`` to ``experiments.tsv``.

    `experiments.tsv` is the system of record (Wave-2-A — the Attio
    ``experiment`` object was descoped). The row is updated in place by
    ``experiment_id`` via ``upsert_experiment`` so no duplicate row is left
    behind. All four terminal verdicts (WON / LOST / REJECTED_NULL /
    REJECTED_DEFENSIVE) are persisted.

    Side effects:
        * Update the experiment's ``status`` column in ``experiments.tsv``.
        * On ``REJECTED_DEFENSIVE``: open a ``variant_paused_defensive``
          Operator Review Queue row (the ``operator_review_queue`` Attio
          object IS live) idempotent on ``experiment_id`` so the auto-pause
          is single-shot per experiment.

    Args:
        experiment_id: stable identifier (e.g. ``"exp-20260511-dm1-soften"``).
        verdict: one of the 4 terminal ``ExperimentVerdict`` literals.
        attio: an ``AttioClient`` instance — forwarded to the
            REJECTED_DEFENSIVE escalation (the live operator_review_queue).
        writer: accepted for backward-compatible call signatures; unused now
            that the status write goes to the TSV rather than via AttioWriter.
        experiments_path: optional TSV override (tests inject a tmp path).
        metrics: optional verdict dict from ``evaluate_experiments`` carrying
            the freshly-measured cohort figures (``cohort_size``,
            ``dm_response_rate``, ``baseline_rate``, ``dm{N}_response_rate``).
            When supplied these are written to the persisted terminal row so
            it reflects the measured cohort rather than the stale running-row
            placeholders (a running experiment row carries cohort_size=0).
            When omitted, the existing TSV row's metrics are carried forward.

    Returns:
        dict with keys ``experiment_id``, ``verdict``, ``status_written``,
        ``queue_opened`` (bool — True iff REJECTED_DEFENSIVE), and
        ``prior_status`` (the status read from the TSV before the write).

    Raises:
        ExperimentNotFoundError: no ``experiments.tsv`` row matches the id.
    """
    del writer  # no longer used — status write goes to experiments.tsv

    status = _VERDICT_TO_STATUS[verdict]

    record = _find_experiment_record(experiment_id, experiments_path)
    if record is None:
        raise ExperimentNotFoundError(experiment_id)

    prior_status: str | None = record.status.value

    # Ordering matters (fold-in BLOCKING-2 from silent-failure QA):
    # escalate BEFORE writing status. If escalate raises, the verdict
    # is NOT persisted — the next learn cycle re-attempts cleanly
    # (escalate is idempotent on experiment_id so the second attempt is
    # a no-op). The reverse ordering would risk a partial-completion
    # bug: status flipped to rejected_defensive in the TSV but no operator
    # queue row, leaving the operator blind to the paused defensive variant
    # (and the next learn run would see status != running and skip the
    # cohort, so the defensive variant keeps sending with no queue row).
    queue_opened = False
    if verdict == "REJECTED_DEFENSIVE":
        from workflows.escalation import escalate

        escalate(
            type="variant_paused_defensive",
            idempotency_key=experiment_id,
            payload={
                "experiment_id": experiment_id,
                "prior_status": prior_status,
                "reason": "defensive_rate_exceeded_threshold",
                "auto_paused_by": APPLY_VERDICT_WRITER_MODULE,
            },
            attio=attio,
        )
        queue_opened = True

    # Persist the verdict to the TSV (system of record). Preserve the
    # experiment's existing metadata; only the status (and completed date)
    # transition. When the caller passes the freshly-measured `metrics`
    # (the evaluate_experiments verdict dict) the persisted row reflects the
    # measured cohort; otherwise the existing TSV row's metrics carry forward.
    # update-in-place by experiment_id — no duplicate row.
    m = metrics or {}

    def _metric(key: str, fallback):
        val = m.get(key)
        return fallback if val is None else val

    updated = Experiment(
        experiment_id=record.experiment_id,
        started=record.started,
        completed=date.today().isoformat(),
        cohort_size=_metric("cohort_size", record.cohort_size),
        dm_response_rate=_metric("dm_response_rate", record.dm_response_rate),
        baseline_rate=_metric("baseline_rate", record.baseline_rate),
        status=status,
        variable=record.variable,
        description=f"auto-evaluated: {status.value}",
        dm1_response_rate=_metric("dm1_response_rate", record.dm1_response_rate),
        dm2_response_rate=_metric("dm2_response_rate", record.dm2_response_rate),
        dm3_response_rate=_metric("dm3_response_rate", record.dm3_response_rate),
    )
    upsert_experiment(updated, experiments_path)

    return {
        "experiment_id": experiment_id,
        "verdict": verdict,
        "status_written": status.value,
        "queue_opened": queue_opened,
        "prior_status": prior_status,
    }
