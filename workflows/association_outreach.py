"""Association outreach workflow.

Sends one-shot outreach emails to industry associations (e.g., AFAMO) asking
for partnership intros or member directory access. NOT a drip campaign.

Reads contacts from `content/association_outreach.json` and tracks already-sent
state in `~/.outbound-agent/association_outreach_sent.json` so re-runs are idempotent.

Usage (from cli.py):
    python3 cli.py email-association --dry-run
    python3 cli.py email-association --yes
"""

import json
from datetime import date, datetime
from pathlib import Path

import click

from clients.resend_client import ResendClient
from models.business_calendar import is_send_day
from workflows.email_compliance import (
    append_footer,
    assert_email_compliance_ready,
    list_unsubscribe_header,
)

CONTACTS_FILE = Path(__file__).parent.parent / "content" / "association_outreach.json"
SENT_STATE_FILE = Path.home() / ".outbound-agent" / "association_outreach_sent.json"


def _load_contacts() -> list[dict]:
    """Load all association outreach contacts from the JSON file."""
    with open(CONTACTS_FILE) as f:
        data = json.load(f)
    return data.get("contacts", [])


def _load_sent_state() -> dict:
    """Load the set of contact IDs already sent."""
    if not SENT_STATE_FILE.exists():
        return {}
    with open(SENT_STATE_FILE) as f:
        return json.load(f)


def _save_sent_state(state: dict) -> None:
    """Persist the sent state."""
    SENT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SENT_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_pending_association_emails() -> list[dict]:
    """Return all contacts that haven't been sent yet."""
    contacts = _load_contacts()
    sent = _load_sent_state()
    return [c for c in contacts if c["id"] not in sent]


def run_association_outreach(
    resend: ResendClient | None,
    dry_run: bool = False,
    auto_confirm: bool = False,
    force_weekend: bool = False,
) -> dict:
    """Send pending association outreach emails. Idempotent.

    Args:
        resend: Resend client (None for dry-run only).
        dry_run: If True, print what would be sent without sending.
        auto_confirm: If True, skip the interactive confirmation prompt.
        force_weekend: If True, send even on Sat/Sun.

    Returns:
        Summary dict with counts.
    """
    today = date.today()
    if not is_send_day(today) and not force_weekend:
        click.echo("Weekend — no association emails sent. Use --force-weekend to override.")
        return {"pending": 0, "sent": 0, "errors": 0, "skipped": 0, "reason": "weekend"}

    # CAN-SPAM send-gate (no-op on dry_run): require a physical postal address.
    assert_email_compliance_ready(dry_run=dry_run)

    pending = get_pending_association_emails()
    summary = {"pending": len(pending), "sent": 0, "errors": 0, "skipped": 0}

    if not pending:
        click.echo("No pending association outreach emails. All recipients already contacted.")
        return summary

    click.echo(f"=== Association Outreach — {len(pending)} pending ===\n")
    for c in pending:
        click.echo(f"  → {c['name']} <{c['email']}>")
        click.echo(f"    Subject: {c['subject']}")
        click.echo(f"    Org: {c['organization']}")
        click.echo()

    if dry_run:
        click.echo("[DRY RUN] No emails sent.")
        return summary

    if not auto_confirm and not click.confirm(f"Send {len(pending)} association outreach email(s)?"):
        click.echo("Cancelled.")
        summary["skipped"] = len(pending)
        return summary

    if resend is None:
        click.echo("Error: ResendClient is required for live send.")
        summary["errors"] = len(pending)
        return summary

    sent_state = _load_sent_state()
    for c in pending:
        try:
            html, text = append_footer(c["body_html"])
            result = resend.send_email(
                to=c["email"],
                subject=c["subject"],
                html=html,
                text=text,
                headers=list_unsubscribe_header(),
            )
            click.echo(f"  ✓ Sent to {c['email']} (Resend ID: {result.get('id', 'unknown')})")
            sent_state[c["id"]] = {
                "sent_at": datetime.utcnow().isoformat() + "Z",
                "email": c["email"],
                "resend_id": result.get("id", ""),
            }
            _save_sent_state(sent_state)
            summary["sent"] += 1
        except Exception as e:
            click.echo(f"  ✗ Failed to send to {c['email']}: {e}")
            summary["errors"] += 1

    click.echo("\n--- Association Outreach Summary ---")
    click.echo(f"Sent:    {summary['sent']}")
    click.echo(f"Errors:  {summary['errors']}")
    click.echo(f"Skipped: {summary['skipped']}")
    return summary
