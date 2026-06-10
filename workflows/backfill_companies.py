"""Backfill company associations on existing Attio pipeline records."""

from __future__ import annotations

import csv
import os
import time
from datetime import date
from typing import TYPE_CHECKING

import click

from workflows.company_matcher import extract_real_domain, match_or_create_company

if TYPE_CHECKING:
    from clients.attio import AttioClient
    from clients.crm.base import CRMProvider
    from clients.phantombuster import PhantomBusterClient


def backfill_export(crm: CRMProvider) -> str:
    """Export LinkedIn URLs of pipeline records that have no company association.

    Writes a CSV suitable for feeding into PhantomBuster's LinkedIn Profile
    Scraper so that company data can be retrieved and later imported with
    backfill_import().

    Returns the path to the written CSV file.

    P1c migration note: this slice reads the CRM exclusively through the
    vendor-neutral ``CRMProvider`` contract (``query_list_entries`` →
    ``list[Entry]``, ``get_person`` → ``Record``, ``extract_person_info`` →
    ``RecordInfo``). The structured ``company`` record-reference check has no
    contract field, so it reads the untouched payload via ``Record.raw`` (the
    contract's documented escape hatch). ``extract_person_info`` returns a
    ``RecordInfo`` dataclass; we unpack the two fields this slice uses at the
    call boundary to keep the rest of the function byte-identical. Sibling
    write-path functions in this module still take a raw ``AttioClient`` — they
    migrate in a later increment.
    """
    entries = crm.query_list_entries(limit=500)

    total = len(entries)
    already_linked = 0
    no_linkedin = 0
    rows: list[dict] = []

    for entry in entries:
        record_id = entry.record_id
        if not record_id:
            continue

        person = crm.get_person(record_id)
        time.sleep(0.2)

        if person is None:
            continue

        # Skip records that already have a company record-reference linked.
        # The company reference is a structured multi-value slug the contract
        # does not model as a flat attribute, so read it off the untouched
        # vendor payload via Record.raw (the contract's escape hatch).
        company_data = person.raw.get("values", {}).get("company", [])
        if company_data and isinstance(company_data, list) and company_data[0].get("target_record_id"):
            already_linked += 1
            continue

        info = crm.extract_person_info(person)
        name, linkedin = info.name, info.linkedin_url
        if not linkedin:
            no_linkedin += 1
            continue

        rows.append({"linkedin_url": linkedin, "attio_record_id": record_id, "name": name})

    os.makedirs("exports", exist_ok=True)
    file_path = f"exports/backfill_companies_{date.today().isoformat()}.csv"

    with open(file_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["linkedin_url", "attio_record_id", "name"])
        writer.writeheader()
        writer.writerows(rows)

    click.echo("\nBackfill export complete:")
    click.echo(f"  Total entries:    {total}")
    click.echo(f"  Already linked:   {already_linked} (skipped)")
    click.echo(f"  No LinkedIn URL:  {no_linkedin} (skipped)")
    click.echo(f"  Exported:         {len(rows)}")
    click.echo(f"  File:             {file_path}")

    return file_path


