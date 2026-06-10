---
name: onboard
description: Stand the outbound engine up against a new operator's CRM, PhantomBuster account, and ICP. Interviews the operator, generates + conformance-tests a non-Attio CRM adapter when needed, writes config/*.yaml + .env, and verifies with a dry run.
---

# /onboard

The install-time capstone. Run this once, per new deployment, to take the
outbound engine from a fresh checkout to a running, operator-specific install.
It is an **interview + orchestration** skill: it asks the operator what their
CRM / PhantomBuster / ICP are, then wires the engine's existing seams and config
artifacts to match — it does not change the engine.

## What it produces

By the end of a successful run:

- `config/crm.yaml` — CRM vendor + (when consumed) field/stage mapping.
- `config/phantombuster.yaml` — phantom (agent) IDs + degree-check backend.
- `config/icp.yaml` — the operator's full ICP (keywords, weights, thresholds,
  the LLM qualifier prompt slots).
- `.env` — every secret (API keys, LinkedIn cookies, user-agent), referenced
  **by name** from the yaml files, never inlined into them.
- For a **non-Attio CRM:** a new `clients/crm/<vendor>_provider.py` adapter,
  registered in `clients/crm/factory.py`, **proven correct** by passing the
  conformance suite `tests/crm/test_provider_contract.py`.

## The four phases

1. **CRM hookup** — pick the vendor, gather credentials. Attio uses the
   reference adapter as-is; any other CRM gets an adapter **generated** against
   `clients/crm/base.py` + `clients/crm/CONTRACT.md`, using
   `clients/crm/fake_provider.py` as the structural template (vendor-free,
   no SDK) and `clients/crm/attio_provider.py` only as the worked example of how
   a real vendor's wire JSON gets normalized — its `{"value": ...}` envelope is
   an Attio quirk, not the contract — and **iterated until green** against
   `tests/crm/test_provider_contract.py`.
2. **PhantomBuster setup** — create the phantoms, capture IDs + cookies, choose
   the degree-check backend, validate.
3. **ICP co-definition** — interview the operator, write their ICP, render the
   qualifier prompt, score sample prospects, iterate until they're satisfied.
4. **Verify** — run the daily flow in `--dry-run` against a small sample and
   report readiness.

## How to run it

Follow **[`references/onboarding-spec.md`](references/onboarding-spec.md)** —
the detailed, machine-followable procedure. It is self-contained: an agent with
no prior knowledge of this repo can execute it end to end. Every step cites the
exact file path or command it touches.

Work the phases **in order** — each builds on the last (the CRM must resolve
before the dry run can read the pipeline; the ICP must load before scoring).
Do not skip the conformance-suite gate in Phase 1 for a non-Attio CRM: a
generated adapter is "done" **only** when that suite is green.

## Honest limitations (read before onboarding a non-Attio CRM)

- **Filter-DSL leak.** `query_object_records(filters=…)` passes the filter body
  through to the adapter as a *vendor-native query* (Attio's DSL today). A
  non-Attio adapter must translate it, or callers that pass a filter stay
  Attio-shaped. Documented in `clients/crm/CONTRACT.md`.
- **Write-path is Attio-coupled today.** `clients/attio_writer.py`'s
  atomicity/`_compensating_rollback` model and its raw-httpx PATCH fallback for
  object types without a typed helper are Attio-specific. The provider seam
  covers the typed reads/writes; the writer's exception model is not yet
  vendor-neutral.
- **Field/stage mapping in `config/crm.yaml` is not yet consumed.** Stage labels
  + field slugs still resolve in code (`models/pipeline.py`, the adapter). Fill
  the mapping for forward-compatibility, but know it is currently inert.

The spec restates each of these at the step where it bites.
