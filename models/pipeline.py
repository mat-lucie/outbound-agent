"""Pipeline stages and transition rules for LinkedIn Outreach.

This module is the canonical source for `STAGE_RANK`, the per-stage funnel rank
used to compare progress across the outreach pipeline. F-PR-1 consolidated three
duplicate rank tables (in `clients/attio.py`, `workflows/daily_check.py`, and
`workflows/detect_responses.py`) into the single definition below. The CI guard
in `tests/test_no_duplicate_stage_rank.py` asserts those three definitions don't
return.
"""

from datetime import date
from enum import Enum
from typing import Literal


class PipelineStage(Enum):
    """Pipeline stages matching the Attio 'LinkedIn Outreach' list.

    PARTNER_INTRO is a manual-only stage for prospects coming in via warm
    partner referrals (e.g., an investor or advisor introducing the product to
    someone in their network). Records in this stage are intentionally
    skipped by the automated daily_check workflow — partners do the
    outreach, and the record is moved forward manually when the partner
    reports back.

    NURTURE and DEFENSIVE_HOLD are post-DM3 terminal-ish stages introduced
    by F-PR-1 to support later PRs:

    - DEFENSIVE_HOLD (§3.6): defensive replies route here instead of RESPONDED
      so the funnel doesn't conflate "wants to talk later" with "actively
      objecting". The §3.6 backfill (a later PR) re-routes existing
      RESPONDED rows where defensive_score >= 0.6.

    - NURTURE (PR-39): post-DM3 prospects who didn't reply land here as a
      cooldown buffer; PR-39 introduces the re-engagement cadence policy.
      NURTURE → DM1_SENT is the ONLY rank-monotonicity-exempt transition,
      controlled by `NURTURE_REENGAGEMENT_ALLOWED` and the §3.18 four-gate
      check. F-PR-1 declares the enum + allowlist; PR-39 wires the cadence.

    - UNREACHABLE (Wave-2-A): a prospect LinkedIn will not let us reach via
      the automated channel — an InMail-required DM (PB returns "InMail
      required" instead of delivering) or an Out-of-Network invite target.
      It is an *undeliverability* terminal, NOT a rejection: the prospect
      never declined, we simply can't message them. Kept distinct from
      NOT_INTERESTED so it does not pollute learning-loop / response metrics
      and so the operator can tell "couldn't reach" from "said no". Gated
      out of every send + invite loop (rank 90 — see STAGE_RANK).
    """
    PROSPECT = "Prospect"
    PARTNER_INTRO = "Partner Intro"
    CONNECTION_SENT = "Connection Sent"
    ACCEPTED = "Accepted"
    DM1_SENT = "DM1 Sent"
    DM2_SENT = "DM2 Sent"
    DM3_SENT = "DM3 Sent"
    NURTURE = "Nurture"
    RESPONDED = "Responded"
    DEFENSIVE_HOLD = "Defensive Hold"
    CALL_BOOKED = "Call Booked"
    QUALIFIED = "Qualified"
    NOT_INTERESTED = "Not Interested"
    UNREACHABLE = "Unreachable"


# Valid forward transitions. Any stage can also move to RESPONDED or NOT_INTERESTED.
STAGE_TRANSITIONS: dict[PipelineStage, PipelineStage] = {
    PipelineStage.PROSPECT: PipelineStage.CONNECTION_SENT,
    PipelineStage.CONNECTION_SENT: PipelineStage.ACCEPTED,
    PipelineStage.ACCEPTED: PipelineStage.DM1_SENT,
    PipelineStage.DM1_SENT: PipelineStage.DM2_SENT,
    PipelineStage.DM2_SENT: PipelineStage.DM3_SENT,
    PipelineStage.DM3_SENT: PipelineStage.RESPONDED,
    PipelineStage.RESPONDED: PipelineStage.CALL_BOOKED,
    PipelineStage.CALL_BOOKED: PipelineStage.QUALIFIED,
}

# Stages that always accept transitions from any other stage
ALWAYS_REACHABLE = {PipelineStage.RESPONDED, PipelineStage.NOT_INTERESTED}


def can_transition(current: PipelineStage, target: PipelineStage) -> bool:
    """Check if a stage transition is valid."""
    if target in ALWAYS_REACHABLE:
        return True
    return STAGE_TRANSITIONS.get(current) == target