def backfill_import(attio: AttioClient, pb_csv_path: str, export_csv_path: str) -> dict:
    """Read PhantomBuster Profile Scraper output and link each person to a company.

    pb_csv_path      -- path to the PhantomBuster CSV output
    export_csv_path  -- path to the CSV produced by backfill_export()

    Returns a summary dict: {processed, linked, created_new, failed, skipped}.
    """
    # Build lookup: normalized_linkedin_url -> attio_record_id
    def _normalize_url(url: str) -> str:
        url = url.strip().rstrip("/").lower()
        url = url.replace("://linkedin.com/", "://www.linkedin.com/")
        return url

    lookup: dict[str, str] = {}
    with open(export_csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            url = _normalize_url(row.get("linkedin_url", ""))
            rid = row.get("attio_record_id", "").strip()
            if url and rid:
                lookup[url] = rid

    counters = {"processed": 0, "linked": 0, "failed": 0, "skipped": 0}

    with open(pb_csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    for i, row in enumerate(rows):
        counters["processed"] += 1

        # Resolve LinkedIn URL from whichever column PB uses
        raw_url = row.get("linkedinProfileUrl", row.get("linkedInUrl", row.get("profileUrl", "")))
        norm_url = _normalize_url(raw_url)

        record_id = lookup.get(norm_url)
        if not record_id:
            counters["skipped"] += 1
            time.sleep(0.2)
            continue

        company_name = row.get("companyName", row.get("company", row.get("currentCompanyName", ""))).strip()
        if not company_name:
            counters["failed"] += 1
            time.sleep(0.2)
            continue

        domain = extract_real_domain(row)
        company_rid = match_or_create_company(attio, company_name, domain=domain or None)

        if company_rid:
            attio.update_person(
                record_id,
                {"company": [{"target_object": "companies", "target_record_id": company_rid}]},
            )
            counters["linked"] += 1
        else:
            counters["failed"] += 1

        time.sleep(0.2)

        if (i + 1) % 25 == 0:
            click.echo(
                f"  Progress: {i + 1}/{len(rows)} — "
                f"linked {counters['linked']}, failed {counters['failed']}, "
                f"skipped {counters['skipped']}"
            )

    click.echo("\nBackfill import complete:")
    click.echo(f"  Processed:    {counters['processed']}")
    click.echo(f"  Linked:       {counters['linked']}")
    click.echo(f"  Failed:       {counters['failed']}")
    click.echo(f"  Skipped:      {counters['skipped']}")

    return counters


def detect_bad_company_links(crm: CRMProvider) -> str:
    """Scan pipeline entries for people linked to companies with linkedin.com domain.

    The LinkedIn-domain bug caused match_or_create_company to save
    ``linkedin.com`` as the domain on the first company it created.  Every
    subsequent prospect then matched that company via domain search.

    Returns the path to an exported CSV listing the affected records.

    P1c migration note: this slice reads the CRM exclusively through the
    vendor-neutral ``CRMProvider`` contract (``query_list_entries`` →
    ``list[Entry]``, ``get_person`` → ``Record``, ``get_company`` → ``Record``,
    ``extract_person_info`` → ``RecordInfo``). Structured company/domain fields
    have no contract attribute, so they are read via ``Record.raw`` (the
    contract's documented escape hatch). The write sibling ``repair_bad_companies``
    stays on ``AttioClient`` — it migrates in a later increment.
    """
    entries = crm.query_list_entries(limit=500)

    total = len(entries)
    bad_rows: list[dict] = []
    # Cache: company_record_id → (is_bad, company_name)
    company_cache: dict[str, tuple[bool, str]] = {}

    click.echo(f"Scanning {total} pipeline entries...")

    for i, entry in enumerate(entries):
        record_id = entry.record_id
        if not record_id:
            continue

        person = crm.get_person(record_id)
        time.sleep(0.15)

        if person is None:
            continue

        # Check for linked company.  The company reference is a structured
        # multi-value slug the contract does not model as a flat attribute, so
        # read it off the untouched vendor payload via Record.raw (the
        # contract's escape hatch).
        company_data = person.raw.get("values", {}).get("company", [])
        if not company_data or not isinstance(company_data, list):
            continue
        company_rid = company_data[0].get("target_record_id", "")
        if not company_rid:
            continue

        # Check if this company has linkedin.com domain (cached)
        if company_rid not in company_cache:
            company_record = crm.get_company(company_rid)
            time.sleep(0.15)
            is_bad = False
            company_name = "Unknown"
            if company_record:
                name_data = company_record.raw.get("values", {}).get("name", [])
                if name_data:
                    company_name = name_data[0].get("value", "Unknown")
                domains = company_record.raw.get("values", {}).get("domains", [])
                for d in domains:
                    domain_val = d.get("domain", "") if isinstance(d, dict) else str(d)
                    if "linkedin.com" in domain_val.lower():
                        is_bad = True
                        break
            company_cache[company_rid] = (is_bad, company_name)

        is_bad, company_name = company_cache[company_rid]
        if not is_bad:
            continue

        # Extract person info
        info = crm.extract_person_info(person)
        bad_rows.append({
            "linkedin_url": info.linkedin_url if info.linkedin_url is not None else "",
            "attio_record_id": record_id,
            "name": info.name,
            "current_wrong_company": company_name,
        })

        if (i + 1) % 50 == 0:
            click.echo(f"  Progress: {i + 1}/{total} entries scanned, {len(bad_rows)} bad links found")

    os.makedirs("exports", exist_ok=True)
    file_path = f"exports/bad_company_links_{date.today().isoformat()}.csv"

    with open(file_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["linkedin_url", "attio_record_id", "name", "current_wrong_company"])
        writer.writeheader()
        writer.writerows(bad_rows)

    click.echo("\nDetection complete:")
    click.echo(f"  Total entries:   {total}")
    click.echo(f"  Bad links found: {len(bad_rows)}")
    click.echo(f"  File:            {file_path}")

    return file_path


def repair_bad_companies(
    attio: AttioClient,
    pb: PhantomBusterClient,
    profile_scraper_id: str,
    detect_csv_path: str,
) -> dict:
    """Repair pipeline records with bad company links via PB re-scraping.

    1. Clean linkedin.com domain from the poisoned company record(s)
    2. Launch PB Profile Scraper on affected LinkedIn URLs
    3. Re-link via backfill_import (now using extract_real_domain)

    Returns the backfill_import summary dict.
    """
    from clients.google_sheets import write_prospects_to_sheet

    # Step 0: Clean poisoned company domains. Loop because there may be
    # multiple companies poisoned with linkedin.com (different runs of the
    # weekly pipeline created different "default" companies).
    click.echo("--- Step 0: Cleaning linkedin.com domains from company records ---")
    cleaned_count = 0
    seen_rids: set[str] = set()
    while True:
        bad_company = attio.search_company_by_domain("linkedin.com")
        if not bad_company:
            break
        rid = bad_company["id"]["record_id"]
        if rid in seen_rids:
            # Safety: avoid infinite loop if update hasn't propagated yet
            break
        seen_rids.add(rid)
        name_data = bad_company.get("values", {}).get("name", [])
        name = name_data[0].get("value", "?") if name_data else "?"
        attio.update_company(rid, {"domains": []})
        click.echo(f"  Cleared linkedin.com domain from '{name}' ({rid})")
        cleaned_count += 1
    if cleaned_count == 0:
        click.echo("  No company with linkedin.com domain found (already clean)")

    # Step 1: Read detection CSV
    with open(detect_csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        click.echo("No records to repair.")
        return {"processed": 0, "linked": 0, "failed": 0, "skipped": 0}

    click.echo(f"\n--- Step 1: Scraping {len(rows)} profiles ---")

    # Step 2: Write to Google Sheet and launch Profile Scraper
    sheet_rows = [{"profileUrl": row["linkedin_url"]} for row in rows]
    sheet_url = write_prospects_to_sheet(sheet_rows)
    click.echo(f"  Wrote {len(sheet_rows)} URLs to Google Sheet")

    from clients.phantombuster import get_phantombuster_credentials

    session_cookie, session_ua = get_phantombuster_credentials()
    launch_args: dict = {"spreadsheetUrl": sheet_url}
    if session_cookie:
        launch_args["sessionCookie"] = session_cookie
        launch_args["userAgent"] = session_ua

    click.echo("  Launching Profile Scraper...")
    launch = pb.launch_agent(profile_scraper_id, launch_args)

    # Step 3: Wait and download (F-PR-5: typed launch, container-keyed CSV)
    click.echo("  Waiting for completion (up to 10 min)...")
    pb.wait_for_completion(launch, poll_interval=15, max_wait=600)

    result_csv = pb.download_result_csv(launch)
    if not result_csv:
        click.echo("  ERROR: Profile Scraper returned no CSV")
        return {"processed": 0, "linked": 0, "failed": 0, "skipped": 0}

    pb_output_path = f"exports/repair_companies_pb_{date.today().isoformat()}.csv"
    with open(pb_output_path, "w", encoding="utf-8") as f:
        f.write(result_csv)
    click.echo(f"  Saved PB output to {pb_output_path}")

    # Step 4: Re-link via backfill_import
    click.echo("\n--- Step 2: Re-linking companies ---")
    return backfill_import(attio, pb_output_path, detect_csv_path)
