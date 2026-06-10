"""Tests for scripts/migrate_state_files_to_attio_daily_run.py (F-PR-8).

The migration script now delegates schema mutation to the shared
``scripts/_attio_migration_helpers`` module. These tests cover the
F-PR-8-specific concerns: daily_limits backfill paths, recheck-cache
archival, and the schema-summary shape that the script's main()
logs. Full helper coverage lives in
``tests/test_attio_migration_helpers.py``.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest

from scripts._attio_migration_helpers import build_attribute_body
from scripts.migrate_state_files_to_attio_daily_run import (
    DAILY_RUN_ATTRIBUTES,
    _archive_recheck_cache,
    _backfill_daily_limits,
    _ensure_schema,
)


def _response(status: int = 200, body: dict | None = None) -> httpx.Response:
    req = httpx.Request("POST", "https://api.attio.com/v2/x")
    return httpx.Response(
        status,
        request=req,
        json=body or {"data": {"id": {"object_id": "obj_x", "record_id": "rec_x"}}},
    )


def _http_error(status: int, body: str = "") -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://api.attio.com/v2/x")
    resp = httpx.Response(status, request=req, content=body.encode())
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    raise AssertionError("unreachable")  # pragma: no cover


@pytest.fixture
def mock_attio():
    client = MagicMock()
    client._client = MagicMock()
    client._client.request.return_value = _response()
    return client


@pytest.fixture
def isolated_state_dir(tmp_path, monkeypatch):
    """Redirect both state-file paths to a tmp dir so tests don't touch
    the real ~/.outbound-agent/."""
    daily_limits = tmp_path / "daily_limits.json"
    recheck = tmp_path / "recheck_cache.json"
    monkeypatch.setattr(
        "scripts.migrate_state_files_to_attio_daily_run.DAILY_LIMITS_PATH",
        daily_limits,
    )
    monkeypatch.setattr(
        "scripts.migrate_state_files_to_attio_daily_run.RECHECK_CACHE_PATH",
        recheck,
    )
    return tmp_path


# ── is_unique contract — uniqueness_key gets server-side dedup ───────────────


def test_daily_run_manifest_marks_only_uniqueness_key_unique():
    """Regression guard: exactly one attr in DAILY_RUN_ATTRIBUTES has
    is_unique=True, and it's `uniqueness_key`. Without this constraint,
    duplicate `{date}|{machine_id}` rows can land in Attio when the
    client-side dedup races. F-PR-8 §3.5 contract.
    """
    unique_attrs = [a[0] for a in DAILY_RUN_ATTRIBUTES if a[4]]
    assert unique_attrs == ["uniqueness_key"], (
        f"expected only uniqueness_key to be is_unique=True; got {unique_attrs}"
    )


def test_build_attribute_body_preserves_is_unique_when_set():
    """The helper must propagate is_unique=True into the POST body so the
    daily_run manifest's uniqueness_key flag actually reaches Attio."""
    body = build_attribute_body("uniqueness_key", "text", is_unique=True)
    assert body["data"]["is_unique"] is True


def test_build_attribute_body_defaults_is_unique_false():
    """Non-unique attrs (the majority) default to is_unique=False — Attio
    would reject duplicates if we set this too broadly."""
    body = build_attribute_body("run_date", "date")
    assert body["data"]["is_unique"] is False


# ── _backfill_daily_limits paths ─────────────────────────────────────────────


def test_backfill_no_source_file_short_circuits(mock_attio, isolated_state_dir):
    """No daily_limits.json on disk → return immediately, no Attio writes."""
    writer = MagicMock()
    summary = _backfill_daily_limits(mock_attio, writer)
    assert summary["daily_limits_action"] == "no_source_file"
    assert summary["daily_limits_archived"] is False
    assert not mock_attio._client.request.called


def test_backfill_stale_date_does_not_archive(mock_attio, isolated_state_dir):
    """A file from a previous day is left in place so the pre-cutover
    safety_limits.py keeps its reset-on-new-day behavior."""
    from scripts.migrate_state_files_to_attio_daily_run import DAILY_LIMITS_PATH as p

    p.write_text(
        json.dumps(
            {"date": "2026-01-01", "connections": 5, "messages": 3, "visits": 0}
        )
    )
    writer = MagicMock()
    summary = _backfill_daily_limits(mock_attio, writer)
    assert summary["daily_limits_action"] == "stale_date_not_backfilled"
    assert summary["daily_limits_archived"] is False
    assert p.exists()  # NOT archived


