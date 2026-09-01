#!/usr/bin/env python3
"""Report + backfill `hq_country_code` for companies holding active-cadence
prospects.

Why: the entry `language` attribute is seeded from the linked company's
`hq_country_code` (scripts/backfill_language.py). When a company lacks it,
language falls to the lane default and can misfire — a US-headquartered
prospect gets the Spanish lane's copy, or a LATAM prospect gets English —
until the operator catches it in dry-run review. Populating HQ country
moves that catch upstream into data.

Two modes, report-first (never writes without a curated CSV):

  --report (default)
      Scan LinkedIn Outreach entries at active DM stages (Accepted /
      DM1 Sent / DM2 Sent / DM3 Sent), resolve each entry's linked
      company, and write a CSV of companies MISSING hq_country_code to
      exports/hq_country_backfill_report_<date>.csv. Columns:
      company_id, company_name, domain, prospect_count,
      suggested_country_code (TLD-derived best-effort, may be blank),
      country_code (EMPTY — the curation column).

  --apply --csv <path>
      Read a curated copy of that CSV (the operator fills/confirms the
      country_code column; rows with a blank country_code are skipped)
      and write hq_country_code to each company. Idempotent by natural
      filter: a company that already carries a non-empty hq_country_code
      is skipped, never overwritten. Every write is re-read and verified
      (fail loud per row, exit 1 if any row failed). Writability probe:
      if the FIRST attempted write fails with zero successes, the whole
      batch aborts — the curated CSV stays intact for a re-run.

# Usage

    python3 scripts/backfill_company_hq_country.py --report [--limit N]
    python3 scripts/backfill_company_hq_country.py --apply --csv exports/hq_country_backfill_report_2026-08-11.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from datetime import date
from pathlib import Path

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from clients.attio import AttioClient  # noqa: E402
from models.pipeline import PipelineStage  # noqa: E402
from workflows.migration_run_writer import MigrationRunWriter  # noqa: E402

SCRIPT_VERSION = "hq-country-v1"

# Stages whose prospects are in (or about to enter) the DM cadence — the
# population whose language attribute the HQ country seeds. Derived from
# the enum so a stage rename can't silently empty the report.
ACTIVE_DM_STAGES = frozenset({
    PipelineStage.ACCEPTED.value,
    PipelineStage.DM1_SENT.value,
    PipelineStage.DM2_SENT.value,
    PipelineStage.DM3_SENT.value,
})

# Best-effort TLD → ISO-3166 alpha-2 suggestion for the curation column.
# Deliberately conservative: only unambiguous country TLDs; .com and
# other gTLDs suggest nothing. The human curates; this just saves typing.
_TLD_SUGGESTION = {
    "mx": "MX", "br": "BR", "pe": "PE", "cl": "CL", "co": "CO",
    "ar": "AR", "ec": "EC", "uy": "UY", "py": "PY", "bo": "BO",
    "gt": "GT", "cr": "CR", "pa": "PA", "do": "DO", "hn": "HN",
    "sv": "SV", "ni": "NI", "ve": "VE", "es": "ES", "pt": "PT",
    "us": "US", "ca": "CA", "uk": "GB", "de": "DE", "fr": "FR",
    "it": "IT", "ch": "CH", "nl": "NL", "be": "BE", "at": "AT",
    "se": "SE", "fi": "FI", "dk": "DK", "no": "NO", "pl": "PL",
    "jp": "JP", "kr": "KR", "cn": "CN", "in": "IN", "au": "AU",
}

_ISO2_RE = re.compile(r"^[A-Z]{2}$")


def _suggest_from_domain(domain: str) -> str:
    tld = domain.rsplit(".", 1)[-1].lower() if "." in domain else ""
    return _TLD_SUGGESTION.get(tld, "")


def _company_ref_from_entry(entry: dict) -> str:
    """Extract the linked company record_id from a raw list entry, or ''.

    Mirrors scripts/backfill_language.py: the `company` entry value is an
    array of record references.
    """
    entry_values = entry.get("entry_values", {})
    company_ref = entry_values.get("company") or []
    if not company_ref:
        return ""
    first = company_ref[0]
    if isinstance(first, dict):
        return str(
            first.get("target_record_id")
            or first.get("record_id")
            or ""
        )
    return ""


def _company_fields(attio: AttioClient, company_id: str) -> tuple[str, str, str]:
    """Return (name, domain, hq_country_code) for a company, '' on absence.

    Same fail-open transient-error contract as backfill_language.py's
    company getter: an Attio error prints a WARN and reads as missing so
    the report never crashes mid-scan — but the affected company is
    still listed (with blank fields) rather than silently dropped.
    """
    try:
        record = attio.get_company(company_id)
        if not record:
            return "", "", ""
        values = record.get("values", {})

        def _first(field: str, key: str) -> str:
            arr = values.get(field) or []
            if not arr:
                return ""
            head = arr[0]
            return (
                str(head.get(key, "")) if isinstance(head, dict) else str(head)
            ).strip()

        name = _first("name", "value")
        domain = _first("domains", "domain")
        country = _first("hq_country_code", "country_code")
        return name, domain, country
    except (httpx.HTTPStatusError, httpx.RequestError, httpx.TimeoutException) as exc:
        print(
            f"  WARN: get_company({company_id!r}) hit {type(exc).__name__}: "
            f"{exc}. Listing with blank fields.",
            file=sys.stderr,
        )
        return "", "", ""


def run_report(
    attio: AttioClient,
    list_id: str,
    limit: int,
    out_path: Path | None = None,
) -> int:
    entries = attio.query_list_entries(list_id=list_id, limit=100_000)
    print(f"Scanned {len(entries)} list entries.")

    # Active-DM-stage entries first; company resolution comes next.
    active: list[dict] = []
    for entry in entries:
        attrs = AttioClient.parse_entry(entry)
        if attrs.get("stage") in ACTIVE_DM_STAGES:
            attrs["_raw_entry"] = entry
            active.append(attrs)

    # Resolve each prospect's company the way the production pipeline
    # does: bulk-preload person records (which populates
    # attio._person_to_company via extract_record_info), then read the
    # link per record. Workspaces whose list entries carry no `company`
    # entry-value rely entirely on that path; the entry_values fallback
    # below covers schemas that do add one.
    from workflows.daily_check import _company_id_for_prospect
    from workflows.record_cache import RecordCache, preload_pipeline_persons

    record_ids = {str(a["record_id"]) for a in active if a.get("record_id")}
    cache = RecordCache(attio)
    preload_pipeline_persons(attio, cache, record_ids)

    # company_id -> prospect_count, over active-DM-stage entries only.
    counts: dict[str, int] = {}
    no_company = 0
    for attrs in active:
        record_id = str(attrs.get("record_id") or "")
        cache.get(record_id)  # prime _person_to_company for this record
        company_id = (
            _company_id_for_prospect(attio, record_id)
            or _company_ref_from_entry(attrs["_raw_entry"])
        )
        if not company_id:
            no_company += 1
            continue
        counts[company_id] = counts.get(company_id, 0) + 1

    print(
        f"Active-DM-stage entries map to {len(counts)} distinct companies. "
        f"{no_company} active-stage entries have NO company link — those are "
        f"not fixable by this script (their language expectation is "
        f"undeterminable regardless of HQ data); link the company in Attio."
    )

    missing: list[dict[str, str]] = []
    have = 0
    for company_id, prospect_count in sorted(
        counts.items(), key=lambda kv: -kv[1]
    )[: limit or None]:
        name, domain, country = _company_fields(attio, company_id)
        if country:
            have += 1
            continue
        missing.append({
            "company_id": company_id,
            "company_name": name,
            "domain": domain,
            "prospect_count": str(prospect_count),
            "suggested_country_code": _suggest_from_domain(domain),
            "country_code": "",  # curation column — the operator fills this
        })

    if out_path is None:
        out_path = (
            Path(__file__).resolve().parent.parent
            / "exports"
            / f"hq_country_backfill_report_{date.today().isoformat()}.csv"
        )
    with out_path.open("w", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "company_id", "company_name", "domain", "prospect_count",
                "suggested_country_code", "country_code",
            ],
        )
        writer.writeheader()
        writer.writerows(missing)

    print(
        f"\n{have} companies already carry hq_country_code; "
        f"{len(missing)} are missing it."
    )
    print(f"Report written: {out_path}")
    print(
        "Next: curate the country_code column (suggested_country_code is "
        "TLD-guessed, verify it), then re-run with "
        f"--apply --csv {out_path}"
    )
    return 0


def run_apply(attio: AttioClient, csv_path: str) -> int:
    with open(csv_path, newline="") as fh:
        rows = list(csv.DictReader(fh))

    mig_writer = MigrationRunWriter(
        script_name=Path(__file__).name,
        script_version=SCRIPT_VERSION,
        rollback_script_path=None,  # values are curated by hand; correct a
        # bad one by editing the company record, not by rollback
        dry_run=False,  # report mode is the dry run; --apply is always wet
        attio=attio,
    )

    skipped_blank = 0
    with mig_writer as mrun:
        for row in rows:
            company_id = (row.get("company_id") or "").strip()
            code = (row.get("country_code") or "").strip().upper()
            label = row.get("company_name") or company_id
            if not company_id:
                continue
            mrun.examine()
            if not code:
                skipped_blank += 1
                mrun.skip_excluded(reason="country_code left blank in curation")
                continue
            if not _ISO2_RE.match(code):
                print(f"  ✗ {label}: invalid country_code {code!r} — skipped.")
                mrun.mark_failed(
                    record_id=company_id,
                    error=f"invalid country_code {code!r}",
                )
                continue
            # Natural-filter idempotency: never overwrite an existing value.
            current = attio.company_hq_country_code(company_id)
            if current:
                mrun.skip_idempotent()
                continue
            try:
                # Write the SAME array-of-objects shape every read site
                # documents ([{"country_code": ...}]) — nothing in the repo
                # has written this attribute before, so don't bet the
                # curated batch on Attio accepting a shorthand.
                attio.update_company(
                    company_id, {"hq_country_code": [{"country_code": code}]}
                )
            except (httpx.HTTPStatusError, httpx.RequestError, httpx.TimeoutException) as exc:
                print(f"  ✗ {label}: write failed ({type(exc).__name__}: {exc}).")
                mrun.mark_failed(record_id=company_id, error=exc)
                if mrun.rows_modified == 0:
                    # Writability probe: the FIRST attempted write failing
                    # with zero successes suggests the attribute itself is
                    # unwritable or the shape is wrong — abort the whole
                    # batch instead of burning every curated row on the
                    # same error.
                    print(
                        "\nABORT: first write failed with zero successes — "
                        "hq_country_code may be unwritable via the API or "
                        "the value shape is wrong. Nothing else attempted; "
                        "the curated CSV is intact for a re-run."
                    )
                    break
                continue
            # Verify by re-read through the SAME getter the language guard
            # uses — a write that lands in the wrong shape reads back None
            # here and fails loud instead of silently doing nothing.
            attio.invalidate_company_hq_country(company_id)
            readback = attio.company_hq_country_code(company_id)
            if readback != code:
                print(
                    f"  ✗ {label}: wrote {code!r} but read back {readback!r} — "
                    f"write did not land in the shape the language guard reads."
                )
                mrun.mark_failed(
                    record_id=company_id,
                    error=f"verify readback {readback!r} != {code!r}",
                )
                if mrun.rows_modified == 0:
                    print(
                        "\nABORT: first write did not verify with zero "
                        "successes — value shape likely wrong. Nothing else "
                        "attempted; the curated CSV is intact for a re-run."
                    )
                    break
                continue
            print(f"  ✓ {label}: hq_country_code = {code}")
            mrun.mark_modified(record_id=company_id, object="companies")

    print(
        f"\nApplied {mrun.rows_modified} · blank {skipped_blank} · "
        f"already-set {mrun.rows_skipped_idempotent} · "
        f"failed {mrun.rows_failed}"
    )
    return 1 if mrun.rows_failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--report", action="store_true", help="Report mode (default)")
    mode.add_argument("--apply", action="store_true", help="Apply a curated CSV")
    parser.add_argument("--csv", help="Curated CSV path (required with --apply)")
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Report mode: cap companies checked (0 = all), highest prospect_count first",
    )
    args = parser.parse_args(argv)

    if args.apply and not args.csv:
        parser.error("--apply requires --csv <curated report>")

    with AttioClient() as attio:
        if args.apply:
            return run_apply(attio, args.csv)
        # Report mode is the only list consumer — apply works purely off
        # the curated CSV's company_ids.
        list_id = os.environ.get("ATTIO_LIST_ID", "").strip()
        if not list_id:
            print(
                "ERROR: LinkedIn Outreach list ID not set. "
                "Export ATTIO_LIST_ID=<uuid> and retry.",
                file=sys.stderr,
            )
            return 2
        return run_report(attio, list_id, args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
