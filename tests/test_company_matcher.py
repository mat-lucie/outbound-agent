"""Tests for the company matching and creation utilities."""

import csv
import os
import tempfile
from unittest.mock import MagicMock, patch

from workflows.company_matcher import extract_real_domain, match_or_create_company, normalize_company_name

# ── A. extract_real_domain ───────────────────────────────────────────────────

class TestExtractRealDomain:

    def test_linkedin_company_url_returns_empty(self):
        row = {"companyUrl": "https://www.linkedin.com/company/prolec-ge/"}
        assert extract_real_domain(row) == ""

    def test_linkedin_url_without_www_returns_empty(self):
        row = {"companyUrl": "https://linkedin.com/company/acme"}
        assert extract_real_domain(row) == ""

    def test_real_website_url(self):
        row = {"companyUrl": "https://www.cementra.com"}
        assert extract_real_domain(row) == "cementra.com"

    def test_real_website_url_with_path(self):
        row = {"companyUrl": "https://www.siemens.com/global/en.html"}
        assert extract_real_domain(row) == "siemens.com"

    def test_empty_string(self):
        assert extract_real_domain({}) == ""
        assert extract_real_domain({"companyUrl": ""}) == ""

    def test_plain_domain(self):
        row = {"companyUrl": "cementra.com"}
        assert extract_real_domain(row) == "cementra.com"

    def test_www_prefix_stripped(self):
        row = {"companyUrl": "www.cementra.com"}
        assert extract_real_domain(row) == "cementra.com"

    def test_falls_back_to_companyWebsite(self):
        row = {"companyWebsite": "https://www.bimbo.com"}
        assert extract_real_domain(row) == "bimbo.com"

    def test_falls_back_to_companyDomain(self):
        row = {"companyDomain": "bimbo.com"}
        assert extract_real_domain(row) == "bimbo.com"

    def test_linkedin_in_fallback_still_rejected(self):
        row = {"companyWebsite": "https://linkedin.com/company/bimbo"}
        assert extract_real_domain(row) == ""

    def test_none_value_returns_empty(self):
        row = {"companyUrl": None}
        assert extract_real_domain(row) == ""


# ── B. normalize_company_name ────────────────────────────────────────────────

class TestNormalizeCompanyName:

    def test_strips_inc(self):
        assert normalize_company_name("Cementra Inc") == "cementra"

    def test_strips_inc_dot(self):
        assert normalize_company_name("Cementra Inc.") == "cementra"

    def test_strips_sa_de_cv(self):
        assert normalize_company_name("Bimbo S.A. de C.V.") == "bimbo"

    def test_strips_ltda(self):
        assert normalize_company_name("Ambev Ltda.") == "ambev"

    def test_strips_gmbh(self):
        assert normalize_company_name("Siemens GmbH") == "siemens"

    def test_handles_already_clean(self):
        assert normalize_company_name("CEMENTRA") == "cementra"

    def test_empty_string(self):
        assert normalize_company_name("") == ""

    def test_strips_llc(self):
        assert normalize_company_name("Acme LLC") == "acme"

    def test_strips_pty_ltd(self):
        assert normalize_company_name("BHP Pty Ltd") == "bhp"

    def test_preserves_internal_words(self):
        # "group" at end should be stripped, but not in the middle
        assert normalize_company_name("Grupo Bimbo") == "grupo bimbo"


# ── B. match_or_create_company ───────────────────────────────────────────────

