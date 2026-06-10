"""Backfill missing contact ↔ deal links in Attio.

A pile of Lead-stage deals were bulk-imported on 2026-02-11 without contacts
attached, even though the corresponding people exist in the workspace and
point at the deal's company via `people.company`. Attio's `deals.associated_people`
join is not auto-derived — it has to be populated explicitly.

Two-phase flow mirroring backfill_companies / attio_dedup:

  detect → emit deal_contact_backfill_report.csv (one row per (deal, candidate person))
  apply  → PATCH each deal's associated_people with the rows marked `link`

Recommendation per (deal, candidate) row:
  link               — score_prospect passes (>= 40); link this person to the deal
  review             — score < 40 but no hard disqualifier; surface for the operator to decide
  skip-disqualified  — quality_gate marked the title disqualifying (sales/marketing/
                       comercial/consultant/global-exec-without-LATAM)
  already-linked     — person is already in deal.associated_people; no-op
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from scripts.attio_dedup import (
    deal_associated_people as _deal_associated_people,
)
from scripts.attio_dedup import (
    deal_company_id as _deal_company_uuid,
)
from scripts.attio_dedup import (
    deal_name_raw as _deal_name,
)
from scripts.attio_dedup import (
    deal_stage_title as _deal_stage,
)

if TYPE_CHECKING:
    from clients.attio import AttioClient

REPORT_FIELDNAMES = [
    "deal_id",
    "deal_name",
    "deal_stage",
    "company_uuid",
    "current_contact_count",
    "candidate_person_id",
    "candidate_name",
    "candidate_title",
    "candidate_linkedin",
    "persona",
    "quality_score",
    "score_band",
    "recommendation",
    "reasons",
]


# ── Helpers — read Attio shapes ─────────────────────────────────


def _first_value(values: dict, key: str) -> Any:
    arr = values.get(key, [])
    if not arr or not isinstance(arr, list):
        return None
    return arr[0]


def _person_company_uuids(person_record: dict) -> list[str]:
    """Extract company-reference target_record_ids from a person record.

    Attio stores the link on `company` (and sometimes legacy `primary_company`)
    as a list of record-reference dicts.
    """
    values = person_record.get("values", {})
    out: list[str] = []
    for key in ("company", "primary_company"):
        arr = values.get(key, [])
        if not arr or not isinstance(arr, list):
            continue
        for item in arr:
            if isinstance(item, dict) and item.get("target_record_id"):
                out.append(item["target_record_id"])
    return out


def _person_name(person_record: dict) -> str:
    name = _first_value(person_record.get("values", {}), "name")
    if isinstance(name, dict):
        full = name.get("full_name") or f"{name.get('first_name', '')} {name.get('last_name', '')}".strip()
        return str(full or "")
    return ""


def _person_job_title(person_record: dict) -> str:
    val = _first_value(person_record.get("values", {}), "job_title")
    if isinstance(val, dict):
        return str(val.get("value", "") or "")
    return str(val or "")


def _person_linkedin(person_record: dict) -> str:
    val = _first_value(person_record.get("values", {}), "linkedin")
    if isinstance(val, dict):
        return str(val.get("value", "") or "")
    return str(val or "")


def _person_location(person_record: dict) -> str:
    val = _first_value(person_record.get("values", {}), "location")
    if isinstance(val, dict):
        return str(val.get("value", "") or "")
    val2 = _first_value(person_record.get("values", {}), "primary_location")
    if isinstance(val2, dict):
        # locality + region + country
        parts = [
            val2.get("locality", ""),
            val2.get("region", ""),
            val2.get("country_code", ""),
        ]
        return ", ".join(p for p in parts if p)
    return str(val or "")


# ── Index + score ──────────────────────────────────────────────


def index_people_by_company(people: list[dict]) -> dict[str, list[dict]]:
    """Build {company_uuid: [person_record, …]} from a flat people list."""
    index: dict[str, list[dict]] = defaultdict(list)
    for p in people:
        for cuuid in _person_company_uuids(p):
            index[cuuid].append(p)
    return index


def _classify_candidate(
    person_record: dict, deal_name: str
) -> tuple[str, str, float, str, str]:
    """Run quality_gate.score_prospect against a candidate person.

    Returns (persona, score_band, score, recommendation, reasons_joined).
    Rule-based only — no LLM call.
    """
    from workflows.quality_gate import score_band as _band_fn
    from workflows.quality_gate import score_prospect

    prospect_data = {
        "name": _person_name(person_record),
        "title": _person_job_title(person_record),
        "company": deal_name,
        "location": _person_location(person_record),
        "linkedin_url": _person_linkedin(person_record),
        "employee_count": "",
        "industry": "",
    }
    result = score_prospect(prospect_data)
    score = float(result.get("score", 0) or 0)
    persona = str(result.get("persona", "") or "")
    band = _band_fn(score) or ""
    reasons = "; ".join(str(r) for r in result.get("reasons", []))

    # Recommendation logic.
    # score_prospect returns pass=False when quality_gate disqualifiers fire
    # (sales/marketing/comercial titles, global-exec-without-LATAM, etc.).
    # Treat those as skip-disqualified.
    if not result.get("pass", False) and score < 40:
        recommendation = "skip-disqualified"
    elif score >= 40:
        recommendation = "link"
    else:
        recommendation = "review"
    return persona, band, score, recommendation, reasons


# ── Detect (scan) ──────────────────────────────────────────────


def detect_deal_contact_gaps(
    attio: AttioClient,
    out_path: str = "deal_contact_backfill_report.csv",
    only_empty: bool = False,
) -> dict:
    """Scan deals + people; emit candidate (deal, person) pairs to a CSV.

    only_empty: when True, restrict to deals where associated_people is empty.
    Default False so we also surface additional candidates for partially-linked
    deals.
    """
    deals = attio.search_deals(filter_=None, limit=10000)
    people = attio.search_people(filter_=None, limit=50000)
    people_by_company = index_people_by_company(people)

    rows: list[dict] = []
    skipped_no_company = 0
    skipped_already_full = 0

    for deal in deals:
        company_uuid = _deal_company_uuid(deal)
        if not company_uuid:
            skipped_no_company += 1
            continue
        existing = set(_deal_associated_people(deal))
        candidates = people_by_company.get(company_uuid, [])
        if only_empty and existing:
            continue
        if not candidates:
            continue

        deal_id = deal.get("id", {}).get("record_id", "")
        deal_name = _deal_name(deal)
        deal_stage = _deal_stage(deal)
        current_count = len(existing)

        for person in candidates:
            person_id = person.get("id", {}).get("record_id", "")
            if not person_id:
                continue
            if person_id in existing:
                # surface as already-linked so the report is complete + auditable
                rows.append({
                    "deal_id": deal_id,
                    "deal_name": deal_name,
                    "deal_stage": deal_stage,
                    "company_uuid": company_uuid,
                    "current_contact_count": current_count,
                    "candidate_person_id": person_id,
                    "candidate_name": _person_name(person),
                    "candidate_title": _person_job_title(person),
                    "candidate_linkedin": _person_linkedin(person),
                    "persona": "",
                    "quality_score": "",
                    "score_band": "",
                    "recommendation": "already-linked",
                    "reasons": "",
                })
                continue

            persona, band, score, rec, reasons = _classify_candidate(person, deal_name)
            rows.append({
                "deal_id": deal_id,
                "deal_name": deal_name,
                "deal_stage": deal_stage,
                "company_uuid": company_uuid,
                "current_contact_count": current_count,
                "candidate_person_id": person_id,
                "candidate_name": _person_name(person),
                "candidate_title": _person_job_title(person),
                "candidate_linkedin": _person_linkedin(person),
                "persona": persona,
                "quality_score": f"{score:.0f}",
                "score_band": band,
                "recommendation": rec,
                "reasons": reasons,
            })

    # Sort: deals with 0 contacts first, then by deal_name, then by recommendation priority
    rec_order = {"link": 0, "review": 1, "skip-disqualified": 2, "already-linked": 3}
    rows.sort(key=lambda r: (
        int(r["current_contact_count"]),
        r["deal_name"] or "",
        rec_order.get(r["recommendation"], 99),
    ))

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=REPORT_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    summary = {
        "deals_scanned": len(deals),
        "people_indexed": len(people),
        "candidate_pairs": len(rows),
        "link": sum(1 for r in rows if r["recommendation"] == "link"),
        "review": sum(1 for r in rows if r["recommendation"] == "review"),
        "skip_disqualified": sum(1 for r in rows if r["recommendation"] == "skip-disqualified"),
        "already_linked": sum(1 for r in rows if r["recommendation"] == "already-linked"),
        "deals_with_no_company": skipped_no_company,
        "deals_already_full_skipped": skipped_already_full,
        "report_path": out_path,
    }
    return summary


# ── Apply ──────────────────────────────────────────────────────


def apply_deal_contact_backfill(
    attio: AttioClient,
    report_path: str,
    pretend: bool = False,
    log_path: str = "deal_contact_link_audit.jsonl",
) -> dict:
    """PATCH deals to add candidate_person_ids marked `link` in the report.

    Idempotent: a person already in deal.associated_people is skipped even if
    the report row says link. Each deal touched gets one PATCH carrying the
    full union (Attio replaces, not appends, multi-valued attrs).
    """
    import json

    rows: list[dict] = []
    with open(report_path, newline="") as f:
        for row in csv.DictReader(f):
            rows.append(row)

    to_link: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        if r.get("recommendation") == "link" and r.get("candidate_person_id"):
            to_link[r["deal_id"]].append(r["candidate_person_id"])

    totals = {
        "deals_touched": 0,
        "people_linked": 0,
        "deals_skipped_idempotent": 0,
        "errors": 0,
    }

    with open(log_path, "w") as fh:
        fh.write(json.dumps({"kind": "apply_run_start", "report": report_path, "pretend": pretend}) + "\n")
        for deal_id, candidate_ids in to_link.items():
            try:
                deal = attio.get_deal(deal_id)
                if deal is None:
                    fh.write(json.dumps({"kind": "deal_not_found", "deal_id": deal_id}) + "\n")
                    totals["errors"] += 1
                    continue
                existing = set(_deal_associated_people(deal))
                new_ids = [pid for pid in candidate_ids if pid not in existing]
                if not new_ids:
                    totals["deals_skipped_idempotent"] += 1
                    fh.write(json.dumps({
                        "kind": "skipped_idempotent",
                        "deal_id": deal_id,
                        "already_linked": sorted(existing),
                    }) + "\n")
                    continue

                union = sorted(existing | set(new_ids))
                payload = {
                    "associated_people": [
                        {"target_object": "people", "target_record_id": pid}
                        for pid in union
                    ]
                }
                if pretend:
                    fh.write(json.dumps({
                        "kind": "would_patch",
                        "deal_id": deal_id,
                        "added": new_ids,
                        "union": union,
                    }) + "\n")
                else:
                    attio.update_deal(deal_id, payload)
                    fh.write(json.dumps({
                        "kind": "patched",
                        "deal_id": deal_id,
                        "added": new_ids,
                        "union": union,
                    }) + "\n")
                totals["deals_touched"] += 1
                totals["people_linked"] += len(new_ids)
            except Exception as e:
                totals["errors"] += 1
                fh.write(json.dumps({
                    "kind": "error",
                    "deal_id": deal_id,
                    "error": f"{type(e).__name__}: {e}",
                }) + "\n")
        fh.write(json.dumps({"kind": "apply_run_end", **totals}) + "\n")

    return totals
