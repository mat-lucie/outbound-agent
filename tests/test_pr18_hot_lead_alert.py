"""PR-18 B-SD-002 + B-SD-003 + B-SD-012: hot-lead alert + defensive routing.

Locks in:
- ``should_emit_hot_lead`` trigger conditions (positive vs manual_unclassified
  with score threshold)
- Channel ordering: queue row FIRST (synchronous, durable), Resend SECOND
- ``fallback_used=True`` flag when Resend fails
- ``HotLeadEmitFailed`` raised when queue write fails
- Defensive replies route to DEFENSIVE_HOLD (NOT hot-lead, NOT RESPONDED)
"""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from workflows.hot_lead_alert import (
    HOT_LEAD_MANUAL_UNCLASSIFIED_SCORE_FLOOR,
    HotLeadEmitFailed,
    emit_hot_lead,
    should_emit_hot_lead,
)


def _attio_mock_no_existing_row(create_record_id: str = "rec_test") -> MagicMock:
    """Build an AttioClient mock whose ``_request`` answers escalate's
    two-call dance: (1) query for existing → returns empty list; (2)
    create row → returns a dict with an id.

    The hot_lead_alert module's fallback-PATCH path also uses
    ``attio._client.request("GET"|"PATCH", ...)`` to read/write the
    queue row body — those return MagicMock by default which is safe
    for the queue-row-only happy path.
    """
    attio = MagicMock()
    calls = {"n": 0}

    def _request(method, path, **kwargs):
        calls["n"] += 1
        # Query/list call → no existing row.
        if "/query" in path:
            return {"data": []}
        # Create call → return a row dict.
        return {"data": {"id": {"record_id": create_record_id}, "values": {}}}

    attio._request.side_effect = _request
    return attio


# ── should_emit_hot_lead trigger conditions ──────────────────────────────


@pytest.mark.parametrize(
    "classification,score,expected",
    [
        # positive → always fires regardless of score
        ("positive", 90, True),
        ("positive", 50, True),
        ("positive", 0, True),
        ("positive", None, True),
        # manual_unclassified → fires iff score >= 70
        ("manual_unclassified", 70, True),
        ("manual_unclassified", 100, True),
        ("manual_unclassified", 69, False),
        ("manual_unclassified", 0, False),
        ("manual_unclassified", None, False),  # None treated as 0
        # defensive → NEVER fires (routes to DEFENSIVE_HOLD instead)
        ("defensive", 100, False),
        ("defensive", 0, False),
        # negative / question / neutral → never hot lead
        ("negative", 100, False),
        ("question", 90, False),
        ("neutral", 80, False),
    ],
)
def test_should_emit_hot_lead_trigger_matrix(classification, score, expected):
    assert should_emit_hot_lead(classification, score) is expected


def test_hot_lead_score_floor_constant():
    """Lock the threshold at 70 — load-bearing for PR-20's manual-reply
    branch. A change here without coordinating PR-20 would silently
    shift which manual-unclassified prospects fire hot-lead alerts."""
    assert HOT_LEAD_MANUAL_UNCLASSIFIED_SCORE_FLOOR == 70


# ── emit_hot_lead channel ordering ───────────────────────────────────────


def test_emit_hot_lead_writes_queue_row_first_before_resend():
    """Queue row write happens BEFORE Resend send (B-SD-002 durability).

    The order is verified by attaching a counter to both side effects;
    queue write counter must be < Resend counter.
    """
    call_order: list[str] = []
    attio = MagicMock()

    def _attio_request(method, path, **kwargs):
        call_order.append(f"attio:{method}:{path}")
        if "/query" in path:
            return {"data": []}
        return {"data": {"id": {"record_id": "rec_queue_1"}, "values": {}}}

    attio._request.side_effect = _attio_request

    resend = MagicMock()

    def _resend_send(*args, **kwargs):
        call_order.append("resend:send_email")
        return {"id": "email_1"}

    resend.send_email.side_effect = _resend_send

    emit_hot_lead(
        record_id="rec_lead_1",
        response_classification="positive",
        prospect_score=85,
        message_excerpt="Interested, schedule a demo!",
        thread_url="https://linkedin.com/in/lead/thread/abc",
        attio=attio,
        resend=resend,
        prospect_name="María García",
        prospect_company="ACME Industria",
    )

    # The first call must be an attio._request (queue row write).
    assert call_order, "no calls captured"
    assert call_order[0].startswith("attio:"), (
        f"first call should be queue row write, got {call_order[0]}"
    )
    # Resend's send_email must come strictly AFTER the queue write.
    assert "resend:send_email" in call_order, "resend.send_email not called"
    resend_idx = call_order.index("resend:send_email")
    attio_first_idx = next(
        i for i, c in enumerate(call_order) if c.startswith("attio:")
    )
    assert attio_first_idx < resend_idx, (
        f"resend fired before queue row: {call_order}"
    )


