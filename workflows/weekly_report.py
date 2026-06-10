"""Weekly KPI report: compute metrics from Attio pipeline, email via Resend.

PR-30 / §0 #9 / QA-v1 D.PR-30 / Round-4 D11 (B-SW-REPORT + B-SW-DEAL-STALE
+ B-GW-DASHBOARD) extends this with:
  * `KPISnapshot` TypedDict — typed contract for the snapshot
  * `compute_persona_funnels` — PersonaFunnel[] table top-of-fold
  * `DealStalenessRule` — configurable staleness flag per active deal
  * `compute_prior_period_compare` — week-over-week delta from the prior
    weekly KPI snapshot file
  * Weekly KPI Snapshot filesystem sidecar — agent-readable record per run

CLI defaults to `--dry-run` per scope-doc; explicit `--send` required to
email. The sidecar JSON file is written on EVERY run regardless of
`--send` (durability: subagents read historical reports by
`week_starting` filename).

Filesystem sidecar (Wave-2-A — the `weekly_kpi_snapshot` Attio object was
never deployed and under the minimize-objects decision this lightweight,
regenerable, JOIN-free weekly KPI store lives on disk instead of Attio):
  * Data home: `reports/weekly-kpi/<week_starting>.json` at repo root
    (filename = ISO Monday `week_starting`). Committed to the repo as a
    durable week-over-week audit trail (sibling to `docs/experiments/`).
  1. BEFORE email send: atomically write the full record to
     `<week_starting>.json`. Same-week re-runs overwrite in place
     (idempotent by deterministic path — no record-id lookup needed;
     the operator re-running after fixing a data issue sees the
     corrected numbers in the same file). The atomic temp-file +
     os.replace guards against a torn read if `cli report` double-fires
     (no run-lock).
  2. AFTER successful Resend send: read-modify-write the file to capture
     `report_resend_message_id` so operator-side triage can correlate to
     a specific email.

A missing/absent prior-week file is the expected cold/gap case and
returns None silently. A genuine OS write error propagates (loud) —
losing the canonical sidecar artifact is not a swallow-and-continue case.

Resend delivery failure opens a typed `resend_delivery_failed`
Operator Review Queue row with the kpi_snapshot_week_starting back-pointer.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypedDict, cast

import click

from clients.attio import AttioClient
from clients.resend_client import ResendClient  # noqa: TC001 — runtime type for `resend` param
from models.pipeline import DealStage, PipelineStage, dm_step_int
from workflows.escalation import escalate

if TYPE_CHECKING:
    from clients.crm.base import CRMProvider

StaleStatus = Literal["stale", "fresh", "unknown"]

logger = logging.getLogger(__name__)

# KPI targets from campaign doc
TARGETS: dict[str, float | tuple[float, float]] = {
    "connection_acceptance_rate": (0.25, 0.35),  # 25-35%
    "response_rate": (0.10, 0.15),               # 10-15%
    "positive_response_rate": (0.05, 0.08),       # 5-8%
    "calls_booked_per_month": (3, 5),
    "acv": 135_000,                               # $135K per qualified prospect
}

# PR-30: 7-day measurement window per scope-doc.
_REPORT_WINDOW_DAYS = 7

# Wave-2-A: filesystem home for the weekly KPI snapshot sidecar.
# `reports/weekly-kpi/<week_starting>.json` at repo root. Committed to the
# repo as a durable week-over-week audit trail. Env-overridable via
# `WEEKLY_KPI_DIR` (mainly for tests + non-default deploy layouts).
REPORTS_DIR = Path(
    os.environ.get(
        "WEEKLY_KPI_DIR",
        str(Path(__file__).resolve().parent.parent / "reports" / "weekly-kpi"),
    )
)

# PR-30: deal-staleness default threshold. A deal in IN_PROGRESS with
# no activity in this many days surfaces as `stale=True` in
# `compute_active_deals` so the report's deal section can highlight
# pipeline-rot risk. Configurable via `OUTBOUND_DEAL_STALENESS_DAYS` env
# (operator can change without a code deploy).
DEAL_STALENESS_DAYS_DEFAULT = 21


@dataclass(frozen=True)
class DealStalenessRule:
    """Configurable rule for flagging stale active deals.

    `days_threshold` — a deal whose most-recent activity is older than
    this many days gets `stale_status="stale"` in the report.
    Defaults to `DEAL_STALENESS_DAYS_DEFAULT` (21d); operator can
    override via ``OUTBOUND_DEAL_STALENESS_DAYS`` env var.
    """
    days_threshold: int = DEAL_STALENESS_DAYS_DEFAULT

    @classmethod
    def from_env(cls) -> DealStalenessRule:
        """Build a rule from env, returning default on missing/invalid.

        Provenance surfaces via `from_env_with_source` for the
        snapshot (silent-failure QA convergence — operators need to
        distinguish "I set 21" from "I set garbage and we silently
        used 21").
        """
        rule, _source = cls.from_env_with_source()
        return rule

    @classmethod
    def from_env_with_source(
        cls,
    ) -> tuple[DealStalenessRule, Literal["default", "env", "env_invalid_fell_back_to_default"]]:
        """Build the rule AND return the provenance tag.

        Surfaced on `KPISnapshot.stale_threshold_source` so a
        misconfigured env doesn't silently shape the report numbers.
        """
        raw = (
            os.environ.get("OUTBOUND_DEAL_STALENESS_DAYS")
            or ""
        ).strip()
        if not raw:
            return cls(), "default"
        try:
            return cls(days_threshold=int(raw)), "env"
        except ValueError:
            logger.warning(
                "Invalid OUTBOUND_DEAL_STALENESS_DAYS=%r; using default %d",
                raw, DEAL_STALENESS_DAYS_DEFAULT,
            )
            return cls(), "env_invalid_fell_back_to_default"

    def evaluate(self, last_activity_at: date | None, today: date) -> StaleStatus:
        """Return Literal `"stale"` / `"fresh"` / `"unknown"` for a deal.

        Type-design QA convergence: `Literal` return so callers branching
        on the string get mypy-checked typo protection.
        """
        if last_activity_at is None:
            return "unknown"
        days_idle = (today - last_activity_at).days
        return "stale" if days_idle >= self.days_threshold else "fresh"


class PersonaFunnelDict(TypedDict):
    """Serialized shape of `PersonaFunnel.to_dict()` (type-design QA
    convergence — keeps the in-process dataclass and the JSON shape
    structurally aligned so a downstream subagent reading
    `persona_funnels_json` from Attio gets a known field set).
    """
    persona: str
    invited: int
    accepted: int
    dm1_sent: int
    dm2_sent: int
    dm3_sent: int
    responded: int
    qualified: int
    acceptance_rate: float
    response_rate: float


@dataclass(frozen=True)
class PersonaFunnel:
    """Per-persona funnel row for the top-of-fold table.

    The funnel is CUMULATIVE: a prospect at DM3_SENT is counted in
    `dm1_sent`, `dm2_sent`, AND `dm3_sent`. `acceptance_rate` is
    `accepted / invited` (unique-prospect rate). `response_rate` is
    `responded / (dm1_sent + dm2_sent + dm3_sent)` — volume-weighted
    by DM messages sent, NOT per unique prospect. The cumulative-funnel
    model means a single DM3-stage prospect contributes 3 DMs to the
    denominator, so the rate is "responses per DM message sent" rather
    than "responses per unique prospect DM'd." This semantic is the
    weekly-report convention (math-QA-build30 clarification).

    `__post_init__` (type-design QA convergence) enforces the
    monotone-non-increasing cadence invariant: a prospect at any DM
    step was already counted upstream, so the chain
    `invited >= accepted >= dm1_sent >= dm2_sent >= dm3_sent >= responded
    >= qualified` must hold. A future refactor that breaks the
    cumulative-funnel logic raises ValueError at the construction site
    instead of producing a silently-wrong funnel.

    `Decimal` would be over-engineering for these presentation-layer
    ratios; `float` is fine for the percent rendered in the HTML email
    and the JSON sidecar.
    """
    persona: str
    invited: int
    accepted: int
    dm1_sent: int
    dm2_sent: int
    dm3_sent: int
    responded: int
    qualified: int

    def __post_init__(self) -> None:
        chain = [
            ("invited", self.invited),
            ("accepted", self.accepted),
            ("dm1_sent", self.dm1_sent),
            ("dm2_sent", self.dm2_sent),
            ("dm3_sent", self.dm3_sent),
            ("responded", self.responded),
            ("qualified", self.qualified),
        ]
        prev_name, prev_val = chain[0]
        for name, val in chain[1:]:
            if val > prev_val:
                raise ValueError(
                    f"PersonaFunnel cadence chain violation: {name}={val} "
                    f"> {prev_name}={prev_val} (cumulative funnel must be "
                    f"monotone non-increasing)"
                )
            prev_name, prev_val = name, val

    @property
    def acceptance_rate(self) -> float:
        return (self.accepted / self.invited) if self.invited > 0 else 0.0

    @property
    def response_rate(self) -> float:
        dm_total = self.dm1_sent + self.dm2_sent + self.dm3_sent
        return (self.responded / dm_total) if dm_total > 0 else 0.0

    def to_dict(self) -> PersonaFunnelDict:
        return {
            "persona": self.persona,
            "invited": self.invited,
            "accepted": self.accepted,
            "dm1_sent": self.dm1_sent,
            "dm2_sent": self.dm2_sent,
            "dm3_sent": self.dm3_sent,
            "responded": self.responded,
            "qualified": self.qualified,
            "acceptance_rate": self.acceptance_rate,
            "response_rate": self.response_rate,
        }


class PriorPeriodDelta(TypedDict):
    """Per-key compare against the prior week's snapshot (type-design
    QA convergence — tightens the inner shape of
    `KPISnapshot.prior_period_compare`).
    """
    prior: float
    current: float
    delta: float


class KPISnapshot(TypedDict):
    """Typed contract for the weekly KPI snapshot (PR-30 / §0 #9 /
    QA-v1 D.PR-30 / Round-4 D11).

    `window` — ISO-format `"YYYY-MM-DD..YYYY-MM-DD"` for the 7-day
    measurement window (today-6d through today).

    `measurement_basis` — describes what the metrics are measuring.
    Today this is the window string; Wave 1D cohort math will extend
    with `"cohort_v1"` etc. as the substrate ships.

    `prior_period_compare` — optional dict of per-key
    `PriorPeriodDelta` against the previous week's snapshot. None on:
    (a) the first-ever report (no prior snapshot), (b) prior-snapshot
    load failure (best-effort lookup logs but does not raise), (c)
    when the prior snapshot lacks any numeric totals comparable to
    the current run (no key match → empty compare collapses to None).
    `delta = current - prior`; positive means improvement.

    `stale_threshold_source` — provenance for the deal-staleness
    threshold (silent-failure-hunter convergence on §0 #9). Value is
    `"default"`, `"env"`, or `"env_invalid_fell_back_to_default"`.
    Consumers can detect a misconfigured env var without grepping logs.

    `notes` — free-text operator observations recorded with the
    snapshot. Empty string when unused.

    `outreach_volume` — invite/DM/visit send volume summed from the
    CRM daily_run ledger over the report window (see
    `compute_outreach_volume`). Carries a `note` caveat when the window
    includes days where counters were not yet tracked.
    """
    window: str
    measurement_basis: str
    prior_period_compare: dict[str, PriorPeriodDelta] | None
    week_starting: str
    totals: dict[str, float]
    stage_counts: dict[str, int]
    persona_funnels: list[PersonaFunnelDict]
    active_deals: dict[str, Any]
    stale_threshold_source: Literal["default", "env", "env_invalid_fell_back_to_default"]
    notes: str
    outreach_volume: dict[str, Any]


def _count_by_stage(entries: list[dict]) -> dict[str, int]:
    """Count entries grouped by pipeline stage."""
    counts: dict[str, int] = {}
    for entry in entries:
        stage = AttioClient.parse_entry(entry)["stage"] or "Unknown"
        counts[stage] = counts.get(stage, 0) + 1
    return counts


def _count_unreachable_dmed(entries: list[dict]) -> int:
    """Count UNREACHABLE entries that received at least DM1 (dm_step>=1).

    Mirrors learn.py's `_counts_as_dmed` UNREACHABLE guard: an UNREACHABLE row
    keeps its dm_step when parked, so dm_step>=1 means it genuinely received a
    DM and belongs in the DM'd / past-connection denominators; a dm_step that
    resolves to 0 (dm0/connection_note/invite/None — an OON invite never
    accepted, or InMail-required on DM1) never received one and is excluded.
    `dm_step` is a select-type slug, so it is coerced via the shared
    `dm_step_int` (a raw `slug >= 1` would TypeError). Counted from entries
    directly because the dm_step guard needs per-row access, not the stage
    totals. Never feeds a response numerator — UNREACHABLE is an
    undeliverability terminal, not a reply.
    """
    n = 0
    for entry in entries:
        parsed = AttioClient.parse_entry(entry)
        if parsed["stage"] != PipelineStage.UNREACHABLE.value:
            continue
        if dm_step_int(parsed.get("dm_step")) >= 1:
            n += 1
    return n


def compute_kpis(entries: list[dict]) -> dict:
    """Compute KPI metrics from pipeline entries."""
    counts = _count_by_stage(entries)
    total = sum(counts.values())

    connection_sent = counts.get(PipelineStage.CONNECTION_SENT.value, 0)
    accepted = counts.get(PipelineStage.ACCEPTED.value, 0)
    dm1 = counts.get(PipelineStage.DM1_SENT.value, 0)
    dm2 = counts.get(PipelineStage.DM2_SENT.value, 0)
    dm3 = counts.get(PipelineStage.DM3_SENT.value, 0)
    responded = counts.get(PipelineStage.RESPONDED.value, 0)
    call_booked = counts.get(PipelineStage.CALL_BOOKED.value, 0)
    qualified = counts.get(PipelineStage.QUALIFIED.value, 0)
    not_interested = counts.get(PipelineStage.NOT_INTERESTED.value, 0)

    # Wave-2-A: UNREACHABLE rows that received >=1 DM before being parked
    # genuinely earned a place in the DM'd + past-connection denominators
    # (a prospect can only be DM'd after accepting, so they're also past
    # Connection Sent). Omitting them dropped delivered-to non-responders and
    # inflated both response_rate and acceptance_rate. dm_step=0/None UNREACHABLE
    # rows are excluded (never delivered). UNREACHABLE never enters a response
    # NUMERATOR — it is an undeliverability terminal, not a reply. stage_counts
    # still surfaces the raw bucket; only these computed rates are adjusted.
    unreachable_dmed = _count_unreachable_dmed(entries)

    total_connections = connection_sent + accepted + dm1 + dm2 + dm3 + responded + call_booked + qualified + not_interested + unreachable_dmed
    # Fix-3 (audit): NOT_INTERESTED prospects received DMs and declined — they are
    # legitimate non-responders and belong in the response-rate denominator.
    # Mirrors learn.py _DMED_STAGES which includes NOT_INTERESTED.
    total_dms = dm1 + dm2 + dm3 + responded + call_booked + qualified + not_interested + unreachable_dmed

    # For acceptance rate, "accepted" includes everyone who moved past Connection Sent
    total_past_connection = accepted + dm1 + dm2 + dm3 + responded + call_booked + qualified + not_interested + unreachable_dmed
    acceptance_rate = total_past_connection / total_connections if total_connections > 0 else 0

    response_rate = (responded + call_booked + qualified) / total_dms if total_dms > 0 else 0
    pipeline_value = qualified * TARGETS["acv"]

    return {
        "total_prospects": total,
        "stage_counts": counts,
        "connection_acceptance_rate": round(acceptance_rate * 100, 1),
        "response_rate": round(response_rate * 100, 1),
        "responded": responded,
        "calls_booked": call_booked,
        "qualified": qualified,
        "not_interested": not_interested,
        "pipeline_value": pipeline_value,
    }


def compute_active_deals(
    deals: list[dict],
    *,
    today: date | None = None,
    staleness_rule: DealStalenessRule | None = None,
) -> dict:
    """Aggregate parsed Attio deal records by stage.

    Each deal in `deals` is the output of AttioClient.parse_deal().
    For In-Progress deals with no `value` set, falls back to TARGETS["acv"]
    so the headline pipeline number stays useful while the data is being
    backfilled. The fallback is flagged per-deal via `value_is_fallback`.

    PR-30: applies `DealStalenessRule` per-deal — In-Progress deals with
    no activity in `days_threshold` (default 21d, env-overridable)
    surface as `stale_status="stale"` so the report's deal section can
    highlight pipeline-rot risk. `today` defaults to `date.today()`.
    """
    # TARGETS is heterogeneous (scalars + range tuples); "acv" is always a
    # scalar — cast so float() below sees a number, not the union.
    acv = cast("float", TARGETS["acv"])
    if today is None:
        today = date.today()
    if staleness_rule is None:
        staleness_rule = DealStalenessRule.from_env()
    in_progress: list[dict] = []
    in_progress_value = 0.0
    blank_value_count = 0
    stale_count = 0
    unknown_count = 0
    deals_by_stage: dict[str, int] = {}

    for d in deals:
        stage = d.get("stage") or "Unknown"
        deals_by_stage[stage] = deals_by_stage.get(stage, 0) + 1

        if stage != DealStage.IN_PROGRESS.value:
            continue

        explicit = d.get("value")
        if explicit is None:
            blank_value_count += 1
            value = float(acv)
            is_fallback = True
        else:
            value = float(explicit)
            is_fallback = False
        in_progress_value += value

        # Staleness: prefer explicit last_activity_at, fall back to
        # updated_at, then created_at. Unparseable → unknown.
        last_activity = _parse_deal_activity(d)
        stale_status = staleness_rule.evaluate(last_activity, today)
        if stale_status == "stale":
            stale_count += 1
        elif stale_status == "unknown":
            unknown_count += 1

        in_progress.append({
            "name": d.get("name") or "(unnamed)",
            "value": value,
            "currency": d.get("currency") or "USD",
            "country": d.get("country"),
            "value_is_fallback": is_fallback,
            "stale_status": stale_status,
            "last_activity_at": last_activity.isoformat() if last_activity else None,
        })

    in_progress.sort(key=lambda r: r["value"], reverse=True)

    return {
        "in_progress": in_progress,
        "in_progress_count": len(in_progress),
        "in_progress_value": in_progress_value,
        "blank_value_count": blank_value_count,
        "stale_count": stale_count,
        "unknown_count": unknown_count,
        "stale_threshold_days": staleness_rule.days_threshold,
        "lead_count": deals_by_stage.get(DealStage.LEAD.value, 0),
        "lost_count": deals_by_stage.get(DealStage.LOST.value, 0),
        "deals_by_stage": deals_by_stage,
    }


def _parse_deal_activity(d: dict) -> date | None:
    """Best-effort activity-date parsing for `DealStalenessRule`.

    Tries `last_activity_at`, then `updated_at`, then `created_at`.
    Accepts ISO datetime strings (`2026-05-22T10:00:00`) and ISO
    dates (`2026-05-22`). Returns None when no field is set or
    parseable — the staleness rule treats None as `"unknown"`
    (operator can't decide; no flag, no false-stale).
    """
    for key in ("last_activity_at", "updated_at", "created_at"):
        raw = d.get(key)
        if not raw:
            continue
        if isinstance(raw, date) and not isinstance(raw, datetime):
            return raw
        try:
            return date.fromisoformat(str(raw)[:10])
        except (TypeError, ValueError):
            continue
    return None


def compute_persona_funnels(entries: list[dict]) -> list[PersonaFunnel]:
    """Aggregate parsed list entries into per-persona funnels.

    Persona is read from `parse_entry`'s `persona` field. Stages
    counted are accept / DM steps / response / qualified — the
    operator-visible cadence funnel.
    """
    per_persona: dict[str, dict[str, int]] = {}
    for entry in entries:
        parsed = AttioClient.parse_entry(entry)
        persona = parsed.get("persona") or "unspecified"
        bucket = per_persona.setdefault(persona, {
            "invited": 0, "accepted": 0, "dm1_sent": 0, "dm2_sent": 0,
            "dm3_sent": 0, "responded": 0, "qualified": 0,
        })
        stage = parsed.get("stage") or ""
        # Funnel is cumulative — a CALL_BOOKED prospect was also accepted +
        # DM1'd + DM2'd, so each upstream counter increments too.
        if stage in {
            PipelineStage.CONNECTION_SENT.value, PipelineStage.ACCEPTED.value,
            PipelineStage.DM1_SENT.value, PipelineStage.DM2_SENT.value,
            PipelineStage.DM3_SENT.value, PipelineStage.RESPONDED.value,
            PipelineStage.CALL_BOOKED.value, PipelineStage.QUALIFIED.value,
            PipelineStage.NOT_INTERESTED.value,
        }:
            bucket["invited"] += 1
        if stage in {
            PipelineStage.ACCEPTED.value, PipelineStage.DM1_SENT.value,
            PipelineStage.DM2_SENT.value, PipelineStage.DM3_SENT.value,
            PipelineStage.RESPONDED.value, PipelineStage.CALL_BOOKED.value,
            PipelineStage.QUALIFIED.value, PipelineStage.NOT_INTERESTED.value,
        }:
            bucket["accepted"] += 1
        # Fix-3 (audit): NOT_INTERESTED prospects received at least DM1 and are
        # legitimate non-responders; include them in dm1_sent so the per-persona
        # response_rate denominator matches the compute_kpis denominator semantics
        # (mirrors learn.py _DMED_STAGES which includes NOT_INTERESTED).
        if stage in {
            PipelineStage.DM1_SENT.value, PipelineStage.DM2_SENT.value,
            PipelineStage.DM3_SENT.value, PipelineStage.RESPONDED.value,
            PipelineStage.CALL_BOOKED.value, PipelineStage.QUALIFIED.value,
            PipelineStage.NOT_INTERESTED.value,
        }:
            bucket["dm1_sent"] += 1
        if stage in {
            PipelineStage.DM2_SENT.value, PipelineStage.DM3_SENT.value,
            PipelineStage.RESPONDED.value, PipelineStage.CALL_BOOKED.value,
            PipelineStage.QUALIFIED.value,
        }:
            bucket["dm2_sent"] += 1
        if stage in {
            PipelineStage.DM3_SENT.value, PipelineStage.RESPONDED.value,
            PipelineStage.CALL_BOOKED.value, PipelineStage.QUALIFIED.value,
        }:
            bucket["dm3_sent"] += 1
        if stage in {
            PipelineStage.RESPONDED.value, PipelineStage.CALL_BOOKED.value,
            PipelineStage.QUALIFIED.value,
        }:
            bucket["responded"] += 1
        if stage == PipelineStage.QUALIFIED.value:
            bucket["qualified"] += 1
        # Wave-2-A: an UNREACHABLE row that received >=1 DM before being parked
        # belongs in the cumulative funnel up to its real DM step (it was
        # necessarily invited + accepted to have been DM'd). It is NOT a
        # response — UNREACHABLE never increments `responded`/`qualified`, so its
        # per-persona response_rate denominator grows without its numerator,
        # de-inflating the rate exactly as the compute_kpis fix does. dm_step is
        # a select-type slug; coerce via dm_step_int. dm_step=0/None
        # (connection_note/invite/OON-never-accepted) is excluded.
        if stage == PipelineStage.UNREACHABLE.value:
            step = dm_step_int(parsed.get("dm_step"))
            if step >= 1:
                bucket["invited"] += 1
                bucket["accepted"] += 1
                bucket["dm1_sent"] += 1
                if step >= 2:
                    bucket["dm2_sent"] += 1
                if step >= 3:
                    bucket["dm3_sent"] += 1

    funnels = [
        PersonaFunnel(persona=p, **bucket)
        for p, bucket in sorted(per_persona.items())
    ]
    return funnels


_VOLUME_COUNTER_KEYS = ("connections_sent", "messages_sent", "visits_sent")


def _counter_value(attributes: dict, key: str) -> int | None:
    """Lenient read of a daily_run counter attr from a normalized
    ``Record.attributes`` map for REPORTING purposes.

    Returns int when well-formed, None when absent/null/non-numeric. Unlike
    the live cap-charging path (``daily_run._strict_counter``, which must fail
    closed to protect the send cap), a report read must not abort the whole
    weekly report on one malformed row — the caller counts Nones as
    ``malformed_reads`` so the gap is visible, not silently zeroed.

    A negative send counter is malformed by domain definition — passing it
    through would silently SUBTRACT from the weekly total.

    Note: ``Record.attributes`` values are already scalar (the provider's
    normalization unwraps the vendor's ``[{"value": N}]`` shape to the plain
    scalar N before returning the record), unlike the raw Attio JSON shape.
    """
    value = attributes.get(key)
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    # A negative send counter is malformed by domain definition.
    return parsed if parsed >= 0 else None


def compute_outreach_volume(
    crm: CRMProvider, window_start: date, window_end: date
) -> dict[str, Any]:
    """Sum daily_run send counters over the report window — the authoritative
    CRM ledger for invite / DM / visit volume.

    Queries one run_date at a time on the BARE ``run_date`` attribute and
    sums across ALL machines for that day. Bare-attr filtering mirrors
    ``daily_run.query_todays_row``: the composite ``uniqueness_key`` is
    re-keyed on row close, so a key-based lookup would miss closed rows.

    Returns a dict with ``totals`` (window sums per counter), ``per_day``
    (only days that had a row), ``window_days`` / ``days_with_rows``
    (ledger coverage), ``malformed_reads`` (counter reads that failed
    lenient parsing — counted as 0 in totals but surfaced),
    ``abandoned_rows_skipped`` (rows whose status read "abandoned" — a
    stale-run takeover marker — skipped so their partial counters don't
    double-count with the replacement row, surfaced per-day and in the
    window total), ``note`` (the operator note string, empty when no caveats
    apply), and ``coverage_warning`` (set when no window day had any row — a
    healthy-looking zero is then more likely a query/schema gap).
    """
    totals: dict[str, int] = dict.fromkeys(_VOLUME_COUNTER_KEYS, 0)
    per_day: dict[str, dict[str, int]] = {}
    malformed_reads = 0
    abandoned_rows_skipped = 0

    d = window_start
    while d <= window_end:
        day_str = d.isoformat()
        # limit 50 >> realistic machine count per day — the
        # (run_date, machine_id) uniqueness constraint means one row
        # per machine, and the fleet is typically 1-2 machines.
        rows = crm.query_object_records(
            "daily_run",
            filters={"run_date": day_str},
            limit=50,
        )
        if len(rows) >= 50:
            logger.warning(
                "daily_run query for %s returned >=50 rows — possible "
                "truncation, volume may be undercounted", day_str,
            )
        day_totals: dict[str, int] = dict.fromkeys(_VOLUME_COUNTER_KEYS, 0)
        day_abandoned = 0
        for row in rows:
            # Read status leniently (a missing/malformed status keeps the row
            # counted — we never silently drop volume). A row marked
            # "abandoned" by stale-run takeover holds partial counters whose
            # work was redone by the replacement row, so summing both would
            # double-count.
            status = row.attributes.get("status")
            if isinstance(status, str) and status == "abandoned":
                day_abandoned += 1
                continue
            for key in _VOLUME_COUNTER_KEYS:
                value = _counter_value(row.attributes, key)
                if value is None:
                    malformed_reads += 1
                else:
                    day_totals[key] += value
        abandoned_rows_skipped += day_abandoned
        if rows:
            per_day[day_str] = dict(day_totals)
            if day_abandoned:
                per_day[day_str]["abandoned_rows_skipped"] = day_abandoned
            for key in _VOLUME_COUNTER_KEYS:
                totals[key] += day_totals[key]
        d += timedelta(days=1)

    window_days = (window_end - window_start).days + 1
    # Silent-zero guard (Phase-0 "0 accepted" lesson): an all-empty query
    # result over the window — wrong object slug, filter-format drift,
    # run_date shape change — would otherwise render as a healthy-looking
    # zero-volume week.
    coverage_warning = ""
    if window_days > 0 and not per_day:
        coverage_warning = (
            f"No daily_run rows found on any of the {window_days} "
            "window days — 0 volume is more likely a ledger/query gap "
            "than a true zero-activity week."
        )
    return {
        "source": "crm_daily_run_ledger",
        "totals": totals,
        "per_day": per_day,
        "window_days": window_days,
        "days_with_rows": len(per_day),
        "malformed_reads": malformed_reads,
        "abandoned_rows_skipped": abandoned_rows_skipped,
        "note": "",
        "coverage_warning": coverage_warning,
    }


def _monday_of(d: date) -> date:
    """ISO Monday of the week containing `d`. Mirrors weekly_prospect's
    `_monday_of`; duplicated here to avoid the weekly_report → weekly_prospect
    import (the workflow modules should be siblings, not chained).
    """
    return d - timedelta(days=d.weekday())


def _load_prior_snapshot(week_starting: date) -> dict | None:
    """Read the prior week's KPI snapshot file and return the parsed
    `kpi_snapshot_json` dict, or None when there is no prior snapshot.

    Prior week is `week_starting - 7d`; the file lives at
    `REPORTS_DIR / "<prior_week>.json"`.

    Missing file → None. This is the EXPECTED cold/gap case (first
    report ever, or a skipped week) and is silent — no WARN/ERROR.
    We catch ONLY `FileNotFoundError`; an `OSError` (permissions, I/O)
    or a `json.JSONDecodeError` (corrupt file) propagates rather than
    being masked as "no prior snapshot" — a corrupt sidecar is a real
    failure, not an absent one.
    """
    prior_week = (week_starting - timedelta(days=7)).isoformat()
    try:
        record = json.loads((REPORTS_DIR / f"{prior_week}.json").read_text())
    except FileNotFoundError:
        return None
    raw = record.get("kpi_snapshot_json") if isinstance(record, dict) else None
    if not raw or not isinstance(raw, str):
        return None
    return json.loads(raw)


def _compute_prior_period_compare(
    current_totals: dict[str, float],
    prior_snapshot: dict | None,
) -> dict[str, PriorPeriodDelta] | None:
    """Build `{key: PriorPeriodDelta}` for each numeric KPI.

    Returns None when:
      * no prior snapshot was loaded (first-ever report)
      * prior snapshot lacks `totals` dict
      * no key in `current_totals` has a numeric prior value
        (e.g., prior snapshot is from a different schema version)

    `delta = current - prior`; positive means current is higher.
    """
    if not prior_snapshot:
        return None
    prior_totals = prior_snapshot.get("totals", {})
    if not isinstance(prior_totals, dict):
        return None
    out: dict[str, PriorPeriodDelta] = {}
    for key, current in current_totals.items():
        prior = prior_totals.get(key)
        if not isinstance(prior, (int, float)):
            continue
        out[key] = {
            "prior": float(prior),
            "current": float(current),
            "delta": float(current) - float(prior),
        }
    return out or None


def _atomic_write_json(path: Path, record: dict) -> None:
    """Write `record` to `path` atomically: serialize to a temp file in
    the SAME directory, then `os.replace` it into place. os.replace is
    atomic on POSIX, so a concurrent reader (e.g. a double-fired
    `cli report` — there is no run-lock) sees either the old file or the
    fully-written new file, never a torn read. A genuine OS error
    (permissions, disk full) propagates — losing the canonical sidecar
    is not a swallow-and-continue case.
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(record, default=str))
        os.replace(tmp, path)
    finally:
        # Clean up the temp file if the rename never happened (e.g. a
        # cross-device os.replace error) so we don't leak .tmp files.
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _supersede_existing_snapshot(path: Path) -> None:
    """Copy an existing same-week snapshot aside before overwriting.

    Fix-5 (audit): a same-week re-run previously silently overwrote the
    first run's file, destroying the audit trail. Now, any existing file
    at `path` is renamed to `<stem>_superseded_<n>.json` (incrementing n
    until the name is unused) before the new write lands.

    Week-over-week delta logic continues to read the canonical `<week>.json`
    (the new write); the superseded files are inert audit copies.

    Missing file → no-op (expected first-run case). A genuine OS error
    (permissions, disk full) propagates — losing the audit trail copy is not
    a swallow-and-continue case.
    """
    if not path.exists():
        return
    n = 1
    while True:
        aside = path.with_name(f"{path.stem}_superseded_{n}.json")
        if not aside.exists():
            break
        n += 1
    # shutil.copy2 would work but os.rename (on same filesystem) is atomic.
    # Use path.rename for POSIX atomicity; fall back to copy+unlink on
    # cross-device (unlikely — all in the same REPORTS_DIR).
    import shutil
    try:
        path.rename(aside)
    except OSError:
        shutil.copy2(path, aside)


def _upsert_kpi_snapshot(
    snapshot: KPISnapshot,
    *,
    persona_funnels: list[PersonaFunnel],
    active_deals: dict,
    sent_to: str,
    resend_message_id: str,
) -> str:
    """Write the weekly KPI snapshot to `REPORTS_DIR/<week_starting>.json`.

    Fix-5 (audit): before overwriting an existing same-week file, copy it
    aside as `<week>_superseded_<n>.json` to preserve the audit trail of
    the first run. The canonical `<week>.json` is always the latest run;
    week-over-week delta logic reads only that file.

    The write is atomic (see `_atomic_write_json`). Returns the file path
    (str) as the sidecar handle; callers pass it to
    `_patch_resend_message_id` after the Resend send to backfill
    `report_resend_message_id`.
    """
    record = {
        "week_starting": snapshot["week_starting"],
        "kpi_snapshot_json": json.dumps(snapshot, default=str),
        "persona_funnels_json": json.dumps(
            [pf.to_dict() for pf in persona_funnels], default=str,
        ),
        "active_deals_json": json.dumps(active_deals, default=str),
        "measurement_basis": snapshot["measurement_basis"],
        "report_email_sent_to": sent_to,
        "report_resend_message_id": resend_message_id,
    }
    path = REPORTS_DIR / f"{snapshot['week_starting']}.json"
    _supersede_existing_snapshot(path)
    _atomic_write_json(path, record)
    return str(path)


def _patch_resend_message_id(
    sidecar_path: str,
    resend_message_id: str,
) -> None:
    """Post-send patch that captures the Resend message id on the
    existing sidecar file.

    The sidecar file was written before the send (durability); this
    read-modify-write fills in `report_resend_message_id` only, so
    operators can correlate the `resend_delivery_failed` queue row's
    `kpi_snapshot_week_starting` back-pointer to the actual delivered
    email via this id.

    Reachable on success (the upsert now returns a real file path, not
    None). A file read/write error PROPAGATES — a local sidecar that
    exists but cannot be updated is a real failure, not a best-effort
    swallow.
    """
    path = Path(sidecar_path)
    record = json.loads(path.read_text())
    record["report_resend_message_id"] = resend_message_id
    _atomic_write_json(path, record)


def _open_resend_delivery_failed_row(
    attio: AttioClient,
    recipient: str,
    week_starting: str,
    resend_error: str,
) -> None:
    """Open a typed `resend_delivery_failed` queue row when Resend
    rejects the email send. Sidecar JSON file is already written by
    the caller — operators correlate via `kpi_snapshot_week_starting`.

    Per silent-failure-hunter QA convergence: catch ONLY infra-
    failure shapes (Attio transport errors, network). Typed
    programmer-bug exceptions from `escalate()` itself —
    `UnknownEscalationType`, `MissingDecisionKey`,
    `EscalationSchemaError` — propagate so a typo in the slug or
    payload schema drift fails CI / the dev's first run instead of
    being silently logged.
    """
    import httpx
    try:
        escalate(
            type="resend_delivery_failed",
            idempotency_key=f"weekly_report|{week_starting}",
            payload={
                "recipient_email": recipient,
                "send_attempt_at": datetime.now(UTC).isoformat(),
                "resend_error_code": resend_error[:200],
                "kpi_snapshot_week_starting": week_starting,
            },
            attio=attio,
        )
    except (httpx.HTTPError, ConnectionError, TimeoutError):
        logger.exception(
            "Failed to open resend_delivery_failed queue row for "
            "recipient=%s week_starting=%s (Attio infra failure — "
            "queue row missing; operator must triage from terminal "
            "output alone)",
            recipient, week_starting,
        )


def _status_indicator(value: float, target_range: tuple[float, float]) -> str:
    """Return a status indicator based on target range."""
    low, high = target_range
    if value >= high * 100:
        return "above target"
    elif value >= low * 100:
        return "on target"
    else:
        return "BELOW TARGET"


def _delta_indicator(
    key: str,
    prior_period_compare: dict[str, PriorPeriodDelta] | None,
) -> str:
    """Render a small ▲/▼ indicator for week-over-week delta.

    Returns an empty string when no compare is available. Triangle
    glyph is uncolored so the email renders cleanly across clients;
    sign is explicit (`+2.0` / `-1.0`).
    """
    if not prior_period_compare:
        return ""
    delta_dict = prior_period_compare.get(key)
    if not delta_dict:
        return ""
    delta = delta_dict.get("delta", 0)
    if delta == 0:
        return ' <span style="color:#9ca3af;font-size:12px;">—</span>'
    sign = "+" if delta > 0 else ""
    arrow = "▲" if delta > 0 else "▼"
    return (
        f' <span style="color:#6b7280;font-size:12px;">'
        f'{arrow} {sign}{delta:g}</span>'
    )


def _persona_funnel_table_html(persona_funnels: list[PersonaFunnel]) -> str:
    """Render the PersonaFunnel top-of-fold table (3-agent QA
    convergence fold — pre-fold the data went to the sidecar only;
    the operator-facing email is now the primary view).
    """
    if not persona_funnels:
        return ""
    rows = ""
    for pf in persona_funnels:
        acceptance_pct = round(pf.acceptance_rate * 100, 1)
        response_pct = round(pf.response_rate * 100, 1)
        rows += f"""
        <tr style="border-bottom:1px solid #eee;">
            <td style="padding:8px 12px;font-size:14px;color:#374151;font-weight:500;">{pf.persona}</td>
            <td style="padding:8px 12px;text-align:right;font-size:14px;color:#374151;">{pf.invited}</td>
            <td style="padding:8px 12px;text-align:right;font-size:14px;color:#374151;">{pf.accepted} <span style="color:#9ca3af;font-size:11px;">({acceptance_pct}%)</span></td>
            <td style="padding:8px 12px;text-align:right;font-size:14px;color:#374151;">{pf.dm1_sent}/{pf.dm2_sent}/{pf.dm3_sent}</td>
            <td style="padding:8px 12px;text-align:right;font-size:14px;color:#374151;">{pf.responded} <span style="color:#9ca3af;font-size:11px;">({response_pct}%)</span></td>
            <td style="padding:8px 12px;text-align:right;font-size:14px;color:#374151;font-weight:600;">{pf.qualified}</td>
        </tr>"""
    # Fix-4 (audit): heading was "Persona Funnel (this week)" which implies a 7-day
    # window; metrics are actually all-time cumulative pipeline snapshots.
    return f"""
    <h2 style="font-size:16px;color:#111827;margin:0 0 12px;">Persona Funnel (cumulative, all-time)</h2>
    <table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5e7eb;border-radius:8px;margin-bottom:24px;">
      <thead>
        <tr style="background:#f3f4f6;">
          <th style="padding:8px 12px;text-align:left;font-size:12px;color:#6b7280;text-transform:uppercase;">Persona</th>
          <th style="padding:8px 12px;text-align:right;font-size:12px;color:#6b7280;text-transform:uppercase;">Invited</th>
          <th style="padding:8px 12px;text-align:right;font-size:12px;color:#6b7280;text-transform:uppercase;">Accepted</th>
          <th style="padding:8px 12px;text-align:right;font-size:12px;color:#6b7280;text-transform:uppercase;">DM 1/2/3</th>
          <th style="padding:8px 12px;text-align:right;font-size:12px;color:#6b7280;text-transform:uppercase;">Responded</th>
          <th style="padding:8px 12px;text-align:right;font-size:12px;color:#6b7280;text-transform:uppercase;">Qualified</th>
        </tr>
      </thead>
      <tbody>
        {rows}
      </tbody>
    </table>
"""


def build_report_html(
    kpis: dict,
    active_deals: dict,
    report_date: date,
    *,
    persona_funnels: list[PersonaFunnel] | None = None,
    prior_period_compare: dict[str, PriorPeriodDelta] | None = None,
    outreach_volume: dict[str, Any] | None = None,
) -> str:
    """Build HTML email for the weekly KPI report.

    Post-fold (3-agent QA convergence on §5 scope-doc "top-of-fold"):
    accepts `persona_funnels` and renders them BEFORE the Key Metrics
    block — the operator's first question opening the report is
    "which persona cohort moved?" and the funnel answers it. Also
    accepts `prior_period_compare` to render week-over-week deltas
    on the Key Metrics rows. Both are keyword-only and default to
    None so legacy callers (or pre-snapshot test fixtures) still work.
    """
    if persona_funnels is None:
        persona_funnels = []
    persona_funnel_html = _persona_funnel_table_html(persona_funnels)
    counts = kpis["stage_counts"]

    # Status indicators
    acceptance_status = _status_indicator(
        kpis["connection_acceptance_rate"],
        cast("tuple[float, float]", TARGETS["connection_acceptance_rate"]),
    )
    response_status = _status_indicator(
        kpis["response_rate"],
        cast("tuple[float, float]", TARGETS["response_rate"]),
    )

    stage_rows = ""
    for stage in PipelineStage:
        count = counts.get(stage.value, 0)
        stage_rows += f"""
        <tr style="border-bottom:1px solid #eee;">
            <td style="padding:8px 12px;font-size:14px;color:#374151;">{stage.value}</td>
            <td style="padding:8px 12px;text-align:center;font-size:14px;color:#374151;font-weight:600;">{count}</td>
        </tr>"""

    # Active deals (In Progress) detail rows
    deal_rows = ""
    for d in active_deals["in_progress"]:
        country_str = f" · {d['country']}" if d["country"] else ""
        if d["value_is_fallback"]:
            value_cell = '<span style="color:#9ca3af;">— <span style="font-size:11px;">(ACV fallback)</span></span>'
        else:
            value_cell = f"${d['value']:,.0f}"
        # GTM QA convergence: surface stale flag in the email's deal
        # section so pipeline-rot is visible to the operator inbox-side
        # (not just in the sidecar JSON).
        stale_badge = ""
        if d.get("stale_status") == "stale":
            stale_badge = ' <span style="color:#b45309;background:#fef3c7;padding:1px 6px;border-radius:4px;font-size:11px;font-weight:500;">stale</span>'
        deal_rows += f"""
        <tr style="border-bottom:1px solid #eee;">
            <td style="padding:8px 12px;font-size:14px;color:#374151;">{d['name']}{stale_badge}<span style="color:#9ca3af;font-size:12px;">{country_str}</span></td>
            <td style="padding:8px 12px;text-align:right;font-size:14px;color:#374151;font-weight:600;">{value_cell}</td>
        </tr>"""

    if active_deals["blank_value_count"] > 0:
        hygiene_banner = f"""
    <div style="margin-bottom:12px;padding:10px 14px;background:#fef3c7;border-left:3px solid #f59e0b;border-radius:4px;font-size:13px;color:#92400e;">
      ⚠️ {active_deals['blank_value_count']} active deals missing <code>value</code> in Attio — using ${TARGETS['acv']:,.0f} ACV fallback
    </div>"""
    else:
        hygiene_banner = ""

    if active_deals["in_progress_count"] > 0:
        active_section = f"""
    <h2 style="font-size:16px;color:#111827;margin:0 0 12px;">Active Deals</h2>
    {hygiene_banner}
    <table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5e7eb;border-radius:8px;margin-bottom:12px;">
      <thead>
        <tr style="background:#f3f4f6;">
          <th style="padding:8px 12px;text-align:left;font-size:12px;color:#6b7280;text-transform:uppercase;">In Progress · {active_deals['in_progress_count']} deals</th>
          <th style="padding:8px 12px;text-align:right;font-size:12px;color:#6b7280;text-transform:uppercase;">Value</th>
        </tr>
      </thead>
      <tbody>
        {deal_rows}
      </tbody>
    </table>
    <p style="margin:0 0 24px;font-size:13px;color:#6b7280;">{active_deals['lead_count']} leads · {active_deals['lost_count']} lost</p>
"""
    else:
        active_section = f"""
    <h2 style="font-size:16px;color:#111827;margin:0 0 12px;">Active Deals</h2>
    <p style="margin:0 0 24px;font-size:13px;color:#6b7280;">No deals currently in progress · {active_deals['lead_count']} leads · {active_deals['lost_count']} lost</p>
"""

    # Outreach volume from the CRM daily_run ledger. Keyword-only and
    # None-defaulted so legacy callers still work.
    volume_section = ""
    if outreach_volume is not None:
        vol = outreach_volume["totals"]
        # On a wet run the email is the artifact the operator actually reads
        # (CLI scrollback is gone) — every caveat the CLI prints must land
        # here too: coverage gap, note, malformed reads.
        warning_lines = []
        if outreach_volume.get("coverage_warning"):
            warning_lines.append(outreach_volume["coverage_warning"])
        if outreach_volume.get("note"):
            warning_lines.append(outreach_volume["note"])
        if outreach_volume.get("malformed_reads"):
            warning_lines.append(
                f"{outreach_volume['malformed_reads']} daily_run counter reads "
                "were malformed and counted as 0 — inspect the daily_run rows."
            )
        volume_note_html = ""
        if warning_lines:
            warning_divs = "".join(
                f'<div style="margin-bottom:4px;">⚠️ {line}</div>'
                for line in warning_lines
            )
            volume_note_html = f"""
    <div style="margin-bottom:24px;padding:10px 14px;background:#fef3c7;border-left:3px solid #f59e0b;border-radius:4px;font-size:13px;color:#92400e;">
      {warning_divs}
    </div>"""
        volume_section = f"""
    <h2 style="font-size:16px;color:#111827;margin:0 0 12px;">Outreach Volume ({outreach_volume['window_days']}d)</h2>
    <table style="width:100%;border-collapse:collapse;margin-bottom:8px;">
      <tr>
        <td style="padding:6px 0;font-size:14px;color:#6b7280;width:200px;">Invites Sent</td>
        <td style="padding:6px 0;font-size:14px;color:#111827;font-weight:600;">{vol['connections_sent']}</td>
      </tr>
      <tr>
        <td style="padding:6px 0;font-size:14px;color:#6b7280;">DMs Sent</td>
        <td style="padding:6px 0;font-size:14px;color:#111827;font-weight:600;">{vol['messages_sent']}</td>
      </tr>
      <tr>
        <td style="padding:6px 0;font-size:14px;color:#6b7280;">Profile Visits</td>
        <td style="padding:6px 0;font-size:14px;color:#111827;font-weight:600;">{vol['visits_sent']}</td>
      </tr>
    </table>
    <p style="margin:0 0 {'12px' if volume_note_html else '24px'};font-size:12px;color:#9ca3af;">Ledger rows on {outreach_volume['days_with_rows']} of {outreach_volume['window_days']} window days</p>
    {volume_note_html}
"""

    return f"""\
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f9fafb;">
  <div style="max-width:600px;margin:0 auto;padding:32px 20px;">
    <h1 style="font-size:20px;color:#111827;margin:0 0 4px;">Outbound Agent — Weekly Report</h1>
    <p style="font-size:14px;color:#6b7280;margin:0 0 24px;">Week ending {report_date.isoformat()}</p>

    {persona_funnel_html}

    <h2 style="font-size:16px;color:#111827;margin:0 0 12px;">Key Metrics</h2>
    <table style="width:100%;border-collapse:collapse;margin-bottom:24px;">
      <tr>
        <td style="padding:6px 0;font-size:14px;color:#6b7280;width:200px;">Total Prospects</td>
        <td style="padding:6px 0;font-size:14px;color:#111827;font-weight:600;">{kpis['total_prospects']}{_delta_indicator('total_prospects', prior_period_compare)}</td>
      </tr>
      <tr>
        <td style="padding:6px 0;font-size:14px;color:#6b7280;">Acceptance Rate</td>
        <td style="padding:6px 0;font-size:14px;color:#111827;font-weight:600;">{kpis['connection_acceptance_rate']}% ({acceptance_status}){_delta_indicator('connection_acceptance_rate', prior_period_compare)}</td>
      </tr>
      <tr>
        <td style="padding:6px 0;font-size:14px;color:#6b7280;">Response Rate</td>
        <td style="padding:6px 0;font-size:14px;color:#111827;font-weight:600;">{kpis['response_rate']}% ({response_status}){_delta_indicator('response_rate', prior_period_compare)}</td>
      </tr>
      <tr>
        <td style="padding:6px 0;font-size:14px;color:#6b7280;">Calls Booked</td>
        <td style="padding:6px 0;font-size:14px;color:#111827;font-weight:600;">{kpis['calls_booked']}{_delta_indicator('calls_booked', prior_period_compare)}</td>
      </tr>
      <tr>
        <td style="padding:6px 0;font-size:14px;color:#6b7280;">Qualified (outbound)</td>
        <td style="padding:6px 0;font-size:14px;color:#111827;font-weight:600;">{kpis['qualified']}{_delta_indicator('qualified', prior_period_compare)}</td>
      </tr>
      <tr>
        <td style="padding:6px 0;font-size:14px;color:#6b7280;">Pipeline Value</td>
        <td style="padding:6px 0;font-size:14px;color:#111827;font-weight:600;">${active_deals['in_progress_value']:,.0f}{_delta_indicator('in_progress_value', prior_period_compare)}</td>
      </tr>
    </table>

    {volume_section}
    {active_section}
    <h2 style="font-size:16px;color:#111827;margin:0 0 12px;">Pipeline Breakdown</h2>
    <table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5e7eb;border-radius:8px;">
      <thead>
        <tr style="background:#f3f4f6;">
          <th style="padding:8px 12px;text-align:left;font-size:12px;color:#6b7280;text-transform:uppercase;">Stage</th>
          <th style="padding:8px 12px;text-align:center;font-size:12px;color:#6b7280;text-transform:uppercase;">Count</th>
        </tr>
      </thead>
      <tbody>
        {stage_rows}
      </tbody>
    </table>

    <p style="margin-top:24px;font-size:12px;color:#9ca3af;">Sent by Outbound Agent</p>
  </div>
</body>
</html>"""


def run_weekly_report(
    attio: AttioClient,
    resend: ResendClient | None = None,
    report_email: str | None = None,
    dry_run: bool = False,
    *,
    today: date | None = None,
    crm: CRMProvider | None = None,
) -> KPISnapshot:
    """Generate the weekly KPI report and write the agent-readable sidecar.

    Args:
        attio: Attio API client.
        resend: Resend email client. None disables email (dry-run or
            test). Per scope-doc, the CLI defaults to dry-run; explicit
            `--send` is required to email (silent-failure-hunter guard
            on §0 #9).
        report_email: Email recipient. Defaults to `REPORT_EMAIL` env.
        dry_run: If True, write sidecar but do NOT send email. The
            sidecar row is written on EVERY run (operator can audit
            historical reports regardless of email policy).
        today: Operator-invocation date (testability). Defaults to
            `date.today()`.
        crm: Vendor-neutral CRMProvider for reading the daily_run
            ledger (``compute_outreach_volume``). When None, the
            outreach_volume section is populated with empty totals and
            a coverage warning (backward-compatible — callers not yet
            on the CRM seam still get a report).

    Returns:
        `KPISnapshot` TypedDict for the run.
    """
    list_id = os.environ.get("ATTIO_LIST_ID", "")
    email = report_email or os.environ.get("REPORT_EMAIL", "ops@example.com")
    if today is None:
        today = date.today()

    click.echo("Querying Attio pipeline...")
    # Fix-1 (audit): raised from 500 to 50_000 to match daily_check_helpers and
    # prevent silent truncation on a pipeline that already exceeds 500 entries.
    entries = attio.query_list_entries(list_id=list_id, limit=50_000)
    raw_deals = attio.search_deals(limit=500)
    deals = [AttioClient.parse_deal(d) for d in raw_deals]

    # Fix-2 (audit): drop soft-deleted duplicate entries whose union-merge winner
    # already carries the cadence identity. Mirrors learn.py:468. Without this,
    # compute_kpis and compute_persona_funnels double-count every merged prospect.
    entries = [e for e in entries if not AttioClient.parse_entry(e).get("merged_into")]

    kpis = compute_kpis(entries)
    staleness_rule, stale_threshold_source = DealStalenessRule.from_env_with_source()
    active = compute_active_deals(deals, today=today, staleness_rule=staleness_rule)
    persona_funnels = compute_persona_funnels(entries)
    report_date = today

    # PR-30 window kept for `snapshot["window"]` (human-readable date range in the
    # sidecar). `measurement_basis` is now honest: no date filter is applied, so
    # every metric is an all-time pipeline snapshot as of today.
    # Fix-4 (audit): replaced the misleading "7d_window_…" label with
    # "all_time_snapshot_asof_<date>" so reading the sidecar is not misled into
    # thinking metrics are windowed to the last 7 days. Actual windowing is
    # future work.
    window_start = today - timedelta(days=_REPORT_WINDOW_DAYS - 1)
    window_str = f"{window_start.isoformat()}..{today.isoformat()}"
    measurement_basis = f"all_time_snapshot_asof_{today.isoformat()}"
    week_starting = _monday_of(today)

    # Invite/DM/visit volume from the CRM daily_run ledger — unlike the
    # pipeline-stage metrics above, this IS windowed to the report week.
    if crm is not None:
        outreach_volume = compute_outreach_volume(crm, window_start, today)
    else:
        outreach_volume = {
            "source": "crm_daily_run_ledger",
            "totals": dict.fromkeys(_VOLUME_COUNTER_KEYS, 0),
            "per_day": {},
            "window_days": (today - window_start).days + 1,
            "days_with_rows": 0,
            "malformed_reads": 0,
            "note": "",
            "coverage_warning": "No CRM provider passed — ledger volume unavailable.",
        }

    # PR-30 prior-period compare from the previous week's snapshot file.
    prior_snapshot = _load_prior_snapshot(week_starting)
    totals = {
        "total_prospects": float(kpis["total_prospects"]),
        "connection_acceptance_rate": float(kpis["connection_acceptance_rate"]),
        "response_rate": float(kpis["response_rate"]),
        "calls_booked": float(kpis["calls_booked"]),
        "qualified": float(kpis["qualified"]),
        "pipeline_value": float(kpis["pipeline_value"]),
        "in_progress_count": float(active["in_progress_count"]),
        "in_progress_value": float(active["in_progress_value"]),
        "stale_count": float(active["stale_count"]),
    }
    prior_period_compare = _compute_prior_period_compare(totals, prior_snapshot)

    snapshot: KPISnapshot = {
        "window": window_str,
        "measurement_basis": measurement_basis,
        "prior_period_compare": prior_period_compare,
        "week_starting": week_starting.isoformat(),
        "totals": totals,
        "stage_counts": kpis["stage_counts"],
        "persona_funnels": [pf.to_dict() for pf in persona_funnels],
        "active_deals": {
            "in_progress_count": active["in_progress_count"],
            "in_progress_value": active["in_progress_value"],
            "stale_count": active["stale_count"],
            "unknown_count": active["unknown_count"],
            "stale_threshold_days": active["stale_threshold_days"],
            "lead_count": active["lead_count"],
            "lost_count": active["lost_count"],
            "blank_value_count": active["blank_value_count"],
        },
        "stale_threshold_source": stale_threshold_source,
        "notes": "",
        "outreach_volume": outreach_volume,
    }

    click.echo(f"\n--- Weekly KPIs ({report_date.isoformat()}) ---")
    click.echo(f"Total prospects:       {kpis['total_prospects']}")
    click.echo(f"Acceptance rate:       {kpis['connection_acceptance_rate']}%")
    click.echo(f"Response rate:         {kpis['response_rate']}%")
    click.echo(f"Calls booked:          {kpis['calls_booked']}")
    click.echo(f"Qualified (outbound):  {kpis['qualified']}")
    click.echo(f"Pipeline value:        ${active['in_progress_value']:,.0f}")
    click.echo(f"Not interested:        {kpis['not_interested']}")
    click.echo()

    for stage, count in kpis["stage_counts"].items():
        click.echo(f"  {stage}: {count}")

    click.echo()
    click.echo(f"--- Outreach Volume ({window_str}, CRM daily_run ledger) ---")
    vol_totals = outreach_volume["totals"]
    click.echo(f"Invites sent:          {vol_totals['connections_sent']}")
    click.echo(f"DMs sent:              {vol_totals['messages_sent']}")
    click.echo(f"Profile visits:        {vol_totals['visits_sent']}")
    click.echo(
        f"Ledger coverage:       {outreach_volume['days_with_rows']} of "
        f"{outreach_volume['window_days']} window days had a daily_run row"
    )
    if outreach_volume["coverage_warning"]:
        click.echo(f"⚠️  {outreach_volume['coverage_warning']}")
    if outreach_volume["note"]:
        click.echo(f"⚠️  {outreach_volume['note']}")
    if outreach_volume["malformed_reads"]:
        click.echo(
            f"⚠️  {outreach_volume['malformed_reads']} daily_run counter reads "
            "were malformed and counted as 0 — inspect the daily_run rows."
        )

    click.echo()
    click.echo("--- Active Deals ---")
    fallback_note = (
        f", {active['blank_value_count']} with ACV fallback"
        if active["blank_value_count"] > 0 else ""
    )
    click.echo(
        f"In Progress:           {active['in_progress_count']} deals  "
        f"(${active['in_progress_value']:,.0f}{fallback_note})"
    )
    for d in active["in_progress"]:
        flag = " (ACV fallback)" if d["value_is_fallback"] else ""
        country = f" ({d['country']})" if d["country"] else ""
        value_str = "—" if d["value_is_fallback"] else f"${d['value']:,.0f}"
        click.echo(f"  - {d['name']}{country}: {value_str}{flag}")
    click.echo(f"Leads:                 {active['lead_count']}")
    click.echo(f"Lost:                  {active['lost_count']}")

    if active["blank_value_count"] > 0:
        click.echo(
            f"\n⚠️  {active['blank_value_count']} active deals missing $ value in Attio"
            f" — using ${TARGETS['acv']:,.0f} ACV fallback"
        )

    # Wave-2-A sidecar: write the Weekly KPI Snapshot JSON file on EVERY
    # run (durability — subagents read by week_starting filename
    # regardless of email policy). The write happens BEFORE the Resend
    # attempt so a Resend outage cannot lose the snapshot. Idempotent by
    # deterministic path: same-week re-runs overwrite the file in place.
    sent_to = "" if dry_run or resend is None else email
    sidecar_path = _upsert_kpi_snapshot(
        snapshot,
        persona_funnels=persona_funnels,
        active_deals=active,
        sent_to=sent_to,
        resend_message_id="",  # filled post-send via _patch_resend_message_id
    )

    if dry_run or resend is None:
        click.echo(
            f"\n[DRY RUN] Report not sent. Sidecar JSON written to "
            f"reports/weekly-kpi/{week_starting.isoformat()}.json."
        )
        return snapshot

    html = build_report_html(
        kpis, active, report_date,
        persona_funnels=persona_funnels,
        prior_period_compare=prior_period_compare,
        outreach_volume=outreach_volume,
    )
    subject = (
        f"Outbound Agent — {report_date.isoformat()} | "
        f"{active['in_progress_count']} active deals, "
        f"{kpis['connection_acceptance_rate']}% acceptance"
    )

    try:
        result = resend.send_email(to=email, subject=subject, html=html)
        resend_message_id = (
            (result or {}).get("id") if isinstance(result, dict) else ""
        ) or ""
        click.echo(f"\nReport sent to {email}.")
        # Post-send patch to fill in the message id on the sidecar file
        # (pre-fold the id was captured here but never persisted, so the
        # correlation field was always empty).
        if sidecar_path and resend_message_id:
            _patch_resend_message_id(sidecar_path, resend_message_id)
    except Exception as err:  # noqa: BLE001 — Resend boundary
        # PR-30: Resend failure opens a typed queue row. Sidecar JSON
        # file was already written above; operator correlates via
        # kpi_snapshot_week_starting.
        logger.exception("Resend send failed for week_starting=%s", week_starting)
        _open_resend_delivery_failed_row(
            attio, recipient=email,
            week_starting=week_starting.isoformat(),
            resend_error=f"{type(err).__name__}: {err}",
        )
        click.echo(
            f"\n⚠ Resend send FAILED for {email}: {type(err).__name__}. "
            "Sidecar JSON written; resend_delivery_failed queue row "
            "opened for operator triage.",
            err=True,
        )

    return snapshot
