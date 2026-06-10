"""Collapse multi-list-entry-per-record duplicates in the linkedin_outreach list.

attio_dedup.py handles duplicate *person records*. It does NOT handle the
case where a single person record has multiple list entries in the same
list — a separate Attio-level duplicate that accumulates every time the
sales workflow re-adds a prospect who was already in the pipeline.

For each record with >1 list entry, keeps the most-advanced entry (highest
STAGE_RANK, tiebreak by oldest created_at) and deletes the rest.

Usage:
  python3 scripts/dedup_list_entries_per_record.py            # dry-run
  python3 scripts/dedup_list_entries_per_record.py --apply    # commit deletes
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import click
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent))

from clients.attio import AttioClient  # noqa: E402
from models.pipeline import PipelineStage  # noqa: E402
from workflows.migration_run_writer import MigrationRunWriter  # noqa: E402

# Stage ranking: highest = winner.
STAGE_RANK: dict[str, int] = {
    PipelineStage.PROSPECT.value: 0,
    PipelineStage.PARTNER_INTRO.value: 0,
    PipelineStage.CONNECTION_SENT.value: 1,
    PipelineStage.ACCEPTED.value: 2,
    PipelineStage.DM1_SENT.value: 3,
    PipelineStage.DM2_SENT.value: 4,
    PipelineStage.DM3_SENT.value: 5,
    PipelineStage.RESPONDED.value: 10,
    PipelineStage.CALL_BOOKED.value: 11,
    PipelineStage.QUALIFIED.value: 12,
    PipelineStage.NOT_INTERESTED.value: 10,
}


@click.command()
@click.option("--apply", "do_apply", is_flag=True)
def main(do_apply: bool) -> int:
    with AttioClient() as attio:
        list_id = os.environ["ATTIO_LIST_ID"]
        entries_raw = attio.query_list_entries(list_id=list_id, limit=50000)
        by_record: dict[str, list[dict]] = {}
        parsed_by_entry: dict[str, dict] = {}
        for raw in entries_raw:
            p = AttioClient.parse_entry(raw)
            parsed_by_entry[p["entry_id"]] = p
            by_record.setdefault(p["record_id"], []).append(p)

        multi = {r: es for r, es in by_record.items() if len(es) > 1}
        click.echo(f"Records with >1 list entry: {len(multi)}")
        if not multi:
            return 0

        total_delete = 0
        winners: list[tuple[str, str, list[str]]] = []  # (record_id, winner_entry, losers)

        for rid, entries in multi.items():
            def key(e: dict) -> tuple[int, str]:
                rank = STAGE_RANK.get(e.get("stage", ""), 0)
                return (-rank, e.get("created_at") or "")

            sorted_entries = sorted(entries, key=key)
            winner = sorted_entries[0]
            losers = sorted_entries[1:]
            winners.append((rid, winner["entry_id"], [e["entry_id"] for e in losers]))
            total_delete += len(losers)

        click.echo(f"Will delete {total_delete} duplicate list entries (keeping one per record).")

        if not do_apply:
            click.echo("\nSample (first 10):")
            for rid, w, losers in winners[:10]:
                wp = parsed_by_entry[w]
                loser_stages = ", ".join(
                    f"{parsed_by_entry[le]['stage']}({le[:8]})" for le in losers
                )
                click.echo(f"  record {rid[:8]}: keep {wp['stage']}({w[:8]})  delete {loser_stages}")
            click.echo("\nDry-run — re-run with --apply.")
            return 0

        ok = 0
        fail = 0
        with MigrationRunWriter(
            script_name="scripts/dedup_list_entries_per_record.py",
            rollback_script_path=None,
            dry_run=not do_apply,
            attio=attio,
        ) as run:
            for _rid, _winner_eid, losers in winners:
                for loser_eid in losers:
                    run.examine()
                    try:
                        attio.delete_list_entry(loser_eid, list_id=list_id)
                        # Wave-2-C fix-up (multi-agent C1): loser_eid is
                        # a list-entry id of a row being DELETED. Use
                        # the list_entry sentinel so MigrationRunWriter
                        # skips the back-pointer PATCH (which would 404
                        # on the deleted entry).
                        run.mark_modified(record_id=loser_eid, object="list_entry")
                        ok += 1
                    except Exception as e:
                        click.secho(f"  FAIL entry {loser_eid[:8]}: {e}", fg="red")
                        run.mark_failed(record_id=loser_eid, error=e)
                        fail += 1
            click.echo(
                f"\nDeleted: {ok}. Failed: {fail}. "
                f"(Migration Run id={run.run_id})"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
