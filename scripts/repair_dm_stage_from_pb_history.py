"""Repair Attio DM stages from PhantomBuster Message Sender history.

Scans recent Message Sender containers for successful send markers
(`Sending message to ...` in PB logs), counts how many DMs each LinkedIn
URL actually received, and flags any Attio list entry whose stage is behind
the implied DM step. A prior bug in the log parser silently defaulted
ambiguous blocks to "skipped", so many delivered DMs never advanced Attio.

Outputs a repair_report.json by default (--apply to commit).

Usage:
  python3 scripts/repair_dm_stage_from_pb_history.py             # dry-run report
  python3 scripts/repair_dm_stage_from_pb_history.py --apply     # apply fixes
  python3 scripts/repair_dm_stage_from_pb_history.py --days 30   # scan back 30 days
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import click
import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent))

from clients.attio import AttioClient  # noqa: E402
from models.pipeline import PipelineStage  # noqa: E402
from workflows.daily_check import _normalize_linkedin_url  # noqa: E402
from workflows.migration_run_writer import MigrationRunWriter  # noqa: E402

PB_BASE = "https://api.phantombuster.com/api/v2"
STAGE_RANK = {
    PipelineStage.PROSPECT: 0,
    PipelineStage.PARTNER_INTRO: 0,
    PipelineStage.CONNECTION_SENT: 1,
    PipelineStage.ACCEPTED: 2,
    PipelineStage.DM1_SENT: 3,
    PipelineStage.DM2_SENT: 4,
    PipelineStage.DM3_SENT: 5,
}
# Stages we must never downgrade out of — manual human calls or terminal
# outcomes. If a prospect is already in one of these, leave them alone.
TERMINAL_STAGES = {
    PipelineStage.RESPONDED,
    PipelineStage.CALL_BOOKED,
    PipelineStage.QUALIFIED,
    PipelineStage.NOT_INTERESTED,
    PipelineStage.PARTNER_INTRO,
    # UNREACHABLE is a rank-90 undeliverability terminal, gated out of every
    # send/invite loop. A stale PB "send" (often an InMail-required attempt
    # miscounted as delivered) must NOT resurrect it to DM1 Sent and drop it
    # back into the send queue — skip it like the other terminals.
    PipelineStage.UNREACHABLE,
}
DM_STAGE_BY_COUNT = {
    1: PipelineStage.DM1_SENT,
    2: PipelineStage.DM2_SENT,
    3: PipelineStage.DM3_SENT,
}


def _pb_headers() -> dict:
    return {"X-Phantombuster-Key": os.environ["PHANTOMBUSTER_API_KEY"]}


def list_containers(agent_id: str, since_ms: int) -> list[dict]:
    """Page through /containers/fetch-all for an agent back to since_ms."""
    results: list[dict] = []
    with httpx.Client(base_url=PB_BASE, headers=_pb_headers(), timeout=30.0) as c:
        cursor = None
        while True:
            params = {"agentId": agent_id, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            r = c.get("/containers/fetch-all", params=params)
            r.raise_for_status()
            data = r.json()
            batch = data.get("containers", [])
            if not batch:
                break
            stop = False
            for item in batch:
                if item.get("createdAt", 0) < since_ms:
                    stop = True
                    break
                results.append(item)
            if stop or not data.get("maxLimitReached"):
                break
            cursor = batch[-1].get("id")
            time.sleep(0.2)
    return results


def fetch_container_log(container_id: str) -> str:
    with httpx.Client(base_url=PB_BASE, headers=_pb_headers(), timeout=30.0) as c:
        r = c.get("/containers/fetch-output", params={"id": container_id})
        r.raise_for_status()
        return (r.json() or {}).get("output", "") or ""


_URL_RE = re.compile(r"Converting regular URL (https?://\S+?)(?:\.\.\.|\s|$)")


def count_sends_per_url(log: str) -> set[str]:
    """Return normalized URLs that had a successful DM sent in this container."""
    sent: set[str] = set()
    current: str | None = None
    sent_flag = False
    for raw in log.splitlines():
        line = raw.strip()
        m = _URL_RE.search(line)
        if m:
            if current is not None and sent_flag:
                sent.add(_normalize_linkedin_url(current))
            current = m.group(1)
            sent_flag = False
            continue
        if current is None:
            continue
        if "Sending message to" in line:
            sent_flag = True
    if current is not None and sent_flag:
        sent.add(_normalize_linkedin_url(current))
    return sent


def build_send_history(agent_id: str, days: int) -> dict[str, list[str]]:
    """Return url -> [container_id, ...] ordered by createdAt ascending."""
    since = datetime.now(UTC) - timedelta(days=days)
    since_ms = int(since.timestamp() * 1000)
    containers = list_containers(agent_id, since_ms)
    containers.sort(key=lambda c: c.get("createdAt", 0))
    click.echo(f"Scanning {len(containers)} Message Sender containers in the last {days} days…")
    history: dict[str, list[str]] = {}
    for i, c in enumerate(containers):
        if c.get("status") != "finished":
            continue
        cid = c["id"]
        log = fetch_container_log(cid)
        sent_urls = count_sends_per_url(log)
        for url in sent_urls:
            history.setdefault(url, []).append(cid)
        click.echo(f"  [{i + 1}/{len(containers)}] container {cid}: {len(sent_urls)} sends")
    return history


def main(days: int, apply: bool, report_path: str) -> int:
    agent_id = os.environ.get("PB_MESSAGE_SENDER_ID")
    if not agent_id:
        click.echo("ERROR: PB_MESSAGE_SENDER_ID not set")
        return 1

    history = build_send_history(agent_id, days)
    if not history:
        click.echo("No sent URLs in PB history — nothing to repair.")
        return 0

    click.echo(f"\nFound {len(history)} unique LinkedIn URLs with ≥1 successful send.")

    # Cross-reference with Attio
    with AttioClient() as attio:
        list_id = os.environ["ATTIO_LIST_ID"]
        entries = attio.query_list_entries(list_id=list_id)
        parsed = [AttioClient.parse_entry(e) for e in entries]

        # URL lookup: record_id -> normalized URL
        url_by_record: dict[str, str] = {}
        for p in parsed:
            rec_id = p["record_id"]
            if rec_id in url_by_record:
                continue
            rec = attio.get_person(rec_id)
            if not rec:
                continue
            _, _, raw_url, _, _ = attio.extract_record_info(rec)
            if raw_url:
                url_by_record[rec_id] = _normalize_linkedin_url(raw_url)

        repairs: list[dict] = []
        for attrs in parsed:
            rec_id = attrs["record_id"]
            url = url_by_record.get(rec_id)
            if not url or url not in history:
                continue
            try:
                current_stage = PipelineStage(attrs["stage"])
            except ValueError:
                continue
            if current_stage in TERMINAL_STAGES:
                continue  # never downgrade out of terminal or manually-owned stages
            n_sends = len(history[url])
            # PB count is inflated by the repeat-DM bug: if Attio never advanced,
            # daily re-queued the same DM step. So N sends do NOT imply DMN
            # was delivered — they may all be repeats of DM1. We only trust PB
            # to *catch up* a record that's still pre-DM1 despite ≥1 delivery.
            # Stages at DM1_SENT or beyond are left for Attio/manual to manage;
            # the cross-URL guard prevents further repeat sends going forward.
            if n_sends < 1:
                continue
            implied = PipelineStage.DM1_SENT
            if STAGE_RANK.get(current_stage, 0) >= STAGE_RANK[implied]:
                continue  # already at DM1+, trust Attio
            repairs.append({
                "entry_id": attrs["entry_id"],
                "record_id": rec_id,
                "linkedin_url": url,
                "current_stage": current_stage.value,
                "implied_stage": implied.value,
                "send_count": n_sends,
                "containers": history[url],
            })

    click.echo(f"\n{len(repairs)} Attio entries are behind their PB-recorded DM step.")
    if repairs:
        Path(report_path).write_text(json.dumps({
            "generated_at": datetime.now(UTC).isoformat(),
            "days_scanned": days,
            "repairs": repairs,
        }, indent=2))
        click.echo(f"Wrote report: {report_path}")
        click.echo("\nPreview (first 10):")
        for r in repairs[:10]:
            click.echo(
                f"  {r['linkedin_url']}: {r['current_stage']} -> {r['implied_stage']} "
                f"({r['send_count']} PB sends)"
            )

    if not apply:
        click.echo("\nDry-run. Re-run with --apply to write changes.")
        return 0

    click.echo("\nApplying repairs to Attio…")
    with AttioClient() as attio, MigrationRunWriter(
        script_name="scripts/repair_dm_stage_from_pb_history.py",
        rollback_script_path=None,
        dry_run=not apply,
        attio=attio,
    ) as run:
        list_id = os.environ["ATTIO_LIST_ID"]
        ok = 0
        fail = 0
        for r in repairs:
            run.examine()
            try:
                attio.update_list_entry(
                    entry_id=r["entry_id"],
                    entry_attributes={
                        "stage": r["implied_stage"],
                        "dm_step": r["send_count"] if r["send_count"] <= 3 else 3,
                    },
                    list_id=list_id,
                )
                # Wave-2-C fix-up (multi-agent C1): list_entry sentinel.
                run.mark_modified(record_id=r["entry_id"], object="list_entry")
                ok += 1
            except Exception as e:
                click.echo(f"  FAIL {r['linkedin_url']}: {e}")
                run.mark_failed(record_id=r["entry_id"], error=e)
                fail += 1
        click.echo(
            f"Applied: {ok}. Failed: {fail}. (Migration Run id={run.run_id})"
        )
    return 0


@click.command()
@click.option("--days", default=60, show_default=True, help="Scan PB history back this many days.")
@click.option("--apply", is_flag=True, help="Commit stage updates to Attio. Default: dry-run.")
@click.option("--report", default="exports/dm-stage-repair-report.json", show_default=True)
def cli(days: int, apply: bool, report: str) -> None:
    """Repair Attio DM stages from PhantomBuster Message Sender history."""
    sys.exit(main(days, apply, report))


if __name__ == "__main__":
    cli()
