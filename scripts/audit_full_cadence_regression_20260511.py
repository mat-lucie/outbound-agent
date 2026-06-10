"""Full cadence-regression audit (2026-05-11): cross-reference EVERY current
pipeline entry against the 2026-04-21 pre-dedup snapshot. Find records that
regressed (current dm_step < snapshot dm_step, or current stage rank <
snapshot stage rank).

The 2026-05-03 weekly_prospect bug silently wiped cadence on prospects whose
list entries were removed in the 2026-04-21 dedup. We've identified ~22 such
victims from today's DM1 queue, but the bug could have hit any prospect.
This script enumerates ALL victims, regardless of current stage, so the
repair script can be extended to cover them.

Read-only. No Attio writes. Outputs:
- Console table sorted by regression severity (DM-step lost)
- CSV at exports/cadence_regression_audit_2026-05-11.csv

Run:
  python3 scripts/audit_full_cadence_regression_20260511.py
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
from workflows.daily_check import (  # noqa: E402
    RecordCache,
    _get_all_entries_parsed,
    preload_pipeline_persons,
)

SNAPSHOT_PATH = Path(__file__).parent.parent / "exports" / "attio-snapshot-2026-04-21-pre-apply.json"
OUT_CSV = Path(__file__).parent.parent / "exports" / "cadence_regression_audit_2026-05-11.csv"

# Stage rank for regression detection (higher = further along the funnel).
STAGE_RANK = {
    "Prospect": 0,
    "Partner Intro": 0,
    "Connection Sent": 1,
    "Accepted": 2,
    "DM1 Sent": 3,
    "DM2 Sent": 4,
    "DM3 Sent": 5,
    "Responded": 99,
    "Call Booked": 99,
    "Qualified": 99,
    "Not Interested": 99,  # terminal — should NOT regress to non-terminal
}


def _first(values: dict, key: str):
    v = values.get(key)
    if isinstance(v, list) and v:
        v0 = v[0]
        if isinstance(v0, dict):
            for k in ("value", "name", "target_object_id"):
                if k in v0:
                    return v0[k]
            if "full_name" in v0:
                return v0["full_name"]
        return v0
    return None


def _stage_title(values: dict) -> str:
    v = values.get("stage")
    if isinstance(v, list) and v:
        return v[0].get("status", {}).get("title", "")
    return ""


def _person_name(p: dict) -> str:
    vals = p.get("values", {})
    nm = vals.get("name")
    if isinstance(nm, list) and nm:
        x = nm[0]
        if isinstance(x, dict):
            return x.get("full_name") or f"{x.get('first_name', '')} {x.get('last_name', '')}".strip()
    return ""


def main() -> int:
    if not SNAPSHOT_PATH.exists():
        print(f"ERROR: {SNAPSHOT_PATH} not found", file=sys.stderr)
        return 1

    print(f"Loading snapshot {SNAPSHOT_PATH.name}...")
    with SNAPSHOT_PATH.open() as f:
        snap = json.load(f)

    # Build snapshot index: keyed by person record_id, value = best (max-rank)
    # state observed across all entries for that record. Some records had
    # duplicate entries pre-dedup at divergent stages; the "best" is the most
    # advanced cadence the person had reached.
    snap_state: dict[str, dict] = {}
    snap_name: dict[str, str] = {}
    for p in snap["people"]:
        rid = p.get("id")
        if isinstance(rid, dict):
            rid = rid.get("record_id")
        snap_name[rid] = _person_name(p)
    for e in snap["entries"]:
        rid = e.get("parent_record_id")
        if isinstance(rid, dict):
            rid = rid.get("record_id")
        vals = e.get("entry_values", {})
        stage = _stage_title(vals)
        dm_step = _first(vals, "dm_step") or 0
        lcd = _first(vals, "last_contact_date") or ""
        rank = STAGE_RANK.get(stage, 0)
        cur = snap_state.get(rid)
        # Track max rank + max dm_step
        if cur is None or rank > cur["rank"] or (rank == cur["rank"] and int(dm_step or 0) > int(cur["dm_step"] or 0)):
            snap_state[rid] = {
                "stage": stage,
                "rank": rank,
                "dm_step": int(dm_step or 0),
                "last_contact_date": lcd,
            }
    print(f"  Snapshot people: {len(snap['people'])}, entries: {len(snap['entries'])}")
    print(f"  Indexed {len(snap_state)} unique record_ids with cadence state.\n")

    print("Loading current Attio state...")
    attio = AttioClient()
    all_parsed = _get_all_entries_parsed(attio)
    cache = RecordCache(attio)
    preload_pipeline_persons(attio, cache, {a["record_id"] for a in all_parsed})
    print(f"  Current entries: {len(all_parsed)}\n")

    # Build current state index, same logic (max-rank per record)
    cur_state: dict[str, dict] = {}
    for attrs in all_parsed:
        rid = attrs.get("record_id")
        if not rid:
            continue
        stage = attrs.get("stage") or ""
        rank = STAGE_RANK.get(stage, 0)
        dm_step = int(attrs.get("dm_step") or 0)
        cur = cur_state.get(rid)
        if cur is None or rank > cur["rank"] or (rank == cur["rank"] and dm_step > cur["dm_step"]):
            cur_state[rid] = {
                "stage": stage,
                "rank": rank,
                "dm_step": dm_step,
                "last_contact_date": attrs.get("last_contact_date") or "",
                "entry_id": attrs.get("entry_id"),
            }

    # Detect regressions
    regressions = []
    for rid, snap_s in snap_state.items():
        cur_s = cur_state.get(rid)
        if cur_s is None:
            # Record was deleted from pipeline entirely. May or may not be a bug
            # (could be intentional removal). Flag separately.
            regressions.append({
                "type": "MISSING_FROM_CURRENT",
                "record_id": rid,
                "name": snap_name.get(rid, "?"),
                "snap_stage": snap_s["stage"],
                "snap_dm_step": snap_s["dm_step"],
                "snap_lcd": snap_s["last_contact_date"],
                "cur_stage": "(deleted)",
                "cur_dm_step": "-",
                "cur_lcd": "-",
                "delta_dm_step": -snap_s["dm_step"],
                "entry_id": "",
            })
            continue
        # Regression criteria:
        #  - dm_step dropped
        #  - stage rank dropped (and not a terminal rank-99 → anything)
        dm_regressed = cur_s["dm_step"] < snap_s["dm_step"]
        rank_regressed = cur_s["rank"] < snap_s["rank"] and snap_s["rank"] != 99
        if not (dm_regressed or rank_regressed):
            continue
        regressions.append({
            "type": "REGRESSED",
            "record_id": rid,
            "name": cache.get(rid)[0] or snap_name.get(rid, "?"),
            "snap_stage": snap_s["stage"],
            "snap_dm_step": snap_s["dm_step"],
            "snap_lcd": snap_s["last_contact_date"],
            "cur_stage": cur_s["stage"],
            "cur_dm_step": cur_s["dm_step"],
            "cur_lcd": cur_s["last_contact_date"],
            "delta_dm_step": cur_s["dm_step"] - snap_s["dm_step"],
            "entry_id": cur_s["entry_id"],
        })

    # Sort: REGRESSED with biggest dm-step loss first, then MISSING
    regressions.sort(key=lambda r: (r["type"] != "REGRESSED", r["delta_dm_step"]))

    print(f"=== Cadence regression audit: {len(regressions)} affected records ===\n")

    regressed = [r for r in regressions if r["type"] == "REGRESSED"]
    missing = [r for r in regressions if r["type"] == "MISSING_FROM_CURRENT"]

    print(f"REGRESSED in pipeline ({len(regressed)}):")
    print(f"  {'name':35s} | {'snap':18s} | {'cur':18s} | dm_step Δ")
    print(f"  {'-'*35} | {'-'*18} | {'-'*18} | -----")
    for r in regressed[:80]:
        snap_s = f"{r['snap_stage'][:15]}/dm{r['snap_dm_step']}"
        cur_s = f"{r['cur_stage'][:15]}/dm{r['cur_dm_step']}"
        print(f"  {r['name'][:35]:35s} | {snap_s:18s} | {cur_s:18s} | {r['delta_dm_step']:+d}")
    if len(regressed) > 80:
        print(f"  ... and {len(regressed) - 80} more (see CSV)")

    print(f"\nMISSING from current pipeline ({len(missing)}):")
    print(f"  {'name':35s} | {'snap stage':18s} | dm_step")
    print(f"  {'-'*35} | {'-'*18} | -------")
    for r in missing[:40]:
        snap_s = f"{r['snap_stage'][:15]}/dm{r['snap_dm_step']}"
        print(f"  {r['name'][:35]:35s} | {snap_s:18s} | {r['snap_dm_step']}")
    if len(missing) > 40:
        print(f"  ... and {len(missing) - 40} more (see CSV)")

    # Write full CSV
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w") as f:
        w = csv.DictWriter(f, fieldnames=[
            "type", "name", "snap_stage", "snap_dm_step", "snap_lcd",
            "cur_stage", "cur_dm_step", "cur_lcd", "delta_dm_step",
            "record_id", "entry_id",
        ])
        w.writeheader()
        for r in regressions:
            w.writerow(r)
    print(f"\nWrote {OUT_CSV}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
