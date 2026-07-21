"""Tests for workflows/daily_run.py (F-PR-8).

Covers:
- machine_id derivation precedence (explicit > env > gethostname)
- open_daily_run happy path + counters round-trip
- ConcurrentRunInAttio on uniqueness collision (cross-machine guard)
- Two-phase reserve / confirm / release lease semantics
- CapacityExhausted on over-reservation
- Recheck-cache observation persistence to the CRM
- Context-manager status transitions (running → completed / failed / aborted)

The module talks to the system of record exclusively through the
vendor-neutral ``CRMProvider`` contract, so these tests inject a
``MagicMock`` provider whose ``create_object_record`` /
``update_object_record`` / ``query_object_records`` stand in for the CRM.
Failure injection that used to raise a raw httpx error on the inner client
now raises the neutral ``UniquenessConflictError`` (collision) or an
``httpx`` transport error on the provider write (transient-failure paths),
preserving the same semantics through the contract surface.
"""

from __future__ import annotations

import socket
from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock, patch

import httpx
import pytest

from clients.crm.base import Record
from clients.crm.exceptions import UniquenessConflictError
from workflows.daily_run import (
    MAX_CONNECTIONS_PER_DAY,
    MAX_MESSAGES_PER_DAY,
    MAX_VISITS_PER_DAY,
    STALE_RUN_TAKEOVER_HOURS,
    CapacityExhausted,
    ConcurrentRunInAttio,
    DailyRun,
    MalformedDailyRunRow,
    derive_machine_id,
    open_daily_run,
)


def _record(record_id: str = "rec_test", **attributes) -> Record:
    """A normalized daily_run ``Record`` as the provider returns it. Values are
    plain scalars (the provider flattens the vendor shape before we ever see
    them)."""
    return Record(
        record_id=record_id,
        object="daily_run",
        attributes=dict(attributes),
        raw={"id": {"record_id": record_id}, "values": dict(attributes)},
    )


def _uniqueness_conflict() -> UniquenessConflictError:
    return UniquenessConflictError("daily_run", attribute="uniqueness_key")


@pytest.fixture
def mock_crm():
    """A CRMProvider mock whose writes succeed by default. Each test customizes
    side_effect / return_value for the call sequence it cares about.

    ``create_object_record`` returns a fresh record id; ``update_object_record``
    (counters / status / reopen / close / observation) is a no-op success;
    ``query_object_records`` returns no rows by default."""
    crm = MagicMock()
    crm.create_object_record.return_value = _record("rec_test")
    crm.update_object_record.return_value = _record("rec_test")
    crm.query_object_records.return_value = []
    return crm


# ── machine_id derivation ────────────────────────────────────────────────────


def test_machine_id_explicit_override_wins(monkeypatch):
    monkeypatch.setenv("OUTBOUND_MACHINE_ID", "env-value")
    assert derive_machine_id("explicit") == "explicit"


def test_machine_id_env_var_wins_over_hostname(monkeypatch):
    monkeypatch.setenv("OUTBOUND_MACHINE_ID", "env-value")
    assert derive_machine_id() == "env-value"


def test_machine_id_falls_back_to_hostname(monkeypatch):
    monkeypatch.delenv("OUTBOUND_MACHINE_ID", raising=False)
    assert derive_machine_id() == socket.gethostname()


# ── open_daily_run happy path ────────────────────────────────────────────────


def test_open_daily_run_creates_row_and_yields_handle(mock_crm):
    mock_crm.create_object_record.return_value = _record("rec_today")
    with open_daily_run(mock_crm, run_id="run-abc", machine_id="laptop-A") as run:
        assert run.record_id == "rec_today"
        assert run.run_id == "run-abc"
        assert run.machine_id == "laptop-A"
    # Final write on close should have status=completed.
    obj, rid, values = mock_crm.update_object_record.call_args[0]
    assert obj == "daily_run"
    assert values["status"] == "completed"
    assert "completed_at" in values


def test_open_daily_run_marks_failed_when_body_raises(mock_crm):
    mock_crm.create_object_record.return_value = _record("rec_today")
    with (
        pytest.raises(RuntimeError, match="boom"),
        open_daily_run(mock_crm, run_id="run-abc", machine_id="laptop-A"),
    ):
        raise RuntimeError("boom")
    values = mock_crm.update_object_record.call_args[0][2]
    assert values["status"] == "failed"
    assert "boom" in values["failure_details"]


