"""Unit coverage for the consolidated eligibility predicates (PR-217).

`models.pipeline.invite_slice_reason` and `workflows.daily_check.dm_due_step`
are the single sources of truth for the invite-slice and DM-due predicate
chains that the daily selection loops, starvation's pool metrics, and
`compute_due_dm_counts` route through. These tests pin the reason-attribution
(canonical gate order) and the `strict` / `honor_stored_floor` policy knobs so
a future edit that changes membership or reason ordering fails loud.
"""

from datetime import date

import pytest

from models.campaign import MessageStep
from models.pipeline import (
    INVITE_QUALITY_SCORE_FLOOR,
    InviteExclusionReason,
    PipelineStage,
    invite_slice_reason,
)
from workflows import daily_check
from workflows.daily_check import DmExclusionReason, dm_due_step

TODAY = date(2026, 1, 10)


def _prospect(**over: object) -> dict:
    """A send-eligible, invite-eligible PROSPECT (reason == None)."""
    base: dict = {"stage": PipelineStage.PROSPECT.value, "quality_score": 70}
    base.update(over)
    return base


# --- invite_slice_reason --------------------------------------------------

class TestInviteSliceReason:
    def test_eligible_prospect_returns_none(self) -> None:
        assert invite_slice_reason(_prospect(), TODAY) is None

    def test_non_prospect_stage(self) -> None:
        entry = _prospect(stage=PipelineStage.ACCEPTED.value)
        assert invite_slice_reason(entry, TODAY) is (
            InviteExclusionReason.NOT_PROSPECT
        )

    def test_missing_stage_key_classifies_not_prospect(self) -> None:
        # `entry.get("stage")` — no KeyError here (unlike the old loop).
        assert invite_slice_reason({"quality_score": 70}, TODAY) is (
            InviteExclusionReason.NOT_PROSPECT
        )

    def test_missing_quality_score(self) -> None:
        entry = _prospect(quality_score=None)
        assert invite_slice_reason(entry, TODAY) is (
            InviteExclusionReason.MISSING_QUALITY_SCORE
        )

    def test_low_quality_score(self) -> None:
        entry = _prospect(quality_score=INVITE_QUALITY_SCORE_FLOOR - 1)
        assert invite_slice_reason(entry, TODAY) is (
            InviteExclusionReason.LOW_QUALITY_SCORE
        )

    def test_floor_boundary_is_inclusive(self) -> None:
        entry = _prospect(quality_score=INVITE_QUALITY_SCORE_FLOOR)
        assert invite_slice_reason(entry, TODAY) is None

    def test_malformed_score_strict_raises(self) -> None:
        entry = _prospect(quality_score="not-a-number")
        with pytest.raises(ValueError):
            invite_slice_reason(entry, TODAY, strict=True)

    def test_malformed_score_non_strict_fails_closed(self) -> None:
        entry = _prospect(quality_score="not-a-number")
        assert invite_slice_reason(entry, TODAY, strict=False) is (
            InviteExclusionReason.MALFORMED_QUALITY_SCORE
        )

    def test_not_send_eligible(self) -> None:
        # A merged-loser PROSPECT is send-ineligible (§3.11).
        entry = _prospect(merged_into="winner-record-id")
        assert invite_slice_reason(entry, TODAY) is (
            InviteExclusionReason.NOT_SEND_ELIGIBLE
        )

    def test_quarantined_is_last_gate(self) -> None:
        entry = _prospect(invite_eligible_after="2026-06-01")
        assert invite_slice_reason(entry, TODAY) is (
            InviteExclusionReason.QUARANTINED
        )

    def test_score_gate_precedes_send_eligibility(self) -> None:
        # A row failing BOTH score and send-eligibility reports the score
        # gate first (canonical order) so the L1-5 escalation stays reachable.
        entry = _prospect(quality_score=None, merged_into="winner")
        assert invite_slice_reason(entry, TODAY) is (
            InviteExclusionReason.MISSING_QUALITY_SCORE
        )


# --- dm_due_step ----------------------------------------------------------

def _accepted(**over: object) -> dict:
    base: dict = {
        "stage": PipelineStage.ACCEPTED.value,
        "last_contact_date": "2026-01-01",
        "dm_step": 0,
    }
    base.update(over)
    return base


class TestDmDueStep:
    def test_dm1_due_returns_step(self) -> None:
        verdict = dm_due_step(_accepted(), TODAY)
        assert verdict.step is MessageStep.DM1
        assert verdict.reason is None
        assert verdict.stage is PipelineStage.ACCEPTED

    def test_invalid_stage(self) -> None:
        verdict = dm_due_step({"stage": "bogus"}, TODAY)
        assert verdict.reason is DmExclusionReason.INVALID_STAGE
        assert verdict.stage is None

    def test_missing_stage_key_is_invalid_stage(self) -> None:
        verdict = dm_due_step({}, TODAY)
        assert verdict.reason is DmExclusionReason.INVALID_STAGE

    def test_not_send_eligible(self) -> None:
        verdict = dm_due_step(_accepted(merged_into="winner"), TODAY)
        assert verdict.reason is DmExclusionReason.NOT_SEND_ELIGIBLE
        assert verdict.stage is PipelineStage.ACCEPTED

    def test_missing_last_contact_date(self) -> None:
        verdict = dm_due_step(_accepted(last_contact_date=None), TODAY)
        assert verdict.reason is DmExclusionReason.MISSING_LAST_CONTACT_DATE
        assert verdict.needs_missing_lcd_escalation is True

    def test_missing_lcd_escalation_only_for_accepted(self) -> None:
        entry = _accepted(
            stage=PipelineStage.DM1_SENT.value, last_contact_date=None
        )
        verdict = dm_due_step(entry, TODAY)
        assert verdict.reason is DmExclusionReason.MISSING_LAST_CONTACT_DATE
        assert verdict.needs_missing_lcd_escalation is False

    def test_malformed_value_strict_raises(self) -> None:
        with pytest.raises(ValueError):
            dm_due_step(_accepted(last_contact_date="garbage"), TODAY, strict=True)

    def test_malformed_value_non_strict_fails_closed(self) -> None:
        verdict = dm_due_step(
            _accepted(last_contact_date="garbage"), TODAY, strict=False
        )
        assert verdict.reason is DmExclusionReason.MALFORMED_VALUE

    def test_not_due(self) -> None:
        # dm_step already at 1 → DM1 refused, nothing else due for ACCEPTED.
        verdict = dm_due_step(_accepted(dm_step=1), TODAY)
        assert verdict.reason is DmExclusionReason.NOT_DUE

    def test_stored_floor_blocks_when_honored(self, monkeypatch) -> None:
        monkeypatch.setattr(
            daily_check, "_is_blocked_by_stored_floor", lambda *a, **k: True
        )
        verdict = dm_due_step(_accepted(), TODAY, honor_stored_floor=True)
        assert verdict.reason is DmExclusionReason.STORED_FLOOR_BLOCKED

    def test_stored_floor_ignored_when_not_honored(self, monkeypatch) -> None:
        monkeypatch.setattr(
            daily_check, "_is_blocked_by_stored_floor", lambda *a, **k: True
        )
        verdict = dm_due_step(_accepted(), TODAY, honor_stored_floor=False)
        assert verdict.step is MessageStep.DM1