# Canonical pipeline rank — single source of truth.
#
# Ranks are designed so higher = "further along the funnel" AND
# `stage_rank(new) >= stage_rank(old)` enforces forward-only motion
# (the F-PR-4 AttioWriter monotonicity rule). Terminal stages are
# spaced so backfills land monotonically: e.g., RESPONDED→DEFENSIVE_HOLD
# (90→95) is allowed; CALL_BOOKED→NOT_INTERESTED is blocked by the
# terminal-class regression check (see `terminal_class`).
#
# NOT_INTERESTED is intentionally bumped to 200 so no later rank can
# monotonically override it — a record that ever said "no" stays "no".
#
# UNREACHABLE shares RESPONDED's rank (90): both mark "the automated
# sequence stops here" and gate the row out of all sends/invites
# (is_send_eligible requires rank < 90; run_connection_requests selects
# only stage==PROSPECT). Entry is forward from any active stage
# (PROSPECT 0 / DM* 3-5 → 90). Tying at 90 (rather than a higher rank)
# deliberately leaves an operator escape hatch: an UNREACHABLE prospect
# who later becomes reachable can still be moved forward to RESPONDED(90)/
# CALL_BOOKED(99)/QUALIFIED(101)/NOT_INTERESTED(200) without a
# monotonicity regression. Non-unique ranks are already the norm here
# (PROSPECT == PARTNER_INTRO == 0).
STAGE_RANK: dict[PipelineStage, int] = {
    PipelineStage.PROSPECT: 0,
    PipelineStage.PARTNER_INTRO: 0,
    PipelineStage.CONNECTION_SENT: 1,
    PipelineStage.ACCEPTED: 2,
    PipelineStage.DM1_SENT: 3,
    PipelineStage.DM2_SENT: 4,
    PipelineStage.DM3_SENT: 5,
    PipelineStage.NURTURE: 6,
    PipelineStage.RESPONDED: 90,
    PipelineStage.UNREACHABLE: 90,
    PipelineStage.DEFENSIVE_HOLD: 95,
    PipelineStage.CALL_BOOKED: 99,
    PipelineStage.QUALIFIED: 101,
    PipelineStage.NOT_INTERESTED: 200,
}


def stage_rank(s: PipelineStage | str) -> int:
    """Return the canonical rank of a pipeline stage.

    Accepts either a `PipelineStage` enum member or its Attio string value
    (e.g., "DM1 Sent"). Raises `ValueError` on unknown stage strings;
    raises `KeyError` if the enum member is missing from `STAGE_RANK`
    (would indicate `STAGE_RANK` and `PipelineStage` drifted).

    For permissive lookups (where unknown stage → fallback) use
    `STAGE_RANK.get(stage_enum, default)` directly.
    """
    if isinstance(s, str):
        s = PipelineStage(s)
    return STAGE_RANK[s]


def dm_step_int(raw: object) -> int:
    """Coerce a `dm_step` attribute value to its integer step number.

    `dm_step` is stored on the linkedin_outreach list as a select-type slug —
    "dm1" / "dm2" / "dm3" for delivered DMs, plus non-DM states "dm0",
    "connection_note", "invite" — while some legacy/numeric callers pass a bare
    int or numeric string (cf. `int(attrs.get("dm_step") or 0)` in daily_check /
    detect_responses). This returns the DM number (1/2/3), or **0** for any
    non-DM / unknown / missing value ("connection_note", "invite", "dm0", None,
    "", or an unparseable string). It NEVER raises — a raw `"invite" >= 1`
    comparison would otherwise `TypeError`-crash any measurement caller that
    reads `dm_step` straight from `parse_entry`.

    "Received at least DM1" is therefore `dm_step_int(value) >= 1`. Mapping every
    non-DM state to 0 is intentional, not a silent failure: those states denote
    "no DM delivered", which is exactly what a DM'd denominator must exclude.
    """
    if raw is None or raw == "":
        return 0
    s = str(raw).strip().lower()
    if s.startswith("dm"):
        s = s[2:]
    try:
        return int(float(s))
    except ValueError:
        return 0


_POSITIVE_TERMINALS = {PipelineStage.CALL_BOOKED, PipelineStage.QUALIFIED}


