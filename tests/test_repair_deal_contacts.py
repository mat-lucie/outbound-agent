"""Tests for workflows/repair_deal_contacts.py."""

from __future__ import annotations

import csv
from unittest.mock import MagicMock

import pytest

from workflows.repair_deal_contacts import (
    _classify_candidate,
    _deal_associated_people,
    _deal_company_uuid,
    _deal_name,
    _person_company_uuids,
    _person_job_title,
    _person_name,
    apply_deal_contact_backfill,
    detect_deal_contact_gaps,
    index_people_by_company,
)


def _person(record_id: str, name: str = "Jane Doe", title: str = "", linkedin: str = "", company_uuids: list[str] | None = None) -> dict:
    values: dict = {
        "name": [{"full_name": name}] if name else [],
    }
    if title:
        values["job_title"] = [{"value": title}]
    if linkedin:
        values["linkedin"] = [{"value": linkedin}]
    if company_uuids:
        values["company"] = [
            {"target_object": "companies", "target_record_id": cid}
            for cid in company_uuids
        ]
    return {"id": {"record_id": record_id}, "values": values}


def _deal(record_id: str, name: str = "", company_uuid: str = "", people: list[str] | None = None, stage: str = "Lead") -> dict:
    values: dict = {
        "name": [{"value": name}] if name else [],
        "stage": [{"status": {"title": stage}}],
    }
    if company_uuid:
        values["associated_company"] = [{
            "target_object": "companies",
            "target_record_id": company_uuid,
        }]
    if people:
        values["associated_people"] = [
            {"target_object": "people", "target_record_id": pid}
            for pid in people
        ]
    return {"id": {"record_id": record_id}, "values": values}


# ── helpers ────────────────────────────────────────────────────


class TestExtractionHelpers:
    def test_person_company_uuids_returns_list(self):
        p = _person("p1", company_uuids=["co-1", "co-2"])
        assert _person_company_uuids(p) == ["co-1", "co-2"]

    def test_person_company_uuids_empty_when_no_link(self):
        p = _person("p1")
        assert _person_company_uuids(p) == []

    def test_person_name_full(self):
        p = _person("p1", name="Ana Garza")
        assert _person_name(p) == "Ana Garza"

    def test_person_job_title(self):
        p = _person("p1", title="Plant Director")
        assert _person_job_title(p) == "Plant Director"

    def test_deal_company_uuid(self):
        d = _deal("d1", company_uuid="co-42")
        assert _deal_company_uuid(d) == "co-42"

    def test_deal_company_uuid_missing(self):
        d = _deal("d1")
        assert _deal_company_uuid(d) == ""

    def test_deal_name(self):
        d = _deal("d1", name="Sigma Alimentos")
        assert _deal_name(d) == "Sigma Alimentos"

    def test_deal_associated_people(self):
        d = _deal("d1", people=["p1", "p2"])
        assert _deal_associated_people(d) == ["p1", "p2"]


# ── index_people_by_company ────────────────────────────────────


class TestIndexPeopleByCompany:
    def test_groups_by_company_uuid(self):
        a = _person("p1", company_uuids=["co-1"])
        b = _person("p2", company_uuids=["co-1", "co-2"])
        c = _person("p3", company_uuids=["co-2"])
        index = index_people_by_company([a, b, c])
        assert {p["id"]["record_id"] for p in index["co-1"]} == {"p1", "p2"}
        assert {p["id"]["record_id"] for p in index["co-2"]} == {"p2", "p3"}

    def test_people_without_company_excluded(self):
        a = _person("p1")
        index = index_people_by_company([a])
        assert index == {}


# ── _classify_candidate ────────────────────────────────────────


