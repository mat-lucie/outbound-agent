"""Retroactively reclassify personas on linkedin_outreach list entries.

Background: a bug (quality_gate.py:1147-1150, pre-fix) stamped the *search*
persona (e.g. ``digitalization_champions``) on every enterprise-mode prospect
regardless of title. ``_classify_persona`` was never called for enterprise rows.
The forward code fix has landed; this script corrects historical Attio data.

## What it does

For each entry in ``linkedin_outreach``:
  1. Fetch the linked person's ``job_title``.
  2. Re-run ``_classify_persona(title)`` (deterministic, no LLM cost).
  3. Compare to the entry's current ``persona``.
  4. If different AND stage is in the pre-DM allowlist (PROSPECT /
     PARTNER_INTRO / CONNECTION_SENT / ACCEPTED): PATCH ``persona`` via
     ``update_list_entry``.
  5. If different but stage is past the allowlist (DM1_SENT+ through terminal):
     COUNT and REPORT but do NOT write (mid-cadence flip would change the next
     DM template; terminal/responded rewrites are pointless churn).

Always writes a diff report to ``exports/persona_backfill_<today>.json``:
list of {entry_id, record_id, name, title, stage, old_persona, new_persona,
eligible: bool}. Written in BOTH dry-run and apply modes.

## Safety guarantees

- ``--dry-run`` is the default: no writes are issued; the report is still
  produced so the operator can inspect blast radius before committing.
- ``--apply`` is an explicit opt-in that enables Attio writes.
- Persona-only PATCH — ``stage`` is never included in the payload, so the
  AttioWriter monotonicity and terminal-class guards are never tripped.
- Abort-on-auth-failure: a 401/403 stops the run immediately (mirrors
  ``apply_industry_classifications.py``) rather than burning through remaining
  records with a bad key.
- Idempotent: re-running after ``--apply`` detects no changes and writes 0 rows.

## Usage

    python3 scripts/backfill_persona_reclassify.py            # dry-run (safe default)
    python3 scripts/backfill_persona_reclassify.py --apply    # write to Attio
"""
from __future__ import annotations

import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import click
import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent))

from clients.attio import AttioClient  # noqa: E402
from models.pipeline import PipelineStage  # noqa: E402
from workflows.quality_gate import _classify_persona  # noqa: E402
from workflows.reclassification_run_writer import ReclassificationRunWriter  # noqa: E402

# Bind parse_entry at import time as a free function so that patching
# AttioClient (the class) in tests does not accidentally shadow the static
# method when _build_diff_report calls it.
_parse_entry = AttioClient.parse_entry

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_NAME = "scripts/backfill_persona_reclassify.py"
SCRIPT_VERSION = "2026-05-31-v1"

# Stages eligible for persona PATCH — entries not yet in active DM cadence.
# Entries at DM1_SENT or later must NOT be touched: the DM template selection
# is keyed on persona and flipping mid-cadence would change the next message.
_SAFE_STAGES: frozenset[str] = frozenset(
    s.value for s in (
        PipelineStage.PROSPECT,
        PipelineStage.PARTNER_INTRO,
        PipelineStage.CONNECTION_SENT,
        PipelineStage.ACCEPTED,
    )
)

# The allowlist above is exactly the pre-DM stages (ranks 0-2). Every other
# stage — DM1_SENT+ through terminal (Responded / Not Interested / Qualified) —
# is excluded from writes: flipping persona on a contacted prospect risks a
# mid-cadence DM-template change, and rewriting terminal/responded rows is
# pointless churn.

# Only ENTERPRISE-mode entries were affected by the over-assignment bug, and
# _classify_persona ONLY emits these three personas. Midmarket personas
# (mx/co/cl_midmarket_manufacturing, assigned via target_company_mode) are
# correct and CANNOT be reproduced by _classify_persona — recomputing them would
# overwrite a correct label with an enterprise one. So the backfill only ever
# touches entries whose CURRENT persona is one of these.
_ENTERPRISE_PERSONAS: frozenset[str] = frozenset(
    {"digitalization_champions", "operations_leaders", "executive_sponsors"}
)


def _is_eligible_stage(stage_str: str) -> bool:
    """Return True iff this stage allows a persona PATCH (pre-DM allowlist).

    Any stage not in the allowlist — including unknown strings — is excluded,
    erring on the side of not writing.
    """
    return stage_str in _SAFE_STAGES


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _extract_title_from_person_record(record: dict) -> str:
    """Extract job_title string from an Attio person record. Returns '' if absent."""
    values = record.get("values", {})
    title_list = values.get("job_title", [])
    if title_list and isinstance(title_list[0], dict):
        return title_list[0].get("value", "") or ""
    return ""


