"""Pure-logic tests for the Follow-up Radar policy model (PR-211/247).

Covers models.followup — the CRM-agnostic staleness/ranking policy — with no
I/O and no CRM coupling. The engine + state-writer tests live alongside the
workflow modules they exercise.
"""
from __future__ import annotations

from datetime import date

import pytest

from models.followup import (
    DEAL_STAGE_REASONS,
    DRAFT_COOLDOWN_DAYS,
    POLICY,
    WAITING_MAX_NUDGES,
    FollowupReason,
    WarmLane,
    days_silent,
    is_positive_nurture,
    is_stale,
    urgency_score,
)

TODAY = date(2026, 7, 1)


def test_days_silent_business_vs_calendar():
    # 2026-06-25 (Thu) → 2026-07-01 (Wed): business days = Fri,Mon,Tue,Wed = 4
    start = date(2026, 6, 25)
    assert days_silent(start, TODAY, business=True) == 4
    # calendar days = 6
    assert days_silent(start, TODAY, business=False) == 6


def test_urgency_overdue_ratio_and_cap():
    assert urgency_score(heat=4, silent_days=3, threshold_days=3) == 4.0
    assert urgency_score(heat=4, silent_days=4, threshold_days=3) == pytest.approx(5.333, abs=0.01)
    # capped at _MAX_OVERDUE_RATIO=3.0 → heat*(1+3)=16
    assert urgency_score(heat=4, silent_days=1000, threshold_days=3) == pytest.approx(16.0)


def test_urgency_value_multiplier():
    base = urgency_score(heat=4, silent_days=3, threshold_days=3, value_mult=1.0)
    boosted = urgency_score(heat=4, silent_days=3, threshold_days=3, value_mult=2.0)
    assert boosted == pytest.approx(base * 2)


def test_urgency_threshold_zero_is_safe():
    # CALLBACK_DUE has threshold 0 — no division-by-zero, no overdue ratio.
    assert urgency_score(heat=7, silent_days=5, threshold_days=0) == 7.0


def test_is_stale():
    start = date(2026, 6, 28)  # Sun; 3 calendar days before TODAY
    waiting = POLICY[FollowupReason.AWAITING_REPLY]  # threshold 7, calendar
    assert is_stale(start, TODAY, waiting) is False
    booked = POLICY[FollowupReason.CALL_BOOKED_STALE]  # threshold 3, business
    assert is_stale(date(2026, 6, 25), TODAY, booked) is True


def test_is_positive_nurture():
    assert is_positive_nurture("positive") is True
    assert is_positive_nurture("question") is True
    assert is_positive_nurture("neutral") is False
    assert is_positive_nurture(None) is False


def test_policy_lanes():
    assert POLICY[FollowupReason.CALL_BOOKED_STALE].lane is WarmLane.OWED
    assert POLICY[FollowupReason.RESPONDED_NO_NEXT_STEP].lane is WarmLane.NUDGE
    assert POLICY[FollowupReason.AWAITING_REPLY].lane is WarmLane.WAITING
    assert POLICY[FollowupReason.PARTNER_INTRO_UNWORKED].lane is WarmLane.PARTNER


def test_waiting_lane_calendar_days_and_ceiling():
    # WAITING is deliberately calendar-day (a send ages over weekends too).
    assert POLICY[FollowupReason.AWAITING_REPLY].business_days is False
    assert POLICY[FollowupReason.AWAITING_REPLY].threshold_days == 7
    assert WAITING_MAX_NUDGES == 2
    assert DRAFT_COOLDOWN_DAYS == 5


def test_deal_stage_reasons_use_enum():
    # Regression guard: the deal-stage map must stay coupled to DealStage.
    from models.pipeline import DealStage

    assert DealStage.LEAD.value in DEAL_STAGE_REASONS
    assert DealStage.IN_PROGRESS.value in DEAL_STAGE_REASONS
    assert DealStage.LOST.value not in DEAL_STAGE_REASONS
