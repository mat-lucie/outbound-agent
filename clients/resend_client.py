"""Resend email client for sending sales reports."""

import os

import httpx


class ResendClient:
    """Thin wrapper for the Resend email API."""

    BASE_URL = "https://api.resend.com"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ["RESEND_API_KEY"]
        self._client = httpx.Client(
            base_url=self.BASE_URL,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    def send_email(
        self,
        to: str | list[str],
        subject: str,
        html: str,
        from_address: str = os.environ.get("RESEND_FROM_ADDRESS", "Outbound Agent <outbound@example.com>"),
        reply_to: str | None = None,
        text: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict:
        """Send an email via Resend.

        Args:
            text: optional plaintext alternative part (improves deliverability
                and accessibility; Resend renders multipart when both html+text
                are present).
            headers: optional custom SMTP headers, e.g.
                ``{"List-Unsubscribe": "<mailto:unsubscribe@...>"}`` for
                one-click unsubscribe support in Gmail/Outlook.
        """
        if isinstance(to, str):
            to = [to]
        payload: dict = {
            "from": from_address,
            "to": to,
            "subject": subject,
            "html": html,
        }
        if reply_to:
            payload["reply_to"] = reply_to
        if text:
            payload["text"] = text
        if headers:
            payload["headers"] = headers
        resp = self._client.post(
            "/emails",
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
