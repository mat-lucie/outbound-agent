---
name: sales-weekly
description: Monday's full weekly sales bundle. Pulls prospect targets, runs prospecting + learn + diagnostic + brain + report as a sequenced step set.
---

# /sales-weekly

Operator-invoked slash command that replaces the Monday 7am cron.
Monday cadence; the operator triggers the whole bundle by running
`/sales-weekly`.

## Step 0 — Verify Attio MCP scope

Same canary procedure as `/sales-daily` step 0. Halt on scope failure.

## Step 1 — Pipeline-starvation early check

Run `evaluate_pipeline_starvation` BEFORE prospecting so the operator
sees whether the prior week's pool is genuinely exhausted. If
`low_prospects` fires, the operator should review whether the
weekly target list is fresh or needs a refresh.

## Step 2 — Sequenced workflow steps

Each step below is a separate sales-cli invocation. Run them in this
order; pause between steps if the operator needs to inspect output.

1. **Prospect targets** — `sales weekly` (export + score the new
   batch).
2. **Borderline finalize** — operator-supervised; the agent presents
   borderline verdicts and the operator confirms via the queue UI.
   Auto-finalize happens later in the day via
   `/sales-finalize-borderline`.
3. **Learn** — cohort-measurement step. Not yet shipped as a
   first-class CLI subcommand; skip until that lands. Manual
   inspection of the weekly run's audit log via `sales audit-tail`
   covers the gap in the interim.
4. **Diagnostic** — `sales weekly-diagnostic` (per-cell verdicts).
5. **Brain** — `sales weekly-brain --experiment-id <id>` (proposal
   generation).
6. **Report** — `sales report` (weekly KPI email).

## LLM dispatch budgets

Per §3.7, the steps that dispatch LLM subagents (borderline qualifier,
brain critique, diagnostic critique, synthetic prescreen) each carry
their own per-week ceiling. On `cost_ceiling_breached` for a step,
this skill halts that step and surfaces the typed escalation; later
steps continue if independent.

## Idempotency

Per-machine flock as `/sales-daily`. The Attio `daily_run` cross-
machine guard does NOT apply to weekly bundles — repeat runs of
`/sales-weekly` within the same Monday produce a fresh weekly cohort
each time UNLESS the operator runs `weekly-finalize` for an existing
batch date, in which case batch-date idempotency keys protect the
prospect commits.
