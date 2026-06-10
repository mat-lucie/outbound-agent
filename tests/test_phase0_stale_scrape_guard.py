"""Phase 0 must halt loudly — not report a confident zero — when the scraper
returns no fresh rows for the submitted batch (dedup refusal or match-back
failure), mirroring the silent-zero rule."""

from __future__ import annotations

import json
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from models.pipeline import PipelineStage

_RECENT = (date.today() - timedelta(days=1)).isoformat()
_CSV_HEADER = "linkedinProfileUrl,connectionDegree,hasPendingInvitation,query,fullName\n"


def _conn_sent(url="https://www.linkedin.com/in/testperson/"):
    return {
        "entry_id": "entry-1",
        "record_id": "rec-1",
        "stage": PipelineStage.CONNECTION_SENT.value,
        "last_contact_date": _RECENT,
        "experiment_id": None,
        "linkedin_url": url,
    }


def _mock_pb(*, log_output: str, csv_text: str) -> MagicMock:
    pb = MagicMock()
    pb.get_agent.return_value = {
        "argument": json.dumps(
            {"numberOfProfilesPerLaunch": 10, "saveImg": False,
             "identities": [{"identityId": "i"}], "spreadsheetUrl": "old"}
        )
    }
    pb.launch_agent.return_value = MagicMock(container_id="ct-77")
    pb.wait_for_completion.return_value = MagicMock(log_output=log_output)
    pb.download_result_csv.return_value = csv_text
    return pb


def _run_phase0(pb, monkeypatch):
    from workflows.daily_check import detect_accepted_connections

    monkeypatch.setenv("PRE_INVITE_DEGREE_CHECK_BACKEND", "sales_nav")
    monkeypatch.setenv("PB_SALES_NAV_PROFILE_SCRAPER_ID", "sn-id")
    monkeypatch.setenv("PB_LI_SALES_NAV_SESSION_COOKIE", "ck")
    monkeypatch.setenv("PB_LI_USER_AGENT", "ua")
    attrs = _conn_sent()
    mock_cache = MagicMock()
    mock_cache.get.return_value = ("Person", None, attrs["linkedin_url"], None, None)
    with (
        patch("workflows.daily_check._get_all_entries_parsed", return_value=[attrs]),
        patch("workflows.daily_check.recheck_cache") as mock_rc,
        patch("workflows.daily_check.write_prospects_to_sheet", return_value="https://s"),
        patch("workflows.daily_check.escalate") as mock_esc,
        patch(
            "workflows.daily_check._normalize_linkedin_url",
            side_effect=lambda u: u,
        ),
        patch(
            "workflows.daily_check._attio_advance_with_escalation",
            return_value=True,
        ),
    ):
        mock_rc.partition.return_value = ({}, [attrs["linkedin_url"]])
        mock_rc.RECHECK_TTL_DAYS = 3
        result = detect_accepted_connections(
            MagicMock(), pb, profile_scraper_id="legacy",
            cache=mock_cache, sales_nav_profile_scraper_id="sn-id",
        )
    return result, mock_rc, mock_esc


def test_zero_matched_rows_halts_no_stamp_and_escalates(monkeypatch, capsys):
    # Fresh per-launch CSV came back header-only: dedup refusal.
    pb = _mock_pb(
        log_output="We've already scraped all these profiles",
        csv_text=_CSV_HEADER,
    )
    result, mock_rc, mock_esc = _run_phase0(pb, monkeypatch)
    assert result["error"] == "stale_scrape"
    mock_rc.record_many.assert_not_called()
    assert mock_esc.call_count == 1
    assert mock_esc.call_args.kwargs["type"] == "phase0_stale_scrape"
    err = capsys.readouterr().err
    assert "❌" in err and "ct-77" in err


def test_zero_matched_without_marker_still_halts(monkeypatch):
    # Match-back failure (URL canonicalization drift) is the same silent zero.
    pb = _mock_pb(log_output="all good", csv_text=_CSV_HEADER)
    result, mock_rc, mock_esc = _run_phase0(pb, monkeypatch)
    assert result["error"] == "stale_scrape"
    mock_rc.record_many.assert_not_called()
    assert mock_esc.call_count == 1


def test_marker_with_fresh_rows_warns_but_continues(monkeypatch, capsys):
    # Partial refusal: fresh row present for our URL → stamp + flip proceed.
    pb = _mock_pb(
        log_output="2 profiles already scraped, 1 scraped",
        csv_text=_CSV_HEADER
        + "https://www.linkedin.com/in/testperson/,2nd,false,https://www.linkedin.com/in/testperson/,Test Person\n",
    )
    result, mock_rc, mock_esc = _run_phase0(pb, monkeypatch)
    assert "error" not in result
    mock_rc.record_many.assert_called_once()
    assert mock_esc.call_count == 1  # visibility escalation still opened
    assert "⚠" in capsys.readouterr().err


