"""Tests for the generic object-record API on ``AttioProvider``.

The four generic methods — ``query_object_records`` / ``get_object_record`` /
``create_object_record`` / ``update_object_record`` — replace the raw
``AttioClient._request(...)`` channel for arbitrary / operational objects
(operator_review_queue, llm_budget_ledger, …). These tests mock the inner
client's ``_request`` and assert each method hits the right path + verb + body
and normalizes the response to a :class:`Record` whose ``object`` is the
requested ``object_type``.

No network: the inner ``AttioClient`` is a ``MagicMock(spec=...)`` throughout,
so ``_request`` is stubbed and we assert on its call args.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx

from clients.attio import AttioClient
from clients.crm import AttioProvider
from clients.crm.base import CRMProvider, Record

# A raw Attio object-record envelope, as returned by the query/get/create/update
# endpoints under the ``data`` key. Mirrors the operator_review_queue shape the
# real raw-_request call sites read.
QUEUE_ROW_RAW = {
    "id": {"record_id": "orq-1"},
    "values": {
        "type": [{"value": "variant_proposal_pending"}],
        "status": [{"status": {"title": "open"}}],
        "idempotency_key": [{"value": "k-1"}],
    },
}


def _provider() -> tuple[AttioProvider, MagicMock]:
    inner = MagicMock(spec=AttioClient)
    return AttioProvider(attio_client=inner), inner


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.attio.com/x")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("err", request=request, response=response)


# ── Contract surface ─────────────────────────────────────────────────────────


class TestContractSurface:
    def test_generic_methods_are_on_the_abc(self) -> None:
        for name in (
            "query_object_records",
            "get_object_record",
            "create_object_record",
            "update_object_record",
        ):
            assert hasattr(CRMProvider, name), name


# ── query_object_records ─────────────────────────────────────────────────────


class TestQueryObjectRecords:
    def test_hits_query_endpoint_and_normalizes(self) -> None:
        provider, inner = _provider()
        inner._request.return_value = {"data": [QUEUE_ROW_RAW]}

        out = provider.query_object_records("operator_review_queue")

        inner._request.assert_called_once_with(
            "POST", "/objects/operator_review_queue/records/query", json={}
        )
        assert len(out) == 1
        rec = out[0]
        assert isinstance(rec, Record)
        assert rec.record_id == "orq-1"
        assert rec.object == "operator_review_queue"
        # First-value flattening unwraps the value/status.title shapes.
        assert rec.attributes["type"] == "variant_proposal_pending"
        assert rec.attributes["status"] == "open"

    def test_passes_filters_sorts_limit_through_faithfully(self) -> None:
        provider, inner = _provider()
        inner._request.return_value = {"data": []}

        filters = {
            "$and": [
                {"type": "degree_unknown"},
                {"status": {"$not": {"$in": ["resolved", "rejected"]}}},
            ]
        }
        sorts = [{"attribute": "opened_at", "direction": "desc"}]

        provider.query_object_records(
            "operator_review_queue", filters=filters, sorts=sorts, limit=100
        )

        inner._request.assert_called_once_with(
            "POST",
            "/objects/operator_review_queue/records/query",
            json={"filter": filters, "sorts": sorts, "limit": 100},
        )

    def test_omits_unset_optional_body_keys(self) -> None:
        provider, inner = _provider()
        inner._request.return_value = {"data": []}

        provider.query_object_records("llm_budget_ledger", limit=1)

        inner._request.assert_called_once_with(
            "POST", "/objects/llm_budget_ledger/records/query", json={"limit": 1}
        )

    def test_empty_result_is_empty_list(self) -> None:
        provider, inner = _provider()
        inner._request.return_value = {"data": []}
        assert provider.query_object_records("deals", filters={"x": 1}) == []

    def test_missing_data_key_is_empty_list(self) -> None:
        provider, inner = _provider()
        inner._request.return_value = {}
        assert provider.query_object_records("deals") == []

    def test_transport_error_propagates(self) -> None:
        provider, inner = _provider()
        inner._request.side_effect = _http_status_error(500)
        try:
            provider.query_object_records("deals")
        except httpx.HTTPStatusError:
            pass
        else:  # pragma: no cover - explicit failure
            raise AssertionError("expected HTTPStatusError to propagate")


# ── get_object_record ────────────────────────────────────────────────────────


class TestGetObjectRecord:
    def test_hits_get_endpoint_and_unwraps_data(self) -> None:
        provider, inner = _provider()
        inner._request.return_value = {"data": QUEUE_ROW_RAW}

        rec = provider.get_object_record("operator_review_queue", "orq-1")

        inner._request.assert_called_once_with(
            "GET", "/objects/operator_review_queue/records/orq-1"
        )
        assert isinstance(rec, Record)
        assert rec.record_id == "orq-1"
        assert rec.object == "operator_review_queue"

    def test_404_returns_none(self) -> None:
        provider, inner = _provider()
        inner._request.side_effect = _http_status_error(404)
        assert provider.get_object_record("operator_review_queue", "missing") is None

    def test_non_404_status_error_propagates(self) -> None:
        provider, inner = _provider()
        inner._request.side_effect = _http_status_error(500)
        try:
            provider.get_object_record("operator_review_queue", "x")
        except httpx.HTTPStatusError:
            pass
        else:  # pragma: no cover - explicit failure
            raise AssertionError("expected HTTPStatusError to propagate")


# ── create_object_record ─────────────────────────────────────────────────────


class TestCreateObjectRecord:
    def test_hits_create_endpoint_with_values_body(self) -> None:
        provider, inner = _provider()
        inner._request.return_value = {"data": QUEUE_ROW_RAW}
        attrs = {"type": "variant_proposal_pending", "status": "open"}

        rec = provider.create_object_record("operator_review_queue", attrs)

        inner._request.assert_called_once_with(
            "POST",
            "/objects/operator_review_queue/records",
            json={"data": {"values": attrs}},
        )
        assert isinstance(rec, Record)
        assert rec.record_id == "orq-1"
        assert rec.object == "operator_review_queue"

    def test_normalizes_unwrapped_response(self) -> None:
        """Some create responses come back without a ``data`` wrapper."""
        provider, inner = _provider()
        inner._request.return_value = QUEUE_ROW_RAW
        rec = provider.create_object_record("operator_review_queue", {"type": "x"})
        assert rec.record_id == "orq-1"


# ── update_object_record ─────────────────────────────────────────────────────


class TestUpdateObjectRecord:
    def test_hits_patch_endpoint_with_values_body(self) -> None:
        provider, inner = _provider()
        inner._request.return_value = {"data": QUEUE_ROW_RAW}
        attrs = {"superseded_by_alarm": True}

        rec = provider.update_object_record(
            "operator_review_queue", "orq-1", attrs
        )

        inner._request.assert_called_once_with(
            "PATCH",
            "/objects/operator_review_queue/records/orq-1",
            json={"data": {"values": attrs}},
        )
        assert isinstance(rec, Record)
        assert rec.record_id == "orq-1"
        assert rec.object == "operator_review_queue"
