"""Operator escalation channel (F-PR-3).

This module is THE place where the agent surfaces a decision to the
operator. Every escalation lands as an `Operator Review Queue` row,
typed by `type` and idempotent on the queue's uniqueness key.

The plan's hardest contract — "no silent fallbacks; every decision is
a typed Attio object, not markdown" — gets enforced here.

## Usage

    from workflows.escalation import escalate

    escalate(
        type="dedup_review",
        idempotency_key=f"dedup-{conflict_group_id}",
        payload={
            "canonical_linkedin_url": "...",
            "record_ids": ["rec_a", "rec_b"],
            "conflict_shape": "entry.persona",
            "auto_mergeable": False,
        },
    )

## Idempotency contract

The queue's uniqueness key is `(type, idempotency_key)` — or
`(type, decision_key, idempotency_key)` when `type="configuration_decision"`.
Re-calling `escalate()` with the same triplet returns the existing row
without mutating it. Callers can be re-run safely.

### Concurrency caveat

`escalate()` is single-process-safe — the read-then-create idempotency
relies on no other process opening the same uniqueness_key between the
query and the create call. The deployment today is single-machine, and
F-PR-7's `flock` (next-up foundation PR) makes daily/weekly runs
mutually exclusive at the process level. **No mitigation in F-PR-3
itself**: Attio v2 has no server-side unique-constraint on text attrs,
so cross-process safety has to come from `flock` or a follow-up
re-query-after-create dedup loop. A duplicate row is an operator
annoyance, not a §3.1 violation — the audit log (F-PR-2) is the
source of truth either way.

## Default-on-expiry

Some escalations carry a `deadline` (date). When the deadline passes
without an operator decision, a separate resolver (not part of F-PR-3 —
ships with the cron in PR-30 or thereabouts) flips
`status="auto_resolved_default"` and records
`decision_source="expired_default_<details>"`. F-PR-3 just records
the deadline at write time.
"""

from __future__ import annotations

import json
import os
import typing
from datetime import UTC, date, datetime
from typing import Any

from clients.crm.attio_provider import AttioProvider
from clients.crm.base import CRMProvider
from workflows.escalation_schemas import (
    CONFIGURATION_DECISION_KEYS_SET,
    ESCALATION_SCHEMAS,
    ESCALATION_TYPES_SET,
    EscalationSchemaError,
    MissingAttioCredentials,
    MissingDecisionKey,
    UnknownEscalationType,
)

if typing.TYPE_CHECKING:
    # AttioClient is referenced only in the `attio` union annotation; the
    # transport now goes through a CRMProvider, so this stays type-only.
    from clients.attio import AttioClient

# Primitives we can type-check at runtime against TypedDict annotations.
# Generic types (list[str], dict, TypedDict-of-TypedDict, etc.) skip the
# isinstance check; presence is still verified by __required_keys__.
_RUNTIME_CHECKABLE: dict[type, tuple[type, ...]] = {
    str: (str,),
    int: (int,),
    bool: (bool,),
    float: (int, float),
    list: (list,),
    dict: (dict,),
}

OPERATOR_REVIEW_QUEUE_SLUG = "operator_review_queue"

# Initial row status set on first open. Resolver flips this to
# 'resolved' | 'dismissed' | 'auto_resolved_default'.
_INITIAL_STATUS = "open"


def _validate_type(type: str) -> None:
    if type not in ESCALATION_TYPES_SET:
        raise UnknownEscalationType(
            f"escalation type {type!r} not in ESCALATION_TYPES "
            f"(see workflows.escalation_schemas — add the slug + a "
            f"TypedDict before emitting)"
        )


