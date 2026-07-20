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
    MAX_CONNECTIONS_PER_DAY,
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
    started_at: str = "2026-06-09T08:00:00Z",
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
        "started_at": started_at,
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


# ── multi-row days: aborted-then-retried (the 2026-06-10 incident) ──────────
#
# `_close` re-keys an aborted row to the released 4-part uniqueness_key,
# freeing the 2-part running-lock, so a retried `daily` creates a SECOND row
# for the same (run_date, machine_id). The bare-attribute lookup must then
# pick deterministically and merge counters — never return an arbitrary row.


def _incident_day_rows() -> tuple[Record, Record]:
    """The 2026-06-10 shape: step 1 `daily` aborted at a pre-flight (zero
    counters, no reply status), then the retry completed with 22 invites and
    reply_detection_status=ok."""
    aborted = _row(
        record_id="rec_aborted",
        status="aborted",
        started_at="2026-06-09T08:00:00Z",
        reply_detection_status=None,
    )
    completed = _row(
        record_id="rec_completed",
        status="completed",
        started_at="2026-06-09T09:00:00Z",
        connections_sent=22,
        reply_detection_status="ok",
    )
    return aborted, completed


@pytest.mark.parametrize("crm_order", ["aborted_first", "completed_first"])
def test_query_todays_row_picks_completed_over_aborted(mock_crm, capsys, crm_order):
    """On a duplicate-row day the pick must be the completed row — in BOTH
    CRM return orders (the incident was exactly this being order-dependent:
    `send-dms` got the aborted zero-counter row back). A loud WARN must list
    the duplicates."""
    aborted, completed = _incident_day_rows()
    rows = [aborted, completed] if crm_order == "aborted_first" else [completed, aborted]
    mock_crm.query_object_records.return_value = rows
    chosen = query_todays_row(mock_crm, "2026-06-09", "laptop-A")
    assert chosen is not None and chosen.record_id == "rec_completed"
    # The full same-day page is fetched (not limit=1).
    assert mock_crm.query_object_records.call_args[1]["limit"] >= 2
    stderr = capsys.readouterr().err
    assert "WARN" in stderr
    assert "rec_aborted" in stderr and "rec_completed" in stderr


def test_query_todays_row_prefers_running_over_completed(mock_crm):
    """A running row still holds the 2-part lock — reopening any OTHER row
    would collide against it, so it must win the pick."""
    running = _row(record_id="rec_running", status="running", started_at="2026-06-09T07:00:00Z")
    completed = _row(record_id="rec_completed", status="completed", started_at="2026-06-09T09:00:00Z")
    mock_crm.query_object_records.return_value = [completed, running]
    chosen = query_todays_row(mock_crm, "2026-06-09", "laptop-A")
    assert chosen is not None and chosen.record_id == "rec_running"


def test_query_todays_row_same_status_tie_breaks_to_most_recent(mock_crm):
    """Two rows with the same status → the most recent started_at wins (it
    carries the freshest reply-detection state; counters are merged anyway)."""
    older = _row(record_id="rec_older", status="completed", started_at="2026-06-09T08:00:00Z")
    newer = _row(record_id="rec_newer", status="completed", started_at="2026-06-09T11:30:00Z")
    mock_crm.query_object_records.return_value = [older, newer]
    chosen = query_todays_row(mock_crm, "2026-06-09", "laptop-A")
    assert chosen is not None and chosen.record_id == "rec_newer"


def test_query_todays_row_single_row_no_warn(mock_crm, capsys):
    """A normal single-row day stays quiet — the duplicate WARN must not
    cry wolf on every run."""
    mock_crm.query_object_records.return_value = [_row(status="running")]
    chosen = query_todays_row(mock_crm, "2026-06-09", "laptop-A")
    assert chosen is not None
    assert "WARN" not in capsys.readouterr().err


