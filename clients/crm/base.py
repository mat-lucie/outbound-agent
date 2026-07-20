"""The ``CRMProvider`` contract — the vendor-neutral seam for CRM access.

The outbound engine is currently hardwired to Attio (``clients/attio.py``,
``AttioClient``). This module defines the abstract interface that every CRM
adapter must satisfy, plus the normalized dataclasses adapters return instead
of raw vendor JSON. Attio becomes one *reference* implementation
(``clients/crm/attio_provider.py``, a later subtask); an onboarding agent can
later GENERATE a new vendor's adapter against this same contract.

Scope of this module (Phase 1.1 of the CRM-agnostic design):
    * Define ``CRMProvider`` (an ``abc.ABC``) covering exactly the CRM
      operations the engine uses today — surveyed from the live call sites in
      ``workflows/`` and ``cli.py``, NOT the speculative §1.1 list verbatim.
    * Define normalized return types (``Record``, ``Entry``, ``Stage``,
      ``RecordInfo``) so adapters return neutral shapes.

What is deliberately NOT here:
    * The Attio adapter, factory, threading into callers, and contract tests —
      those are later Phase 1 subtasks.
    * **Vendor-specific quirks.** Attio's LinkedIn-Clearbit corruption check
      (``is_linkedin_clearbit_corrupted`` / ``is_person_company_corrupted``) and
      LinkedIn-URL canonicalization (``_canonical_linkedin_url`` and friends)
      live INSIDE the concrete adapter, never on this ABC. The contract exposes
      the *behavior* an adapter must guarantee (e.g. "upsert by LinkedIn URL is
      idempotent") and lets each adapter satisfy it however its vendor requires.

Object model. The live engine is object-typed: it manipulates *people*,
*companies*, and *deals* as distinct record kinds, plus *list entries* (the
pipeline) and *notes*. Rather than collapse these into a single generic
``get_record(id)`` (the simplified shape sketched in the design doc's §1.1),
this contract preserves the object distinction the call sites actually depend
on. Most mainstream CRMs (Attio, HubSpot, Salesforce, Pipedrive) share the
person / company / deal triad, so this stays vendor-neutral in practice.

Stage model. There is no ``get_stage``/``set_stage`` method on the live
``AttioClient``; the §1.1 sketch invented those. In the real engine, stage is
just an attribute on a list entry: it is *read* by parsing an entry (surfaced
here as :class:`Entry.stage`) and *written* by passing a ``stage`` value in the
attrs of :meth:`CRMProvider.add_list_entry` / :meth:`update_list_entry`.
**Stage monotonicity is NOT enforced inside the CRM client today** — it is
enforced one layer up, in ``clients/attio_writer.py`` (``AttioWriter``). So this
contract does not promise that stage writes never regress; it only promises that
a write of the configured stage field is persisted. The monotonicity guarantee
remains the writer's responsibility and is out of scope for this seam.

Attrs payloads. Where the live code passes around plain dicts of vendor
attribute slugs → values, this contract types them as ``dict[str, Any]`` rather
than over-modeling every field. The set of valid keys is vendor- and
schema-specific (driven by the field mapping in ``config/crm.yaml``), so a typed
model here would be a false promise. Normalized *reads* (:class:`Record`,
:class:`Entry`, :class:`RecordInfo`) ARE modeled, because callers depend on a
stable read shape.

See ``clients/crm/CONTRACT.md`` for the method-by-method mapping from this ABC
to the concrete ``AttioClient`` methods.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

# ── Normalized data types ────────────────────────────────────────────────
#
# Adapters return these neutral shapes at the boundary instead of raw vendor
# JSON. They are intentionally minimal — they model only the fields callers
# actually read today. Frozen where the value is a pure read snapshot.


@dataclass(frozen=True)
class Stage:
    """A pipeline stage, normalized away from any vendor's encoding.

    ``name`` is the human-facing stage label as the engine knows it (e.g.
    ``"Connection Sent"``) — the same string space as ``models.pipeline``'s
    ``PipelineStage`` values. ``rank`` is the funnel position used for
    most-advanced-entry selection and (one layer up) monotonicity checks;
    higher means further along. ``rank`` is ``None`` when the adapter cannot
    resolve a rank for an unrecognized stage string — callers MUST treat an
    unknown stage as rank-0/oldest rather than crashing (this mirrors today's
    ``AttioClient`` behavior, where an unparseable stage falls back to 0).
    """

    name: str
    rank: int | None = None


@dataclass(frozen=True)
class Record:
    """A normalized CRM object record (person, company, or deal).

    ``record_id`` is the vendor's stable record identifier. ``object`` is the
    object kind (``"people"`` | ``"companies"`` | ``"deals"``). ``attributes``
    is the flat ``slug -> value`` map of the record's fields as the vendor
    returns them — left as ``dict[str, Any]`` because the valid keys are
    schema-specific (see module docstring). ``raw`` carries the untouched
    vendor payload so an adapter-aware caller can reach a field this normalized
    shape does not model yet; new callers should prefer ``attributes`` and the
    typed helpers (:meth:`CRMProvider.extract_person_info`) over ``raw``.
    """

    record_id: str
    object: str
    attributes: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Entry:
    """A normalized pipeline list entry (the staged-cadence row).

    A list entry is the join between a person record and the pipeline list; it
    carries the cadence state (stage + per-step timestamps + scoring signals).
    This dataclass models the identity (``entry_id``, ``record_id``), the
    ``stage`` (as a :class:`Stage`), and the flat ``attributes`` map of every
    other entry value the engine reads (persona, language, dm_step,
    quality_score, experiment_id, the §3.x timestamp/quarantine attrs, …).
    Modeling each of those ~30 attrs as a typed field would couple this seam to
    the engine's exact pipeline schema, so they live in ``attributes`` keyed by slug
    — the same flat shape ``AttioClient.parse_entry`` returns today. ``raw`` is
    the untouched vendor payload for escape-hatch reads.
    """

    entry_id: str
    record_id: str
    stage: Stage
    attributes: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RecordInfo:
    """The denormalized display tuple the cadence/dry-run paths read off a
    person record: who they are, where they work, and their role.

    Mirrors the 5-tuple ``AttioClient.extract_record_info`` returns today, but
    as a named structure. ``name``, ``company`` and ``industry`` are optional:
    missing values are ``None`` (NEVER a sentinel string like ``"Unknown"`` —
    downstream code checks ``is None``). ``linkedin_url`` and ``title`` default
    to the empty string rather than ``None`` to match the current contract,
    where ``extract_record_info`` returns ``""`` (not ``None``) for a blank URL
    or title; adapters MUST preserve this ``""``-when-blank behavior for these
    two fields so downstream truthiness checks keep working unchanged.
    """

    name: str | None
    company: str | None
    linkedin_url: str
    industry: str | None
    title: str


# ── The contract ─────────────────────────────────────────────────────────


class CRMProvider(abc.ABC):
    """Vendor-neutral CRM port: the operations the outbound engine performs
    against a system of record.

    A concrete adapter (e.g. ``AttioProvider``) implements every abstract
    method below, translating between these normalized shapes and its vendor's
    API. The engine threads a ``CRMProvider`` instance instead of a concrete
    ``AttioClient`` (a later subtask wires the factory + threading).

    General contract notes that apply to every method:
        * **Reads return normalized shapes.** Methods that return records or
          entries return :class:`Record` / :class:`Entry` (or lists thereof),
          never raw vendor JSON, so callers are vendor-agnostic.
        * **Writes take attr dicts.** ``attributes`` is a flat ``slug -> value``
          map keyed by the vendor's field slugs (the engine maps its own field
          names to vendor slugs via ``config/crm.yaml``; that mapping is the
          adapter's concern, not the caller's).
        * **Missing optional records read as ``None``**, not an exception —
          ``get_*`` by id returns ``None`` for a record that does not exist.
        * **Schema validation is NOT a contract promise.** Writes take a flat
          ``slug -> value`` attrs dict and pass it THROUGH to the vendor; the
          contract does **not** require an adapter to detect or reject a slug the
          vendor schema does not know. An earlier draft promised "no silent field
          drops" (raise-on-unknown-field) as a silent-failure guard — but neither
          reference adapter (``FakeProvider``, ``AttioProvider``) enforces it, and
          enforcing it would couple the seam to per-vendor schema introspection.
          So this is explicitly downgraded to a non-promise: an adapter MAY accept
          arbitrary attrs and let the vendor be the schema authority (Attio
          returns a 4xx for a genuinely-unknown attribute, which propagates; some
          vendors silently drop). Operators who need strict schema enforcement
          must validate attrs upstream of the provider, or run an adapter that
          opts into it — it is **not** asserted by the conformance suite.
    """

    # ── People records ────────────────────────────────────────

    @abc.abstractmethod
    def search_people(
        self, filter_: dict[str, Any] | None = None, limit: int = 50
    ) -> list[Record]:
        """Search person records, optionally filtered, auto-paginating up to
        ``limit``.

        Args:
            filter_: Vendor-shaped filter (e.g. ``{"linkedin": <url>}``), or
                ``None`` for an unfiltered scan.
            limit: Maximum records to return.

        Returns: a list of person :class:`Record` (possibly empty).
        """

    @abc.abstractmethod
    def get_person(self, record_id: str) -> Record | None:
        """Fetch one person record by its id.

        Returns the person :class:`Record`, or ``None`` if no such record
        exists. Transport/5xx errors propagate; a 404 is the ``None`` case.
        """

    @abc.abstractmethod
    def bulk_fetch_persons(
        self, record_ids: set[str], *, metrics: Any = None
    ) -> dict[str, Record]:
        """Fetch many person records concurrently, with per-record isolation.

        A single failed fetch is logged/counted (via the optional ``metrics``
        sink) but NEVER aborts the batch. Returns a ``{record_id: Record}`` map
        containing only the ids that resolved to an existing record — callers
        detect drops by comparing keys against the requested set.

        ``metrics``, when supplied, is the engine's ``DailyRunMetrics`` sink
        (``workflows/metrics.py``): the adapter MUST bump its
        ``bulk_fetch_records_requested`` by the number of ids requested,
        ``bulk_fetch_records_returned`` by the number resolved, and
        ``bulk_fetch_records_failed`` by the number that errored, so
        partial-outage volume is observable rather than silent. These three
        counter names are the contract — the conformance suite passes a sink with
        exactly these attributes and asserts they reflect the fetch.
        """

    @abc.abstractmethod
    def search_person_by_linkedin(self, linkedin_url: str) -> Record | None:
        """Find a person by LinkedIn profile URL, or ``None`` if not found.

        The adapter is responsible for whatever URL-normalization its vendor's
        matching requires (Attio, for instance, tries multiple canonical/encoded
        variants because its filter is exact-string). That normalization is a
        vendor quirk and lives inside the adapter, not on this contract — the
        contract only promises that two URLs denoting the same profile resolve
        to the same record. ``None`` / empty input returns ``None``.
        """

    @abc.abstractmethod
    def create_person(self, attributes: dict[str, Any]) -> Record:
        """Create a new person record from ``attributes`` and return it."""

    @abc.abstractmethod
    def update_person(self, record_id: str, attributes: dict[str, Any]) -> Record:
        """Patch ``attributes`` onto an existing person record; return the
        updated record. Unspecified fields are left untouched."""

    @abc.abstractmethod
    def upsert_person(
        self, matching_attribute: str, attributes: dict[str, Any]
    ) -> Record:
        """Create-or-update a person, matched on ``matching_attribute``.

        Idempotent on the match key: calling it repeatedly with the same
        matching value updates the one existing record rather than creating
        duplicates. ``matching_attribute`` names the field to dedupe on (e.g.
        ``"linkedin"`` or ``"email"``). The adapter owns any value-canonicalization
        needed to make the match reliable (vendor quirk). Returns the resulting
        record.
        """

    # ── Company records ──────────────────────────────────────

    @abc.abstractmethod
    def search_companies(
        self, filter_: dict[str, Any] | None = None, limit: int = 50
    ) -> list[Record]:
        """Search company records, optionally filtered, auto-paginating up to
        ``limit``. Returns a list of company :class:`Record`."""

    @abc.abstractmethod
    def get_company(self, record_id: str) -> Record | None:
        """Fetch one company record by id, or ``None`` if not found."""

    @abc.abstractmethod
    def create_company(self, attributes: dict[str, Any]) -> Record:
        """Create a new company record from ``attributes`` and return it."""

    @abc.abstractmethod
    def update_company(self, record_id: str, attributes: dict[str, Any]) -> Record:
        """Patch ``attributes`` onto an existing company record; return it."""

    @abc.abstractmethod
    def search_company_by_domain(self, domain: str) -> Record | None:
        """Find a company by web domain, or ``None`` if not found."""

    # ── Deal records ──────────────────────────────────────────

    @abc.abstractmethod
    def get_deal(self, record_id: str) -> Record | None:
        """Fetch one deal record by id, or ``None`` if not found."""

    @abc.abstractmethod
    def create_deal(self, attributes: dict[str, Any]) -> Record:
        """Create a new deal record from ``attributes`` and return it.

        Callers achieve creation-idempotency by including an idempotency-key
        attribute in ``attributes`` (the engine's deal-creation workflow owns
        that key); the contract itself does not dedupe deals.
        """

    @abc.abstractmethod
    def update_deal(self, record_id: str, attributes: dict[str, Any]) -> Record:
        """Patch ``attributes`` onto an existing deal record; return it."""

    @abc.abstractmethod
    def search_deals(
        self, filter_: dict[str, Any] | None = None, limit: int = 500
    ) -> list[Record]:
        """Search deal records, optionally filtered, auto-paginating up to
        ``limit``. Returns a list of deal :class:`Record`."""

    # ── List entries (the pipeline) ───────────────────────────

    @abc.abstractmethod
    def query_list_entries(
        self,
        list_id: str | None = None,
        filter_: dict[str, Any] | None = None,
        limit: int = 50000,
        *,
        fail_if_truncated: bool = False,
    ) -> list[Entry]:
        """Query the pipeline list's entries, optionally filtered,
        auto-paginating up to ``limit``.

        ``list_id`` selects the list; ``None`` means the adapter's configured
        default pipeline list. Returns normalized :class:`Entry` objects (stage
        + flat attribute map), the staged-cadence rows the engine iterates.

        With ``fail_if_truncated=True`` a full-sweep read that hits ``limit``
        with entries remaining raises :class:`ResultTruncatedError` instead of
        returning a silently incomplete set (PR-234).
        """

    @abc.abstractmethod
    def add_list_entry(
        self,
        record_id: str,
        stage_name: str,
        entry_attributes: dict[str, Any] | None = None,
        list_id: str | None = None,
        existing_entries: list[Entry] | None = None,
    ) -> Entry:
        """Add a person record to the pipeline list, or update its existing
        entry if one is already present for this ``(record_id, list)`` pair.

        Idempotent on ``(record_id, list_id)``: the vendor may not enforce
        entry uniqueness, so the adapter MUST upsert — if one entry already
        exists it is patched; if several exist (legacy duplicates) the
        most-advanced one (by stage rank) is patched and the rest left for a
        dedup pass. ``stage_name``, when non-empty, is written as the entry's
        stage attribute. ``existing_entries``, when supplied, lets the adapter
        run the dedupe filter client-side against a pre-fetched list instead of
        issuing a fresh full-list scan per call (a batch-flow optimization).

        Note: this method does NOT enforce stage monotonicity — it will write a
        lower stage if asked. The no-regression guarantee lives one layer up in
        the writer (see module docstring). Returns the resulting :class:`Entry`.
        """

    @abc.abstractmethod
    def update_list_entry(
        self,
        entry_id: str,
        entry_attributes: dict[str, Any],
        list_id: str | None = None,
    ) -> Entry:
        """Patch ``entry_attributes`` onto a pipeline list entry identified by
        ``entry_id`` (change stage, scoring signals, timestamps, …).

        ``list_id`` selects the list (``None`` = configured default). Returns
        the updated :class:`Entry`. Like :meth:`add_list_entry`, this does not
        itself guard stage regression — that is the writer's job.
        """

    # ── Notes ─────────────────────────────────────────────────

    @abc.abstractmethod
    def create_note(
        self,
        record_id: str,
        title: str,
        content: str,
        parent_object: str = "people",
    ) -> Record:
        """Attach a plaintext note (``title`` + ``content``) to the record
        ``record_id`` of kind ``parent_object`` (``"people"`` | ``"companies"``).

        Notes are append-only annotations — each call creates a new note; there
        is no dedupe. Returns a :class:`Record` describing the created note.
        """

    # ── Generic object-record API ─────────────────────────────
    #
    # The typed ``get_person`` / ``create_company`` / … methods above stay the
    # ergonomic path for the engine's *core* CRM objects (people, companies,
    # deals, list entries, notes). The four methods below are the vendor-neutral
    # surface for *arbitrary / operational* objects the engine also touches —
    # ``operator_review_queue``, ``llm_budget_ledger``, ``reclassification_run``,
    # ``migration_run``, ``experiment``, and even ad-hoc reads of people /
    # companies by a non-standard filter. Today those go through the raw private
    # ``AttioClient._request(...)`` channel (surveyed: ~25 call sites in
    # ``workflows/`` + ``cli.py``); this is the ONE seam that replaces that
    # bypass so a generated non-Attio adapter has a defined contract to satisfy.
    #
    # **Known limitation — the filter-shape leak.** ``filters`` and ``sorts`` are
    # passed THROUGH to the adapter as a *vendor-native query body* (Attio's
    # filter DSL — e.g. ``{"$and": [{"type": "x"}, {"status": {"$not": …}}]}``).
    # This contract does NOT define a vendor-neutral filter DSL (that would be its
    # own design and is out of scope). A generated non-Attio adapter therefore
    # MUST translate these shapes into its vendor's query language, or every
    # caller that passes a filter stays Attio-coupled. New callers should treat
    # ``filters``/``sorts`` as "the vendor's native query body" and budget for
    # that translation cost. The RETURN type is still normalized (``Record``), so
    # reads downstream of the query stay vendor-agnostic.

    @abc.abstractmethod
    def query_object_records(
        self,
        object_type: str,
        *,
        filters: dict[str, Any] | None = None,
        sorts: list[Any] | None = None,
        limit: int | None = None,
    ) -> list[Record]:
        """Query records of an arbitrary object type, returning normalized
        :class:`Record` objects (``object`` set to ``object_type``).

        Args:
            object_type: The vendor object slug (e.g. ``"operator_review_queue"``,
                ``"llm_budget_ledger"``, ``"deals"``).
            filters: A **vendor-native** filter body (see the filter-shape-leak
                note on this section). ``None`` means no filter (full scan up to
                ``limit``).
            sorts: A vendor-native sort spec, or ``None``. Like ``filters`` this
                is passed through unmodified — it is NOT a neutral sort DSL.
            limit: Maximum records to return; ``None`` lets the adapter apply its
                own default page bound.

        Returns: a list of :class:`Record` (possibly empty). Transport / 5xx
        errors propagate — there is no silent empty-list fallback.
        """

    @abc.abstractmethod
    def get_object_record(self, object_type: str, record_id: str) -> Record | None:
        """Fetch one record of an arbitrary object type by id.

        Returns the normalized :class:`Record` (``object`` set to
        ``object_type``), or ``None`` if no such record exists. Mirrors the
        typed ``get_*`` semantics: a 404 is the ``None`` case; transport / 5xx
        errors propagate.
        """

    @abc.abstractmethod
    def create_object_record(
        self, object_type: str, attributes: dict[str, Any]
    ) -> Record:
        """Create a record of an arbitrary object type from ``attributes``
        (a flat ``slug -> value`` map) and return the normalized :class:`Record`.

        **Uniqueness.** If the write violates a uniqueness constraint the vendor
        enforces on one of the written attributes (e.g. a ``daily_run`` row whose
        ``uniqueness_key`` is already held), the adapter raises
        :class:`~clients.crm.exceptions.UniquenessConflictError` — the neutral
        "a record with this value already exists" signal — rather than leaking a
        vendor HTTP error the caller would have to sniff. This is the contract a
        concurrent-run guard depends on (``workflows/daily_run.py``). Any OTHER
        4xx/5xx (genuinely-unknown attribute, transport failure) propagates as
        the adapter's native exception. Per the schema-validation non-promise
        above, the contract does NOT require the adapter to reject unknown slugs.
        """

    @abc.abstractmethod
    def update_object_record(
        self, object_type: str, record_id: str, attributes: dict[str, Any]
    ) -> Record:
        """Patch ``attributes`` onto an existing record of an arbitrary object
        type; return the normalized :class:`Record`. Unspecified fields are left
        untouched.

        **Uniqueness.** Like :meth:`create_object_record`, a vendor
        uniqueness-constraint violation on a written attribute (e.g. re-keying a
        ``daily_run`` row's ``uniqueness_key`` to a value a concurrent run already
        holds) raises :class:`~clients.crm.exceptions.UniquenessConflictError`,
        not a raw vendor error. Other 4xx/5xx propagate unchanged.
        """

    # ── Normalized person read ────────────────────────────────

    @abc.abstractmethod
    def extract_person_info(self, record: Record) -> RecordInfo:
        """Resolve a person :class:`Record` into the display tuple the cadence
        and dry-run paths read: name, employer company, LinkedIn URL, industry,
        and job title.

        Resolving the employer may require the adapter to follow a record
        reference and fetch the linked company (and cache it). Missing values
        come back as ``None`` (never a sentinel string); ``linkedin_url`` is the
        empty string when blank. Any vendor data-quality guard the adapter runs
        while resolving the company (e.g. Attio's Clearbit-corruption flag) is
        an internal side effect — it is NOT surfaced on this method or on
        :class:`RecordInfo`; adapters that need to expose such a flag do so via
        their own concrete API, not this contract.
        """
