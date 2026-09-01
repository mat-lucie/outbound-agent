# `CRMProvider` Contract — Method Map

This is the worked guide for the Attio-adapter subtask and the onboarding agent.
It maps each `CRMProvider` abstract method (`clients/crm/base.py`) to the
`AttioClient` method(s) it corresponds to (`clients/attio.py`), and records the
surveyed call-site justification for including it.

The contract was derived from the **real** call sites in `workflows/` and
`cli.py`, not the simplified §1.1 list in the design doc. Where the two diverge,
this file is the source of truth and the divergences are called out at the end.

## Normalized types → Attio shapes

| Contract type | Replaces (Attio) | Notes |
|---|---|---|
| `Record` | raw object-record JSON (`{"id": {...}, "values": {...}}`) | `record_id` ← `id.record_id`; `object` ← people/companies/deals; `attributes` ← flattened `values`; `raw` keeps the untouched payload. |
| `Entry` | raw list-entry JSON + `AttioClient.parse_entry()` output | `entry_id`/`record_id` ← `parse_entry`; `stage` ← `Stage`; `attributes` ← the rest of the `parse_entry` flat dict (persona, dm_step, timestamps, scoring signals, …). |
| `Stage` | `AttioClient._extract_stage()` + `models.pipeline.STAGE_RANK` | `name` is the stage label (same space as `PipelineStage` values); `rank` is the funnel rank, `None` for an unknown stage (→ caller treats as 0). |
| `RecordInfo` | `AttioClient.extract_record_info()` 5-tuple | Named struct for `(name, company, linkedin_url, industry, title)`. Missing = `None`; `linkedin_url` = `""` when blank. |

## Method map

| `CRMProvider` method | `AttioClient` method | Call-site justification |
|---|---|---|
| `search_people(filter_, limit)` | `search_people` | 4 call sites in workflows/cli; people discovery + filtered lookups. |
| `get_person(record_id)` | `get_person` | 13 call sites repo-wide; single-record resolve, 404 → `None`. |
| `bulk_fetch_persons(ids, *, metrics)` | `bulk_fetch_persons_by_record_ids` | RecordCache batch hydrate; per-record isolation + metrics. Renamed to drop the Attio-ish `_by_record_ids` suffix; `max_workers` is an Attio-internal tuning knob, dropped from the contract. |
| `prefetch_companies_for_persons(records, *, metrics)` | `bulk_prime_company_caches` | **Optional, non-abstract, default no-op.** Read-path optimization only: warms the adapter's employer-lookup cache so `extract_person_info` stops doing one blocking company GET per first-seen company. No engine behavior may depend on it having run; adapters without a per-person company reference inherit the no-op. |
| `search_person_by_linkedin(url)` | `search_person_by_linkedin` | Dedupe-by-LinkedIn before create. URL-variant matching is an adapter quirk. |
| `create_person(attrs)` | `create_person` | Person creation in prospecting. |
| `update_person(record_id, attrs)` | `update_person` | 8 call sites; the people branch of `AttioWriter._single_patch`. |
| `upsert_person(matching_attribute, attrs)` | `upsert_person` | Idempotent create-or-update on LinkedIn/email. |
| `search_companies(filter_, limit)` | `search_companies` | 4 call sites; company lookups. |
| `get_company(record_id)` | `get_company` | 3 call sites; company resolve. |
| `create_company(attrs)` | `create_company` | Company creation. |
| `update_company(record_id, attrs)` | `update_company` | Companies branch of `AttioWriter._single_patch`. |
| `search_company_by_domain(domain)` | `search_company_by_domain` | Dedupe-by-domain before company create. |
| `get_deal(record_id)` | `get_deal` | Deal resolve in deal-creation/response flows. |
| `create_deal(attrs)` | `create_deal` | Deal creation (idempotency via a key attr the caller supplies). |
| `update_deal(record_id, attrs)` | `update_deal` | Deal updates. |
| `search_deals(filter_, limit)` | `search_deals` | Deal lookups. |
| `query_list_entries(list_id, filter_, limit)` | `query_list_entries` | 41 call sites — the pipeline read. `list_id=None` → configured default list. |
| `add_list_entry(record_id, stage_name, entry_attributes, list_id, existing_entries)` | `add_list_entry` | Pipeline upsert; idempotent on `(record_id, list)`; most-advanced-entry selection. |
| `update_list_entry(entry_id, entry_attributes, list_id)` | `update_list_entry` | 17 call sites; the list-entry branch of `AttioWriter._single_patch` (stage/attr writes). |
| `create_note(record_id, title, content, parent_object)` | `create_note` | 10 call sites; cadence/audit annotations. |
| `extract_person_info(record)` | `extract_record_info` | 11 call sites; the display tuple for cadence + dry-run. Takes a normalized `Record` instead of raw JSON. |
| `query_object_records(object_type, *, filters, sorts, limit)` | `_request("POST", "/objects/<obj>/records/query", json={"filter", "sorts", "limit"})` | ~15 raw-`_request` query sites: `operator_review_queue` (sales_approve, industry_approve, escalation, auto_finalize, daily_check, cli, unit_economics), `deals` (deal_creation), `llm_budget_ledger` (llm_budget). |
| `get_object_record(object_type, record_id)` | `_request("GET", "/objects/<obj>/records/<id>")`, 404 → `None` | Raw GET sites: `people` (cross_channel_suppression), `companies` (wave2_blast, email_campaign). |
| `create_object_record(object_type, attributes)` | `_request("POST", "/objects/<obj>/records", json={"data": {"values": attrs}})` (retrying); a vendor uniqueness-violation 400/409 → `UniquenessConflictError` | Raw create sites: `operator_review_queue` (escalation), `llm_budget_ledger`, `reclassification_run`, `migration_run`, `daily_run` (daily_run open). |
| `update_object_record(object_type, record_id, attributes)` | `_request("PATCH", "/objects/<obj>/records/<id>", json={"data": {"values": attrs}})` (retrying); a vendor uniqueness-violation 400/409 → `UniquenessConflictError` | Raw PATCH sites: `operator_review_queue` (unit_economics supersede), `people`/`companies` back-pointers (reclassification/migration writers), `daily_run` (counters/status/close/reopen). |