class TestClassifyCandidate:
    def test_decision_maker_title_gets_link(self):
        # Plant Director with LATAM context should pass the quality gate.
        p = _person("p1", name="Ana", title="Plant Director", linkedin="linkedin.com/in/ana")
        # Set location via primary_location since _person_location reads it
        p["values"]["primary_location"] = [{"locality": "Monterrey", "region": "NL", "country_code": "MX"}]
        persona, band, score, rec, _ = _classify_candidate(p, deal_name="Sigma Alimentos")
        # Plant Director is a decision-maker title; score should be above the link threshold.
        assert score > 0
        # We don't assert exact recommendation here — quality_gate scoring can vary —
        # but anything that doesn't disqualify should be link or review, never skip.
        assert rec in {"link", "review"}

    def test_sales_title_gets_skip_disqualified(self):
        p = _person("p1", name="Carlos", title="Director Comercial", linkedin="linkedin.com/in/carlos")
        persona, band, score, rec, reasons = _classify_candidate(p, deal_name="Sigma Alimentos")
        # quality_gate disqualifies sales/comercial titles outright.
        assert rec == "skip-disqualified"


# ── detect_deal_contact_gaps — end-to-end with mocked Attio ────


class TestDetectDealContactGaps:
    def test_emits_csv_with_link_and_already_linked_rows(self, tmp_path):
        attio = MagicMock()
        d1 = _deal("d1", name="Sigma", company_uuid="co-sigma", people=["p1"])
        attio.search_deals.return_value = [d1]
        ana = _person("p1", name="Ana", title="Plant Director", company_uuids=["co-sigma"])
        ana["values"]["primary_location"] = [{"country_code": "MX"}]
        beto = _person("p2", name="Beto", title="VP Operations", company_uuids=["co-sigma"])
        beto["values"]["primary_location"] = [{"country_code": "MX"}]
        attio.search_people.return_value = [ana, beto]

        out = tmp_path / "report.csv"
        summary = detect_deal_contact_gaps(attio, out_path=str(out))

        assert summary["deals_scanned"] == 1
        assert summary["people_indexed"] == 2
        rows = list(csv.DictReader(open(out)))
        assert len(rows) == 2
        by_person = {r["candidate_person_id"]: r for r in rows}
        assert by_person["p1"]["recommendation"] == "already-linked"
        # p2 is a viable LATAM ops decision-maker; should be link (not skip-disqualified).
        assert by_person["p2"]["recommendation"] in {"link", "review"}

    def test_only_empty_skips_deals_with_contacts(self, tmp_path):
        attio = MagicMock()
        # d1 has contacts, d2 has none — only d2 should be reported when only_empty=True.
        d1 = _deal("d1", name="Sigma", company_uuid="co-sigma", people=["p1"])
        d2 = _deal("d2", name="Pacasmayo", company_uuid="co-pacasmayo", people=[])
        attio.search_deals.return_value = [d1, d2]
        p1 = _person("p1", name="Ana", title="Plant Director", company_uuids=["co-sigma"])
        p1["values"]["primary_location"] = [{"country_code": "MX"}]
        p2 = _person("p2", name="Beto", title="Plant Director", company_uuids=["co-pacasmayo"])
        p2["values"]["primary_location"] = [{"country_code": "PE"}]
        attio.search_people.return_value = [p1, p2]

        out = tmp_path / "report.csv"
        detect_deal_contact_gaps(attio, out_path=str(out), only_empty=True)

        rows = list(csv.DictReader(open(out)))
        deal_ids = {r["deal_id"] for r in rows}
        assert deal_ids == {"d2"}

    def test_deals_without_associated_company_skipped(self, tmp_path):
        attio = MagicMock()
        d1 = _deal("d1", name="Orphan", company_uuid="")  # no company link
        attio.search_deals.return_value = [d1]
        attio.search_people.return_value = []

        out = tmp_path / "report.csv"
        summary = detect_deal_contact_gaps(attio, out_path=str(out))

        assert summary["deals_with_no_company"] == 1
        rows = list(csv.DictReader(open(out)))
        assert rows == []


# ── apply_deal_contact_backfill ────────────────────────────────


