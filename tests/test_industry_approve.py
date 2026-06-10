"""FU-2 tests for ``workflows.industry_approve``.

Mirrors ``tests/test_pr32_sales_approve.py`` (PR-32) but for the
``industry_low_confidence`` queue type.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from clients.crm.attio_provider import AttioProvider
from workflows.industry_approve import (
    AmbiguousCompanyError,
    CompanyNotFoundError,
    IndustryRowNotFoundError,
    InvalidVerticalError,
    approve_industry_classification,
    list_pending_industry_low_confidence,
    reject_industry_classification,
)

# ────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────


def _queue_row(
    company_name: str,
    suggested: str = "Food & Beverage",
    confidence: float = 0.55,
    record_id: str = "q-low-1",
    payload_serialized: bool = True,
) -> dict:
    """Shape of an ``operator_review_queue`` row carrying an
    ``industry_low_confidence`` payload as Attio returns it.

    The migration script defines ``payload_json`` as ``long_text``, so the
    payload arrives serialized. ``payload_serialized=False`` exercises the
    dict-passthrough branch in ``_parse_payload`` (for fixtures that mimic
    the in-memory test write path).
    """
    payload = {
        "company_name": company_name,
        "suggested_vertical": suggested,
        "confidence": confidence,
        "reasoning": "ambiguous web copy",
        "model_used": "claude-haiku-4-5-20251001",
    }
    payload_value = json.dumps(payload) if payload_serialized else payload
    return {
        "id": {"record_id": record_id},
        "values": {
            "type": [{"value": "industry_low_confidence"}],
            "status": [{"value": "open"}],
            "payload_json": [{"value": payload_value}],
        },
    }


def _company_record(record_id: str, name: str) -> dict:
    return {
        "id": {"record_id": record_id},
        "values": {"name": [{"value": name}]},
    }


def _provider_over(attio: MagicMock) -> AttioProvider:
    """Wrap a mock ``AttioClient`` in the real ``AttioProvider``.

    The pending-row read now flows through ``CRMProvider.query_object_records``;
    wrapping the same mock that backs ``search_companies`` + the writer keeps a
    single source of truth. ``query_object_records`` calls ``attio._request``
    and normalizes each row to a ``Record`` whose ``.raw`` reproduces the
    original row dict, so the parsed listing output is asserted through the
    real provider path.
    """
    return AttioProvider(attio_client=attio)


# ────────────────────────────────────────────────────────────────────────
# list_pending_industry_low_confidence
# ────────────────────────────────────────────────────────────────────────


class TestListPending:
    def test_returns_parsed_rows(self):
        attio = MagicMock()
        attio._request.return_value = {
            "data": [
                _queue_row("Sigma Alimentos", "Food & Beverage", 0.55, "q-1"),
                _queue_row("Cementra", "Construction", 0.62, "q-2"),
            ]
        }
        rows = list_pending_industry_low_confidence(_provider_over(attio))
        assert len(rows) == 2
        assert rows[0]["company_name"] == "Sigma Alimentos"
        assert rows[0]["queue_record_id"] == "q-1"
        assert rows[0]["suggested_vertical"] == "Food & Beverage"
        assert rows[0]["confidence"] == 0.55
        assert rows[1]["company_name"] == "Cementra"
        # Queries the operator_review_queue object with the Attio-native
        # pending filter passed through unchanged.
        method, path = attio._request.call_args[0][:2]
        assert method == "POST"
        assert path == "/objects/operator_review_queue/records/query"
        body = attio._request.call_args.kwargs["json"]
        assert body["filter"] == {
            "$and": [
                {"type": "industry_low_confidence"},
                {"status": {"$not": {"$in": ["resolved", "rejected"]}}},
            ],
        }
        assert body["limit"] == 100

    def test_returns_empty_when_no_pending(self):
        attio = MagicMock()
        attio._request.return_value = {"data": []}
        assert list_pending_industry_low_confidence(_provider_over(attio)) == []

    def test_dict_payload_passthrough(self):
        attio = MagicMock()
        attio._request.return_value = {
            "data": [_queue_row("Sigma", payload_serialized=False)]
        }
        rows = list_pending_industry_low_confidence(_provider_over(attio))
        assert rows[0]["company_name"] == "Sigma"


# ────────────────────────────────────────────────────────────────────────
# approve_industry_classification
# ────────────────────────────────────────────────────────────────────────


class TestApprove:
    def test_writes_company_then_resolves_queue(self):
        attio = MagicMock()
        attio._request.return_value = {
            "data": [_queue_row("Sigma Alimentos", record_id="q-99")]
        }
        attio.search_companies.return_value = [
            _company_record("co-42", "Sigma Alimentos"),
        ]
        writer = MagicMock()

        result = approve_industry_classification(
            "Sigma Alimentos",
            vertical="Food & Beverage",
            crm=_provider_over(attio),
            attio=attio,
            writer=writer,
        )

        assert result == {
            "queue_record_id": "q-99",
            "company_record_id": "co-42",
            "vertical": "Food & Beverage",
            "company_updated": True,
            "queue_resolved": True,
        }

        # Two WriteIntents: Companies first, then operator_review_queue.
        assert writer.apply.call_count == 2

        company_intent = writer.apply.call_args_list[0][0][0]
        assert company_intent.object == "companies"
        assert company_intent.record_id == "co-42"
        assert company_intent.updates["industry_vertical"] == "Food & Beverage"
        assert company_intent.updates["industry_vertical_status"] == "confirmed"
        assert company_intent.writer_module == "workflows.industry_approve"

        queue_intent = writer.apply.call_args_list[1][0][0]
        assert queue_intent.object == "operator_review_queue"
        assert queue_intent.record_id == "q-99"
        assert queue_intent.updates["status"] == "resolved"
        assert queue_intent.updates["decision_json"]["operator_choice"] == "Food & Beverage"
        assert queue_intent.updates["decision_json"]["suggested_vertical"] == "Food & Beverage"
        assert queue_intent.updates["decision_json"]["company_record_id"] == "co-42"
        assert queue_intent.updates["decision_json"]["resolved_by"] == "workflows.industry_approve"

    def test_invalid_vertical_raises_before_any_write(self):
        attio = MagicMock()
        writer = MagicMock()
        with pytest.raises(InvalidVerticalError):
            approve_industry_classification(
                "Sigma Alimentos",
                vertical="Not A Real Label",
                crm=_provider_over(attio),
                attio=attio,
                writer=writer,
            )
        # No queue lookup, no Attio calls — validation is the first gate.
        attio._request.assert_not_called()
        attio.search_companies.assert_not_called()
        writer.apply.assert_not_called()

    def test_no_pending_row_raises_before_company_write(self):
        attio = MagicMock()
        attio._request.return_value = {"data": []}
        writer = MagicMock()
        with pytest.raises(IndustryRowNotFoundError):
            approve_industry_classification(
                "Sigma Alimentos",
                vertical="Food & Beverage",
                crm=_provider_over(attio),
                attio=attio,
                writer=writer,
            )
        # Company lookup never ran, no writes happened.
        attio.search_companies.assert_not_called()
        writer.apply.assert_not_called()

    def test_company_not_found_raises_before_queue_write(self):
        attio = MagicMock()
        attio._request.return_value = {
            "data": [_queue_row("Sigma Alimentos")]
        }
        attio.search_companies.return_value = []
        writer = MagicMock()
        with pytest.raises(CompanyNotFoundError):
            approve_industry_classification(
                "Sigma Alimentos",
                vertical="Food & Beverage",
                crm=_provider_over(attio),
                attio=attio,
                writer=writer,
            )
        # Lookup failed before any write — queue row stays open.
        writer.apply.assert_not_called()

    def test_ambiguous_company_raises_before_queue_write(self):
        attio = MagicMock()
        attio._request.return_value = {
            "data": [_queue_row("Sigma Alimentos")]
        }
        attio.search_companies.return_value = [
            _company_record("co-1", "Sigma Alimentos"),
            _company_record("co-2", "Sigma Alimentos"),
        ]
        writer = MagicMock()
        with pytest.raises(AmbiguousCompanyError) as exc:
            approve_industry_classification(
                "Sigma Alimentos",
                vertical="Food & Beverage",
                crm=_provider_over(attio),
                attio=attio,
                writer=writer,
            )
        assert len(exc.value.candidates) == 2
        writer.apply.assert_not_called()

    def test_record_id_override_bypasses_lookup(self):
        attio = MagicMock()
        attio._request.return_value = {
            "data": [_queue_row("Sigma Alimentos", record_id="q-9")]
        }
        # Two matches would normally raise — override resolves it.
        attio.search_companies.return_value = [
            _company_record("co-1", "Sigma Alimentos"),
            _company_record("co-2", "Sigma Alimentos"),
        ]
        writer = MagicMock()
        result = approve_industry_classification(
            "Sigma Alimentos",
            vertical="Food & Beverage",
            crm=_provider_over(attio),
            attio=attio,
            record_id="co-1",
            writer=writer,
        )
        assert result["company_record_id"] == "co-1"
        # The override skips search_companies entirely.
        attio.search_companies.assert_not_called()

    def test_queue_write_failure_leaves_company_updated(self):
        """Partial-completion contract from the docstring: Companies write
        first, queue write second; if the queue write fails, the Companies
        update has already landed and re-running approve is idempotent on
        the Companies PATCH side. Future refactors that swap to a single
        transactional write (or reorder the calls) must update this test
        deliberately rather than silently breaking the documented recovery
        story."""
        attio = MagicMock()
        attio._request.return_value = {
            "data": [_queue_row("Sigma Alimentos", record_id="q-partial")]
        }
        attio.search_companies.return_value = [
            _company_record("co-42", "Sigma Alimentos"),
        ]
        writer = MagicMock()
        # First call (Companies) succeeds, second call (queue) raises.
        writer.apply.side_effect = [None, RuntimeError("attio 5xx on queue write")]

        with pytest.raises(RuntimeError, match="attio 5xx on queue write"):
            approve_industry_classification(
                "Sigma Alimentos",
                vertical="Food & Beverage",
                crm=_provider_over(attio),
                attio=attio,
                writer=writer,
            )

        # Both write attempts happened in the documented order.
        assert writer.apply.call_count == 2
        first_intent = writer.apply.call_args_list[0][0][0]
        assert first_intent.object == "companies"
        assert first_intent.updates["industry_vertical"] == "Food & Beverage"
        assert first_intent.updates["industry_vertical_status"] == "confirmed"
        second_intent = writer.apply.call_args_list[1][0][0]
        assert second_intent.object == "operator_review_queue"

    def test_name_normalization_matches_attio_record(self):
        """Operator passes 'sigma alimentos' (lowercase, trailing space) and
        the Attio record holds 'Sigma Alimentos'. normalize_company_name
        must equate them so the lookup succeeds."""
        attio = MagicMock()
        attio._request.return_value = {
            "data": [_queue_row("Sigma Alimentos")]
        }
        attio.search_companies.return_value = [
            _company_record("co-42", "Sigma Alimentos"),
        ]
        writer = MagicMock()
        result = approve_industry_classification(
            "sigma alimentos ",  # operator input
            vertical="Food & Beverage",
            crm=_provider_over(attio),
            attio=attio,
            writer=writer,
        )
        assert result["company_record_id"] == "co-42"


# ────────────────────────────────────────────────────────────────────────
# reject_industry_classification
# ────────────────────────────────────────────────────────────────────────


class TestReject:
    def test_marks_queue_rejected_without_company_write(self):
        attio = MagicMock()
        attio._request.return_value = {
            "data": [_queue_row("Sigma Alimentos", record_id="q-r1")]
        }
        writer = MagicMock()
        result = reject_industry_classification(
            "Sigma Alimentos",
            rationale="LLM guessed F&B but they're a holding company",
            crm=_provider_over(attio),
            attio=attio,
            writer=writer,
        )
        assert result == {
            "queue_record_id": "q-r1",
            "queue_rejected": True,
            "company_updated": False,
        }
        # Only ONE write — the queue row. Company stays untouched.
        writer.apply.assert_called_once()
        intent = writer.apply.call_args[0][0]
        assert intent.object == "operator_review_queue"
        assert intent.updates["status"] == "rejected"
        assert "rationale" in intent.updates["decision_json"]

    def test_empty_rationale_raises(self):
        attio = MagicMock()
        with pytest.raises(ValueError, match="non-empty rationale"):
            reject_industry_classification(
                "Sigma Alimentos", rationale="", crm=_provider_over(attio),
                attio=attio,
            )
        with pytest.raises(ValueError, match="non-empty rationale"):
            reject_industry_classification(
                "Sigma Alimentos", rationale="   ", crm=_provider_over(attio),
                attio=attio,
            )

    def test_no_pending_row_raises(self):
        attio = MagicMock()
        attio._request.return_value = {"data": []}
        with pytest.raises(IndustryRowNotFoundError):
            reject_industry_classification(
                "Sigma Alimentos", rationale="not a mfg",
                crm=_provider_over(attio), attio=attio,
            )


# ────────────────────────────────────────────────────────────────────────
# Writer registry compliance
# ────────────────────────────────────────────────────────────────────────


class TestWriterRegistryCompliance:
    EXPECTED_MODULE = "workflows.industry_approve"

    def test_industry_vertical_writer_registered(self):
        from clients.attio_writer_registry import WRITE_OWNER_REGISTRY
        owner = WRITE_OWNER_REGISTRY[("companies", "industry_vertical")]
        # PR-25 promoted this from sole-writer (bare str) to multi-writer (list)
        # by registering scripts.reclassify_industry as a co-writer for the
        # backfill path. Assert membership rather than identity so the
        # industry_approve writer contract stays pinned without re-pinning
        # the full writer set.
        if isinstance(owner, str):
            assert owner == self.EXPECTED_MODULE
        else:
            assert self.EXPECTED_MODULE in owner

    def test_industry_vertical_status_co_writer_registered(self):
        from clients.attio_writer_registry import WRITE_OWNER_REGISTRY
        writers = WRITE_OWNER_REGISTRY[("companies", "industry_vertical_status")]
        assert isinstance(writers, list)
        assert self.EXPECTED_MODULE in writers

    def test_queue_status_co_writer_registered(self):
        from clients.attio_writer_registry import WRITE_OWNER_REGISTRY
        writers = WRITE_OWNER_REGISTRY[("operator_review_queue", "status")]
        assert self.EXPECTED_MODULE in writers

    def test_queue_resolved_at_co_writer_registered(self):
        from clients.attio_writer_registry import WRITE_OWNER_REGISTRY
        writers = WRITE_OWNER_REGISTRY[("operator_review_queue", "resolved_at")]
        assert self.EXPECTED_MODULE in writers

    def test_queue_decision_json_co_writer_registered(self):
        from clients.attio_writer_registry import WRITE_OWNER_REGISTRY
        writers = WRITE_OWNER_REGISTRY[("operator_review_queue", "decision_json")]
        assert self.EXPECTED_MODULE in writers


# ────────────────────────────────────────────────────────────────────────
# Integration with real AttioWriter (no Attio call — patches _patch_with_retry)
# ────────────────────────────────────────────────────────────────────────


class TestAttioWriterAuthorization:
    """End-to-end: a real AttioWriter instance must accept the WriteIntents
    we construct (registry-authorized). Patches the network seam, not the
    authorization check."""

    def test_company_write_passes_registry(self):
        from clients.attio_writer import AttioWriter
        attio = MagicMock()
        attio._request.return_value = {
            "data": [_queue_row("Sigma Alimentos")]
        }
        attio.search_companies.return_value = [
            _company_record("co-42", "Sigma Alimentos"),
        ]
        # Patch the actual PATCH HTTP path so the test doesn't hit network,
        # but let the registry check run.
        with patch.object(AttioWriter, "_patch_with_retry", return_value={"data": {}}):
            result = approve_industry_classification(
                "Sigma Alimentos",
                vertical="Food & Beverage",
                crm=_provider_over(attio),
                attio=attio,
            )
        assert result["company_updated"] is True
        assert result["queue_resolved"] is True