class TestMatchOrCreateCompany:

    def test_domain_match_returns_existing(self):
        attio = MagicMock()
        attio.search_company_by_domain.return_value = {
            "id": {"record_id": "comp-123"},
            "values": {"name": [{"value": "Cargill"}]},
        }

        result = match_or_create_company(attio, "Cargill", domain="cargill.com")

        assert result == "comp-123"
        attio.create_company.assert_not_called()

    def test_name_match_returns_existing(self):
        attio = MagicMock()
        attio.search_company_by_domain.return_value = None
        attio.search_companies.return_value = [
            {"id": {"record_id": "comp-456"}, "values": {"name": [{"value": "Cementra S.A. de C.V."}]}}
        ]

        result = match_or_create_company(attio, "CEMENTRA Inc.", domain=None)

        assert result == "comp-456"
        attio.create_company.assert_not_called()

    def test_no_match_creates_company(self):
        attio = MagicMock()
        attio.search_company_by_domain.return_value = None
        attio.search_companies.return_value = []
        attio.create_company.return_value = {"id": {"record_id": "comp-new"}}

        result = match_or_create_company(attio, "New Corp", domain="newcorp.com")

        assert result == "comp-new"
        attio.create_company.assert_called_once()
        # Verify note was created
        attio.create_note.assert_called_once()
        note_call = attio.create_note.call_args
        assert note_call[1].get("parent_object", note_call[0][3] if len(note_call[0]) > 3 else "") == "companies"

    def test_api_error_returns_none(self):
        attio = MagicMock()
        attio.search_company_by_domain.side_effect = Exception("API error")

        result = match_or_create_company(attio, "Cargill", domain="cargill.com")

        assert result is None

    def test_empty_company_name_returns_none(self):
        attio = MagicMock()

        result = match_or_create_company(attio, "", domain="test.com")

        assert result is None
        attio.search_company_by_domain.assert_not_called()

    def test_no_domain_skips_domain_search(self):
        attio = MagicMock()
        attio.search_companies.return_value = []
        attio.create_company.return_value = {"id": {"record_id": "comp-xyz"}}

        result = match_or_create_company(attio, "Some Corp", domain=None)

        assert result == "comp-xyz"
        attio.search_company_by_domain.assert_not_called()

    def test_create_includes_domain_when_provided(self):
        attio = MagicMock()
        attio.search_company_by_domain.return_value = None
        attio.search_companies.return_value = []
        attio.create_company.return_value = {"id": {"record_id": "comp-new"}}

        match_or_create_company(attio, "New Corp", domain="newcorp.com")

        create_call = attio.create_company.call_args
        attrs = create_call[0][0] if create_call[0] else create_call[1].get("attributes", {})
        assert "domains" in attrs
        assert attrs["domains"] == [{"domain": "newcorp.com"}]

    def test_www_prefix_stripped_from_domain(self):
        attio = MagicMock()
        attio.search_company_by_domain.return_value = {
            "id": {"record_id": "comp-www"},
            "values": {"name": [{"value": "Cargill"}]},
        }

        result = match_or_create_company(attio, "Cargill", domain="www.cargill.com")

        assert result == "comp-www"
        attio.search_company_by_domain.assert_called_once_with("cargill.com")

    def test_create_excludes_domain_when_not_provided(self):
        attio = MagicMock()
        attio.search_companies.return_value = []
        attio.create_company.return_value = {"id": {"record_id": "comp-new"}}

        match_or_create_company(attio, "New Corp", domain=None)

        create_call = attio.create_company.call_args
        attrs = create_call[0][0] if create_call[0] else create_call[1].get("attributes", {})
        assert "domains" not in attrs

    def test_create_includes_industry_vertical_when_provided(self):
        attio = MagicMock()
        attio.search_company_by_domain.return_value = None
        attio.search_companies.return_value = []
        attio.create_company.return_value = {"id": {"record_id": "comp-new"}}

        result = match_or_create_company(
            attio, "Nissan", domain="nissan.com", industry_vertical="Automotive"
        )

        assert result == "comp-new"
        attio.create_company.assert_called_once()
        create_call = attio.create_company.call_args
        attrs = create_call[0][0] if create_call[0] else create_call[1].get("attributes", {})
        assert attrs["industry_vertical"] == "Automotive"
        assert attrs["name"] == "Nissan"
        assert attrs["domains"] == [{"domain": "nissan.com"}]

    def test_create_omits_industry_vertical_when_not_provided(self):
        attio = MagicMock()
        attio.search_company_by_domain.return_value = None
        attio.search_companies.return_value = []
        attio.create_company.return_value = {"id": {"record_id": "comp-new"}}

        result = match_or_create_company(attio, "New Corp", domain="newcorp.com")

        assert result == "comp-new"
        create_call = attio.create_company.call_args
        attrs = create_call[0][0] if create_call[0] else create_call[1].get("attributes", {})
        assert "industry_vertical" not in attrs


# ── C. backfill_import ──────────────────────────────────────────────────────

def _write_csv(path: str, rows: list[dict]) -> None:
    """Helper: write a list of dicts to a CSV file."""
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


