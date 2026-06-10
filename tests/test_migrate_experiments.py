"""Tests for scripts/migrate_experiments_tsv_to_attio.py (F-PR-6).

Covers the §3.17 supersedence rule, state_history construction, and the
idempotency contract for the Experiment upsert path. Attio is mocked
via the same `_client.request` pattern as tests/test_attio_writer.py;
no network I/O.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest

from scripts.migrate_experiments_tsv_to_attio import (
    _apply_supersedence_rule,
    _build_state_history,
    _upsert_experiment,
)


def _row(
    experiment_id: str,
    *,
    status: str = "running",
    started: str = "2026-05-01",
    completed: str = "",
    cohort_size: str = "0",
    dm_response_rate: str = "0.0",
    baseline_rate: str = "0.0",
    variable: str = "messages.json:dm1:operations_leaders:es",
    description: str = "test row",
) -> dict:
    """Build a TSV-shaped row dict (DictReader returns str values)."""
    return {
        "experiment_id": experiment_id,
        "started": started,
        "completed": completed,
        "cohort_size": cohort_size,
        "dm_response_rate": dm_response_rate,
        "dm1_response_rate": "",
        "dm2_response_rate": "",
        "dm3_response_rate": "",
        "baseline_rate": baseline_rate,
        "status": status,
        "variable": variable,
        "description": description,
    }


# ── §3.17 supersedence rule ──────────────────────────────────────────────────


def test_supersedence_no_duplicates_passes_through_unchanged():
    rows = [
        _row("baseline-v0", status="won", completed="2026-04-10"),
        _row("exp-soften", status="running", started="2026-05-11"),
    ]
    winners, events = _apply_supersedence_rule(rows)
    assert [w["experiment_id"] for w in winners] == ["baseline-v0", "exp-soften"]
    assert events == []


def test_supersedence_winner_is_row_with_completion_date():
    """§3.17: rows 3+4 of the live TSV share an experiment_id; the row
    with non-null `completed` wins, the other becomes a supersedence event."""
    rows = [
        _row(
            "exp-20260505-ops-dm1-no-editorial",
            status="running",
            started="2026-05-05",
            completed="",
        ),
        _row(
            "exp-20260505-ops-dm1-no-editorial",
            status="rejected",
            started="2026-05-05",
            completed="2026-05-11",
        ),
    ]
    winners, events = _apply_supersedence_rule(rows)

    assert len(winners) == 1
    winner = winners[0]
    assert winner["status"] == "rejected"
    assert winner["completed"] == "2026-05-11"

    assert len(events) == 1
    evt = events[0]
    assert evt["experiment_id"] == "exp-20260505-ops-dm1-no-editorial"
    assert evt["status"] == "superseded"
    # Typed fields make the loser reconstructable without parsing prose.
    assert evt["loser_status"] == "running"
    assert evt["loser_started"] == "2026-05-05"
    assert evt["loser_completed"] == ""
    # Timestamp derived from loser's own data, not wall clock.
    assert evt["timestamp"] == "2026-05-05"
    assert "running" in evt["reason"]


def test_supersedence_event_count_matches_loser_count():
    """3 rows sharing one id → 1 winner + 2 supersedence events."""
    rows = [
        _row("exp-x", status="running", completed=""),
        _row("exp-x", status="rejected", completed="2026-05-10"),
        _row("exp-x", status="lost", completed="2026-05-15"),
    ]
    winners, events = _apply_supersedence_rule(rows)
    assert len(winners) == 1
    # Latest-by-file-order tiebreak among completed rows.
    assert winners[0]["status"] == "lost"
    assert len(events) == 2


def test_supersedence_all_running_falls_back_to_last_row():
    """No row has `completed` set → pick the last row, one supersedence
    event for the non-winning first row."""
    rows = [
        _row("exp-y", status="running", completed=""),
        _row("exp-y", status="running", completed=""),
    ]
    winners, events = _apply_supersedence_rule(rows)
    assert len(winners) == 1
    # One loser (the first row) → one event.
    assert len(events) == 1


# ── _build_state_history ──────────────────────────────────────────────────────


def test_state_history_includes_migrated_from_tsv_event():
    row = _row("exp-z", status="running", started="2026-05-01")
    history = _build_state_history(row, supersedence_events=[])
    lines = history.splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["timestamp"] == "2026-05-01"
    assert parsed["status"] == "running"
    assert "Migrated from experiments.tsv" in parsed["reason"]


def test_state_history_appends_superseded_events_for_matching_experiment_id():
    row = _row("exp-w", status="rejected", completed="2026-05-11")
    events = [
        {
            "experiment_id": "exp-w",
            "timestamp": "2026-05-05",
            "status": "superseded",
            "loser_status": "running",
            "loser_started": "2026-05-05",
            "loser_completed": "",
            "reason": "Migration §3.17 supersedence: row with status='running'...",
        },
        {
            # Different experiment_id — must NOT be attached.
            "experiment_id": "exp-other",
            "timestamp": "2026-05-05",
            "status": "superseded",
            "loser_status": "running",
            "loser_started": "",
            "loser_completed": "",
            "reason": "unrelated",
        },
    ]
    history = _build_state_history(row, events)
    lines = history.splitlines()
    assert len(lines) == 2
    assert "Migrated from experiments.tsv" in json.loads(lines[0])["reason"]
    superseded_event = json.loads(lines[1])
    assert superseded_event["status"] == "superseded"
    assert superseded_event["experiment_id"] == "exp-w"


def test_apply_supersedence_rule_is_deterministic():
    """Re-running on the same TSV input MUST produce byte-identical events.

    Idempotency contract guard: if `_apply_supersedence_rule` ever
    introduces a wall-clock timestamp again, the migration's "second
    run = zero writes" claim silently regresses. This test pins the
    determinism property at the rule layer so the regression is caught
    before it reaches Attio.
    """
    rows = [
        _row(
            "exp-20260505-ops-dm1-no-editorial",
            status="running",
            started="2026-05-05",
            completed="",
        ),
        _row(
            "exp-20260505-ops-dm1-no-editorial",
            status="rejected",
            started="2026-05-05",
            completed="2026-05-11",
        ),
    ]
    winners_a, events_a = _apply_supersedence_rule(rows)
    winners_b, events_b = _apply_supersedence_rule(rows)
    assert events_a == events_b
    assert winners_a == winners_b


def test_supersedence_multiple_completed_rows_picks_latest_file_order():
    """When several rows carry non-null `completed`, latest-by-file-order
    wins. The tiebreak intentionally ignores `completed` date arithmetic —
    file order is canonical because TSV is append-only and operator
    reordering should be a deliberate event."""
    rows = [
        # Later date but earlier in file → loses.
        _row("exp-multi", status="rejected", completed="2026-05-15"),
        # Earlier date but later in file → wins.
        _row("exp-multi", status="lost", completed="2026-05-10"),
    ]
    winners, events = _apply_supersedence_rule(rows)
    assert len(winners) == 1
    assert winners[0]["status"] == "lost"
    assert winners[0]["completed"] == "2026-05-10"
    assert len(events) == 1
    assert events[0]["loser_status"] == "rejected"


# ── Idempotency contract ──────────────────────────────────────────────────────


def _response(body: dict, status: int = 200, method: str = "POST", path: str = "/v2") -> httpx.Response:
    req = httpx.Request(method, f"https://api.attio.com{path}")
    return httpx.Response(status, request=req, json=body)


def _query_response_with_matching_history(history: str) -> httpx.Response:
    """Build an Attio query response for an existing row whose state_history
    already matches what the migration would write — the upsert should
    treat this as a no-op (`skipped_idempotent`)."""
    body = {
        "data": [
            {
                "id": {"record_id": "rec_existing"},
                "values": {
                    "state_history": [{"value": history}],
                },
            }
        ]
    }
    return _response(body, path="/objects/experiment/records/query")


def _query_response_empty() -> httpx.Response:
    return _response({"data": []}, path="/objects/experiment/records/query")


@pytest.fixture
def mock_attio():
    client = MagicMock()
    client._client = MagicMock()
    return client


def test_upsert_creates_when_no_existing_record(mock_attio):
    row = _row("exp-new", status="running", started="2026-05-01")
    history = _build_state_history(row, [])
    mock_attio._client.request.side_effect = [
        _query_response_empty(),  # the query for existing
        _response(
            {"data": {"id": {"record_id": "rec_new"}, "values": {}}},
            path="/objects/experiment/records",
        ),  # the create
    ]

    outcome, changed = _upsert_experiment(mock_attio, row, history)
    assert outcome == "created"
    assert changed is True
    # First call = query, second call = POST create.
    calls = mock_attio._client.request.call_args_list
    assert calls[0][0][0] == "POST"
    assert "/objects/experiment/records/query" in calls[0][0][1]
    assert calls[1][0][0] == "POST"
    assert calls[1][0][1] == "/objects/experiment/records"


def test_upsert_skipped_idempotent_when_history_matches(mock_attio):
    """Re-running the migration with the same TSV → Attio already holds
    the exact state_history we'd write → outcome must be skipped_idempotent
    AND no PATCH/POST call follows the query."""
    row = _row("exp-existing", status="running", started="2026-05-01")
    history = _build_state_history(row, [])

    mock_attio._client.request.side_effect = [
        _query_response_with_matching_history(history),
    ]

    outcome, changed = _upsert_experiment(mock_attio, row, history)
    assert outcome == "skipped_idempotent"
    assert changed is False
    # Only the query happened; no second call.
    assert mock_attio._client.request.call_count == 1


def test_upsert_patches_when_history_diverges(mock_attio):
    """Existing row with a stale state_history → migration must PATCH."""
    row = _row("exp-existing", status="rejected", completed="2026-05-11")
    new_history = _build_state_history(row, [])
    stale_history = "stale content"

    mock_attio._client.request.side_effect = [
        _query_response_with_matching_history(stale_history),
        _response(
            {"data": {"id": {"record_id": "rec_existing"}, "values": {}}},
            method="PATCH",
            path="/objects/experiment/records/rec_existing",
        ),
    ]

    outcome, changed = _upsert_experiment(mock_attio, row, new_history)
    assert outcome == "updated"
    assert changed is True
    calls = mock_attio._client.request.call_args_list
    assert calls[1][0][0] == "PATCH"
    assert "/records/rec_existing" in calls[1][0][1]


def test_upsert_full_5_row_idempotency_smoke(mock_attio):
    """End-to-end idempotency: 5 winner rows + matching Attio state →
    every call returns skipped_idempotent. Mirrors the "re-run is no-op"
    invariant from the migration script docstring."""
    rows = [
        _row("baseline-v0", status="won", started="2026-04-10", completed="2026-04-10"),
        _row("exp-a", status="running", started="2026-05-01"),
        _row("exp-b", status="rejected", started="2026-05-05", completed="2026-05-11"),
        _row("exp-c", status="running", started="2026-05-11"),
        _row("exp-d", status="won", started="2026-05-15", completed="2026-05-20"),
    ]
    histories = [_build_state_history(r, []) for r in rows]

    mock_attio._client.request.side_effect = [
        _query_response_with_matching_history(h) for h in histories
    ]

    outcomes = []
    for row, history in zip(rows, histories, strict=True):
        outcome, _changed = _upsert_experiment(mock_attio, row, history)
        outcomes.append(outcome)

    assert outcomes == ["skipped_idempotent"] * 5
    # Five queries, zero writes.
    assert mock_attio._client.request.call_count == 5


# ── _ensure_attribute 400/409 disambiguation ────────────────────────────────


def _http_error(status: int, body: str) -> httpx.HTTPStatusError:
    """Build an HTTPStatusError raised by raise_for_status on a non-2xx response."""
    req = httpx.Request("POST", "https://api.attio.com/v2/objects/experiment/attributes")
    resp = httpx.Response(status, request=req, content=body.encode())
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    raise AssertionError("did not raise")  # pragma: no cover


def test_ensure_attribute_swallows_409_unconditionally(mock_attio):
    """409 always means already-exists; no body inspection required."""
    from scripts.migrate_experiments_tsv_to_attio import _ensure_attribute

    exc = _http_error(409, '{"error": "conflict"}')
    mock_attio._client.request.side_effect = exc
    result = _ensure_attribute(mock_attio, "experiment", "slug", "text", None, True)
    assert result is False


def test_ensure_attribute_swallows_400_only_when_body_marks_duplicate(mock_attio):
    """400 must inspect response body — silently swallowing arbitrary 400s
    would mask malformed-request bugs as no-ops."""
    from scripts.migrate_experiments_tsv_to_attio import _ensure_attribute

    duplicate_body = '{"error": "duplicate slug experiment_id"}'
    exc = _http_error(400, duplicate_body)
    mock_attio._client.request.side_effect = exc
    result = _ensure_attribute(mock_attio, "experiment", "slug", "text", None, True)
    assert result is False


def test_ensure_attribute_raises_on_400_without_duplicate_marker(mock_attio):
    """A real 400 (bad request, malformed config) must propagate so the
    operator sees the schema bug instead of an "all attrs skipped" summary."""
    from scripts.migrate_experiments_tsv_to_attio import _ensure_attribute

    bad_request_body = '{"error": "invalid config: max field required for rating type"}'
    exc = _http_error(400, bad_request_body)
    mock_attio._client.request.side_effect = exc
    with pytest.raises(httpx.HTTPStatusError):
        _ensure_attribute(mock_attio, "experiment", "slug", "rating", None, True)