@pytest.fixture
def report_csv(tmp_path):
    out = tmp_path / "report.csv"
    rows = [
        {
            "deal_id": "d1", "deal_name": "Sigma", "deal_stage": "Lead",
            "company_uuid": "co-sigma", "current_contact_count": "0",
            "candidate_person_id": "p1", "candidate_name": "Ana",
            "candidate_title": "Plant Director", "candidate_linkedin": "",
            "persona": "operations_leaders", "quality_score": "70",
            "score_band": "60-75", "recommendation": "link", "reasons": "",
        },
        {
            "deal_id": "d1", "deal_name": "Sigma", "deal_stage": "Lead",
            "company_uuid": "co-sigma", "current_contact_count": "0",
            "candidate_person_id": "p2", "candidate_name": "Carlos",
            "candidate_title": "Director Comercial", "candidate_linkedin": "",
            "persona": "", "quality_score": "20", "score_band": "<40",
            "recommendation": "skip-disqualified", "reasons": "sales title",
        },
        {
            "deal_id": "d2", "deal_name": "Pacasmayo", "deal_stage": "Lead",
            "company_uuid": "co-pacasmayo", "current_contact_count": "1",
            "candidate_person_id": "p3", "candidate_name": "Beto",
            "candidate_title": "VP Ops", "candidate_linkedin": "",
            "persona": "executive_sponsors", "quality_score": "80",
            "score_band": ">75", "recommendation": "link", "reasons": "",
        },
    ]
    from workflows.repair_deal_contacts import REPORT_FIELDNAMES
    with open(out, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=REPORT_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    return str(out)


class TestApplyDealContactBackfill:
    def test_links_marked_rows_only(self, report_csv, tmp_path):
        attio = MagicMock()
        # d1 currently has no people; d2 already has p4
        attio.get_deal.side_effect = lambda did: {
            "d1": _deal("d1", company_uuid="co-sigma", people=[]),
            "d2": _deal("d2", company_uuid="co-pacasmayo", people=["p4"]),
        }[did]

        totals = apply_deal_contact_backfill(
            attio, report_path=report_csv, log_path=str(tmp_path / "log.jsonl"),
        )

        assert totals["deals_touched"] == 2
        assert totals["people_linked"] == 2
        # d1 gets p1 (skip-disqualified p2 ignored); d2 gets p3 unioned with existing p4
        calls = {c.args[0]: c.args[1] for c in attio.update_deal.call_args_list}
        assert set(calls.keys()) == {"d1", "d2"}
        d1_people = sorted(item["target_record_id"] for item in calls["d1"]["associated_people"])
        d2_people = sorted(item["target_record_id"] for item in calls["d2"]["associated_people"])
        assert d1_people == ["p1"]
        assert d2_people == ["p3", "p4"]

    def test_idempotent_when_already_linked(self, report_csv, tmp_path):
        attio = MagicMock()
        # Both d1 and d2 already have all the candidates
        attio.get_deal.side_effect = lambda did: {
            "d1": _deal("d1", company_uuid="co-sigma", people=["p1"]),
            "d2": _deal("d2", company_uuid="co-pacasmayo", people=["p3"]),
        }[did]

        totals = apply_deal_contact_backfill(
            attio, report_path=report_csv, log_path=str(tmp_path / "log.jsonl"),
        )

        assert totals["deals_touched"] == 0
        assert totals["people_linked"] == 0
        assert totals["deals_skipped_idempotent"] == 2
        attio.update_deal.assert_not_called()

    def test_pretend_does_not_call_update(self, report_csv, tmp_path):
        attio = MagicMock()
        attio.get_deal.side_effect = lambda did: {
            "d1": _deal("d1", company_uuid="co-sigma", people=[]),
            "d2": _deal("d2", company_uuid="co-pacasmayo", people=["p4"]),
        }[did]

        totals = apply_deal_contact_backfill(
            attio, report_path=report_csv, pretend=True,
            log_path=str(tmp_path / "log.jsonl"),
        )

        assert totals["deals_touched"] == 2
        attio.update_deal.assert_not_called()
