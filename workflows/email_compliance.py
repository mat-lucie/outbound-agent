"""Email compliance helpers: CAN-SPAM/GDPR footer, send-gate, List-Unsubscribe
header, plaintext rendering, and an idempotency sent-ledger.

The email campaign path must, by law (CAN-SPAM in the US, comparable rules
elsewhere), include in every commercial message: a clear opt-out mechanism and
a valid physical postal address. This module centralizes those requirements:

  * ``assert_email_compliance_ready`` — fail-loud send-gate (dry-run exempt):
    refuses to send live when no physical address is configured.
  * ``list_unsubscribe_header`` — the RFC 2369/8058 ``List-Unsubscribe`` mailto
    header (one-click unsubscribe in Gmail/Outlook → lands in the operator's
    unsubscribe inbox).
  * ``append_footer`` — appends the visible CAN-SPAM footer (sender org +
    physical address + opt-out line) and returns both the HTML body and a
    plaintext alternative.
  * sent-ledger (``already_sent``/``mark_sent``) — a local append-only record so
    a crash between the provider send and the CRM stage write does not re-send
    the same email on the next run.

Env config (read at call time so tests can monkeypatch; consistent with the
existing env-only EMAIL_FROM/EMAIL_REPLY_TO):
  * EMAIL_PHYSICAL_ADDRESS  — required for live sends (CAN-SPAM postal address)
  * EMAIL_SENDER_ORG        — legal sender name (default: EMAIL_FROM display name)
  * EMAIL_UNSUBSCRIBE_MAILTO— unsubscribe inbox (default: EMAIL_REPLY_TO)

What this module does NOT do (documented operator infra): host an HTTP
unsubscribe endpoint (RFC 8058 one-click POST) or receive Resend
bounce/complaint webhooks. Those require an internet-reachable service the
operator stands up; see GETTING_STARTED.md.
"""

from __future__ import annotations

import json
import os
import re
from html import unescape
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date


class ComplianceError(Exception):
    """Raised when a live email send would violate a compliance requirement
    (e.g. no physical postal address configured). Always fails loud — never a
    silent send of a non-compliant message."""


# ── Env-backed config (read live) ────────────────────────────────────────────


def _physical_address() -> str:
    return os.environ.get("EMAIL_PHYSICAL_ADDRESS", "").strip()


def _unsubscribe_mailto() -> str:
    return (
        os.environ.get("EMAIL_UNSUBSCRIBE_MAILTO")
        or os.environ.get("EMAIL_REPLY_TO")
        or ""
    ).strip()


def _sender_org() -> str:
    """Legal sender name for the footer. Defaults to the display name parsed
    from EMAIL_FROM (e.g. ``"Acme <hi@acme.com>"`` → ``"Acme"``), else the
    bare EMAIL_FROM value."""
    explicit = os.environ.get("EMAIL_SENDER_ORG", "").strip()
    if explicit:
        return explicit
    email_from = os.environ.get("EMAIL_FROM", "").strip()
    if "<" in email_from:
        return email_from.split("<", 1)[0].strip() or email_from
    return email_from


# ── Send-gate ─────────────────────────────────────────────────────────────────


def assert_email_compliance_ready(*, dry_run: bool = False) -> None:
    """Fail loud before a LIVE email send if a hard compliance requirement is
    unmet. Currently: a physical postal address (CAN-SPAM). ``dry_run`` is
    exempt so an operator can preview without configuring everything first."""
    if dry_run:
        return
    if not _physical_address():
        raise ComplianceError(
            "EMAIL_PHYSICAL_ADDRESS is not set. CAN-SPAM requires a valid "
            "physical postal address in every commercial email. Set "
            "EMAIL_PHYSICAL_ADDRESS in your .env before sending live, or use "
            "--dry-run to preview."
        )


# ── List-Unsubscribe header ──────────────────────────────────────────────────


def list_unsubscribe_header() -> dict[str, str] | None:
    """Return the ``List-Unsubscribe`` mailto header, or ``None`` if no
    unsubscribe address is configured (no EMAIL_UNSUBSCRIBE_MAILTO / EMAIL_REPLY_TO).

    The mailto variant needs no hosted endpoint: the recipient's client shows a
    one-click "Unsubscribe" that emails the operator's inbox. (The RFC 8058
    one-click HTTP POST variant requires an operator-hosted endpoint and is out
    of scope — see module docstring.)"""
    mailto = _unsubscribe_mailto()
    if not mailto:
        return None
    return {"List-Unsubscribe": f"<mailto:{mailto}?subject=unsubscribe>"}


# ── Footer (visible CAN-SPAM block) + plaintext rendering ─────────────────────


