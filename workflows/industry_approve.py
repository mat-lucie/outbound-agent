"""Operator override handler for ``industry_low_confidence`` queue rows.

Mirrors ``workflows.sales_approve`` (PR-32) for a different escalation type.
The Haiku industry classifier opens an ``industry_low_confidence`` row when
``classify_with_confidence`` returns a label whose confidence falls below
the threshold (default 0.7). Operators review the LLM's best-guess label
and either confirm (the LLM was right, just under-confident) or override
(pick a different vertical from the 11 ``INDUSTRY_LABELS``).

On approval, this module writes to the Companies record:
  * ``industry_vertical`` ← the operator's chosen label
  * ``industry_vertical_status`` ← ``"confirmed"`` (lifts the abstain gate
    in ``quality_gate._industry_score``)

The queue row is then resolved with the operator's choice in
``decision_json`` for audit.

All Attio writes go through ``AttioWriter.apply(WriteIntent)`` — the §3.15
write-owner registry has ``workflows.industry_approve`` declared as a
co-writer of every slug touched here.

Wave-2 deferred: ``IndustryLowConfidencePayload`` carries ``company_name``
but no ``record_id``. This module looks up the Company by name with the
same ``company_matcher.normalize_company_name`` rule used at create time;
when name resolution is ambiguous the operator must disambiguate via the
``--record-id`` flag. Adding ``record_id`` to the payload is on the Wave-2
list (internal scope doc; not shipped with this repo).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from clients.attio_writer import AttioWriter, WriteIntent
from models.campaign import INDUSTRY_LABELS
from workflows.company_matcher import normalize_company_name

if TYPE_CHECKING:
    from clients.attio import AttioClient
    from clients.crm.base import CRMProvider

logger = logging.getLogger(__name__)

WRITER_MODULE = "workflows.industry_approve"
QUEUE_TYPE = "industry_low_confidence"


class IndustryRowNotFoundError(RuntimeError):
    """No pending ``industry_low_confidence`` queue row matches the company."""

    def __init__(self, company_name: str) -> None:
        self.company_name = company_name
        super().__init__(
            f"No pending industry_low_confidence queue row for "
            f"company_name={company_name!r}. Has the classifier flagged it?"
        )


class CompanyNotFoundError(RuntimeError):
    """The Companies object has no record matching ``company_name``.

    Raised before any writes so partial-completion can't leave the queue
    row resolved without the Companies update.
    """

    def __init__(self, company_name: str) -> None:
        self.company_name = company_name
        super().__init__(
            f"No Companies record matches company_name={company_name!r}. "
            f"Pass --record-id <id> to disambiguate, or check Attio."
        )


class AmbiguousCompanyError(RuntimeError):
    """Multiple Companies records match ``company_name`` and ``--record-id``
    was not supplied. Operator must disambiguate explicitly so the wrong
    record isn't silently confirmed."""

    def __init__(self, company_name: str, candidates: list[str]) -> None:
        self.company_name = company_name
        self.candidates = candidates
        super().__init__(
            f"{len(candidates)} Companies records match {company_name!r}. "
            f"Re-run with --record-id <id>. Candidates: {candidates}"
        )


class InvalidVerticalError(ValueError):
    """The chosen vertical is not one of the 11 canonical INDUSTRY_LABELS."""


def list_pending_industry_low_confidence(crm: CRMProvider) -> list[dict]:
    """Return open ``industry_low_confidence`` queue rows.

    Each row dict has ``queue_record_id``, ``company_name``,
    ``suggested_vertical``, ``confidence``, ``reasoning``, ``model_used``
    so the CLI can print them and operators can pick one.

    Raises any httpx error from Attio — no silent empty-list fallback
    on transport failure (§0 #9).
    """
    records = crm.query_object_records(
        "operator_review_queue",
        filters={
            "$and": [
                {"type": QUEUE_TYPE},
                {"status": {"$not": {"$in": ["resolved", "rejected"]}}},
            ],
        },
        limit=100,
    )
    pending: list[dict] = []
    for record in records:
        # ``record.raw`` is the untouched Attio object-record payload — the
        # same per-row shape the raw ``_request`` query returned. Read the
        # array-wrapped ``payload_json`` slug off it via ``_parse_payload``
        # exactly as before; the flat ``record.attributes`` map would have
        # first-value-unwrapped it.
        row = record.raw
        values = row.get("values", {}) or {}
        queue_record_id = row.get("id", {}).get("record_id", "")
        payload = _parse_payload(
            values.get("payload_json", []),
            queue_record_id=queue_record_id,
        )
        suggested = payload.get("suggested_vertical", "")
        if suggested and suggested not in INDUSTRY_LABELS:
            # The label was valid when the row was emitted but isn't in the
            # current INDUSTRY_LABELS — likely a schema drift since emission.
            # Operators need to know the LLM's suggestion is stale so they
            # don't reflexively confirm it.
            logger.warning(
                "industry_approve.list_pending: queue row %r suggested_vertical=%r "
                "is no longer in INDUSTRY_LABELS — operator must override or reject",
                queue_record_id, suggested,
            )
        pending.append({
            "queue_record_id": queue_record_id,
            "company_name": payload.get("company_name", ""),
            "suggested_vertical": suggested,
            "confidence": payload.get("confidence", 0.0),
            "reasoning": payload.get("reasoning", ""),
            "model_used": payload.get("model_used", ""),
        })
    return pending


