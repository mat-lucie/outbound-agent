# Onboarding spec — stand the outbound engine up for a new operator

This is the detailed, machine-followable procedure behind the `/onboard` skill.
It is written so an agent with **no prior knowledge of this repo** can execute it
end to end. Every step cites the exact file path or command it touches; verify
each path exists (`ls`/`cat`) before relying on it.

## What this procedure is

The outbound engine ships pre-built. Onboarding does **not** modify the engine,
the CRM contract, the adapters' contract, the config loaders, or any workflow.
It **orchestrates** the existing seams:

- the CRM seam (`clients/crm/`),
- the config convention (`clients/settings.py` + `config/*.yaml` + `.env`),
- the ICP loader (`workflows/icp_config.py`),
- the PhantomBuster loader (`clients/pb_config.py`),
- the daily dry-run (`cli.py`).

The one exception, for a **non-Attio CRM only**, is that this procedure
**generates a new adapter file** (`clients/crm/<vendor>_provider.py`), registers
it in the factory, and adds it to the conformance fixture. That is new code the
onboarding run writes; it does not edit the existing contract or the existing
Attio/Fake adapters.

## Ground rules (apply to every phase)

1. **Secrets live in `.env`, never in YAML.** Every `config/*.yaml`
   `credentials:` block names the **env var** that holds the secret; the value
   stays in `.env`. The loader resolves the name → value
   (`clients/settings.py:resolve_env_ref`). See `config/README.md`. Never write
   a raw key/cookie/token into a `.yaml`.
2. **Live config is gitignored; only `*.example.yaml` is tracked.** You always
   `cp foo.example.yaml foo.yaml`, then edit the live copy
   (`config/README.md` → Convention).
3. **The config loader fails loud.** `clients/settings.py:ConfigError` is raised
   on a missing/empty required value — there is no silent default that masks a
   misconfig. When a step "verifies," a clean exit means the loader accepted the
   config.
4. **Work the phases in order.** Phase 4's dry run reads the CRM (Phase 1),
   PhantomBuster IDs (Phase 2), and ICP scoring (Phase 3); none of it works
   until the earlier phases land.
5. **Interview, don't assume.** Ask the operator for each value. Do not invent
   API keys, phantom IDs, or ICP keywords.

First, copy the env template if it is not already present:

```bash
cp .env.example .env      # only if .env does not exist yet
```

---

## Phase 1 — CRM hookup

**Goal:** the engine's CRM factory (`clients/crm/factory.py:get_crm_provider`)
constructs a working provider for the operator's CRM, and reads/writes round-trip
against their system of record.

### 1.0 — Read the seam first

Before touching anything, read these so you understand the shapes you are
wiring:

- `clients/crm/base.py` — the `CRMProvider` ABC and the normalized dataclasses
  (`Record`, `Entry`, `Stage`, `RecordInfo`). This is the contract every adapter
  must satisfy.
- `clients/crm/CONTRACT.md` — the method-by-method map from the ABC to the
  reference Attio methods, the generic object-record API, and the documented
  **filter-shape leak**.
- `clients/crm/fake_provider.py` — the in-memory reference and the **PRIMARY
  structural template** for a new adapter: a vendor-free `CRMProvider` whose
  method bodies show exactly which dataclass each method returns, with **no
  vendor-shape baggage** to copy by accident. Scaffold your adapter from this.
- `clients/crm/attio_provider.py` — the **reference real-vendor adapter**. Read it
  to see how a live vendor's wire JSON gets normalized into the dataclasses (the
  technique), but treat its `{"value": ...}` / `values`-list unwrap as an **Attio
  quirk, not the contract** — model your own vendor's JSON shape instead.
- `clients/crm/factory.py` — `get_crm_provider()` / `CRMBundle`: how a vendor is
  selected from `config/crm.yaml`.
- `config/crm.example.yaml` — the CRM config template.
- `tests/crm/test_provider_contract.py` — **the conformance suite.** Read its
  module docstring: *"Any new `CRMProvider` implementation MUST be added to the
  `provider` fixture and pass this suite."* This is the adapter gate.

### 1.1 — Ask which CRM

Ask the operator: **which CRM is your system of record?** Offer:
Attio / HubSpot / Salesforce / Pipedrive / other.

Then gather their credentials. **Store every secret in `.env`** and reference it
by name from `config/crm.yaml`. At minimum you need the vendor's API key (or
OAuth token); most CRMs also need a base URL or account/instance id, and the
engine needs the id of the **pipeline list** the cadence runs on.

