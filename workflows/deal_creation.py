"""PR-38 — create_deal_from_response + threshold-driven auto/confirm/skip.

When the response_classifier returns a positive verdict on a prospect's
reply, this workflow decides whether to:

  - Create a new Lead deal automatically (confidence > auto threshold)
  - Open a `deal_creation_confirm` operator-review row (confidence in
    the confirm-band)
  - Skip silently (confidence below confirm threshold)

Idempotency contract (Round-4 D25): one deal per
``(person_record_id, response_classification, response_received_at)``
tuple. The synthesized ``creation_idempotency_key`` is stored on the
new Deal row; subsequent calls with the same tuple short-circuit on the
existing row instead of creating a duplicate.

Thresholds default to ``DEAL_AUTO_THRESHOLD=0.85`` /
``DEAL_CONFIRM_THRESHOLD=0.50`` and are tunable via the
``configuration_decision`` queue with ``decision_key="deal_creation_threshold"``.
The constants here are the runtime defaults; the operator updates them
via a PR after reviewing the queue row's recommendation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from workflows.escalation import escalate

if TYPE_CHECKING:
    from datetime import datetime

    from clients.attio import AttioClient

# Round-4 D25 thresholds. Tunable via ``configuration_decision`` with
# ``decision_key="deal_creation_threshold"``.
DEAL_AUTO_THRESHOLD: float = 0.85
DEAL_CONFIRM_THRESHOLD: float = 0.50

WRITER_MODULE = "workflows.deal_creation.create_deal_from_response"
DEAL_STAGE_LEAD = "Lead"

CreatedVia = Literal["agent_auto", "agent_confirm", "operator_manual", "operator_unknown"]
ActionLiteral = Literal[
    "created",
    "escalated_confirm",
    "skipped_low_confidence",
    "cached_idempotent",
]


@dataclass(frozen=True)
class DealCreationResult:
    """Outcome of a single ``create_deal_from_response`` call.

    Callers read ``action`` to know what happened; ``deal_record_id`` is
    populated on ``created`` and ``cached_idempotent`` (None on the
    confirm/skip branches). ``escalation_record_id`` is populated only
    on ``escalated_confirm``.
    """
    action: ActionLiteral
    idempotency_key: str
    confidence: float
    rationale: str
    deal_record_id: str | None = None
    escalation_record_id: str | None = None
    created_via: CreatedVia | None = None


def build_idempotency_key(
    *,
    person_record_id: str,
    response_classification: str,
    response_received_at: datetime,
) -> str:
    """Synthesize the Round-4 D25 idempotency key.

    The triple uniquely identifies a single positive-response event;
    re-running the same response classification (e.g. on cron replay)
    must not open a second deal.

    ``response_received_at`` MUST be timezone-aware — a naive datetime's
    ``isoformat()`` carries no offset, so the same wall-clock instant in
    different timezones (or naive vs UTC on a cron re-run) would yield
    distinct keys and break the idempotency contract. Same class as
    PR-36's UTC-midnight-straddle bug.
    """
    if not person_record_id:
        raise ValueError("person_record_id is required for the idempotency key")
    if not response_classification:
        raise ValueError("response_classification is required for the idempotency key")
    if response_received_at.tzinfo is None:
        raise ValueError(
            "response_received_at must be timezone-aware "
            "(naive datetimes break the idempotency contract on cron replay)"
        )
    return (
        f"{person_record_id}_{response_classification}_"
        f"{response_received_at.isoformat()}"
    )


def _find_existing_deal(
    attio: AttioClient,
    *,
    idempotency_key: str,
) -> dict | None:
    """Look up an existing deal by ``creation_idempotency_key``.

    Direct query on the deals object (not via search_deals which doesn't
    accept the filter shape we need). Returns the raw record dict or None.
    Transport failures propagate — §0 #9, no silent skip-to-create that
    would break the idempotency contract.

    Filter uses the explicit ``{"$eq": ...}`` form mirroring the
    ``workflows.escalation._find_existing_row`` precedent (lookup-by-single-
    text-attribute pattern). Bare-value form also works in this codebase
    (PR-34/PR-35 use it inside ``$and``), but the explicit form is more
    defensive against API-shape drift.
    """
    data = attio._request(
        "POST",
        "/objects/deals/records/query",
        json={
            "filter": {"creation_idempotency_key": {"$eq": idempotency_key}},
            "limit": 1,
        },
    )
    rows = data.get("data", [])
    return rows[0] if rows else None


def _deal_record_id(record: dict) -> str:
    """Extract the record_id from a deal record envelope (creation or query)."""
    rid = record.get("id")
    if isinstance(rid, dict):
        return rid.get("record_id", "")
    return record.get("record_id", "")


def create_deal_from_response(
    *,
    person_record_id: str,
    response_classification: str,
    response_received_at: datetime,
    classifier_confidence: float,
    deal_name: str,
    associated_company_id: str | None,
    attio: AttioClient,
    auto_threshold: float | None = None,
    confirm_threshold: float | None = None,
) -> DealCreationResult:
    """Decide whether to create / escalate / skip a deal for a positive response.

    Args:
        person_record_id: Attio Person record_id who responded.
        response_classification: Classifier verdict (e.g. ``"positive"``).
        response_received_at: When the reply arrived (UTC datetime). Used in
            the idempotency key so two distinct reply-events from the same
            person on different days both get their own deal review.
        classifier_confidence: float in ``[0.0, 1.0]``. Driven by the
            response_classifier's confidence on this verdict.
        deal_name: human-readable deal name (e.g. ``"<Company> – <Person>"``).
        associated_company_id: Attio Company record_id to attach (None when
            the company is not yet resolved — the deal still creates with
            just the person and the company-link is patched later).
        attio: ``AttioClient`` for the read + write round-trips.
        auto_threshold: confidence ≥ this → create deal automatically.
            Defaults to ``DEAL_AUTO_THRESHOLD`` (0.85).
        confirm_threshold: confidence ≥ this AND < auto_threshold → open a
            ``deal_creation_confirm`` escalation. Defaults to
            ``DEAL_CONFIRM_THRESHOLD`` (0.50).

    Returns:
        DealCreationResult — see class docstring.

    Raises:
        ValueError: invalid threshold ordering or missing required identity.
    """
    auto = DEAL_AUTO_THRESHOLD if auto_threshold is None else auto_threshold
    confirm = DEAL_CONFIRM_THRESHOLD if confirm_threshold is None else confirm_threshold
    if confirm > auto:
        raise ValueError(
            f"confirm_threshold ({confirm}) must be <= auto_threshold ({auto})"
        )

    idempotency_key = build_idempotency_key(
        person_record_id=person_record_id,
        response_classification=response_classification,
        response_received_at=response_received_at,
    )

    existing = _find_existing_deal(attio, idempotency_key=idempotency_key)
    if existing is not None:
        return DealCreationResult(
            action="cached_idempotent",
            idempotency_key=idempotency_key,
            confidence=classifier_confidence,
            deal_record_id=_deal_record_id(existing),
            rationale="Deal already exists for this idempotency key; re-run no-op.",
        )

    if classifier_confidence < confirm:
        return DealCreationResult(
            action="skipped_low_confidence",
            idempotency_key=idempotency_key,
            confidence=classifier_confidence,
            rationale=(
                f"classifier_confidence={classifier_confidence:.4f} < "
                f"confirm_threshold={confirm}; no deal opened."
            ),
        )

    # Force the confirm path when the company link is missing — even at
    # high confidence. An auto-created orphan deal has no associated
    # company until someone runs a backfill; surfacing it as an operator-
    # confirm escalation gives a typed signal instead of silently
    # producing a dataset of orphan Leads.
    needs_confirm = classifier_confidence < auto or not associated_company_id

    if needs_confirm:
        escalation_row = _open_confirm_escalation(
            attio=attio,
            person_record_id=person_record_id,
            response_classification=response_classification,
            response_received_at=response_received_at,
            classifier_confidence=classifier_confidence,
            idempotency_key=idempotency_key,
            deal_name=deal_name,
            associated_company_id=associated_company_id,
        )
        if classifier_confidence < auto:
            band_note = (
                f"classifier_confidence={classifier_confidence:.4f} in "
                f"[{confirm}, {auto}); operator confirmation requested."
            )
        else:
            band_note = (
                f"classifier_confidence={classifier_confidence:.4f} >= "
                f"auto_threshold={auto} BUT associated_company_id is "
                "missing; operator confirmation requested to avoid an "
                "orphan deal."
            )
        return DealCreationResult(
            action="escalated_confirm",
            idempotency_key=idempotency_key,
            confidence=classifier_confidence,
            escalation_record_id=_record_id_of(escalation_row),
            rationale=band_note,
        )

    # Auto-create branch: confidence ≥ auto_threshold AND company linked
    deal_attrs = _build_deal_attributes(
        idempotency_key=idempotency_key,
        deal_name=deal_name,
        person_record_id=person_record_id,
        associated_company_id=associated_company_id,
        created_via="agent_auto",
    )
    created = attio.create_deal(deal_attrs)
    return DealCreationResult(
        action="created",
        idempotency_key=idempotency_key,
        confidence=classifier_confidence,
        deal_record_id=_deal_record_id(created),
        created_via="agent_auto",
        rationale=(
            f"classifier_confidence={classifier_confidence:.4f} >= "
            f"auto_threshold={auto}; deal auto-created."
        ),
    )


def _build_deal_attributes(
    *,
    idempotency_key: str,
    deal_name: str,
    person_record_id: str,
    associated_company_id: str | None,
    created_via: CreatedVia,
) -> dict[str, Any]:
    """Assemble the values dict for a new deal record.

    Mirrors the associated_people / associated_company reference shape
    used by ``workflows.repair_deal_contacts.apply_deal_links``.
    """
    attrs: dict[str, Any] = {
        "name": deal_name,
        "stage": DEAL_STAGE_LEAD,
        "creation_idempotency_key": idempotency_key,
        "created_via": created_via,
        "associated_people": [
            {"target_object": "people", "target_record_id": person_record_id},
        ],
    }
    if associated_company_id:
        attrs["associated_company"] = [
            {"target_object": "companies", "target_record_id": associated_company_id},
        ]
    return attrs


def _open_confirm_escalation(
    *,
    attio: AttioClient,
    person_record_id: str,
    response_classification: str,
    response_received_at: datetime,
    classifier_confidence: float,
    idempotency_key: str,
    deal_name: str,
    associated_company_id: str | None,
) -> dict:
    """Open a ``deal_creation_confirm`` operator-review row.

    The escalation carries the candidate deal shape so the operator can
    review and either (a) confirm — at which point a backfill script
    creates the deal with ``created_via="agent_confirm"`` — or (b) reject.
    """
    payload = {
        "person_record_id": person_record_id,
        "response_classification": response_classification,
        "response_received_at": response_received_at.isoformat(),
        "classifier_confidence": classifier_confidence,
        "candidate_deal": {
            "name": deal_name,
            "stage": DEAL_STAGE_LEAD,
            "associated_company_id": associated_company_id,
            "creation_idempotency_key": idempotency_key,
        },
        "opened_by": WRITER_MODULE,
    }

    return escalate(
        type="deal_creation_confirm",
        idempotency_key=idempotency_key,
        payload=payload,
        attio=attio,
    )


def _record_id_of(row: dict) -> str:
    """Extract record_id from an escalation/queue row response envelope."""
    rid = row.get("id")
    if isinstance(rid, dict):
        return rid.get("record_id", "")
    return row.get("record_id", "")
