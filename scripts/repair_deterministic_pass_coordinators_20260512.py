"""Repair: flip deterministic-pass Junior/IC entries to Not Interested.

Reads the CSV produced by audit_deterministic_pass_coordinators_20260512.py
and updates each Attio list entry, attaching an audit note documenting the
verdict_path bug that let them through.

Preview: python3 scripts/repair_deterministic_pass_coordinators_20260512.py
Apply:   python3 scripts/repair_deterministic_pass_coordinators_20260512.py --apply

Safety:
- Already-terminal stages (Responded / Not Interested / Call Booked /
  Qualified) are skipped even if they appear in the CSV — operator
  already made a decision there.
- Each repair attaches a Note titled "Reclassified — junior/IC role
  (2026-05-12 backfix)" so the change is auditable.
"""
from __future__ import annotations

import csv
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).parent.parent / ".env")

import click  # noqa: E402

from clients.attio import AttioClient  # noqa: E402
from models.pipeline import PipelineStage  # noqa: E402
from workflows.migration_run_writer import MigrationRunWriter  # noqa: E402

CSV_PATH = (
    Path(__file__).parent.parent
    / "exports"
    / f"deterministic_pass_coordinators_{date.today().isoformat()}.csv"
)

TERMINAL_STAGES = {
    PipelineStage.RESPONDED.value,
    PipelineStage.NOT_INTERESTED.value,
    PipelineStage.CALL_BOOKED.value,
    PipelineStage.QUALIFIED.value,
}

NOTE_TITLE = "Reclassified — junior/IC role (2026-05-12 backfix)"
NOTE_TEMPLATE = (
    "This prospect was scored as deterministic_pass via {verdict_path} "
    "and added to the pipeline despite holding a Junior/IC title "
    '("{title}"). The 2026-05-12 QA cleanup added the Haiku qualifier '
    "prompt's IC-disqualifier to the deterministic scorer; this entry "
    "is one of the historical cases where the two paths silently "
    "disagreed. Stage flipped to Not Interested to stop the sequence; "
    "the person record stays so future weekly runs do not re-prospect."
)


@click.command()
@click.option("--apply", "apply_changes", is_flag=True,
              help="Actually write to Attio (default: preview only).")
@click.option("--csv", "csv_path_override", type=click.Path(exists=True),
              help="Use a different audit CSV (defaults to today's file).")
def main(apply_changes: bool, csv_path_override: str | None) -> None:
    list_id = os.environ.get("ATTIO_LIST_ID", "")
    if not list_id:
        click.echo("ERROR: ATTIO_LIST_ID not set", err=True)
        sys.exit(1)

    csv_path = Path(csv_path_override) if csv_path_override else CSV_PATH
    if not csv_path.exists():
        click.echo(
            f"ERROR: {csv_path} not found. Run the audit script first.",
            err=True,
        )
        sys.exit(1)

    with csv_path.open() as f:
        rows = list(csv.DictReader(f))

    mode = "APPLY" if apply_changes else "PREVIEW (no writes)"
    click.echo(f"=== Repair: deterministic-pass coordinators {mode} ({len(rows)} candidates) ===\n")

    applied = 0
    skipped_terminal = 0
    flip_failed = 0
    note_failed = 0
    note_failed_labels: list[str] = []

    with AttioClient() as attio, MigrationRunWriter(
        script_name="scripts/repair_deterministic_pass_coordinators_20260512.py",
        rollback_script_path=None,
        dry_run=not apply_changes,
        attio=attio,
    ) as run:
        for row in rows:
            run.examine()
            name = row.get("name", "?")
            company = row.get("company", "?")
            title = row.get("title", "?")
            stage = row.get("stage", "")
            entry_id = row.get("entry_id", "")
            record_id = row.get("record_id", "")

            if stage in TERMINAL_STAGES:
                skipped_terminal += 1
                run.skip_excluded(reason="terminal stage — operator-owned")
                continue

            label = f"{name} ({company}) — {title} — {stage}"
            if not apply_changes:
                click.echo(f"  [PREVIEW] {label}")
                continue

            # Two-step write: flip stage first, then attach note. If flip
            # raises the entry is unchanged (retry safe). If the note raises
            # the entry IS already flipped — track separately so the operator
            # can manually backfill the note instead of re-running.
            try:
                attio.update_list_entry(
                    entry_id=entry_id,
                    entry_attributes={"stage": PipelineStage.NOT_INTERESTED.value},
                    list_id=list_id,
                )
                # Wave-2-C fix-up (multi-agent C1): entry_id is a
                # list-entry id; use the list_entry sentinel so the
                # back-pointer PATCH doesn't 404.
                run.mark_modified(record_id=entry_id, object="list_entry")
            except Exception as e:  # noqa: BLE001 — best-effort batch
                flip_failed += 1
                run.mark_failed(record_id=entry_id, error=e)
                click.echo(f"  ✗ flip-failed {label} — {e}", err=True)
                continue

            try:
                attio.create_note(
                    record_id=record_id,
                    title=NOTE_TITLE,
                    content=NOTE_TEMPLATE.format(
                        verdict_path=row.get("verdict_path", "?"),
                        title=title,
                    ),
                )
            except Exception as e:  # noqa: BLE001 — best-effort batch
                note_failed += 1
                note_failed_labels.append(label)
                click.echo(
                    f"  ⚠ stage flipped but note failed — manual note required for "
                    f"{label} — {e}",
                    err=True,
                )
                continue

            applied += 1
            click.echo(f"  ✓ {label}")

    click.echo()
    click.echo("--- Summary ---")
    click.echo(f"Applied (flip + note):       {applied}")
    click.echo(f"Skipped (terminal stage):    {skipped_terminal}")
    click.echo(f"Flip failed (no change):     {flip_failed}")
    click.echo(f"Flip OK, note failed:        {note_failed}")
    if note_failed_labels:
        click.echo()
        click.echo("Manual note backfill needed for:")
        for lbl in note_failed_labels:
            click.echo(f"  - {lbl}")
    if not apply_changes:
        click.echo()
        click.echo("This was a PREVIEW run. Re-run with --apply to write to Attio.")


if __name__ == "__main__":
    main()
