---
name: data-quality-report
description: Compute the weekly Data Quality Report — 8-metric Attio dashboard with P0/P1 alarms that gate downstream DM sends.
---

# /data-quality-report

Operator runs this weekly (Sunday cadence recommended). Replaces the
06:00 Sunday cron. Read-only Attio aggregator + writes one Data
Quality Report row. Zero LLM cost.

## Step 0 — Verify Attio MCP scope

Same as `/sales-daily`.

## Step 1 — Run the report

```bash
sales data-quality-report
```

Options:
- `--period-days N` — lookback window (default 7).
- `--no-write` — print the report without writing the Attio row
  (useful for rehearsals).

## Exit code semantics

- `0` — clean. No alarms fired.
- `1` — P1 alarm fired (`manual_reply_classification_gap`). Operator
  should review; daily run continues.
- `70` — P0 alarm fired (one of `cohort_tagging_regression`,
  `write_owner_invariant_violated`, `migration_idempotency_regression`).
  Downstream `/sales-daily` halts DM sends until the operator
  resolves the queue row.

## Recovery from a P0 alarm

1. Open Attio → Operator Review Queue.
2. Filter `type=<slug>` from the exit message.
3. Investigate the upstream cause; resolve via `status=resolved` or
   `status=dismissed`.
4. Re-run `/sales-daily` — the halt gate clears once no open P0 row
   exists.

## Threshold tuning

The P1 threshold defaults to 10, synced with §10's halt-on-10
backstop (M7). Override via `OUTBOUND_MANUAL_REPLY_GAP_THRESHOLD`. The
DQR fires at-threshold, NOT before — the visible row gives the
operator the slug name attached to the halt rather than a bare halt.

## When to run

- Sunday (recommended cadence).
- Before `/sales-weekly` Monday morning — fresh DQR snapshot guides
  weekly priorities.
- Ad-hoc after any large data-mutating script run.