def build_footer() -> tuple[str, str]:
    """Return ``(html_footer, text_footer)`` carrying the sender org, physical
    address, and opt-out line. When the address is unset (dry-run preview), a
    visible placeholder is rendered so the operator notices it's missing."""
    org = _sender_org() or "[EMAIL_SENDER_ORG not set]"
    addr = _physical_address() or "[EMAIL_PHYSICAL_ADDRESS not set]"
    optout = "Reply with UNSUBSCRIBE to stop receiving these emails."
    html = (
        '<hr style="border:none;border-top:1px solid #ddd;margin:24px 0 12px">'
        '<p style="color:#888;font-size:12px;line-height:1.5">'
        f"{org}<br>{addr}<br>{optout}</p>"
    )
    text = f"\n\n--\n{org}\n{addr}\n{optout}"
    return html, text


_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_BREAK_RE = re.compile(r"(?i)</p>|<br\s*/?>")
_WS_RUN_RE = re.compile(r"[ \t]+")
_NEWLINE_RUN_RE = re.compile(r"\n{3,}")


def html_to_text(html: str) -> str:
    """Render a minimal plaintext alternative from an HTML body.

    Not a full HTML renderer — converts block breaks (</p>, <br>) to newlines,
    strips remaining tags, and unescapes entities. Sufficient for the simple
    drip templates; keeps a text/plain part present for deliverability."""
    text = _BLOCK_BREAK_RE.sub("\n", html)
    text = _TAG_RE.sub("", text)
    text = unescape(text)
    text = _WS_RUN_RE.sub(" ", text)
    text = _NEWLINE_RUN_RE.sub("\n\n", text)
    return text.strip()


def append_footer(html_body: str) -> tuple[str, str]:
    """Append the compliance footer to ``html_body`` and return
    ``(html_with_footer, text_with_footer)`` — the HTML to send and a plaintext
    alternative (body rendered to text + the text footer)."""
    html_footer, text_footer = build_footer()
    html = html_body + html_footer
    text = html_to_text(html_body) + text_footer
    return html, text


# ── Idempotency sent-ledger ──────────────────────────────────────────────────

LEDGER_DIR = Path.home() / ".outbound-agent"
LEDGER_FILE = LEDGER_DIR / "email_sent.json"


def _ledger_key(record_id: str, step: str) -> str:
    # Keyed by (record_id, step) WITHOUT the date: each email step is a
    # once-ever event per contact, so a send must never repeat even across
    # days. A date-scoped key would only dedupe same-day re-runs and re-send a
    # crash-interrupted contact (send ok, CRM write failed) on the NEXT day.
    return f"{record_id}|{step}"


def _load_ledger() -> dict[str, str]:
    try:
        with open(LEDGER_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def already_sent(record_id: str, step: str) -> bool:
    """True if this ``(record_id, step)`` email was already sent (on any prior
    run, possibly one that crashed before the CRM stage write) and must not be
    re-sent — same-day OR cross-day."""
    return _ledger_key(record_id, step) in _load_ledger()


def mark_sent(record_id: str, step: str, day: date) -> None:
    """Record ``(record_id, step)`` in the sent-ledger (value = send date for
    audit). Call IMMEDIATELY after the provider send returns and BEFORE the CRM
    stage write, so a crash in between never causes a re-send on any later run."""
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    ledger = _load_ledger()
    ledger[_ledger_key(record_id, step)] = day.isoformat()
    with open(LEDGER_FILE, "w") as f:
        json.dump(ledger, f, indent=2)


# ── Honoring opt-outs ─────────────────────────────────────────────────────────


def unsubscribe_email(attio, email: str) -> str | None:
    """Mark the Person with ``email`` as ``email_campaign_stage=UNSUBSCRIBED``.

    The operator's way to honor an opt-out (e.g. a reply to the List-Unsubscribe
    mailto, or a manual request). An UNSUBSCRIBED contact is excluded from
    ``ACTIVE_STAGES`` and the send loops' explicit skip, so they are never
    emailed again. Idempotent (re-setting UNSUBSCRIBED is a no-op write).

    Returns the updated Person ``record_id``, or ``None`` if no Person matches.
    """
    from models.email_campaign import EmailStage

    results = attio.search_people(filter_={"email_addresses": email}, limit=5)
    record_id = ""
    for record in results:
        rid = record.get("id", {}).get("record_id", "")
        if rid:
            record_id = rid
            break
    if not record_id:
        return None
    attio.update_person(
        record_id, {"email_campaign_stage": EmailStage.UNSUBSCRIBED.value}
    )
    return record_id
