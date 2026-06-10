"""Where did the 7 1st-degree URLs come from, and where are they now?

For each 1st-degree URL from the last PB CSV, find matching Attio entries
across all stages.
"""
from __future__ import annotations

import csv
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

from clients.attio import AttioClient  # noqa: E402
from clients.phantombuster import PhantomBusterClient  # noqa: E402
from workflows.daily_check import (  # noqa: E402
    RecordCache,
    _get_all_entries_parsed,
    _normalize_linkedin_url,
)


def main() -> int:
    load_dotenv()
    scraper_id = os.environ["PB_PROFILE_SCRAPER_ID"]

    with PhantomBusterClient() as pb, AttioClient() as attio:
        # Debug script: inspects whichever CSV is currently at the
        # agent's "latest" S3 path. Production callers use the typed
        # `download_result_csv(launch)` route instead (F-PR-5).
        csv_text = pb.download_latest_csv_for_agent(scraper_id)
        assert csv_text

        reader = csv.DictReader(io.StringIO(csv_text))
        first_urls: list[str] = []
        for row in reader:
            url = (
                row.get("linkedin_url") or row.get("linkedinProfileUrl")
                or row.get("profileUrl") or row.get("query") or ""
            )
            degree = (row.get("connectionDegree") or "").strip()
            if degree == "1st" and url:
                first_urls.append(_normalize_linkedin_url(url))

        targets = set(first_urls)
        print(f"Looking up {len(targets)} unique 1st-degree URLs in Attio\n")

        all_parsed = _get_all_entries_parsed(attio)
        cache = RecordCache(attio)
        found: dict[str, list[dict]] = {u: [] for u in targets}
        for attrs in all_parsed:
            _, _, linkedin_url, _, _ = cache.get(attrs["record_id"])
            if not linkedin_url:
                continue
            norm = _normalize_linkedin_url(linkedin_url)
            if norm in targets:
                name, company, _, _, _ = cache.get(attrs["record_id"])
                found[norm].append({
                    "name": name,
                    "company": company,
                    "stage": attrs["stage"],
                    "record_id": attrs["record_id"],
                    "entry_id": attrs["entry_id"],
                    "last_contact_date": attrs.get("last_contact_date"),
                })

        for u, hits in found.items():
            print(f"URL: {u}")
            if not hits:
                print("  ❌ NOT FOUND in Attio pipeline list")
            else:
                for h in hits:
                    print(f"  ✓ {h['name']} @ {h['company']} | stage={h['stage']} | last_contact={h['last_contact_date']}")
                    print(f"    record={h['record_id']} entry={h['entry_id']}")
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