def test_select_canonical_row_parses_mixed_timestamp_formats(mock_crm):
    """started_at must be PARSED for the most-recent tie-break, not
    string-compared: 'Z'-suffixed text sorts above '+00:00'-suffixed text
    lexicographically ('Z' > '.') even when it is chronologically older."""
    older_z = _row(
        record_id="rec_older", status="completed",
        started_at="2026-06-09T08:00:00Z",
    )
    newer_offset = _row(
        record_id="rec_newer", status="completed",
        started_at="2026-06-09T08:00:00.500000+00:00",
    )
    mock_crm.query_object_records.return_value = [older_z, newer_offset]
    chosen = query_todays_row(mock_crm, "2026-06-09", "laptop-A")
    assert chosen is not None and chosen.record_id == "rec_newer"


def test_attach_incident_day_binds_completed_row_and_reads_reply_status(mock_crm):
    """THE 2026-06-10 regression. Day has [aborted(0 counters, no reply
    status), completed(22 invites, reply ok)] → attach must bind the
    completed row: reply_detection_status rehydrates to "ok" (so `send-dms`
    proceeds instead of refusing) and the invite counter continues at 22/25
    (no cap reset). The reopen write must target the completed row."""
    aborted, completed = _incident_day_rows()
    mock_crm.query_object_records.return_value = [aborted, completed]
    with attach_daily_run(
        mock_crm, run_id="dm-1", run_date=date(2026, 6, 9), machine_id="laptop-A"
    ) as run:
        assert run.record_id == "rec_completed"
        assert run.get_reply_detection_status() == "ok"
        assert run.remaining("connections") == MAX_CONNECTIONS_PER_DAY - 22
    # The reopen must target the completed row (flip to running). Target it by
    # record_id — the aborted sibling now also gets an update_object_record call
    # (the [superseded] auto-mark), so it is no longer necessarily first.
    reopen_writes = [
        c for c in mock_crm.update_object_record.call_args_list
        if c[0][1] == "rec_completed" and c[0][2].get("status") == "running"
    ]
    assert reopen_writes
    # The aborted sibling is auto-marked [superseded], never flipped to running.
    aborted_writes = [
        c for c in mock_crm.update_object_record.call_args_list
        if c[0][1] == "rec_aborted"
    ]
    assert len(aborted_writes) == 1
    assert "[superseded]" in aborted_writes[0][0][2]["failure_details"]


def test_attach_auto_marks_stray_sibling_not_active_row(mock_crm):
    """On an aborted-then-retried day, attach binds the canonical (completed)
    row and auto-marks the failed SIBLING [superseded] — never the active row.
    The sibling's counters still feed the baseline (caps do not reset)."""
    active = _row(record_id="rec_active", status="completed", messages_sent=4)
    stray = _row(
        record_id="rec_stray", status="failed", messages_sent=3,
        reply_detection_status=None,
    )
    mock_crm.query_object_records.return_value = [active, stray]
    with attach_daily_run(
        mock_crm, run_id="dm-1", run_date=date(2026, 6, 9), machine_id="laptop-A",
    ) as run:
        assert run.record_id == "rec_active"
        # Baseline includes the sibling's 3 messages → cap does NOT reset.
        assert run.remaining("messages") == MAX_MESSAGES_PER_DAY - 4 - 3
    # The stray was marked; the active row was NOT marked [superseded].
    marked = [
        c for c in mock_crm.update_object_record.call_args_list
        if c[0][1] == "rec_stray"
    ]
    assert len(marked) == 1
    assert "[superseded]" in marked[0][0][2]["failure_details"]
    active_superseded = [
        c for c in mock_crm.update_object_record.call_args_list
        if c[0][1] == "rec_active"
        and "[superseded]" in str(c[0][2].get("failure_details", ""))
    ]
    assert active_superseded == []


