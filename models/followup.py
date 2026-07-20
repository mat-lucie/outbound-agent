"""Follow-up Radar policy: warm-account staleness lanes, thresholds, ranking.

Pure logic — no I/O, fully unit-testable. The Python engine
(``workflows.followup_radar``) computes these verdicts from Attio-resident
data (list-entry stage + ``last_contact_date`` + ``response_received_at``, deal
stage + age). The skill layer (``/sales-daily`` Phase C) *refines* them with
MCP-only signals it can see but Python cannot — Gmail thread direction
(dropped-ball vs nudge), Granola call transcripts (recap owed, unfulfilled
commitment), and deferral callback dates. See the sales-daily SKILL.md Phase C
section for that split.

Thresholds are deliberately widened for enterprise-LATAM sales cycles: a plant
manager who goes quiet for a few days is normal, so nagging at day 1-2 would
train the operator to ignore the digest. The failure mode of a follow-up tool
is not a missed account — it is losing operator trust by crying wolf.

Provenance: ported from upstream Follow-up Radar (PR-211, WAITING lane PR-247).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from models.business_calendar import business_days_between
from models.pipeline import DealStage, PipelineStage

if TYPE_CHECKING:
    from datetime import date


class WarmLane(Enum):
    """Digest lane. PARTNER items are partner-intro follow-ups — highest stakes
    (a dropped intro burns the partner's credibility, not just the prospect), so
    they render in their own section ABOVE Owed. OWED items are near-certain
    actions (do them); NUDGE items are judgment calls (sometimes waiting is
    right). Shown as separate sections so a near-certain task never gets buried
    under speculative nudges."""

    PARTNER = "partner"
    OWED = "owed"
    WAITING = "waiting"
    NUDGE = "nudge"


class FollowupReason(Enum):
    """The Attio-computable subset of warm-stale reasons.

    Richer reasons — recap_owed, unfulfilled_commitment, callback_due,
    proposal_stall — require call transcripts / Gmail threads that only the
    skill layer can read via MCP, and are assigned there. The Python engine
    emits the coarse reason below; the skill layer may promote it (e.g.
    RESPONDED_NO_NEXT_STEP → recap_owed when Granola shows a recent call).
    """

    CALLBACK_DUE = "callback_due"
    AWAITING_REPLY = "awaiting_reply"
    CALL_BOOKED_STALE = "call_booked_stale"
    PARTNER_INTRO_UNWORKED = "partner_intro_unworked"
    DEAL_LEAD_UNWORKED = "deal_lead_unworked"
    RESPONDED_NO_NEXT_STEP = "responded_no_next_step"
    QUALIFIED_STALE = "qualified_stale"
    DEAL_IN_PROGRESS_STALE = "deal_in_progress_stale"
    NURTURE_POSITIVE_STALE = "nurture_positive_stale"


@dataclass(frozen=True)
class ReasonPolicy:
    """Per-reason staleness + ranking policy.

    ``heat`` is the base urgency weight (higher = hotter). ``threshold_days``
    is how long silence must run before the account surfaces; ``business_days``
    picks Mon-Fri counting vs raw calendar days (long deal-age windows use
    calendar days — a 10-day-old deal is 10 days old regardless of weekends).
    ``label`` is the scannable "why" shown in the digest.
    """

    lane: WarmLane
    heat: int
    threshold_days: int
    business_days: bool
    label: str


# Enterprise-LATAM-tuned defaults. Tune empirically off the discard-rate the
# weekly report will surface (see plan feedback loop) — do not guess twice.
POLICY: dict[FollowupReason, ReasonPolicy] = {
    # A promised callback ("contáctame en Q3") that has come due — the highest
    # heat and threshold 0 (it's due the day it arrives, no silence needed).
    FollowupReason.CALLBACK_DUE: ReasonPolicy(
        WarmLane.OWED, heat=7, threshold_days=0, business_days=False,
        label="callback due (they asked to reconnect now)",
    ),
    # Sent-without-answer (WAITING lane). State-derived from
    # awaiting_reply_since, never stage-derived — deliberately NOT in
    # WARM_ENTRY_REASONS/DEAL_STAGE_REASONS. The threshold is a DELIBERATE
    # 7 calendar days for post-send silence (not the 3-business-day booked-call
    # cadence): nudging a proposal recipient at day 3 reads as pressure, and
    # calendar days are right because a send ages over weekends too.
    FollowupReason.AWAITING_REPLY: ReasonPolicy(
        WarmLane.WAITING, heat=4, threshold_days=7, business_days=False,
        label="you sent, no reply",
    ),
    FollowupReason.CALL_BOOKED_STALE: ReasonPolicy(
        WarmLane.OWED, heat=6, threshold_days=3, business_days=True,
        label="call booked, gone quiet",
    ),
    FollowupReason.PARTNER_INTRO_UNWORKED: ReasonPolicy(
        WarmLane.PARTNER, heat=5, threshold_days=3, business_days=True,
        label="partner intro not worked",
    ),
    FollowupReason.DEAL_LEAD_UNWORKED: ReasonPolicy(
        WarmLane.OWED, heat=5, threshold_days=3, business_days=True,
        label="inbound/lead not worked",
    ),
    FollowupReason.RESPONDED_NO_NEXT_STEP: ReasonPolicy(
        WarmLane.NUDGE, heat=4, threshold_days=3, business_days=True,
        label="replied, no next step",
    ),
    FollowupReason.QUALIFIED_STALE: ReasonPolicy(
        WarmLane.NUDGE, heat=4, threshold_days=5, business_days=True,
        label="qualified, no movement",
    ),
    FollowupReason.DEAL_IN_PROGRESS_STALE: ReasonPolicy(
        WarmLane.NUDGE, heat=3, threshold_days=10, business_days=False,
        label="deal in progress, no touch",
    ),
    FollowupReason.NURTURE_POSITIVE_STALE: ReasonPolicy(
        WarmLane.NUDGE, heat=2, threshold_days=7, business_days=True,
        label="positive nurture, cooling",
    ),
}

# LinkedIn Outreach stage (title) → reason for the warm-entry path.
# NURTURE is only warm when the prior reply was positive (gated in the engine);
# a plain post-DM3 NURTURE row is cold, not warm.
WARM_ENTRY_REASONS: dict[str, FollowupReason] = {
    PipelineStage.CALL_BOOKED.value: FollowupReason.CALL_BOOKED_STALE,
    PipelineStage.QUALIFIED.value: FollowupReason.QUALIFIED_STALE,
    PipelineStage.RESPONDED.value: FollowupReason.RESPONDED_NO_NEXT_STEP,
    PipelineStage.PARTNER_INTRO.value: FollowupReason.PARTNER_INTRO_UNWORKED,
    PipelineStage.NURTURE.value: FollowupReason.NURTURE_POSITIVE_STALE,
}

# Deal stage (title) → reason. Only working stages; DealStage.LOST is terminal.
# Reuse the DealStage enum (not bare literals) so a stage rename stays coupled
# to the same source weekly_report.py reads from.
DEAL_STAGE_REASONS: dict[str, FollowupReason] = {
    DealStage.LEAD.value: FollowupReason.DEAL_LEAD_UNWORKED,
    DealStage.IN_PROGRESS.value: FollowupReason.DEAL_IN_PROGRESS_STALE,
}

# Response classifications that keep a NURTURE row "warm" (worth re-engaging).
# negative/defensive/hard_no are handled by the suppression set, not here.
_POSITIVE_CLASSIFICATIONS: frozenset[str] = frozenset({"positive", "question"})

# Urgency: silence past the threshold can multiply heat by at most this much,
# so a very old low-heat item can't outrank a fresh high-heat one indefinitely.
_MAX_OVERDUE_RATIO = 3.0

# Don't re-draft an account within this many calendar days of the last draft
# (unless real activity advanced past the draft). Past this, an un-actioned
# draft re-surfaces as an escalation rather than staying silently suppressed.
DRAFT_COOLDOWN_DAYS = 5

# Hard ceiling on WAITING-lane nudges per waiting cycle. The 5-day cooldown
# only PACES nagging; this is what STOPS it — at the ceiling the account
# renders as a terminal "handed to you" count and never auto-drafts again
# (a silent prospect drip-nagged forever is prospect harassment, the
# prospect-perception QA lens). Enforced in workflows.followup_state
# (increment raises at the ceiling) AND read-side in the radar.
WAITING_MAX_NUDGES = 2


def days_silent(last_touch: date, today: date, *, business: bool) -> int:
    """Days of silence since ``last_touch`` (Mon-Fri or raw calendar)."""
    if business:
        return business_days_between(last_touch, today)
    return max(0, (today - last_touch).days)


def is_stale(last_touch: date, today: date, policy: ReasonPolicy) -> bool:
    """True if silence has reached the reason's threshold."""
    return days_silent(last_touch, today, business=policy.business_days) >= policy.threshold_days


def urgency_score(
    heat: int,
    silent_days: int,
    threshold_days: int,
    value_mult: float = 1.0,
) -> float:
    """Rank score = heat × (1 + overdue ratio) × value multiplier.

    ``overdue ratio`` is how far past the threshold the silence has run,
    normalized by the threshold and capped at ``_MAX_OVERDUE_RATIO`` so an
    ancient low-value item can't crowd out a fresh hot one.
    """
    overdue = max(0, silent_days - threshold_days)
    overdue_ratio = min(overdue / threshold_days, _MAX_OVERDUE_RATIO) if threshold_days else 0.0
    return round(heat * (1.0 + overdue_ratio) * value_mult, 3)


def is_positive_nurture(response_classification: str | None) -> bool:
    """Whether a NURTURE row is warm enough to re-engage."""
    return response_classification in _POSITIVE_CLASSIFICATIONS
