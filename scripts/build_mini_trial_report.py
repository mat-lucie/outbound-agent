"""Build a mini dedup-report for the apply-trial — 3 auto-safe clusters only.

Extracts the first N auto_safe clusters from the full report and emits a
standalone report file that attio_dedup apply can consume. The structure
matches the full report so no code changes are needed.

Usage:
  python3 scripts/build_mini_trial_report.py
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import click


@click.command()
@click.option("--source", default="exports/dedup-report-2026-04-21.json")
@click.option("--out", default="exports/dedup-report-mini-trial.json")
@click.option("--n", default=3, type=int, help="How many auto_safe clusters to include.")
def main(source: str, out: str, n: int) -> int:
    data = json.loads(Path(source).read_text())
    people = data.get("people", {})
    auto_safe = people.get("auto_safe_groups", [])
    selected = auto_safe[:n]

    mini = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source_report": source,
        "people": {
            "auto_safe_groups": selected,
            "conflict_groups": [],
            "approved_conflict_groups": [],
            "skipped_groups": [],
        },
        "companies": {
            "auto_safe_groups": [],
            "conflict_groups": [],
            "approved_conflict_groups": [],
            "skipped_groups": [],
        },
        "summary": {
            "people_groups_auto_safe": len(selected),
            "people_groups_conflict": 0,
            "people_records_to_delete": sum(len(g.get("loser_ids", [])) for g in selected),
            "companies_groups_auto_safe": 0,
            "companies_groups_conflict": 0,
            "companies_records_to_delete": 0,
        },
    }
    Path(out).write_text(json.dumps(mini, indent=2))
    click.echo(f"Wrote {out}")
    click.echo(f"  Clusters: {len(selected)}")
    click.echo(f"  Records to delete: {mini['summary']['people_records_to_delete']}")
    for g in selected:
        click.echo(f"  • {g['linkedin_url_normalized']}  winner={g['winner_id'][:8]}  losers={len(g['loser_ids'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
