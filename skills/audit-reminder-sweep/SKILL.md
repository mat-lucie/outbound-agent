---
name: audit-reminder-sweep
description: Scan the Operator Review Queue for rows that have stayed `open` longer than the staleness floor. Read-only digest.
---

# /audit-reminder-sweep

Ad-hoc operator command. Useful when the queue UI feels backlogged
and you want a one-screen digest of the rows that need attention
most.

Read-only — no Attio mutations.

## Step 0 — Verify Attio MCP scope

Same canary procedure as `/sales-daily`. Halt on scope failure.
Even though this command is read-only, the canary confirms the
read scope is intact — a stale token would silently return an
empty queue and the operator would see "Clean" when in fact the
query never landed.

## Step 1 — Run the sweep

```bash
sales audit-reminder-sweep --stale-days 7
```

Lists every open queue row whose `opened_at` is more than `--stale-days`
ago. Default 7 days.

## When this fires zero rows

Either the operator is keeping up OR all stale rows are already
resolved/dismissed. If suspicion warrants a deeper look, drop
`--stale-days` to 1 and re-run.
