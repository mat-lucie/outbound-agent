#!/usr/bin/env python3
"""Deploy missing Attio schema entries from the manifest (F-PR-3.5b).

# What this script does

Walks `docs/attio_schema_deltas.yaml`, identifies every entry with
`status: shipped` (configurable via `--status-filter`), and POSTs the
ones that don't exist in production Attio. Idempotent on the (object,
slug) pair via `scripts._attio_migration_helpers.ensure_attribute`.

# Why it exists

Phase-G lens-QA pass-1 round-2 (internal QA finding R2-B1):
the manifest had drifted ~30 attrs + 7 objects ahead of production
Attio because individual PRs flipped their manifest entries
`planned→shipped` when the *writer code* shipped, not when the *Attio
schema* shipped. Every write path through those slugs would have
404'd. This script closes the deploy gap in one shot and is safe to
re-run forever (idempotent + dry-run).

# Holdback per agent-4 rethink (Wave-2-A)

The manifest's top-level `deploy_holdback_objects` lists object slugs
whose data is being collapsed into column-collections on existing
objects rather than created as standalone Attio objects. This script
SKIPS the holdback set entirely — neither the object nor any of its
child attributes get deployed. Once the rethink follow-up lands the
new placements, the manifest entries get re-pointed and the holdback
list shrinks.

# Order of operations

1. NEW OBJECTS first — any object referenced by a shipped attr that
   doesn't exist in production gets created via `ensure_object`. The
   set is the union of:
     * explicit `new_objects` entries with the target status
     * objects auto-derived from any shipped `attributes` entry
   minus the pre-existing baseline (companies, people, deals), the
   list-typed objects (linkedin_outreach is a LIST), and the holdback.

2. ATTRIBUTES second — for each shipped attribute entry, call
   `ensure_attribute`. The helper handles:
     * type-name mapping (`datetime`→`timestamp`, `long_text`→`text`,
       `record_reference`→`record-reference`)
     * idempotency: skips when attribute exists with the expected type
     * select-option backfill: adds missing options if attr exists but
       options drifted
     * record-reference target UUID resolution (helper looks up the
       target object's UUID from its slug)

3. Migration Run row gets written on `__exit__` per §3.13.

# Running

    # Preview without writing (default behavior):
    python scripts/migrate_attio_schema_F-PR-3.5b.py
    python scripts/migrate_attio_schema_F-PR-3.5b.py --dry-run  # equivalent

    # Live deploy (writes to production Attio):
    python scripts/migrate_attio_schema_F-PR-3.5b.py --apply

The script DEFAULTS TO DRY-RUN. An explicit `--apply` flag is required
to write to production Attio. This guards against the operator footgun
of pasting the command from chat history without the dry-run flag.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clients.attio import AttioClient  # noqa: E402
from scripts._attio_migration_helpers import (  # noqa: E402
    ensure_attribute,
    ensure_object,
)
from workflows.migration_run_writer import MigrationRunWriter  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = REPO_ROOT / "docs" / "attio_schema_deltas.yaml"

# linkedin_outreach is a LIST under the people object, not a standalone
# object. Attribute creates target /lists/{list_id}/attributes.
LINKEDIN_OUTREACH_LIST_ID = "a68f3b3e-b957-40a9-bcbd-8f3b4b6f4188"
LIST_PARENT_IDS: dict[str, str] = {
    "linkedin_outreach": LINKEDIN_OUTREACH_LIST_ID,
}

# Pre-existing Attio objects we never (re-)create.
PREEXISTING_OBJECTS: frozenset[str] = frozenset({
    "companies", "people", "deals",
})

# Optional override of (singular_noun, plural_noun) when ensure_object
# needs to create a new object. Defaults to title-cased slug + "s" when
# absent.
OBJECT_NOUNS: dict[str, tuple[str, str]] = {
    "experiment": ("Experiment", "Experiments"),
    "llm_budget_ledger": ("LLM Budget Ledger", "LLM Budget Ledger Entries"),
    "weekly_kpi_snapshot": ("Weekly KPI Snapshot", "Weekly KPI Snapshots"),
}

# record_reference target resolution. The manifest doesn't carry an
# explicit target slug — we encode the mapping here so the helper can
# look up the target object's UUID. KEY is (object, attribute_slug);
# VALUE is the target object slug. List-typed attrs (merged_into,
# linkedin_outreach_entry) point at the LIST's parent object since
# Attio record_reference attrs target objects, not list entries.
REFERENCE_TARGETS: dict[tuple[str, str], str] = {
    ("linkedin_outreach", "last_classified_by"): "reclassification_run",
    ("linkedin_outreach", "last_migrated_by"): "migration_run",
    ("linkedin_outreach", "merged_into"): "people",
    ("companies", "last_outreach_person_id"): "people",
}


# Manifest placeholder pattern: a select attribute may declare
# options=["see X.Y.Z (N items)"] when the canonical source lives in a
# Python constant rather than the manifest. We resolve those at deploy
# time so ensure_attribute creates the attr with the real options.
def _resolve_placeholder_options(
    obj: str, slug: str, raw: list[str] | None,
) -> list[str] | None:
    if not raw or len(raw) != 1 or not isinstance(raw[0], str):
        return raw
    if not raw[0].lower().startswith("see "):
        return raw
    if obj == "operator_review_queue" and slug == "type":
        from workflows.escalation_schemas import ESCALATION_TYPES
        return list(ESCALATION_TYPES)
    if obj == "operator_review_queue" and slug == "decision_key":
        from workflows.escalation_schemas import CONFIGURATION_DECISION_KEYS
        return list(CONFIGURATION_DECISION_KEYS)
    if obj == "companies" and slug == "industry_vertical":
        from models.campaign import INDUSTRY_LABELS
        return list(INDUSTRY_LABELS.keys())
    # Unknown placeholder — surface loudly rather than POST a sentinel
    # string as the only option.
    raise ValueError(
        f"unresolved options placeholder for {obj}.{slug}: {raw!r} — "
        f"add a branch to _resolve_placeholder_options."
    )


def _obj_nouns(slug: str) -> tuple[str, str]:
    if slug in OBJECT_NOUNS:
        return OBJECT_NOUNS[slug]
    title = slug.replace("_", " ").title()
    return (title, title + "s")


def _collect_object_set(
    manifest: dict, status_filter: str, holdback: frozenset[str],
) -> tuple[list[str], list[str]]:
    """Return (explicit_new_objects, auto_derived_from_attrs).

    Both lists are de-duped and exclude:
      - pre-existing baseline objects (companies/people/deals)
      - list-typed objects (linkedin_outreach — not created via script)
      - holdback objects per `deploy_holdback_objects` in the manifest
    """
    explicit: list[str] = []
    explicit_set: set[str] = set()
    for spec in manifest.get("new_objects", []) or []:
        if spec.get("status") != status_filter:
            continue
        slug = spec.get("object")
        if (
            not slug
            or slug in PREEXISTING_OBJECTS
            or slug in LIST_PARENT_IDS
            or slug in holdback
        ):
            continue
        if slug not in explicit_set:
            explicit_set.add(slug)
            explicit.append(slug)

    auto: list[str] = []
    auto_set: set[str] = set()
    for spec in manifest.get("attributes", []) or []:
        if spec.get("status") != status_filter:
            continue
        slug = spec.get("object")
        if (
            not slug
            or slug in PREEXISTING_OBJECTS
            or slug in LIST_PARENT_IDS
            or slug in holdback
            or slug in explicit_set
            or slug in auto_set
        ):
            continue
        auto_set.add(slug)
        auto.append(slug)

    return (explicit, auto)


def _deploy(
    attio: AttioClient,
    manifest: dict,
    *,
    status_filter: str,
    dry_run: bool,
    run: MigrationRunWriter,
) -> dict:
    """Run the deploy. Returns a summary dict for the CLI report."""
    actions: list[dict] = []
    holdback = frozenset(manifest.get("deploy_holdback_objects", []) or [])

    explicit_objs, auto_objs = _collect_object_set(
        manifest, status_filter, holdback,
    )
    for kind, slugs in (("explicit", explicit_objs), ("auto", auto_objs)):
        for slug in slugs:
            run.examine()
            singular, plural = _obj_nouns(slug)
            try:
                action, body = ensure_object(
                    attio, slug, singular, plural, dry_run=dry_run,
                )
            except Exception as exc:
                run.mark_failed(record_id=slug, error=exc)
                actions.append({
                    "target": "object", "slug": slug, "source": kind,
                    "action": "failed", "error": str(exc)[:300],
                })
                continue
            if action == "skipped":
                run.skip_idempotent()
            else:
                run.mark_modified(record_id=slug, object="schema")
            actions.append({
                "target": "object", "slug": slug, "source": kind,
                "action": action,
            })

    skipped_holdback = 0
    for spec in manifest.get("attributes", []) or []:
        if spec.get("status") != status_filter:
            continue
        obj = spec["object"]
        slug = spec["slug"]
        if obj in holdback:
            skipped_holdback += 1
            continue
        if obj in LIST_PARENT_IDS:
            parent_kind = "list"
            parent_id = LIST_PARENT_IDS[obj]
        else:
            parent_kind = "object"
            parent_id = obj

        type_ = spec["type"]
        options = _resolve_placeholder_options(obj, slug, spec.get("options"))
        ref_target = REFERENCE_TARGETS.get((obj, slug))
        if type_ == "record_reference" and not ref_target:
            run.mark_failed(
                record_id=f"{obj}.{slug}",
                error=ValueError(
                    f"no REFERENCE_TARGETS entry for record_reference "
                    f"{obj}.{slug} — add one before re-running."
                ),
            )
            actions.append({
                "target": "attribute", "slug": f"{obj}.{slug}",
                "action": "failed",
                "error": f"no REFERENCE_TARGETS entry for {obj}.{slug}",
            })
            continue

        run.examine()
        # Manifest `note` is YAML free-text; Attio description is a flat
        # string. Truncate to 500 chars so we don't choke on multiline
        # essays in the manifest.
        desc = spec.get("note") or None
        if isinstance(desc, str) and len(desc) > 500:
            desc = desc[:497] + "..."

        try:
            action = ensure_attribute(
                attio, parent_kind, parent_id, slug, type_,
                options=options,
                description=desc,
                referenced_object=ref_target,
                dry_run=dry_run,
            )
        except Exception as exc:
            run.mark_failed(record_id=f"{obj}.{slug}", error=exc)
            actions.append({
                "target": "attribute", "slug": f"{obj}.{slug}",
                "type": type_, "action": "failed",
                "error": str(exc)[:300],
            })
            continue

        if action == "skipped":
            run.skip_idempotent()
        else:
            run.mark_modified(record_id=f"{obj}.{slug}", object="schema")
        actions.append({
            "target": "attribute", "slug": f"{obj}.{slug}",
            "type": type_, "action": action,
        })

    return {
        "rows_examined": run.rows_examined,
        "rows_modified": run.rows_modified,
        "rows_skipped_idempotent": run.rows_skipped_idempotent,
        "rows_failed": run.rows_failed,
        "holdback_objects_skipped": sorted(holdback),
        "holdback_attrs_skipped_count": skipped_holdback,
        "actions": actions,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--manifest", type=Path, default=DEFAULT_MANIFEST,
        help=f"Path to manifest YAML (default: {DEFAULT_MANIFEST})",
    )
    parser.add_argument(
        "--status-filter", default="shipped",
        choices=("planned", "shipped", "verified"),
        help="Only deploy entries with this status (default: shipped).",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help=(
            "Live deploy: write changes to production Attio. Without this "
            "flag the script runs in dry-run mode and prints what would "
            "change. --dry-run is also accepted (and is the default)."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help=(
            "Explicit dry-run. Equivalent to omitting --apply. Kept for "
            "backwards compatibility with operator muscle memory."
        ),
    )
    args = parser.parse_args(argv)

    # Default is dry-run. Live deploy requires explicit --apply.
    # --dry-run is accepted for clarity but doesn't change behavior vs default.
    dry_run = not args.apply

    manifest = yaml.safe_load(args.manifest.read_text(encoding="utf-8")) or {}
    if not isinstance(manifest, dict):
        print(
            f"error: manifest top-level must be a mapping; got {type(manifest).__name__}",
            file=sys.stderr,
        )
        return 2

    try:
        attio = AttioClient()
    except KeyError:
        print("error: ATTIO_API_KEY env var not set", file=sys.stderr)
        return 2

    summary: dict = {}
    with MigrationRunWriter(
        script_name="scripts/migrate_attio_schema_F-PR-3.5b.py",
        script_version="F-PR-3.5b",
        # Attio object/attribute creation is operator-manual reversal
        # only — no rollback_script_path → MigrationRunWriter writes
        # `rollback_status="irreversible"` on the run row.
        rollback_script_path=None,
        dry_run=dry_run,
        attio=attio,
    ) as run:
        summary = _deploy(
            attio, manifest,
            status_filter=args.status_filter,
            dry_run=dry_run, run=run,
        )

    print(json.dumps({
        "run_id": run.run_id,
        "status_filter": args.status_filter,
        "dry_run": dry_run,
        **{k: v for k, v in summary.items() if k != "actions"},
    }, indent=2))

    # Actions are verbose — emit to stderr so stdout stays parseable.
    print(json.dumps({"actions": summary["actions"]}, indent=2), file=sys.stderr)

    if dry_run:
        print(
            "\nThis was a DRY-RUN preview. To execute the deploy against "
            "production Attio, re-run with --apply.",
            file=sys.stderr,
        )

    return 1 if run.rows_failed > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
