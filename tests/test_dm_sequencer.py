"""Tests for workflows/dm_sequencer.py — timing logic.

Reference dates used throughout:
  Sun 2026-03-01
  Tue 2026-03-10
  Wed 2026-03-11
  Thu 2026-03-12
  Fri 2026-03-13
  Sat 2026-03-14
  Sun 2026-03-15
  Mon 2026-03-16
  Fri 2026-03-20
"""

from datetime import date

from models.campaign import MessageStep
from models.pipeline import PipelineStage
from workflows.dm_sequencer import (
    DM_TIMING,
    earliest_send_date,
    get_pending_dms,
    is_dm_eligible,
    shift_off_weekend,
)


class TestDmTiming:
    def test_dm1_requires_1_day(self):
        assert DM_TIMING[MessageStep.DM1] == 1

    def test_dm2_requires_5_days(self):
        assert DM_TIMING[MessageStep.DM2] == 5

    def test_dm3_requires_10_days(self):
        assert DM_TIMING[MessageStep.DM3] == 10


class TestShiftOffWeekend:
    def test_saturday_shifts_to_friday(self):
        assert shift_off_weekend(date(2026, 3, 14)) == date(2026, 3, 13)

    def test_sunday_shifts_to_monday(self):
        assert shift_off_weekend(date(2026, 3, 15)) == date(2026, 3, 16)

    def test_weekday_unchanged(self):
        for d in [date(2026, 3, 11), date(2026, 3, 12), date(2026, 3, 13), date(2026, 3, 16)]:
            assert shift_off_weekend(d) == d


class TestEarliestSendDate:
    def test_dm2_landing_on_sunday_shifts_to_monday(self):
        # Tue + 5 = Sun → Mon
        assert earliest_send_date(MessageStep.DM2, date(2026, 3, 10)) == date(2026, 3, 16)

    def test_dm2_landing_on_saturday_shifts_to_friday(self):
        # Mon + 5 = Sat → Fri
        assert earliest_send_date(MessageStep.DM2, date(2026, 3, 9)) == date(2026, 3, 13)

    def test_dm3_landing_on_weekday_unchanged(self):
        # Sun + 10 = Wed
        assert earliest_send_date(MessageStep.DM3, date(2026, 3, 1)) == date(2026, 3, 11)


class TestIsDmEligible:
    def test_dm1_eligible_after_1_day(self):
        # Tue + 1 = Wed; today=Wed
        assert is_dm_eligible(MessageStep.DM1, date(2026, 3, 10), date(2026, 3, 11))

    def test_dm1_eligible_after_many_days(self):
        # last=Sun, today=Fri → eligible
        assert is_dm_eligible(MessageStep.DM1, date(2026, 3, 1), date(2026, 3, 13))

    def test_dm1_not_eligible_same_day(self):
        # last=Tue, today=Tue → window not open
        assert not is_dm_eligible(MessageStep.DM1, date(2026, 3, 10), date(2026, 3, 10))

    def test_dm2_eligible_after_shifted_window(self):
        # last=Tue 03-10 → due Sun 03-15 → shift to Mon 03-16; today=Mon
        assert is_dm_eligible(MessageStep.DM2, date(2026, 3, 10), date(2026, 3, 16))

    def test_dm2_not_eligible_before_shifted_window(self):
        # Same setup; today=Fri 03-13 < shifted due Mon 03-16
        assert not is_dm_eligible(MessageStep.DM2, date(2026, 3, 10), date(2026, 3, 13))

    def test_dm3_eligible_after_10_days(self):
        # last=Sun 03-01 + 10 = Wed 03-11; today=Thu 03-12 → eligible
        assert is_dm_eligible(MessageStep.DM3, date(2026, 3, 1), date(2026, 3, 12))

    def test_dm3_not_eligible_before_10_days(self):
        # last=Tue 03-10 + 10 = Fri 03-20; today=Mon 03-16 < 03-20
        assert not is_dm_eligible(MessageStep.DM3, date(2026, 3, 10), date(2026, 3, 16))

    def test_none_date_returns_false(self):
        assert not is_dm_eligible(MessageStep.DM1, None, date(2026, 3, 13))


class TestGetPendingDms:
    def test_accepted_prospect_gets_dm1(self):
        result = get_pending_dms(PipelineStage.ACCEPTED, date(2026, 3, 10), date(2026, 3, 11))
        assert result == MessageStep.DM1

    def test_accepted_same_day_no_dm(self):
        result = get_pending_dms(PipelineStage.ACCEPTED, date(2026, 3, 10), date(2026, 3, 10))
        assert result is None

    def test_dm1_sent_gets_dm2_after_shifted_window(self):
        # last=Tue 03-10; due Sun 03-15 → Mon 03-16
        result = get_pending_dms(PipelineStage.DM1_SENT, date(2026, 3, 10), date(2026, 3, 16))
        assert result == MessageStep.DM2

    def test_dm1_sent_no_dm2_before_shifted_window(self):
        result = get_pending_dms(PipelineStage.DM1_SENT, date(2026, 3, 10), date(2026, 3, 13))
        assert result is None

    def test_dm2_sent_gets_dm3_after_10_days(self):
        # last=Sun 03-01 + 10 = Wed 03-11; today=Thu 03-12
        result = get_pending_dms(PipelineStage.DM2_SENT, date(2026, 3, 1), date(2026, 3, 12))
        assert result == MessageStep.DM3

    def test_prospect_stage_returns_none(self):
        result = get_pending_dms(PipelineStage.PROSPECT, date(2026, 3, 1), date(2026, 3, 13))
        assert result is None

    def test_responded_stage_returns_none(self):
        result = get_pending_dms(PipelineStage.RESPONDED, date(2026, 3, 1), date(2026, 3, 13))
        assert result is None

    def test_none_last_contact_returns_none(self):
        result = get_pending_dms(PipelineStage.ACCEPTED, None, date(2026, 3, 13))
        assert result is None

    def test_dm_step_1_blocks_dm1_even_if_stage_accepted(self):
        # Stale stage (Attio says Accepted) but dm_step counter proves DM1 went out.
        result = get_pending_dms(
            PipelineStage.ACCEPTED, date(2026, 3, 10), date(2026, 3, 16), dm_step=1
        )
        assert result is None

    def test_dm_step_1_allows_dm2_with_dm1_sent_stage(self):
        result = get_pending_dms(
            PipelineStage.DM1_SENT, date(2026, 3, 10), date(2026, 3, 16), dm_step=1
        )
        assert result == MessageStep.DM2

    def test_dm_step_2_blocks_dm2_even_if_stage_dm1_sent(self):
        result = get_pending_dms(
            PipelineStage.DM1_SENT, date(2026, 3, 10), date(2026, 3, 16), dm_step=2
        )
        assert result is None

    def test_dm_step_0_allows_dm1(self):
        result = get_pending_dms(
            PipelineStage.ACCEPTED, date(2026, 3, 10), date(2026, 3, 16), dm_step=0
        )
        assert result == MessageStep.DM1
