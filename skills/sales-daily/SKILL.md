---
name: sales-daily
description: Run the daily sales check. Phase A = invites (every day), Phase B = DM sequencing (Mon-Fri only). Includes pipeline-starvation evaluation. Skipped under --dry-run.
---

# /sales-daily

Operator-invoked slash command that replaces the daily 9am cron. The
operator runs this each business day; the daily cap policy and
weekend gate are enforced inside the Python entry point.

## Step 0 — Verify Attio MCP scope

Before touching any Attio data, confirm read+list+write+delete scope:

1. Call `whoami` on the Attio MCP — non-error response required.
2. Call `list-lists` — non-error response required.
3. Create a sentinel note on the canary Person record, then delete it.

On any failure, halt and escalate `mcp_scope_insufficient` directly to
the user. Do not proceed to the daily run if scope is incomplete.

## Step 1 — Verify Data Quality halt gate

Before any DM send, check for open P0 alarms in the Operator Review
Queue. Filter `type IN (cohort_tagging_regression,
write_owner_invariant_violated, migration_idempotency_regression)`
AND `status='open'`. If any row exists, halt with the slug list
visible and tell the operator the recovery path (Attio → Operator
Review Queue → resolve or dismiss the row).

## Step 2 — Run the daily check

Invoke:

```bash
sales daily
```

Options:
- `--dry-run` — preview only, skips PB launches AND skips the
  pipeline-starvation check (to avoid opening alarm rows during
  rehearsals).
- `--force-weekend` — override the Mon-Fri DM rule (rarely needed).
- `--skip-dms` — invites only.

**Weekend policy.** Saturday and Sunday: invites are allowed, DMs are
skipped automatically. `--force-weekend` overrides ONLY the DM gate.
Per the operator-policy memory entry, weekend runs should normally
be invites-only.

The script will:
1. Phase 0 — detect newly-accepted connection requests.
2. Phase 0.5 — detect responses to DMs.
3. Pipeline-starvation check (`evaluate_pipeline_starvation`) — opens
   a typed queue row if any of the three triggers fire.
4. Part A — send connection requests with quarantine + degree-check
   gates.
5. Part B — DM1/DM2/DM3 sequencing (Mon-Fri only).

## LLM dispatch budgets

Per §3.7, `industry_classification`, `borderline_verdict`,
`synthetic_prescreen`, `weekly_brain_critique`, `company_hq_classifier`,
`synth_holdout`, `diagnostic_critique`, and the Haiku qualifier
execute as Claude Code subagent dispatches from this parent skill —
no Anthropic API key in any engine process. On `cost_ceiling_breached`,
this skill halts the affected step and logs the typed escalation; the
remainder of the daily run continues if the step is non-critical.

## Idempotency

Per-machine flock (`workflows/run_lock.py`) prevents concurrent runs
of `sales-daily` on the same machine. The Attio `daily_run` object
prevents concurrent runs across machines on the same `(run_date,
machine_id)` key. Exit code 75 (EX_TEMPFAIL) on lock contention.