def test_open_daily_run_marks_aborted_on_keyboard_interrupt(mock_crm):
    mock_crm.create_object_record.return_value = _record("rec_today")
    with pytest.raises(KeyboardInterrupt), open_daily_run(mock_crm, run_id="run-abc", machine_id="laptop-A"):
        raise KeyboardInterrupt
    values = mock_crm.update_object_record.call_args[0][2]
    assert values["status"] == "aborted"


# ── uniqueness_key auto-release on close (#20) ───────────────────────────────


def test_close_releases_uniqueness_key_on_completed(mock_crm):
    """On a completed close, the row's uniqueness_key must flip from the
    2-part `{date}|{machine}` running-lock to a released
    `{status}|{date}|{machine}|{record_id}` — in the SAME write as the status
    change — so the next same-date/machine run no longer collides on the unique
    constraint (#20: the manual-re-key toil this eliminates). The final segment
    is the row's record_id (globally unique), not run_id, so a recycled PID
    can't cause a same-date released-key collision."""
    mock_crm.create_object_record.return_value = _record("rec_rel")
    with open_daily_run(
        mock_crm, run_id="run-rel", run_date=date(2026, 5, 21), machine_id="laptop-A"
    ):
        pass
    open_values = mock_crm.create_object_record.call_args[0][1]
    close_values = mock_crm.update_object_record.call_args[0][2]
    assert open_values["uniqueness_key"] == "2026-05-21|laptop-A"
    assert close_values["status"] == "completed"
    assert close_values["uniqueness_key"] == "completed|2026-05-21|laptop-A|rec_rel"
    assert close_values["uniqueness_key"] != open_values["uniqueness_key"]


def test_close_releases_uniqueness_key_on_failed(mock_crm):
    """The released key encodes the terminal status, so a failed run also frees
    the running-lock (this is the exact case that wedged 2026-05-27 and forced
    two manual re-keys)."""
    mock_crm.create_object_record.return_value = _record("rec_rel")
    with (
        pytest.raises(RuntimeError, match="kaboom"),
        open_daily_run(
            mock_crm, run_id="run-fail", run_date=date(2026, 5, 21), machine_id="laptop-A"
        ),
    ):
        raise RuntimeError("kaboom")
    close_values = mock_crm.update_object_record.call_args[0][2]
    assert close_values["status"] == "failed"
    assert close_values["uniqueness_key"] == "failed|2026-05-21|laptop-A|rec_rel"


def test_close_releases_uniqueness_key_on_aborted(mock_crm):
    """A KeyboardInterrupt (BaseException) closes status=aborted and must also
    release the lock with the matching `aborted|…` key — guards against a future
    refactor wiring the abort path to the wrong released key (this is exactly the
    path the 2026-05-28 wet-run abort exercised)."""
    mock_crm.create_object_record.return_value = _record("rec_rel")
    with (
        pytest.raises(KeyboardInterrupt),
        open_daily_run(
            mock_crm, run_id="run-abort", run_date=date(2026, 5, 21), machine_id="laptop-A"
        ),
    ):
        raise KeyboardInterrupt
    close_values = mock_crm.update_object_record.call_args[0][2]
    assert close_values["status"] == "aborted"
    assert close_values["uniqueness_key"] == "aborted|2026-05-21|laptop-A|rec_rel"


# ── Cross-machine collision via uniqueness_key ───────────────────────────────


def test_open_daily_run_raises_concurrent_when_unique_collision(mock_crm):
    """A provider UniquenessConflictError on create → ConcurrentRunInAttio,
    carrying the existing-row forensics from the follow-up query."""
    mock_crm.create_object_record.side_effect = _uniqueness_conflict()
    # The pre-open scan filters on machine_id=laptop-A, so laptop-B's row
    # does not appear in it — only the forensics query (filtered on the
    # uniqueness_key both machines contend for) surfaces it.
    mock_crm.query_object_records.side_effect = [
        [],  # pre-open same-day scan
        [
            _record(
                "rec_other",
                hostname="laptop-B",
                started_at="2026-05-21T10:00:00Z",
                run_id="concurrent-other",
            )
        ],  # collision forensics query
    ]

    with (
        pytest.raises(ConcurrentRunInAttio) as excinfo,
        open_daily_run(mock_crm, run_id="run-collide", machine_id="laptop-A"),
    ):
        pass
    assert excinfo.value.machine_id == "laptop-A"
    assert excinfo.value.existing is not None
    assert excinfo.value.existing["hostname"] == "laptop-B"
    assert excinfo.value.existing["run_id"] == "concurrent-other"


