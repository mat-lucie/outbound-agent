"""Pipeline-starvation evaluator.

Three triggers detect failure modes where the invite pipeline silently
runs dry:

  1. **low_prospects** — invite-eligible PROSPECT pool below a floor.
     Weekly prospecting didn't run, or ran with an exhausted target
     list.
  2. **stale_weekly** — no new prospects committed in more than N
     business days. Weekly cadence has broken.
  3. **short_runway** — at the current daily invite rate, the pool
     exhausts in fewer than M business days. Early-warning band.

Each trigger opens a typed `pipeline_starvation` Operator Review Queue
row (one per trigger per day; idempotency keys make re-runs no-ops).
On Attio failure the function raises — a silent "no starvation" would
mask the real signal (§0 invariant #9).

False-alarm guard: `stale_weekly` waits
`OUTBOUND_STARVATION_MIN_BDAYS_SINCE_COMMIT` business days (default 5)
before firing, so a single missed weekly run doesn't page the operator.
"""
from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

from models.business_calendar import business_days_between
from models.env import env_int_positive
from models.pipeline import PipelineStage, is_invite_eligible, is_send_eligible
from workflows.escalation import escalate

if TYPE_CHECKING:
    from collections.abc import Callable

    from clients.attio import AttioClient

__all__ = ["evaluate_pipeline_starvation"]

# Floors. Override via env for ops tuning without code changes.
STARVATION_LOW_PROSPECTS_FLOOR_DEFAULT = 10
STARVATION_SHORT_RUNWAY_BDAYS_DEFAULT = 3
STARVATION_DAILY_INVITE_RATE_DEFAULT = 15  # matches daily_check.batch_size
STARVATION_STALE_WEEKLY_BDAYS_DEFAULT = 7
STARVATION_MIN_BDAYS_SINCE_COMMIT_DEFAULT = 5


def _list_outreach_entries(
    attio: AttioClient, attio_query: Callable[[AttioClient], list[dict]] | None,
) -> list[dict]:
    """Fetch all LinkedIn Outreach entries via the parser helper.

    Indirected through a callable so tests can pass a fixture without
    standing up a fake AttioClient just to flow through parse_entry.
    The production code path imports the helper lazily to avoid a
    workflows-circular-import hazard on cli.py startup.
    """
    if attio_query is not None:
        return attio_query(attio)
    from workflows.daily_check_helpers import _get_all_entries_parsed
    return _get_all_entries_parsed(attio)


def _pool_metrics(entries: list[dict], today: date) -> dict[str, Any]:
    """Compute the three pool metrics + the most recent commit date.

    Returns a dict with:
      - `invite_eligible_pool`: int — PROSPECT entries with score >= 60
        that have cleared the quarantine window.
      - `quarantined_pool`: int — PROSPECT entries with score >= 60 that
        are still in quarantine.
      - `most_recent_commit`: date | None — max prospect_committed_at
        truncated to date. None when no entry carries the attribute
        (legacy pipeline, backfill not yet run).
    """
    invite_eligible = 0
    quarantined = 0
    most_recent: date | None = None
    for attrs in entries:
        if attrs.get("stage") != PipelineStage.PROSPECT.value:
            # Track most-recent commit across ALL stages so a healthy
            # cadence is recognized even when prospects have already
            # progressed past PROSPECT.
            committed_at = attrs.get("prospect_committed_at")
            if committed_at:
                try:
                    d = date.fromisoformat(str(committed_at)[:10])
                except (TypeError, ValueError):
                    continue
                if most_recent is None or d > most_recent:
                    most_recent = d
            continue
        if not is_send_eligible(attrs):
            continue
        score = attrs.get("quality_score")
        if score is None or int(score) < 60:
            continue
        committed_at = attrs.get("prospect_committed_at")
        if committed_at:
            parsed_d: date | None
            try:
                parsed_d = date.fromisoformat(str(committed_at)[:10])
            except (TypeError, ValueError):
                parsed_d = None
            if parsed_d is not None and (
                most_recent is None or parsed_d > most_recent
            ):
                most_recent = parsed_d
        if is_invite_eligible(attrs, today):
            invite_eligible += 1
        else:
            quarantined += 1
    return {
        "invite_eligible_pool": invite_eligible,
        "quarantined_pool": quarantined,
        "most_recent_commit": most_recent,
    }


