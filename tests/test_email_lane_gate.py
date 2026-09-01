"""Tests for the email-lane kill switch (``OUTBOUND_EMAIL_ENABLED``).

The drip senders (``email-daily``, ``email-wave2``) are disarmed by default:
most installs never run the email drip, and a mis-typed ``--yes`` would mail
every contact parked at stage ``queued`` for real. The switch blocks LIVE sends
only — ``--dry-run`` stays usable so an operator can inspect the queue without
arming anything.

``tests/conftest.py`` arms the flag suite-wide so the existing send-path tests
keep exercising the send path; every test here deletes it again in its own
scope.
"""

from unittest.mock import MagicMock, patch

import pytest

from workflows.email_lane_gate import (
    ENV_FLAG,
    EmailLaneDisabledError,
    assert_email_lane_enabled,
    email_lane_enabled,
)


@pytest.fixture(autouse=True)
def _disarmed(monkeypatch):
    """Remove the suite-wide arming so each test starts from the shipped default."""
    monkeypatch.delenv(ENV_FLAG, raising=False)


class TestEmailLaneEnabled:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "True", "yes", "on", " 1 "])
    def test_truthy_values_arm_the_lane(self, monkeypatch, value):
        monkeypatch.setenv(ENV_FLAG, value)
        assert email_lane_enabled() is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe", "2"])
    def test_falsy_values_leave_it_disarmed(self, monkeypatch, value):
        monkeypatch.setenv(ENV_FLAG, value)
        assert email_lane_enabled() is False

    def test_unset_is_disarmed(self):
        assert email_lane_enabled() is False


class TestAssertEmailLaneEnabled:
    def test_raises_when_disarmed(self):
        with pytest.raises(EmailLaneDisabledError) as exc:
            assert_email_lane_enabled("email-daily")
        message = str(exc.value)
        # The message has to tell a tired human what to do, in one read.
        assert "email-daily" in message
        assert ENV_FLAG in message
        assert "--dry-run" in message

    def test_dry_run_is_exempt(self):
        assert_email_lane_enabled("email-daily", dry_run=True)  # must not raise

    def test_armed_passes(self, monkeypatch):
        monkeypatch.setenv(ENV_FLAG, "1")
        assert_email_lane_enabled("email-daily")  # must not raise


class TestDripSendersRefuseWhenDisarmed:
    """The gate must fire before ANY CRM read, send, or state write."""

    def test_email_daily_refuses_live_run(self):
        from workflows.email_campaign import run_email_daily

        attio = MagicMock()
        resend = MagicMock()

        with pytest.raises(EmailLaneDisabledError):
            run_email_daily(attio, resend, dry_run=False, auto_confirm=True)

        attio.search_people.assert_not_called()
        resend.send_email.assert_not_called()

    def test_wave2_refuses_live_run(self):
        from workflows.wave2_blast import run_wave2_blast

        attio = MagicMock()
        resend = MagicMock()

        with pytest.raises(EmailLaneDisabledError):
            run_wave2_blast(attio, resend, dry_run=False, auto_confirm=True)

        attio.search_people.assert_not_called()
        resend.send_email.assert_not_called()

    def test_email_daily_gate_precedes_the_weekend_return(self):
        """A weekend live run must abort on the lane, not return 'weekend'.

        Ordering regression guard: if the gate ever moves below the weekday
        check, a Saturday run would exit 0 and hide the disarmed state.
        """
        from workflows.email_campaign import run_email_daily

        with patch("workflows.email_campaign.is_weekday", return_value=False), \
                pytest.raises(EmailLaneDisabledError):
            run_email_daily(MagicMock(), MagicMock(), dry_run=False, auto_confirm=True)

    def test_wave2_gate_precedes_the_weekend_return(self):
        from workflows.wave2_blast import run_wave2_blast

        with patch("workflows.wave2_blast.is_weekday", return_value=False), \
                pytest.raises(EmailLaneDisabledError):
            run_wave2_blast(MagicMock(), MagicMock(), dry_run=False, auto_confirm=True)


class TestDryRunStaysUsable:
    """Disarming must not take the preview away — that is how you inspect the queue."""

    def test_email_daily_dry_run_is_not_blocked(self):
        from workflows.email_campaign import run_email_daily

        attio = MagicMock()
        attio.search_people.return_value = []

        with patch("workflows.email_campaign.build_suppression_set", return_value=set()), \
                patch("workflows.email_campaign._build_linkedin_collision_set", return_value=set()), \
                patch("workflows.email_campaign.is_weekday", return_value=True):
            result = run_email_daily(attio, None, dry_run=True, auto_confirm=True)

        assert result["sent"] == 0

    def test_wave2_dry_run_is_not_blocked(self):
        from workflows.wave2_blast import run_wave2_blast

        attio = MagicMock()
        attio.search_people.return_value = []

        with patch("workflows.wave2_blast.build_suppression_set", return_value=set()), \
                patch("workflows.wave2_blast._build_linkedin_collision_set", return_value=set()), \
                patch("workflows.wave2_blast.is_weekday", return_value=True):
            result = run_wave2_blast(attio, None, dry_run=True, auto_confirm=True)

        assert result["sent"] == 0