def test_open_daily_run_propagates_non_uniqueness_errors(mock_crm):
    """A non-uniqueness write error (the adapter never wrapped it) propagates."""
    mock_crm.create_object_record.side_effect = httpx.HTTPStatusError(
        "missing required attribute",
        request=httpx.Request("POST", "https://api.attio.com/v2/x"),
        response=httpx.Response(400),
    )
    with pytest.raises(httpx.HTTPStatusError), open_daily_run(mock_crm, run_id="run-bad", machine_id="laptop-A"):
        pass


def test_open_daily_run_query_failure_in_collision_path_yields_none_existing(mock_crm):
    """If the lookup-existing-row query itself fails, the ConcurrentRunInAttio
    must still be raised — never crash on the error path."""
    mock_crm.create_object_record.side_effect = _uniqueness_conflict()
    mock_crm.query_object_records.side_effect = [
        [],  # pre-open same-day scan succeeds (empty day)
        httpx.ConnectError("network down"),  # forensics lookup fails
    ]
    with (
        pytest.raises(ConcurrentRunInAttio) as excinfo,
        open_daily_run(mock_crm, run_id="run-collide", machine_id="laptop-A"),
    ):
        pass
    assert excinfo.value.existing is None


# ── Two-phase reserve / confirm / release ────────────────────────────────────


def _run(crm=None, **counters) -> DailyRun:
    base = {"connections": 0, "messages": 0, "visits": 0}
    base.update(counters)
    return DailyRun(
        crm=crm or MagicMock(),
        record_id="rec",
        run_date="2026-05-21",
        machine_id="laptop",
        run_id="r",
        initial_counters=base,
    )


def test_reserve_decreases_remaining_capacity():
    run = _run()
    assert run.remaining("messages") == MAX_MESSAGES_PER_DAY
    token = run.reserve_send("messages", 5)
    assert run.remaining("messages") == MAX_MESSAGES_PER_DAY - 5
    # Lease alone doesn't write to the CRM.
    assert run._crm.update_object_record.call_count == 0
    # Confirm patches the counter via the provider write layer.
    run.confirm_lease(token)
    assert run.remaining("messages") == MAX_MESSAGES_PER_DAY - 5
    assert run._crm.update_object_record.call_count == 1


def test_release_lease_rolls_back_capacity():
    run = _run()
    token = run.reserve_send("messages", 7)
    run.release_lease(token)
    # Release restores capacity AND does NOT write the CRM.
    assert run.remaining("messages") == MAX_MESSAGES_PER_DAY
    assert run._crm.update_object_record.call_count == 0


def test_confirm_lease_idempotent_on_unknown_token():
    run = _run()
    run.confirm_lease("not-a-real-token")  # no raise
    assert run._crm.update_object_record.call_count == 0


def test_reserve_raises_when_capacity_exhausted():
    run = _run(connections=MAX_CONNECTIONS_PER_DAY - 2)
    with pytest.raises(CapacityExhausted) as excinfo:
        run.reserve_send("connections", 5)
    assert excinfo.value.kind == "connections"
    assert excinfo.value.available == 2


def test_two_concurrent_leases_reserve_combined():
    run = _run()
    token_a = run.reserve_send("messages", 10)
    token_b = run.reserve_send("messages", 15)
    assert run.remaining("messages") == MAX_MESSAGES_PER_DAY - 25
    run.confirm_lease(token_a)
    run.release_lease(token_b)
    assert run.remaining("messages") == MAX_MESSAGES_PER_DAY - 10


# ── record_send direct path ──────────────────────────────────────────────────


def test_record_send_patches_counter():
    crm = MagicMock()
    run = _run(crm=crm)
    run.record_send("connections", 3)
    obj, rid, values = crm.update_object_record.call_args[0]
    assert obj == "daily_run"
    assert values["connections_sent"] == 3
    assert values["messages_sent"] == 0


# ── Recheck-cache observation ────────────────────────────────────────────────


def test_record_observation_patches_linkedin_outreach_record():
    crm = MagicMock()
    run = _run(crm=crm)
    run.record_observation("rec_prospect_42", degree="2nd")
    obj, rid, values = crm.update_object_record.call_args[0]
    assert obj == "linkedin_outreach"
    assert rid == "rec_prospect_42"
    assert values["last_observed_degree"] == "second"  # normalized from "2nd"
    assert "last_observed_at" in values