class TestBackfillImport:

    @patch.dict(os.environ, {"ATTIO_API_KEY": "fake", "ATTIO_LIST_ID": "list-001"})
    def test_links_company_from_pb_csv(self):
        """PB CSV row with matching LinkedIn URL triggers company matching + update_person."""
        from workflows.backfill_companies import backfill_import

        with tempfile.TemporaryDirectory() as tmp:
            export_csv = os.path.join(tmp, "export.csv")
            pb_csv = os.path.join(tmp, "pb_output.csv")

            _write_csv(export_csv, [
                {"linkedin_url": "https://www.linkedin.com/in/anaejemplo", "attio_record_id": "rec-001", "name": "Ana Ejemplo"},
            ])
            _write_csv(pb_csv, [
                {"linkedinProfileUrl": "https://www.linkedin.com/in/anaejemplo", "companyName": "Cementra", "companyUrl": "https://www.cementra.com"},
            ])

            attio = MagicMock()

            with patch("workflows.backfill_companies.match_or_create_company", return_value="comp-cementra") as mock_matcher:
                result = backfill_import(attio, pb_csv, export_csv)

            assert result["linked"] == 1
            assert result["failed"] == 0
            assert result["skipped"] == 0
            mock_matcher.assert_called_once_with(attio, "Cementra", domain="cementra.com")
            attio.update_person.assert_called_once_with(
                "rec-001",
                {"company": [{"target_object": "companies", "target_record_id": "comp-cementra"}]},
            )

    @patch.dict(os.environ, {"ATTIO_API_KEY": "fake", "ATTIO_LIST_ID": "list-001"})
    def test_linkedin_company_url_not_passed_as_domain(self):
        """PB CSV with LinkedIn companyUrl should pass domain=None, not linkedin.com."""
        from workflows.backfill_companies import backfill_import

        with tempfile.TemporaryDirectory() as tmp:
            export_csv = os.path.join(tmp, "export.csv")
            pb_csv = os.path.join(tmp, "pb_output.csv")

            _write_csv(export_csv, [
                {"linkedin_url": "https://www.linkedin.com/in/anaejemplo", "attio_record_id": "rec-001", "name": "Ana Ejemplo"},
            ])
            _write_csv(pb_csv, [
                {"linkedinProfileUrl": "https://www.linkedin.com/in/anaejemplo", "companyName": "Cementra", "companyUrl": "https://www.linkedin.com/company/cementra/"},
            ])

            attio = MagicMock()

            with patch("workflows.backfill_companies.match_or_create_company", return_value="comp-cementra") as mock_matcher:
                result = backfill_import(attio, pb_csv, export_csv)

            assert result["linked"] == 1
            mock_matcher.assert_called_once_with(attio, "Cementra", domain=None)

    @patch.dict(os.environ, {"ATTIO_API_KEY": "fake", "ATTIO_LIST_ID": "list-001"})
    def test_skips_unmatched_linkedin_url(self):
        """PB CSV row whose LinkedIn URL is not in the export CSV gets skipped."""
        from workflows.backfill_companies import backfill_import

        with tempfile.TemporaryDirectory() as tmp:
            export_csv = os.path.join(tmp, "export.csv")
            pb_csv = os.path.join(tmp, "pb_output.csv")

            _write_csv(export_csv, [
                {"linkedin_url": "https://www.linkedin.com/in/anaejemplo", "attio_record_id": "rec-001", "name": "Ana Ejemplo"},
            ])
            _write_csv(pb_csv, [
                {"linkedinProfileUrl": "https://www.linkedin.com/in/unknown-person", "companyName": "SomeCo", "companyUrl": ""},
            ])

            attio = MagicMock()

            with patch("workflows.backfill_companies.match_or_create_company") as mock_matcher:
                result = backfill_import(attio, pb_csv, export_csv)

            assert result["skipped"] == 1
            assert result["linked"] == 0
            mock_matcher.assert_not_called()
            attio.update_person.assert_not_called()

    @patch.dict(os.environ, {"ATTIO_API_KEY": "fake", "ATTIO_LIST_ID": "list-001"})
    def test_fails_when_no_company_name(self):
        """PB CSV row with matching URL but empty company name counts as failed."""
        from workflows.backfill_companies import backfill_import

        with tempfile.TemporaryDirectory() as tmp:
            export_csv = os.path.join(tmp, "export.csv")
            pb_csv = os.path.join(tmp, "pb_output.csv")

            _write_csv(export_csv, [
                {"linkedin_url": "https://www.linkedin.com/in/anaejemplo", "attio_record_id": "rec-001", "name": "Ana Ejemplo"},
            ])
            _write_csv(pb_csv, [
                {"linkedinProfileUrl": "https://www.linkedin.com/in/anaejemplo", "companyName": "", "companyUrl": ""},
            ])

            attio = MagicMock()

            with patch("workflows.backfill_companies.match_or_create_company") as mock_matcher:
                result = backfill_import(attio, pb_csv, export_csv)

            assert result["failed"] == 1
            assert result["linked"] == 0
            mock_matcher.assert_not_called()


