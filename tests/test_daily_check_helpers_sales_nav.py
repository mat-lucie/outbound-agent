"""PR-B.9 (Sales Nav migration) — pure-helper coverage for
:func:`workflows.daily_check_helpers._pb_sales_nav_session_args`.

The helper is the contract surface for injecting the Sales Nav session
cookie into the saved PB phantom argument. A regression here silently
breaks every Sales Nav launch — the phantom rejects partial argument
objects with "Your Phantom Argument Isn't Valid" (verified 2026-05-25
against phantom 602790655114603), so a missing cookie or wrong shape
means the next scrape returns 0 rows and the §3.1-hardened partition
escalates every prospect to `degree_unknown`.
"""
from __future__ import annotations

import pytest

from workflows.daily_check_helpers import (
    SalesNavConfigError,
    _pb_sales_nav_session_args,
)


class TestPbSalesNavSessionArgs:
    def test_returns_cookie_and_ua_when_both_set(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("PB_LI_SALES_NAV_SESSION_COOKIE", "fake-li-at")
        monkeypatch.setenv("PB_LI_USER_AGENT", "Mozilla/5.0 (test)")

        args = _pb_sales_nav_session_args()

        assert args == {
            "sessionCookie": "fake-li-at",
            "userAgent": "Mozilla/5.0 (test)",
        }

    def test_omits_user_agent_when_env_var_empty(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        """PB_LI_USER_AGENT is OPTIONAL — when unset, the helper must omit
        the `userAgent` key entirely (not pass an empty string, which the
        phantom may interpret as a literal empty UA and reject).
        """
        monkeypatch.setenv("PB_LI_SALES_NAV_SESSION_COOKIE", "fake-li-at")
        monkeypatch.delenv("PB_LI_USER_AGENT", raising=False)

        args = _pb_sales_nav_session_args()

        assert args == {"sessionCookie": "fake-li-at"}
        assert "userAgent" not in args

    def test_omits_user_agent_when_env_var_whitespace(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        """Whitespace-only UA is treated as missing — the .strip() guard
        prevents passing `"   "` to the phantom."""
        monkeypatch.setenv("PB_LI_SALES_NAV_SESSION_COOKIE", "fake-li-at")
        monkeypatch.setenv("PB_LI_USER_AGENT", "   ")

        args = _pb_sales_nav_session_args()

        assert args == {"sessionCookie": "fake-li-at"}

    def test_raises_when_session_cookie_unset(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        """Missing cookie is a fatal config error — the helper must fail
        loud at config-resolve time so callers don't waste a PB launch on
        a 0-row CSV (which under PR-B's §3.1-hardened partition would
        escalate every prospect to `degree_unknown`)."""
        monkeypatch.delenv("PB_LI_SALES_NAV_SESSION_COOKIE", raising=False)

        with pytest.raises(SalesNavConfigError) as exc_info:
            _pb_sales_nav_session_args()
        assert "PB_LI_SALES_NAV_SESSION_COOKIE" in str(exc_info.value)

    def test_raises_when_session_cookie_empty_string(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        """Empty-string cookie env var is treated identically to unset.
        Operators sometimes paste an empty value when rotating; the helper
        must catch that case too."""
        monkeypatch.setenv("PB_LI_SALES_NAV_SESSION_COOKIE", "")

        with pytest.raises(SalesNavConfigError):
            _pb_sales_nav_session_args()

    def test_raises_when_session_cookie_whitespace_only(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        """Whitespace-only cookie is treated as missing — same as empty.
        The .strip() guard mirrors the empty-string path."""
        monkeypatch.setenv("PB_LI_SALES_NAV_SESSION_COOKIE", "   \n  ")

        with pytest.raises(SalesNavConfigError):
            _pb_sales_nav_session_args()

    def test_error_message_names_the_runbook(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        """The error must point operators at the cookie-rotation runbook
        so they can resolve without reading the source."""
        monkeypatch.delenv("PB_LI_SALES_NAV_SESSION_COOKIE", raising=False)

        with pytest.raises(SalesNavConfigError) as exc_info:
            _pb_sales_nav_session_args()
        msg = str(exc_info.value)
        assert "phantombuster-cookie-rotation" in msg
        assert "DevTools" in msg or ".env" in msg
