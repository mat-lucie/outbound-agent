"""Idempotent setup of Attio attributes needed by the industry-aware scorer
and persistence work.

Creates 4 list-entry attributes on the LinkedIn Outreach list and 2 record
attributes on the Companies object, skipping any that already exist (matched
by api_slug). Safe to re-run.

The optional Follow-up Radar feature (``--feature radar``) provisions the
warm-account follow-up state attributes (PR-211/214/247). The radar is
DISABLED by default: a fresh install never touches these attributes, and the
radar command fails-closed-clean when they are absent. Run the selector only
once you want the radar (see GETTING_STARTED.md → Follow-up Radar). The
attribute specs mirror the CRM-agnostic requirement documented there; the
sole in-code writer is ``workflows.followup_state``.

The optional Email Response Detection feature (``--feature phase06``,
PR-243) provisions the four ``people`` attributes and two terminal
``email_campaign_stage`` options Phase 0.6 needs to flip a prospect who
replied to an outreach email. Also DISABLED by default: the detector
no-ops without a Gmail token, and these writes 400 until provisioned. See
GETTING_STARTED.md → Email Response Detection.

The optional Migration Provenance feature (``--feature provenance``)
creates the ``last_migrated_by`` back-pointer on the record objects
``MigrationRunWriter`` stamps (``people`` and ``companies``). Without it
every ``mark_modified()`` on those objects fails its back-pointer PATCH
and the run prints a back-pointer WARNING while still exiting 0 — alarm
fatigue that trains operators to skim a warning that sometimes means a
real forensics gap. Only needed if you run the ``migrate_``/``backfill_``
scripts; a fresh install that never migrates can skip it.

Usage:
  python3 scripts/setup_attio_schema.py --dry-run
  python3 scripts/setup_attio_schema.py
  python3 scripts/setup_attio_schema.py --feature radar       # add radar attrs
  python3 scripts/setup_attio_schema.py --feature phase06     # add email-detection attrs
  python3 scripts/setup_attio_schema.py --feature provenance  # add last_migrated_by
  python3 scripts/setup_attio_schema.py --feature all         # all of the above
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import click
import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent))

from clients.attio import AttioClient  # noqa: E402
from scripts._attio_migration_helpers import (  # noqa: E402
    ensure_attribute,
    reconcile_select_options,
)
from workflows.quality_gate import VERDICT_PATHS  # noqa: E402

# ── Follow-up Radar attribute specs (PR-211/214/247) ───────────────────────
# (slug, source-type, description). Types are the manifest source-side names
# (`datetime`/`checkbox`/... — ensure_attribute maps to the Attio API type).
# Sole writer of every attribute below: workflows.followup_state. The radar is
# OFF by default; these are only provisioned via `--feature radar`.
#
# The five followup_* state attrs land on BOTH warm object types (the
# LinkedIn Outreach list + the deals object); awaiting_reply_* powers the
# WAITING lane; deals-only extras carry partner attribution + verified touch;
# people-only is_partner is the operator-seeded partner roster flag.
_RADAR_SHARED_ATTRS: list[tuple[str, str, str]] = [
    ("followup_draft_at", "datetime",
     "When a follow-up review-draft was last generated. A newer real "
     "last-touch supersedes it (the account was acted on)."),
    ("followup_draft_id", "text",
     "Draft id of the last follow-up draft (dedup on re-run + digest link)."),
    ("followup_snooze_until", "date",
     "Radar skips this account until this date (inclusive). Operator/skill set."),
    ("followup_muted", "checkbox",
     "Permanently exclude from the radar (operator parks stale/irrelevant rows)."),
    ("followup_callback_date", "date",
     "Deferral tickler — suppress until this date, then hard-surface."),
    ("awaiting_reply_since", "date",
     "Date of the most recent unanswered send to this account (WAITING lane; "
     "re-stamped on newer sends; 60-day TTL read-side)."),
    ("awaiting_reply_thread_id", "text",
     "Advisory email thread id of the unanswered send (skill re-resolves live)."),
    ("awaiting_reply_note_id", "text",
     "Id of the one canonical waiting-context note per record (survives clear)."),
    ("awaiting_reply_nudge_count", "number",
     "Nudge drafts this waiting cycle (hard max 2; at the ceiling the account "
     "renders terminal and never auto-drafts again)."),
]

_RADAR_DEALS_ONLY_ATTRS: list[tuple[str, str, str]] = [
    ("referred_by", "text",
     "Referring partner's canonical lowercase email (deal-side partner "
     "attribution). Stamped by the skill layer when a known partner is found "
     "among the deal's email thread participants."),
    ("last_verified_touch", "date",
     "Skill-layer-verified true last-touch date (Follow-up Radar v2). Highest-"
     "precedence deal recency source for radar ranking."),
]

_RADAR_PEOPLE_ONLY_ATTRS: list[tuple[str, str, str]] = [
    ("is_partner", "checkbox",
     "Partner roster flag. Seeded MANUALLY by the operator via the CRM UI — no "
     "engine code writes it. Read by the skill layer to recognize a known "
     "partner among a deal's email thread participants."),
]


def _provision_radar(client: AttioClient, list_id: str, dry_run: bool) -> None:
    """Idempotently create the Follow-up Radar attributes (PR-211/214/247).

    The five shared state attrs land on the LinkedIn Outreach list AND the
    deals object; deals also gets the partner-attribution + verified-touch
    extras; people gets only the operator-seeded is_partner roster flag.
    """
    # (parent_kind, parent_id, label, attrs)
    plan: list[tuple[str, str, str, list[tuple[str, str, str]]]] = [
        ("list", list_id, "linkedin_outreach", _RADAR_SHARED_ATTRS),
        ("object", "deals", "deals", _RADAR_SHARED_ATTRS + _RADAR_DEALS_ONLY_ATTRS),
        ("object", "people", "people", _RADAR_PEOPLE_ONLY_ATTRS),
    ]
    click.echo("\nFollow-up Radar attributes (PR-211/214/247):")
    created = skipped = failed = 0
    for parent_kind, parent_id, label, attrs in plan:
        for slug, type_, description in attrs:
            target = f"{label}.{slug}"
            try:
                action = ensure_attribute(
                    client, parent_kind, parent_id, slug, type_,  # type: ignore[arg-type]
                    description=description, dry_run=dry_run,
                )
            except Exception as exc:  # noqa: BLE001
                click.echo(f"  ✗ {target}: FAILED — {exc}", err=True)
                failed += 1
                continue
            if action == "created" or action == "would_create":
                tag = "[dry-run] would create" if dry_run else "created"
                click.echo(f"  + {target} ({type_}): {tag}")
                created += 1
            else:
                click.echo(f"  ✓ {target}: exists ({action})")
                skipped += 1
    click.echo(
        f"Radar attributes: created={created}, skipped={skipped}, failed={failed}."
    )
    if failed:
        raise click.ClickException(f"{failed} radar attribute(s) failed to provision.")


# ── Email response detection attribute specs (Phase 0.6, PR-243) ────────────
# (slug, source-type, options, description). All land on the `people` object.
# Sole writer of the first three: workflows.detect_email_responses; the
# email lane (workflows.email_campaign.run_email_daily) writes
# email_last_resend_id. OFF by default — only provisioned via
# `--feature phase06`; the detector no-ops without a Gmail token regardless.
_PHASE06_PEOPLE_ATTRS: list[tuple[str, str, list[str] | None, str]] = [
    ("email_response_classification", "select",
     ["positive", "question", "neutral", "negative", "defensive"],
     "Classifier verdict for a detected inbound email reply (Phase 0.6). "
     "Same taxonomy as the LinkedIn response classification."),
    ("email_response_received_at", "datetime", None,
     "Gmail internalDate of the detected reply. Doubles as the Phase 0.6 "
     "idempotency marker: set => person is never re-scanned."),
    ("last_email_response_text", "long_text", None,
     "Reply body (quoted history stripped, truncated to 1000 chars). Full "
     "text lives in the auto-created CRM note."),
    ("email_last_resend_id", "text", None,
     "Resend message id of the most recent outreach email — enables future "
     "thread-id reply matching. Written by the email lane."),
]

# The two new terminal email_campaign_stage select options Phase 0.6 flips
# to. Reconciled (not created) — the email_campaign_stage attribute must
# already exist (the email lane writes it); RESPONDED/NOT_INTERESTED may
# already be present from earlier enum additions, so this is idempotent.
_PHASE06_STAGE_OPTIONS = ["email_responded", "email_not_interested"]


def _provision_phase06(client: AttioClient, dry_run: bool) -> None:
    """Idempotently provision the email-response-detection schema (PR-243).

    Four new `people` attributes + two new `email_campaign_stage` select
    options. Safe to re-run; skips anything already present.
    """
    click.echo("\nEmail response detection attributes (Phase 0.6, PR-243):")
    created = skipped = failed = 0
    for slug, type_, options, description in _PHASE06_PEOPLE_ATTRS:
        target = f"people.{slug}"
        try:
            action = ensure_attribute(
                client, "object", "people", slug, type_,  # type: ignore[arg-type]
                options=options, description=description, dry_run=dry_run,
            )
        except Exception as exc:  # noqa: BLE001
            click.echo(f"  ✗ {target}: FAILED — {exc}", err=True)
            failed += 1
            continue
        if action in ("created", "would_create"):
            tag = "[dry-run] would create" if dry_run else "created"
            click.echo(f"  + {target} ({type_}): {tag}")
            created += 1
        else:
            click.echo(f"  ✓ {target}: exists ({action})")
            skipped += 1

    # Seed the two terminal reply stages onto the existing
    # email_campaign_stage select (must already exist — the email lane
    # writes it). Idempotent: reconcile only adds missing options.
    click.echo("Terminal email_campaign_stage options:")
    try:
        action = reconcile_select_options(
            client, "object", "people", "email_campaign_stage",
            _PHASE06_STAGE_OPTIONS, dry_run=dry_run,
        )
        click.echo(f"  people.email_campaign_stage options: {action}")
    except httpx.HTTPStatusError as exc:
        click.echo(
            f"  ✗ email_campaign_stage options FAILED — {exc}. The "
            "email_campaign_stage select attribute must exist on `people` "
            "first (the email lane provisions it).",
            err=True,
        )
        failed += 1

    click.echo(
        f"Phase 0.6 attributes: created={created}, skipped={skipped}, "
        f"failed={failed}."
    )
    if failed:
        raise click.ClickException(f"{failed} Phase 0.6 item(s) failed to provision.")


# ── Migration provenance back-pointer (§3.13) ──────────────────────────────
# `MigrationRunWriter._stamp_back_pointers` PATCHes
# `/objects/{object}/records/{record_id}` with `last_migrated_by` -> the
# Migration Run row for every `mark_modified(object=...)` call that is not a
# `schema` / `list_entry` sentinel. Only these two object-level targets are
# reachable that way, so only they need the attribute. Sole writer:
# workflows.migration_run_writer.MigrationRunWriter (registered in
# clients/attio_writer_registry.py and docs/attio_schema_deltas.yaml).
_PROVENANCE_OBJECTS: list[str] = ["people", "companies"]

_PROVENANCE_SLUG = "last_migrated_by"
_PROVENANCE_REFERENCED_OBJECT = "migration_run"
_PROVENANCE_DESCRIPTION = (
    "Provenance pointer to the Migration Run that last modified this record "
    "(plan section 3.13). Written only by MigrationRunWriter."
)


def _provision_provenance(client: AttioClient, dry_run: bool) -> None:
    """Idempotently create `last_migrated_by` on the migration back-pointer
    targets (§3.13).

    Replaces the per-object one-off migrate scripts: `ensure_attribute` skips
    an attribute that already exists, so re-running is free and a workspace
    that already carries one of them is left alone.
    """
    click.echo("\nMigration provenance back-pointers (§3.13):")
    created = skipped = failed = 0
    for object_slug in _PROVENANCE_OBJECTS:
        target = f"{object_slug}.{_PROVENANCE_SLUG}"
        try:
            action = ensure_attribute(
                client, "object", object_slug, _PROVENANCE_SLUG, "record_reference",
                description=_PROVENANCE_DESCRIPTION,
                referenced_object=_PROVENANCE_REFERENCED_OBJECT,
                dry_run=dry_run,
            )
        except Exception as exc:  # noqa: BLE001
            click.echo(f"  ✗ {target}: FAILED — {exc}", err=True)
            failed += 1
            continue
        if action in ("created", "would_create"):
            tag = "[dry-run] would create" if dry_run else "created"
            click.echo(f"  + {target} (record_reference → migration_run): {tag}")
            created += 1
        else:
            click.echo(f"  ✓ {target}: exists ({action})")
            skipped += 1

    click.echo(
        f"Provenance attributes: created={created}, skipped={skipped}, "
        f"failed={failed}."
    )
    if failed:
        raise click.ClickException(
            f"{failed} provenance item(s) failed to provision."
        )


# Spec: each entry is (parent_kind, parent_id_or_slug, attribute body).
# parent_kind is "list" or "object".
def _build_specs(list_id: str) -> list[tuple[str, str, dict]]:
    return [
        # ── LinkedIn Outreach list entry attrs ─────────────────────────
        (
            "list",
            list_id,
            {
                "title": "Score Breakdown",
                "api_slug": "score_breakdown",
                "type": "text",
                "is_required": False,
                "is_unique": False,
                "is_multiselect": False,
                "default_value": None,
                "config": {},
                "description": "JSON-encoded per-component scoring breakdown (size, role, competitor, industry, total + reasons).",
            },
        ),
        (
            "list",
            list_id,
            {
                "title": "Scoring Lane",
                "api_slug": "scoring_lane",
                "type": "select",
                "is_required": False,
                "is_unique": False,
                "is_multiselect": False,
                "default_value": None,
                "config": {},
                "description": "Which scoring lane was used: target_company_mode, enterprise_mode, or legacy.",
            },
        ),
        (
            "list",
            list_id,
            {
                "title": "Verdict Path",
                "api_slug": "verdict_path",
                "type": "select",
                "is_required": False,
                "is_unique": False,
                "is_multiselect": False,
                "default_value": None,
                "config": {},
                "description": "Which scoring branch decided the verdict: target_pass / enterprise_pass / borderline_pass / borderline_reject / deterministic_reject. Distinct from the LLM's icp_lane (which is 1 or 2).",
            },
        ),
        (
            "list",
            list_id,
            {
                "title": "LLM Rationale",
                "api_slug": "llm_rationale",
                "type": "text",
                "is_required": False,
                "is_unique": False,
                "is_multiselect": False,
                "default_value": None,
                "config": {},
                "description": "Haiku borderline qualifier rationale (40-75 score band only).",
            },
        ),
        # ── Companies record attrs ──────────────────────────────────────
        (
            "object",
            "companies",
            {
                "title": "Industry Source",
                "api_slug": "industry_source",
                "type": "select",
                "is_required": False,
                "is_unique": False,
                "is_multiselect": False,
                "default_value": None,
                "config": {},
                "description": "How industry_vertical was assigned: haiku_classifier / pb_scrape / manual.",
            },
        ),
        (
            "object",
            "companies",
            {
                "title": "Industry Classified At",
                "api_slug": "industry_classified_at",
                "type": "date",
                "is_required": False,
                "is_unique": False,
                "is_multiselect": False,
                "default_value": None,
                "config": {},
                "description": "Date industry_vertical was last classified. Lets us re-run the classifier on stale rows.",
            },
        ),
        (
            "object",
            "companies",
            {
                "title": "Industry Vertical Status",
                "api_slug": "industry_vertical_status",
                "type": "select",
                "is_required": False,
                "is_unique": False,
                "is_multiselect": False,
                "default_value": None,
                "config": {},
                "description": "PR-25 confidence status of industry_vertical: confirmed / unknown / low_confidence. Only 'confirmed' unlocks the ops_industrial_joint bonus in the scorer.",
            },
        ),
        (
            "object",
            "companies",
            {
                "title": "Industry Vertical Confidence",
                "api_slug": "industry_vertical_confidence",
                "type": "number",
                "is_required": False,
                "is_unique": False,
                "is_multiselect": False,
                "default_value": None,
                "config": {},
                "description": "Classifier confidence (0.0-1.0) for industry_vertical. Backfilled classifier labels write 0.0 until operator-confirmed.",
            },
        ),
        (
            # PR-240 language guard: the HQ country code is the canonical source
            # the fail-closed language guard (workflows.daily_check.
            # expected_language_for_entry) re-derives the expected DM language
            # from. Created idempotently — skipped if the workspace already
            # carries a native `hq_country_code` attribute.
            "object",
            "companies",
            {
                "title": "HQ Country Code",
                "api_slug": "hq_country_code",
                "type": "text",
                "is_required": False,
                "is_unique": False,
                "is_multiselect": False,
                "default_value": None,
                "config": {},
                "description": "ISO country code of the company HQ. Seeds the DM language (PR-240 fail-closed language guard re-derives expected language from it).",
            },
        ),
    ]


# Select options to seed after attribute creation.
# verdict_path options come from the canonical VERDICT_PATHS registry in
# quality_gate so newly added scorer branches (e.g. the disqualifier_* slugs)
# are seeded automatically on the next run instead of drifting. (PR-225)
SELECT_OPTIONS: dict[tuple[str, str], list[str]] = {
    ("list", "scoring_lane"): ["target_company_mode", "enterprise_mode", "legacy"],
    ("list", "verdict_path"): sorted(VERDICT_PATHS),
    ("object", "industry_source"): ["haiku_classifier", "claude_session", "pb_scrape", "manual"],
    ("object", "industry_vertical_status"): ["confirmed", "unknown", "low_confidence"],
}


def _list_existing_slugs(client: AttioClient, parent_kind: str, parent_id: str) -> set[str]:
    base = "/lists" if parent_kind == "list" else "/objects"
    resp = client._request("GET", f"{base}/{parent_id}/attributes", params={"limit": 100})
    return {a.get("api_slug", "") for a in resp.get("data", [])}


def _existing_options(client: AttioClient, parent_kind: str, parent_id: str, slug: str) -> set[str]:
    base = "/lists" if parent_kind == "list" else "/objects"
    try:
        resp = client._request("GET", f"{base}/{parent_id}/attributes/{slug}/options")
    except httpx.HTTPStatusError:
        return set()
    return {o.get("title", "") for o in resp.get("data", [])}


def _create_attribute(
    client: AttioClient, parent_kind: str, parent_id: str, body: dict, dry_run: bool
) -> dict:
    base = "/lists" if parent_kind == "list" else "/objects"
    if dry_run:
        click.echo(f"  [dry-run] POST {base}/{parent_id}/attributes — {body['api_slug']}")
        return {}
    return client._request(
        "POST",
        f"{base}/{parent_id}/attributes",
        json={"data": body},
    )


def _create_option(
    client: AttioClient, parent_kind: str, parent_id: str, slug: str, title: str, dry_run: bool
) -> dict:
    base = "/lists" if parent_kind == "list" else "/objects"
    if dry_run:
        click.echo(f"  [dry-run] POST {base}/{parent_id}/attributes/{slug}/options — '{title}'")
        return {}
    return client._request(
        "POST",
        f"{base}/{parent_id}/attributes/{slug}/options",
        json={"data": {"title": title}},
    )


@click.command()
@click.option("--dry-run", is_flag=True, help="List actions without writing.")
@click.option(
    "--feature",
    type=click.Choice(["core", "radar", "phase06", "provenance", "all"]),
    default="core",
    show_default=True,
    help=(
        "Which attribute set to provision. 'core' (default) = scorer + "
        "persistence attrs only. 'radar' = Follow-up Radar attrs only. "
        "'phase06' = email response detection attrs only (PR-243). "
        "'provenance' = the §3.13 last_migrated_by back-pointers only. "
        "'all' = all of them. Radar, phase06 and provenance are off by "
        "default; provision them only when you want those features (see "
        "GETTING_STARTED.md)."
    ),
)
def main(dry_run: bool, feature: str) -> int:
    list_id = os.environ["ATTIO_LIST_ID"]
    specs = _build_specs(list_id) if feature in ("core", "all") else []

    with AttioClient() as client:
        if feature == "radar":
            _provision_radar(client, list_id, dry_run)
            return 0
        if feature == "phase06":
            _provision_phase06(client, dry_run)
            return 0
        if feature == "provenance":
            _provision_provenance(client, dry_run)
            return 0
        # Cache existing slugs per parent so we make one GET per parent
        cache: dict[tuple[str, str], set[str]] = {}

        click.echo(f"Configuring schema (dry_run={dry_run}):\n")
        created_attrs = 0
        skipped_attrs = 0
        for parent_kind, parent_id, body in specs:
            key = (parent_kind, parent_id)
            if key not in cache:
                cache[key] = _list_existing_slugs(client, parent_kind, parent_id)
            existing = cache[key]
            slug = body["api_slug"]

            if slug in existing:
                click.echo(f"✓ {parent_kind}:{parent_id} — '{slug}' exists, skipping")
                skipped_attrs += 1
            else:
                click.echo(f"+ {parent_kind}:{parent_id} — creating '{slug}' ({body['type']})")
                _create_attribute(client, parent_kind, parent_id, body, dry_run)
                if not dry_run:
                    cache[key].add(slug)
                created_attrs += 1

        # Seed select options
        click.echo("\nSeeding select options:")
        created_opts = 0
        skipped_opts = 0
        for (parent_kind, slug), titles in SELECT_OPTIONS.items():
            parent_id = list_id if parent_kind == "list" else "companies"
            if dry_run:
                existing_titles: set[str] = set()
            else:
                existing_titles = _existing_options(client, parent_kind, parent_id, slug)
            for title in titles:
                if title in existing_titles:
                    click.echo(f"  ✓ {parent_kind}:{slug} — option '{title}' exists, skipping")
                    skipped_opts += 1
                else:
                    click.echo(f"  + {parent_kind}:{slug} — adding option '{title}'")
                    _create_option(client, parent_kind, parent_id, slug, title, dry_run)
                    created_opts += 1

        click.echo(
            f"\nDone. Attributes: created={created_attrs}, skipped={skipped_attrs}. "
            f"Options: created={created_opts}, skipped={skipped_opts}."
        )

        if feature == "all":
            _provision_radar(client, list_id, dry_run)
            _provision_phase06(client, dry_run)
            _provision_provenance(client, dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
