"""Tests for the auto_finalize_borderline_batch idempotency contract."""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock

import pytest

from workflows.auto_finalize import (
    DEFAULT_EXPIRE,
    FinalizeRunFailed,
    auto_finalize_borderline_batch,
)


@pytest.fixture
def mock_attio():
    client = MagicMock()
    state: dict = {
        "operator_decision_rows": [],
        "finalize_rows": [],
        "posts": [],
    }
    client.state = state  # type: ignore[attr-defined]

    def _request(method: str, path: str, json: dict | None = None, **_):
        body = json or {}

        if "operator_review_queue/records/query" in path:
            filter_ = body.get("filter", {})
            uniq = filter_.get("uniqueness_key", {}).get("$eq")
            if uniq:
                hit = [
                    r for r in state["finalize_rows"]
                    if r.get("uniqueness_key") == uniq
                ]
                return {"data": hit[:1]}
            # Operator-decision lookup requires BOTH type and status
            # filters in real production. Mirror that here so tests
            # exercise the full filter contract.
            type_filter = filter_.get("type", {}).get("$eq")
            status_filter = filter_.get("status", {}).get("$eq")
            if (
                type_filter == "weekly_finalize_stale"
                and status_filter == "resolved"
            ):
                return {"data": state["operator_decision_rows"]}
            return {"data": []}

        if path.endswith("/records") and method == "POST":
            state["posts"].append({"path": path, "body": body})
            values = body.get("data", {}).get("values", {})
            row = {
                "id": {"record_id": f"rec-{len(state['posts'])}"},
                "values": values,
                "uniqueness_key": values.get("uniqueness_key", ""),
            }
            state["finalize_rows"].append(row)
            return {"data": row}

        return {"data": {}}

    client._request.side_effect = _request
    return client


def _resolved_row(*, decision_run_id: str, batch_iso: str) -> dict:
    """Build a queue row matching the real escalate-resolve shape:
    decision_json carries the operator's payload as a JSON string.
    """
    return {
        "id": {"record_id": "rec-operator-1"},
        "values": {
            "type": [{"value": "weekly_finalize_stale"}],
            "status": [{"value": "resolved"}],
            "idempotency_key": [{"value": f"finalize_{batch_iso}"}],
            "decision_json": [{"value": json.dumps({
                "decision_run_id": decision_run_id,
                "operator": "operator@example.com",
            })}],
        },
    }


def _noop_finalize(batch_iso: str, decision_run_id: str) -> dict:
    return {"exit_code": 0, "batch": batch_iso, "decision_run_id": decision_run_id}


def _failing_finalize(batch_iso: str, decision_run_id: str) -> dict:
    return {
        "exit_code": 1,
        "batch": batch_iso,
        "decision_run_id": decision_run_id,
    }


class TestDefaultExpirePath:
    def test_no_operator_decision_uses_default_expire(self, mock_attio):
        out = auto_finalize_borderline_batch(
            mock_attio,
            batch_date=date(2026, 5, 22),
            finalize_fn=_noop_finalize,
        )
        assert out["decision_run_id"] == DEFAULT_EXPIRE
        assert out["idempotency_key"] == f"2026-05-22_{DEFAULT_EXPIRE}"
        assert out["action"] == "finalized"

    def test_same_day_re_run_is_no_op(self, mock_attio):
        # First call lands a finalize row.
        first = auto_finalize_borderline_batch(
            mock_attio,
            batch_date=date(2026, 5, 22),
            finalize_fn=_noop_finalize,
        )
        assert first["action"] == "finalized"
        # Second call sees the finalize_rows state and short-circuits.
        second = auto_finalize_borderline_batch(
            mock_attio,
            batch_date=date(2026, 5, 22),
            finalize_fn=_noop_finalize,
        )
        assert second["action"] == "skipped_idempotent"
        assert second["finalize_result"] is None


