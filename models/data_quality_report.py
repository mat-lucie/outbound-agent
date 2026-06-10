"""Typed containers for the weekly Data Quality Report.

The DQR is a read-only Attio aggregator: it queries existing typed-state
(Operator Review Queue, Migration Run, Daily Run, LinkedIn Outreach
archaeology) and rolls up 8 metrics. Four of them double as alarms —
three P0 (block downstream DM sends until resolved) and one P1
(visible early-warning signal that co-fires with §10's halt-on-10
backstop per M7).

The DQR does NOT detect violations itself; upstream agents own
detection + open the typed escalation queue rows. The DQR turns "row
exists in the queue" into a visible dashboard + the halt gate
`/sales-daily` consumes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, get_args

if TYPE_CHECKING:
    from collections.abc import Callable

# --- Alarm slug discriminators ------------------------------------

# Slug-vocabulary discriminators kept as `Literal` aliases so:
#   (a) a typo in a downstream consumer fails at type-check time, and
#   (b) the import-time assertion below catches drift against the
#       upstream `ESCALATION_TYPES` vocabulary — the same string MUST
#       appear in both places so an agent opening the row and the
#       dashboard reading the row share one source of truth.

P0AlarmSlug = Literal[
    "cohort_tagging_regression",
    "write_owner_invariant_violated",
    "migration_idempotency_regression",
]
P1AlarmSlug = Literal["manual_reply_classification_gap"]

P0_ALARM_SLUGS: tuple[P0AlarmSlug, ...] = get_args(P0AlarmSlug)
P1_ALARM_SLUGS: tuple[P1AlarmSlug, ...] = get_args(P1AlarmSlug)

# Subset assertion against ESCALATION_TYPES — fails fast at module
# load if either vocabulary drifts. Avoids the failure mode where a
# typo in the DQR slug tuple means the Attio query returns zero rows
# and the alarm silently never fires.
def _assert_slug_subset() -> None:
    from workflows.escalation_schemas import ESCALATION_TYPES_SET

    dqr_slugs = set(P0_ALARM_SLUGS) | set(P1_ALARM_SLUGS)
    missing = dqr_slugs - ESCALATION_TYPES_SET
    if missing:
        raise RuntimeError(
            f"DQR alarm slugs {missing!r} not in ESCALATION_TYPES — "
            f"the dashboard would read but upstream agents could never "
            f"open these queue rows."
        )


_assert_slug_subset()

# P1 alarm threshold synced with §10's halt-on-10 backstop (M7). At
# this count the P1 row fires AND §10 halts — the P1 row exists so the
# operator sees the slug name, not just a hard halt without context.
# Override via OUTBOUND_MANUAL_REPLY_GAP_THRESHOLD.
MANUAL_REPLY_CLASSIFICATION_GAP_THRESHOLD_DEFAULT = 10


def manual_reply_gap_threshold() -> int:
    from models.env import env_int_positive

    return env_int_positive(
        "OUTBOUND_MANUAL_REPLY_GAP_THRESHOLD",
        MANUAL_REPLY_CLASSIFICATION_GAP_THRESHOLD_DEFAULT,
    )


# --- Report containers --------------------------------------------

@dataclass(frozen=True)
class DQRMetrics:
    # P0 alarms
    cohort_tagging_regression_count: int = 0
    write_owner_invariant_violated_count: int = 0
    migration_idempotency_regression_count: int = 0
    # P1 alarm
    manual_reply_classification_gap_count: int = 0
    # Dashboard-only observability
    nurture_silent_skipped_count_7d: int = 0
    nurture_count_parse_errors_7d: int = 0
    pipeline_starvation_open_count: int = 0
    back_pointer_failures_count_7d: int = 0
    legacy_archaeology_pool_count: int = 0


@dataclass
class DQRReport:
    run_id: str
    generated_at: str  # ISO-8601 datetime
    period_start: str  # ISO-8601 date
    period_end: str    # ISO-8601 date
    metrics: DQRMetrics
    p0_alarms_fired: list[str] = field(default_factory=list)
    p1_alarms_fired: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Loud failure on a typo'd slug beats a silent halt on a slug
        # Attio doesn't know.
        bad_p0 = set(self.p0_alarms_fired) - set(P0_ALARM_SLUGS)
        if bad_p0:
            raise ValueError(
                f"p0_alarms_fired contains unknown slug(s): {bad_p0!r}"
            )
        bad_p1 = set(self.p1_alarms_fired) - set(P1_ALARM_SLUGS)
        if bad_p1:
            raise ValueError(
                f"p1_alarms_fired contains unknown slug(s): {bad_p1!r}"
            )

    def has_p0(self) -> bool:
        return bool(self.p0_alarms_fired)

    def has_p1(self) -> bool:
        return bool(self.p1_alarms_fired)

    def exit_severity(self) -> Literal["clean", "p1", "p0"]:
        # P0 dominates P1 — a consumer reading only has_p1() must not
        # be allowed to ship sends while a P0 is open.
        if self.has_p0():
            return "p0"
        if self.has_p1():
            return "p1"
        return "clean"


class DataQualityHalt(RuntimeError):
    """Raised by the downstream consumer (e.g., /sales-daily) when a
    P0 DQR alarm is open at send time. Mirrors the §3.7
    `CostCeilingExhausted` pattern: detection lives upstream; the
    consumer reads the typed queue row and halts.

    The DQR script itself never raises this — it just writes the row
    and exits 70. PR-44's `/sales-daily` skill imports
    `assert_no_open_p0_alarms` and catches this exception to halt DM
    sends with the slug list visible to the operator.
    """

    def __init__(self, alarm_slugs: list[str]) -> None:
        self.alarm_slugs = list(alarm_slugs)
        super().__init__(
            f"P0 data-quality alarm(s) open: {', '.join(alarm_slugs)}. "
            f"Resolve via Attio → Operator Review Queue → filter "
            f"type=<slug> → set status=resolved or dismissed."
        )


# --- Consumer helper ----------------------------------------------

def assert_no_open_p0_alarms(attio_query_fn: Callable[[str], int]) -> None:
    """Halt-gate the daily DM send path on open P0 alarms.

    `attio_query_fn(slug)` returns the count of open queue rows of
    `type=slug`. Injected so callers can mock or share a query cache.

    Raises `DataQualityHalt` with the firing slug list when any P0
    count > 0. Returns None when clean.

    When `attio_query_fn` raises (Attio outage), the exception
    propagates unchanged. Callers MUST NOT catch broadly — a halt
    check that silently treats Attio errors as "no alarms" would
    violate §3.1.
    """
    fired: list[str] = [s for s in P0_ALARM_SLUGS if attio_query_fn(s) > 0]
    if fired:
        raise DataQualityHalt(fired)


# EX_SOFTWARE=70 — P0 halt is a software-detected runtime condition
# that blocks downstream sends. Matches the §3.7 halt convention.
EXIT_OK = 0
EXIT_P1 = 1
EXIT_P0 = 70


def exit_code_for_severity(severity: Literal["clean", "p1", "p0"]) -> int:
    # Dict lookup not if/elif so a future severity tier can't silently
    # fall through to EXIT_OK — KeyError fails loud.
    return {
        "clean": EXIT_OK,
        "p1": EXIT_P1,
        "p0": EXIT_P0,
    }[severity]
