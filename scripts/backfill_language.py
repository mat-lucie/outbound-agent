#!/usr/bin/env python3
"""PR-26 language attribute backfill: populate language for LinkedIn Outreach
entries where the language attribute is missing.

Per plan §9 line 1082, for each LinkedIn Outreach list entry where
`language` is None or empty:

  1. Fetch the linked Company record via the entry's `company` reference
  2. Extract the company's domain + HQ country code
  3. Call `detect_language(domain, country_code)` to infer the language
  4. If inference succeeds, update the entry's `language` attribute
  5. If domain and country are both absent, mark_failed + emit an
     `language_unknown` Operator Review Queue row for manual triage

Idempotency contract (§9.4): entries that already carry a non-empty
`language` attr skip with `skip_idempotent()`. Second consecutive run
returns rows_modified=0, rows_skipped_idempotent=N.

# Usage

    python3 scripts/backfill_language.py --dry-run [--limit N] [--verbose]
    python3 scripts/backfill_language.py --apply [--limit N] [--verbose]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clients.attio import AttioClient  # noqa: E402
from models.email_campaign import detect_language  # noqa: E402
from workflows.escalation import escalate  # noqa: E402
from workflows.migration_run_writer import MigrationRunWriter  # noqa: E402

SCRIPT_VERSION = "pr-26-v1"
WRITER_MODULE = "scripts.backfill_language"


def _get_list_id() -> str:
    """Get LinkedIn Outreach list ID from env."""
    list_id = os.environ.get("ATTIO_LIST_ID", "").strip()
    if list_id:
        return list_id
    raise RuntimeError(
        "LinkedIn Outreach list ID not set. "
        "Export ATTIO_LIST_ID=<uuid> and retry."
    )


def _get_company_domain_and_country(
    attio: AttioClient, company_id: str
) -> tuple[str, str]:
    """Fetch a company record and extract its domain + HQ country code.

    Returns (domain, country_code). Either or both may be empty strings
    if the company record lacks those values.
    """
    try:
        companies = attio.search_companies(filter_={"id": company_id})
        if not companies:
            return "", ""
        company = companies[0]
        values = company.get("values", {})

        # Extract domain: domains is an array of {domain: str, ...}
        domain = ""
        domains_list = values.get("domains") or []
        if domains_list:
            first = domains_list[0]
            domain = (
                first.get("domain", "")
                if isinstance(first, dict)
                else str(first)
            )

        # Extract HQ country code: hq_country_code is an array of
        # {country_code: str, ...}
        country_code = ""
        country_list = values.get("hq_country_code") or []
        if country_list:
            first = country_list[0]
            country_code = (
                first.get("country_code", "")
                if isinstance(first, dict)
                else str(first)
            )

        return domain.strip(), country_code.strip()
    except (httpx.HTTPStatusError, httpx.RequestError, httpx.TimeoutException) as exc:
        # Transient Attio API error — log + treat as "couldn't extract"
        # so the caller can mark_failed and escalate. Per §3 #9 (no
        # silent fallbacks), surface the failure on stderr instead of
        # swallowing it silently with a bare Exception catch.
        print(
            f"  WARN: _get_company_domain_and_country({company_id!r}) "
            f"hit {type(exc).__name__}: {exc}. Treating as missing.",
            file=sys.stderr,
        )
        return "", ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Dry-run mode: count updates without writing to Attio",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply mode: write language updates to Attio",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after processing this many entries",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Verbose output",
    )
    args = parser.parse_args(argv)

    # Enforce XOR: one of --dry-run or --apply required
    if (args.dry_run and args.apply) or (not args.dry_run and not args.apply):
        print(
            "error: exactly one of --dry-run or --apply must be specified",
            file=sys.stderr,
        )
        return 2

    try:
        attio = AttioClient()
    except KeyError:
        print("error: ATTIO_API_KEY env var not set", file=sys.stderr)
        return 2

    try:
        list_id = _get_list_id()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        f"PR-26 language backfill "
        f"({'dry-run' if args.dry_run else 'apply'}) — fetching all entries..."
    )

    all_entries = attio.query_list_entries(list_id=list_id, limit=100_000)
    print(f"  Fetched {len(all_entries)} linkedin_outreach entries.")

    if not all_entries:
        print("  No entries found — nothing to backfill.")
        return 0

    mig_writer = MigrationRunWriter(
        script_name=Path(__file__).name,
        script_version=SCRIPT_VERSION,
        rollback_script_path=None,  # language inference is effectively irreversible
        dry_run=args.dry_run,
        attio=attio,
    )

    processed = 0
    with mig_writer as mrun:
        for entry in all_entries:
            if args.limit and processed >= args.limit:
                break
            processed += 1

            mrun.examine()

            attrs = AttioClient.parse_entry(entry)
            entry_id = attrs.get("entry_id", "")
            record_id = attrs.get("record_id", "")

            if not entry_id:
                mrun.mark_failed(
                    record_id=record_id or "unknown",
                    error="missing entry_id",
                )
                if args.verbose:
                    print(
                        "  WARN: entry missing entry_id, skipping.",
                        file=sys.stderr,
                    )
                continue

            # Idempotency: skip if language is already set
            if attrs.get("language"):
                mrun.skip_idempotent()
                if args.verbose:
                    print(
                        f"  SKIP entry_id={entry_id}: "
                        f"language={attrs['language']!r}"
                    )
                continue

            # Get company domain + country code from the entry's company reference
            # The company record is linked via the entry — we need to extract it
            # from entry_values. For now, we'll try to get it from the raw entry.
            entry_values = entry.get("entry_values", {})
            company_ref = entry_values.get("company") or []
            company_id = None
            if company_ref:
                first = company_ref[0] if isinstance(company_ref, list) else company_ref
                company_id = (
                    first.get("target_record_id")
                    if isinstance(first, dict)
                    else str(first)
                )

            if not company_id:
                mrun.mark_failed(
                    record_id=record_id or entry_id,
                    error="no_company_reference",
                )
                if args.verbose:
                    print(
                        f"  FAIL entry_id={entry_id}: no company reference in entry",
                        file=sys.stderr,
                    )
                continue

            # Fetch company and extract domain + country
            domain, country_code = _get_company_domain_and_country(
                attio, company_id
            )

            if not domain and not country_code:
                # §3 hard constraint #9 (no silent fallbacks): emit a
                # typed Operator Review Queue row so operators see the
                # gap, not just a stderr line. Uses the existing
                # `missing_language` slug (§4.1 Prospect-daily lens,
                # plan line 149) — same semantic shape: we cannot
                # resolve the row's language with current evidence.
                # MissingLanguagePayload (TypedDict at
                # workflows.escalation_schemas:518) requires record_id,
                # persona, language_value, dm_step, error_msg.
                escalate(
                    type="missing_language",
                    idempotency_key=f"backfill-lang-{entry_id}",
                    payload={
                        "record_id": record_id or entry_id,
                        "persona": None,  # backfill is persona-agnostic
                        "language_value": None,  # absent, not invalid
                        "dm_step": "backfill",
                        "error_msg": (
                            f"backfill_language: company {company_id} "
                            f"has no domain AND no hq_country_code; "
                            f"cannot infer language"
                        ),
                    },
                )
                mrun.mark_failed(
                    record_id=record_id or entry_id,
                    error="no_domain_no_country",
                )
                if args.verbose:
                    print(
                        f"  FAIL entry_id={entry_id}: "
                        f"company {company_id} has no domain or country "
                        f"(escalated missing_language)",
                        file=sys.stderr,
                    )
                continue

            # Infer language from domain + country
            inferred_lang = detect_language(domain, country_code)

            # Write update
            if args.dry_run:
                mrun.mark_modified(record_id=entry_id, object="linkedin_outreach")
                if args.verbose:
                    print(
                        f"  DRY entry_id={entry_id}: "
                        f"would set language={inferred_lang!r} "
                        f"(domain={domain!r}, country={country_code!r})"
                    )
            else:
                try:
                    attio.update_list_entry(
                        entry_id=entry_id,
                        entry_attributes={"language": inferred_lang},
                        list_id=list_id,
                    )
                    mrun.mark_modified(record_id=entry_id, object="linkedin_outreach")
                    if args.verbose:
                        print(
                            f"  UPDATE entry_id={entry_id}: "
                            f"set language={inferred_lang!r}"
                        )
                except (httpx.HTTPStatusError, httpx.RequestError,
                        httpx.TimeoutException) as err:
                    # Narrowed from bare `except Exception` per §3 #9:
                    # only catch httpx-class transient errors. Other
                    # exceptions (KeyError, AttributeError, etc.) are
                    # code bugs / infrastructure failures that should
                    # propagate and halt the run.
                    mrun.mark_failed(
                        record_id=record_id or entry_id,
                        error=f"attio_error: {type(err).__name__}",
                    )
                    if args.verbose:
                        print(
                            f"  ERROR entry_id={entry_id}: {err}",
                            file=sys.stderr,
                        )

    print(
        f"\nPR-26 language backfill complete "
        f"({'dry-run' if args.dry_run else 'apply'}):\n"
        f"  migration.rows_examined={mrun.rows_examined}\n"
        f"  migration.rows_modified={mrun.rows_modified}\n"
        f"  migration.rows_skipped_idempotent={mrun.rows_skipped_idempotent}\n"
        f"  migration.rows_failed={mrun.rows_failed}"
    )

    if mrun.rows_failed > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
