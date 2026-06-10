"""Apply Claude-Code-session industry classifications back to Attio.

Reads a JSON file mapping record_id → industry label and PATCHes each
company record. Idempotent: re-running with the same input is safe; Attio
update_company is a PATCH so unspecified fields remain unchanged.

Marks each record with industry_source="claude_session" and
industry_classified_at=today so the source/timestamp story stays intact even
when classification was offloaded to the Claude Code operator.

Usage:
  python3 scripts/apply_industry_classifications.py --input <classifications.json>
  python3 scripts/apply_industry_classifications.py --input <file.json> --dry-run
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import click
import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent))

from clients.attio import AttioClient  # noqa: E402
from workflows.migration_run_writer import MigrationRunWriter  # noqa: E402

VALID_LABELS = {
    "Manufacturing", "Food & Beverage", "Construction", "Oil & Gas", "Mining",
    "Automotive", "Textiles", "Packaging", "Chemicals", "Pharma", "Other",
}


@click.command()
@click.option("--input", "input_path", required=True, help="JSON file: {record_id: label}.")
@click.option("--dry-run", is_flag=True, help="Validate input but skip Attio writes.")
def main(input_path: str, dry_run: bool) -> int:
    src = Path(input_path)
    if not src.exists():
        click.echo(f"ERROR: {src} not found", err=True)
        return 1

    classifications: dict[str, str] = json.loads(src.read_text())
    if not isinstance(classifications, dict):
        click.echo(f"ERROR: expected dict {{record_id: label}}, got {type(classifications).__name__}", err=True)
        return 1

    invalid = {rid: lbl for rid, lbl in classifications.items() if lbl not in VALID_LABELS}
    if invalid:
        click.echo(f"ERROR: {len(invalid)} entries have invalid labels:", err=True)
        for rid, lbl in list(invalid.items())[:10]:
            click.echo(f"  {rid}: {lbl!r}", err=True)
        return 1

    click.echo(f"{len(classifications)} classifications to apply (dry_run={dry_run})")

    today = date.today().isoformat()
    written = 0
    errors = 0

    if dry_run:
        for i, (record_id, label) in enumerate(classifications.items(), 1):
            if i <= 5 or i % 50 == 0:
                click.echo(f"  [dry-run] {record_id} → {label}")
        click.echo("\nDone. Written: 0 (dry-run)")
        return 0

    # Parallel writes: Attio PATCH latency dominates, so 8 concurrent workers
    # cuts wall-clock from ~60min to ~7min for ~1800 records. Skip-on-existing
    # logic in classify_industry already prevents redoing classifications, so
    # re-running this script is safe.
    import threading
    from concurrent.futures import ThreadPoolExecutor

    progress_lock = threading.Lock()
    progress = {"done": 0}
    abort_event = threading.Event()  # Tripped on auth failure — stop the run

    def _write_one(rid_label: tuple[str, str]) -> tuple[str, str | None]:
        rid, label = rid_label
        if abort_event.is_set():
            return rid, "ABORTED (auth failure on prior record)"
        try:
            with AttioClient() as worker_client:
                worker_client.update_company(
                    rid,
                    {
                        "industry_vertical": label,
                        "industry_source": "claude_session",
                        "industry_classified_at": today,
                    },
                )
            with progress_lock:
                progress["done"] += 1
                if progress["done"] % 50 == 0:
                    click.echo(f"  Wrote {progress['done']}/{len(classifications)}…", nl=True)
            return rid, None
        except httpx.HTTPStatusError as err:
            if err.response.status_code in (401, 403):
                # Auth failure — abort the whole run instead of burning
                # through the remaining ~1800 records with the same bad key.
                abort_event.set()
                return rid, f"AUTH FAILURE — aborting run: HTTP {err.response.status_code}"
            return rid, f"HTTPStatusError({err.response.status_code}): {err}"
        except Exception as err:  # noqa: BLE001
            return rid, f"{type(err).__name__}: {err}"

    items = list(classifications.items())
    with MigrationRunWriter(
        script_name="scripts/apply_industry_classifications.py",
        rollback_script_path=None,
        dry_run=dry_run,
    ) as run:
        # Wave-2-C fix-up (multi-agent C2): the ABORTED cascade is a
        # different semantic from a real per-record failure — these
        # rows were never even attempted against Attio because an
        # upstream auth failure tripped `abort_event`. Route them to
        # `skip_excluded` so the operator-facing `rows_failed` counter
        # reflects only real failures (one auth fail, not ~1800
        # cascade-induced skips).
        # Thread-safety note: pool.map serializes results back to the
        # main thread, so all counter/writer method calls in the loop
        # below run single-threaded. Do NOT refactor to as_completed
        # with worker callbacks without adding a lock to
        # MigrationRunWriter's counters.
        with ThreadPoolExecutor(max_workers=8) as pool:
            for rid, err in pool.map(_write_one, items):
                run.examine()
                if err is None:
                    run.mark_modified(record_id=rid, object="companies")
                    written += 1
                elif err.startswith("ABORTED"):
                    run.skip_excluded(
                        reason="aborted after upstream auth failure",
                    )
                    errors += 1
                    click.echo(f"  SKIPPED on {rid}: {err}", err=True)
                else:
                    run.mark_failed(record_id=rid, error=err)
                    errors += 1
                    click.echo(f"  ERROR on {rid}: {err}", err=True)

        click.echo(
            f"\nDone. Written: {written}, Errors: {errors}. "
            f"(Migration Run id={run.run_id})"
        )
        if abort_event.is_set():
            click.echo(
                "Run aborted due to auth failure — fix ATTIO_API_KEY and re-run.",
                err=True,
            )
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
