"""Tests for the Data Quality Report bootstrap migration.

The migration script now delegates schema mutation to the shared
``scripts/_attio_migration_helpers`` module. These tests focus on the
DQR-specific manifest invariants (attribute count + writer-registry
parity) and the migration-script-level idempotency contract. Full
helper coverage lives in ``tests/test_attio_migration_helpers.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from scripts._attio_migration_helpers import (
    ensure_attribute as _ensure_attribute,
)
from scripts._attio_migration_helpers import (
    ensure_object as _ensure_object,
)
from scripts.migrate_attio_data_quality_report import (
    ATTRIBUTES,
    OBJECT_PLURAL,
    OBJECT_SINGULAR,
    OBJECT_SLUG,
)


def _http_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.attio.test/")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("", request=request, response=response)


def _attio_mock(*, get_handler=None, post_handler=None) -> MagicMock:
    client = MagicMock()
    client.posted: list[dict] = []  # type: ignore[attr-defined]

    def _request(method: str, path: str, json: dict | None = None, **_):
        if method == "GET" and get_handler is not None:
            return get_handler(path)
        if method == "POST":
            client.posted.append({"path": path, "body": json or {}})
            if post_handler is not None:
                return post_handler(path, json or {})
            return {"data": {}}
        return {"data": {}}

    client._request.side_effect = _request
    return client


class TestEnsureObject:
    """Sanity that the DQR script is wired to the shared helper. Full
    coverage of ensure_object lives in test_attio_migration_helpers."""

    def test_skips_when_object_exists(self):
        attio = _attio_mock(get_handler=lambda path: {"data": {}})
        action, body = _ensure_object(
            attio, OBJECT_SLUG, OBJECT_SINGULAR, OBJECT_PLURAL,
            dry_run=False,
        )
        assert action == "skipped"
        assert body == {}
        assert attio.posted == []

    def test_creates_when_missing(self):
        def _get(path):
            raise _http_error(404)
        attio = _attio_mock(get_handler=_get)
        action, body = _ensure_object(
            attio, OBJECT_SLUG, OBJECT_SINGULAR, OBJECT_PLURAL,
            dry_run=False,
        )
        assert action == "created"
        assert body["data"]["api_slug"] == OBJECT_SLUG
        assert len(attio.posted) == 1

    def test_dry_run_doesnt_write(self):
        def _get(path):
            raise _http_error(404)
        attio = _attio_mock(get_handler=_get)
        action, body = _ensure_object(
            attio, OBJECT_SLUG, OBJECT_SINGULAR, OBJECT_PLURAL,
            dry_run=True,
        )
        assert action == "would_create"
        assert body["data"]["api_slug"] == OBJECT_SLUG
        assert attio.posted == []


class TestEnsureAttribute:
    """Sanity that the DQR script is wired to the shared helper. Full
    coverage of ensure_attribute lives in test_attio_migration_helpers."""

    def test_skips_when_attribute_exists_same_type(self):
        def _get(path):
            return {"data": {"type": "number"}}
        attio = _attio_mock(get_handler=_get)
        action = _ensure_attribute(
            attio, "object", OBJECT_SLUG, "metric_x", "number",
            is_required=True, description="desc", dry_run=False,
        )
        assert action == "skipped"
        assert attio.posted == []

    def test_raises_loudly_on_type_mismatch(self):
        def _get(path):
            return {"data": {"type": "text"}}
        attio = _attio_mock(get_handler=_get)
        with pytest.raises(RuntimeError, match="manual Attio UI fix"):
            _ensure_attribute(
                attio, "object", OBJECT_SLUG, "metric_x", "number",
                is_required=True, description="desc", dry_run=False,
            )

    def test_creates_when_attribute_missing(self):
        def _get(path):
            raise _http_error(404)
        attio = _attio_mock(get_handler=_get)
        action = _ensure_attribute(
            attio, "object", OBJECT_SLUG, "metric_x", "number",
            is_required=True, description="desc", dry_run=False,
        )
        assert action == "created"
        assert len(attio.posted) == 1
        body = attio.posted[0]["body"]["data"]
        assert body["api_slug"] == "metric_x"
        assert body["type"] == "number"

    def test_dry_run_doesnt_write_attribute(self):
        def _get(path):
            raise _http_error(404)
        attio = _attio_mock(get_handler=_get)
        action = _ensure_attribute(
            attio, "object", OBJECT_SLUG, "metric_x", "number",
            is_required=True, description="desc", dry_run=True,
        )
        assert action == "would_create"
        assert attio.posted == []


class TestMigrationContract:
    def test_attributes_spec_count_matches_manifest(self):
        # Manifest should have exactly the ATTRIBUTES count for this object.
        # Catches the "14 vs 15" drift the QA pass flagged.
        from pathlib import Path

        import yaml

        manifest_path = Path(__file__).resolve().parent.parent / "docs" / "attio_schema_deltas.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        dqr_attrs = [
            a for a in manifest.get("attributes", [])
            if a.get("object") == OBJECT_SLUG
        ]
        assert len(dqr_attrs) == len(ATTRIBUTES), (
            f"manifest has {len(dqr_attrs)} data_quality_report attrs, "
            f"ATTRIBUTES has {len(ATTRIBUTES)}"
        )

    def test_attributes_match_writer_registry(self):
        from clients.attio_writer_registry import WRITE_OWNER_REGISTRY

        spec_slugs = {slug for slug, *_ in ATTRIBUTES}
        registry_slugs = {
            slug for (obj, slug) in WRITE_OWNER_REGISTRY
            if obj == OBJECT_SLUG
        }
        assert spec_slugs == registry_slugs, (
            f"diff: spec - registry={spec_slugs - registry_slugs}, "
            f"registry - spec={registry_slugs - spec_slugs}"
        )