class TestOperatorRespondedPath:
    def test_operator_decision_key_used_when_resolved_row_exists(self, mock_attio):
        mock_attio.state["operator_decision_rows"] = [
            _resolved_row(decision_run_id="op-run-abc", batch_iso="2026-05-22"),
        ]
        out = auto_finalize_borderline_batch(
            mock_attio,
            batch_date=date(2026, 5, 22),
            finalize_fn=_noop_finalize,
        )
        assert out["decision_run_id"] == "op-run-abc"
        assert out["idempotency_key"] == "2026-05-22_op-run-abc"

    def test_operator_and_default_can_both_land_per_day(self, mock_attio):
        """At most one finalize per (batch, decision_run_id) — but
        different decision_run_id values produce different keys, so
        an operator-decision finalize AND a default-expire finalize
        can BOTH land for the same batch_date if they happened in
        different runs. The collision is per-key, not per-batch.
        """
        # First call: no operator decision yet → default_expire key.
        first = auto_finalize_borderline_batch(
            mock_attio,
            batch_date=date(2026, 5, 22),
            finalize_fn=_noop_finalize,
        )
        assert first["decision_run_id"] == DEFAULT_EXPIRE

        # Now the operator responds (between runs). Inject a resolved row.
        mock_attio.state["operator_decision_rows"] = [
            _resolved_row(decision_run_id="op-run-late", batch_iso="2026-05-22"),
        ]
        second = auto_finalize_borderline_batch(
            mock_attio,
            batch_date=date(2026, 5, 22),
            finalize_fn=_noop_finalize,
        )
        # Different key → does NOT collide with the default-expire row.
        assert second["decision_run_id"] == "op-run-late"
        assert second["action"] == "finalized"

    def test_operator_decision_supplied_directly_overrides_lookup(self, mock_attio):
        """Caller may pass `operator_decision_run_id` to skip the
        lookup (used by tests + by callers that already have the id)."""
        out = auto_finalize_borderline_batch(
            mock_attio,
            batch_date=date(2026, 5, 22),
            finalize_fn=_noop_finalize,
            operator_decision_run_id="explicit-id",
        )
        assert out["decision_run_id"] == "explicit-id"
        assert out["idempotency_key"] == "2026-05-22_explicit-id"


class TestFinalizeFnCalledExactlyOnce:
    def test_finalize_invoked_only_on_first_run(self, mock_attio):
        call_count = {"n": 0}
        def _counting_finalize(batch_iso: str, decision_run_id: str) -> dict:
            call_count["n"] += 1
            return {"batch": batch_iso}
        auto_finalize_borderline_batch(
            mock_attio,
            batch_date=date(2026, 5, 22),
            finalize_fn=_counting_finalize,
        )
        auto_finalize_borderline_batch(
            mock_attio,
            batch_date=date(2026, 5, 22),
            finalize_fn=_counting_finalize,
        )
        assert call_count["n"] == 1


class TestErrorPropagation:
    def test_attio_lookup_failure_propagates(self, mock_attio):
        """If Attio is unreachable during the operator-decision lookup,
        the error MUST NOT be swallowed as 'no decision → default_expire'.
        The error bubbles."""
        def _broken_request(*_a, **_k):
            raise RuntimeError("attio down")
        mock_attio._request.side_effect = _broken_request
        with pytest.raises(RuntimeError, match="attio down"):
            auto_finalize_borderline_batch(
                mock_attio,
                batch_date=date(2026, 5, 22),
                finalize_fn=_noop_finalize,
            )


