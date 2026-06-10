#!/usr/bin/env python3
"""Add the ``Unreachable`` status to the linkedin_outreach ``stage`` field (Wave-2-A).

# Why

`stage` is an Attio *status*-type attribute with a fixed option set. The
Wave-2-A `UNREACHABLE` PipelineStage (for InMail-required DMs + Out-of-Network
invites) writes `stage = "Unreachable"`; that PATCH 400s until the status option
exists in Attio (the `week_starting`-class trap). This migration deploys the
option so the writers in `daily_check.run_dm_sequencing` (InMail dead-end) and
`pre_invite_check._pre_invite_degree_check` (OON park) are safe.

DEPLOY THIS BEFORE the UNREACHABLE-writing code goes live, or those writes 400.

# What it does

Idempotently POSTs a single status, ``Unreachable``, to the
`/lists/{list}/attributes/stage/statuses` sub-resource. Additive: it never
edits or archives existing statuses. A second `--apply` is a no-op (the status
already exists → skipped).

# Running

    # Preview (default, read-only — GET statuses, report would-add/skipped):
    python scripts/migrate_attio_add_unreachable_stage.py
    python scripts/migrate_attio_add_unreachable_stage.py --dry-run

    # Live deploy (writes to production Attio):
    python scripts/migrate_attio_add_unreachable_stage.py --apply

DEFAULTS TO DRY-RUN. An explicit ``--apply`` is required to write to production
Attio.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clients.attio import AttioClient  # noqa: E402
from workflows.migration_run_writer import MigrationRunWriter  # noqa: E402

SCRIPT_VERSION = "1.0"
TARGET_LIST = "linkedin_outreach"
STAGE_SLUG = "stage"
NEW_STATUS_TITLE = "Unreachable"


def ensure_stage_status(
    attio: AttioClient, *, list_id: str, title: str, dry_run: bool,
) -> str:
    """Idempotently add a status option to the `stage` status attribute.

    Returns ``"skipped"`` (already present), ``"would_add"`` (dry-run), or
    ``"added"``. Mirrors the select-option backfill pattern in
    `_attio_migration_helpers.reconcile_select_options`, but targets the
    status sub-resource (`/statuses`) rather than `/options`.

    Archived statuses are filtered: Attio returns them in the list but
    rejects writes against them, and an operator archiving "Unreachable"
    via the UI must not make this migration believe it's still present.
    """
    base = f"/lists/{list_id}/attributes/{STAGE_SLUG}/statuses"
    data = attio._request("GET", base)
    existing = {
        s.get("title")
        for s in data.get("data", [])
        if not s.get("is_archived")
    }
    if title in existing:
        return "skipped"
    if dry_run:
        return "would_add"
    try:
        attio._request("POST", base, json={"data": {"title": title}})
    except httpx.HTTPStatusError as exc:
        # Surface the response body + sent payload so a body-shape mismatch
        # is a minutes-not-hours debug (the status-create body is less
        # battle-tested than the select-option one).
        if exc.response.status_code == 400:
            raise RuntimeError(
                f"Attio rejected POST {base} with 400. "
                f"Response: {exc.response.text}. "
                f"Sent body: {{'data': {{'title': {title!r}}}}}."
            ) from exc
        raise
    return "added"


def deploy(
    attio: AttioClient, *, list_id: str, dry_run: bool, run: MigrationRunWriter,
    actions: dict[str, str],
) -> None:
    """Add the Unreachable status. Additive + idempotent. Logged on the
    Migration Run row via object="schema" (sentinel that skips the
    back-pointer PATCH — no target record)."""
    run.examine()
    actions[NEW_STATUS_TITLE] = ensure_stage_status(
        attio, list_id=list_id, title=NEW_STATUS_TITLE, dry_run=dry_run,
    )
    if all(a == "skipped" for a in actions.values()):
        run.skip_idempotent()
    else:
        run.mark_modified(
            record_id=f"{TARGET_LIST}.stage.unreachable", object="schema",
        )


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

    if args.apply and args.dry_run:
        print(
            "error: pass either --apply or --dry-run, not both "
            "(refusing to guess — --apply would silently win).",
            file=sys.stderr,
        )
        return 2

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
        rollback_script_path=None,  # additive; Attio status removal is operator-manual-only
        dry_run=dry_run,
        attio=attio,
    ) as run:
        try:
            deploy(attio, list_id=list_id, dry_run=dry_run, run=run, actions=actions)
        except Exception as exc:
            run.mark_failed(record_id=f"{TARGET_LIST}.stage.unreachable", error=exc)
            print(
                f"add-unreachable-stage migration failed after {actions}: {exc}",
                file=sys.stderr,
            )
            failed = True

    print(json.dumps({
        "dry_run": dry_run,
        "list": TARGET_LIST,
        "attribute": STAGE_SLUG,
        "actions": actions,
    }, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
