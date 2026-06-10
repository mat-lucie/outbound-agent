"""Render a single conflict group from a dedup report, non-interactive.

Usage:
  python3 scripts/triage_render_one.py <report.json> <group_index>

Prints the same side-by-side view as triage_dedup.py for the given group,
without prompting. Intended for assisted workflows where an agent pastes
the view back to the human and persists the human's decision separately.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent))

from clients.attio import AttioClient  # noqa: E402
from scripts.triage_dedup import (  # noqa: E402
    fetch_list_entries_by_record,
    record_facts,
    render_group,
)


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    report_path = Path(sys.argv[1])
    idx = int(sys.argv[2])

    data = json.loads(report_path.read_text())
    people = data.get("people", {})
    conflict = people.get("conflict_groups", [])
    if not 0 <= idx < len(conflict):
        print(f"Index {idx} out of range (0..{len(conflict)-1})", file=sys.stderr)
        return 2
    group = conflict[idx]

    approved_keys = {g["linkedin_url_normalized"] for g in people.get("approved_conflict_groups", [])}
    skipped_keys = {g["linkedin_url_normalized"] for g in people.get("skipped_groups", [])}
    status = (
        "APPROVED" if group["linkedin_url_normalized"] in approved_keys
        else "SKIPPED" if group["linkedin_url_normalized"] in skipped_keys
        else "PENDING"
    )

    import os as _os
    with AttioClient() as attio:
        list_id = _os.environ["ATTIO_LIST_ID"]
        entries_by_record = fetch_list_entries_by_record(attio, list_id)
        company_cache: dict = {}
        winner_facts = record_facts(attio, company_cache, group["winner_id"])
        loser_facts = [record_facts(attio, company_cache, rid) for rid in group["loser_ids"]]

    print(f"[group {idx} of {len(conflict)} — status={status}]")
    render_group(group, winner_facts, loser_facts, entries_by_record)
    return 0


if __name__ == "__main__":
    sys.exit(main())
