# Autosales Research Program

This document defines how the autonomous learning loop operates. The agent reads this file but never modifies it. Only the human edits this document.

This is the equivalent of Karpathy's `program.md` — it tells the agent how to think about experiments, not what experiments to run.

> **Scope:** this file governs DM voice, philosophy, ICP, red lines, and experiment rules. Email voice, the canonical platform pitch, and cross-channel technical rules (Gmail HTML, prospect-local TZ) lived in the original operator's email canon, which is internal and not shipped with this repo — define your own equivalents when deploying the email path.

---

## Philosophy & Tone

This program's outreach is **peer-to-peer, pain-first, and consultative**. We are a manufacturing person talking to a manufacturing person about a real problem they face every day: the production schedule falling apart.

**Voice principles:**
- First person, conversational. Not corporate. Not marketing.
- Lead with the prospect's pain, not our product. The first message should make them nod, not sell.
- Specific beats generic. "3-4 hours a day rebuilding the plan" beats "improve operational efficiency."
- Ask questions, don't pitch. The goal of DM1 is a reply, not a sale.
- Respect their time. DM3 is always a graceful exit — no pressure, no fake urgency.

**Brand red lines (NEVER do these):**
- Never mention pricing in DMs
- Never bash competitors by name
- Never make ROI promises without data to back them up
- Never use fake urgency ("limited spots", "this week only")
- Never send the same message to two people at the same company
- Never mix languages within a message
- Never use corporate buzzwords: "leverage", "synergize", "paradigm", "unlock value"

---

## ICP Definition

This program sells to two distinct ICPs. **ICP 1 is the primary strategic target.** ICP 2 is opportunistic and phasing out — do not build new infrastructure around it. Per-persona details live in `content/personas.json` (`icp_notes` field).

### ICP 1 — Primary (Enterprise/$500M+)

- **Revenue:** $500M+ USD (global or regional parent company)
- **Geography:** All Americas — Mexico, Chile, Peru, Brazil to start; United States by end of 2026. No geography exclusions within the Americas.
- **Manufacturing type:** ALL — discrete, continuous, hybrid/process. Food & bev, pharma, chemicals, automotive, industrial, consumer goods, packaging, metals, mining, aerospace.
- **Structure (required):** Has an ERP (SAP, Oracle, S/4HANA, etc.) + corporate team + local/regional operations + plant operations. Multi-tier hierarchy.
- **Buyer:** Local/country-tier ops leaders — VP Operations, Country GM, Plant Director, Director de Manufactura, COO (country/subsidiary level). **NOT** global/worldwide executives — they don't take cold outreach from LATAM vendors.
- **Decision pattern:** 3–6 month cycle, multi-stakeholder. Real pain, real budget, serious evaluation. IT involvement but local ops drives the decision.
- **Enforcement:** `enterprise_mode=true` in `content/personas.json`. No curated target-company list — wide net via Sales Navigator saved searches (MX + BR live; CL/PE placeholders pending new saved searches).

### ICP 2 — Opportunistic (Mid-Market, Phasing Out)

**Use while it lasts. Do not build new infrastructure around ICP 2.**

- **Revenue:** $50M–$500M USD
- **Employees:** 200–2000 (50–199 OK for capital-intensive verticals like tequila, wine, specialty metalworking)
- **Geography:** Mexico, Colombia, Chile ONLY. Peru and Brazil → ICP 1 lane.
- **Vertical:** Discrete manufacturing — plastics, food & bev, packaging, metalworking, furniture, medical devices, textiles, wine, tequila/brewing
- **Buyer:** Operations Director, Plant Manager, Director de Manufactura, Director General, Gerente General, owner-operator
- **Decision pattern:** Family-owned or founder-led. Decisions in days. Fast pilot capability. Tech adopters.
- **Enforcement:** `target_company_mode=true` in `content/personas.json`, curated target-company list per country (`content/{mx,co,cl}-midmarket-targets.json`).

**Hard disqualifiers (both ICPs):** Non-LATAM multinationals where decisions live at global HQ (but their LATAM plants ARE ICP 1), PE-owned rollups, pure services, consultants, systems integrators, ERP/APS vendors (competitors), academic/research/committee roles, HR/Finance/Marketing without direct operations scope.