## Stage handling (read this before implementing the adapter)

There is **no** `get_stage`/`set_stage` on `AttioClient`; the design doc's §1.1
invented them. In the live engine:

- **Read:** stage is parsed off a list entry (`_extract_stage` inside
  `parse_entry`). The contract surfaces it as `Entry.stage: Stage`.
- **Write:** stage is just an attribute — pass `stage_name` to `add_list_entry`
  or a `stage` value in `update_list_entry`'s attrs.
- **Monotonicity (no-regression) is enforced one layer UP**, in
  `clients/attio_writer.py` (`AttioWriter._check_stage_monotonicity` /
  `_check_terminal_class_regression`), **not** in the CRM client. So the
  `CRMProvider` methods do **not** promise stage writes never regress — they
  only promise the write is persisted. The writer keeps owning monotonicity.

## Generic object-record API (typed-vs-generic division)

The contract carries **two** record-access surfaces, and the division is
deliberate:

- **Typed methods** (`get_person`, `create_company`, `update_deal`,
  `query_list_entries`, `create_note`, …) — the ergonomic path for the engine's
  **core** CRM objects. They return purpose-shaped types (`Record` with a known
  `object`, `Entry`, `RecordInfo`) and own object-specific quirks (LinkedIn
  variant matching, stage parsing). Prefer these whenever the object is people /
  companies / deals / list-entries / notes.
- **Generic methods** (`query_object_records`, `get_object_record`,
  `create_object_record`, `update_object_record`) — the vendor-neutral surface
  for **arbitrary / operational** objects the engine also writes and reads:
  `operator_review_queue`, `llm_budget_ledger`, `reclassification_run`,
  `migration_run`, `experiment`, plus ad-hoc people/companies reads by a
  non-standard filter. These four replace the ~25 raw `AttioClient._request(...)`
  call sites surveyed in `workflows/` + `cli.py` — the private-channel bypass the
  design decision set out to close. They normalize the return to `Record` (with
  `object` = the requested `object_type`) and match the typed methods' error
  semantics (404 → `None` for `get_object_record`; otherwise propagate).

