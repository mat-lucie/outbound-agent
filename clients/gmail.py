"""Read-only Gmail client for Phase 0.6 email response detection (PR-243).

Standalone OPTIONAL data source — NOT part of the ``CRMProvider`` contract.
Gmail is a distinct external system (parallel to ``clients/google_sheets.py``
and ``clients/phantombuster.py``), so it stays off that contract on purpose.

Auth model: reuses the existing Google OAuth client secret
(``credentials/google-oauth.json`` — the same file ``clients/google_sheets.py``
uses) but keeps a SEPARATE authorized-user token
(``credentials/gmail-authorized-user.json``) with the ``gmail.readonly``
scope. Widening the Sheets token's scopes instead would invalidate it and
force re-consent on the DM-send path — the two tokens stay independent on
purpose.

The one-time consent flow is interactive (browser); run
``python -m clients.gmail`` once on the operator machine to mint the token.
The daily run never triggers the interactive flow: when the token is
absent/corrupt beyond healing, ``GmailClient.from_credentials`` raises
``GmailCredentialsMissing`` and the email-detection path skips (mirrors the
PhantomBuster scraper-id guard). That exception is the "feature disabled"
signal consumers degrade on — Gmail is OFF by default until a token exists.

Requires the ``[gmail]`` optional install extra
(``pip install -e '.[gmail]'``) for ``google-api-python-client`` /
``google-auth-oauthlib``; those are lazy-imported so the core install and
the mocked tests never need them. If a valid token is present but the extra
is NOT installed, ``from_credentials`` raises ``GmailDependencyMissing`` (a
``GmailCredentialsMissing`` subclass) rather than a raw ``ModuleNotFoundError``
— so a missing library degrades to the same visible skip as a missing token
instead of hard-crashing the daily run.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import shutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date

CREDENTIALS_DIR = os.path.join(os.path.dirname(__file__), "..", "credentials")
OAUTH_CREDENTIALS = os.path.join(CREDENTIALS_DIR, "google-oauth.json")
GMAIL_AUTHORIZED_USER = os.path.join(CREDENTIALS_DIR, "gmail-authorized-user.json")
GMAIL_AUTHORIZED_USER_BACKUP = GMAIL_AUTHORIZED_USER + ".bak"

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Cap per-prospect message listing; a reply thread never legitimately has
# more inbound messages than this within a campaign window.
MAX_MESSAGES_PER_QUERY = 10

# Quoted-history markers: everything from the first match onward is the
# prior thread, not the prospect's new text.
# - The "On ... wrote:" attribution may be WRAPPED across lines by the
#   sending client, so the match is allowed to span up to ~200 chars
#   including newlines ([\s\S]) before "wrote:".
# - "De:"/"From:" header lines only count as a forwarded-header block when
#   they contain an address-ish token (@ or <) — prose that merely starts
#   with "From: our side..." must not truncate a real reply.
_QUOTED_HISTORY_RE = re.compile(
    r"(?:^On [\s\S]{0,200}?wrote:\s*$)"
    r"|(?:^El [\s\S]{0,200}?escribió:\s*$)"   # ES Gmail attribution
    r"|(?:^-{2,}\s*Original Message\s*-{2,}\s*$)"
    r"|(?:^_{5,}\s*$)"
    r"|(?:^De:\s.*[@<].*$)"      # ES/PT forwarded-header block
    r"|(?:^From:\s.*[@<].*$)",
    re.MULTILINE | re.IGNORECASE,
)


class GmailCredentialsMissing(Exception):
    """Gmail token/client secret unavailable — email detection should skip."""


class GmailDependencyMissing(GmailCredentialsMissing):
    """The optional ``[gmail]`` extra is not installed — email detection skips.

    Subclasses :class:`GmailCredentialsMissing` on purpose: every consumer
    already guards on that base type, so a missing Google library degrades to
    the SAME visible skip as a missing token instead of hard-crashing the run
    with a raw ``ModuleNotFoundError``. It stays distinguishable (and carries
    an actionable install hint) for callers that want to tell the two apart.
    """


def _is_valid_token(path: str) -> bool:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return False
    return bool(data.get("refresh_token"))


def _heal_corrupt_token() -> None:
    """Same non-atomic-refresh healing as clients/google_sheets.py."""
    if not os.path.exists(GMAIL_AUTHORIZED_USER) or _is_valid_token(GMAIL_AUTHORIZED_USER):
        return
    if _is_valid_token(GMAIL_AUTHORIZED_USER_BACKUP):
        shutil.copy2(GMAIL_AUTHORIZED_USER_BACKUP, GMAIL_AUTHORIZED_USER)
        print(f"  Restored corrupt Gmail OAuth token from {GMAIL_AUTHORIZED_USER_BACKUP}")
        return
    os.remove(GMAIL_AUTHORIZED_USER)
    # Loud on purpose: after this delete, email detection skips with the same
    # message as "never configured" — the operator must know the token
    # self-destructed and detection is now OFF until re-minted.
    print(
        f"  ⚠ Gmail OAuth token at {GMAIL_AUTHORIZED_USER} was corrupt with "
        f"no valid backup — REMOVED. Email response detection is disabled "
        f"until you re-mint it: python -m clients.gmail"
    )


def _backup_token() -> None:
    if not _is_valid_token(GMAIL_AUTHORIZED_USER):
        return
    tmp = f"{GMAIL_AUTHORIZED_USER_BACKUP}.tmp.{os.getpid()}"
    try:
        shutil.copy2(GMAIL_AUTHORIZED_USER, tmp)
        os.replace(tmp, GMAIL_AUTHORIZED_USER_BACKUP)
    except OSError:
        if os.path.exists(tmp):
            with contextlib.suppress(OSError):
                os.remove(tmp)


def strip_quoted_history(body: str) -> str:
    """Drop quoted prior-thread text so the classifier sees only new text.

    Cuts at the first quoted-history marker, then drops any remaining
    ``>``-prefixed quote lines.
    """
    match = _QUOTED_HISTORY_RE.search(body)
    if match:
        body = body[: match.start()]
    lines = [ln for ln in body.splitlines() if not ln.lstrip().startswith(">")]
    return "\n".join(lines).strip()


def _walk_for_plaintext(payload: dict) -> str:
    """Depth-first MIME walk preferring text/plain, falling back to text/html."""
    plain, html = _collect_bodies(payload)
    if plain:
        return plain
    if html:
        # Crude de-tagging is fine — the classifier needs the words, not
        # markup. Stripped tags become a SPACE (not ""), so adjacent
        # cell/span text keeps its token boundary ("<td>a</td><td>b" ->
        # "a b", not "ab").
        text = re.sub(r"<(?:br|/p|/div|/tr)[^>]*>", "\n", html, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        return text
    return ""


def _collect_bodies(part: dict) -> tuple[str, str]:
    plain, html = "", ""
    mime = part.get("mimeType", "")
    data = part.get("body", {}).get("data")
    if data and mime in ("text/plain", "text/html"):
        try:
            # Gmail sometimes omits base64url padding; re-pad before decode
            # so a real reply isn't silently dropped as empty.
            padded = data + "=" * (-len(data) % 4)
            decoded = base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
        except (ValueError, TypeError):
            decoded = ""
        if mime == "text/plain":
            plain = plain or decoded
        else:
            html = html or decoded
    for child in part.get("parts", []) or []:
        c_plain, c_html = _collect_bodies(child)
        plain = plain or c_plain
        html = html or c_html
    return plain, html


class GmailClient:
    """Thin read-only wrapper over the Gmail API ``users().messages()`` surface.

    Pass a prebuilt ``service`` for tests; production callers use
    :meth:`from_credentials`.
    """

    def __init__(self, service) -> None:
        self._service = service

    @classmethod
    def from_credentials(cls) -> GmailClient:
        """Build from the on-disk token. Raises GmailCredentialsMissing when
        the token is absent/unhealable — never triggers the interactive flow.
        """
        _heal_corrupt_token()
        if not _is_valid_token(GMAIL_AUTHORIZED_USER):
            raise GmailCredentialsMissing(
                f"No usable Gmail token at {GMAIL_AUTHORIZED_USER}. "
                "Run `python -m clients.gmail` once to mint it."
            )
        # Lazy imports: google-api-python-client is a runtime dep of this
        # path only (the [gmail] extra); tests inject a mock service and
        # never need it. A missing extra with a valid token present would
        # otherwise raise ModuleNotFoundError — which escapes every
        # consumer's `except GmailCredentialsMissing` guard and hard-crashes
        # the run. Re-raise it as GmailDependencyMissing (a subclass) so the
        # feature degrades to the same visible skip as a missing token.
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise GmailDependencyMissing(
                "Gmail support requires the optional extra: "
                "pip install -e '.[gmail]'"
            ) from exc

        creds = Credentials.from_authorized_user_file(GMAIL_AUTHORIZED_USER, GMAIL_SCOPES)
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        _backup_token()
        return cls(service)

    def search_inbound(self, from_address: str, after: date) -> list[dict]:
        """List inbound messages from ``from_address`` on/after ``after``.

        Returns [{"message_id", "thread_id"}] newest-first per Gmail's
        default ordering. Empty list on no results.
        """
        query = f"from:{from_address} after:{after.strftime('%Y/%m/%d')} -in:sent"
        resp = (
            self._service.users()
            .messages()
            .list(userId="me", q=query, maxResults=MAX_MESSAGES_PER_QUERY)
            .execute()
        )
        return [
            {"message_id": m["id"], "thread_id": m.get("threadId", "")}
            for m in resp.get("messages", []) or []
        ]

    def get_message(self, message_id: str) -> tuple[str, dict, int]:
        """Fetch one message.

        Returns ``(plain_text_body, headers, internal_date_ms)`` where
        ``headers`` is a lower-cased-name dict and the body has quoted
        history stripped.
        """
        msg = (
            self._service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        payload = msg.get("payload", {}) or {}
        headers = {
            h.get("name", "").lower(): h.get("value", "")
            for h in payload.get("headers", []) or []
        }
        body = strip_quoted_history(_walk_for_plaintext(payload))
        try:
            internal_date = int(msg.get("internalDate", 0))
        except (TypeError, ValueError):
            internal_date = 0
        return body, headers, internal_date


def is_auto_generated(headers: dict, from_header: str = "") -> bool:
    """True for bounces / auto-replies / OOO that must NOT flip a stage.

    An OOO flipping a prospect to RESPONDED would wrongly stop the
    sequence on a vacation responder; a bounce classified as a reply
    would pollute the classification data.
    """
    sender = (from_header or headers.get("from", "")).lower()
    if "mailer-daemon@" in sender or "postmaster@" in sender:
        return True
    auto_submitted = headers.get("auto-submitted", "").lower()
    if auto_submitted and auto_submitted != "no":
        return True
    if "x-autoreply" in headers or "x-autorespond" in headers:
        return True
    if headers.get("precedence", "").lower() in ("bulk", "auto_reply", "junk"):
        return True
    content_type = headers.get("content-type", "").lower()
    if "report-type=delivery-status" in content_type:
        return True
    subject = headers.get("subject", "").lower()
    return subject.startswith(("out of office", "automatic reply", "auto:", "respuesta automática"))


def _interactive_mint_token() -> None:  # pragma: no cover
    """One-time operator flow: mint credentials/gmail-authorized-user.json."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise GmailDependencyMissing(
            "Gmail support requires the optional extra: pip install -e '.[gmail]'"
        ) from exc

    flow = InstalledAppFlow.from_client_secrets_file(OAUTH_CREDENTIALS, GMAIL_SCOPES)
    creds = flow.run_local_server(port=0)
    with open(GMAIL_AUTHORIZED_USER, "w") as f:
        f.write(creds.to_json())
    _backup_token()
    print(f"Gmail token written to {GMAIL_AUTHORIZED_USER}")


if __name__ == "__main__":  # pragma: no cover
    _interactive_mint_token()
