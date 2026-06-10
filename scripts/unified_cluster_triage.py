#!/usr/bin/env python3
"""Unified cluster triage — decide target DM stage for dedup clusters.

For each cluster in a dedup-report.json, pulls PB send history and Attio
list entries, computes the target DM stage, and annotates the cluster
with triage metadata. Supports dry-run (default) and apply modes.

Usage:
  python3 scripts/unified_cluster_triage.py --dry-run
  python3 scripts/unified_cluster_triage.py --apply
  python3 scripts/unified_cluster_triage.py --apply --days 90
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import click
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent))

from clients.attio import AttioClient  # noqa: E402
from models.pipeline import PipelineStage  # noqa: E402
from scripts.repair_dm_stage_from_pb_history import (  # noqa: E402
    STAGE_RANK,
    TERMINAL_STAGES,
    build_send_history,
)
from scripts.triage_dedup import fetch_list_entries_by_record  # noqa: E402
from workflows.daily_check import _normalize_linkedin_url  # noqa: E402


def decide_target_stage(
    cluster_max_stage: str,
    cluster_terminal_stage: str | None,
    pb_send_count: int,
    has_conflict: bool,
    n_entries: int,
) -> tuple[str, str]:
    """Decide target DM stage for a cluster and reason.

    Args:
        cluster_max_stage: Current max PipelineStage across all entries (string value).
        cluster_terminal_stage: Terminal stage if any entry is RESPONDED, CALL_BOOKED, etc.
        pb_send_count: Number of successful DM sends to this LinkedIn URL.
        has_conflict: Whether the cluster has conflict_reasons (field is non-empty).
        n_entries: Number of list entries in the cluster.

    Returns:
        (target_stage, reason) tuple where:
        - target_stage: PipelineStage.value (string)
        - reason: one of "terminal_preserved", "pb_history", "attio_max_stage", "fallback_lost"
    """
    # 1. If terminal stage exists, never downgrade.
    if cluster_terminal_stage:
        return cluster_terminal_stage, "terminal_preserved"

    # 2. PB send count is NOT a reliable indicator of which DM *step* the
    #    prospect progressed to — the repeat-DM bug means the same step was
    #    sent multiple times to the same URL. A count of 3 does not imply
    #    DM1+DM2+DM3 was sent; it often means DM1 was sent three times
    #    because Attio never advanced past Accepted. So we only use PB to
    #    *upgrade* Attio when Attio is clearly behind: at least one DM was
    #    delivered (pb ≥ 1) and Attio's max is still pre-DM1. In that case
    #    we set target to DM1 Sent, never higher.
    max_rank = 0
    max_stage_enum: PipelineStage | None = None
    if cluster_max_stage:
        try:
            max_stage_enum = PipelineStage(cluster_max_stage)
            max_rank = STAGE_RANK.get(max_stage_enum, 0)
        except (ValueError, KeyError):
            pass

    dm1_rank = STAGE_RANK[PipelineStage.DM1_SENT]

    if pb_send_count >= 1 and max_rank < dm1_rank:
        return PipelineStage.DM1_SENT.value, "pb_bug1_catchup"

    # 3. Fallback-lost: Prospect cluster with zero PB sends, >2 entries, and
    #    unresolved conflict reasons. Orphaned duplicates — mark as Not
    #    Interested so they exit the funnel.
    if (
        max_stage_enum == PipelineStage.PROSPECT
        and pb_send_count == 0
        and n_entries > 2
        and has_conflict
    ):
        return PipelineStage.NOT_INTERESTED.value, "fallback_lost"

    # 4. Trust Attio's most-advanced stage among cluster entries.
    if cluster_max_stage:
        return cluster_max_stage, "attio_max_stage"

    # Default to Prospect (shouldn't reach here if logic is complete).
    return PipelineStage.PROSPECT.value, "attio_max_stage"


def stage_rank_value(stage_str: str) -> int:
    """Look up the rank of a stage string in STAGE_RANK dict."""
    try:
        enum_val = PipelineStage(stage_str)
        return STAGE_RANK.get(enum_val, 0)
    except (ValueError, KeyError):
        return 0


def find_cluster_stages(
    cluster: dict, entries_by_record: dict[str, list[dict]]
) -> tuple[str, str | None]:
    """Find max stage and first terminal stage across all cluster records.

    Returns:
        (cluster_max_stage, cluster_terminal_stage)
        - cluster_max_stage: string value of highest-rank non-terminal stage
        - cluster_terminal_stage: string value of first terminal stage found, or None
    """
    all_stages = []
    cluster_terminal = None

    winner_id = cluster.get("winner_id", "")
    loser_ids = cluster.get("loser_ids", [])
    record_ids = [winner_id] + (loser_ids or [])

    for rid in record_ids:
        if not rid:
            continue
        entries = entries_by_record.get(rid, [])
        for entry in entries:
            stage_str = entry.get("stage") or ""
            if stage_str:
                all_stages.append(stage_str)
                # Check if terminal
                try:
                    stage_enum = PipelineStage(stage_str)
                    if stage_enum in TERMINAL_STAGES and not cluster_terminal:
                        cluster_terminal = stage_str
                except (ValueError, KeyError):
                    pass

    if not all_stages:
        return PipelineStage.PROSPECT.value, None

    # Find max by STAGE_RANK
    max_stage = max(all_stages, key=stage_rank_value)
    return max_stage, cluster_terminal


@click.command()
@click.option(
    "--report",
    type=click.Path(exists=True),
    default="exports/dedup-report-2026-04-21.json",
    help="Path to dedup-report.json",
)
@click.option(
    "--days",
    type=int,
    default=60,
    help="Days of PB history to scan (default 60)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=True,
    help="Print decisions without writing (default)",
)
@click.option(
    "--apply",
    is_flag=True,
    default=False,
    help="Apply decisions: update report and emit triage-decisions JSON",
)
def main(report: str, days: int, dry_run: bool, apply: bool) -> int:
    """Triage dedup clusters and assign target DM stages.

    Reads a dedup-report.json, pulls PB send history and Attio list entries
    for each cluster, decides a target DM stage, and optionally writes updates
    back into the report.
    """
    # Override dry-run if apply is explicitly passed.
    if apply:
        dry_run = False

    click.echo(f"Loading dedup report from {report}…")
    report_path = Path(report)
    if not report_path.exists():
        click.secho(f"Error: {report} not found.", fg="red")
        return 1

    report_data = json.loads(report_path.read_text())

    api_key = os.environ.get("ATTIO_API_KEY")
    list_id = os.environ.get("ATTIO_LIST_ID")
    pb_sender_id = os.environ.get("PB_MESSAGE_SENDER_ID")

    if not api_key:
        click.secho("Error: ATTIO_API_KEY not set.", fg="red")
        return 1
    if not list_id:
        click.secho("Error: ATTIO_LIST_ID not set.", fg="red")
        return 1
    if not pb_sender_id:
        click.secho("Error: PB_MESSAGE_SENDER_ID not set.", fg="red")
        return 1

    # Initialize clients
    attio = AttioClient(api_key=api_key)

    # Fetch list entries once
    click.echo(f"Fetching list entries from {list_id}…")
    entries_by_record = fetch_list_entries_by_record(attio, list_id)

    # Build PB send history
    click.echo(f"Building PB send history (last {days} days)…")
    pb_history = build_send_history(pb_sender_id, days=days)

    # Triage each cluster
    decisions = []
    people = report_data.get("people", {})
    auto_safe = people.get("auto_safe_groups", [])
    conflict = people.get("conflict_groups", [])
    approved = set(c.get("linkedin_url_normalized", "") for c in people.get("approved_conflict_groups", []))
    skipped = set(c.get("linkedin_url_normalized", "") for c in people.get("skipped_groups", []))

    to_triage = auto_safe + conflict
    to_triage = [c for c in to_triage if c.get("linkedin_url_normalized", "") not in approved and c.get("linkedin_url_normalized", "") not in skipped]

    click.echo(f"Triaging {len(to_triage)} clusters…")
    for cluster in to_triage:
        url = cluster.get("linkedin_url_normalized", "")
        winner_id = cluster.get("winner_id", "")
        loser_ids = cluster.get("loser_ids", [])
        n_losers = len(loser_ids or [])
        has_conflict = bool(cluster.get("conflict_reasons"))

        # Get PB send count. attio_dedup reports the raw URL under the
        # `linkedin_url_normalized` key (historical misnomer — it doesn't
        # normalize), while build_send_history normalizes via
        # workflows.daily_check._normalize_linkedin_url (decode, lowercase,
        # strip www, strip trailing slash). Pass the normalized form to the
        # lookup so we actually match PB's history keys.
        pb_send_count = len(pb_history.get(_normalize_linkedin_url(url), []))

        # Find cluster stages
        cluster_max_stage, cluster_terminal = find_cluster_stages(cluster, entries_by_record)

        # Decide target stage
        target_stage, reason = decide_target_stage(
            cluster_max_stage=cluster_max_stage,
            cluster_terminal_stage=cluster_terminal,
            pb_send_count=pb_send_count,
            has_conflict=has_conflict,
            n_entries=len(list(filter(None, [winner_id] + (loser_ids or [])))),
        )

        decisions.append({
            "url": url,
            "winner_record_id": winner_id,
            "current_max_stage": cluster_max_stage,
            "target_stage": target_stage,
            "pb_send_count": pb_send_count,
            "reason": reason,
            "n_losers": n_losers,
        })

        if not dry_run:
            # Annotate cluster for approval
            cluster["post_apply_stage_override"] = target_stage
            cluster["pb_send_count"] = pb_send_count
            cluster["triage_reason"] = reason
            # Move from conflict/auto_safe to approved
            people["approved_conflict_groups"].append(cluster)
            if cluster in conflict:
                conflict.remove(cluster)
            if cluster in auto_safe:
                auto_safe.remove(cluster)

    # Print summary table
    click.echo()
    click.secho("═" * 120, fg="cyan")
    header = f"{'URL':<52} | {'current':^16} | {'target':^16} | {'pb':>3} | {'losers':>6} | reason"
    click.secho(header, fg="cyan")
    click.secho("─" * 120, fg="cyan")

    for dec in decisions:
        url_trunc = (dec["url"][:48] + "…") if len(dec["url"]) > 50 else dec["url"]
        current = dec["current_max_stage"][:12] if dec["current_max_stage"] else "?"
        target = dec["target_stage"][:12] if dec["target_stage"] else "?"
        pb = dec["pb_send_count"]
        losers = dec["n_losers"]
        reason = dec["reason"]
        row = f"{url_trunc:<52} | {current:^16} | {target:^16} | {pb:>3} | {losers:>6} | {reason}"
        click.echo(row)

    click.secho("═" * 120, fg="cyan")
    click.echo(f"Triaged {len(decisions)} clusters.")

    if dry_run:
        click.echo("(dry-run mode — no files written)")
    else:
        # Write updated report
        click.echo(f"Writing updated report to {report_path}…")
        report_path.write_text(json.dumps(report_data, indent=2))

        # Write decisions export
        timestamp = datetime.now().strftime("%Y-%m-%d")
        decisions_path = Path("exports") / f"triage-decisions-{timestamp}.json"
        decisions_path.parent.mkdir(parents=True, exist_ok=True)
        click.echo(f"Writing decisions to {decisions_path}…")
        decisions_path.write_text(json.dumps(decisions, indent=2))

        click.secho("Done.", fg="green")

    return 0


if __name__ == "__main__":
    sys.exit(main())
