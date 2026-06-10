"""``FakeProvider`` — an in-memory reference :class:`CRMProvider`.

This is a self-contained, pure-Python implementation of the vendor-neutral
:class:`~clients.crm.base.CRMProvider` contract. It exists for two reasons:

1. **Proof the contract is implementable without a vendor.** If a behavior can
   only be satisfied by Attio's idiosyncrasies, it doesn't belong on the ABC —
   the fact that this dict-backed store honors every documented invariant is
   the evidence that the seam is genuinely vendor-neutral.
2. **A test double for future work.** Workflow slices migrating onto
   ``CRMProvider`` (see ``clients/crm/CONTRACT.md`` §Migration) can construct a
   ``FakeProvider`` instead of mocking Attio, getting real round-trip
   create/update/query semantics with zero network and zero vendor SDK.

It runs the same conformance suite (``tests/crm/test_provider_contract.py``)
as the reference ``AttioProvider``.

Design
------
The backing store is a set of plain dicts keyed by id, one per object type,
plus an entry store keyed by ``entry_id``. Ids are monotonic per object kind
(``people_1``, ``companies_2``, …) so they are stable and human-legible in test
failures. Everything is in-process; there is no I/O, no threading, no vendor
SDK import.

Filter support — deliberately minimal
-------------------------------------
``search_*`` and ``query_object_records`` accept a filter argument. The real
contract documents that ``query_object_records``' ``filters`` is a
**vendor-native query body** (Attio's filter DSL — the documented "filter-shape
leak"), and ``search_*``' ``filter_`` is likewise vendor-shaped. A faithful
fake cannot reimplement Attio's full DSL, and the contract does not define a
neutral one. So this fake implements only a **minimal flat-equality subset**:
a filter dict of ``{slug: value}`` matches records whose flattened
``attributes[slug] == value`` (every key must match — implicit AND). Anything
richer (``$and``/``$or``/``$not``/nested operator dicts) is intentionally
**not** understood and is treated as "no constraint expressible here," so the
fake returns the full (limit-bounded) set rather than silently mis-filtering.
This is enough for tests to exercise deterministic filtering of the contract's
return-normalization; it is NOT a model of any vendor's query language.
"""

from __future__ import annotations

import itertools
from typing import Any

from clients.crm.base import CRMProvider, Entry, Record, RecordInfo, Stage
from clients.crm.exceptions import UniquenessConflictError
from models.pipeline import STAGE_RANK, PipelineStage

_DEFAULT_LIST = "__default__"

# Object slugs whose named attribute the fake enforces as UNIQUE on
# create/update — enough to simulate the one vendor uniqueness constraint the
# engine actually depends on (``daily_run.uniqueness_key``, the cross-machine
# concurrent-run lock). Keyed by object slug → the unique attribute. A write
# that would land a duplicate value on this attribute raises
# :class:`UniquenessConflictError`, mirroring the AttioProvider mapping, so
# ``open_daily_run`` / ``attach_daily_run`` exercise their collision branches
# against the fake with the same neutral exception they get from Attio.
_UNIQUE_ATTRS: dict[str, str] = {"daily_run": "uniqueness_key"}


def _stage_from_name(stage_name: str) -> Stage:
    """Build a :class:`Stage` from a label, resolving ``rank`` via the canonical
    ``STAGE_RANK`` map; an unrecognized label yields ``rank=None`` (the
    contract's "unknown stage → caller treats as rank-0/oldest" case)."""
    try:
        rank: int | None = STAGE_RANK[PipelineStage(stage_name)]
    except (KeyError, ValueError):
        rank = None
    return Stage(name=stage_name, rank=rank)


def _matches(attributes: dict[str, Any], filter_: dict[str, Any] | None) -> bool:
    """Minimal flat-equality match (see module docstring).

    A ``None`` / empty filter matches everything. A filter whose values are not
    plain scalars (i.e. an operator/nested dict — a richer vendor DSL we do not
    model) is treated as non-constraining for those keys, so the record still
    matches. Scalar keys must compare equal.
    """
    if not filter_:
        return True
    for key, expected in filter_.items():
        if isinstance(expected, (dict, list)):
            # A vendor-DSL operator shape we deliberately don't interpret.
            continue
        if attributes.get(key) != expected:
            return False
    return True