def terminal_class(s: PipelineStage | str) -> Literal["positive", "negative", "defensive"] | None:
    """Classify a terminal stage for cross-class regression protection.

    Returns `"positive"` for CALL_BOOKED/QUALIFIED, `"negative"` for
    NOT_INTERESTED, `"defensive"` for DEFENSIVE_HOLD, and `None` for any
    non-terminal stage (including RESPONDED, which is the most-permissive
    terminal and acts as a routing-only stage).

    UNREACHABLE is intentionally `None` (not `"negative"`): it is an
    undeliverability terminal, not a rejection. Classifying it as negative
    would (a) conflate it with NOT_INTERESTED in any class-based metric and
    (b) block an operator from later routing a now-reachable prospect to a
    positive terminal. Monotonicity (rank 90) is what gates it out of sends.

    F-PR-4's `AttioWriter.apply` consults this to BLOCK cross-class
    terminal flips (e.g., CALL_BOOKED → NOT_INTERESTED) unless the caller
    explicitly opts in via `WriteIntent.allow_terminal_class_change=True`
    AND opens a `defensive_classification_review` queue row first.
    """
    if isinstance(s, str):
        try:
            s = PipelineStage(s)
        except ValueError:
            return None
    if s in _POSITIVE_TERMINALS:
        return "positive"
    if s is PipelineStage.NOT_INTERESTED:
        return "negative"
    if s is PipelineStage.DEFENSIVE_HOLD:
        return "defensive"
    return None


# Stage pairs that bypass rank-monotonicity in F-PR-4's AttioWriter.
#
# The §3.18 NURTURE re-engagement allowlist. ONLY (NURTURE → DM1_SENT)
# is permitted; expansion requires explicit CI-guard escalation
# (`cohort_tagging_regression` queue row). AttioWriter.apply still
# enforces the four §3.18 gates (is_send_eligible, per-company throttle,
# nurture_re_eligible_at not-null-and-elapsed) on top of allowlist
# membership — defense in depth.
NURTURE_REENGAGEMENT_ALLOWED: set[tuple[PipelineStage, PipelineStage]] = {
    (PipelineStage.NURTURE, PipelineStage.DM1_SENT),
}


_SEND_INELIGIBLE_FROZEN_AT = frozenset({
    "legacy_inferred_by_archaeology",
    "legacy_pure_unknown",
})


def is_send_eligible(entry: dict) -> bool:
    """Per §3.10 — gate every daily-eligibility caller before queuing a send.

    Returns False when:
    - The row carries `merged_into` (§3.11 union-merge loser; the winner
      inherits cadence + suppression). Re-queuing a loser would re-send
      to a prospect whose state lives on the winner — §3.1 hard red line.
    - The row is archaeology-stamped (`experiment_id_frozen_at` is one of
      the `legacy_*` values introduced by PR-22). Archaeology rows are
      measurement-only — they carry a cohort identity for `learn.py` /
      `weekly_brain.py` math but must NEVER be re-eligible for sends
      (would violate the §3.1 no-resend hard red line).
    - The row's stage is terminal (rank >= 90), with the sole exception
      of NURTURE — NURTURE is the only re-engagement-eligible terminal
      (controlled separately by `NURTURE_REENGAGEMENT_ALLOWED`).
    - The row has no resolvable stage (missing or unknown).

    Returns True otherwise.

    Accepts a flat-attribute entry dict (as produced by
    `AttioClient.parse_entry` and friends). Looks up `merged_into`,
    `stage`, and `experiment_id_frozen_at` only.
    """
    # §3.11 soft-delete defense — FIRST guard so no downstream eligibility
    # rule can ever override it. The merged_into stamp is the canonical
    # "this row is dead" signal across daily + weekly callers.
    if entry.get("merged_into"):
        return False
    if entry.get("experiment_id_frozen_at") in _SEND_INELIGIBLE_FROZEN_AT:
        return False
    stage_value = entry.get("stage")
    if not stage_value:
        return False
    if isinstance(stage_value, PipelineStage):
        stage = stage_value
    else:
        try:
            stage = PipelineStage(stage_value)
        except ValueError:
            return False
    if stage is PipelineStage.NURTURE:
        return True
    return STAGE_RANK.get(stage, 0) < 90


def is_invite_eligible(entry: dict, today: date) -> bool:
    """§3.1 defense — gate every invite caller on quarantine.

    A freshly-committed prospect must wait `invite_eligible_after` business
    days before receiving its first connection invite. The quarantine
    prevents an over-eager daily run from inviting a prospect the same day
    weekly_prospect committed them, which would short-circuit the human
    review window and risk re-inviting someone who'd been flipped to
    PARTNER_INTRO or similar between commit and invite.

    Composes with `is_send_eligible` at the caller — keep the two separate
    so the failure modes stay independently observable.

    Malformed `invite_eligible_after` is treated as still-quarantined
    (fail-closed): better to drop the row than ship an invite we can't
    reason about. Legacy rows with no attribute are eligible — the
    backfill is best-effort, so a missing value means "not subject to
    the quarantine".
    """
    raw = entry.get("invite_eligible_after")
    if raw in (None, ""):
        return True
    if isinstance(raw, date):
        return raw <= today
    try:
        parsed = date.fromisoformat(str(raw)[:10])
    except (TypeError, ValueError):
        return False
    return parsed <= today


