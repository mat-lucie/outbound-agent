"""One-off audit (2026-05-08): list all CONNECTION_SENT records, sorted by
last_contact_date ascending (oldest first). The 44 records older than Phase
0's 14-day window are the missed-accept candidates — the operator can visually
cross-reference against his LinkedIn 'My Network' to spot accepted people
still stuck at CONNECTION_SENT in Attio.

Read-only. No PB calls. No Attio writes.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).parent.parent / ".env")

from clients.attio import AttioClient  # noqa: E402
from models.pipeline import PipelineStage  # noqa: E402
from workflows.daily_check import (  # noqa: E402
    RecordCache,
    _get_all_entries_parsed,
)

ACCEPTANCE_CHECK_WINDOW_DAYS = 14


def main() -> int:
    cutoff = (date.today() - timedelta(days=ACCEPTANCE_CHECK_WINDOW_DAYS)).isoformat()

    with AttioClient() as attio:
        all_parsed = _get_all_entries_parsed(attio)
        cache = RecordCache(attio)

        rows: list[dict] = []
        for attrs in all_parsed:
            if attrs.get("stage") != PipelineStage.CONNECTION_SENT.value:
                continue
            name, company, linkedin_url, _, _ = cache.get(attrs["record_id"])
            last = (attrs.get("last_contact_date") or "")[:10] or "—"
            rows.append({
                "name": name or "?",
                "company": company or "?",
                "url": linkedin_url or "?",
                "last_contact": last,
                "exp": attrs.get("experiment_id") or "—",
                "dm_step": attrs.get("dm_step"),
                "record_id": attrs["record_id"],
            })

        rows.sort(key=lambda r: (r["last_contact"], r["name"]))

        # Split into stale (>14d) vs recent
        stale = [r for r in rows if r["last_contact"] != "—" and r["last_contact"] < cutoff]
        recent = [r for r in rows if r["last_contact"] != "—" and r["last_contact"] >= cutoff]
        no_date = [r for r in rows if r["last_contact"] == "—"]

        print(f"Total CONNECTION_SENT entries: {len(rows)}")
        print(f"  Stale (>14d, OUTSIDE Phase 0 window): {len(stale)}")
        print(f"  Recent (≤14d, INSIDE Phase 0 window): {len(recent)}")
        print(f"  No date: {len(no_date)}")
        print(f"  Phase 0 cutoff: {cutoff}\n")

        print(f"=== STALE ({len(stale)}) — Phase 0 SKIPS these. Cross-check against your LI 'My Network' ===\n")
        for r in stale:
            print(f"  {r['last_contact']}  {r['name']:<40}  {r['company']:<35}  {r['url']}")
            print(f"              exp={r['exp']}  dm_step={r['dm_step']}  record={r['record_id']}")

        if no_date:
            print(f"\n=== NO LAST_CONTACT_DATE ({len(no_date)}) ===\n")
            for r in no_date:
                print(f"  {r['name']:<40}  {r['company']:<35}  {r['url']}")
                print(f"              exp={r['exp']}  dm_step={r['dm_step']}  record={r['record_id']}")

        print(f"\n=== RECENT ({len(recent)}) — Phase 0 already checked these today ===\n")
        for r in recent:
            print(f"  {r['last_contact']}  {r['name']:<40}  {r['company']:<35}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