def test_backfill_today_zero_counters_does_not_archive(mock_attio, isolated_state_dir):
    """An all-zero file gets `today_but_zero_counters` and stays in
    place — same reasoning as stale-date."""
    from datetime import date

    from scripts.migrate_state_files_to_attio_daily_run import DAILY_LIMITS_PATH as p

    p.write_text(
        json.dumps(
            {
                "date": date.today().isoformat(),
                "connections": 0,
                "messages": 0,
                "visits": 0,
            }
        )
    )
    writer = MagicMock()
    summary = _backfill_daily_limits(mock_attio, writer)
    assert summary["daily_limits_action"] == "today_but_zero_counters"
    assert summary["daily_limits_archived"] is False
    assert p.exists()


def test_backfill_today_non_zero_creates_row_and_archives(mock_attio, isolated_state_dir):
    """Happy path: today's file has non-zero counters AND no existing
    daily_run row → POST creates the row, file is archived."""
    from datetime import date

    from scripts.migrate_state_files_to_attio_daily_run import DAILY_LIMITS_PATH as p

    p.write_text(
        json.dumps(
            {
                "date": date.today().isoformat(),
                "connections": 5,
                "messages": 3,
                "visits": 0,
            }
        )
    )
    # First call: existing-row query returns empty; second: create POST.
    mock_attio._client.request.side_effect = [
        _response(body={"data": []}),
        _response(body={"data": {"id": {"record_id": "rec_backfilled"}}}),
    ]
    writer = MagicMock()
    summary = _backfill_daily_limits(mock_attio, writer)
    assert summary["daily_limits_action"] == "backfilled"
    assert summary["daily_limits_archived"] is True
    assert not p.exists()
    archived = list(isolated_state_dir.glob("daily_limits.json.f-pr-8-archived.*"))
    assert len(archived) == 1


def test_backfill_corrupt_json_quarantines_without_crash(mock_attio, isolated_state_dir):
    """A corrupt JSON file is moved to .corrupt.<timestamp> so the next
    migration run sees no source file, instead of crash-looping on the
    same input."""
    from scripts.migrate_state_files_to_attio_daily_run import DAILY_LIMITS_PATH as p

    p.write_text("{ not valid json }")
    writer = MagicMock()
    summary = _backfill_daily_limits(mock_attio, writer)
    assert "quarantined_corrupt" in summary["daily_limits_action"]
    assert summary["daily_limits_archived"] is False
    assert not p.exists()
    quarantined = list(isolated_state_dir.glob("daily_limits.json.f-pr-8-corrupt.*"))
    assert len(quarantined) == 1


def test_backfill_non_int_counters_also_quarantines(mock_attio, isolated_state_dir):
    """ValueError from int() on a string counter must not crash the
    migration — same quarantine path as JSONDecodeError."""
    from datetime import date

    from scripts.migrate_state_files_to_attio_daily_run import DAILY_LIMITS_PATH as p

    p.write_text(
        json.dumps(
            {"date": date.today().isoformat(), "connections": "twelve", "messages": 0, "visits": 0}
        )
    )
    writer = MagicMock()
    summary = _backfill_daily_limits(mock_attio, writer)
    assert "quarantined_corrupt" in summary["daily_limits_action"]
    assert not p.exists()
    quarantined = list(isolated_state_dir.glob("daily_limits.json.f-pr-8-corrupt.*"))
    assert len(quarantined) == 1


def test_backfill_existing_row_already_today_archives(mock_attio, isolated_state_dir):
    """If a daily_run row for today already exists (e.g. re-running
    after a previous successful backfill), we archive the file
    without re-creating — idempotent second run."""
    from datetime import date

    from scripts.migrate_state_files_to_attio_daily_run import DAILY_LIMITS_PATH as p

    p.write_text(
        json.dumps(
            {
                "date": date.today().isoformat(),
                "connections": 5,
                "messages": 3,
                "visits": 0,
            }
        )
    )
    # Existing-row query returns a hit.
    mock_attio._client.request.return_value = _response(
        body={"data": [{"id": {"record_id": "rec_existing"}}]}
    )
    writer = MagicMock()
    summary = _backfill_daily_limits(mock_attio, writer)
    assert summary["daily_limits_action"] == "daily_run_already_exists_for_today"
    assert summary["daily_limits_archived"] is True
    # Only the query happened; no POST create.
    assert mock_attio._client.request.call_count == 1


# ── _archive_recheck_cache ───────────────────────────────────────────────────


def test_archive_recheck_cache_moves_file_when_present(isolated_state_dir):
    from scripts.migrate_state_files_to_attio_daily_run import RECHECK_CACHE_PATH as p

    p.write_text('{"some-url": {"checked_at": "2026-05-21", "degree": "first"}}')
    summary = _archive_recheck_cache()
    assert summary["recheck_cache_archived"] is True
    assert not p.exists()
    archived = list(isolated_state_dir.glob("recheck_cache.json.f-pr-8-archived.*"))
    assert len(archived) == 1


