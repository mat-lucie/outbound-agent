"""Inspect the PhantomBuster Profile Scraper — last run output + CSV headers.

Usage: python3 scripts/debug_profile_scraper.py
"""
from __future__ import annotations

import csv
import io
import os
import sys
from collections import Counter

# Ensure project root import works
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

from clients.phantombuster import PhantomBusterClient  # noqa: E402


def main() -> int:
    load_dotenv()
    agent_id = os.environ.get("PB_PROFILE_SCRAPER_ID")
    if not agent_id:
        print("ERROR: PB_PROFILE_SCRAPER_ID not set in .env")
        return 1

    with PhantomBusterClient() as pb:
        agent = pb.get_agent(agent_id)
        print("=== Profile Scraper agent ===")
        print(f"  id:        {agent.get('id')}")
        print(f"  name:      {agent.get('name')}")
        print(f"  script:    {agent.get('scriptId')}")
        print(f"  s3Folder:  {agent.get('s3Folder')}")
        print(f"  orgFolder: {agent.get('orgS3Folder')}")
        print()

        # Saved arguments tell us what spreadsheetUrl / columns config is stored
        print("=== Saved arguments (last-applied phantom config) ===")
        arg = agent.get("argument") or agent.get("arguments")
        print(f"  {arg}\n")

        out = pb.get_output(agent_id)
        print("=== Last run output metadata ===")
        print(f"  status:           {out.get('status')}")
        print(f"  isAgentRunning:   {out.get('isAgentRunning')}")
        print(f"  lastEndStatus:    {out.get('lastEndStatus')}")
        print(f"  lastEndMessage:   {out.get('lastEndMessage')}")
        print(f"  containerId:      {out.get('containerId')}")
        print()

        # Tail the stdout log to see why it may have returned empty results
        log = out.get("output", "") or ""
        print("=== Last 80 log lines ===")
        for line in log.splitlines()[-80:]:
            print(f"  {line}")
        print()

        # Debug script: inspects whichever CSV is currently at the
        # agent's "latest" S3 path. Production callers use the typed
        # `download_result_csv(launch)` route instead.
        csv_text = pb.download_latest_csv_for_agent(agent_id)
        if not csv_text:
            print("=== result CSV: NONE ===")
            return 0
        print(f"=== result CSV: {len(csv_text)} chars, "
              f"{csv_text.count(chr(10))} rows ===")
        reader = csv.DictReader(io.StringIO(csv_text))
        print(f"Fieldnames: {reader.fieldnames}")
        rows = list(reader)
        print(f"Row count:  {len(rows)}")
        if rows:
            degrees = Counter((r.get('connectionDegree') or '').strip() for r in rows)
            print(f"connectionDegree distribution: {dict(degrees)}")
            print()
            print("=== first 3 rows (truncated to 200 chars each) ===")
            for i, r in enumerate(rows[:3]):
                print(f"--- row {i} ---")
                for k, v in r.items():
                    s = str(v)
                    if len(s) > 200:
                        s = s[:200] + "..."
                    print(f"    {k}: {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
