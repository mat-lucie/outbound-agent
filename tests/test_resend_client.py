"""Tests for clients/resend_client.py — reply_to parameter."""

from unittest.mock import MagicMock, patch

from clients.resend_client import ResendClient


class TestSendEmailReplyTo:
    @patch.dict("os.environ", {"RESEND_API_KEY": "test-key"})
    def test_send_email_includes_reply_to(self):
        client = ResendClient()
        mock_response = MagicMock()
        mock_response.json.return_value = {"id": "email-123"}
        mock_response.content = b'{"id": "email-123"}'
        client._client = MagicMock()
        client._client.post.return_value = mock_response

        client.send_email(
            to="test@example.com",
            subject="Test",
            html="<p>Hello</p>",
            reply_to="reply@example.com",
        )

        call_args = client._client.post.call_args
        payload = call_args[1]["json"] if "json" in call_args[1] else call_args[0][1]
        assert payload["reply_to"] == "reply@example.com"

    @patch.dict("os.environ", {"RESEND_API_KEY": "test-key"})
    def test_send_email_without_reply_to(self):
        client = ResendClient()
        mock_response = MagicMock()
        mock_response.json.return_value = {"id": "email-123"}
        mock_response.content = b'{"id": "email-123"}'
        client._client = MagicMock()
        client._client.post.return_value = mock_response

        client.send_email(
            to="test@example.com",
            subject="Test",
            html="<p>Hello</p>",
        )

        call_args = client._client.post.call_args
        payload = call_args[1]["json"] if "json" in call_args[1] else call_args[0][1]
        assert "reply_to" not in payload
