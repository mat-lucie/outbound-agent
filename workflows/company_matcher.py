"""Company matching and creation utilities for Attio CRM."""

import contextlib
import re
from urllib.parse import urlparse

import click
import httpx

from clients.attio import AttioClient


def extract_real_domain(raw: dict) -> str:
    """Extract a real company domain from a PB CSV row, ignoring LinkedIn URLs.

    PhantomBuster exports `companyUrl` as a LinkedIn company page URL
    (e.g. https://www.linkedin.com/company/acme/), not the actual company
    website.  Passing that through urlparse yields ``linkedin.com`` which
    poisons domain-based company matching.  This helper returns an empty
    string for LinkedIn URLs so the caller falls back to name-only matching.
    """
    url = raw.get("companyUrl", raw.get("companyWebsite", raw.get("companyDomain", "")))
    if not url or not isinstance(url, str):
        return ""
    url = url.strip()
    if not url:
        return ""
    if "linkedin.com" in url.lower():
        return ""
    if "://" in url:
        url = urlparse(url).netloc or url
    if url.startswith("www."):
        url = url[4:]
    return url

SUFFIXES = [
    "s.a. de c.v.", "s. de r.l.", "pty ltd",
    "inc.", "inc", "llc", "ltd.", "ltd", "ltda.", "ltda",
    "s.a.", "sa", "s.a.s", "s.r.l.", "srl", "s.l.",
    "gmbh", "ag", "co.", "corp.", "corp", "corporation",
    "group", "holding", "holdings", "plc", "pty",
]


def normalize_company_name(name: str) -> str:
    """Normalize a company name for fuzzy comparison."""
    result = name.lower()

    for suffix in SUFFIXES:
        if result.endswith(suffix):
            result = result[: -len(suffix)]

    result = result.strip(".,- ")
    result = re.sub(r" {2,}", " ", result)
    result = result.strip()

    return result


def find_company_record(
    attio: AttioClient,
    company_name: str,
    domain: str | None = None,
) -> dict | None:
    """Find an existing CRM company record. Read-only — never creates.

    Same matching order as match_or_create_company: domain-first (normalized),
    then normalized-name equality. Returns the full record dict (id + values)
    or None when no match. HTTP errors propagate to the caller. (PR-225)
    """
    if not company_name:
        return None

    # 1. Domain-first search (normalize www. prefix)
    if domain:
        if domain.startswith("www."):
            domain = domain[4:]
        result = attio.search_company_by_domain(domain)
        if result:
            return result

    # 2. Name matching
    results = attio.search_companies(filter_={"name": company_name}, limit=5)
    normalized_input = normalize_company_name(company_name)
    for result in results:
        # Guard the deref: the workspace contains name-less shell companies
        # (import artifacts) that the CRM's name filter can still return. A
        # malformed hit must degrade to no-match, not crash the caller.
        name_data = result.get("values", {}).get("name") or []
        candidate = name_data[0].get("value", "") if name_data and isinstance(name_data[0], dict) else ""
        if candidate and normalize_company_name(candidate) == normalized_input:
            return result

    return None


def match_or_create_company(
    attio: AttioClient,
    company_name: str,
    domain: str | None = None,
    *,
    industry_vertical: str | None = None,
    industry_status: str | None = None,
) -> str | None:
    """Find an existing CRM company record or create a new one.

    When a new company is created and industry_vertical is provided, the value
    is set as the company's industry_vertical attribute; industry_status (when
    also provided) is stamped alongside it with the classifier provenance attrs
    so the record carries the same payload the backfill writer produces.
    Existing companies found via domain or name match are NOT updated — this
    function only sets the fields on CREATE.

    Returns the record_id string, or None on failure.
    """
    try:
        if not company_name:
            return None

        existing = find_company_record(attio, company_name, domain)
        if existing:
            return existing["id"]["record_id"]

        # Create new company
        if domain and domain.startswith("www."):
            domain = domain[4:]
        attributes: dict = {"name": company_name}
        if domain:
            attributes["domains"] = [{"domain": domain}]
        if industry_vertical:
            if industry_status:
                from workflows.industry_classifier import build_classifier_payload
                attributes.update(build_classifier_payload(industry_vertical, status=industry_status))
            else:
                attributes["industry_vertical"] = industry_vertical

        created = attio.create_company(attributes)
        record_id = created.get("id", {}).get("record_id", "")
        if record_id:
            with contextlib.suppress(httpx.HTTPStatusError):
                attio.create_note(
                    record_id,
                    "Auto-created company",
                    "Created automatically by sales agent during prospecting. "
                    "Needs review — verify company details are correct.",
                    parent_object="companies",
                )
        return record_id or None

    except Exception as e:
        click.echo(f"      ⚠ Company matching failed for {company_name}: {e}")
        return None
