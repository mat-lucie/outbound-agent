"""PR-36 — Live Attio GTM inventory + ``icp_phasing`` escalation.

Read-only snapshot of the GTM funnel state. Pulls LinkedIn Outreach list
entries, Company records, Deal records, and Diagnostic Run rows; computes
per-lane counts, DM3-completion rates, and positive-response rates;
writes a JSON inventory; and OPTIONALLY opens an ``icp_phasing``
configuration_decision queue row (7d deadline) when the lane-balance
heuristic recommends a phasing change.

Read-only contract: zero writes to Attio except the queue escalation
itself. The script never PATCHes a list entry, Company, or Deal.

Per memory: ICP 2 (midmarket/legacy) is phasing out in favour of ICP 1
(enterprise + target_company_mode). The heuristic surfaces evidence for
or against keeping ICP 2 in the funnel — it never decides on its own.

Usage:
    python3 scripts/gtm_attio_inventory.py                    # snapshot + open escalation if warranted
    python3 scripts/gtm_attio_inventory.py --dry-run          # snapshot only, NO escalation
    python3 scripts/gtm_attio_inventory.py --out PATH         # custom JSON output path
    python3 scripts/gtm_attio_inventory.py --no-emit-empty    # skip JSON file when no entries
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import click
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent))

from clients.attio import AttioClient  # noqa: E402
from workflows.escalation import (  # noqa: E402
    EscalationSchemaError,
    MissingAttioCredentials,
    UnknownEscalationType,
    escalate,
)

# ============================================================
# Exit codes
# ============================================================

EXIT_OK = 0
EXIT_HEURISTIC_ABSTAINED = 1     # config-side data dirty / population implausibly small
EXIT_CONFIG_MISSING = 2          # ATTIO_LIST_ID unset, etc.
EXIT_ATTIO_FAILURE = 3           # Attio HTTP error, schema error, missing creds

# ============================================================
# Constants
# ============================================================

# Lane buckets are derived from the list-entry `scoring_lane` attribute
# (set by `quality_gate.score_prospect`). Canonical taxonomy is pinned to
# `workflows.weekly_prospect.enforce_icp_lane_geo` and tested in
# `tests/test_pr36_gtm_attio_inventory.py::TestLaneTaxonomyConsistency`:
#
#   - "enterprise_mode"      → ICP 1 (the only ICP 1 lane: $500M+ LATAM mfg)
#   - "target_company_mode"  → ICP 2 (target-list persona-scoped harvest;
#                              weekly_prospect.py:349 treats this as ICP 2
#                              for the geo-violation gate)
#   - "legacy"               → ICP 2 default
LANE_BUCKETS: dict[str, str] = {
    "enterprise_mode": "icp1_enterprise",
    "target_company_mode": "icp2_target",
    "legacy": "icp2_legacy",
}
ICP1_LANES: frozenset[str] = frozenset({"icp1_enterprise"})
ICP2_LANES: frozenset[str] = frozenset({"icp2_target", "icp2_legacy"})
UNKNOWN_LANE_BUCKET = "unknown"

# Heuristic thresholds. Conservative defaults chosen so the escalation
# only fires when there's actual evidence; tuning requires a PR (the
# constants are not read from Attio at runtime).
DM3_COMPLETED_MIN_FOR_RATE: int = 30      # min N per ICP aggregate to trust the rate
ICP2_LOW_RATE_THRESHOLD: float = 0.05     # 5% positive — below = phase-out evidence
ICP1_VS_ICP2_RATE_RATIO: float = 2.0      # ICP1 ≥ 2× ICP2 → phase-out evidence

# Data-quality gates — abstain instead of opening a misleading row.
UNKNOWN_LANE_ABSTAIN_FRACTION: float = 0.05    # >5% of dm3-completed entries with no lane → abstain
EXPECTED_MIN_LIST_ENTRIES_ENV = "GTM_INVENTORY_EXPECTED_MIN"
DEFAULT_EXPECTED_MIN_LIST_ENTRIES: int = 500   # below = partial-pull suspect

POSITIVE_RESPONSE_CLASSIFICATIONS: frozenset[str] = frozenset({"positive", "interested"})

ESCALATION_DEADLINE_DAYS: int = 7
DECISION_KEY: str = "icp_phasing"

# Idempotency anchor — operator-local week. America/Lima has no DST and
# is the GTM-team timezone (per CLAUDE.md / Sales canon). Anchoring on
# operator-local Monday avoids the UTC-midnight straddle that would
# otherwise produce two `icp_phasing` rows for the same operator-week.
WEEK_ANCHOR_TZ = "America/Lima"


# ============================================================
# Inventory data shape
# ============================================================


def _lane_bucket(entry_attrs: dict) -> str:
    """Map a list entry's ``scoring_lane`` to a stable inventory bucket."""
    lane = entry_attrs.get("scoring_lane")
    if lane in LANE_BUCKETS:
        return LANE_BUCKETS[lane]
    return UNKNOWN_LANE_BUCKET


