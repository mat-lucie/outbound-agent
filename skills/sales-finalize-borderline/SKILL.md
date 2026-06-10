---
name: sales-finalize-borderline
description: Finalize the morning's borderline batch. Replaces the Monday 14:00 cron. Idempotent within the day; safe to re-run.
---

# /sales-finalize-borderline

Operator runs this Monday afternoon (or any time after the morning
borderline batch is staged). Picks the operator-decision-driven
`decision_run_id` when one exists in the Operator Review Queue;
otherwise uses `default_expire`.

## Step 0 — Verify Attio MCP scope

Same as `/sales-daily`.

## Step 1 — Run the finalize wrapper

```bash
sales sales-finalize-borderline --batch YYYY-MM-DD
```

The wrapper:
1. Looks up the latest operator-resolved `weekly_finalize_stale`
   queue row for the batch (via `decision_source` lookup).
2. If found: uses the operator's `decision_run_id`.
3. Else: uses `default_expire`.
4. Computes the idempotency key
   `{batch_date}_{decision_run_id}` and checks for an existing
   finalize row.
5. If existing: returns `action=skipped_idempotent` (no-op).
6. Else: invokes `weekly-finalize --batch <date>` and records the
   completion as a typed queue row.

## Idempotency contract

Re-running within the same day is a no-op unless the operator's
decision_run_id changed. Both keys can land at most once per day:
`{batch}_default_expire` AND `{batch}_<operator-run-id>`.

## Dry-run

`--dry-run` propagates to the inner `weekly-finalize` invocation —
the borderline verdicts are computed but no Attio writes happen.
