"""PR-39 — Per-ICP cadence + post-DM3 NURTURE routing.

The `CADENCE_BY_LANE` dict defines how long (in business days) a prospect
sits in NURTURE after DM3 before becoming re-engagement eligible.
Lane-specific defaults reflect the §3.18 cadence policy:

  - enterprise → 90 business days (ICP 1; high-value, low-pressure)
  - target     → 60 business days (ICP 2 target_company_mode)
  - legacy     → 45 business days (ICP 2 default; tighter feedback loop)

Business days (skipping Sat/Sun via ``models.business_calendar.add_business_days``)
match the repo convention used by every other Attio date attribute —
``invite_eligible_after`` in workflows.weekly_prospect is the directly
analogous gate.

The four signals that NULL ``nurture_re_eligible_at`` (belt-and-suspenders
per Round-3 M9 — protects §3.1 "hardest red line, never re-contact a 'no'"):

  1. ``suppress_re_engagement`` is True
  2. ``response_classification`` is one of the negative labels emitted by
     ``workflows.response_classifier`` ({negative, defensive})
  3. ``stage`` is NOT_INTERESTED
  4. ``stage`` is DEFENSIVE_HOLD

The negative-label set is pinned to
``workflows.response_classifier.VALID_CLASSIFICATIONS`` — adding a token
that the classifier never emits would be a phantom signal that gives a
false sense of belt-and-suspenders coverage while contributing zero real
suppression. Mirror exactly what the upstream writer can actually produce.

ANY of these → the entry is permanently ineligible for re-engagement and
``compute_nurture_re_eligible_at`` returns None. Callers (the backfill
script, the post-DM3 routing path) check the None sentinel and skip the
write — never write a forward-eligibility timestamp that would re-arm
the §3.18 four-gate eligibility check on a "no" prospect.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, cast

from clients.outreach_config import load_outreach_config
from models.business_calendar import add_business_days
from models.pipeline import PipelineStage

CadenceLane = Literal["enterprise", "target", "legacy"]

# Lane → cooldown in BUSINESS DAYS (Sat/Sun skipped via add_business_days).
# Sourced from config/outreach.yaml (cadence.nurture_business_days); longer
# NURTURE = less re-engagement noise on prospects who didn't reply. The loader
# validates the exact {enterprise, target, legacy} key set, so the cast is safe.
CADENCE_BY_LANE: dict[CadenceLane, int] = cast(
    "dict[CadenceLane, int]", dict(load_outreach_config().nurture_business_days)
)

# Lane-string promotion: the legacy `scoring_lane` strings (set in
# workflows.quality_gate.score_prospect) map to the typed cadence lanes.
# `cadence_lane` is the new attribute that PR-39 stamps at PROSPECT-commit;
# this dict promotes the legacy free-form string to the typed enum.
SCORING_LANE_TO_CADENCE_LANE: dict[str, CadenceLane] = {
    "enterprise_mode": "enterprise",
    "target_company_mode": "target",
    "legacy": "legacy",
}

# Re-engagement suppression — ANY truthy trigger blocks the
# nurture_re_eligible_at write. Pinned to the labels that
# ``workflows.response_classifier`` actually emits (VALID_CLASSIFICATIONS
# minus the positive/neutral/question buckets). Aligns with
# ``workflows.cross_channel_suppression.NEGATIVE_RESPONSE_CLASSIFICATIONS``
# so the §3.1 hard-red-line predicate stays consistent across both writers.
NEGATIVE_RESPONSE_CLASSIFICATIONS: frozenset[str] = frozenset({
    "negative", "defensive",
})
SUPPRESSED_STAGES: frozenset[str] = frozenset({
    PipelineStage.NOT_INTERESTED.value,
    PipelineStage.DEFENSIVE_HOLD.value,
})

# Per-signal suppression reason codes. ``classify_suppression`` returns
# one of these so callers (counters / logs) can attribute which signal
# tripped — the OR-merge doctrine still applies but observability lives
# on per-signal counts so a future regression in one signal can be
# detected by the others still firing.
SuppressionReason = Literal[
    "flag",                       # suppress_re_engagement is True
    "classification",             # response_classification is negative/defensive
    "stage_not_interested",
    "stage_defensive_hold",
]


def derive_cadence_lane(scoring_lane: str | None) -> CadenceLane:
    """Promote a free-form ``scoring_lane`` string to the typed cadence lane.

    Unknown / missing lanes default to ``"enterprise"`` — the most
    conservative cooldown (longest NURTURE window). Defaulting to
    ``"legacy"`` (the shortest cooldown) would give unstamped / unknown
    rows the most-aggressive re-engagement schedule, which is the inverse
    of the §3.1 fail-safe-by-default posture.
    """
    if not scoring_lane:
        return "enterprise"
    return SCORING_LANE_TO_CADENCE_LANE.get(scoring_lane, "enterprise")


def classify_suppression(entry_attrs: dict) -> SuppressionReason | None:
    """Return which §3.1 signal (if any) suppresses this entry.

    Order is deterministic so re-runs map to the same reason: flag →
    classification → stage. The OR-merge doctrine still holds — any
    signal alone is sufficient — but callers (counters, audit logs)
    get per-signal attribution for observability.
    """
    if entry_attrs.get("suppress_re_engagement"):
        return "flag"
    if entry_attrs.get("response_classification") in NEGATIVE_RESPONSE_CLASSIFICATIONS:
        return "classification"
    stage = entry_attrs.get("stage")
    if stage == PipelineStage.NOT_INTERESTED.value:
        return "stage_not_interested"
    if stage == PipelineStage.DEFENSIVE_HOLD.value:
        return "stage_defensive_hold"
    return None


def is_re_engagement_suppressed(entry_attrs: dict) -> bool:
    """Return True when ANY of the 4 §3.1 hard-red-line signals fire.

    Thin convenience wrapper over ``classify_suppression``; callers that
    only need the boolean use this, callers that want the per-signal
    reason call ``classify_suppression`` directly.
    """
    return classify_suppression(entry_attrs) is not None


def compute_nurture_re_eligible_at(
    *,
    dm3_sent_at: date | str | None,
    cadence_lane: CadenceLane,
    entry_attrs: dict,
) -> date | None:
    """Decide when (or whether) an entry becomes nurture re-eligible.

    Returns the target date (``dm3_sent_at + CADENCE_BY_LANE[lane]`` BUSINESS
    days, skipping Sat/Sun) when the entry is eligible for re-engagement.
    Returns ``None`` when:

      - ``dm3_sent_at`` is missing (cannot anchor the cadence)
      - ``is_re_engagement_suppressed(entry_attrs)`` is True (§3.1)

    Callers MUST treat None as "do not write nurture_re_eligible_at" —
    NOT as "write today's date" or "write a far-future sentinel". The
    None sentinel keeps the four-gate eligibility check in
    ``models.pipeline.is_send_eligible`` from re-arming a no-prospect.
    """
    if is_re_engagement_suppressed(entry_attrs):
        return None

    if dm3_sent_at is None:
        return None

    anchor = dm3_sent_at if isinstance(dm3_sent_at, date) else _parse_date(dm3_sent_at)
    if anchor is None:
        return None

    days = CADENCE_BY_LANE.get(cadence_lane)
    if days is None:
        # Unknown lane — defensive default. The validator on the
        # cadence_lane select attribute should prevent this in practice
        # but if a legacy entry slipped through we use the conservative
        # ENTERPRISE floor (longest cooldown, fail-safe direction).
        days = CADENCE_BY_LANE["enterprise"]

    return add_business_days(anchor, days)


def _parse_date(raw: str | None) -> date | None:
    """Parse Attio date / datetime strings. Returns None on malformed input
    rather than raising — a corrupted source row should not cascade into
    a daily-check crash; the suppression-class entries are caught by the
    is_re_engagement_suppressed gate before this anyway."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        # Strip the time portion if present (Attio datetime fields).
        return date.fromisoformat(raw.split("T", 1)[0])
    except ValueError:
        return None