def _validate_payload_against_typeddict(type: str, payload: dict) -> None:
    """Runtime payload validation against the registered TypedDict.

    Two checks (engineer-QA-build3 #2 — strengthens beyond presence-only):
    1. Every field declared `total=True` on the TypedDict must be present.
    2. For fields whose annotation is a runtime-checkable primitive
       (str/int/bool/float/list/dict), the value's type is verified
       with isinstance. Generic-aliased types (e.g. `list[str]`) only
       check the container shape; deep validation is left to call sites.

    Unregistered types skip validation (and are allowed to accept any
    dict — the CI guard tracks coverage).
    """
    schema = ESCALATION_SCHEMAS.get(type)
    if schema is None:
        return
    # `from __future__ import annotations` stores annotations as strings;
    # resolve them to real types via typing.get_type_hints.
    try:
        annotations = typing.get_type_hints(schema)
    except Exception:
        # Fallback to raw __annotations__ if hint resolution fails
        # (e.g., circular forward refs). Presence check still works.
        annotations = getattr(schema, "__annotations__", {}) or {}
    # `total=True` is the TypedDict default; treat all fields as required
    # unless declared `NotRequired`. `__optional_keys__` / `__required_keys__`
    # are present on Python 3.11+ TypedDicts.
    required = getattr(schema, "__required_keys__", set(annotations.keys()))
    missing = [k for k in required if k not in payload]
    if missing:
        raise EscalationSchemaError(
            f"payload for type={type!r} is missing required fields: "
            f"{missing} (TypedDict={schema.__name__})"
        )

    # Type-check primitives. Generic types are skipped — `list[str]`
    # checks `isinstance(value, list)` but doesn't recurse into elements.
    for field_name, expected in annotations.items():
        if field_name not in payload:
            continue
        # Unwrap generic aliases: `list[str].__origin__` is `list`.
        origin = getattr(expected, "__origin__", expected)
        checkable = _RUNTIME_CHECKABLE.get(origin)
        if checkable is None:
            continue  # TypedDict-of-TypedDict or unknown — skip.
        if not isinstance(payload[field_name], checkable):
            actual_type_name = payload[field_name].__class__.__name__
            raise EscalationSchemaError(
                f"payload for type={type!r} has field {field_name!r} with "
                f"value of type {actual_type_name!r}, "
                f"expected {origin.__name__!r} (TypedDict={schema.__name__})"
            )

    # type-design IMPORTANT-1: per-type semantic validators.
    #
    # `dedup_experiment_id_conflict` semantically requires ≥2 distinct
    # experiment_ids — that IS the conflict. An empty or single-element
    # `experiment_ids` list is incoherent and indicates a caller bug.
    if type == "dedup_experiment_id_conflict":
        exp_ids = payload.get("experiment_ids", [])
        if not isinstance(exp_ids, list) or len(exp_ids) < 2:
            raise EscalationSchemaError(
                f"payload for type={type!r} requires `experiment_ids` "
                f"with ≥2 distinct ids (the conflict); got {exp_ids!r}"
            )
        # winner_id MUST NOT appear in loser_ids — a record can't be its
        # own duplicate. type-design NIT: disjointness check.
        winner_id = payload.get("winner_id")
        loser_ids = payload.get("loser_ids", [])
        if winner_id and isinstance(loser_ids, list) and winner_id in loser_ids:
            raise EscalationSchemaError(
                f"payload for type={type!r}: winner_id={winner_id!r} "
                f"appears in loser_ids — a record cannot be its own duplicate"
            )

    # type-design NIT: `dedup_review` also gets the disjointness check —
    # record_ids must be unique (winner + losers, no record appearing twice).
    if type == "dedup_review":
        record_ids = payload.get("record_ids", [])
        if isinstance(record_ids, list) and len(record_ids) != len(set(record_ids)):
            dupes = [r for r in record_ids if record_ids.count(r) > 1]
            raise EscalationSchemaError(
                f"payload for type={type!r}: record_ids contain duplicates "
                f"({dupes!r}) — the group's records must be unique"
            )