def test_record_observation_omits_degree_when_unknown():
    """A None / unrecognized degree writes only last_observed_at."""
    crm = MagicMock()
    run = _run(crm=crm)
    run.record_observation("rec_prospect_42", degree=None)
    values = crm.update_object_record.call_args[0][2]
    assert "last_observed_at" in values
    assert "last_observed_degree" not in values


# ── F-PR-8 smoke test (per plan §4 line 507) ─────────────────────────────────


def test_f_pr_8_machine_id_smoke(mock_crm, monkeypatch):
    """Plan §4 line 507 explicitly requires a smoke test simulating two
    `(run_date, gethostname())` calls and asserting the second raises
    `ConcurrentRunInAttio`. We deliberately let `derive_machine_id()` resolve
    via `socket.gethostname()` (not an explicit override) so the test exercises
    the full derivation → uniqueness_key → collision chain. `gethostname()` is
    monkeypatched to a stable value so the test doesn't depend on the CI
    machine's name."""
    monkeypatch.setattr("workflows.daily_run.socket.gethostname", lambda: "stable-laptop")
    monkeypatch.delenv("OUTBOUND_MACHINE_ID", raising=False)

    mock_crm.create_object_record.side_effect = [
        _record("rec_first"),  # first invocation: create succeeds
        _uniqueness_conflict(),  # second invocation: collision
    ]
    # The second invocation's pre-open scan returns the FIRST run's closed
    # row (it exists for today), so it needs valid counters for the seed.
    first_row_scan = _record(
        "rec_first",
        hostname="stable-laptop",
        started_at="2026-05-21T08:00:00Z",
        run_id="first-run",
        status="completed",
        connections_sent=0,
        messages_sent=0,
        visits_sent=0,
    )
    # Forensics row carries no machine_id (as before this fix), so the
    # stale same-machine takeover path stays out of this smoke test.
    forensics_row = _record(
        "rec_first",
        hostname="stable-laptop",
        started_at="2026-05-21T08:00:00Z",
        run_id="first-run",
    )
    mock_crm.query_object_records.side_effect = [
        [],  # first invocation: pre-open scan, nothing yet
        [first_row_scan],  # second invocation: scan sees the first run's row
        [forensics_row],  # second invocation: forensics query
    ]

    # First invocation — machine_id derived from gethostname().
    with open_daily_run(mock_crm, run_id="r1") as run:
        assert run.machine_id == "stable-laptop"

    # Second invocation same date → collision via the actual derivation chain.
    with (
        pytest.raises(ConcurrentRunInAttio) as excinfo,
        open_daily_run(mock_crm, run_id="r2"),
    ):
        pass
    assert excinfo.value.machine_id == "stable-laptop"
    assert excinfo.value.existing is not None
    assert excinfo.value.existing["hostname"] == "stable-laptop"
    assert excinfo.value.existing["run_id"] == "first-run"


def test_open_daily_run_marks_aborted_on_system_exit(mock_crm):
    """SystemExit is BaseException (not Exception) → status="aborted"."""
    mock_crm.create_object_record.return_value = _record("rec_sysexit")
    with pytest.raises(SystemExit), open_daily_run(mock_crm, run_id="run-sysexit", machine_id="laptop"):
        raise SystemExit(1)
    values = mock_crm.update_object_record.call_args[0][2]
    assert values["status"] == "aborted"


def test_open_daily_run_marks_failed_captures_traceback_not_just_repr(mock_crm):
    """`_format_exc` should capture a multi-line traceback so a forensics scan
    can pinpoint where the daily run crashed — not just the exception's repr."""
    mock_crm.create_object_record.return_value = _record("rec_tb")
    with (
        pytest.raises(RuntimeError, match="boom-tb"),
        open_daily_run(mock_crm, run_id="run-tb", machine_id="laptop"),
    ):
        raise RuntimeError("boom-tb")
    failure_details = mock_crm.update_object_record.call_args[0][2]["failure_details"]
    # Multi-line traceback — must include both the exception and a frame.
    assert "boom-tb" in failure_details
    assert "Traceback" in failure_details
    assert "test_open_daily_run_marks_failed_captures_traceback" in failure_details


