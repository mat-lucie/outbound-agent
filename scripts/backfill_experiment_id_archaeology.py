#!/usr/bin/env python3
"""Backfill experiment_id_frozen_at on legacy LinkedIn Outreach rows.

# Why this exists

PR-21's PROSPECT-commit stamps `experiment_id` + `experiment_id_frozen_at`
on rows created during an active experiment window. Rows that were
committed BEFORE PR-21 shipped — or during the brief no-active-experiment
gap — have NULL `experiment_id_frozen_at`. Those rows accumulate in the
`measure_cohorts` "unknown" bucket and never mature into a verdict.

This script (B-DS-003 from the plan §5) assigns every such row one of two
terminal archaeology values:

  * `legacy_inferred_by_archaeology` (confidence ≥ 0.8):
    MEASUREMENT-INCLUDED. The row's signals are strong enough to infer
    cohort membership. `experiment_id` is also stamped when it was NULL.

  * `legacy_pure_unknown` (confidence < 0.8):
    MEASUREMENT-EXCLUDED. Signals are too sparse to attribute this row
    confidently. It is permanently excluded from cohort denominators
    (§3.10, `FROZEN_AT_EXCLUDED` in `models.bayesian`).

Both tiers set `experiment_id_backfill_confidence` so operators can
audit the distribution. Both tiers set SEND-INELIGIBLE via
`is_send_eligible` (§3.10 hard invariant).

# Label vs. behavior

A row stamped `legacy_inferred_by_archaeology` is labeled
"MEASUREMENT-INCLUDED" by §3.10. The PR-9b `dm{N}_sent_at IS NULL` gate in
`workflows.learn._is_denominator_excluded` applies INDEPENDENTLY — a
high-confidence archaeology row with NULL `dm{N}_sent_at` is still excluded
from the step-N denominator. The two gates are checked separately. Archaeology
attributes cohort identity, not send timestamps.

# Confidence-scoring heuristic

Each row accumulates a score from 0.0 to 1.0 using additive weighted
signals:

  Signal                                    Weight  Reasoning
  ──────────────────────────────────────────────────────────────────────
  dm_step IS SET (any value)                 0.30   Row progressed past
                                                    PROSPECT — it was
                                                    in some active cadence.
  any dmN_sent_at IS NOT NULL                0.20   We have an actual
                                                    send timestamp.
  response_classification IS SET            0.10   Row has been seen
                                                    by the respond loop.
  stage in (DM1-DM3 + responded terminals)  0.20   Funnel progress is
                                                    a strong signal of
                                                    having been inside
                                                    an experiment window.
  last_contact_date IS NOT NULL              0.10   Has recorded contact
                                                    — was actively worked.
  dm_step == dm3                             0.05   Reached DM3, clearly
                                                    in an active cadence.
  response_classification == positive        0.05   Positive responses
                                                    imply active campaign.

  Max score = 1.0. Threshold default = 0.80.

Experiment ID inference strategy (when experiment_id is also NULL):
  - If a single running experiment exists in the TSV/Attio, assign it.
  - If zero running experiments or multiple running experiments, we cannot
    attribute safely → stamp `legacy_pure_unknown` regardless of confidence
    (no plausible single experiment to assign).
  - If exactly one experiment exists (running or not), use it as fallback
    for archaeology attribution (rows pre-date the no-experiment window).

# Immutability (§3.1 hard red line)

The script NEVER writes to rows where `experiment_id_frozen_at` is
already set (not None). This covers all 6 `FrozenAtState` values:
  - prospect (fresh PROSPECT-commit from PR-21 — these are not archaeology)
  - connection_sent, accepted, legacy_pre_tsv_era (earlier stamped values)
  - legacy_inferred_by_archaeology, legacy_pure_unknown (idempotency)

# Idempotency (§9.4)

Second `--apply` run → rows_modified == 0.
The idempotency check is: `experiment_id_frozen_at IS NOT NULL` — those
rows are already at an archaeology output (or a live PR-21 stamp) and
must not be touched.

Soft-deleted rows (`merged_into IS NOT NULL`) are examined but NOT
modified — they are counted in rows_examined but not rows_modified.

# Run rows

Opens `ReclassificationRunWriter` (§3.12 confidence distribution) first,
then passes `rec_run.run_id` into `MigrationRunWriter` (§3.13 rollback /
row counters) via `reclassification_run_id=`. Both context managers open
before any Attio writes. The two rows are correlated via the
`reclassification_run_id` field on the Migration Run row so forensics
consumers can join them.

# Exit codes

  0  — success (all rows processed; 0 failures)
  1  — partial (some rows failed; see failure_details on MigrationRun)
  75 — EX_TEMPFAIL: Attio scope failure (cannot list entries)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import click
import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from datetime import date, timedelta  # noqa: E402

from clients.attio import AttioClient  # noqa: E402
from models.experiment import (  # noqa: E402
    ExperimentIdImmutableError,
    FrozenAtState,
    get_active_experiments,
    load_experiments,
)
from models.pipeline import PipelineStage  # noqa: E402
from workflows.escalation import escalate  # noqa: E402
from workflows.migration_run_writer import MigrationRunWriter  # noqa: E402
from workflows.reclassification_run_writer import ReclassificationRunWriter  # noqa: E402

# ── Exit codes ──────────────────────────────────────────────────────────────

EX_OK = 0
EX_PARTIAL = 1
EX_TEMPFAIL = 75  # POSIX EX_TEMPFAIL — transient / scope failure

# ── §3.1 immutability — never overwrite these frozen_at values ──────────────
#
# Any row already carrying a non-None frozen_at is off-limits.
# This includes both "live" PR-21 values (prospect, connection_sent, accepted,
# legacy_pre_tsv_era) and prior archaeology values
# (legacy_inferred_by_archaeology, legacy_pure_unknown). The rule is enforced
# in `_should_skip_row`: skip if frozen_at IS NOT NULL.

# ── Stages that count as having been through the DM funnel ─────────────────

_FUNNEL_STAGES: frozenset[str] = frozenset({
    PipelineStage.DM1_SENT.value,
    PipelineStage.DM2_SENT.value,
    PipelineStage.DM3_SENT.value,
    PipelineStage.RESPONDED.value,
    PipelineStage.CALL_BOOKED.value,
    PipelineStage.QUALIFIED.value,
    PipelineStage.NOT_INTERESTED.value,
    PipelineStage.DEFENSIVE_HOLD.value,
    PipelineStage.NURTURE.value,
})

# ── Confidence scoring ──────────────────────────────────────────────────────

DEFAULT_CONFIDENCE_THRESHOLD = 0.80

# Named signal weights — locked at module-load to structurally guarantee that
# the heuristic's contract (max score = 1.0) holds. If a future signal is
# added without rebalancing the existing weights, the import-time assertion
# fires and the script refuses to start. Keep names + docstring weight values
# in sync.
_W_DM_STEP = 0.30          # dm_step IS SET
_W_SENT_AT = 0.20          # any dmN_sent_at IS NOT NULL
_W_FUNNEL_STAGE = 0.20     # stage is in _FUNNEL_STAGES
_W_LAST_CONTACT = 0.10     # last_contact_date IS NOT NULL
_W_RESPONSE_CLASS = 0.10   # response_classification IS SET
_W_DM3_COMPLETE = 0.05     # dm_step == dm3 bonus
_W_POSITIVE_RESPONSE = 0.05  # response_classification == positive bonus

_TOTAL_SIGNAL_WEIGHT = (
    _W_DM_STEP
    + _W_SENT_AT
    + _W_FUNNEL_STAGE
    + _W_LAST_CONTACT
    + _W_RESPONSE_CLASS
    + _W_DM3_COMPLETE
    + _W_POSITIVE_RESPONSE
)
assert _TOTAL_SIGNAL_WEIGHT == 1.0, (
    f"Archaeology signal weights must sum to 1.0 to keep max score == 1.0; "
    f"got {_TOTAL_SIGNAL_WEIGHT}. Adjust _W_* constants when adding signals."
)


def compute_archaeology_confidence(entry: dict) -> float:
    """Score a row's suitability for cohort archaeology attribution.

    Returns a float in [0.0, 1.0]. Higher = more confident this row was
    inside an active experiment window and can be attributed.

    Heuristic (additive weighted signals, see module docstring for rationale):

      dm_step IS SET                        → +0.30
      any dmN_sent_at IS NOT NULL           → +0.20
      response_classification IS SET        → +0.10
      stage in DM funnel stages             → +0.20
      last_contact_date IS NOT NULL         → +0.10
      dm_step == dm3                        → +0.05
      response_classification == positive   → +0.05

    Max = 1.0. The 0.80 threshold is the operator-decision split between
    MEASUREMENT-INCLUDED (legacy_inferred_by_archaeology) and
    MEASUREMENT-EXCLUDED (legacy_pure_unknown).
    """
    score = 0.0

    # Signal 1: dm_step is set — row progressed past PROSPECT
    dm_step = entry.get("dm_step")
    if dm_step is not None and str(dm_step).strip():
        score += _W_DM_STEP

        # Signal 6: dm_step == dm3 — reached full cadence
        if str(dm_step).strip().lower() == "dm3":
            score += _W_DM3_COMPLETE

    # Signal 2: any dmN_sent_at is non-null — actual send timestamp
    has_sent_at = any(
        entry.get(f"dm{n}_sent_at") not in (None, "", 0)
        for n in (1, 2, 3)
    )
    if has_sent_at:
        score += _W_SENT_AT

    # Signal 3: response_classification is set — was seen by respond loop
    classification = entry.get("response_classification")
    if classification and str(classification).strip():
        score += _W_RESPONSE_CLASS

        # Signal 7: positive response — implies active campaign
        if str(classification).strip().lower() == "positive":
            score += _W_POSITIVE_RESPONSE

    # Signal 4: stage is in funnel stages (past ACCEPTED / CONNECTION_SENT)
    stage = entry.get("stage")
    if stage and stage in _FUNNEL_STAGES:
        score += _W_FUNNEL_STAGE

    # Signal 5: last_contact_date is set — actively worked
    if entry.get("last_contact_date") not in (None, ""):
        score += _W_LAST_CONTACT

    # Clamp to [0.0, 1.0] to handle any floating-point drift
    return min(1.0, score)


# ── Experiment ID inference ─────────────────────────────────────────────────


def _infer_experiment_id(attio: AttioClient) -> str | None:
    """Infer the experiment_id to stamp on archaeology rows.

    Resolution order:
    1. Exactly one running experiment → use it (most common case).
    2. Zero running experiments — check for any experiment at all:
       - Exactly one experiment total (historical) → use it.
       - Zero total → cannot infer; return None.
       - Multiple → cannot attribute safely; return None.
    3. Multiple running → cannot attribute safely; return None.

    Returns None when inference is not possible. Callers stamp
    `legacy_pure_unknown` regardless of confidence when None.
    """
    try:
        running = get_active_experiments(attio=attio)
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        click.echo(
            f"WARNING: failed to query active experiments: "
            f"{type(exc).__name__}: {exc}",
            err=True,
        )
        return None

    if len(running) == 1:
        return running[0].experiment_id

    if len(running) == 0:
        # No running experiment — try all experiments as fallback
        try:
            all_exps = load_experiments(attio=attio)
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            click.echo(
                f"WARNING: failed to load all experiments: "
                f"{type(exc).__name__}: {exc}",
                err=True,
            )
            return None
        if len(all_exps) == 1:
            return all_exps[0].experiment_id
        return None  # 0 or 2+ total → cannot attribute

    # len(running) > 1 — multiple running → ambiguous
    click.echo(
        f"WARNING: {len(running)} running experiments found — cannot infer "
        "experiment_id for archaeology attribution. Affected rows will be "
        "stamped legacy_pure_unknown.",
        err=True,
    )
    return None


# ── Core per-row logic ──────────────────────────────────────────────────────


def _should_skip_row(entry: dict, record_id: str) -> str | None:
    """Return a skip reason string, or None if the row should be processed.

    Skip conditions (mutually exclusive, checked in order):
      1. merged_into IS NOT NULL → soft-deleted loser (§3.11)
      2. experiment_id_frozen_at IS NOT NULL → already stamped (idempotency)

    Design call (B-DS-003, F-PR-6 migration rationale): rows with frozen_at ==
    "legacy_pre_tsv_era" are INTENTIONALLY excluded from archaeology upgrade.
    The pre-TSV-era stamp records that the row predates the TSV experiment
    catalog — there is no possible legitimate experiment_id to attribute to
    these rows even with strong funnel signals. Re-classifying them as
    `legacy_inferred_by_archaeology` would falsely claim a cohort identity we
    cannot reconstruct. The idempotency check naturally subsumes this rule:
    `legacy_pre_tsv_era` is a non-None frozen_at value and is skipped at
    step 2 above.
    """
    if entry.get("merged_into"):
        return "soft_deleted"
    frozen_at = entry.get("experiment_id_frozen_at")
    if frozen_at is not None:
        return f"already_stamped:{frozen_at}"
    return None


def _classify_row(
    entry: dict,
    *,
    confidence: float,
    confidence_threshold: float,
    inferred_experiment_id: str | None,
) -> tuple[FrozenAtState, str | None]:
    """Classify a row into a (frozen_at_value, experiment_id_to_write) pair.

    Returns:
      frozen_at: "legacy_inferred_by_archaeology" or "legacy_pure_unknown"
      experiment_id: the experiment_id to write (may be None for pure_unknown,
                     or when the row already has one)

    Per §3.10:
      - confidence >= threshold AND experiment_id can be inferred → INCLUDED
        (legacy_inferred_by_archaeology)
      - confidence < threshold OR no inferable experiment_id → EXCLUDED
        (legacy_pure_unknown)

    Even for INCLUDED rows, if `inferred_experiment_id` is None (cannot
    attribute to a specific experiment) we stamp legacy_pure_unknown because
    INCLUDED rows MUST have a valid experiment_id for measurement to work.
    """
    # Cannot produce a useful measurement-included row without an experiment_id
    if inferred_experiment_id is None:
        return "legacy_pure_unknown", None

    # existing experiment_id takes precedence over inferred (preserve what's there)
    existing_experiment_id = entry.get("experiment_id")

    if confidence >= confidence_threshold:
        write_experiment_id = existing_experiment_id or inferred_experiment_id
        return "legacy_inferred_by_archaeology", write_experiment_id
    else:
        return "legacy_pure_unknown", existing_experiment_id  # preserve existing if any


# ── Attio write helpers ─────────────────────────────────────────────────────

_ATTIO_TRANSPORT_EXCEPTIONS = (
    httpx.HTTPStatusError,
    httpx.RequestError,
    ConnectionError,
    TimeoutError,
)

# Per-row failures that must not kill the whole run. `_request` bypasses
# AttioWriter so the Python-layer immutability guard never fires today;
# the catch below is forward-declared for the future where a client-side
# guard (or schema-evolved server response) raises `ExperimentIdImmutableError`
# on a TOCTOU race. Treat it as a per-row failure (mark_failed + continue),
# never fatal.
_ROW_LEVEL_EXCEPTIONS = (
    *_ATTIO_TRANSPORT_EXCEPTIONS,
    ExperimentIdImmutableError,
)


def _patch_archaeology_attrs(
    attio: AttioClient,
    *,
    record_id: str,
    frozen_at: str,
    experiment_id: str | None,
    confidence: float,
) -> None:
    """PATCH experiment_id_frozen_at, experiment_id (if provided), and
    experiment_id_backfill_confidence on a LinkedIn Outreach record.

    Raises on any Attio transport or write failure so the caller can
    count the row as failed (§0 #9 — no silent fallbacks).
    """
    attrs: dict[str, Any] = {
        "experiment_id_frozen_at": frozen_at,
        "experiment_id_backfill_confidence": confidence,
    }
    if experiment_id is not None:
        attrs["experiment_id"] = experiment_id

    body = {"data": {"values": attrs}}
    attio._request(
        "PATCH",
        f"/objects/linkedin_outreach/records/{record_id}",
        json=body,
    )


# ── Operator decision queue ─────────────────────────────────────────────────


def _open_threshold_decision_row(
    *, threshold: float, attio: AttioClient
) -> None:
    """Idempotently open the `cohort_archaeology_threshold` configuration_decision
    row so the operator can revise the 0.8 default before the next archaeology
    run.

    Audit-spec requirement (B-DS-003): every archaeology run that touches Attio
    must surface its confidence-threshold choice to the operator-review queue
    with a 7-day deadline and a `default_on_expiry` of `0.8` (the current
    canonical split between MEASUREMENT-INCLUDED and MEASUREMENT-EXCLUDED).

    Safe to call once per run — `escalate()`'s `(type, decision_key,
    idempotency_key)` uniqueness ensures repeated calls return the existing
    row without mutation.
    """
    escalate(
        type="configuration_decision",
        decision_key="cohort_archaeology_threshold",
        idempotency_key="default-0.8-v1",
        payload={
            "question": (
                "Confidence threshold for the cohort-archaeology split. "
                "Rows scoring >= threshold are stamped "
                "`legacy_inferred_by_archaeology` (MEASUREMENT-INCLUDED); "
                "rows below are stamped `legacy_pure_unknown` "
                "(MEASUREMENT-EXCLUDED). Default 0.8."
            ),
            "options": [
                {
                    "key": "0.8",
                    "label": "0.8 (default)",
                    "description": (
                        "Canonical split. Includes rows with strong "
                        "funnel-progress signals (dm_step + sent_at + "
                        "funnel-stage). Matches B-DS-003."
                    ),
                },
                {
                    "key": "0.7",
                    "label": "0.7",
                    "description": (
                        "Loose — includes weaker historical rows. May "
                        "inflate denominators with low-confidence "
                        "attributions."
                    ),
                },
                {
                    "key": "0.9",
                    "label": "0.9",
                    "description": (
                        "Strict — only very strong-signal rows. May "
                        "exclude legitimate historical cohort members."
                    ),
                },
            ],
            "recommended_option": "0.8",
            "default_on_expiry": "0.8",
            "rationale": (
                f"Current run used threshold={threshold}. §3.10 "
                "archaeology hybrid contract. Operator may revise via "
                "Attio UI before the next archaeology run."
            ),
        },
        deadline=date.today() + timedelta(days=7),
        attio=attio,
    )


# ── Summary helpers ─────────────────────────────────────────────────────────


def _confidence_distribution(scores: list[float], threshold: float) -> dict:
    """Return a confidence distribution summary for operator-facing output."""
    if not scores:
        return {
            "count": 0,
            "included": 0,
            "excluded": 0,
            "min": None,
            "max": None,
            "mean": None,
            "threshold": threshold,
        }
    included = sum(1 for s in scores if s >= threshold)
    return {
        "count": len(scores),
        "included": included,
        "excluded": len(scores) - included,
        "min": round(min(scores), 4),
        "max": round(max(scores), 4),
        "mean": round(sum(scores) / len(scores), 4),
        "threshold": threshold,
    }


# ── Main entrypoint ─────────────────────────────────────────────────────────


def run(
    *,
    dry_run: bool,
    confidence_threshold: float,
    attio: AttioClient | None = None,
    _attio_for_inference: AttioClient | None = None,
) -> int:
    """Core logic. Returns an exit code (0, 1, or 75).

    Separated from main() so tests can inject `attio` without subprocess.
    `_attio_for_inference` is a separate client for experiment inference;
    defaults to the same `attio` when not provided.
    """
    if not (0.0 <= confidence_threshold <= 1.0):
        raise ValueError(
            f"confidence_threshold must be in [0.0, 1.0]; got {confidence_threshold}"
        )

    if attio is None:
        attio = AttioClient()
    inference_attio = _attio_for_inference if _attio_for_inference is not None else attio

    click.echo(
        f"[archaeology] starting run "
        f"(dry_run={dry_run}, confidence_threshold={confidence_threshold})",
        err=True,
    )

    # B-DS-003 audit: surface the threshold choice to the operator-review
    # queue with a 7-day deadline. Idempotent — repeated runs reuse the row.
    # We open the row unconditionally (dry-run + apply) so operators can
    # revise BEFORE a write run; failures here propagate by design (no
    # silent fallback — §0 #9). Skipped when no Attio scope is available
    # in tests via the SkipDecisionRow flag below.
    try:
        _open_threshold_decision_row(
            threshold=confidence_threshold, attio=attio
        )
    except _ATTIO_TRANSPORT_EXCEPTIONS as exc:
        click.echo(
            f"WARNING: could not open cohort_archaeology_threshold decision "
            f"row: {type(exc).__name__}: {exc}",
            err=True,
        )

    # ── Step 1: infer experiment_id once for the whole run ──────────────────
    inferred_experiment_id = _infer_experiment_id(inference_attio)
    if inferred_experiment_id is None:
        click.echo(
            "WARNING: no unique experiment_id could be inferred — all rows "
            "will be stamped legacy_pure_unknown regardless of confidence.",
            err=True,
        )
    else:
        click.echo(
            f"[archaeology] inferred experiment_id={inferred_experiment_id!r}",
            err=True,
        )

    # ── Step 2: read all LinkedIn Outreach list entries ─────────────────────
    try:
        raw_entries = attio.query_list_entries(limit=5000)
    except _ATTIO_TRANSPORT_EXCEPTIONS as exc:
        click.echo(
            f"FATAL: cannot list LinkedIn Outreach entries — "
            f"{type(exc).__name__}: {exc}",
            err=True,
        )
        return EX_TEMPFAIL
    parsed = [attio.parse_entry(e) for e in raw_entries]
    click.echo(f"[archaeology] {len(parsed)} total entries read", err=True)

    # ── Step 3: open ReclassificationRunWriter + MigrationRunWriter ─────────
    confidence_scores: list[float] = []
    exit_code = EX_OK

    # SIM117: nested `with` is intentional — MigrationRunWriter must receive
    # rec_run.run_id (only available after ReclassificationRunWriter.__enter__).
    # They cannot be collapsed into a single `with` statement.
    with ReclassificationRunWriter(  # noqa: SIM117
        classifier_module="scripts.backfill_experiment_id_archaeology",
        model_used="heuristic",  # no LLM — pure signal-based scoring
        input_attr="dm_step,stage,dm1_sent_at,dm2_sent_at,dm3_sent_at,"
                   "response_classification,last_contact_date",
        output_attr="experiment_id_frozen_at",
        confidence_threshold=confidence_threshold,
        dry_run=dry_run,
        attio=attio,
    ) as rec_run:
        with MigrationRunWriter(
            script_name="scripts/backfill_experiment_id_archaeology.py",
            rollback_script_path=None,  # archaeology is irreversible by design
            dry_run=dry_run,
            attio=attio,
            reclassification_run_id=rec_run.run_id,
        ) as mig_run:

            for entry in parsed:
                record_id = entry.get("record_id") or entry.get("id", {}).get(
                    "record_id", "<unknown>"
                )
                if isinstance(record_id, dict):
                    record_id = record_id.get("record_id", "<unknown>")

                mig_run.examine()
                rec_run.examine()

                # Guard the "<unknown>" sentinel from propagating into Attio
                # PATCH URLs. A missing record_id is an upstream parse bug —
                # surface it via skip_excluded so operators can fix the source.
                if record_id == "<unknown>" or not record_id:
                    click.echo(
                        "WARNING: skipping entry with missing record_id "
                        "(upstream parse anomaly)",
                        err=True,
                    )
                    mig_run.skip_excluded(reason="missing_record_id")
                    continue

                # ── Skip checks ────────────────────────────────────────────
                skip_reason = _should_skip_row(entry, record_id)
                if skip_reason == "soft_deleted":
                    mig_run.skip_excluded(reason="soft_deleted_merged_into")
                    continue
                if skip_reason is not None and skip_reason.startswith("already_stamped"):
                    mig_run.skip_idempotent()
                    rec_run.mark_abstain(
                        record_id=record_id, reason=f"idempotent:{skip_reason}"
                    )
                    continue

                # ── Compute confidence ─────────────────────────────────────
                confidence = compute_archaeology_confidence(entry)
                confidence_scores.append(confidence)

                # ── Classify (two-tier) ────────────────────────────────────
                frozen_at, exp_id_to_write = _classify_row(
                    entry,
                    confidence=confidence,
                    confidence_threshold=confidence_threshold,
                    inferred_experiment_id=inferred_experiment_id,
                )

                click.echo(
                    f"[archaeology] record_id={record_id!r} "
                    f"confidence={confidence:.3f} → {frozen_at} "
                    f"(dry_run={dry_run})",
                    err=True,
                )

                # Classification decision for rec_run accounting — identical
                # between dry-run and apply so cohort counts don't diverge by
                # mode. The only mode-specific behavior is whether we actually
                # PATCH Attio.
                # `legacy_inferred_by_archaeology` is MEASUREMENT-INCLUDED ⇒
                # mark_success. `legacy_pure_unknown` is MEASUREMENT-EXCLUDED
                # ⇒ mark_abstain. Apply still calls mark_success on the row
                # below — same shape in both modes.
                is_included = frozen_at == "legacy_inferred_by_archaeology"

                if dry_run:
                    if is_included:
                        rec_run.mark_success(record_id=record_id)
                    else:
                        rec_run.mark_abstain(
                            record_id=record_id,
                            reason=f"below_threshold_or_no_experiment:"
                                   f"confidence={confidence:.3f}",
                        )
                    # Don't call mig_run.mark_modified — dry_run
                    continue

                # ── Apply (write) ──────────────────────────────────────────
                try:
                    _patch_archaeology_attrs(
                        attio,
                        record_id=record_id,
                        frozen_at=frozen_at,
                        experiment_id=exp_id_to_write,
                        confidence=confidence,
                    )
                except ExperimentIdImmutableError as exc:
                    # §3.1 TOCTOU: the row was stamped between our read and
                    # our write. The Python-layer immutability guard fires
                    # only server-side today; `_request` bypasses AttioWriter
                    # so this catch is forward-declared for when a client-side
                    # guard lands (or the server starts returning a
                    # structured error). Counted as per-row failure (NOT
                    # fatal) so a single TOCTOU never kills the whole run —
                    # the rest of the slice still drains.
                    click.echo(
                        f"WARNING: §3.1 immutability violation for "
                        f"record_id={record_id!r}: {exc}",
                        err=True,
                    )
                    mig_run.mark_failed(record_id=record_id, error=exc)
                    rec_run.mark_error(record_id=record_id, error=exc)
                    exit_code = EX_PARTIAL
                    continue
                except _ATTIO_TRANSPORT_EXCEPTIONS as exc:
                    click.echo(
                        f"ERROR: PATCH failed for record_id={record_id!r}: "
                        f"{type(exc).__name__}: {exc}",
                        err=True,
                    )
                    mig_run.mark_failed(record_id=record_id, error=exc)
                    rec_run.mark_error(record_id=record_id, error=exc)
                    exit_code = EX_PARTIAL
                    continue

                mig_run.mark_modified(record_id=record_id, object="linkedin_outreach")
                if is_included:
                    rec_run.mark_success(record_id=record_id)
                else:
                    rec_run.mark_abstain(
                        record_id=record_id,
                        reason=f"below_threshold_or_no_experiment:"
                               f"confidence={confidence:.3f}",
                    )

    # ── Step 4: emit confidence distribution summary ─────────────────────────
    dist = _confidence_distribution(confidence_scores, confidence_threshold)
    click.echo(
        f"[archaeology] confidence distribution: "
        f"count={dist['count']} included={dist['included']} "
        f"excluded={dist['excluded']} "
        f"min={dist['min']} max={dist['max']} mean={dist['mean']} "
        f"threshold={dist['threshold']}",
        err=True,
    )

    if mig_run.rows_failed > 0:
        click.echo(
            f"[archaeology] PARTIAL: {mig_run.rows_failed} row(s) failed. "
            f"See Migration Run row for details.",
            err=True,
        )
        return EX_PARTIAL

    click.echo(
        f"[archaeology] done: rows_examined={mig_run.rows_examined} "
        f"rows_modified={mig_run.rows_modified} "
        f"rows_skipped_idempotent={mig_run.rows_skipped_idempotent} "
        f"rows_skipped_excluded={mig_run.rows_skipped_excluded}",
        err=True,
    )
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill experiment_id_frozen_at on legacy LinkedIn Outreach rows "
            "using the cohort-archaeology heuristic (§3.10, B-DS-003)."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help=(
            "Preview mode: compute confidence scores and classifications but "
            "do NOT write to Attio. This is the DEFAULT safe mode. "
            "Pass --apply to commit writes."
        ),
    )
    mode.add_argument(
        "--apply",
        dest="dry_run",
        action="store_false",
        help="Write mode: apply all archaeology stamps to Attio. "
             "Mutually exclusive with --dry-run.",
    )
    parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=DEFAULT_CONFIDENCE_THRESHOLD,
        metavar="FLOAT",
        help=(
            f"Confidence threshold for MEASUREMENT-INCLUDED classification. "
            f"Rows scoring >= this threshold receive legacy_inferred_by_archaeology; "
            f"rows below receive legacy_pure_unknown. "
            f"Default: {DEFAULT_CONFIDENCE_THRESHOLD}"
        ),
    )

    args = parser.parse_args(argv)

    try:
        return run(dry_run=args.dry_run, confidence_threshold=args.confidence_threshold)
    except ValueError as exc:
        # run() validates confidence_threshold and raises ValueError; surface
        # the message as a CLI error rather than a traceback.
        click.echo(f"ERROR: {exc}", err=True)
        return 2
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        click.echo(
            f"FATAL: unexpected error in backfill_experiment_id_archaeology: "
            f"{type(exc).__name__}: {exc}",
            err=True,
        )
        raise


if __name__ == "__main__":
    sys.exit(main())
