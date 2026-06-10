"""Tests for the daily_run reattach primitive (wet-run DM approval gate).

Covers the reattach state-machine:
- attach binds the EXISTING (run_date, machine_id) row, never creates a new one
- counter rehydration: remaining("messages") == 30 - N, not 30 (the cap-breach gate)
- reopen-collision → fail closed
- no row for today → fail closed
- malformed counter → fail closed
- running row reattaches as-is (no reopen)
- reply_detection_status rehydrates across the process boundary

The module talks to the CRM through the vendor-neutral ``CRMProvider`` seam, so
the tests inject a ``MagicMock`` provider: ``query_object_records`` returns the
normalized ``Record`` for today's row, ``update_object_record`` stands in for the
reopen / close writes, and a uniqueness collision on reopen is injected as the
neutral ``UniquenessConflictError`` (the same shape the Attio adapter raises).
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from clients.crm.base import Record
from clients.crm.exceptions import UniquenessConflictError
from workflows.daily_run import (
    MAX_MESSAGES_PER_DAY,
    MalformedDailyRunRow,
    NoDailyRunRow,
    ReopenCollision,
    _first_int,
    _first_select,
    _strict_counter,
    attach_daily_run,
    query_todays_row,
)


def _row(
    record_id: str = "rec_today",
    status: str = "completed",
    run_date: str = "2026-06-09",
    machine_id: str = "laptop-A",
    messages_sent: int = 0,
    connections_sent: int = 0,
    visits_sent: int = 0,
    reply_detection_status: str | None = "ok",
) -> Record:
    """A daily_run ``Record`` as the provider returns it.

    ``status`` and ``reply_detection_status`` are SELECT-type attrs; the
    provider normalization already unwrapped the vendor select shape
    (``[{"option": {"title": <v>}}]``) to its plain title string, so by the
    time we read them off ``Record.attributes`` they are scalars — exactly what
    ``_first_select`` consumes."""
    attrs: dict = {
        "status": status,
        "run_date": run_date,
        "machine_id": machine_id,
        "messages_sent": messages_sent,
        "connections_sent": connections_sent,
        "visits_sent": visits_sent,
        "run_id": "prior-run",
    }
    if reply_detection_status is not None:
        attrs["reply_detection_status"] = reply_detection_status
    return Record(
        record_id=record_id,
        object="daily_run",
        attributes=attrs,
        raw={"id": {"record_id": record_id}, "values": attrs},
    )


@pytest.fixture
def mock_crm():
    crm = MagicMock()
    crm.query_object_records.return_value = [_row()]
    crm.update_object_record.return_value = _row()
    return crm


def test_attach_no_row_today_fails_closed(mock_crm):
    """Zero rows for (today, me) → NoDailyRunRow (caller exits EX_TEMPFAIL)."""
    mock_crm.query_object_records.return_value = []
    with pytest.raises(NoDailyRunRow), attach_daily_run(
        mock_crm, run_id="dm-1", run_date=date(2026, 6, 9), machine_id="laptop-A"
    ):
        pass


@pytest.mark.parametrize("terminal_status", ["completed", "failed", "aborted"])
def test_attach_binds_existing_row_no_new_record(mock_crm, terminal_status):
    """Reattach binds the existing (run_date, machine_id) row and creates NO new
    daily_run row. Every terminal status (anything != "running") is reopened via
    a write then re-closed; create_object_record is never called."""
    mock_crm.query_object_records.return_value = [
        _row(status=terminal_status, messages_sent=4)
    ]
    with attach_daily_run(
        mock_crm, run_id="dm-1", run_date=date(2026, 6, 9), machine_id="laptop-A"
    ) as run:
        assert run.record_id == "rec_today"
    # No new row created — only update_object_record (reopen + close).
    mock_crm.create_object_record.assert_not_called()
    # Every terminal status takes the reopen path: the first write flips status
    # back to running.
    reopen_values = mock_crm.update_object_record.call_args_list[0][0][2]
    assert reopen_values["status"] == "running"


def test_attach_rehydrates_messages_sent_into_remaining(mock_crm):
    """CRITICAL cap-breach gate. Reattach to a row with messages_sent=N>0 →
    remaining("messages") == 30 - N, NOT 30.

    A reattach that binds the row but forgets to rehydrate messages_sent starts
    send-dms at 0/30 and lets the operator ship up to 60 DMs on a 30/day cap."""
    n = 11
    mock_crm.query_object_records.return_value = [
        _row(status="completed", messages_sent=n)
    ]
    with attach_daily_run(
        mock_crm, run_id="dm-1", run_date=date(2026, 6, 9), machine_id="laptop-A"
    ) as run:
        assert run.remaining("messages") == MAX_MESSAGES_PER_DAY - n
        assert run.remaining("messages") != MAX_MESSAGES_PER_DAY


def test_attach_does_not_cross_charge_connections_to_messages(mock_crm):
    """Connections vs messages are SEPARATE counters. A row with
    connections_sent=20 but messages_sent=0 must leave the message budget full
    — reattach must not mis-charge invites against DMs."""
    mock_crm.query_object_records.return_value = [
        _row(status="completed", connections_sent=20, messages_sent=0)
    ]
    with attach_daily_run(
        mock_crm, run_id="dm-1", run_date=date(2026, 6, 9), machine_id="laptop-A"
    ) as run:
        assert run.remaining("messages") == MAX_MESSAGES_PER_DAY
        from workflows.daily_run import MAX_CONNECTIONS_PER_DAY
        assert run.remaining("connections") == MAX_CONNECTIONS_PER_DAY - 20


def test_attach_reopen_collision_fails_closed(mock_crm):
    """Reopen-write uniqueness collision (concurrent step-1 retry holds the
    2-part key) → ReopenCollision, send nothing. No close write fires because
    the row was never successfully reopened/bound."""
    mock_crm.query_object_records.return_value = [
        _row(status="completed", messages_sent=3)
    ]
    mock_crm.update_object_record.side_effect = UniquenessConflictError(
        "daily_run", attribute="uniqueness_key"
    )
    with pytest.raises(ReopenCollision), attach_daily_run(
        mock_crm, run_id="dm-1", run_date=date(2026, 6, 9), machine_id="laptop-A"
    ):
        pytest.fail("body must not execute when reopen collides")


@pytest.mark.parametrize(
    "value, expected",
    [
        (5, 5),       # present integer
        (0, 0),       # present real zero
        (None, 0),    # null value → 0 (optional-field semantics)
        ("7", 7),     # numeric string → coerced
        (7.9, 7),     # float → truncated int
    ],
)
def test_first_int_parses_optional_field(value, expected):
    """``_first_int`` is the lenient parser for optional/general fields: a
    missing key / null all default to 0; numeric strings and floats coerce to
    int. Used for non-counter reads only."""
    attrs = {} if value is None else {"messages_sent": value}
    # null distinct from missing: an explicit None still → 0.
    if value is None:
        attrs = {"messages_sent": None}
    assert _first_int(attrs, "messages_sent") == expected


def test_first_int_missing_key_is_zero():
    assert _first_int({}, "messages_sent") == 0


@pytest.mark.parametrize(
    "value, expected",
    [
        (5, 5),     # present integer
        (0, 0),     # a real 0 is VALID — must NOT raise
        ("7", 7),   # numeric string → coerced
        (7.9, 7),   # float → truncated int
    ],
)
def test_strict_counter_accepts_real_values(value, expected):
    """``_strict_counter`` accepts a present integer, a real 0, a numeric
    string, and a float — only absent/null/non-numeric must fail closed."""
    assert _strict_counter({"messages_sent": value}, "messages_sent", "rec_x") == expected


@pytest.mark.parametrize(
    "attrs",
    [
        {},                            # missing key
        {"messages_sent": None},       # null value
        {"messages_sent": "abc"},      # non-numeric string
    ],
)
def test_strict_counter_fails_closed_on_malformed(attrs):
    """A counter that is absent / null / non-numeric on a fetched row signals a
    malformed CRM response. open_daily_run ALWAYS writes the three counters (0 at
    minimum), so absence is never legitimate — fail closed with
    MalformedDailyRunRow rather than silently resetting the cap to 0."""
    with pytest.raises(MalformedDailyRunRow):
        _strict_counter(attrs, "messages_sent", "rec_x")


def test_attach_missing_messages_sent_fails_closed(mock_crm):
    """Reattach to an existing row whose ``messages_sent`` is absent →
    MalformedDailyRunRow, do NOT bind the run, do NOT default to 0.

    Defaulting a missing counter to 0 silently resets the cap to 0/30 and is the
    exact 60-DM cap breach this feature prevents. G(ii) reorder: the strict
    rehydrate runs BEFORE the reopen PATCH, so a malformed TERMINAL row raises
    with NO update_object_record call — a malformed row is never flipped to
    "running" and stranded holding the lock."""
    bad_row = _row(status="completed", messages_sent=4)
    del bad_row.attributes["messages_sent"]
    mock_crm.query_object_records.return_value = [bad_row]
    with pytest.raises(MalformedDailyRunRow), attach_daily_run(
        mock_crm, run_id="dm-1", run_date=date(2026, 6, 9), machine_id="laptop-A"
    ):
        pytest.fail("body must not execute when a counter is malformed")
    # G(ii): no reopen (or any) write fired — the rehydrate raised first.
    mock_crm.update_object_record.assert_not_called()


def test_attach_running_row_no_reopen(mock_crm):
    """A status=running row (step 1 crashed before close) reattaches as-is — no
    reopen write, just the close on exit (state-machine step 3).

    Also the regression guard for the status-read DOA bug: a SELECT
    ``status=running`` must be recognised (not parsed to "") so the running row
    does NOT take the reopen branch — exactly ONE update_object_record (the
    close)."""
    mock_crm.query_object_records.return_value = [
        _row(status="running", messages_sent=2)
    ]
    with attach_daily_run(
        mock_crm, run_id="dm-1", run_date=date(2026, 6, 9), machine_id="laptop-A"
    ) as run:
        assert run.remaining("messages") == MAX_MESSAGES_PER_DAY - 2
    # Exactly one write: the close on exit. No reopen.
    assert mock_crm.update_object_record.call_count == 1
    close_values = mock_crm.update_object_record.call_args[0][2]
    assert close_values["status"] == "completed"


# ── select-attr read: the DOA-bug regression ─────────────────────────────


@pytest.mark.parametrize(
    "attrs, expected",
    [
        ({"reply_detection_status": "ok"}, "ok"),
        ({"reply_detection_status": "running"}, "running"),
        ({}, ""),                                    # missing key → ""
        ({"reply_detection_status": None}, ""),      # null → ""
    ],
)
def test_first_select_reads_scalar_title(attrs, expected):
    """``_first_select`` reads a normalized SELECT attr (already unwrapped to its
    option.title scalar by the provider) as a plain string, "" for
    absent/None."""
    assert _first_select(attrs, "reply_detection_status") == expected


def test_attach_rehydrates_reply_detection_status(mock_crm):
    """THE cross-process gate. A reattached row whose reply_detection_status is
    'ok' must rehydrate to "ok" — not None.

    send-dms runs in a SEPARATE process from daily; the in-memory
    _reply_detection_status does not survive the boundary, so the guard relies
    entirely on reading the persisted select back. If the read returned ""/None
    the fail-closed != "ok" guard would ALWAYS refuse and the DM phase could
    never run."""
    mock_crm.query_object_records.return_value = [
        _row(status="completed", messages_sent=4, reply_detection_status="ok")
    ]
    with attach_daily_run(
        mock_crm, run_id="dm-1", run_date=date(2026, 6, 9), machine_id="laptop-A"
    ) as run:
        assert run.get_reply_detection_status() == "ok"


def test_attach_set_reply_detection_status_writes_plain_title(mock_crm):
    """The WRITE side persists the select as the bare title string. Assert the
    reopen/status write carries the plain title under reply_detection_status —
    NOT a nested payload — so the cross-process read finds it back."""
    mock_crm.query_object_records.return_value = [
        _row(status="running", messages_sent=1)
    ]
    with attach_daily_run(
        mock_crm, run_id="dm-1", run_date=date(2026, 6, 9), machine_id="laptop-A"
    ) as run:
        run.set_reply_detection_status("ok")
    rds_writes = [
        c[0][2]
        for c in mock_crm.update_object_record.call_args_list
        if "reply_detection_status" in c[0][2]
    ]
    assert rds_writes
    assert rds_writes[0]["reply_detection_status"] == "ok"


# ── query_todays_row ─────────────────────────────────────────────────────


def test_query_todays_row_filters_on_bare_attributes(mock_crm):
    """query_todays_row queries on the BARE (run_date, machine_id) attrs — not
    the composite uniqueness_key — so it matches a row regardless of its
    re-keyed close-state."""
    mock_crm.query_object_records.return_value = [_row(status="completed")]
    row = query_todays_row(mock_crm, "2026-06-09", "laptop-A")
    assert row is not None
    obj, = mock_crm.query_object_records.call_args[0]
    assert obj == "daily_run"
    filters = mock_crm.query_object_records.call_args[1]["filters"]
    assert filters == {"run_date": "2026-06-09", "machine_id": "laptop-A"}
    assert "uniqueness_key" not in filters


def test_query_todays_row_returns_none_when_absent(mock_crm):
    mock_crm.query_object_records.return_value = []
    assert query_todays_row(mock_crm, "2026-06-09", "laptop-A") is None


def _row_with_started(record_id: str, status: str, started_at: str) -> Record:
    """A daily_run Record carrying a started_at timestamp for the multi-row
    selection path."""
    r = _row(record_id=record_id, status=status)
    r.attributes["started_at"] = started_at
    return r


# ── G(i): multi-row (run_date, machine_id) selection ──────────────────────


def test_query_todays_row_prefers_running_and_warns(mock_crm, capsys):
    """Same-day re-runs / takeovers can leave several rows sharing the bare
    (run_date, machine_id) pair. query_todays_row prefers the "running" row and
    emits a loud warning naming the chosen record_id."""
    abandoned = _row_with_started("rec_old", "abandoned", "2026-06-09T08:00:00Z")
    running = _row_with_started("rec_live", "running", "2026-06-09T12:00:00Z")
    mock_crm.query_object_records.return_value = [abandoned, running]
    chosen = query_todays_row(mock_crm, "2026-06-09", "laptop-A")
    assert chosen is not None and chosen.record_id == "rec_live"
    # A small page is fetched (not limit=1).
    assert mock_crm.query_object_records.call_args[1]["limit"] >= 2
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "rec_live" in err


def test_query_todays_row_two_terminal_picks_latest_started_at(mock_crm, capsys):
    """No running row → choose the most recent by started_at."""
    older = _row_with_started("rec_a", "failed", "2026-06-09T06:00:00Z")
    newer = _row_with_started("rec_b", "completed", "2026-06-09T18:00:00Z")
    # Provide out of order to prove the sort, not the input order, decides.
    mock_crm.query_object_records.return_value = [newer, older]
    chosen = query_todays_row(mock_crm, "2026-06-09", "laptop-A")
    assert chosen is not None and chosen.record_id == "rec_b"
    assert "rec_b" in capsys.readouterr().err


def test_query_todays_row_single_row_no_warning(mock_crm, capsys):
    """The common single-row case still returns that row with no warning."""
    mock_crm.query_object_records.return_value = [_row(status="running")]
    chosen = query_todays_row(mock_crm, "2026-06-09", "laptop-A")
    assert chosen is not None
    assert "WARNING" not in capsys.readouterr().err


# ── G(iv): read_only attach + restore-prior-status ────────────────────────


def test_read_only_attach_does_not_reopen_or_close(mock_crm):
    """A read_only (dry-run) attach to a TERMINAL row must NOT reopen it and
    must NOT close it on exit — the prior status is left untouched, no writes."""
    mock_crm.query_object_records.return_value = [
        _row(status="failed", messages_sent=4)
    ]
    with attach_daily_run(
        mock_crm, run_id="dm-1", run_date=date(2026, 6, 9), machine_id="laptop-A",
        read_only=True,
    ) as run:
        assert run.record_id == "rec_today"
        assert run.remaining("messages") == MAX_MESSAGES_PER_DAY - 4
    mock_crm.update_object_record.assert_not_called()
    mock_crm.create_object_record.assert_not_called()


def test_read_only_attach_still_fails_on_missing_row(mock_crm):
    """read_only relaxes the close, not the existence guard — a missing row
    still raises NoDailyRunRow."""
    mock_crm.query_object_records.return_value = []
    with pytest.raises(NoDailyRunRow), attach_daily_run(
        mock_crm, run_id="dm-1", run_date=date(2026, 6, 9), machine_id="laptop-A",
        read_only=True,
    ):
        pass


def test_wet_attach_no_sends_restores_prior_status(mock_crm):
    """Wet attach to a 'failed' terminal row that exits WITHOUT any send (the
    no-sender-id early return) must close with the ORIGINAL status, not upgrade
    failed→completed."""
    mock_crm.query_object_records.return_value = [
        _row(status="failed", messages_sent=4)
    ]
    with attach_daily_run(
        mock_crm, run_id="dm-1", run_date=date(2026, 6, 9), machine_id="laptop-A"
    ):
        pass  # no send recorded this session
    # Last write is the close — its status must restore "failed".
    close_values = mock_crm.update_object_record.call_args[0][2]
    assert close_values["status"] == "failed"


def test_wet_attach_with_send_closes_completed(mock_crm):
    """Wet attach that DOES record a send closes the row 'completed' as normal —
    the restore-prior-status path only fires when nothing was sent."""
    mock_crm.query_object_records.return_value = [
        _row(status="failed", messages_sent=4)
    ]
    with attach_daily_run(
        mock_crm, run_id="dm-1", run_date=date(2026, 6, 9), machine_id="laptop-A"
    ) as run:
        run.record_send("messages", 1)  # a send happened this session
    close_values = mock_crm.update_object_record.call_args[0][2]
    assert close_values["status"] == "completed"
