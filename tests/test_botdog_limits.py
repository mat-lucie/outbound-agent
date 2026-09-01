"""Tests for workflows/botdog_limits.py — the account-limit sync.

Covers the cap-comparison logic (match → ok, mismatch/missing → loud),
account resolution, and the sync policy: report-only NEVER calls
set_account_limits; an explicit apply calls it with the full desired cap
set and re-confirms. The Botdog client is mocked; no live calls.

Why the comparison is asymmetric: Botdog's server-side limits are the
second safety layer behind our own daily lease/ledger. A Botdog limit
BELOW ours can never out-send our policy, so it passes; only a limit
ABOVE ours is a mismatch. A managed key MISSING from the response is
reported loudly, never assumed correct.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from workflows.botdog_limits import (
    CAP_KEY_INVITES,
    CAP_KEY_MESSAGES,
    compare_limits,
    desired_caps,
    format_comparison,
    resolve_account_id,
    sync_limits,
)
from workflows.safety_limits import (
    MAX_CONNECTIONS_PER_DAY,
    MAX_MESSAGES_PER_DAY,
)


class TestDesiredCaps:
    def test_maps_our_caps_to_botdog_keys(self):
        assert desired_caps() == {
            CAP_KEY_INVITES: MAX_CONNECTIONS_PER_DAY,
            CAP_KEY_MESSAGES: MAX_MESSAGES_PER_DAY,
        }


def _limits_shape(invitations: int, messages: int) -> dict:
    """The nested account-limits response shape.

    Includes an unmanaged key (`profileVisits`) on purpose: keys we do not
    manage must be ignored by the comparison, never "corrected".
    """
    return {
        "accountId": "acme-account-1",
        "limits": {
            "invitations": {"limit": invitations, "usedToday": 0},
            "messages": {"limit": messages, "usedToday": 0},
            "profileVisits": {"limit": 50, "usedToday": 0},
        },
    }


class TestCompareLimits:
    def test_match(self):
        cmp = compare_limits(
            {"invitations": 25, "messages": 30}, _limits_shape(25, 30)
        )
        assert cmp.matches
        assert cmp.mismatches == []
        assert cmp.missing == []

    def test_stricter_botdog_limit_passes(self):
        """Botdog BELOW ours is the safe direction — never a mismatch."""
        cmp = compare_limits(
            {"invitations": 25, "messages": 30}, _limits_shape(20, 30)
        )
        assert cmp.matches

    def test_looser_botdog_limit_mismatches(self):
        cmp = compare_limits(
            {"invitations": 25, "messages": 30}, _limits_shape(100, 30)
        )
        assert not cmp.matches
        assert cmp.mismatches == [("invitations", 25, 100)]

    def test_flat_fallback_shape_still_parses(self):
        cmp = compare_limits(
            {"invitations": 25, "messages": 30},
            {"invitations": 100, "messages": 30},
        )
        assert cmp.mismatches == [("invitations", 25, 100)]

    def test_missing_managed_key_is_a_mismatch_not_assumed_ok(self):
        actual = _limits_shape(25, 30)
        del actual["limits"]["messages"]
        cmp = compare_limits({"invitations": 25, "messages": 30}, actual)
        assert not cmp.matches
        assert cmp.missing == ["messages"]

    def test_format_comparison_is_loud_on_mismatch(self):
        cmp = compare_limits({"invitations": 25}, _limits_shape(100, 30))
        text = format_comparison(cmp, {"invitations": 25})
        assert "⚠" in text
        assert "invitations" in text


class TestResolveAccountId:
    def test_explicit_wins(self):
        botdog = MagicMock()
        assert resolve_account_id(botdog, "acc_explicit") == "acc_explicit"
        botdog.get_accounts.assert_not_called()

    def test_single_account_auto_resolves(self):
        botdog = MagicMock()
        botdog.get_accounts.return_value = [{"id": "acc_1"}]
        assert resolve_account_id(botdog) == "acc_1"

    def test_ambiguous_raises(self):
        botdog = MagicMock()
        botdog.get_accounts.return_value = [{"id": "acc_1"}, {"id": "acc_2"}]
        with pytest.raises(ValueError):
            resolve_account_id(botdog)


class TestSyncLimits:
    def test_match_reports_ok_and_never_sets(self):
        botdog = MagicMock()
        botdog.get_account_limits.return_value = _limits_shape(
            MAX_CONNECTIONS_PER_DAY, MAX_MESSAGES_PER_DAY
        )
        report = sync_limits(botdog, account_id="a", apply=True)
        assert report["matches"] is True
        assert report["applied"] is False
        botdog.set_account_limits.assert_not_called()

    def test_report_only_never_sets_even_on_mismatch(self):
        botdog = MagicMock()
        botdog.get_account_limits.return_value = _limits_shape(999, 999)
        report = sync_limits(botdog, account_id="a", apply=False)
        assert report["matches"] is False
        assert report["applied"] is False
        botdog.set_account_limits.assert_not_called()

    def test_apply_sets_full_desired_set_and_reconfirms(self):
        botdog = MagicMock()
        botdog.get_account_limits.side_effect = [
            {"dailyInvites": 999, "dailyMessages": 30},   # before
            desired_caps(),                                # after set
        ]
        report = sync_limits(botdog, account_id="a", apply=True)
        botdog.set_account_limits.assert_called_once_with("a", desired_caps())
        assert report["applied"] is True
        assert report["matches"] is True
