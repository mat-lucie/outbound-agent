"""One-shot rescore of every PROSPECT-stage entry with the industry-aware scorer.

Pulls all PROSPECT entries, fetches their parent person + company records to
gather the inputs the scorer needs (title, company, location, employee_count,
industry), recomputes the score with the industry-aware code, and either prints
a delta histogram (--dry-run) or writes the new scores + breakdown back to Attio.

Usage:
  python3 scripts/rescore_prospect_cohort.py --dry-run
  python3 scripts/rescore_prospect_cohort.py
  python3 scripts/rescore_prospect_cohort.py --persona-config-file content/personas.json
"""
from __future__ import annotations

import json
import logging
import os
import sys
from collections import Counter
from pathlib import Path

import click
import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent))

from clients.attio import AttioClient  # noqa: E402
from models.campaign import load_personas  # noqa: E402
from workflows.migration_run_writer import MigrationRunWriter  # noqa: E402
from workflows.quality_gate import score_prospect  # noqa: E402

logger = logging.getLogger(__name__)


def _resolve_persona_config(persona_key: str | None, personas_data: dict) -> dict | None:
    """Pick a persona_config dict matching what weekly_prospect would have used.

    personas.json is keyed by persona name at the top level (e.g.
    `personas["mx_midmarket_manufacturing"]`), not nested under a `personas`
    array. The returned dict needs a `key` field — synthesize it from the
    persona name so target_company_mode/enterprise_mode can be detected.
    """
    if not persona_key:
        return None
    cfg = personas_data.get(persona_key)
    if not cfg:
        return None
    # Inject the key so score_prospect can read persona_config["key"] for the
    # persona-scoped harvest path (preserves the Attio persona instead of
    # re-classifying by title).
    return {**cfg, "key": persona_key}


def _fetch_company(client: AttioClient, company_record_id: str) -> dict:
    cached = client._industry_cache.get(company_record_id)
    if cached is not None:
        return {"industry": cached}
    try:
        resp = client._request("GET", f"/objects/companies/records/{company_record_id}")
        cr = resp.get("data", resp)
        values = cr.get("values", {})
        iv = values.get("industry_vertical", [])
        industry = ""
        if iv and isinstance(iv[0], dict):
            industry = iv[0].get("option", {}).get("title", "") or ""
        client._industry_cache[company_record_id] = industry
        return {"industry": industry}
    except httpx.HTTPStatusError as err:
        # Auth failures cascade — every subsequent call will also fail.
        # Re-raise so the run aborts with a clear signal instead of silently
        # rescoring the whole cohort with industry=missing.
        if err.response.status_code in (401, 403):
            raise
        logger.warning(
            "_fetch_company: HTTP %s for %s — treating industry as unknown",
            err.response.status_code, company_record_id,
        )
        return {"industry": ""}
    except (httpx.ConnectError, httpx.TimeoutException) as err:
        logger.warning("_fetch_company: network error for %s: %s", company_record_id, err)
        return {"industry": ""}


def _fetch_person(client: AttioClient, record_id: str) -> dict:
    try:
        resp = client._request("GET", f"/objects/people/records/{record_id}")
        person = resp.get("data", resp)
    except httpx.HTTPStatusError as err:
        if err.response.status_code in (401, 403):
            raise  # auth failure — abort the whole run
        logger.warning("_fetch_person: HTTP %s for %s", err.response.status_code, record_id)
        return {}
    except (httpx.ConnectError, httpx.TimeoutException) as err:
        logger.warning("_fetch_person: network error for %s: %s", record_id, err)
        return {}
    values = person.get("values", {})

    name_data = values.get("name", [])
    name = ""
    if name_data:
        first = name_data[0].get("first_name", "")
        last = name_data[0].get("last_name", "")
        name = f"{first} {last}".strip()

    title_data = values.get("job_title", [])
    title = title_data[0].get("value", "") if title_data and isinstance(title_data[0], dict) else ""

    location_data = values.get("primary_location", [])
    location = ""
    if location_data and isinstance(location_data[0], dict):
        loc = location_data[0]
        parts = [loc.get("locality", ""), loc.get("country_code", "")]
        location = ", ".join([p for p in parts if p])

    company_record_id = ""
    company_data = values.get("company") or values.get("primary_company") or []
    if company_data and isinstance(company_data, list):
        ref = company_data[0]
        if isinstance(ref, dict) and ref.get("target_record_id"):
            company_record_id = ref["target_record_id"]

    return {
        "name": name,
        "title": title,
        "location": location,
        "company_record_id": company_record_id,
    }


