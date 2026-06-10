"""Experiment tracking for autosales A/B testing.

`experiments.tsv` (a flat TSV at the repo root) is the SYSTEM OF RECORD.
The Attio `experiment` object was descoped (Wave-2-A, 2026-06-01) — it
was never deployed (404 live) and its manifest entry is `descoped`. All
reads come from the TSV directly; there is no Attio probe and no per-read
WARN.

Resolution order for `load_experiments(path=None, *, attio=None)`:

1. If `path` is explicitly provided → read that TSV directly. Preserves
   backward compatibility with `workflows.learn`'s `experiments_path`
   argument and all existing tests.
2. Default — read the canonical `experiments.tsv` at the repo root.

A missing TSV → return `[]` (no alarm), preserving prior behaviour.

The `attio` keyword argument is retained on the read helpers for
backward-compatible call signatures but is IGNORED — the Attio
experiment object no longer exists. The legacy experiments-source env
var is also retired (it only ever selected the TSV vs. the now-gone
Attio path).

Writes: `append_experiment` appends one row (append-only); `upsert_experiment`
updates a row in place by `experiment_id` (last-row semantics — used by
verdict persistence in `workflows.learn.apply_verdict`).
"""

from __future__ import annotations

import csv
import os
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

# fold-in NIT (type-design): PR-21 frozen_at attribute alias + terminal-set
# subtype. Used by ExperimentIdImmutableError.field typing and downstream
# guards. Keep in sync with workflows.pre_invite_check._IMMUTABLE_FROZEN_AT_VALUES.
FrozenAtState = Literal[
    "prospect",
    "connection_sent",
    "accepted",
    "legacy_pre_tsv_era",
    "legacy_inferred_by_archaeology",
    "legacy_pure_unknown",
]
ExperimentIdField = Literal["experiment_id", "experiment_id_frozen_at"]


class ExperimentStatus(StrEnum):
    RUNNING = "running"
    WON = "won"
    LOST = "lost"
    REJECTED = "rejected"
    REJECTED_DEFENSIVE = "rejected_defensive"
    INSUFFICIENT_DATA = "insufficient_data"
    SUPERSEDED = "superseded"
    REJECTED_NULL = "rejected_null"


SALES_AGENT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_TSV_PATH = SALES_AGENT_DIR / "experiments.tsv"

TSV_FIELDS = [
    "experiment_id",
    "started",
    "completed",
    "cohort_size",
    "dm_response_rate",
    "dm1_response_rate",
    "dm2_response_rate",
    "dm3_response_rate",
    "baseline_rate",
    "status",
    "variable",
    "description",
]


@dataclass
class Experiment:
    experiment_id: str
    started: str
    completed: str
    cohort_size: int
    dm_response_rate: float
    baseline_rate: float
    status: ExperimentStatus
    variable: str
    description: str
    dm1_response_rate: float | None = None
    dm2_response_rate: float | None = None
    dm3_response_rate: float | None = None


def _resolve_path(path) -> Path:
    if path is None:
        return DEFAULT_TSV_PATH
    return Path(path)


def _optional_float(value: str | None) -> float | None:
    """Parse a TSV cell as float, returning None for missing/empty/'None' values.

    Tolerates legacy rows that predate the per-step columns (DictReader returns
    None for missing keys; legacy backfill may leave empty strings or the literal
    string 'None').
    """
    if value is None or value == "" or value == "None":
        return None
    return float(value)


