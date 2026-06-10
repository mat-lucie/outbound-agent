"""Preview harvest for a single persona without writing to Attio.

Launches PhantomBuster Search Export on the persona's Mexico SN URL,
scores the returned profiles, and prints what WOULD be added. Does not
write to Attio. Does burn one PB credit per invocation.

Usage:
    python3 scripts/preview_persona.py <persona_key>

Example:
    python3 scripts/preview_persona.py mx_midmarket_manufacturing
"""

import os
import sys
from datetime import date
from pathlib import Path

# Load .env
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

sys.path.insert(0, str(Path(__file__).parent.parent))

from clients.attio import AttioClient  # noqa: E402
from clients.crm.attio_provider import AttioProvider  # noqa: E402
from clients.phantombuster import PhantomBusterClient  # noqa: E402
from models.campaign import load_personas  # noqa: E402
from workflows.weekly_prospect import _launch_and_download, _process_prospects  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 1
    persona_key = sys.argv[1]

    personas_data = load_personas()
    if persona_key not in personas_data:
        print(f"Error: persona '{persona_key}' not found. Available: {list(personas_data.keys())}")
        return 1

    persona = personas_data[persona_key]
    sn_urls = persona.get("search_queries", {}).get("sn_search_urls", {})
    # Pick the first available geo URL (mx, co, cl, br, etc.)
    sn_url = ""
    for _geo, url in sn_urls.items():
        if url and "PLACEHOLDER" not in url:
            sn_url = url
            break
    if not sn_url:
        print(f"Error: persona '{persona_key}' has no SN URL configured.")
        return 1

    print(f"=== Previewing persona: {persona_key} ===")
    print(f"URL: {sn_url}")
    print()

    pb_api_key = os.environ.get("PHANTOMBUSTER_API_KEY", "")
    pb_search_export_id = os.environ.get("PB_SEARCH_EXPORT_ID", "")
    attio_api_key = os.environ.get("ATTIO_API_KEY", "")
    if not pb_api_key or not pb_search_export_id or not attio_api_key:
        print("Error: missing PHANTOMBUSTER_API_KEY, PB_SEARCH_EXPORT_ID, or ATTIO_API_KEY.")
        return 1

    pb = PhantomBusterClient(api_key=pb_api_key)
    # _process_prospects migrated to the vendor-neutral CRMProvider (P1c);
    # wrap the raw AttioClient in the reference provider.
    crm = AttioProvider(AttioClient(api_key=attio_api_key))

    print("Launching PhantomBuster Search Export...")
    csv_text = _launch_and_download(pb, pb_search_export_id, sn_url, batch_size=50)
    if not csv_text:
        print("No results from PhantomBuster.")
        return 0

    import csv as csv_lib
    import io
    prospects_raw = list(csv_lib.DictReader(io.StringIO(csv_text)))
    print(f"Exported {len(prospects_raw)} raw profiles.\n")

    summary = {"exported": len(prospects_raw), "scored": 0, "qualified": 0, "duplicates": 0, "rejected": 0, "added": 0}
    seen_urls: set[str] = set()
    today = date.today().isoformat()

    persona_config = dict(persona)
    persona_config["key"] = persona_key
    _process_prospects(prospects_raw, crm, list_id="", today=today, dry_run=True, summary=summary, seen_urls=seen_urls, persona_config=persona_config)

    print()
    print(f"--- Preview Summary ({persona_key}) ---")
    print(f"Exported:   {summary['exported']}")
    print(f"Scored:     {summary['scored']}")
    print(f"Qualified:  {summary['qualified']}")
    print(f"Duplicates: {summary['duplicates']}")
    print(f"Rejected:   {summary['rejected']}")
    print(f"Would add:  {summary['qualified'] - summary['duplicates']}")
    print()
    print("No Attio writes performed. Re-run `python3 cli.py weekly` (without --dry-run) for real execution.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
