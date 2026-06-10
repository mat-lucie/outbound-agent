"""Tests for workflows/association_outreach.py — weekday gate."""

from unittest.mock import patch

from workflows.association_outreach import run_association_outreach


class TestWeekendGate:
    @patch("workflows.association_outreach.is_send_day")
    def test_skips_on_weekend(self, mock_is_send_day):
        mock_is_send_day.return_value = False

        result = run_association_outreach(resend=None, dry_run=True)

        assert result["reason"] == "weekend"
        assert result["sent"] == 0
        assert result["pending"] == 0

    @patch("workflows.association_outreach.get_pending_association_emails")
    @patch("workflows.association_outreach.is_send_day")
    def test_force_weekend_overrides_skip(self, mock_is_send_day, mock_pending):
        # Saturday + force_weekend=True → gate is bypassed; pending list consulted.
        mock_is_send_day.return_value = False
        mock_pending.return_value = []  # no pending → workflow exits cleanly past gate

        result = run_association_outreach(resend=None, dry_run=True, force_weekend=True)

        assert result.get("reason") != "weekend"
        # No pending → "all recipients already contacted" path; sent stays 0.
        assert result["sent"] == 0
