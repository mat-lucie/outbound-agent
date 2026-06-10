"""Per-launch csvName must bust the SN phantom's filename-keyed dedup DB,
and the dedup marker in a container log must be loudly surfaced."""

from __future__ import annotations

import json
import re
from unittest.mock import MagicMock

from clients.pb_envelope import has_scraper_dedup_marker


def test_has_scraper_dedup_marker_variants():
    assert has_scraper_dedup_marker("We've already scraped all these profiles")
    assert has_scraper_dedup_marker("We already processed every profile from this input")
    assert not has_scraper_dedup_marker("Scraped 5 profiles successfully")
    assert not has_scraper_dedup_marker("")
    assert not has_scraper_dedup_marker(None)


def test_fresh_csv_name_format_and_uniqueness():
    from workflows.daily_check_helpers import _fresh_csv_name

    a = _fresh_csv_name("deg")
    b = _fresh_csv_name("deg")
    assert re.fullmatch(r"deg-\d{8}-\d{6}-\d{6}", a)
    assert a != b  # microsecond suffix → unique within one process


def _saved_argument() -> dict:
    return {
        "numberOfProfilesPerLaunch": 10,
        "saveImg": False,
        "identities": [{"identityId": "id-1", "sessionCookie": "OLD", "userAgent": "OLD"}],
        "spreadsheetUrl": "old-url",
    }


def _mock_pb(log_output: str = "") -> MagicMock:
    pb = MagicMock()
    pb.get_agent.return_value = {"argument": json.dumps(_saved_argument())}
    pb.launch_agent.return_value = MagicMock(container_id="ct-9")
    pb.wait_for_completion.return_value = MagicMock(log_output=log_output)
    pb.download_result_csv.return_value = (
        "linkedinProfileUrl,connectionDegree,hasPendingInvitation,query,fullName\n"
        "https://www.linkedin.com/in/foo/,2nd,false,https://www.linkedin.com/in/foo/,Foo Bar\n"
    )
    return pb


def _run(pb, monkeypatch):
    from workflows.pre_invite_check import _launch_sales_nav_scrape

    monkeypatch.setenv("PB_LI_SALES_NAV_SESSION_COOKIE", "ck")
    monkeypatch.setenv("PB_LI_USER_AGENT", "ua")
    return _launch_sales_nav_scrape(
        pb, "sn-id", ["https://www.linkedin.com/in/foo/"], retry_on_timeout=False
    )


def test_launch_args_include_fresh_csv_name(monkeypatch):
    pb = _mock_pb()
    _run(pb, monkeypatch)
    args = pb.launch_agent.call_args[0][1]
    assert re.fullmatch(r"deg-\d{8}-\d{6}-\d{6}", args["csvName"])
    # Full saved shape still spread (SN phantom rejects partial args).
    assert args["saveImg"] is False
    assert args["identities"][0]["sessionCookie"] == "ck"


def test_download_uses_matching_csv_name(monkeypatch):
    pb = _mock_pb()
    _run(pb, monkeypatch)
    launch_csv_name = pb.launch_agent.call_args[0][1]["csvName"]
    assert pb.download_result_csv.call_args.kwargs["csv_name"] == launch_csv_name


def test_dedup_marker_emits_loud_warning(monkeypatch, capsys):
    pb = _mock_pb(log_output="We've already scraped all these profiles")
    _run(pb, monkeypatch)
    err = capsys.readouterr().err
    assert "dedup" in err.lower() or "already scraped" in err.lower()
    assert "ct-9" in err


def test_retry_uses_fresh_csv_name(monkeypatch):
    """On timeout retry, the second attempt gets a DIFFERENT csvName."""
    from clients.pb_envelope import PBRunTimeout
    from workflows.pre_invite_check import _launch_sales_nav_scrape

    monkeypatch.setenv("PB_LI_SALES_NAV_SESSION_COOKIE", "ck")
    monkeypatch.setenv("PB_LI_USER_AGENT", "ua")

    pb = MagicMock()
    pb.get_agent.return_value = {"argument": json.dumps(_saved_argument())}
    # First launch → timeout; second launch → success
    timeout_exc = PBRunTimeout(
        container_id="c1",
        agent_id="a",
        elapsed_seconds=1,
        last_observed_status=None,
        last_observed_output=None,
    )
    pb.launch_agent.return_value = MagicMock(container_id="ct-retry")
    pb.wait_for_completion.side_effect = [timeout_exc, MagicMock(log_output="")]
    pb.download_result_csv.return_value = (
        "linkedinProfileUrl,connectionDegree,hasPendingInvitation,query,fullName\n"
        "https://www.linkedin.com/in/foo/,2nd,false,https://www.linkedin.com/in/foo/,Foo\n"
    )

    _launch_sales_nav_scrape(
        pb, "sn-id", ["https://www.linkedin.com/in/foo/"],
        retry_on_timeout=True,
    )

    assert pb.launch_agent.call_count == 2
    first_csv_name = pb.launch_agent.call_args_list[0][0][1]["csvName"]
    second_csv_name = pb.launch_agent.call_args_list[1][0][1]["csvName"]
    assert re.fullmatch(r"deg-\d{8}-\d{6}-\d{6}", first_csv_name)
    assert re.fullmatch(r"deg-\d{8}-\d{6}-\d{6}", second_csv_name)
    assert first_csv_name != second_csv_name, (
        "Retry must use a fresh csvName — reusing the first would defeat the dedup-bust"
    )
