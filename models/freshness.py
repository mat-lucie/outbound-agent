"""Target-list freshness data shapes (PR-29 fold — module split per
code-reviewer + type-design QA convergence).

The freshness band thresholds + `FreshnessStatus` enum +
`FreshnessResult` dataclass + `WeeklyTargetListStaleError` exception
live here as pure domain types. The orchestration logic
(`check_target_list_freshness`) was moved to
`workflows/target_freshness.py` so the layering follows the project
convention: `models/` for data shapes, `workflows/` for I/O.

See `workflows/target_freshness.py` for the gate function itself and
the orchestration semantics (queue-write-before-raise, idempotency,
operator UX).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date  # noqa: TC003 — re-exported in dataclass field annotation
from enum import StrEnum
from pathlib import Path  # noqa: TC003 — re-exported in dataclass field annotation

WARN_THRESHOLD_DAYS = 30
STALE_THRESHOLD_DAYS = 60


class FreshnessStatus(StrEnum):
    """Three-band freshness status. `StrEnum` (Python 3.11+) keeps
    `.value` as a plain string for Attio + JSON serialization.
    """
    FRESH = "fresh"
    WARN = "warn_30_60"
    STALE = "stale_over_60"


@dataclass(frozen=True)
class FreshnessResult:
    """Outcome of `check_target_list_freshness`.

    `status` is the band; `days_stale` is the integer-day age (always
    >= 0, enforced via `max(0, ...)` clamp in the function);
    `last_modified_at` is the file's effective-mtime (file mtime,
    OR the `_meta.updated_at` field inside the JSON if present per
    the code-reviewer QA convergence on git-checkout false-refresh).
    `target_list_path` is the absolute path that was checked.
    """
    status: FreshnessStatus
    days_stale: int
    last_modified_at: date
    target_list_path: Path

    def __post_init__(self) -> None:
        # Type-design QA recommendation: bulletproof the
        # status↔days_stale correlation against any future caller
        # that constructs a result by hand.
        if self.days_stale < 0:
            raise ValueError(f"days_stale must be >= 0; got {self.days_stale}")
        if self.status is FreshnessStatus.FRESH and self.days_stale >= WARN_THRESHOLD_DAYS:
            raise ValueError(
                f"FRESH status requires days_stale < {WARN_THRESHOLD_DAYS}; "
                f"got {self.days_stale}"
            )
        if self.status is FreshnessStatus.WARN and not (
            WARN_THRESHOLD_DAYS <= self.days_stale < STALE_THRESHOLD_DAYS
        ):
            raise ValueError(
                f"WARN status requires {WARN_THRESHOLD_DAYS} <= days_stale "
                f"< {STALE_THRESHOLD_DAYS}; got {self.days_stale}"
            )
        if self.status is FreshnessStatus.STALE and self.days_stale < STALE_THRESHOLD_DAYS:
            raise ValueError(
                f"STALE status requires days_stale >= {STALE_THRESHOLD_DAYS}; "
                f"got {self.days_stale}"
            )


class WeeklyTargetListStaleError(RuntimeError):
    """Raised when one or more target lists are >60d old. The runtime
    caller (`/sales-weekly`) catches this and halts the batch.

    Carries the list of `FreshnessResult` objects that triggered the
    halt (one per stale file). Multi-STALE collection (silent-failure
    + pr-test QA convergence): the orchestrator iterates ALL persona
    target lists, writes a queue row per stale file, and raises this
    error once at the end with the full set — operators get a single
    terminal error AND complete queue-row coverage AND know the full
    refresh scope in one cycle, not N cycles.
    """

    def __init__(self, results: list[FreshnessResult]) -> None:
        if not results:
            raise ValueError("WeeklyTargetListStaleError requires at least one result")
        self.results = results
        # Format: "N target list(s) stale; halting weekly batch:
        # path1 (Nd), path2 (Md). Operator Review Queue row(s) opened."
        details = ", ".join(
            f"{r.target_list_path} ({r.days_stale}d)" for r in results
        )
        super().__init__(
            f"{len(results)} target list(s) stale; halting weekly batch: "
            f"{details}. Operator Review Queue row(s) opened for triage."
        )

    @property
    def result(self) -> FreshnessResult:
        """Backwards-compat single-result accessor (returns the first
        stale result for callers that don't iterate `.results`).
        """
        return self.results[0]