def test_confirm_lease_rolls_back_state_on_patch_failure():
    """If the CRM write fails with a transport error, confirm_lease must
    re-instate the lease + decrement the in-memory counter so process state
    stays consistent with the CRM. Without this, the in-memory counter would say
    N+5 while the CRM still says N — and the docstring's "under-count over
    double-count" trade-off inverts (process refuses to send when the CRM has
    capacity)."""
    crm = MagicMock()
    crm.update_object_record.side_effect = httpx.ConnectError("network down")
    run = _run(crm=crm)
    token = run.reserve_send("messages", 5)
    assert run.remaining("messages") == MAX_MESSAGES_PER_DAY - 5
    with pytest.raises(httpx.ConnectError):
        run.confirm_lease(token)
    # State after failure: lease re-instated, counter rolled back.
    assert run._counters["messages"] == 0
    assert token in run._reservations
    assert run.remaining("messages") == MAX_MESSAGES_PER_DAY - 5  # still reserved


def test_derive_machine_id_strips_whitespace(monkeypatch):
    monkeypatch.setenv("OUTBOUND_MACHINE_ID", "   ")
    # Whitespace-only env var must fall through, not produce machine_id="   "
    monkeypatch.setattr("workflows.daily_run.socket.gethostname", lambda: "real-host")
    assert derive_machine_id() == "real-host"
    # Whitespace override also stripped.
    monkeypatch.setenv("OUTBOUND_MACHINE_ID", "  real-laptop  ")
    assert derive_machine_id() == "real-laptop"


def test_derive_machine_id_warns_on_generic_hostname(monkeypatch):
    monkeypatch.delenv("OUTBOUND_MACHINE_ID", raising=False)
    monkeypatch.setattr("workflows.daily_run.socket.gethostname", lambda: "MacBook-Pro.local")
    import warnings as warnings_module

    with warnings_module.catch_warnings(record=True) as captured:
        warnings_module.simplefilter("always")
        derive_machine_id()
    assert any("generic" in str(w.message).lower() for w in captured)


def test_derive_machine_id_quiet_on_non_generic_hostname(monkeypatch):
    monkeypatch.delenv("OUTBOUND_MACHINE_ID", raising=False)
    monkeypatch.setattr("workflows.daily_run.socket.gethostname", lambda: "mats-work-laptop")
    import warnings as warnings_module

    with warnings_module.catch_warnings(record=True) as captured:
        warnings_module.simplefilter("always")
        derive_machine_id()
    assert not [w for w in captured if "generic" in str(w.message).lower()]


# ── status writes route through the provider write layer ────────────────────


def test_set_reply_detection_status_routes_through_provider():
    """set_reply_detection_status must write via the provider's
    update_object_record (the adapter's retrying layer)."""
    crm = MagicMock()
    run = _run(crm=crm)
    run.set_reply_detection_status("ok")
    obj, rid, values = crm.update_object_record.call_args[0]
    assert obj == "daily_run"
    assert values["reply_detection_status"] == "ok"


def test_set_reply_detection_status_persistent_failure_raises():
    """Persistent failure propagates out of set_reply_detection_status."""
    crm = MagicMock()
    crm.update_object_record.side_effect = httpx.ConnectError("network down")
    run = _run(crm=crm)
    with pytest.raises(httpx.ConnectError):
        run.set_reply_detection_status("failed")


def test_patch_counters_routes_through_provider():
    """_patch_counters writes via the provider's update_object_record."""
    crm = MagicMock()
    run = _run(crm=crm)
    run.record_send("messages", 3)
    obj, rid, values = crm.update_object_record.call_args[0]
    assert obj == "daily_run"
    assert values["messages_sent"] == 3


def test_record_observation_routes_through_provider():
    """record_observation writes via the provider's update_object_record."""
    crm = MagicMock()
    run = _run(crm=crm)
    run.record_observation("linkedin_rec_1", degree="1st")
    obj, rid, _values = crm.update_object_record.call_args[0]
    assert obj == "linkedin_outreach"
    assert rid == "linkedin_rec_1"


# ── close retry + stale-run takeover ────────────────────────────────────────


def test_close_with_warn_retries_3x_before_giving_up(capsys):
    """_close_with_warn retries up to 3× before printing WARN. (L4-4.)"""
    from workflows.daily_run import _close_with_warn

    crm = MagicMock()
    crm.update_object_record.side_effect = httpx.ConnectError("network down")
    run = _run(crm=crm)
    with patch("workflows.daily_run.time.sleep"):
        _close_with_warn(run, "completed", None)

    # Should have attempted _CLOSE_RETRY_ATTEMPTS (3) times.
    assert crm.update_object_record.call_count == 3
    stderr = capsys.readouterr().err
    assert "WARN" in stderr
    assert "3" in stderr  # "after 3 attempts"


