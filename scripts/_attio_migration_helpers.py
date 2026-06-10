"""Shared Attio v2 schema-mutation helpers used by every bootstrap
migration script.

# Why this module exists

The Attio v2 API breaking-changes that landed between F-PR-3 (2026-Q1)
and now (2026-05-25) required identical patches in 4 separate migration
scripts:

  * Type renames: ``long_text`` → ``text``, ``datetime`` → ``timestamp``.
  * New required POST fields: ``is_unique`` (bool), ``is_multiselect``
    (bool), ``config`` (object, even if ``{}``), ``description`` (str).
  * Currency config: ``{"default_currency_code": "USD", "display_type":
    "symbol"}`` lives under ``config.currency`` (not flat).
  * Record-reference config: ``{"allowed_objects": [<UUID>]}`` lives
    under ``config.record_reference``, takes the target object's UUID
    (not slug). API asymmetry: GET returns ``allowed_object_ids`` but
    POST requires ``allowed_objects``.
  * Type names returned by ``GET`` use kebab-case (``record-reference``)
    not snake_case — idempotency comparison must mirror the source-side
    mapping.
  * Select options that were created without options in a partial earlier
    run need to be backfilled — pure "skip if exists" check misses them.
  * Lists vs. objects: ``linkedin_outreach`` is a LIST (parent_object=
    people) and its attributes must be POSTed to
    ``/lists/{list_id}/attributes``, not ``/objects/.../attributes``.

# Public surface

  * ``TYPE_MAP`` — source type name → API type name (kebab-case where
    needed).
  * ``build_attribute_body(slug, type_, ...)`` — single source of truth
    for the attribute-create body. Returns a dict ready to POST.
  * ``ensure_object(attio, slug, singular, plural, dry_run)`` — idempotent
    object creator. Returns ``(action, create_body)``.
  * ``ensure_attribute(attio, parent, slug, type_, ...)`` — idempotent
    attribute creator. ``parent`` can be ``("object", slug)`` or
    ``("list", list_id)``. Handles create + type-mismatch + select option
    backfill.
  * ``reconcile_select_options(attio, parent, attr_slug, expected,
    dry_run)`` — adds missing options to an existing select attribute.
  * ``resolve_referenced_object_id(attio, slug)`` — looks up the UUID
    for a record_reference target object.

All helpers are I/O-thin (delegate to ``AttioClient._request``) so they
can be unit-tested with a mocked client without touching live Attio.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx

# ---------------------------------------------------------------------------
# Type mapping. Apply consistently on BOTH the request side (build body)
# AND the comparison side (idempotency check), or the "exists with same
# type" branch will spuriously fail when source spec says `datetime` but
# API returns `timestamp`.
# ---------------------------------------------------------------------------

TYPE_MAP: dict[str, str] = {
    # Source name (what the migration scripts write) → API name (what
    # /objects/.../attributes accepts and returns).
    "long_text": "text",
    "datetime": "timestamp",
    "record_reference": "record-reference",
    # Already-canonical types pass through but are listed for self-doc.
    "text": "text",
    "number": "number",
    "select": "select",
    "date": "date",
    "checkbox": "checkbox",
    "currency": "currency",
    "timestamp": "timestamp",
}


def map_type(source_type: str) -> str:
    """Translate a source-side type name to the API-side type name.

    Unknown types pass through unchanged — Attio will reject them on POST
    and that's the right place to surface the typo (loud 400) rather than
    silently swallowing here.
    """
    return TYPE_MAP.get(source_type, source_type)


# ---------------------------------------------------------------------------
# Body builder. ONE place that knows the v2 contract.
# ---------------------------------------------------------------------------


def build_attribute_body(
    slug: str,
    type_: str,
    *,
    options: list[str] | None = None,
    is_required: bool = False,
    is_unique: bool = False,
    description: str | None = None,
    referenced_object_id: str | None = None,
    currency_code: str = "USD",
    currency_display: str = "symbol",
) -> dict[str, Any]:
    """Build the JSON body for ``POST /objects/{slug}/attributes`` or
    ``POST /lists/{list_id}/attributes``.

    Args:
        slug: ``api_slug`` for the new attribute.
        type_: Source-side type (e.g. ``datetime``, ``long_text``,
            ``record_reference``). Mapped to API-side via ``TYPE_MAP``.
        options: List of select option titles. Required when
            ``type_`` is ``"select"`` and only meaningful then.
        is_required: Whether the attribute is required for record
            creation.
        description: Human-readable description. Defaults to
            ``slug.replace("_", " ").title()`` when omitted because the
            v2 API requires the field be non-null.
        referenced_object_id: For ``record_reference`` types, the UUID
            (NOT slug) of the target object. Resolve via
            ``resolve_referenced_object_id`` first.
        currency_code: ISO-4217 code (default ``"USD"``).
        currency_display: One of ``"symbol"``, ``"code"``, ``"name"``,
            ``"narrowSymbol"``. Default ``"symbol"`` to match Attio's
            built-in ``funding_raised_usd``.

    Returns:
        Dict ready to pass as the ``json=`` kwarg to a POST. The wrapper
        is always ``{"data": {...}}``.
    """
    api_type = map_type(type_)
    desc_text = description.strip() if description else slug.replace("_", " ").title()
    body: dict[str, Any] = {
        "data": {
            "api_slug": slug,
            "title": slug.replace("_", " ").title(),
            "description": desc_text,
            "type": api_type,
            "is_required": is_required,
            "is_unique": is_unique,
            "is_multiselect": False,
            "config": {},
            "default_value": None,
        }
    }
    if api_type == "select":
        body["data"]["config"] = {
            "options": [{"title": o} for o in (options or [])]
        }
    elif api_type == "currency":
        body["data"]["config"] = {
            "currency": {
                "default_currency_code": currency_code,
                "display_type": currency_display,
            }
        }
    elif api_type == "record-reference":
        if not referenced_object_id:
            raise ValueError(
                f"build_attribute_body: type=record_reference requires "
                f"referenced_object_id (the target object's UUID), got "
                f"None for slug={slug!r}. Call "
                f"resolve_referenced_object_id(attio, target_slug) first."
            )
        # Attio API asymmetry (verified 2026-05-25): POST expects
        # `allowed_objects` (plural, no `_ids` suffix). GET returns
        # `allowed_object_ids`. Don't be misled by reading an existing
        # attr's shape via GET — the write contract is different.
        body["data"]["config"] = {
            "record_reference": {
                "allowed_objects": [referenced_object_id]
            }
        }
    return body


# ---------------------------------------------------------------------------
# Parent abstraction. Migration scripts target both objects (via
# /objects/{slug}/attributes) and lists (via /lists/{list_id}/attributes).
# Same body, different URL prefix.
# ---------------------------------------------------------------------------

ParentKind = Literal["object", "list"]


def _attr_url(parent_kind: ParentKind, parent_id: str, attr_slug: str = "") -> str:
    """Build the attribute URL for an object-attribute OR list-attribute.

    Returns the collection URL (POST target) when ``attr_slug`` is
    empty, otherwise the single-attr URL (GET / option-list target).
    """
    if parent_kind == "object":
        base = f"/objects/{parent_id}/attributes"
    elif parent_kind == "list":
        base = f"/lists/{parent_id}/attributes"
    else:
        raise ValueError(f"unknown parent_kind: {parent_kind!r}")
    return f"{base}/{attr_slug}" if attr_slug else base


# ---------------------------------------------------------------------------
# Object creator.
# ---------------------------------------------------------------------------


def ensure_object(
    attio,  # noqa: ANN001 — AttioClient duck-typed for testability
    slug: str,
    singular: str,
    plural: str,
    *,
    dry_run: bool,
) -> tuple[str, dict]:
    """Idempotent object creation.

    Returns:
        ``(action, create_body)`` where action ∈ ``{"created", "skipped",
        "would_create"}``. ``create_body`` is the full POST body — empty
        when the object already exists (skipped path) so callers can rely
        on truthiness to detect new-vs-existing.
    """
    try:
        attio._request("GET", f"/objects/{slug}")
        return ("skipped", {})
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise

    body = {
        "data": {
            "api_slug": slug,
            "singular_noun": singular,
            "plural_noun": plural,
        }
    }
    if dry_run:
        return ("would_create", body)
    attio._request("POST", "/objects", json=body)
    # Invalidate the resolver cache: if a prior process resolved this slug
    # before deletion + recreate, the cached UUID is now stale. Newly-
    # created objects get a fresh UUID; downstream record-reference attrs
    # must look it up again.
    _REFERENCED_ID_CACHE.pop(slug, None)
    return ("created", body)


# ---------------------------------------------------------------------------
# Referenced object id resolver — looks up the UUID for a target object
# slug. Cached per-process so a batch of record_reference attrs targeting
# the same object only hits Attio once.
# ---------------------------------------------------------------------------


_REFERENCED_ID_CACHE: dict[str, str] = {}


def resolve_referenced_object_id(attio, slug: str) -> str:  # noqa: ANN001
    """Return the Attio object UUID for ``slug``.

    Raises ``RuntimeError`` if the object doesn't exist — callers should
    create the referenced object first (chicken-and-egg ordering in
    bootstrap migrations is intentional).
    """
    if slug in _REFERENCED_ID_CACHE:
        return _REFERENCED_ID_CACHE[slug]
    try:
        data = attio._request("GET", f"/objects/{slug}")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise RuntimeError(
                f"resolve_referenced_object_id: object {slug!r} does not "
                f"exist. Create it (and its attributes) BEFORE registering "
                f"any record_reference attribute that targets it."
            ) from exc
        raise
    obj_id = (data.get("data", {}).get("id", {}) or {}).get("object_id")
    if not obj_id:
        raise RuntimeError(
            f"resolve_referenced_object_id: Attio returned no object_id "
            f"for slug={slug!r}. Response shape may have changed; inspect "
            f"the raw GET /objects/{slug} payload."
        )
    _REFERENCED_ID_CACHE[slug] = obj_id
    return obj_id


def clear_referenced_id_cache() -> None:
    """Drop the resolver cache. Production callers shouldn't normally need
    this — ``ensure_object`` invalidates the cache automatically when it
    creates a new object. Use this only when the target object was
    recreated outside of ``ensure_object`` (e.g., operator deleted +
    recreated via Attio UI) in the same Python process.
    """
    _REFERENCED_ID_CACHE.clear()


# Backwards-compatible alias for existing test imports.
_clear_referenced_id_cache = clear_referenced_id_cache


# ---------------------------------------------------------------------------
# Select-option backfill. F1 had this; F2 + data_quality_report + daily_run
# did not, so partial earlier runs that created the select attr without
# options would never converge. Fix: every script gets the backfill.
# ---------------------------------------------------------------------------


def reconcile_select_options(
    attio,  # noqa: ANN001
    parent_kind: ParentKind,
    parent_id: str,
    attr_slug: str,
    expected: list[str],
    *,
    dry_run: bool,
) -> str:
    """Add any expected options not yet present on a select attribute.

    Returns one of: ``"skipped"`` (all options present), ``"would_add_
    options:N"``, or ``"added_options:N"``.
    """
    url = _attr_url(parent_kind, parent_id, attr_slug) + "/options"
    data = attio._request("GET", url)
    # Filter archived options — Attio returns them in the list but rejects
    # writes to them. Without this filter, an operator archiving an option
    # via UI silently breaks idempotency: we see the title as "present"
    # and skip the backfill, then later record writes 400.
    existing = {
        opt.get("title")
        for opt in data.get("data", [])
        if not opt.get("is_archived")
    }
    missing = [o for o in expected if o not in existing]
    if not missing:
        return "skipped"
    if dry_run:
        return f"would_add_options:{len(missing)}"
    for opt in missing:
        attio._request("POST", url, json={"data": {"title": opt}})
    return f"added_options:{len(missing)}"


# ---------------------------------------------------------------------------
# Attribute creator. The big one — handles create + idempotency +
# type-mismatch + select option backfill.
# ---------------------------------------------------------------------------


def ensure_attribute(
    attio,  # noqa: ANN001
    parent_kind: ParentKind,
    parent_id: str,
    slug: str,
    type_: str,
    *,
    options: list[str] | None = None,
    is_required: bool = False,
    is_unique: bool = False,
    description: str | None = None,
    referenced_object: str | None = None,
    dry_run: bool,
) -> str:
    """Idempotent attribute creation.

    Returns one of:
      * ``"created"`` — attribute did not exist; created.
      * ``"would_create"`` — dry-run; would have created.
      * ``"skipped"`` — attribute exists with the expected type (and
        all expected select options, if applicable).
      * ``"added_options:N"`` / ``"would_add_options:N"`` — existing
        select attribute had missing options; backfilled.

    Raises:
      ``RuntimeError`` when the attribute exists with a DIFFERENT type
      (Attio doesn't support type migrations; operator must resolve via
      UI).

    Args:
      parent_kind: ``"object"`` or ``"list"``.
      parent_id: Object slug (when parent_kind="object") or list_id UUID
        (when parent_kind="list").
      referenced_object: For record_reference types, the SLUG of the
        target object (this function resolves to UUID internally).
    """
    expected_api_type = map_type(type_)
    attr_url = _attr_url(parent_kind, parent_id, slug)
    list_url = _attr_url(parent_kind, parent_id)

    try:
        data = attio._request("GET", attr_url)
        existing = data.get("data", {}) or {}
        existing_type = existing.get("type")
        if existing_type and existing_type != expected_api_type:
            raise RuntimeError(
                f"attribute {parent_id}.{slug} exists with type "
                f"{existing_type!r} but migration expects "
                f"{expected_api_type!r} (source: {type_!r}) — manual "
                f"Attio UI fix required (type migrations are irreversible)."
            )
        if expected_api_type == "select" and options:
            return reconcile_select_options(
                attio, parent_kind, parent_id, slug, options, dry_run=dry_run,
            )
        return "skipped"
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise

    if dry_run:
        return "would_create"

    ref_id: str | None = None
    if expected_api_type == "record-reference":
        if not referenced_object:
            raise ValueError(
                f"ensure_attribute({slug}): type=record_reference requires "
                f"referenced_object (target object slug)."
            )
        ref_id = resolve_referenced_object_id(attio, referenced_object)

    body = build_attribute_body(
        slug,
        type_,
        options=options,
        is_required=is_required,
        is_unique=is_unique,
        description=description,
        referenced_object_id=ref_id,
    )
    try:
        attio._request("POST", list_url, json=body)
    except httpx.HTTPStatusError as exc:
        # Re-raise with the body that was sent. Attio's 400s identify the
        # validation path but not the payload — having both makes a debug
        # cycle minutes instead of hours. Especially valuable for the
        # untested-against-live cases (currency, record-reference) where
        # the helper's guessed shape may turn out to be wrong.
        if exc.response.status_code == 400:
            raise RuntimeError(
                f"ensure_attribute({parent_kind}:{parent_id}.{slug}, "
                f"type={type_!r}): Attio rejected POST with 400. "
                f"Response: {exc.response.text}. "
                f"Sent body: {body}."
            ) from exc
        raise
    return "created"
