# Wave-2-D Runbook: 14-company throttle-violation triage

**Status:** Action required by Mat (operator). The diagnostic script in this PR is read-only; the actual triage requires per-company judgment + Attio-UI writes.

## Background

`done-qa-gtm-pass1-round2` finding **GTM-MIN-5** flagged that the **PR-13 per-company throttle** has been **non-functional in production** because the Companies attributes (`last_outreach_at` + 3 siblings) were never deployed to Attio:

- The code path is correct: `workflows/throttle.py` reads `last_outreach_at`, `workflows/daily_check.py` writes it after every confirmed send.
- The schema path is broken: `values.get("last_outreach_at")` always returns `None`, so `workflows/throttle.py:158` returns "permit" for every call. The throttle is dead.

PR-A (`#127`, merged) deploys the missing attributes. PR-B (`#129`) routes the throttle-tally write through `AttioWriter`. Once those land, the throttle **starts working for future runs**. But the historical violations need to be triaged BEFORE the next outreach cycle, otherwise:

- `last_outreach_at` is `NULL` on the affected companies → throttle still permits the next send → Mat sends a third DM to a company that's already received two.

## Brand red line being violated

`sales-program.md:27`: **"Never send the same message to two people at the same company."** This is a **soft red line** (lighter than the §3.1 no-resend-to-same-person hard red line, which IS enforced via `dm_step` + `cross_channel_suppression`), but Mat has flagged it as brand-damaging.

## The 14 affected companies

Source: `done-qa-gtm-pass1-round2.json::attio_data_cross_checks.per_company_throttle_violations_observed`. Method: `mcp__attio__run-basic-report` on `linkedin_outreach`, grouped by `parent_record→people.company`, filtered to `dm_step >= 1 AND last_contact_date >= 2026-05-01`.

### Count = 3 (3 companies)

These companies have **3 active multi-person DMs** in the last 30 days. Highest brand risk — at least two contacts must be paused.

| Company record_id | Action required |
|---|---|
| `3c29c5bb-94f4-4f4d-94d2-341f9bccb356` | Triage: keep 1 contact (highest `quality_score` or already in `DM3_SENT`); flip other 2 to `NURTURE` |
| `c9e34db5-9eee-5a77-aff5-6389d22ecf22` | Triage: keep 1 contact; flip other 2 to `NURTURE` |
| `b57796a1-ed60-5d79-bfca-980557773179` | Triage: keep 1 contact; flip other 2 to `NURTURE` |

### Count = 2 (11 companies)

These have **2 active multi-person DMs**. One must be paused.

| Company record_id |
|---|
| `1563c1c2-1fb1-5bb6-aedd-7e33a41bf2c5` |
| `54fa12b7-71af-4814-b20a-3ca66b2dc0d5` |
| `398144b1-cc45-593f-844f-843aefbe8eb6` |
| `987c3d79-b689-4d07-af4c-5b45eb14f7ed` |
| `cf080b92-9687-48bf-9153-90a4a48f1a26` |
| `99fc5736-7fbd-4065-9c19-8f1d83ec6546` |
| `dce1e821-3f8b-54a0-9434-5b54b3215c3f` |
| `801b9371-d4b7-4d7c-8ce3-e9023303d1dd` |
| `7a309314-785c-5cb8-80b1-ae66bc23ae73` |
| `5c39cbd7-9de9-4c50-8f6e-696bf960a709` |
| `cabca447-a224-472b-b064-c601f5f3db4a` |

For each: pick **1 contact** (by stage rank or quality_score) and flip the **other contact** to `NURTURE` stage.

## Triage procedure

### Step 1 — Fetch the current state (read-only, safe to run)

```bash
python3 scripts/diagnose_throttle_violations_20260525.py
```

This prints, for each company:
- Company name + record_id
- All LinkedIn Outreach entries linked to it (entry_id, person name, stage, dm_step, last_contact_date, quality_score, persona)
- A recommended **keep / pause** assignment per Mat's rules:
  - Keep the entry with the highest `dm_step` (already deepest in the funnel)
  - Tiebreak by highest `quality_score`
  - Final tiebreak: oldest `last_contact_date` first (more committed to the cadence — deterministic AND semantically meaningful)

The script does **NOT write to Attio**. Output is a JSON file + a stdout summary.

