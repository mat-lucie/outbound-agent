"""Unit tests for ``scripts/_attio_migration_helpers``.

Covers the v2 Attio API contract these helpers encode:

  * Type mapping (``long_text`` → ``text``, ``datetime`` → ``timestamp``,
    ``record_reference`` → ``record-reference``) applied on BOTH the
    request side and the idempotency comparison side.
  * Required POST fields: ``is_unique``, ``is_multiselect``, ``config``,
    ``description``, ``default_value``.
  * Currency config shape: ``{"currency": {"default_currency_code":
    "USD", "display_type": "symbol"}}``.
  * Record-reference config shape: ``{"record_reference":
    {"allowed_object_ids": [<UUID>]}}``.
  * Select-option backfill when an existing attribute has no / partial
    options.
  * Idempotency: existing attr with matching MAPPED type returns
    ``"skipped"`` (not "type mismatch") even when the source-side spec
    used the unmapped name (e.g. ``datetime`` vs. ``timestamp``).
  * Object/list endpoint routing: list-attrs POST to
    ``/lists/{list_id}/attributes``, object-attrs POST to
    ``/objects/{slug}/attributes``.

No live Attio calls — ``AttioClient`` is mocked end-to-end.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from scripts import _attio_migration_helpers as helpers
from scripts._attio_migration_helpers import (
    TYPE_MAP,
    build_attribute_body,
    ensure_attribute,
    ensure_object,
    map_type,
    reconcile_select_options,
    resolve_referenced_object_id,
)


@pytest.fixture(autouse=True)
def _clear_resolver_cache() -> None:
    helpers._clear_referenced_id_cache()


# ---------------------------------------------------------------------------
# Helpers: build a minimal fake AttioClient that records calls and lets
# tests script GET responses + raise 404s on demand.
# ---------------------------------------------------------------------------


def _make_404() -> httpx.HTTPStatusError:
    resp = httpx.Response(404, request=httpx.Request("GET", "https://example/"))
    return httpx.HTTPStatusError("not found", request=resp.request, response=resp)


class FakeAttio:
    """Minimal AttioClient stand-in. Tests pre-load `responses` keyed by
    ``(method, path)`` — falsy entries raise 404. Records every call in
    ``calls`` for assertions.
    """

    def __init__(self) -> None:
        self.responses: dict[tuple[str, str], Any] = {}
        self.calls: list[dict[str, Any]] = []

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        self.calls.append({"method": method, "path": path, **kwargs})
        if (method, path) in self.responses:
            entry = self.responses[(method, path)]
            if isinstance(entry, Exception):
                raise entry
            return entry
        # Default: 404 for GETs, success for POSTs.
        if method == "GET":
            raise _make_404()
        return {"data": {"id": {"object_id": "auto-id"}}}


# ---------------------------------------------------------------------------
# TYPE_MAP / map_type
# ---------------------------------------------------------------------------


def test_type_map_covers_v2_drift() -> None:
    """The two API-rename drifts MUST be covered."""
    assert TYPE_MAP["long_text"] == "text"
    assert TYPE_MAP["datetime"] == "timestamp"
    assert TYPE_MAP["record_reference"] == "record-reference"


def test_map_type_canonical_pass_through() -> None:
    for t in ("text", "number", "select", "date", "checkbox", "currency", "timestamp"):
        assert map_type(t) == t


def test_map_type_unknown_passes_through() -> None:
    # Surface typos via Attio's 400, not silent default.
    assert map_type("blueberry") == "blueberry"


# ---------------------------------------------------------------------------
# build_attribute_body — per type.
# ---------------------------------------------------------------------------


def _required_keys() -> set[str]:
    """v2 API rejects POSTs missing any of these on data.* — assert all
    present in every build_attribute_body output."""
    return {
        "api_slug",
        "title",
        "description",
        "type",
        "is_required",
        "is_unique",
        "is_multiselect",
        "config",
        "default_value",
    }


def test_build_body_text_has_all_required_fields() -> None:
    body = build_attribute_body("run_id", "text", is_required=True,
                                description="Run identifier.")
    data = body["data"]
    assert _required_keys().issubset(data.keys())
    assert data["type"] == "text"
    assert data["is_unique"] is False
    assert data["is_multiselect"] is False
    assert data["config"] == {}
    assert data["description"] == "Run identifier."
    assert data["is_required"] is True


def test_build_body_long_text_maps_to_text() -> None:
    body = build_attribute_body("failure_details_pointer", "long_text",
                                description="JSON pointer.")
    assert body["data"]["type"] == "text"


def test_build_body_datetime_maps_to_timestamp() -> None:
    body = build_attribute_body("started", "datetime", is_required=True,
                                description="Start.")
    assert body["data"]["type"] == "timestamp"


def test_build_body_select_carries_options() -> None:
    body = build_attribute_body(
        "rollback_status", "select",
        options=["ready", "applied", "failed", "irreversible"],
        is_required=True, description="Lifecycle.",
    )
    assert body["data"]["type"] == "select"
    assert body["data"]["config"] == {
        "options": [
            {"title": "ready"}, {"title": "applied"},
            {"title": "failed"}, {"title": "irreversible"},
        ]
    }


def test_build_body_select_without_options_still_valid_shape() -> None:
    """A bare select still gets ``config.options=[]`` so the v2 API
    sees the required key."""
    body = build_attribute_body("status", "select", description="Lifecycle.")
    assert body["data"]["config"] == {"options": []}


def test_build_body_currency_uses_correct_v2_shape() -> None:
    """Per Attio API discovery 2026-05-25: currency lives under
    ``config.currency = {default_currency_code, display_type}`` — NOT
    flat ``config.currency_code``."""
    body = build_attribute_body("cost_usd_actual", "currency",
                                description="Spend.")
    assert body["data"]["type"] == "currency"
    assert body["data"]["config"] == {
        "currency": {
            "default_currency_code": "USD",
            "display_type": "symbol",
        }
    }


def test_build_body_currency_respects_overrides() -> None:
    body = build_attribute_body(
        "cost_eur", "currency",
        currency_code="EUR", currency_display="code",
        description="Spend EUR.",
    )
    cfg = body["data"]["config"]["currency"]
    assert cfg["default_currency_code"] == "EUR"
    assert cfg["display_type"] == "code"


def test_build_body_record_reference_uses_correct_v2_shape() -> None:
    """Per Attio API discovery 2026-05-25: record_reference lives under
    ``config.record_reference = {allowed_objects: [<UUID>]}`` (plural,
    no ``_ids`` suffix). API asymmetry: GET returns ``allowed_object_ids``
    but POST requires ``allowed_objects``. Takes the target object's
    UUID, not its slug."""
    body = build_attribute_body(
        "last_migrated_by", "record_reference",
        referenced_object_id="9c9496a5-6e72-4b5c-8a0d-dd2790b95158",
        description="Provenance.",
    )
    assert body["data"]["type"] == "record-reference"
    assert body["data"]["config"] == {
        "record_reference": {
            "allowed_objects": ["9c9496a5-6e72-4b5c-8a0d-dd2790b95158"]
        }
    }


def test_build_body_record_reference_without_object_id_raises() -> None:
    with pytest.raises(ValueError, match="record_reference"):
        build_attribute_body("last_migrated_by", "record_reference",
                             description="No id.")


def test_build_body_default_description_from_slug() -> None:
    body = build_attribute_body("run_id", "text")
    # Default description is the human-readable slug.
    assert body["data"]["description"] == "Run Id"
    assert body["data"]["description"] is not None  # v2 rejects null.


# ---------------------------------------------------------------------------
# ensure_object
# ---------------------------------------------------------------------------


def test_ensure_object_existing_returns_skipped() -> None:
    attio = FakeAttio()
    attio.responses[("GET", "/objects/migration_run")] = {"data": {"id": {"object_id": "x"}}}
    action, body = ensure_object(attio, "migration_run", "MR", "MRs", dry_run=False)
    assert action == "skipped"
    assert body == {}


def test_ensure_object_missing_creates() -> None:
    attio = FakeAttio()
    # GET 404s by default; POST returns success.
    action, body = ensure_object(attio, "migration_run", "MR", "MRs", dry_run=False)
    assert action == "created"
    assert body["data"]["api_slug"] == "migration_run"
    # Verify a POST was made.
    post_calls = [c for c in attio.calls if c["method"] == "POST"]
    assert len(post_calls) == 1
    assert post_calls[0]["path"] == "/objects"


def test_ensure_object_dry_run_returns_would_create_no_post() -> None:
    attio = FakeAttio()
    action, body = ensure_object(attio, "migration_run", "MR", "MRs", dry_run=True)
    assert action == "would_create"
    assert body["data"]["api_slug"] == "migration_run"
    post_calls = [c for c in attio.calls if c["method"] == "POST"]
    assert post_calls == []


# ---------------------------------------------------------------------------
# resolve_referenced_object_id
# ---------------------------------------------------------------------------


def test_resolve_referenced_object_id_hits_cache_on_second_call() -> None:
    attio = FakeAttio()
    attio.responses[("GET", "/objects/migration_run")] = {
        "data": {"id": {"object_id": "uuid-1"}}
    }
    first = resolve_referenced_object_id(attio, "migration_run")
    second = resolve_referenced_object_id(attio, "migration_run")
    assert first == second == "uuid-1"
    # Only one GET should hit the wire.
    get_calls = [c for c in attio.calls if c["method"] == "GET"]
    assert len(get_calls) == 1


def test_resolve_referenced_object_id_missing_object_raises() -> None:
    attio = FakeAttio()
    # 404 by default → RuntimeError with helpful message.
    with pytest.raises(RuntimeError, match="does not exist"):
        resolve_referenced_object_id(attio, "ghost_object")


def test_ensure_object_create_invalidates_resolver_cache() -> None:
    """If a prior resolver cached a UUID for slug X, and then X is
    recreated via ensure_object (in the same process), the cache must
    drop X so the next resolve hits Attio for the fresh UUID. Otherwise
    record-reference attrs created post-recreate point at a dead UUID.
    """
    from scripts._attio_migration_helpers import _REFERENCED_ID_CACHE
    _REFERENCED_ID_CACHE["ghost"] = "stale-uuid"
    attio = FakeAttio()
    # GET 404s → POST succeeds → "created" path → cache should drop "ghost".
    ensure_object(attio, "ghost", "G", "Gs", dry_run=False)
    assert "ghost" not in _REFERENCED_ID_CACHE


# ---------------------------------------------------------------------------
# reconcile_select_options
# ---------------------------------------------------------------------------


def test_reconcile_select_all_present_returns_skipped() -> None:
    attio = FakeAttio()
    attio.responses[("GET", "/objects/migration_run/attributes/status/options")] = {
        "data": [{"title": "ready"}, {"title": "applied"}]
    }
    action = reconcile_select_options(
        attio, "object", "migration_run", "status",
        ["ready", "applied"], dry_run=False,
    )
    assert action == "skipped"


def test_reconcile_select_backfills_missing_options() -> None:
    """The §3.13 brokenness: rollback_status was created with no options
    in a partial earlier run. Pure 'skip if exists' missed it — this
    helper must POST the missing options."""
    attio = FakeAttio()
    attio.responses[("GET", "/objects/migration_run/attributes/rollback_status/options")] = {
        "data": []  # Created without options.
    }
    action = reconcile_select_options(
        attio, "object", "migration_run", "rollback_status",
        ["ready", "applied", "failed", "irreversible"], dry_run=False,
    )
    assert action == "added_options:4"
    post_calls = [c for c in attio.calls if c["method"] == "POST"]
    assert len(post_calls) == 4
    titles = {c["json"]["data"]["title"] for c in post_calls}
    assert titles == {"ready", "applied", "failed", "irreversible"}


def test_reconcile_select_dry_run_does_not_post() -> None:
    attio = FakeAttio()
    attio.responses[("GET", "/objects/migration_run/attributes/rollback_status/options")] = {
        "data": [{"title": "ready"}]
    }
    action = reconcile_select_options(
        attio, "object", "migration_run", "rollback_status",
        ["ready", "applied", "failed"], dry_run=True,
    )
    assert action == "would_add_options:2"
    post_calls = [c for c in attio.calls if c["method"] == "POST"]
    assert post_calls == []


def test_reconcile_select_uses_list_endpoint_when_parent_is_list() -> None:
    """Selects on a LIST attribute must read/write via /lists/..., not
    /objects/...."""
    attio = FakeAttio()
    attio.responses[("GET", "/lists/list-uuid/attributes/foo/options")] = {
        "data": []
    }
    reconcile_select_options(
        attio, "list", "list-uuid", "foo", ["a"], dry_run=False,
    )
    paths = [c["path"] for c in attio.calls]
    assert all(p.startswith("/lists/list-uuid/attributes/foo") for p in paths)


def test_reconcile_select_ignores_archived_options() -> None:
    """Archived options come back in the GET payload with is_archived=True.
    They look "present" by title but Attio rejects writes to them. The
    helper must filter them out so the backfill re-adds a live copy.
    """
    attio = FakeAttio()
    attio.responses[("GET", "/objects/migration_run/attributes/rollback_status/options")] = {
        "data": [
            {"title": "ready", "is_archived": False},
            {"title": "applied", "is_archived": False},
            {"title": "failed", "is_archived": True},  # operator archived via UI
            {"title": "irreversible", "is_archived": False},
        ]
    }
    action = reconcile_select_options(
        attio, "object", "migration_run", "rollback_status",
        ["ready", "applied", "failed", "irreversible"], dry_run=False,
    )
    # `failed` should be re-added because the archived version doesn't count.
    assert action == "added_options:1"
    post_calls = [c for c in attio.calls if c["method"] == "POST"]
    assert len(post_calls) == 1
    assert post_calls[0]["json"]["data"]["title"] == "failed"


# ---------------------------------------------------------------------------
# ensure_attribute — full integration of mapping + idempotency + backfill.
# ---------------------------------------------------------------------------


def test_ensure_attribute_missing_creates_with_correct_body() -> None:
    attio = FakeAttio()
    # GET 404 (missing) → POST.
    action = ensure_attribute(
        attio, "object", "data_quality_report", "run_id", "text",
        is_required=True, description="Unique id.", dry_run=False,
    )
    assert action == "created"
    post_calls = [c for c in attio.calls if c["method"] == "POST"]
    assert len(post_calls) == 1
    body = post_calls[0]["json"]["data"]
    assert body["api_slug"] == "run_id"
    assert body["type"] == "text"
    assert body["description"] == "Unique id."
    assert body["is_unique"] is False
    assert body["is_multiselect"] is False
    assert body["config"] == {}


def test_ensure_attribute_existing_same_mapped_type_returns_skipped() -> None:
    """The §3.13 stable-type mismatch: source spec is ``datetime`` but
    API returns ``timestamp``. ensure_attribute must NOT spuriously flag
    a type migration — apply TYPE_MAP on the comparison side too."""
    attio = FakeAttio()
    attio.responses[("GET", "/objects/migration_run/attributes/started")] = {
        "data": {"id": {"attribute_id": "x"}, "type": "timestamp"}
    }
    action = ensure_attribute(
        attio, "object", "migration_run", "started", "datetime",
        is_required=True, description="Start.", dry_run=False,
    )
    assert action == "skipped"


def test_ensure_attribute_existing_long_text_maps_to_text_skipped() -> None:
    attio = FakeAttio()
    attio.responses[("GET", "/objects/migration_run/attributes/failure_details_pointer")] = {
        "data": {"id": {"attribute_id": "x"}, "type": "text"}
    }
    action = ensure_attribute(
        attio, "object", "migration_run", "failure_details_pointer", "long_text",
        is_required=False, description="JSON.", dry_run=False,
    )
    assert action == "skipped"


def test_ensure_attribute_record_reference_maps_to_kebab_on_compare() -> None:
    attio = FakeAttio()
    attio.responses[("GET", "/lists/list-uuid/attributes/last_migrated_by")] = {
        "data": {"id": {"attribute_id": "x"}, "type": "record-reference"}
    }
    action = ensure_attribute(
        attio, "list", "list-uuid", "last_migrated_by", "record_reference",
        referenced_object="migration_run",
        description="Provenance.", dry_run=False,
    )
    assert action == "skipped"


def test_ensure_attribute_type_mismatch_raises() -> None:
    attio = FakeAttio()
    attio.responses[("GET", "/objects/migration_run/attributes/started")] = {
        "data": {"id": {"attribute_id": "x"}, "type": "date"}
    }
    with pytest.raises(RuntimeError, match="type migrations are irreversible"):
        ensure_attribute(
            attio, "object", "migration_run", "started", "datetime",
            description="Start.", dry_run=False,
        )


def test_ensure_attribute_select_existing_with_partial_options_backfills() -> None:
    """The §3.13 brokenness: rollback_status created without options.
    ensure_attribute for an existing select must reach into
    reconcile_select_options to backfill — NOT just return 'skipped'."""
    attio = FakeAttio()
    attio.responses[("GET", "/objects/migration_run/attributes/rollback_status")] = {
        "data": {"id": {"attribute_id": "x"}, "type": "select"}
    }
    attio.responses[("GET", "/objects/migration_run/attributes/rollback_status/options")] = {
        "data": [{"title": "ready"}]  # Only one of four expected options.
    }
    action = ensure_attribute(
        attio, "object", "migration_run", "rollback_status", "select",
        options=["ready", "applied", "failed", "irreversible"],
        is_required=True, description="Lifecycle.", dry_run=False,
    )
    assert action == "added_options:3"


def test_ensure_attribute_record_reference_create_resolves_target_uuid() -> None:
    """Creating a new record-reference attr must look up the target
    object's UUID and embed it in config.record_reference.allowed_objects
    (plural, no _ids suffix — the POST contract differs from GET)."""
    attio = FakeAttio()
    # /lists/.../attributes/last_migrated_by → 404 (missing).
    # /objects/migration_run → returns object_id for resolver.
    attio.responses[("GET", "/objects/migration_run")] = {
        "data": {"id": {"object_id": "uuid-mr"}}
    }
    action = ensure_attribute(
        attio, "list", "list-uuid", "last_migrated_by", "record_reference",
        referenced_object="migration_run",
        description="Provenance.", dry_run=False,
    )
    assert action == "created"
    post_calls = [c for c in attio.calls if c["method"] == "POST"]
    assert len(post_calls) == 1
    assert post_calls[0]["path"] == "/lists/list-uuid/attributes"
    body = post_calls[0]["json"]["data"]
    assert body["type"] == "record-reference"
    assert body["config"] == {
        "record_reference": {"allowed_objects": ["uuid-mr"]}
    }


def test_ensure_attribute_currency_create_uses_correct_config_shape() -> None:
    attio = FakeAttio()
    action = ensure_attribute(
        attio, "object", "reclassification_run", "cost_usd_actual", "currency",
        is_required=False, description="Spend.", dry_run=False,
    )
    assert action == "created"
    post_calls = [c for c in attio.calls if c["method"] == "POST"]
    body = post_calls[0]["json"]["data"]
    assert body["type"] == "currency"
    assert body["config"] == {
        "currency": {"default_currency_code": "USD", "display_type": "symbol"}
    }


def test_ensure_attribute_dry_run_no_post() -> None:
    attio = FakeAttio()
    action = ensure_attribute(
        attio, "object", "data_quality_report", "run_id", "text",
        description="x.", dry_run=True,
    )
    assert action == "would_create"
    post_calls = [c for c in attio.calls if c["method"] == "POST"]
    assert post_calls == []


def test_ensure_attribute_list_routes_to_lists_endpoint() -> None:
    attio = FakeAttio()
    ensure_attribute(
        attio, "list", "list-uuid", "last_observed_at", "date",
        description="Date.", dry_run=False,
    )
    post_calls = [c for c in attio.calls if c["method"] == "POST"]
    assert post_calls[0]["path"] == "/lists/list-uuid/attributes"


def test_ensure_attribute_object_routes_to_objects_endpoint() -> None:
    attio = FakeAttio()
    ensure_attribute(
        attio, "object", "daily_run", "run_id", "text",
        description="x.", dry_run=False,
    )
    post_calls = [c for c in attio.calls if c["method"] == "POST"]
    assert post_calls[0]["path"] == "/objects/daily_run/attributes"


def test_ensure_attribute_500_propagates() -> None:
    """Non-404 errors must surface — silent swallowing would hide infra
    failures."""
    attio = FakeAttio()
    err_resp = httpx.Response(500, request=httpx.Request("GET", "https://x/"))
    attio.responses[("GET", "/objects/data_quality_report/attributes/run_id")] = (
        httpx.HTTPStatusError("oops", request=err_resp.request, response=err_resp)
    )
    with pytest.raises(httpx.HTTPStatusError):
        ensure_attribute(
            attio, "object", "data_quality_report", "run_id", "text",
            description="x.", dry_run=False,
        )


# ---------------------------------------------------------------------------
# Smoke: real AttioClient instantiation path. (Doesn't touch network — we
# pass a MagicMock to ensure_attribute via duck typing.)
# ---------------------------------------------------------------------------


def test_ensure_attribute_works_with_magicmock_client() -> None:
    """Helpers are I/O-thin — any object exposing ``_request`` works,
    confirming the test-double pattern in this file is the same pattern
    the production AttioClient satisfies."""
    attio = MagicMock()
    attio._request.side_effect = [_make_404(), {}]  # GET 404, then POST OK.
    action = ensure_attribute(
        attio, "object", "daily_run", "run_id", "text",
        description="x.", dry_run=False,
    )
    assert action == "created"
    assert attio._request.call_count == 2
