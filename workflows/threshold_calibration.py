"""Score-threshold ROC calibration.

Sweeps candidate pass-thresholds across the labeled LinkedIn Outreach
cohort, computes max-Youden and cost-weighted recommendations, opens
a typed recommendation queue row, and returns the result dict. Pure
Attio read + arithmetic — zero LLM cost. Never changes the live
threshold.

Sweep range 40..80 step 5. Selectors:
  - max-Youden:    argmax(TPR + TNR - 1)
  - cost-weighted: argmax(value × TPR - cost × (1 - TNR))

The script opens a `threshold_recommendation` queue row; the operator
confirms via the UI and any change to the live `quality_gate`
threshold lands as a separate code change.

# Labeling

Outcome 1 (positive) when stage ∈ {Qualified, Call Booked} OR
response_classification == "positive". A row is INCLUDED in the
cohort only when:
  - stage ∈ {Qualified, Call Booked, Not Interested}, OR
  - stage == Responded AND classification ∈ {positive, negative,
    defensive}.

Responded rows with classification in {question, neutral, None} are
EXCLUDED — they're in-flight handoffs awaiting human follow-up, not
verdicts. Including them as negatives biases the ROC toward lower
thresholds (suppresses TPR via false negatives).

# Abstain

Three abstain conditions, all loud (typed queue row):
  - `insufficient_labeled_data` — `n_labeled < ABSTAIN_THRESHOLD`.
  - `class_imbalance` — total_positives < MIN_PER_CLASS or
    total_negatives < MIN_PER_CLASS. The ROC math degenerates on a
    near-one-class cohort (all TPR = 0 or all TNR = 0).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, TypedDict

from workflows.escalation import escalate

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date as date_type

    from clients.attio import AttioClient

ABSTAIN_THRESHOLD = 50
MIN_PER_CLASS = 5

# Sweep parameters: candidate thresholds 40..80 step 5 → [40, 45, ..., 80].
THRESHOLD_START = 40
THRESHOLD_END = 80
THRESHOLD_STEP = 5

LABELED_STAGES = frozenset({"Qualified", "Call Booked", "Not Interested", "Responded"})
POSITIVE_STAGES = frozenset({"Qualified", "Call Booked"})
POSITIVE_RESPONSE_CLASSIFICATIONS = frozenset({"positive"})
# Classifications that qualify a Responded row as labeled. Question /
# neutral / None are excluded because they're in-flight handoffs, not
# verdicts.
LABELED_RESPONDED_CLASSIFICATIONS = frozenset({"positive", "negative", "defensive"})


class ROCRow(TypedDict):
    threshold: int
    tp: int
    fp: int
    fn: int
    tn: int
    tpr: float
    tnr: float
    youden: float


class _CalibrationOk(TypedDict):
    status: Literal["ok"]
    n_labeled: int
    n_dropped_invalid_score: int
    roc_table: list[ROCRow]
    max_youden: ROCRow
    cost_weighted: ROCRow
    queue_row_key: str


class _CalibrationAbstain(TypedDict):
    status: Literal["abstain"]
    n_labeled: int
    n_dropped_invalid_score: int
    reason: Literal["insufficient_labeled_data", "class_imbalance"]
    queue_row_key: str


CalibrationResult = _CalibrationOk | _CalibrationAbstain


def _candidate_thresholds() -> list[int]:
    return list(range(THRESHOLD_START, THRESHOLD_END + 1, THRESHOLD_STEP))


def _collect_labeled_pairs(
    attio: AttioClient,
    *,
    attio_query: Callable[[AttioClient], list[dict]] | None = None,
) -> tuple[list[tuple[int, int]], int]:
    """Return `([(quality_score, outcome), ...], n_dropped_invalid_score)`.

    Counts non-numeric quality_score drops separately so the operator
    can tell "0 invalid" from "we dropped 50 rows because someone wrote
    'high' to the score field." `attio_query` is injected for tests;
    production resolves through `_get_all_entries_parsed`.
    """
    if attio_query is not None:
        entries = attio_query(attio)
    else:
        from workflows.daily_check_helpers import _get_all_entries_parsed
        entries = _get_all_entries_parsed(attio)
    pairs: list[tuple[int, int]] = []
    n_dropped_invalid_score = 0
    for attrs in entries:
        stage = attrs.get("stage") or ""
        if stage not in LABELED_STAGES:
            continue
        score = attrs.get("quality_score")
        if score is None:
            continue
        # bool is an int subclass — coercion would silently turn True/
        # False into 1/0 and corrupt the cohort.
        if isinstance(score, bool):
            n_dropped_invalid_score += 1
            continue
        try:
            score_int = int(score)
        except (TypeError, ValueError):
            n_dropped_invalid_score += 1
            continue
        classification = attrs.get("response_classification") or ""
        # Responded rows require a verdict-bearing classification.
        # question / neutral / None are in-flight handoffs, not labels.
        if (
            stage == "Responded"
            and classification not in LABELED_RESPONDED_CLASSIFICATIONS
        ):
            continue
        outcome = 1 if (
            stage in POSITIVE_STAGES
            or classification in POSITIVE_RESPONSE_CLASSIFICATIONS
        ) else 0
        pairs.append((score_int, outcome))
    return pairs, n_dropped_invalid_score


def _sweep(pairs: list[tuple[int, int]]) -> list[ROCRow]:
    """Compute the ROC table — one row per candidate threshold."""
    total_positives = sum(1 for _s, o in pairs if o == 1)
    total_negatives = sum(1 for _s, o in pairs if o == 0)
    rows: list[ROCRow] = []
    for t in _candidate_thresholds():
        tp = sum(1 for s, o in pairs if s >= t and o == 1)
        fp = sum(1 for s, o in pairs if s >= t and o == 0)
        fn = total_positives - tp
        tn = total_negatives - fp
        tpr = tp / total_positives if total_positives else 0.0
        tnr = tn / total_negatives if total_negatives else 0.0
        rows.append(ROCRow(
            threshold=t,
            tp=tp, fp=fp, fn=fn, tn=tn,
            tpr=round(tpr, 4),
            tnr=round(tnr, 4),
            youden=round(tpr + tnr - 1, 4),
        ))
    return rows


def _select_max_youden(rows: list[ROCRow]) -> ROCRow:
    return max(rows, key=lambda r: r["youden"])


def _select_cost_weighted(
    rows: list[ROCRow],
    *,
    cost_per_send: float,
    value_per_positive: float,
) -> ROCRow:
    """argmax(value_per_positive * TPR - cost_per_send * (1 - TNR))."""
    def _utility(row: ROCRow) -> float:
        return (
            value_per_positive * row["tpr"]
            - cost_per_send * (1 - row["tnr"])
        )
    return max(rows, key=_utility)


def _open_abstain_row(
    attio: AttioClient,
    *,
    iso_today: str,
    reason: Literal["insufficient_labeled_data", "class_imbalance"],
    n_labeled: int,
    rationale: str,
) -> str:
    key = f"threshold_recommendation_{reason}|{iso_today}"
    escalate(
        type="configuration_decision",
        decision_key="industry_threshold_calibration",
        idempotency_key=key,
        payload={
            "decision_key": "industry_threshold_calibration",
            "question": (
                f"Threshold calibration abstained ({reason})."
            ),
            "options": [{
                "key": "wait",
                "label": "Wait for more labeled rows",
                "description": (
                    "Re-run after the labeled cohort grows. "
                    "Default on expiry: continue waiting; do "
                    "not change the live threshold."
                ),
            }],
            "recommended_option": "wait",
            "default_on_expiry": "wait",
            "rationale": rationale,
        },
        attio=attio,
    )
    return key


def calibrate_score_threshold(
    attio: AttioClient,
    *,
    cost_per_send: float,
    value_per_positive: float,
    today: date_type,
    attio_query: Callable[[AttioClient], list[dict]] | None = None,
) -> CalibrationResult:
    """Compute the ROC sweep + open a recommendation queue row.

    `today` is caller-supplied so tests can pin time and idempotency
    keys are stable. Abstains loudly on insufficient data OR class
    imbalance — a near-one-class cohort makes the ROC math degenerate.
    """
    iso_today = today.isoformat()
    pairs, n_dropped_invalid = _collect_labeled_pairs(
        attio, attio_query=attio_query,
    )
    n_labeled = len(pairs)

    if n_labeled < ABSTAIN_THRESHOLD:
        rationale = (
            f"n_labeled={n_labeled} below the abstain floor of "
            f"{ABSTAIN_THRESHOLD}; ROC sweep would produce high-"
            f"variance recommendations."
        )
        key = _open_abstain_row(
            attio, iso_today=iso_today,
            reason="insufficient_labeled_data",
            n_labeled=n_labeled, rationale=rationale,
        )
        return _CalibrationAbstain(
            status="abstain",
            n_labeled=n_labeled,
            n_dropped_invalid_score=n_dropped_invalid,
            reason="insufficient_labeled_data",
            queue_row_key=key,
        )

    total_positives = sum(1 for _s, o in pairs if o == 1)
    total_negatives = sum(1 for _s, o in pairs if o == 0)
    if total_positives < MIN_PER_CLASS or total_negatives < MIN_PER_CLASS:
        rationale = (
            f"class imbalance: total_positives={total_positives}, "
            f"total_negatives={total_negatives}, MIN_PER_CLASS="
            f"{MIN_PER_CLASS}. ROC math would pick a degenerate "
            f"threshold (max-Youden = highest TNR or lowest TPR)."
        )
        key = _open_abstain_row(
            attio, iso_today=iso_today, reason="class_imbalance",
            n_labeled=n_labeled, rationale=rationale,
        )
        return _CalibrationAbstain(
            status="abstain",
            n_labeled=n_labeled,
            n_dropped_invalid_score=n_dropped_invalid,
            reason="class_imbalance",
            queue_row_key=key,
        )

    roc_table = _sweep(pairs)
    max_youden = _select_max_youden(roc_table)
    cost_weighted = _select_cost_weighted(
        roc_table,
        cost_per_send=cost_per_send,
        value_per_positive=value_per_positive,
    )

    key = f"threshold_recommendation|{iso_today}"
    escalate(
        type="threshold_recommendation",
        idempotency_key=key,
        payload={
            "today": iso_today,
            "n_labeled": n_labeled,
            "n_dropped_invalid_score": n_dropped_invalid,
            "cost_per_send": cost_per_send,
            "value_per_positive": value_per_positive,
            "roc_table": roc_table,
            "max_youden_threshold": max_youden["threshold"],
            "max_youden_score": max_youden["youden"],
            "cost_weighted_threshold": cost_weighted["threshold"],
            "cost_weighted_tpr": cost_weighted["tpr"],
            "cost_weighted_tnr": cost_weighted["tnr"],
        },
        attio=attio,
    )
    return _CalibrationOk(
        status="ok",
        n_labeled=n_labeled,
        n_dropped_invalid_score=n_dropped_invalid,
        roc_table=roc_table,
        max_youden=max_youden,
        cost_weighted=cost_weighted,
        queue_row_key=key,
    )
