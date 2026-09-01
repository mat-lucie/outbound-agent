"""Read-only sweep of the outreach pool for integrator / service-provider
companies — "SELLS to your buyers" rather than "IS one".

WHY: the weekly qualifier can pass a small industrial-automation INTEGRATOR and
cadence it all the way through the DM sequence. Every signal the scorer reads
points the wrong way — in-ICP CRM categories, a plant-coded title, a brand-only
company name. Only the company DESCRIPTION states the business model.

`workflows.quality_gate` carries a `disqualifier_integrator` family for that
shape, but it only fires on prospects scored AFTER it shipped. This script finds
the ones already in the pool.

READ-ONLY. Writes NOTHING to the CRM — no stage moves, no field edits, no
suppression. It prints candidates and writes a report file for an operator to
review and action by hand.

Two tiers, because the shipped gate is deliberately narrower than the shape
worth eyeballing:

  TIER A — would fire today. Service-provider description AND an off-ICP
    industry_vertical. Same predicate as the live gate, so this is the backlog
    the gate would have caught.

  TIER B — watch list. The description reads as a service provider but the
    industry classifier labelled the company something else (or abstained), so
    the gate stays silent. Expect this band to be dominated by LARGE
    automation-equipment makers that genuinely run plants — i.e. correct
    exclusions, not misses. Review it as the calibration lever: if it fills with
    real integrators the classifier mislabelled, the fix is the industry
    classifier, not a wider keyword list.

Usage:
  python3 scripts/audit_integrator_prospects.py
  python3 scripts/audit_integrator_prospects.py --stage Prospect
  python3 scripts/audit_integrator_prospects.py --out exports/my-report.md
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

import click
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent))

from clients.attio import (  # noqa: E402
    AttioClient,
    first_option_title,
    first_text_value,
    is_linkedin_clearbit_corrupted,
)
from workflows.quality_gate import (  # noqa: E402
    INTEGRATOR_DESCRIPTION_KEYWORDS,
    INTEGRATOR_MANUFACTURER_CARVEOUTS,
    _find_first_match,
    _is_integrator_service_provider,
    _match_any,
)

DEFAULT_OUT = Path("exports/integrator-audit.md")


def _describe_reason(
    description: str, industry: str, industry_status: str
) -> tuple[str, str] | None:
    """Return `(tier, matched_keyword)` or None.

    Tier A defers to the SHIPPED predicate so this report can never drift from
    the live gate. Tier B re-runs the description half alone, for companies the
    gate skipped on the industry half.
    """
    if not description:
        return None
    kw = _is_integrator_service_provider(
        description, industry, industry_status or "confirmed"
    )
    if kw is not None:
        return ("A", kw)
    if _match_any(description, INTEGRATOR_MANUFACTURER_CARVEOUTS):
        return None
    hit = _find_first_match(
        description, INTEGRATOR_DESCRIPTION_KEYWORDS, word_boundary=True
    )
    return ("B", hit[2]) if hit is not None else None


@click.command()
@click.option(
    "--stage", default=None,
    help="Only audit entries at this pipeline stage (default: every stage).",
)
@click.option(
    "--out", type=click.Path(path_type=Path), default=DEFAULT_OUT,
    help=f"Report path (default: {DEFAULT_OUT}).",
)
def main(stage: str | None, out: Path) -> None:
    list_id = os.environ.get("ATTIO_LIST_ID", "")
    if not list_id:
        click.echo("ERROR: ATTIO_LIST_ID not set", err=True)
        raise SystemExit(1)

    with AttioClient() as attio:
        click.echo(f"Fetching entries in list {list_id} …")
        entries = attio.query_list_entries(list_id=list_id, limit=50000)
        parsed = [AttioClient.parse_entry(e) for e in entries]
        if stage:
            parsed = [p for p in parsed if (p.get("stage") or "") == stage]
        by_person = {p["record_id"]: p for p in parsed if p.get("record_id")}
        click.echo(f"  {len(by_person)} entries to scan. Fetching people …")

        persons = attio.bulk_fetch_persons_by_record_ids(set(by_person), max_workers=8)
        click.echo(f"  {len(persons)} people resolved. Fetching companies …")

        company_ids = {
            rid: cid
            for rid, rec in persons.items()
            if (cid := AttioClient.person_company_ref_id(rec))
        }
        companies: dict[str, dict] = {}
        for cid in sorted(set(company_ids.values())):
            # Per-company isolation: one bad record must not abort the sweep.
            try:
                rec = attio.get_company(cid, retry_500=False)
            except Exception as err:  # noqa: BLE001 — audit must not die on one row
                click.echo(f"  WARN: get_company({cid}) failed: "
                           f"{type(err).__name__}: {err}", err=True)
                continue
            if rec:
                companies[cid] = rec
        click.echo(f"  {len(companies)} companies resolved. Scanning …\n")

    rows: list[dict] = []
    skipped_corrupt = 0
    no_company = 0
    for rid, person in persons.items():
        cid = company_ids.get(rid)
        crec = companies.get(cid or "")
        if crec is None:
            no_company += 1
            continue
        # Same abstain the live gate applies: a Clearbit-corrupted record
        # carries LinkedIn's enrichment under the employer's name, so its
        # description describes LinkedIn, not this company.
        if is_linkedin_clearbit_corrupted(crec):
            skipped_corrupt += 1
            continue
        cv = crec.get("values", {})
        description = first_text_value(cv.get("description"))
        industry = first_option_title(cv.get("industry_vertical"))
        industry_status = first_option_title(cv.get("industry_vertical_status"))
        verdict = _describe_reason(description, industry, industry_status)
        if verdict is None:
            continue
        tier, kw = verdict
        title_items = (person.get("values", {}) or {}).get("job_title") or []
        rows.append({
            "tier": tier,
            "keyword": kw,
            "person_id": rid,
            "company_id": cid,
            "name": first_text_value((person.get("values", {}) or {}).get("name"))
                    or _person_name(person),
            "title": first_text_value(title_items),
            "company": first_text_value(cv.get("name")),
            "stage": by_person[rid].get("stage") or "",
            "industry_status": industry_status or "(none)",
            "categories": ", ".join(
                (c.get("option", {}) or {}).get("title", "")
                for c in (cv.get("categories") or []) if isinstance(c, dict)
            ),
            "industry_vertical": industry,
            "description": description,
        })

    rows.sort(key=lambda r: (r["tier"], r["company"].lower(), r["name"].lower()))
    _render(rows, out, scanned=len(persons), skipped_corrupt=skipped_corrupt,
            no_company=no_company, stage=stage)


def _person_name(person: dict) -> str:
    items = (person.get("values", {}) or {}).get("name") or []
    if items and isinstance(items[0], dict):
        first = items[0].get("first_name", "") or ""
        last = items[0].get("last_name", "") or ""
        return f"{first} {last}".strip()
    return ""


def _render(
    rows: list[dict], out: Path, *,
    scanned: int, skipped_corrupt: int, no_company: int, stage: str | None,
) -> None:
    tier_a = [r for r in rows if r["tier"] == "A"]
    tier_b = [r for r in rows if r["tier"] == "B"]

    lines: list[str] = []
    lines.append("# Integrator / service-provider audit — outreach pool\n")
    lines.append(
        "Read-only sweep. Nothing was written to the CRM. Every row below is a "
        "CANDIDATE for manual review, not a decision.\n"
    )
    lines.append(f"- Entries scanned: **{scanned}**"
                 + (f" (stage `{stage}` only)" if stage else " (all stages)"))
    lines.append(f"- Tier A — the live `disqualifier_integrator` gate would "
                 f"fire on these: **{len(tier_a)}**")
    lines.append(f"- Tier B — same description signal, industry_vertical is "
                 f"not an off-ICP label: **{len(tier_b)}**")
    lines.append(f"- Skipped, Clearbit-corrupted company record: {skipped_corrupt}")
    lines.append(f"- Skipped, no company record: {no_company}\n")

    for tier, group, blurb in (
        ("A", tier_a, "The gate's predicate matches exactly. These are the "
                      "backlog it would have caught."),
        ("B", tier_b, "Calibration lever, NOT gate misses. Expect large "
                      "automation-equipment makers that do run plants. If it "
                      "fills with real integrators, the fix is the industry "
                      "classifier, not more keywords."),
    ):
        lines.append(f"\n## Tier {tier} — {len(group)} candidates\n")
        lines.append(f"{blurb}\n")
        if not group:
            lines.append("_(none)_\n")
            continue
        by_stage = Counter(r["stage"] or "(no stage)" for r in group)
        lines.append("By stage: "
                     + ", ".join(f"{s} {n}" for s, n in by_stage.most_common()) + "\n")
        for r in group:
            lines.append(f"### {r['name']} — {r['company']}\n")
            lines.append(f"- Title: {r['title']}")
            lines.append(f"- Stage: **{r['stage'] or '(none)'}**")
            lines.append(f"- Matched description keyword: `{r['keyword']}`")
            lines.append(f"- industry_vertical_status: {r['industry_status']}")
            lines.append(f"- Categories: {r['categories'] or '(none)'}")
            lines.append(f"- industry_vertical: {r['industry_vertical'] or '(none)'}")
            lines.append(f"- Person: `{r['person_id']}` · Company: `{r['company_id']}`")
            lines.append(f"- Description: {r['description']}\n")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")

    click.echo(f"Tier A (gate would fire): {len(tier_a)}")
    click.echo(f"Tier B (watch / calibration): {len(tier_b)}")
    click.echo(f"Skipped corrupt company records: {skipped_corrupt}")
    for r in tier_a:
        click.echo(f"  A  {r['stage']:<14} {r['company'][:28]:<28} "
                   f"{r['name'][:24]:<24} kw={r['keyword']!r}")
    click.echo(f"\nReport written to {out}")
    click.echo("READ-ONLY — nothing written to the CRM.")


if __name__ == "__main__":
    main()