def test_clean_scrape_no_escalation(monkeypatch):
    pb = _mock_pb(
        log_output="Scraped 1 profile. CSV saved at https://x/y/deg-1.csv",
        csv_text=_CSV_HEADER
        + "https://www.linkedin.com/in/testperson/,1st,false,https://www.linkedin.com/in/testperson/,Test Person\n",
    )
    result, mock_rc, mock_esc = _run_phase0(pb, monkeypatch)
    assert "error" not in result
    mock_esc.assert_not_called()
    mock_rc.record_many.assert_called_once()


def test_phase0_launch_includes_fresh_csv_name(monkeypatch):
    import re

    pb = _mock_pb(log_output="ok", csv_text=_CSV_HEADER)
    _run_phase0(pb, monkeypatch)
    args = pb.launch_agent.call_args[0][1]
    assert re.fullmatch(r"deg-\d{8}-\d{6}-\d{6}", args["csvName"])
    assert pb.download_result_csv.call_args.kwargs["csv_name"] == args["csvName"]


def test_stale_scrape_is_registered_escalation_type():
    from workflows.escalation_schemas import ESCALATION_TYPES

    assert "phase0_stale_scrape" in ESCALATION_TYPES


# ── FIX 1: regular backend gets per-launch csvName ───────────────────────────

def _run_phase0_regular(pb, monkeypatch):
    """Variant of _run_phase0 using PRE_INVITE_DEGREE_CHECK_BACKEND=regular."""
    from workflows.daily_check import detect_accepted_connections

    monkeypatch.setenv("PRE_INVITE_DEGREE_CHECK_BACKEND", "regular")
    monkeypatch.setenv("PB_LI_SESSION_COOKIE", "ck")
    monkeypatch.setenv("PB_LI_USER_AGENT", "ua")
    attrs = _conn_sent()
    mock_cache = MagicMock()
    mock_cache.get.return_value = ("Person", None, attrs["linkedin_url"], None, None)
    with (
        patch("workflows.daily_check._get_all_entries_parsed", return_value=[attrs]),
        patch("workflows.daily_check.recheck_cache") as mock_rc,
        patch("workflows.daily_check.write_prospects_to_sheet", return_value="https://s"),
        patch("workflows.daily_check.escalate") as mock_esc,
        patch(
            "workflows.daily_check._normalize_linkedin_url",
            side_effect=lambda u: u,
        ),
        patch(
            "workflows.daily_check._attio_advance_with_escalation",
            return_value=True,
        ),
    ):
        mock_rc.partition.return_value = ({}, [attrs["linkedin_url"]])
        mock_rc.RECHECK_TTL_DAYS = 3
        result = detect_accepted_connections(
            MagicMock(), pb, profile_scraper_id="legacy",
            cache=mock_cache,
        )
    return result, mock_rc, mock_esc


def test_regular_backend_launch_includes_fresh_csv_name(monkeypatch):
    import re

    # CSV header-only → stale-scrape path (no accepted flips), but we only
    # care that the launch arg and download call both got a fresh name.
    pb = _mock_pb(log_output="ok", csv_text=_CSV_HEADER)
    _run_phase0_regular(pb, monkeypatch)
    args = pb.launch_agent.call_args[0][1]
    assert re.fullmatch(r"deg-\d{8}-\d{6}-\d{6}", args["csvName"]), (
        f"Regular backend launch_agent must include a fresh csvName; got {args}"
    )
    assert pb.download_result_csv.call_args.kwargs["csv_name"] == args["csvName"], (
        "download_result_csv must be called with the same csvName as launch_agent"
    )


# ── FIX 2a: no_csv path escalates to Attio + stderr ─────────────────────────