def _build_idempotency_lookup_key(
    type: str, idempotency_key: str, decision_key: str | None
) -> str:
    """Per the F-PR-3 idempotency contract.

    For `type="configuration_decision"`, `decision_key` is part of the
    uniqueness triple so the same idempotency_key can be reused safely
    across different decision_keys.
    """
    if type == "configuration_decision":
        if decision_key is None:
            raise MissingDecisionKey(
                "type='configuration_decision' requires `decision_key` "
                "(one of CONFIGURATION_DECISION_KEYS)"
            )
        return f"{type}|{decision_key}|{idempotency_key}"
    return f"{type}|{idempotency_key}"


def escalate(
    type: str,
    idempotency_key: str,
    payload: dict[str, Any],
    *,
    deadline: date | None = None,
    decision_key: str | None = None,
    attio: CRMProvider | AttioClient | None = None,
) -> dict[str, Any]:
    """Open or refresh an Operator Review Queue row.

    Args:
        type: One of `ESCALATION_TYPES` (`len(ESCALATION_TYPES)` typed slugs).
            Raises `UnknownEscalationType` on miss.
        idempotency_key: Caller-provided dedup key. Re-calling with the
            same `(type, idempotency_key)` (or
            `(type, decision_key, idempotency_key)` when
            `type="configuration_decision"`) is a no-op that returns the
            existing row.
        payload: Validated against the TypedDict registered in
            `ESCALATION_SCHEMAS` if one exists; unregistered types accept
            any dict.
        deadline: Optional date for default-on-expiry resolvers.
        decision_key: REQUIRED when `type="configuration_decision"`. Must
            be in `CONFIGURATION_DECISION_KEYS`.
        attio: Optional CRM access (for testing / threading). Union shim
            (P1c-5): the param stays NAMED `attio` (~20 production callers
            pass it positionally/by-name) but accepts a `CRMProvider`, an
            `AttioClient` (or a test double mocking its `_request`), or
            `None`. A `CRMProvider` is used directly; anything else is
            wrapped in an `AttioProvider`; `None` builds a default
            `AttioProvider` from `ATTIO_API_KEY`. Transport then goes
            through the provider's generic object-record API.

    Returns: The Attio queue row dict.

    Raises:
        UnknownEscalationType: `type` not in `ESCALATION_TYPES`.
        MissingDecisionKey: `type="configuration_decision"` without a
            valid `decision_key`.
        EscalationSchemaError: payload doesn't match the registered
            TypedDict.
    """
    _validate_type(type)

    if type == "configuration_decision":
        if decision_key is None or decision_key not in CONFIGURATION_DECISION_KEYS_SET:
            raise MissingDecisionKey(
                f"type='configuration_decision' requires `decision_key` "
                f"in CONFIGURATION_DECISION_KEYS; got {decision_key!r}"
            )
        # The payload schema also carries decision_key — keep them in
        # lockstep so an operator reading the Attio row only needs the
        # payload, not the function arguments.
        payload = {**payload, "decision_key": decision_key}

    _validate_payload_against_typeddict(type, payload)

    # ---- Normalize transport to a CRMProvider (P1c-5 union shim) ----
    # A CRMProvider is used directly; an AttioClient (or a test double
    # mocking its `_request`) is wrapped in an AttioProvider; None builds a
    # default AttioProvider from ATTIO_API_KEY. AttioProvider construction
    # bottoms out in AttioClient.__init__, which raises a bare KeyError when
    # ATTIO_API_KEY is unset — surface a typed error so callers can
    # distinguish config-missing from real Attio failures.
    crm: CRMProvider
    if isinstance(attio, CRMProvider):
        crm = attio
    else:
        try:
            crm = AttioProvider() if attio is None else AttioProvider(attio)
        except KeyError as exc:
            raise MissingAttioCredentials(
                "escalate() needs Attio access but ATTIO_API_KEY is unset. "
                "Set the env var or pass attio=AttioClient(api_key=...) "
                "explicitly."
            ) from exc

    lookup_key = _build_idempotency_lookup_key(type, idempotency_key, decision_key)

    # ---- Idempotency check ----
    existing = _find_existing_row(crm, lookup_key)
    if existing is not None:
        return existing

    # ---- Open new row ----
    return _create_row(
        crm,
        type_slug=type,
        idempotency_key=idempotency_key,
        decision_key=decision_key,
        payload=payload,
        deadline=deadline,
    )


