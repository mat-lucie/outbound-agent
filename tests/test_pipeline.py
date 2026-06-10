"""Tests for models/pipeline.py — stage transitions and canonical STAGE_RANK."""

from datetime import date

import pytest

from models.pipeline import (
    NURTURE_REENGAGEMENT_ALLOWED,
    STAGE_RANK,
    STAGE_TRANSITIONS,
    PipelineStage,
    can_transition,
    dm_step_int,
    is_send_eligible,
    stage_rank,
    terminal_class,
)
from workflows.dm_sequencer import STAGE_FOR_DM, get_pending_dms


class TestDmStepInt:
    """dm_step is a select-type slug on linkedin_outreach; coercion must never
    raise (a raw `slug >= 1` TypeErrors the learn loop + weekly KPI write)."""

    @pytest.mark.parametrize(("raw", "expected"), [
        ("dm1", 1), ("dm2", 2), ("dm3", 3), ("DM2", 2),  # delivered-DM slugs
        ("dm0", 0), ("connection_note", 0), ("invite", 0),  # non-DM states → 0
        (None, 0), ("", 0), ("garbage", 0),  # missing / unparseable → 0
        (2, 2), ("2", 2), (2.0, 2),  # legacy numeric / float forms
    ])
    def test_coerces_without_raising(self, raw, expected):
        assert dm_step_int(raw) == expected

    def test_received_at_least_dm1_predicate(self):
        # The "received >=1 DM" gate used by both measurement consumers.
        assert dm_step_int("dm1") >= 1
        assert dm_step_int("invite") < 1
        assert dm_step_int(None) < 1


class TestPipelineStage:
    def test_has_14_stages(self):
        # 11 original + NURTURE + DEFENSIVE_HOLD (F-PR-1) + UNREACHABLE (Wave-2-A).
        assert len(PipelineStage) == 14

    def test_stage_values_match_attio(self):
        expected = [
            "Prospect", "Partner Intro", "Connection Sent", "Accepted",
            "DM1 Sent", "DM2 Sent", "DM3 Sent",
            "Nurture",
            "Responded",
            "Defensive Hold",
            "Call Booked", "Qualified", "Not Interested",
            "Unreachable",
        ]
        assert [s.value for s in PipelineStage] == expected


class TestPartnerIntroStage:
    """PARTNER_INTRO is a manual-only stage for warm partner referrals.
    It must be invisible to every automated outreach workflow."""

    def test_partner_intro_not_a_dm_trigger_stage(self):
        # STAGE_FOR_DM maps each DM step to the stage that triggers it.
        # PARTNER_INTRO must never appear in that mapping.
        assert PipelineStage.PARTNER_INTRO not in STAGE_FOR_DM.values()

    def test_get_pending_dms_never_fires_for_partner_intro(self):
        # Even with a last_contact_date far enough in the past to clear
        # every timing gate, a Partner Intro stage returns None.
        long_ago = date(2020, 1, 1)
        today = date(2026, 4, 13)
        assert get_pending_dms(PipelineStage.PARTNER_INTRO, long_ago, today) is None

    def test_partner_intro_not_in_forward_transitions(self):
        # No automated stage advances into or out of Partner Intro.
        # It can only move via RESPONDED / NOT_INTERESTED (always reachable)
        # or by manual intervention.
        assert PipelineStage.PARTNER_INTRO not in STAGE_TRANSITIONS
        assert PipelineStage.PARTNER_INTRO not in STAGE_TRANSITIONS.values()

    def test_partner_intro_can_still_transition_to_responded(self):
        # When a partner reports a positive reply, we move the record
        # into RESPONDED manually. This must be allowed.
        assert can_transition(PipelineStage.PARTNER_INTRO, PipelineStage.RESPONDED)

    def test_partner_intro_can_transition_to_not_interested(self):
        # When a partner reports no reply or a decline, move to NOT_INTERESTED.
        assert can_transition(PipelineStage.PARTNER_INTRO, PipelineStage.NOT_INTERESTED)