class TestFinalizeRunFailed:
    """finalize_fn returning a non-zero exit_code MUST raise
    FinalizeRunFailed BEFORE the completion row lands — otherwise a
    failed finalize poisons the idempotency key and blocks retry."""

    def test_nonzero_exit_code_raises(self, mock_attio):
        with pytest.raises(FinalizeRunFailed) as exc:
            auto_finalize_borderline_batch(
                mock_attio,
                batch_date=date(2026, 5, 22),
                finalize_fn=_failing_finalize,
            )
        assert exc.value.payload["exit_code"] == 1

    def test_no_completion_row_written_on_failure(self, mock_attio):
        with pytest.raises(FinalizeRunFailed):
            auto_finalize_borderline_batch(
                mock_attio,
                batch_date=date(2026, 5, 22),
                finalize_fn=_failing_finalize,
            )
        assert mock_attio.state["posts"] == [], (
            "FinalizeRunFailed must prevent the completion-row escalate()"
        )

    def test_retry_after_failure_can_proceed(self, mock_attio):
        # 1st run fails — no key written.
        with pytest.raises(FinalizeRunFailed):
            auto_finalize_borderline_batch(
                mock_attio,
                batch_date=date(2026, 5, 22),
                finalize_fn=_failing_finalize,
            )
        # 2nd run with a fixed finalize_fn lands successfully.
        out = auto_finalize_borderline_batch(
            mock_attio,
            batch_date=date(2026, 5, 22),
            finalize_fn=_noop_finalize,
        )
        assert out["action"] == "finalized"


class TestDecisionJsonMalformed:
    """The operator-resolved row carries `decision_json` as a JSON
    string. Malformed payloads must surface as a typed error rather
    than silently defaulting to default_expire."""

    def test_invalid_json_raises(self, mock_attio):
        mock_attio.state["operator_decision_rows"] = [{
            "id": {"record_id": "r1"},
            "values": {
                "decision_json": [{"value": "not json"}],
            },
        }]
        with pytest.raises(ValueError, match="not valid JSON"):
            auto_finalize_borderline_batch(
                mock_attio,
                batch_date=date(2026, 5, 22),
                finalize_fn=_noop_finalize,
            )

    def test_payload_not_object_raises(self, mock_attio):
        mock_attio.state["operator_decision_rows"] = [{
            "id": {"record_id": "r1"},
            "values": {
                "decision_json": [{"value": json.dumps(["not", "object"])}],
            },
        }]
        with pytest.raises(ValueError, match="must be an object"):
            auto_finalize_borderline_batch(
                mock_attio,
                batch_date=date(2026, 5, 22),
                finalize_fn=_noop_finalize,
            )

    def test_missing_decision_run_id_falls_to_default_expire(self, mock_attio):
        """A resolved row whose decision_json omits decision_run_id is
        a legitimate "operator resolved but didn't pick a run" — fall
        back to default_expire."""
        mock_attio.state["operator_decision_rows"] = [{
            "id": {"record_id": "r1"},
            "values": {
                "decision_json": [{"value": json.dumps({"operator": "mat"})}],
            },
        }]
        out = auto_finalize_borderline_batch(
            mock_attio,
            batch_date=date(2026, 5, 22),
            finalize_fn=_noop_finalize,
        )
        assert out["decision_run_id"] == DEFAULT_EXPIRE


class TestEmptyOperatorDecisionRunId:
    def test_empty_string_arg_falls_to_lookup(self, mock_attio):
        """An empty-string arg must NOT be treated as a supplied value
        that bypasses the lookup — empty is "not supplied"."""
        mock_attio.state["operator_decision_rows"] = [
            _resolved_row(decision_run_id="from-lookup", batch_iso="2026-05-22"),
        ]
        out = auto_finalize_borderline_batch(
            mock_attio,
            batch_date=date(2026, 5, 22),
            finalize_fn=_noop_finalize,
            operator_decision_run_id="",
        )
        assert out["decision_run_id"] == "from-lookup"


class TestResponseShape:
    def test_missing_data_key_raises(self, mock_attio):
        def _bad_shape(*_a, **_k):
            return {"unexpected": "shape"}
        mock_attio._request.side_effect = _bad_shape
        with pytest.raises(RuntimeError, match="missing 'data' key"):
            auto_finalize_borderline_batch(
                mock_attio,
                batch_date=date(2026, 5, 22),
                finalize_fn=_noop_finalize,
            )


class TestNonDictResultRaises:
    def test_non_dict_finalize_result_raises_typeerror(self, mock_attio):
        with pytest.raises(TypeError, match="must return dict"):
            auto_finalize_borderline_batch(
                mock_attio,
                batch_date=date(2026, 5, 22),
                finalize_fn=lambda _b, _r: None,  # type: ignore[return-value,arg-type]
            )