def _parse_payload(payload_arr: object, *, queue_record_id: str = "") -> dict:
    """Extract the payload dict from Attio's array-wrapped attribute shape.

    Attio returns array-of-objects for most multi-value attributes; the
    ``payload_json`` long_text may arrive serialized when set via ``escalate``.

    Returns ``{}`` for any malformed shape so callers can keep listing
    rows. Logs a warning naming ``queue_record_id`` when the fallback
    fires so genuine data corruption (corrupted long_text, schema drift,
    classifier writing an unexpected envelope) is surfaced in logs rather
    than silently producing blank ghost rows in the CLI listing.
    """
    if not isinstance(payload_arr, list) or not payload_arr:
        if payload_arr not in (None, [], ()):  # genuinely unexpected shape
            logger.warning(
                "industry_approve._parse_payload: queue row %r payload_json "
                "is not a non-empty list (got %r); returning empty payload",
                queue_record_id, type(payload_arr).__name__,
            )
        return {}
    first = payload_arr[0]
    if not isinstance(first, dict):
        logger.warning(
            "industry_approve._parse_payload: queue row %r payload_json[0] "
            "is not a dict (got %r); returning empty payload",
            queue_record_id, type(first).__name__,
        )
        return {}
    value = first.get("value")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as err:
            logger.warning(
                "industry_approve._parse_payload: queue row %r payload_json "
                "is not valid JSON (%s); returning empty payload",
                queue_record_id, err,
            )
            return {}
    return value if isinstance(value, dict) else {}


def _find_pending_row_or_raise(crm: CRMProvider, company_name: str) -> dict:
    normalized = normalize_company_name(company_name)
    for row in list_pending_industry_low_confidence(crm):
        if normalize_company_name(row["company_name"]) == normalized:
            return row
    raise IndustryRowNotFoundError(company_name)


def _resolve_company_record_id(
    attio: AttioClient,
    company_name: str,
    record_id_override: str | None,
) -> str:
    """Resolve the Companies record_id for ``company_name``.

    Returns the supplied override unchanged when present (operator
    disambiguation path). Otherwise searches Attio by name and applies
    ``normalize_company_name`` to filter candidates.

    Raises ``CompanyNotFoundError`` when no record matches, and
    ``AmbiguousCompanyError`` when more than one matches and no override
    is supplied — both before any write happens, so a failed lookup
    can't leave the queue row resolved without the Companies update.
    """
    if record_id_override:
        return record_id_override

    results = attio.search_companies(filter_={"name": company_name}, limit=10)
    normalized_input = normalize_company_name(company_name)
    matches: list[tuple[str, str]] = []
    for result in results:
        name_values = result.get("values", {}).get("name", [])
        if not name_values:
            # Genuine data corruption: a Companies record without a name slug.
            # Surface it so an operator can audit, then continue scanning the
            # remaining candidates. Use the record's own id rather than the
            # search filter for traceability.
            logger.warning(
                "industry_approve._resolve_company_record_id: Companies "
                "record %r has no name slug; skipping",
                result.get("id", {}).get("record_id", "<unknown>"),
            )
            continue
        candidate = name_values[0].get("value", "")
        if normalize_company_name(candidate) == normalized_input:
            rid = result.get("id", {}).get("record_id", "")
            if rid:
                matches.append((rid, candidate))
            else:
                logger.warning(
                    "industry_approve._resolve_company_record_id: Companies "
                    "record matching name=%r has no record_id; skipping",
                    candidate,
                )

    if not matches:
        raise CompanyNotFoundError(company_name)
    if len(matches) > 1:
        raise AmbiguousCompanyError(
            company_name,
            [f"{rid} ({name})" for rid, name in matches],
        )
    return matches[0][0]


