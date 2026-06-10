"""PR-42 — Funnel economics watch + lane unit-economics alarm.

Per-lane unit-economics ceilings (cost-per-positive-response). When a
week's observed cost-per-positive on a lane exceeds the ceiling, the
watcher opens an operator queue row:

  - Single-week breach   → ``unit_economics_alarm_warning`` (low priority,
                            advisory; gives operator a heads-up)
  - Two consecutive weeks → ``unit_economics_alarm`` (P1, halt-eligible)
  - Spend with zero positives → ``unit_economics_alarm_warning``
                            (unmeasurable CPL; spend without conversion
                            is itself a unit-economics breach signal —
                            distinct from PR-17 pool-starvation, which
                            tracks prospect inventory not response rate).

Ceiling defaults are conservative; the operator tunes them via the
``configuration_decision`` queue with ``decision_key="unit_economics_ceiling"``.
The constants here are the runtime fallback used when no operator-set
override has landed yet.

LaneEconomics is the typed surface: callers build one per lane per
week and pass it to ``evaluate_lane_breach`` which decides which (if
any) escalation to open.

SCAFFOLDING NOTE: PR-42 ships the watcher (evaluate_lane_breach +
open_breach_escalation + fetch_prior_week_breached) but does NOT wire
it into the weekly cadence. The follow-up wiring PR adds the call site
in workflows.weekly_finalize that builds LaneEconomics per lane from
the week's send/positive/spend rollup and invokes the watcher. Until
that wiring lands, operators can run the watcher manually via
``cli.py unit-economics-watch`` (or by calling the module directly).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Literal

from workflows.escalation import escalate

if TYPE_CHECKING:
    from clients.attio import AttioClient
    from workflows.cadence import CadenceLane

WRITER_MODULE = "workflows.unit_economics.evaluate_lane_breach"

# Per-lane cost-per-positive ceilings in USD. Defaults per plan §5
# PR-42 (Round-4): enterprise/target/legacy = $400 / $150 / $200.
#
# Counterintuitive ordering note (legacy > target):
#   target lane is the active growth surface (multinational subs,
#   high-touch); the $150 ceiling reflects the tight CPL operators
#   expect on a converting lane. legacy lane is the phasing-out ICP 2
#   pool — its responses are noisier and slower, so a higher per-positive
#   cost is the NATURAL rate, not a signal of misallocation. The $200
#   ceiling permits the natural CPL without firing the alarm on noise
#   while still being tighter than enterprise ($400). If the operator
#   wants to ACCELERATE the phase-out by squeezing legacy spend, the
#   ceiling override path through the configuration_decision queue is
#   the right surface — flipping the default here would couple two
#   independent decisions (the alarm threshold vs the phase-out pace).
LANE_CEILING_USD: dict[CadenceLane, float] = {
    "enterprise": 400.0,
    "target": 150.0,
    "legacy": 200.0,
}

# Two consecutive weeks of breach trips the P1 alarm. One week alone
# trips the low-priority warning. Tunable via the same configuration_decision
# queue (decision_key="unit_economics_ceiling") at the same time as
# the per-lane ceilings.
CONSECUTIVE_BREACH_THRESHOLD: int = 2

AlarmTier = Literal["clear", "warning", "alarm"]


@dataclass(frozen=True)
class LaneEconomics:
    """One lane's observed economics for a single week.

    Fields:
        lane                 : the CadenceLane this row describes.
        week_starting        : ISO Monday of the observed week.
        sends                : number of DMs / emails sent in the week
                               attributed to this lane.
        positives            : number of positive responses received.
        spend_usd            : total spend attributed to this lane in
                               the week (LLM cost + tool cost + paid
                               profile lookups).
        prior_week_breached  : did THIS lane breach the ceiling in the
                               immediately-prior week? Caller computes
                               this by reading the prior LaneEconomics
                               row (cached by week_starting in Attio).
    """
    lane: CadenceLane
    week_starting: date
    sends: int
    positives: int
    spend_usd: float
    prior_week_breached: bool = False

    @property
    def cost_per_positive(self) -> float | None:
        """Spend / positives. Returns None when positives == 0
        (cannot estimate without a denominator)."""
        if self.positives <= 0:
            return None
        return self.spend_usd / self.positives


@dataclass(frozen=True)
class BreachVerdict:
    """Outcome of evaluating a LaneEconomics row against the ceiling.

    Fields:
        tier             : "clear" / "warning" / "alarm"
        lane             : echo of the input lane for downstream routing
        observed_cpl     : the observed cost-per-positive (None when
                           positives == 0 — no breach can fire either)
        ceiling          : the applied per-lane ceiling
        rationale        : human-readable summary the operator reads
    """
    tier: AlarmTier
    lane: CadenceLane
    observed_cpl: float | None
    ceiling: float
    rationale: str


def evaluate_lane_breach(
    econ: LaneEconomics,
    *,
    ceiling_override: float | None = None,
) -> BreachVerdict:
    """Decide whether ``econ`` breaches the ceiling and at what tier.

    Tier logic:
        - When ``positives == 0`` AND ``spend_usd > 0``: ``warning`` —
          unmeasurable CPL but spend without conversion is itself a
          unit-economics breach signal (we burned money without learning
          anything). Distinct from PR-17 starvation (which tracks
          prospect-pool inventory, not response rates).
        - When ``positives == 0`` AND ``spend_usd == 0``: ``clear`` —
          no activity at all in this lane this week (legitimate idle).
        - When CPL <= ceiling: ``clear``.
        - When CPL > ceiling AND this is the second consecutive breach
          (``prior_week_breached=True``): ``alarm`` (P1, halt-eligible).
        - When CPL > ceiling AND ``prior_week_breached=False``: ``warning``
          (low priority, advisory).

    Args:
        econ: the week's observed economics.
        ceiling_override: optional ceiling injection — when set, used
            INSTEAD of LANE_CEILING_USD[econ.lane]. The configuration_decision
            queue surfaces operator-tuned ceilings; the caller fetches
            the live value and passes it here.
    """
    ceiling = ceiling_override if ceiling_override is not None else LANE_CEILING_USD[econ.lane]
    cpl = econ.cost_per_positive

    if cpl is None:
        # positives == 0. Distinguish "spent money + got nothing" from
        # "idle lane" — the former is itself a unit-economics breach.
        if econ.spend_usd > 0:
            return BreachVerdict(
                tier="warning",
                lane=econ.lane,
                observed_cpl=None,
                ceiling=ceiling,
                rationale=(
                    f"Spent ${econ.spend_usd:.2f} on lane {econ.lane!r} "
                    f"in week {econ.week_starting.isoformat()} with ZERO "
                    "positives — unmeasurable CPL, but spend-without-"
                    "conversion is a unit-economics breach. Advisory."
                ),
            )
        return BreachVerdict(
            tier="clear",
            lane=econ.lane,
            observed_cpl=None,
            ceiling=ceiling,
            rationale=(
                f"No activity on lane {econ.lane!r} in week "
                f"{econ.week_starting.isoformat()} (spend=$0, positives=0); "
                "idle lane — no breach signal."
            ),
        )

    if cpl <= ceiling:
        return BreachVerdict(
            tier="clear",
            lane=econ.lane,
            observed_cpl=cpl,
            ceiling=ceiling,
            rationale=(
                f"CPL=${cpl:.2f} <= ceiling=${ceiling:.2f} on lane "
                f"{econ.lane!r}, week {econ.week_starting.isoformat()}."
            ),
        )

    # Above ceiling.
    if econ.prior_week_breached:
        return BreachVerdict(
            tier="alarm",
            lane=econ.lane,
            observed_cpl=cpl,
            ceiling=ceiling,
            rationale=(
                f"CPL=${cpl:.2f} > ${ceiling:.2f} for "
                f"{CONSECUTIVE_BREACH_THRESHOLD} consecutive weeks on lane "
                f"{econ.lane!r}; P1 alarm — halt-eligible."
            ),
        )

    return BreachVerdict(
        tier="warning",
        lane=econ.lane,
        observed_cpl=cpl,
        ceiling=ceiling,
        rationale=(
            f"CPL=${cpl:.2f} > ${ceiling:.2f} on lane {econ.lane!r}, "
            f"week {econ.week_starting.isoformat()} — single-week breach "
            "(advisory; alarm at next consecutive breach)."
        ),
    )


def fetch_prior_week_breached(
    attio: AttioClient,
    *,
    lane: CadenceLane,
    this_week_starting: date,
) -> bool:
    """Return True if either an alarm or warning row exists for ``lane``
    on the immediately-preceding week's Monday.

    Looks up the Operator Review Queue for ``unit_economics_alarm`` OR
    ``unit_economics_alarm_warning`` rows whose ``idempotency_key``
    matches ``warning|{lane}|{prior_monday}`` or ``alarm|{lane}|{prior_monday}``.
    Either-tier hit counts as "breached" for the consecutive-breach gate.

    Lifts the prior-week-breached signal out of the caller's hands —
    a wrong manual flag would either suppress a real alarm or trip a
    spurious one. Fetching from Attio means the source of truth is the
    persisted queue row, not a caller-passed boolean.

    Returns False when no prior-week row is found (treat absence as
    "did not breach" — the alarm gate fires on a fresh first breach).
    """
    from datetime import timedelta as _td

    prior_monday = this_week_starting - _td(days=7)
    keys = [
        f"warning|{lane}|{prior_monday.isoformat()}",
        f"alarm|{lane}|{prior_monday.isoformat()}",
    ]
    for key in keys:
        body = {"filter": {"idempotency_key": {"$eq": key}}, "limit": 1}
        data = attio._request(
            "POST", "/objects/operator_review_queue/records/query", json=body,
        )
        if data.get("data"):
            return True
    return False


def _idempotency_key_for(verdict: BreachVerdict, *, week_starting: date) -> str:
    """Build the escalation idempotency key for ``verdict``.

    Pinned to (tier, lane, week_starting) so re-runs within the same week
    are no-ops, but a fresh row opens at the next week boundary.
    """
    return f"{verdict.tier}|{verdict.lane}|{week_starting.isoformat()}"


def open_breach_escalation(
    verdict: BreachVerdict,
    *,
    econ: LaneEconomics,
    attio: AttioClient | None = None,
) -> dict | None:
    """Open the appropriate operator queue row for the verdict tier.

    Returns the queue row dict on warning/alarm, None on clear. The
    escalate() idempotency contract handles re-runs (same key → existing
    row returned, no duplicate).

    Warning-supersede-on-alarm: when an alarm row is opened for a
    (lane, week_starting), any existing warning row for the same
    (lane, week_starting) is marked as ``superseded_by_alarm`` in its
    rationale via the auto-resolve note. Operators triaging the queue
    see one current row per breach event instead of two.
    """
    if verdict.tier == "clear":
        return None

    type_slug = (
        "unit_economics_alarm" if verdict.tier == "alarm"
        else "unit_economics_alarm_warning"
    )

    payload = {
        "lane": verdict.lane,
        "week_starting": econ.week_starting.isoformat(),
        "observed_cost_per_positive": verdict.observed_cpl,
        "ceiling_usd": verdict.ceiling,
        "sends": econ.sends,
        "positives": econ.positives,
        "spend_usd": econ.spend_usd,
        "rationale": verdict.rationale,
        "prior_week_breached": econ.prior_week_breached,
        "opened_by": WRITER_MODULE,
    }

    row = escalate(
        type=type_slug,
        idempotency_key=_idempotency_key_for(verdict, week_starting=econ.week_starting),
        payload=payload,
        attio=attio,
    )

    # Auto-mark any warning row for the same (lane, week) as
    # superseded — alarm overrides warning. The warning slug + key are
    # distinct from the alarm so the operator sees both rows in the
    # queue UI; the superseded note tells them which one is current.
    if verdict.tier == "alarm" and attio is not None:
        _mark_warning_superseded(
            attio,
            lane=verdict.lane,
            week_starting=econ.week_starting,
        )

    return row


def _mark_warning_superseded(
    attio: AttioClient,
    *,
    lane: CadenceLane,
    week_starting: date,
) -> None:
    """Best-effort note on a warning row that an alarm has superseded it.

    Read-only lookup followed by a single PATCH on the rationale field;
    failures here do NOT block the alarm itself (the alarm escalation
    already wrote successfully). Logs to stderr instead of raising.
    """
    import sys

    warning_key = f"warning|{lane}|{week_starting.isoformat()}"
    try:
        data = attio._request(
            "POST",
            "/objects/operator_review_queue/records/query",
            json={"filter": {"idempotency_key": {"$eq": warning_key}}, "limit": 1},
        )
        rows = data.get("data", [])
        if not rows:
            return
        row_id = (rows[0].get("id") or {}).get("record_id", "")
        if not row_id:
            return
        attio._request(
            "PATCH",
            f"/objects/operator_review_queue/records/{row_id}",
            json={"data": {"values": {
                "superseded_by_alarm": True,
                "superseded_at": date.today().isoformat(),
            }}},
        )
    except Exception as exc:
        sys.stderr.write(
            f"WARN: mark_warning_superseded failed for {warning_key!r}: "
            f"{exc!s}. Operator will see both warning + alarm rows.\n"
        )
