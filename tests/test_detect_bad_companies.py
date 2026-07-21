"""Tests for the P1c-migrated ``detect_bad_company_links`` slice.

``detect_bad_company_links`` was previously untested and took a raw
``AttioClient``.  This test covers the read slice's migration to the
vendor-neutral ``CRMProvider`` contract (P1c-increment-3), closing the
coverage gap.

Contract methods exercised:
    * ``query_list_entries`` → ``list[Entry]``
    * ``get_person`` → ``Record | None``
    * ``get_company`` → ``Record | None``
    * ``extract_person_info`` → ``RecordInfo``

The write sibling ``repair_bad_companies`` stays on ``AttioClient`` and is
not tested here.
"""

import csv
from unittest.mock import MagicMock, patch

from clients.crm.base import CRMProvider, Entry, Record, RecordInfo, Stage
from workflows.backfill_companies import detect_bad_company_links

# ── Helpers ──────────────────────────────────────────────────────────────────


def _entry(record_id: str) -> Entry:
    return Entry(
        entry_id=f"ent-{record_id}",
        record_id=record_id,
        stage=Stage(name="Connection Sent", rank=2),
    )


def _person(record_id: str, *, company_rid: str | None) -> Record:
    """Build a normalized person Record.

    ``company_rid`` controls whether the raw payload carries a resolved
    company record-reference (``None`` → no company linked).
    """
    company_values = (
        [{"target_record_id": company_rid, "target_object": "companies"}]
        if company_rid is not None
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


def _company(record_id: str, *, domains: list[str]) -> Record:
    """Build a normalized company Record with the given domain strings."""
    domain_values = [{"domain": d} for d in domains]
    name_values = [{"value": f"Company-{record_id}"}]
    return Record(
        record_id=record_id,
        object="companies",
        attributes={},
        raw={
            "id": {"record_id": record_id},
            "values": {
                "name": name_values,
                "domains": domain_values,
            },
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


# ── Test cases ────────────────────────────────────────────────────────────────


class TestDetectBadCompanyLinks:
    def test_person_linked_to_linkedin_domain_company_written_to_csv(
        self, tmp_path, monkeypatch
    ):
        """A person linked to a company whose domain contains 'linkedin.com'
        results in a row being written to the output CSV, with the correct
        columns (linkedin_url, attio_record_id, name, current_wrong_company).
        The contract methods query_list_entries, get_person, get_company, and
        extract_person_info are all called."""
        monkeypatch.chdir(tmp_path)
        crm = _make_crm()

        crm.query_list_entries.return_value = [_entry("rec-001")]
        crm.get_person.return_value = _person("rec-001", company_rid="comp-bad")
        crm.get_company.return_value = _company("comp-bad", domains=["linkedin.com"])
        crm.extract_person_info.return_value = _info(
            "Ana Garcia", "https://www.linkedin.com/in/anagarcia"
        )

        with patch("workflows.backfill_companies.time.sleep"):
            file_path = detect_bad_company_links(crm)

        rows = _read_csv(file_path)

        # Contract methods were all called.
        crm.query_list_entries.assert_called_once_with(
            limit=50_000, fail_if_truncated=True,
        )
        crm.get_person.assert_called_once_with("rec-001")
        crm.get_company.assert_called_once_with("comp-bad")
        crm.extract_person_info.assert_called_once()

        assert len(rows) == 1
        assert rows[0]["linkedin_url"] == "https://www.linkedin.com/in/anagarcia"
        assert rows[0]["attio_record_id"] == "rec-001"
        assert rows[0]["name"] == "Ana Garcia"
        assert rows[0]["current_wrong_company"] == "Company-comp-bad"

    def test_person_linked_to_clean_company_skipped(self, tmp_path, monkeypatch):
        """A person linked to a company with a clean domain (not linkedin.com)
        is NOT written to the CSV."""
        monkeypatch.chdir(tmp_path)
        crm = _make_crm()

        crm.query_list_entries.return_value = [_entry("rec-002")]
        crm.get_person.return_value = _person("rec-002", company_rid="comp-clean")
        crm.get_company.return_value = _company(
            "comp-clean", domains=["acme.com", "acme.mx"]
        )

        with patch("workflows.backfill_companies.time.sleep"):
            file_path = detect_bad_company_links(crm)

        rows = _read_csv(file_path)

        # Clean company → extract_person_info never called, row never written.
        crm.extract_person_info.assert_not_called()
        assert rows == []

    def test_person_with_no_company_link_skipped(self, tmp_path, monkeypatch):
        """A person with no company reference in the raw payload is skipped
        before any get_company call."""
        monkeypatch.chdir(tmp_path)
        crm = _make_crm()

        crm.query_list_entries.return_value = [_entry("rec-003")]
        crm.get_person.return_value = _person("rec-003", company_rid=None)

        with patch("workflows.backfill_companies.time.sleep"):
            file_path = detect_bad_company_links(crm)

        rows = _read_csv(file_path)

        crm.get_company.assert_not_called()
        crm.extract_person_info.assert_not_called()
        assert rows == []

    def test_csv_columns_and_path_shape(self, tmp_path, monkeypatch):
        """The output CSV is written to exports/bad_company_links_<date>.csv
        and has exactly the four expected columns."""
        monkeypatch.chdir(tmp_path)
        crm = _make_crm()

        crm.query_list_entries.return_value = [_entry("rec-col")]
        crm.get_person.return_value = _person("rec-col", company_rid="comp-li")
        crm.get_company.return_value = _company("comp-li", domains=["www.linkedin.com"])
        crm.extract_person_info.return_value = _info("Test User", "https://linkedin.com/in/test")

        with patch("workflows.backfill_companies.time.sleep"):
            file_path = detect_bad_company_links(crm)

        # Path is in exports/ and has the right prefix.
        assert file_path.startswith("exports/bad_company_links_")
        assert file_path.endswith(".csv")

        with open(file_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            assert reader.fieldnames == [
                "linkedin_url",
                "attio_record_id",
                "name",
                "current_wrong_company",
            ]

    def test_missing_person_record_skipped(self, tmp_path, monkeypatch):
        """An entry whose get_person returns None (404) is skipped."""
        monkeypatch.chdir(tmp_path)
        crm = _make_crm()

        crm.query_list_entries.return_value = [_entry("rec-404")]
        crm.get_person.return_value = None

        with patch("workflows.backfill_companies.time.sleep"):
            file_path = detect_bad_company_links(crm)

        rows = _read_csv(file_path)

        crm.get_company.assert_not_called()
        crm.extract_person_info.assert_not_called()
        assert rows == []

    def test_company_cache_prevents_duplicate_get_company_calls(
        self, tmp_path, monkeypatch
    ):
        """Two entries linked to the same bad company result in only ONE
        get_company call (the cache is hit on the second entry)."""
        monkeypatch.chdir(tmp_path)
        crm = _make_crm()

        crm.query_list_entries.return_value = [
            _entry("rec-A"),
            _entry("rec-B"),
        ]
        crm.get_person.side_effect = [
            _person("rec-A", company_rid="comp-shared"),
            _person("rec-B", company_rid="comp-shared"),
        ]
        crm.get_company.return_value = _company(
            "comp-shared", domains=["linkedin.com"]
        )
        crm.extract_person_info.side_effect = [
            _info("Alice", "https://linkedin.com/in/alice"),
            _info("Bob", "https://linkedin.com/in/bob"),
        ]

        with patch("workflows.backfill_companies.time.sleep"):
            file_path = detect_bad_company_links(crm)

        rows = _read_csv(file_path)

        # get_company called once, not twice (cache hit for second entry).
        crm.get_company.assert_called_once_with("comp-shared")
        assert len(rows) == 2
        names = {r["name"] for r in rows}
        assert names == {"Alice", "Bob"}