**Sales / commercial / marketing / D2C roles are out of ICP — always.** We sell to manufacturing **operations** (the people who own production scheduling). A Sales Director, Director Comercial, Diretor Comercial, VP Ventas, D2C/B2C Director, Account Executive, Business Development lead, Channel Manager, Customer Success lead, Marketing/Growth/RevOps director, or Sales Engineer sits on the **revenue side** of the prospect's business. They are not the buyer of production-scheduling tooling regardless of seniority. Enforced deterministically in `workflows/quality_gate.py::SALES_ROLE_KEYWORDS` and reinforced in the LLM qualifier prompt. **Exception:** Sales & Operations Planning (S&OP) is an ops/supply-chain function — those titles ARE buyers; they pass.

> **Note on large LATAM manufacturers:** Grupo Lala, Arca Continental, Grupo Bimbo, Mabe → ICP 1. Large LATAM-origin multinationals with real plants and local ops structures are **not disqualified**. Automotive Tier 1 local plants (Bosch MX, Continental MX, etc.) → ICP 1.

---

## Experiment Rules

### One variable per experiment

Every experiment changes exactly ONE thing:
- One DM template for one persona/language/step combination, OR
- One scoring weight or keyword list in quality_gate.py

Never change messaging AND scoring simultaneously. Never change DM1 and DM2 in the same experiment.

### Cohort requirements

- **Minimum cohort size:** 15 prospects who have been DM'd (reached DM1_SENT or beyond)
- **Observation window:** 7 days minimum from the first DM1 in the cohort
- **Insufficient data = wait.** The agent never draws conclusions from small or immature cohorts.

### Verdict thresholds

- **Won:** DM response rate > baseline + 2 percentage points
- **Lost:** DM response rate < baseline - 2 percentage points
- **Inconclusive:** Within ±2pp of baseline. Extend observation window or declare tie.

### Simultaneous experiments

Maximum 2-3 experiments running at once, and they must be on **different surfaces**:
- One messaging experiment + one scoring experiment = OK
- Two messaging experiments on different personas = OK
- Two messaging experiments on the same persona = NOT OK (confounded)

### Experiment lifecycle

```
PROPOSE → APPROVE (human) → RUN → MEASURE → VERDICT → PROMOTE or DISCARD
```

The agent proposes. The human approves via PR merge. The agent runs, measures, and reports the verdict. Won experiments become the new baseline. Lost experiments are discarded.

---

## Messaging Principles

These principles guide the agent when generating new DM variants. They are NOT templates — the agent uses them to craft experiments.

### DM1 (The Hook)

**Goal:** Get a reply. Nothing else.

- Open with something specific to their world — a pain point they experience, not a feature we sell
- Ask a question they can answer easily. "How many times a week does your plan fall apart?" is better than "Are you interested in AI scheduling?"
- Keep it under 50 words if possible. Shorter messages get more replies on LinkedIn.
- Reference their company by name. Generic messages feel like spam.

### DM2 (The Bridge)

**Goal:** Connect our solution to their pain. Ask for 15 minutes.

- Reference a real example or outcome. Specificity = credibility.
- Show the transformation: "before [pain], now [outcome]"
- One clear CTA: "send me 2 times that work for you"
- Longer than DM1 is OK — they already connected, they have some interest.

### DM3 (The Exit)

**Goal:** Leave the door open without pressure.

- Short. 2-3 sentences max.
- Explicitly say this is the last message.
- No guilt, no pressure, no "just checking in."
- Warm closing — "whenever you're ready, I'm here."

### Language

- Auto-detect from prospect's location. Match their language.
- Mexico/Colombia/Chile/Spain → Spanish
- Brazil → Portuguese
- USA/Canada/UK → English
- When in doubt, default to the language of their LinkedIn profile.
- Optional `--lang` override exists if auto-detection fails.

---

## Modifiable Surfaces

The agent may propose changes to these files via PR:

| File | What can change | What cannot change |
|------|----------------|-------------------|
| `content/messages.json` | DM copy (wording, hooks, CTAs, tone, length) | Template structure, placeholder format (`[Name]`, `[Company]`), number of steps |
| `workflows/quality_gate.py` | Scoring weights (point values per criterion), score threshold, keyword lists | Function signatures, return format, classification logic structure |

### Everything else is read-only

The agent must NOT propose changes to:
- `cli.py`, `clients/`, `models/pipeline.py` — infrastructure
- `workflows/daily_check.py`, `workflows/dm_sequencer.py` — workflow logic
- `workflows/safety_limits.py` — safety limits are human-set
- `content/personas.json` — persona definitions are human-managed
- This file (`sales-program.md`) — human-only

---

## Attio Schema — response_classification and last_response_text

Two attributes on the **LinkedIn Outreach** list hold the post-reply
classification for every prospect. Both are REQUIRED for the automated
reply classification path (`workflows/response_classifier.py`) to work.