def test_close_with_warn_succeeds_on_first_retry():
    """If the first attempt fails but the second succeeds, no WARN is emitted. (L4-4.)"""
    import io

    from workflows.daily_run import _close_with_warn

    crm = MagicMock()
    crm.update_object_record.side_effect = [
        httpx.ConnectError("transient"),
        _record("rec_retry_close"),
    ]
    run = _run(crm=crm)
    captured = io.StringIO()
    with patch("workflows.daily_run.time.sleep"), patch("workflows.daily_run.sys.stderr", captured):
        _close_with_warn(run, "completed", None)
    assert "WARN" not in captured.getvalue()


def test_open_daily_run_stale_same_machine_takeover():
    """A status=running row from the SAME machine older than
    STALE_RUN_TAKEOVER_HOURS is marked aborted and the new run opens fresh.
    (L4-4.)

    PR-210: ``aborted`` (not ``abandoned``) because the daily_run.status select
    has no ``abandoned`` option — writing it rejects the update and re-wedges the
    lock."""
    stale_started = (datetime.now(UTC) - timedelta(hours=STALE_RUN_TAKEOVER_HOURS + 1)).isoformat()

    crm = MagicMock()
    crm.create_object_record.side_effect = [
        _uniqueness_conflict(),  # initial create collides
        _record("new_rec_456"),  # retry after takeover succeeds
    ]
    # The pre-open scan sees the stale running row — with real counters, so
    # the new run's seed continues today's totals instead of restarting at 0.
    crm.query_object_records.return_value = [
        _record(
            "stale_rec_123",
            hostname="laptop-A",
            started_at=stale_started,
            run_id="old-run",
            machine_id="laptop-A",
            status="running",
            connections_sent=7,
            messages_sent=0,
            visits_sent=0,
        )
    ]
    crm.update_object_record.return_value = _record("any")

    with patch("workflows.daily_run.time.sleep"), open_daily_run(crm, run_id="new-run", machine_id="laptop-A") as run:
        assert run.record_id == "new_rec_456"
        # Seeded from the stale row's counters — caps don't reset.
        assert run.remaining("connections") == MAX_CONNECTIONS_PER_DAY - 7

    # Takeover write is the first update_object_record call.
    obj, rid, values = crm.update_object_record.call_args_list[0][0]
    assert obj == "daily_run"
    assert rid == "stale_rec_123"
    assert values["status"] == "aborted"
    # The released uniqueness_key must carry the SAME status it writes, or the
    # row would keep the running-lock. Guards the abandoned→aborted fix (PR-210).
    assert values["uniqueness_key"].startswith("aborted|")
    assert values["uniqueness_key"].endswith("|stale_rec_123")


def test_open_daily_run_cross_machine_stale_raises_concurrent():
    """A stale running row from a DIFFERENT machine must NOT be taken over —
    raises ConcurrentRunInAttio with age in the message. (L4-4.)"""
    stale_started = (datetime.now(UTC) - timedelta(hours=STALE_RUN_TAKEOVER_HOURS + 1)).isoformat()

    crm = MagicMock()
    crm.create_object_record.side_effect = _uniqueness_conflict()
    # The pre-open scan filters on machine_id=laptop-A, so laptop-B's row
    # does not appear in it — only the forensics query surfaces it.
    crm.query_object_records.side_effect = [
        [],  # pre-open same-day scan
        [
            _record(
                "other_rec",
                hostname="laptop-B",
                started_at=stale_started,
                run_id="other-run",
                machine_id="laptop-B",
            )
        ],  # collision forensics query
    ]

    with pytest.raises(ConcurrentRunInAttio) as excinfo, open_daily_run(crm, run_id="my-run", machine_id="laptop-A"):
        pass

    # Age info present in message.
    assert "age=" in str(excinfo.value)
    # No takeover write was issued.
    crm.update_object_record.assert_not_called()


