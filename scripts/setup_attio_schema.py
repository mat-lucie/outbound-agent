"""Idempotent setup of Attio attributes needed by the industry-aware scorer
and persistence work.

Creates 4 list-entry attributes on the LinkedIn Outreach list and 2 record
attributes on the Companies object, skipping any that already exist (matched
by api_slug). Safe to re-run.

Usage:
  python3 scripts/setup_attio_schema.py --dry-run
  python3 scripts/setup_attio_schema.py
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
    ]


# Select options to seed after attribute creation.
SELECT_OPTIONS: dict[tuple[str, str], list[str]] = {
    ("list", "scoring_lane"): ["target_company_mode", "enterprise_mode", "legacy"],
    ("list", "verdict_path"): [
        "target_pass",
        "enterprise_pass",
        "borderline_pass",
        "borderline_reject",
        "borderline_llm_error",
        "deterministic_reject",
    ],
    ("object", "industry_source"): ["haiku_classifier", "claude_session", "pb_scrape", "manual"],
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
def main(dry_run: bool) -> int:
    list_id = os.environ["ATTIO_LIST_ID"]
    specs = _build_specs(list_id)

    with AttioClient() as client:
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
