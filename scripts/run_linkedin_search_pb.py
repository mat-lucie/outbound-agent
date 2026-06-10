#!/usr/bin/env python3
"""Launch the LinkedIn People Search Export PhantomBuster agent with a custom URL.

Distinct from the weekly Sales Navigator flow: uses PB_LINKEDIN_SEARCH_EXPORT_ID,
not PB_SEARCH_EXPORT_ID. Local driver for one-off referral-brief CSV pulls.

Usage:
    python3 scripts/run_linkedin_search_pb.py \\
        --url "https://www.linkedin.com/search/results/people/?..." \\
        --output exports/<host>-pb-export.csv \\
        [--profiles 500]
"""
import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from clients.phantombuster import PhantomBusterClient  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="LinkedIn People Search URL")
    parser.add_argument("--output", required=True, help="Path to write CSV result")
    parser.add_argument(
        "--profiles", type=int, default=500, help="Max profiles to scrape (default: 500)"
    )
    parser.add_argument(
        "--max-wait", type=int, default=900, help="Max wait seconds (default: 900 = 15min)"
    )
    args = parser.parse_args()

    load_dotenv(REPO / ".env")

    api_key = os.environ["PHANTOMBUSTER_API_KEY"]
    agent_id = os.environ["PB_LINKEDIN_SEARCH_EXPORT_ID"]
    li_cookie = os.environ.get("PB_LI_SESSION_COOKIE", "")
    li_ua = os.environ.get("PB_LI_USER_AGENT", "")
    if not li_cookie:
        print("ERROR: PB_LI_SESSION_COOKIE not set in .env — search will run unauthenticated and ignore the connectionOf filter.")
        return 1

    pb = PhantomBusterClient(api_key)
    try:
        launch_args = {
            "search": args.url,
            "numberOfProfiles": args.profiles,
            "numberOfResultsPerSearch": args.profiles,
            "numberOfLinesPerLaunch": args.profiles,
            "removeDuplicateProfiles": True,
            "identities": [{"sessionCookie": li_cookie, "userAgent": li_ua}],
        }
        print(f"→ Launching PB agent {agent_id} for up to {args.profiles} profiles…")
        launch = pb.launch_agent(agent_id, launch_args)
        print(f"  container: {launch.container_id}")

        print(f"→ Waiting for completion (max {args.max_wait}s)…")
        try:
            pb.wait_for_completion(
                launch, poll_interval=15, max_wait=args.max_wait
            )
        except Exception as e:
            print(f"ERROR: PB run failed or timed out: {e}")
            return 1

        print("→ Downloading CSV…")
        csv_text = pb.download_result_csv(launch)
        if not csv_text:
            print("ERROR: PB returned no CSV (check phantom config: cookie expired?)")
            return 1

        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(csv_text)
        rows = csv_text.count("\n") - 1
        print(f"✓ Saved {len(csv_text):,} bytes ({rows} rows) → {out_path}")
        return 0
    finally:
        pb.close()


if __name__ == "__main__":
    sys.exit(main())
