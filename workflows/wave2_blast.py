"""Wave-2 blast: one-off re-engage email for the 94 stalled drip-campaign contacts.

Targets people whose `email_campaign_stage` is `email1_sent` or `email2_sent` —
i.e. mid-sequence contacts who went idle when the daily cron stopped running.
Sends the `wave2` template (en/es/pt) and advances them to `wave2_sent` so
`email-daily` no longer picks them up.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from clients.attio import AttioClient
    from clients.resend_client import ResendClient
from models.email_campaign import (
    CONTENT_DIR,
    EMAIL_FROM,
    EMAIL_REPLY_TO,
    EmailStage,
    detect_language,
    personalize_email,
)
from workflows.content_guard import assert_content_replaced
from workflows.cross_channel_suppression import build_suppression_set
from workflows.daily_check import _assert_no_unresolved_placeholders
from workflows.email_campaign import _build_linkedin_collision_set, _domain_from_email, _is_collision
from workflows.email_compliance import (
    append_footer,
    assert_email_compliance_ready,
    list_unsubscribe_header,
)
from workflows.email_lane_gate import assert_email_lane_enabled
from workflows.email_sequencer import is_weekday

logger = logging.getLogger(__name__)

WAVE2_STAGE = "wave2_sent"
WAVE2_SOURCE_STAGES = (EmailStage.EMAIL1_SENT, EmailStage.EMAIL2_SENT)


def _load_wave2_template(language: str) -> dict:
    with open(CONTENT_DIR / "emails.json") as f:
        templates = json.load(f)
    return templates["wave2"][language]


def run_wave2_blast(
    attio: AttioClient,
    resend: ResendClient | None,
    dry_run: bool = False,
    auto_confirm: bool = False,
    force_weekend: bool = False,
    max_n: int = 100,
) -> dict:
    """Send the wave-2 re-engage email to mid-sequence stalled contacts."""
    # Kill switch first — before the weekday return, before any CRM read.
    # A disarmed live run must abort loudly, not exit 0 as "weekend".
    assert_email_lane_enabled("email-wave2", dry_run=dry_run)

    today = date.today()

    if not is_weekday(today) and not force_weekend:
        click.echo("Weekend — no emails sent. Use --force-weekend to override.")
        return {"sent": 0, "reason": "weekend"}

    # Shipped-placeholder send gate (no-op on dry_run): a fresh install ships
    # neutral placeholder email copy with a sentinel; refuse to send it live.
    assert_content_replaced(dry_run=dry_run, filenames=("emails.json",))

    # CAN-SPAM send-gate (no-op on dry_run): require a physical postal address.
    assert_email_compliance_ready(dry_run=dry_run)

    click.echo("Building LinkedIn anti-collision set...")
    collision_set = _build_linkedin_collision_set(attio)
    click.echo(f"  {len(collision_set)} active LinkedIn contacts loaded.")

    # PR-37 (§3.1 hardest red line + §3.16): cross-channel suppression set.
    # Anyone marked suppress_re_engagement, classified negative/defensive, or
    # parked in NOT_INTERESTED/DEFENSIVE_HOLD must NOT receive a wave-2 email.
    click.echo("Building cross-channel suppression set...")
    suppression_set = build_suppression_set(attio)
    click.echo(f"  {len(suppression_set)} suppressed records loaded.")

    company_cache: dict[str, tuple[str, str]] = {}
    contacts: list[dict] = []
    for stage in WAVE2_SOURCE_STAGES:
        results = attio.search_people(
            filter_={"email_campaign_stage": stage.value},
            limit=50_000,
            fail_if_truncated=True,
        )
        for record in results:
            values = record.get("values", {})
            record_id = record.get("id", {}).get("record_id", "")
            # Skip id-less records: can't advance their stage or match the
            # suppression set, so never blind-send to them.
            if not record_id:
                continue

            name_data = values.get("name", [])
            first_name = name_data[0].get("first_name", "") if name_data else ""
            last_name = name_data[0].get("last_name", "") if name_data else ""

            email_data = values.get("email_addresses", [])
            email = ""
            if email_data:
                e = email_data[0]
                email = e.get("email_address", "") if isinstance(e, dict) else str(e)

            if not email or not first_name:
                continue

            person_country = ""
            ploc = values.get("primary_location", [])
            if ploc and isinstance(ploc, list):
                loc = ploc[0]
                if isinstance(loc, dict):
                    person_country = loc.get("country_code", "") or ""

            company = ""
            company_country = ""
            company_data = values.get("company", [])
            if company_data and isinstance(company_data, list):
                ref = company_data[0]
                if isinstance(ref, dict) and ref.get("target_record_id"):
                    cid = ref["target_record_id"]
                    if cid in company_cache:
                        company, company_country = company_cache[cid]
                    else:
                        # Exception discipline mirrors FU-3 (PR #111): a
                        # company-fetch failure for one record must NOT
                        # abort the blast, but programmer errors must
                        # not be demoted to silent skips either.
                        try:
                            cr = attio._request("GET", f"/objects/companies/records/{cid}")
                            cr_data = cr.get("data", cr)
                            cr_vals = cr_data.get("values", {})
                            cr_name = cr_vals.get("name", [])
                            if cr_name:
                                company = cr_name[0].get("value", "")
                            cr_loc = cr_vals.get("primary_location", [])
                            if cr_loc and isinstance(cr_loc, list):
                                company_country = cr_loc[0].get("country_code", "") or ""
                        except (SystemExit, KeyboardInterrupt):
                            # Never demote interpreter-control exceptions.
                            raise
                        except (ImportError, TypeError, AttributeError):
                            # Programmer errors (missing module, wrong call
                            # shape, renamed attribute) signal an actual
                            # bug — silently swallowing them as "transient
                            # company-fetch failure" would hide real
                            # regressions. Surface them so CI / monitoring
                            # sees the crash.
                            raise
                        except Exception as err:  # noqa: BLE001
                            # Transient runtime errors (Attio 5xx, network
                            # blip) — log with the failing record_id so an
                            # operator can audit blank-company rows in
                            # post-blast review. Do NOT cache the failed
                            # result (see else: clause below) — caching an
                            # empty-string default would poison the cache
                            # and silently ship blank-company emails for
                            # every subsequent person referencing the same
                            # company, with only ONE warning logged for N
                            # affected rows.
                            logger.warning(
                                "wave2_blast: company-fetch failed for record_id=%r, "
                                "person record_id=%r: %s",
                                cid, record_id, err,
                            )
                        else:
                            # Only cache successful fetches. Transient
                            # failures re-trigger the Attio call for the
                            # next person referencing the same company,
                            # which yields an independent warning log per
                            # affected row (correct oncall visibility) and
                            # naturally recovers once the infra is healthy.
                            company_cache[cid] = (company, company_country)

            domain = _domain_from_email(email)
            country_code = person_country or company_country
            contacts.append({
                "record_id": record_id,
                "first_name": first_name,
                "last_name": last_name,
                "email": email,
                "domain": domain,
                "company": company,
                "language": detect_language(domain, country_code),
                "stage": stage.value,
            })

    click.echo(f"Found {len(contacts)} stalled contacts (email1_sent + email2_sent).")

    if not contacts:
        click.echo("No contacts to wave-2 today.")
        return {"sent": 0, "collisions": 0}

    if max_n and len(contacts) > max_n:
        click.echo(f"Capping send at --max {max_n}.")
        contacts = contacts[:max_n]

    click.echo(f"\nReady to send wave-2 to {len(contacts)} contacts.")

    if not dry_run and not auto_confirm and not click.confirm(f"Send {len(contacts)} wave-2 emails?"):
        click.echo("Cancelled.")
        return {"sent": 0, "cancelled": True}

    if not dry_run and resend is None:
        raise RuntimeError("ResendClient not configured")

    today_str = today.isoformat()
    sent = 0
    collisions = 0
    suppressed = 0

    for contact in contacts:
        if _is_collision(contact["first_name"], contact["last_name"], contact["domain"], collision_set):
            click.echo(f"  [SKIP] Collision: {contact['first_name']} {contact['last_name']} ({contact['domain']})")
            collisions += 1
            continue

        if contact["record_id"] in suppression_set:
            click.echo(
                f"  [SKIP] Suppressed (cross-channel): {contact['first_name']} {contact['last_name']}"
            )
            suppressed += 1
            continue

        template = _load_wave2_template(contact["language"])
        subject = personalize_email(template["subject"], contact["first_name"], contact["company"])
        body = personalize_email(template["body_html"], contact["first_name"], contact["company"])

        _assert_no_unresolved_placeholders(
            [{"message": f"{subject}\n{body}", "linkedInUrl": contact["email"]}],
            "wave2",
        )

        if dry_run:
            click.echo(
                f"  [DRY RUN] wave2 | {contact['first_name']} {contact['last_name']} | "
                f"{contact['email']} | {contact['company']} | {contact['language']} | {subject}"
            )
        else:
            html, text = append_footer(body)
            # Non-dry-run path: resend is guaranteed non-None by the guard at
            # the top of this function (raises RuntimeError if unset).
            assert resend is not None
            resend.send_email(
                to=contact["email"],
                subject=subject,
                html=html,
                from_address=EMAIL_FROM,
                reply_to=EMAIL_REPLY_TO,
                text=text,
                headers=list_unsubscribe_header(),
            )
            attio.update_person(contact["record_id"], {
                "email_campaign_stage": WAVE2_STAGE,
                "email_campaign_last_sent": today_str,
            })
            click.echo(f"  Sent wave2 -> {contact['email']}")

        sent += 1

    click.echo(
        f"\nDone. Sent: {sent}, Collisions skipped: {collisions}, "
        f"Suppressed: {suppressed}"
    )
    return {"sent": sent, "collisions": collisions, "suppressed": suppressed}
