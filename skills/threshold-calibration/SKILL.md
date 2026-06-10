---
name: threshold-calibration
description: Compute ROC sweep for the quality_score pass threshold. Math-only — opens a recommendation queue row; never changes the live threshold.
---

# /threshold-calibration

Operator runs this weekly (Sunday cadence recommended, after
`/data-quality-report`). Replaces the 06:30 Sunday cron. Pure Attio
read + numpy/scipy ROC math; zero LLM cost.

Ships in PR-45.5 — until then this skill prints a "not yet shipped"
message and exits.

## Step 0 — Verify Attio MCP scope

Same as `/sales-daily`.

## Step 1 — Run the calibration

```bash
sales threshold-calibration
```

Options:
- `--dry-run` — print the ROC table without opening the recommendation
  queue row.

## Output

The script:
1. Queries LinkedIn Outreach for labeled prospects (stage ∈ {QUALIFIED,
   CALL_BOOKED, NOT_INTERESTED, RESPONDED} AND `response_classification
   ∈ {positive, defensive}` AND `quality_score IS NOT NULL`).
2. Sweeps thresholds 40..80 step 5; computes max-Youden + cost-weighted
   recommendations.
3. Opens a `threshold_recommendation` queue row with the ROC table.

## Abstain semantics

When `n_labeled < 50`, the function returns `{"status": "abstain"}`
and opens a `threshold_recommendation_insufficient_data` row. The
operator sees the abstain explicitly — never silently skipped.

## Operator decision

The script NEVER changes the live threshold in `quality_gate.score_prospect`.
The recommendation queue row is for operator confirmation; a separate
PR is required to flip the threshold in code.
