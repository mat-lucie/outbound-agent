"""Read-only diagnostic: enumerate the 14 throttle-violation companies
flagged in done-qa-gtm-pass1-round2 (GTM-MIN-5), fetch their current
LinkedIn Outreach entries, and emit a recommended keep/pause assignment
per the operator's triage rules.

Wave-2-D companion to `docs/runbooks/wave-2-D-throttle-violation-triage.md`.

The script is INTENTIONALLY read-only — it makes ZERO Attio writes. It
exists so the operator can review the per-company state offline (no UI clicks)
before deciding whether to pause via the Attio admin UI or to write a
one-off triage-batch script. Output lands in
``exports/triage_throttle_violations_20260525.json`` for the operator to append
per-company decisions to.

Triage rule (the recommended assignment, see runbook Step 2):
  1. Keep the entry with the highest ``dm_step`` (deepest in funnel).
  2. Tiebreak by highest ``quality_score``.
  3. Final tiebreak: oldest ``last_contact_date`` (the more committed
     thread). Deterministic AND semantically meaningful — picks the
     contact who's been engaged longer, not the one whose UUID happens
     to sort first.

Usage:
  python3 scripts/diagnose_throttle_violations_20260525.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import click  # noqa: E402

from clients.attio import AttioClient  # noqa: E402

# The 14 company record_ids from the gtm audit's per-company throttle
# violation list. Source: internal gtm QA audit,
# attio_data_cross_checks.per_company_throttle_violations_observed.
VIOLATION_COMPANIES: list[tuple[str, int]] = [
    # (company_record_id, observed_active_dm_count)
    ("3c29c5bb-94f4-4f4d-94d2-341f9bccb356", 3),
    ("c9e34db5-9eee-5a77-aff5-6389d22ecf22", 3),
    ("b57796a1-ed60-5d79-bfca-980557773179", 3),
    ("1563c1c2-1fb1-5bb6-aedd-7e33a41bf2c5", 2),
    ("54fa12b7-71af-4814-b20a-3ca66b2dc0d5", 2),
    ("398144b1-cc45-593f-844f-843aefbe8eb6", 2),
    ("987c3d79-b689-4d07-af4c-5b45eb14f7ed", 2),
    ("cf080b92-9687-48bf-9153-90a4a48f1a26", 2),
    ("99fc5736-7fbd-4065-9c19-8f1d83ec6546", 2),
    ("dce1e821-3f8b-54a0-9434-5b54b3215c3f", 2),
    ("801b9371-d4b7-4d7c-8ce3-e9023303d1dd", 2),
    ("7a309314-785c-5cb8-80b1-ae66bc23ae73", 2),
    ("5c39cbd7-9de9-4c50-8f6e-696bf960a709", 2),
    ("cabca447-a224-472b-b064-c601f5f3db4a", 2),
]

OUTPUT_PATH = (
    Path(__file__).resolve().parent.parent
    / "exports"
    / "triage_throttle_violations_20260525.json"
)

# Stage rank for the keep-by-highest-dm_step rule. Higher = deeper in
# funnel = preferred keep target.
ACTIVE_STAGE_RANK: dict[str, int] = {
    "DM1 Sent": 1,
    "DM2 Sent": 2,
    "DM3 Sent": 3,
}


def _build_company_index(
    attio: AttioClient, all_parsed: list[dict],
) -> dict[str, list[dict]]:
    """Return ``{company_record_id: [matching_parsed_entry, ...]}``.

    Wave-2-D fix-up (multi-agent review D2): pre-Wave-2-D-fix-up the
    diagnostic re-fetched the entire LinkedIn Outreach list once per
    company AND issued an ``attio.get_person(rid)`` call per parsed
    entry — 14 × full-list + N+1 person lookups. This helper does the
    same work in O(list_size + distinct_persons) by:

    1. Walking the parsed entries once.
    2. Filtering to the active-DM criteria (stage in DM1/DM2/DM3_SENT,
       dm_step >= 1, last_contact_date >= 2026-05-01) BEFORE any
       per-person Attio call so we never lookup persons we'll discard.
    3. Looking up each distinct person record_id at most once, then
       inverting person→company refs into the returned dict.

    Returns a dict keyed by every company_record_id touched; callers
    look up their company id and get the pre-filtered entries.
    """
    # Step 1: filter to active multi-person DM criteria, dedup persons.
    active_rows: list[dict] = []
    person_ids_needed: set[str] = set()
    for parsed in all_parsed:
        stage = parsed.get("stage", "")
        if stage not in ACTIVE_STAGE_RANK:
            continue
        dm_step = int(parsed.get("dm_step") or 0)
        if dm_step < 1:
            continue
        last_contact = str(parsed.get("last_contact_date") or "")[:10]
        if last_contact < "2026-05-01":
            continue
        person_record_id = parsed.get("record_id")
        if not person_record_id:
            continue
        active_rows.append(parsed)
        person_ids_needed.add(person_record_id)

    # Step 2: batch-fetch each distinct person record once.
    person_to_companies: dict[str, set[str]] = {}
    persons = attio.bulk_fetch_persons_by_record_ids(person_ids_needed)
    for rid, record in persons.items():
        values = (record or {}).get("values", {})
        company_refs = values.get("company") or []
        if not isinstance(company_refs, list):
            continue
        person_to_companies[rid] = {
            ref.get("target_record_id")
            for ref in company_refs
            if isinstance(ref, dict) and ref.get("target_record_id")
        }

    # Step 3: invert into the company → entries index.
    out: dict[str, list[dict]] = {}
    for parsed in active_rows:
        person_record_id = parsed["record_id"]
        for company_id in person_to_companies.get(person_record_id, ()):
            out.setdefault(company_id, []).append({
                "entry_id": parsed.get("entry_id"),
                "person_record_id": person_record_id,
                "stage": parsed.get("stage", ""),
                "dm_step": int(parsed.get("dm_step") or 0),
                "last_contact_date": str(parsed.get("last_contact_date") or "")[:10],
                "quality_score": parsed.get("quality_score"),
                "persona": parsed.get("persona"),
            })
    return out


def _recommend_keep(entries: list[dict]) -> tuple[str | None, list[str]]:
    """Apply the keep/pause assignment rule. Returns (keep_entry_id,
    pause_entry_ids).

    Tiebreak: stage rank DESC → quality_score DESC → last_contact_date
    ASC (oldest first; means "more committed to the cadence"). The
    final tiebreak is deterministic AND semantically meaningful.
    """
    if not entries:
        return None, []

    def _rank_key(e: dict) -> tuple:
        return (
            -ACTIVE_STAGE_RANK.get(e["stage"], 0),
            -(int(e.get("quality_score") or 0)),
            str(e.get("last_contact_date") or ""),
        )

    sorted_entries = sorted(entries, key=_rank_key)
    keep = sorted_entries[0]
    pause = [e["entry_id"] for e in sorted_entries[1:]]
    return keep["entry_id"], pause


@click.command()
def main() -> None:
    # Wave-2-D fix-up (multi-agent review D3): fail loud at startup if
    # ATTIO_LIST_ID is unset. Pre-fix-up the empty env var produced a
    # malformed URL deep in the loop after the script had already
    # printed company names — bad operator UX, late-bind failure.
    list_id = os.environ.get("ATTIO_LIST_ID", "")
    if not list_id:
        click.echo(
            "ERROR: ATTIO_LIST_ID env var is required (the diagnostic "
            "needs to query the LinkedIn Outreach list for active "
            "multi-person DMs). Re-run with ATTIO_LIST_ID exported.",
            err=True,
        )
        sys.exit(2)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    report: list[dict] = []
    summary = {
        "total_companies": len(VIOLATION_COMPANIES),
        "found_in_attio": 0,
        "missing_in_attio": 0,
        "total_entries_to_pause": 0,
    }

    with AttioClient() as attio:
        # Wave-2-D fix-up (multi-agent review D1+D2): fetch the
        # LinkedIn Outreach list ONCE (auto-paginating via
        # query_list_entries — pre-fix-up the raw _request bypassed
        # Attio's 100-entry per-page cap and silently truncated) and
        # build the company → entries index ONCE (vs the pre-fix-up
        # pattern of 14× full-list fetches + N+1 get_person calls).
        click.echo("Fetching LinkedIn Outreach list (paginated)...")
        raw_entries = attio.query_list_entries(list_id=list_id, limit=50000)
        all_parsed = [AttioClient.parse_entry(raw) for raw in raw_entries]
        click.echo(f"  Parsed {len(all_parsed)} list entries.")

        click.echo("Indexing person→company refs...")
        company_index = _build_company_index(attio, all_parsed)
        click.echo(f"  Indexed {len(company_index)} companies with active multi-person DMs.")

        for i, (company_id, expected_count) in enumerate(VIOLATION_COMPANIES, 1):
            click.echo(
                f"\n[{i}/{len(VIOLATION_COMPANIES)}] company={company_id} "
                f"(expected_active_DMs={expected_count})"
            )
            try:
                company = attio._request(
                    "GET", f"/objects/companies/records/{company_id}",
                )
                company_record = company.get("data", {}) if isinstance(company, dict) else {}
                company_name = ""
                name_arr = company_record.get("values", {}).get("name", [])
                if name_arr and isinstance(name_arr[0], dict):
                    company_name = name_arr[0].get("value", "") or ""
                click.echo(f"  name: {company_name!r}")
            except Exception as exc:  # noqa: BLE001 — diagnostic, never raises
                click.echo(f"  ⚠ company fetch failed: {exc}", err=True)
                company_name = "<unknown>"

            entries = company_index.get(company_id, [])
            click.echo(f"  active multi-person DMs: {len(entries)}")
            if not entries:
                summary["missing_in_attio"] += 1
                report.append({
                    "company_record_id": company_id,
                    "company_name": company_name,
                    "expected_active_DMs": expected_count,
                    "found_active_DMs": 0,
                    "note": "no active multi-person DMs found — likely resolved organically",
                })
                continue
            summary["found_in_attio"] += 1

            for e in entries:
                click.echo(
                    f"    entry_id={e['entry_id'][:8]}  stage={e['stage']:>12s}  "
                    f"dm_step={e['dm_step']}  last_contact={e['last_contact_date']}  "
                    f"qs={e['quality_score']}"
                )

            keep_entry_id, pause_entry_ids = _recommend_keep(entries)
            summary["total_entries_to_pause"] += len(pause_entry_ids)
            click.echo(f"  → recommended keep: {keep_entry_id}")
            for pid in pause_entry_ids:
                click.echo(f"    → recommended pause: {pid}")

            report.append({
                "company_record_id": company_id,
                "company_name": company_name,
                "expected_active_DMs": expected_count,
                "found_active_DMs": len(entries),
                "entries": entries,
                "recommendation": {
                    "keep_entry_id": keep_entry_id,
                    "pause_entry_ids": pause_entry_ids,
                },
                # the operator fills these in during Step 2 of the runbook:
                "operator_decision": {
                    "keep_entry_id": None,
                    "pause_entry_ids": [],
                    "rationale": "",
                },
            })

    OUTPUT_PATH.write_text(
        json.dumps(
            {"summary": summary, "rows": report}, indent=2, default=str,
        ),
        encoding="utf-8",
    )
    click.echo("\n=== Summary ===")
    click.echo(f"Companies enumerated:        {summary['total_companies']}")
    click.echo(f"  Found active DMs in Attio: {summary['found_in_attio']}")
    click.echo(f"  No active DMs found:       {summary['missing_in_attio']}")
    click.echo(f"Entries recommended to pause: {summary['total_entries_to_pause']}")
    click.echo(f"\nReport written to: {OUTPUT_PATH}")
    click.echo("\nNext step: review the report and follow Step 2 in")
    click.echo("docs/runbooks/wave-2-D-throttle-violation-triage.md.")


if __name__ == "__main__":
    main()
