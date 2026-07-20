"""Tests for clients/gmail.py — Phase 0.6 read-only Gmail client (PR-243).

Service object is MOCKED throughout; the google-api-python-client [gmail]
extra is never imported. All identities are synthetic (example.com).
"""

from __future__ import annotations

import base64
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from clients.gmail import (
    GmailClient,
    GmailCredentialsMissing,
    is_auto_generated,
    strip_quoted_history,
)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _make_service(list_resp=None, get_resp=None):
    service = MagicMock()
    messages = service.users.return_value.messages.return_value
    messages.list.return_value.execute.return_value = list_resp or {}
    messages.get.return_value.execute.return_value = get_resp or {}
    return service, messages


# ── from_credentials — the "feature disabled" contract ─────────────


def test_from_credentials_raises_when_token_missing(tmp_path):
    """Absent token → GmailCredentialsMissing (skip signal), not a crash."""
    with (
        patch("clients.gmail.GMAIL_AUTHORIZED_USER", str(tmp_path / "absent.json")),
        pytest.raises(GmailCredentialsMissing),
    ):
        GmailClient.from_credentials()


def test_from_credentials_raises_on_zero_byte_token(tmp_path):
    """0-byte token heals to nothing → GmailCredentialsMissing, not a crash."""
    token = tmp_path / "gmail-authorized-user.json"
    token.write_text("")
    with (
        patch("clients.gmail.GMAIL_AUTHORIZED_USER", str(token)),
        patch("clients.gmail.GMAIL_AUTHORIZED_USER_BACKUP", str(token) + ".bak"),
        pytest.raises(GmailCredentialsMissing),
    ):
        GmailClient.from_credentials()


# ── search_inbound ─────────────────────────────────────────────────


def test_search_inbound_query_shape():
    service, messages = _make_service(
        list_resp={"messages": [{"id": "m1", "threadId": "t1"}]}
    )
    client = GmailClient(service)
    hits = client.search_inbound("prospect@example.com", date(2026, 7, 1))
    assert hits == [{"message_id": "m1", "thread_id": "t1"}]
    kwargs = messages.list.call_args.kwargs
    assert kwargs["q"] == "from:prospect@example.com after:2026/07/01 -in:sent"
    assert kwargs["userId"] == "me"


def test_search_inbound_empty_results():
    service, _ = _make_service(list_resp={})
    assert GmailClient(service).search_inbound("x@example.com", date(2026, 7, 1)) == []


# ── get_message / MIME extraction ──────────────────────────────────


def test_get_message_prefers_text_plain_and_strips_quotes():
    body = "Gracias, vamos a desarrollarlo internamente.\n\nOn Mon, Jul 13, 2026 Sender wrote:\n> hola"
    get_resp = {
        "internalDate": "1752345600000",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [{"name": "From", "value": "Prospect <prospect@example.com>"}],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64(body)}},
                {"mimeType": "text/html", "body": {"data": _b64("<p>html version</p>")}},
            ],
        },
    }
    service, _ = _make_service(get_resp=get_resp)
    text, headers, internal = GmailClient(service).get_message("m1")
    assert text == "Gracias, vamos a desarrollarlo internamente."
    assert headers["from"] == "Prospect <prospect@example.com>"
    assert internal == 1752345600000


def test_get_message_falls_back_to_html():
    get_resp = {
        "internalDate": "0",
        "payload": {
            "mimeType": "text/html",
            "headers": [],
            "body": {"data": _b64("<div>No thanks<br>we build in-house</div>")},
        },
    }
    service, _ = _make_service(get_resp=get_resp)
    text, _, _ = GmailClient(service).get_message("m1")
    assert "No thanks" in text and "we build in-house" in text
    assert "<" not in text


def test_strip_quoted_history_drops_gt_lines():
    assert strip_quoted_history("new text\n> old line\n>> older") == "new text"


def test_strip_quoted_history_handles_wrapped_attribution():
    """Gmail wraps long 'On ... wrote:' attributions across lines."""
    body = (
        "Interesados, hablemos.\n\n"
        "On Mon, Jul 13, 2026 at 3:00 PM Sender Name <sender@example.com>\n"
        "wrote:\n"
        "full prior pitch text here"
    )
    assert strip_quoted_history(body) == "Interesados, hablemos."


def test_strip_quoted_history_handles_spanish_attribution():
    body = (
        "No gracias.\n\n"
        "El lun, 13 jul 2026 a las 15:00, Sender (<sender@example.com>) escribió:\n"
        "pitch"
    )
    assert strip_quoted_history(body) == "No gracias."


def test_strip_quoted_history_keeps_prose_starting_with_from():
    """'From: our side...' is prose, not a forwarded-header block — it must
    not truncate the reply."""
    body = "From: our side we can start in Q3.\nLet's talk."
    assert strip_quoted_history(body) == body


def test_strip_quoted_history_cuts_real_forwarded_header():
    body = "ok\nFrom: Sender Name <sender@example.com>\nSubject: Intro"
    assert strip_quoted_history(body) == "ok"


# ── auto-generated filtering ───────────────────────────────────────


@pytest.mark.parametrize(
    "headers",
    [
        {"from": "MAILER-DAEMON@googlemail.com"},
        {"from": "postmaster@corp.com"},
        {"auto-submitted": "auto-replied"},
        {"auto-submitted": "auto-generated"},
        {"x-autoreply": "yes"},
        {"precedence": "bulk"},
        {"content-type": "multipart/report; report-type=delivery-status"},
        {"subject": "Automatic reply: Intro"},
        {"subject": "Out of Office"},
    ],
)
def test_is_auto_generated_true(headers):
    assert is_auto_generated(headers) is True


@pytest.mark.parametrize(
    "headers",
    [
        {"from": "prospect@example.com", "subject": "RE: Intro"},
        {"auto-submitted": "no"},
        {},
    ],
)
def test_is_auto_generated_false(headers):
    assert is_auto_generated(headers) is False