def test_archive_recheck_cache_no_op_when_absent(isolated_state_dir):
    summary = _archive_recheck_cache()
    assert summary["recheck_cache_archived"] is False


# ── _ensure_schema integration ───────────────────────────────────────────────


def test_ensure_schema_idempotent_second_run(monkeypatch):
    """Second invocation after objects + attrs exist returns all-skipped
    — no created counters increment. The new helpers use GET-first
    (returns 200 when present) rather than POST-then-409-swallow, so
    every call resolves to the 'skipped' path."""
    monkeypatch.setenv("ATTIO_LIST_ID", "list-uuid")
    attio = MagicMock()

    def _request(method, path, **_):
        if method == "GET":
            # Object + attributes all exist with matching types. The type
            # field is read from the GET response on attribute reads only;
            # /objects/daily_run GET returns the object shape (no `type`).
            if path == "/objects/daily_run":
                return {"data": {"id": {"object_id": "x"}}}
            # Each attr GET — return matching MAPPED type. Helper applies
            # TYPE_MAP on the comparison side so e.g. datetime ↔ timestamp
            # is a match.
            type_by_slug = {
                "/objects/daily_run/attributes/run_id": "text",
                "/objects/daily_run/attributes/run_date": "date",
                "/objects/daily_run/attributes/machine_id": "text",
                "/objects/daily_run/attributes/uniqueness_key": "text",
                "/objects/daily_run/attributes/hostname": "text",
                "/objects/daily_run/attributes/started_at": "timestamp",
                "/objects/daily_run/attributes/completed_at": "timestamp",
                "/objects/daily_run/attributes/status": "select",
                "/objects/daily_run/attributes/process_id": "number",
                "/objects/daily_run/attributes/connections_sent": "number",
                "/objects/daily_run/attributes/messages_sent": "number",
                "/objects/daily_run/attributes/visits_sent": "number",
                "/objects/daily_run/attributes/failure_details": "text",
                "/lists/list-uuid/attributes/last_observed_degree": "select",
                "/lists/list-uuid/attributes/last_observed_at": "date",
            }
            if path in type_by_slug:
                return {"data": {"type": type_by_slug[path]}}
            # status + last_observed_degree options endpoints — return
            # all expected options present so reconcile_select_options
            # returns "skipped".
            options_by_path = {
                "/objects/daily_run/attributes/status/options": [
                    {"title": s} for s in
                    ["running", "completed", "failed", "aborted"]
                ],
                "/lists/list-uuid/attributes/last_observed_degree/options": [
                    {"title": s} for s in
                    ["first", "second", "third", "unknown"]
                ],
            }
            if path in options_by_path:
                return {"data": options_by_path[path]}
        raise AssertionError(f"unexpected {method} {path}")

    attio._request.side_effect = _request

    from scripts.migrate_state_files_to_attio_daily_run import (
        DAILY_RUN_ATTRIBUTES,
        LINKEDIN_OUTREACH_RECHECK_ATTRIBUTES,
    )

    summary = _ensure_schema(attio)
    assert summary["daily_run_object_created"] is False
    assert summary["daily_run_object_action"] == "skipped"
    assert summary["daily_run_attrs_created"] == 0
    assert summary["daily_run_attrs_skipped"] == len(DAILY_RUN_ATTRIBUTES)
    assert summary["linkedin_recheck_attrs_created"] == 0
    assert summary["linkedin_recheck_attrs_skipped"] == len(
        LINKEDIN_OUTREACH_RECHECK_ATTRIBUTES
    )


def test_ensure_schema_skips_list_attrs_when_list_id_unset(monkeypatch):
    """Without ATTIO_LIST_ID, last_observed_* attrs are skipped with a
    warning rather than failing."""
    monkeypatch.delenv("ATTIO_LIST_ID", raising=False)
    attio = MagicMock()

    def _request(method, path, **_):
        if method == "GET":
            if path == "/objects/daily_run":
                return {"data": {"id": {"object_id": "x"}}}
            # All daily_run attrs missing → POST creates them.
            req = httpx.Request("GET", "https://x/")
            resp = httpx.Response(404, request=req)
            raise httpx.HTTPStatusError("404", request=req, response=resp)
        # POST always succeeds.
        return {"data": {"id": {"object_id": "x"}}}

    attio._request.side_effect = _request
    summary = _ensure_schema(attio)
    assert summary.get("linkedin_recheck_attrs_skipped_no_list_id") is True
    assert summary["linkedin_recheck_attrs_created"] == 0