def test_open_daily_run_retry_after_takeover_collides_raises_concurrent():
    """G(iii): a racing process can grab the freed key between the stale-row
    takeover and the retry create. The retry's raw UniquenessConflictError must
    be surfaced as ConcurrentRunInAttio (chained from the retry error), NOT
    leaked past cli's ConcurrentRunInAttio handler."""
    stale_started = (
        datetime.now(UTC) - timedelta(hours=STALE_RUN_TAKEOVER_HOURS + 1)
    ).isoformat()

    crm = MagicMock()
    crm.create_object_record.side_effect = [
        _uniqueness_conflict(),  # initial create collides
        _uniqueness_conflict(),  # retry after takeover ALSO collides (race)
    ]
    # Same-machine stale row: the pre-open scan sees it too, so it carries
    # valid counters for the baseline seed (strict parse fails closed).
    crm.query_object_records.return_value = [
        _record(
            "stale_rec_123",
            hostname="laptop-A",
            started_at=stale_started,
            run_id="old-run",
            machine_id="laptop-A",
            status="running",
            connections_sent=0,
            messages_sent=0,
            visits_sent=0,
        )
    ]
    crm.update_object_record.return_value = _record("any")

    with (
        patch("workflows.daily_run.time.sleep"),
        pytest.raises(ConcurrentRunInAttio) as excinfo,
        open_daily_run(crm, run_id="new-run", machine_id="laptop-A"),
    ):
        pass
    # Chained from the retry's UniquenessConflictError (forensics preserved).
    assert isinstance(excinfo.value.__cause__, UniquenessConflictError)


def test_open_daily_run_collision_chains_vendor_error():
    """G(iii): the cross-machine collision raise now chains `from exc` so the
    vendor error body is preserved for forensics (was `from None`)."""
    crm = MagicMock()
    crm.create_object_record.side_effect = _uniqueness_conflict()
    crm.query_object_records.side_effect = [
        [],  # pre-open same-day scan
        [
            _record(
                "rec_other", hostname="laptop-B",
                started_at="2026-05-21T10:00:00Z", run_id="other",
            )
        ],  # collision forensics query
    ]
    with (
        pytest.raises(ConcurrentRunInAttio) as excinfo,
        open_daily_run(crm, run_id="run-collide", machine_id="laptop-A"),
    ):
        pass
    assert isinstance(excinfo.value.__cause__, UniquenessConflictError)


# ── Aborted-then-retried day: counter seeding (2026-06-10 incident) ─────────


def _prior_day_row(
    record_id: str = "rec_prior",
    status: str = "aborted",
    connections_sent: int = 0,
    messages_sent: int = 0,
    visits_sent: int = 0,
) -> Record:
    return _record(
        record_id,
        status=status,
        started_at="2026-06-10T08:00:00Z",
        machine_id="laptop-A",
        connections_sent=connections_sent,
        messages_sent=messages_sent,
        visits_sent=visits_sent,
    )


def test_open_daily_run_seeds_counters_from_prior_same_day_rows(mock_crm, capsys):
    """A retried `daily` after an abort: the released-key close freed the
    2-part lock, so the retry creates a SECOND row with 0/0/0 counters in
    the CRM. The pre-open scan must seed the new run's in-memory counters
    from the prior rows' sums so the caps continue at N/25, N/30 — not
    restart at 0 (the daily-path half of the 2026-06-10 multi-row
    incident). A WARN announces the seeding."""
    mock_crm.query_object_records.return_value = [
        _prior_day_row(connections_sent=22, messages_sent=5),
    ]
    mock_crm.create_object_record.return_value = _record("rec_retry")
    with open_daily_run(mock_crm, run_id="retry-run", machine_id="laptop-A") as run:
        assert run.remaining("connections") == MAX_CONNECTIONS_PER_DAY - 22
        assert run.remaining("messages") == MAX_MESSAGES_PER_DAY - 5
        assert run.remaining("visits") == MAX_VISITS_PER_DAY
    stderr = capsys.readouterr().err
    assert "WARN" in stderr
    assert "cannot reset" in stderr


def test_open_daily_run_seeds_sum_across_multiple_prior_rows(mock_crm):
    """Two prior rows (failed mid-send, then aborted retry) → the seed is
    their SUM, the fail-safe direction for the caps."""
    mock_crm.query_object_records.return_value = [
        _prior_day_row(record_id="rec_a", status="failed", connections_sent=10),
        _prior_day_row(record_id="rec_b", status="aborted", connections_sent=4),
    ]
    mock_crm.create_object_record.return_value = _record("rec_retry")
    with open_daily_run(mock_crm, run_id="retry-run", machine_id="laptop-A") as run:
        assert run.remaining("connections") == MAX_CONNECTIONS_PER_DAY - 14