class TestCanTransition:
    # Valid forward transitions
    def test_prospect_to_connection_sent(self):
        assert can_transition(PipelineStage.PROSPECT, PipelineStage.CONNECTION_SENT)

    def test_connection_sent_to_accepted(self):
        assert can_transition(PipelineStage.CONNECTION_SENT, PipelineStage.ACCEPTED)

    def test_accepted_to_dm1(self):
        assert can_transition(PipelineStage.ACCEPTED, PipelineStage.DM1_SENT)

    def test_dm1_to_dm2(self):
        assert can_transition(PipelineStage.DM1_SENT, PipelineStage.DM2_SENT)

    def test_dm2_to_dm3(self):
        assert can_transition(PipelineStage.DM2_SENT, PipelineStage.DM3_SENT)

    def test_dm3_to_responded(self):
        assert can_transition(PipelineStage.DM3_SENT, PipelineStage.RESPONDED)

    def test_responded_to_call_booked(self):
        assert can_transition(PipelineStage.RESPONDED, PipelineStage.CALL_BOOKED)

    def test_call_booked_to_qualified(self):
        assert can_transition(PipelineStage.CALL_BOOKED, PipelineStage.QUALIFIED)

    # Always-reachable stages
    def test_any_stage_to_responded(self):
        for stage in PipelineStage:
            assert can_transition(stage, PipelineStage.RESPONDED)

    def test_any_stage_to_not_interested(self):
        for stage in PipelineStage:
            assert can_transition(stage, PipelineStage.NOT_INTERESTED)

    # Invalid transitions
    def test_cannot_skip_stages(self):
        assert not can_transition(PipelineStage.PROSPECT, PipelineStage.DM1_SENT)

    def test_cannot_go_backwards(self):
        assert not can_transition(PipelineStage.ACCEPTED, PipelineStage.PROSPECT)

    def test_prospect_cannot_go_to_qualified(self):
        assert not can_transition(PipelineStage.PROSPECT, PipelineStage.QUALIFIED)

    def test_dm1_cannot_go_to_dm3(self):
        assert not can_transition(PipelineStage.DM1_SENT, PipelineStage.DM3_SENT)


class TestStageRank:
    """F-PR-1 canonical STAGE_RANK + stage_rank() helper."""

    def test_stage_rank_covers_every_pipeline_stage(self):
        # If a new stage is added to PipelineStage without an entry in
        # STAGE_RANK, monotonicity comparisons silently fall back to 0
        # and forward-only motion breaks. This test pins the contract.
        assert set(STAGE_RANK.keys()) == set(PipelineStage)

    def test_stage_rank_monotonic_funnel(self):
        # Active funnel order: PROSPECT/PARTNER_INTRO (0) →
        # CONNECTION_SENT (1) → ACCEPTED (2) → DM1 (3) → DM2 (4) →
        # DM3 (5) → NURTURE (6). Strict-monotonic except the dual-start
        # PROSPECT/PARTNER_INTRO sharing rank 0.
        active = [
            PipelineStage.CONNECTION_SENT,
            PipelineStage.ACCEPTED,
            PipelineStage.DM1_SENT,
            PipelineStage.DM2_SENT,
            PipelineStage.DM3_SENT,
            PipelineStage.NURTURE,
        ]
        ranks = [STAGE_RANK[s] for s in active]
        assert ranks == sorted(ranks)
        assert len(set(ranks)) == len(ranks)

    def test_partner_intro_shares_rank_with_prospect(self):
        assert STAGE_RANK[PipelineStage.PARTNER_INTRO] == STAGE_RANK[PipelineStage.PROSPECT]

    def test_not_interested_is_hardest_terminal(self):
        # NOT_INTERESTED at 200 means no other rank can monotonically
        # override it — a "no" stays "no". §3.1 hard red line.
        not_interested_rank = STAGE_RANK[PipelineStage.NOT_INTERESTED]
        for stage in PipelineStage:
            if stage is PipelineStage.NOT_INTERESTED:
                continue
            assert STAGE_RANK[stage] < not_interested_rank

    def test_terminal_ranks_above_funnel(self):
        # Every terminal stage must rank above every non-terminal active
        # funnel stage so cross-URL guards block forward DMs to siblings
        # already in a terminal state.
        funnel = [
            PipelineStage.PROSPECT,
            PipelineStage.PARTNER_INTRO,
            PipelineStage.CONNECTION_SENT,
            PipelineStage.ACCEPTED,
            PipelineStage.DM1_SENT,
            PipelineStage.DM2_SENT,
            PipelineStage.DM3_SENT,
            PipelineStage.NURTURE,
        ]
        terminals = [
            PipelineStage.RESPONDED,
            PipelineStage.DEFENSIVE_HOLD,
            PipelineStage.CALL_BOOKED,
            PipelineStage.QUALIFIED,
            PipelineStage.NOT_INTERESTED,
        ]
        max_funnel = max(STAGE_RANK[s] for s in funnel)
        min_terminal = min(STAGE_RANK[s] for s in terminals)
        assert min_terminal > max_funnel

    def test_responded_backfill_to_defensive_hold_is_monotonic(self):
        # The §3.6 backfill rewrites RESPONDED→DEFENSIVE_HOLD for rows
        # with defensive_score>=0.6. Must be a monotonic forward move.
        assert STAGE_RANK[PipelineStage.RESPONDED] < STAGE_RANK[PipelineStage.DEFENSIVE_HOLD]

    def test_every_stage_transition_is_rank_monotonic(self):
        # F-PR-4's AttioWriter rule: stage_rank(new) >= stage_rank(old)
        # for every forward transition. This pins the invariant mechanically
        # so any future STAGE_TRANSITIONS edit that violates it fails CI.
        for current, target in STAGE_TRANSITIONS.items():
            assert STAGE_RANK[current] <= STAGE_RANK[target], (
                f"STAGE_TRANSITIONS[{current}] = {target} regresses rank "
                f"{STAGE_RANK[current]} -> {STAGE_RANK[target]}"
            )

    def test_stage_rank_accepts_enum(self):
        assert stage_rank(PipelineStage.DM2_SENT) == STAGE_RANK[PipelineStage.DM2_SENT]

    def test_stage_rank_accepts_attio_string(self):
        assert stage_rank("DM2 Sent") == STAGE_RANK[PipelineStage.DM2_SENT]

    def test_stage_rank_raises_on_unknown_string(self):
        import pytest
        with pytest.raises(ValueError):
            stage_rank("Not A Real Stage")


