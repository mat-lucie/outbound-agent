# config/

Operator configuration for the outbound engine.

## Convention

Every config file has an **example template** committed to the repo (`*.example.yaml`)
and a **live copy** that is git-ignored (`*.yaml`). To configure the engine:

1. Copy the example file: `cp foo.example.yaml foo.yaml`
2. Edit the live copy with your values.
3. Never commit `foo.yaml` — it is in `.gitignore`.

## Secrets stay in environment variables

**Do not put secrets (API keys, cookies, tokens) in YAML files.**

YAML files hold the *name* of the environment variable that contains the secret:

```yaml
# config/crm.yaml
credentials:
  api_key_env: ATTIO_API_KEY    # ← the env var name, not the value
```

The actual value lives in `.env`:

```
# .env  (git-ignored)
ATTIO_API_KEY=your-secret-key-here
```

The `clients/settings.py` loader reads the env-var name from YAML and resolves
it from `os.environ` — so secrets never appear in config files or version control.

## Config directory override

By default the engine loads config from `<repo_root>/config/`. To use a different
directory (e.g. in tests or multi-instance setups), set:

```
OUTBOUND_CONFIG_DIR=/path/to/your/config
```

## Files

| File | Purpose |
|------|---------|
| `crm.yaml` | CRM vendor, field/stage mapping, credential env refs (P1) |
| `icp.yaml` | ICP keyword lists, score weights, thresholds, disqualifiers, geography + the `qualifier_prompt:` slots for the LLM tiebreaker prompt. Consumed by workflows/quality_gate.py via ICPConfig; falls back to icp.example.yaml when absent. The shipped `icp.example.yaml` is a **neutral, generic-B2B template** — replace it with your own ICP. A fully worked reference (the synthetic Acme example operator) lives at `examples/acme/config/icp.yaml`. |
| `prompts/qualifier.md.j2` | LLM qualifier system-prompt Jinja2 template (P2b). Renders the `qualifier_prompt:` slots (product summary, geography requirement, ICP lanes, disqualifier bullets, and the two `lane_labels` glosses in the `icp_lane` line) around the STATIC JSON output contract. The contract is engine-owned and stays in the template — only the ICP narrative is operator-tunable. Rendered with `undefined=StrictUndefined`, so a typo'd template variable raises rather than silently rendering blank. A live `prompts/qualifier.md.j2` in your `OUTBOUND_CONFIG_DIR` overrides the shipped one (same fallback rule as the YAML). |
| `outreach.yaml` | Operational knobs: daily invite/DM caps, invite batch size, DM cadence intervals, post-DM3 nurture cadence, invite-queue lane priority, weekend send-days, and the per-company throttle window. Consumed via `clients/outreach_config.py` (`load_outreach_config()`); falls back to `outreach.example.yaml` when absent. Edit this to tune *how much / how often / which-first* you reach out. (Timezone lives in the `OUTBOUND_TZ` env var, not here.) |
| `phantombuster.yaml` | PhantomBuster phantom (agent) IDs + `pre_invite_degree_check_backend` (P3). Consumed by `clients/pb_config.py` via `PBConfig`; resolution is yaml → env var → code default, so an ABSENT `phantombuster.yaml` falls back to the `PB_*` env vars (current-deploy behavior). Secrets (API key, cookies, user-agent) stay env-only — the `credentials:` block only names them. First-time setup: `docs/onboarding/phantombuster-setup.md`. |
