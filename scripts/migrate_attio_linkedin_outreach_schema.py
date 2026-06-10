#!/usr/bin/env python3
"""Deploy the linkedin_outreach schema elements that PR-20/PR-21 code writes but
were never created in production Attio (2026-05-28 wet-run halt).

# Why

The first wet ``cli.py daily --yes`` run halted because merged code wrote two
``linkedin_outreach`` list schema elements that the production workspace never
received:

  1. ``experiment_id_frozen_at`` (select, 6 options) — PR-21 freezes the §3.1
     experiment cohort label at each stage transition. Seven writer modules
     stamp it, but the attribute was manifest ``status: planned`` and never
     deployed, so 3 Phase 0 ACCEPTED flips + 3 Part A pre-invite flips 400'd
     and all 6 connection invites were safe-skipped.
  2. ``manual_unclassified`` — a missing OPTION on the existing
     ``response_classification`` select. PR-20's manual-reply handler stamps it,
     so a detected manual reply (Marco Bortolon) 400'd and its classification
     was lost for that cycle.

This script creates the attribute and adds the option, both on the
``linkedin_outreach`` list (UUID from ``ATTIO_LIST_ID``). After it runs, flip the
manifest entries for ``experiment_id_frozen_at`` (and ``experiment_id``, which is
already in prod) to ``status: shipped`` so the new wet-run schema-drift pre-flight
covers them.

# Running

    # Preview (default behaviour):
    python scripts/migrate_attio_linkedin_outreach_schema.py
    python scripts/migrate_attio_linkedin_outreach_schema.py --dry-run

    # Live deploy (writes to production Attio):
    python scripts/migrate_attio_linkedin_outreach_schema.py --apply

DEFAULTS TO DRY-RUN. An explicit ``--apply`` flag is required to write to
production Attio. Idempotent: a second ``--apply`` run skips (the attribute and
option already exist).
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
from scripts._attio_migration_helpers import (  # noqa: E402
    ensure_attribute,
    reconcile_select_options,
)
from workflows.migration_run_writer import MigrationRunWriter  # noqa: E402

SCRIPT_VERSION = "1.0"
TARGET_LIST = "linkedin_outreach"

FROZEN_AT_SLUG = "experiment_id_frozen_at"
FROZEN_AT_OPTIONS = [
    "prospect",
    "connection_sent",
    "accepted",
    "legacy_pre_tsv_era",
    "legacy_inferred_by_archaeology",
    "legacy_pure_unknown",
]
FROZEN_AT_DESCRIPTION = (
    "§3.1 experiment cohort label frozen at the stage transition that observed "
    "it (PR-21). Six-value enum; never add a 'no_active_experiment' sentinel — "
    "rows with NULL experiment_id are measurement-excluded."
)

RESPONSE_CLASS_SLUG = "response_classification"
NEW_RESPONSE_OPTION = "manual_unclassified"


def deploy(
    attio: object, *, list_id: str, dry_run: bool, run: MigrationRunWriter,
    actions: dict[str, str],
) -> None:
    """Create experiment_id_frozen_at + add the manual_unclassified option.

    Both mutations target the linkedin_outreach LIST (parent_kind="list").
    Records each mutation's verdict into the caller-provided ``actions`` dict
    AS IT COMMITS (``created`` | ``would_create`` | ``skipped`` |
    ``added_options:N`` | ``would_add_options:N``), so a partial failure
    (mutation 1 lands, mutation 2 raises) still reports what actually
    committed — the helpers are idempotent, so a re-run heals the remainder.
    The schema mutations are logged on the Migration Run row via
    ``object="schema"`` — the sentinel that skips the back-pointer PATCH
    (there is no target record to stamp).
    """
    run.examine()

    frozen_action = ensure_attribute(
        attio,
        "list",
        list_id,
        FROZEN_AT_SLUG,
        "select",
        options=FROZEN_AT_OPTIONS,
        description=FROZEN_AT_DESCRIPTION,
        dry_run=dry_run,
    )
    actions[FROZEN_AT_SLUG] = frozen_action
    # ensure_attribute's CREATE path does NOT persist select options on a
    # list attribute in the same call (Attio creates the select optionless),
    # so after a fresh create reconcile the options explicitly — otherwise a
    # single --apply would leave experiment_id_frozen_at with zero options
    # and writes of "accepted"/etc. would 400. (When the attr already exists,
    # ensure_attribute has already reconciled options internally.)
    if frozen_action == "created":
        actions[f"{FROZEN_AT_SLUG}_options"] = reconcile_select_options(
            attio, "list", list_id, FROZEN_AT_SLUG, FROZEN_AT_OPTIONS,
            dry_run=dry_run,
        )

    actions[RESPONSE_CLASS_SLUG] = reconcile_select_options(
        attio,
        "list",
        list_id,
        RESPONSE_CLASS_SLUG,
        [NEW_RESPONSE_OPTION],
        dry_run=dry_run,
    )

    if all(a == "skipped" for a in actions.values()):
        run.skip_idempotent()
    else:
        run.mark_modified(record_id=f"{TARGET_LIST}.schema_PR20_21", object="schema")


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
        rollback_script_path=None,  # additive; Attio attr/option removal is operator-manual-only
        dry_run=dry_run,
        attio=attio,
    ) as run:
        try:
            deploy(attio, list_id=list_id, dry_run=dry_run, run=run, actions=actions)
        except Exception as exc:
            run.mark_failed(record_id=f"{TARGET_LIST}.schema_PR20_21", error=exc)
            # `actions` carries whatever committed before the failure — print
            # it so a partial deploy is visible (not reported as "nothing done").
            print(
                f"linkedin_outreach schema migration failed after "
                f"{actions}: {exc}",
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
