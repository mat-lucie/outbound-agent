"""Tests for scripts/migrate_attio_F1_operator_review_queue.py (F-PR-3)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Load the migration script as a module without making it a package.
_spec = importlib.util.spec_from_file_location(
    "migrate_attio_F1_operator_review_queue",
    REPO_ROOT / "scripts" / "migrate_attio_F1_operator_review_queue.py",
)
mig = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mig
_spec.loader.exec_module(mig)


def _make_http_404(path: str) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", f"https://api.attio.com/v2/{path}")
    resp = httpx.Response(404, request=req)
    return httpx.HTTPStatusError("not found", request=req, response=resp)


@pytest.fixture
def audit_tmp(tmp_path, monkeypatch):
    """Redirect the bootstrap JSONL audit dir to a tmp path."""
    monkeypatch.setattr(mig, "AUDIT_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def attio_first_run(monkeypatch):
    """AttioClient where the queue object does NOT exist yet."""
    attio = MagicMock()

    def _request(method, path, json=None, **kwargs):
        # Object doesn't exist.
        if method == "GET" and path == f"/objects/{mig.OBJECT_SLUG}":
            raise _make_http_404(path)
        if method == "POST" and path == "/objects":
            return {"data": {"api_slug": mig.OBJECT_SLUG}}
        # No attribute exists yet.
        if method == "GET" and "/attributes/" in path:
            raise _make_http_404(path)
        if method == "POST" and path.endswith("/attributes"):
            return {"data": {}}
        return {"data": {}}

    attio._request.side_effect = _request

    def _factory():
        return attio

    monkeypatch.setattr(mig, "AttioClient", _factory)
    return attio


@pytest.fixture
def attio_second_run(monkeypatch):
    """AttioClient where the queue object + every attribute exists with
    the right type + every select option already registered."""
    attio = MagicMock()

    # Real Attio API returns MAPPED types (long_text→text, datetime→timestamp,
    # record_reference→record-reference). The shared helper applies TYPE_MAP
    # on the comparison side, so this fixture must mirror what the API actually
    # returns — not the source-side spec.
    from scripts._attio_migration_helpers import map_type

    attribute_types = {slug: map_type(type_) for slug, type_, *_ in mig.ATTRIBUTES}
    attribute_options = {
        slug: list(opts) for slug, _type, opts, *_ in mig.ATTRIBUTES if opts
    }

    def _request(method, path, json=None, **kwargs):
        if method == "GET" and path == f"/objects/{mig.OBJECT_SLUG}":
            return {"data": {"api_slug": mig.OBJECT_SLUG}}
        if method == "GET" and "/attributes/" in path and not path.endswith("/options"):
            slug = path.rsplit("/", 1)[-1]
            t = attribute_types.get(slug)
            if t is None:
                raise _make_http_404(path)
            return {"data": {"api_slug": slug, "type": t}}
        if method == "GET" and path.endswith("/options"):
            slug = path.split("/attributes/")[1].rsplit("/", 1)[0]
            opts = attribute_options.get(slug, [])
            return {"data": [{"title": o} for o in opts]}
        # Any POST is a write — the test asserts none happen.
        if method == "POST":
            raise AssertionError(f"second run should not write: {method} {path}")
        return {"data": {}}

    attio._request.side_effect = _request

    def _factory():
        return attio

    monkeypatch.setattr(mig, "AttioClient", _factory)
    return attio


class TestObjectCreate:
    def test_first_run_creates_object_then_attributes(self, audit_tmp, attio_first_run):
        rc = mig.main([])
        assert rc == 0
        # One JSONL line was written.
        path = mig._audit_path()
        assert path.exists()
        line = json.loads(path.read_text().strip())
        assert line["rows_failed"] == 0
        # First run: object + 12 attributes = 13 examined rows; all modified.
        assert line["rows_examined"] == 1 + len(mig.ATTRIBUTES)
        assert line["rows_modified"] == 1 + len(mig.ATTRIBUTES)
        assert line["rows_skipped_idempotent"] == 0

    def test_first_run_dry_run_doesnt_write(self, audit_tmp, attio_first_run):
        rc = mig.main(["--dry-run"])
        assert rc == 0
        line = json.loads(mig._audit_path().read_text().strip())
        assert line["dry_run"] is True
        # All actions should be `would_create` (or similar prefix).
        for a in line["actions"]:
            assert "would" in a.get("action", "") or a.get("action") == "failed"


class TestIdempotency:
    def test_second_run_is_no_op(self, audit_tmp, attio_second_run):
        """Section 9 done-criterion: second consecutive run must be
        no-op. rows_modified == 0, rows_skipped_idempotent == rows_examined."""
        rc = mig.main([])
        assert rc == 0
        line = json.loads(mig._audit_path().read_text().strip())
        assert line["rows_failed"] == 0
        assert line["rows_modified"] == 0
        assert line["rows_skipped_idempotent"] == line["rows_examined"]


class TestTypeMismatch:
    def test_type_mismatch_fails_loud(self, audit_tmp, monkeypatch):
        """If an attribute exists with a DIFFERENT type, we must fail
        loudly — silent migrations of Attio attr types would corrupt
        existing data."""
        attio = MagicMock()

        def _request(method, path, json=None, **kwargs):
            if method == "GET" and path == f"/objects/{mig.OBJECT_SLUG}":
                return {"data": {"api_slug": mig.OBJECT_SLUG}}
            if path.endswith(f"/attributes/{mig.ATTRIBUTES[0][0]}"):
                # First attribute exists with wrong type.
                return {"data": {"api_slug": mig.ATTRIBUTES[0][0], "type": "text"}}
            # All other attributes don't exist (so creation succeeds).
            if method == "GET" and "/attributes/" in path:
                raise _make_http_404(path)
            if method == "POST" and path.endswith("/attributes"):
                return {"data": {}}
            return {"data": {}}

        attio._request.side_effect = _request
        monkeypatch.setattr(mig, "AttioClient", lambda: attio)
        rc = mig.main([])
        # Type mismatch on one attr → rc=1 (rows_failed > 0).
        assert rc == 1
        line = json.loads(mig._audit_path().read_text().strip())
        assert line["rows_failed"] >= 1


class TestBootstrapAuditLine:
    def test_audit_line_has_required_idempotency_fields(self, audit_tmp, attio_first_run):
        mig.main([])
        line = json.loads(mig._audit_path().read_text().strip())
        for field in (
            "run_id", "script_name", "script_version", "started", "completed",
            "dry_run", "rows_examined", "rows_modified",
            "rows_skipped_idempotent", "rows_failed", "actions",
        ):
            assert field in line, f"missing audit field: {field}"

    def test_audit_line_declares_operator_manual_rollback(self, audit_tmp, attio_first_run):
        mig.main([])
        line = json.loads(mig._audit_path().read_text().strip())
        # Per plan §4 F-PR-3 / Round-4 D28: object deletion is manual UI.
        assert line["rollback_strategy"] == "operator_manual"
