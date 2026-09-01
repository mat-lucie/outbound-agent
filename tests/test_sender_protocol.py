"""Sender seam: `PBSender` conformance to the `Sender` protocol.

The send code in `workflows/daily_check.py` calls the PB-shaped per-launch
methods (`launch_invite_batch` / `launch_dm_batch`) directly — those are
pinned by the existing send-path suites. This file pins the protocol
surface itself: the three `Sender` methods exist, delegate to the same
transport flow, and `fetch_events` stays empty on PB (its detection is
scrape-based).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from clients.pb_envelope import INVITE_OPTIMISTIC_ADVANCE, SendOutcome
from clients.sender import LeadEvent, PBSender, PBSendResult, Sender


def _pb(csv_text: str | None, log_output: str = "") -> MagicMock:
    pb = MagicMock()
    pb.launch_agent.return_value = MagicMock(container_id="cid-1")
    pb.wait_for_completion.return_value = MagicMock(
        status="finished", log_output=log_output
    )
    pb.download_result_csv.return_value = csv_text
    return pb


def _sender(pb: MagicMock) -> PBSender:
    return PBSender(
        pb,
        network_booster_id="nb-id",
        message_sender_id="ms-id",
        write_sheet=lambda rows: "https://sheet/x",
        session_args=lambda: {"sessionCookie": "c", "userAgent": "ua"},
    )


def test_pbsender_satisfies_sender_protocol():
    assert isinstance(_sender(_pb(None)), Sender)


def test_fetch_events_is_empty_on_pb():
    # PB detection stays scrape-based (Phase 0 / 0.5); the event feed is a
    # capability of a poll-based transport only.
    assert _sender(_pb(None)).fetch_events(None) == []
    assert _sender(_pb(None)).fetch_events(None) == []  # idempotent no-op


def test_send_invites_delegates_to_invite_launch_flow():
    pb = _pb("query,error\n")  # NB CSV: no status column → parsed Skipped/0
    sender = _sender(pb)
    batch = [
        {"linkedInUrl": "https://linkedin.com/in/acme-alice", "message": "hola"},
        {"linkedInUrl": "https://linkedin.com/in/acme-bob", "message": "hola"},
    ]

    outcome = sender.send_invites(batch)

    assert isinstance(outcome, SendOutcome)
    pb.launch_agent.assert_called_once()
    agent_id, args = pb.launch_agent.call_args.args
    assert agent_id == "nb-id"
    assert args["spreadsheetUrl"] == "https://sheet/x"
    assert args["sessionCookie"] == "c"
    # Clean, authenticated, uncapped launch → the invite override marks all
    # requested URLs sent (compute_invite_outcome).
    assert outcome.drift_skipped_reason == INVITE_OPTIMISTIC_ADVANCE
    assert outcome.sent_count == 2


def test_send_dm_delegates_to_single_row_dm_launch():
    url = "https://linkedin.com/in/acme-alice"
    pb = _pb(f"query,status\n{url},Message sent\n")
    sender = _sender(pb)

    outcome = sender.send_dm({"linkedInUrl": url, "name": "Alice"}, "hola Alice")

    assert isinstance(outcome, SendOutcome)
    pb.launch_agent.assert_called_once()
    agent_id, args = pb.launch_agent.call_args.args
    assert agent_id == "ms-id"
    assert args["message"] == "#message#"  # per-row from sheet column
    assert outcome.csv_status == "Message sent"
    assert outcome.sent_count == 1


def test_launch_dm_batch_returns_full_pb_envelope():
    """The send code needs launch + completion alongside the outcome (advance
    gate keys on the container id; queue rows embed the launch)."""
    url = "https://linkedin.com/in/acme-alice"
    pb = _pb(f"query,status\n{url},Message sent\n")
    sender = _sender(pb)

    result = sender.launch_dm_batch(
        [{"linkedInUrl": url, "message": "hola"}],
        {url},
        step_label="dm1",
    )

    assert isinstance(result, PBSendResult)
    assert result.launch is pb.launch_agent.return_value
    assert result.completion is pb.wait_for_completion.return_value
    assert result.outcome.container_id == "cid-1"


def test_missing_agent_id_raises_instead_of_launching():
    pb = _pb(None)
    sender = PBSender(pb, write_sheet=lambda rows: "https://sheet/x")
    with pytest.raises(ValueError, match="network_booster_id"):
        sender.launch_invite_batch(
            [{"linkedInUrl": "https://linkedin.com/in/acme-alice"}], set()
        )
    with pytest.raises(ValueError, match="message_sender_id"):
        sender.launch_dm_batch(
            [{"linkedInUrl": "https://linkedin.com/in/acme-alice"}],
            set(),
            step_label="dm1",
        )
    pb.launch_agent.assert_not_called()


def test_lead_event_is_frozen():
    ev = LeadEvent(
        event_type="invitation-accepted",
        lead_linkedin_url="https://linkedin.com/in/acme-alice",
        occurred_at=None,
        raw={},
    )
    with pytest.raises(AttributeError):
        ev.event_type = "message-replied"  # type: ignore[misc]