def evaluate_pipeline_starvation(
    attio: AttioClient,
    today: date,
    *,
    attio_query: Callable[[AttioClient], list[dict]] | None = None,
) -> dict[str, Any]:
    """Evaluate the three starvation triggers and open queue rows.

    Args:
        attio: AttioClient (or fake).
        today: Calendar date. Caller-supplied so tests can pin time and
            `/sales-daily` can re-use its TZ-aware today (PR-45's
            `is_send_day` work).
        attio_query: Optional fixture hook returning the parsed list of
            entries. Production callers leave this None.

    Returns:
        {
            'invite_eligible_pool': int,
            'quarantined_pool': int,
            'most_recent_commit': date|None,
            'bdays_since_commit': int|None,
            'runway_bdays_remaining': float|None,
            'triggers_fired': list[str],   # subset of
                ('low_prospects','stale_weekly','short_runway')
            'queue_rows_opened': list[str], # idempotency keys
        }

    Per §0 invariant #9: Attio query failures propagate as exceptions
    (silent-no-starvation would mask the real problem). The
    `low_prospects` and `short_runway` triggers ALWAYS evaluate; the
    `stale_weekly` trigger requires
    `bdays_since_commit >= OUTBOUND_STARVATION_MIN_BDAYS_SINCE_COMMIT`
    (default 5) before firing — false-alarm guard.
    """
    low_floor = env_int_positive(
        "OUTBOUND_STARVATION_LOW_PROSPECTS_FLOOR",
        STARVATION_LOW_PROSPECTS_FLOOR_DEFAULT,
    )
    runway_floor = env_int_positive(
        "OUTBOUND_STARVATION_SHORT_RUNWAY_BDAYS",
        STARVATION_SHORT_RUNWAY_BDAYS_DEFAULT,
    )
    daily_rate = env_int_positive(
        "OUTBOUND_STARVATION_DAILY_INVITE_RATE",
        STARVATION_DAILY_INVITE_RATE_DEFAULT,
    )
    stale_floor = env_int_positive(
        "OUTBOUND_STARVATION_STALE_WEEKLY_BDAYS",
        STARVATION_STALE_WEEKLY_BDAYS_DEFAULT,
    )
    min_bdays_for_alarm = env_int_positive(
        "OUTBOUND_STARVATION_MIN_BDAYS_SINCE_COMMIT",
        STARVATION_MIN_BDAYS_SINCE_COMMIT_DEFAULT,
    )

    entries = _list_outreach_entries(attio, attio_query)
    metrics = _pool_metrics(entries, today)
    invite_eligible_pool: int = metrics["invite_eligible_pool"]
    quarantined_pool: int = metrics["quarantined_pool"]
    most_recent_commit: date | None = metrics["most_recent_commit"]

    bdays_since_commit: int | None = (
        None if most_recent_commit is None
        else business_days_between(most_recent_commit, today)
    )

    runway_bdays_remaining: float | None = (
        None if daily_rate <= 0 else invite_eligible_pool / daily_rate
    )

    triggers_fired: list[str] = []
    queue_rows_opened: list[str] = []

    iso_today = today.isoformat()
    most_recent_iso = (
        most_recent_commit.isoformat() if most_recent_commit else None
    )

    if invite_eligible_pool < low_floor:
        triggers_fired.append("low_prospects")
        key = f"low_prospects|{iso_today}"
        escalate(
            type="pipeline_starvation",
            idempotency_key=key,
            payload={
                "trigger": "low_prospects",
                "today": iso_today,
                "invite_eligible_pool": invite_eligible_pool,
                "low_floor": low_floor,
                "quarantined_pool": quarantined_pool,
                "most_recent_commit": most_recent_iso,
            },
            attio=attio,
        )
        queue_rows_opened.append(key)

    # stale_weekly: false-alarm guard requires bdays_since_commit to
    # cross BOTH the stale floor AND min_bdays_for_alarm — prevents
    # alarm fatigue when the operator simply hasn't run `/sales-weekly`
    # yet this week.
    if (
        bdays_since_commit is not None
        and bdays_since_commit >= stale_floor
        and bdays_since_commit >= min_bdays_for_alarm
    ):
        triggers_fired.append("stale_weekly")
        key = f"stale_weekly|{iso_today}"
        escalate(
            type="pipeline_starvation",
            idempotency_key=key,
            payload={
                "trigger": "stale_weekly",
                "today": iso_today,
                "invite_eligible_pool": invite_eligible_pool,
                "most_recent_commit": most_recent_iso,
                "bdays_since_commit": bdays_since_commit,
                "stale_floor": stale_floor,
            },
            attio=attio,
        )
        queue_rows_opened.append(key)

    if (
        runway_bdays_remaining is not None
        and runway_bdays_remaining < runway_floor
        and invite_eligible_pool > 0
    ):
        # When invite_eligible_pool == 0 the runway is 0 days, but
        # low_prospects already covers that case (with strictly more
        # context). short_runway is for the "you have invites left
        # today but tomorrow you won't" early warning band.
        triggers_fired.append("short_runway")
        key = f"short_runway|{iso_today}"
        escalate(
            type="pipeline_starvation",
            idempotency_key=key,
            payload={
                "trigger": "short_runway",
                "today": iso_today,
                "invite_eligible_pool": invite_eligible_pool,
                "daily_rate": daily_rate,
                "runway_bdays_remaining": round(runway_bdays_remaining, 2),
                "runway_floor": runway_floor,
            },
            attio=attio,
        )
        queue_rows_opened.append(key)

    return {
        "invite_eligible_pool": invite_eligible_pool,
        "quarantined_pool": quarantined_pool,
        "most_recent_commit": most_recent_commit,
        "bdays_since_commit": bdays_since_commit,
        "runway_bdays_remaining": runway_bdays_remaining,
        "triggers_fired": triggers_fired,
        "queue_rows_opened": queue_rows_opened,
    }
