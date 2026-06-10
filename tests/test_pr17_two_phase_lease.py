"""PR-17 B-SD-006: two-phase messages lease — partial confirmation.

F-PR-8 shipped ``reserve_send`` / ``confirm_lease`` / ``release_lease`` with
all-or-nothing confirmation semantics. PR-17 extends ``confirm_lease`` to
accept ``confirmed_count``, so a reservation of N can settle with K < N
when PB's CSV reports fewer sends than the batch requested. The drift
(N - K) refunds to capacity, preserving the §3.1 contract that quota
consumed = sends actually executed (not optimistically requested).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from workflows.daily_run import (
    MAX_MESSAGES_PER_DAY,
    DailyRun,
)


@pytest.fixture
def run_handle() -> DailyRun:
    crm = MagicMock()
    return DailyRun(
        crm=crm,
        record_id="rec_dr",
        run_date="2026-05-21",
        machine_id="test-host",
        run_id="run-1",
        initial_counters={"connections": 0, "messages": 0, "visits": 0},
    )


# ── Partial confirmation ─────────────────────────────────────────────────


def test_confirm_lease_full_count_default_matches_lease(run_handle):
    """Default behaviour (no ``confirmed_count``) commits the full reservation —
    preserves F-PR-8's API contract for callers that haven't migrated."""
    token = run_handle.reserve_send("messages", 5)
    run_handle.confirm_lease(token)
    assert run_handle._counters["messages"] == 5
    assert run_handle.remaining("messages") == MAX_MESSAGES_PER_DAY - 5


def test_confirm_lease_partial_count_refunds_drift_to_capacity(run_handle):
    """Reserve 10, confirm 7 → counter += 7, capacity reflects only 7 consumed."""
    token = run_handle.reserve_send("messages", 10)
    assert run_handle.remaining("messages") == MAX_MESSAGES_PER_DAY - 10  # reserved hold

    run_handle.confirm_lease(token, confirmed_count=7)

    assert run_handle._counters["messages"] == 7
    # Drift (10 - 7 = 3) refunded — capacity reflects 7 consumed, not 10.
    assert run_handle.remaining("messages") == MAX_MESSAGES_PER_DAY - 7


def test_confirm_lease_zero_count_consumes_lease_no_counter_change(run_handle):
    """Reserve N, PB sent 0 (entire batch silent-skipped) → no counter bump,
    but the lease is consumed (subsequent confirm with the same token is no-op)."""
    token = run_handle.reserve_send("messages", 4)
    run_handle.confirm_lease(token, confirmed_count=0)

    assert run_handle._counters["messages"] == 0
    assert run_handle.remaining("messages") == MAX_MESSAGES_PER_DAY
    # Idempotent on second confirm — token gone.
    run_handle.confirm_lease(token, confirmed_count=999)
    assert run_handle._counters["messages"] == 0


def test_confirm_lease_overshoot_raises_value_error_and_reinstates_lease(run_handle):
    """``confirmed_count`` greater than the reservation is a programming bug.
    Surface ValueError loudly so test/CI catches the miscount; reinstate the
    lease so a follow-up confirm with the correct count still works."""
    token = run_handle.reserve_send("messages", 3)
    with pytest.raises(ValueError, match="confirmed_count=5"):
        run_handle.confirm_lease(token, confirmed_count=5)
    # Lease is back — counter untouched, capacity still reflects the hold.
    assert run_handle._counters["messages"] == 0
    assert run_handle.remaining("messages") == MAX_MESSAGES_PER_DAY - 3
    # Caller can retry with the correct count.
    run_handle.confirm_lease(token, confirmed_count=3)
    assert run_handle._counters["messages"] == 3


def test_confirm_lease_negative_count_raises_value_error(run_handle):
    """Negative ``confirmed_count`` is a programming bug — guard explicitly."""
    token = run_handle.reserve_send("messages", 2)
    with pytest.raises(ValueError, match="confirmed_count=-1"):
        run_handle.confirm_lease(token, confirmed_count=-1)
    # Lease reinstated.
    run_handle.release_lease(token)
    assert run_handle.remaining("messages") == MAX_MESSAGES_PER_DAY


def test_partial_confirm_crm_write_rolls_back_on_transport_error():
    """If the CRM write fails mid-confirm, the in-memory counter rolls back to
    its prior value and the lease is reinstated — caller retries.

    The counter write goes through the provider's update_object_record (the
    adapter's retrying layer); inject the transport error there.
    """
    crm = MagicMock()
    req = httpx.Request("PATCH", "https://api.attio.com/v2/x")
    failing_resp = httpx.Response(503, request=req, content=b"upstream down")
    crm.update_object_record.side_effect = httpx.HTTPStatusError(
        "upstream down", request=req, response=failing_resp,
    )

    run = DailyRun(
        crm=crm, record_id="rec_dr", run_date="2026-05-21",
        machine_id="test-host", run_id="run-1",
        initial_counters={"connections": 0, "messages": 0, "visits": 0},
    )
    token = run.reserve_send("messages", 6)
    with pytest.raises(httpx.HTTPStatusError):
        run.confirm_lease(token, confirmed_count=4)

    # Counter reverted; lease reinstated.
    assert run._counters["messages"] == 0
    assert token in run._reservations
