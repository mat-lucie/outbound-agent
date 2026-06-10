"""Tests for the configurable ``field_mapping`` consumed by ``AttioClient``.

Commit 2 of the CRM-mapping wiring increment makes three documented engine
fields point at workspace-renamed Attio attribute slugs:

  * ``linkedin_url``    -> ``linkedin``  (the PERSON's LinkedIn profile slug)
  * ``full_name``       -> ``name``      (the PERSON's name slug)
  * ``company_domain``  -> ``domains``   (the COMPANY's web-domain slug)

Two halves are pinned here:

  * GOLDEN/identity — under the default mapping every routed slug resolves to
    its canonical Attio literal, so reads/filters are byte-identical to the
    pre-seam code. This is the behavior-preservation contract that keeps the
    whole existing suite green.
  * REMAP — with an operator's renamed slugs, the reads/filters follow the
    rename, EXCEPT the company-``name`` read, which has no documented mapping
    key and therefore stays the literal ``"name"``.
"""

from __future__ import annotations

from unittest.mock import patch

from clients.attio import AttioClient, is_linkedin_clearbit_corrupted


def _client(field_mapping=None) -> AttioClient:
    """Construct a real AttioClient (no network — ``search_*`` are patched per
    test). ``ATTIO_API_KEY`` is delenv'd by conftest, so pass one explicitly."""
    return AttioClient(api_key="test-key", field_mapping=field_mapping)


# ── GOLDEN: the default mapping resolves to the canonical Attio slugs ────────


class TestDefaultFieldSlugResolution:
    def test_documented_fields_resolve_to_canonical_slugs(self) -> None:
        client = _client()
        assert client._field_slug("linkedin_url") == "linkedin"
        assert client._field_slug("full_name") == "name"
        assert client._field_slug("company_domain") == "domains"

    def test_unmapped_field_passes_through(self) -> None:
        client = _client()
        assert client._field_slug("job_title") == "job_title"
        assert client._field_slug("anything_else") == "anything_else"

    def test_none_field_mapping_defaults_to_canonical(self) -> None:
        # Explicit None and omitted both yield the canonical Attio map.
        assert _client(field_mapping=None)._field_mapping == {
            "linkedin_url": "linkedin",
            "full_name": "name",
            "company_domain": "domains",
        }


class TestDefaultSearchFilters:
    def test_search_person_by_linkedin_filters_on_literal_linkedin(self) -> None:
        client = _client()
        with patch.object(client, "search_people", return_value=[]) as mock_sp:
            client.search_person_by_linkedin("https://linkedin.com/in/ada")
        # First variant attempt; the filter key is the canonical literal slug.
        first_call = mock_sp.call_args_list[0]
        assert "linkedin" in first_call.kwargs["filter_"]

    def test_search_company_by_domain_filters_on_literal_domains(self) -> None:
        client = _client()
        with patch.object(client, "search_companies", return_value=[]) as mock_sc:
            client.search_company_by_domain("cementra.com")
        assert mock_sc.call_args.kwargs["filter_"] == {"domains": "cementra.com"}


class TestDefaultExtractRecordInfo:
    PERSON_RAW = {
        "id": {"record_id": "rec-1"},
        "values": {
            "name": [{"first_name": "Ada", "last_name": "Lovelace"}],
            "job_title": [{"value": "Plant Manager"}],
            "linkedin": [{"value": "https://linkedin.com/in/ada"}],
        },
    }

    def test_reads_person_name_and_linkedin_from_canonical_slugs(self) -> None:
        client = _client()
        name, company, linkedin, industry, title = client.extract_record_info(
            self.PERSON_RAW
        )
        assert name == "Ada Lovelace"
        assert linkedin == "https://linkedin.com/in/ada"
        assert title == "Plant Manager"
        assert company is None
        assert industry is None


# ── REMAP: operator-renamed slugs are honored ───────────────────────────────

_REMAP = {
    "linkedin_url": "linkedin_profile",
    "full_name": "full_name_attr",
    "company_domain": "website",
}


class TestRemappedFieldSlugResolution:
    def test_documented_fields_resolve_to_renamed_slugs(self) -> None:
        client = _client(_REMAP)
        assert client._field_slug("linkedin_url") == "linkedin_profile"
        assert client._field_slug("full_name") == "full_name_attr"
        assert client._field_slug("company_domain") == "website"

    def test_unmapped_field_still_passes_through(self) -> None:
        client = _client(_REMAP)
        assert client._field_slug("job_title") == "job_title"


class TestRemappedSearchFilters:
    def test_search_person_by_linkedin_filters_on_renamed_slug(self) -> None:
        client = _client(_REMAP)
        with patch.object(client, "search_people", return_value=[]) as mock_sp:
            client.search_person_by_linkedin("https://linkedin.com/in/ada")
        first_call = mock_sp.call_args_list[0]
        assert "linkedin_profile" in first_call.kwargs["filter_"]
        assert "linkedin" not in first_call.kwargs["filter_"]

    def test_search_company_by_domain_filters_on_renamed_slug(self) -> None:
        client = _client(_REMAP)
        with patch.object(client, "search_companies", return_value=[]) as mock_sc:
            client.search_company_by_domain("cementra.com")
        assert mock_sc.call_args.kwargs["filter_"] == {"website": "cementra.com"}