### 1.2a — If the CRM is Attio

The reference `AttioProvider` is used **as-is** — no code generation.

1. Copy the template and set the vendor:
   ```bash
   cp config/crm.example.yaml config/crm.yaml
   ```
2. In `config/crm.yaml`, keep `vendor: attio` and the `credentials:` block:
   ```yaml
   vendor: attio
   credentials:
     api_key_env: ATTIO_API_KEY
     list_id_env: ATTIO_LIST_ID
   ```
   (These name env vars; do not inline the key.)
3. Put the secret + list id in `.env`:
   ```
   ATTIO_API_KEY=<the operator's Attio API key>
   ATTIO_LIST_ID=<the pipeline list id>
   ```
4. Verify the factory builds the provider (no write, just construction). This
   standalone `python -c` does **not** load `.env` for you (`clients/settings.py`
   never calls `load_dotenv()` — only `cli.py` does), so load it first or a
   correctly-filled `.env` still raises `ConfigError`:
   ```bash
   python -c "from dotenv import load_dotenv; load_dotenv(); \
   from clients.crm.factory import get_crm_provider; \
   b = get_crm_provider(); print('vendor ok:', type(b.provider).__name__)"
   ```
   A clean print of `AttioProvider` means the factory resolved the config and
   the key. A `ConfigError` names the missing piece — fix `.env`/yaml and rerun.

Skip to **1.4**.

### 1.2b — If the CRM is NOT Attio: generate an adapter

You will **write a new adapter** `clients/crm/<vendor>_provider.py` (e.g.
`hubspot_provider.py`) that implements **every** abstract method of
`CRMProvider` (`clients/crm/base.py`), register it in the factory, add it to the
conformance fixture, and **iterate until the full conformance suite is green.**
That passing suite is the gate that the adapter is correct.

**Step 1 — scaffold from `fake_provider.py`, cross-reference `attio_provider.py`.**
Use **`clients/crm/fake_provider.py` as your structural template**: it is the
in-memory / mock-backed `CRMProvider` (no vendor SDK) and its method bodies show
exactly which dataclass each method must build and return — the cleanest skeleton
to copy method-for-method. Then **read `clients/crm/attio_provider.py` to see how a
REAL vendor's wire shape gets normalized** into those same dataclasses (the
`_to_record` / `_to_entry` normalizers, the 404→`None` mapping, the list-unwrap),
and `clients/crm/CONTRACT.md`'s "Method map" table for what each method must do.

> **Warning — Attio's envelope is an ATTIO QUIRK, not the contract.** Attio wraps
> every attribute value in a `{"value": ...}` object and returns most fields as a
> `values` **list** that the adapter unwraps to a scalar. That `{"value": ...}` /
> `values`-list unwrap is Attio's JSON shape — it is **not** part of the
> `CRMProvider` contract. Do **not** carry it into your adapter: model **YOUR
> vendor's actual JSON shape** and normalize *that* into `Record`/`Entry`/
> `RecordInfo`. (Scaffolding from Attio first actively misleads — a cold-agent dry
> run that started from `attio_provider.py` reproduced Attio's envelope against a
> vendor that doesn't use it. Start from `fake_provider.py`, which has no
> vendor-shape baggage, and consult Attio only for the *normalization technique*.)

For each abstract method on `CRMProvider`, the CONTRACT.md "Method map" table tells
you what it does, `fake_provider.py` shows the dataclass to return, and the
`AttioProvider` body shows how a real vendor payload gets normalized: call the
vendor's API, then **normalize** the response into the contract's dataclasses.
The methods you must implement (all `@abc.abstractmethod` in `base.py`):

- People: `search_people`, `get_person`, `bulk_fetch_persons`,
  `search_person_by_linkedin`, `create_person`, `update_person`, `upsert_person`.
- Companies: `search_companies`, `get_company`, `create_company`,
  `update_company`, `search_company_by_domain`.
- Deals: `get_deal`, `create_deal`, `update_deal`, `search_deals`.
- List entries (the pipeline): `query_list_entries`, `add_list_entry`,
  `update_list_entry`.
- Notes: `create_note`.
- Generic object API: `query_object_records`, `get_object_record`,
  `create_object_record`, `update_object_record`.
- Normalized read: `extract_person_info`.

**Step 2 — honor the contract's invariants** (these are exactly what the suite
asserts; see `tests/crm/test_provider_contract.py`):