**response_classification** — Single select
- Options: `positive`, `question`, `neutral`, `negative`, `defensive`, `none`
- Default: `none`
- Written by: `workflows/detect_responses.py` when a reply is detected
- Read by: `workflows/learn.py` during cohort measurement

**last_response_text** — Text
- Max length: 1000 chars (truncate in code)
- Written by: `workflows/detect_responses.py` when a reply is detected
- Read by: `workflows/weekly_brain.py` when writing markdown proposals

**Manual setup (one-time):**
1. Open Attio → Lists → LinkedIn Outreach → Settings → Attributes
2. Add attribute `response_classification`, type `Status` (single select), options as above
3. Add attribute `last_response_text`, type `Text`
4. Save

No backfill needed — existing entries default to `none` / empty.

---

## Attio Schema — Scorer Persistence Attributes

Four attributes on the **LinkedIn Outreach** list and two on the **Companies**
object hold per-prospect scoring metadata so future "why did this prospect
score what they did?" questions become a CRM lookup, not a re-derivation.

**On the LinkedIn Outreach list entry:**

| Attribute | Type | Written by | Purpose |
|---|---|---|---|
| `score_breakdown` | Text | `workflows/quality_gate.py:score_prospect` | JSON-encoded per-component scoring breakdown (size, role, competitor, industry, total + reasons). |
| `scoring_lane` | Single select | `workflows/quality_gate.py:score_prospect` | Which scoring lane ran. Options: `target_company_mode`, `enterprise_mode`, `legacy`. |
| `verdict_path` | Single select | `workflows/quality_gate.py:score_prospect` | Which branch decided the verdict. Options: `target_pass`, `enterprise_pass`, `borderline_pass`, `borderline_reject`, `deterministic_reject`. Distinct from the LLM's `icp_lane` integer (1 or 2). |
| `llm_rationale` | Text | `workflows/quality_gate.py::_llm_qualify` (when borderline 40–75 calls Haiku) | Haiku's one-line rationale for the borderline verdict. Empty for deterministic decisions. |

**On the Companies record:**

| Attribute | Type | Written by | Purpose |
|---|---|---|---|
| `industry_source` | Single select | `workflows/industry_classifier.py` and `scripts/apply_industry_classifications.py` | How `industry_vertical` was assigned. Options: `haiku_classifier`, `claude_session`, `pb_scrape`, `manual`. |
| `industry_classified_at` | Date | Same writers as above | When the classification ran. Lets us re-run the classifier on stale rows. |

**Manual setup (one-time):** create the attributes via the Attio UI, OR run
`python3 scripts/setup_attio_schema.py` to provision them via the API. The
script is idempotent — safe to re-run.

**Schema cleanup note:** an orphan `icp_lane` Attio attribute exists on the
LinkedIn Outreach list (created during a mid-implementation rename to
`verdict_path`). Nothing reads or writes it — safe to delete via the Attio
UI when convenient. The Attio API rejected DELETE during the migration
without explicit operator authorization; left in place to keep the change
non-destructive.

**Backfill:** `python3 scripts/dump_companies_needing_industry.py` exports
companies missing `industry_vertical` for offline classification (when no
`ANTHROPIC_API_KEY` is set). The Claude Code operator classifies them and
runs `python3 scripts/apply_industry_classifications.py --input <file.json>`
to write results back. This pairs with the in-process Haiku path in
`workflows/industry_classifier.py:backfill_missing_industries` — use whichever
fits the environment.

---

## Real-Time Response Classification

Every reply detected by `workflows/detect_responses.py` is classified into one of five buckets — `positive`, `question`, `neutral`, `negative`, `defensive` — and written to Attio as `response_classification` alongside `last_response_text` (truncated to 1000 chars).

### Two-stage classifier

1. **LLM-first** (`workflows/response_classifier.py`) — `claude-haiku-4-5` call with the taxonomy cached in the system prompt via `cache_control`. Primary path whenever `ANTHROPIC_API_KEY` is set. Returns `{classification, confidence, reasoning, suggested_action, summary}`.
2. **Keyword fallback** (`workflows/quality_gate.py::classify_response`) — runs when the LLM returns `None` (no key) or raises. Keeps the agent unblocked during API outages and when running locally without a key.

### Stage routing

- `negative` → `NOT_INTERESTED` (hard stop, never retry)
- `defensive` → `RESPONDED` (stop the automated sequence, but preserve the prospect — the reply is reactance, not a decline)
- `positive` / `question` / `neutral` → `RESPONDED` (operator picks it up)

### Why defensive is its own bucket