def _find_existing_row(crm: CRMProvider, lookup_key: str) -> dict | None:
    """Look up an existing queue row by its uniqueness_key text attribute.

    Attio v2 text attributes are searchable via `$eq` filter but are NOT
    indexed — this is an O(n) scan on every escalate() call. The queue
    is expected to stay in the low-thousands range (one row per operator
    decision; resolved rows can be archived) so this stays fast. If the
    queue ever grows past ~10k open rows, a follow-up should add a
    composite-key custom object or move the dedup to a local SQLite cache.
    """
    # P1c-5: query via the vendor-neutral generic object-record API.
    # Operator Review Queue is a custom object; `query_object_records`
    # builds the same `{"filter": ..., "limit": 1}` body the raw call used
    # and passes the filter through as Attio's native query body (the
    # documented filter-shape leak). Return the UNTOUCHED vendor payload
    # (``Record.raw``) so the existing-row identity + `["values"]` indexing
    # the F-PR-3 contract relies on are byte-for-byte preserved.
    records = crm.query_object_records(
        OPERATOR_REVIEW_QUEUE_SLUG,
        filters={"uniqueness_key": {"$eq": lookup_key}},
        limit=1,
    )
    return records[0].raw if records else None


def _create_row(
    crm: CRMProvider,
    *,
    type_slug: str,
    idempotency_key: str,
    decision_key: str | None,
    payload: dict,
    deadline: date | None,
) -> dict:
    """Insert a new queue row."""
    attrs: dict[str, Any] = {
        "type": type_slug,
        "idempotency_key": idempotency_key,
        "uniqueness_key": _build_idempotency_lookup_key(
            type_slug, idempotency_key, decision_key
        ),
        "payload_json": json.dumps(payload, default=str),
        "status": _INITIAL_STATUS,
        "opened_at": datetime.now(UTC).isoformat(),
        "decision_source": "agent_opened",
        "agent_host": os.uname().nodename if hasattr(os, "uname") else "",
    }
    if decision_key is not None:
        attrs["decision_key"] = decision_key
    if deadline is not None:
        attrs["deadline"] = deadline.isoformat()

    # P1c-6b: open the row through the CRM seam. The READ side
    # (`_find_existing_row`) migrated onto `query_object_records` at P1c-5;
    # the CREATE side now follows onto `create_object_record`, completing the
    # operator-review-queue migration. The inner-client escape hatch is gone
    # from this module.
    #
    # Behavior preservation: `create_object_record` issues the SAME
    # `POST /objects/<slug>/records {"data": {"values": attrs}}` body via the
    # inner `_request` (identical retry / transport) the pre-6b `_create_row`
    # did, then normalizes the response via `_to_record`. We return
    # `record.raw` — the UNTOUCHED vendor payload — so for the well-formed
    # dict body that every escalate caller (and the F-PR-3 contract's
    # `["values"]` indexing / existing-row identity) relies on, the return is
    # byte-for-byte identical to the pre-6b `data.get("data", data)`.
    # `_to_record` was hardened at 6a to tolerate empty / non-dict / malformed
    # create bodies without raising (e.g. detect-responses doubles that stub
    # `_request` → `{"data": []}`): such a body degrades to `record.raw == {}`
    # where the pre-6b code returned `[]`. That divergence is unobservable —
    # no escalate caller or test reads the create return as a list — and no
    # full-suite test exercises this degenerate create path (3318 still pass).
    return crm.create_object_record(OPERATOR_REVIEW_QUEUE_SLUG, attrs).raw