def test_open_daily_run_seeded_baseline_not_persisted_to_new_row(mock_crm):
    """Row independence: the baseline inherited from prior same-day rows
    counts against the caps but must NEVER be written into the new row's
    counters. If it were, the prior rows' sends would be double-counted by
    every later cross-row sum (attach merges and the weekly outreach-volume
    report), compounding once per retry."""
    mock_crm.query_object_records.return_value = [
        _prior_day_row(connections_sent=22),
    ]
    mock_crm.create_object_record.return_value = _record("rec_retry")
    with open_daily_run(mock_crm, run_id="retry-run", machine_id="laptop-A") as run:
        run.record_send("connections", 3)
        # Cap math: 22 baseline + 3 own.
        assert run.remaining("connections") == MAX_CONNECTIONS_PER_DAY - 25
    # The counter write carries own 3, NOT the seeded 25.
    counter_values = mock_crm.update_object_record.call_args_list[0][0][2]
    assert counter_values["connections_sent"] == 3
    # The create also wrote 0s (the row's own ledger starts empty).
    create_values = mock_crm.create_object_record.call_args[0][1]
    assert create_values["connections_sent"] == 0


def test_query_todays_rows_fails_closed_at_fetch_limit(mock_crm):
    """A response at the fetch limit may be truncated; a truncated merge
    would silently UNDERcount the caps — the one direction the strict rule
    forbids. Fail closed instead of proceeding."""
    from workflows.daily_run import _MAX_SAME_DAY_ROWS, query_todays_rows

    mock_crm.query_object_records.return_value = [
        _prior_day_row(record_id=f"rec_{i}") for i in range(_MAX_SAME_DAY_ROWS)
    ]
    with pytest.raises(RuntimeError, match="undercount"):
        query_todays_rows(mock_crm, "2026-06-10", "laptop-A")


def test_open_daily_run_malformed_prior_counter_fails_closed(mock_crm):
    """A prior same-day row with a missing counter raises
    MalformedDailyRunRow BEFORE the create — defaulting it to 0 would
    silently re-open spent headroom, the exact breach the strict-counter
    rule exists to prevent. No new row is created."""
    bad = _prior_day_row(record_id="rec_bad")
    del bad.attributes["messages_sent"]
    mock_crm.query_object_records.return_value = [bad]
    with pytest.raises(MalformedDailyRunRow), open_daily_run(mock_crm, run_id="retry-run", machine_id="laptop-A"):
        pytest.fail("body must not execute when a prior counter is malformed")
    mock_crm.create_object_record.assert_not_called()


# ── Parametrized lease counter tests for connections/visits kinds ─────────────
# Port of upstream #182: invites + re-checks are now charged on the daily_run
# row via the two-phase lease. These pin the Attio-side counter contract for the
# two newly-charged kinds (the messages path is covered above).


@pytest.mark.parametrize("kind,attr,cap", [
    ("connections", "connections_sent", MAX_CONNECTIONS_PER_DAY),
    ("visits", "visits_sent", MAX_VISITS_PER_DAY),
])
def test_reserve_and_confirm_updates_counter_for_kind(kind, attr, cap):
    """reserve 3, confirm confirmed_count=2 → the provider PATCH body carries
    attr == 2, and remaining(kind) == cap - 2."""
    crm = MagicMock()
    run = _run(crm=crm)

    assert run.remaining(kind) == cap
    token = run.reserve_send(kind, 3)
    assert run.remaining(kind) == cap - 3
    # Lease alone must NOT write to the CRM.
    crm.update_object_record.assert_not_called()

    # Confirm with confirmed_count=2 (partial — e.g. 1 skipped).
    run.confirm_lease(token, confirmed_count=2)

    assert run.remaining(kind) == cap - 2
    _obj, _rid, values = crm.update_object_record.call_args[0]
    assert values[attr] == 2, f"Expected {attr}=2 in PATCH body, got: {values}"


@pytest.mark.parametrize("kind,cap", [
    ("connections", MAX_CONNECTIONS_PER_DAY),
    ("visits", MAX_VISITS_PER_DAY),
])
def test_reserve_send_blocks_at_kind_cap(kind, cap):
    """reserve full cap → second reserve_send(kind, 1) raises CapacityExhausted;
    release → remaining(kind) back to cap."""
    run = _run()

    token = run.reserve_send(kind, cap)
    assert run.remaining(kind) == 0

    with pytest.raises(CapacityExhausted) as excinfo:
        run.reserve_send(kind, 1)
    assert excinfo.value.kind == kind
    assert excinfo.value.available == 0

    run.release_lease(token)
    assert run.remaining(kind) == cap