def test_emit_hot_lead_queue_failure_raises_HotLeadEmitFailed():
    """If the queue-row write fails, emit_hot_lead must raise — the
    operator's only durability signal is gone. Resend MUST NOT fire
    because that would create a hot lead the operator can't track."""
    attio = MagicMock()
    req = httpx.Request("POST", "https://api.attio.com/v2/x")
    attio._request.side_effect = httpx.HTTPStatusError(
        "Attio 500", request=req, response=httpx.Response(500, request=req)
    )
    resend = MagicMock()

    with pytest.raises(HotLeadEmitFailed, match="rec_failed"):
        emit_hot_lead(
            record_id="rec_failed",
            response_classification="positive",
            prospect_score=80,
            message_excerpt="...",
            thread_url="https://x",
            attio=attio,
            resend=resend,
        )

    # Resend MUST NOT have been called — queue failed first.
    assert not resend.send_email.called, (
        "resend.send_email called despite queue-row write failure — "
        "this creates an untrackable hot lead"
    )


def test_emit_hot_lead_resend_failure_returns_payload_with_fallback_used():
    """If queue row succeeds but Resend fails, emit_hot_lead returns the
    payload with ``fallback_used=True``. Operator already has the queue
    row; the Resend failure is an annotation, not a halt."""
    attio = _attio_mock_no_existing_row(create_record_id="rec_q1")
    # Hot-lead-alert's fallback PATCH path uses attio._client.request;
    # MagicMock default behaviour returns a MagicMock which has .json()
    # available; that's enough for the fallback to not crash.

    resend = MagicMock()
    req = httpx.Request("POST", "https://api.resend.com/emails")
    resend.send_email.side_effect = httpx.RequestError("connection refused", request=req)

    payload = emit_hot_lead(
        record_id="rec_resend_fail",
        response_classification="positive",
        prospect_score=92,
        message_excerpt="Yes please demo!",
        thread_url="https://linkedin.com/in/x",
        attio=attio,
        resend=resend,
    )

    assert payload["fallback_used"] is True
    # Both queue rows (hot_lead and resend_delivery_failed) involve a
    # query-then-create round trip. We expect at least 4 calls: 2 per
    # escalation × 2 escalations.
    create_calls = [
        c for c in attio._request.call_args_list
        if "/operator_review_queue/records" in str(c) and "/query" not in str(c)
    ]
    assert len(create_calls) >= 2, (
        "expected at least 2 create writes (hot_lead + resend_delivery_failed); "
        f"got {len(create_calls)}: {create_calls}"
    )


def test_emit_hot_lead_no_resend_client_writes_queue_only():
    """When ``resend=None`` (test path or operator disabled email),
    only the queue row fires — no error."""
    attio = _attio_mock_no_existing_row(create_record_id="rec_q2")

    payload = emit_hot_lead(
        record_id="rec_q_only",
        response_classification="positive",
        prospect_score=75,
        message_excerpt="...",
        thread_url="https://x",
        attio=attio,
        resend=None,
    )

    assert payload["fallback_used"] is False
    assert payload["record_id"] == "rec_q_only"
    assert payload["response_classification"] == "positive"


def test_emit_hot_lead_truncates_long_message_excerpt():
    """The queue UI gets a 500-char preview; the full thread is on
    LinkedIn. Truncation prevents the queue from becoming the storage
    layer for arbitrary message bodies."""
    attio = _attio_mock_no_existing_row(create_record_id="rec_q3")

    long_body = "A" * 2000
    payload = emit_hot_lead(
        record_id="rec_long",
        response_classification="positive",
        prospect_score=80,
        message_excerpt=long_body,
        thread_url="https://x",
        attio=attio,
        resend=None,
    )
    assert len(payload["message_excerpt"]) == 500
