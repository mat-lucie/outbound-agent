"""LinkedIn-thread-based cadence reconstructor (2026-05-11).

Treats LinkedIn message threads as the source of truth and reconciles Attio
state against them. Fixes the cadence corruption caused by the 2026-04-21
dedup + 2026-05-03 weekly_prospect bug. The Attio `dm_step` field is
unreliable across the entire pre-2026-04-26 cohort (older script versions
didn't always write it, and the dedup deleted the entries that did).

Algorithm:
  1. Pull all current pipeline entries from Attio.
  2. Launch PB SN Inbox Scraper with numberOfThreadsToScrape=200.
  3. For each scraped thread:
       - Match `participantProfileUrl` to a pipeline entry by normalized URL.
       - Infer cadence:
           * isLastMessageFromMe=true, totalMessageCount=N → dm_step=N,
             stage=DM{N}_SENT (or stays Accepted if N=0).
           * isLastMessageFromMe=false → stage=RESPONDED, dm_step preserved
             from current entry (we can't infer outbound count from a thread
             that ends with their reply via this signal alone).
       - If no matching thread found: prospect has had no message exchange,
         so dm_step must be 0 and stage cannot be DM{N}_SENT/RESPONDED.
  4. Compare against current Attio state, emit a delta report:
       - REGRESSED: thread evidence shows higher cadence than current → repair
       - STALE: current state shows cadence but thread evidence shows none →
         likely terminal or LinkedIn account issue; flag for manual review
       - MATCH: no discrepancy
  5. With --apply: write the REGRESSED repairs to Attio + audit notes.

Read-only by default. Outputs:
  - Console summary
  - exports/cadence_reconstruction_2026-05-11.csv (full delta report)

Run preview:
  python3 scripts/reconstruct_cadence_from_linkedin_20260511.py
Apply (REGRESSED rows only — STALE flagged for manual review):
  python3 scripts/reconstruct_cadence_from_linkedin_20260511.py --apply
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
from clients.phantombuster import PhantomBusterClient  # noqa: E402
from models.pipeline import PipelineStage  # noqa: E402
from workflows.daily_check import (  # noqa: E402
    RecordCache,
    _get_all_entries_parsed,
    _normalize_linkedin_url,
    _pb_session_args,
    preload_pipeline_persons,
)
from workflows.migration_run_writer import MigrationRunWriter  # noqa: E402

OUT_CSV = Path(__file__).parent.parent / "exports" / "cadence_reconstruction_2026-05-11.csv"
NUM_THREADS_TO_SCRAPE = 200  # the operator reports ~150 SN conversations; 200 buffers for new ones.

# Stage that corresponds to each dm_step (when last message is from us).
STAGE_FOR_DM_STEP = {
    0: PipelineStage.ACCEPTED.value,
    1: PipelineStage.DM1_SENT.value,
    2: PipelineStage.DM2_SENT.value,
    3: PipelineStage.DM3_SENT.value,
}

# Stages we consider "thread-bearing" (i.e. a LinkedIn thread should exist).
# If a record is in one of these stages but no thread is found, it's STALE.
THREAD_BEARING_STAGES = {
    PipelineStage.DM1_SENT.value,
    PipelineStage.DM2_SENT.value,
    PipelineStage.DM3_SENT.value,
    PipelineStage.RESPONDED.value,
}

# Terminal stages — operator made a deliberate decision (replied, no-interest,
# booked, qualified). Never automatically flip OUT of these based on thread
# state, even if the thread shows a different cadence. Only update dm_step
# inside the terminal stage if it's lower than thread evidence.
TERMINAL_STAGES = {
    PipelineStage.RESPONDED.value,
    PipelineStage.NOT_INTERESTED.value,
    PipelineStage.CALL_BOOKED.value,
    PipelineStage.QUALIFIED.value,
}


def _normalize_name(s: str) -> str:
    return "".join(c.lower() for c in (s or "") if c.isalnum())


def _infer_cadence(total_count: int, is_from_me: bool) -> tuple[int, str]:
    """Infer (dm_step, stage) from thread signals."""
    if total_count == 0:
        return 0, PipelineStage.ACCEPTED.value
    if not is_from_me:
        # Prospect replied. Stage = Responded. dm_step is the count of our
        # outbound messages, which we approximate as max(1, total - 1) since
        # the thread ends with their message. Cap at 3 (our cadence max).
        return min(max(total_count - 1, 1), 3), PipelineStage.RESPONDED.value
    # Last message is from us; whole thread is outbound. dm_step = total.
    return min(total_count, 3), STAGE_FOR_DM_STEP.get(min(total_count, 3), PipelineStage.DM3_SENT.value)


@click.command()
@click.option("--apply", "apply_changes", is_flag=True, help="Write REGRESSED repairs to Attio. STALE rows always flagged-only.")
@click.option("--inbox-scraper-id", envvar="PB_INBOX_SCRAPER_ID", help="PB SN Inbox Scraper agent ID")
@click.option("--num-threads", default=NUM_THREADS_TO_SCRAPE, help=f"numberOfThreadsToScrape (default {NUM_THREADS_TO_SCRAPE})")
def main(apply_changes: bool, inbox_scraper_id: str | None, num_threads: int) -> None:
    list_id = os.environ.get("ATTIO_LIST_ID", "")
    if not list_id:
        click.echo("ERROR: ATTIO_LIST_ID not set", err=True)
        sys.exit(1)
    if not inbox_scraper_id:
        click.echo("ERROR: PB_INBOX_SCRAPER_ID not set", err=True)
        sys.exit(1)

    mode = "APPLY (REGRESSED repairs)" if apply_changes else "PREVIEW (no writes)"
    click.echo(f"=== Cadence reconstruction {mode} ===\n")

    # 1. Pull current pipeline state
    click.echo("Loading current Attio pipeline...")
    with AttioClient() as attio, PhantomBusterClient() as pb:
        all_parsed = _get_all_entries_parsed(attio)
        cache = RecordCache(attio)
        preload_pipeline_persons(attio, cache, {a["record_id"] for a in all_parsed})
        click.echo(f"  Loaded {len(all_parsed)} pipeline entries.")

        # Build URL → entries index (multiple entries can share a URL)
        url_to_entries: dict[str, list[dict]] = {}
        name_to_entries: dict[str, list[dict]] = {}
        for attrs in all_parsed:
            name, company, url, _, _ = cache.get(attrs["record_id"])
            if url:
                norm_url = _normalize_linkedin_url(url)
                attrs["_url"] = url
                attrs["_normalized_url"] = norm_url
                attrs["_name"] = name or ""
                attrs["_company"] = company or ""
                url_to_entries.setdefault(norm_url, []).append(attrs)
            if name:
                name_to_entries.setdefault(_normalize_name(name), []).append(attrs)

        click.echo(f"  Indexed {len(url_to_entries)} unique URLs, {len(name_to_entries)} unique normalized names.\n")

        # 2. Launch Inbox Scraper
        click.echo(f"Launching SN Inbox Scraper (numberOfThreadsToScrape={num_threads})...")
        launch = pb.launch_agent(inbox_scraper_id, {
            "inboxFilter": "all",
            "numberOfThreadsToScrape": num_threads,
            **_pb_session_args(),
        })
        pb.wait_for_completion(launch, poll_interval=15, max_wait=900)

        # F-PR-5: CSV keyed to launch.container_id, not "latest".
        result_csv = pb.download_result_csv(launch)
        if not result_csv:
            click.echo("ERROR: Inbox Scraper returned no CSV.", err=True)
            sys.exit(1)

        import io
        reader = csv.DictReader(io.StringIO(result_csv))
        threads = list(reader)
        click.echo(f"  Scraper returned {len(threads)} threads.\n")

        # 3. Reconcile each thread against pipeline
        deltas: list[dict] = []
        unmatched_threads: list[dict] = []
        matched_url_set: set[str] = set()

        for t in threads:
            url = (t.get("participantProfileUrl") or "").strip()
            name = (t.get("participantFullName") or "").strip()
            is_from_me_raw = (t.get("isLastMessageFromMe") or "").strip().lower()
            is_from_me = is_from_me_raw == "true"
            try:
                total = int((t.get("totalMessageCount") or "0").strip())
            except ValueError:
                total = 0

            # Match by URL first (most reliable). SN URLs have a different
            # form (linkedin.com/sales/people/...) than the
            # linkedin.com/in/ form stored in Attio — fall back to name match.
            entries = None
            if url:
                norm = _normalize_linkedin_url(url)
                entries = url_to_entries.get(norm)
            if entries is None and name:
                entries = name_to_entries.get(_normalize_name(name))

            if not entries:
                unmatched_threads.append({"name": name, "url": url, "totalMessageCount": total, "isLastMessageFromMe": is_from_me})
                continue

            inferred_step, inferred_stage = _infer_cadence(total, is_from_me)

            for attrs in entries:
                matched_url_set.add(attrs.get("_normalized_url", ""))
                cur_step = int(attrs.get("dm_step") or 0)
                cur_stage = attrs.get("stage") or ""

                # Terminal stages are sticky — never flip stage. Only update
                # dm_step within the terminal stage if thread shows higher.
                if cur_stage in TERMINAL_STAGES:
                    if inferred_step > cur_step:
                        deltas.append({
                            "type": "DM_STEP_RESTORE_IN_TERMINAL",
                            "name": attrs["_name"],
                            "company": attrs["_company"],
                            "linkedin_url": attrs["_url"],
                            "current_stage": cur_stage,
                            "current_dm_step": cur_step,
                            "inferred_stage": cur_stage,  # KEEP terminal stage
                            "inferred_dm_step": inferred_step,
                            "thread_total": total,
                            "thread_last_from_me": is_from_me,
                            "record_id": attrs["record_id"],
                            "entry_id": attrs["entry_id"],
                        })
                    continue

                # Classify delta for non-terminal stages
                if inferred_step > cur_step:
                    deltas.append({
                        "type": "REGRESSED",  # current state regressed vs thread
                        "name": attrs["_name"],
                        "company": attrs["_company"],
                        "linkedin_url": attrs["_url"],
                        "current_stage": cur_stage,
                        "current_dm_step": cur_step,
                        "inferred_stage": inferred_stage,
                        "inferred_dm_step": inferred_step,
                        "thread_total": total,
                        "thread_last_from_me": is_from_me,
                        "record_id": attrs["record_id"],
                        "entry_id": attrs["entry_id"],
                    })
                elif inferred_step < cur_step:
                    deltas.append({
                        "type": "OVERSTATED",  # current claims more than thread shows
                        "name": attrs["_name"],
                        "company": attrs["_company"],
                        "linkedin_url": attrs["_url"],
                        "current_stage": cur_stage,
                        "current_dm_step": cur_step,
                        "inferred_stage": inferred_stage,
                        "inferred_dm_step": inferred_step,
                        "thread_total": total,
                        "thread_last_from_me": is_from_me,
                        "record_id": attrs["record_id"],
                        "entry_id": attrs["entry_id"],
                    })
                # else: MATCH (skip)

        # 4. Find STALE: in pipeline at thread-bearing stage but no thread found
        stale: list[dict] = []
        for attrs in all_parsed:
            cur_stage = attrs.get("stage") or ""
            if cur_stage not in THREAD_BEARING_STAGES:
                continue
            norm_url = attrs.get("_normalized_url")
            if norm_url and norm_url in matched_url_set:
                continue
            stale.append({
                "name": attrs.get("_name", "?"),
                "company": attrs.get("_company", "?"),
                "linkedin_url": attrs.get("_url", ""),
                "current_stage": cur_stage,
                "current_dm_step": int(attrs.get("dm_step") or 0),
                "record_id": attrs["record_id"],
                "entry_id": attrs["entry_id"],
            })

        # 5. Report
        regressed = [d for d in deltas if d["type"] == "REGRESSED"]
        overstated = [d for d in deltas if d["type"] == "OVERSTATED"]
        terminal_dm_step_fixes = [d for d in deltas if d["type"] == "DM_STEP_RESTORE_IN_TERMINAL"]

        click.echo("=== Reconstruction summary ===")
        click.echo(f"  Total threads scraped:     {len(threads)}")
        click.echo(f"  Threads matched to pipeline: {len(threads) - len(unmatched_threads)}")
        click.echo(f"  Unmatched threads (non-pipeline contacts): {len(unmatched_threads)}")
        click.echo("")
        click.echo(f"  REGRESSED (thread shows more cadence than Attio): {len(regressed)}")
        click.echo(f"  DM_STEP_RESTORE_IN_TERMINAL (terminal stage, raise dm_step): {len(terminal_dm_step_fixes)}")
        click.echo(f"  OVERSTATED (Attio shows more than thread): {len(overstated)}")
        click.echo(f"  STALE (Attio at DM-stage but no thread):     {len(stale)}")

        if regressed:
            click.echo("\n  TOP REGRESSED (will be applied with --apply):")
            print(f"  {'name':30s} | {'company':25s} | cur     -> inferred  | thread")
            print(f"  {'-'*30} | {'-'*25} | -------- | -------- | ------")
            for d in sorted(regressed, key=lambda x: -x["inferred_dm_step"])[:30]:
                cur = f"{d['current_stage'][:10]}/dm{d['current_dm_step']}"
                inf = f"{d['inferred_stage'][:10]}/dm{d['inferred_dm_step']}"
                thr = f"n={d['thread_total']},me={d['thread_last_from_me']}"
                print(f"  {d['name'][:30]:30s} | {d['company'][:25]:25s} | {cur:8s} -> {inf:8s} | {thr}")
            if len(regressed) > 30:
                print(f"  ... and {len(regressed) - 30} more (see CSV)")

        if stale:
            click.echo("\n  STALE (manual review — no thread found):")
            for d in stale[:20]:
                cur = f"{d['current_stage'][:10]}/dm{d['current_dm_step']}"
                print(f"    {d['name'][:30]:30s} | {d['company'][:25]:25s} | {cur}")
            if len(stale) > 20:
                print(f"    ... and {len(stale) - 20} more")

        # 6. Write CSV
        OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
        with OUT_CSV.open("w") as f:
            w = csv.writer(f)
            w.writerow([
                "type", "name", "company", "linkedin_url",
                "current_stage", "current_dm_step",
                "inferred_stage", "inferred_dm_step",
                "thread_total", "thread_last_from_me",
                "record_id", "entry_id",
            ])
            for d in regressed + terminal_dm_step_fixes + overstated:
                w.writerow([
                    d["type"], d["name"], d["company"], d["linkedin_url"],
                    d["current_stage"], d["current_dm_step"],
                    d["inferred_stage"], d["inferred_dm_step"],
                    d["thread_total"], d["thread_last_from_me"],
                    d["record_id"], d["entry_id"],
                ])
            for d in stale:
                w.writerow([
                    "STALE", d["name"], d["company"], d["linkedin_url"],
                    d["current_stage"], d["current_dm_step"],
                    "", "", "", "",
                    d["record_id"], d["entry_id"],
                ])
        click.echo(f"\nWrote {OUT_CSV}")

        # 7. Apply REGRESSED + DM_STEP_RESTORE_IN_TERMINAL repairs
        # Wave-2-C fix-up (multi-agent C4): hoist the MigrationRunWriter
        # wrap out of the `if apply_changes and to_apply:` conditional
        # so dry-runs and empty-to_apply runs ALSO produce a Migration
        # Run row (with rows_examined=0, rows_modified=0). Matches the
        # pattern every other wrapped script in PR-C uses, and gives
        # the operator an "I ran but found nothing to do" audit trail
        # rather than silent silence.
        to_apply = regressed + terminal_dm_step_fixes
        if apply_changes and to_apply:
            click.echo(f"\n=== Applying {len(regressed)} REGRESSED + "
                       f"{len(terminal_dm_step_fixes)} TERMINAL dm_step fixes ===")
        today_str = date.today().isoformat()
        applied = 0
        failed = 0
        with MigrationRunWriter(
            script_name="scripts/reconstruct_cadence_from_linkedin_20260511.py",
            rollback_script_path=None,
            dry_run=not apply_changes,
            attio=attio,
        ) as run:
            for d in to_apply:
                run.examine()
                if not apply_changes:
                    run.skip_excluded(reason="dry-run preview, no apply")
                    continue
                try:
                    attio.update_list_entry(
                        entry_id=d["entry_id"],
                        entry_attributes={
                            "stage": d["inferred_stage"],
                            "dm_step": d["inferred_dm_step"],
                            "last_contact_date": today_str,
                        },
                        list_id=list_id,
                    )
                    attio.create_note(
                        record_id=d["record_id"],
                        title="Cadence reconstruction 2026-05-11",
                        content=(
                            f"Restored cadence from LinkedIn thread state.\n\n"
                            f"Before: stage={d['current_stage']}, dm_step={d['current_dm_step']}\n"
                            f"After:  stage={d['inferred_stage']}, dm_step={d['inferred_dm_step']}\n"
                            f"Thread evidence: totalMessageCount={d['thread_total']}, "
                            f"isLastMessageFromMe={d['thread_last_from_me']}\n"
                        ),
                    )
                    # Wave-2-C fix-up (multi-agent C1): entry_id is
                    # a list-entry id; use the list_entry sentinel.
                    run.mark_modified(record_id=d["entry_id"], object="list_entry")
                    applied += 1
                except Exception as e:
                    run.mark_failed(record_id=d["entry_id"], error=e)
                    failed += 1
                    click.echo(f"  ✗ failed for {d['name']}: {e}")
            if apply_changes:
                click.echo(f"  applied={applied}, failed={failed}")


if __name__ == "__main__":
    main()
