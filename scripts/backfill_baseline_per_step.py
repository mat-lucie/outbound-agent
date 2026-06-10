#!/usr/bin/env python3
"""Backfill per-step response rates onto the baseline-v0 row in experiments.tsv.

One-time human-run script. Re-runs are safe (idempotent) — they overwrite the
per-step rates with the current measurement against live Attio. After running,
inspect the printed before/after diff and `git commit experiments.tsv` manually.

Usage:
    python3 scripts/backfill_baseline_per_step.py
"""

import csv
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Allow `python3 scripts/backfill_baseline_per_step.py` from repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from clients.attio import AttioClient  # noqa: E402
from models.experiment import DEFAULT_TSV_PATH, TSV_FIELDS  # noqa: E402
from workflows.learn import measure_cohorts  # noqa: E402

BASELINE_ID = "baseline-v0"


def main() -> int:
    print(f"=== Backfill {BASELINE_ID} per-step rates ===\n")

    if not DEFAULT_TSV_PATH.exists():
        print(f"  ERROR: {DEFAULT_TSV_PATH} does not exist.", file=sys.stderr)
        return 1

    with AttioClient() as attio:
        cohorts = measure_cohorts(attio)

    baseline_cohort = next(
        (c for c in cohorts if c["experiment_id"] == BASELINE_ID),
        None,
    )
    if baseline_cohort is None:
        print(f"  ERROR: no cohort with experiment_id={BASELINE_ID!r} found in Attio.", file=sys.stderr)
        return 1

    new_dm1 = baseline_cohort["dm1_response_rate"]
    new_dm2 = baseline_cohort["dm2_response_rate"]
    new_dm3 = baseline_cohort["dm3_response_rate"]

    print(f"  Cohort dm1: {baseline_cohort['dm1_replies']}/{baseline_cohort['dm1_received']} = {new_dm1:.4f}")
    print(f"  Cohort dm2: {baseline_cohort['dm2_replies']}/{baseline_cohort['dm2_received']} = {new_dm2:.4f}")
    print(f"  Cohort dm3: {baseline_cohort['dm3_replies']}/{baseline_cohort['dm3_received']} = {new_dm3:.4f}")

    with DEFAULT_TSV_PATH.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))

    target_row = next((r for r in rows if r.get("experiment_id") == BASELINE_ID), None)
    if target_row is None:
        print(f"  ERROR: no row with experiment_id={BASELINE_ID!r} in {DEFAULT_TSV_PATH}.", file=sys.stderr)
        return 1

    print("\n  Before:")
    for k in ("dm1_response_rate", "dm2_response_rate", "dm3_response_rate"):
        print(f"    {k}: {target_row.get(k, '<missing>')!r}")

    target_row["dm1_response_rate"] = str(new_dm1)
    target_row["dm2_response_rate"] = str(new_dm2)
    target_row["dm3_response_rate"] = str(new_dm3)

    print("\n  After:")
    for k in ("dm1_response_rate", "dm2_response_rate", "dm3_response_rate"):
        print(f"    {k}: {target_row[k]!r}")

    # Ensure every row has all current TSV_FIELDS as keys (DictWriter requires this).
    for row in rows:
        for field in TSV_FIELDS:
            row.setdefault(field, "")

    with DEFAULT_TSV_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TSV_FIELDS, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n  Wrote {DEFAULT_TSV_PATH}")
    print("  Inspect with `git diff experiments.tsv` and commit when satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
