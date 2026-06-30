"""Legacy-scraper-deletion hardening.

The legacy LinkedIn Profile Scraper agent (PB_PROFILE_SCRAPER_ID) was
deleted from the PhantomBuster workspace — verified via GET
/api/v2/agents/fetch returning 404 "Agent not found". Two protections:

1. ``DEGREE_CHECK_BACKEND_DEFAULT`` flipped to ``sales_nav`` (covered in
   tests/test_pr15_strict_degree.py::TestResolveDegreeCheckBackend) so a
   missing/typo'd env var fails loud at resolve time instead of silently
   selecting a backend that 404s mid-launch.
2. ``preflight_legacy_profile_scraper`` runs at the top of every remaining
   ``backend=regular`` launch site (Phase 0 acceptance detection + the
   pre-invite degree check) so an explicitly selected dead legacy id fails
   as an actionable config error BEFORE any sheet write or PB launch —
   not as a raw httpx 404 buried mid-run. This file covers the preflight
   unit behavior and both call-site integrations.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from workflows.daily_check_helpers import (
    LegacyScraperGoneError,
    SalesNavConfigError,
    build_sales_nav_launch_args,
    preflight_legacy_profile_scraper,
)

# Stand-in for the deleted legacy agent id. Kept generic on purpose — the
# real PB agent id never appears in this repo.
_DEAD_LEGACY_ID = "legacy-id-deleted"


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.phantombuster.com/api/v2/agents/fetch")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(
        f"{status_code} error", request=request, response=response
    )


class TestPreflightUnit:
    def test_resolving_agent_passes(self):
        pb = MagicMock()
        pb.get_agent.return_value = {"id": "legacy-id", "name": "Profile Scraper"}
        preflight_legacy_profile_scraper(pb, "legacy-id")
        pb.get_agent.assert_called_once_with("legacy-id")

    def test_404_raises_actionable_config_error(self):
        pb = MagicMock()
        pb.get_agent.side_effect = _http_status_error(404)
        with pytest.raises(LegacyScraperGoneError, match="deleted") as excinfo:
            preflight_legacy_profile_scraper(pb, _DEAD_LEGACY_ID)
        # The message must name the dead id and both remedies.
        msg = str(excinfo.value)
        assert _DEAD_LEGACY_ID in msg
        assert "sales_nav" in msg
        assert "PB_PROFILE_SCRAPER_ID" in msg

    def test_non_404_http_error_propagates_unwrapped(self):
        """A 500/429 from PB is a transient API problem, not a deleted
        agent — wrapping it as LegacyScraperGoneError would tell the
        operator to rewire config that is actually fine."""
        pb = MagicMock()
        pb.get_agent.side_effect = _http_status_error(500)
        with pytest.raises(httpx.HTTPStatusError):
            preflight_legacy_profile_scraper(pb, "legacy-id")


class TestSalesNavScraperGone:
    """The SN scraper is exposed to the same deleted-agent event class that
    killed the legacy scraper. build_sales_nav_launch_args wraps the 404 so
    all three SN launch sites fail with the env var named, not a raw httpx
    traceback."""

    def test_404_raises_sales_nav_config_error(self, monkeypatch):
        monkeypatch.setenv("PB_LI_SALES_NAV_SESSION_COOKIE", "fake-sn-li-at")
        pb = MagicMock()
        pb.get_agent.side_effect = _http_status_error(404)
        with pytest.raises(
            SalesNavConfigError, match="PB_SALES_NAV_PROFILE_SCRAPER_ID"
        ):
            build_sales_nav_launch_args(
                pb, "sn-id-gone", spreadsheet_url="https://s", launch_count=2
            )
        pb.launch_agent.assert_not_called()

    def test_non_404_http_error_propagates_unwrapped(self, monkeypatch):
        monkeypatch.setenv("PB_LI_SALES_NAV_SESSION_COOKIE", "fake-sn-li-at")
        pb = MagicMock()
        pb.get_agent.side_effect = _http_status_error(429)
        with pytest.raises(httpx.HTTPStatusError):
            build_sales_nav_launch_args(
                pb, "sn-id", spreadsheet_url="https://s", launch_count=2
            )


class TestPhase0LegacyCallSite:
    def test_dead_legacy_id_fails_before_sheet_write_and_launch(self, monkeypatch):
        """Phase 0 backend=regular with a dead agent id must raise the
        actionable error before the production-sheet write and never reach
        pb.launch_agent."""
        from models.pipeline import PipelineStage
        from workflows.daily_check import detect_accepted_connections

        monkeypatch.setenv("PRE_INVITE_DEGREE_CHECK_BACKEND", "regular")
        monkeypatch.setenv("PB_LI_SESSION_COOKIE", "regular-cookie")

        from datetime import date, timedelta

        conn_sent_attrs = {
            "entry_id": "ent-1",
            "record_id": "rec-1",
            "linkedin_url": "https://www.linkedin.com/in/alice/",
            "stage": PipelineStage.CONNECTION_SENT.value,
            # Within Phase 0's re-check window, or the profile is filtered out
            # before the launch path this test exercises.
            "last_contact_date": (date.today() - timedelta(days=3)).isoformat(),
        }
        pb = MagicMock()
        pb.get_agent.side_effect = _http_status_error(404)
        cache = MagicMock()
        cache.get.return_value = (
            "Person", None, conn_sent_attrs["linkedin_url"], None, None,
        )

        with (
            patch(
                "workflows.daily_check._get_all_entries_parsed",
                return_value=[conn_sent_attrs],
            ),
            patch("workflows.daily_check.recheck_cache") as mock_rc,
            patch(
                "workflows.daily_check.write_prospects_to_sheet",
                return_value="https://sheet-url.example",
            ) as mock_sheet,
        ):
            mock_rc.partition.return_value = ({}, [conn_sent_attrs["linkedin_url"]])
            mock_rc.RECHECK_TTL_DAYS = 7
            with pytest.raises(LegacyScraperGoneError):
                detect_accepted_connections(
                    MagicMock(),
                    pb,
                    profile_scraper_id=_DEAD_LEGACY_ID,
                    cache=cache,
                    sales_nav_profile_scraper_id=None,
                )

        mock_sheet.assert_not_called()
        pb.launch_agent.assert_not_called()


class TestPreInviteLegacyCallSite:
    def test_dead_legacy_id_fails_before_sheet_write_and_launch(self, monkeypatch):
        """Pre-invite backend=regular with a dead agent id must raise before
        the production-sheet write (the legacy scrape's input IS that sheet)
        and before any PB launch."""
        from workflows.pre_invite_check import _pre_invite_degree_check

        monkeypatch.setenv("PRE_INVITE_DEGREE_CHECK_BACKEND", "regular")

        batch = [{
            "linkedInUrl": "https://www.linkedin.com/in/alice",
            "message": "hi A",
            "entry_id": "ent-A",
            "record_id": "rec-A",
            "current_stage": "Prospect",
            "experiment_id": "exp-test",
            "experiment_id_frozen_at": "prospect",
        }]
        pb = MagicMock()
        pb.get_agent.side_effect = _http_status_error(404)

        with (
            patch(
                "workflows.daily_check.write_prospects_to_sheet",
                return_value="https://sheet-url.example",
            ) as mock_sheet,
            pytest.raises(LegacyScraperGoneError),
        ):
            _pre_invite_degree_check(
                batch,
                pb,
                _DEAD_LEGACY_ID,
                MagicMock(),  # attio
                "list-id",
            )

        mock_sheet.assert_not_called()
        pb.launch_agent.assert_not_called()