- **Reads return normalized dataclasses**, never raw vendor JSON. A person/
  company/deal read returns a `Record` (with `object` set to
  `"people"`/`"companies"`/`"deals"`); a pipeline read returns an `Entry`;
  `extract_person_info` returns a `RecordInfo`. Always set `Record.raw` /
  `Entry.raw` to the untouched vendor payload (the escape hatch callers rely on).
- **Missing id → `None`, never an exception.** `get_person` / `get_company` /
  `get_deal` / `get_object_record` return `None` for a record that does not
  exist (a vendor 404 maps to `None`; transport/5xx errors propagate).
- **Create/update round-trip.** A written value reads back via the matching
  `get_*`. `update_*` patches only the named fields and leaves the rest.
- **`upsert_person` is idempotent on the match key.** Repeated upserts with the
  same `matching_attribute` value update the one record, never create a
  duplicate.
- **`add_list_entry` is idempotent on `(record_id, list_id)`** and, when legacy
  duplicate entries exist, patches the **most-advanced one by stage rank** and
  leaves the rest. Map the vendor's stage encoding to `Stage(name, rank)`; an
  unrecognized stage string resolves `rank=None` (callers treat it as
  rank-0/oldest — never crash on an unknown stage). The canonical stage names +
  ranks come from `models/pipeline.py` (`PipelineStage` + `STAGE_RANK`); use
  those, exactly as `AttioProvider` does.
- **`extract_person_info` returns `""` (not `None`) for a blank `linkedin_url`
  and a blank `title`**, but `None` (never a sentinel string) for a missing
  `name`/`company`/`industry`. This `""`-when-blank behavior is load-bearing for
  downstream truthiness checks — preserve it.
- **No silent field drops.** If a write references a field the vendor schema does
  not know, raise rather than silently discarding it (`base.py` general contract
  notes).
- **Harden normalization against empty/malformed responses.** Follow the
  `AttioProvider` precedent: its `_to_record`/`_to_entry` tolerate empty/non-dict
  vendor returns instead of throwing `AttributeError` (see the
  attio_provider.py docstring "6a hardening" note). Your normalizers must not
  crash on a blank or partial payload.

**Step 3 — handle the filter-shape leak.** `query_object_records`' `filters` /
`sorts` and `search_*`' `filter_` are passed through as a **vendor-native query
body** — the contract does **not** define a neutral filter DSL (documented in
`clients/crm/CONTRACT.md` → "Known limitation — the filter-shape leak"). Two
honest options, surface the choice to the operator:
  - translate the Attio-shaped filter bodies callers pass into your vendor's
    query language inside the adapter, OR
  - accept that callers passing a `filters` body stay Attio-coupled until the
    filter DSL is genericized.
The conformance suite only exercises a **flat-equality** filter
(`{slug: value}`), which `FakeProvider` models as a minimal equality subset —
implement at least that much so `test_query_applies_equality_filter_and_normalizes`
passes.

**Step 4 — keep vendor quirks INSIDE the adapter.** Anything vendor-specific
(URL canonicalization, data-quality guards, concurrency knobs like
`max_workers`) lives in your adapter, never on the ABC — mirroring how
`AttioProvider` keeps Clearbit-corruption detection and LinkedIn-URL variants
internal (`clients/crm/CONTRACT.md` → "Deliberate exclusions").

**Step 5 — export + register.**
1. Add your adapter to `clients/crm/__init__.py`'s imports and `__all__`
   (mirror the `AttioProvider`/`FakeProvider` lines).
2. Register the vendor in `clients/crm/factory.py:get_crm_provider`. Today it
   handles `vendor == "attio"` and raises `ConfigError("Unknown CRM vendor …")`
   for anything else. Add a branch for your vendor that constructs your adapter
   from `config/crm.yaml` (resolve the credential env refs via
   `resolve_env_ref`, exactly as the Attio branch resolves `api_key_env`), and
   return a `CRMBundle(provider=<your adapter>, attio=None)`. (`attio` is `None`
   for any non-Attio vendor — it is the transition-only raw inner client.)
   While you are in `factory.py`, fix any stale "only attio is supported" wording
   in its module docstring and in the final `ConfigError` message's
   "Supported vendors:" list so they name your vendor too (see also §1.3).