def test_no_csv_escalates(monkeypatch, capsys):
    """download_result_csv returning '' → error=='no_csv' + escalation opened."""
    from workflows.daily_check import detect_accepted_connections

    pb = _mock_pb(log_output="ok", csv_text=_CSV_HEADER)
    pb.download_result_csv.return_value = ""

    attrs = _conn_sent()
    mock_cache = MagicMock()
    mock_cache.get.return_value = ("Person", None, attrs["linkedin_url"], None, None)
    with (
        patch("workflows.daily_check._get_all_entries_parsed", return_value=[attrs]),
        patch("workflows.daily_check.recheck_cache") as mock_rc,
        patch("workflows.daily_check.write_prospects_to_sheet", return_value="https://s"),
        patch("workflows.daily_check.escalate") as mock_esc,
        patch("workflows.daily_check._normalize_linkedin_url", side_effect=lambda u: u),
        patch("workflows.daily_check._attio_advance_with_escalation", return_value=True),
    ):
        mock_rc.partition.return_value = ({}, [attrs["linkedin_url"]])
        mock_rc.RECHECK_TTL_DAYS = 3
        monkeypatch.setenv("PRE_INVITE_DEGREE_CHECK_BACKEND", "sales_nav")
        monkeypatch.setenv("PB_SALES_NAV_PROFILE_SCRAPER_ID", "sn-id")
        monkeypatch.setenv("PB_LI_SALES_NAV_SESSION_COOKIE", "ck")
        monkeypatch.setenv("PB_LI_USER_AGENT", "ua")
        result = detect_accepted_connections(
            MagicMock(), pb, profile_scraper_id="legacy",
            cache=mock_cache, sales_nav_profile_scraper_id="sn-id",
        )

    assert result["error"] == "no_csv"
    assert mock_esc.call_count >= 1
    call_kwargs = mock_esc.call_args.kwargs if mock_esc.call_args.kwargs else mock_esc.call_args[1]
    assert call_kwargs["type"] == "phase0_stale_scrape"
    assert call_kwargs["idempotency_key"].endswith("|no_csv")
    err = capsys.readouterr().err
    assert "❌" in err and "BLIND" in err


# ── FIX 2b: idempotency key has flavor suffix (blind vs partial) ─────────────

def test_guard_blind_key_uses_blind_flavor(monkeypatch):
    """Zero rows (without dedup marker) → idempotency_key ends in '|blind'."""
    pb = _mock_pb(log_output="ok", csv_text=_CSV_HEADER)
    _result, _mock_rc, mock_esc = _run_phase0(pb, monkeypatch)
    esc_key = mock_esc.call_args.kwargs.get(
        "idempotency_key", mock_esc.call_args[1].get("idempotency_key", "")
    )
    assert esc_key.endswith("|blind"), f"Expected key ending in '|blind', got {esc_key!r}"


def test_guard_partial_key_uses_partial_flavor(monkeypatch):
    """Dedup marker present + rows > 0 → idempotency_key ends in '|partial'."""
    pb = _mock_pb(
        log_output="2 profiles already scraped, 1 scraped",
        csv_text=_CSV_HEADER
        + "https://www.linkedin.com/in/testperson/,2nd,false,https://www.linkedin.com/in/testperson/,Test Person\n",
    )
    _result, _mock_rc, mock_esc = _run_phase0(pb, monkeypatch)
    esc_key = mock_esc.call_args.kwargs.get(
        "idempotency_key", mock_esc.call_args[1].get("idempotency_key", "")
    )
    assert esc_key.endswith("|partial"), f"Expected key ending in '|partial', got {esc_key!r}"


# ── FIX 2c: escalation failure must not crash the run ────────────────────────

def test_guard_survives_escalate_failure(monkeypatch, capsys):
    """If escalate() raises, the guard returns error=='stale_scrape' (not crash)."""
    from workflows.daily_check import detect_accepted_connections

    pb = _mock_pb(log_output="ok", csv_text=_CSV_HEADER)

    attrs = _conn_sent()
    mock_cache = MagicMock()
    mock_cache.get.return_value = ("Person", None, attrs["linkedin_url"], None, None)
    with (
        patch("workflows.daily_check._get_all_entries_parsed", return_value=[attrs]),
        patch("workflows.daily_check.recheck_cache") as mock_rc,
        patch("workflows.daily_check.write_prospects_to_sheet", return_value="https://s"),
        patch("workflows.daily_check.escalate", side_effect=RuntimeError("attio down")),
        patch("workflows.daily_check._normalize_linkedin_url", side_effect=lambda u: u),
        patch("workflows.daily_check._attio_advance_with_escalation", return_value=True),
    ):
        mock_rc.partition.return_value = ({}, [attrs["linkedin_url"]])
        mock_rc.RECHECK_TTL_DAYS = 3
        monkeypatch.setenv("PRE_INVITE_DEGREE_CHECK_BACKEND", "sales_nav")
        monkeypatch.setenv("PB_SALES_NAV_PROFILE_SCRAPER_ID", "sn-id")
        monkeypatch.setenv("PB_LI_SALES_NAV_SESSION_COOKIE", "ck")
        monkeypatch.setenv("PB_LI_USER_AGENT", "ua")
        result = detect_accepted_connections(
            MagicMock(), pb, profile_scraper_id="legacy",
            cache=mock_cache, sales_nav_profile_scraper_id="sn-id",
        )

    assert result["error"] == "stale_scrape"
    mock_rc.record_many.assert_not_called()
    err = capsys.readouterr().err
    assert "could not open" in err
