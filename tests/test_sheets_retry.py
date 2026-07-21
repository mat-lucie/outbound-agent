"""Tests for the gspread transient-error retry in clients/google_sheets.py.

A one-off Google Sheets 503 on ``ws.clear()`` took down a whole ``daily``
run mid-Phase-0 (PR-239 incident). ``_with_retry`` now rides a small 5/10/20s
backoff on 429/500/502/503 so the mutating Sheets calls survive a transient
blip. A non-retryable status (e.g. 403 permission) still fails fast.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import gspread
import pytest
import requests

from clients.google_sheets import _with_retry, write_prospects_to_sheet


def _api_error(status: int) -> gspread.exceptions.APIError:
    """Build a gspread APIError whose response carries ``status`` — the shape
    ``_with_retry`` inspects via ``exc.response.status_code``."""
    resp = requests.models.Response()
    resp.status_code = status
    resp._content = json.dumps(
        {"error": {"code": status, "message": "transient", "status": "UNAVAILABLE"}}
    ).encode()
    return gspread.exceptions.APIError(resp)


def test_retries_then_succeeds():
    """429 twice then success → a single successful return, two sleeps."""
    fn = MagicMock(side_effect=[_api_error(429), _api_error(429), "ok"])
    with patch("clients.google_sheets.time.sleep") as sleep:
        assert _with_retry(fn) == "ok"
    assert fn.call_count == 3
    assert sleep.call_count == 2
    assert [c.args[0] for c in sleep.call_args_list] == [5, 10]


def test_non_retryable_status_raises_immediately():
    """403 (permission) is NOT transient → re-raise on the first hit, no retry,
    no sleep."""
    fn = MagicMock(side_effect=_api_error(403))
    with (
        patch("clients.google_sheets.time.sleep") as sleep,
        pytest.raises(gspread.exceptions.APIError),
    ):
        _with_retry(fn)
    assert fn.call_count == 1
    assert sleep.call_count == 0


def test_final_attempt_reraises():
    """Retryable status on every attempt → the final attempt re-raises."""
    fn = MagicMock(side_effect=[_api_error(503), _api_error(503), _api_error(503)])
    with (
        patch("clients.google_sheets.time.sleep"),
        pytest.raises(gspread.exceptions.APIError),
    ):
        _with_retry(fn)
    assert fn.call_count == 3


def test_happy_path_no_retry():
    """A call that succeeds first time returns immediately, no sleep."""
    fn = MagicMock(return_value="done")
    with patch("clients.google_sheets.time.sleep") as sleep:
        assert _with_retry(fn) == "done"
    assert fn.call_count == 1
    assert sleep.call_count == 0


def test_write_prospects_retries_transient_clear_then_writes():
    """End-to-end: a 503 on ws.clear() is retried and the write still lands
    exactly once (the PR-239 crash scenario, now survivable)."""
    ws = MagicMock()
    ws.clear.side_effect = [_api_error(503), None]  # first blip, then ok
    sh = MagicMock()
    sh.worksheet.return_value = ws

    with (
        patch("clients.google_sheets.get_client") as get_client,
        patch("clients.google_sheets.time.sleep"),
    ):
        get_client.return_value.open_by_key.return_value = sh
        url = write_prospects_to_sheet(
            [{"linkedInUrl": "u1", "message": "m1"}],
            spreadsheet_id="sid123",
        )

    assert ws.clear.call_count == 2  # one retry
    ws.update.assert_called_once()
    assert url == "https://docs.google.com/spreadsheets/d/sid123"