class TestTerminalClass:
    """F-PR-1 terminal_class() classifies terminal stages for
    cross-class regression protection in F-PR-4."""

    def test_call_booked_is_positive(self):
        assert terminal_class(PipelineStage.CALL_BOOKED) == "positive"

    def test_qualified_is_positive(self):
        assert terminal_class(PipelineStage.QUALIFIED) == "positive"

    def test_not_interested_is_negative(self):
        assert terminal_class(PipelineStage.NOT_INTERESTED) == "negative"

    def test_defensive_hold_is_defensive(self):
        assert terminal_class(PipelineStage.DEFENSIVE_HOLD) == "defensive"

    def test_responded_has_no_class(self):
        # RESPONDED is routing-only; it doesn't carry a class verdict on
        # its own. The classifier in workflows/auto_classifier upgrades it
        # to DEFENSIVE_HOLD / CALL_BOOKED / NOT_INTERESTED.
        assert terminal_class(PipelineStage.RESPONDED) is None

    def test_funnel_stages_have_no_class(self):
        for stage in (
            PipelineStage.PROSPECT, PipelineStage.PARTNER_INTRO,
            PipelineStage.CONNECTION_SENT, PipelineStage.ACCEPTED,
            PipelineStage.DM1_SENT, PipelineStage.DM2_SENT,
            PipelineStage.DM3_SENT, PipelineStage.NURTURE,
        ):
            assert terminal_class(stage) is None

    def test_accepts_attio_string(self):
        assert terminal_class("Defensive Hold") == "defensive"

    def test_unknown_string_returns_none(self):
        assert terminal_class("Not A Real Stage") is None


class TestUnreachableStage:
    """Wave-2-A — UNREACHABLE is an undeliverability terminal (InMail-required
    DMs + Out-of-Network invites), distinct from NOT_INTERESTED."""

    def test_rank_gates_out_of_sends(self):
        # is_send_eligible requires rank < 90; UNREACHABLE at 90 is gated out.
        assert STAGE_RANK[PipelineStage.UNREACHABLE] == 90
        assert not is_send_eligible({"stage": "Unreachable"})

    def test_forward_entry_from_active_stages_is_monotonic(self):
        # The two writers move PROSPECT (OON) and DM* (InMail) → UNREACHABLE.
        # Each must be a non-regressing rank move (AttioWriter monotonicity).
        unreachable = STAGE_RANK[PipelineStage.UNREACHABLE]
        for src in (
            PipelineStage.PROSPECT,
            PipelineStage.CONNECTION_SENT,
            PipelineStage.DM1_SENT,
            PipelineStage.DM2_SENT,
            PipelineStage.DM3_SENT,
        ):
            assert STAGE_RANK[src] <= unreachable, src

    def test_operator_can_still_escape_forward(self):
        # Tying at RESPONDED's rank (90) leaves an escape hatch: a
        # now-reachable prospect can move forward to any higher terminal
        # without a monotonicity regression.
        unreachable = STAGE_RANK[PipelineStage.UNREACHABLE]
        for dest in (
            PipelineStage.CALL_BOOKED,
            PipelineStage.QUALIFIED,
            PipelineStage.NOT_INTERESTED,
        ):
            assert STAGE_RANK[dest] >= unreachable, dest

    def test_terminal_class_is_none_not_negative(self):
        # Undeliverable != rejected. None keeps it out of the negative
        # bucket (measurement-clean) and unblocks operator re-routing.
        assert terminal_class(PipelineStage.UNREACHABLE) is None
        assert terminal_class("Unreachable") is None

    def test_below_not_interested(self):
        # An UNREACHABLE prospect may still later be marked Not Interested.
        assert STAGE_RANK[PipelineStage.UNREACHABLE] < STAGE_RANK[PipelineStage.NOT_INTERESTED]


