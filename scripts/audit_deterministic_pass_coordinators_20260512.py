"""Audit: pipeline entries that deterministic-passed but are Junior/IC roles.

Pre-2026-05-12, the scorer treated "coordinator" / "coordinador" / "analyst"
etc. as `is_influencer` and scored them +18 or +24. At an in-ICP enterprise
(size 28 + role 24 + competitor 20 + industry 12 = 84) this cleared the
`>75` deterministic-pass threshold, bypassing the LLM gate — even though
the qualifier prompt explicitly rejects Junior/IC titles.

This script surfaces the existing pipeline entries that fell through that
gap. Read-only — outputs CSV at exports/deterministic_pass_coordinators_<date>.csv.
The companion repair script flips them to Not Interested with an audit note.

Run:
  python3 scripts/audit_deterministic_pass_coordinators_20260512.py
"""
from __future__ import annotations

import csv
import os
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).parent.parent / ".env")

import click  # noqa: E402

from clients.attio import AttioClient  # noqa: E402
from workflows.daily_check import RecordCache  # noqa: E402
from workflows.quality_gate import (  # noqa: E402
    JUNIOR_IC_EXEMPTIONS,
    JUNIOR_IC_KEYWORDS,
    _match_any,
    _match_any_word,
)

# Verdict paths that indicate the deterministic gate let the prospect through
# without the LLM gate firing. borderline_pass goes through LLM, so don't flag.
DETERMINISTIC_PASS_PATHS = {"target_pass", "enterprise_pass"}

OUTPUT_PATH = (
    Path(__file__).parent.parent
    / "exports"
    / f"deterministic_pass_coordinators_{date.today().isoformat()}.csv"
)

# Stages where it still matters operationally — terminal stages
# (Responded / Not Interested / Qualified / Call Booked) have already had
# operator review, so don't second-guess.
ACTIONABLE_STAGES = {
    "Prospect", "Partner Intro", "Connection Sent", "Accepted",
    "DM1 Sent", "DM2 Sent", "DM3 Sent",
}


@click.command()
def main() -> None:
    list_id = os.environ.get("ATTIO_LIST_ID", "")
    if not list_id:
        click.echo("ERROR: ATTIO_LIST_ID not set", err=True)
        sys.exit(1)

    click.echo("=== Audit: deterministic-pass Junior/IC coordinators ===\n")

    with AttioClient() as attio:
        entries = attio.query_list_entries(list_id=list_id)
        click.echo(f"Loaded {len(entries)} pipeline entries.")
        cache = RecordCache(attio)

        # Identify candidates: deterministic-pass entries whose person title
        # matches the new JUNIOR_IC_KEYWORDS and is NOT exempted.
        candidates: list[dict] = []
        no_title_skipped = 0
        no_record_skipped = 0
        for raw in entries:
            attrs = AttioClient.parse_entry(raw)
            if attrs.get("verdict_path") not in DETERMINISTIC_PASS_PATHS:
                continue
            if attrs.get("stage") not in ACTIONABLE_STAGES:
                continue

            record_id = attrs.get("record_id")
            if not record_id:
                continue

            # Resolve the person's job_title from the linked record.
            record = attio.get_person(record_id)
            if not record:
                no_record_skipped += 1
                continue
            values = record.get("values", {})
            title_list = values.get("job_title", [])
            title = ""
            if title_list and isinstance(title_list[0], dict):
                title = title_list[0].get("value", "") or ""
            if not title:
                no_title_skipped += 1
                continue
            title_lower = title.lower()

            if not _match_any_word(title_lower, JUNIOR_IC_KEYWORDS):
                continue
            if _match_any(title_lower, JUNIOR_IC_EXEMPTIONS):
                continue

            name, company, _, _ = cache.get(record_id)
            candidates.append({
                "name": name,
                "company": company,
                "title": title,
                "stage": attrs.get("stage", ""),
                "dm_step": attrs.get("dm_step") or 0,
                "last_contact_date": attrs.get("last_contact_date", ""),
                "score": attrs.get("quality_score", ""),
                "scoring_lane": attrs.get("scoring_lane", ""),
                "verdict_path": attrs.get("verdict_path", ""),
                "record_id": record_id,
                "entry_id": attrs.get("entry_id", ""),
            })

    click.echo(f"\nFound {len(candidates)} deterministic-pass IC entries in actionable stages.")
    click.echo(
        f"  (Skipped {no_title_skipped} deterministic-pass entries in actionable stages "
        f"that had no job_title in Attio — these may also be Junior/IC but the audit "
        f"can't tell. Investigate manually if non-zero.)"
    )
    if no_record_skipped:
        click.echo(
            f"  (Skipped {no_record_skipped} entries because get_person returned no record.)"
        )
    click.echo()

    if not candidates:
        click.echo("Nothing to audit. Pipeline is clean on this dimension.")
        return

    # Title histogram for quick review
    title_counts = Counter(c["title"] for c in candidates)
    click.echo("--- Title histogram (top 20) ---")
    for t, n in title_counts.most_common(20):
        click.echo(f"  {n:3d}  {t}")
    click.echo()

    # Stage breakdown
    stage_counts = Counter(c["stage"] for c in candidates)
    click.echo("--- Stage breakdown ---")
    for s, n in stage_counts.most_common():
        click.echo(f"  {n:3d}  {s}")
    click.echo()

    # Write CSV for the repair script
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(candidates[0].keys())
    with OUTPUT_PATH.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(candidates)
    click.echo(f"Wrote {OUTPUT_PATH}")
    click.echo()
    click.echo("Next: review the CSV, then run")
    click.echo("  python3 scripts/repair_deterministic_pass_coordinators_20260512.py --apply")


if __name__ == "__main__":
    main()