def _resolve_queue_row(
    attio: AttioClient,
    queue_record_id: str,
    *,
    status: str,
    decision: dict,
    writer: AttioWriter | None = None,
) -> None:
    if writer is None:
        writer = AttioWriter(attio=attio)
    writer.apply(WriteIntent(
        object="operator_review_queue",
        record_id=queue_record_id,
        updates={
            "status": status,
            "resolved_at": datetime.now(UTC).isoformat(),
            "decision_json": {**decision, "resolved_by": WRITER_MODULE},
        },
        writer_module=WRITER_MODULE,
    ))


def _update_company_industry(
    attio: AttioClient,
    company_record_id: str,
    vertical: str,
    writer: AttioWriter | None = None,
) -> None:
    if writer is None:
        writer = AttioWriter(attio=attio)
    writer.apply(WriteIntent(
        object="companies",
        record_id=company_record_id,
        updates={
            "industry_vertical": vertical,
            "industry_vertical_status": "confirmed",
        },
        writer_module=WRITER_MODULE,
    ))


def approve_industry_classification(
    company_name: str,
    *,
    vertical: str,
    crm: CRMProvider,
    attio: AttioClient,
    record_id: str | None = None,
    writer: AttioWriter | None = None,
) -> dict:
    """Approve a pending ``industry_low_confidence`` row with the operator's
    chosen ``vertical``.

    The pending-queue-row lookup reads through the vendor-neutral ``crm``
    (``CRMProvider``). The Companies name resolution (``search_companies``)
    and both writes (Companies update + queue-row resolution via
    ``AttioWriter``) still go through the raw ``attio`` client — those paths
    are deferred to later CRM-migration increments.

    Writes both the Companies update and the queue-row resolution. Returns
    a summary dict with ``queue_record_id``, ``company_record_id``,
    ``vertical``, ``company_updated``, ``queue_resolved``.

    Ordering (matches PR-32 partial-completion shape): Companies update
    first, queue resolution second. If the Companies write succeeds but
    the queue resolution fails, the company record reflects the operator's
    choice and the queue row stays open — re-running the same approval is
    idempotent on the Companies side (PATCH same values) and recovers the
    queue. The reverse ordering (queue first, company second) would leave
    a confirmed queue row pointing at an unupdated company, which is the
    worse partial state.

    Raises:
        InvalidVerticalError: ``vertical`` not in ``INDUSTRY_LABELS``.
        IndustryRowNotFoundError: no pending queue row for the company.
        CompanyNotFoundError: no Companies record matches the name.
        AmbiguousCompanyError: multiple Companies records match (operator
            must pass ``record_id`` to disambiguate).
    """
    valid_labels = set(INDUSTRY_LABELS.keys())
    if vertical not in valid_labels:
        raise InvalidVerticalError(
            f"vertical={vertical!r} is not one of {sorted(valid_labels)}"
        )

    row = _find_pending_row_or_raise(crm, company_name)
    company_record_id = _resolve_company_record_id(attio, company_name, record_id)

    if writer is None:
        writer = AttioWriter(attio=attio)

    _update_company_industry(attio, company_record_id, vertical, writer=writer)
    _resolve_queue_row(
        attio,
        row["queue_record_id"],
        status="resolved",
        decision={
            "operator_choice": vertical,
            "suggested_vertical": row["suggested_vertical"],
            "company_record_id": company_record_id,
        },
        writer=writer,
    )

    return {
        "queue_record_id": row["queue_record_id"],
        "company_record_id": company_record_id,
        "vertical": vertical,
        "company_updated": True,
        "queue_resolved": True,
    }


def reject_industry_classification(
    company_name: str,
    *,
    rationale: str,
    crm: CRMProvider,
    attio: AttioClient,
    writer: AttioWriter | None = None,
) -> dict:
    """Reject a pending ``industry_low_confidence`` row.

    The pending-row lookup reads through the vendor-neutral ``crm``; the
    queue-row resolution still writes through the raw ``attio`` client via
    ``AttioWriter``.

    The Companies record is left untouched — rejecting means "don't lift
    the abstain gate" rather than "set a vertical I disagree with."
    Status stays ``low_confidence`` until a future classifier run upgrades
    it or another queue row is approved.

    Raises ``ValueError`` on empty rationale (audit requirement) and
    ``IndustryRowNotFoundError`` if no pending row exists.
    """
    if not rationale or not rationale.strip():
        raise ValueError(
            "reject_industry_classification requires a non-empty rationale "
            "for audit."
        )
    row = _find_pending_row_or_raise(crm, company_name)
    _resolve_queue_row(
        attio,
        row["queue_record_id"],
        status="rejected",
        decision={
            "rationale": rationale,
            "suggested_vertical": row["suggested_vertical"],
        },
        writer=writer,
    )
    return {
        "queue_record_id": row["queue_record_id"],
        "queue_rejected": True,
        "company_updated": False,
    }
