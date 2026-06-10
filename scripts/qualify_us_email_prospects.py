"""Qualify USA discrete/hybrid manufacturing prospects for the email drip.

Outputs a CSV-ready list of Attio people who:
- Live in the US (person.primary_location.country_code == "US",
  falling back to company location when person has none)
- Belong to a company with industry_vertical in the discrete/hybrid set
- Have an email address and a first name
- Are NOT yet in the email campaign (email_campaign_stage is empty)
- Are NOT actively engaged in the LinkedIn pipeline (anti-collision)

Usage: python3 scripts/qualify_us_email_prospects.py
"""

from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from clients.attio import AttioClient  # noqa: E402
from models.pipeline import PipelineStage  # noqa: E402

DISCRETE_HYBRID = {
    "Manufacturing",
    "Automotive",
    "Packaging",
    "Food & Beverage",
    "Textiles",
    "Pharma",
}

LINKEDIN_ACTIVE = {
    PipelineStage.CONNECTION_SENT.value,
    PipelineStage.ACCEPTED.value,
    PipelineStage.DM1_SENT.value,
    PipelineStage.DM2_SENT.value,
    PipelineStage.DM3_SENT.value,
}


def main() -> None:
    attio = AttioClient(api_key=os.environ["ATTIO_API_KEY"])

    # 1. Pull every company with a qualifying industry_vertical.
    print("Fetching qualifying companies...", flush=True)
    qualifying_company_ids: dict[str, dict] = {}
    for industry in DISCRETE_HYBRID:
        offset = 0
        while True:
            page = attio._request(
                "POST",
                "/objects/companies/records/query",
                json={
                    "filter": {"industry_vertical": industry},
                    "limit": 500,
                    "offset": offset,
                },
            )
            data = page.get("data", [])
            if not data:
                break
            for c in data:
                cid = c["id"]["record_id"]
                vals = c.get("values", {})
                name = ""
                if vals.get("name"):
                    name = vals["name"][0].get("value", "")
                country = ""
                loc = vals.get("primary_location", [])
                if loc:
                    country = loc[0].get("country_code", "") or ""
                qualifying_company_ids[cid] = {
                    "name": name,
                    "country": country,
                    "industry": industry,
                }
            offset += len(data)
            if len(data) < 500:
                break
    print(f"  {len(qualifying_company_ids)} qualifying companies (any country).", flush=True)

    # 2. Build LinkedIn anti-collision set.
    print("Building LinkedIn anti-collision set...", flush=True)
    list_id = os.environ.get("ATTIO_LIST_ID", "")
    entries = attio.query_list_entries(list_id=list_id)
    li_collision: set[tuple[str, str, str]] = set()
    for entry in entries:
        attrs = AttioClient.parse_entry(entry)
        if attrs["stage"] not in LINKEDIN_ACTIVE:
            continue
        rec = attio.get_person(attrs["record_id"])
        if not rec:
            continue
        name, _company, _, _, _ = attio.extract_record_info(rec)
        # PR-14 (B-PD-006): `extract_record_info` now returns None for
        # missing name (was the literal "Unknown" pre-PR-14). The old
        # `name == "Unknown"` check became dead code after PR-14 and
        # let None fall through to .lower().split() → AttributeError.
        # Two-agent QA convergence (salesman-daily-QA + prospect-daily-QA)
        # caught this missed caller.
        if not name:
            continue
        parts = name.lower().split()
        first = parts[0] if parts else ""
        last = parts[-1] if len(parts) > 1 else ""
        vals = rec.get("values", {})
        email_data = vals.get("email_addresses", [])
        domain = ""
        if email_data:
            e = email_data[0]
            es = e.get("email_address", "") if isinstance(e, dict) else str(e)
            if "@" in es:
                domain = es.split("@")[-1].lower()
        if first:
            li_collision.add((first, last, domain.lower()))
    print(f"  {len(li_collision)} active LinkedIn contacts.", flush=True)

    # 3. Pull every person; filter to qualifying-company + USA + email + no-campaign-stage.
    print("Scanning people...", flush=True)
    qualified: list[dict] = []
    not_us = 0
    no_email = 0
    no_first_name = 0
    already_in_campaign = 0
    li_collisions = 0
    company_not_qualifying = 0

    offset = 0
    page_size = 500
    total_seen = 0
    while True:
        page = attio._request(
            "POST",
            "/objects/people/records/query",
            json={"limit": page_size, "offset": offset},
        )
        data = page.get("data", [])
        if not data:
            break
        for p in data:
            total_seen += 1
            vals = p.get("values", {})
            # Company link
            company_data = vals.get("company", [])
            if not company_data:
                company_not_qualifying += 1
                continue
            cid = company_data[0].get("target_record_id")
            if cid not in qualifying_company_ids:
                company_not_qualifying += 1
                continue
            company_meta = qualifying_company_ids[cid]

            # Email
            email_data = vals.get("email_addresses", [])
            email = ""
            if email_data:
                e = email_data[0]
                email = e.get("email_address", "") if isinstance(e, dict) else str(e)
            if not email:
                no_email += 1
                continue

            # Name
            name_data = vals.get("name", [])
            first_name = name_data[0].get("first_name", "") if name_data else ""
            last_name = name_data[0].get("last_name", "") if name_data else ""
            full_name = name_data[0].get("full_name", "") if name_data else ""
            if not first_name:
                no_first_name += 1
                continue

            # Country: person → company
            person_country = ""
            ploc = vals.get("primary_location", [])
            if ploc:
                person_country = ploc[0].get("country_code", "") or ""
            country = person_country or company_meta["country"]
            if country != "US":
                not_us += 1
                continue

            # Already in email campaign?
            stage_data = vals.get("email_campaign_stage", [])
            stage_val = ""
            if stage_data:
                item = stage_data[0]
                stage_val = item.get("value", "") if isinstance(item, dict) else str(item)
            if stage_val:
                already_in_campaign += 1
                continue

            # LinkedIn anti-collision
            domain = email.split("@")[-1].lower() if "@" in email else ""
            if (first_name.lower(), last_name.lower(), domain) in li_collision:
                li_collisions += 1
                continue

            title_data = vals.get("job_title", [])
            title = ""
            if title_data:
                t = title_data[0]
                title = t.get("value", "") if isinstance(t, dict) else str(t)

            persona_data = vals.get("persona", [])
            persona = ""
            if persona_data:
                pd = persona_data[0]
                if isinstance(pd, dict):
                    persona = pd.get("value", "") or ""
                    if not persona and isinstance(pd.get("option"), dict):
                        persona = pd["option"].get("title", "") or ""

            linkedin_data = vals.get("linkedin", [])
            linkedin = ""
            if linkedin_data:
                ld = linkedin_data[0]
                linkedin = ld.get("value", "") if isinstance(ld, dict) else str(ld)

            qualified.append({
                "name": full_name or f"{first_name} {last_name}".strip(),
                "first_name": first_name,
                "last_name": last_name,
                "email": email,
                "domain": domain,
                "title": title,
                "persona": persona,
                "linkedin": linkedin,
                "company": company_meta["name"],
                "industry": company_meta["industry"],
                "country": country,
            })
        offset += len(data)
        if len(data) < page_size:
            break

    print(f"  {total_seen} people scanned.", flush=True)
    print(f"  Excluded — not US: {not_us}", flush=True)
    print(f"  Excluded — no email: {no_email}", flush=True)
    print(f"  Excluded — no first_name: {no_first_name}", flush=True)
    print(f"  Excluded — already in campaign: {already_in_campaign}", flush=True)
    print(f"  Excluded — LinkedIn collision: {li_collisions}", flush=True)
    print(f"  Excluded — company not qualifying: {company_not_qualifying}", flush=True)
    print(f"  QUALIFIED: {len(qualified)}", flush=True)

    # Write CSV next to other exports
    out = ROOT / "exports" / "us_email_qualified_2026-05-05.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["name", "first_name", "last_name", "email", "domain", "title", "persona", "linkedin", "company", "industry", "country"],
        )
        w.writeheader()
        for row in qualified:
            w.writerow(row)
    print(f"\nWrote {out}", flush=True)

    from collections import Counter
    print("\nPersona distribution:")
    for k, v in Counter(r["persona"] or "(none)" for r in qualified).most_common():
        print(f"  {v:5d}  {k}")


if __name__ == "__main__":
    main()
