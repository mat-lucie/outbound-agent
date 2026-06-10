"""Idempotent migration: create the 4 Phase 1 auto-research attributes on the
pipeline list (ATTIO_LIST_ID).

Phase 1 of the ICP auto-research loop persists 4 audit fields to Attio so the
weekly diagnostic can stratify on them natively instead of parsing JSON-in-text:

- icp_lane_persisted (number) — Haiku gate verdict for borderline scores (1 or 2)
- defensive_score (number) — LLM defensive-likelihood score on detected replies
- engagement_score (number) — LLM engagement-likelihood score on detected replies
- quality_score_band (select) — discrete band derived from quality_score

Idempotent: safe to re-run. Existing attributes are detected and skipped.
On 401/403 the script exits non-zero with manual UI instructions so the
operator can fall back to creating the attributes by hand. The downstream
write paths in workflows/weekly_prospect.py and workflows/detect_responses.py
already retry-without-them on 400, so the live pipeline keeps running even
if this migration fails.
"""

from __future__ import annotations

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()


ATTIO_BASE_URL = "https://api.attio.com/v2"


# Attribute specs. `options` is set only for select-type attributes.
SCHEMA_SPECS: list[dict] = [
    {
        "title": "ICP Lane Persisted",
        "api_slug": "icp_lane_persisted",
        "type": "number",
        "description": (
            "Haiku qualifier verdict for borderline scores (40-75). "
            "1 = ICP 1 enterprise, 2 = ICP 2 mid-market. "
            "Empty when verdict was deterministic. Phase 1 auto-research."
        ),
    },
    {
        "title": "Defensive Score",
        "api_slug": "defensive_score",
        "type": "number",
        "description": (
            "LLM-classified defensive likelihood [0,1] on detected reply. "
            "Phase 1 auto-research."
        ),
    },
    {
        "title": "Engagement Score",
        "api_slug": "engagement_score",
        "type": "number",
        "description": (
            "LLM-classified engagement likelihood [0,1] on detected reply. "
            "Phase 1 auto-research."
        ),
    },
    {
        "title": "Quality Score Band",
        "api_slug": "quality_score_band",
        "type": "select",
        "description": (
            "Discrete band derived from quality_score for stratification. "
            "Tracks the score_prospect verdict path. Phase 1 auto-research."
        ),
        "options": ["<40", "40-59", "60-75", ">75"],
    },
]


def _client(api_key: str) -> httpx.Client:
    return httpx.Client(
        base_url=ATTIO_BASE_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        timeout=30.0,
    )


def _print_manual_instructions() -> None:
    """Print Attio UI steps when API auth fails."""
    print("\n=== Manual fallback ===", file=sys.stderr)
    print(
        "The API token cannot create list attributes. Add these 4 attributes "
        "to the pipeline list via Attio UI:",
        file=sys.stderr,
    )
    for spec in SCHEMA_SPECS:
        kind = spec["type"]
        line = f"  • {spec['api_slug']} ({kind})"
        if spec.get("options"):
            line += f" — options: {', '.join(spec['options'])}"
        print(line, file=sys.stderr)
    print(
        "\nWriters in weekly_prospect.py and detect_responses.py retry "
        "without these attrs on a 400, so the pipeline keeps running. "
        "Once you add the attributes via UI, the next runs will populate "
        "them automatically.\n",
        file=sys.stderr,
    )


def _get_existing_slugs(client: httpx.Client, list_id: str) -> set[str]:
    """Return the set of api_slugs already defined on the list."""
    resp = client.get(f"/lists/{list_id}/attributes?limit=200")
    resp.raise_for_status()
    payload = resp.json()
    slugs: set[str] = set()
    for attr in payload.get("data", []):
        slug = attr.get("api_slug") or attr.get("slug")
        if slug:
            slugs.add(slug)
    return slugs


def _create_attribute(client: httpx.Client, list_id: str, spec: dict) -> None:
    """POST a single attribute to the list. Raises on non-2xx."""
    body = {
        "data": {
            "title": spec["title"],
            "api_slug": spec["api_slug"],
            "type": spec["type"],
            "description": spec.get("description", ""),
            "is_required": False,
            "is_unique": False,
            "is_multiselect": False,
            "default_value": None,
            "config": {},
        }
    }
    resp = client.post(f"/lists/{list_id}/attributes", json=body)
    resp.raise_for_status()


def _create_select_options(
    client: httpx.Client, list_id: str, slug: str, options: list[str]
) -> None:
    """POST each option for a select-type attribute. Idempotent per option."""
    # Fetch existing options first to skip-if-already-present.
    existing_titles: set[str] = set()
    try:
        resp = client.get(f"/lists/{list_id}/attributes/{slug}/options?limit=100")
        resp.raise_for_status()
        for opt in resp.json().get("data", []):
            title = opt.get("title")
            if title:
                existing_titles.add(title)
    except httpx.HTTPStatusError:
        pass  # If options listing fails, fall through and try to POST each.

    for opt_title in options:
        if opt_title in existing_titles:
            continue
        body = {"data": {"title": opt_title}}
        resp = client.post(
            f"/lists/{list_id}/attributes/{slug}/options", json=body
        )
        if resp.status_code in (400, 409):
            # Already exists or rejected for benign reason — skip
            continue
        resp.raise_for_status()


def main() -> int:
    api_key = os.environ.get("ATTIO_API_KEY")
    list_id = os.environ.get("ATTIO_LIST_ID")
    if not api_key:
        print("ERROR: ATTIO_API_KEY not set in env", file=sys.stderr)
        return 2
    if not list_id:
        print("ERROR: ATTIO_LIST_ID not set in env", file=sys.stderr)
        return 2

    print(f"Phase 1 schema migration against list {list_id}...")
    with _client(api_key) as client:
        try:
            existing = _get_existing_slugs(client, list_id)
        except httpx.HTTPStatusError as he:
            print(
                f"ERROR: failed to list attributes ({he.response.status_code}): "
                f"{he.response.text[:200]}",
                file=sys.stderr,
            )
            if he.response.status_code in (401, 403):
                _print_manual_instructions()
            return 1

        created = 0
        skipped = 0
        for spec in SCHEMA_SPECS:
            slug = spec["api_slug"]
            if slug in existing:
                print(f"  · {slug}: already exists, skipping")
                skipped += 1
            else:
                try:
                    _create_attribute(client, list_id, spec)
                except httpx.HTTPStatusError as he:
                    print(
                        f"  ✗ {slug}: failed ({he.response.status_code}): "
                        f"{he.response.text}",
                        file=sys.stderr,
                    )
                    if he.response.status_code in (401, 403):
                        _print_manual_instructions()
                        return 1
                    return 1
                print(f"  ✓ {slug}: created ({spec['type']})")
                created += 1

            if spec.get("options"):
                try:
                    _create_select_options(client, list_id, slug, spec["options"])
                except httpx.HTTPStatusError as he:
                    print(
                        f"    ✗ {slug} options: failed ({he.response.status_code}): "
                        f"{he.response.text[:200]}",
                        file=sys.stderr,
                    )
                    return 1

        print(f"\nDone. Created {created}, skipped {skipped}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