# ── PR-225: find_company_record + industry_status stamping ──────────────────

class TestFindCompanyRecord:
    def test_domain_match_returns_full_record(self):
        from workflows.company_matcher import find_company_record
        attio = MagicMock()
        rec = {"id": {"record_id": "c1"}, "values": {"name": [{"value": "Acme"}]}}
        attio.search_company_by_domain.return_value = rec
        assert find_company_record(attio, "Acme", "acme.com") is rec

    def test_name_match_returns_full_record(self):
        from workflows.company_matcher import find_company_record
        attio = MagicMock()
        attio.search_company_by_domain.return_value = None
        rec = {"id": {"record_id": "c2"}, "values": {"name": [{"value": "Acme Foods"}]}}
        attio.search_companies.return_value = [rec]
        assert find_company_record(attio, "Acme Foods") is rec

    def test_nameless_shell_company_degrades_to_no_match(self):
        # A name-less shell company (import artifact) returned by the name
        # filter must degrade to no-match, not raise IndexError.
        from workflows.company_matcher import find_company_record
        attio = MagicMock()
        attio.search_company_by_domain.return_value = None
        attio.search_companies.return_value = [{"id": {"record_id": "c3"}, "values": {"name": []}}]
        assert find_company_record(attio, "Acme Foods") is None

    def test_empty_name_returns_none(self):
        from workflows.company_matcher import find_company_record
        assert find_company_record(MagicMock(), "") is None


class TestMatchOrCreateIndustryStatus:
    def test_create_stamps_classifier_payload_when_status_given(self):
        attio = MagicMock()
        attio.search_company_by_domain.return_value = None
        attio.search_companies.return_value = []
        attio.create_company.return_value = {"id": {"record_id": "new-1"}}
        match_or_create_company(
            attio, "Acme", domain="acme.com",
            industry_vertical="Food & Beverage", industry_status="low_confidence",
        )
        attrs = attio.create_company.call_args[0][0]
        assert attrs["industry_vertical"] == "Food & Beverage"
        assert attrs["industry_vertical_status"] == "low_confidence"
        assert attrs["industry_source"] == "haiku_classifier"
        assert attrs["industry_vertical_confidence"] == 0.0

    def test_create_plain_vertical_without_status(self):
        attio = MagicMock()
        attio.search_company_by_domain.return_value = None
        attio.search_companies.return_value = []
        attio.create_company.return_value = {"id": {"record_id": "new-2"}}
        match_or_create_company(
            attio, "Acme", industry_vertical="Food & Beverage",
        )
        attrs = attio.create_company.call_args[0][0]
        assert attrs["industry_vertical"] == "Food & Beverage"
        assert "industry_vertical_status" not in attrs


class TestBuildClassifierPayload:
    def test_low_confidence_carries_zero_confidence(self):
        from workflows.industry_classifier import build_classifier_payload
        p = build_classifier_payload("Food & Beverage")
        assert p["industry_vertical"] == "Food & Beverage"
        assert p["industry_vertical_status"] == "low_confidence"
        assert p["industry_vertical_confidence"] == 0.0
        assert p["industry_source"] == "haiku_classifier"

    def test_confirmed_omits_confidence(self):
        from workflows.industry_classifier import build_classifier_payload
        p = build_classifier_payload("Food & Beverage", status="confirmed")
        assert p["industry_vertical_status"] == "confirmed"
        assert "industry_vertical_confidence" not in p
