"""PR-17 B-SD-011: run-end summary written onto the daily_run row.

Locks in:
- starvation_signal classification (healthy / low_dmN / multi_low)
- run_end_summary writes the 6 PR-17 attrs via PATCH
- degree_unknown_count aggregates from operator_review_queue
- degree_unknown_count writes None on transport failure (PR-17 fold-in)
- compute_due_dm_counts excludes terminal stages + non-cadence-due rows
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import httpx
import pytest

from clients.crm.attio_provider import AttioProvider
from workflows.daily_check import (
    _classify_starvation_signal,
    _count_degree_unknown_today,
    compute_due_dm_counts,
    run_end_summary,
)
from workflows.daily_run import DailyRun


def _ok_response(body: dict | None = None) -> httpx.Response:
    req = httpx.Request("PATCH", "https://api.attio.com/v2/x")
    return httpx.Response(200, request=req, json=body or {"data": {}})


# ── starvation_signal classifier ─────────────────────────────────────────


@pytest.mark.parametrize(
    "due,expected",
    [
        ((10, 10, 10), "healthy"),
        ((3, 5, 8), "healthy"),  # exactly at floor counts as healthy
        ((0, 5, 5), "low_dm1"),
        ((5, 0, 5), "low_dm2"),
        ((5, 5, 0), "low_dm3"),
        ((0, 0, 5), "multi_low"),
        ((0, 0, 0), "multi_low"),
    ],
)
def test_starvation_signal_classification(due, expected):
    assert _classify_starvation_signal(*due) == expected


# ── degree_unknown_count aggregation ─────────────────────────────────────


def test_count_degree_unknown_returns_attio_row_count_for_today():
    attio = MagicMock()
    today = date(2026, 5, 21)
    attio._request.return_value = {
        "data": [{"id": "r1"}, {"id": "r2"}, {"id": "r3"}]
    }
    # CRM-seam migration: the function now takes a CRMProvider. Wrap the mock
    # AttioClient in a real AttioProvider so query_object_records delegates to
    # the same attio._request the assertions below inspect — behavior identical.
    count = _count_degree_unknown_today(AttioProvider(attio), today)
    assert count == 3
    call = attio._request.call_args
    body = call.kwargs.get("json") or call.args[-1]
    # Attio's query DSL requires an explicit $and wrapper for multi-key
    # filters (a bare two-key object 400s — the 2026-05-28 halt). opened_at
    # is a datetime, so the $gte bound is a full ISO timestamp.
    clauses = body["filter"]["$and"]
    assert {"type": "degree_unknown"} in clauses
    assert {"opened_at": {"$gte": "2026-05-21T00:00:00Z"}} in clauses


def test_count_degree_unknown_returns_none_on_transport_failure(capsys):
    """PR-17 fold-in (silent-failure-hunter BLOCKING): on transport
    failure, return ``None`` rather than ``0`` so the daily_run summary
    distinguishes "no unknowns today" from "we don't know." Stderr WARN
    must fire so the failure is operator-visible."""
    attio = MagicMock()
    req = httpx.Request("POST", "https://api.attio.com/v2/x")
    attio._request.side_effect = httpx.HTTPStatusError(
        "boom", request=req, response=httpx.Response(500, request=req)
    )
    count = _count_degree_unknown_today(AttioProvider(attio), date(2026, 5, 21))
    assert count is None
    captured = capsys.readouterr()
    assert "degree_unknown aggregation failed" in captured.err
    assert "HTTPStatusError" in captured.err


# ── run_end_summary write ────────────────────────────────────────────────


def test_run_end_summary_writes_all_pr17_attrs():
    attio = MagicMock()
    attio._client = MagicMock()
    attio._client.request.return_value = _ok_response()
    attio._request.return_value = {"data": []}

    handle = DailyRun(
        crm=AttioProvider(attio), record_id="rec_dr_abc",
        run_date="2026-05-21", machine_id="host",
        run_id="run-1",
        initial_counters={"connections": 0, "messages": 0, "visits": 0},
    )
    today = date(2026, 5, 21)

    summary = run_end_summary(
        AttioProvider(attio), handle,
        prospect_pool_size=600,
        due_dm1_count=8,
        due_dm2_count=6,
        due_dm3_count=4,
        today=today,
    )

    assert summary == {
        "prospect_pool_size": 600,
        "due_dm1_count": 8,
        "due_dm2_count": 6,
        "due_dm3_count": 4,
        "degree_unknown_count": 0,
        "starvation_signal": "healthy",
    }

    # Asserts the PATCH carries every PR-17 attr but NOT reply_detection_status
    # (PR-19's territory) and NOT nurture_silent_skipped_count (PR-39's).
    patch_call = attio._client.request.call_args
    assert patch_call.args[0] == "PATCH"
    assert "/objects/daily_run/records/rec_dr_abc" in patch_call.args[1]
    values = patch_call.kwargs["json"]["data"]["values"]
    assert set(values.keys()) == {
        "prospect_pool_size", "due_dm1_count", "due_dm2_count",
        "due_dm3_count", "degree_unknown_count", "starvation_signal",
    }
    assert "reply_detection_status" not in values
    assert "nurture_silent_skipped_count" not in values


def test_run_end_summary_emits_starvation_signal_when_dues_low():
    attio = MagicMock()
    attio._client = MagicMock()
    attio._client.request.return_value = _ok_response()
    attio._request.return_value = {"data": []}

    handle = DailyRun(
        crm=AttioProvider(attio), record_id="rec_dr_starve",
        run_date="2026-05-21", machine_id="host",
        run_id="run-2",
        initial_counters={"connections": 0, "messages": 0, "visits": 0},
    )
    summary = run_end_summary(
        AttioProvider(attio), handle,
        prospect_pool_size=600,
        due_dm1_count=1, due_dm2_count=0, due_dm3_count=0,
        today=date(2026, 5, 21),
    )
    assert summary["starvation_signal"] == "multi_low"


def test_run_end_summary_includes_degree_unknown_count():
    attio = MagicMock()
    attio._client = MagicMock()
    attio._client.request.return_value = _ok_response()
    attio._request.return_value = {
        "data": [{"id": "r1"}, {"id": "r2"}]
    }

    handle = DailyRun(
        crm=AttioProvider(attio), record_id="rec_dr_du",
        run_date="2026-05-21", machine_id="host",
        run_id="run-3",
        initial_counters={"connections": 0, "messages": 0, "visits": 0},
    )
    summary = run_end_summary(
        AttioProvider(attio), handle,
        prospect_pool_size=600,
        due_dm1_count=5, due_dm2_count=5, due_dm3_count=5,
        today=date(2026, 5, 21),
    )
    assert summary["degree_unknown_count"] == 2


def test_run_end_summary_omits_degree_unknown_count_when_query_fails(capsys):
    """When the degree_unknown aggregation query fails, the count is None.
    Attio REJECTS JSON null on a number column (validation_type 400 — the
    2026-05-28 crash), so the PATCH must OMIT the field rather than write
    null. The returned summary still carries None for operator-facing
    logging (absence in Attio reads as "unknown"). WARN to stderr confirms
    the aggregation failure is operator-visible.
    """
    attio = MagicMock()
    attio._client = MagicMock()
    attio._client.request.return_value = _ok_response()
    req = httpx.Request("POST", "https://api.attio.com/v2/x")
    attio._request.side_effect = httpx.HTTPStatusError(
        "503 service unavailable", request=req,
        response=httpx.Response(503, request=req),
    )

    handle = DailyRun(
        crm=AttioProvider(attio), record_id="rec_dr_failed",
        run_date="2026-05-21", machine_id="host",
        run_id="run-4",
        initial_counters={"connections": 0, "messages": 0, "visits": 0},
    )
    summary = run_end_summary(
        AttioProvider(attio), handle,
        prospect_pool_size=600,
        due_dm1_count=5, due_dm2_count=5, due_dm3_count=5,
        today=date(2026, 5, 21),
    )

    # Returned dict keeps None (for logging); the PATCH omits it.
    assert summary["degree_unknown_count"] is None
    patch_call = attio._client.request.call_args
    values = patch_call.kwargs["json"]["data"]["values"]
    assert "degree_unknown_count" not in values
    # The other five counters are still written.
    assert values["prospect_pool_size"] == 600
    assert values["starvation_signal"] == "healthy"
    captured = capsys.readouterr()
    assert "degree_unknown aggregation failed" in captured.err


def test_run_end_summary_is_nonfatal_when_attio_write_fails(capsys):
    """Observability lens: a failure writing the run-end summary must NOT
    crash the run or block daily_run closure (the 2026-05-28 crash escalated
    a summary-write rejection to a fatal exit 1). run_end_summary should
    swallow-and-WARN (AttioWriter already opens an attio_write_failed queue
    row for triage) and still return the computed values.
    """
    attio = MagicMock()
    attio._request.return_value = {"data": []}  # degree_unknown count = 0

    handle = DailyRun(
        crm=AttioProvider(attio), record_id="rec_dr_writefail",
        run_date="2026-05-21", machine_id="host",
        run_id="run-5",
        initial_counters={"connections": 0, "messages": 0, "visits": 0},
    )

    with patch("clients.attio_writer.AttioWriter.apply", side_effect=RuntimeError("boom")):
        summary = run_end_summary(
            AttioProvider(attio), handle,
            prospect_pool_size=600,
            due_dm1_count=5, due_dm2_count=5, due_dm3_count=5,
            today=date(2026, 5, 21),
        )

    # No exception propagated; values still returned for logging.
    assert summary["prospect_pool_size"] == 600
    captured = capsys.readouterr()
    assert "run-end summary write failed" in captured.err


def test_run_end_summary_propagates_prewrite_rejections():
    """Pre-write rejections (write-owner registry drift / illegal stage /
    terminal regression) open NO attio_write_failed queue row, so they must
    NOT be swallowed — masking them would re-introduce the silent-failure
    class this PR fixes. They are config bugs and must fail loudly; the
    operational (queue-row-opening) failures stay non-fatal.
    """
    from clients.attio_writer import UnauthorizedAttioWriteError

    attio = MagicMock()
    attio._request.return_value = {"data": []}

    handle = DailyRun(
        crm=AttioProvider(attio), record_id="rec_dr_unauth",
        run_date="2026-05-21", machine_id="host",
        run_id="run-6",
        initial_counters={"connections": 0, "messages": 0, "visits": 0},
    )

    with patch(
        "clients.attio_writer.AttioWriter.apply",
        side_effect=UnauthorizedAttioWriteError("registry drift"),
    ), pytest.raises(UnauthorizedAttioWriteError):
        run_end_summary(
            AttioProvider(attio), handle,
            prospect_pool_size=600,
            due_dm1_count=5, due_dm2_count=5, due_dm3_count=5,
            today=date(2026, 5, 21),
        )


# ── compute_due_dm_counts ────────────────────────────────────────────────


def _entry(record_id: str, stage: str, last_date: str, dm_step: int = 0) -> dict:
    return {
        "record_id": record_id,
        "entry_id": f"ent-{record_id}",
        "stage": stage,
        "last_contact_date": last_date,
        "dm_step": dm_step,
        "persona": "operations_leaders",
        "language": "en",
    }


@patch("workflows.daily_check._get_all_entries_parsed")
def test_compute_due_dm_counts_groups_by_pending_step(_get_entries):
    """Two cadence-due ACCEPTED rows → due_dm1=2; one cadence-due DM1_SENT
    row → due_dm2=1; one terminal stage row → excluded from pool only if
    it doesn't appear in the PipelineStage enum (NOT_INTERESTED is in the
    enum, so it counts toward pool_size but not toward any due_dm).
    """
    today = date(2026, 5, 20)
    _get_entries.return_value = [
        _entry("a1", "Accepted", "2026-05-18", dm_step=0),
        _entry("a2", "Accepted", "2026-05-18", dm_step=0),
        _entry("b1", "DM1 Sent", "2026-05-10", dm_step=1),
        _entry("term", "Not Interested", "2026-04-01", dm_step=0),
        _entry("nodate", "Accepted", "", dm_step=0),  # excluded (no last date)
    ]
    attio = MagicMock()
    cache = MagicMock()
    cache.get.side_effect = lambda rid: (
        f"Name-{rid}", f"Company-{rid}", f"https://linkedin.com/in/{rid}",
        "manufacturing", "Plant Director",
    )

    result = compute_due_dm_counts(attio, cache=cache, today=today)

    assert result["due_dm1_count"] == 2
    assert result["due_dm2_count"] == 1
    assert result["due_dm3_count"] == 0
    assert result["prospect_pool_size"] >= 4