### Step 2 — Mat reviews + decides per company (manual judgment)

Read the JSON output. For each company:

1. **If the recommendation looks right** (the kept contact is the strongest buyer per Mat's read of name + title): note the entry_id to keep.
2. **If the recommendation looks wrong** (e.g. Mat has personal context about which buyer is more responsive): override per Mat's judgment.

Document the per-company decision in `exports/triage_throttle_violations_20260525.json` (the diagnostic script writes a template; Mat appends `keep_entry_id` + `pause_entry_ids` per row).

### Step 3 — Apply the pause (Attio UI or one-off script)

**Recommended path: Attio admin UI.** For each `pause_entry_ids`:

1. Open the LinkedIn Outreach record in Attio
2. Flip `stage` to **NURTURE** (per `sales-program.md` cadence rules for stale-but-not-rejected contacts)
3. Add a note: `2026-05-25 throttle-violation triage — paused; same-company contact at <keep_entry_id> is the primary buyer`

**Alternative path: scripted batch.** If Mat wants to automate (e.g. >20 contacts to pause and the UI is slow), write `scripts/triage_throttle_violations_20260525.py` following the PR-C pattern (MigrationRunWriter wrap, AttioWriter dispatch, dry-run flag). This script is **NOT shipped in PR-D** — only write it when Mat explicitly asks.

### Step 4 — Backfill `last_outreach_at` on Companies

After the pauses land, the historical throttle state needs to seed so the next daily run reads the correct `last_outreach_at`. Run the existing backfill script:

```bash
# Dry-run first to verify the planned writes:
python3 scripts/backfill_per_company_outreach_state.py --dry-run

# Review the output; if it looks right:
python3 scripts/backfill_per_company_outreach_state.py --apply
```

The script (already wrapped in `MigrationRunWriter` since PR-13) walks every Company, finds the most-advanced LinkedIn Outreach entry linked to it, and stamps `last_outreach_at` + 3 siblings. **Idempotent**: a second consecutive run shows `rows_modified=0`.

After the backfill, the next `/sales-daily` invocation will respect the throttle for all 14 companies (and every other company in Attio).

### Step 5 — Verify

Re-run the original detection query to confirm 0 violations remain:

```python
# Equivalent of the gtm audit's report:
# - LinkedIn Outreach where stage in {DM1_SENT, DM2_SENT, DM3_SENT}
#   AND last_contact_date > now() - 30d
# - GROUP BY parent_record→people.company
# - HAVING count >= 2
```

Expected outcome: **0 rows**. If any rows remain, repeat Step 1–3 for those companies.

## Rollback

- **Pause (`stage → NURTURE`):** fully reversible via the Attio UI — flip back to the prior stage. Use the entry's history tab if you don't remember what stage it was on before the triage.
- **Backfill (`last_outreach_at` PATCH):** **not trivially reversible.** Attio's API doesn't have a "clear typed attribute back to NULL" primitive in the same shape as writing a value; the practical reversion is either (a) PATCH the attribute to the prior value (which you'd need to have snapshotted before running Step 4), or (b) clear the attribute through the Attio admin UI. If true rollback might matter, take a snapshot of the affected Companies records BEFORE invoking `scripts/backfill_per_company_outreach_state.py --apply`.

The Migration Run row from Step 4 carries the `record_id` of every Company touched along with the new value — so even without a snapshot, the rollback targets are auditable from the Migration Run audit trail. But that audit trail is a starting point for a manual revert, not an automatic undo button.

## What this PR contains

- `scripts/diagnose_throttle_violations_20260525.py` — read-only diagnostic.
- `docs/runbooks/wave-2-D-throttle-violation-triage.md` — this document.
- **No mutation scripts.** The triage script is intentionally NOT shipped; Mat decides UI-vs-script when reviewing the diagnostic output.

## References

- gtm audit: internal QA finding GTM-MIN-5
- Brand red line: `sales-program.md:27`
- Throttle code: `workflows/throttle.py`, `workflows/daily_check.py::_write_company_throttle_tally`
- Backfill script: `scripts/backfill_per_company_outreach_state.py` (already MigrationRunWriter-wrapped per PR-13)
- Wave-2 plan: `~/.claude/plans/abstract-twirling-cocoa.md` §3.8 + §3.13
