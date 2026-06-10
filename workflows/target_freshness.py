"""Target-list freshness gate (PR-29, B-PW-TARGETS-FRESH).

Module split out from `models/freshness.py` per code-reviewer +
type-design QA convergence: types stay in `models/`, I/O orchestration
lives here in `workflows/` so the layer dependency runs the right
direction (workflows → models, never inverted).

The weekly prospecting batch loads target-company lists from JSON
files under `content/{key}-targets.json`. Those lists go stale —
companies fold, get acquired, drop out of ICP — and a multi-month-old
target list silently degrades the prospect pool. This module adds a
freshness gate that runs as step 0 of `/sales-weekly`:

- File age <30d → `FreshnessStatus.FRESH` (no signal)
- File age 30-60d → `FreshnessStatus.WARN` (operator-visible stderr
  via `click.echo` so the signal survives logger-config differences
  per silent-failure-hunter QA convergence on §0 #9)
- File age ≥60d → `FreshnessStatus.STALE` — opens a typed
  `target_list_stale` Operator Review Queue row AND raises
  `WeeklyTargetListStaleError` so the batch halts before any
  PROSPECT commit.

The queue row is written BEFORE the exception is raised. If
`escalate()` itself raises (`MissingAttioCredentials`,
`EscalationSchemaError`, network error), the error is logged with
full freshness context AND `WeeklyTargetListStaleError` still
propagates — operators get both signals so an Attio outage doesn't
mask a real staleness violation (silent-failure QA convergence).

Freshness source: the function reads `_meta.updated_at` from inside
the JSON if present, falling back to file mtime. The `_meta` field
makes the gate robust against git checkout / git pull operations
that touch mtime without semantic refresh (code-reviewer QA
convergence). Operator workflow: bump `_meta.updated_at` to today
when truly refreshing the list.

Invocation surfaces (per §0 invariant #6 — no cron):
- Inside `/sales-weekly` as step 0, before any prospect harvest
  (`_check_all_persona_target_lists_fresh` in workflows.weekly_prospect).
- Standalone via `/audit-reminder-sweep` (PR-44 wiring); the stable
  entry point is `workflows.target_freshness.check_target_list_freshness`.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path  # noqa: TC003 — used both as annotation AND runtime (stat/exists)

import click

from clients.attio import AttioClient  # noqa: TC001 — runtime default for `attio=None`
from clients.crm.base import CRMProvider  # noqa: TC001 — union-shim annotation
from models.freshness import (
    STALE_THRESHOLD_DAYS,
    WARN_THRESHOLD_DAYS,
    FreshnessResult,
    FreshnessStatus,
    WeeklyTargetListStaleError,
)
from workflows.escalation import escalate

logger = logging.getLogger(__name__)


def _read_meta_updated_at(target_list_path: Path) -> date | None:
    """Read `_meta.updated_at` from the JSON if present, else None.

    Operators bump this field when semantically refreshing the list;
    falling back to file mtime is the catch-all but is fragile to git
    checkout/pull operations that touch mtime without true refresh
    (code-reviewer QA convergence). Any JSON-decode error or missing
    key returns None — the function silently falls back to mtime
    rather than raising on best-effort metadata.
    """
    try:
        with target_list_path.open() as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    meta = data.get("_meta")
    if not isinstance(meta, dict):
        return None
    raw = meta.get("updated_at")
    if not isinstance(raw, str):
        return None
    try:
        # Accept ISO date (2026-05-22) or ISO datetime (2026-05-22T10:30:00).
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def check_target_list_freshness(
    target_list_path: Path,
    today: date,
    *,
    attio: CRMProvider | AttioClient | None = None,
) -> FreshnessResult:
    """Check a target-list file's age against the freshness bands.

    `attio` is consulted ONLY on the STALE path (queue-row write);
    FRESH/WARN paths never touch it (type-design QA clarification).

    Args:
      target_list_path: Path to the JSON target list.
      today: Operator-invocation date (passed in for testability;
        the function never reads the clock — orchestrator uses
        `models.business_calendar.operator_today()`).
      attio: CRM access for the STALE queue write — accepts a
        `CRMProvider` (the vendor-neutral path) or an `AttioClient`
        (legacy), passed straight through to `escalate()`'s union
        shim. Defaults to building an `AttioProvider` from
        `ATTIO_API_KEY` per `escalate()`'s contract.

    Returns: `FreshnessResult` for FRESH and WARN bands.

    Raises:
      `WeeklyTargetListStaleError` (carrying a single-item results
      list) for the STALE band — after the queue row write.
      `FileNotFoundError` if path is missing. Different failure class
      than staleness (operator misconfig); fail-loud rather than
      silent-STALE-masking.
    """
    if not target_list_path.exists():
        raise FileNotFoundError(
            f"Target list path does not exist: {target_list_path}"
        )

    # Freshness source: prefer _meta.updated_at (refresh-of-intent),
    # fall back to mtime (always available but fragile to git ops).
    meta_date = _read_meta_updated_at(target_list_path)
    if meta_date is not None:
        last_modified = meta_date
    else:
        mtime_ts = target_list_path.stat().st_mtime
        last_modified = datetime.fromtimestamp(mtime_ts).date()

    days_stale = max(0, (today - last_modified).days)

    if days_stale < WARN_THRESHOLD_DAYS:
        return FreshnessResult(
            status=FreshnessStatus.FRESH,
            days_stale=days_stale,
            last_modified_at=last_modified,
            target_list_path=target_list_path,
        )

    if days_stale < STALE_THRESHOLD_DAYS:
        # WARN band: operator-visible via stderr `click.echo` AND
        # logger.warning. Stderr-via-click ensures the signal lands
        # regardless of root logger config (no basicConfig + lastResort
        # handler edge cases — silent-failure QA convergence).
        msg = (
            f"⚠ Target list {target_list_path} is {days_stale} days old "
            f"(WARN band: {WARN_THRESHOLD_DAYS}-{STALE_THRESHOLD_DAYS}d). "
            f"Consider refreshing _meta.updated_at before next weekly batch."
        )
        click.echo(msg, err=True)
        logger.warning(
            "Target list %s is %d days old (WARN band: %d-%d days).",
            target_list_path, days_stale,
            WARN_THRESHOLD_DAYS, STALE_THRESHOLD_DAYS,
        )
        return FreshnessResult(
            status=FreshnessStatus.WARN,
            days_stale=days_stale,
            last_modified_at=last_modified,
            target_list_path=target_list_path,
        )

    # STALE band: open queue row FIRST, then raise.
    result = FreshnessResult(
        status=FreshnessStatus.STALE,
        days_stale=days_stale,
        last_modified_at=last_modified,
        target_list_path=target_list_path,
    )

    # Wrap escalate() in try/except so an Attio outage doesn't mask
    # the staleness signal (silent-failure I-1 convergence). Log full
    # context on failure, then STILL raise so the batch halts.
    try:
        escalate(
            type="target_list_stale",
            idempotency_key=f"{target_list_path}|{last_modified.isoformat()}",
            payload={
                "target_list_path": str(target_list_path),
                "last_modified_at": last_modified.isoformat(),
                "days_stale": days_stale,
                "freshness_check_at": today.isoformat(),
            },
            attio=attio,
        )
    except Exception:  # noqa: BLE001 — durability propagation boundary
        logger.exception(
            "Failed to write target_list_stale queue row for %s "
            "(days_stale=%d, last_modified_at=%s). The batch will still "
            "halt via WeeklyTargetListStaleError, but the queue row is "
            "missing — operators must triage from terminal output alone.",
            target_list_path, days_stale, last_modified.isoformat(),
        )

    raise WeeklyTargetListStaleError([result])
