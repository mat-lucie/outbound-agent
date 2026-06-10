#!/usr/bin/env python3
"""Weekly Data Quality Report.

Read-only Attio aggregator. Pure reads + one Data Quality Report row
written at the end. The slug authority for which alarms exist + their
P0/P1 tier lives in `models.data_quality_report`. Per §3.13 the script
is pinned in `tests/test_migration_writer_compliance.py::EXEMPT_SCRIPTS`;
adding any mutation beyond `write_report` must opt back into
`MigrationRunWriter`.

Exit codes: 0 clean, 1 P1, 70 P0 (consumer halts DM sends).
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from clients.attio import AttioClient  # noqa: E402
from models.data_quality_report import (  # noqa: E402
    P0_ALARM_SLUGS,
    P1_ALARM_SLUGS,
    DQRMetrics,
    DQRReport,
    exit_code_for_severity,
    manual_reply_gap_threshold,
)

# Failure-detail kind slugs MigrationRunWriter emits when a back-pointer
# PATCH fails (§3.13 forensics). Pinning here so a schema rename surfaces
# as a test failure rather than a silent under-count.
BACK_POINTER_FAILURE_KINDS = frozenset({
    "back_pointer",
    "back_pointer_failure",
    "missing_migration_run_record_id",
})

DEFAULT_PERIOD_DAYS = 7
DATA_QUALITY_REPORT_SLUG = "data_quality_report"
OPERATOR_REVIEW_QUEUE_SLUG = "operator_review_queue"
MIGRATION_RUN_SLUG = "migration_run"
RECLASSIFICATION_RUN_SLUG = "reclassification_run"
DAILY_RUN_SLUG = "daily_run"
LIST_ENTRY_LIMIT = 5000


# --- Attio query helpers ------------------------------------------

def _query_object_records(
    attio: AttioClient,
    slug: str,
    *,
    filter_: dict | None = None,
    limit: int = 1000,
) -> list[dict]:
    body: dict = {"limit": limit}
    if filter_:
        body["filter"] = filter_
    data = attio._request(
        "POST", f"/objects/{slug}/records/query", json=body,
    )
    return data.get("data", []) or []


def _record_first_value(record: dict, slug: str):
    """Read the first value of a slug from an object-record's `values`
    shape. Distinct from `AttioClient._extract_value` which targets
    list-entry values — the object-record path returns plain text /
    number for the attrs this script consumes (no select-fallback path
    needed)."""
    items = (record.get("values", {}) or {}).get(slug) or []
    if not items:
        return None
    item = items[0]
    if isinstance(item, dict):
        return item.get("value")
    return item


def _count_open_queue_rows(attio: AttioClient, type_slug: str) -> int:
    rows = _query_object_records(
        attio, OPERATOR_REVIEW_QUEUE_SLUG,
        filter_={"type": {"$eq": type_slug}, "status": {"$eq": "open"}},
    )
    return len(rows)


# --- Per-metric collectors ----------------------------------------

def _collect_p0_alarm_counts(attio: AttioClient) -> dict[str, int]:
    return {slug: _count_open_queue_rows(attio, slug) for slug in P0_ALARM_SLUGS}


def _collect_p1_alarm_counts(attio: AttioClient) -> dict[str, int]:
    return {slug: _count_open_queue_rows(attio, slug) for slug in P1_ALARM_SLUGS}


def _collect_nurture_silent_skipped_7d(
    attio: AttioClient, *, since: date,
) -> tuple[int, int]:
    """Sum + parse-error count for §3.18 nurture skip rollup.

    Malformed numeric values are counted separately (not silently
    dropped) so the operator can tell "we recorded 0 skips" from "we
    saw 50 rows but couldn't parse them" — §0 #5.
    """
    rows = _query_object_records(
        attio, DAILY_RUN_SLUG,
        filter_={"run_date": {"$gte": since.isoformat()}},
        limit=LIST_ENTRY_LIMIT,
    )
    total = 0
    parse_errors = 0
    for row in rows:
        value = _record_first_value(row, "nurture_silent_skipped_count")
        if value is None:
            continue
        try:
            total += int(value)
        except (TypeError, ValueError):
            parse_errors += 1
    return total, parse_errors


def _collect_back_pointer_failures_7d(
    attio: AttioClient, *, since: date,
) -> int:
    """Count Migration Run rows whose `failure_details_pointer` mentions
    one of the back-pointer failure kinds emitted by MigrationRunWriter.

    Substring-match against the pinned `BACK_POINTER_FAILURE_KINDS` set
    rather than parsing JSON — failure_details is JSON-as-string in
    practice but downstream payload schema drift would otherwise
    silently zero this metric. The pinned set surfaces drift as a
    fail-loud test once MigrationRunWriter changes its slugs.
    """
    # `completed` is a timestamp attr; Attio v2 rejects ISO strings with
    # `+00:00` offset suffix on timestamp filters — must use `Z`. Convert
    # the date-typed `since` to an ISO-8601 UTC timestamp with Z.
    since_ts = f"{since.isoformat()}T00:00:00Z"
    rows = _query_object_records(
        attio, MIGRATION_RUN_SLUG,
        filter_={"completed": {"$gte": since_ts}},
        limit=LIST_ENTRY_LIMIT,
    )
    count = 0
    for row in rows:
        details = _record_first_value(row, "failure_details_pointer")
        if not details:
            continue
        details_str = str(details).lower()
        if any(kind in details_str for kind in BACK_POINTER_FAILURE_KINDS):
            count += 1
    return count


def _collect_legacy_archaeology_count(attio: AttioClient) -> int:
    """LinkedIn Outreach list entries stamped with a §3.10 archaeology
    sentinel. These rows must NEVER receive a send; pool size is a
    leading indicator of how much of the cohort is locked out.

    Raises `RuntimeError` when `ATTIO_LIST_ID` is unset. Silent zero
    here would mean a misconfigured deployment shows
    `legacy_archaeology_pool_count=0` on the dashboard while never
    having queried — the §0 #9 / §0 #5 failure mode this PR exists to
    prevent.
    """
    list_id = os.environ.get("ATTIO_LIST_ID", "")
    if not list_id:
        raise RuntimeError(
            "ATTIO_LIST_ID env var is required for "
            "legacy_archaeology_pool_count; silent zero would mask a "
            "misconfigured deployment (§0 #5 / §0 #9)."
        )
    raw_entries = attio.query_list_entries(list_id=list_id, limit=LIST_ENTRY_LIMIT)
    sentinels = {"legacy_inferred_by_archaeology", "legacy_pure_unknown"}
    return sum(
        1 for raw in raw_entries
        if AttioClient.parse_entry(raw).get("experiment_id_frozen_at") in sentinels
    )


# --- Report assembly ----------------------------------------------

def build_report(
    attio: AttioClient,
    *,
    period_days: int = DEFAULT_PERIOD_DAYS,
    today: date | None = None,
) -> DQRReport:
    # today is caller-supplied so tests can pin time; production
    # derives it from operator-TZ in the slash-command wiring.
    period_end = today or date.today()
    period_start = period_end - timedelta(days=period_days)

    p0 = _collect_p0_alarm_counts(attio)
    p1 = _collect_p1_alarm_counts(attio)
    nurture_total, nurture_parse_errors = _collect_nurture_silent_skipped_7d(
        attio, since=period_start,
    )
    back_pointer = _collect_back_pointer_failures_7d(attio, since=period_start)
    archaeology = _collect_legacy_archaeology_count(attio)
    starvation_open = _count_open_queue_rows(attio, "pipeline_starvation")

    metrics = DQRMetrics(
        cohort_tagging_regression_count=p0["cohort_tagging_regression"],
        write_owner_invariant_violated_count=p0["write_owner_invariant_violated"],
        migration_idempotency_regression_count=p0["migration_idempotency_regression"],
        manual_reply_classification_gap_count=p1["manual_reply_classification_gap"],
        nurture_silent_skipped_count_7d=nurture_total,
        nurture_count_parse_errors_7d=nurture_parse_errors,
        pipeline_starvation_open_count=starvation_open,
        back_pointer_failures_count_7d=back_pointer,
        legacy_archaeology_pool_count=archaeology,
    )

    p0_fired = [s for s, c in p0.items() if c > 0]
    # P1 fires at-threshold (>=); §10's halt-on-10 fires at the same
    # count. The DQR row co-fires with §10 so the operator sees the
    # slug name attached to the halt, not just a bare halt.
    p1_threshold = manual_reply_gap_threshold()
    p1_fired = [s for s, c in p1.items() if c >= p1_threshold]

    return DQRReport(
        run_id=uuid.uuid4().hex[:12],
        generated_at=datetime.now(UTC).isoformat(),
        period_start=period_start.isoformat(),
        period_end=period_end.isoformat(),
        metrics=metrics,
        p0_alarms_fired=p0_fired,
        p1_alarms_fired=p1_fired,
    )


def render_report(report: DQRReport) -> str:
    m = report.metrics
    # Severity + firing slugs go FIRST so a P0 doesn't get buried below
    # the metric table on operator scan.
    head = [
        f"Data Quality Report — {report.run_id} (severity: {report.exit_severity()})",
    ]
    if report.has_p0():
        head.append(f"*** P0 BLOCKED: {', '.join(report.p0_alarms_fired)} ***")
    if report.has_p1():
        head.append(f"P1 fired: {', '.join(report.p1_alarms_fired)}")
    body = [
        "",
        f"period: {report.period_start} → {report.period_end}",
        f"generated: {report.generated_at}",
        "",
        "P0 alarms (HALT DM sends until resolved):",
        f"  cohort_tagging_regression_count: {m.cohort_tagging_regression_count}",
        f"  write_owner_invariant_violated_count: {m.write_owner_invariant_violated_count}",
        f"  migration_idempotency_regression_count: {m.migration_idempotency_regression_count}",
        "",
        "P1 alarms (visible; do not halt):",
        f"  manual_reply_classification_gap_count: {m.manual_reply_classification_gap_count}",
        "",
        "Observability (dashboard only):",
        f"  nurture_silent_skipped_count_7d: {m.nurture_silent_skipped_count_7d}",
        f"  nurture_count_parse_errors_7d: {m.nurture_count_parse_errors_7d}",
        f"  pipeline_starvation_open_count: {m.pipeline_starvation_open_count}",
        f"  back_pointer_failures_count_7d: {m.back_pointer_failures_count_7d}",
        f"  legacy_archaeology_pool_count: {m.legacy_archaeology_pool_count}",
    ]
    return "\n".join(head + body)


_ALARMS_FIRED_NONE_SENTINEL = "none"


def write_report(attio: AttioClient, report: DQRReport) -> str:
    """Persist the report as a Data Quality Report Attio row.

    Raises `RuntimeError` if Attio returns a 2xx without a usable
    `record_id` — the write looked successful but the row is
    unreachable, exactly the silent-success case §0 #9 prohibits.

    Empty alarm lists serialize as the `"none"` sentinel rather than
    an empty string so a downstream consumer that `.split(",")`s the
    text doesn't get a length-1 list with an empty element.
    """
    m = report.metrics
    attrs: dict = {
        "run_id": report.run_id,
        "generated_at": report.generated_at,
        "period_start": report.period_start,
        "period_end": report.period_end,
        "cohort_tagging_regression_count": m.cohort_tagging_regression_count,
        "write_owner_invariant_violated_count": m.write_owner_invariant_violated_count,
        "migration_idempotency_regression_count": m.migration_idempotency_regression_count,
        "manual_reply_classification_gap_count": m.manual_reply_classification_gap_count,
        "nurture_silent_skipped_count_7d": m.nurture_silent_skipped_count_7d,
        "nurture_count_parse_errors_7d": m.nurture_count_parse_errors_7d,
        "pipeline_starvation_open_count": m.pipeline_starvation_open_count,
        "back_pointer_failures_count_7d": m.back_pointer_failures_count_7d,
        "legacy_archaeology_pool_count": m.legacy_archaeology_pool_count,
        "p0_alarms_fired": ",".join(report.p0_alarms_fired) or _ALARMS_FIRED_NONE_SENTINEL,
        "p1_alarms_fired": ",".join(report.p1_alarms_fired) or _ALARMS_FIRED_NONE_SENTINEL,
        "report_text": render_report(report),
    }
    body = {"data": {"values": attrs}}
    data = attio._request(
        "POST", f"/objects/{DATA_QUALITY_REPORT_SLUG}/records", json=body,
    )
    record = data.get("data", data)
    record_id = (
        record.get("id", {}).get("record_id")
        if isinstance(record.get("id"), dict)
        else record.get("record_id")
    )
    if not record_id:
        raise RuntimeError(
            f"Attio data_quality_report write returned no record_id; "
            f"response shape: {data!r}"
        )
    return str(record_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--period-days",
        type=int,
        default=DEFAULT_PERIOD_DAYS,
        help=f"Lookback window in days (default: {DEFAULT_PERIOD_DAYS}).",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Compute + print the report without writing to Attio.",
    )
    args = parser.parse_args(argv)

    if args.period_days < 1:
        print("error: --period-days must be >= 1", file=sys.stderr)
        return 2

    try:
        attio = AttioClient()
    except KeyError:
        print("error: ATTIO_API_KEY env var not set", file=sys.stderr)
        return 2

    report = build_report(attio, period_days=args.period_days)
    print(render_report(report))

    if not args.no_write:
        record_id = write_report(attio, report)
        print(f"\nAttio: data_quality_report row {record_id}")

    # Log the asdict snapshot for forensics so a script-only run still
    # produces structured output a future audit can replay.
    print(f"\nsnapshot: {asdict(report)}", file=sys.stderr)

    return exit_code_for_severity(report.exit_severity())


if __name__ == "__main__":
    sys.exit(main())