def _is_dm3_completed(entry_attrs: dict) -> bool:
    """True when the entry has been through DM3 (``dm3_sent_at`` non-null)."""
    return bool(entry_attrs.get("dm3_sent_at"))


def _is_positive(entry_attrs: dict) -> bool:
    """True when the entry's ``response_classification`` is a positive signal."""
    return entry_attrs.get("response_classification") in POSITIVE_RESPONSE_CLASSIFICATIONS


def compute_lane_metrics(entries: list[dict]) -> dict[str, dict[str, Any]]:
    """Aggregate list entries into per-lane inventory metrics.

    Returns a dict keyed by lane bucket. Each value carries:
        - total          : total entries in the lane
        - dm3_completed  : entries with ``dm3_sent_at`` set
        - positive       : entries with positive ``response_classification``
        - positive_rate  : positive / dm3_completed (None when N below
                           ``DM3_COMPLETED_MIN_FOR_RATE``)

    The positive_rate gate (None below N) is deliberate: a 2-of-5 rate
    is noise and should not drive a phasing decision. Operators get the
    raw counts and can override.
    """
    buckets: dict[str, dict[str, Any]] = {}
    for entry in entries:
        attrs = AttioClient.parse_entry(entry)
        bucket = _lane_bucket(attrs)
        b = buckets.setdefault(
            bucket, {"total": 0, "dm3_completed": 0, "positive": 0}
        )
        b["total"] += 1
        if _is_dm3_completed(attrs):
            b["dm3_completed"] += 1
        if _is_positive(attrs):
            b["positive"] += 1

    for b in buckets.values():
        n = b["dm3_completed"]
        b["positive_rate"] = (b["positive"] / n) if n >= DM3_COMPLETED_MIN_FOR_RATE else None
    return buckets


def stage_distribution(entries: list[dict]) -> dict[str, int]:
    """Count list entries by current ``stage`` (PipelineStage)."""
    counts: Counter[str] = Counter()
    for entry in entries:
        attrs = AttioClient.parse_entry(entry)
        counts[attrs.get("stage") or "unknown"] += 1
    return dict(counts)


def response_distribution(entries: list[dict]) -> dict[str, int]:
    """Count list entries by ``response_classification`` (None bucket included)."""
    counts: Counter[str] = Counter()
    for entry in entries:
        attrs = AttioClient.parse_entry(entry)
        counts[attrs.get("response_classification") or "unclassified"] += 1
    return dict(counts)


# ============================================================
# Phasing heuristic
# ============================================================


def _aggregate(lane_metrics: dict[str, dict[str, Any]], lanes: frozenset[str]) -> dict[str, Any]:
    """Sum dm3_completed + positive across a set of lane buckets."""
    n = sum(lane_metrics.get(lane, {}).get("dm3_completed", 0) for lane in lanes)
    positives = sum(lane_metrics.get(lane, {}).get("positive", 0) for lane in lanes)
    rate = (positives / n) if n >= DM3_COMPLETED_MIN_FOR_RATE else None
    return {"dm3_completed": n, "positive": positives, "positive_rate": rate}


