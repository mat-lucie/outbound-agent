"""Pattern A backfill audit (2026-05-08): cross-reference current PROSPECT-stage
records against `exports/dm-stage-repair-report.json` to find every record that
shouldn't be in PROSPECT (had prior PB Message Sender DMs before the 2026-04-21
dedup, then got re-added by weekly_prospect 2026-05-03).

Read-only. No Attio writes. Outputs:
- Console table for review.
- CSV at `exports/pattern_a_backfill_candidates_2026-05-08.csv`.

Run:
  python3 scripts/audit_pattern_a_backfill_20260508.py
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).parent.parent / ".env")

from clients.attio import AttioClient  # noqa: E402
from models.pipeline import PipelineStage  # noqa: E402
from workflows.daily_check import (  # noqa: E402
    RecordCache,
    _get_all_entries_parsed,
    _normalize_linkedin_url,
)

REPORT_PATH = Path(__file__).parent.parent / "exports" / "dm-stage-repair-report.json"
OUT_CSV = Path(__file__).parent.parent / "exports" / "pattern_a_backfill_candidates_2026-05-08.csv"


def main() -> int:
    if not REPORT_PATH.exists():
        print(f"ERROR: {REPORT_PATH} not found", file=sys.stderr)
        return 1

    with REPORT_PATH.open() as f:
        report = json.load(f)

    # URL → send_count from the 2026-04-21 PB Message Sender history.
    prior_dm_by_url: dict[str, int] = {}
    for r in report.get("repairs", []):
        url = _normalize_linkedin_url(r["linkedin_url"])
        prior_dm_by_url[url] = max(prior_dm_by_url.get(url, 0), r.get("send_count", 0))

    print(f"Prior-cadence URL set: {len(prior_dm_by_url)} (from {REPORT_PATH.name})\n")

    with AttioClient() as attio:
        all_parsed = _get_all_entries_parsed(attio)
        cache = RecordCache(attio)

        candidates: list[dict] = []
        for attrs in all_parsed:
            if attrs.get("stage") != PipelineStage.PROSPECT.value:
                continue
            name, company, linkedin_url, _, _ = cache.get(attrs["record_id"])
            if not linkedin_url:
                continue
            norm = _normalize_linkedin_url(linkedin_url)
            prior_dms = prior_dm_by_url.get(norm)
            if prior_dms is None:
                continue
            candidates.append({
                "name": name or "?",
                "company": company or "?",
                "prior_dm_count": prior_dms,
                "current_score": attrs.get("quality_score"),
                "last_contact_date": attrs.get("last_contact_date") or "",
                "experiment_id": attrs.get("experiment_id") or "",
                "record_id": attrs["record_id"],
                "entry_id": attrs["entry_id"],
                "linkedin_url": linkedin_url,
                "default_action": _default_action(prior_dms),
            })

        candidates.sort(key=lambda c: (-c["prior_dm_count"], c["name"]))

        print(f"=== Pattern A backfill candidates: {len(candidates)} ===\n")
        for c in candidates:
            print(f"  [{c['prior_dm_count']} prior DM(s) → {c['default_action']:<15}]  {c['name']:<35}  {c['company']:<35}")
            print(f"      score={c['current_score']}  last_contact={c['last_contact_date']}  exp={c['experiment_id']}")
            print(f"      url={c['linkedin_url']}")
            print(f"      record={c['record_id']}  entry={c['entry_id']}")
            print()

        # Distribution
        from collections import Counter
        dist = Counter(c["default_action"] for c in candidates)
        print("--- Default-action distribution ---")
        for action, count in dist.most_common():
            print(f"  {action:<20}  {count}")

        # CSV out
        OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
        with OUT_CSV.open("w", newline="") as f:
            fieldnames = ["name", "company", "prior_dm_count", "default_action", "current_score",
                          "last_contact_date", "experiment_id", "record_id", "entry_id", "linkedin_url"]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(candidates)
        print(f"\nWritten: {OUT_CSV}")

    return 0


def _default_action(prior_dms: int) -> str:
    """Default repair action based on prior PB Message Sender history.

    Defaults are SAFE (favor NOT_INTERESTED). The operator can override per-record in the CSV
    before applying the repair script.
    """
    if prior_dms >= 3:
        return "NOT_INTERESTED"   # full cadence completed
    if prior_dms >= 1:
        return "NOT_INTERESTED"   # partial cadence — the operator can override to RE_ENGAGE
    return "VERIFY_VIA_PB"        # 0 PB DMs but in repair report (rare); needs degree check


if __name__ == "__main__":
    sys.exit(main())
