"""Snapshot every Attio person record + list entry to a JSON file.

Read-only. Writes `exports/attio-snapshot-YYYY-MM-DD.json` with the full
raw dump from Attio — the backup we can diff against if apply goes wrong.

Usage:
  python3 scripts/attio_snapshot.py
  python3 scripts/attio_snapshot.py --out exports/custom-path.json
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import click
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent))

from clients.attio import AttioClient  # noqa: E402


@click.command()
@click.option("--out", default=None, help="Output path; defaults to exports/attio-snapshot-YYYY-MM-DD.json")
def main(out: str | None) -> int:
    timestamp = datetime.now().strftime("%Y-%m-%d")
    out_path = Path(out or f"exports/attio-snapshot-{timestamp}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with AttioClient() as attio:
        list_id = os.environ["ATTIO_LIST_ID"]
        click.echo("Fetching person records…")
        people = attio.search_people(filter_=None, limit=50000)
        click.echo(f"  {len(people)} people")
        click.echo(f"Fetching list entries from {list_id}…")
        entries = attio.query_list_entries(list_id=list_id, limit=50000)
        click.echo(f"  {len(entries)} entries")

    snapshot = {
        "generated_at": datetime.now().isoformat(),
        "list_id": list_id,
        "people_count": len(people),
        "entries_count": len(entries),
        "people": people,
        "entries": entries,
    }
    out_path.write_text(json.dumps(snapshot, indent=2))
    click.echo(f"Wrote {out_path} ({out_path.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
