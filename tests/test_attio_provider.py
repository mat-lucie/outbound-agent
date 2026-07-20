"""Tests for the Attio reference adapter (``AttioProvider``).

These pin the normalization layer the adapter adds on top of the inner
``AttioClient``. The inner client is mocked throughout — no network. Each
contract method is checked for (a) correct delegation to the inner client and
(b) the right normalized return shape (``Record`` / ``Entry`` / ``RecordInfo``).

The tricky normalizations get extra scrutiny against the SAME raw Attio JSON
shapes the real ``test_attio_client.py`` suite uses:
  * ``query_list_entries`` → ``Entry`` with stage/attributes matching what
    ``AttioClient.parse_entry`` produced.
  * ``extract_person_info`` → ``""`` (not ``None``) for blank linkedin/title,
    and company resolved off ``Record.raw``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from clients.attio import AttioClient
from clients.crm import AttioProvider
from clients.crm.base import Entry, Record, RecordInfo, Stage

# ── Sample raw Attio payloads (mirror test_attio_client.py shapes) ──────────

PERSON_RAW = {
    "id": {"record_id": "rec-1"},
    "values": {
        "name": [{"first_name": "Ada", "last_name": "Lovelace"}],
        "job_title": [{"value": "Plant Manager"}],
        "linkedin": [{"value": "https://linkedin.com/in/ada"}],
        "company": [{"target_object": "companies", "target_record_id": "co-9"}],
    },
}

ENTRY_RAW = {
    "id": {"entry_id": "entry-1", "list_id": "list-X"},
    "parent_record_id": "rec-1",
    "created_at": "2026-04-01T00:00:00Z",
    "entry_values": {
        "stage": [{"status": {"title": "DM1 Sent"}}],
        "persona": [{"attribute_type": "select", "option": {"title": "operations_leaders"}}],
        "dm_step": [{"value": 1}],
        "quality_score": [{"value": 72}],
    },
}


def _provider() -> tuple[AttioProvider, MagicMock]:
    """An AttioProvider wrapping a MagicMock inner client."""
    inner = MagicMock(spec=AttioClient)
    return AttioProvider(attio_client=inner), inner


# ── Construction ────────────────────────────────────────────────────────────


class TestConstruction:
    def test_composes_injected_client(self) -> None:
        inner = MagicMock(spec=AttioClient)
        provider = AttioProvider(attio_client=inner)
        assert provider._attio is inner

    def test_builds_client_from_api_key_when_none_given(self) -> None:
        with patch("clients.crm.attio_provider.AttioClient") as mock_cls:
            AttioProvider(api_key="test-key")
        mock_cls.assert_called_once_with(api_key="test-key")


# ── People reads ─────────────────────────────────────────────────────────────


class TestPeopleReads:
    def test_search_people_normalizes_to_records(self) -> None:
        provider, inner = _provider()
        inner.search_people.return_value = [PERSON_RAW]

        result = provider.search_people(filter_={"linkedin": "x"}, limit=10)

        inner.search_people.assert_called_once_with(filter_={"linkedin": "x"}, limit=10)
        assert len(result) == 1
        rec = result[0]
        assert isinstance(rec, Record)
        assert rec.record_id == "rec-1"
        assert rec.object == "people"
        assert rec.raw is PERSON_RAW

    def test_get_person_returns_record(self) -> None:
        provider, inner = _provider()
        inner.get_person.return_value = PERSON_RAW

        rec = provider.get_person("rec-1")

        inner.get_person.assert_called_once_with("rec-1")
        assert isinstance(rec, Record)
        assert rec.record_id == "rec-1"
        assert rec.object == "people"

    def test_get_person_returns_none_on_404(self) -> None:
        provider, inner = _provider()
        inner.get_person.return_value = None
        assert provider.get_person("missing") is None

    def test_search_person_by_linkedin_returns_record(self) -> None:
        provider, inner = _provider()
        inner.search_person_by_linkedin.return_value = PERSON_RAW

        rec = provider.search_person_by_linkedin("https://linkedin.com/in/ada")

        inner.search_person_by_linkedin.assert_called_once_with(
            "https://linkedin.com/in/ada"
        )
        assert isinstance(rec, Record)
        assert rec.record_id == "rec-1"

    def test_search_person_by_linkedin_returns_none(self) -> None:
        provider, inner = _provider()
        inner.search_person_by_linkedin.return_value = None
        assert provider.search_person_by_linkedin("nope") is None


class TestBulkFetchPersons:
    def test_normalizes_map_and_uses_internal_max_workers(self) -> None:
        provider, inner = _provider()
        inner.bulk_fetch_persons_by_record_ids.return_value = {"rec-1": PERSON_RAW}
        metrics = object()

        result = provider.bulk_fetch_persons({"rec-1", "rec-2"}, metrics=metrics)

        # Delegates to the Attio-suffixed inner method, passing max_workers as
        # an internal default and threading metrics through.
        call = inner.bulk_fetch_persons_by_record_ids.call_args
        assert call.args[0] == {"rec-1", "rec-2"}
        assert call.kwargs["max_workers"] == 8
        assert call.kwargs["metrics"] is metrics
        assert set(result.keys()) == {"rec-1"}
        assert isinstance(result["rec-1"], Record)
        assert result["rec-1"].object == "people"


# ── People writes ────────────────────────────────────────────────────────────


class TestPeopleWrites:
    def test_create_person(self) -> None:
        provider, inner = _provider()
        inner.create_person.return_value = PERSON_RAW
        attrs = {"name": [{"full_name": "Ada"}]}

        rec = provider.create_person(attrs)

        inner.create_person.assert_called_once_with(attrs)
        assert rec.record_id == "rec-1"
        assert rec.object == "people"

    def test_update_person(self) -> None:
        provider, inner = _provider()
        inner.update_person.return_value = PERSON_RAW
        rec = provider.update_person("rec-1", {"job_title": "x"})
        inner.update_person.assert_called_once_with("rec-1", {"job_title": "x"})
        assert rec.object == "people"

    def test_upsert_person(self) -> None:
        provider, inner = _provider()
        inner.upsert_person.return_value = PERSON_RAW
        rec = provider.upsert_person("linkedin", {"linkedin": "x"})
        inner.upsert_person.assert_called_once_with("linkedin", {"linkedin": "x"})
        assert rec.object == "people"


# ── Companies ────────────────────────────────────────────────────────────────

COMPANY_RAW = {
    "id": {"record_id": "co-9"},
    "values": {"name": [{"value": "Acme"}]},
}


class TestCompanies:
    def test_search_companies(self) -> None:
        provider, inner = _provider()
        inner.search_companies.return_value = [COMPANY_RAW]
        result = provider.search_companies(filter_=None, limit=5)
        inner.search_companies.assert_called_once_with(filter_=None, limit=5)
        assert result[0].object == "companies"
        assert result[0].record_id == "co-9"

    def test_get_company(self) -> None:
        provider, inner = _provider()
        inner.get_company.return_value = COMPANY_RAW
        rec = provider.get_company("co-9")
        assert rec is not None and rec.object == "companies"

    def test_get_company_none(self) -> None:
        provider, inner = _provider()
        inner.get_company.return_value = None
        assert provider.get_company("missing") is None

    def test_create_company(self) -> None:
        provider, inner = _provider()
        inner.create_company.return_value = COMPANY_RAW
        rec = provider.create_company({"name": "Acme"})
        inner.create_company.assert_called_once_with({"name": "Acme"})
        assert rec.object == "companies"

    def test_update_company(self) -> None:
        provider, inner = _provider()
        inner.update_company.return_value = COMPANY_RAW
        rec = provider.update_company("co-9", {"name": "Acme2"})
        inner.update_company.assert_called_once_with("co-9", {"name": "Acme2"})
        assert rec.object == "companies"

    def test_search_company_by_domain(self) -> None:
        provider, inner = _provider()
        inner.search_company_by_domain.return_value = COMPANY_RAW
        rec = provider.search_company_by_domain("acme.com")
        inner.search_company_by_domain.assert_called_once_with("acme.com")
        assert rec is not None and rec.object == "companies"

    def test_search_company_by_domain_none(self) -> None:
        provider, inner = _provider()
        inner.search_company_by_domain.return_value = None
        assert provider.search_company_by_domain("nope.com") is None


# ── Deals ────────────────────────────────────────────────────────────────────

DEAL_RAW = {"id": {"record_id": "deal-3"}, "values": {"name": [{"value": "D"}]}}


class TestDeals:
    def test_get_deal(self) -> None:
        provider, inner = _provider()
        inner.get_deal.return_value = DEAL_RAW
        rec = provider.get_deal("deal-3")
        assert rec is not None and rec.object == "deals"

    def test_get_deal_none(self) -> None:
        provider, inner = _provider()
        inner.get_deal.return_value = None
        assert provider.get_deal("missing") is None

    def test_create_deal(self) -> None:
        provider, inner = _provider()
        inner.create_deal.return_value = DEAL_RAW
        rec = provider.create_deal({"name": "D"})
        inner.create_deal.assert_called_once_with({"name": "D"})
        assert rec.object == "deals"

    def test_update_deal(self) -> None:
        provider, inner = _provider()
        inner.update_deal.return_value = DEAL_RAW
        rec = provider.update_deal("deal-3", {"stage": "Won"})
        inner.update_deal.assert_called_once_with("deal-3", {"stage": "Won"})
        assert rec.object == "deals"

    def test_search_deals(self) -> None:
        provider, inner = _provider()
        inner.search_deals.return_value = [DEAL_RAW]
        result = provider.search_deals(filter_=None, limit=100)
        inner.search_deals.assert_called_once_with(filter_=None, limit=100)
        assert result[0].object == "deals"


# ── List entries — the tricky shape ──────────────────────────────────────────


class TestQueryListEntries:
    def test_returns_normalized_entries_matching_parse_entry(self) -> None:
        provider, inner = _provider()
        inner.query_list_entries.return_value = [ENTRY_RAW]

        result = provider.query_list_entries(list_id="list-X", limit=100)

        inner.query_list_entries.assert_called_once_with(
            list_id="list-X", filter_=None, limit=100, fail_if_truncated=False,
        )
        assert len(result) == 1
        entry = result[0]
        assert isinstance(entry, Entry)
        assert entry.entry_id == "entry-1"
        assert entry.record_id == "rec-1"
        assert entry.raw is ENTRY_RAW

        # Stage matches what _extract_stage produced, with rank resolved.
        assert isinstance(entry.stage, Stage)
        assert entry.stage.name == "DM1 Sent"
        assert entry.stage.rank is not None  # known stage → resolved rank

        # The flat attribute map mirrors parse_entry's output minus identity/stage.
        parsed = AttioClient.parse_entry(ENTRY_RAW)
        for key, value in parsed.items():
            if key in ("entry_id", "record_id", "stage"):
                assert key not in entry.attributes
            else:
                assert entry.attributes[key] == value, key
        # Spot-check the select-typed + scalar parses survived normalization.
        assert entry.attributes["persona"] == "operations_leaders"
        assert entry.attributes["dm_step"] == 1
        assert entry.attributes["quality_score"] == 72

    def test_unknown_stage_yields_rank_none(self) -> None:
        provider, inner = _provider()
        weird = {
            "id": {"entry_id": "e-2"},
            "parent_record_id": "rec-2",
            "entry_values": {"stage": [{"status": {"title": "Totally Made Up"}}]},
        }
        inner.query_list_entries.return_value = [weird]

        entry = provider.query_list_entries()[0]
        assert entry.stage.name == "Totally Made Up"
        assert entry.stage.rank is None

    def test_blank_stage_yields_rank_none(self) -> None:
        provider, inner = _provider()
        no_stage = {
            "id": {"entry_id": "e-3"},
            "parent_record_id": "rec-3",
            "entry_values": {},
        }
        inner.query_list_entries.return_value = [no_stage]
        entry = provider.query_list_entries()[0]
        assert entry.stage.name == ""
        assert entry.stage.rank is None


class TestListEntryWrites:
    def test_add_list_entry_normalizes_and_delegates(self) -> None:
        provider, inner = _provider()
        inner.add_list_entry.return_value = ENTRY_RAW

        entry = provider.add_list_entry(
            record_id="rec-1",
            stage_name="DM1 Sent",
            entry_attributes={"persona": "x"},
            list_id="list-X",
        )

        inner.add_list_entry.assert_called_once_with(
            record_id="rec-1",
            stage_name="DM1 Sent",
            entry_attributes={"persona": "x"},
            list_id="list-X",
            existing_entries=None,
        )
        assert isinstance(entry, Entry)
        assert entry.entry_id == "entry-1"

    def test_add_list_entry_threads_raw_from_existing_entries(self) -> None:
        """existing_entries arrive as normalized Entry objects; the adapter must
        hand the inner client back the RAW payloads so its client-side dedupe
        filter/rank runs against the same shapes it always has."""
        provider, inner = _provider()
        inner.add_list_entry.return_value = ENTRY_RAW
        existing = [provider._to_entry(ENTRY_RAW)]

        provider.add_list_entry(
            record_id="rec-1",
            stage_name="DM2 Sent",
            list_id="list-X",
            existing_entries=existing,
        )

        passed = inner.add_list_entry.call_args.kwargs["existing_entries"]
        assert passed == [ENTRY_RAW]
        assert passed[0] is ENTRY_RAW  # exact raw payload, not a re-serialization

    def test_update_list_entry(self) -> None:
        provider, inner = _provider()
        inner.update_list_entry.return_value = ENTRY_RAW

        entry = provider.update_list_entry(
            entry_id="entry-1",
            entry_attributes={"stage": "DM2 Sent"},
            list_id="list-X",
        )

        inner.update_list_entry.assert_called_once_with(
            entry_id="entry-1",
            entry_attributes={"stage": "DM2 Sent"},
            list_id="list-X",
        )
        assert entry.entry_id == "entry-1"


# ── Notes ────────────────────────────────────────────────────────────────────


class TestNotes:
    def test_create_note_wraps_as_record(self) -> None:
        provider, inner = _provider()
        note_raw = {"id": {"note_id": "note-7"}, "title": "t"}
        inner.create_note.return_value = note_raw

        rec = provider.create_note("rec-1", "t", "body", parent_object="people")

        inner.create_note.assert_called_once_with(
            record_id="rec-1", title="t", content="body", parent_object="people"
        )
        assert isinstance(rec, Record)
        assert rec.record_id == "note-7"
        assert rec.object == "notes"
        assert rec.raw is note_raw


# ── extract_person_info — the other tricky shape ─────────────────────────────


class TestExtractPersonInfo:
    def test_delegates_via_record_raw_and_returns_recordinfo(self) -> None:
        provider, inner = _provider()
        inner.extract_record_info.return_value = (
            "Ada Lovelace",
            "Acme",
            "https://linkedin.com/in/ada",
            "Manufacturing",
            "Plant Manager",
        )
        record = provider._to_record(PERSON_RAW, "people")

        info = provider.extract_person_info(record)

        # Company resolution runs off the RAW payload, not the flat attrs.
        inner.extract_record_info.assert_called_once_with(PERSON_RAW)
        assert isinstance(info, RecordInfo)
        assert info.name == "Ada Lovelace"
        assert info.company == "Acme"
        assert info.linkedin_url == "https://linkedin.com/in/ada"
        assert info.industry == "Manufacturing"
        assert info.title == "Plant Manager"

    def test_blank_linkedin_and_title_stay_empty_string_not_none(self) -> None:
        """RecordInfo.linkedin_url and .title MUST be "" (not None) when blank —
        downstream truthiness checks depend on it. extract_record_info already
        returns "" for these two; the adapter passes them straight through."""
        provider, inner = _provider()
        inner.extract_record_info.return_value = (None, None, "", None, "")
        record = provider._to_record({"id": {"record_id": "r"}, "values": {}}, "people")

        info = provider.extract_person_info(record)

        assert info.name is None
        assert info.company is None
        assert info.industry is None
        # The load-bearing assertion: empty string, never None.
        assert info.linkedin_url == ""
        assert info.linkedin_url is not None
        assert info.title == ""
        assert info.title is not None


# ── Attribute flattening ─────────────────────────────────────────────────────


class TestRecordAttributeFlattening:
    def test_first_value_per_slug_unwraps_scalars(self) -> None:
        provider, inner = _provider()
        inner.get_company.return_value = COMPANY_RAW
        rec = provider.get_company("co-9")
        assert rec is not None
        # name slug flattens to its first value's scalar.
        assert rec.attributes["name"] == "Acme"

    def test_flattening_matches_object_record_first_value(self) -> None:
        """The chosen flattening is exactly AttioClient.object_record_first_value
        applied per slug — pin that so the documented contract can't drift."""
        provider, _ = _provider()
        rec = provider._to_record(PERSON_RAW, "people")
        for slug in PERSON_RAW["values"]:
            assert rec.attributes[slug] == AttioClient.object_record_first_value(
                PERSON_RAW["values"], slug
            )


# ── Quirks stay adapter-internal ─────────────────────────────────────────────


class TestVendorQuirks:
    def test_is_person_company_corrupted_delegates(self) -> None:
        provider, inner = _provider()
        inner.is_person_company_corrupted.return_value = True
        assert provider.is_person_company_corrupted("rec-1") is True
        inner.is_person_company_corrupted.assert_called_once_with("rec-1")

    def test_corruption_check_not_on_abc(self) -> None:
        """The Clearbit-corruption quirk must NOT leak onto the contract."""
        from clients.crm.base import CRMProvider

        assert not hasattr(CRMProvider, "is_person_company_corrupted")


# ── Lifecycle ────────────────────────────────────────────────────────────────


class TestLifecycle:
    def test_context_manager_closes_inner(self) -> None:
        provider, inner = _provider()
        with provider as p:
            assert p is provider
        inner.close.assert_called_once_with()
