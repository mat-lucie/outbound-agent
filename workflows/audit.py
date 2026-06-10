"""Per-run JSONL audit log infrastructure (F-PR-2).

Every state-changing daily/weekly run emits append-only JSONL lines via
`AuditLogger`. Lines carry a `run_id` so a crashed run can be inspected
via `resume_from_audit(run_id)` and replayed selectively.

F-PR-2 is behavior-additive: it adds the writer + reader + CLI surface
but does not wire into existing workflows. Downstream PRs (PR-22
archaeology, PR-15 Pattern-A flip, PR-17 daily_run summary, the §3.18
nurture-silent-skip log line, etc.) consume `AuditLogger` directly.

Audit files live at:

    ~/.outbound-agent/audit/run-{YYYY-MM-DD}-{run_id}.jsonl

Each line is one JSON object with at least:

    {"ts": "ISO8601", "run_id": "...", "seq": int, "event": "..."}

…plus any keyword payload the caller passed to `AuditLogger.event(...)`.
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Iterator

AUDIT_DIR = Path.home() / ".outbound-agent" / "audit"


def new_run_id() -> str:
    """Return a fresh 12-character run identifier."""
    return uuid.uuid4().hex[:12]


def _audit_path(run_id: str, run_date: date | None = None) -> Path:
    run_date = run_date or date.today()
    return AUDIT_DIR / f"run-{run_date.isoformat()}-{run_id}.jsonl"


def _find_audit_path(run_id: str) -> Path:
    """Locate the audit file for an existing `run_id` across all dates."""
    if not AUDIT_DIR.exists():
        raise FileNotFoundError(f"audit dir missing: {AUDIT_DIR}")
    matches = sorted(AUDIT_DIR.glob(f"run-*-{run_id}.jsonl"), reverse=True)
    if not matches:
        raise FileNotFoundError(f"no audit log for run_id={run_id}")
    # Newest first if run_id collides across dates (uuid12 collision is negligible).
    return matches[0]


class AuditLogger:
    """Append-only JSONL writer keyed by run_id.

    The context-manager form is canonical — it brackets the run with
    `run_started` / `run_completed` (or `run_crashed`) events so
    `resume_from_audit` can detect status without scanning every line.

    Example:

        with AuditLogger(workflow="daily_check") as log:
            log.event("connection_request_sent", record_id=rid, persona=p)
            log.event("dm_sent", record_id=rid, step="DM1")
    """

    def __init__(
        self,
        run_id: str | None = None,
        *,
        workflow: str,
        dry_run: bool = False,
        run_date: date | None = None,
    ) -> None:
        self.run_id = run_id or new_run_id()
        self.workflow = workflow
        self.dry_run = dry_run
        self.path: Path = _audit_path(self.run_id, run_date)
        self._fh: IO[str] | None = None
        self._sequence = 0

    def __enter__(self) -> AuditLogger:
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a")
        self.event(
            "run_started",
            host=socket.gethostname(),
            pid=os.getpid(),
            workflow=self.workflow,
            dry_run=self.dry_run,
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> Literal[False]:
        try:
            if exc_type is None:
                self.event("run_completed")
            else:
                self.event(
                    "run_crashed",
                    exc_type=exc_type.__name__,
                    # Truncate to keep audit lines bounded; full traceback
                    # belongs in stderr / Sentry, not the audit log.
                    exc_msg=str(exc_val)[:500],
                )
        finally:
            if self._fh is not None:
                self._fh.close()
                self._fh = None
        return False  # never suppress

    def event(self, event: str, **payload: Any) -> None:
        """Append one JSONL line. `event` is the typed event name; any
        keyword args are merged into the payload."""
        if self._fh is None:
            raise RuntimeError(
                "AuditLogger.event called outside `with` context "
                "(or after __exit__). Wrap your run in `with AuditLogger(...) as log:`."
            )
        self._sequence += 1
        line = {
            "ts": datetime.now(UTC).isoformat(),
            "run_id": self.run_id,
            "seq": self._sequence,
            "event": event,
            **payload,
        }
        self._fh.write(json.dumps(line, separators=(",", ":"), default=str) + "\n")
        # Flush every line so a crash leaves a usable trail.
        self._fh.flush()


def list_runs(since_days: int = 7) -> list[Path]:
    """Return audit files modified within `since_days`, newest first."""
    if not AUDIT_DIR.exists():
        return []
    cutoff_ts = datetime.now(UTC).timestamp() - since_days * 86_400
    return sorted(
        (p for p in AUDIT_DIR.glob("run-*.jsonl") if p.stat().st_mtime >= cutoff_ts),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def iter_events(run_id: str) -> Iterator[dict]:
    """Yield events from a run's audit log in seq order.

    Empty lines are skipped (defensive against a crash mid-newline-write);
    malformed lines raise `json.JSONDecodeError` so corruption is loud.
    """
    path = _find_audit_path(run_id)
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


# Housekeeping events that don't carry meaningful per-record state.
# Excluded from `record_ids_seen` even though `run_crashed` may technically
# carry a `record_id` if the caller embedded one before crashing.
_HOUSEKEEPING_EVENTS = frozenset({"run_started", "run_completed", "run_crashed"})


def resume_from_audit(run_id: str) -> dict:
    """Produce a resume manifest for `run_id`.

    Returns a dict with run metadata and the set of `record_id` values
    that appeared in the run. The caller is responsible for deciding
    which event names count as "completion" in their workflow context —
    this function does NOT make that distinction.

    Manifest fields:
        run_id, status, started_at, last_seq, last_event,
        crash_exc_type (str | None — only set when status='crashed'),
        crash_exc_msg (str | None — only set when status='crashed'),
        record_ids_seen (set[str]) — every record_id appearing in any
          non-housekeeping, non-error event,
        record_ids_failed (set[str]) — record_ids from `error` events.

    `status` ∈ {'completed', 'crashed', 'in_progress', 'empty'}.

    IMPORTANT — `record_ids_seen` is NOT "definitely completed":
        It counts every record the run touched, including dry-run
        previews and silent skips (e.g., the §3.18 `nurture_silent_skip`
        line carries `record_id` but no Attio mutation occurred).
        Callers responsible for record_id-level dedup on resume MUST
        narrow this set by intersecting with their workflow's specific
        completion-event names (e.g., {"dm_sent", "connection_request_sent"}).
        Use `record_ids_for_events(run_id, completion_events)` for that.

    Resume contract: a new run gets a new `run_id`; the old run's file
    stays untouched as evidence (the §3-hard-constraint #10
    determinism + replayability invariant).
    """
    events = list(iter_events(run_id))
    if not events:
        return {
            "run_id": run_id,
            "status": "empty",
            "started_at": None,
            "last_seq": 0,
            "last_event": None,
            "crash_exc_type": None,
            "crash_exc_msg": None,
            "record_ids_seen": set(),
            "record_ids_failed": set(),
        }

    started_at = events[0].get("ts")
    last = events[-1]
    if last["event"] == "run_completed":
        status = "completed"
        crash_exc_type, crash_exc_msg = None, None
    elif last["event"] == "run_crashed":
        status = "crashed"
        crash_exc_type = last.get("exc_type")
        crash_exc_msg = last.get("exc_msg")
    else:
        status = "in_progress"
        crash_exc_type, crash_exc_msg = None, None

    record_ids_seen = {
        e["record_id"]
        for e in events
        if "record_id" in e
        and e.get("event") not in _HOUSEKEEPING_EVENTS
        and e.get("event") != "error"
    }
    record_ids_failed = {
        e["record_id"] for e in events
        if "record_id" in e and e.get("event") == "error"
    }

    return {
        "run_id": run_id,
        "status": status,
        "started_at": started_at,
        "last_seq": last["seq"],
        "last_event": last["event"],
        "crash_exc_type": crash_exc_type,
        "crash_exc_msg": crash_exc_msg,
        "record_ids_seen": record_ids_seen,
        "record_ids_failed": record_ids_failed,
    }


def record_ids_for_events(run_id: str, event_names: set[str]) -> set[str]:
    """Return the set of `record_id` values that appeared in any event
    whose name is in `event_names`.

    Use this from a workflow's resume path to narrow `record_ids_seen`
    to "what actually got applied" (e.g.,
    `record_ids_for_events(run_id, {"dm_sent", "connection_request_sent"})`).
    """
    return {
        e["record_id"]
        for e in iter_events(run_id)
        if "record_id" in e and e.get("event") in event_names
    }


def audit_stats(since_days: int = 7) -> dict:
    """Aggregate stats over recent audit logs.

    Resilient to per-line JSON corruption: a single malformed line in
    any run is skipped (counted in `malformed_line_count`) rather than
    halting the entire cross-run rollup. Per-run inspection via
    `iter_events` remains loud-by-design — `audit_stats` is the
    cross-run dashboard, where one bad file shouldn't kill visibility
    into the rest.

    Returns:
        {
          "run_count": int,
          "completed_count": int,
          "crashed_count": int,
          "in_progress_count": int,
          "event_count_total": int,
          "event_count_by_type": {event_name: count, ...},
          "malformed_line_count": int,
        }
    """
    runs = list_runs(since_days)
    by_event: dict[str, int] = {}
    completed = crashed = in_progress = 0
    total_events = 0
    malformed = 0
    for path in runs:
        last_event_name: str | None = None
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    malformed += 1
                    continue
                total_events += 1
                name = ev.get("event", "<missing>")
                by_event[name] = by_event.get(name, 0) + 1
                last_event_name = name
        if last_event_name == "run_completed":
            completed += 1
        elif last_event_name == "run_crashed":
            crashed += 1
        else:
            in_progress += 1
    return {
        "run_count": len(runs),
        "completed_count": completed,
        "crashed_count": crashed,
        "in_progress_count": in_progress,
        "event_count_total": total_events,
        "event_count_by_type": by_event,
        "malformed_line_count": malformed,
    }