class FakeProvider(CRMProvider):
    """In-memory :class:`CRMProvider`. No network, no vendor SDK.

    Backing store: per-object-type ``{record_id: Record}`` dicts and a single
    ``{entry_id: Entry}`` dict for list entries. Ids are assigned from a
    per-kind monotonic counter.
    """

    def __init__(self) -> None:
        self._records: dict[str, dict[str, Record]] = {}
        self._entries: dict[str, Entry] = {}
        self._counters: dict[str, itertools.count[int]] = {}

    # ── Internal helpers ──────────────────────────────────────

    def _next_id(self, kind: str) -> str:
        counter = self._counters.setdefault(kind, itertools.count(1))
        return f"{kind}_{next(counter)}"

    def _store(self, object_: str) -> dict[str, Record]:
        return self._records.setdefault(object_, {})

    def _enforce_uniqueness(
        self, object_: str, attributes: dict[str, Any], *, exclude_id: str | None = None
    ) -> None:
        """Raise :class:`UniquenessConflictError` if ``attributes`` would land a
        duplicate value on ``object_``'s registered unique attribute.

        ``exclude_id`` is the record being updated (it may legitimately re-write
        its own current value); a create passes ``None``. Only objects in
        :data:`_UNIQUE_ATTRS` are constrained — everything else is unconstrained,
        matching the contract (the fake models only the one constraint the engine
        depends on).
        """
        attr = _UNIQUE_ATTRS.get(object_)
        if attr is None or attr not in attributes:
            return
        new_value = attributes[attr]
        for rid, rec in self._store(object_).items():
            if rid == exclude_id:
                continue
            if rec.attributes.get(attr) == new_value:
                raise UniquenessConflictError(object_, attribute=attr)

    def _create(self, object_: str, attributes: dict[str, Any]) -> Record:
        self._enforce_uniqueness(object_, attributes)
        record_id = self._next_id(object_)
        record = Record(
            record_id=record_id,
            object=object_,
            attributes=dict(attributes),
            raw={"id": {"record_id": record_id}, "values": dict(attributes)},
        )
        self._store(object_)[record_id] = record
        return record

    def _update(
        self, object_: str, record_id: str, attributes: dict[str, Any]
    ) -> Record:
        self._enforce_uniqueness(object_, attributes, exclude_id=record_id)
        store = self._store(object_)
        existing = store.get(record_id)
        merged = dict(existing.attributes) if existing else {}
        merged.update(attributes)
        record = Record(
            record_id=record_id,
            object=object_,
            attributes=merged,
            raw={"id": {"record_id": record_id}, "values": merged},
        )
        store[record_id] = record
        return record

    # ── People records ────────────────────────────────────────

    def search_people(
        self, filter_: dict[str, Any] | None = None, limit: int = 50
    ) -> list[Record]:
        matched = [
            r for r in self._store("people").values() if _matches(r.attributes, filter_)
        ]
        return matched[:limit]

    def get_person(self, record_id: str) -> Record | None:
        return self._store("people").get(record_id)

    def bulk_fetch_persons(
        self, record_ids: set[str], *, metrics: Any = None
    ) -> dict[str, Record]:
        store = self._store("people")
        result = {rid: store[rid] for rid in record_ids if rid in store}
        if metrics is not None:
            # Observability: bump the canonical DailyRunMetrics counters
            # (workflows/metrics.py), the same names the real AttioClient bumps,
            # so a single sink works identically across adapters. Guarded by
            # hasattr so a partial/stub sink doesn't raise.
            for name, value in (
                ("bulk_fetch_records_requested", len(record_ids)),
                ("bulk_fetch_records_returned", len(result)),
                ("bulk_fetch_records_failed", len(record_ids) - len(result)),
            ):
                if hasattr(metrics, name):
                    setattr(metrics, name, getattr(metrics, name) + value)
        return result

    def search_person_by_linkedin(self, linkedin_url: str) -> Record | None:
        if not linkedin_url:
            return None
        for record in self._store("people").values():
            if record.attributes.get("linkedin") == linkedin_url:
                return record
        return None

    def create_person(self, attributes: dict[str, Any]) -> Record:
        return self._create("people", attributes)

    def update_person(self, record_id: str, attributes: dict[str, Any]) -> Record:
        return self._update("people", record_id, attributes)

    def upsert_person(
        self, matching_attribute: str, attributes: dict[str, Any]
    ) -> Record:
        match_value = attributes.get(matching_attribute)
        if match_value is not None:
            for record in self._store("people").values():
                if record.attributes.get(matching_attribute) == match_value:
                    return self._update("people", record.record_id, attributes)
        return self._create("people", attributes)

    # ── Company records ──────────────────────────────────────

    def search_companies(
        self, filter_: dict[str, Any] | None = None, limit: int = 50
    ) -> list[Record]:
        matched = [
            r
            for r in self._store("companies").values()
            if _matches(r.attributes, filter_)
        ]
        return matched[:limit]

    def get_company(self, record_id: str) -> Record | None:
        return self._store("companies").get(record_id)

    def create_company(self, attributes: dict[str, Any]) -> Record:
        return self._create("companies", attributes)

    def update_company(self, record_id: str, attributes: dict[str, Any]) -> Record:
        return self._update("companies", record_id, attributes)

    def search_company_by_domain(self, domain: str) -> Record | None:
        if not domain:
            return None
        for record in self._store("companies").values():
            if record.attributes.get("domain") == domain:
                return record
        return None

    # ── Deal records ──────────────────────────────────────────

    def get_deal(self, record_id: str) -> Record | None:
        return self._store("deals").get(record_id)

    def create_deal(self, attributes: dict[str, Any]) -> Record:
        return self._create("deals", attributes)

    def update_deal(self, record_id: str, attributes: dict[str, Any]) -> Record:
        return self._update("deals", record_id, attributes)

    def search_deals(
        self, filter_: dict[str, Any] | None = None, limit: int = 500
    ) -> list[Record]:
        matched = [
            r for r in self._store("deals").values() if _matches(r.attributes, filter_)
        ]
        return matched[:limit]

    # ── List entries (the pipeline) ───────────────────────────

    def query_list_entries(
        self,
        list_id: str | None = None,
        filter_: dict[str, Any] | None = None,
        limit: int = 50000,
    ) -> list[Entry]:
        target = list_id or _DEFAULT_LIST
        matched = [
            e
            for e in self._entries.values()
            if e.attributes.get("__list_id__") == target
            and _matches(e.attributes, filter_)
        ]
        return matched[:limit]

    def add_list_entry(
        self,
        record_id: str,
        stage_name: str,
        entry_attributes: dict[str, Any] | None = None,
        list_id: str | None = None,
        existing_entries: list[Entry] | None = None,
    ) -> Entry:
        target = list_id or _DEFAULT_LIST
        attrs = dict(entry_attributes or {})

        # Idempotency on (record_id, list): find existing entries for this pair.
        # Prefer a caller-supplied pre-fetched list (batch-flow optimization),
        # else scan the store.
        pool = (
            existing_entries
            if existing_entries is not None
            else list(self._entries.values())
        )
        candidates = [
            e
            for e in pool
            if e.record_id == record_id
            and e.attributes.get("__list_id__") == target
        ]

        if candidates:
            # Most-advanced-entry wins: pick the highest stage rank (unknown
            # rank → treated as oldest/0), patch it, leave the rest.
            winner = max(candidates, key=lambda e: (e.stage.rank or 0))
            return self._patch_entry(winner.entry_id, stage_name, attrs)

        entry_id = self._next_id("entry")
        stored_attrs = {**attrs, "__list_id__": target}
        entry = Entry(
            entry_id=entry_id,
            record_id=record_id,
            stage=_stage_from_name(stage_name),
            attributes=stored_attrs,
            raw={"id": {"entry_id": entry_id}, "parent_record_id": record_id},
        )
        self._entries[entry_id] = entry
        return entry

    def _patch_entry(
        self, entry_id: str, stage_name: str, attrs: dict[str, Any]
    ) -> Entry:
        existing = self._entries[entry_id]
        merged = dict(existing.attributes)
        merged.update(attrs)
        stage = _stage_from_name(stage_name) if stage_name else existing.stage
        entry = Entry(
            entry_id=entry_id,
            record_id=existing.record_id,
            stage=stage,
            attributes=merged,
            raw=existing.raw,
        )
        self._entries[entry_id] = entry
        return entry

    def update_list_entry(
        self,
        entry_id: str,
        entry_attributes: dict[str, Any],
        list_id: str | None = None,
    ) -> Entry:
        attrs = dict(entry_attributes)
        # A "stage" key in the attrs updates the entry's stage (mirrors how the
        # engine writes stage via update_list_entry's attrs).
        stage_name = attrs.get("stage", "")
        return self._patch_entry(entry_id, stage_name, attrs)

    # ── Notes ─────────────────────────────────────────────────

    def create_note(
        self,
        record_id: str,
        title: str,
        content: str,
        parent_object: str = "people",
    ) -> Record:
        note_id = self._next_id("note")
        return Record(
            record_id=note_id,
            object="notes",
            attributes={"title": title, "content": content},
            raw={
                "id": {"note_id": note_id},
                "parent_object": parent_object,
                "parent_record_id": record_id,
                "title": title,
                "content": content,
            },
        )

    # ── Generic object-record API ─────────────────────────────

    def query_object_records(
        self,
        object_type: str,
        *,
        filters: dict[str, Any] | None = None,
        sorts: list[Any] | None = None,
        limit: int | None = None,
    ) -> list[Record]:
        matched = [
            r
            for r in self._store(object_type).values()
            if _matches(r.attributes, filters)
        ]
        return matched if limit is None else matched[:limit]

    def get_object_record(self, object_type: str, record_id: str) -> Record | None:
        return self._store(object_type).get(record_id)

    def create_object_record(
        self, object_type: str, attributes: dict[str, Any]
    ) -> Record:
        return self._create(object_type, attributes)

    def update_object_record(
        self, object_type: str, record_id: str, attributes: dict[str, Any]
    ) -> Record:
        return self._update(object_type, record_id, attributes)

    # ── Normalized person read ────────────────────────────────

    def extract_person_info(self, record: Record) -> RecordInfo:
        attrs = record.attributes
        # Resolve employer off a linked company reference if present, else the
        # flat "company" attribute (string).
        company: str | None = None
        company_ref = attrs.get("company")
        if isinstance(company_ref, str) and company_ref:
            linked = self._store("companies").get(company_ref)
            company = (
                linked.attributes.get("name") if linked is not None else company_ref
            )

        def _opt(value: Any) -> str | None:
            return value if value else None

        return RecordInfo(
            name=_opt(attrs.get("name")),
            company=company,
            # "" not None for these two (contract: downstream truthiness checks).
            linkedin_url=attrs.get("linkedin") or "",
            industry=_opt(attrs.get("industry")),
            title=attrs.get("job_title") or "",
        )