def _load_from_tsv(path) -> list[Experiment]:
    """Read experiments from a TSV file. Internal helper used by both the
    backward-compatible path-override and the cache fallback."""
    tsv_path = _resolve_path(path)
    if not tsv_path.exists():
        return []
    experiments = []
    with open(tsv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            experiments.append(
                Experiment(
                    experiment_id=row["experiment_id"],
                    started=row["started"],
                    completed=row["completed"],
                    cohort_size=int(row["cohort_size"]),
                    dm_response_rate=float(row["dm_response_rate"]),
                    baseline_rate=float(row["baseline_rate"]),
                    status=ExperimentStatus(row["status"]),
                    variable=row["variable"],
                    description=row["description"],
                    dm1_response_rate=_optional_float(row.get("dm1_response_rate")),
                    dm2_response_rate=_optional_float(row.get("dm2_response_rate")),
                    dm3_response_rate=_optional_float(row.get("dm3_response_rate")),
                )
            )
    return experiments


def load_experiments(path=None, *, attio=None) -> list[Experiment]:
    """Load experiments from the canonical `experiments.tsv` (system of record).

    Reads the TSV directly — no Attio probe, no per-read WARN (the Attio
    `experiment` object was descoped Wave-2-A). A missing TSV returns `[]`.

    `attio` is accepted for backward-compatible call signatures but is
    ignored. Schema-drift errors (a `ValueError` from an unknown
    `ExperimentStatus` in the TSV) propagate so the operator sees the bug
    rather than silently routing the daily DM tagger onto a stale cohort.
    """
    return _load_from_tsv(path if path is not None else DEFAULT_TSV_PATH)


def _serialize_optional_float(value: float | None) -> str:
    """Serialize an optional float to a TSV cell. None becomes empty string."""
    if value is None:
        return ""
    return str(value)


def _write_experiments_tsv(experiments: list[Experiment], tsv_path: Path) -> None:
    """Internal: write the full experiment list to a TSV file (header + rows).

    Atomic: serialize to a temp file in the SAME directory, then ``os.replace``
    into place. ``experiments.tsv`` is now the sole system of record (no Attio
    backstop), so a crash mid-write must never truncate/corrupt the canonical
    file — a reader sees either the old file or the fully-written new one. The
    temp file is removed if the write fails before the rename.
    """
    tmp = tsv_path.with_name(f"{tsv_path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=TSV_FIELDS, delimiter="\t")
            writer.writeheader()
            for exp in experiments:
                writer.writerow(
                    {
                        "experiment_id": exp.experiment_id,
                        "started": exp.started,
                        "completed": exp.completed,
                        "cohort_size": exp.cohort_size,
                        "dm_response_rate": exp.dm_response_rate,
                        "dm1_response_rate": _serialize_optional_float(exp.dm1_response_rate),
                        "dm2_response_rate": _serialize_optional_float(exp.dm2_response_rate),
                        "dm3_response_rate": _serialize_optional_float(exp.dm3_response_rate),
                        "baseline_rate": exp.baseline_rate,
                        "status": exp.status,
                        "variable": exp.variable,
                        "description": exp.description,
                    }
                )
        os.replace(tmp, tsv_path)
    finally:
        # Clean up the temp file if the rename never happened (write error).
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def upsert_experiment(exp: Experiment, path=None) -> bool:
    """Update an experiment row in place by `experiment_id`, or append it.

    `experiments.tsv` is the system of record. Verdict persistence
    (`workflows.learn.apply_verdict`) calls this to flip an experiment's
    `status` (and rewrite its terminal metrics) without leaving a duplicate
    row behind — unlike `append_experiment`, which is append-only.

    When `experiment_id` already appears in the TSV, the LAST matching row
    is replaced (matching the last-row-wins dedup that `load_experiments`
    consumers apply); all other rows are preserved verbatim. When the id is
    absent the row is appended. Returns True if an existing row was updated,
    False if the row was appended (new id or empty/missing file).
    """
    tsv_path = _resolve_path(path)
    existing = _load_from_tsv(tsv_path)

    # Find the LAST row index matching this experiment_id (last-row-wins).
    last_idx: int | None = None
    for i, e in enumerate(existing):
        if e.experiment_id == exp.experiment_id:
            last_idx = i

    if last_idx is None:
        existing.append(exp)
        updated = False
    else:
        existing[last_idx] = exp
        updated = True

    _write_experiments_tsv(existing, tsv_path)
    return updated


def append_experiment(exp: Experiment, path=None) -> None:
    """Append one experiment row to the TSV (append-only).

    `experiments.tsv` is the system of record. This helper is used to add a
    NEW experiment cohort. To transition an existing experiment's status
    (verdict persistence) use `upsert_experiment` so no duplicate row is
    left behind.
    """
    tsv_path = _resolve_path(path)
    write_header = not tsv_path.exists() or tsv_path.stat().st_size == 0
    with open(tsv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TSV_FIELDS, delimiter="\t")
        if write_header:
            writer.writeheader()
        writer.writerow(
            {
                "experiment_id": exp.experiment_id,
                "started": exp.started,
                "completed": exp.completed,
                "cohort_size": exp.cohort_size,
                "dm_response_rate": exp.dm_response_rate,
                "dm1_response_rate": _serialize_optional_float(exp.dm1_response_rate),
                "dm2_response_rate": _serialize_optional_float(exp.dm2_response_rate),
                "dm3_response_rate": _serialize_optional_float(exp.dm3_response_rate),
                "baseline_rate": exp.baseline_rate,
                "status": exp.status,
                "variable": exp.variable,
                "description": exp.description,
            }
        )


def get_active_experiments(path=None, *, attio=None) -> list[Experiment]:
    """Return experiments with status == 'running'.

    Deduplicates by experiment_id (last row wins) to handle
    append-only TSV where verdict rows follow the original running row.
    """
    experiments = load_experiments(path, attio=attio)
    by_id = {e.experiment_id: e for e in experiments}
    return [e for e in by_id.values() if e.status == ExperimentStatus.RUNNING]


# Maturity floor (mirrors workflows.learn._MATURITY_MIN_DMED). A 'won' source
# experiment whose cohort is below this is suspect: at low n the verdict may
# have been prior-dominated. We can't read the prior_dominated flag from the
# TSV (it isn't persisted), so cohort_size is the observable proxy.
_BASELINE_MATURITY_FLOOR = 15


def _aggregate_rate_from_steps(exp: Experiment) -> float | None:
    """Reconstruct an overall baseline rate from an experiment's per-step
    rates when its `dm_response_rate` column is 0.0/empty but the per-step
    columns ARE populated.

    Context (fix/audit-learning-loop-stats): the baseline-v0 row carries
    dm_response_rate=0.0 while dm1/dm2/dm3_response_rate hold real values
    (4.26% / 2.44% / 21.05%). Returning that 0.0 made every non-step
    experiment graded against 0.0 + 2pp — i.e. almost anything "won".

    The TSV stores per-step *rates* but NOT per-step *denominators*, so a
    true denominator-weighted aggregate cannot be reconstructed. We use the
    DM1 rate as the aggregate proxy: dm1 is the funnel entry point with the
    largest denominator and the dominant contributor to the overall DM
    response rate, so it is the closest honest single-number stand-in for
    "overall DM response rate". (When real per-step counts become available
    this should be replaced by a count-weighted mean.)

    Returns None when no per-step rate is populated (caller keeps prior
    behaviour).
    """
    # dm1 is the canonical aggregate proxy; fall through to dm2/dm3 only if
    # dm1 is missing, preferring the earliest populated step.
    for rate in (exp.dm1_response_rate, exp.dm2_response_rate, exp.dm3_response_rate):
        if rate is not None and rate > 0.0:
            return rate
    return None


def _resolve_baseline_from_experiment(exp: Experiment, source_label: str) -> float:
    """Return a usable overall baseline rate from `exp`, refusing a 0.0 that
    masks populated per-step columns.

    Guard (fix/audit-learning-loop-stats): a baseline of exactly 0.0 with
    populated per-step rates is a READ bug, not a real measurement. We
    recover the aggregate from the per-step rates instead of returning 0.0.
    """
    rate = exp.dm_response_rate
    if rate == 0.0:
        aggregate = _aggregate_rate_from_steps(exp)
        if aggregate is not None:
            print(
                f"WARNING: get_baseline_rate: {source_label} "
                f"(experiment_id={exp.experiment_id!r}) has dm_response_rate=0.0 "
                f"but populated per-step rates; using DM1-based aggregate "
                f"{aggregate:.4f} instead of 0.0. "
                "(experiments.tsv overall column is empty for this row — "
                "fixing the READ path, not the data.)",
                file=sys.stderr,
            )
            return aggregate
    return rate


def get_baseline_rate(path=None, *, attio=None) -> float:
    """Return the overall DM-response baseline rate.

    Resolution order:
      1. The dm_response_rate of the most recent 'won' experiment.
      2. baseline-v0's rate.
      3. 0.0 only when no experiments exist at all.

    Two hardenings (fix/audit-learning-loop-stats):
      - 0.0-with-populated-steps guard: a row whose overall column is 0.0
        but whose per-step columns are populated (baseline-v0 is exactly
        this) yields a DM1-based aggregate instead of a misleading 0.0.
      - prior-dominated source warning: when the most-recent 'won' source's
        cohort is below the maturity floor, its verdict may have been
        prior-dominated; we warn so the operator doesn't chain a fragile
        baseline forward unnoticed. (We can't read the prior_dominated flag
        from the TSV; cohort_size is the observable proxy.)
    """
    experiments = load_experiments(path, attio=attio)
    if not experiments:
        return 0.0

    # Most recent won experiment (last in file wins)
    won = [e for e in experiments if e.status == ExperimentStatus.WON]
    if won:
        source = won[-1]
        if source.cohort_size < _BASELINE_MATURITY_FLOOR:
            print(
                f"WARNING: get_baseline_rate: most-recent 'won' baseline source "
                f"experiment_id={source.experiment_id!r} has cohort_size="
                f"{source.cohort_size} (< maturity floor {_BASELINE_MATURITY_FLOOR}); "
                "its verdict may have been prior-dominated. Chaining this baseline "
                "forward carries that uncertainty. Verify the source cohort before "
                "trusting downstream verdicts.",
                file=sys.stderr,
            )
        return _resolve_baseline_from_experiment(source, "most-recent 'won' source")

    # Fall back to baseline-v0
    for e in experiments:
        if e.experiment_id == "baseline-v0":
            return _resolve_baseline_from_experiment(e, "baseline-v0 fallback")

    return 0.0


_STEP_FIELD_MAP = {
    "dm1": "dm1_response_rate",
    "dm2": "dm2_response_rate",
    "dm3": "dm3_response_rate",
}


def _step_rate(exp: Experiment, step: str) -> float | None:
    """Return the per-step rate for a given step on an experiment, or None."""
    field = _STEP_FIELD_MAP.get(step)
    if field is None:
        return None
    return getattr(exp, field, None)


def _variable_touches_step(variable: str, step: str) -> bool:
    """True when an experiment's `variable` column targets the given DM step.

    Variable format used by experiments: 'messages.json:<step>:<persona>:<lang>'
    or similar. Step segment is the second colon-delimited piece.
    """
    if not variable:
        return False
    parts = variable.split(":")
    return len(parts) >= 2 and parts[1] == step


def get_baseline_step_rate(step: str, path=None, *, attio=None) -> float:
    """Return the per-step baseline response rate for a given DM step.

    Resolution order:
      1. Most recent 'won' experiment whose `variable` targets this step.
      2. baseline-v0's per-step rate.
      3. Overall get_baseline_rate (covers both pre-backfill and unknown-step calls).
    """
    if step not in _STEP_FIELD_MAP:
        return get_baseline_rate(path, attio=attio)

    experiments = load_experiments(path, attio=attio)

    # Most recent won experiment that targeted this step
    won_for_step = [
        e for e in experiments
        if e.status == ExperimentStatus.WON and _variable_touches_step(e.variable, step)
    ]
    if won_for_step:
        rate = _step_rate(won_for_step[-1], step)
        if rate is not None:
            return rate

    # Fall back to baseline-v0's per-step rate
    for e in experiments:
        if e.experiment_id == "baseline-v0":
            rate = _step_rate(e, step)
            if rate is not None:
                return rate
            break

    # Final fallback: overall baseline rate
    return get_baseline_rate(path, attio=attio)


class ExperimentIdImmutableError(RuntimeError):
    """§3.1 hard-red-line guard — DO NOT catch broadly.

    Raised when a caller attempts to overwrite an already-frozen
    experiment_id or experiment_id_frozen_at field. Subclass of RuntimeError
    specifically to avoid silent swallow by ``except ValueError:`` blocks
    (three such blocks exist in daily_check.py at lines ~93, ~1034, ~1066,
    ~1728 — subclassing ValueError would silently swallow §3.1 violations).

    Typed attributes:
      - record_id: Attio record ID of the affected LinkedIn Outreach entry.
      - entry_id: Attio list-entry ID (for cross-referencing).
      - field: The attribute slug being overwritten ("experiment_id" or
        "experiment_id_frozen_at").
      - old_value: The current (immutable) value in Attio.
      - new_value: The attempted replacement value.
    """

    def __init__(
        self,
        *,
        record_id: str,
        entry_id: str | None = None,
        field: ExperimentIdField,
        old_value: str | None,
        new_value: str | None,
    ) -> None:
        self.record_id = record_id
        self.entry_id = entry_id
        self.field: ExperimentIdField = field
        self.old_value = old_value
        self.new_value = new_value
        super().__init__(
            f"§3.1 immutability violation: cannot overwrite {field!r} "
            f"on record {record_id!r} (entry_id={entry_id!r}): "
            f"old={old_value!r} → new={new_value!r}. "
            "DO NOT catch broadly — this is a §3.1 hard-red-line guard."
        )


class MultipleRunningExperimentsError(RuntimeError):
    """Raised when `get_current_experiment_id` finds more than one running
    experiment.

    Per §0 invariant #9: no silent fallback. The operator must resolve the
    ambiguity (manually close one experiment) before the daily run proceeds.
    The error message includes all running experiment_ids so the operator can
    triage without digging through Attio.

    Exposes the ambiguous experiment IDs as a typed attribute (`experiment_ids`)
    so callers (e.g., the daily entrypoint pre-flight guard) can format their
    own error messages or take programmatic action without re-parsing the
    exception message string.
    """

    def __init__(self, experiment_ids: tuple[str, ...]) -> None:
        self.experiment_ids = experiment_ids
        super().__init__(
            f"Multiple running experiments detected: {list(experiment_ids)}. "
            "Resolve the ambiguity (close one experiment) before proceeding."
        )


def get_current_experiment_id(path=None, *, attio=None) -> str | None:
    """Return the experiment_id of the currently running experiment, or None.

    Semantics (§0 invariant #9 — no silent fallback):
    - Zero running experiments → returns None (explicit "no current experiment").
    - Exactly one running experiment → returns its experiment_id.
    - Two or more running experiments → raises MultipleRunningExperimentsError
      with all experiment_ids in the message so the operator can triage.

    Callers that previously relied on the "returns baseline-v0" fallback must
    now handle None explicitly.
    """
    running = get_active_experiments(path, attio=attio)
    if not running:
        return None
    if len(running) == 1:
        return running[0].experiment_id
    ids = tuple(e.experiment_id for e in running)
    raise MultipleRunningExperimentsError(ids)
