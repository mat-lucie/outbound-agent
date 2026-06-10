#!/usr/bin/env python3
"""Migrate local state files to the Attio daily_run object (F-PR-8).

Creates the ``daily_run`` Attio object + its identity/lease attributes
(per ``docs/attio_schema_deltas.yaml``), adds ``last_observed_degree``
+ ``last_observed_at`` to LinkedIn Outreach, and backfills any
in-flight content from the local JSON state files so today's run
isn't reset by the migration cutover.

# Files migrated

  - ``~/.outbound-agent/daily_limits.json`` → if today's counters are
    non-zero, a daily_run row is created with them so the new code
    sees a continuation, not a reset. The JSON file is moved aside
    to ``daily_limits.json.f-pr-8-archived.<timestamp>`` (NOT deleted).
  - ``~/.outbound-agent/recheck_cache.json`` → its entries are NOT
    migrated in bulk; the cache stays on disk as a thin regenerable
    overlay for the new Attio-backed reads. The next time a recheck
    fires for any given prospect, the new code writes
    ``last_observed_*`` to Attio and the JSON entry becomes
    eventually-consistent. File is archived with the same suffix
    pattern as a forensics trail.

# Idempotency

  - Re-running after objects exist → no schema mutations (409/
    duplicate-slug 400 handled like every other F-PR migration).
  - Re-running after JSON archival → the .archived suffix means
    the source files no longer exist and the daily_run query short-
    circuits to "nothing to backfill".

# Reversibility

Object + attribute deletion is operator-manual via the Attio UI
(Attio API limitation). The archived JSON files are intentionally
preserved so the operator can restore them manually if the migration
needs to be rolled back. ``rollback_status='irreversible'`` is
recorded.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clients.attio import AttioClient  # noqa: E402
from scripts._attio_migration_helpers import (  # noqa: E402
    ensure_attribute,
    ensure_object,
)
from workflows.migration_run_writer import MigrationRunWriter  # noqa: E402

# (slug, type, options, is_required, is_unique, description). Descriptions are
# required by the Attio v2 API. is_unique enforces server-side dedup; only
# uniqueness_key needs it (the {date}|{machine_id} key).
DAILY_RUN_ATTRIBUTES = [
    ("run_id", "text", None, True, False, "Unique id per daily run."),
    ("run_date", "date", None, True, False, "Calendar date the run is for."),
    ("machine_id", "text", None, True, False, "Hostname / agent id that owns the run."),
    ("uniqueness_key", "text", None, True, True, "{date}|{machine_id} dedup key. is_unique=True so Attio rejects duplicate rows server-side; preserves dedup contract if client-side check races."),
    ("hostname", "text", None, False, False, "Hostname (audit-only when machine_id == hostname)."),
    ("started_at", "datetime", None, True, False, "When the run started."),
    ("completed_at", "datetime", None, False, False, "When the run finished (null while running)."),
    (
        "status",
        "select",
        ["running", "completed", "failed", "aborted"],
        True,
        False,
        "Lifecycle status.",
    ),
    ("process_id", "number", None, False, False, "OS process id (post-mortem of crashes)."),
    ("connections_sent", "number", None, False, False, "Invites sent in this run."),
    ("messages_sent", "number", None, False, False, "DMs sent in this run."),
    ("visits_sent", "number", None, False, False, "Profile visits in this run."),
    ("failure_details", "long_text", None, False, False, "JSON of failures captured during the run."),
]

# Per §3.5 + F-PR-8 manifest entries. `linkedin_outreach` is a LIST
# (parent_object=people), so these attrs are registered via
# /lists/{ATTIO_LIST_ID}/attributes, not /objects/linkedin_outreach/attributes.
LINKEDIN_OUTREACH_RECHECK_ATTRIBUTES = [
    (
        "last_observed_degree",
        "select",
        ["first", "second", "third", "unknown"],
        False,
        "Most-recent observed LinkedIn connection degree for this prospect.",
    ),
    ("last_observed_at", "date", None, False,
     "Date last_observed_degree was observed."),
]

DAILY_LIMITS_PATH = Path.home() / ".outbound-agent" / "daily_limits.json"
RECHECK_CACHE_PATH = Path.home() / ".outbound-agent" / "recheck_cache.json"


# Schema-mutation helpers live in scripts/_attio_migration_helpers — single
# source of truth for the v2 API contract (type mapping, required POST
# fields, currency/record-reference config shape, list-vs-object endpoint
# routing, select-option backfill).


def _ensure_schema(attio: AttioClient) -> dict:
    """Phase 1: schema. Idempotent across re-runs.

    daily_run is an Attio object; linkedin_outreach recheck attrs live on
    the LinkedIn Outreach LIST (not on /objects/linkedin_outreach, which
    doesn't exist as an object). ATTIO_LIST_ID env var carries the list
    UUID for the latter.
    """
    obj_action, _body = ensure_object(
        attio, "daily_run", "Daily Run", "Daily Runs", dry_run=False,
    )
    summary: dict = {
        "daily_run_object_created": obj_action == "created",
        "daily_run_object_action": obj_action,
        "daily_run_attrs_created": 0,
        "daily_run_attrs_skipped": 0,
        "daily_run_attrs_options_added": 0,
        "linkedin_recheck_attrs_created": 0,
        "linkedin_recheck_attrs_skipped": 0,
        "linkedin_recheck_attrs_options_added": 0,
    }
    for slug, atype, options, required, unique, description in DAILY_RUN_ATTRIBUTES:
        action = ensure_attribute(
            attio, "object", "daily_run", slug, atype,
            options=options, is_required=required, is_unique=unique,
            description=description, dry_run=False,
        )
        if action == "created":
            summary["daily_run_attrs_created"] += 1
        elif action.startswith("added_options"):
            summary["daily_run_attrs_options_added"] += 1
        else:
            summary["daily_run_attrs_skipped"] += 1

    list_id = os.environ.get("ATTIO_LIST_ID", "")
    if not list_id:
        summary["linkedin_recheck_attrs_skipped_no_list_id"] = True
        print(
            "warning: ATTIO_LIST_ID not set; linkedin_outreach recheck "
            "attrs will be skipped. Re-run with ATTIO_LIST_ID set to "
            "register last_observed_degree + last_observed_at on the list.",
            file=sys.stderr,
        )
    else:
        for slug, atype, options, required, description in LINKEDIN_OUTREACH_RECHECK_ATTRIBUTES:
            action = ensure_attribute(
                attio, "list", list_id, slug, atype,
                options=options, is_required=required,
                description=description, dry_run=False,
            )
            if action == "created":
                summary["linkedin_recheck_attrs_created"] += 1
            elif action.startswith("added_options"):
                summary["linkedin_recheck_attrs_options_added"] += 1
            else:
                summary["linkedin_recheck_attrs_skipped"] += 1
    return summary


def _quarantine_corrupt(path: Path) -> Path:
    """Move a corrupt source file aside to a `.corrupt.<timestamp>` suffix
    so it doesn't trigger the same crash on the next migration run."""
    quarantined = path.with_suffix(
        f".json.f-pr-8-corrupt.{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"
    )
    shutil.move(str(path), str(quarantined))
    return quarantined


def _backfill_daily_limits(attio: AttioClient, run: MigrationRunWriter) -> dict:
    """If today's daily_limits.json has non-zero counts AND there's no
    matching daily_run row yet, create one so the new code doesn't
    appear to reset the day's progress.

    Returns a summary dict for the migration log. Archives the source
    file ONLY on success or "already-backfilled" — corrupt files are
    quarantined to `.corrupt.<timestamp>` instead; stale/zero files are
    LEFT IN PLACE so `safety_limits.py` (still the pre-cutover source
    of truth) keeps reading them with sensible reset semantics.
    """
    summary = {"daily_limits_action": "skip", "daily_limits_archived": False}
    if not DAILY_LIMITS_PATH.exists():
        summary["daily_limits_action"] = "no_source_file"
        return summary

    run.examine()
    try:
        data = json.loads(DAILY_LIMITS_PATH.read_text())
        file_date = data.get("date")
        today = date.today().isoformat()
        counters = {
            "connections_sent": int(data.get("connections", 0)),
            "messages_sent": int(data.get("messages", 0)),
            "visits_sent": int(data.get("visits", 0)),
        }
    except (json.JSONDecodeError, OSError, ValueError, TypeError) as exc:
        # Corrupt file: quarantine so the next run doesn't crash on the
        # same input. Don't archive (we want operator visibility on the
        # quarantine path) and don't propagate (return to let the schema
        # migration complete its other phases).
        quarantined = _quarantine_corrupt(DAILY_LIMITS_PATH)
        summary["daily_limits_action"] = f"quarantined_corrupt: {exc!r}"
        summary["daily_limits_quarantined_to"] = str(quarantined)
        return summary

    if file_date != today:
        summary["daily_limits_action"] = "stale_date_not_backfilled"
    elif sum(counters.values()) == 0:
        summary["daily_limits_action"] = "today_but_zero_counters"
    else:
        machine_id = socket.gethostname()
        key = f"{today}|{machine_id}"
        existing = attio._client.request(
            "POST",
            "/objects/daily_run/records/query",
            json={"filter": {"uniqueness_key": key}, "limit": 1},
        )
        existing.raise_for_status()
        if existing.json().get("data"):
            summary["daily_limits_action"] = "daily_run_already_exists_for_today"
        else:
            resp = attio._client.request(
                "POST",
                "/objects/daily_run/records",
                json={
                    "data": {
                        "values": {
                            "run_id": f"backfilled-{today}-{machine_id}",
                            "run_date": today,
                            "machine_id": machine_id,
                            "uniqueness_key": key,
                            "hostname": machine_id,
                            "started_at": datetime.now(UTC).isoformat(),
                            "status": "completed",
                            "completed_at": datetime.now(UTC).isoformat(),
                            **counters,
                        }
                    }
                },
            )
            resp.raise_for_status()
            run.mark_modified(
                record_id=resp.json()["data"]["id"]["record_id"],
                object="daily_run",
            )
            summary["daily_limits_action"] = "backfilled"

    # Archive ONLY when we either (a) backfilled today's counters into
    # a fresh daily_run row, or (b) confirmed today's daily_run row
    # already exists. Stale-date / zero-counter files are left in place
    # so safety_limits.py (the pre-cutover source of truth until the
    # cli.py wrap lands) keeps reading them and resets cleanly on the
    # next day rollover.
    archive_actions = {"backfilled", "daily_run_already_exists_for_today"}
    if summary["daily_limits_action"] in archive_actions:
        archived_path = DAILY_LIMITS_PATH.with_suffix(
            f".json.f-pr-8-archived.{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"
        )
        shutil.move(str(DAILY_LIMITS_PATH), str(archived_path))
        summary["daily_limits_archived"] = True
    return summary


def _archive_recheck_cache() -> dict:
    """The recheck cache stays useful as a thin regenerable overlay even
    after F-PR-8, but the file is no longer the source of truth. Move it
    aside so the migration cutover is explicit; if/when the new code
    needs the JSON again it'll be recreated from Attio reads."""
    summary = {"recheck_cache_archived": False}
    if RECHECK_CACHE_PATH.exists():
        archived_path = RECHECK_CACHE_PATH.with_suffix(
            f".json.f-pr-8-archived.{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"
        )
        shutil.move(str(RECHECK_CACHE_PATH), str(archived_path))
        summary["recheck_cache_archived"] = True
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate ~/.outbound-agent/*.json state files to Attio daily_run (F-PR-8)."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be done; no Attio writes, no file archival.",
    )
    args = parser.parse_args()

    if args.dry_run:
        print("--- DRY RUN ---")
        print(f"Would ensure daily_run object + {len(DAILY_RUN_ATTRIBUTES)} attrs.")
        print(
            f"Would ensure linkedin_outreach recheck attrs: "
            f"{[a[0] for a in LINKEDIN_OUTREACH_RECHECK_ATTRIBUTES]}"
        )
        if DAILY_LIMITS_PATH.exists():
            print(f"Would inspect + archive {DAILY_LIMITS_PATH}")
        if RECHECK_CACHE_PATH.exists():
            print(f"Would archive {RECHECK_CACHE_PATH}")
        return 0

    with AttioClient() as attio, MigrationRunWriter(
        attio=attio,
        script_name="migrate_state_files_to_attio_daily_run",
        script_version="F-PR-8-v1",
        rollback_script_path=None,
    ) as run:
        schema_summary = _ensure_schema(attio)
        print(f"Schema: {schema_summary}")
        backfill_summary = _backfill_daily_limits(attio, run)
        print(f"daily_limits backfill: {backfill_summary}")
        cache_summary = _archive_recheck_cache()
        print(f"recheck_cache archival: {cache_summary}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