class TestNurtureReengagementAllowlist:
    """§3.18 — NURTURE→DM1_SENT is the SOLE rank-monotonicity-exempt
    transition. F-PR-4's AttioWriter consults this set."""

    def test_allowlist_has_exactly_one_pair(self):
        # CI guard. Expansion requires explicit operator-review escalation
        # (`cohort_tagging_regression` queue row) per §3.18.
        assert len(NURTURE_REENGAGEMENT_ALLOWED) == 1
        assert (PipelineStage.NURTURE, PipelineStage.DM1_SENT) in NURTURE_REENGAGEMENT_ALLOWED

    def test_allowlist_pair_is_rank_regressing(self):
        # The whole point of the allowlist is that rank decreases on this
        # transition (NURTURE rank 6 → DM1_SENT rank 3). If both stages
        # ever had the same rank, the allowlist wouldn't be needed.
        nurture_rank = STAGE_RANK[PipelineStage.NURTURE]
        dm1_rank = STAGE_RANK[PipelineStage.DM1_SENT]
        assert nurture_rank > dm1_rank


class TestIsSendEligible:
    """§3.10 — daily-eligibility gate. Returns False for archaeology-stamped
    rows and for non-NURTURE terminal rows, so the daily DM queue never
    re-sends to ineligible prospects."""

    def test_funnel_row_eligible(self):
        assert is_send_eligible({"stage": "DM2 Sent"})

    def test_prospect_row_eligible(self):
        assert is_send_eligible({"stage": "Prospect"})

    def test_nurture_eligible(self):
        # NURTURE is the sole re-engagement-eligible terminal.
        assert is_send_eligible({"stage": "Nurture"})

    def test_responded_not_eligible(self):
        assert not is_send_eligible({"stage": "Responded"})

    def test_defensive_hold_not_eligible(self):
        assert not is_send_eligible({"stage": "Defensive Hold"})

    def test_call_booked_not_eligible(self):
        assert not is_send_eligible({"stage": "Call Booked"})

    def test_not_interested_not_eligible(self):
        assert not is_send_eligible({"stage": "Not Interested"})

    def test_archaeology_stamped_not_eligible_even_in_funnel(self):
        # §3.10 hard invariant: archaeology rows are measurement-only.
        # An archaeology-stamped row in DM2_SENT must NEVER queue a DM —
        # otherwise tomorrow's batch would re-send (§3.1 violation).
        for frozen in ("legacy_inferred_by_archaeology", "legacy_pure_unknown"):
            entry = {"stage": "DM2 Sent", "experiment_id_frozen_at": frozen}
            assert not is_send_eligible(entry), frozen

    def test_archaeology_blocks_nurture_too(self):
        # An archaeology-stamped NURTURE row also cannot re-engage —
        # the archaeology stamp wins over the NURTURE re-engagement
        # carve-out, by design (§3.10 + §3.18 layered defense).
        entry = {
            "stage": "Nurture",
            "experiment_id_frozen_at": "legacy_inferred_by_archaeology",
        }
        assert not is_send_eligible(entry)

    def test_living_legacy_stamps_remain_eligible(self):
        # Non-archaeology legacy stamps (real history, full provenance)
        # must stay eligible — only archaeology / pure-unknown are gated.
        for frozen in ("prospect", "connection_sent", "accepted", "legacy_pre_tsv_era"):
            entry = {"stage": "DM1 Sent", "experiment_id_frozen_at": frozen}
            assert is_send_eligible(entry), frozen

    def test_missing_stage_not_eligible(self):
        assert not is_send_eligible({})
        assert not is_send_eligible({"stage": ""})

    def test_unknown_stage_not_eligible(self):
        assert not is_send_eligible({"stage": "Not A Real Stage"})

    def test_accepts_pipeline_stage_object(self):
        assert is_send_eligible({"stage": PipelineStage.DM1_SENT})
        assert not is_send_eligible({"stage": PipelineStage.NOT_INTERESTED})

    def test_merged_into_loser_not_eligible(self):
        """§3.11 soft-delete defense: a row carrying `merged_into` is a
        union-merge loser. Its winner inherits cadence + suppression;
        re-queuing the loser would re-send (§3.1 hard red line). This
        is the FIRST guard, so it must beat every other eligibility
        rule — even a clean DM1_SENT funnel row gets rejected.
        """
        entry = {
            "stage": "DM1 Sent",
            "merged_into": {"target_object": "people",
                            "target_record_id": "rec-winner"},
        }
        assert not is_send_eligible(entry)

    def test_merged_into_loser_in_nurture_not_eligible(self):
        # NURTURE is normally re-engagement-eligible, but a soft-deleted
        # loser must lose to that carve-out too.
        entry = {
            "stage": "Nurture",
            "merged_into": {"target_object": "people",
                            "target_record_id": "rec-winner"},
        }
        assert not is_send_eligible(entry)
