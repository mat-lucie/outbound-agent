"""Tests for models/business_calendar.py — outreach calendar policy."""

from datetime import date

from models.business_calendar import (
    add_business_days,
    business_days_between,
    is_send_day,
    is_weekday,
    shift_off_weekend,
)

# Reference week (Mon 2026-03-09 through Sun 2026-03-15):
#   Mon 03-09, Tue 03-10, Wed 03-11, Thu 03-12, Fri 03-13, Sat 03-14, Sun 03-15


class TestIsSendDay:
    def test_monday_is_send_day(self):
        assert is_send_day(date(2026, 3, 9))

    def test_tuesday_is_send_day(self):
        assert is_send_day(date(2026, 3, 10))

    def test_wednesday_is_send_day(self):
        assert is_send_day(date(2026, 3, 11))

    def test_thursday_is_send_day(self):
        assert is_send_day(date(2026, 3, 12))

    def test_friday_is_send_day(self):
        assert is_send_day(date(2026, 3, 13))

    def test_saturday_is_not_send_day(self):
        assert not is_send_day(date(2026, 3, 14))

    def test_sunday_is_not_send_day(self):
        assert not is_send_day(date(2026, 3, 15))

    def test_default_today_returns_bool(self):
        # Don't assert truthiness — just that the no-arg call works.
        assert isinstance(is_send_day(), bool)


class TestIsWeekdayAlias:
    def test_is_weekday_is_alias_of_is_send_day(self):
        assert is_weekday is is_send_day


class TestShiftOffWeekend:
    def test_saturday_shifts_back_to_friday(self):
        assert shift_off_weekend(date(2026, 3, 14)) == date(2026, 3, 13)

    def test_sunday_shifts_forward_to_monday(self):
        assert shift_off_weekend(date(2026, 3, 15)) == date(2026, 3, 16)

    def test_weekday_unchanged(self):
        for d in [date(2026, 3, 9), date(2026, 3, 10), date(2026, 3, 11),
                  date(2026, 3, 12), date(2026, 3, 13)]:
            assert shift_off_weekend(d) == d


class TestBusinessDaysBetween:
    def test_zero_when_end_equals_start(self):
        assert business_days_between(date(2026, 3, 10), date(2026, 3, 10)) == 0

    def test_zero_when_end_before_start(self):
        assert business_days_between(date(2026, 3, 12), date(2026, 3, 10)) == 0

    def test_one_weekday_difference(self):
        # Mon -> Tue
        assert business_days_between(date(2026, 3, 9), date(2026, 3, 10)) == 1

    def test_skips_weekends(self):
        # Fri 03-13 to Mon 03-16: only Mon counts (Sat/Sun skipped, Fri excluded as start)
        assert business_days_between(date(2026, 3, 13), date(2026, 3, 16)) == 1

    def test_full_workweek(self):
        # Mon 03-09 to Fri 03-13: Tue/Wed/Thu/Fri = 4 business days after start
        assert business_days_between(date(2026, 3, 9), date(2026, 3, 13)) == 4

    def test_spanning_a_weekend(self):
        # Thu 03-12 to Thu 03-19: Fri/Mon/Tue/Wed/Thu = 5 (Sat/Sun skipped)
        assert business_days_between(date(2026, 3, 12), date(2026, 3, 19)) == 5


class TestAddBusinessDays:
    def test_add_one_to_friday_lands_monday(self):
        assert add_business_days(date(2026, 3, 13), 1) == date(2026, 3, 16)

    def test_add_one_to_monday_lands_tuesday(self):
        assert add_business_days(date(2026, 3, 9), 1) == date(2026, 3, 10)

    def test_add_five_to_monday_lands_next_monday(self):
        # 5 business days = Tue/Wed/Thu/Fri/Mon
        assert add_business_days(date(2026, 3, 9), 5) == date(2026, 3, 16)

    def test_add_zero_returns_start_unchanged(self):
        assert add_business_days(date(2026, 3, 10), 0) == date(2026, 3, 10)
