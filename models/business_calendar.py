"""Single source of truth for outreach calendar policy.

Outreach never fires on weekends (Sat/Sun) without an explicit
override. This module owns that rule plus the shared business-day math
used by DM and email cadences.
"""

import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from clients.outreach_config import load_outreach_config

# Send-day policy, sourced from config/outreach.yaml (schedule.send_days):
# WHICH days outreach may fire. Default Mon–Fri. This is distinct from the
# fixed Mon–Fri WORKING CALENDAR used by the business-day date math
# (business_days_between / add_business_days / shift_off_weekend /
# advance_off_weekend) — those encode "Sat/Sun are non-working" for cadence
# arithmetic and intentionally do NOT follow send_days, so changing send_days
# (e.g. to add Saturday sends) never silently reshapes nurture-window math.
_OUTREACH = load_outreach_config()

# Default operator timezone (LATAM ICP). Override via OUTBOUND_TZ env
# Cron and CI runners typically default to UTC, which is off by 5 hours
# from local day — important for is_send_day weekend gating and the
# §3.1 quarantine window.
DEFAULT_OPERATOR_TZ = "America/Lima"


def operator_today() -> date:
    """Return today's calendar date in the operator's local timezone.

    Reads ``OUTBOUND_TZ``, falling back to ``America/Lima``. Garbage TZ names
    fall back to UTC with a stderr warning rather than crashing the
    daily run — but the UTC fallback IS observable so misconfiguration
    isn't silent (covers §0 invariant #9).
    """
    tz_name = (
        os.environ.get("OUTBOUND_TZ")
        or DEFAULT_OPERATOR_TZ
    )
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        # Misconfigured TZ — surface to operator and use UTC. UTC vs.
        # configured TZ at midnight boundary is a 5-hour off-by-one
        # risk on §3.1, so a noisy fallback is correct.
        import sys
        print(
            f"WARNING: OUTBOUND_TZ={tz_name!r} not a valid timezone; "
            f"falling back to UTC. This may shift quarantine + send-day "
            f"calendar by up to 5 hours.",
            file=sys.stderr,
        )
        tz = ZoneInfo("UTC")
    return datetime.now(tz).date()


def is_send_day(today: date | None = None) -> bool:
    """True if today is a configured send day (default: Mon-Fri).

    Reads ``schedule.send_days`` from config/outreach.yaml (a frozenset of
    weekday ints). When `today` is None, defers to `operator_today()` so the
    weekend boundary respects ``OUTBOUND_TZ`` instead of the cron
    host's UTC clock.
    """
    return (today or operator_today()).weekday() in _OUTREACH.send_days


# Alias kept for callers that prefer the noun form (e.g., email_sequencer).
is_weekday = is_send_day


def shift_off_weekend(d: date) -> date:
    """Sat -> Fri (1 day back), Sun -> Mon (1 day fwd), weekday unchanged.

    Compresses a weekend date to the nearest weekday. Used by
    `dm_sequencer.earliest_send_date` for the in-memory cadence floor
    (where a 1-day-early DM is acceptable on Fri-vs-Sat collisions).
    See `advance_off_weekend` for the strict forward-only variant used
    by `next_eligible_send_date`.
    """
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def advance_off_weekend(d: date) -> date:
    """Sat -> Mon (+2 days), Sun -> Mon (+1 day), weekday unchanged.

    Forward-only weekend shift. Used by `next_eligible_send_date`
    (PR-12) where the cadence floor stored in Attio must NEVER be
    shortened by a weekend collision — a Sat target moves +2 days, not
    -1, so any DM that reads the stored date sees a strictly safer
    (later-or-equal) eligibility window than the in-memory math.

    Distinct from `shift_off_weekend`, which compresses to the nearest
    weekday and can shift Sat backward to Fri. The two functions
    deliberately diverge:

      * `shift_off_weekend`: in-memory eligibility check; "Friday is
        close enough to Saturday for cadence purposes."
      * `advance_off_weekend`: stored Attio attribute; "the floor we
        commit to MUST NOT regress."
    """
    weekday = d.weekday()
    if weekday == 5:  # Saturday
        return d + timedelta(days=2)
    if weekday == 6:  # Sunday
        return d + timedelta(days=1)
    return d


def business_days_between(start: date, end: date) -> int:
    """Count Mon-Fri working days strictly after start, up to and including end.

    Uses the fixed Mon-Fri working calendar (NOT schedule.send_days) — "business
    day" math is independent of which days the operator chooses to send on.
    """
    if end <= start:
        return 0
    count = 0
    current = start + timedelta(days=1)
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def add_business_days(start: date, days: int) -> date:
    """Add N Mon-Fri working days to a date.

    Uses the fixed Mon-Fri working calendar (NOT schedule.send_days) so cadence
    windows (e.g. nurture_business_days) are stable regardless of the send-day
    policy.
    """
    current = start
    added = 0
    while added < days:
        current += timedelta(days=1)
        if current.weekday() < 5:
            added += 1
    return current