@click.command()
@click.option("--dry-run", is_flag=True, help="Print delta histogram without writing.")
@click.option("--limit", type=int, default=None, help="Process at most N PROSPECT entries (for testing).")
@click.option(
    "--default-persona-key",
    default="mx_midmarket_manufacturing",
    help="Persona config key to fall back to when an entry has no Attio persona. "
    "Per-entry persona is read from the entry's persona attribute first.",
)
def main(dry_run: bool, limit: int | None, default_persona_key: str) -> int:
    list_id = os.environ["ATTIO_LIST_ID"]
    personas_data = load_personas()
    default_persona_config = _resolve_persona_config(default_persona_key, personas_data)
    if default_persona_config is None:
        click.echo(
            f"WARNING: default persona key {default_persona_key!r} not in personas.json — "
            "entries without an Attio persona will fall back to legacy scoring",
            err=True,
        )

    deltas: list[int] = []
    new_scores: list[int] = []
    old_scores: list[int] = []
    by_verdict: Counter = Counter()
    write_errors = 0
    written = 0
    skipped_missing_data = 0

    with AttioClient() as client, MigrationRunWriter(
        script_name="scripts/rescore_prospect_cohort.py",
        rollback_script_path=None,
        dry_run=dry_run,
        attio=client,
    ) as run:
        click.echo("Fetching PROSPECT-stage entries…")
        all_entries = client.query_list_entries(list_id=list_id)
        prospect_entries = []
        for raw_entry in all_entries:
            parsed = client.parse_entry(raw_entry)
            if parsed["stage"] == "Prospect":
                prospect_entries.append(parsed)
        if limit:
            prospect_entries = prospect_entries[:limit]
        click.echo(f"  {len(prospect_entries)} PROSPECT entries to rescore")

        for i, entry in enumerate(prospect_entries, 1):
            run.examine()
            person = _fetch_person(client, entry["record_id"])
            if not person.get("name") or not person.get("title"):
                skipped_missing_data += 1
                continue

            industry = ""
            if person.get("company_record_id"):
                company_info = _fetch_company(client, person["company_record_id"])
                industry = company_info.get("industry", "")

            prospect_data = {
                "name": person["name"],
                "title": person["title"],
                "company": "",  # not needed for scoring
                "location": person["location"],
                "employee_count": None,  # not stored on the person record post-create
                "industry": industry or None,
            }

            # Use the entry's stored persona to pick the right scoring lane —
            # falling back to the default if the entry's persona is missing or
            # not a known config key.
            entry_persona = entry.get("persona") or default_persona_key
            persona_config = _resolve_persona_config(entry_persona, personas_data) or default_persona_config
            new_result = score_prospect(prospect_data, persona_config=persona_config)
            old_score = entry["quality_score"] or 0
            new_score = new_result["score"]
            delta = new_score - old_score

            old_scores.append(old_score)
            new_scores.append(new_score)
            deltas.append(delta)
            by_verdict[new_result.get("verdict_path") or "unset"] += 1

            if dry_run:
                if i <= 10 or i % 50 == 0:
                    click.echo(
                        f"  [{i}/{len(prospect_entries)}] {person['name'][:30]:30s} "
                        f"old={old_score:3d} new={new_score:3d} Δ={delta:+d} ({industry or 'unknown'})"
                    )
                continue

            try:
                client.update_list_entry(
                    entry_id=entry["entry_id"],
                    entry_attributes={
                        "quality_score": new_score,
                        "score_breakdown": json.dumps(new_result["score_breakdown"]),
                        "scoring_lane": new_result["scoring_lane"],
                        "verdict_path": new_result.get("verdict_path") or "",
                        "llm_rationale": new_result.get("llm_rationale") or "",
                    },
                    list_id=list_id,
                )
                # Wave-2-C fix-up (multi-agent C1): list_entry sentinel.
                run.mark_modified(record_id=entry["entry_id"], object="list_entry")
                written += 1
                if i % 25 == 0:
                    click.echo(f"  Wrote {i}/{len(prospect_entries)}…")
            except Exception as err:  # noqa: BLE001
                write_errors += 1
                run.mark_failed(record_id=entry["entry_id"], error=err)
                click.echo(f"  ERROR on {entry['entry_id']}: {err}", err=True)

    click.echo("\n=== Rescore Summary ===")
    click.echo(f"Processed:           {len(deltas)}")
    click.echo(f"Skipped (no data):   {skipped_missing_data}")
    if not dry_run:
        click.echo(f"Written:             {written}")
        click.echo(f"Write errors:        {write_errors}")

    if deltas:
        click.echo("\nScore delta histogram (new - old):")
        bins = [(-100, -20), (-19, -10), (-9, -1), (0, 0), (1, 9), (10, 19), (20, 100)]
        for lo, hi in bins:
            count = sum(1 for d in deltas if lo <= d <= hi)
            label = f"{lo:+d}..{hi:+d}" if lo != hi else f"  {lo:+d}"
            bar = "█" * int(count / max(deltas.__len__(), 1) * 40)
            click.echo(f"  {label:>12}  {count:4d}  {bar}")

        click.echo("\nNew score distribution:")
        bands = [(0, 39), (40, 59), (60, 69), (70, 74), (75, 89), (90, 100)]
        for lo, hi in bands:
            count = sum(1 for s in new_scores if lo <= s <= hi)
            click.echo(f"  {lo:3d}..{hi:3d}  {count:4d}")

        click.echo("\nVerdict path distribution:")
        for path, count in by_verdict.most_common():
            click.echo(f"  {path:25s}  {count:4d}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