3. **Add a factory-gate test and run it.** `tests/test_crm_factory.py` is the
   **factory conformance gate** — today it pins three behaviors via
   `get_crm_provider()`: the no-config default, an explicit `vendor: attio`
   build, and an unknown vendor raising `ConfigError`. Add a per-vendor test
   class for your vendor that mirrors `TestWithConfig.test_vendor_attio_builds_provider`:
   point `OUTBOUND_CONFIG_DIR` at a `tmp_path`, write a `config/crm.yaml` with
   `vendor: <your-vendor>` + your `credentials:` block, set the credential env
   vars (via `monkeypatch.setenv`), patch your vendor's SDK/client class the same
   way the Attio tests patch `clients.attio.AttioClient`, call
   `get_crm_provider()`, and assert it returns a `CRMBundle` whose `provider` is
   your adapter (and `attio is None`). Add a second test asserting a **missing
   required credential raises `ConfigError`** (omit the env var; expect the loud
   failure). Then run the gate:
   ```bash
   python -m pytest tests/test_crm_factory.py
   ```
   Green here proves the factory resolves your `config/crm.yaml` → adapter wiring,
   the layer **above** the per-method conformance suite of Step 6.

**Step 6 — add to the conformance fixture and iterate until green.** This is the
gate.
1. In `tests/crm/test_provider_contract.py`, add your vendor to the `provider`
   fixture's `params` list and its construction branch (the fixture currently
   builds `FakeProvider` for `"fake"` and `AttioProvider` over a stateful mock
   for `"attio"`). For a real vendor you will typically back the adapter with a
   stateful in-memory mock of its client (see the `_StatefulAttioClient`
   pattern in that file) so create→get→update chains round-trip without network.
2. Extend the `_seed_duplicate_entry` helper with a seeding path for your
   provider — the suite needs to inject a legacy duplicate entry directly into
   your backing store to exercise "most-advanced duplicate wins" (the public API
   forbids creating duplicates, so the test seeds one). The helper already
   raises `AssertionError(f"no seeding path for provider …")` for an unknown
   provider — add your branch.
3. Run the suite and **iterate until it is fully green**:
   ```bash
   python -m pytest tests/crm/test_provider_contract.py
   ```
   Every red test names a contract invariant your adapter has not yet satisfied.
   Fix the adapter (not the test's intent) and rerun. **The adapter is "done"
   only when this suite passes for your vendor param.**

#### Conformance suite — what it does and doesn't check

The suite has **real teeth**: it catches missing-id→`None` (not an exception),
blank→`""` vs missing→`None` in `extract_person_info`, create/update round-trip,
`upsert_person` / `add_list_entry` idempotency, "most-advanced duplicate wins" on
stage rank, and bulk-fetch leakage of unknown ids. A green run is strong evidence.

But a green run is **not** total coverage. The cold-agent dry run that generated a
passing non-Attio (Pipedrive) adapter found these invariants are documented in the
contract yet **NOT asserted** by the suite — so a buggy adapter can still pass.
**Verify each one by hand against your adapter before you go live:**

- **(S2) "No silent field drops" is unchecked.** The contract (`base.py`) says a
  write referencing a field the vendor schema does not know must **raise**, not
  silently discard it — but no test exercises an unknown-field write. Verify your
  adapter raises (write a throwaway record with a bogus field slug and confirm it
  errors rather than dropping the field).
- **(S3) Filters are only tested at flat-equality.** The suite exercises a single
  `{slug: value}` equality filter (`test_query_applies_equality_filter_and_normalizes`).
  The **nested Attio-DSL** shapes real callers pass (`$and` / `$or` / `$not` /
  operator dicts — the documented filter-shape leak) are **not** tested. If your
  CRM needs those callers, translate and **test the nested filters yourself**.
- **(S4) `bulk_fetch_persons` partial-outage `metrics` counters are unchecked.**
  The suite does not assert the requested/returned/failed counters on a partial
  outage. If you rely on those for observability, verify your adapter bumps them
  correctly when some ids 404 and others succeed.
- **(S5) `create_note` `parent_object` linkage is not round-tripped.** No test
  asserts the note's `parent_object` / parent-record linkage survives the write.
  If notes must attach to people vs companies vs deals correctly, verify the
  linkage round-trips in your vendor.

These four invariants are **on you** — the suite passing does not cover them.
(Closing these gaps in the suite itself is future work; this callout only flags
them so a cold operator checks them manually.)

### 1.3 — Write `config/crm.yaml` for the non-Attio CRM

```bash
cp config/crm.example.yaml config/crm.yaml
```

Set `vendor: <your-vendor>` and a `credentials:` block naming your env vars
(then put the values in `.env`). Capture the operator's **field/stage mapping**
into `field_mapping:`, `stage_field:`, and `stage_mapping:`.

> **Update the stale "only attio" comments.** When you add a vendor, fix the
> now-inaccurate "only attio" wording in two tracked files so the next operator
> isn't misled: the header comment in `config/crm.example.yaml` (the line about
> which adapter the engine builds) and the module docstring + the
> "Supported vendors:" list in the unknown-vendor `ConfigError` of
> `clients/crm/factory.py`. Both should name your vendor alongside attio.

