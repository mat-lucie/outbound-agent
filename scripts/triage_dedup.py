"""Interactive triage for attio_dedup conflict groups.

Walks every unresolved conflict_group in a dedup_report.json, pulls the
winner + loser person records from Attio, shows name / company / job
title / email / stage / last_contact_date / persona / created_at side-by-
side, and lets you approve, skip, change the winner, or quit.

Writes decisions back into the report file so `attio_dedup.py apply` can
execute them.

Usage:
  python3 scripts/triage_dedup.py --report exports/dedup-report-2026-04-21.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent))

from clients.attio import AttioClient  # noqa: E402


def _first(values):
    if isinstance(values, list) and values:
        return values[0]
    return values


def _extract(val_list, key="value"):
    v = _first(val_list)
    if isinstance(v, dict):
        return v.get(key) or v.get("value") or v.get("title") or ""
    return v or ""


def record_facts(attio: AttioClient, company_cache: dict, record_id: str) -> dict:
    rec = attio.get_person(record_id)
    if not rec:
        return {"record_id": record_id, "name": "(missing)", "company": "", "email": "",
                "job_title": "", "created_at": "", "linkedin": ""}
    vals = rec.get("values", {})
    name = ""
    name_v = _first(vals.get("name", []))
    if isinstance(name_v, dict):
        name = name_v.get("full_name") or name_v.get("first_name", "") + " " + name_v.get("last_name", "")
    email = _extract(vals.get("email_addresses", []), key="email_address")
    job = _extract(vals.get("job_title", []))
    linkedin = _extract(vals.get("linkedin", []))
    created = _extract(rec.get("created_at")) if isinstance(rec.get("created_at"), list) else rec.get("created_at", "")
    # Company — target_record_id → look up name
    company = ""
    cv = _first(vals.get("company", []))
    if isinstance(cv, dict):
        cid = cv.get("target_record_id")
        if cid:
            if cid not in company_cache:
                crec = attio.get_company(cid)
                if crec:
                    company_cache[cid] = _extract(crec.get("values", {}).get("name", []))
                else:
                    company_cache[cid] = cid[:8] + "…"
            company = company_cache[cid]
    return {
        "record_id": record_id,
        "name": str(name).strip() or "?",
        "company": company,
        "email": email or "",
        "job_title": job or "",
        "created_at": str(created)[:10],
        "linkedin": linkedin or "",
    }


def list_entry_facts(parsed: dict) -> dict:
    """Compact summary of a linkedin_outreach list entry (from AttioClient.parse_entry)."""
    return {
        "entry_id": parsed.get("entry_id", ""),
        "stage": parsed.get("stage", ""),
        "persona": parsed.get("persona", ""),
        "language": parsed.get("language", ""),
        "last_contact_date": str(parsed.get("last_contact_date") or "")[:10],
        "dm_step": parsed.get("dm_step", ""),
        "quality_score": parsed.get("quality_score", ""),
    }


def fetch_list_entries_by_record(attio: AttioClient, list_id: str) -> dict[str, list[dict]]:
    entries = attio.query_list_entries(list_id=list_id)
    by_record: dict[str, list[dict]] = {}
    for e in entries:
        parsed = AttioClient.parse_entry(e)
        pid = parsed.get("record_id")
        if pid:
            by_record.setdefault(pid, []).append(parsed)
    return by_record


def render_group(group: dict, winner_facts: dict, loser_facts: list[dict],
                 entries_by_record: dict[str, list[dict]]) -> None:
    click.echo()
    click.secho(f"═══ {group['linkedin_url_normalized']} ═══", fg="cyan", bold=True)
    reasons = group.get("conflict_reasons", []) or []
    if reasons:
        click.echo("Conflict reasons:")
        for r in reasons:
            click.echo(f"  • {r}")
    click.echo()
    rows = [("WINNER", winner_facts)] + [("loser", f) for f in loser_facts]
    for role, f in rows:
        marker = "★" if role == "WINNER" else " "
        color = "green" if role == "WINNER" else "yellow"
        click.secho(
            f" {marker} {f['name'][:28]:28} | {f['company'][:20]:20} | {f['job_title'][:25]:25} | "
            f"{(f['email'] or '-')[:30]:30} | created={f['created_at']} | id={f['record_id'][:8]}",
            fg=color,
        )
        for entry in entries_by_record.get(f["record_id"], []):
            ef = list_entry_facts(entry)
            click.echo(
                f"       └─ stage={ef['stage']:16} persona={str(ef['persona'])[:22]:22} "
                f"lang={ef['language']:4} last={ef['last_contact_date']} dm_step={ef['dm_step']} "
                f"entry={str(ef['entry_id'])[:8]}"
            )


def save_report(path: Path, report: dict) -> None:
    path.write_text(json.dumps(report, indent=2))


def status_of(group: dict, approved_ids: set[str], skipped_ids: set[str]) -> str:
    key = group["linkedin_url_normalized"]
    if key in approved_ids:
        return "APPROVED"
    if key in skipped_ids:
        return "SKIPPED"
    return "PENDING"


@click.command()
@click.option("--report", required=True, type=click.Path(exists=True))
@click.option("--skip-approved", is_flag=True, default=True, help="Hide already-triaged groups (default).")
def main(report: str, skip_approved: bool) -> None:
    path = Path(report)
    data = json.loads(path.read_text())
    people = data.setdefault("people", {})
    approved = people.setdefault("approved_conflict_groups", [])
    skipped = people.setdefault("skipped_groups", [])
    conflict = people.get("conflict_groups", [])

    approved_keys = {g["linkedin_url_normalized"] for g in approved}
    skipped_keys = {g["linkedin_url_normalized"] for g in skipped}

    pending = [
        g for g in conflict
        if g["linkedin_url_normalized"] not in approved_keys
        and g["linkedin_url_normalized"] not in skipped_keys
    ] if skip_approved else list(conflict)

    click.echo(f"Report: {path.name}")
    click.echo(f"Total conflict groups: {len(conflict)}  |  already approved: {len(approved)}  |  "
               f"already skipped: {len(skipped)}  |  pending this session: {len(pending)}")
    if not pending:
        click.echo("Nothing pending. 🎉")
        return

    with AttioClient() as attio:
        list_id = __import__("os").environ["ATTIO_LIST_ID"]
        click.echo("Fetching list entries…")
        entries_by_record = fetch_list_entries_by_record(attio, list_id)
        company_cache: dict[str, str] = {}

        for i, group in enumerate(pending):
            click.echo()
            click.secho(f"── [{i + 1}/{len(pending)}] ──", bold=True)
            try:
                winner_facts = record_facts(attio, company_cache, group["winner_id"])
                loser_facts = [record_facts(attio, company_cache, rid) for rid in group["loser_ids"]]
            except Exception as e:
                click.secho(f"  Error fetching records: {e} — skipping", fg="red")
                continue

            while True:
                render_group(group, winner_facts, loser_facts, entries_by_record)
                click.echo()
                choice = click.prompt(
                    "  [a]pprove / [s]kip / [w]inner <record_id_prefix> / [i]nspect / [q]uit",
                    default="a", show_default=False,
                ).strip().lower()

                if choice in ("a", "approve"):
                    approved.append(group)
                    click.secho("  ✓ approved", fg="green")
                    save_report(path, data)
                    break
                if choice in ("s", "skip"):
                    skipped.append(group)
                    click.secho("  ✗ skipped", fg="yellow")
                    save_report(path, data)
                    break
                if choice.startswith("w"):
                    parts = choice.split()
                    if len(parts) < 2:
                        click.echo("  Usage: w <prefix of record_id to become winner>")
                        continue
                    prefix = parts[1]
                    candidates = [group["winner_id"]] + list(group["loser_ids"])
                    match = next((c for c in candidates if c.startswith(prefix)), None)
                    if not match:
                        click.secho("  No matching record id", fg="red")
                        continue
                    if match == group["winner_id"]:
                        click.echo("  That's already the winner.")
                        continue
                    new_losers = [c for c in candidates if c != match]
                    group["winner_id"] = match
                    group["loser_ids"] = new_losers
                    # Re-plan list entry actions would require re-calling the
                    # dedup helpers; for now we just swap the id lists and
                    # annotate so apply-step can rebuild.
                    group["_winner_overridden"] = True
                    click.secho(f"  Winner -> {match}. Re-run attio_dedup scan after triage to regenerate list_entry_actions.", fg="magenta")
                    # Refresh facts
                    winner_facts = record_facts(attio, company_cache, group["winner_id"])
                    loser_facts = [record_facts(attio, company_cache, rid) for rid in group["loser_ids"]]
                    save_report(path, data)
                    continue
                if choice in ("i", "inspect"):
                    click.echo(json.dumps(group, indent=2, default=str)[:4000])
                    continue
                if choice in ("q", "quit"):
                    click.echo("Exiting. Progress saved.")
                    return
                click.echo("  Unknown option.")
    click.echo(f"\nDone. {len(approved)} approved, {len(skipped)} skipped. Report: {path}")


if __name__ == "__main__":
    main()