def _extract_name_from_person_record(record: dict) -> str:
    """Extract display name from an Attio person record."""
    values = record.get("values", {})
    name_data = values.get("name", [])
    if name_data:
        first = name_data[0].get("first_name", "") or ""
        last = name_data[0].get("last_name", "") or ""
        return f"{first} {last}".strip()
    return ""


def _build_diff_report(
    entries: list[dict],
    attio: AttioClient,
) -> tuple[list[dict], dict[str, int]]:
    """Walk all entries, recompute persona, and return (diff, stats).

    ``diff`` holds one record per CHANGED entry:
        entry_id, record_id, name, title, stage,
        old_persona, new_persona, eligible: bool

    ``stats`` reconciles every examined entry into a disjoint bucket so the
    operator-facing summary accounts for all 695 rows (no silent drops):
        out_of_scope     — current persona not enterprise (midmarket/null/legacy)
        no_person        — linked person record missing/inaccessible (fetch drop)
        empty_title      — enterprise entry with a blank title (no signal)
        unchanged        — enterprise entry whose title re-derives the same persona
    The four skip buckets + len(diff) + rows missing a record_id == len(entries).
    """
    diff: list[dict] = []
    stats = {"out_of_scope": 0, "no_person": 0, "empty_title": 0, "unchanged": 0}

    for raw in entries:
        parsed = _parse_entry(raw)
        entry_id = parsed.get("entry_id", "")
        record_id = parsed.get("record_id", "")
        if not record_id:
            stats["out_of_scope"] += 1
            continue

        current_persona = parsed.get("persona") or ""

        # Guard: only reclassify entries currently carrying an ENTERPRISE persona.
        # Midmarket (target_company_mode) personas are correct and unreproducible
        # by _classify_persona; null/legacy personas are out of scope for this
        # bug. Touching either would corrupt or invent labels.
        if current_persona not in _ENTERPRISE_PERSONAS:
            stats["out_of_scope"] += 1
            continue

        stage = parsed.get("stage") or ""

        # Fetch the linked person record to get current job_title. A None here is
        # a real fetch failure (deleted/inaccessible record) — count it visibly
        # rather than letting it vanish into "unchanged".
        person = attio.get_person(record_id)
        if not person:
            stats["no_person"] += 1
            continue

        title = _extract_title_from_person_record(person)
        name = _extract_name_from_person_record(person)

        # No title signal — leave persona unchanged rather than defaulting it.
        if not title.strip():
            stats["empty_title"] += 1
            continue

        new_persona = _classify_persona(title)

        if new_persona == current_persona:
            stats["unchanged"] += 1
            continue

        eligible = _is_eligible_stage(stage)
        diff.append({
            "entry_id": entry_id,
            "record_id": record_id,
            "name": name,
            "title": title,
            "stage": stage,
            "old_persona": current_persona,
            "new_persona": new_persona,
            "eligible": eligible,
        })

    return diff, stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option(
    "--apply",
    "apply_mode",
    is_flag=True,
    default=False,
    help="Write persona corrections to Attio. Default is dry-run (no writes).",
)
def main(apply_mode: bool) -> int:
    """Backfill persona reclassification for linkedin_outreach list entries.

    Dry-run by default. Pass --apply to write changes to Attio.
    """
    dry_run = not apply_mode

    list_id = os.environ.get("ATTIO_LIST_ID", "").strip()
    if not list_id:
        click.echo(
            "ERROR: ATTIO_LIST_ID env var not set. "
            "Export ATTIO_LIST_ID=<uuid> and retry.",
            err=True,
        )
        return 1

    today = date.today().isoformat()
    exports_dir = Path(__file__).parent.parent / "exports"
    exports_dir.mkdir(exist_ok=True)
    report_path = exports_dir / f"persona_backfill_{today}.json"

    mode_label = "DRY-RUN" if dry_run else "APPLY"
    click.echo(f"[{mode_label}] persona backfill — list_id={list_id}")

    with AttioClient() as attio:
        # ── 1. Pull all list entries ──────────────────────────────────────
        click.echo("Fetching all linkedin_outreach entries…")
        try:
            entries = attio.query_list_entries(list_id=list_id)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                click.echo(
                    f"AUTH FAILURE ({e.response.status_code}) fetching list entries — "
                    "fix ATTIO_API_KEY and retry.",
                    err=True,
                )
                return 1
            raise
        click.echo(f"  {len(entries)} entries fetched.")

        # ── 2. Build diff report (sequential — persona_backfill blast radius is ≤~200) ─
        click.echo("Recomputing personas…")
        diff, stats = _build_diff_report(entries, attio)

        # Partition by eligibility
        eligible = [d for d in diff if d["eligible"]]
        excluded = [d for d in diff if not d["eligible"]]

        # ── 3. Write diff report JSON (always, both modes) ────────────────
        report_path.write_text(json.dumps(diff, indent=2, ensure_ascii=False))
        click.echo(f"Diff report written to {report_path}")
        click.echo(
            f"\nSummary (pre-write):\n"
            f"  Examined     : {len(entries)}\n"
            f"  Changed      : {len(diff)}\n"
            f"  Eligible     : {len(eligible)}\n"
            f"  Excl (stage) : {len(excluded)}\n"
            f"  Unchanged    : {stats['unchanged']}\n"
            f"  Out of scope : {stats['out_of_scope']} (midmarket/null persona)\n"
            f"  No person    : {stats['no_person']} (fetch failed — investigate if > 0)\n"
            f"  Empty title  : {stats['empty_title']} (no signal — left as-is)\n"
        )

        if excluded:
            click.echo(
                f"  NOTE: {len(excluded)} changed entr{'y' if len(excluded)==1 else 'ies'} "
                f"past the pre-DM allowlist — NOT written (avoids persona flips on "
                f"contacted/in-flight/terminal prospects):",
            )
            for row in excluded[:10]:
                click.echo(
                    f"    {row['name'] or row['record_id']} | stage={row['stage']} | "
                    f"{row['old_persona']} → {row['new_persona']}"
                )
            if len(excluded) > 10:
                click.echo(f"    … and {len(excluded) - 10} more (see report JSON).")

        if dry_run:
            click.echo("\n[DRY-RUN] No writes issued. Re-run with --apply to commit.")
            return 0

        if not eligible:
            click.echo("\nNo eligible entries to write. Nothing to do.")
            return 0

        # ── 4. Parallel writes (apply mode only) ─────────────────────────
        click.echo(f"\nWriting {len(eligible)} persona correction(s) to Attio…")

        written = 0
        errors = 0
        abort_event = threading.Event()
        progress_lock = threading.Lock()
        progress: dict[str, int] = {"done": 0}

        def _write_one(row: dict) -> tuple[dict, str | None]:
            if abort_event.is_set():
                return row, "ABORTED (auth failure on prior record)"
            try:
                with AttioClient() as worker_client:
                    worker_client.update_list_entry(
                        entry_id=row["entry_id"],
                        entry_attributes={"persona": row["new_persona"]},
                        list_id=list_id,
                    )
                with progress_lock:
                    progress["done"] += 1
                    if progress["done"] % 25 == 0:
                        click.echo(f"  Wrote {progress['done']}/{len(eligible)}…")
                return row, None
            except httpx.HTTPStatusError as err:
                if err.response.status_code in (401, 403):
                    abort_event.set()
                    return row, f"AUTH FAILURE — aborting run: HTTP {err.response.status_code}"
                return row, f"HTTPStatusError({err.response.status_code}): {err}"
            except Exception as err:  # noqa: BLE001
                return row, f"{type(err).__name__}: {err}"

        with ReclassificationRunWriter(
            classifier_module=SCRIPT_NAME,
            classifier_version=SCRIPT_VERSION,
            model_used="none (deterministic title-rule)",
            input_attr="job_title",
            output_attr="persona",
            dry_run=dry_run,
        ) as run:
            # Thread-safety note: pool.map results are serialised back to the
            # main thread, so all run.* counter calls below are single-threaded.
            with ThreadPoolExecutor(max_workers=8) as pool:
                for row, err in pool.map(_write_one, eligible):
                    run.examine()
                    if err is None:
                        run.mark_success(
                            record_id=row["record_id"],
                            object="linkedin_outreach",
                        )
                        written += 1
                    elif err.startswith("ABORTED"):
                        # Not a real failure — these rows were never attempted
                        # (an upstream auth failure tripped abort_event). Route
                        # them to mark_abstain so the error count reflects only
                        # the single root-cause auth failure, not the cascade.
                        run.mark_abstain(
                            record_id=row["record_id"],
                            reason="aborted_auth_failure",
                        )
                        errors += 1
                        click.echo(f"  SKIPPED {row['record_id']}: {err}", err=True)
                    else:
                        run.mark_error(record_id=row["record_id"], error=err)
                        errors += 1
                        click.echo(f"  ERROR on {row['record_id']}: {err}", err=True)

            click.echo(
                f"\nDone.\n"
                f"  Examined     : {len(entries)}\n"
                f"  Changed      : {len(diff)}\n"
                f"  Written      : {written}\n"
                f"  Excl (stage) : {len(excluded)}\n"
                f"  Unchanged    : {stats['unchanged']}\n"
                f"  Errors       : {errors}\n"
                f"  Run id       : {run.run_id}\n"
                f"  Report       : {report_path}\n"
            )
            if abort_event.is_set():
                click.echo(
                    "Run aborted due to auth failure — fix ATTIO_API_KEY and re-run.",
                    err=True,
                )

    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