def test_attach_stray_archive_idempotent_on_rerun(mock_crm):
    """A sibling already carrying [superseded] is not re-marked — no second
    write on a repeat attach."""
    active = _row(record_id="rec_active", status="completed", messages_sent=4)
    stray = _row(
        record_id="rec_stray", status="failed", messages_sent=3,
        reply_detection_status=None,
    )
    stray.attributes["failure_details"] = "boom\n[superseded] earlier"
    mock_crm.query_object_records.return_value = [active, stray]
    with attach_daily_run(
        mock_crm, run_id="dm-1", run_date=date(2026, 6, 9), machine_id="laptop-A",
    ):
        pass
    marked = [
        c for c in mock_crm.update_object_record.call_args_list
        if c[0][1] == "rec_stray"
    ]
    assert marked == []


def test_attach_merges_counters_across_duplicate_rows(mock_crm):
    """Counters are SUMMED across all same-day rows: a failed-mid-send row
    (10 DMs) plus its retry (12 DMs) → remaining == 30 - 22. Binding either
    row with only its own counters would re-open headroom that was already
    spent — the latent cap breach."""
    failed = _row(
        record_id="rec_failed", status="failed",
        started_at="2026-06-09T08:00:00Z", messages_sent=10,
    )
    completed = _row(
        record_id="rec_completed", status="completed",
        started_at="2026-06-09T09:00:00Z", messages_sent=12,
    )
    mock_crm.query_object_records.return_value = [failed, completed]
    with attach_daily_run(
        mock_crm, run_id="dm-1", run_date=date(2026, 6, 9), machine_id="laptop-A"
    ) as run:
        assert run.remaining("messages") == MAX_MESSAGES_PER_DAY - 22


def test_attach_malformed_sibling_counter_fails_closed_before_reopen(mock_crm):
    """The strict-counter rule extends to EVERY same-day row: a sibling row
    with a missing counter raises MalformedDailyRunRow rather than being
    silently dropped from the merge (dropping it would under-count and
    re-open spent headroom).

    CRITICALLY, the raise must happen BEFORE the reopen write: raising
    after it would leave the bound row re-keyed to status=running holding
    the 2-part lock with no close path — wedging every subsequent `daily`
    and `send-dms` until a manual re-key."""
    bad_sibling = _row(record_id="rec_bad", status="aborted", started_at="2026-06-09T08:00:00Z")
    del bad_sibling.attributes["messages_sent"]
    good = _row(record_id="rec_good", status="completed", started_at="2026-06-09T09:00:00Z")
    mock_crm.query_object_records.return_value = [bad_sibling, good]
    with pytest.raises(MalformedDailyRunRow), attach_daily_run(
        mock_crm, run_id="dm-1", run_date=date(2026, 6, 9), machine_id="laptop-A"
    ):
        pytest.fail("body must not execute when a sibling counter is malformed")
    mock_crm.update_object_record.assert_not_called()


def test_attach_patches_own_counters_only_not_baseline(mock_crm):
    """Row independence: sends recorded after a duplicate-day attach must
    persist the BOUND row's own count, never the sibling baseline. If the
    baseline leaked into the write, every later merge would double-count
    it (compounding per retry) and the weekly outreach-volume report —
    which sums rows per day — would inflate."""
    failed = _row(
        record_id="rec_failed", status="failed",
        started_at="2026-06-09T08:00:00Z", messages_sent=10,
    )
    completed = _row(
        record_id="rec_completed", status="completed",
        started_at="2026-06-09T09:00:00Z", messages_sent=12,
    )
    mock_crm.query_object_records.return_value = [failed, completed]
    with attach_daily_run(
        mock_crm, run_id="dm-1", run_date=date(2026, 6, 9), machine_id="laptop-A"
    ) as run:
        run.record_send("messages", 1)
        # Cap math sees baseline 10 + own 13.
        assert run.remaining("messages") == MAX_MESSAGES_PER_DAY - 23
    # The counter write carries own 13, NOT baseline-inflated 23.
    counter_writes = [
        c[0][2]
        for c in mock_crm.update_object_record.call_args_list
        if "messages_sent" in c[0][2]
    ]
    assert counter_writes
    assert counter_writes[0]["messages_sent"] == 13


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
