#!/usr/bin/env python3
"""PR-9a schema migration: add per-step send timestamps + canonical URL attrs.

Creates six new attributes on the linkedin_outreach list object:

  - dm1_sent_at       (datetime)  — per §3.15 backfill exception
  - dm2_sent_at       (datetime)  — per §3.15 backfill exception
  - dm3_sent_at       (datetime)  — per §3.15 backfill exception
  - response_received_at (datetime) — sole writer: detect_responses
  - canonical_linkedin_url (text) — multi-writer: weekly_prospect + PR-9a backfill
  - vanity_url_slug   (text)      — multi-writer: weekly_prospect + PR-14 backfill

This script is SCHEMA-ONLY — it writes zero data to any list entry.
The idempotency contract: re-run finds all 6 attrs already exist →
rows_modified=0, rows_skipped_idempotent=6, rows_failed=0.

Caller: run from repo root:
    python3 scripts/migrate_dmN_sent_at_schema.py [--dry-run]

Separate from backfill_canonical_linkedin_url.py (the PR-9a B2-fix
value backfill that runs after this schema step completes).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from clients.attio import AttioClient  # noqa: E402
from workflows.migration_run_writer import MigrationRunWriter  # noqa: E402

SCRIPT_VERSION = "pr-9a-v1"

# The Attio object that holds LinkedIn Outreach list entry attributes.
# In Attio's API, list-entry custom attributes live on the list object.
LINKEDIN_OUTREACH_LIST_ID_ENV = "ATTIO_LIST_ID"

# Six attributes to create — (slug, type, description).
NEW_ATTRS: list[tuple[str, str, str]] = [
    (
        "dm1_sent_at",
        "datetime",
        "Timestamp of DM 1 send. Written by run_dm_sequencing at send time; "
        "backfilled from PB history by PR-9b. NULL = not yet sent or "
        "send-time unknown (§0 #9: explicit None, not silent fallback).",
    ),
    (
        "dm2_sent_at",
        "datetime",
        "Timestamp of DM 2 send. See dm1_sent_at note.",
    ),
    (
        "dm3_sent_at",
        "datetime",
        "Timestamp of DM 3 send. See dm1_sent_at note.",
    ),
    (
        "response_received_at",
        "datetime",
        "Timestamp of first inbound reply classified by detect_responses. "
        "Sole writer: workflows.detect_responses.classify_reply.",
    ),
    (
        "canonical_linkedin_url",
        "text",
        "Canonical LinkedIn profile URL: URL-decoded, lowercase, no www., "
        "no trailing slash. Computed from the parent Person's linkedin field. "
        "PR-9.5 dedup-join depends on this being populated before it runs.",
    ),
    (
        "vanity_url_slug",
        "text",
        "The path slug from the canonical URL, e.g. 'mateo-lt-12345' from "
        "https://linkedin.com/in/mateo-lt-12345. Used by PR-14 vanity-URL "
        "prospect lookups.",
    ),
]


def _ensure_attribute(
    attio: AttioClient,
    list_id: str,
    slug: str,
    type_: str,
    dry_run: bool,
) -> str:
    """Create the attribute if it doesn't already exist.

    Returns one of: 'created' | 'skipped' | 'would_create'.

    Raises on type mismatch (existing attr has different type) — this is
    a hard error requiring manual Attio UI intervention.
    """
    try:
        data = attio._request("GET", f"/lists/{list_id}/attributes/{slug}")
        existing_type = data.get("data", {}).get("type")
        if existing_type and existing_type != type_:
            raise RuntimeError(
                f"linkedin_outreach.{slug} already exists with type "
                f"{existing_type!r} but migration expects {type_!r}. "
                "Manual Attio UI fix required — type migrations are "
                "irreversible via API."
            )
        return "skipped"
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise

    if dry_run:
        return "would_create"

    body: dict = {
        "data": {
            "api_slug": slug,
            "title": slug.replace("_", " ").title(),
            "type": type_,
            "is_required": False,
        }
    }
    attio._request("POST", f"/lists/{list_id}/attributes", json=body)
    return "created"


def _get_list_id(attio: AttioClient) -> str:
    """Return the LinkedIn Outreach list ID from the environment or discovery."""
    list_id = os.environ.get(LINKEDIN_OUTREACH_LIST_ID_ENV, "").strip()
    if list_id:
        return list_id
    # Attempt auto-discovery: list all lists, find the one with slug
    # 'linkedin_outreach' or title matching.
    discovery_error: str = ""
    try:
        data = attio._request("GET", "/lists")
        lists = data.get("data", [])
        for lst in lists:
            slug = lst.get("api_slug", "")
            name = lst.get("name", "")
            if slug == "linkedin_outreach" or "outreach" in name.lower():
                lid = lst.get("id", {}).get("list_id", "")
                if lid:
                    return lid
    except Exception as exc:
        # Surface the real error (e.g., 401 auth failure) in the message
        # so operators know to fix their API key, not just the env var.
        discovery_error = f" (auto-discovery failed: {exc})"
    raise RuntimeError(
        f"Could not determine LinkedIn Outreach list ID.{discovery_error} "
        f"Set {LINKEDIN_OUTREACH_LIST_ID_ENV} env var to the list's UUID."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        attio = AttioClient()
    except KeyError:
        print("error: ATTIO_API_KEY env var not set", file=sys.stderr)
        return 2

    try:
        list_id = _get_list_id(attio)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(
            f"[dry-run] Would create/verify {len(NEW_ATTRS)} attrs on "
            f"linkedin_outreach list (list_id={list_id})."
        )

    with MigrationRunWriter(
        script_name=Path(__file__).name,
        script_version=SCRIPT_VERSION,
        rollback_script_path=None,  # attribute creation is irreversible via API
        dry_run=args.dry_run,
        attio=attio,
    ) as run:
        for slug, type_, _ in NEW_ATTRS:
            run.examine()
            try:
                action = _ensure_attribute(attio, list_id, slug, type_, args.dry_run)
                if action == "skipped":
                    run.skip_idempotent()
                    if args.dry_run:
                        print(f"  [dry-run] {slug}: already exists, no-op")
                    else:
                        print(f"  {slug}: already exists (idempotent)")
                else:
                    # schema-level change: no target record to stamp
                    run.mark_modified(
                        record_id=f"linkedin_outreach.{slug}", object="schema"
                    )
                    tag = "[dry-run] would create" if args.dry_run else "created"
                    print(f"  {slug} ({type_}): {tag}")
            except Exception as exc:
                run.mark_failed(record_id=f"linkedin_outreach.{slug}", error=exc)
                print(
                    f"  {slug}: FAILED — {exc}",
                    file=sys.stderr,
                )

    print(
        f"\nPR-9a schema migration complete "
        f"({'dry-run' if args.dry_run else 'live'}): "
        f"examined={run.rows_examined}, "
        f"created={run.rows_modified}, "
        f"already_existed={run.rows_skipped_idempotent}, "
        f"failed={run.rows_failed}"
    )
    return 1 if run.rows_failed > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
