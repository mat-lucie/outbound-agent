"""Dump every company missing industry_vertical to JSON for offline classification.

Pairs with scripts/apply_industry_classifications.py — together they replace
the API-key path of workflows/industry_classifier.py:backfill_missing_industries
when no ANTHROPIC_API_KEY is set. Operator (Claude Code session) reads the dump,
fills in industry labels by hand or via in-session reasoning, and runs the apply
script to write results back to Attio.

Usage:
  python3 scripts/dump_companies_needing_industry.py
  python3 scripts/dump_companies_needing_industry.py --out exports/needs-industry.json --limit 500
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent))

from clients.attio import AttioClient  # noqa: E402


@click.command()
@click.option("--out", default="exports/companies-needing-industry.json", help="Output path.")
@click.option("--limit", type=int, default=2000, help="Max companies to scan.")
def main(out: str, limit: int) -> int:
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with AttioClient() as client:
        click.echo(f"Scanning up to {limit} companies…")
        all_companies = client.search_companies(filter_=None, limit=limit)
        click.echo(f"  {len(all_companies)} companies returned")

    needing: list[dict] = []
    for record in all_companies:
        record_id = record.get("id", {}).get("record_id", "")
        if not record_id:
            continue

        values = record.get("values", {})
        iv = values.get("industry_vertical", [])
        has_industry = bool(iv) and (
            not isinstance(iv[0], dict)
            or iv[0].get("option", {}).get("title", "")
        )
        if has_industry:
            continue

        name_data = values.get("name", [])
        name = (
            name_data[0].get("value", "") if name_data and isinstance(name_data[0], dict) else ""
        )
        if not name:
            continue

        domain = ""
        domain_data = values.get("domains", [])
        if domain_data and isinstance(domain_data, list):
            first = domain_data[0]
            if isinstance(first, dict):
                domain = first.get("domain") or first.get("value") or ""

        needing.append({
            "record_id": record_id,
            "name": name,
            "domain": domain,
        })

    out_path.write_text(json.dumps(needing, indent=2))
    click.echo(f"Wrote {len(needing)} entries to {out_path}")
    click.echo(
        "\nNext step: classify each entry with one of these labels (case-sensitive):\n"
        "  Manufacturing | Food & Beverage | Construction | Oil & Gas | Mining |\n"
        "  Automotive | Textiles | Packaging | Chemicals | Pharma | Other\n"
        "Then write a classifications JSON keyed by record_id and run:\n"
        "  python3 scripts/apply_industry_classifications.py --input <classifications.json>"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
