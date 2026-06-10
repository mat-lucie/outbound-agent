"""PR-39 — Backfill nurture_re_eligible_at on existing DM3_SENT entries.

Operator-facing CLI. Wraps the registry-authorized writer that lives at
``workflows.gtm.nurture_backfill``; this script just loads .env, opens
the AttioClient + AttioWriter, enumerates candidates, and dispatches.

Usage:
    python3 scripts/backfill_nurture_re_eligible_at.py            # live PATCH
    python3 scripts/backfill_nurture_re_eligible_at.py --dry-run  # print actions, no PATCH
    python3 scripts/backfill_nurture_re_eligible_at.py --limit N  # cap batch size
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

from clients.attio import AttioClient  # noqa: E402
from clients.attio_writer import AttioWriter  # noqa: E402
from workflows.gtm.nurture_backfill import (  # noqa: E402
    backfill_one,
    select_candidates,
)
from workflows.migration_run_writer import MigrationRunWriter  # noqa: E402


@click.command()
@click.option("--dry-run", is_flag=True, default=False, help="Print actions; no Attio writes.")
@click.option("--limit", type=int, default=None, help="Cap the number of entries processed.")
def main(dry_run: bool, limit: int | None) -> int:
    list_id = os.environ.get("ATTIO_LIST_ID", "").strip()
    if not list_id:
        click.echo("ERROR: ATTIO_LIST_ID unset.", err=True)
        sys.exit(2)

    with AttioClient() as attio:
        entries = attio.query_list_entries(list_id=list_id, limit=100_000)
        candidates = select_candidates(entries)
        if limit is not None:
            candidates = candidates[:limit]

        writer = AttioWriter(attio=attio) if not dry_run else None

        outcomes: Counter[str] = Counter()
        with MigrationRunWriter(
            script_name=Path(__file__).name,
            rollback_script_path=None,
            dry_run=dry_run,
            attio=attio,
        ) as run:
            for entry in candidates:
                run.examine()
                outcome = backfill_one(
                    entry, writer=writer, list_id=list_id, dry_run=dry_run,
                )
                outcomes[outcome] += 1

                entry_id = AttioClient.parse_entry(entry).get("entry_id", "") or "unknown"
                if outcome in ("patched", "would_patch"):
                    run.mark_modified(record_id=entry_id, object="linkedin_outreach")
                elif outcome == "skipped_corrupt_dm3_anchor":
                    run.mark_failed(record_id=entry_id, error=outcome)
                else:
                    run.skip_excluded(reason=outcome)

        click.echo(f"Candidates inspected: {len(candidates)}")
        for tag, n in sorted(outcomes.items()):
            click.echo(f"  {tag}: {n}")

    sys.exit(0)


if __name__ == "__main__":
    main()