**Known limitation — the filter-shape leak.** `query_object_records`' `filters`
and `sorts` arguments are passed **through to the adapter as a vendor-native
query body** — for Attio, its filter DSL
(`{"$and": [{"type": "x"}, {"status": {"$not": {"$in": [...]}}}]}`) and sort
spec. This contract does **not** define a vendor-neutral filter/sort DSL; that
would be a separate design and is out of scope for this increment. The
consequence: a **generated non-Attio adapter must translate** these shapes into
its own vendor's query language, or every caller that passes a `filters` body
stays Attio-coupled. The return type is still normalized (`Record`), so reads
*downstream* of the query remain vendor-agnostic — only the query body itself
leaks. Callers should treat `filters`/`sorts` as "the vendor's native query
body," not as a portable abstraction. (The create/update/get methods do **not**
leak — they take only an `object_type`, a `record_id`, and a flat
`slug -> value` attrs dict, all of which are already neutral.)

## Normalized exception family (`clients/crm/exceptions.py`)

The contract normalizes one **error** the same way it normalizes reads: callers
branch on the failure's *meaning*, not on a vendor HTTP status/body.

- **`CRMError`** — the catchable root for contract-level failures. Transport /
  connectivity errors are NOT wrapped in it; they propagate as the adapter's
  native exception.
