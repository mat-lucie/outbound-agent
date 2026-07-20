"""``CRMProvider`` conformance suite — the adapter bar.

Any new ``CRMProvider`` implementation MUST be added to the ``provider``
fixture and pass this suite. **This is the adapter conformance bar.** The
onboarding agent that generates a new vendor's adapter (and the P4 work) point
here: a generated adapter is "correct" iff it is green against this file.

The assertions encode the *contract* (``clients/crm/base.py`` +
``clients/crm/CONTRACT.md``), not any one vendor's quirks:

  * reads return the normalized dataclasses (``Record`` / ``Entry`` /
    ``RecordInfo``), never raw vendor JSON;
  * ``get_*`` / ``get_object_record`` return ``None`` for a missing id (never
    raise);
  * ``create`` / ``update`` round-trip (the written value reads back);
  * ``upsert_person`` and ``add_list_entry`` are idempotent on their keys;
  * stage rank resolves and "most-advanced-entry wins" on add;
  * ``extract_person_info`` returns ``""`` (not ``None``) for blank
    ``linkedin_url`` / ``title``;
  * ``query_object_records`` applies a flat-equality filter and normalizes rows
    to ``Record``;
  * ``bulk_fetch_persons`` bumps the passed ``DailyRunMetrics`` sink's
    ``bulk_fetch_records_requested/returned/failed`` counters;
  * ``create_note`` links the created note to its parent record + object.

Deliberate non-promises (NOT asserted here — documented so a future reader does
not mistake their absence for a coverage gap)
---------------------------------------------------------------------------------
  * **Schema validation / "no silent field drops".** The contract does NOT
    require a write to RAISE when an attr names a slug the vendor schema does not
    know (``base.py`` downgrades this from an earlier over-promise). Writes pass
    attrs THROUGH; schema authority is the vendor's (Attio 4xx propagates; other
    vendors may silently drop). Neither reference adapter enforces it, so there
    is intentionally no raise-on-unknown-field conformance test. Operators
    needing strict enforcement validate upstream of the provider.
  * **Nested / rich filter DSL.** ``query_object_records(filters=...)`` (and
    ``search_*``'s ``filter_``) take a **vendor-native** query body — the
    documented "filter-shape leak" (``base.py`` / ``CONTRACT.md``). The contract
    defines NO neutral filter DSL, so anything beyond the single flat-equality
    case below (``$and`` / ``$or`` / ``$not`` / nested operator dicts) is
    per-adapter and NOT universally assertable. A generated non-Attio adapter
    MUST test its own translation of these shapes; a universal nested-filter test
    here would be a false promise and is deliberately omitted.

Providers under test
---------------------
The suite is parametrized over TWO implementations and runs the **full**
behavioral suite against both:

  * ``FakeProvider`` — the in-memory reference (no network, no vendor SDK).
  * ``AttioProvider`` — the Attio reference adapter over a *stateful* mocked
    inner ``AttioClient``. The mock is backed by an in-memory raw-Attio-shaped
    store (``_StatefulAttioClient`` below) whose method side-effects produce the
    raw JSON the adapter normalizes — so create/update/query/list-entry
    operations genuinely round-trip, not just return canned fixtures. This lets
    AttioProvider exercise the same idempotency / round-trip / normalization
    assertions as FakeProvider rather than a thin canned-return subset.

Where a behavior is genuinely impossible to drive identically through the Attio
mock (none today), it would be split into a FakeProvider-only test with a
documented justification. Currently the full suite runs against both.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from clients.crm import AttioProvider, FakeProvider
from clients.crm.base import CRMProvider, Entry, Record, RecordInfo, Stage
from clients.crm.exceptions import UniquenessConflictError

# ── Stateful Attio inner-client mock ─────────────────────────────────────────
#
# A minimal in-memory store that emits raw-Attio-shaped JSON, so AttioProvider
# (which only adds a normalization layer) round-trips through the SAME contract
# operations as FakeProvider. This mirrors test_attio_provider.py's mocking
# approach (a stand-in for AttioClient) but stateful, so create→get→update
# chains behave like a real backend.


class _StatefulAttioClient:
    """Behaves like ``AttioClient`` for the methods the contract uses, backed
    by dict stores and emitting raw-Attio JSON shapes."""

    def __init__(self) -> None:
        self._records: dict[str, dict[str, dict[str, Any]]] = {}
        self._entries: dict[str, dict[str, Any]] = {}
        self._counters: dict[str, itertools.count[int]] = {}

    def _next_id(self, kind: str) -> str:
        counter = self._counters.setdefault(kind, itertools.count(1))
        return f"{kind}_{next(counter)}"

    @staticmethod
    def _raw(record_id: str, attributes: dict[str, Any]) -> dict[str, Any]:
        # Store each attr as Attio's [{"value": ...}] list shape so the
        # adapter's object_record_first_value flattening exercises real unwrap.
        values = {slug: [{"value": val}] for slug, val in attributes.items()}
        return {"id": {"record_id": record_id}, "values": values}

    def _store(self, object_: str) -> dict[str, dict[str, Any]]:
        return self._records.setdefault(object_, {})

    def _create(self, object_: str, attributes: dict[str, Any]) -> dict[str, Any]:
        rid = self._next_id(object_)
        raw = self._raw(rid, attributes)
        self._store(object_)[rid] = raw
        return raw

    def _update(
        self, object_: str, record_id: str, attributes: dict[str, Any]
    ) -> dict[str, Any]:
        store = self._store(object_)
        existing = store.get(record_id, {"values": {}})
        merged = {
            slug: vals[0].get("value")
            for slug, vals in existing.get("values", {}).items()
        }
        merged.update(attributes)
        raw = self._raw(record_id, merged)
        store[record_id] = raw
        return raw

    @staticmethod
    def _first(raw: dict[str, Any], slug: str) -> Any:
        vals = raw.get("values", {}).get(slug, [])
        return vals[0].get("value") if vals else None

    # People
    def search_people(
        self, filter_: dict[str, Any] | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        return self._search("people", filter_, limit)

    def get_person(self, record_id: str) -> dict[str, Any] | None:
        return self._store("people").get(record_id)

    def bulk_fetch_persons_by_record_ids(
        self, record_ids: set[str], *, max_workers: int = 8, metrics: Any = None
    ) -> dict[str, dict[str, Any]]:
        store = self._store("people")
        result = {rid: store[rid] for rid in record_ids if rid in store}
        # Honor the metrics contract the same way the real AttioClient does
        # (clients/attio.py): bump the canonical DailyRunMetrics counters so the
        # AttioProvider path exercises the same observability promise as Fake.
        if metrics is not None:
            metrics.bulk_fetch_records_requested += len(record_ids)
            metrics.bulk_fetch_records_returned += len(result)
            metrics.bulk_fetch_records_failed += len(record_ids) - len(result)
        return result

    def search_person_by_linkedin(self, linkedin_url: str) -> dict[str, Any] | None:
        if not linkedin_url:
            return None
        for raw in self._store("people").values():
            if self._first(raw, "linkedin") == linkedin_url:
                return raw
        return None

    def create_person(self, attributes: dict[str, Any]) -> dict[str, Any]:
        return self._create("people", attributes)

    def update_person(
        self, record_id: str, attributes: dict[str, Any]
    ) -> dict[str, Any]:
        return self._update("people", record_id, attributes)

    def upsert_person(
        self, matching_attribute: str, attributes: dict[str, Any]
    ) -> dict[str, Any]:
        match_value = attributes.get(matching_attribute)
        if match_value is not None:
            for rid, raw in self._store("people").items():
                if self._first(raw, matching_attribute) == match_value:
                    return self._update("people", rid, attributes)
        return self._create("people", attributes)

    # Companies
    def search_companies(
        self, filter_: dict[str, Any] | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        return self._search("companies", filter_, limit)

    def get_company(self, record_id: str) -> dict[str, Any] | None:
        return self._store("companies").get(record_id)

    def create_company(self, attributes: dict[str, Any]) -> dict[str, Any]:
        return self._create("companies", attributes)

    def update_company(
        self, record_id: str, attributes: dict[str, Any]
    ) -> dict[str, Any]:
        return self._update("companies", record_id, attributes)

    def search_company_by_domain(self, domain: str) -> dict[str, Any] | None:
        if not domain:
            return None
        for raw in self._store("companies").values():
            if self._first(raw, "domain") == domain:
                return raw
        return None

    # Deals
    def get_deal(self, record_id: str) -> dict[str, Any] | None:
        return self._store("deals").get(record_id)

    def create_deal(self, attributes: dict[str, Any]) -> dict[str, Any]:
        return self._create("deals", attributes)

    def update_deal(self, record_id: str, attributes: dict[str, Any]) -> dict[str, Any]:
        return self._update("deals", record_id, attributes)

    def search_deals(
        self, filter_: dict[str, Any] | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        return self._search("deals", filter_, limit)

    def _search(
        self, object_: str, filter_: dict[str, Any] | None, limit: int
    ) -> list[dict[str, Any]]:
        rows = list(self._store(object_).values())
        if filter_:
            rows = [
                r
                for r in rows
                if all(
                    not isinstance(v, (dict, list)) and self._first(r, k) == v
                    for k, v in filter_.items()
                )
            ]
        return rows[:limit]

    # List entries
    def query_list_entries(
        self,
        list_id: str | None = None,
        filter_: dict[str, Any] | None = None,
        limit: int = 50000,
        *,
        fail_if_truncated: bool = False,
    ) -> list[dict[str, Any]]:
        from clients.attio import AttioResultTruncated

        target = list_id or "__default__"
        rows = [e for e in self._entries.values() if e["id"].get("list_id") == target]
        if fail_if_truncated and len(rows) > limit:
            raise AttioResultTruncated(
                f"list {target!r} has {len(rows)} entries past limit {limit}"
            )
        return rows[:limit]

    def add_list_entry(
        self,
        record_id: str,
        stage_name: str,
        entry_attributes: dict[str, Any] | None = None,
        list_id: str | None = None,
        existing_entries: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        target = list_id or "__default__"
        attrs = dict(entry_attributes or {})
        pool = (
            existing_entries
            if existing_entries is not None
            else list(self._entries.values())
        )
        candidates = [
            e
            for e in pool
            if e.get("parent_record_id") == record_id
            and e["id"].get("list_id") == target
        ]
        if candidates:
            winner = max(candidates, key=lambda e: self._rank(e))
            return self._patch_entry(winner["id"]["entry_id"], stage_name, attrs)
        entry_id = self._next_id("entry")
        ev = {slug: [{"value": v}] for slug, v in attrs.items()}
        if stage_name:
            ev["stage"] = [{"status": {"title": stage_name}}]
        raw = {
            "id": {"entry_id": entry_id, "list_id": target},
            "parent_record_id": record_id,
            "entry_values": ev,
        }
        self._entries[entry_id] = raw
        return raw

    @staticmethod
    def _rank(entry_raw: dict[str, Any]) -> int:
        from models.pipeline import STAGE_RANK, PipelineStage

        stage_vals = entry_raw.get("entry_values", {}).get("stage", [])
        name = (
            stage_vals[0].get("status", {}).get("title", "") if stage_vals else ""
        )
        try:
            return STAGE_RANK[PipelineStage(name)] or 0
        except (KeyError, ValueError):
            return 0

    def _patch_entry(
        self, entry_id: str, stage_name: str, attrs: dict[str, Any]
    ) -> dict[str, Any]:
        raw = self._entries[entry_id]
        ev = raw.setdefault("entry_values", {})
        for slug, v in attrs.items():
            if slug == "stage":
                ev["stage"] = [{"status": {"title": v}}]
            else:
                ev[slug] = [{"value": v}]
        if stage_name:
            ev["stage"] = [{"status": {"title": stage_name}}]
        return raw

    def update_list_entry(
        self,
        entry_id: str,
        entry_attributes: dict[str, Any],
        list_id: str | None = None,
    ) -> dict[str, Any]:
        attrs = dict(entry_attributes)
        return self._patch_entry(entry_id, attrs.get("stage", ""), attrs)

    # Notes
    def create_note(
        self,
        record_id: str,
        title: str,
        content: str,
        parent_object: str = "people",
    ) -> dict[str, Any]:
        note_id = self._next_id("note")
        # Echo the parent linkage into the raw payload, mirroring Attio's note
        # create response (which carries parent_object + parent_record_id). The
        # adapter preserves this on Record.raw, so the conformance suite can
        # assert the note links to its parent across both providers.
        return {
            "id": {"note_id": note_id},
            "parent_object": parent_object,
            "parent_record_id": record_id,
            "title": title,
        }

    def _raise_attio_uniqueness(self) -> None:
        """Raise the Attio-shaped uniqueness-violation HTTP error (400 + a body
        naming the constraint) so AttioProvider's create/update maps it to the
        neutral UniquenessConflictError — the same path a real Attio
        ``is_unique`` rejection takes."""
        import httpx

        request = httpx.Request("POST", "/x")
        response = httpx.Response(
            400,
            request=request,
            content=b'{"error": "uniqueness_key value already exists"}',
        )
        raise httpx.HTTPStatusError("400", request=request, response=response)

    def _check_unique(
        self, object_type: str, attrs: dict[str, Any], *, exclude_id: str | None = None
    ) -> None:
        # Enforce the one constraint the engine depends on:
        # daily_run.uniqueness_key. Mirrors _UNIQUE_ATTRS in fake_provider.
        if object_type != "daily_run" or "uniqueness_key" not in attrs:
            return
        new_value = attrs["uniqueness_key"]
        for rid, raw in self._store(object_type).items():
            if rid == exclude_id:
                continue
            if self._first(raw, "uniqueness_key") == new_value:
                self._raise_attio_uniqueness()

    # Generic object API — AttioProvider calls the private _request
    def _request(
        self, method: str, path: str, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        # path forms: /objects/<obj>/records/query | /objects/<obj>/records |
        #             /objects/<obj>/records/<id>
        parts = path.strip("/").split("/")
        object_type = parts[1]
        if method == "POST" and parts[-1] == "query":
            body = json or {}
            rows = self._search(object_type, body.get("filter"), body.get("limit", 9999))
            return {"data": rows}
        if method == "POST":
            attrs = (json or {}).get("data", {}).get("values", {})
            self._check_unique(object_type, attrs)
            return {"data": self._create(object_type, attrs)}
        if method == "GET":
            rid = parts[-1]
            raw = self._store(object_type).get(rid)
            if raw is None:
                import httpx

                request = httpx.Request("GET", path)
                response = httpx.Response(404, request=request)
                raise httpx.HTTPStatusError("404", request=request, response=response)
            return {"data": raw}
        if method == "PATCH":
            rid = parts[-1]
            attrs = (json or {}).get("data", {}).get("values", {})
            self._check_unique(object_type, attrs, exclude_id=rid)
            return {"data": self._update(object_type, rid, attrs)}
        raise AssertionError(f"unexpected request: {method} {path}")

    # extract_record_info — resolve linked company off raw
    def extract_record_info(
        self, raw: dict[str, Any]
    ) -> tuple[str | None, str | None, str, str | None, str]:
        name = self._first(raw, "name")
        company_rid = self._first(raw, "company")
        company = None
        if company_rid:
            co = self._store("companies").get(company_rid)
            company = self._first(co, "name") if co else company_rid
        industry = self._first(raw, "industry")
        return (
            name or None,
            company,
            self._first(raw, "linkedin") or "",
            industry or None,
            self._first(raw, "job_title") or "",
        )


# ── Provider fixture (parametrized) ──────────────────────────────────────────


def _make_attio() -> AttioProvider:
    inner = _StatefulAttioClient()
    return AttioProvider(attio_client=inner)  # type: ignore[arg-type]


@pytest.fixture(params=["fake", "attio"])
def provider(request: pytest.FixtureRequest) -> CRMProvider:
    """A fresh, empty ``CRMProvider`` per param. New adapters: add a param here."""
    if request.param == "fake":
        return FakeProvider()
    return _make_attio()


def _seed_duplicate_entry(
    provider: CRMProvider, record_id: str, stage_name: str
) -> str:
    """Inject a legacy duplicate list entry directly into the provider's backing
    store (bypassing the idempotent ``add_list_entry``), so the "most-advanced
    duplicate wins" invariant can be exercised. Returns the seeded entry id.

    This is the one place the conformance suite reaches into provider internals
    — it has to, because the public API deliberately forbids creating duplicate
    entries. A new adapter added to the ``provider`` fixture must extend this
    helper with its own seeding path."""
    target = "__default__"
    if isinstance(provider, FakeProvider):
        entry_id = provider._next_id("entry")
        provider._entries[entry_id] = Entry(
            entry_id=entry_id,
            record_id=record_id,
            stage=Stage(name=stage_name, rank=None),  # rank recomputed on read
            attributes={"__list_id__": target},
            raw={"id": {"entry_id": entry_id}, "parent_record_id": record_id},
        )
        # Re-resolve the stage rank the same way add_list_entry would.
        from clients.crm.fake_provider import _stage_from_name as _fake_stage

        provider._entries[entry_id] = Entry(
            entry_id=entry_id,
            record_id=record_id,
            stage=_fake_stage(stage_name),
            attributes={"__list_id__": target},
            raw=provider._entries[entry_id].raw,
        )
        return entry_id
    if isinstance(provider, AttioProvider):
        inner = provider._attio
        assert isinstance(inner, _StatefulAttioClient)
        entry_id = inner._next_id("entry")
        inner._entries[entry_id] = {
            "id": {"entry_id": entry_id, "list_id": target},
            "parent_record_id": record_id,
            "entry_values": {"stage": [{"status": {"title": stage_name}}]},
        }
        return entry_id
    raise AssertionError(f"no seeding path for provider {type(provider)!r}")


# ── Conformance tests ────────────────────────────────────────────────────────


class TestPersonRoundTrip:
    def test_create_returns_record_with_id_and_object(
        self, provider: CRMProvider
    ) -> None:
        rec = provider.create_person({"linkedin": "https://li/in/a", "name": "Ada"})
        assert isinstance(rec, Record)
        assert rec.record_id  # non-empty, adapter-assigned
        assert rec.object == "people"

    def test_get_reads_back_created(self, provider: CRMProvider) -> None:
        created = provider.create_person({"name": "Ada"})
        fetched = provider.get_person(created.record_id)
        assert fetched is not None
        assert isinstance(fetched, Record)
        assert fetched.record_id == created.record_id
        assert fetched.attributes.get("name") == "Ada"

    def test_get_missing_returns_none(self, provider: CRMProvider) -> None:
        assert provider.get_person("does-not-exist") is None

    def test_update_patches_and_reads_back(self, provider: CRMProvider) -> None:
        created = provider.create_person({"name": "Ada", "job_title": "Eng"})
        updated = provider.update_person(created.record_id, {"job_title": "Manager"})
        assert isinstance(updated, Record)
        assert updated.attributes.get("job_title") == "Manager"
        # Unspecified field left untouched.
        assert updated.attributes.get("name") == "Ada"
        assert provider.get_person(created.record_id).attributes["job_title"] == "Manager"  # type: ignore[union-attr]

    def test_search_by_linkedin_finds_and_misses(self, provider: CRMProvider) -> None:
        provider.create_person({"linkedin": "https://li/in/ada", "name": "Ada"})
        found = provider.search_person_by_linkedin("https://li/in/ada")
        assert found is not None and found.object == "people"
        assert provider.search_person_by_linkedin("https://li/in/none") is None
        assert provider.search_person_by_linkedin("") is None


class TestUpsertIdempotency:
    def test_upsert_is_idempotent_on_match_key(self, provider: CRMProvider) -> None:
        a = provider.upsert_person("linkedin", {"linkedin": "https://li/x", "name": "A"})
        b = provider.upsert_person(
            "linkedin", {"linkedin": "https://li/x", "name": "A2"}
        )
        # Same record updated, not a duplicate.
        assert a.record_id == b.record_id
        assert b.attributes.get("name") == "A2"
        # Only one person with that linkedin exists.
        matches = provider.search_people(filter_={"linkedin": "https://li/x"})
        assert len(matches) == 1

    def test_upsert_creates_when_no_match(self, provider: CRMProvider) -> None:
        a = provider.upsert_person("linkedin", {"linkedin": "https://li/a"})
        b = provider.upsert_person("linkedin", {"linkedin": "https://li/b"})
        assert a.record_id != b.record_id


class TestBulkFetch:
    def test_returns_only_resolved_ids(self, provider: CRMProvider) -> None:
        r1 = provider.create_person({"name": "One"})
        r2 = provider.create_person({"name": "Two"})
        result = provider.bulk_fetch_persons({r1.record_id, r2.record_id, "ghost"})
        assert set(result.keys()) == {r1.record_id, r2.record_id}
        assert all(isinstance(v, Record) for v in result.values())

    def test_empty_request_empty_map(self, provider: CRMProvider) -> None:
        assert provider.bulk_fetch_persons(set()) == {}

    def test_metrics_sink_counters_reflect_fetch(
        self, provider: CRMProvider
    ) -> None:
        """Contract (base.py): a supplied ``metrics`` sink is bumped for
        requested / returned / failed by the canonical DailyRunMetrics counter
        names. Universal across both providers — the real AttioClient and Fake
        both bump ``bulk_fetch_records_{requested,returned,failed}``."""
        r1 = provider.create_person({"name": "One"})
        r2 = provider.create_person({"name": "Two"})

        # A minimal stand-in for workflows.metrics.DailyRunMetrics exposing only
        # the three contract counters (the adapter must guard for missing attrs,
        # but these are the names it MUST bump when present).
        class _Sink:
            bulk_fetch_records_requested = 0
            bulk_fetch_records_returned = 0
            bulk_fetch_records_failed = 0

        sink = _Sink()
        result = provider.bulk_fetch_persons(
            {r1.record_id, r2.record_id, "ghost"}, metrics=sink
        )

        assert set(result.keys()) == {r1.record_id, r2.record_id}
        # 3 requested, 2 resolved, 1 dropped — counters observe the partial miss.
        assert sink.bulk_fetch_records_requested == 3
        assert sink.bulk_fetch_records_returned == 2
        assert sink.bulk_fetch_records_failed == 1

    def test_metrics_sink_optional(self, provider: CRMProvider) -> None:
        """``metrics`` is optional: omitting it (None) must not raise."""
        r1 = provider.create_person({"name": "Solo"})
        assert set(provider.bulk_fetch_persons({r1.record_id}).keys()) == {
            r1.record_id
        }


class TestCompanyRoundTrip:
    def test_create_get_update(self, provider: CRMProvider) -> None:
        c = provider.create_company({"name": "Acme", "domain": "acme.com"})
        assert c.object == "companies"
        assert provider.get_company(c.record_id) is not None
        u = provider.update_company(c.record_id, {"name": "Acme2"})
        assert u.attributes.get("name") == "Acme2"

    def test_get_missing_none(self, provider: CRMProvider) -> None:
        assert provider.get_company("nope") is None

    def test_search_by_domain(self, provider: CRMProvider) -> None:
        provider.create_company({"name": "Acme", "domain": "acme.com"})
        assert provider.search_company_by_domain("acme.com") is not None
        assert provider.search_company_by_domain("other.com") is None


class TestDealRoundTrip:
    def test_create_get_update_search(self, provider: CRMProvider) -> None:
        d = provider.create_deal({"name": "D1", "stage": "Open"})
        assert d.object == "deals"
        assert provider.get_deal(d.record_id) is not None
        u = provider.update_deal(d.record_id, {"stage": "Won"})
        assert u.attributes.get("stage") == "Won"
        results = provider.search_deals()
        assert any(r.record_id == d.record_id for r in results)

    def test_get_missing_none(self, provider: CRMProvider) -> None:
        assert provider.get_deal("nope") is None


class TestListEntries:
    def test_add_returns_entry_with_stage(self, provider: CRMProvider) -> None:
        person = provider.create_person({"name": "Ada"})
        entry = provider.add_list_entry(person.record_id, "Connection Sent")
        assert isinstance(entry, Entry)
        assert entry.entry_id
        assert entry.record_id == person.record_id
        assert isinstance(entry.stage, Stage)
        assert entry.stage.name == "Connection Sent"
        assert entry.stage.rank == 1  # known stage resolves a rank

    def test_unknown_stage_rank_none(self, provider: CRMProvider) -> None:
        person = provider.create_person({"name": "X"})
        entry = provider.add_list_entry(person.record_id, "Totally Made Up")
        assert entry.stage.rank is None

    def test_add_is_idempotent_on_record_and_list(
        self, provider: CRMProvider
    ) -> None:
        person = provider.create_person({"name": "Ada"})
        e1 = provider.add_list_entry(person.record_id, "Connection Sent")
        e2 = provider.add_list_entry(person.record_id, "DM1 Sent")
        # Same entry patched, not a second entry created.
        assert e1.entry_id == e2.entry_id
        assert e2.stage.name == "DM1 Sent"
        entries = provider.query_list_entries()
        mine = [e for e in entries if e.record_id == person.record_id]
        assert len(mine) == 1

    def test_query_returns_entries(self, provider: CRMProvider) -> None:
        p1 = provider.create_person({"name": "A"})
        p2 = provider.create_person({"name": "B"})
        provider.add_list_entry(p1.record_id, "Connection Sent")
        provider.add_list_entry(p2.record_id, "Accepted")
        entries = provider.query_list_entries()
        assert all(isinstance(e, Entry) for e in entries)
        assert {e.record_id for e in entries} >= {p1.record_id, p2.record_id}

    def test_query_fail_if_truncated_raises(self, provider: CRMProvider) -> None:
        """PR-234: with ``fail_if_truncated=True`` a sweep that hits its limit
        with entries remaining raises the neutral ``ResultTruncatedError``
        instead of silently returning a partial set."""
        from clients.crm.exceptions import ResultTruncatedError

        for i in range(3):
            person = provider.create_person({"name": f"P{i}"})
            provider.add_list_entry(person.record_id, "Connection Sent")
        with pytest.raises(ResultTruncatedError):
            provider.query_list_entries(limit=1, fail_if_truncated=True)

    def test_query_fail_if_truncated_ok_within_limit(
        self, provider: CRMProvider
    ) -> None:
        person = provider.create_person({"name": "solo"})
        provider.add_list_entry(person.record_id, "Connection Sent")
        entries = provider.query_list_entries(limit=1000, fail_if_truncated=True)
        assert any(e.record_id == person.record_id for e in entries)

    def test_update_list_entry_changes_stage(self, provider: CRMProvider) -> None:
        person = provider.create_person({"name": "Ada"})
        entry = provider.add_list_entry(person.record_id, "Connection Sent")
        updated = provider.update_list_entry(entry.entry_id, {"stage": "Accepted"})
        assert isinstance(updated, Entry)
        assert updated.stage.name == "Accepted"
        assert updated.entry_id == entry.entry_id

    def test_most_advanced_entry_wins_on_duplicate(
        self, provider: CRMProvider
    ) -> None:
        """When legacy duplicate entries exist for a (record, list), add patches
        the most-advanced one by stage rank and leaves the rest.

        Duplicates can't be created via the idempotent ``add_list_entry`` API, so
        they are seeded directly into the provider's backing store via the
        provider-specific ``_seed_duplicate_entry`` helper below."""
        person = provider.create_person({"name": "Ada"})
        low_id = _seed_duplicate_entry(provider, person.record_id, "Connection Sent")
        high_id = _seed_duplicate_entry(provider, person.record_id, "DM2 Sent")

        result = provider.add_list_entry(person.record_id, "DM3 Sent")

        # The most-advanced (DM2, rank 4) entry is patched, not the low one.
        assert result.entry_id == high_id
        assert result.stage.name == "DM3 Sent"
        # The low entry is untouched (still Connection Sent).
        low_after = next(
            e
            for e in provider.query_list_entries()
            if e.entry_id == low_id
        )
        assert low_after.stage.name == "Connection Sent"


class TestNotes:
    def test_create_note_returns_record(self, provider: CRMProvider) -> None:
        person = provider.create_person({"name": "Ada"})
        note = provider.create_note(person.record_id, "Title", "Body")
        assert isinstance(note, Record)
        assert note.object == "notes"
        assert note.record_id  # adapter-assigned note id

    def test_create_note_links_to_parent(self, provider: CRMProvider) -> None:
        """Contract (base.py ``create_note``): the note attaches to ``record_id``
        of kind ``parent_object``. The created note must reflect that linkage.

        Universal across both providers via the untouched ``Record.raw`` payload:
        FakeProvider stores ``parent_object`` / ``parent_record_id`` there
        directly; AttioProvider preserves the vendor note-create body (which
        carries the parent it was created with) on ``raw``. The default parent
        object is ``"people"``; an explicit ``parent_object`` is honored."""
        person = provider.create_person({"name": "Ada"})
        note = provider.create_note(person.record_id, "Title", "Body")
        # Default parent object is people; the note links to the person record.
        assert note.raw.get("parent_object") == "people"
        assert note.raw.get("parent_record_id") == person.record_id

        company = provider.create_company({"name": "Acme"})
        co_note = provider.create_note(
            company.record_id, "T", "B", parent_object="companies"
        )
        # Explicit parent_object is carried through onto the created note.
        assert co_note.raw.get("parent_object") == "companies"
        assert co_note.raw.get("parent_record_id") == company.record_id


class TestExtractPersonInfo:
    def test_returns_recordinfo(self, provider: CRMProvider) -> None:
        company = provider.create_company({"name": "Acme"})
        person = provider.create_person(
            {
                "name": "Ada Lovelace",
                "linkedin": "https://li/in/ada",
                "job_title": "Plant Manager",
                "industry": "Manufacturing",
                "company": company.record_id,
            }
        )
        info = provider.extract_person_info(provider.get_person(person.record_id))  # type: ignore[arg-type]
        assert isinstance(info, RecordInfo)
        assert info.name == "Ada Lovelace"
        assert info.company == "Acme"
        assert info.linkedin_url == "https://li/in/ada"
        assert info.industry == "Manufacturing"
        assert info.title == "Plant Manager"

    def test_blank_linkedin_and_title_are_empty_string_not_none(
        self, provider: CRMProvider
    ) -> None:
        person = provider.create_person({"name": "NoLinks"})
        info = provider.extract_person_info(provider.get_person(person.record_id))  # type: ignore[arg-type]
        # Load-bearing: "" not None for these two (downstream truthiness checks).
        assert info.linkedin_url == ""
        assert info.linkedin_url is not None
        assert info.title == ""
        assert info.title is not None
        # name present here; company/industry absent → None (never sentinel str).
        assert info.company is None
        assert info.industry is None


class TestGenericObjectRecords:
    def test_create_get_update_round_trip(self, provider: CRMProvider) -> None:
        rec = provider.create_object_record(
            "operator_review_queue", {"status": "pending", "kind": "x"}
        )
        assert isinstance(rec, Record)
        assert rec.object == "operator_review_queue"
        fetched = provider.get_object_record("operator_review_queue", rec.record_id)
        assert fetched is not None
        assert fetched.attributes.get("status") == "pending"
        updated = provider.update_object_record(
            "operator_review_queue", rec.record_id, {"status": "done"}
        )
        assert updated.attributes.get("status") == "done"

    def test_get_missing_none(self, provider: CRMProvider) -> None:
        assert provider.get_object_record("operator_review_queue", "nope") is None

    def test_query_applies_equality_filter_and_normalizes(
        self, provider: CRMProvider
    ) -> None:
        provider.create_object_record("llm_budget_ledger", {"month": "jan", "spend": 1})
        provider.create_object_record("llm_budget_ledger", {"month": "feb", "spend": 2})
        rows = provider.query_object_records(
            "llm_budget_ledger", filters={"month": "jan"}
        )
        assert all(isinstance(r, Record) for r in rows)
        assert all(r.object == "llm_budget_ledger" for r in rows)
        assert len(rows) == 1
        assert rows[0].attributes.get("month") == "jan"

    def test_query_no_filter_returns_all(self, provider: CRMProvider) -> None:
        provider.create_object_record("migration_run", {"n": 1})
        provider.create_object_record("migration_run", {"n": 2})
        rows = provider.query_object_records("migration_run")
        assert len(rows) == 2


class TestUniquenessContract:
    """The neutral uniqueness-conflict signal. A write that violates the
    vendor's uniqueness constraint on ``daily_run.uniqueness_key`` raises the
    contract's ``UniquenessConflictError`` — NOT a raw vendor error — so a
    caller (``workflows/daily_run.py``'s concurrent-run guard) branches on
    meaning. Asserted against BOTH reference providers."""

    def test_create_duplicate_unique_key_raises(
        self, provider: CRMProvider
    ) -> None:
        provider.create_object_record(
            "daily_run", {"uniqueness_key": "2026-06-09|laptop-A", "messages_sent": 0}
        )
        with pytest.raises(UniquenessConflictError):
            provider.create_object_record(
                "daily_run",
                {"uniqueness_key": "2026-06-09|laptop-A", "messages_sent": 0},
            )

    def test_create_distinct_unique_keys_both_succeed(
        self, provider: CRMProvider
    ) -> None:
        a = provider.create_object_record(
            "daily_run", {"uniqueness_key": "2026-06-09|laptop-A"}
        )
        b = provider.create_object_record(
            "daily_run", {"uniqueness_key": "2026-06-09|laptop-B"}
        )
        assert a.record_id != b.record_id

    def test_update_into_held_unique_key_raises(
        self, provider: CRMProvider
    ) -> None:
        provider.create_object_record(
            "daily_run", {"uniqueness_key": "running|2026-06-09|laptop-A"}
        )
        other = provider.create_object_record(
            "daily_run", {"uniqueness_key": "completed|2026-06-09|laptop-A|rec_x"}
        )
        # Re-keying `other` onto the held running-lock collides (the reopen-race).
        with pytest.raises(UniquenessConflictError):
            provider.update_object_record(
                "daily_run",
                other.record_id,
                {"uniqueness_key": "running|2026-06-09|laptop-A"},
            )

    def test_update_rewrites_own_unique_key(self, provider: CRMProvider) -> None:
        """Re-writing a record's OWN current unique value is not a collision
        (idempotent self-update) — only a DIFFERENT record holding the value is."""
        rec = provider.create_object_record(
            "daily_run", {"uniqueness_key": "2026-06-09|laptop-A", "messages_sent": 0}
        )
        provider.update_object_record(
            "daily_run",
            rec.record_id,
            {"uniqueness_key": "2026-06-09|laptop-A", "messages_sent": 5},
        )  # no raise