# Minimum quality_score for the daily invite slice — the floor the
# invite-chain call sites shared as a bare `60` literal before the predicate
# was consolidated into `invite_slice_reason` (PR-217). Scope note:
# quality_gate.py's commit-time pass gate and Prospect.passes_quality_gate
# carry their own 60 literals (the same contract, different stage of the
# pipeline) — raising this constant alone does NOT move those gates.
INVITE_QUALITY_SCORE_FLOOR = 60


class InviteExclusionReason(Enum):
    """Why a row is excluded from the daily invite slice (PR-217).

    Returned by `invite_slice_reason`; callers key their per-reason
    counters / escalations off these members. Values are slugs suitable
    for audit payloads.
    """
    NOT_PROSPECT = "not_prospect"
    MISSING_QUALITY_SCORE = "missing_quality_score"
    MALFORMED_QUALITY_SCORE = "malformed_quality_score"
    LOW_QUALITY_SCORE = "low_quality_score"
    NOT_SEND_ELIGIBLE = "not_send_eligible"
    QUARANTINED = "quarantined"


def invite_slice_reason(
    entry: dict, today: date, *, strict: bool = True
) -> "InviteExclusionReason | None":
    """Single source of truth for the invite-slice predicate chain (PR-217).

    Returns None when the row belongs in the invite slice (stage PROSPECT,
    quality_score >= INVITE_QUALITY_SCORE_FLOOR, send-eligible per §3.10/§3.11,
    quarantine cleared per §3.1), otherwise the FIRST failing gate in
    canonical order:

        NOT_PROSPECT → MISSING_QUALITY_SCORE → MALFORMED_QUALITY_SCORE /
        LOW_QUALITY_SCORE → NOT_SEND_ELIGIBLE → QUARANTINED

    All callers of the chain (the daily invite selection loop, starvation's
    pool metrics, and any multi-operator invite claim filter) MUST route
    through this function — a gate added here reaches all of them at once; a
    gate added at one call site re-opens the 2026-07-02
    claims-spent-on-undispatchable-rows starvation bug.

    Canonical-order contract:
    - Score gates come first (after stage) so MISSING_QUALITY_SCORE is
      reported even for rows that also fail later gates — the daily loop's
      L1-5 `missing_quality_score` escalation must stay reachable regardless
      of send-eligibility.
    - QUARANTINED is last: it is a timing state, not a disqualification.
      QUARANTINED therefore means "would be invited today except
      invite_eligible_after has not elapsed" — exactly the population
      starvation's `quarantined_pool` metric counts.

    ``strict`` selects the malformed-quality_score policy:
    - True (the selection loops): ``int(score)`` raises TypeError/ValueError,
      per the loops' fail-loud policy on corrupt data (§0 invariant #9).
    - False (a claim filter): fails closed — returns MALFORMED_QUALITY_SCORE,
      since leaving a row unclaimed is cheaper than wasting a claim (or
      crashing the claim pass) on it.
    """
    if entry.get("stage") != PipelineStage.PROSPECT.value:
        return InviteExclusionReason.NOT_PROSPECT
    score = entry.get("quality_score")
    if score is None:
        return InviteExclusionReason.MISSING_QUALITY_SCORE
    try:
        low = int(score) < INVITE_QUALITY_SCORE_FLOOR
    except (TypeError, ValueError):
        if strict:
            raise
        return InviteExclusionReason.MALFORMED_QUALITY_SCORE
    if low:
        return InviteExclusionReason.LOW_QUALITY_SCORE
    if not is_send_eligible(entry):
        return InviteExclusionReason.NOT_SEND_ELIGIBLE
    if not is_invite_eligible(entry, today):
        return InviteExclusionReason.QUARANTINED
    return None


class DealStage(Enum):
    """Stages on the Attio 'deals' object — distinct from PipelineStage,
    which tracks the LinkedIn Outreach list-entry funnel. Deal records
    represent qualified opportunities regardless of how they entered
    (warm intro, inbound, partner, cold-outbound graduation)."""
    LEAD = "Lead"
    IN_PROGRESS = "In Progress"
    LOST = "Lost"
