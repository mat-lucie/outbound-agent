"""Replay Phase 0 URL-matching against the last PB Profile Scraper CSV.

Fetches the latest result CSV from PB, fetches our CONNECTION_SENT pool from
Attio, and reports where the URL match fails.

Usage: python3 scripts/debug_phase0_match.py
"""
from __future__ import annotations

import csv
import io
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

from clients.attio import AttioClient  # noqa: E402
from clients.phantombuster import PhantomBusterClient  # noqa: E402
from models.pipeline import PipelineStage  # noqa: E402
from workflows.daily_check import (  # noqa: E402
    RecordCache,
    _get_all_entries_parsed,
    _normalize_linkedin_url,
)


def main() -> int:
    load_dotenv()
    scraper_id = os.environ["PB_PROFILE_SCRAPER_ID"]

    with PhantomBusterClient() as pb, AttioClient() as attio:
        # Debug script: inspects the agent's "latest" CSV at S3.
        csv_text = pb.download_latest_csv_for_agent(scraper_id)
        assert csv_text, "No CSV"

        # Collect our CONNECTION_SENT pool from Attio
        all_parsed = _get_all_entries_parsed(attio)
        cache = RecordCache(attio)
        our: list[dict] = []
        for attrs in all_parsed:
            if attrs["stage"] != PipelineStage.CONNECTION_SENT.value:
                continue
            _, _, linkedin_url, _, _ = cache.get(attrs["record_id"])
            if not linkedin_url:
                continue
            our.append({
                "record_id": attrs["record_id"],
                "linkedin_url_raw": linkedin_url,
                "linkedin_url_norm": _normalize_linkedin_url(linkedin_url),
            })
        our_urls = {p["linkedin_url_norm"] for p in our}
        print(f"CONNECTION_SENT pool: {len(our)} profiles, {len(our_urls)} unique URLs")

        # Parse CSV
        reader = csv.DictReader(io.StringIO(csv_text))
        rows = list(reader)

        # For each row, pick the same URL field our production code does
        def pick_url(row: dict) -> str:
            return (
                row.get("linkedin_url", "")
                or row.get("linkedinProfileUrl", "")
                or row.get("profileUrl", "")
                or row.get("query", "")
            )

        degree_dist: Counter[str] = Counter()
        matched = 0
        unmatched_first_degree: list[tuple[str, str]] = []
        matched_first = 0
        all_firsts: list[str] = []
        for row in rows:
            url = pick_url(row)
            degree = (row.get("connectionDegree") or "").strip()
            if not url or not degree:
                continue
            degree_dist[degree] += 1
            norm = _normalize_linkedin_url(url)
            hit = norm in our_urls
            if degree == "1st":
                all_firsts.append(url)
                if hit:
                    matched_first += 1
                else:
                    unmatched_first_degree.append((url, norm))
            if hit:
                matched += 1

        print(f"\nCSV degree distribution (rows with url+degree): {dict(degree_dist)}")
        print(f"Rows that matched our CONNECTION_SENT pool: {matched}")
        print(f"1st-degree rows matched:   {matched_first}")
        print(f"1st-degree rows UNMATCHED: {len(unmatched_first_degree)}")
        print()
        if unmatched_first_degree:
            print("=== 1st-degree URLs that did NOT match ===")
            for raw, norm in unmatched_first_degree:
                print(f"  raw:  {raw}")
                print(f"  norm: {norm}")
                # Find closest match in our pool for debugging
                for p in our:
                    if p["linkedin_url_norm"].split("/")[-1] in norm:
                        print(f"  our candidate: {p['linkedin_url_raw']} -> {p['linkedin_url_norm']}")
                        break
                print()
        print("=== All 1st-degree URLs reported by PB ===")
        for u in all_firsts:
            print(f"  {u}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