> **Honest limitation:** as of the current CRM-seam increment, `field_mapping`,
> `stage_field`, and `stage_mapping` in `config/crm.yaml` are **NOT YET
> CONSUMED** — field slugs and stage labels still resolve in code
> (`models/pipeline.py` + the adapter's normalization). Fill them for
> forward-compatibility and as the operator's record of intent, but know editing
> them has no runtime effect today (stated verbatim in
> `config/crm.example.yaml`). Your adapter's normalization is where the mapping
> actually happens right now.

### 1.4 — Capture the field/stage mapping (Attio too)

Whether Attio or generated, record the operator's mapping of engine field names
→ their CRM's field slugs, and engine stages → their stage labels, in
`config/crm.yaml` (the `field_mapping` / `stage_field` / `stage_mapping`
sections). Same caveat as 1.3: documentation-of-intent today, consumed later.

### Phase 1 — what success looks like

- [ ] `config/crm.yaml` exists with the operator's `vendor` and a `credentials:`
      block (env-var names only).
- [ ] All secrets are in `.env`, not the yaml.
- [ ] `python -c "from dotenv import load_dotenv; load_dotenv(); \
      from clients.crm.factory import get_crm_provider; \
      print(type(get_crm_provider().provider).__name__)"` prints the expected
      provider class with no `ConfigError`.
- [ ] **(Non-Attio only)** `python -m pytest tests/crm/test_provider_contract.py`
      is **green** with your vendor added to the `provider` fixture.
- [ ] **(Non-Attio only)** the vendor is registered in
      `clients/crm/factory.py` and exported from `clients/crm/__init__.py`, and
      the stale "only attio" wording in `factory.py` + `config/crm.example.yaml`
      is updated to name your vendor.
- [ ] **(Non-Attio only)** `python -m pytest tests/test_crm_factory.py` is
      **green** with a per-vendor test asserting `get_crm_provider()` builds your
      adapter and that a missing required credential raises `ConfigError` (the
      factory conformance gate).

---

## Phase 2 — PhantomBuster setup

**Goal:** the engine resolves the operator's phantom IDs + degree-check backend
via `clients/pb_config.py:load_pb_config`, and the cookies/API key are in `.env`.

Walk **`docs/onboarding/phantombuster-setup.md`** interactively, step by step.
It is the canonical first-time runbook; this section is the orchestration
wrapper. Read it now — the summary below maps to its sections.

### 2.1 — API key → `.env`

`docs/onboarding/phantombuster-setup.md` §1. PhantomBuster dashboard →
Settings → API → copy the key into `.env`:

```
PHANTOMBUSTER_API_KEY=<api key>
```

The env var name is documented (not resolved) by the `credentials:` block of
`config/phantombuster.example.yaml`. The loader reads it straight from the
environment.

### 2.2 — Create the phantoms, capture IDs → `config/phantombuster.yaml`

`docs/onboarding/phantombuster-setup.md` §2. Create each phantom from the PB
store and capture its numeric Phantom ID (in the dashboard URL
`.../phantoms/<ID>/...`). Then:

```bash
cp config/phantombuster.example.yaml config/phantombuster.yaml
```

Fill the `agents:` block — `search_export`, `network_booster`, `message_sender`,
`profile_scraper`, `sales_nav_profile_scraper`, `inbox_scraper`. Leave
`sales_nav_url_converter: ""` (not needed — the Sales Nav scraper auto-converts
`/in/` URLs).

> **Hard rule** (enforced by the loader): `sales_nav_profile_scraper` MUST be a
> **different** phantom ID than `profile_scraper`. They emit different CSV
> schemas; `clients/pb_config.py` rejects an accidental duplicate. (Resolution
> for each ID is **yaml → env var → `""`**; a blank yaml value falls through to
> the matching `PB_*` env var.)

### 2.3 — LinkedIn session cookie + user-agent → `.env`

`docs/onboarding/phantombuster-setup.md` §3. These are secrets — `.env` only:

```
PB_LI_SESSION_COOKIE=<li_at cookie value>
PB_LI_USER_AGENT=<navigator.userAgent string>
```

If `PB_LI_USER_AGENT` is blank the engine falls back to a shipped Chrome UA
(`clients/pb_config.py:DEFAULT_USER_AGENT`), but matching the real browser
reduces LinkedIn friction.

### 2.4 — Choose the degree-check backend

`docs/onboarding/phantombuster-setup.md` §4. Set in `config/phantombuster.yaml`:

```yaml
pre_invite_degree_check_backend: regular   # or: sales_nav
```

- **`regular`** (default) — uses `profile_scraper` + `PB_LI_SESSION_COOKIE`.
  Recommend this for a first install; it is the simplest path.
- **`sales_nav`** — uses `sales_nav_profile_scraper` + a separate Sales Nav
  cookie pair (`docs/onboarding/phantombuster-setup.md` §5). Only flip to it
  after capturing `PB_LI_SALES_NAV_SESSION_COOKIE` + `PB_LI_SALES_NAV_LI_A_COOKIE`
  in `.env` **and** `python3 scripts/validate_sales_nav_health.py` returns `OK`.
  The value is read live per run, so rollback is "set it back to `regular` and
  re-run."

### 2.5 — Validate (no-send check)

`docs/onboarding/phantombuster-setup.md` §6. Confirm the loader resolves the IDs
+ backend with no PB launch and no secret printed:

```bash
python -c "from dotenv import load_dotenv; load_dotenv(); \
from clients.pb_config import load_pb_config; c = load_pb_config(); \
print('search_export:', c.search_export_id); \
print('network_booster:', c.network_booster_id); \
print('backend:', c.degree_check_backend_raw)"
```

(Load `.env` first — `clients/settings.py` does not call `load_dotenv()`; only
`cli.py` does. Without it a correctly-filled `.env` still trips `ConfigError`.)

A blank value for any agent ID means the loader found neither a yaml value nor
the matching `PB_*` env var — fill it in and rerun.

(For `sales_nav`, additionally run `python3 scripts/validate_sales_nav_health.py`
and require `OK` before flipping the backend.)

### Phase 2 — what success looks like

- [ ] `config/phantombuster.yaml` exists with all required `agents:` IDs filled
      (and `sales_nav_profile_scraper` ≠ `profile_scraper`).
- [ ] `PHANTOMBUSTER_API_KEY`, `PB_LI_SESSION_COOKIE`, `PB_LI_USER_AGENT` in
      `.env`.
- [ ] `load_pb_config()` prints the operator's IDs + backend with no error.
- [ ] If `sales_nav`: Sales Nav cookies in `.env` and
      `scripts/validate_sales_nav_health.py` returns `OK`.

---

## Phase 3 — ICP co-definition

**Goal:** `config/icp.yaml` encodes the operator's ICP, the qualifier prompt
renders from it, and sample prospects score sensibly to the operator.

**Read first:** `workflows/icp_config.py` (`ICPConfig` + `load_icp_config`),
`config/icp.example.yaml` (every section, including `qualifier_prompt:` +
`lane_labels`), and `config/prompts/qualifier.md.j2`.

> **The math is unchanged — only the inputs.** The scoring LOGIC lives in
> `workflows/quality_gate.py` (`score_prospect`, `_llm_qualify`) and is **not**
> operator-tunable. `config/icp.yaml` supplies only the deterministic INPUTS:
> keyword lists, industry buckets, numeric weights, thresholds, geography lists,
> and the narrative qualifier-prompt slots. Stated verbatim at the top of
> `config/icp.example.yaml` and in `workflows/icp_config.py`. You are replacing
> the operator's *who-do-I-sell-to* data, not the engine's scoring.

### 3.1 — Copy the template

```bash
cp config/icp.example.yaml config/icp.yaml
```

The shipped `config/icp.example.yaml` currently holds the **original operator's**
ICP (LATAM manufacturing). Replace **every** section with the operator's ICP. If
`config/icp.yaml` is absent the loader falls back to the example
(`workflows/icp_config.py:_config_name`), so writing `config/icp.yaml` is what
makes the operator's ICP active.

### 3.2 — Interview the operator and fill each section

Walk the operator through each block of `config/icp.yaml` and replace the
shipped example values:

- **`qualifier_prompt.product_summary`** — one line: what they sell + to whom.
  Renders as `"You qualify B2B LinkedIn prospects for {product_summary}."`
- **`qualifier_prompt.geography_requirement`** — the geography gate paragraph.
- **`qualifier_prompt.lanes`** — the **two** ICP-lane description blocks (the
  engine classifies every prospect into exactly one of two lanes). Markdown
  markers in the strings are intentional.
- **`qualifier_prompt.lane_labels`** — **exactly two** short glosses for the two
  lanes (the loader rejects any count other than 2). They render into the
  `icp_lane` output line.
- **`qualifier_prompt.disqualifiers`** — the hard-disqualifier bullets (each a
  non-blank string; the loader rejects empty/whitespace bullets).
- **`roles.*`** — persona/role keyword lists (digitalization / executive /
  operations / decision-maker / influencer + the decision-maker exemptions).
- **`geography.*`** — global-executive demote keywords, geo overrides, and the
  PT/ES/EN location lists used for language detection.
- **`industries.in_icp` / `industries.off_icp`** — the in-ICP industry labels
  (case-sensitive, canonical capitalized) and the off-ICP "not a manufacturer"
  bucket.
- **`weights.*`** — the numeric score deltas (`industry_bonus_in_icp`,
  `industry_penalty_off_icp`, `ops_in_industrial_combined`).
- **`thresholds.score_band_labels`** — the band labels tracking the verdict-path
  branches.
- **`disqualifiers.*`** — every disqualifier keyword family
  (competitor / academic / consultant / sales-role + ops-exemptions /
  junior-IC + exemptions / HR / finance / innovation / PE / state-owned /
  ops-override). **Order matters** for families scanned for the first positional
  match — preserve order when editing.

> The loader is strict (`workflows/icp_config.py`): every section must be a
> mapping, every keyword list must be a list of strings, `lane_labels` must be
> exactly two, `disqualifiers`/`lanes`/`lane_labels` must be non-empty with no
> blank elements, weights must be integers. Any violation raises `ConfigError`
> naming the offending key — so a "passing" load is your structural proof.

### 3.3 — Render the qualifier prompt and confirm

The qualifier prompt loads from the operator's config and assembles through
`config/prompts/qualifier.md.j2` (the JSON output contract is STATIC engine
scaffolding; only the ICP narrative is operator data). Render it to show the
operator the assembled prompt and confirm the load succeeds:

```bash
python -c "from dotenv import load_dotenv; load_dotenv(); \
from workflows.icp_config import load_icp_config; \
from workflows.quality_gate import _render_qualifier_system_prompt; \
print(_render_qualifier_system_prompt(load_icp_config()))"
```

(Load `.env` first — `clients/settings.py` does not call `load_dotenv()`; only
`cli.py` does. The render path imports `workflows.quality_gate`, whose
module-level `_ICP = load_icp_config()` reads config, so a standalone `python -c`
without `load_dotenv()` raises `ConfigError` even when `.env` is correct.)

A clean print = the ICP config loaded and the prompt rendered. A `ConfigError`
names the bad key — fix `config/icp.yaml` and rerun. (`qualifier.md.j2` renders
with `StrictUndefined`, so a missing slot raises rather than rendering blank.)

### 3.4 — Score 5–10 sample prospects and iterate

Ask the operator for 5–10 real sample prospects (name, title, company,
industry, location — whatever their CRM carries). Score each through
`workflows/quality_gate.py:score_prospect` and show the operator the verdict
(pass/reject, lane, the verdict-path/score band, and the rationale). Then:

- If the operator disagrees with a verdict, trace it to the input that drove it
  (a disqualifier family that fired, a missing in-ICP industry label, a weight),
  edit `config/icp.yaml`, re-render (3.3), and re-score.
- **Iterate until the operator is satisfied** that the sample verdicts match
  their judgment. The deterministic scorer is only as good as these inputs, so
  this loop is the real work of Phase 3.

Inspect `workflows/quality_gate.py:score_prospect` for the exact argument shape
to pass (it consumes the loaded `ICPConfig` via the module-level `_ICP =
load_icp_config()`); construct the prospect inputs to match its signature.

> Any standalone `python -c "..."` you write to call `score_prospect` (or
> otherwise import `workflows.quality_gate` / read config) MUST start with
> `from dotenv import load_dotenv; load_dotenv();` — `clients/settings.py` never
> loads `.env` (only `cli.py` does), so without it the module-level
> `_ICP = load_icp_config()` raises `ConfigError` even with a correct `.env`.
> Commands invoked through `cli.py` / the `sales` console script already load
> `.env`, so they need no prefix.

### Phase 3 — what success looks like

- [ ] `config/icp.yaml` exists with **every** section replaced by the operator's
      ICP (no leftover example values).
- [ ] `load_icp_config()` returns with no `ConfigError`.
- [ ] `_render_qualifier_system_prompt(load_icp_config())` prints the operator's
      assembled qualifier prompt.
- [ ] 5–10 sample prospects score, and the operator agrees with the verdicts.

---

## Phase 4 — Verify

**Goal:** run the engine's daily flow in `--dry-run` against a small sample,
confirm prospects score and the cadence composes, and report readiness.

### 4.1 — Run the daily dry-run

The daily command is defined in `cli.py` (`@cli.command()` `def daily`, with
`--dry-run` "Preview actions without executing"). It can be invoked either via
the console script `sales` (declared in `pyproject.toml` → `[project.scripts]`
`sales = "cli:cli"`) or directly:

```bash
# console script:
sales daily --dry-run
# or, equivalently, directly:
python cli.py daily --dry-run
```

A dry run previews actions without executing them: it scores prospects and
composes the cadence but does **not** send invites/DMs or write to the CRM
(every PB launch and write is gated on `mode.is_dry_run()` in `cli.py`). Keep
the sample small — use `--batch-size <N>` (a `daily` option) to bound it.

Watch the output for:
- prospects being **scored** (Phase 3's ICP is doing its job),
- the **cadence composing** (the pipeline read from Phase 1's CRM is working),
- `Skipping (dry run)` lines where live PB launches/writes would have fired
  (confirms no live action),
- `Skipping (no PB_*_ID set)` for any still-blank phantom — go back to Phase 2.

If the run errors with a `ConfigError`, it names the missing config/secret —
return to the relevant phase, fix it, and rerun.

### 4.2 — Report readiness

Summarize for the operator:

- **What is configured** — list the live `config/*.yaml` files now present:
  `config/crm.yaml`, `config/phantombuster.yaml`, `config/icp.yaml` (and, for a
  non-Attio CRM, the generated `clients/crm/<vendor>_provider.py` + its green
  conformance run).
- **What secrets are in `.env`** — by **name** only (never print values): the
  CRM key (e.g. `ATTIO_API_KEY`) + list id, `PHANTOMBUSTER_API_KEY`,
  `PB_LI_SESSION_COOKIE`, `PB_LI_USER_AGENT`, and (if `sales_nav`) the Sales Nav
  cookie pair.
- **Dry-run result** — prospects scored, cadence composed, no live sends.
- **The honest edges** (below) so the operator knows the limits of a non-Attio
  install before going live.

### Phase 4 — what success looks like

- [ ] `sales daily --dry-run` (or `python cli.py daily --dry-run`) completes with
      no `ConfigError`.
- [ ] Prospects score and the cadence composes in the output.
- [ ] Only `Skipping (dry run)` / `Skipping (no PB_*_ID set)` lines appear where
      live actions would — no live send fired.
- [ ] Readiness report delivered: config files listed, `.env` secret names
      listed, limitations stated.

---

## Honest limitations to state to the operator

These are real edges of the current code. State them at the end of onboarding,
especially for a non-Attio CRM:

1. **Write-path exception model is Attio-coupled.**
   `clients/attio_writer.py:_single_patch` and its `_compensating_rollback`
   atomicity model, plus the raw-httpx PATCH fallback for object types without a
   typed provider helper, are Attio-specific. The provider seam covers typed
   reads/writes; the writer's exception/rollback model is **not** yet
   vendor-neutral. A non-Attio adapter satisfies the read/write contract, but the
   writer layer above it still assumes Attio semantics.
2. **The filter-DSL leak.** `query_object_records(filters=…)` (and `search_*`'
   `filter_`) take a **vendor-native** query body — Attio's filter DSL today
   (`clients/crm/CONTRACT.md` → filter-shape-leak note). A non-Attio operator
   must either translate those bodies inside the adapter or accept that callers
   passing a `filters` body stay Attio-shaped. The return type is still
   normalized, so reads downstream of the query are vendor-agnostic — only the
   query body leaks.
3. **`config/crm.yaml` field/stage mapping is not yet consumed.** Field slugs and
   stage labels still resolve in code (`models/pipeline.py` + the adapter's
   normalization), not from `field_mapping`/`stage_field`/`stage_mapping`. Fill
   them for forward-compatibility, but they are inert at runtime today
   (`config/crm.example.yaml` says so verbatim).
4. **The shipped `config/icp.example.yaml` is the original operator's ICP.** It is
   the fallback when `config/icp.yaml` is absent — so a half-finished ICP silently
   inherits the example manufacturing ICP. Writing a complete `config/icp.yaml` is what makes
   the operator's ICP active; verify the load and the sample scores rather than
   trusting the fallback.