Reactance replies (e.g. `"estoy segura que no viven en Excel con toda certeza"`) look nothing like polite declines. The prospect is defending the premise, not rejecting the offer. Retrying the same opener variant on a defensive responder doubles down on the reactance. The `rejected_defensive` experiment verdict (see below) uses the aggregate defensive rate to kill variants that trigger reactance at cohort scale.

---

## Experiment Verdicts — `rejected_defensive`

`workflows/learn.py::evaluate_experiments` scores mature cohorts against `baseline_rate ± 2pp` and emits one of four verdicts:

- `won` — `dm_response_rate > baseline + 2pp` AND defensive rate in range
- `lost` — `dm_response_rate < baseline - 2pp`
- `inconclusive` — within ±2pp of baseline
- `rejected_defensive` — `defensive_rate > 2 × BASELINE_DEFENSIVE_RATE` (hardcoded at 0.10 pending the first real cohort). Overrides every other verdict.

`measure_cohorts` returns `classification_breakdown` (counts per bucket) and `per_persona` (same metrics narrowed per sales persona). A variant that wins for one persona and loses for another is visible at that grain, not hidden in an aggregate.

---

## Weekly Brain — Variant Proposals

`workflows/weekly_brain.py` runs during `sales-weekly`. Every Sunday evening it:

1. Loads DM1 variants (`dm1`, `dm1_v1`, `dm1_v3`) from `content/messages.json` for every persona.
2. Runs `workflows/synthetic_prescreen.py::score_variant_matrix` against 3 synthetic evaluator personas (see `content/synthetic_personas.json`):
   - `patricia_defensive_senior` — senior digital lead at global pharma, high reactance risk
   - `family_ceo_curious` — second-gen MX family manufacturer, cautious but open
   - `coo_actively_searching` — actively-buying mid-market COO, easy to engage
3. Scores every `(variant × synthetic persona)` pair on `defensive_likelihood` and `engagement_likelihood`, each returned as a `[min, max]` range (never a point estimate — per DESIGN_SPEC).
4. Pulls defensive reply samples from Attio via `collect_defensive_samples`.
5. Writes a markdown proposal to `docs/experiments/{experiment_id}-proposal.md` following DESIGN_SPEC > Guidelines for Markdown Proposals: decision first, variant copy inline, ranges not points, defensive samples inline.

### CLI

```bash
python cli.py weekly-brain --experiment-id exp-003          # write proposal
python cli.py weekly-brain --experiment-id exp-003 --dry-run  # compute path only
```

### Operator review loop

The human reads the proposal, either accepts the recommended variant or edits it, and runs `sales-approve` to merge the experiment branch. The weekly brain never merges on its own — every copy change ships via a reviewed PR.

---

## Example Experiment Ideas

Seed hypotheses to get the loop started. The agent should exhaust these before generating novel ones.

### Messaging experiments

1. **Informal DM1 for operations_leaders (ES):** Replace the formal question with a more casual opening. Hypothesis: mid-market manufacturing directors in Mexico respond better to informal tone.

2. **Shorter DM1 across all personas:** Cut DM1 to under 30 words. Hypothesis: brevity increases reply rates on LinkedIn.

3. **Company-specific reference in DM1:** Add a detail about the prospect's company (from Attio or web research). Hypothesis: personalization beyond `[Company]` name increases response rate.

4. **Different CTA in DM2:** Replace "send me 2 times" with "I can send you a 2-minute video of how it works." Hypothesis: lower commitment CTA gets more replies.

5. **Remove DM3 entirely:** Skip the third message. Hypothesis: DM3 adds no incremental responses and may annoy prospects.

### Scoring experiments

6. **Lower threshold from 60 to 50:** Let more prospects through. Hypothesis: we're being too selective and missing good fits.

7. **Add "lean manufacturing" keywords:** Extend operations keyword list. Hypothesis: people with lean/kaizen backgrounds are a strong ICP signal we're missing.

8. **Boost domain influencer score:** Increase the 24-point score for domain influencers to 28 (matching decision-makers). Hypothesis: innovation managers are as good as directors for this product's sell.

---

## Reporting

The agent logs all experiments to `experiments.tsv` (append-only, tab-separated). The human reviews this file weekly for patterns.

When proposing a PR, the agent must include:
1. **Hypothesis** — what we're testing and why
2. **Data** — response rates for relevant cohorts
3. **Change** — what specifically changed (visible in the diff)
4. **Next** — what we'll try if this wins or loses

The PR description is the experiment's documentation. Write it for a human who wants to understand the reasoning, not just the result.