class TestRemappedExtractRecordInfo:
    def test_reads_person_name_and_linkedin_from_renamed_slugs(self) -> None:
        client = _client(_REMAP)
        raw = {
            "id": {"record_id": "rec-1"},
            "values": {
                "full_name_attr": [{"first_name": "Ada", "last_name": "Lovelace"}],
                "job_title": [{"value": "Plant Manager"}],
                "linkedin_profile": [{"value": "https://linkedin.com/in/ada"}],
            },
        }
        name, _company, linkedin, _industry, title = client.extract_record_info(raw)
        assert name == "Ada Lovelace"
        assert linkedin == "https://linkedin.com/in/ada"
        assert title == "Plant Manager"

    def test_canonical_slugs_are_invisible_under_remap(self) -> None:
        # Same payload but stored under the CANONICAL slugs: with a remap the
        # client looks at the renamed slugs, so it reads nothing.
        client = _client(_REMAP)
        raw = {
            "id": {"record_id": "rec-1"},
            "values": {
                "name": [{"first_name": "Ada", "last_name": "Lovelace"}],
                "linkedin": [{"value": "https://linkedin.com/in/ada"}],
            },
        }
        name, _company, linkedin, _industry, _title = client.extract_record_info(raw)
        assert name is None
        assert linkedin == ""

    def test_company_name_stays_literal_under_remap(self) -> None:
        # The linked company's name is read from the LITERAL "name" even under a
        # remap (no documented field_mapping key for company name). We drive the
        # company fetch via a patched _request and assert the resolved company.
        client = _client(_REMAP)
        person_raw = {
            "id": {"record_id": "rec-1"},
            "values": {
                "full_name_attr": [{"first_name": "Ada", "last_name": "Lovelace"}],
                "company": [
                    {"target_object": "companies", "target_record_id": "co-9"}
                ],
            },
        }
        company_payload = {
            "data": {
                "values": {
                    "name": [{"value": "Cementra"}],
                    "industry_vertical": [
                        {"option": {"title": "Construction"}}
                    ],
                }
            }
        }
        with patch.object(client, "_request", return_value=company_payload):
            name, company, _linkedin, industry, _title = client.extract_record_info(
                person_raw
            )
        assert name == "Ada Lovelace"
        assert company == "Cementra"
        assert industry == "Construction"


# ── Corruption detector slug routing ─────────────────────────────────────────


class TestCorruptionDetectorSlugRouting:
    def test_direct_call_reads_canonical_slugs(self) -> None:
        # Default (no field_slug) path — byte-identical to pre-seam: reads the
        # literal "domains" / "linkedin" / "name".
        record = {
            "values": {
                "name": [{"value": "Prolec GE"}],
                "domains": [{"domain": "linkedin.com"}],
            }
        }
        assert is_linkedin_clearbit_corrupted(record) is True

    def test_renamed_domain_slug_is_honored(self) -> None:
        resolver = {
            "company_domain": "website",
            "linkedin_url": "linkedin_profile",
        }.get
        # Corruption signal now lives under the renamed "website" slug.
        record = {
            "values": {
                "name": [{"value": "Prolec GE"}],
                "website": [{"domain": "linkedin.com"}],
            }
        }
        assert is_linkedin_clearbit_corrupted(record, lambda f: resolver(f, f)) is True
        # Under the literal canonical slug there's no corruption visible anymore.
        record_literal = {
            "values": {
                "name": [{"value": "Prolec GE"}],
                "domains": [{"domain": "linkedin.com"}],
            }
        }
        assert (
            is_linkedin_clearbit_corrupted(
                record_literal, lambda f: resolver(f, f)
            )
            is False
        )

    def test_renamed_linkedin_self_url_is_honored(self) -> None:
        resolver = {
            "company_domain": "website",
            "linkedin_url": "linkedin_profile",
        }.get
        record = {
            "values": {
                "name": [{"value": "Acme Foods"}],
                "website": [{"domain": "acme.com"}],
                "linkedin_profile": [
                    {"value": "https://linkedin.com/company/linkedin"}
                ],
            }
        }
        assert is_linkedin_clearbit_corrupted(record, lambda f: resolver(f, f)) is True

    def test_extract_record_info_threads_field_slug_into_corruption_cache(
        self,
    ) -> None:
        # End-to-end: a remapped client resolving a person whose linked company
        # carries the corruption fingerprint under renamed slugs flags it.
        client = _client(_REMAP)
        person_raw = {
            "id": {"record_id": "rec-1"},
            "values": {
                "full_name_attr": [{"first_name": "Ada", "last_name": "Lovelace"}],
                "company": [
                    {"target_object": "companies", "target_record_id": "co-9"}
                ],
            },
        }
        company_payload = {
            "data": {
                "values": {
                    "name": [{"value": "Prolec GE"}],
                    "website": [{"domain": "linkedin.com"}],
                }
            }
        }
        with patch.object(client, "_request", return_value=company_payload):
            client.extract_record_info(person_raw)
        assert client.is_person_company_corrupted("rec-1") is True
