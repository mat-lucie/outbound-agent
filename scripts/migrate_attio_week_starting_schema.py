#!/usr/bin/env python3
"""Deploy the linkedin_outreach ``week_starting`` attribute (PR-28).

# Why

The first wet ``cli.py weekly-finalize`` run (2026-05-31) halted: every PASS
prospect commit goes through ``_build_prospect_entry_attrs`` (weekly_prospect.py),
which stamps ``week_starting`` (PR-28 batch-traceability — the Monday of the
operator-invocation date, so weekly cohorts can be queried by week). The
attribute was manifest ``status: planned`` and never created in production
Attio, so the first ``add_list_entry`` PATCH 400'd with::

    value_not_found: Cannot find attribute with slug/ID "week_starting".

The finalize 400-fallback only strips the Phase-1 attrs
(``quality_score_band`` / ``icp_lane_persisted``), not ``week_starting``, so it
crashed on the first PASS (0 of 387 committed — no partial damage).

A schema diff confirmed ``week_starting`` is the ONLY missing attribute of the
~17 the writer emits; all others (cadence_lane, prospect_committed_at,
experiment_id_frozen_at, …) are already deployed.

# What it does

Creates the ``week_starting`` (date) attribute on the ``linkedin_outreach`` LIST
via the idempotent ``ensure_attribute`` helper. After it runs, flip the manifest
entry (``docs/attio_schema_deltas.yaml``, linkedin_outreach ``week_starting``)
from ``status: planned`` to ``status: shipped`` so the wet-run schema-drift
pre-flight covers it.

# Running

    # Preview (default):
    python scripts/migrate_attio_week_starting_schema.py
    python scripts/migrate_attio_week_starting_schema.py --dry-run

    # Live deploy (writes to production Attio):
    python scripts/migrate_attio_week_starting_schema.py --apply

DEFAULTS TO DRY-RUN. An explicit ``--apply`` is required to write to production
Attio. Idempotent: a second ``--apply`` skips (the attribute already exists).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clients.attio import AttioClient  # noqa: E402
from scripts._attio_migration_helpers import ensure_attribute  # noqa: E402
from workflows.migration_run_writer import MigrationRunWriter  # noqa: E402

SCRIPT_VERSION = "1.0"
TARGET_LIST = "linkedin_outreach"

WEEK_STARTING_SLUG = "week_starting"
WEEK_STARTING_DESCRIPTION = (
    "PR-28 batch-traceability: the Monday of the operator-invocation date for "
    "the prospect commit, so weekly cohorts can be queried/grouped by "
    "week_starting. Sole writer is _build_prospect_entry_attrs at PROSPECT-commit."
)


def deploy(
    attio: object, *, list_id: str, dry_run: bool, run: MigrationRunWriter,
    actions: dict[str, str],
) -> None:
    """Create the week_starting (date) attribute on the linkedin_outreach list.

    Additive + idempotent. Logged on the Migration Run row via object="schema"
    (the sentinel that skips the back-pointer PATCH — no target record).
    """
    run.examine()

    actions[WEEK_STARTING_SLUG] = ensure_attribute(
        attio,
        "list",
        list_id,
        WEEK_STARTING_SLUG,
        "date",
        description=WEEK_STARTING_DESCRIPTION,
        dry_run=dry_run,
    )

    if all(a == "skipped" for a in actions.values()):
        run.skip_idempotent()
    else:
        run.mark_modified(record_id=f"{TARGET_LIST}.schema_PR28", object="schema")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--apply", action="store_true",
        help="Live deploy: write to production Attio. Default is dry-run.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Explicit dry-run (default; equivalent to omitting --apply).",
    )
    args = parser.parse_args(argv)

    # Default is dry-run. Live deploy requires explicit --apply.
    dry_run = not args.apply

    list_id = os.environ.get("ATTIO_LIST_ID", "")
    if not list_id:
        print("error: ATTIO_LIST_ID env var not set", file=sys.stderr)
        return 2

    try:
        attio = AttioClient()
    except KeyError:
        print("error: ATTIO_API_KEY env var not set", file=sys.stderr)
        return 2

    actions: dict[str, str] = {}
    failed = False
    with MigrationRunWriter(
        script_name=Path(__file__).name,
        script_version=SCRIPT_VERSION,
        rollback_script_path=None,  # additive; Attio attr removal is operator-manual-only
        dry_run=dry_run,
        attio=attio,
    ) as run:
        try:
            deploy(attio, list_id=list_id, dry_run=dry_run, run=run, actions=actions)
        except Exception as exc:
            run.mark_failed(record_id=f"{TARGET_LIST}.schema_PR28", error=exc)
            print(
                f"week_starting schema migration failed after {actions}: {exc}",
                file=sys.stderr,
            )
            failed = True

    print(json.dumps({
        "dry_run": dry_run,
        "list": TARGET_LIST,
        "actions": actions,
    }, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
