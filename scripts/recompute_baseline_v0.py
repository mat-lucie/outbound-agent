#!/usr/bin/env python3
"""Recompute the baseline-v0 experiment's per-step rates from Attio cohort data.

# Three-step transactional contract (Round-4 D14 — authoritative)

1. Compute new baseline-v0 rates in-memory from Attio cohort data using
   `workflows.learn._per_step_rates` + BayesianPosterior posterior means.
2. Open a `Reclassification Run` row capturing the cohort hash + record count
   in `audit_log_pointer`. The hash deterministically identifies the cohort for
   crash recovery; individual record_ids are recoverable from the cohort's hash
   + the underlying Attio cohort data at recompute time (not stored on the
   rec_run row).
3. Write the recomputed rates to the Attio `experiment` object (baseline-v0 row)
   and append a `superseded` event to `state_history`, cross-referencing the
   Reclassification Run's `run_id`. Also open a Migration Run row that
   cross-references the Reclassification Run via `reclassification_run_id`.

# Crash recovery (§9.4)

If the script crashes between step 2 (Reclassification Run opened) and step 3
(rates written), re-running the script detects the orphaned Reclassification Run
by matching the input cohort data hash. It resumes from step 3 — does NOT open a
second Reclassification Run for the same cohort data.

Idempotency key: SHA-256 of the sorted input record_ids (16-char prefix). A
second `--apply` run with identical cohort data is a no-op (rows_modified == 0).
Crash-resume dedup guard: before appending a `superseded` event, the writer
checks the existing `state_history` for an event already carrying the same
`reclassification_run_id` and skips the append if found. This prevents
double-append when step 3 succeeded the PATCH but crashed before the
MigrationRun row was created (operationally meaningful `datetime.now()`
timestamps are preserved without sacrificing idempotency).

# Prior-dominated gate (§0 invariant #9)

If any per-step posterior has n_observed < SMALL_N_THRESHOLD (5), the cohort is
prior-dominated and the script refuses to overwrite baseline-v0 with what is
effectively prior-mean noise. Exits 1 with a diagnostic stderr message naming
the thin step(s). Operator must wait for the cohort to mature or close
baseline-v0 manually.

# §3.1 hard red line

The `superseded` event write is the audit trail for baseline-v0 replacement.
If this write fails, the recompute is incomplete — the script exits with code 1.
Do NOT silence this failure.

# Exit codes

  0  success (or dry-run)
  1  partial failure (rates written but superseded event failed, or other error)
  75 Attio MCP scope failure (EX_TEMPFAIL — transient, retry is appropriate)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clients.attio import AttioClient  # noqa: E402
from workflows.learn import _is_prior_dominated, _per_step_rates  # noqa: E402
from workflows.migration_run_writer import MigrationRunWriter  # noqa: E402
from workflows.reclassification_run_writer import ReclassificationRunWriter  # noqa: E402

# Transient HTTP exceptions worth catching narrowly in helper queries — they
# represent "could not determine state" cases. Anything broader (a programmer
# error, an OOM, a KeyError) must propagate to the outer try in main() which
# maps to EX_TEMPFAIL or exit 1 with a traceback.
_TRANSIENT_HTTP_ERRORS: tuple[type[BaseException], ...] = (
    httpx.HTTPStatusError,
    httpx.RequestError,
)

# EX_TEMPFAIL from sysexits.h — "temporary failure, command can be retried"
EX_TEMPFAIL = 75

SCRIPT_NAME = "scripts/recompute_baseline_v0.py"
BASELINE_V0_ID = "baseline-v0"


def _cohort_hash(record_ids: list[str]) -> str:
    """Stable SHA-256 of sorted record_ids (16-char hex prefix).

    Idempotency key: same cohort data → same hash → script detects an
    existing Reclassification Run opened for this cohort and resumes
    from step 3 rather than opening a second run.

    Note: the hash includes ALL record_ids returned by
    `_load_baseline_v0_entries` — including rows that would be excluded from
    the per-step denominator (e.g., dm{N}_sent_at NULL). This is intentional:
    the hash identifies the COHORT INPUT (membership of baseline-v0 minus
    soft-deleted rows), not the per-step eligible set. Two runs with the same
    cohort membership will get the same hash regardless of dm-step eligibility
    drift between the runs.
    """
    canonical = "\n".join(sorted(record_ids))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _load_baseline_v0_entries(attio: AttioClient) -> tuple[list[dict], list[str]]:
    """Return (dmed_entries, record_ids) for the baseline-v0 cohort.

    Queries Attio list entries, filters to experiment_id == 'baseline-v0',
    parses each entry, and strips soft-deleted (merged_into) rows to match
    the denominator exclusion in `workflows.learn.measure_cohorts`.
    """
    raw_entries = attio.query_list_entries(limit=5000)
    parsed = [attio.parse_entry(e) for e in raw_entries]
    # §3.11 soft-delete filter — mirrors measure_cohorts
    parsed = [e for e in parsed if not e.get("merged_into")]
    # Filter to baseline-v0 cohort
    cohort = [e for e in parsed if e.get("experiment_id") == BASELINE_V0_ID]
    record_ids = [e["record_id"] for e in cohort if e.get("record_id")]
    return cohort, record_ids


def _find_orphaned_rec_run(attio: AttioClient, cohort_hash: str) -> dict | None:
    """Check Attio for an orphaned Reclassification Run opened by this script.

    Looks for rows where `classifier_module` == this script name AND
    `input_attr` encodes the cohort hash (stored as classifier_module tag).
    Returns the run dict if found, None otherwise.

    An 'orphaned' run is one that was opened (step 2) but for which the
    corresponding Migration Run row referencing it does not yet exist (step 3
    never completed).
    """
    try:
        # Use AttioClient._request so we benefit from its 429/502/503 backoff
        # and ReadTimeout retry. Narrow the catch to transient HTTP errors —
        # programming errors (KeyError, NameError, AttributeError) should NOT
        # be silenced; they propagate to the outer try in main() and surface
        # to the operator with a traceback.
        data = attio._request(
            "POST",
            "/objects/reclassification_run/records/query",
            json={
                "filter": {"classifier_module": SCRIPT_NAME},
                "limit": 50,
            },
        )
        rows = data.get("data", [])
    except _TRANSIENT_HTTP_ERRORS:
        # If we can't query, proceed as if no orphan — will open a new run.
        return None

    for row in rows:
        values = row.get("values", {})
        # The cohort hash is encoded in the `audit_log_pointer` field
        stored_hash = _attio_text(values, "audit_log_pointer")
        if stored_hash and cohort_hash in stored_hash:
            run_id = _attio_text(values, "run_id")
            record_id = row.get("id", {}).get("record_id")
            if run_id and record_id:
                # Check whether step 3 already completed for this rec_run
                # by looking for a Migration Run that cross-references it.
                if _migration_run_exists_for(attio, run_id):
                    # Step 3 already done — fully idempotent, skip everything.
                    return {"run_id": run_id, "record_id": record_id, "completed": True}
                return {"run_id": run_id, "record_id": record_id, "completed": False}
    return None


def _attio_text(values: dict, key: str) -> str:
    arr = values.get(key)
    if not isinstance(arr, list) or not arr:
        return ""
    item = arr[0]
    if not isinstance(item, dict):
        return str(item)
    return str(item.get("value", ""))


def _migration_run_exists_for(attio: AttioClient, rec_run_id: str) -> bool:
    """Return True if a Migration Run row already references this rec_run_id."""
    try:
        data = attio._request(
            "POST",
            "/objects/migration_run/records/query",
            json={
                "filter": {"script_name": SCRIPT_NAME},
                "limit": 50,
            },
        )
        rows = data.get("data", [])
    except _TRANSIENT_HTTP_ERRORS:
        return False
    for row in rows:
        values = row.get("values", {})
        stored = _attio_text(values, "reclassification_run_id")
        if stored == rec_run_id:
            return True
    return False


def _get_baseline_v0_attio_record_id(attio: AttioClient) -> tuple[str | None, str | None]:
    """Return (record_id, error_message) for the baseline-v0 Experiment row.

    On success returns (record_id, None). On a true "not found" returns
    (None, None). On a transient HTTP failure returns (None, error_text)
    so the caller can surface the underlying cause to the operator instead
    of telling them to run the F-PR-6 migration (which would be a misleading
    hint when the real failure is, e.g., a 503 from Attio).
    """
    try:
        data = attio._request(
            "POST",
            "/objects/experiment/records/query",
            json={
                "filter": {"experiment_id": BASELINE_V0_ID},
                "limit": 1,
            },
        )
        rows = data.get("data", [])
        if rows:
            return rows[0]["id"]["record_id"], None
        return None, None
    except _TRANSIENT_HTTP_ERRORS as exc:
        return None, f"transient Attio query failure: {exc!r}"


def _get_existing_record_values(attio: AttioClient, record_id: str) -> dict:
    """Return the full `values` dict for the baseline-v0 row.

    Returns {} on transient HTTP failure. The caller then sees empty
    `state_history` (treated as fresh) and None for prior rates, which is a
    safe over-write degradation — the operator can still recover prior values
    from Attio's row history if forensic detail is needed later.
    """
    try:
        data = attio._request(
            "GET", f"/objects/experiment/records/{record_id}"
        )
        values = data.get("data", {}).get("values", {})
        if isinstance(values, dict):
            return values
        return {}
    except _TRANSIENT_HTTP_ERRORS:
        return {}


def _attio_number(values: dict, key: str) -> float | None:
    """Extract a numeric scalar from an Attio values dict, or None."""
    arr = values.get(key)
    if not isinstance(arr, list) or not arr:
        return None
    item = arr[0]
    if not isinstance(item, dict):
        try:
            return float(item)
        except (TypeError, ValueError):
            return None
    raw = item.get("value")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _build_superseded_event(
    rec_run_id: str,
    old_dm1: float | None,
    old_dm2: float | None,
    old_dm3: float | None,
    new_dm1: float,
    new_dm2: float,
    new_dm3: float,
    cohort_size: int,
    experiment_id: str = BASELINE_V0_ID,
) -> dict:
    """Build the §3.17-formatted superseded event for the state_history append.

    Timestamp is derived from the recompute's wall-clock time — unlike the
    F-PR-6 TSV migration which uses deterministic dates, a recompute IS a
    time-stamped event. The crash-resume dedup guard (in
    `_write_rates_and_superseded_event`) keeps idempotency intact: a second
    run carrying the same `reclassification_run_id` will detect the existing
    event and skip the append rather than re-appending with a new timestamp.

    Schema divergence from F-PR-6's `superseded` event (intentional, not a
    drift): F-PR-6's TSV migration emits supersedence to record WHICH cohort
    was superseded — fields `loser_status`, `loser_started`, `loser_completed`.
    PR-11's recompute supersedence records WHAT RATES CHANGED for an in-place
    recompute — fields `prior_dm{N}_response_rate`, `new_dm{N}_response_rate`,
    `reclassification_run_id`. Both schemas share `status='superseded'` and a
    deterministic identifier; the rest differs by intent. Consumers reading
    state_history should branch on the presence of `reclassification_run_id`
    (PR-11 recompute) vs. `loser_status` (F-PR-6 TSV migration).
    """
    return {
        "timestamp": datetime.now(UTC).strftime("%Y-%m-%d"),
        "status": "superseded",
        "experiment_id": experiment_id,
        "reason": (
            f"{experiment_id} rates recomputed from Attio cohort data "
            f"(cohort_size={cohort_size}) via {SCRIPT_NAME}; "
            f"reclassification_run_id={rec_run_id}"
        ),
        "reclassification_run_id": rec_run_id,
        "prior_dm1_response_rate": old_dm1,
        "prior_dm2_response_rate": old_dm2,
        "prior_dm3_response_rate": old_dm3,
        "new_dm1_response_rate": new_dm1,
        "new_dm2_response_rate": new_dm2,
        "new_dm3_response_rate": new_dm3,
    }


def _superseded_event_already_appended(
    existing_history: str, rec_run_id: str
) -> bool:
    """Return True if a `superseded` event carrying `rec_run_id` is already
    present in `existing_history`.

    Dedup guard for crash-resume: if step 3's PATCH succeeded but the script
    crashed before opening the MigrationRun row, a naive re-run would
    double-append the same superseded event with a fresh `datetime.now()`
    timestamp. This guard reads the existing JSONL and skips the append when
    an event with the same `reclassification_run_id` is already present.

    The check tolerates non-JSON lines (e.g., legacy plain-text events written
    before §3.17 formalized the schema) — they are skipped rather than
    treated as errors.
    """
    if not existing_history:
        return False
    for line in existing_history.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        if (
            event.get("status") == "superseded"
            and event.get("reclassification_run_id") == rec_run_id
        ):
            return True
    return False


def _write_rates_and_superseded_event(
    attio: AttioClient,
    record_id: str,
    existing_history: str,
    superseded_event: dict,
    new_dm1: float,
    new_dm2: float,
    new_dm3: float,
    rec_run_id: str,
) -> str:
    """Step 3: write recomputed rates + append superseded event to state_history.

    Returns the resulting state_history string (for log/test introspection).

    §3.1 hard red line: if the PATCH itself fails, the caller must treat the
    recompute as incomplete. This function raises on any Attio error — do not
    catch here.

    Crash-resume dedup: if a `superseded` event with the same `rec_run_id` is
    already in `existing_history` (we crashed between the PATCH and the
    MigrationRun row creation on a prior attempt), the append is skipped and
    the PATCH is still issued — the values are idempotent on equal inputs and
    the missing MigrationRun row will be created by the caller next.
    """
    if _superseded_event_already_appended(existing_history, rec_run_id):
        print(
            f"INFO: superseded event for reclassification_run_id={rec_run_id} "
            "already present in state_history; skipping JSONL append (idempotent "
            "crash-resume — re-issuing PATCH with identical rate values).",
            file=sys.stderr,
        )
        new_state_history = existing_history
    else:
        new_history_lines = (
            existing_history.rstrip("\n").split("\n") if existing_history else []
        )
        new_history_lines.append(json.dumps(superseded_event))
        new_state_history = "\n".join(line for line in new_history_lines if line)

    body = {
        "data": {
            "values": {
                "dm1_response_rate": new_dm1,
                "dm2_response_rate": new_dm2,
                "dm3_response_rate": new_dm3,
                "state_history": new_state_history,
            }
        }
    }
    # Use AttioClient._request so 429/502/503 backoff applies. Any
    # raise_for_status failure propagates to the caller (§3.1 hard red line).
    attio._request(
        "PATCH",
        f"/objects/experiment/records/{record_id}",
        json=body,
    )
    return new_state_history


def main(argv: list[str] | None = None) -> int:
    """Three-step transactional recompute. Returns exit code."""
    parser = argparse.ArgumentParser(
        description=(
            "Recompute baseline-v0 per-step rates from Attio cohort data.\n"
            "Three-step transactional: compute → open ReclassificationRun → write rates + superseded event."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and log; no Attio writes.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Apply the recompute to Attio.",
    )
    args = parser.parse_args(argv)

    try:
        attio = AttioClient()
    except Exception as exc:
        print(
            f"ERROR: Could not construct AttioClient: {exc}",
            file=sys.stderr,
        )
        return EX_TEMPFAIL

    # ── Step 1: Compute new rates in-memory ──────────────────────────────────
    print("Step 1: Loading baseline-v0 cohort from Attio …")
    try:
        with attio:
            cohort_entries, record_ids = _load_baseline_v0_entries(attio)
    except Exception as exc:
        print(f"ERROR: Failed to load cohort entries: {exc}", file=sys.stderr)
        return EX_TEMPFAIL

    cohort_size = len(cohort_entries)
    print(f"  Cohort size: {cohort_size} entries, {len(record_ids)} record_ids")

    if not cohort_entries:
        print("  WARNING: baseline-v0 cohort is empty — no rates to recompute.")
        if args.dry_run:
            return 0
        return 1

    # Compute Bayesian posteriors using PR-10's _per_step_rates
    per_step = _per_step_rates(cohort_entries)
    new_dm1 = per_step["dm1_response_rate"]
    new_dm2 = per_step["dm2_response_rate"]
    new_dm3 = per_step["dm3_response_rate"]

    print("  Computed posteriors:")
    print(f"    dm1_response_rate = {new_dm1:.6f} (posterior mean)")
    print(f"    dm2_response_rate = {new_dm2:.6f} (posterior mean)")
    print(f"    dm3_response_rate = {new_dm3:.6f} (posterior mean)")
    print(f"    dm1 n_observed = {per_step['dm1_received']}, n_successes = {per_step['dm1_replies']}")
    print(f"    dm2 n_observed = {per_step['dm2_received']}, n_successes = {per_step['dm2_replies']}")
    print(f"    dm3 n_observed = {per_step['dm3_received']}, n_successes = {per_step['dm3_replies']}")
    print(f"  Input record_ids (first 5): {record_ids[:5]}")

    # §0 invariant #9 gate: refuse to overwrite baseline-v0 with prior-mean
    # noise. `_is_prior_dominated` returns True if any per-step posterior has
    # n_observed < SMALL_N_THRESHOLD (5). Writing prior-mean rates as the new
    # baseline would silently degrade every downstream verdict comparison.
    if _is_prior_dominated(dict(per_step)):
        print(
            "ERROR: baseline-v0 cohort is prior-dominated (n_observed < 5 on at "
            "least one step); refusing to overwrite baseline-v0 with prior-mean "
            "rates. Per-step n_observed: "
            f"dm1={per_step['dm1_received']}, "
            f"dm2={per_step['dm2_received']}, "
            f"dm3={per_step['dm3_received']}. "
            "Wait for the cohort to mature or close baseline-v0 manually.",
            file=sys.stderr,
        )
        return 1

    c_hash = _cohort_hash(record_ids)
    print(f"  Cohort hash: {c_hash}")

    if args.dry_run:
        print("\nDRY RUN: no Attio writes. Use --apply to commit.")
        return 0

    # ── Step 2: Open Reclassification Run (or resume orphaned one) ───────────
    print("\nStep 2: Checking for orphaned Reclassification Run …")
    try:
        with AttioClient() as attio2:
            orphan = _find_orphaned_rec_run(attio2, c_hash)
    except Exception as exc:
        print(f"ERROR: Failed to check for orphaned run: {exc}", file=sys.stderr)
        return EX_TEMPFAIL

    rec_run_id: str
    if orphan is not None and orphan.get("completed"):
        print(f"  Found fully-completed run for this cohort (run_id={orphan['run_id']}). Idempotent no-op.")
        return 0

    if orphan is not None and not orphan.get("completed"):
        rec_run_id = orphan["run_id"]
        print(f"  Resuming orphaned Reclassification Run (run_id={rec_run_id})")
        print("  Skipping step 2 — proceeding directly to step 3.")
    else:
        print("  No orphan found. Opening new Reclassification Run …")
        try:
            with AttioClient() as attio3:
                with ReclassificationRunWriter(
                    classifier_module=SCRIPT_NAME,
                    model_used="deterministic",  # No LLM — pure Bayesian math
                    input_attr="experiment_cohort_entries",
                    output_attr="dm1_response_rate,dm2_response_rate,dm3_response_rate",
                    dry_run=False,
                    attio=attio3,
                    # Encode the cohort hash in audit_log_pointer for crash recovery.
                    audit_log_pointer=f"cohort_hash={c_hash}",
                ) as rec_writer:
                    # Record each cohort entry as examined
                    for rid in record_ids:
                        rec_writer.examine()
                        rec_writer.mark_success(record_id=rid, object="linkedin_outreach")
                rec_run_id = rec_writer.run_id
                print(f"  Reclassification Run opened: run_id={rec_run_id}")
        except Exception as exc:
            print(f"ERROR: Failed to open Reclassification Run: {exc}", file=sys.stderr)
            return EX_TEMPFAIL

    # ── Step 3: Write recomputed rates + superseded event ────────────────────
    print("\nStep 3: Writing recomputed rates and superseded event to Attio …")
    try:
        with AttioClient() as attio4:
            baseline_record_id, lookup_err = _get_baseline_v0_attio_record_id(attio4)
            if baseline_record_id is None:
                if lookup_err is not None:
                    print(
                        f"ERROR: failed to query Attio for the baseline-v0 row: "
                        f"{lookup_err}. Retry; if this persists, escalate.",
                        file=sys.stderr,
                    )
                else:
                    print(
                        "ERROR: baseline-v0 row not found in Attio Experiment object. "
                        "Run scripts/migrate_experiments_tsv_to_attio.py first.",
                        file=sys.stderr,
                    )
                return 1

            # Fetch the full record once and extract both state_history AND
            # the current rates so the superseded event records real prior
            # values (not None). 5-agent convergence on PR-11 review: a
            # superseded event with prior rates of None is forensically dead
            # — a future investigator can't reason about the delta.
            existing_values = _get_existing_record_values(attio4, baseline_record_id)
            existing_history = _attio_text(existing_values, "state_history")
            prior_dm1 = _attio_number(existing_values, "dm1_response_rate")
            prior_dm2 = _attio_number(existing_values, "dm2_response_rate")
            prior_dm3 = _attio_number(existing_values, "dm3_response_rate")

            # Build superseded event (§3.17 format)
            superseded_event = _build_superseded_event(
                rec_run_id=rec_run_id,
                old_dm1=prior_dm1,
                old_dm2=prior_dm2,
                old_dm3=prior_dm3,
                new_dm1=new_dm1,
                new_dm2=new_dm2,
                new_dm3=new_dm3,
                cohort_size=cohort_size,
                experiment_id=BASELINE_V0_ID,
            )

            # §3.1: this write is the audit trail. Failure here means the
            # recompute is incomplete. We do NOT silence the error.
            _write_rates_and_superseded_event(
                attio=attio4,
                record_id=baseline_record_id,
                existing_history=existing_history,
                superseded_event=superseded_event,
                new_dm1=new_dm1,
                new_dm2=new_dm2,
                new_dm3=new_dm3,
                rec_run_id=rec_run_id,
            )
            print(f"  Wrote dm1={new_dm1:.6f}, dm2={new_dm2:.6f}, dm3={new_dm3:.6f}")
            print(f"  Appended superseded event to state_history (reclassification_run_id={rec_run_id})")

            # Also open a Migration Run to cross-reference the Reclassification Run
            # (mirrors the PR-9b pattern from backfill_per_step_timestamps.py).
            with MigrationRunWriter(
                attio=attio4,
                script_name=SCRIPT_NAME,
                script_version=None,  # auto-detects via git SHA
                rollback_script_path=None,  # irreversible (rates overwritten)
                dry_run=False,
                reclassification_run_id=rec_run_id,
            ) as mig_run:
                mig_run.examine()
                mig_run.mark_modified(
                    record_id=baseline_record_id,
                    object="schema",  # Experiment row; no last_migrated_by back-pointer
                )
            print(f"  Migration Run opened: run_id={mig_run.run_id} (cross-ref: rec_run={rec_run_id})")

    except Exception as exc:
        # §3.1 hard red line: if this write fails, the recompute is incomplete.
        print(
            f"ERROR: Step 3 failed — recompute is INCOMPLETE. "
            f"Re-run the script to resume from step 3 (orphan detection will "
            f"find the existing Reclassification Run run_id={rec_run_id}). "
            f"Details: {exc}",
            file=sys.stderr,
        )
        return 1

    print("\nRecompute complete.")
    return 0


if __name__ == "__main__":
    # Wave-2-A (2026-06-01): this script reads + PATCHes the Attio
    # `experiment` object, which was descoped (never deployed, 404 live).
    # experiments.tsv is now the system of record, so running this would crash
    # on a live 404. Fail loud at the CLI boundary rather than misbehave. A TSV
    # rewrite is non-trivial (the script threads ReclassificationRun +
    # MigrationRun writers + a superseded-event append through Attio); rewrite
    # before re-enabling. `main()` stays importable/testable.
    raise SystemExit(
        "experiment Attio object descoped 2026-06-01 — rewrite to operate on "
        "experiments.tsv before use (see the 2026-06-01 Attio object-model "
        "consolidation decision)."
    )
    sys.exit(main())
