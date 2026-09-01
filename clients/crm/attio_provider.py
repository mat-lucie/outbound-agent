"""Attio reference adapter — ``AttioProvider(CRMProvider)``.

This is the reference implementation of the vendor-neutral
:class:`~clients.crm.base.CRMProvider` contract. It is BOTH (a) a faithful
wrapper that preserves today's Attio behavior exactly and (b) the worked
example an onboarding agent reads to generate adapters for other CRMs.

Composition over inheritance
----------------------------
``AttioProvider`` *composes* an :class:`~clients.attio.AttioClient` instance
(``self._attio``) rather than subclassing it. The inner client owns all
HTTP/auth/retry/pagination behavior; this adapter adds only the normalization
layer that turns raw Attio JSON into the contract's neutral dataclasses
(:class:`~clients.crm.base.Record` / :class:`~clients.crm.base.Entry` /
:class:`~clients.crm.base.RecordInfo`). Writes go straight through to the inner
client unchanged — Attio already accepts the flat ``slug -> value`` attr dicts
the contract specifies.

Vendor quirks stay HERE
-----------------------
Per the contract's quirk-isolation rule, Attio's data-quality and URL-matching
quirks never leak onto the ABC:

* LinkedIn-Clearbit corruption detection — :meth:`is_person_company_corrupted`
  (delegated to the inner client, whose corruption caches are populated as a
  side effect of :meth:`extract_person_info`).
* LinkedIn-URL canonicalization / variant matching — handled inside the inner
  client's ``search_person_by_linkedin`` / ``upsert_person``; not re-exposed.

``max_workers`` — the contract dropped it from ``bulk_fetch_persons``; the
adapter keeps it as an internal default (:data:`_BULK_FETCH_MAX_WORKERS`).

Attribute flattening (``Record.attributes``)
---------------------------------------------
The contract types ``Record.attributes`` as a flat ``slug -> value`` map. Attio
object-record JSON stores every field as ``values[slug] = [ {…}, … ]`` (a list
of value objects). We flatten by taking the FIRST value of each slug via
:meth:`AttioClient.object_record_first_value` — the same reader the live
object-record call sites (Operator Review Queue, Migration Run, Data Quality
Report) already use. This unwraps plain ``value`` and ``status.title`` shapes
to a scalar and is intentionally lossy on multi-value / structured slugs (e.g.
``name`` first/last parts, ``company`` references): callers that need those
structured shapes read them off :attr:`Record.raw` (which carries the untouched
payload) or via the typed helper :meth:`extract_person_info`. This mirrors
today's behavior, where structured reads always went through ``raw`` / the
dedicated parsers, never a generic flat map.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from clients.attio import AttioClient, AttioResultTruncated
from clients.crm.base import CRMProvider, Entry, Record, RecordInfo, Stage
from clients.crm.exceptions import ResultTruncatedError, UniquenessConflictError
from clients.crm.mapping import CRMMapping, default_attio_mapping

if TYPE_CHECKING:
    from collections.abc import Iterable

# Attio-internal concurrency tuning for bulk person fetch. The contract drops
# this knob from the signature (see module docstring); we keep the inner
# client's historical default of 8 so behavior is byte-for-byte unchanged.
_BULK_FETCH_MAX_WORKERS = 8


def _is_attio_uniqueness_violation(exc: httpx.HTTPStatusError) -> bool:
    """True iff an Attio HTTP error is a uniqueness-constraint violation.

    Attio surfaces a write that collides with an ``is_unique=True`` attribute as
    an HTTP 400 (occasionally 409) whose body names a uniqueness / duplicate /
    already-exists condition. This is the same shape ``workflows/daily_run.py``'s
    historical ``_is_uniqueness_collision`` matched against the raw httpx error;
    the logic is absorbed here so the *adapter* (not the workflow) owns the
    vendor-body inspection and re-raises the neutral
    :class:`UniquenessConflictError`.
    """
    if exc.response.status_code not in (400, 409):
        return False
    try:
        body = exc.response.text.lower()
    except Exception:  # noqa: BLE001 — body access must never mask the 4xx
        return False
    return "unique" in body or "duplicate" in body or "already exists" in body


class AttioProvider(CRMProvider):
    """Reference :class:`CRMProvider` over the Attio REST API v2.

    Wraps an :class:`~clients.attio.AttioClient` (composition) and normalizes
    its raw-JSON returns into the contract's neutral dataclasses. Construct
    either with an explicit client (tests inject a mock) or let it build one
    from ``ATTIO_API_KEY``.
    """

    def __init__(
        self,
        attio_client: AttioClient | None = None,
        *,
        api_key: str | None = None,
        mapping: CRMMapping | None = None,
    ) -> None:
        self._attio = attio_client or AttioClient(api_key=api_key)
        # The field/stage translation table. Defaults to the IDENTITY mapping
        # (:func:`default_attio_mapping`), under which every stage translation
        # is a no-op and behavior is byte-identical to the pre-mapping adapter —
        # this is why every existing ``AttioProvider(mock_client)`` construction
        # in the suite stays unchanged.
        self._mapping = mapping if mapping is not None else default_attio_mapping()

    # ── Stage translation ─────────────────────────────────────

    def _stage_from_name(self, vendor_label: str) -> Stage:
        """Build a normalized :class:`Stage` from a raw vendor stage label.

        Translates the vendor option label to the engine-canonical stage value
        via the mapping (``Stage.name`` is ALWAYS the canonical value — every
        downstream consumer does ``PipelineStage(entry.stage.name)``), and
        resolves ``rank`` from the canonical value. An unrecognized label passes
        through unchanged with ``rank=None`` (the contract's "caller treats
        unknown as rank-0/oldest" case). Under the identity default the canonical
        value equals the vendor label and ``rank`` equals the ``STAGE_RANK``
        lookup, so this is byte-identical to the pre-mapping ``_stage_from_name``.
        """
        canonical = self._mapping.to_canonical_stage(vendor_label)
        return Stage(name=canonical, rank=self._mapping.rank_for_canonical(canonical))

    # ── Normalization helpers ─────────────────────────────────

    @staticmethod
    def _to_record(
        raw: Any, object_: str, *, fallback_id: str = ""
    ) -> Record:
        """Normalize one raw Attio object-record JSON into a :class:`Record`.

        ``record_id`` ← ``id.record_id``; ``attributes`` ← first-value-per-slug
        flattening of ``values`` (see module docstring); ``raw`` keeps the
        untouched payload for structured / escape-hatch reads.

        **Tolerance (adapters must not crash on empty bodies).** A real Attio
        API can return an empty/odd 200 body on a PATCH/POST, and test doubles
        often stub ``None`` / ``{}`` / a bare list. This normalizer therefore
        NEVER raises on a structurally-empty or malformed payload: ``None``, a
        non-dict body (e.g. a list from ``{"data": []}`` unwrapping), a dict
        missing ``id`` / with ``id`` not a dict, or ``values`` that is not a
        dict all degrade to a best-effort :class:`Record`. ``fallback_id`` is
        the record id the caller already knows (e.g. the id passed to an
        ``update_*`` / ``get_*``) and is used as ``record_id`` whenever the body
        carries no usable one. ``raw`` preserves whatever body came back (or
        ``{}`` for ``None``). Well-formed bodies are normalized exactly as
        before — this only adds the degraded branch.
        """
        body: dict[str, Any] = raw if isinstance(raw, dict) else {}
        id_obj = body.get("id")
        record_id = (
            id_obj.get("record_id", "") if isinstance(id_obj, dict) else ""
        ) or fallback_id
        values = body.get("values")
        if not isinstance(values, dict):
            values = {}
        attributes = {
            slug: AttioClient.object_record_first_value(values, slug)
            for slug in values
        }
        return Record(
            record_id=record_id,
            object=object_,
            attributes=attributes,
            raw=body,
        )

    def _to_entry(self, raw: Any, *, fallback_entry_id: str = "") -> Entry:
        """Normalize one raw Attio list-entry JSON into an :class:`Entry`.

        Folds in ``AttioClient.parse_entry``: ``entry_id`` / ``record_id`` and
        the flat attribute map come straight from its output, ``stage`` is the
        parsed stage label wrapped in a ranked :class:`Stage`, and the rest of
        the parsed dict (persona, dm_step, timestamps, scoring signals, …)
        becomes ``Entry.attributes``. ``raw`` keeps the untouched payload.

        **Tolerance (adapters must not crash on empty bodies).** Like
        :meth:`_to_record`, this never raises on a ``None`` / non-dict /
        empty-shaped list-entry body (an empty 200 from an entry PATCH, a
        stubbed ``None`` in a test double). A non-dict body degrades to an empty
        entry; ``fallback_entry_id`` is used as ``entry_id`` when the body
        carries no usable one (e.g. the ``entry_id`` passed to
        :meth:`update_list_entry`). Well-formed bodies parse exactly as before.
        """
        body: dict[str, Any] = raw if isinstance(raw, dict) else {}
        parsed = AttioClient.parse_entry(body)
        if fallback_entry_id and not parsed.get("entry_id"):
            parsed["entry_id"] = fallback_entry_id
        entry_id = parsed.get("entry_id", "")
        record_id = parsed.get("record_id", "")
        stage = self._stage_from_name(parsed.get("stage", "") or "")
        # Everything parse_entry produced except identity + stage becomes the
        # flat attribute map — same slug-keyed shape parse_entry returns today.
        attributes = {
            k: v
            for k, v in parsed.items()
            if k not in ("entry_id", "record_id", "stage")
        }
        return Entry(
            entry_id=entry_id,
            record_id=record_id,
            stage=stage,
            attributes=attributes,
            raw=raw,
        )

    # ── People records ────────────────────────────────────────

    def search_people(
        self,
        filter_: dict[str, Any] | None = None,
        limit: int = 50,
        *,
        fail_if_truncated: bool = False,
    ) -> list[Record]:
        # Map the vendor-native truncation signal to the neutral contract
        # exception so callers catch ResultTruncatedError, not an Attio type
        # (same mapping query_list_entries does).
        try:
            raw = self._attio.search_people(
                filter_=filter_, limit=limit, fail_if_truncated=fail_if_truncated
            )
        except AttioResultTruncated as exc:
            raise ResultTruncatedError(str(exc)) from exc
        return [self._to_record(r, "people") for r in raw]

    def get_person(self, record_id: str) -> Record | None:
        raw = self._attio.get_person(record_id)
        return (
            self._to_record(raw, "people", fallback_id=record_id)
            if raw is not None
            else None
        )

    def bulk_fetch_persons(
        self, record_ids: set[str], *, metrics: Any = None
    ) -> dict[str, Record]:
        raw_map = self._attio.bulk_fetch_persons_by_record_ids(
            record_ids,
            max_workers=_BULK_FETCH_MAX_WORKERS,
            metrics=metrics,
        )
        return {rid: self._to_record(raw, "people") for rid, raw in raw_map.items()}

    def prefetch_companies_for_persons(
        self, records: Iterable[Record], *, metrics: Any = None
    ) -> int:
        """Warm the inner client's company caches for these persons' employers.

        Attio's ``extract_record_info`` follows the person's ``company``
        record reference with one blocking GET per first-seen company,
        serially — the dominant cost of a large pipeline preload. Harvest the
        distinct linked company ids off the untouched payloads
        (``Record.raw``) and fan them out through the client's bounded pool so
        the per-person resolves that follow are cache hits.

        Fail-open per the contract: ``bulk_prime_company_caches`` isolates
        per-company failures internally, and an unwarmed company still
        resolves lazily in ``extract_person_info``.
        """
        company_ids = {
            cid
            for record in records
            if (cid := AttioClient.person_company_ref_id(record.raw)) is not None
        }
        if not company_ids:
            return 0
        return self._attio.bulk_prime_company_caches(company_ids, metrics=metrics)

    def search_person_by_linkedin(self, linkedin_url: str) -> Record | None:
        raw = self._attio.search_person_by_linkedin(linkedin_url)
        return self._to_record(raw, "people") if raw is not None else None

    def create_person(self, attributes: dict[str, Any]) -> Record:
        return self._to_record(self._attio.create_person(attributes), "people")

    def update_person(self, record_id: str, attributes: dict[str, Any]) -> Record:
        return self._to_record(
            self._attio.update_person(record_id, attributes),
            "people",
            fallback_id=record_id,
        )

    def upsert_person(
        self, matching_attribute: str, attributes: dict[str, Any]
    ) -> Record:
        return self._to_record(
            self._attio.upsert_person(matching_attribute, attributes), "people"
        )

    # ── Company records ──────────────────────────────────────

    def search_companies(
        self, filter_: dict[str, Any] | None = None, limit: int = 50
    ) -> list[Record]:
        return [
            self._to_record(r, "companies")
            for r in self._attio.search_companies(filter_=filter_, limit=limit)
        ]

    def get_company(self, record_id: str) -> Record | None:
        raw = self._attio.get_company(record_id)
        return (
            self._to_record(raw, "companies", fallback_id=record_id)
            if raw is not None
            else None
        )

    def create_company(self, attributes: dict[str, Any]) -> Record:
        return self._to_record(self._attio.create_company(attributes), "companies")

    def update_company(self, record_id: str, attributes: dict[str, Any]) -> Record:
        return self._to_record(
            self._attio.update_company(record_id, attributes),
            "companies",
            fallback_id=record_id,
        )

    def search_company_by_domain(self, domain: str) -> Record | None:
        raw = self._attio.search_company_by_domain(domain)
        return self._to_record(raw, "companies") if raw is not None else None

    # ── Deal records ──────────────────────────────────────────

    def get_deal(self, record_id: str) -> Record | None:
        raw = self._attio.get_deal(record_id)
        return (
            self._to_record(raw, "deals", fallback_id=record_id)
            if raw is not None
            else None
        )

    def create_deal(self, attributes: dict[str, Any]) -> Record:
        return self._to_record(self._attio.create_deal(attributes), "deals")

    def update_deal(self, record_id: str, attributes: dict[str, Any]) -> Record:
        return self._to_record(
            self._attio.update_deal(record_id, attributes),
            "deals",
            fallback_id=record_id,
        )

    def search_deals(
        self, filter_: dict[str, Any] | None = None, limit: int = 500
    ) -> list[Record]:
        return [
            self._to_record(r, "deals")
            for r in self._attio.search_deals(filter_=filter_, limit=limit)
        ]

    # ── List entries (the pipeline) ───────────────────────────

    def query_list_entries(
        self,
        list_id: str | None = None,
        filter_: dict[str, Any] | None = None,
        limit: int = 50000,
        *,
        fail_if_truncated: bool = False,
    ) -> list[Entry]:
        # Map the vendor-native truncation signal to the neutral contract
        # exception so callers catch ResultTruncatedError, not an Attio type.
        try:
            raw_entries = self._attio.query_list_entries(
                list_id=list_id, filter_=filter_, limit=limit,
                fail_if_truncated=fail_if_truncated,
            )
        except AttioResultTruncated as exc:
            raise ResultTruncatedError(str(exc)) from exc
        return [self._to_entry(e) for e in raw_entries]

    def add_list_entry(
        self,
        record_id: str,
        stage_name: str,
        entry_attributes: dict[str, Any] | None = None,
        list_id: str | None = None,
        existing_entries: list[Entry] | None = None,
    ) -> Entry:
        # The inner client's upsert dedupe works against raw entry JSON; thread
        # the normalized entries' raw payloads back through so the client-side
        # filter/rank runs against the same shapes it always has.
        raw_existing: list[dict[str, Any]] | None = (
            [e.raw for e in existing_entries] if existing_entries is not None else None
        )
        # The engine passes a canonical stage value; translate it to the vendor
        # option label before handing it to the inner client. No-op under the
        # identity default (canonical == vendor label).
        raw = self._attio.add_list_entry(
            record_id=record_id,
            stage_name=self._mapping.to_vendor_stage(stage_name),
            entry_attributes=entry_attributes,
            list_id=list_id,
            existing_entries=raw_existing,
        )
        return self._to_entry(raw)

    def update_list_entry(
        self,
        entry_id: str,
        entry_attributes: dict[str, Any],
        list_id: str | None = None,
    ) -> Entry:
        # If the attrs carry a canonical stage value under the literal "stage"
        # key, translate it to the vendor option label. Copy the dict so the
        # caller's payload is never mutated. No-op under the identity default,
        # and untouched when there is no "stage" key. (The configurable
        # stage_field slug is Commit 2; this commit uses the literal "stage".)
        if "stage" in entry_attributes:
            entry_attributes = {
                **entry_attributes,
                "stage": self._mapping.to_vendor_stage(entry_attributes["stage"]),
            }
        raw = self._attio.update_list_entry(
            entry_id=entry_id,
            entry_attributes=entry_attributes,
            list_id=list_id,
        )
        return self._to_entry(raw, fallback_entry_id=entry_id)

    # ── Notes ─────────────────────────────────────────────────

    def create_note(
        self,
        record_id: str,
        title: str,
        content: str,
        parent_object: str = "people",
    ) -> Record:
        # A note is not an object-record; the API returns a note payload whose
        # id carries note_id (not record_id). We wrap it as a Record with the
        # raw payload intact, object="notes", matching the contract's "Record
        # describing the created note" promise without inventing a note type.
        raw = self._attio.create_note(
            record_id=record_id,
            title=title,
            content=content,
            parent_object=parent_object,
        )
        note_id = raw.get("id", {}).get("note_id", "") if isinstance(raw, dict) else ""
        return Record(record_id=note_id, object="notes", attributes={}, raw=raw)

    # ── Generic object-record API ─────────────────────────────
    #
    # Replaces the raw ``AttioClient._request(...)`` channel for arbitrary /
    # operational objects (operator_review_queue, llm_budget_ledger, …). Bodies
    # mirror the inner client's typed object methods byte-for-byte:
    #   query  → POST /objects/<obj>/records/query  {"filter", "sorts", "limit"}
    #   get    → GET  /objects/<obj>/records/<id>   (404 → None)
    #   create → POST /objects/<obj>/records        {"data": {"values": attrs}}
    #   update → PATCH /objects/<obj>/records/<id>  {"data": {"values": attrs}}
    # ``filters``/``sorts`` are passed through as Attio's native query body — the
    # documented filter-shape leak (see the contract). Each response row is
    # normalized via ``_to_record`` with ``object`` set to ``object_type``.

    def query_object_records(
        self,
        object_type: str,
        *,
        filters: dict[str, Any] | None = None,
        sorts: list[Any] | None = None,
        limit: int | None = None,
    ) -> list[Record]:
        body: dict[str, Any] = {}
        if filters is not None:
            body["filter"] = filters
        if sorts is not None:
            body["sorts"] = sorts
        if limit is not None:
            body["limit"] = limit
        data = self._attio._request(
            "POST", f"/objects/{object_type}/records/query", json=body
        )
        # Tolerant of an empty / odd body: a non-dict response, a missing
        # ``data`` key, or ``{"data": null}`` all yield ``[]`` rather than
        # crashing the list comprehension (adapters must not crash on empty
        # bodies). Transport / 5xx errors still propagate from ``_request``.
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            rows = []
        return [self._to_record(r, object_type) for r in rows]

    def get_object_record(self, object_type: str, record_id: str) -> Record | None:
        try:
            data = self._attio._request(
                "GET", f"/objects/{object_type}/records/{record_id}"
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        raw = data.get("data", data) if isinstance(data, dict) else data
        return self._to_record(raw, object_type, fallback_id=record_id)

    def create_object_record(
        self, object_type: str, attributes: dict[str, Any]
    ) -> Record:
        # Route through the inner client's retrying ``_request`` so a transient
        # 429/5xx retries (contract: writes get the same transient resilience the
        # rest of the engine has). A uniqueness-constraint 400/409 is NOT
        # transient — map it to the neutral UniquenessConflictError so callers
        # branch on meaning, not a vendor HTTP body.
        try:
            data = self._attio._request(
                "POST",
                f"/objects/{object_type}/records",
                json={"data": {"values": attributes}},
            )
        except httpx.HTTPStatusError as exc:
            if _is_attio_uniqueness_violation(exc):
                raise UniquenessConflictError(object_type) from exc
            raise
        raw = data.get("data", data) if isinstance(data, dict) else data
        return self._to_record(raw, object_type)

    def update_object_record(
        self, object_type: str, record_id: str, attributes: dict[str, Any]
    ) -> Record:
        try:
            data = self._attio._request(
                "PATCH",
                f"/objects/{object_type}/records/{record_id}",
                json={"data": {"values": attributes}},
            )
        except httpx.HTTPStatusError as exc:
            if _is_attio_uniqueness_violation(exc):
                raise UniquenessConflictError(object_type) from exc
            raise
        raw = data.get("data", data) if isinstance(data, dict) else data
        return self._to_record(raw, object_type, fallback_id=record_id)

    # ── Normalized person read ────────────────────────────────

    def extract_person_info(self, record: Record) -> RecordInfo:
        """Resolve a person :class:`Record` into a :class:`RecordInfo`.

        Threads the untouched payload via ``record.raw`` into the inner
        client's ``extract_record_info`` so the linked-company lookup (and its
        corruption-cache side effect feeding :meth:`is_person_company_corrupted`)
        works exactly as today. Preserves the ``""``-when-blank contract for
        ``linkedin_url`` and ``title`` because ``extract_record_info`` already
        returns ``""`` (never ``None``) for those two — we pass them straight
        through.
        """
        name, company, linkedin_url, industry, title = self._attio.extract_record_info(
            record.raw
        )
        return RecordInfo(
            name=name,
            company=company,
            linkedin_url=linkedin_url,
            industry=industry,
            title=title,
        )

    # ── Attio-specific quirks (NOT on the ABC) ────────────────

    def is_person_company_corrupted(self, person_record_id: str) -> bool:
        """Attio data-quality quirk: True iff this person's linked company
        carries the LinkedIn-Clearbit corruption fingerprint.

        Delegates to the inner client, which answers from caches populated as a
        side effect of :meth:`extract_person_info`. Not part of the
        :class:`CRMProvider` contract — exposed only on this concrete adapter
        for callers that still need the flag post-threading.
        """
        return self._attio.is_person_company_corrupted(person_record_id)

    # ── Concrete escape hatch (NOT on the ABC) ────────────────

    @property
    def inner_client(self) -> AttioClient:
        """Read-only handle to the composed :class:`AttioClient`.

        Exposed for the ONE residual raw-httpx fallback in
        ``clients/attio_writer.py::AttioWriter._single_patch`` (object types
        that have no typed provider write yet). That fallback must own its
        own retry policy and therefore bypasses the inner ``_request`` retry
        loop — see the note at that call site. This property is the
        Attio-specific escape hatch the writer reaches for; it is
        deliberately NOT part of the vendor-neutral :class:`CRMProvider`
        contract.
        """
        return self._attio

    # ── Lifecycle ─────────────────────────────────────────────

    def close(self) -> None:
        self._attio.close()

    def __enter__(self) -> AttioProvider:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
