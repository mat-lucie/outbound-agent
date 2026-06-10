"""Tests for the P1c-migrated ``backfill_export`` slice.

``backfill_export`` is the first workflow slice migrated from the raw
``AttioClient`` to the vendor-neutral ``CRMProvider`` contract
(P1c-increment-2). These tests construct a ``CRMProvider`` (a ``MagicMock``
speced to the ABC, returning the contract's normalized dataclasses) and assert
the slice reads the CRM exclusively through the contract methods
(``query_list_entries`` → ``Entry``, ``get_person`` → ``Record``,
``extract_person_info`` → ``RecordInfo``), preserving today's filtering and
CSV-export behavior exactly.
"""

import csv
from unittest.mock import MagicMock, patch

from clients.crm.base import CRMProvider, Entry, Record, RecordInfo, Stage
from workflows.backfill_companies import backfill_export


def _entry(record_id: str) -> Entry:
    return Entry(
        entry_id=f"ent-{record_id}",
        record_id=record_id,
        stage=Stage(name="Connection Sent", rank=2),
    )


def _person(record_id: str, *, company_link: bool) -> Record:
    """Build a normalized person Record. ``company_link`` controls whether the
    raw payload carries a resolved company record-reference (the already-linked
    skip path)."""
    company_values = (
        [{"target_record_id": "comp-001", "target_object": "companies"}]
        if company_link
        else []
    )
    return Record(
        record_id=record_id,
        object="people",
        attributes={},
        raw={
            "id": {"record_id": record_id},
            "values": {"company": company_values},
        },
    )


def _info(name: str | None, linkedin_url: str) -> RecordInfo:
    return RecordInfo(
        name=name,
        company=None,
        linkedin_url=linkedin_url,
        industry=None,
        title="",
    )


def _make_crm() -> MagicMock:
    return MagicMock(spec=CRMProvider)


def _read_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


class TestBackfillExportCRM:
    def test_exports_unlinked_records_via_contract(self, tmp_path, monkeypatch):
        """An unlinked person with a LinkedIn URL is exported, and the slice
        reads only through the CRMProvider contract methods."""
        monkeypatch.chdir(tmp_path)
        crm = _make_crm()
        crm.query_list_entries.return_value = [_entry("rec-001")]
        crm.get_person.return_value = _person("rec-001", company_link=False)
        crm.extract_person_info.return_value = _info(
            "Ana Garcia", "https://www.linkedin.com/in/anagarcia"
        )

        with patch("workflows.backfill_companies.time.sleep"):
            file_path = backfill_export(crm)
        rows = _read_csv(file_path)

        # Contract methods were used (not raw AttioClient methods).
        crm.query_list_entries.assert_called_once_with(limit=500)
        crm.get_person.assert_called_once_with("rec-001")
        crm.extract_person_info.assert_called_once()
        assert rows == [
            {
                "linkedin_url": "https://www.linkedin.com/in/anagarcia",
                "attio_record_id": "rec-001",
                "name": "Ana Garcia",
            }
        ]

    def test_skips_already_linked_via_record_raw(self, tmp_path, monkeypatch):
        """A person whose raw payload carries a resolved company reference is
        skipped (already-linked) — the boundary read goes through Record.raw."""
        monkeypatch.chdir(tmp_path)
        crm = _make_crm()
        crm.query_list_entries.return_value = [_entry("rec-002")]
        crm.get_person.return_value = _person("rec-002", company_link=True)

        with patch("workflows.backfill_companies.time.sleep"):
            file_path = backfill_export(crm)
        rows = _read_csv(file_path)

        # Already linked → never resolves display info, never exported.
        crm.extract_person_info.assert_not_called()
        assert rows == []

    def test_skips_record_without_linkedin(self, tmp_path, monkeypatch):
        """A person with no LinkedIn URL is skipped (no_linkedin path)."""
        monkeypatch.chdir(tmp_path)
        crm = _make_crm()
        crm.query_list_entries.return_value = [_entry("rec-003")]
        crm.get_person.return_value = _person("rec-003", company_link=False)
        crm.extract_person_info.return_value = _info("No Url Person", "")

        with patch("workflows.backfill_companies.time.sleep"):
            file_path = backfill_export(crm)
        rows = _read_csv(file_path)

        assert rows == []

    def test_skips_entry_without_record_id(self, tmp_path, monkeypatch):
        """An entry with a blank record_id is skipped before any get_person."""
        monkeypatch.chdir(tmp_path)
        crm = _make_crm()
        crm.query_list_entries.return_value = [_entry("")]

        with patch("workflows.backfill_companies.time.sleep"):
            backfill_export(crm)

        crm.get_person.assert_not_called()

    def test_skips_missing_person(self, tmp_path, monkeypatch):
        """A record_id that resolves to None (404) is skipped."""
        monkeypatch.chdir(tmp_path)
        crm = _make_crm()
        crm.query_list_entries.return_value = [_entry("rec-404")]
        crm.get_person.return_value = None

        with patch("workflows.backfill_companies.time.sleep"):
            file_path = backfill_export(crm)
        rows = _read_csv(file_path)

        crm.extract_person_info.assert_not_called()
        assert rows == []
