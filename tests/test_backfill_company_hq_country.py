"""Tests for scripts/backfill_company_hq_country.py.

Pins: TLD suggestion conservatism, apply-mode validation + natural-filter
idempotency + write-verify fail-loud, and report CSV shape.
"""

import csv
from unittest.mock import MagicMock

from scripts.backfill_company_hq_country import (
    _company_ref_from_entry,
    _suggest_from_domain,
    run_apply,
    run_report,
)


class TestSuggestFromDomain:
    def test_country_tld_suggests(self):
        assert _suggest_from_domain("empresa.mx") == "MX"
        assert _suggest_from_domain("firma.com.br") == "BR"

    def test_gtld_suggests_nothing(self):
        assert _suggest_from_domain("acme.com") == ""
        assert _suggest_from_domain("globex.org") == ""

    def test_empty_domain(self):
        assert _suggest_from_domain("") == ""


class TestCompanyRefFromEntry:
    def test_extracts_target_record_id(self):
        entry = {"entry_values": {"company": [{"target_record_id": "c-1"}]}}
        assert _company_ref_from_entry(entry) == "c-1"

    def test_no_company_ref(self):
        assert _company_ref_from_entry({"entry_values": {}}) == ""


def _apply_csv(tmp_path, rows):
    path = tmp_path / "curated.csv"
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["company_id", "company_name", "country_code"]
        )
        writer.writeheader()
        writer.writerows(rows)
    return str(path)


class TestRunApply:
    def _attio(self, existing=None, readback="MX"):
        attio = MagicMock()
        attio.company_hq_country_code.side_effect = (
            # First call = idempotency check, second = post-write verify.
            [existing, readback]
        )
        attio._company_hq_country_cache = {}
        return attio

    def test_applies_and_verifies(self, tmp_path):
        attio = self._attio(existing=None, readback="MX")
        rc = run_apply(attio, _apply_csv(tmp_path, [
            {"company_id": "c-1", "company_name": "Acme", "country_code": "mx"},
        ]))
        assert rc == 0
        # Write shape = the array-of-objects shape every read site parses.
        attio.update_company.assert_called_once_with(
            "c-1", {"hq_country_code": [{"country_code": "MX"}]}
        )
        attio.invalidate_company_hq_country.assert_called_once_with("c-1")

    def test_existing_value_never_overwritten(self, tmp_path):
        attio = self._attio(existing="US")
        rc = run_apply(attio, _apply_csv(tmp_path, [
            {"company_id": "c-1", "company_name": "Acme", "country_code": "MX"},
        ]))
        assert rc == 0
        attio.update_company.assert_not_called()

    def test_blank_country_code_skipped(self, tmp_path):
        attio = self._attio()
        rc = run_apply(attio, _apply_csv(tmp_path, [
            {"company_id": "c-1", "company_name": "Acme", "country_code": ""},
        ]))
        assert rc == 0
        attio.update_company.assert_not_called()

    def test_invalid_code_is_exit_1(self, tmp_path):
        attio = self._attio()
        rc = run_apply(attio, _apply_csv(tmp_path, [
            {"company_id": "c-1", "company_name": "Acme", "country_code": "MEX"},
        ]))
        assert rc == 1
        attio.update_company.assert_not_called()

    def test_verify_mismatch_is_exit_1(self, tmp_path):
        # Write "landed" per the API but reads back None through the
        # guard's getter — wrong value shape. Must fail loud.
        attio = self._attio(existing=None, readback=None)
        rc = run_apply(attio, _apply_csv(tmp_path, [
            {"company_id": "c-1", "company_name": "Acme", "country_code": "MX"},
        ]))
        assert rc == 1

    def test_http_write_failure_is_exit_1_and_aborts_batch(self, tmp_path):
        # First write 400s with zero prior successes → writability-probe
        # abort: the second curated row must never be attempted.
        import httpx

        attio = MagicMock()
        attio.company_hq_country_code.return_value = None
        attio._company_hq_country_cache = {}
        attio.update_company.side_effect = httpx.HTTPStatusError(
            "400", request=MagicMock(), response=MagicMock()
        )
        rc = run_apply(attio, _apply_csv(tmp_path, [
            {"company_id": "c-1", "company_name": "A", "country_code": "MX"},
            {"company_id": "c-2", "company_name": "B", "country_code": "BR"},
        ]))
        assert rc == 1
        assert attio.update_company.call_count == 1

    def test_suggestion_column_never_auto_applies(self, tmp_path):
        # A row with suggested_country_code filled but country_code blank
        # must be SKIPPED — the suggestion is curation input, not a value.
        path = tmp_path / "curated.csv"
        with path.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=[
                "company_id", "company_name", "domain", "prospect_count",
                "suggested_country_code", "country_code",
            ])
            writer.writeheader()
            writer.writerow({
                "company_id": "c-1", "company_name": "Acme",
                "domain": "acme.mx", "prospect_count": "3",
                "suggested_country_code": "MX", "country_code": "",
            })
        attio = MagicMock()
        attio._company_hq_country_cache = {}
        rc = run_apply(attio, str(path))
        assert rc == 0
        attio.update_company.assert_not_called()


class TestRunReport:
    def test_report_lists_only_missing_hq(self, tmp_path):
        def _entry(record_id, stage, company_id):
            return {
                "id": {"entry_id": f"e-{record_id}", "record_id": record_id},
                "parent_record_id": record_id,
                "entry_values": {
                    "stage": [{"status": {"title": stage}}],
                    "company": [{"target_record_id": company_id}],
                },
            }

        attio = MagicMock()
        attio.query_list_entries.return_value = [
            _entry("r1", "Accepted", "c-has"),
            _entry("r2", "DM1 Sent", "c-missing"),
            _entry("r3", "Prospect", "c-ignored"),  # not an active DM stage
        ]
        # Person-record company resolution: r1/r2 resolve through the
        # production _person_to_company path; empty bulk fetch keeps the
        # RecordCache path inert (fixtures also carry the entry_values
        # fallback ref).
        attio.bulk_fetch_persons_by_record_ids.return_value = {}
        attio._person_to_company = {"r1": "c-has", "r2": "c-missing"}
        attio.extract_record_info.return_value = (None, None, "", None, "")
        # parse_entry is NOT patched — the fixtures use the real Attio
        # entry shape so the real _extract_stage path is exercised and the
        # ACTIVE_DM_STAGES frozenset is pinned against the real parser.

        def _get_company(cid):
            return {
                "c-has": {"values": {
                    "name": [{"value": "Has"}],
                    "domains": [{"domain": "has.mx"}],
                    "hq_country_code": [{"country_code": "MX"}],
                }},
                "c-missing": {"values": {
                    "name": [{"value": "Missing"}],
                    "domains": [{"domain": "missing.com.br"}],
                }},
            }[cid]

        attio.get_company.side_effect = _get_company

        out_path = tmp_path / "report.csv"
        rc = run_report(attio, "list-1", limit=0, out_path=out_path)
        assert rc == 0
        with out_path.open() as fh:
            rows = list(csv.DictReader(fh))
        ids = {r["company_id"] for r in rows}
        assert "c-missing" in ids
        assert "c-has" not in ids
        missing_row = next(r for r in rows if r["company_id"] == "c-missing")
        assert missing_row["suggested_country_code"] == "BR"
        assert missing_row["country_code"] == ""
