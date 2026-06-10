"""Inspect PhantomBuster Message Sender output — last run CSV + columns.

Usage: python3 scripts/debug_message_sender_csv.py
"""
from __future__ import annotations

import csv
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

from clients.phantombuster import PhantomBusterClient  # noqa: E402


def main() -> int:
    load_dotenv()
    agent_id = os.environ.get("PB_MESSAGE_SENDER_ID")
    if not agent_id:
        print("ERROR: PB_MESSAGE_SENDER_ID not set")
        return 1

    with PhantomBusterClient() as pb:
        out = pb.get_output(agent_id)
        print(f"status={out.get('status')} isRunning={out.get('isAgentRunning')}")
        # Debug script: inspects the agent's "latest" CSV at S3. Use
        # the typed `download_result_csv(launch)` from production code.
        csv_text = pb.download_latest_csv_for_agent(agent_id)
        if not csv_text:
            print("No CSV returned")
            return 0
        print(f"CSV bytes: {len(csv_text)}")
        reader = csv.DictReader(io.StringIO(csv_text))
        print(f"Fieldnames: {reader.fieldnames}")
        rows = list(reader)
        print(f"Row count: {len(rows)}")
        for i, r in enumerate(rows[:10]):
            print(f"--- row {i} ---")
            for k, v in r.items():
                s = str(v)
                if len(s) > 180:
                    s = s[:180] + "..."
                print(f"  {k}: {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
