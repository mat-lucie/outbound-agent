#!/usr/bin/env python3
"""Migrate experiments.tsv to the Attio Experiment object (F-PR-6).

Lays down the `experiment` Attio object with its F-PR-6 attributes
(per `docs/attio_schema_deltas.yaml`) and upserts the 5 current TSV
rows into the new Experiment object.

# §3.17 supersedence rule

The TSV has a duplicate `experiment_id` ("exp-20260505-ops-dm1-no-editorial"
appears twice: once `status="running"` with `completed=""`, then once
`status="rejected"` with `completed="2026-05-11"`). The migration applies
the winner-by-completion-date rule:

    For each duplicate experiment_id, the row with non-null `completed`
    wins. The status from the winning row is kept. A `superseded` event
    is appended to the loser's contribution to `state_history`.

The Experiment object is upserted on `experiment_id`, so a second
invocation of this script is a no-op (rows_skipped_idempotent == 5).

# Idempotency contract

- Re-running after objects exist is a no-op for object creation.
- Re-running after attributes exist is a no-op for attribute creation.
- Re-running after rows exist is a no-op for upserts (same key →
  Attio returns existing row).
- Exactly 5 Experiment rows created on first run (4 unique IDs +
  1 historical baseline-v0).

# Reversibility

Object deletion is operator-manual-only (Attio API limitation).
`rollback_status='irreversible'` recorded in the MigrationRunWriter.
To revert: delete the Experiment object via Attio UI, restore
`experiments.tsv` from git, redeploy.

# Bootstrap dependency

Depends on F-PR-3.7's MigrationRunWriter + F-PR-3's escalation
infrastructure. Runs AFTER F-PR-3.7's `scripts/migrate_attio_F2_*.py`
has created the Migration Run object.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from clients.attio import AttioClient  # noqa: E402
from workflows.migration_run_writer import MigrationRunWriter  # noqa: E402

# F-PR-6 schema. Tuples are (slug, type, options, required).
#
# This list is intentionally limited to the BASE Experiment object attrs.
# PR-33's per-experiment verdict attrs (`verdict_v1`, `verdict_v2`,
# `dm3_window_settles_at`) are owned by `workflows.weekly_brain` and ship
# in that PR's own migration — they MUST NOT be added here without an
# explicit manifest update + multi-writer declaration in the registry.
EXPERIMENT_ATTRIBUTES = [
    ("experiment_id", "text", None, True),
    (
        "status",
        "select",
        [
            "running",
            "won",
            "lost",
            "rejected",
            "rejected_null",
            "rejected_defensive",
            "superseded",
            "insufficient_data",
        ],
        True,
    ),
    ("started", "date", None, True),
    ("completed", "date", None, False),
    ("variable", "text", None, False),
    ("description", "long_text", None, False),
    ("state_history", "long_text", None, False),
    ("cohort_size", "number", None, False),
    ("dm_response_rate", "number", None, False),
    ("dm1_response_rate", "number", None, False),
    ("dm2_response_rate", "number", None, False),
    ("dm3_response_rate", "number", None, False),
    ("baseline_rate", "number", None, False),
]


def _ensure_object(
    attio: AttioClient,
    slug: str,
    singular: str,
    plural: str,
) -> tuple[bool, str]:
    """Create the Attio object if it doesn't exist. Returns (created, object_id).

    Idempotent: a 409 on already-exists is treated as "found, skip"; the
    existing object id is returned.
    """
    try:
        resp = attio._client.request(
            "POST",
            "/objects",
            json={
                "data": {
                    "api_slug": slug,
                    "singular_noun": singular,
                    "plural_noun": plural,
                }
            },
        )
        resp.raise_for_status()
        return True, resp.json()["data"]["id"]["object_id"]
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409:
            # Already exists; fetch and return its id.
            resp = attio._client.request("GET", f"/objects/{slug}")
            resp.raise_for_status()
            return False, resp.json()["data"]["id"]["object_id"]
        raise


def _ensure_attribute(
    attio: AttioClient,
    object_slug: str,
    attr_slug: str,
    attr_type: str,
    options: list[str] | str | None,
    required: bool,
) -> bool:
    """Create the attribute on the object if it doesn't exist.

    Returns True if newly created, False if already existed. Idempotent.
    For `select` types, `options` is the list of option strings. For
    `record_reference` types, `options` is the target object slug.
    """
    config: dict = {}
    if attr_type == "select" and isinstance(options, list):
        config["options"] = [{"title": o} for o in options]
    elif attr_type == "record_reference" and isinstance(options, str):
        config["allowed_objects"] = [options]

    body = {
        "data": {
            "api_slug": attr_slug,
            "title": attr_slug.replace("_", " ").title(),
            "type": attr_type,
            "is_required": required,
            "is_unique": False,
            "is_multiselect": False,
            "config": config,
        }
    }

    try:
        resp = attio._client.request(
            "POST",
            f"/objects/{object_slug}/attributes",
            json=body,
        )
        resp.raise_for_status()
        return True
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409:
            return False
        if exc.response.status_code == 400:
            # Attio sometimes returns 400 with "duplicate slug" for races
            # where the create lost the slug-collision check. Body inspection
            # guards against silently swallowing a real bad-request (wrong
            # type, malformed config) as "already exists".
            try:
                body_text = exc.response.text.lower()
            except Exception:
                body_text = ""
            if "duplicate" in body_text or "already exists" in body_text:
                return False
        raise


def _ensure_objects_and_attrs(attio: AttioClient) -> dict:
    """Phase 1: bring the schema up to date. Counts how many object + attribute creations actually happened."""
    summary = {
        "experiment_object_created": False,
        "experiment_attrs_created": 0,
        "experiment_attrs_skipped": 0,
    }

    created, _ = _ensure_object(attio, "experiment", "Experiment", "Experiments")
    summary["experiment_object_created"] = created
    for slug, atype, options, required in EXPERIMENT_ATTRIBUTES:
        if _ensure_attribute(attio, "experiment", slug, atype, options, required):
            summary["experiment_attrs_created"] += 1
        else:
            summary["experiment_attrs_skipped"] += 1

    return summary


def _apply_supersedence_rule(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """§3.17: when a duplicate experiment_id appears, keep the row with
    non-null `completed`. The losing row is folded into the winner's
    `state_history` as a `superseded` event.

    Tiebreak when multiple rows have non-null `completed`:
    latest-by-file-order wins. TSV is append-only, so file order is
    canonical "this is how the operator chronologically recorded the
    supersedence". Sorting by `completed` date arithmetic would
    surprise an operator who manually reordered rows.

    Returns (winners, supersedence_events). The events list is
    deterministic across re-runs (no wall-clock timestamps) so the
    migration's idempotency contract holds: identical TSV → identical
    state_history → no Attio PATCH on second invocation.
    """
    by_id: dict[str, list[dict]] = {}
    for row in rows:
        by_id.setdefault(row["experiment_id"], []).append(row)

    winners: list[dict] = []
    events: list[dict] = []
    for experiment_id, group in by_id.items():
        if len(group) == 1:
            winners.append(group[0])
            continue
        # Tie-break: row with non-null `completed` wins.
        with_completed = [r for r in group if r.get("completed", "").strip()]
        winner = with_completed[-1] if with_completed else group[-1]
        winners.append(winner)
        for loser in group:
            if loser is winner:
                continue
            # Deterministic timestamp derived from loser's own data —
            # NEVER `datetime.now()`. Re-running the migration on the
            # same TSV must produce a byte-identical event so the
            # idempotency check skips the PATCH.
            loser_completed = loser.get("completed", "").strip()
            loser_started = loser.get("started", "").strip()
            event_timestamp = loser_completed or loser_started or "unknown"
            events.append({
                "experiment_id": experiment_id,
                "timestamp": event_timestamp,
                "status": "superseded",
                "loser_status": loser["status"],
                "loser_started": loser_started,
                "loser_completed": loser_completed,
                "reason": (
                    f"Migration §3.17 supersedence: row with "
                    f"status={loser['status']!r} and "
                    f"completed={loser_completed!r} folded "
                    f"into the winning row."
                ),
            })
    return winners, events


def _build_state_history(row: dict, supersedence_events: list[dict]) -> str:
    """Construct the state_history JSONL string for an Experiment row.

    Always includes a `created` event for the row's own (started, status)
    pair. Appends any matching supersedence events.
    """
    lines = [
        json.dumps({
            "timestamp": row["started"],
            "status": row["status"],
            "reason": "Migrated from experiments.tsv via F-PR-6",
        })
    ]
    for evt in supersedence_events:
        if evt["experiment_id"] == row["experiment_id"]:
            lines.append(json.dumps(evt))
    return "\n".join(lines)


def _attio_first_text(values: dict, key: str) -> str:
    """Defensive read of a first wrapped value as a string.

    Mirrors `models.experiment._attio_first_value` but returns `""` for
    any malformed shape rather than raising — used by the idempotency
    check where a missing attribute on a half-built row should not crash
    the migration before the PATCH has a chance to repair it.
    """
    arr = values.get(key)
    if not isinstance(arr, list) or not arr:
        return ""
    item = arr[0]
    if not isinstance(item, dict):
        return ""
    return str(item.get("value", ""))


def _upsert_experiment(
    attio: AttioClient,
    row: dict,
    state_history: str,
) -> tuple[str, bool]:
    """Upsert an Experiment row on `experiment_id`.

    Returns ('created' | 'updated' | 'skipped_idempotent', changed_or_not).
    """
    # Query for existing row by experiment_id.
    existing = attio._client.request(
        "POST",
        "/objects/experiment/records/query",
        json={
            "filter": {"experiment_id": row["experiment_id"]},
            "limit": 1,
        },
    )
    existing.raise_for_status()
    data = existing.json().get("data", [])

    def _opt_number(v: str) -> float | None:
        # `None` (vs `0.0`) is meaningful per §0 invariant #5 — it
        # distinguishes "unmeasured" from "measured-as-zero". Attio's
        # `number` type accepts JSON `null` as "cleared/absent" and
        # reads back as an empty list; the round-trip preserves the
        # None semantics. Verify on first deploy that a null write
        # reads back as `[]` rather than coercing to `0.0`.
        if v in (None, "", "None"):
            return None
        return float(v)

    values: dict = {
        "experiment_id": row["experiment_id"],
        "status": row["status"],
        "started": row["started"],
        "completed": row.get("completed", "") or None,
        "variable": row.get("variable", ""),
        "description": row.get("description", ""),
        "state_history": state_history,
        "cohort_size": int(row.get("cohort_size", 0)),
        "dm_response_rate": _opt_number(row.get("dm_response_rate", "")),
        "dm1_response_rate": _opt_number(row.get("dm1_response_rate", "")),
        "dm2_response_rate": _opt_number(row.get("dm2_response_rate", "")),
        "dm3_response_rate": _opt_number(row.get("dm3_response_rate", "")),
        "baseline_rate": _opt_number(row.get("baseline_rate", "")),
    }
    body = {"data": {"values": values}}

    if data:
        record_id = data[0]["id"]["record_id"]
        # Compare current values to detect true no-ops.
        current_values = data[0].get("values", {})
        # Loose comparison — Attio wraps values in single-element lists
        # with metadata. For idempotency we compare `state_history` (the
        # field that uniquely captures both migration-time content AND
        # supersedence events); a deterministic _build_state_history +
        # deterministic _apply_supersedence_rule guarantee re-runs hit
        # this branch with byte-identical strings.
        existing_history = _attio_first_text(current_values, "state_history")
        if existing_history == state_history:
            return ("skipped_idempotent", False)
        attio._client.request(
            "PATCH",
            f"/objects/experiment/records/{record_id}",
            json=body,
        ).raise_for_status()
        return ("updated", True)
    else:
        attio._client.request(
            "POST",
            "/objects/experiment/records",
            json=body,
        ).raise_for_status()
        return ("created", True)


def _read_tsv(tsv_path: Path) -> list[dict]:
    with open(tsv_path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate experiments.tsv → Attio Experiment object (F-PR-6)."
    )
    parser.add_argument(
        "--tsv",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "experiments.tsv",
        help="Path to experiments.tsv (default: repo root)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read + plan but don't write to Attio. Prints the resolved upserts.",
    )
    args = parser.parse_args()

    if not args.tsv.exists():
        print(f"ERROR: {args.tsv} does not exist", file=sys.stderr)
        return 1

    rows = _read_tsv(args.tsv)
    winners, supersedence_events = _apply_supersedence_rule(rows)

    print(f"Read {len(rows)} TSV rows; {len(winners)} unique experiments after §3.17 supersedence rule.")
    for evt in supersedence_events:
        print(f"  Supersedence: {evt['experiment_id']} ← {evt['reason']}")

    if args.dry_run:
        print("\n--- DRY RUN: would upsert ---")
        for row in winners:
            history = _build_state_history(row, supersedence_events)
            print(f"  {row['experiment_id']} (status={row['status']}, completed={row.get('completed', '')!r})")
            print(f"    state_history: {history[:120]}…")
        return 0

    with AttioClient() as attio, MigrationRunWriter(
        attio=attio,
        script_name="migrate_experiments_tsv_to_attio",
        script_version="F-PR-6-v1",
        rollback_script_path=None,  # irreversible (object deletion is operator-manual)
    ) as run:
        # Schema phase INSIDE the writer context so a partial-build
        # crash leaves a Migration Run row with failure_details
        # captured. Outside the context, a crash mid-attribute would
        # leave Attio with a half-built schema and zero audit trail.
        schema_summary = _ensure_objects_and_attrs(attio)
        print(f"\nSchema: {schema_summary}")
        for row in winners:
            run.examine()
            history = _build_state_history(row, supersedence_events)
            outcome, _ = _upsert_experiment(attio, row, history)
            if outcome == "skipped_idempotent":
                run.skip_idempotent()
            else:
                # `object="schema"` skips the back-pointer PATCH —
                # F-PR-3.7 only added `last_migrated_by` to LinkedIn
                # Outreach / Companies / People / Deals, not to
                # Experiment. Extending the pointer to Experiment
                # is a separate decision (manifest update + per-PR
                # entry) that F-PR-6 deliberately defers.
                run.mark_modified(
                    record_id=row["experiment_id"],
                    object="schema",
                )

    return 0


if __name__ == "__main__":
    # Wave-2-A (2026-06-01): the Attio `experiment` object this script created
    # was descoped (never deployed, 404 live). experiments.tsv is now the
    # system of record — there is nothing to migrate TO, and running this would
    # crash on a live 404. Fail loud at the CLI boundary rather than misbehave.
    # Helpers (_read_tsv / _apply_supersedence_rule / _ensure_attribute) stay
    # importable for the existing test suite + schema-symmetry references.
    raise SystemExit(
        "experiment Attio object descoped 2026-06-01 — experiments.tsv is the "
        "system of record; this migration is obsolete (see the 2026-06-01 "
        "Attio object-model consolidation decision)."
    )
    sys.exit(main())