def evaluate_phasing_recommendation(lane_metrics: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Decide whether the inventory warrants an ``icp_phasing`` escalation.

    Returns a dict with:
        - escalate         : bool — open the queue row?
        - recommended      : str  — one of ``keep_both`` / ``phase_out_icp2``
                                    / ``double_down_icp1``
        - rationale        : str  — human-readable summary the operator reads
        - triggers         : list[str] — which condition(s) fired

    The rules (EITHER trigger is sufficient to escalate ``phase_out_icp2``):
        Gate: ``ICP2_total.dm3_completed >= DM3_COMPLETED_MIN_FOR_RATE``
              (we need a real rate to evaluate at all)
        Trigger A: ``ICP2_total.positive_rate < ICP2_LOW_RATE_THRESHOLD``
                   (strict <; exactly 0.05 does NOT fire)
        Trigger B: ``ICP1_total`` has a usable rate AND
                   ``icp1_rate / icp2_rate >= ICP1_VS_ICP2_RATE_RATIO``
                   (inclusive >=; exactly 2.0 DOES fire; skipped when icp2_rate=0)

    ICP aggregates: ICP 1 = ``ICP1_LANES`` (enterprise_mode only); ICP 2 =
    ``ICP2_LANES`` (target_company_mode + legacy). Pinned to the production
    ICP-2 detection rule in ``workflows.weekly_prospect.enforce_icp_lane_geo``.
    """
    triggers: list[str] = []

    icp2 = _aggregate(lane_metrics, ICP2_LANES)
    icp2_n = icp2["dm3_completed"]
    icp2_rate = icp2["positive_rate"]

    icp1 = _aggregate(lane_metrics, ICP1_LANES)
    icp1_n_total = icp1["dm3_completed"]
    icp1_rate = icp1["positive_rate"]

    if icp2_n < DM3_COMPLETED_MIN_FOR_RATE:
        return {
            "escalate": False,
            "recommended": "keep_both",
            "rationale": (
                f"ICP 2 DM3-completed pool ({icp2_n}) below evidence threshold "
                f"({DM3_COMPLETED_MIN_FOR_RATE}); cannot trust positive_rate yet."
            ),
            "triggers": [],
            "icp1_aggregate": icp1,
            "icp2_aggregate": icp2,
        }

    if icp2_rate is not None and icp2_rate < ICP2_LOW_RATE_THRESHOLD:
        triggers.append(
            f"ICP2_total.positive_rate={icp2_rate:.4f} < {ICP2_LOW_RATE_THRESHOLD}"
        )
    if icp1_rate is not None and icp2_rate is not None and icp2_rate > 0:
        ratio = icp1_rate / icp2_rate
        if ratio >= ICP1_VS_ICP2_RATE_RATIO:
            triggers.append(
                f"icp1_rate/icp2_rate={ratio:.2f} >= {ICP1_VS_ICP2_RATE_RATIO}"
            )

    if not triggers:
        return {
            "escalate": False,
            "recommended": "keep_both",
            "rationale": (
                f"ICP 2 N={icp2_n}, rate={icp2_rate}. ICP 1 N={icp1_n_total}, "
                f"rate={icp1_rate}. No phasing trigger fired."
            ),
            "triggers": [],
            "icp1_aggregate": icp1,
            "icp2_aggregate": icp2,
        }

    recommended = "phase_out_icp2"
    rationale = (
        f"ICP 2 positive_rate={icp2_rate} on N={icp2_n} vs "
        f"ICP 1 positive_rate={icp1_rate} on N={icp1_n_total}. "
        f"Triggers: {'; '.join(triggers)}."
    )
    return {
        "escalate": True,
        "recommended": recommended,
        "rationale": rationale,
        "triggers": triggers,
        "icp1_aggregate": icp1,
        "icp2_aggregate": icp2,
    }


def _operator_local_monday(today: date | None = None) -> date:
    """Return the Monday of the current operator-local week (America/Lima).

    The week-anchor TZ is operator-local rather than UTC so a cron straddling
    UTC midnight on a Sunday/Monday boundary stays on the same operator-week.
    America/Lima (UTC-5) has no DST so the offset is stable year-round.
    """
    from zoneinfo import ZoneInfo

    if today is None:
        today = datetime.now(ZoneInfo(WEEK_ANCHOR_TZ)).date()
    return today - timedelta(days=today.weekday())


def _idempotency_key_for(today: date) -> str:
    """Idempotency key for the weekly ``icp_phasing`` row.

    Pinned to the operator-local Monday of the run-week so re-runs on the
    same week are no-ops, but a fresh decision opens at the next week
    boundary. ``today`` should already be expressed in the operator TZ
    when passed in by callers.
    """
    monday = today - timedelta(days=today.weekday())
    return f"icp_phasing|{monday.isoformat()}"


def open_icp_phasing_escalation(
    recommendation: dict[str, Any],
    lane_metrics: dict[str, dict[str, Any]],
    *,
    today: date,
    attio: AttioClient | None = None,
) -> dict[str, Any]:
    """Open (or refresh) the ``icp_phasing`` configuration_decision row.

    Idempotent on (configuration_decision, icp_phasing, weekly key). Returns
    the Attio row dict ``escalate()`` returned.
    """
    deadline = today + timedelta(days=ESCALATION_DEADLINE_DAYS)

    options = [
        {
            "key": "keep_both",
            "label": "Keep both ICPs in funnel",
            "description": "No change. Continue current lane allocation.",
        },
        {
            "key": "phase_out_icp2",
            "label": "Phase out ICP 2 (midmarket/legacy)",
            "description": "Stop new ICP 2 prospects; finish in-flight cadences.",
        },
        {
            "key": "double_down_icp1",
            "label": "Double down on ICP 1",
            "description": "Increase ICP 1 weekly target; ICP 2 stays paused.",
        },
    ]

    payload = {
        "question": "Should we phase out ICP 2 from the funnel?",
        "options": options,
        "recommended_option": recommendation["recommended"],
        "default_on_expiry": "keep_both",
        "rationale": recommendation["rationale"],
        "lane_metrics": lane_metrics,
        "icp1_aggregate": recommendation.get("icp1_aggregate"),
        "icp2_aggregate": recommendation.get("icp2_aggregate"),
        "triggers": recommendation["triggers"],
        "opened_by": "scripts.gtm_attio_inventory.main",
    }

    return escalate(
        type="configuration_decision",
        decision_key=DECISION_KEY,
        idempotency_key=_idempotency_key_for(today),
        payload=payload,
        deadline=deadline,
        attio=attio,
    )


# ============================================================
# Inventory orchestration
# ============================================================


def _expected_min_list_entries() -> int:
    """Per-environment floor for the partial-pull sanity gate.

    Reads ``GTM_INVENTORY_EXPECTED_MIN`` when set; otherwise falls back to
    ``DEFAULT_EXPECTED_MIN_LIST_ENTRIES``. Non-int values raise so the
    misconfiguration is visible (§0 #9 fail-loud).
    """
    raw = os.environ.get(EXPECTED_MIN_LIST_ENTRIES_ENV, "").strip()
    if not raw:
        return DEFAULT_EXPECTED_MIN_LIST_ENTRIES
    return int(raw)  # int() raises ValueError on garbage — intentionally loud


def assess_data_quality(inventory: dict[str, Any], *, expected_min: int) -> dict[str, Any]:
    """Decide whether the snapshot is clean enough to drive an escalation.

    Returns ``{abstain: bool, reasons: list[str]}``. Reasons:
        - ``population_below_floor`` — fewer entries than ``expected_min``
          (partial Attio pull / outage suspect)
        - ``unknown_lane_fraction_high`` — more than ``UNKNOWN_LANE_ABSTAIN_FRACTION``
          of DM3-completed entries have no scoring_lane.
    """
    reasons: list[str] = []
    total_entries = inventory["totals"]["list_entries"]
    if total_entries < expected_min:
        reasons.append(
            f"population_below_floor (got {total_entries} < expected_min {expected_min})"
        )

    by_lane = inventory["by_lane"]
    unknown = by_lane.get(UNKNOWN_LANE_BUCKET, {})
    unknown_dm3 = unknown.get("dm3_completed", 0)
    total_dm3 = sum(b.get("dm3_completed", 0) for b in by_lane.values())
    if total_dm3 > 0:
        frac = unknown_dm3 / total_dm3
        if frac > UNKNOWN_LANE_ABSTAIN_FRACTION:
            reasons.append(
                f"unknown_lane_fraction_high ({unknown_dm3}/{total_dm3}="
                f"{frac:.4f} > {UNKNOWN_LANE_ABSTAIN_FRACTION})"
            )

    return {"abstain": bool(reasons), "reasons": reasons}


def build_inventory(
    *,
    attio: AttioClient,
    list_id: str,
) -> dict[str, Any]:
    """Pull live Attio data and assemble the inventory snapshot."""
    entries = attio.query_list_entries(list_id=list_id, limit=100_000)
    companies = attio.search_companies(filter_=None, limit=100_000)
    deals = attio.search_deals(filter_=None, limit=10_000)

    lane_metrics = compute_lane_metrics(entries)
    stages = stage_distribution(entries)
    responses = response_distribution(entries)
    recommendation = evaluate_phasing_recommendation(lane_metrics)

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "list_id": list_id,
        "totals": {
            "list_entries": len(entries),
            "companies": len(companies),
            "deals": len(deals),
        },
        "by_lane": lane_metrics,
        "by_stage": stages,
        "by_response_classification": responses,
        "phasing_recommendation": recommendation,
    }


# ============================================================
# CLI
# ============================================================


def _run_inventory(*, out: str | None, dry_run: bool, no_emit_empty: bool, week_anchor: str | None) -> int:
    """Body of the CLI separated from click decorators for testability and
    typed exit-code control. Caller wraps this in the click outer."""
    import httpx  # local import: keep top-level deps explicit

    list_id = os.environ.get("ATTIO_LIST_ID", "").strip()
    if not list_id:
        click.echo(
            "ERROR: ATTIO_LIST_ID env var unset. Cannot inventory the LinkedIn "
            "Outreach list.",
            err=True,
        )
        return EXIT_CONFIG_MISSING

    if week_anchor:
        try:
            anchor_date = date.fromisoformat(week_anchor)
        except ValueError as exc:
            click.echo(f"ERROR: --week-anchor must be YYYY-MM-DD: {exc}", err=True)
            return EXIT_CONFIG_MISSING
    else:
        anchor_date = _operator_local_monday()

    out_path = Path(out or f"exports/gtm-inventory-{anchor_date.isoformat()}.json")
    expected_min = _expected_min_list_entries()

    try:
        with AttioClient() as attio:
            inventory = build_inventory(attio=attio, list_id=list_id)

            # Surface dirty-data signals on the inventory itself so the JSON
            # record carries the abstain reasoning for forensic review.
            quality = assess_data_quality(inventory, expected_min=expected_min)
            inventory["data_quality"] = quality

            if inventory["totals"]["list_entries"] == 0 and no_emit_empty:
                click.echo("List empty; --no-emit-empty set; skipping JSON write.")
            else:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(json.dumps(inventory, indent=2, default=str))
                click.echo(
                    f"Wrote {out_path} "
                    f"({inventory['totals']['list_entries']} entries, "
                    f"{inventory['totals']['companies']} companies, "
                    f"{inventory['totals']['deals']} deals)"
                )

            if quality["abstain"]:
                click.echo(
                    "WARN: data-quality gate ABSTAINED: "
                    f"{'; '.join(quality['reasons'])}. icp_phasing escalation skipped.",
                    err=True,
                )
                return EXIT_HEURISTIC_ABSTAINED

            recommendation = inventory["phasing_recommendation"]
            if dry_run:
                click.echo(
                    f"--dry-run set; would-have-escalated={recommendation['escalate']} "
                    f"recommendation={recommendation['recommended']}"
                )
                return EXIT_OK

            if not recommendation["escalate"]:
                click.echo(
                    f"No icp_phasing escalation: {recommendation['rationale']}"
                )
                return EXIT_OK

            row = open_icp_phasing_escalation(
                recommendation,
                inventory["by_lane"],
                today=anchor_date,
                attio=attio,
            )
            row_id = row.get("id")
            record_id = (
                row_id.get("record_id") if isinstance(row_id, dict)
                else row.get("record_id", "")
            )
            click.echo(
                f"Opened icp_phasing escalation (record_id={record_id}); "
                f"recommended={recommendation['recommended']}"
            )

        return EXIT_OK

    except (UnknownEscalationType, EscalationSchemaError) as exc:
        click.echo(f"FATAL: escalation contract violated: {exc}", err=True)
        return EXIT_ATTIO_FAILURE
    except MissingAttioCredentials as exc:
        click.echo(f"FATAL: missing Attio credentials: {exc}", err=True)
        return EXIT_ATTIO_FAILURE
    except httpx.HTTPError as exc:
        click.echo(f"FATAL: Attio HTTP error: {exc}", err=True)
        return EXIT_ATTIO_FAILURE


@click.command()
@click.option(
    "--out",
    default=None,
    help="Output JSON path; defaults to exports/gtm-inventory-YYYY-MM-DD.json",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Skip the icp_phasing escalation, write the JSON only.",
)
@click.option(
    "--no-emit-empty",
    is_flag=True,
    default=False,
    help="Suppress the JSON file when the list contains zero entries.",
)
@click.option(
    "--week-anchor",
    default=None,
    help=(
        "Override the week-anchor date (YYYY-MM-DD) used for the idempotency "
        f"key and the default --out filename. Defaults to {WEEK_ANCHOR_TZ} "
        "Monday so cron straddling UTC midnight does not duplicate rows."
    ),
)
def main(out: str | None, dry_run: bool, no_emit_empty: bool, week_anchor: str | None) -> int:
    rc = _run_inventory(out=out, dry_run=dry_run, no_emit_empty=no_emit_empty, week_anchor=week_anchor)
    # Explicit sys.exit so Click forwards our typed exit code; returning rc
    # from a @click.command function is ignored by Click's runner.
    sys.exit(rc)


if __name__ == "__main__":
    sys.exit(main())
