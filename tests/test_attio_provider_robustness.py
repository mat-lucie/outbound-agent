"""Robustness tests for ``AttioProvider`` response normalization.

A real Attio API can return an empty/odd 200 body on a PATCH (and many test
doubles stub ``None`` / ``{}`` / a bare ``{"data": []}``). Normalization must
therefore NEVER crash on a structurally-empty or malformed body — it returns a
best-effort :class:`Record` / :class:`Entry` using whatever is known (notably the
``record_id`` / ``entry_id`` the caller already passed) instead of raising. This
unblocks routing the write path through ``self._crm.update_*`` (increment 6b).

These tests pin the degraded branch only; the well-formed-body behavior is pinned
(unchanged) by ``test_attio_provider.py`` + ``test_crm_object_records.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from clients.attio import AttioClient
from clients.crm import AttioProvider
from clients.crm.base import Entry, Record, Stage


def _provider() -> tuple[AttioProvider, MagicMock]:
    inner = MagicMock(spec=AttioClient)
    return AttioProvider(attio_client=inner), inner


# ── _to_record: each tolerated empty/odd shape ───────────────────────────────


class TestToRecordTolerance:
    @pytest.mark.parametrize(
        "bad_body",
        [
            None,
            {},
            {"data": []},  # a bare list where a record dict was expected
            [],
            {"id": None},  # id present but not a dict
            {"id": {}},  # id dict missing record_id
            {"values": None},  # values present but not a dict
            {"values": []},
            {"id": {"record_id": "x"}, "values": "nope"},  # values wrong type
        ],
    )
    def test_never_raises_and_returns_record(self, bad_body: object) -> None:
        rec = AttioProvider._to_record(bad_body, "companies")
        assert isinstance(rec, Record)
        assert rec.object == "companies"
        assert rec.attributes == {}

    def test_fallback_id_is_used_when_body_has_none(self) -> None:
        rec = AttioProvider._to_record(None, "companies", fallback_id="co-7")
        assert rec.record_id == "co-7"
        assert rec.raw == {}  # None body preserved as {}

    def test_body_id_wins_over_fallback(self) -> None:
        rec = AttioProvider._to_record(
            {"id": {"record_id": "real"}}, "people", fallback_id="ignored"
        )
        assert rec.record_id == "real"

    def test_empty_dict_with_fallback(self) -> None:
        rec = AttioProvider._to_record({}, "deals", fallback_id="d-1")
        assert rec.record_id == "d-1"
        assert rec.raw == {}

    def test_no_fallback_no_id_yields_empty_string_id(self) -> None:
        rec = AttioProvider._to_record({}, "companies")
        assert rec.record_id == ""

    def test_well_formed_body_unchanged(self) -> None:
        # Sanity: the degraded branch did not touch the happy path.
        body = {
            "id": {"record_id": "co-9"},
            "values": {"name": [{"value": "Acme"}]},
        }
        rec = AttioProvider._to_record(body, "companies", fallback_id="ignored")
        assert rec.record_id == "co-9"
        assert rec.attributes == {"name": "Acme"}
        assert rec.raw is body


# ── _to_entry: each tolerated empty/odd shape ────────────────────────────────


class TestToEntryTolerance:
    @pytest.mark.parametrize("bad_body", [None, {}, [], "nope", 0])
    def test_never_raises_and_returns_entry(self, bad_body: object) -> None:
        provider, _ = _provider()
        entry = provider._to_entry(bad_body)
        assert isinstance(entry, Entry)
        assert isinstance(entry.stage, Stage)
        assert entry.entry_id == ""

    def test_fallback_entry_id_used_when_body_empty(self) -> None:
        provider, _ = _provider()
        entry = provider._to_entry({}, fallback_entry_id="entry-9")
        assert entry.entry_id == "entry-9"

    def test_body_entry_id_wins_over_fallback(self) -> None:
        provider, _ = _provider()
        body = {"id": {"entry_id": "real"}, "entry_values": {}}
        entry = provider._to_entry(body, fallback_entry_id="ignored")
        assert entry.entry_id == "real"


# ── Typed method: update_company over an empty inner return ───────────────────


class TestTypedMethodTolerance:
    def test_update_company_returns_record_when_inner_returns_none(self) -> None:
        """The exact case that crashed before: inner ``update_company`` → None."""
        provider, inner = _provider()
        inner.update_company.return_value = None
        rec = provider.update_company("rec1", {})
        assert isinstance(rec, Record)
        assert rec.record_id == "rec1"  # best-effort from the input id
        assert rec.object == "companies"
        assert rec.attributes == {}

    def test_update_company_empty_dict_uses_fallback_id(self) -> None:
        provider, inner = _provider()
        inner.update_company.return_value = {}
        rec = provider.update_company("rec2", {"name": "X"})
        assert rec.record_id == "rec2"

    def test_update_person_none_return(self) -> None:
        provider, inner = _provider()
        inner.update_person.return_value = None
        rec = provider.update_person("p-1", {"x": 1})
        assert rec.record_id == "p-1"
        assert rec.object == "people"

    def test_update_deal_none_return(self) -> None:
        provider, inner = _provider()
        inner.update_deal.return_value = None
        rec = provider.update_deal("dl-1", {"x": 1})
        assert rec.record_id == "dl-1"

    def test_update_list_entry_none_return_uses_fallback(self) -> None:
        provider, inner = _provider()
        inner.update_list_entry.return_value = None
        entry = provider.update_list_entry("entry-1", {"stage": "DM1 Sent"})
        assert isinstance(entry, Entry)
        assert entry.entry_id == "entry-1"


# ── Generic methods: empty/odd bodies from _request ──────────────────────────


class TestGenericMethodTolerance:
    def test_update_object_record_none_body(self) -> None:
        provider, inner = _provider()
        inner._request.return_value = None
        rec = provider.update_object_record("operator_review_queue", "orq-1", {})
        assert rec.record_id == "orq-1"
        assert rec.object == "operator_review_queue"

    def test_update_object_record_data_list_body(self) -> None:
        # {"data": []} unwraps to a bare list — must not crash _to_record.
        provider, inner = _provider()
        inner._request.return_value = {"data": []}
        rec = provider.update_object_record("operator_review_queue", "orq-2", {})
        assert rec.record_id == "orq-2"

    def test_get_object_record_empty_dict_body_uses_fallback(self) -> None:
        # Malformed 200 body (not a 404) → best-effort Record, NOT None.
        provider, inner = _provider()
        inner._request.return_value = {}
        rec = provider.get_object_record("operator_review_queue", "orq-3")
        assert rec is not None
        assert rec.record_id == "orq-3"

    def test_create_object_record_none_body(self) -> None:
        provider, inner = _provider()
        inner._request.return_value = None
        rec = provider.create_object_record("operator_review_queue", {})
        assert isinstance(rec, Record)
        assert rec.record_id == ""  # create has no caller-known id

    def test_query_object_records_none_body_is_empty_list(self) -> None:
        provider, inner = _provider()
        inner._request.return_value = None
        assert provider.query_object_records("operator_review_queue") == []

    def test_query_object_records_data_null_is_empty_list(self) -> None:
        provider, inner = _provider()
        inner._request.return_value = {"data": None}
        assert provider.query_object_records("operator_review_queue") == []
