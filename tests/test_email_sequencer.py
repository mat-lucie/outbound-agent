"""Tests for workflows/email_sequencer.py — business-day timing logic."""

from datetime import date

from models.email_campaign import EmailStage, EmailStep
from workflows.email_sequencer import (
    add_business_days,
    business_days_between,
    get_pending_email,
    is_email_eligible,
    is_weekday,
)


class TestIsWeekday:
    def test_monday_is_weekday(self):
        assert is_weekday(date(2026, 4, 6))  # Monday

    def test_friday_is_weekday(self):
        assert is_weekday(date(2026, 4, 10))  # Friday

    def test_saturday_is_not_weekday(self):
        assert not is_weekday(date(2026, 4, 11))  # Saturday

    def test_sunday_is_not_weekday(self):
        assert not is_weekday(date(2026, 4, 12))  # Sunday


class TestBusinessDaysBetween:
    def test_same_day_is_zero(self):
        assert business_days_between(date(2026, 4, 6), date(2026, 4, 6)) == 0

    def test_monday_to_friday_is_4(self):
        # Mon Apr 6 to Fri Apr 10 = 4 business days
        assert business_days_between(date(2026, 4, 6), date(2026, 4, 10)) == 4

    def test_monday_to_next_monday_is_5(self):
        # Mon Apr 6 to Mon Apr 13 = 5 business days (skips Sat+Sun)
        assert business_days_between(date(2026, 4, 6), date(2026, 4, 13)) == 5

    def test_friday_to_monday_is_1(self):
        # Fri Apr 10 to Mon Apr 13 = 1 business day
        assert business_days_between(date(2026, 4, 10), date(2026, 4, 13)) == 1

    def test_across_two_weeks(self):
        # Mon Apr 6 to Fri Apr 17 = 9 business days
        assert business_days_between(date(2026, 4, 6), date(2026, 4, 17)) == 9

    def test_wednesday_to_next_wednesday_is_5(self):
        # Wed Apr 8 to Wed Apr 15 = 5 business days
        assert business_days_between(date(2026, 4, 8), date(2026, 4, 15)) == 5


class TestAddBusinessDays:
    def test_add_zero_days(self):
        assert add_business_days(date(2026, 4, 6), 0) == date(2026, 4, 6)

    def test_add_3_from_monday(self):
        # Mon + 3 business days = Thu
        assert add_business_days(date(2026, 4, 6), 3) == date(2026, 4, 9)

    def test_add_3_from_wednesday(self):
        # Wed + 3 business days = Mon (skips weekend)
        assert add_business_days(date(2026, 4, 8), 3) == date(2026, 4, 13)

    def test_add_5_from_monday(self):
        # Mon + 5 business days = Mon next week
        assert add_business_days(date(2026, 4, 6), 5) == date(2026, 4, 13)

    def test_add_7_from_monday(self):
        # Mon + 7 business days = Wed next-next week
        assert add_business_days(date(2026, 4, 6), 7) == date(2026, 4, 15)


class TestIsEmailEligible:
    def test_email2_eligible_after_3_business_days(self):
        # Sent on Mon Apr 6, check on Thu Apr 9 = 3 business days
        assert is_email_eligible(EmailStep.EMAIL2, date(2026, 4, 6), date(2026, 4, 9))

    def test_email2_not_eligible_after_2_business_days(self):
        # Sent on Mon Apr 6, check on Wed Apr 8 = 2 business days
        assert not is_email_eligible(EmailStep.EMAIL2, date(2026, 4, 6), date(2026, 4, 8))

    def test_email2_eligible_across_weekend(self):
        # Sent on Thu Apr 9, check on Tue Apr 14 = 3 business days (Fri, Mon, Tue)
        assert is_email_eligible(EmailStep.EMAIL2, date(2026, 4, 9), date(2026, 4, 14))

    def test_email3_eligible_after_7_business_days(self):
        # Email3 delay from Email2 is 4 business days (7 total - 3 for Email2 = 4)
        # Sent Email2 on Thu Apr 9, check on Wed Apr 15 = 4 business days
        assert is_email_eligible(EmailStep.EMAIL3, date(2026, 4, 9), date(2026, 4, 15))

    def test_email3_not_eligible_after_3_business_days_from_email2(self):
        # Sent Email2 on Thu Apr 9, check on Tue Apr 14 = 3 business days (need 4)
        assert not is_email_eligible(EmailStep.EMAIL3, date(2026, 4, 9), date(2026, 4, 14))

    def test_none_date_returns_false(self):
        assert not is_email_eligible(EmailStep.EMAIL2, None, date(2026, 4, 15))


class TestGetPendingEmail:
    def test_queued_gets_email1(self):
        result = get_pending_email(EmailStage.QUEUED, date(2026, 4, 6), date(2026, 4, 6))
        assert result == EmailStep.EMAIL1

    def test_email1_sent_gets_email2_after_3_bdays(self):
        result = get_pending_email(EmailStage.EMAIL1_SENT, date(2026, 4, 6), date(2026, 4, 9))
        assert result == EmailStep.EMAIL2

    def test_email1_sent_no_email2_after_2_bdays(self):
        result = get_pending_email(EmailStage.EMAIL1_SENT, date(2026, 4, 6), date(2026, 4, 8))
        assert result is None

    def test_email2_sent_gets_email3_after_4_bdays(self):
        # Email2 sent on Thu Apr 9. Email3 needs 4 bdays after that = Wed Apr 15
        result = get_pending_email(EmailStage.EMAIL2_SENT, date(2026, 4, 9), date(2026, 4, 15))
        assert result == EmailStep.EMAIL3

    def test_completed_returns_none(self):
        result = get_pending_email(EmailStage.COMPLETED, date(2026, 4, 6), date(2026, 4, 20))
        assert result is None

    def test_unsubscribed_returns_none(self):
        result = get_pending_email(EmailStage.UNSUBSCRIBED, date(2026, 4, 6), date(2026, 4, 20))
        assert result is None