- **`UniquenessConflictError(CRMError)`** — raised by `create_object_record` /
  `update_object_record` when the vendor rejects a write for violating a
  uniqueness constraint on a written attribute. The Attio adapter maps its
  vendor-native uniqueness violation (an HTTP 400/409 whose body names a
  uniqueness / duplicate / already-exists condition — the logic absorbed from
  `daily_run`'s old `_is_uniqueness_collision`) to this type; `FakeProvider`
  raises it when a write would duplicate a value on an attribute it models as
  unique (`daily_run.uniqueness_key`). The conformance suite
  (`TestUniquenessContract`) asserts this against **both** reference providers.
  `workflows/daily_run.py`'s cross-machine concurrent-run guard
  (`open_daily_run` → `ConcurrentRunInAttio`) and the DM-gate reopen race
  (`attach_daily_run` → `ReopenCollision`) catch THIS, never a raw vendor error,
  so the guard is vendor-neutral. Any other 4xx/5xx propagates unchanged.

## Deliberate non-promises (NOT enforced by the conformance suite)

Two behaviors a reader might *expect* from a "no-silent-failure" CRM seam are
deliberately **not** part of this contract. They are documented here so their
absence is read as a decision, not a gap.

- **Schema validation / "no silent field drops."** An earlier draft of
  `base.py` promised that a write referencing a slug the vendor schema does not
  know MUST raise. That over-promised: neither reference adapter (`FakeProvider`,
  `AttioProvider`) enforces it, and enforcing it would couple the seam to
  per-vendor schema introspection. The contract now **downgrades this to a
  non-promise** — writes pass the flat `slug -> value` attrs THROUGH and the
  vendor is the schema authority (Attio returns a 4xx for a genuinely-unknown
  attribute, which propagates; some vendors silently drop). Operators needing
  strict enforcement validate attrs upstream of the provider. There is
  intentionally no raise-on-unknown-field conformance test.
- **Nested / rich filter DSL.** `query_object_records`' `filters`/`sorts` (and
  `search_*`'s `filter_`) are a **vendor-native query body** — the filter-shape
  leak documented above. The contract defines no neutral filter DSL, so any
  behavior richer than flat equality (`$and`/`$or`/`$not`/nested operator dicts)
  is per-adapter, NOT universally assertable. The conformance suite asserts only
  the single flat-equality case; a generated non-Attio adapter must test its own
  translation of richer shapes.

## Deliberate exclusions (YAGNI / quirk-isolation)

| Excluded | Why |
|---|---|
| `add_comment` (from §1.1) | **No such method exists** on `AttioClient` and there are zero callers anywhere. Pure §1.1 invention — excluded. |
| `upsert_record` / `get_records_by_ids` (generic, from §1.1) | The live engine is object-typed for its core triad (people/companies/deals). `get_records_by_ids` maps to `bulk_fetch_persons`. There is **no** generic upsert: no surveyed raw-`_request` site upserts an arbitrary object (they query-then-create/patch explicitly), so a generic `upsert_object_record` would be YAGNI. (`get_record` / `create_record` / `update_record` generics ARE now provided — see the Generic object-record API section — but scoped to the surveyed arbitrary/operational objects, not as a replacement for the typed core methods.) |
| `delete_person` / `delete_company` / `delete_deal` / `delete_list_entry` / `delete_note` | Zero callers in `workflows/`/`cli.py`. Used only by maintenance scripts (`scripts/`, which mypy ignores). Not part of the engine's runtime contract. |
| `list_notes_for_record` | Zero callers in `workflows/`/`cli.py`. |
| `is_person_company_corrupted` / `is_linkedin_clearbit_corrupted` | **Attio data-quality quirk.** Stays inside `attio_provider.py`. If a caller still needs the flag post-threading, expose it on the concrete adapter, not the ABC. |
| `_canonical_linkedin_url`, `_vanity_url_slug`, `_linkedin_url_variants` | Attio exact-string-match quirk. Internal to the adapter. |
| `parse_entry` / `parse_deal` / `_extract_stage` / `_extract_value` / `object_record_first_value` | Vendor JSON parsing internals. Their *output* is the normalized `Entry`/`Record`/`Stage`; the parsing itself is adapter-private. |

## Notes for the adapter subtask

- `AttioWriter` (`clients/attio_writer.py`) currently reaches into
  `AttioClient._client.request(...)` directly (line ~459) for object types
  without a dedicated update helper. That private-channel bypass is a
  **threading concern**, not a contract concern — flag it when wiring the
  writer behind the provider; do not add a raw-request method to the ABC.
- `bulk_fetch_persons` drops `max_workers` from the signature (Attio-internal
  concurrency tuning). If a non-Attio adapter needs a knob, keep it adapter-side
  with a default, not on the contract.
- Adapters must convert vendor JSON → the normalized dataclasses at the boundary
  for every read return, and accept flat `dict[str, Any]` attrs for every write.

## Migration pattern (workflow slices → `CRMProvider`)

The engine threads the raw `AttioClient` into every workflow today
(`cli.py` hands `bundle.attio` to each command). Migrating that to the contract
happens **one read-oriented vertical slice at a time**. The recipe below is the
one used for the first slice — `workflows.backfill_companies.backfill_export`
(P1c-increment-2). Follow it for the next chunks.

**1. Pick the slice.** Choose the smallest command whose *whole* call-tree reads
the CRM only through methods that exist on the contract (`query_list_entries`,
`get_person`, `search_*`, `extract_person_info`, …) and that does **not** write
through `AttioWriter` / `clients/attio_writer.py`. Slices that reach arbitrary
objects via the raw private channel `attio._request(...)` (e.g. the
`operator_review_queue` reads in `sales_approve` / `industry_approve`, the
`llm_budget_ledger` rows, the run-writers) now have a contract method —
`query_object_records` / `get_object_record` / `create_object_record` /
`update_object_record` — so they are migratable: swap each raw `_request(...)`
for the matching generic method (mind the filter-shape leak — the `filters` body
stays Attio-native). Slices that write through `AttioWriter` remain blocked until
the writer is migrated.

**2. Swap the signature.** `def fn(attio: AttioClient)` →
`def fn(crm: CRMProvider)`. Keep `from clients.crm.base import CRMProvider` under
`TYPE_CHECKING`. Leave the module's `AttioClient` import in place if sibling
*unmigrated* functions in the same module still use it.

**3. Substitute contract methods.** The two non-1:1 substitutions to watch:

| Legacy (`AttioClient`) | Contract (`CRMProvider`) | Shape change |
|---|---|---|
| `attio.query_list_entries(...)` + `AttioClient.parse_entry(e)["record_id"]` | `crm.query_list_entries(...)` → `list[Entry]`, then `entry.record_id` | Returns pre-parsed `Entry` objects — **drop `parse_entry` entirely**; read `entry.record_id` / `entry.stage.name` off the dataclass. |
| `attio.extract_record_info(raw)` → 5-tuple | `crm.extract_person_info(record)` → `RecordInfo` | Takes a normalized `Record` (not raw JSON), returns a **dataclass** not a tuple. |
| `attio.get_person(id)` → raw `dict\|None` | `crm.get_person(id)` → `Record \| None` | Returns a `Record`; structured reads go through `record.raw` (see #4). |
| `bulk_fetch_persons_by_record_ids(ids, max_workers=…)` | `crm.bulk_fetch_persons(ids, metrics=…)` | `max_workers` dropped (adapter-internal). |

**4. The `Record.raw` escape hatch.** A read the contract does not model as a
flat attribute — e.g. a structured multi-value record-reference like the person's
linked `company` (`person["values"]["company"][0]["target_record_id"]`) — has no
typed field. Read it off `record.raw["values"][…]` (the untouched vendor payload
the contract guarantees). This keeps the slice behavior-identical without
inventing new contract surface.

**5. Dataclass→tuple boundary trick (behavior preservation).** When a migrated
read returns the `RecordInfo` *dataclass* but the slice's downstream code (or a
consumer outside the slice) still expects the legacy 5-tuple, convert **at the
call boundary** rather than rippling the change outward:
`name, company, linkedin_url, industry, title = (info.name, info.company,
info.linkedin_url, info.industry, info.title)` (or unpack only the fields the
slice uses). Preserve `""`-not-`None` for `linkedin_url`/`title` — the contract
already guarantees `extract_person_info` returns `""` (never `None`) for those
two, so downstream truthiness checks keep working. The goal is a behavior-identical
slice, not a repo-wide reshape.

**6. Wire `cli.py`.** Migrated commands receive `bundle.provider` via the
`_crm_provider()` context manager; unmigrated commands keep `_attio_client()`
(`bundle.attio`). Both share the same factory + lifecycle, so the only change per
slice is which accessor the command's `with` block uses. As all of a module's
commands migrate, its `_attio_client()` calls disappear.

**7. Do NOT touch write paths yet.** `AttioWriter` / `clients/attio_writer.py`,
`clients/attio.py`, and `clients/crm/*` are out of scope for a read-slice
migration — they are separate later increments. If the smallest available read
slice is still entangled with `AttioWriter` or a shared mutable helper used by
unmigrated commands, either convert only your command's usage at the call
boundary, or pick a different slice — do not change a shared helper's signature.

**8. Tests.** Construct a `CRMProvider` in the slice's test — either
`MagicMock(spec=CRMProvider)` returning the contract dataclasses (`Entry` /
`Record` / `RecordInfo`), or the real `AttioProvider` over a mocked inner client.
Assert the contract methods are called with normalized shapes and the slice's
observable output is unchanged. Keep the test *intent* identical — you are
changing how the client is provided, not what behavior is asserted.

## Migration status + the `_attio_inner_client` escape hatch (tracked debt)

Slices migrated so far: `backfill-export`, the operator-review-queue reads
(`sales_approve`/`industry_approve`), `detect-bad-companies`, **`weekly`**,
**`RecordCache`**, and **`daily`**.

Several of these are NOT pure contract migrations — they flip the command trunk
to `CRMProvider` but route specific Attio-coupled call paths back to the raw
client via `_attio_inner_client(crm)` (currently defined in
`workflows/weekly_prospect.py`; the provider's `inner_client` property is the
Attio-only handle, not on the ABC). This is the §7 "convert at the call boundary"
move for paths that have no contract equivalent yet. **The `daily` slice is
deliberately escape-hatch-heavy** (~7 uses): `daily_run.open_daily_run` (its
non-retrying `_client.request` + `ConcurrentRunInAttio` collision exception have
no contract equivalent), the send loops (`run_connection_requests`,
`run_dm_sequencing`, `detect_accepted_connections`, `detect_responses`,
`recover_unrecorded_dm_sends`), `evaluate_pipeline_starvation` /
`_get_all_entries_parsed` (shared dict-based send-eligibility, also used by
unmigrated `threshold_calibration`), and the Attio-only quirks
(`_person_to_company`, `is_person_company_corrupted`). `weekly` adds 2 more
(`match_or_create_company`, `backfill_missing_industries`).

These `_attio_inner_client` uses are **tracked debt the vendor-neutral
exception-model increment will delete** (routing the transport-semantics paths
through neutral retry-aware writes), at which point the helper should also move
to a neutral home (`clients/crm/` or a small `workflows/_crm_compat.py`) rather
than living in `weekly_prospect`. Grep `_attio_inner_client` to enumerate them.
