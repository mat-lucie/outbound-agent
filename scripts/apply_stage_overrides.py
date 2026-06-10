"""Apply post-dedup stage overrides from a triaged report.

For each cluster in `people.approved_conflict_groups` that has a
`post_apply_stage_override` different from the winner's current list entry
stage, PATCH the winner entry to that stage. Used to flip "fallback_lost"
clusters to Not Interested after dedup has collapsed them.

Usage:
  python3 scripts/apply_stage_overrides.py --report exports/dedup-report-2026-04-21.json --dry-run
  python3 scripts/apply_stage_overrides.py --report exports/dedup-report-2026-04-21.json --apply
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent))

from clients.attio import AttioClient  # noqa: E402
from workflows.migration_run_writer import MigrationRunWriter  # noqa: E402


@click.command()
@click.option("--report", required=True, type=click.Path(exists=True))
@click.option("--apply", "do_apply", is_flag=True)
def main(report: str, do_apply: bool) -> int:
    data = json.loads(Path(report).read_text())
    approved = data.get("people", {}).get("approved_conflict_groups", [])

    targets: list[dict] = []
    for g in approved:
        target_stage = g.get("post_apply_stage_override")
        if not target_stage:
            continue
        reason = g.get("triage_reason")
        # Only override stages that are explicitly computed as different from
        # attio_max_stage. fallback_lost is the reason-of-interest here.
        if reason != "fallback_lost" and reason != "pb_bug1_catchup":
            continue
        targets.append({
            "url": g["linkedin_url_normalized"],
            "winner_record_id": g["winner_id"],
            "target_stage": target_stage,
            "reason": reason,
        })

    click.echo(f"Clusters needing stage override: {len(targets)}")
    for t in targets:
        click.echo(f"  {t['url'][:60]}  winner={t['winner_record_id'][:8]}  -> {t['target_stage']}  ({t['reason']})")

    if not targets:
        return 0

    if not do_apply:
        click.echo("\nDry-run — no writes. Re-run with --apply.")
        return 0

    with AttioClient() as attio, MigrationRunWriter(
        script_name="scripts/apply_stage_overrides.py",
        rollback_script_path=None,
        dry_run=not do_apply,
        attio=attio,
    ) as run:
        list_id = os.environ["ATTIO_LIST_ID"]
        # For each winner record, find its current list entry in the list
        entries = attio.query_list_entries(list_id=list_id, limit=50000)
        # Map parent_record_id → entry_id
        entry_by_record: dict[str, str] = {}
        for e in entries:
            parsed = AttioClient.parse_entry(e)
            rid = parsed.get("record_id")
            eid = parsed.get("entry_id")
            if rid and eid:
                entry_by_record[rid] = eid

        ok = 0
        fail = 0
        for t in targets:
            run.examine()
            entry_id = entry_by_record.get(t["winner_record_id"])
            if not entry_id:
                click.secho(
                    f"  FAIL {t['url']}: no list entry for record {t['winner_record_id'][:8]}",
                    fg="red",
                )
                run.mark_failed(
                    record_id=t["winner_record_id"],
                    error="no list entry for winner record_id",
                )
                fail += 1
                continue
            try:
                attio.update_list_entry(
                    entry_id=entry_id,
                    entry_attributes={"stage": t["target_stage"]},
                    list_id=list_id,
                )
                click.echo(f"  ✓ {t['url'][:60]} -> {t['target_stage']}")
                run.mark_modified(record_id=t["winner_record_id"])
                ok += 1
            except Exception as e:
                click.secho(f"  FAIL {t['url']}: {e}", fg="red")
                run.mark_failed(record_id=t["winner_record_id"], error=e)
                fail += 1
        click.echo(f"\nApplied: {ok}. Failed: {fail}. (Migration Run id={run.run_id})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
