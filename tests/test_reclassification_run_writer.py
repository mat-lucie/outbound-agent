"""Tests for workflows/reclassification_run_writer.py (F-PR-3.7)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from workflows.reclassification_run_writer import ReclassificationRunWriter


@pytest.fixture
def mock_attio():
    client = MagicMock()

    def _request(method, path, json=None, **kwargs):
        if method == "POST" and path.endswith("/objects/reclassification_run/records"):
            return {
                "data": {
                    "id": {"record_id": "rec_reclassification_run_xyz"},
                    "values": {},
                }
            }
        if method == "PATCH":
            return {"data": {}}
        return {"data": {}}

    client._request.side_effect = _request
    return client


def _reclassification_create_body(attio):
    """Filter past back-pointer PATCHes to find the create-row POST."""
    for call in attio._request.call_args_list:
        if call[0][:2] == ("POST", "/objects/reclassification_run/records"):
            return call[1]["json"]["data"]["values"]
    raise AssertionError("no Reclassification Run create call recorded")


class TestLifecycle:
    def test_writes_one_reclassification_run_row(self, mock_attio):
        with ReclassificationRunWriter(
            classifier_module="workflows.industry_classifier",
            classifier_version="abc123",
            model_used="claude-opus-4-6",
            input_attr="company_name",
            output_attr="industry_vertical_status",
            confidence_threshold=0.8,
            attio=mock_attio,
        ) as run:
            run.examine()
            run.mark_success(record_id="rec_a", value="manufacturing")
        creates = [
            c for c in mock_attio._request.call_args_list
            if c[0][:2] == ("POST", "/objects/reclassification_run/records")
        ]
        assert len(creates) == 1
        body = creates[0][1]["json"]["data"]["values"]
        assert body["classifier_module"] == "workflows.industry_classifier"
        assert body["model_used"] == "claude-opus-4-6"
        assert body["input_attr"] == "company_name"
        assert body["output_attr"] == "industry_vertical_status"
        assert body["confidence_threshold"] == 0.8
        assert body["batch_size"] == 1
        assert body["success_count"] == 1

    def test_dry_run_does_not_write(self, mock_attio):
        with ReclassificationRunWriter(
            classifier_module="x",
            model_used="x",
            input_attr="x",
            output_attr="x",
            dry_run=True,
            attio=mock_attio,
        ) as run:
            run.examine()
            run.mark_success(record_id="rec_a")
        mock_attio._request.assert_not_called()


class TestCounters:
    def test_success_abstain_error_split(self, mock_attio):
        with ReclassificationRunWriter(
            classifier_module="x", model_used="x",
            input_attr="x", output_attr="x", attio=mock_attio,
        ) as run:
            for _ in range(10):
                run.examine()
            run.mark_success(record_id="a")
            run.mark_success(record_id="b")
            run.mark_success(record_id="c")
            run.mark_abstain(record_id="d", reason="below_threshold")
            run.mark_abstain(record_id="e", reason="below_threshold")
            run.mark_error(record_id="f", error=RuntimeError("llm timeout"))
        body = _reclassification_create_body(mock_attio)
        assert body["batch_size"] == 10
        assert body["success_count"] == 3
        assert body["abstain_count"] == 2
        assert body["error_count"] == 1

        details = json.loads(body["failure_details_pointer"])
        assert {a["record_id"] for a in details["abstains"]} == {"d", "e"}
        assert {e["record_id"] for e in details["errors"]} == {"f"}

    def test_cost_accumulates(self, mock_attio):
        with ReclassificationRunWriter(
            classifier_module="x", model_used="x",
            input_attr="x", output_attr="x", attio=mock_attio,
        ) as run:
            run.add_cost(0.0042)
            run.add_cost(0.0058)
        body = _reclassification_create_body(mock_attio)
        assert body["cost_usd_actual"] == pytest.approx(0.01, abs=1e-6)


class TestBackPointerStamping:
    """§3.12 contract: every reclassified record gets last_classified_by →
    Reclassification Run. Same shape as MigrationRunWriter."""

    def _patches(self, attio):
        return [
            c for c in attio._request.call_args_list
            if c[0][0] == "PATCH"
        ]

    def test_back_pointer_patch_fires_per_success(self, mock_attio):
        with ReclassificationRunWriter(
            classifier_module="x", model_used="x",
            input_attr="x", output_attr="x", attio=mock_attio,
        ) as run:
            run.examine()
            run.examine()
            run.mark_success(record_id="rec_a", value="manufacturing")
            run.mark_success(record_id="rec_b", value="logistics")
            run.mark_abstain(record_id="rec_c", reason="below_threshold")
        # 2 successes → 2 PATCHes. The abstain does NOT trigger one.
        patches = self._patches(mock_attio)
        assert len(patches) == 2
        for call in patches:
            body = call[1]["json"]["data"]["values"]
            assert body["last_classified_by"][0]["target_object"] == \
                "reclassification_run"
            assert body["last_classified_by"][0]["target_record_id"] == \
                "rec_reclassification_run_xyz"

    def test_abstains_dont_trigger_back_pointer(self, mock_attio):
        """Abstain means classifier didn't have enough confidence —
        the record should NOT carry last_classified_by since no
        verdict was applied."""
        with ReclassificationRunWriter(
            classifier_module="x", model_used="x",
            input_attr="x", output_attr="x", attio=mock_attio,
        ) as run:
            run.examine()
            run.mark_abstain(record_id="rec_a", reason="below_threshold")
        assert self._patches(mock_attio) == []

    def test_back_pointer_failure_recorded(self, mock_attio):
        original = mock_attio._request.side_effect

        def fail_patch(method, path, json=None, **kwargs):
            if method == "PATCH":
                raise ConnectionError("attio down")
            return original(method, path, json=json, **kwargs)

        mock_attio._request.side_effect = fail_patch
        with ReclassificationRunWriter(
            classifier_module="x", model_used="x",
            input_attr="x", output_attr="x", attio=mock_attio,
        ) as run:
            run.examine()
            run.mark_success(record_id="rec_a")
        assert len(run._back_pointer_failures) == 1
        assert run._back_pointer_failures[0]["record_id"] == "rec_a"


class TestExceptionHandling:
    def test_uncaught_exception_recorded(self, mock_attio):
        with pytest.raises(RuntimeError, match="boom"), ReclassificationRunWriter(
            classifier_module="x", model_used="x",
            input_attr="x", output_attr="x", attio=mock_attio,
        ) as run:
            run.examine()
            raise RuntimeError("boom")
        body = _reclassification_create_body(mock_attio)
        assert body["error_count"] >= 1
        details = json.loads(body["failure_details_pointer"])
        assert any(e.get("exc_type") == "RuntimeError" for e in details["errors"])
