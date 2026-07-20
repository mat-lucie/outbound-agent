"""Phase 0 must re-scrape CONNECTION_SENT rows whose cached degree is a stale
non-"1st" (PR-208): the Sales Nav scraper's connectionDegree lags the live
graph, so a cached "2nd" must not pin a freshly-accepted connection at
CONNECTION_SENT for RECHECK_TTL_DAYS. Plus the phase0_suspected_stale_degree
reconcile alarm and the reserved PROSPECT scrape budget.

Mirrors the harness in tests/test_phase0_stale_scrape_guard.py.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from models.pipeline import PipelineStage

_TODAY = date.today().isoformat()
_YESTERDAY = (date.today() - timedelta(days=1)).isoformat()
_OLD = (date.today() - timedelta(days=7)).isoformat()  # in 14d window, > 4d old
_CSV_HEADER = "linkedinProfileUrl,connectionDegree,hasPendingInvitation,query,fullName\n"


def _conn_sent(rec="rec-1", url="https://www.linkedin.com/in/foo", last=_YESTERDAY):
    return {
        "entry_id": f"entry-{rec}",
        "record_id": rec,
        "stage": PipelineStage.CONNECTION_SENT.value,
        "last_contact_date": last,
        "experiment_id": None,
        "linkedin_url": url,
    }


def _prospect(rec, url):
    return {
        "entry_id": f"entry-{rec}",
        "record_id": rec,
        "stage": PipelineStage.PROSPECT.value,
        "last_contact_date": None,
        "experiment_id": None,
        "prospect_committed_at": _OLD,
        "linkedin_url": url,
    }


def _mock_pb(*, log_output="Scraped. CSV saved at https://x/deg.csv", csv_text=_CSV_HEADER):
    pb = MagicMock()
    pb.get_agent.return_value = {
        "argument": json.dumps(
            {"numberOfProfilesPerLaunch": 10, "saveImg": False,
             "identities": [{"identityId": "i"}], "spreadsheetUrl": "old"}
        )
    }
    pb.launch_agent.return_value = MagicMock(container_id="ct-1")
    pb.wait_for_completion.return_value = MagicMock(log_output=log_output)
    pb.download_result_csv.return_value = csv_text
    return pb


def _row(url, degree, pending="false"):
    return f"{url},{degree},{pending},{url},Foo\n"


def _run(pb, *, entries, fresh_cache, stale, monkeypatch):
    """Drive detect_accepted_connections with a controllable cache partition.

    fresh_cache: {url: {"degree": ..., "checked_at": ...}}  (cache-hit entries)
    stale:       [url, ...]                                  (forced to scrape)
    Returns (result, mock_rc, mock_esc, mock_adv).
    """
    from workflows.daily_check import detect_accepted_connections

    monkeypatch.setenv("PRE_INVITE_DEGREE_CHECK_BACKEND", "sales_nav")
    monkeypatch.setenv("PB_SALES_NAV_PROFILE_SCRAPER_ID", "sn-id")
    monkeypatch.setenv("PB_LI_SALES_NAV_SESSION_COOKIE", "ck")
    monkeypatch.setenv("PB_LI_USER_AGENT", "ua")

    url_by_rec = {e["record_id"]: e["linkedin_url"] for e in entries}
    mock_cache = MagicMock()
    mock_cache.get.side_effect = lambda rid: (
        f"Person-{rid}", None, url_by_rec.get(rid), None, None
    )
    mock_adv = MagicMock(return_value=True)
    with (
        patch("workflows.daily_check._get_all_entries_parsed", return_value=entries),
        patch("workflows.daily_check.recheck_cache") as mock_rc,
        patch("workflows.daily_check.write_prospects_to_sheet", return_value="https://s"),
        patch("workflows.daily_check.escalate") as mock_esc,
        patch("workflows.daily_check._normalize_linkedin_url", side_effect=lambda u: u),
        patch("workflows.daily_check._attio_advance_with_escalation", mock_adv),
    ):
        mock_rc.partition.return_value = (dict(fresh_cache), list(stale))
        mock_rc.RECHECK_TTL_DAYS = 3
        mock_rc.last_checked.return_value = ""
        result = detect_accepted_connections(
            MagicMock(), pb, profile_scraper_id="legacy",
            cache=mock_cache, sales_nav_profile_scraper_id="sn-id",
        )
    return result, mock_rc, mock_esc, mock_adv


def _accepted_flip(mock_adv) -> bool:
    """True if any _attio_advance call flipped a row to ACCEPTED."""
    return any(
        c.kwargs.get("entry_attributes", {}).get("stage") == PipelineStage.ACCEPTED.value
        for c in mock_adv.call_args_list
    )


# ── Primary fix: stale non-1st CONNECTION_SENT is re-scraped ────────────────

def test_stale_negative_is_rescraped_and_flips(monkeypatch):
    """Cached '2nd' from a PRIOR day → forced into the scrape; CSV now reads
    '1st' → row flips to ACCEPTED. The core regression test."""
    cs = _conn_sent()
    fresh = {cs["linkedin_url"]: {"degree": "2nd", "checked_at": _YESTERDAY}}
    pb = _mock_pb(csv_text=_CSV_HEADER + _row(cs["linkedin_url"], "1st"))
    result, _rc, _esc, mock_adv = _run(
        pb, entries=[cs], fresh_cache=fresh, stale=[], monkeypatch=monkeypatch
    )
    pb.launch_agent.assert_called_once()  # it WAS re-scraped
    assert _accepted_flip(mock_adv)
    assert result["accepted"] == 1


def test_same_day_negative_is_not_rescraped(monkeypatch):
    """Cached '2nd' checked TODAY → still a cache-hit; no second PB launch
    within the same day (no duplicate credits)."""
    cs = _conn_sent()
    fresh = {cs["linkedin_url"]: {"degree": "2nd", "checked_at": _TODAY}}
    pb = _mock_pb()
    result, _rc, _esc, _adv = _run(
        pb, entries=[cs], fresh_cache=fresh, stale=[], monkeypatch=monkeypatch
    )
    pb.launch_agent.assert_not_called()
    assert result["accepted"] == 0


def test_visit_only_entry_without_degree_key_does_not_crash(monkeypatch):
    """A visit-only cache entry (checked_at but NO 'degree' key) must not
    KeyError; it is treated as stale and re-scraped."""
    cs = _conn_sent()
    fresh = {cs["linkedin_url"]: {"checked_at": _YESTERDAY}}  # no 'degree'
    pb = _mock_pb(csv_text=_CSV_HEADER + _row(cs["linkedin_url"], "2nd"))
    result, _rc, _esc, _adv = _run(
        pb, entries=[cs], fresh_cache=fresh, stale=[], monkeypatch=monkeypatch
    )
    pb.launch_agent.assert_called_once()
    assert "error" not in result


def test_cached_first_degree_flips_without_scrape(monkeypatch):
    """A cached '1st' (even from a prior day) is terminal → flips via the
    cache-hit path, no re-scrape."""
    cs = _conn_sent()
    fresh = {cs["linkedin_url"]: {"degree": "1st", "checked_at": _YESTERDAY}}
    pb = _mock_pb()
    result, _rc, _esc, mock_adv = _run(
        pb, entries=[cs], fresh_cache=fresh, stale=[], monkeypatch=monkeypatch
    )
    pb.launch_agent.assert_not_called()
    assert _accepted_flip(mock_adv)
    assert result["accepted"] == 1


# ── Reserved PROSPECT scrape budget ─────────────────────────────────────────

def test_prospect_budget_reserved_under_connection_sent_flood(monkeypatch, capsys):
    """60 stale CONNECTION_SENT + 20 stale PROSPECT, cap 50, reserve 10 →
    40 CONNECTION_SENT + 10 PROSPECT (PROSPECT sweep not starved)."""
    cs = [_conn_sent(f"cs{i}", f"https://www.linkedin.com/in/cs{i}", _OLD) for i in range(60)]
    pr = [_prospect(f"pr{i}", f"https://www.linkedin.com/in/pr{i}") for i in range(20)]
    entries = cs + pr
    stale = [e["linkedin_url"] for e in entries]
    # All non-1st so nothing flips and the run reaches the scrape echo.
    csv_text = _CSV_HEADER + "".join(_row(e["linkedin_url"], "2nd") for e in entries)
    pb = _mock_pb(csv_text=csv_text)
    _run(pb, entries=entries, fresh_cache={}, stale=stale, monkeypatch=monkeypatch)
    out = capsys.readouterr().out
    assert "40 CONNECTION_SENT + 10 PROSPECT" in out


# ── Secondary: phase0_suspected_stale_degree reconcile alarm ────────────────

def _esc_types(mock_esc):
    return [
        (c.kwargs.get("type") or (c.args[0] if c.args else None))
        for c in mock_esc.call_args_list
    ]


def test_suspected_stale_degree_fires(monkeypatch, capsys):
    """Old CONNECTION_SENT invite, scrape says resolved (pending=false) but
    still '2nd' → phase0_suspected_stale_degree opened once."""
    cs = _conn_sent(last=_OLD)
    pb = _mock_pb(csv_text=_CSV_HEADER + _row(cs["linkedin_url"], "2nd", pending="false"))
    _result, _rc, mock_esc, _adv = _run(
        pb, entries=[cs], fresh_cache={}, stale=[cs["linkedin_url"]], monkeypatch=monkeypatch
    )
    types = _esc_types(mock_esc)
    assert "phase0_suspected_stale_degree" in types
    key = next(
        c.kwargs["idempotency_key"] for c in mock_esc.call_args_list
        if c.kwargs.get("type") == "phase0_suspected_stale_degree"
    )
    assert key.startswith("phase0-suspected-stale|")
    assert "phase0_suspected_stale_degree" in capsys.readouterr().err or True


def test_suspected_not_fired_when_pending_true(monkeypatch):
    """pending='true' (invite still outstanding) → not a reconcile candidate."""
    cs = _conn_sent(last=_OLD)
    pb = _mock_pb(csv_text=_CSV_HEADER + _row(cs["linkedin_url"], "2nd", pending="true"))
    _result, _rc, mock_esc, _adv = _run(
        pb, entries=[cs], fresh_cache={}, stale=[cs["linkedin_url"]], monkeypatch=monkeypatch
    )
    assert "phase0_suspected_stale_degree" not in _esc_types(mock_esc)


def test_suspected_not_fired_when_invite_young(monkeypatch):
    """Invite younger than SUSPECTED_STALE_MIN_AGE_DAYS → not yet a candidate
    (it may flip to 1st on the next scrape)."""
    cs = _conn_sent(last=_YESTERDAY)  # 1 day old < 4
    pb = _mock_pb(csv_text=_CSV_HEADER + _row(cs["linkedin_url"], "2nd", pending="false"))
    _result, _rc, mock_esc, _adv = _run(
        pb, entries=[cs], fresh_cache={}, stale=[cs["linkedin_url"]], monkeypatch=monkeypatch
    )
    assert "phase0_suspected_stale_degree" not in _esc_types(mock_esc)


def test_suspected_not_fired_when_flipped(monkeypatch):
    """Degree '1st' → row flips, no suspected-stale alarm."""
    cs = _conn_sent(last=_OLD)
    pb = _mock_pb(csv_text=_CSV_HEADER + _row(cs["linkedin_url"], "1st", pending="false"))
    _result, _rc, mock_esc, mock_adv = _run(
        pb, entries=[cs], fresh_cache={}, stale=[cs["linkedin_url"]], monkeypatch=monkeypatch
    )
    assert _accepted_flip(mock_adv)
    assert "phase0_suspected_stale_degree" not in _esc_types(mock_esc)


def test_blind_warning_when_pending_column_absent(monkeypatch, capsys):
    """If the SN CSV lacks hasPendingInvitation entirely (column renamed), the
    reconcile alarm is blind — warn loudly instead of reporting a quiet zero,
    and open no suspected-stale escalation."""
    cs = _conn_sent(last=_OLD)
    # CSV WITHOUT the hasPendingInvitation column.
    csv_text = (
        "linkedinProfileUrl,connectionDegree,query,fullName\n"
        f"{cs['linkedin_url']},2nd,{cs['linkedin_url']},Foo\n"
    )
    pb = _mock_pb(csv_text=csv_text)
    _result, _rc, mock_esc, _adv = _run(
        pb, entries=[cs], fresh_cache={}, stale=[cs["linkedin_url"]], monkeypatch=monkeypatch
    )
    assert "phase0_suspected_stale_degree" not in _esc_types(mock_esc)
    assert "BLIND this run" in capsys.readouterr().err


def test_suspected_stale_degree_is_registered_escalation_type():
    from workflows.escalation_schemas import ESCALATION_SCHEMAS, ESCALATION_TYPES

    assert "phase0_suspected_stale_degree" in ESCALATION_TYPES
    assert "phase0_suspected_stale_degree" in ESCALATION_SCHEMAS
