"""Daily check workflow: send connections, sequence DMs, detect responses."""

import os
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Literal

import click
import httpx

from clients.attio import AttioClient

if TYPE_CHECKING:
    from clients.crm.base import CRMProvider
from clients.google_sheets import profiles_per_launch, write_prospects_to_sheet
from clients.outreach_config import load_outreach_config
from clients.pb_envelope import (
    INVITE_OPTIMISTIC_ADVANCE,
    PBRunFailed,
    PBRunTimeout,
    compute_invite_outcome,
    has_scraper_dedup_marker,
    parse_send_outcome,
    should_advance_batch,
)
from clients.phantombuster import PhantomBusterClient
from models.business_calendar import business_days_between, operator_today
from models.campaign import (
    MessageStep,
    MissingMessageError,
    Persona,
    get_industry_label,
    get_message,
    personalize,
)
from models.experiment import get_current_experiment_id
from models.pipeline import STAGE_RANK, PipelineStage, dm_step_int, is_invite_eligible, is_send_eligible  # noqa: E501
from models.resolution import (
    MissingLanguageError,
    compute_next_eligible_send_date,
    resolve_language,
)
from workflows import recheck_cache
from workflows.daily_check_helpers import (
    UnresolvedPlaceholderError,  # re-exported; consumed by tests + cli.py
    _assert_no_unresolved_placeholders,
    _dedupe_by_linkedin_url,
    _fresh_csv_name,
    _get_all_entries_parsed,
    _get_all_entries_with_raw,
    _normalize_linkedin_url,
    _pb_session_args,
    _resolve_degree_check_backend,
    build_sales_nav_launch_args,
    preflight_legacy_profile_scraper,
)

# Intentional re-exports — silence F401. UnresolvedPlaceholderError is
# imported by cli.py + email_campaign tests; can_send_messages is patched
# via `workflows.daily_check.can_send_messages` in integration tests.
__all__ = ["UnresolvedPlaceholderError", "can_send_messages"]
from workflows.audit import AuditLogger
from workflows.consistency_sweep import run_company_tally_consistency_sweep
from workflows.content_guard import assert_content_replaced
from workflows.daily_run import DailyRun
from workflows.dm_sequencer import NEXT_STAGE, STAGE_FOR_DM, get_pending_dms
from workflows.escalation import escalate
from workflows.pb_advance_gate import emit_pb_inmail_dead_end, emit_pb_silent_no_op
from workflows.pre_invite_check import (
    _IMMUTABLE_FROZEN_AT_VALUES,
    SALES_NAV_HAS_PENDING_INVITATION_COL,
    _pre_invite_degree_check,
)
from workflows.quality_gate import classify_response
from workflows.record_cache import RecordCache
from workflows.safety_limits import (
    can_send_connections,
    can_send_messages,  # re-exported; tests patch via daily_check.can_send_messages
    get_remaining,
    get_status,
    record_connections,
    record_visits,
)
from workflows.schema_preflight import assert_dm_writer_schema
from workflows.throttle import (
    DEFAULT_THROTTLE_WINDOW_DAYS,
    company_throttle_permits,
    ensure_throttle_policy_decision_opened,
)

# PR-17 fold-in: starvation signal alphabet is wire-format (Attio select
# column) so Literal is preferred over Enum — keeps the YAML the source of
# truth for the values, mirrors what other modules do with SendKind.
StarvationSignal = Literal["healthy", "low_dm1", "low_dm2", "low_dm3", "multi_low"]

# Outreach operational knobs (config/outreach.yaml). Used for the invite-queue
# lane priority in run_connection_requests.
_OUTREACH = load_outreach_config()

# Acceptance detection (Phase 0) only scans CONNECTION_SENT profiles whose
# last_contact_date falls within this window. Invites older than ~2 weeks
# rarely convert, and scanning them all doesn't scale with pipeline size.
ACCEPTANCE_CHECK_WINDOW_DAYS = 14

# L1-2: CONNECTION_SENT rows whose last_contact_date is older than this
# are permanently beyond the acceptance-detection window and should be
# surfaced to the operator. One aggregated escalation per day — no stage
# changes, visibility only.
STALE_CONNECTION_SENT_ESCALATE_DAYS = 45

# 2026-06-11 oversized-launch fix: Phase 0 passes the whole stale batch as
# numberOfProfilesPerLaunch, but the Sales Navigator Profile Scraper's
# argument schema caps that field at 150 (verified 2026-06-11 via PB
# scripts/fetch, script id 11108) and PB rejects the ENTIRE launch above it
# ("numberOfProfilesPerLaunch => is more than maximum"). Phase 0 then goes
# BLIND for the run, and because BLIND runs skip recheck_cache stamping the
# same oversized launch recurs every run until the stale set shrinks — hit
# in prod 2026-06-11 with 197 stale profiles. Cap each run's scrape batch
# well under the schema max; the deferred tail stays unstamped in the
# recheck cache and rotates into the next run's batch. 50 (not 150) keeps
# the per-run LinkedIn profile-visit volume at what the retired visit
# budget (MAX_VISITS_PER_DAY) allowed — one run must not burn a week of
# account-safety headroom clearing a backlog.
PHASE0_MAX_PROFILES_PER_LAUNCH = 50

# PR-209 (Leak A): of each Phase-0 scrape batch, reserve this many slots for the
# OLDEST in-window invites (about to exit ACCEPTANCE_CHECK_WINDOW_DAYS — a
# last-chance check so a late accepter isn't lost forever); the remaining budget
# is filled NEWEST-first. Fresh acceptances cluster in the first days
# post-invite and are the highest-value to detect + DM promptly. Pure
# oldest-first starved them: on a backlog day the newest invites were deferred
# last, so confirmed acceptances sat undetected behind the older tail. Stays
# under the cap → no extra profile-visit volume. Tunable; must be <
# PHASE0_MAX_PROFILES_PER_LAUNCH.
PHASE0_EXPIRING_RESERVE = 15

# PR-208 stale-degree fix: the Sales Nav scraper's connectionDegree LAGS the
# live graph (returns "2nd" for a freshly-accepted, now-1st-degree connection),
# so the fix below re-scrapes every non-"1st" CONNECTION_SENT row daily instead
# of letting a cached "2nd" suppress it for RECHECK_TTL_DAYS. That raises the
# steady-state CONNECTION_SENT scrape volume, which would let the
# CONNECTION_SENT-priority slice consume the whole per-run cap and starve the
# Defect-2 PROSPECT sweep to ~0 every day. Reserve a floor of scrape slots for
# stale PROSPECTs (when any exist) so the PROSPECT acceptance sweep keeps making
# progress. CONNECTION_SENT still takes the lion's share (cap − reserve) plus
# any reserved slots the PROSPECT pool doesn't use.
PHASE0_PROSPECT_MIN_BUDGET = 10

# PR-208 acceptance-reconcile alarm: a CONNECTION_SENT row the SN scrape reports
# as invite-resolved (hasPendingInvitation="false") but still NOT 1st-degree is
# either a declined/withdrawn invite OR an accepted invite whose degree the
# scraper is mis-reading. Both warrant a human cross-reference against LinkedIn
# "My Network". Only surface rows whose invite is at least this old, so invites
# that just resolved (and may flip to 1st on the next scrape) don't spam the
# queue.
SUSPECTED_STALE_MIN_AGE_DAYS = 4


def _is_blocked_by_stored_floor(
    attrs: dict,
    today: date,
    audit_logger: "AuditLogger | None" = None,
) -> bool:
    """Return True if `attrs.next_eligible_send_date` is in the future
    (caller MUST skip the row to honor §3.1).

    PR-12 (B-PD-003) §3.1 protection: the stored floor is a forward-only
    cadence boundary written by `run_dm_sequencing` after each confirmed
    send. Honoring the stored value is strictly safer than recomputing
    locally because `advance_off_weekend` (used at write time) is
    forward-only, while `dm_sequencer.shift_off_weekend` (used by the
    local floor) can shift Sat backward to Fri. Respecting the stored
    floor therefore never advances a send earlier — only blocks the
    rare case where the floor is later.

    A malformed stored value (anything `date.fromisoformat` rejects)
    falls through to the local eligibility check and emits a
    `malformed_next_eligible_send_date` audit event so operators can
    triage the upstream writer. Per §0 #9, the audit event makes the
    silent fall-through observable; per the queue-construction tight
    loop, NO Operator Review Queue row is opened here — tomorrow's run
    sees a clean state once the upstream writer is fixed, and an
    aggregated `attio_data_corruption` queue row is a follow-up PR.
    """
    stored_floor_raw = attrs.get("next_eligible_send_date")
    if not stored_floor_raw:
        return False
    try:
        stored_floor = date.fromisoformat(str(stored_floor_raw)[:10])
    except ValueError:
        if audit_logger is not None:
            audit_logger.event(
                "malformed_next_eligible_send_date",
                record_id=str(attrs.get("record_id", "")),
                raw_value=str(stored_floor_raw)[:50],
            )
        return False
    return today < stored_floor


def _company_id_for_prospect(
    attio: AttioClient,
    record_id: str,
) -> str | None:
    """Look up the prospect's linked company_id from the AttioClient
    cache populated by `extract_record_info` (and therefore
    `RecordCache.get`).

    PR-13 throttle integration: callers MUST have already invoked
    `cache.get(record_id)` for this prospect so the cache is primed.
    Returns None when the prospect has no linked company (which is
    treated as permissively-unthrottled per §3.8).
    """
    return attio._person_to_company.get(record_id)


def _check_company_throttle_or_skip(
    attrs: dict,
    *,
    attio: AttioClient,
    today: date,
    audit_logger: "AuditLogger | None" = None,
    dry_run: bool = False,
) -> bool:
    """PR-13 (B-PD-002) throttle gate.

    Returns True iff the prospect's company has had NO outbound contact
    within the throttle window (default 30d per §3.8). When the company
    IS throttled, opens a `company_throttled` Operator Review Queue row
    so the operator can see which prospect was skipped and why.

    Idempotency: keyed on `(record_id, throttle_date)` so each prospect
    generates at most one queue row per day even on multi-step retries.

    Under ``dry_run`` the skip DECISION is preserved (still returns
    False, still logs the audit event) but the `company_throttled` queue
    row is NOT written to Attio — a preview must stay read-only, mirroring
    how cli.py gates PB launches + AttioWriter flips on ``mode.is_dry_run()``.
    """
    record_id = str(attrs.get("record_id") or "")
    company_id = _company_id_for_prospect(attio, record_id)
    # Same-person exemption is DM-path only: thread the candidate's
    # identity for engaged (post-PROSPECT) rows. A PROSPECT-stage row
    # with a self-stamp is corrupted state (its invite went out but the
    # stage advance never landed) — the 14-day quarantine is the
    # correct outcome there, so the invite path never threads.
    engaged = attrs.get("stage") not in (None, "", PipelineStage.PROSPECT.value)
    if company_throttle_permits(
        company_id, today, attio=attio, audit_logger=audit_logger,
        person_record_id=(record_id or None) if engaged else None,
        person_dm_step=int(attrs.get("dm_step") or 0) if engaged else None,
    ):
        return True

    # PR-13 fold-in: emit the actual window_days the helper enforced
    # (currently DEFAULT_THROTTLE_WINDOW_DAYS, but threading the value
    # through the queue payload prevents triage mislead once PR-39's
    # configuration_decision resolver lands and starts varying it).
    if not dry_run:
        escalate(
            type="company_throttled",
            idempotency_key=f"company-throttled|{record_id}|{today.isoformat()}",
            payload={
                "record_id": record_id,
                "company_id": company_id or "",
                "throttle_date": today.isoformat(),
                "window_days": DEFAULT_THROTTLE_WINDOW_DAYS,
            },
            attio=attio,
        )
    if audit_logger is not None:
        audit_logger.event(
            "company_throttled_skip",
            record_id=record_id,
            company_id=company_id or "",
            throttle_date=today.isoformat(),
        )
    return False


def _write_company_throttle_tally(
    *,
    attio: AttioClient,
    company_id: str | None,
    person_record_id: str,
    step_label: str,
    experiment_id: str | None,
    today: date,
    writer_module: str = "workflows.daily_check.run_dm_sequencing",
    audit_logger: "AuditLogger | None" = None,
    escalate_failures: list | None = None,
    person_advance_ok: bool | None = None,
) -> None:
    """PR-13 (§3.15 write-owner): record the latest outbound contact
    to `company_id` after a confirmed-send.

    This is the SOLE writer for the four `companies` attributes
    `last_outreach_at`, `last_outreach_person_id`, `last_outreach_step`,
    `last_outreach_experiment_id`. The write happens AFTER each
    confirmed send so subsequent prospects evaluated within the same
    daily run see the updated throttle state (multi-thread ABM safety
    per Round-4 D32).

    `step_label` is the lowercase send step ("dm1"/"dm2"/"dm3" from
    `MessageStep.value`, or "invite" from the invite path). The Attio
    select options are uppercase per the manifest enumeration —
    normalize here so callers can pass whatever they already have.
    """
    if company_id is None:
        return
    # Manifest select options: CONNECTION_SENT | DM1 | DM2 | DM3.
    select_normalized = {
        "invite": "CONNECTION_SENT",
        "connection_note": "CONNECTION_SENT",
        "dm1": "DM1",
        "dm2": "DM2",
        "dm3": "DM3",
    }.get(step_label.lower())
    if select_normalized is None:
        # Unknown step — refuse to write a malformed select. This is
        # a programmer error (callers always pass one of five known
        # values per the docstring); raise a typed exception so the
        # next-day run fails loud rather than silently skipping the
        # throttle write. §0 #9 says missing data → typed error.
        raise ValueError(
            f"_write_company_throttle_tally: unknown step_label={step_label!r}; "
            f"expected one of dm1/dm2/dm3/invite/connection_note"
        )

    attrs_to_write: dict = {
        "last_outreach_at": datetime.combine(today, datetime.min.time(), tzinfo=UTC).isoformat(),
        "last_outreach_person_id": [{
            "target_object": "people",
            "target_record_id": person_record_id,
        }],
        "last_outreach_step": select_normalized,
        "last_outreach_experiment_id": experiment_id or "",
    }
    from clients.attio_writer import (
        AttioError,
        AttioMonotonicityViolation,
        AttioTerminalClassRegression,
        AttioWriter,
        UnauthorizedAttioWriteError,
        WriteIntent,
    )

    writer = AttioWriter(attio=attio)
    try:
        # Wave-2-B §3.15 bypass cleanup: route through AttioWriter so the
        # write-owner registry enforces `companies.last_outreach_*`
        # attribution. The previous bypass kept the throttle attrs in
        # the manifest but the registry check never fired in production.
        writer.apply(WriteIntent(
            object="companies",
            record_id=company_id,
            updates=attrs_to_write,
            prior_values={},
            writer_module=writer_module,
        ))
    except (UnauthorizedAttioWriteError,
            AttioMonotonicityViolation,
            AttioTerminalClassRegression):
        # Programmer-bug class — propagate. The throttle attrs are not
        # stage-typed so monotonicity / terminal-class shouldn't fire
        # here; if they do, that's a registry or attrs schema bug.
        raise
    except AttioError as exc:
        # AttioWriter exhausted retries / hit a permanent 4xx. It has
        # already DLQ'd and opened the `attio_write_failed` queue row
        # via _dlq_and_escalate, so we don't re-escalate here — that
        # would create a duplicate row with a different idempotency
        # key. Preserve the existing audit-logger event + escalate-
        # failures tally so the end-of-batch ERROR summary stays
        # informative.
        click.echo(
            f"  ⚠ _write_company_throttle_tally: AttioWriter failed for "
            f"company_id={company_id!r} person_record_id={person_record_id!r}: "
            f"{type(exc).__name__}: {exc}. Tally write lost this cycle; "
            f"caller's per-row loop continues.",
            err=True,
        )
        if escalate_failures is not None:
            escalate_failures.append({
                "site": "_write_company_throttle_tally",
                "company_id": company_id,
                "person_record_id": person_record_id,
                "error_class": type(exc).__name__,
            })
        if audit_logger is not None:
            audit_logger.event(
                "company_throttle_write_failed",
                company_id=company_id,
                person_record_id=person_record_id,
                error_class=type(exc).__name__,
                person_advance_ok=person_advance_ok,
            )
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        # Defense in depth: AttioWriter should narrow httpx into its
        # typed hierarchy, but if a raw httpx error ever leaks through
        # the AttioWriter path (e.g. inside its escalate-deferred-
        # import), keep the legacy escalate path so the throttle tally
        # failure is still operator-visible.
        try:
            escalate(
                type="attio_write_failed",
                idempotency_key=f"company-throttle-write|{company_id}|{today.isoformat()}",
                payload={
                    "object": "companies",
                    "record_id": company_id,
                    "attribute_writes": attrs_to_write,
                    "error_class": type(exc).__name__,
                    "error_msg": str(exc)[:500],
                    "retry_count": 0,
                },
                attio=attio,
            )
        except Exception as esc_exc:  # noqa: BLE001 — Wave-1.6.3 carve-out
            click.echo(
                f"  ⚠ escalate(attio_write_failed) in "
                f"_write_company_throttle_tally FAILED for "
                f"company_id={company_id!r} person_record_id={person_record_id!r} "
                f"[{type(esc_exc).__name__}]: {esc_exc}. Continuing batch.",
                err=True,
            )
            if escalate_failures is not None:
                escalate_failures.append({
                    "site": "_write_company_throttle_tally",
                    "company_id": company_id,
                    "person_record_id": person_record_id,
                    "error_class": type(esc_exc).__name__,
                })
        if audit_logger is not None:
            audit_logger.event(
                "company_throttle_write_failed",
                company_id=company_id,
                person_record_id=person_record_id,
                error_class=type(exc).__name__,
                person_advance_ok=person_advance_ok,
            )


def _finalize_confirmed_dm_send(
    *,
    attio: AttioClient,
    row: dict,
    step: "MessageStep",
    attrs_to_update: dict,
    list_id: str,
    today: date,
    today_str: str,
    experiment_id: str | None,
    audit_logger: "AuditLogger | None" = None,
    escalate_failures: list | None = None,
) -> int:
    """Post-confirmed-send per-row finalization: advance the person's
    list entry (or entries), then stamp the company throttle tally.

    Invariant (2026-06-09 desync design): the tally records reality —
    the DM was confirmed-sent — so it is written even when the person
    advance fails. A failed advance emits `dm_person_advance_desync`
    so the end-of-run consistency sweep converges the entry the same
    day; PR #170's throttle guard quarantines the row in the interim.
    Skipping the tally instead would leave the company unstamped and
    let a sibling at the same company through the throttle.

    Returns the number of entries successfully advanced.
    """
    updated = 0
    failed_entry_ids: list[str] = []
    for entry_id in row.get("entry_ids") or [row.get("entry_id")]:
        if not entry_id:
            continue
        ok = _attio_advance_with_escalation(
            attio=attio,
            entry_id=entry_id,
            entry_attributes=attrs_to_update,
            list_id=list_id,
            linkedin_url=row.get("linkedInUrl", ""),
            today=today_str,
            step_label=step.value,
            writer_module="workflows.daily_check.run_dm_sequencing",
            # Wave-2-B fix-up: use the row's real prior stage so the
            # monotonicity gate detects regressions instead of being
            # lied to; STAGE_FOR_DM[step] is only the fallback for
            # legacy row shapes that never carried an explicit stage.
            prior_stage=row.get("current_stage")
                        or row.get("stage")
                        or STAGE_FOR_DM[step].value,
            person_record_id=row.get("record_id"),
            audit_logger=audit_logger,
            escalate_failures=escalate_failures,
        )
        if ok:
            updated += 1
        else:
            failed_entry_ids.append(entry_id)

    person_record_id = row.get("record_id")
    company_id = (
        _company_id_for_prospect(attio, person_record_id)
        if person_record_id
        else None
    )
    if person_record_id:
        _write_company_throttle_tally(
            attio=attio,
            company_id=company_id,
            person_record_id=person_record_id,
            step_label=step.value,
            experiment_id=experiment_id,
            today=today,
            audit_logger=audit_logger,
            escalate_failures=escalate_failures,
            person_advance_ok=not failed_entry_ids,
        )
    # After the tally on purpose: the invariant write must never be gated by observability I/O.
    if failed_entry_ids and audit_logger is not None:
        audit_logger.event(
            "dm_person_advance_desync",
            person_record_id=person_record_id or "",
            company_id=company_id or "",
            step_label=step.value,
            failed_entry_ids=failed_entry_ids,
            intended_attrs=attrs_to_update,
        )
    return updated


def _attio_advance_with_escalation(
    *,
    attio: AttioClient,
    entry_id: str,
    entry_attributes: dict,
    list_id: str,
    linkedin_url: str,
    today: str,
    step_label: str,
    writer_module: str,
    prior_stage: str | None = None,
    person_record_id: str | None = None,
    audit_logger: AuditLogger | None = None,
    escalate_failures: list | None = None,
) -> bool:
    """Try to advance an Attio list entry; on failure, escalate loudly.

    PB has already confirmed the send happened (the advance gate
    passed at the batch level + this URL was in `outcome.sent_urls`).
    If we now fail to write Attio, the row sits at its PRIOR stage,
    and tomorrow's `run_dm_sequencing` / `run_connection_requests`
    will re-queue the SAME send — the §3.1 violation in disguise,
    one layer down from PB.

    Wave-2-B (§3.15 bypass cleanup): the write now routes through
    ``AttioWriter.apply`` so registry / monotonicity / terminal-class
    enforcement actually fires on this critical advance path. The
    pre-Wave-2 ``attio.update_list_entry`` bypass left the registry as
    documentation; a future regression that flipped a stage backward
    on this path would not have tripped any gate. ``writer_module`` is
    threaded through from each caller (run_dm_sequencing /
    run_connection_requests / detect_accepted_connections) so the
    registry attribution lines up with the function whose contract is
    being executed; ``prior_stage`` powers the monotonicity check.

    AttioWriter handles retry + DLQ + ``attio_write_failed`` queue row
    on permanent-HTTP / max-attempts. The helper preserves the
    pre-existing audit-logger event + escalate_failures tally so the
    end-of-batch summary stays informative; it does NOT re-escalate
    (AttioWriter already opened the queue row).

    Returns:
        True if the write succeeded; False if AttioWriter exhausted
        retries / hit a permanent HTTP error (DLQ + escalation already
        landed inside AttioWriter).
    """
    from clients.attio_writer import (
        AttioError,
        AttioMonotonicityViolation,
        AttioTerminalClassRegression,
        AttioWriter,
        UnauthorizedAttioWriteError,
        WriteIntent,
    )

    prior_values: dict = {}
    if prior_stage is not None:
        prior_values["stage"] = prior_stage

    writer = AttioWriter(attio=attio)
    try:
        writer.apply(WriteIntent(
            object="linkedin_outreach",
            record_id=entry_id,
            updates=entry_attributes,
            prior_values=prior_values,
            writer_module=writer_module,
            is_list_entry=True,
            list_id=list_id,
            # Wave-2-B fix-up (multi-agent I-4): underlying person
            # record_id for one-click navigation from
            # attio_write_failed queue rows.
            companion_record_id=person_record_id,
        ))
        return True
    except (UnauthorizedAttioWriteError,
            AttioMonotonicityViolation,
            AttioTerminalClassRegression):
        # Programmer-bug class (registry/monotonicity/terminal-class):
        # these must NEVER reach production. Propagate so the run halts
        # and the operator sees the typed exception in the crash log
        # — silent-swallow would be a §3.1 risk by another name.
        raise
    except AttioError as exc:
        # Infra failure path: AttioWriter exhausted retries (or hit a
        # permanent 4xx) AND already wrote the DLQ entry + opened the
        # `attio_write_failed` queue row via its built-in
        # _dlq_and_escalate. We DO NOT re-escalate from here — that
        # would create a duplicate queue row with a different
        # idempotency key. The audit-logger event + escalate_failures
        # tally below keep the end-of-batch ERROR summary informative
        # without double-flagging.
        click.echo(
            f"  ⚠ FAILED to advance Attio for {linkedin_url} "
            f"(entry {entry_id}): {exc!s}. AttioWriter opened the "
            f"`attio_write_failed` queue row — operator MUST "
            f"reconcile before next run to avoid re-send.",
            err=True,
        )
        if audit_logger is not None:
            audit_logger.event(
                "attio_advance_failed",
                entry_id=entry_id,
                linkedin_url=linkedin_url,
                step_label=step_label,
                error_class=type(exc).__name__,
                error_msg=str(exc)[:200],
            )
        if escalate_failures is not None:
            escalate_failures.append({
                "site": "_attio_advance_with_escalation",
                "entry_id": entry_id,
                "step_label": step_label,
                "error_class": type(exc).__name__,
            })
        return False
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        # Defense in depth: AttioWriter narrows httpx exceptions to its
        # typed hierarchy, but if a downstream change ever leaked a raw
        # httpx error through this path (e.g. an AttioClient internal
        # call inside _dlq_and_escalate's deferred-import escalate),
        # keep the legacy escalate + return-False semantics so PB-
        # confirmed sends don't orphan into a re-DM tomorrow.
        click.echo(
            f"  ⚠ FAILED to advance Attio for {linkedin_url} "
            f"(entry {entry_id}): {exc!s}. Opening attio_write_failed "
            f"queue row — operator MUST reconcile before next run to "
            f"avoid re-send.",
            err=True,
        )
        try:
            escalate(
                type="attio_write_failed",
                idempotency_key=f"{entry_id}|{today}|{step_label}",
                payload={
                    "object": "list_entries",
                    "record_id": entry_id,
                    "attribute_writes": entry_attributes,
                    "error_class": type(exc).__name__,
                    "error_msg": str(exc)[:500],
                    "retry_count": 0,
                },
                attio=attio,
            )
        except Exception as esc_exc:  # noqa: BLE001 — Wave-1.6.3 carve-out
            click.echo(
                f"  ⚠ escalate(attio_write_failed) in "
                f"_attio_advance_with_escalation FAILED for "
                f"entry_id={entry_id!r} step={step_label!r} "
                f"[{type(esc_exc).__name__}]: {esc_exc}. Continuing batch.",
                err=True,
            )
            if escalate_failures is not None:
                escalate_failures.append({
                    "site": "_attio_advance_with_escalation",
                    "entry_id": entry_id,
                    "step_label": step_label,
                    "error_class": type(esc_exc).__name__,
                })
        if audit_logger is not None:
            audit_logger.event(
                "attio_advance_failed",
                entry_id=entry_id,
                linkedin_url=linkedin_url,
                step_label=step_label,
                error_class=type(exc).__name__,
                error_msg=str(exc)[:200],
            )
        return False


def _phase0_accepted_update(attrs: dict, *, label: str) -> dict:
    """Build the Phase 0 attr update dict for an ACCEPTED flip.

    Returns `{"stage": "Accepted", ...}`. When prior `experiment_id` exists,
    also stamps `experiment_id_frozen_at="accepted"` and logs the flip with
    record_id for operator triage. When prior `experiment_id` is None, logs
    a "measurement-excluded" note so operators see the silent flip case
    (fold-in NIT: surface the no-experiment_id Phase 0 path).

    `label` differentiates the call site in the log line (e.g.,
    "Phase 0 cache-hit flip" vs. "Phase 0 PB flip").
    """
    update: dict = {"stage": PipelineStage.ACCEPTED.value}
    prior_eid = attrs.get("experiment_id")
    if prior_eid is not None:
        update["experiment_id_frozen_at"] = "accepted"
        click.echo(
            f"  [PR-21] {label}: "
            f"record_id={attrs.get('record_id')!r} "
            f"entry_id={attrs.get('entry_id')!r} "
            f"url={attrs.get('linkedin_url', '<unknown>')!r} "
            f"experiment_id={prior_eid!r} "
            f"frozen_at: prior → 'accepted'",
            err=True,
        )
    else:
        click.echo(
            f"  [PR-21] {label}: "
            f"record_id={attrs.get('record_id')!r} "
            f"experiment_id=None — flipping stage only (measurement-excluded)",
            err=True,
        )
    return update


def _escalate_phase0_stale_scrape(
    *,
    attio: AttioClient,
    backend: str,
    profiles_submitted: int,
    profiles_deferred: int,
    rows_matched: int,
    dedup_marker_present: bool,
    container_id: str,
    log_excerpt: str,
    flavor: str,
) -> None:
    """Open a phase0_stale_scrape escalation row, swallowing errors.

    Centralizes the escalation call used at two sites:
    - The silent-zero guard (after CSV download, rows_matched==0 or dedup marker)
    - The no_csv site (PB returned no CSV at all)

    ``flavor`` is appended to the idempotency key so a partial-refusal
    morning row cannot mask a fully-blind afternoon row on the same day:
    ``phase0-stale-scrape|{date}|blind``,
    ``phase0-stale-scrape|{date}|partial``,
    ``phase0-stale-scrape|{date}|no_csv``.

    ``flavor`` is NOT a payload field (Phase0StaleScrapePayload).

    ``profiles_deferred`` (2026-06-11 oversized-launch fix) is the stale
    tail beyond the per-run scrape cap. BLIND/no_csv runs stamp nothing
    into the recheck cache, so the same head batch is re-picked while this
    row keeps firing — the payload must surface the backlog hidden behind
    a poison head batch.
    """
    op_today = operator_today().isoformat()
    try:
        escalate(
            type="phase0_stale_scrape",
            idempotency_key=f"phase0-stale-scrape|{op_today}|{flavor}",
            payload={
                "run_date": op_today,
                "backend": backend,
                "profiles_submitted": profiles_submitted,
                "profiles_deferred": profiles_deferred,
                "rows_matched": rows_matched,
                "dedup_marker_present": dedup_marker_present,
                "container_id": container_id,
                "log_excerpt": log_excerpt,
            },
            attio=attio,
        )
    except Exception as esc_exc:  # noqa: BLE001 — guard must not crash the run
        click.echo(
            f"  ⚠ could not open phase0_stale_scrape escalation: "
            f"{type(esc_exc).__name__}: {esc_exc}",
            err=True,
        )


def _escalate_phase0_suspected_stale_degree(
    *,
    attio: AttioClient,
    backend: str,
    record_ids: list[str],
) -> None:
    """Open a phase0_suspected_stale_degree escalation row, swallowing errors.

    PR-208: emitted once per day with the CONNECTION_SENT rows the SN scrape
    reported invite-resolved (hasPendingInvitation="false") yet still NOT
    1st-degree — the stale-degree / declined-invite cohort an operator should
    cross-reference against LinkedIn "My Network". Visibility only; no stage
    changes. Swallow + log so a queue-write outage (e.g. the Attio select option
    not yet registered by the operator_review_queue migration) never crashes the
    daily run — mirrors `_escalate_phase0_stale_scrape`.
    """
    op_today = operator_today().isoformat()
    try:
        escalate(
            type="phase0_suspected_stale_degree",
            idempotency_key=f"phase0-suspected-stale|{op_today}",
            payload={
                "run_date": op_today,
                "backend": backend,
                "count": len(record_ids),
                "record_ids": record_ids[:20],
            },
            attio=attio,
        )
    except Exception as esc_exc:  # noqa: BLE001 — guard must not crash the run
        click.echo(
            f"  ⚠ could not open phase0_suspected_stale_degree escalation: "
            f"{type(esc_exc).__name__}: {esc_exc}",
            err=True,
        )


def _echo_phase0_prospect_summary(*, checked: int, accepted: int, regressions: int) -> None:
    """Echo the Defect-2 PROSPECT sweep line. No-op when no PROSPECT
    candidates were collected so quiet runs read identically to pre-Defect-2
    output."""
    if checked == 0:
        return
    click.echo(
        f"  PROSPECT sweep: checked {checked}, {accepted} accepted "
        f"(already 1st-degree), {regressions} Pattern-A regression(s) flagged."
    )


def _prospect_accept_disposition(attrs: dict) -> Literal["flip", "regression", "skip"]:
    """Decide what the Phase 0 PROSPECT sweep should do with a 1st-degree row.

    Defect 2: Phase 0 acceptance detection historically only scanned
    CONNECTION_SENT rows, so a LinkedIn 1st-degree connection sitting at
    PROSPECT (we connected outside the tracked invite flow, or they invited us)
    was invisible and never entered the DM cadence. The PROSPECT sweep closes
    that blind spot — but a row can be at PROSPECT for two very different
    reasons, and only ONE is safe to flip:

      * "flip" (Pattern B — externally connected, never messaged): ZERO
        engagement — dm_step coerces to 0 AND none of dm1/dm2/dm3_sent_at /
        response_received_at are set. Flipping to ACCEPTED enters DM1 cadence
        from scratch, which is exactly right.

      * "regression" (Pattern A — DM'd then knocked back to PROSPECT): degree
        is 1st but the row carries DM depth (dm_step>0 OR any *_sent_at OR
        response_received_at). Flipping to ACCEPTED would reset dm_step and
        WIPE the cadence depth, re-sending DM1 to someone already mid-cadence.
        The sweep must NOT flip and must NOT guess a DM stage — it escalates
        and leaves the row at PROSPECT for the repair tooling.

      * "skip": caller-side guard — anything else (not yet used since the
        caller only invokes this for confirmed-1st PROSPECT rows, but kept
        explicit so the contract is total).

    The caller is responsible for confirming degree=="1st" AND stage==PROSPECT
    before consulting this; the disposition only reasons about engagement depth.
    """
    has_depth = (
        dm_step_int(attrs.get("dm_step")) > 0
        or bool(attrs.get("dm1_sent_at"))
        or bool(attrs.get("dm2_sent_at"))
        or bool(attrs.get("dm3_sent_at"))
        or bool(attrs.get("response_received_at"))
    )
    return "regression" if has_depth else "flip"


def _phase0_flip_one(
    attrs: dict,
    *,
    attio: AttioClient,
    list_id: str,
    today_iso: str,
    cache: RecordCache,
    helper_escalate_failures: list,
    cached: bool,
) -> Literal["accepted", "regression", "skip"]:
    """Apply the Phase 0 acceptance disposition to ONE confirmed-1st row.

    Shared by both the cache-hit and post-scrape flip loops, and by both
    candidate stages (CONNECTION_SENT and PROSPECT). Branches on the
    candidate's ``_phase0_kind`` tag:

      * ``conn_sent`` — the historical path, UNCHANGED in behavior: a 1st
        degree is unconditionally flipped to ACCEPTED. CONNECTION_SENT rows
        are tracked invites, so a 1st degree is an accepted invite by
        definition; no engagement-depth gate applies.

      * ``prospect`` — Defect 2 sweep: route through
        ``_prospect_accept_disposition``. ZERO-engagement rows flip to
        ACCEPTED (Pattern B); rows with DM depth are a Pattern-A regression —
        do NOT flip (would wipe cadence depth), escalate instead and leave at
        PROSPECT.

    ``cached`` only tweaks the success log line ("cached" suffix) so the two
    call sites read identically to the pre-refactor output.

    Returns ``"accepted"`` (stage advanced), ``"regression"`` (Pattern-A row
    escalated, left at PROSPECT), or ``"skip"`` (write failed — the helper
    already opened the attio_write_failed row).
    """
    kind = attrs.get("_phase0_kind", "conn_sent")
    url = attrs["linkedin_url"]

    if kind == "prospect":
        disposition = _prospect_accept_disposition(attrs)
        if disposition == "regression":
            _escalate_prospect_first_degree_with_depth(
                attrs, attio=attio, escalate_failures=helper_escalate_failures
            )
            return "regression"
        # disposition == "flip" → fall through to the ACCEPTED advance below.
        label = "Phase 0 PROSPECT sweep flip"
        step_label = "phase0_prospect_accepted"
    else:
        label = "Phase 0 cache-hit flip" if cached else "Phase 0 PB flip"
        step_label = "phase0_cache_hit_accepted" if cached else "phase0_pb_accepted"

    phase0_update = _phase0_accepted_update(attrs, label=label)
    # F-PR-4 §3.15: route through _attio_advance_with_escalation so a transient
    # Attio error opens an attio_write_failed queue row instead of being
    # swallowed. prior_stage carries the entry's ACTUAL stage (Wave-2-B
    # code-reviewer B1) so the monotonicity gate catches a drifted row instead
    # of a fabricated prior. PROSPECT(rank0)→ACCEPTED(rank2) is forward.
    ok = _attio_advance_with_escalation(
        attio=attio,
        entry_id=attrs["entry_id"],
        entry_attributes=phase0_update,
        list_id=list_id,
        linkedin_url=url,
        today=today_iso,
        step_label=step_label,
        writer_module="workflows.daily_check.detect_accepted_connections",
        prior_stage=attrs.get("stage"),
        person_record_id=attrs.get("record_id"),
        escalate_failures=helper_escalate_failures,
    )
    if not ok:
        return "skip"
    name, _, _, _, _ = cache.get(attrs["record_id"])
    if kind == "prospect":
        click.echo(f"  ✓ {name} accepted (PROSPECT sweep, 1st-degree)")
    else:
        suffix = ", cached" if cached else ""
        click.echo(
            f"  ✓ {name} accepted (sent {attrs.get('last_contact_date', '?')}{suffix})"
        )
    return "accepted"


def _escalate_prospect_first_degree_with_depth(
    attrs: dict, *, attio: AttioClient, escalate_failures: list | None = None
) -> None:
    """Open a ``prospect_first_degree_with_depth`` queue row for a Pattern-A
    regression caught by the PROSPECT sweep (a 1st-degree PROSPECT that already
    carries DM depth). Wrapped in swallow-and-log so a queue-write outage never
    crashes the Phase 0 sweep — mirrors the other escalate() guards here.

    On escalate failure the row is appended to ``escalate_failures`` (when
    provided) so it joins the end-of-batch ERROR rollup — without it, a
    queue-write outage during a regression would only scroll past as a per-row
    WARN and the operator would get no aggregated paging-level signal.
    """
    record_id = str(attrs.get("record_id", ""))
    click.echo(
        f"  ⚠ PROSPECT sweep: {attrs.get('linkedin_url', '<unknown>')!r} is "
        f"1st-degree but carries DM depth (dm_step={attrs.get('dm_step')!r}) — "
        f"NOT flipping to ACCEPTED (would wipe cadence depth). Pattern-A "
        f"regression; opening prospect_first_degree_with_depth for repair.",
        err=True,
    )
    try:
        escalate(
            type="prospect_first_degree_with_depth",
            # operator_today() (OUTBOUND_TZ), not date.today() (UTC), so the
            # row's date aligns with the daily_run run_date for forensics —
            # matches the Phase 0 degrade escalations.
            idempotency_key=(
                f"prospect-1st-depth|{record_id}|{operator_today().isoformat()}"
            ),
            payload={
                "record_id": record_id,
                "entry_id": str(attrs.get("entry_id", "")),
                "linkedin_url": str(attrs.get("linkedin_url", "")),
                "dm_step": str(attrs.get("dm_step") or ""),
                "dm1_sent_at_set": bool(attrs.get("dm1_sent_at")),
                "dm2_sent_at_set": bool(attrs.get("dm2_sent_at")),
                "dm3_sent_at_set": bool(attrs.get("dm3_sent_at")),
                "response_received_at_set": bool(attrs.get("response_received_at")),
            },
            attio=attio,
        )
    except Exception as esc_exc:  # noqa: BLE001 — guard must not crash the sweep
        click.echo(
            f"  ⚠ could not open prospect_first_degree_with_depth escalation: "
            f"{type(esc_exc).__name__}: {esc_exc}",
            err=True,
        )
        if escalate_failures is not None:
            escalate_failures.append({
                "site": "_escalate_prospect_first_degree_with_depth",
                "record_id": record_id,
                "error_class": type(esc_exc).__name__,
            })


def detect_accepted_connections(
    attio: AttioClient,
    pb: PhantomBusterClient,
    profile_scraper_id: str,
    cache: RecordCache | None = None,
    sales_nav_profile_scraper_id: str | None = None,
) -> dict:
    """Phase 0: Live-check CONNECTION_SENT profiles for accepted connections.

    Launches PB Profile Scraper on CONNECTION_SENT profiles to get current
    connectionDegree. Any with "1st" degree are moved to ACCEPTED in Attio.
    Keeps original last_contact_date so DM1 timing counts from send date.

    Only checks profiles whose last_contact_date is within the last
    ACCEPTANCE_CHECK_WINDOW_DAYS days (older invites rarely convert).

    At most PHASE0_MAX_PROFILES_PER_LAUNCH stale profiles are scraped per
    run (2026-06-11 oversized-launch fix). Within that cap the batch is filled
    NEWEST-invite first (PR-209) — fresh acceptances cluster in the first days
    post-invite and are the highest-value to detect + DM promptly — after
    reserving PHASE0_EXPIRING_RESERVE slots for the oldest invites about to exit
    the acceptance window (last-chance check). The middle band is deferred and
    rotates in on subsequent runs via the recheck cache. The ``deferred`` key of
    the returned summary carries the deferred size.

    Backend selection mirrors the pre-invite degree-check (PR-B):
    ``PRE_INVITE_DEGREE_CHECK_BACKEND=sales_nav`` routes the scrape through
    the Sales Navigator Profile Scraper (id from
    ``sales_nav_profile_scraper_id``), which requires injecting session
    credentials into ``identities[0]`` of the saved phantom argument shape
    rather than passing them top-level. The flag is shared between Phase 0
    and pre-invite because both consume the same external service (PB
    profile scraping); the legacy regular scraper was deleted from the PB
    workspace as part of the same migration, so ``regular`` is effectively
    a no-op fallback once the operator flips this flag for pre-invite.

    `cache` may be provided by the caller (e.g. cli.py daily) to share person
    lookups across phases; if None, a fresh cache is built so standalone use
    keeps working.

    Returns summary dict with counts.
    """
    import csv
    import io

    list_id = os.environ.get("ATTIO_LIST_ID", "")
    all_parsed = _get_all_entries_parsed(attio)
    if cache is None:
        cache = RecordCache(attio)

    # Collect recent CONNECTION_SENT profiles (older invites rarely convert).
    cutoff = (date.today() - timedelta(days=ACCEPTANCE_CHECK_WINDOW_DAYS)).isoformat()
    # L1-2: anything older than STALE_CONNECTION_SENT_ESCALATE_DAYS is permanently
    # invisible to acceptance detection. Track for the aggregated escalation below.
    stale_escalate_cutoff = (
        date.today() - timedelta(days=STALE_CONNECTION_SENT_ESCALATE_DAYS)
    ).isoformat()
    conn_sent: list[dict] = []  # [{entry attrs + linkedin_url}]
    skipped_stale = 0
    stale_record_ids: list[str] = []  # L1-2: beyond STALE_CONNECTION_SENT_ESCALATE_DAYS
    for attrs in all_parsed:
        if attrs["stage"] != PipelineStage.CONNECTION_SENT.value:
            continue
        last_sent = (attrs.get("last_contact_date") or "")[:10]
        # L1-2: collect records older than the escalation threshold BEFORE
        # the acceptance-window filter so we get the full stale set.
        if last_sent and last_sent < stale_escalate_cutoff:
            stale_record_ids.append(str(attrs["record_id"]))
        if not last_sent or last_sent < cutoff:
            skipped_stale += 1
            continue
        _, _, linkedin_url, _, _ = cache.get(attrs["record_id"])
        if not linkedin_url:
            continue
        attrs["linkedin_url"] = linkedin_url
        attrs["_phase0_kind"] = "conn_sent"
        conn_sent.append(attrs)

    if skipped_stale:
        click.echo(
            f"  Skipping {skipped_stale} CONNECTION_SENT profiles older than "
            f"{ACCEPTANCE_CHECK_WINDOW_DAYS} days."
        )

    # Defect 2: ALSO sweep PROSPECT-stage rows for already-1st degree
    # connections (we connected outside the tracked invite flow, or they
    # invited us). These were invisible to acceptance detection — only
    # CONNECTION_SENT was scanned — so a real 1st-degree connection sat at
    # PROSPECT forever and never entered the DM cadence. No 14-day window here
    # (PROSPECTs have no invite last_contact_date); the recheck-cache TTL and
    # the scrape cap provide rotation, and CONNECTION_SENT keeps scrape priority.
    prospect_cands: list[dict] = []
    for attrs in all_parsed:
        if attrs["stage"] != PipelineStage.PROSPECT.value:
            continue
        # Sourced-gate (prospect-perception safety): only sweep records WE
        # prospected. `prospect_committed_at` is stamped unconditionally on
        # every pipeline commit (weekly_prospect._build_prospect_entry_attrs),
        # so its presence is the reliable "this is a sales prospect we sourced"
        # signal. A 1st-degree PROSPECT WITHOUT it is a manual/organic/imported
        # connection (a peer, an inbound invite, a personal contact) we never
        # intended to cold-DM — flipping it to ACCEPTED would drop it into the
        # DM cadence and send a cold pitch. Skip those. (Gate before cache.get
        # so we also avoid an unnecessary get_person on non-sourced records.)
        if not attrs.get("prospect_committed_at"):
            continue
        _, _, linkedin_url, _, _ = cache.get(attrs["record_id"])
        if not linkedin_url:
            continue
        attrs["linkedin_url"] = linkedin_url
        attrs["_phase0_kind"] = "prospect"
        prospect_cands.append(attrs)

    # L1-2: emit ONE aggregated stale_connection_sent queue row per day.
    # These rows are permanently beyond ACCEPTANCE_CHECK_WINDOW_DAYS and will
    # never receive an acceptance check — surfacing them lets the operator
    # decide whether to park or purge. No stage changes, visibility only.
    if stale_record_ids:
        # Best-effort (mirrors _escalate_phase0_stale_scrape): this is a
        # visibility-only row. A transient escalate() failure must not crash
        # the acceptance-detection pass that follows — swallow + stderr echo.
        try:
            escalate(
                type="stale_connection_sent",
                idempotency_key=f"stale_cs|{date.today().isoformat()}",
                payload={
                    "count": len(stale_record_ids),
                    "record_ids": stale_record_ids[:20],
                },
                attio=attio,
            )
        except Exception as esc_exc:  # noqa: BLE001 — guard must not crash the run
            click.echo(
                f"  ⚠ could not open stale_connection_sent escalation: "
                f"{type(esc_exc).__name__}: {esc_exc}",
                err=True,
            )
        click.echo(
            f"  ⚠ {len(stale_record_ids)} CONNECTION_SENT row(s) older than "
            f"{STALE_CONNECTION_SENT_ESCALATE_DAYS} days — permanently beyond "
            f"the acceptance-detection window. stale_connection_sent queue row "
            f"opened (one per day).",
            err=True,
        )

    if not conn_sent and not prospect_cands:
        click.echo("  No CONNECTION_SENT or PROSPECT profiles to check.")
        return {
            "accepted": 0,
            "checked": 0,
            "deferred": 0,
            "prospects_checked": 0,
            "prospects_accepted": 0,
            "prospect_regressions_flagged": 0,
        }

    # Consult recheck cache: profiles scraped in the last RECHECK_TTL_DAYS days
    # short-circuit the PB launch. Cached "1st" → flip to ACCEPTED now; cached
    # "2nd"/"3rd" → still pending, skip. Stale entries fall through to PB.
    # Defect 2: PROSPECTs share the SAME cache/partition/flip pipeline as
    # CONNECTION_SENT (DRY) — the per-row flip behavior branches on
    # `_phase0_kind` inside _phase0_flip_one.
    candidates = conn_sent + prospect_cands
    urls_in = [p["linkedin_url"] for p in candidates]
    fresh_cache, stale_urls = recheck_cache.partition(urls_in)
    stale_set = set(stale_urls)

    # PR-208 stale-degree fix. The SN scraper's connectionDegree lags the live
    # graph — it returns "2nd" for a connection that has actually accepted (now
    # 1st-degree). partition() treats that cached "2nd" as fresh for
    # RECHECK_TTL_DAYS and suppresses re-scraping, so the 2nd→1st correction is
    # never observed and the row sticks at CONNECTION_SENT indefinitely. For
    # CONNECTION_SENT candidates ONLY, force a re-scrape of any cached NON-"1st"
    # degree UNLESS it was already checked today — same-day repeat passes still
    # cache-hit, so no duplicate scrape credits within a day, while a stale
    # negative from a prior day re-checks daily. A cached "1st" is terminal (the
    # flip loop below advances it) so it stays a cache-hit. PROSPECT candidates
    # keep the plain TTL behavior: they have no invite to have "accepted", and
    # they share the capped scrape budget. today_iso (UTC) is compared against
    # recheck_cache's UTC `checked_at` stamp so the same-day predicate cannot
    # drift off-by-one.
    today_iso = date.today().isoformat()
    for p in conn_sent:
        url = p["linkedin_url"]
        if url in stale_set:
            continue
        cached_entry = fresh_cache.get(url) or {}
        # .get() (not subscript): visit-only cache entries carry checked_at but
        # no `degree` key (recheck_cache.record_many only writes degree when
        # truthy) — a subscript would KeyError and crash Phase 0.
        if (
            cached_entry.get("degree") != "1st"
            and cached_entry.get("checked_at") != today_iso
        ):
            stale_set.add(url)
            fresh_cache.pop(url, None)

    conn_sent_stale = [p for p in conn_sent if p["linkedin_url"] in stale_set]
    prospect_stale = [p for p in prospect_cands if p["linkedin_url"] in stale_set]
    cache_hit_count = len(candidates) - len(conn_sent_stale) - len(prospect_stale)
    if cache_hit_count:
        click.echo(
            f"  Skipping {cache_hit_count} profile(s) cached "
            f"within {recheck_cache.RECHECK_TTL_DAYS} days."
        )

    # Flip any cache-hit "1st" entries to ACCEPTED without re-scraping.
    # Cache-hit flips are NOT capped (no PB cost) — apply to both stages.
    # (today_iso is computed above, before the PR-208 forced-rescrape predicate.)
    accepted = 0
    prospects_accepted = 0
    prospect_regressions_flagged = 0
    # Wave-1.6.3: same calibration as run_connection_requests for the
    # Phase 0 callers of _attio_advance_with_escalation. If escalate()
    # raises inside the helper, the helper now swallows + appends here so
    # the per-row loop survives and the end-of-function summary surfaces
    # the count loudly.
    helper_escalate_failures: list = []
    for attrs in candidates:
        url = attrs["linkedin_url"]
        entry = fresh_cache.get(url)
        if not entry or entry.get("degree") != "1st":
            continue
        outcome = _phase0_flip_one(
            attrs,
            attio=attio,
            list_id=list_id,
            today_iso=today_iso,
            cache=cache,
            helper_escalate_failures=helper_escalate_failures,
            cached=True,
        )
        if outcome == "accepted":
            accepted += 1
            if attrs.get("_phase0_kind") == "prospect":
                prospects_accepted += 1
        elif outcome == "regression":
            prospect_regressions_flagged += 1

    if not conn_sent_stale and not prospect_stale:
        click.echo(f"  Checked {len(candidates)} profiles, {accepted} accepted (all cached).")
        _echo_phase0_prospect_summary(
            checked=len(prospect_cands),
            accepted=prospects_accepted,
            regressions=prospect_regressions_flagged,
        )
        if helper_escalate_failures:
            sites = ", ".join(
                sorted({f["site"] for f in helper_escalate_failures})
            )
            click.echo(
                f"  ❌ ERROR: {len(helper_escalate_failures)} "
                f"attio_write_failed escalate() call(s) failed during "
                f"Phase 0 cache-hit ACCEPTED flips (sites: {sites}). "
                f"Operator review queue is missing reconciliation rows.",
                err=True,
            )
        return {
            "accepted": accepted,
            "checked": len(candidates),
            "cache_hits": cache_hit_count,
            "deferred": 0,
            "prospects_checked": len(prospect_cands),
            "prospects_accepted": prospects_accepted,
            "prospect_regressions_flagged": prospect_regressions_flagged,
        }

    # Bound the launch — see PHASE0_MAX_PROFILES_PER_LAUNCH for the 2026-06-11
    # incident and the cap rationale. The TOTAL scrape batch (CONNECTION_SENT +
    # PROSPECT) must stay at or under the cap so the per-run PB profile-scrape
    # volume is UNCHANGED by the Defect-2 PROSPECT sweep (flat PB cost).
    #
    # CONNECTION_SENT keeps PRIORITY: fill the batch with stale CONNECTION_SENT
    # first, then fill the REMAINING budget with stale PROSPECTs. On busy
    # CONNECTION_SENT days PROSPECTs get little/no budget — acceptable, they
    # rotate in on quieter days. Scraped rows are stamped into the recheck cache
    # below and read as fresh next run, so each deferred tail rotates in instead
    # of re-queueing.
    #
    # PR-208: reserve a PROSPECT floor so the daily CONNECTION_SENT re-scrape
    # flood (stale-degree fix) can't starve the Defect-2 PROSPECT sweep — see
    # PHASE0_PROSPECT_MIN_BUDGET. CONNECTION_SENT takes (cap − reserve) plus any
    # reserved slots PROSPECTs don't use; total never exceeds the cap and the
    # no-PROSPECT case is unchanged (reserve = 0).
    reserved_for_prospects = (
        min(PHASE0_PROSPECT_MIN_BUDGET, len(prospect_stale)) if prospect_stale else 0
    )
    # max(0, …): guards the "total ≤ cap" invariant if PHASE0_PROSPECT_MIN_BUDGET
    # is ever raised at/above the cap — without it conn_sent_cap goes negative and
    # the slice would silently scrape zero CONNECTION_SENT (the inverse starvation).
    conn_sent_cap = max(0, PHASE0_MAX_PROFILES_PER_LAUNCH - reserved_for_prospects)
    #
    # PR-209 Leak-A fix: WITHIN the CONNECTION_SENT budget (conn_sent_cap),
    # reserve PHASE0_EXPIRING_RESERVE slots for the OLDEST invites (about to exit
    # the ACCEPTANCE_CHECK_WINDOW_DAYS window — a last-chance check) and fill the
    # rest NEWEST-first. Fresh acceptances cluster in the first days post-invite;
    # pure oldest-first deferred those newest invites behind the backlog and
    # starved the most-likely-to-have-just-accepted profiles. The deferred set is
    # the MIDDLE band, which rotates in via the 3-day recheck cache. (Composes
    # with the PROSPECT reserve above: the expiring/newest split runs against
    # conn_sent_cap, not the full cap.)
    conn_sent_stale.sort(key=lambda p: p.get("last_contact_date") or "")  # oldest -> newest
    expiring_reserve = min(
        PHASE0_EXPIRING_RESERVE, conn_sent_cap, len(conn_sent_stale)
    )
    expiring_batch = conn_sent_stale[:expiring_reserve]  # oldest, about to expire
    fresh_pool = conn_sent_stale[expiring_reserve:]
    fresh_budget = conn_sent_cap - len(expiring_batch)
    fresh_batch = fresh_pool[-fresh_budget:] if fresh_budget > 0 else []  # newest end
    conn_sent_batch = expiring_batch + fresh_batch
    conn_sent_deferred = max(0, len(conn_sent_stale) - len(conn_sent_batch))

    prospect_budget = PHASE0_MAX_PROFILES_PER_LAUNCH - len(conn_sent_batch)
    # Rotate the PROSPECT tail least-recently-checked first so a small budget
    # (busy CONNECTION_SENT days) can't starve the same tail forever. Records
    # never checked sort first (""), then oldest checked_at. Combined with the
    # 3-day cache TTL this gives deterministic round-robin coverage.
    prospect_stale.sort(key=lambda p: recheck_cache.last_checked(p["linkedin_url"]) or "")
    prospect_batch = prospect_stale[:prospect_budget]
    prospect_deferred = len(prospect_stale) - len(prospect_batch)

    # Downstream pipeline (sheet write, launch, match-back, post-scrape flips)
    # operates on the combined batch; per-row flip behavior branches on
    # `_phase0_kind` inside _phase0_flip_one. `deferred` keeps its historical
    # meaning (the CONNECTION_SENT tail) so existing callers/tests are unchanged;
    # `prospect_deferred` is reported separately in the summary.
    scrape_batch = conn_sent_batch + prospect_batch
    deferred = conn_sent_deferred
    if conn_sent_deferred or prospect_deferred:
        click.echo(
            f"  Capping Phase 0 scrape batch at {PHASE0_MAX_PROFILES_PER_LAUNCH} "
            f"(per-run visit-safety cap; phantom schema max is 150) — "
            f"{conn_sent_deferred} CONNECTION_SENT + {prospect_deferred} PROSPECT "
            f"profile(s) deferred to subsequent runs (CONNECTION_SENT filled "
            f"newest-first with a {PHASE0_EXPIRING_RESERVE}-slot expiring-invite "
            f"reserve); CONNECTION_SENT keeps scrape priority."
        )

    click.echo(
        f"  Checking {len(scrape_batch)} profile(s) via Profile Scraper "
        f"({len(conn_sent_batch)} CONNECTION_SENT + {len(prospect_batch)} PROSPECT)..."
    )

    # PR-Phase-0-SN-migration: branch on the same flag the pre-invite path
    # already honors. ``regular`` keeps the legacy top-level-cookie launch
    # for the LinkedIn Profile Scraper (documented-dead — see the preflight
    # below). ``sales_nav`` POSTs the full saved-args + identities-inject
    # shape via daily_check_helpers.build_sales_nav_launch_args (the contract
    # the SN phantom enforces — see that helper's docstring).
    #
    # Resolve + preflight BEFORE the sheet write below: a config error
    # (invalid backend value, missing SN env, dead legacy agent id) must
    # not burn a write to the production autoconnect sheet first.
    backend = _resolve_degree_check_backend()
    if backend == "sales_nav":
        if not sales_nav_profile_scraper_id:
            raise RuntimeError(
                "Phase 0 detect_accepted_connections: "
                "PRE_INVITE_DEGREE_CHECK_BACKEND=sales_nav but "
                "sales_nav_profile_scraper_id was not provided. "
                "Caller (cli.py daily) must pass PB_SALES_NAV_PROFILE_SCRAPER_ID."
            )
    else:
        # DOCUMENTED-DEAD legacy path: the legacy Profile Scraper agent was
        # deleted from the PB workspace and backend=regular is no longer the
        # default. Preflight turns the otherwise-raw httpx 404 at launch into
        # an actionable config error. Branch kept for a future re-deployed
        # phantom (rollback requires a NEW agent id).
        preflight_legacy_profile_scraper(pb, profile_scraper_id)

    # Build URL set for matching results back to our batch
    our_urls = {_normalize_linkedin_url(p["linkedin_url"]) for p in scrape_batch}

    # Write to Google Sheet and launch Profile Scraper (expects 'profileUrl' column)
    sheet_rows = [{"profileUrl": p["linkedin_url"]} for p in scrape_batch]
    sheet_url = write_prospects_to_sheet(sheet_rows, columns=["profileUrl"])

    if backend == "sales_nav":
        # Already guaranteed non-None by the resolve-time guard above (the
        # first backend branch raises when it's missing) — re-narrow for the
        # type checker now that the guard and the launch live in separate
        # blocks (config errors must precede the sheet write between them).
        assert sales_nav_profile_scraper_id is not None
        click.echo("  Launching Sales Nav Profile Scraper...")
        csv_name = _fresh_csv_name("deg")
        launch_args = {
            # Saved-args + identities-inject contract lives in the shared
            # helper. launch_count: +1 for the sheet header row PB counts as
            # a processable line — see clients.google_sheets.profiles_per_launch
            # (last-row-dropped incident).
            **build_sales_nav_launch_args(
                pb,
                sales_nav_profile_scraper_id,
                spreadsheet_url=sheet_url,
                launch_count=profiles_per_launch(len(scrape_batch)),
            ),
            # PB keys the phantom's processed-inputs DB on the result file
            # name; a fresh name per launch forces re-scrape (Phase 0 exists
            # to observe 2nd→1st flips ON re-scrape) and isolates this
            # launch's CSV from prior runs' stale rows.
            "csvName": csv_name,
        }
        launch = pb.launch_agent(sales_nav_profile_scraper_id, launch_args)
    else:
        click.echo("  Launching Profile Scraper...")
        csv_name = _fresh_csv_name("deg")
        launch = pb.launch_agent(profile_scraper_id, {
            "spreadsheetUrl": sheet_url,
            # Same dedup-bust as the sales_nav branch: the legacy scraper
            # keys its processed-inputs DB on the result filename too —
            # without csvName it retains the original confident-zero bug.
            "csvName": csv_name,
            **_pb_session_args(),
        })
    # The Sales Nav scraper queues + executes slower than the legacy scraper:
    # a clean run observed 2026-05-27 was ~3m49s queued + ~6m53s executing
    # (~643s wall), so SN gets 750s. The legacy scraper was near-instant — keep
    # its 300s ceiling so the regular branch doesn't silently block longer than
    # it ever needed to. We deliberately do NOT retry on timeout (unlike
    # pre-invite): a Phase 0 retry would re-launch a fresh container and
    # re-scrape the SAME profiles — double PB credits for no benefit, since
    # acceptance detection is naturally idempotent (any still-pending
    # CONNECTION_SENT entry is re-checked on the next daily run).
    wait_max = 750 if backend == "sales_nav" else 300
    try:
        completion = pb.wait_for_completion(launch, poll_interval=10, max_wait=wait_max)
    except (PBRunFailed, PBRunTimeout) as exc:
        # Graceful degrade: a Phase 0 scrape outage must NOT crash the whole
        # daily run (pre-fix it raised, skipping Parts A + B entirely). On
        # timeout the orphaned PB container finishes in the background and its
        # CSV goes unprocessed this run; on PBRunFailed (PB reports
        # status="error" for the container) there is no CSV at all. Either
        # way the profiles get re-checked next run.
        # Return the partial result (cache-hit accepts already applied) so
        # the caller proceeds to Parts A (invites) and B (DMs).
        failed = isinstance(exc, PBRunFailed)
        kind = "failed (PB status=error)" if failed else "timed out"
        click.echo(
            f"  ⚠ Phase 0 scrape {kind} ({exc}). Skipping live acceptance "
            f"detection this run; {len(scrape_batch)} profile(s) will be "
            f"re-checked next run. Daily run continues to Parts A/B.",
            err=True,
        )
        # The daily_run row still closes status=completed on a degrade, so the
        # only signal of a silent Phase 0 outage was this stderr line. Open an
        # operator-review row so degraded runs are visible Attio-side too.
        # Idempotent per (type, date): a second same-day timeout is a no-op.
        # Key on operator_today() (OUTBOUND_TZ), NOT date.today() (UTC), so the
        # escalation's date matches the daily_run row's run_date — otherwise a
        # late-evening run labels the escalation one UTC-day ahead of its
        # parent run, confusing forensics. (per-machine context is intentionally
        # NOT in the key — single-machine deployment; add |{machine_id} if that
        # ever changes.)
        # Best-effort: escalation must NOT re-introduce the crash the graceful
        # degrade just avoided (e.g. Attio down, or the select option not yet
        # deployed) — swallow + log instead.
        op_today = operator_today().isoformat()
        # str(exc) is safe in both arms: PBRunFailed scrubs profile URLs from
        # its message at construction (L10-8); PBRunTimeout carries no URLs.
        if failed:
            esc_type = "phase0_scrape_failed"
            idempotency_key = f"phase0-failed|{op_today}"
            payload = {
                "run_date": op_today,
                "backend": backend,
                "profiles_pending": len(scrape_batch),
                "profiles_deferred": deferred,
                "container_id": exc.container_id,
                "error": str(exc)[:300],
            }
        else:
            esc_type = "phase0_scrape_timeout"
            idempotency_key = f"phase0-timeout|{op_today}"
            payload = {
                "run_date": op_today,
                "backend": backend,
                "profiles_pending": len(scrape_batch),
                "profiles_deferred": deferred,
                "wait_max_seconds": wait_max,
                "error": str(exc)[:300],
            }
        try:
            escalate(
                type=esc_type,
                idempotency_key=idempotency_key,
                payload=payload,
                attio=attio,
            )
        except Exception as esc_exc:  # noqa: BLE001 — degrade path must not re-crash
            click.echo(
                f"  ⚠ could not open {esc_type} escalation: "
                f"{type(esc_exc).__name__}: {esc_exc}",
                err=True,
            )
        return {
            "accepted": accepted,
            "checked": len(conn_sent),
            "cache_hits": cache_hit_count,
            "deferred": deferred,
            "prospects_checked": len(prospect_cands),
            "prospects_accepted": prospects_accepted,
            "prospect_regressions_flagged": prospect_regressions_flagged,
            "error": "pb_failed" if failed else "pb_timeout",
        }

    # Download result CSV and check degrees (filter to only our batch).
    # `download_result_csv(launch)` keys off launch.container_id (F-PR-5).
    # Pass csv_name so the agent-scoped S3 fallback resolves the right file.
    result_csv = pb.download_result_csv(launch, csv_name=csv_name)
    if not result_csv:
        click.echo(
            "  ❌ Profile Scraper returned no CSV — acceptance detection "
            "is BLIND this run; profiles will be re-checked next run.",
            err=True,
        )
        _escalate_phase0_stale_scrape(
            attio=attio,
            backend=backend,
            profiles_submitted=len(scrape_batch),
            profiles_deferred=deferred,
            rows_matched=0,
            dedup_marker_present=has_scraper_dedup_marker(
                getattr(completion, "log_output", "") or ""
            ),
            container_id=str(launch.container_id or ""),
            log_excerpt=(getattr(completion, "log_output", "") or "")[-300:],
            flavor="no_csv",
        )
        if helper_escalate_failures:
            sites = ", ".join(
                sorted({f["site"] for f in helper_escalate_failures})
            )
            click.echo(
                f"  ❌ ERROR: {len(helper_escalate_failures)} "
                f"attio_write_failed escalate() call(s) failed during "
                f"Phase 0 cache-hit ACCEPTED flips (sites: {sites}). "
                f"Operator review queue is missing reconciliation rows.",
                err=True,
            )
        return {
            "accepted": accepted,
            "checked": len(conn_sent),
            "cache_hits": cache_hit_count,
            "deferred": deferred,
            "prospects_checked": len(prospect_cands),
            "prospects_accepted": prospects_accepted,
            "prospect_regressions_flagged": prospect_regressions_flagged,
            "error": "no_csv",
        }

    reader = csv.DictReader(io.StringIO(result_csv))
    degree_lookup: dict[str, str] = {}
    # PR-208: also capture hasPendingInvitation per URL. It is NOT used to flip
    # (declined/withdrawn invites also read "false", which would be a
    # false-accept); it feeds the phase0_suspected_stale_degree reconcile alarm
    # below — a CONNECTION_SENT row reported invite-resolved (pending="false")
    # but still not 1st-degree is exactly the stale-degree / declined cohort a
    # human should eyeball.
    pending_lookup: dict[str, str] = {}
    for row in reader:
        # PB Profile Scraper uses multiple URL columns; try all
        url = (
            row.get("linkedin_url", "")
            or row.get("linkedinProfileUrl", "")
            or row.get("profileUrl", "")
            or row.get("query", "")
        )
        degree = row.get("connectionDegree", "")
        if url and degree:
            norm = _normalize_linkedin_url(url)
            if norm in our_urls:
                degree_lookup[norm] = degree
                pending_lookup[norm] = (
                    row.get(SALES_NAV_HAS_PENDING_INVITATION_COL) or ""
                ).strip().lower()

    # Silent-zero guard (2026-06-09 incident): a dedup-refused scrape used to
    # join stale result.csv rows and report a confident "0 accepted". With a
    # per-launch csvName the CSV contains ONLY this launch's rows, so zero
    # matched rows for a non-empty batch means NO fresh observation — halt,
    # escalate, and stamp nothing (stamping would suppress re-checks for
    # RECHECK_TTL_DAYS on data we never actually saw).
    log_output = (getattr(completion, "log_output", "") or "")
    dedup_marker = has_scraper_dedup_marker(log_output)
    rows_matched = len(degree_lookup)
    if dedup_marker or rows_matched == 0:
        _escalate_phase0_stale_scrape(
            attio=attio,
            backend=backend,
            profiles_submitted=len(scrape_batch),
            profiles_deferred=deferred,
            rows_matched=rows_matched,
            dedup_marker_present=dedup_marker,
            container_id=str(launch.container_id or ""),
            log_excerpt=log_output[-300:],
            flavor="blind" if rows_matched == 0 else "partial",
        )
        if rows_matched == 0:
            click.echo(
                f"  ❌ Phase 0 scrape returned NO fresh rows for "
                f"{len(scrape_batch)} submitted profile(s) "
                f"(container {launch.container_id}, dedup marker: "
                f"{'YES' if dedup_marker else 'no'}). Acceptance detection is "
                f"BLIND this run — skipping recheck_cache stamping so these "
                f"profiles are re-checked next run. NOT a confident zero.",
                err=True,
            )
            return {
                "accepted": accepted,
                "checked": len(conn_sent),
                "cache_hits": cache_hit_count,
                "deferred": deferred,
                "prospects_checked": len(prospect_cands),
                "prospects_accepted": prospects_accepted,
                "prospect_regressions_flagged": prospect_regressions_flagged,
                "error": "stale_scrape",
            }
        click.echo(
            f"  ⚠ Phase 0 container {launch.container_id} logged a dedup "
            f"marker but returned {rows_matched} fresh row(s) — partial "
            f"refusal; unmatched profiles will be re-checked next run.",
            err=True,
        )

    # Match results to Attio entries and update accepted ones. Both stages
    # (CONNECTION_SENT + PROSPECT) share this loop; _phase0_flip_one branches
    # the flip behavior on `_phase0_kind` (PROSPECTs route through the
    # Pattern-A/B disposition gate so a 1st-degree-with-depth row is escalated
    # rather than flipped — never wiping cadence depth).
    cache_updates: dict[str, str | None] = {}
    for attrs in scrape_batch:
        normalized = _normalize_linkedin_url(attrs["linkedin_url"])
        degree = degree_lookup.get(normalized, "")
        if degree:
            cache_updates[attrs["linkedin_url"]] = degree
        if degree == "1st":
            outcome = _phase0_flip_one(
                attrs,
                attio=attio,
                list_id=list_id,
                today_iso=today_iso,
                cache=cache,
                helper_escalate_failures=helper_escalate_failures,
                cached=False,
            )
            if outcome == "accepted":
                accepted += 1
                if attrs.get("_phase0_kind") == "prospect":
                    prospects_accepted += 1
            elif outcome == "regression":
                prospect_regressions_flagged += 1

    recheck_cache.record_many(cache_updates)

    # PR-208 acceptance-reconcile alarm. Surface CONNECTION_SENT rows the SN
    # scrape reports as invite-resolved (hasPendingInvitation="false") but still
    # NOT 1st-degree. These are either declined/withdrawn invites or — the bug
    # this guards — accepted invites whose degree the scraper is mis-reading;
    # both need a human cross-reference against LinkedIn "My Network". Auto-flip
    # is NOT triggered (pending="false" alone can't distinguish accepted from
    # declined, so flipping would risk cold-DMing someone who declined). The
    # blind/no_csv paths return early above; the PARTIAL-dedup path (dedup marker
    # + rows>0) falls through here and MAY co-fire with phase0_stale_scrape
    # (partial) — that is intended: distinct alarms, distinct idempotency keys,
    # both visibility-only. Aggregated to one row/day (mirrors
    # stale_connection_sent).
    suspect_cutoff = (
        date.today() - timedelta(days=SUSPECTED_STALE_MIN_AGE_DAYS)
    ).isoformat()
    suspected_ids: list[str] = []
    conn_sent_matched = 0  # CONNECTION_SENT rows we got a degree for this scrape
    pending_signal_seen = 0  # ...of those, how many carried a hasPendingInvitation
    for attrs in scrape_batch:
        if attrs.get("_phase0_kind") != "conn_sent":
            continue
        norm = _normalize_linkedin_url(attrs["linkedin_url"])
        degree = degree_lookup.get(norm, "")
        # Only judge rows actually observed this scrape (degree present).
        if not degree:
            continue
        conn_sent_matched += 1
        if pending_lookup.get(norm):
            pending_signal_seen += 1
        if degree == "1st":
            continue
        if pending_lookup.get(norm) != "false":
            continue
        last_sent = (attrs.get("last_contact_date") or "")[:10]
        # "at least SUSPECTED_STALE_MIN_AGE_DAYS old" → <= so an invite sent
        # exactly on the cutoff date is included (conservative: never younger).
        if last_sent and last_sent <= suspect_cutoff:
            suspected_ids.append(str(attrs["record_id"]))

    # Silent-blind guard: on the sales_nav backend the SN CSV always carries
    # hasPendingInvitation. If we observed CONNECTION_SENT rows yet the column
    # was empty on ALL of them, this alarm is blind (column renamed upstream, or
    # the legacy `regular` backend that omits it) — say so loudly rather than
    # report a quiet "no suspected-stale rows", which would read as all-clear.
    if backend == "sales_nav" and conn_sent_matched and pending_signal_seen == 0:
        click.echo(
            f"  ⚠ phase0_suspected_stale_degree is BLIND this run: "
            f"hasPendingInvitation was empty on all {conn_sent_matched} matched "
            f"CONNECTION_SENT row(s) — the SN CSV column may have been renamed. "
            f"The stale-degree reconcile alarm cannot evaluate.",
            err=True,
        )
    if suspected_ids:
        _escalate_phase0_suspected_stale_degree(
            attio=attio,
            backend=backend,
            record_ids=suspected_ids,
        )
        click.echo(
            f"  ⚠ {len(suspected_ids)} CONNECTION_SENT row(s) scraped as "
            f"invite-resolved (hasPendingInvitation=false) but NOT 1st-degree — "
            f"likely a stale SN degree or a declined invite. "
            f"phase0_suspected_stale_degree queue row opened for manual "
            f"reconcile against LinkedIn 'My Network'.",
            err=True,
        )
    click.echo(f"  Checked {len(scrape_batch)} profiles, {accepted} accepted.")
    _echo_phase0_prospect_summary(
        checked=len(prospect_cands),
        accepted=prospects_accepted,
        regressions=prospect_regressions_flagged,
    )
    if helper_escalate_failures:
        # Wave-1.6.3: surface swallowed escalate() failures in the Phase 0
        # ACCEPTED-flip path. Per-row WARN already named each; this is the
        # paging-level rollup mirroring run_connection_requests.
        sites = ", ".join(
            sorted({f["site"] for f in helper_escalate_failures})
        )
        click.echo(
            f"  ❌ ERROR: {len(helper_escalate_failures)} attio_write_failed "
            f"escalate() call(s) failed during Phase 0 ACCEPTED flips "
            f"(sites: {sites}). The operator review queue is missing the "
            f"reconciliation rows. Inspect the per-row WARN log lines "
            f"above for the underlying exceptions.",
            err=True,
        )
    return {
        "accepted": accepted,
        "checked": len(conn_sent),
        "scraped": len(scrape_batch),
        "cache_hits": cache_hit_count,
        "deferred": deferred,
        "prospects_checked": len(prospect_cands),
        "prospects_accepted": prospects_accepted,
        "prospect_regressions_flagged": prospect_regressions_flagged,
    }


def _advance_already_processed_rows(
    to_send_data: list[dict],
    *,
    attio: AttioClient,
    list_id: str,
    today: str,
    audit_logger: "AuditLogger | None" = None,
    escalate_failures: list | None = None,
) -> int:
    """Advance prospects PB reported as already-invited to CONNECTION_SENT.

    Layer 2 of the Pattern-A re-prospecting fix. The caller invokes this ONLY
    when ``SendOutcome.already_processed`` is True — PB Auto Connect's explicit
    ``input-already-processed`` dedup signal, NEVER a generic Skipped (a cap or
    error must not be mistaken for "already invited", which would falsely mark
    un-invited prospects as CONNECTION_SENT). These rows were stuck at PROSPECT,
    re-queued every run because the advance gate only advanced on "Message
    sent"; advancing them removes them from the invite pool and lets Phase 0
    watch for acceptance. No invite was sent, so the daily cap is NOT charged.

    Cohort semantics mirror the Pattern-A flip (NOT the invite-success path):
    the invite happened in a PRIOR launch, so we PRESERVE the prior
    ``experiment_id`` (never write it, never back-fill the currently-running
    cohort — that would contaminate measurement with a historical event) and
    re-stamp ``experiment_id_frozen_at="connection_sent"`` only when the row
    already carried an experiment AND its prior frozen_at is not a
    terminal/immutable value (re-stamping over accepted/legacy_* would be an
    immutability violation).

    Returns the count of entries advanced.
    """
    advanced = 0
    for row in to_send_data:
        advance_attrs: dict = {
            "stage": PipelineStage.CONNECTION_SENT.value,
            "last_contact_date": today,
        }
        prior_experiment_id = row.get("experiment_id")
        prior_frozen_at = row.get("experiment_id_frozen_at")
        if (
            prior_experiment_id is not None
            and prior_frozen_at not in _IMMUTABLE_FROZEN_AT_VALUES
        ):
            advance_attrs["experiment_id_frozen_at"] = "connection_sent"
        for entry_id in row.get("entry_ids") or [row.get("entry_id")]:
            if not entry_id:
                continue
            # Count only confirmed advances. A failed write is NOT silent —
            # _attio_advance_with_escalation opens the attio_write_failed DLQ
            # row + echoes — and the row stays at PROSPECT to retry; the count
            # must not overstate success.
            if _attio_advance_with_escalation(
                attio=attio,
                entry_id=entry_id,
                entry_attributes=advance_attrs,
                list_id=list_id,
                linkedin_url=row.get("linkedInUrl", ""),
                today=today,
                step_label="invite",
                writer_module="workflows.daily_check.run_connection_requests",
                prior_stage=row.get("current_stage")
                            or row.get("stage")
                            or PipelineStage.PROSPECT.value,
                person_record_id=row.get("record_id"),
                audit_logger=audit_logger,
                escalate_failures=escalate_failures,
            ):
                advanced += 1
    return advanced


def _build_invite_send_data(
    prospects: list[dict],
    *,
    target: int,
    attio: AttioClient,
    cache: RecordCache,
    today: date,
    audit_logger: "AuditLogger | None",
    dry_run: bool,
) -> tuple[list[dict], dict]:
    """Scan the sorted `prospects` pool in order and accumulate up to
    `target` connection-note rows into `to_send_data`.

    Backfill semantics (port of upstream #156): rows skipped by the §3.8
    per-company throttle, a missing language/copy escalation, a missing
    LinkedIn URL, or the within-run same-company guard do NOT consume a
    slot — scanning continues so the target is filled from later rows.
    The pre-#156 behaviour iterated `prospects[:target]`, so a throttled
    or duplicate-company row burned a slot and the 25/day cap was left
    chronically under-filled (9 sent vs 371 eligible). Stops at `target`
    sends or pool exhaustion.

    Within-run company dedup: `company_throttle_permits` reads
    `last_outreach_at`, which is only written AFTER a confirmed send, so
    two prospects at the same un-throttled company would both clear the
    throttle in one run. `seen_company_ids` holds each company to one
    invite per run. `company_id is None` prospects are never added to the
    set and never deduped against it (mirrors the throttle's permissive
    None handling). NB: "company_id is None" covers both a prospect with
    no linked company AND one whose Attio `company` field is a plain value
    rather than a `target_record_id` reference (no `_person_to_company`
    entry) — neither is deduped.

    Returns `(to_send_data, skip_counts)` where skip_counts has keys
    `company_throttled`, `same_company_run`, `missing_language`,
    `missing_copy`, `missing_url`.
    """
    to_send_data: list[dict] = []
    seen_company_ids: set[str] = set()
    counts = {
        "company_throttled": 0,
        "same_company_run": 0,
        "missing_language": 0,
        "missing_copy": 0,
        "missing_url": 0,
    }

    for attrs in prospects:
        if len(to_send_data) >= target:
            break
        name, company, linkedin_url, industry_raw, title = cache.get(attrs["record_id"])
        if not linkedin_url:
            # L1-4: escalate so the operator can fix the missing URL in Attio.
            # Previously a silent counter-only skip — invisible in the queue.
            record_id = str(attrs["record_id"])
            escalate(
                type="missing_linkedin_url",
                idempotency_key=f"missing_url|{record_id}",
                payload={
                    "record_id": record_id,
                    "name": name or None,
                    "company": company or None,
                },
                attio=attio,
            )
            click.echo(
                f"  ⚠ Skipping {name or record_id}: no LinkedIn URL — "
                f"missing_linkedin_url queue row opened.",
                err=True,
            )
            counts["missing_url"] += 1
            continue
        # §3.8 per-company throttle (Attio 14-day window). cache.get above
        # has populated `attio._person_to_company`; the check is cheap.
        if not _check_company_throttle_or_skip(
            attrs, attio=attio, today=today, audit_logger=audit_logger,
            dry_run=dry_run,
        ):
            counts["company_throttled"] += 1
            continue
        # Within-run dedup: one invite per company per run. last_outreach_at
        # is only written post-send, so two same-company rows would both
        # clear the throttle within a single run without this guard.
        company_id = _company_id_for_prospect(attio, str(attrs.get("record_id", "")))
        if company_id is not None and company_id in seen_company_ids:
            counts["same_company_run"] += 1
            continue
        persona = Persona.from_attio(attrs.get("persona", "operations_leaders"))
        # B-PD-001: language MUST be explicitly set on the Attio row. The
        # pre-PR-12 default-to-English silently shipped English DMs to
        # prospects whose `language` field was unset; missing language now
        # opens `missing_language` and skips the prospect.
        try:
            language = resolve_language(
                attrs, persona=persona.value, dm_step="connection_note"
            )
        except MissingLanguageError as exc:
            escalate(
                type="missing_language",
                idempotency_key=f"missing_lang|{attrs['record_id']}|connection_note",
                payload={
                    "record_id": str(attrs["record_id"]),
                    "persona": persona.value,
                    "language_value": exc.language,
                    "dm_step": "connection_note",
                    "error_msg": str(exc),
                },
                attio=attio,
            )
            click.echo(
                f"  ⚠ Skipping {name or attrs['record_id']}: {exc} — "
                f"missing_language queue row opened.",
                err=True,
            )
            counts["missing_language"] += 1
            continue
        # PR-16 (B-PD-005): wrap get_message in MissingMessageError catch.
        try:
            template = get_message(
                persona, language, MessageStep.CONNECTION_NOTE,
                record_id=str(attrs["record_id"]),
            )
        except MissingMessageError as exc:
            escalate(
                type="missing_copy",
                idempotency_key=f"missing_copy|{attrs['record_id']}|connection_note",
                payload={
                    "record_id": str(attrs["record_id"]),
                    "persona": exc.persona or "",
                    "language": exc.language or "",
                    "dm_step": exc.dm_step or "connection_note",
                    "variant": exc.variant or "default",
                    "error_msg": str(exc),
                },
                attio=attio,
            )
            click.echo(
                f"  ⚠ Skipping {name or attrs['record_id']}: {exc} — "
                f"missing_copy queue row opened.",
                err=True,
            )
            counts["missing_copy"] += 1
            continue
        # PR-14 fold-in: company may be None (RecordCache Unknown → None).
        note = personalize(
            template,
            name.split()[0] if name else "",
            company or "",
            industry=get_industry_label(industry_raw, language),
            language=language,
        )
        to_send_data.append({
            "linkedInUrl": linkedin_url,
            "message": note,
            "entry_id": attrs["entry_id"],
            "record_id": attrs.get("record_id", ""),  # PR-13 + PR-15
            "current_stage": attrs["stage"],  # PR-15: for AttioWriter.apply prior_values
            "name": name,
            "company": company,
            "title": title,
            # Thread the quarantine attr through to pre_invite_check for
            # defense-in-depth re-verification of the §3.1 gate.
            "invite_eligible_after": attrs.get("invite_eligible_after"),
            # PR-21 (Lesson 6): experiment cohort identity — REQUIRED KEYS.
            # Direct key access in pre_invite_check.py will raise KeyError
            # if these are absent. Both may be None (pre-PR-21 row or no
            # active experiment at PROSPECT-commit time) — that is valid.
            # Shape contract: `experiment_id: str | None`,
            #                 `experiment_id_frozen_at: str | None`.
            "experiment_id": attrs.get("experiment_id"),
            "experiment_id_frozen_at": attrs.get("experiment_id_frozen_at"),
        })
        if company_id is not None:
            seen_company_ids.add(company_id)

    return to_send_data, counts


def run_connection_requests(
    attio: AttioClient,
    pb: PhantomBusterClient,
    network_booster_id: str,
    batch_size: int = 15,
    dry_run: bool = False,
    auto_confirm: bool = False,
    cache: RecordCache | None = None,
    profile_scraper_id: str | None = None,
    sales_nav_profile_scraper_id: str | None = None,
    audit_logger: AuditLogger | None = None,
    today: date | None = None,
    *,
    daily_run: DailyRun,
) -> dict:
    """Part A: Send connection requests to qualified prospects.

    `cache` may be supplied by the caller to reuse person records pre-fetched
    by earlier phases; if None, a fresh cache is built.

    Returns summary dict with counts.
    """
    # Shipped-placeholder send gate (no-op on dry_run): a fresh install ships
    # neutral placeholder DM copy with a sentinel; refuse to send it live.
    assert_content_replaced(dry_run=dry_run, filenames=("messages.json",))

    list_id = os.environ.get("ATTIO_LIST_ID", "")
    if cache is None:
        cache = RecordCache(attio)

    # PR-13 (§3.8): open the throttle-policy configuration_decision row
    # once per daily run so operators can revise the 30-day default.
    # Idempotent — see workflows.throttle.ensure_throttle_policy_decision_opened.
    ensure_throttle_policy_decision_opened(attio)

    # Query all entries — filter Prospects and CONNECTION_SENT
    all_parsed = _get_all_entries_parsed(attio)
    prospects = []
    connection_sent = []
    # Caller-supplied today (operator-TZ-aware); fall back to system date
    # for tests that omit it. Production cli.py always supplies it.
    today_op = today if today is not None else operator_today()
    quarantine_skipped = 0
    skipped_low_score = 0
    for attrs in all_parsed:
        if attrs["stage"] == PipelineStage.PROSPECT.value:
            score = attrs.get("quality_score")
            # L1-5: quality_score=None means the weekly pipeline never
            # stamped this record — data bug, not a legitimate filter.
            # Escalate so the operator can investigate and skip loudly.
            if score is None:
                record_id = str(attrs["record_id"])
                escalate(
                    type="missing_quality_score",
                    idempotency_key=f"missing_qs|{record_id}",
                    payload={"record_id": record_id},
                    attio=attio,
                )
                click.echo(
                    f"  ⚠ Skipping {record_id}: quality_score is None — "
                    f"weekly pipeline never stamped this record; "
                    f"missing_quality_score queue row opened.",
                    err=True,
                )
                continue
            # score < 60: legitimate silent filter. Track in skip-counts
            # so operators can see how many PROSPECTs were filtered.
            if int(score) < 60:
                skipped_low_score += 1
                continue
            # §3.1 defense: a fresh PROSPECT must clear its
            # invite_eligible_after quarantine before joining the invite
            # slice.
            if not is_invite_eligible(attrs, today_op):
                quarantine_skipped += 1
                continue
            # §3.10 defense (PR-22 fold-in): a PROSPECT-stage row stamped
            # with archaeology frozen_at (`legacy_pure_unknown` /
            # `legacy_inferred_by_archaeology`) MUST be excluded from the
            # invite slice. `is_invite_eligible` only checks quarantine; an
            # archaeology row that bypasses quarantine (no
            # invite_eligible_after attribute) would otherwise leak into
            # the invite path — a §3.1 hard red line.
            if not is_send_eligible(attrs):
                continue
            prospects.append(attrs)
        elif attrs["stage"] == PipelineStage.CONNECTION_SENT.value:
            connection_sent.append(attrs)
    if quarantine_skipped:
        click.echo(
            f"  Quarantine: held back {quarantine_skipped} fresh prospect(s) "
            f"whose invite_eligible_after has not yet elapsed."
        )

    # Sort by ICP priority then score so the daily slice picks the best
    # cohort first instead of relying on Attio's non-deterministic order.
    # Lane priority: enterprise_mode (ICP 1 — the primary strategic lane in
    # the shipped example program, see sales-program.md) >
    # target_company_mode (ICP 2 — opportunistic and phasing out) >
    # legacy (older scoring, pre-lane records).
    _LANE_RANK = _OUTREACH.lane_rank
    prospects.sort(key=lambda a: (
        _LANE_RANK.get(a.get("scoring_lane") or "legacy", 3),
        -int(a.get("quality_score") or 0),
        a.get("created_at") or "",
    ))

    # Respect daily limits (only new connections count). Backfill (port of
    # upstream #156): gate on can_send_connections(1) — "can we send at least
    # one?" — the canonical per-day early-out. The pre-#156 gate asked for
    # min(len(prospects), batch_size) capacity, which rejected an entire run
    # when fewer than a full batch of slots remained (defeating the fill-to-cap
    # backfill). `target` below bounds the actual count to remaining capacity.
    if not can_send_connections(1):
        click.echo(f"Daily connection limit reached.\n{get_status()}")
        return {"sent": 0, "pb_queued": 0, "skipped": len(prospects), "reason": "daily_limit", "skipped_low_score": skipped_low_score}

    remaining = get_remaining()
    # Split-brain fix (port of upstream #182): the daily_run row is the
    # authoritative cross-run cap ledger (PR-17 introduced it for DMs; this
    # extends it to invites). The local file gate above stays as a
    # belt-and-braces mirror — the target takes the MIN of both sources so
    # enforcement is never weaker than either ledger alone.
    remaining_attio = daily_run.remaining("connections")
    if remaining_attio <= 0:
        click.echo("Daily connection limit reached (daily_run counter).")
        return {"sent": 0, "pb_queued": 0, "skipped": len(prospects), "reason": "daily_limit", "skipped_low_score": skipped_low_score}
    # Target = how many invites to fill this run. Bounded by the remaining
    # daily connection capacity from BOTH the local file and the daily_run row,
    # plus the batch_size knob. The trim echo names the binding ledger so an
    # operator reconciling a trimmed run against the log looks at the right one.
    target = min(remaining["connections"], remaining_attio, batch_size)
    if target < batch_size:
        if remaining_attio < remaining["connections"]:
            click.echo(
                f"  Target trimmed to {target} (daily_run remaining: "
                f"{remaining_attio}; local file: {remaining['connections']})."
            )
        else:
            click.echo(
                f"  Target trimmed to {target} (remaining daily connections: "
                f"{remaining['connections']})."
            )

    # Prepare connection notes for new prospects. Backfill scan (port of
    # upstream #156): scan the FULL sorted eligible pool to `target` sends,
    # backfilling past throttled / duplicate-company / missing-copy rows
    # instead of capping at `target` *rows scanned* (the pre-#156 behaviour,
    # which left the 25/day cap chronically under-filled). `_build_invite_send_data`
    # owns the per-company dedup + skip tally; the reconciliation echo below
    # makes a short fill visible to the operator.
    to_send_data, skip_counts = _build_invite_send_data(
        prospects, target=target, attio=attio, cache=cache, today=today_op,
        audit_logger=audit_logger, dry_run=dry_run,
    )
    skipped_company_throttled = skip_counts["company_throttled"]
    skipped_missing_language = skip_counts["missing_language"]
    skipped_missing_copy = skip_counts["missing_copy"]
    skipped_same_company_run = skip_counts["same_company_run"]
    skipped_missing_url = skip_counts["missing_url"]
    # Operator reconciliation: one line showing the fill ratio and WHY it fell
    # short. Without this, a run that fills 9/25 because throttle+dedup
    # exhausted the distinct-company pool looks identical to a full run.
    click.echo(
        f"  Invite fill: {len(to_send_data)}/{target} slots — skipped "
        f"{skipped_company_throttled} throttled, {skipped_same_company_run} "
        f"same-company(run), {skipped_missing_language} missing-language, "
        f"{skipped_missing_copy} missing-copy, {skipped_missing_url} missing-url."
    )

    # Drop duplicate LinkedIn URLs — duplicate Attio records must not trigger
    # duplicate outbound messages.
    to_send_data, dropped_send = _dedupe_by_linkedin_url(to_send_data)
    if dropped_send:
        click.echo(f"  Dropped {len(dropped_send)} duplicate prospect URL(s): {dropped_send[:3]}{'...' if len(dropped_send) > 3 else ''}")

    # Pre-send guard: refuse to ship any row with an unresolved [...] placeholder.
    _assert_no_unresolved_placeholders(to_send_data, "connection_note")

    # Pre-invite degree check: scrape LinkedIn degree for each PROSPECT and
    # partition out anyone who's already a 1st-degree connection (flip them to
    # ACCEPTED instead of inviting). Catches Pattern A re-prospects and Pattern
    # B externally-connected prospects before LinkedIn no-ops the invite and
    # Phase 0 misclassifies the no-op as a fresh acceptance.
    # PR-15 (B-SD-010): always invoke `_pre_invite_degree_check` when
    # there's send data, regardless of whether a scraper-id is set.
    # The function itself handles STRICT mode (raises ConfigError when
    # scraper-id is missing on the send_invite codepath) and the
    # Pattern-A flip carve-out (cache_hit_flip path proceeds even
    # without a scraper-id). The pre-PR-15 silent-bypass `elif not
    # profile_scraper_id` branch — which warned and proceeded with
    # un-verified invites — was the §0 #9 violation closed here.
    pre_invite_already_connected: list[dict] = []
    if to_send_data:
        click.echo(f"  Pre-invite degree check: scraping/inspecting {len(to_send_data)} profiles...")
        to_send_data, pre_invite_already_connected = _pre_invite_degree_check(
            to_send_data, pb, profile_scraper_id, attio, list_id,
            sales_nav_profile_scraper_id=sales_nav_profile_scraper_id,
            today=today_op,
            # Wave-1.6 FIX-1: thread dry_run so the degree check is a
            # true no-op preview (no PB launch, no GSheet write, no
            # AttioWriter flip). Closes the 2026-05-25 leak.
            dry_run=dry_run,
            # A2 telemetry: emit the pattern_a_pending_flips heal-path event.
            audit_logger=audit_logger,
        )
        if pre_invite_already_connected:
            click.echo(
                f"  ✓ {len(pre_invite_already_connected)} already 1st-degree → "
                f"flipped to ACCEPTED, removed from invite queue:"
            )
            for row in pre_invite_already_connected:
                click.echo(f"      - {row['linkedInUrl']}")
        click.echo(f"  ✓ {len(to_send_data)} confirmed 2nd/3rd-degree → invite queue")

    # Prepare re-check rows for CONNECTION_SENT (empty message — phantom just scrapes).
    # Cache-skip URLs visited within RECHECK_TTL_DAYS to avoid burning Network
    # Booster visits on the same profiles every day.
    recheck_candidates: list[dict] = []
    for attrs in connection_sent:
        _, _, linkedin_url, _, _ = cache.get(attrs["record_id"])
        if not linkedin_url:
            continue
        recheck_candidates.append({
            "linkedInUrl": linkedin_url,
            "message": "",
        })
    candidate_urls = [r["linkedInUrl"] for r in recheck_candidates]
    _, stale_recheck_urls = recheck_cache.partition(candidate_urls)
    stale_recheck_set = set(stale_recheck_urls)
    recheck_data = [r for r in recheck_candidates if r["linkedInUrl"] in stale_recheck_set]
    cache_hit_rechecks = len(recheck_candidates) - len(recheck_data)
    if cache_hit_rechecks:
        click.echo(
            f"  Cache hit: skipping {cache_hit_rechecks} re-check visits "
            f"(within {recheck_cache.RECHECK_TTL_DAYS} days)."
        )
    recheck_data, dropped_recheck = _dedupe_by_linkedin_url(recheck_data)
    if dropped_recheck:
        click.echo(f"  Dropped {len(dropped_recheck)} duplicate re-check URL(s).")

    # Split-brain fix (port of upstream #182): re-checks are profile visits —
    # trim to the remaining daily visit budget (MIN of the daily_run ledger and
    # the legacy local file, same dual-source rule as the invite target above).
    # Visits were previously charged but never gated; the visits_per_day cap now
    # actually binds. Overflow rows stay stale and re-queue tomorrow —
    # recheck_cache.record_many below only marks rows that remain in
    # recheck_data, so trimmed rows are NOT cache-suppressed. The echo names the
    # binding ledger so the operator reconciles against the right one.
    remaining_visits_attio = daily_run.remaining("visits")
    remaining_visits_local = remaining["visits"]
    remaining_visits = min(remaining_visits_attio, remaining_visits_local)
    if len(recheck_data) > remaining_visits:
        kept = max(0, remaining_visits)
        if remaining_visits_local < remaining_visits_attio:
            binding_ledger = (
                f"local file: {remaining_visits_local}; "
                f"daily_run: {remaining_visits_attio}"
            )
        else:
            binding_ledger = (
                f"daily_run: {remaining_visits_attio}; "
                f"local file: {remaining_visits_local}"
            )
        click.echo(
            f"  Trimming re-checks {len(recheck_data)} → {kept} "
            f"(remaining daily visit budget — {binding_ledger})."
        )
        recheck_data = recheck_data[:kept]

    combined = to_send_data + recheck_data

    if not combined:
        click.echo("No prospects or re-checks ready.")
        return {"sent": 0, "pb_queued": 0, "rechecked": 0, "skipped": 0, "skipped_low_score": skipped_low_score}

    click.echo(f"Prepared {len(to_send_data)} connection requests + {len(recheck_data)} re-checks.")

    if dry_run:
        for row in to_send_data:
            click.echo(
                f"  [DRY RUN] {row.get('name', '?')} @ {row.get('company', '?')} "
                f"| {row.get('title', '?') or '(no title)'}"
            )
            click.echo(f"    url: {row['linkedInUrl']}")
            click.echo(f"    msg: {row['message'][:80]}...")
        for row in recheck_data:
            click.echo(f"  [DRY RUN RE-CHECK] {row['linkedInUrl']}")
        return {
            "sent": 0, "pb_queued": len(to_send_data), "rechecked": 0,
            "dry_run": len(to_send_data), "dry_run_rechecks": len(recheck_data),
            "skipped_low_score": skipped_low_score,
        }

    if not auto_confirm and not click.confirm(f"Send {len(to_send_data)} connections + {len(recheck_data)} re-checks?"):
        click.echo("Cancelled.")
        return {"sent": 0, "pb_queued": len(to_send_data), "rechecked": 0, "cancelled": True, "skipped_low_score": skipped_low_score}

    # Write combined batch to Google Sheet and launch phantom.
    sheet_url = write_prospects_to_sheet(combined)
    click.echo(f"  Wrote {len(combined)} rows to Google Sheet.")
    launch_args = {"spreadsheetUrl": sheet_url, **_pb_session_args()}
    # Split-brain fix (port of upstream #182): mirror of the Part-B DM lease
    # (PR-17 B-SD-006). Reserve capacity on the daily_run row BEFORE PB is
    # touched; confirm with the actual charge (sent_for_cap / re-check count)
    # AS SOON AS it is known, in the same guarded span as the local-file
    # mirror so the two ledgers cannot drift on a mid-advance crash; release
    # in `finally` if anything raises in between. Invites and re-checks share
    # this one Network Booster launch, so both leases bracket the same
    # reserve -> confirm span. The trim above already enforced both caps, so
    # these reservations should always succeed — a CapacityExhausted here
    # means external state drift and must propagate (same contract as the DM
    # path).
    conn_lease: str | None = (
        daily_run.reserve_send("connections", len(to_send_data))
        if to_send_data else None
    )
    visit_lease: str | None = None
    try:
        if recheck_data:
            visit_lease = daily_run.reserve_send("visits", len(recheck_data))
        launch = pb.launch_agent(network_booster_id, launch_args)
        # Block until the phantom finishes — prevents workspace parallel-execution
        # cap when the next phantom (Message Sender) launches in Part B.
        completion = pb.wait_for_completion(
            launch, poll_interval=15, max_wait=1800
        )

        # F-PR-5 advance gate. Network Booster's CSV reports per-URL send status
        # the same way Message Sender does. Parse the outcome, then either
        # advance the rows PB confirmed OR take the dry-skip path with a
        # `pb_silent_no_op` queue row.
        csv_text = pb.download_result_csv(launch)
        requested_urls_for_send = {
            _normalize_linkedin_url(row["linkedInUrl"])
            for row in to_send_data
            if row.get("linkedInUrl")
        }
        outcome = parse_send_outcome(
            launch=launch,
            completion=completion,
            csv_text=csv_text,
            requested_urls=requested_urls_for_send,
        )
        # Network Booster (invite) override: its result.csv is unreliable for
        # per-run send confirmation (no `status` column, accumulates stale rows),
        # so the parsed outcome is always Skipped/0 and the gate never advanced
        # invites — they re-queued forever and Phase 0 never watched for accepts.
        # A clean authenticated launch optimistically advances all requested
        # invites; a dead-cookie / cap / restriction launch stays Skipped →
        # pb_silent_no_op. (DM sequencing keeps the strict CSV gate — its
        # `status` column is reliable.) Because the override flips csv_status to
        # "Message sent" with sent_urls = all requested, the standard per-row
        # advance branch below runs and sets sent_for_cap = outcome.sent_count
        # (= optimistically-advanced count); a cap-blocked launch leaves the
        # outcome Skipped → gate-fail branch → sent_for_cap = 0. The connections
        # lease then confirms with that value, so the cap charge matches the
        # rows actually advanced.
        outcome = compute_invite_outcome(outcome, completion, requested_urls_for_send)
        if outcome.drift_skipped_reason == INVITE_OPTIMISTIC_ADVANCE:
            click.echo(
                f"  ↪ Advancing {outcome.sent_count} invite(s) OPTIMISTICALLY — "
                f"Network Booster CSV is unreliable, launch was clean + authenticated "
                f"with no cap/restriction signal. If a LinkedIn cap was hit silently, "
                f"verify in the Invitation Manager (these are recoverable via the "
                f"`{INVITE_OPTIMISTIC_ADVANCE}` audit marker)."
            )
        # Stamp post-send dates with the operator-local date (today_op) that the
        # eligibility gate above used — NOT date.today() (UTC). This keeps
        # last_contact_date in the same TZ frame as is_invite_eligible / cadence
        # math, so an invite sent near UTC midnight doesn't record a next-day
        # last_contact_date and shift the whole DM cadence by a day. Mirrors the
        # operator_today() convention already used for the Phase-0 timeout key.
        today_iso = today_op.isoformat()
        # Returns None when no experiment is running (§0 #9 — no silent fallback to
        # baseline-v0). None is a valid experiment tag meaning "no active experiment".
        current_experiment_id = get_current_experiment_id()
        updated = 0
        # A2: rows advanced via the already-processed (Pattern-A) path. Counted
        # separately from `updated` (the regular per-row loop) so the summary
        # line reflects ALL Attio-confirmed advances, not just confirmed-send
        # advances.
        already_processed_advanced = 0
        # Wave-1.6.2 FIX-A (adversarial EXT-SB-1 BLOCKING): tally failed
        # `experiment_id_immutability_violation` escalate() calls so we can
        # surface a loud end-of-batch summary line. See the try/except at the
        # escalate() call site below for the full rationale.
        escalate_failed_count = 0

        # Wave-1.6.3 (adversarial follow-up): tally failed escalate() calls
        # raised inside the post-PB-send helpers (_attio_advance_with_escalation,
        # _write_company_throttle_tally). Both wrap escalate() in inner
        # try/except Exception to keep the per-row loop alive; the failure
        # record is appended here so the end-of-batch summary can surface
        # the count loudly without losing the row-by-row stage advances.
        helper_escalate_failures: list = []

        # E-fix: on a gate-fail (pb_silent_no_op) launch the re-check profiles
        # were NOT reliably visited (dead cookie / cap / restriction), so the
        # visit lease must confirm 0 and the recheck cache must NOT be stamped
        # — otherwise tomorrow's run skips a re-check that never happened.
        batch_advance_gate_failed = False

        if not should_advance_batch(launch, outcome):
            batch_advance_gate_failed = True
            if outcome.already_processed and not recheck_data:
                # Layer 2 (Pattern-A re-prospecting fix): PB Auto Connect reports
                # these profiles were already invited in a prior launch
                # ("input-already-processed"). They are NOT a silent no-op — advance
                # them to CONNECTION_SENT so they leave the invite pool (they were
                # stuck at PROSPECT, re-queuing forever because the gate only
                # advanced on "Message sent"). Phase 0 then watches for acceptance.
                #
                # GUARDS (pipeline-leakage red line — never mark an un-invited
                # prospect as CONNECTION_SENT):
                #   1. `outcome.already_processed` only — a cap/error Skipped never
                #      lands here (verified by SendOutcome tests).
                #   2. `not recheck_data` — the phantom is launched on
                #      `to_send_data + recheck_data` (already-invited CONNECTION_SENT
                #      re-check rows). The marker is batch-level, so on a MIXED batch
                #      the recheck rows could trip it even if some invite rows are
                #      genuinely new. Restrict the advance to invites-only batches
                #      where "every profile was already processed" unambiguously
                #      refers to the invite rows. Mixed batches fall through to the
                #      conservative pb_silent_no_op path (rows re-queue; Layer 1 +
                #      the recheck-cache clear drain the genuinely-pending ones).
                click.echo(
                    f"  ↪ PB reports input-already-processed for "
                    f"{len(to_send_data)} profile(s) — advancing to CONNECTION_SENT "
                    f"(already invited; Pattern-A fix), not opening pb_silent_no_op."
                )
                # A2: capture the advanced count (previously discarded) so the
                # summary line below reflects the already-processed advance path,
                # not just the regular per-row loop's `updated`.
                already_processed_advanced = _advance_already_processed_rows(
                    to_send_data,
                    attio=attio,
                    list_id=list_id,
                    today=today_iso,
                    audit_logger=audit_logger,
                    escalate_failures=helper_escalate_failures,
                )
                sent_for_cap = 0
            else:
                if outcome.already_processed:
                    # already_processed but the batch was mixed with re-check rows:
                    # do NOT advance (the batch-level marker is ambiguous about which
                    # rows it refers to). Leaving invites at PROSPECT to re-queue is
                    # the safe direction; opening pb_silent_no_op preserves the
                    # existing operator signal.
                    click.echo(
                        "  ⚠ input-already-processed fired on a batch mixed with "
                        "re-check rows; NOT advancing to avoid a false CONNECTION_SENT "
                        "(invites stay at PROSPECT and re-queue)."
                    )
                click.echo(
                    f"  ⚠ PB Network Booster advance gate FAILED "
                    f"(csv_status={outcome.csv_status}, sent={outcome.sent_count}, "
                    f"requested={outcome.requested_count}); opening pb_silent_no_op "
                    f"queue row and skipping Attio updates for this batch."
                )
                emit_pb_silent_no_op(
                    launch,
                    outcome,
                    attio=attio,
                    audit_logger=audit_logger,
                    experiment_id=current_experiment_id,
                )
                # Daily-cap counter charges only the actually-sent count — a
                # gate-failed batch with sent_count=0 must NOT consume tomorrow's
                # invite budget for sends that never happened.
                sent_for_cap = 0
        else:
            # Per-row Attio advance: only rows PB confirmed as "Message sent"
            # advance — others stay at PROSPECT so tomorrow's run retries.
            # Symmetric with the run_dm_sequencing 3-row-class partition:
            #   PB-confirmed-sent → stage flip
            #   PB-explicit-skipped (Already-1st-degree / Invite-limit / etc.)
            #       → no state mutation, open pb_inmail_dead_end queue row
            #       so the operator can flag the prospect out manually.
            #   PB-unreported → no state mutation, retried tomorrow
            try:
                for row in to_send_data:
                    url_key = _normalize_linkedin_url(row.get("linkedInUrl", ""))
                    if url_key in outcome.skipped_urls:
                        emit_pb_inmail_dead_end(
                            launch,
                            linkedin_url=url_key,
                            dm_step="invite",
                            pb_status="skipped_in_csv",
                            attio=attio,
                            audit_logger=audit_logger,
                            experiment_id=current_experiment_id,
                        )
                        continue
                    if url_key not in outcome.sent_urls:
                        # PB didn't confirm send for this row; leave at PROSPECT.
                        continue
                    # Wave-1.6 FIX-2: experiment_id immutability guard for the
                    # regular invite path. Pre-Wave-1.6 this branch silently
                    # overwrote any prior PROSPECT-stamped `experiment_id` with
                    # the experiment running NOW — meaning a row committed
                    # during exp-A and invited after the exp-A→exp-B switch
                    # had its cohort tag flipped to exp-B at invite time.
                    # Production rows from 2026-04-07 / 2026-04-20 confirmed
                    # the bug fired (see done-qa-advertiser-daily-pass1-round2
                    # BLOCKING-1). The Pattern-A flip path in
                    # pre_invite_check.py:_check_experiment_id_immutability
                    # was guarded; this branch was not.
                    #
                    # Wave-1.6-ext FIX-2' (adversarial SB-4): we used to `raise
                    # ExperimentIdImmutableError` here, but the raise fires
                    # AFTER PB has already physically launched all invites in
                    # the batch (line 849). Raising mid-loop orphans rows that
                    # PB sent for but haven't been Attio-advanced yet — those
                    # rows stay at PROSPECT and tomorrow's run re-invites them
                    # (the §3.1 hard red line FIX-2 was supposed to prevent).
                    #
                    # Behavior now: preserve prior (cohort tag honored), still
                    # advance the stage to CONNECTION_SENT (no re-invite), and
                    # open an Operator Review Queue row via
                    # `experiment_id_immutability_violation` so the cohort
                    # mismatch is visible. Continue processing the batch — no
                    # raise, no orphans. See done-qa-adversarial-pass1-round2
                    # finding SB-4.
                    prior_experiment_id = row.get("experiment_id")
                    effective_experiment_id = (
                        prior_experiment_id
                        if prior_experiment_id is not None
                        else current_experiment_id
                    )
                    if (
                        prior_experiment_id is not None
                        and current_experiment_id is not None
                        and prior_experiment_id != current_experiment_id
                    ):
                        # Wave-1.6.2 FIX-A (adversarial EXT-SB-1 BLOCKING):
                        # the escalate() call here MUST NOT take down the loop.
                        # If it raises (transient Attio 5xx, network blip, payload
                        # schema drift during F1 deploy race, etc.), the
                        # surrounding `for row in to_send_data:` loop terminates
                        # and rows AFTER this offender never get the
                        # CONNECTION_SENT stage advance at lines below. Since PB
                        # has already physically launched invites for those rows
                        # (line 849), tomorrow's run would re-invite them — the
                        # exact §3.1 red-line FIX-2'/FIX-A was sold as fixing.
                        #
                        # The catch is intentionally broad (Exception) BECAUSE the
                        # alternative (orphan re-invites) is strictly worse than
                        # a swallowed escalate failure. We log loud + tally for
                        # an end-of-batch ERROR summary; we DO NOT skip the
                        # stage advance. The escalate call is best-effort — the
                        # row's experiment_id_frozen_at="connection_sent" stamp
                        # below preserves the prior cohort so the immutability
                        # semantic FIX-2' protects still holds at the data layer.
                        try:
                            escalate(
                                type="experiment_id_immutability_violation",
                                idempotency_key=(
                                    f"experiment-id-immutability"
                                    f"|{row.get('record_id') or ''}"
                                    f"|{prior_experiment_id}"
                                    f"|{current_experiment_id}"
                                ),
                                payload={
                                    "record_id": str(row.get("record_id") or ""),
                                    "entry_id": row.get("entry_id") or "",
                                    "prior_experiment_id": prior_experiment_id,
                                    "current_experiment_id": current_experiment_id,
                                    "effective_experiment_id": prior_experiment_id,
                                    "context": (
                                        "regular_invite_success_path: PB confirmed "
                                        "send. Preserving prior experiment_id; row "
                                        "still advances to CONNECTION_SENT so it "
                                        "does not re-invite tomorrow."
                                    ),
                                },
                                attio=attio,
                            )
                        except Exception as esc_exc:  # noqa: BLE001 — see comment above
                            escalate_failed_count += 1
                            click.echo(
                                f"  ⚠ escalate(experiment_id_immutability_violation) "
                                f"failed for record_id={row.get('record_id')!r} "
                                f"(prior_experiment_id={prior_experiment_id!r}, "
                                f"current_experiment_id={current_experiment_id!r}) "
                                f"[{type(esc_exc).__name__}]: {esc_exc}. "
                                f"Continuing batch — stage advance still fires so "
                                f"the row is NOT re-invited tomorrow. End-of-batch "
                                f"summary will surface this failure.",
                                err=True,
                            )

                    for entry_id in row.get("entry_ids") or [row.get("entry_id")]:
                        if not entry_id:
                            continue
                        # PR-21 (Lesson 4 / fold-in BLOCKING-1): invite-success path
                        # must also stamp experiment_id_frozen_at="connection_sent"
                        # so the `connection_sent` enum value is actually used. Guard
                        # by effective_experiment_id is not None — no stamp on
                        # no-experiment rows (mirrors Phase 0 pattern in
                        # detect_accepted_connections).
                        invite_advance_attrs: dict = {
                            "stage": PipelineStage.CONNECTION_SENT.value,
                            "last_contact_date": today_iso,
                            "experiment_id": effective_experiment_id,
                        }
                        if effective_experiment_id is not None:
                            invite_advance_attrs["experiment_id_frozen_at"] = "connection_sent"
                        _attio_advance_with_escalation(
                            attio=attio,
                            entry_id=entry_id,
                            entry_attributes=invite_advance_attrs,
                            list_id=list_id,
                            linkedin_url=row.get("linkedInUrl", ""),
                            today=today_iso,
                            step_label="invite",
                            writer_module="workflows.daily_check.run_connection_requests",
                            # Wave-2-B fix-up (code-reviewer B1): real prior
                            # from the to_send_data row, not the upstream-
                            # filter invariant (PROSPECT). The row dict is
                            # built upstream from the eligible-prospect
                            # parsed-entry attrs at line ~1140 — `current_stage`
                            # holds the actual value at PB-send time.
                            prior_stage=row.get("current_stage")
                                        or row.get("stage")
                                        or PipelineStage.PROSPECT.value,
                            person_record_id=row.get("record_id"),
                            audit_logger=audit_logger,
                            escalate_failures=helper_escalate_failures,
                        )
                        updated += 1
                    # PR-13 (§3.15): tally Companies.last_outreach_at + 3 siblings
                    # right after each confirmed invite so multi-thread ABM
                    # scenarios (two persons at same company in same batch)
                    # don't both leak through the throttle.
                    person_record_id = row.get("record_id")
                    if person_record_id:
                        # `today_iso` is the iso-string assigned earlier in this
                        # function (the post-PB-send block). Parse back to date for
                        # the tally helper's typed signature; the helper writes
                        # the ISO datetime back to Attio internally.
                        today_date = date.fromisoformat(today_iso)
                        _write_company_throttle_tally(
                            attio=attio,
                            company_id=_company_id_for_prospect(attio, person_record_id),
                            person_record_id=person_record_id,
                            step_label="invite",
                            experiment_id=current_experiment_id,
                            today=today_date,
                            writer_module="workflows.daily_check.run_connection_requests",
                            audit_logger=audit_logger,
                            escalate_failures=helper_escalate_failures,
                        )
            except Exception:
                # The deliberately re-raised programmer-bug classes
                # (UnauthorizedAttioWriteError / monotonicity / terminal-class
                # regression) from _attio_advance_with_escalation — or any other
                # raise inside this advance loop — abort the batch BEFORE the
                # confirm below, so the `finally` REFUNDS the connections + visits
                # leases even though PB already physically launched this invite
                # batch. Mirror Part A's post-launch charge-failure echo so the
                # abort never looks like a clean pre-send failure.
                click.echo(
                    f"  ❌ ERROR: PB Network Booster launch completed "
                    f"({len(to_send_data)} invite(s) sent) but advancing the rows "
                    f"FAILED before the daily_run charge. The invites MAY HAVE BEEN "
                    f"PHYSICALLY SENT but are now UNCHARGED (the connections + visits "
                    f"leases refund on this abort). Verify today's sends in the "
                    f"LinkedIn Invitation Manager and reconcile the daily_run "
                    f"counters before the next run.",
                    err=True,
                )
                raise
            # Charge the cap for confirmed-sent rows only.
            sent_for_cap = outcome.sent_count

        # Charge the daily_run row + local mirror in lockstep, AS SOON AS
        # sent_for_cap is known. sent_for_cap is the same value the legacy
        # local path charged in every reachable branch (0 on a gate-fail /
        # silent-no-op / already-processed batch, outcome.sent_count on a
        # per-row advance-pass). record_connections fires inside this guarded
        # span so a transport error at confirm time leaves neither ledger
        # charged.
        try:
            if conn_lease is not None:
                daily_run.confirm_lease(conn_lease, confirmed_count=sent_for_cap)
                conn_lease = None
                # Legacy local mirror (~/.outbound-agent/daily_limits.json):
                # still read by `cli limits` / get_status() and the local
                # belt-and-braces gate at the top of this function. Charged
                # here, in lockstep with the daily_run confirm.
                record_connections(sent_for_cap)
            if visit_lease is not None:
                # Re-checks are profile visits; a clean launch visited them all
                # — the CSV carries no per-run visit status to refund from, so
                # the full reservation commits. But on a gate-fail
                # (pb_silent_no_op) the launch did NOT cleanly visit anything,
                # so confirm 0 (refunds the reservation) rather than charging
                # for visits that never happened.
                visits_charged = 0 if batch_advance_gate_failed else len(recheck_data)
                daily_run.confirm_lease(visit_lease, confirmed_count=visits_charged)
                visit_lease = None
                record_visits(visits_charged)
        except (httpx.HTTPStatusError, httpx.RequestError):
            # PB has ALREADY sent this batch — only the daily_run charge
            # failed (confirm_lease rolled back and re-raised). Without this
            # echo the abort looks like a clean pre-send failure. The rows
            # advanced above stay advanced; tomorrow's run re-queues anything
            # that did not. The pre-invite degree check + PB's
            # input-already-processed marker are the safety nets against an
            # actual duplicate invite.
            click.echo(
                f"  ❌ ERROR: PB launch completed ({sent_for_cap} invite(s) "
                f"sent, {len(recheck_data)} re-check(s)) but charging the "
                f"daily_run row FAILED on a transport error. Verify today's "
                f"sends in the LinkedIn Invitation Manager before the next run.",
                err=True,
            )
            raise
    finally:
        # Release only what confirm_lease didn't consume — any raise in the
        # reserve -> launch -> wait -> csv -> parse -> advance -> confirm span
        # refunds the reservation (mirrors the DM block's quota-leak guard; an
        # unreleased lease would silently starve tomorrow's budget for
        # capacity never used).
        if conn_lease is not None:
            daily_run.release_lease(conn_lease)
        if visit_lease is not None:
            daily_run.release_lease(visit_lease)
    # Mark visited URLs in the recheck cache so tomorrow's run skips them.
    # Skip on a gate-fail: those profiles were NOT reliably visited, so
    # stamping them would suppress a needed re-check next run.
    if recheck_data and not batch_advance_gate_failed:
        recheck_cache.record_many({row["linkedInUrl"]: None for row in recheck_data})
    # L3-3: honest summary. "queued N" = prepared for PB; "advanced M" =
    # Attio-confirmed. A2: the total now spans BOTH advance paths — the
    # regular per-row loop (`updated`, confirmed sends) AND the already-
    # processed Pattern-A path (`already_processed_advanced`). Pre-fix the
    # latter was discarded, so a batch healed entirely via already-processed
    # logged "advanced 0" despite every row advancing. Print both when they
    # differ so operators can spot real PB send-phantom gaps.
    total_advanced = updated + already_processed_advanced
    if total_advanced != len(to_send_data):
        click.echo(
            f"Connection requests: queued {len(to_send_data)}, "
            f"advanced {total_advanced} (Attio-confirmed"
            + (f"; {already_processed_advanced} via already-processed"
               if already_processed_advanced else "")
            + ")."
        )
    else:
        click.echo(f"Sent {len(to_send_data)} connection requests. Attio updated: {total_advanced}/{len(to_send_data)}.")
    if escalate_failed_count > 0:
        # Wave-1.6.2 FIX-A: a swallowed escalate() failure is acceptable
        # (the alternative is orphan re-invites — see the try/except in
        # the loop above), but the operator MUST see it in the daily log
        # so the queue gap can be investigated. ERROR level for paging.
        click.echo(
            f"  ❌ ERROR: {escalate_failed_count} "
            f"experiment_id_immutability_violation escalate() call(s) "
            f"failed during this batch. Stage advances still fired so "
            f"PB-sent rows will NOT re-invite tomorrow, but the operator "
            f"review queue is missing those rows. Inspect the per-row "
            f"WARN log lines above for the underlying exceptions.",
            err=True,
        )
    if helper_escalate_failures:
        # Wave-1.6.3: same calibration as FIX-A, surfaced for the helpers
        # _attio_advance_with_escalation and _write_company_throttle_tally.
        # The per-row WARN already named each failure; this is the
        # paging-level rollup so the daily-run summary makes the queue
        # gap inspectable without scrolling the per-row log.
        sites = ", ".join(
            sorted({f["site"] for f in helper_escalate_failures})
        )
        click.echo(
            f"  ❌ ERROR: {len(helper_escalate_failures)} attio_write_failed "
            f"escalate() call(s) failed during the invite batch "
            f"(sites: {sites}). Per-row stage advances behave per their "
            f"original semantics (failed advance → row stays at prior "
            f"stage; failed tally → throttle write lost this cycle) but "
            f"the operator review queue is missing the reconciliation "
            f"rows. Inspect the per-row WARN log lines above for the "
            f"underlying exceptions.",
            err=True,
        )
    click.echo(f"Re-checking {len(recheck_data)} CONNECTION_SENT profiles.")
    if skipped_missing_language:
        click.echo(
            f"Skipped {skipped_missing_language} prospect(s) with missing/invalid "
            f"language — see `missing_language` Operator Review Queue rows."
        )
    if skipped_company_throttled:
        throttle_row_note = (
            "queue rows suppressed (dry-run preview)"
            if dry_run
            else "see `company_throttled` queue rows"
        )
        click.echo(
            f"Skipped {skipped_company_throttled} prospect(s) for per-company "
            f"throttle (§3.8 {DEFAULT_THROTTLE_WINDOW_DAYS}-day window) — {throttle_row_note}."
        )
    if skipped_missing_copy:
        click.echo(
            f"Skipped {skipped_missing_copy} prospect(s) with missing/empty "
            f"message copy — see `missing_copy` Operator Review Queue rows."
        )
    return {
        "sent": len(to_send_data),
        "pb_queued": len(to_send_data),  # L3-3: honest prep count
        "attio_updated": updated,
        "rechecked": len(recheck_data),
        "skipped_missing_language": skipped_missing_language,
        "skipped_company_throttled": skipped_company_throttled,
        "skipped_missing_copy": skipped_missing_copy,
        "skipped_same_company_run": skipped_same_company_run,
        "skipped_missing_url": skipped_missing_url,
        "skipped_low_score": skipped_low_score,  # L1-5: visible in summary
    }


def run_dm_sequencing(
    attio: AttioClient,
    pb: PhantomBusterClient,
    message_sender_id: str,
    daily_run: DailyRun,
    dry_run: bool = False,
    auto_confirm: bool = False,
    cache: RecordCache | None = None,
    audit_logger: AuditLogger | None = None,
) -> dict:
    """Part B: Send DMs to accepted connections based on timing.

    `cache` may be supplied by the caller to reuse person records pre-fetched
    by earlier phases; if None, a fresh cache is built. Standalone debug runs
    of this function then print the duplicate-URL heartbeat as the first signal
    of progress.

    ``daily_run`` is the F-PR-8 capacity ledger and is required: PR-17's
    two-phase lease replaces the optimistic ``record_messages(sent_count)``
    path. Reservation lands BEFORE ``pb.launch_agent``, confirmation lands
    AFTER ``parse_send_outcome`` with the actual ``sent_count`` (any drift
    refunds to capacity), and a try/finally releases the lease if any step
    in between raises. Tests must construct a ``MagicMock(spec=DailyRun)``;
    the legacy ``daily_limits.json``-only path was removed in PR-17 fold-in
    to eliminate the silent-fallback risk (§0 #9).

    Returns summary dict with counts per DM step.
    """
    # Shipped-placeholder send gate (no-op on dry_run): a fresh install ships
    # neutral placeholder DM copy with a sentinel; refuse to send it live.
    assert_content_replaced(dry_run=dry_run, filenames=("messages.json",))

    # PR-19 B-SD-005 Part-B short-circuit: if Phase 0.5 reply detection
    # failed (no CSV from PB inbox scrape), DM3 firing risks
    # re-messaging a prospect whose reply may already be in the inbox.
    # §3.1 no-resend protection: halt before queue selection. Open a
    # ``dm_sequencing_blocked_on_reply_failure`` queue row + audit
    # event, return early. The pb_csv_empty row from detect_responses
    # is the upstream signal; this row is the downstream consequence
    # marker so operators correlate the two.
    if daily_run.get_reply_detection_status() == "failed":
        run_date = getattr(daily_run, "run_date", date.today().isoformat())
        # PR-19 fold-in (silent-failure-hunter Finding 4): log on
        # suppress so the failure is visible — the queue row is the
        # durable correlation marker; without it operators triaging
        # tomorrow only see the upstream pb_csv_empty row and have to
        # infer the downstream consequence.
        try:
            escalate(
                type="dm_sequencing_blocked_on_reply_failure",
                idempotency_key=f"{run_date}|dm_sequencing",
                payload={
                    "run_date": run_date,
                    "reason": "reply_detection_status=failed",
                    "upstream_signal": "pb_csv_empty",
                },
                attio=attio,
            )
        except (httpx.HTTPStatusError, httpx.RequestError) as q_exc:
            click.echo(
                f"  ⚠ dm_sequencing_blocked_on_reply_failure queue write "
                f"FAILED ({type(q_exc).__name__}: {q_exc}); audit event + "
                f"reply_detection_status on the daily_run row remain the "
                f"durable record.",
                err=True,
            )
        if audit_logger is not None:
            audit_logger.event(
                "dm_sequencing_short_circuited_on_reply_detection_failed",
                run_date=run_date,
            )
        click.echo(
            "  ⚠ Phase 0.5 reply detection FAILED — short-circuiting "
            "Part-B DM sequencing per §3.1 (would risk re-messaging a "
            "prospect whose reply sits unread in the inbox). Opening "
            "dm_sequencing_blocked_on_reply_failure queue row.",
            err=True,
        )
        return {
            "dm1": 0, "dm2": 0, "dm3": 0,
            "reason": "reply_detection_failed",
            "dry_run": {"dm1": 0, "dm2": 0, "dm3": 0},
        }

    list_id = os.environ.get("ATTIO_LIST_ID", "")

    # 2026-06-09 desync design §3: verify writer attrs against the live
    # schema before any send — wet AND dry (read-only check).
    assert_dm_writer_schema(attio, list_id, audit_logger=audit_logger)

    # Fix 1: keep the raw entries alongside the parsed-filtered list so the
    # consistency sweep can reuse the snapshot on write-free exits instead of
    # re-fetching the same ~50k-entry list (up to ~500 paged round-trips).
    _raw_entries_snapshot: list[dict] | None
    _raw_entries_snapshot, all_parsed = _get_all_entries_with_raw(attio)
    today = date.today()
    if cache is None:
        cache = RecordCache(attio)

    def _consistency_sweep_epilogue(
        results: dict, *, entries_snapshot: list[dict] | None = None
    ) -> None:
        # 2026-06-09 desync design §2 — runs on EVERY post-preflight exit
        # (incl. no-DMs-due / trimmed / cancelled): prior-day desyncs must
        # not wait for a day that happens to send DMs.
        # entries_snapshot: when not None, the sweep reuses this raw list
        # instead of re-fetching (valid only on write-free exits — see
        # individual call sites below).
        try:
            sweep = run_company_tally_consistency_sweep(
                attio=attio,
                list_id=list_id,
                today=today,
                dry_run=dry_run,
                advance_fn=_attio_advance_with_escalation,
                audit_logger=audit_logger,
                entries=entries_snapshot,
            )
            results["consistency_sweep"] = sweep
            company_errors = sweep.get("company_errors", 0)
            dry_run_divergent = sweep.get("dry_run_divergent", 0)
            aborted = sweep.get("aborted_invariant_violation")
            entries_unparseable = sweep.get("entries_unparseable", 0)
            circuit_open = sweep.get("repair_circuit_open", False)
            cap_hit = sweep.get("company_cap_hit", False)
            escalate_failed = sweep.get("escalate_failed", 0)
            line = (
                f"  Consistency sweep: {sweep['companies_checked']} "
                f"companies stamped in window — {sweep['consistent']} "
                f"consistent, {sweep['repaired']} repaired, "
                f"{sweep['escalated']} escalated, "
                f"{sweep['skipped_no_floor'] + sweep['skipped_malformed_stamp']} skipped"
                + (f", {company_errors} errored" if company_errors > 0 else "")
                + (f", {dry_run_divergent} divergent (dry-run)" if dry_run_divergent > 0 else "")
                # unparseable raw entries can hide divergences as false
                # "no_list_entry" — operators must see it, not just audit.
                + (f", {entries_unparseable} entries unparseable" if entries_unparseable > 0 else "")
                # escalate_failed = a divergence was found but its operator
                # queue row never landed; the desync is invisible without it.
                + (f", {escalate_failed} escalate(s) FAILED" if escalate_failed > 0 else "")
                # cap hit = an unknown number of stamped companies were
                # never checked; a "clean" line would be a lie.
                + (" COMPANY CAP HIT (some companies unchecked)" if cap_hit else "")
                + (" REPAIR CIRCUIT OPEN" if circuit_open else "")
                + (f" ABORTED ({aborted})" if aborted else "")
                + "."
            )
            warn = (
                sweep.get("repaired")
                or sweep.get("escalated")
                or dry_run_divergent > 0
                or aborted
                or company_errors > 0
                or entries_unparseable > 0
                or escalate_failed > 0
                or circuit_open
                or cap_hit
            )
            if warn:
                click.echo(f"⚠ {line}", err=True)
            else:
                click.echo(line)
        except Exception as exc:  # noqa: BLE001
            click.echo(
                f"  ❌ ERROR: consistency sweep failed ({type(exc).__name__}: "
                f"{exc}) — desync detection skipped this run; the PR #170 "
                f"throttle guard still quarantines any divergent row.",
                err=True,
            )
            if audit_logger is not None:
                audit_logger.event(
                    "consistency_sweep_failed",
                    error_class=type(exc).__name__,
                    error=str(exc)[:500],
                )

    # PR-13 (§3.8): open the throttle-policy configuration_decision row
    # once per daily run so operators can revise the 30-day default
    # before the next run. Idempotent — `(type, decision_key,
    # idempotency_key)` uniqueness ensures only ONE row exists.
    ensure_throttle_policy_decision_opened(attio)

    dm_queues: dict[MessageStep, list] = {
        MessageStep.DM1: [],
        MessageStep.DM2: [],
        MessageStep.DM3: [],
    }
    # PR-13 fold-in: count throttle-skipped prospects so the return
    # summary has parity with run_connection_requests + operators see
    # the daily aggregate alongside individual `company_throttled`
    # queue rows.
    queue_throttled_count = 0

    # Wave-1.6.3 (adversarial follow-up): tally failed escalate() calls
    # raised inside the post-PB-send helpers. Mirrors the counter in
    # run_connection_requests; see the helpers' inner try/except for
    # the orphan-prevention rationale.
    helper_escalate_failures: list = []

    # Build url -> max stage rank across all entries so duplicate Attio records
    # at divergent stages don't each queue their own DM for the same LinkedIn
    # URL. If any sibling entry is already at or past the next-stage we would
    # advance to, skip. Terminal stages (RESPONDED, NOT_INTERESTED, etc.) also
    # block further DMs.
    unique_record_ids = {a["record_id"] for a in all_parsed if a.get("record_id")}
    click.echo(
        f"  Building duplicate-URL stage-rank index "
        f"({len(all_parsed)} entries, {len(unique_record_ids)} unique records)..."
    )
    url_to_max_rank: dict[str, int] = {}
    url_to_stages: dict[str, list[tuple[str, str]]] = {}  # url -> [(stage, entry_id), ...]
    for attrs in all_parsed:
        try:
            s = PipelineStage(attrs["stage"])
        except ValueError:
            continue
        _, _, url, _, _ = cache.get(attrs["record_id"])
        if not url:
            continue
        key = _normalize_linkedin_url(url)
        rank = STAGE_RANK.get(s, 0)
        if rank > url_to_max_rank.get(key, -1):
            url_to_max_rank[key] = rank
        url_to_stages.setdefault(key, []).append((s.value, attrs["entry_id"]))

    # Surface URLs with duplicate Attio entries at divergent stages — the most
    # common source of repeat-DM bugs. Print before queueing so dry-run reveals
    # the mess even when no DMs fire.
    divergent: list[tuple[str, list[tuple[str, str]]]] = [
        (url, sorted(set(stages)))
        for url, stages in url_to_stages.items()
        if len({s for s, _ in stages}) > 1
    ]
    if divergent:
        click.echo(f"  ⚠ {len(divergent)} LinkedIn URL(s) have duplicate Attio entries at divergent stages:")
        for url, stages in divergent[:20]:
            stage_summary = ", ".join(f"{stg} ({eid[:8]}…)" for stg, eid in stages)
            click.echo(f"     {url}: {stage_summary}")
        if len(divergent) > 20:
            click.echo(f"     ... and {len(divergent) - 20} more")

    for attrs in all_parsed:
        stage_str = attrs["stage"]

        try:
            stage = PipelineStage(stage_str)
        except ValueError:
            continue

        # §3.10 send-eligibility gate. Archaeology-stamped rows
        # (experiment_id_frozen_at ∈ legacy_*) and terminal stages (except
        # NURTURE) must never queue a DM — the §3.1 no-resend hard red line.
        if not is_send_eligible(attrs):
            continue

        last_date_str = attrs.get("last_contact_date")
        if not last_date_str:
            # L1-6: ACCEPTED rows with null last_contact_date can never
            # receive DM1 because get_pending_dms requires a valid date.
            # They sit silently in the pipeline forever — escalate so the
            # operator can fix the missing accept date in Attio.
            # Do NOT auto-backfill: writing a fabricated date would corrupt
            # the cadence record and inflate dm_response_rate denominators.
            if stage == PipelineStage.ACCEPTED:
                record_id = str(attrs["record_id"])
                entry_id = str(attrs.get("entry_id", ""))
                escalate(
                    type="accepted_missing_last_contact_date",
                    idempotency_key=f"accepted_no_lcd|{record_id}",
                    payload={
                        "record_id": record_id,
                        "entry_id": entry_id,
                    },
                    attio=attio,
                )
                click.echo(
                    f"  ⚠ Skipping ACCEPTED {record_id}: null last_contact_date — "
                    f"DM1 eligibility cannot be computed; "
                    f"accepted_missing_last_contact_date queue row opened.",
                    err=True,
                )
            continue
        last_date = date.fromisoformat(str(last_date_str)[:10])

        pending_dm = get_pending_dms(
            stage,
            last_date,
            today,
            dm_step=int(attrs.get("dm_step") or 0),
        )
        if not pending_dm:
            continue

        # PR-12 (B-PD-003) §3.1 protection: honor the forward-only
        # stored cadence floor when present. See _is_blocked_by_stored_floor
        # for the contract; rows pre-dating PR-12 lack the attribute and
        # fall through to `get_pending_dms`'s in-memory eligibility.
        if _is_blocked_by_stored_floor(attrs, today, audit_logger):
            continue

        # Cross-URL guard: a sibling record for this LinkedIn URL may already be
        # at or past the stage we're about to advance to. Skip if so.
        _, _, url, _, _ = cache.get(attrs["record_id"])
        if url:
            key = _normalize_linkedin_url(url)
            next_rank = STAGE_RANK[NEXT_STAGE[pending_dm]]
            if url_to_max_rank.get(key, -1) >= next_rank:
                click.echo(
                    f"  Skipping {pending_dm.value} for {url} — "
                    f"sibling entry at rank {url_to_max_rank[key]} ≥ next-stage rank {next_rank}"
                )
                continue

        # PR-13 (B-PD-002): per-company throttle. `cache.get` above has
        # already populated `attio._person_to_company[record_id]`. The
        # throttle is the §3.1 second line of defense: even if THIS
        # prospect's per-person guards pass, the company may have been
        # contacted via a sibling person within the last 30 days.
        if not _check_company_throttle_or_skip(
            attrs, attio=attio, today=today, audit_logger=audit_logger,
            dry_run=dry_run,
        ):
            queue_throttled_count += 1
            continue

        dm_queues[pending_dm].append(attrs)

    total_messages = sum(len(q) for q in dm_queues.values())
    if total_messages == 0:
        if queue_throttled_count:
            # Never report a bare zero when the throttle ate the whole
            # queue — 2026-06-09 the operator saw "No DMs due today"
            # while 10 cadence-due DMs sat silently company-throttled.
            click.echo(
                f"No DMs due today ({queue_throttled_count} cadence-due "
                f"DM(s) skipped by the §3.8 company throttle — see "
                f"company_throttled queue rows)."
            )
        else:
            click.echo("No DMs due today.")
        _early = {"dm1": 0, "dm2": 0, "dm3": 0, "dry_run": {"dm1": 0, "dm2": 0, "dm3": 0}}
        # write-free exit: no list entries were advanced this run — reuse snapshot
        _consistency_sweep_epilogue(_early, entries_snapshot=_raw_entries_snapshot)
        return _early

    # --- DM1-first reservation, then DM3 > DM2 ----------------------------
    # ACCEPTED → DM1 momentum decays fast: a fresh-accept that waits 2-3
    # days for DM1 conveys agent-on-vacation, not high-touch. The ACCEPTED
    # pool is naturally small (typical: 1-5/day, capped by invite-accept
    # rate), so reserving it whole rarely starves DM2/DM3. Remaining budget
    # splits DM3 > DM2 oldest-first — DM3 keeps last-chance priority and
    # DM2 absorbs deferrals (its 5-day cadence tolerates a 1-day slip).
    remaining_cap = daily_run.remaining("messages")
    if total_messages > remaining_cap:
        click.echo(
            f"\n  ⚠ Queue total {total_messages} > remaining cap {remaining_cap}. "
            f"Reserving all DM1 first, then DM3 > DM2 oldest-first for the remainder."
        )

        # Sort each queue oldest-first (last_contact_date ASC).
        for q in dm_queues.values():
            q.sort(key=lambda a: str(a.get("last_contact_date") or ""))

        # Reserve DM1 in full. If DM1 alone exceeds the cap (unusual — would
        # mean >30 accepts in one day), keep the oldest cap-worth and drop
        # the rest, preserving the trim-to-cap invariant.
        dm1_q = dm_queues[MessageStep.DM1]
        if len(dm1_q) > remaining_cap:
            kept_dm1 = dm1_q[:remaining_cap]
            dropped_dm1 = dm1_q[remaining_cap:]
            click.echo(
                f"      dm1: keeping {len(kept_dm1)}/{len(dm1_q)} oldest; "
                f"deferring {len(dropped_dm1)} to next run."
            )
            dm_queues[MessageStep.DM1] = kept_dm1
            for step in (MessageStep.DM3, MessageStep.DM2):
                dropped_q = dm_queues[step]
                if dropped_q:
                    click.echo(f"      Skipping all {len(dropped_q)} {step.value} (DM1 consumed the cap).")
                dm_queues[step] = []
            budget = 0
        else:
            budget = remaining_cap - len(dm1_q)

        # Allocate remaining budget DM3 > DM2.
        for step in (MessageStep.DM3, MessageStep.DM2):
            q = dm_queues[step]
            if budget <= 0:
                if q:
                    click.echo(f"      Skipping all {len(q)} {step.value} (no budget left).")
                dm_queues[step] = []
                continue
            if len(q) > budget:
                kept = q[:budget]
                dropped = q[budget:]
                click.echo(
                    f"      {step.value}: keeping {len(kept)}/{len(q)} oldest; "
                    f"deferring {len(dropped)} to next run."
                )
                dm_queues[step] = kept
                budget -= len(kept)
            else:
                budget -= len(q)

        total_messages = sum(len(q) for q in dm_queues.values())
        click.echo(
            f"  Final queue after trim: DM1={len(dm_queues[MessageStep.DM1])}, "
            f"DM2={len(dm_queues[MessageStep.DM2])}, DM3={len(dm_queues[MessageStep.DM3])} "
            f"(total {total_messages})."
        )
        if total_messages == 0:
            click.echo("  Nothing left to send after trim.")
            _early = {"dm1": 0, "dm2": 0, "dm3": 0, "reason": "trimmed_to_zero", "dry_run": {"dm1": 0, "dm2": 0, "dm3": 0}}
            # write-free exit: trim zeroed the queue before any sends — reuse snapshot
            _consistency_sweep_epilogue(_early, entries_snapshot=_raw_entries_snapshot)
            return _early

    if not dry_run and not auto_confirm and not click.confirm(f"Send {total_messages} DMs across DM1/DM2/DM3?"):
        click.echo("Cancelled.")
        _early = {"dm1": 0, "dm2": 0, "dm3": 0, "cancelled": True, "dry_run": {"dm1": 0, "dm2": 0, "dm3": 0}}
        # write-free exit: user cancelled before any sends — reuse snapshot
        _consistency_sweep_epilogue(_early, entries_snapshot=_raw_entries_snapshot)
        return _early

    results: dict = {
        "dm1": 0,
        "dm2": 0,
        "dm3": 0,
        "skipped_corrupted_company": 0,
        "skipped_missing_language": 0,
        "skipped_company_throttled": queue_throttled_count,
        "skipped_missing_copy": 0,
        "dry_run": {"dm1": 0, "dm2": 0, "dm3": 0},
    }
    if queue_throttled_count:
        throttle_row_note = (
            "queue rows suppressed (dry-run preview)"
            if dry_run
            else "see `company_throttled` queue rows"
        )
        click.echo(
            f"  Skipped {queue_throttled_count} prospect(s) for per-company "
            f"throttle (§3.8 {DEFAULT_THROTTLE_WINDOW_DAYS}-day window) — {throttle_row_note}."
        )

    for step, queue in dm_queues.items():
        if not queue:
            continue

        click.echo(f"Sending {len(queue)} {step.value} messages...")

        rows = []
        for attrs in queue:
            name, company, linkedin_url, industry_raw, title = cache.get(attrs["record_id"])
            if not linkedin_url:
                continue
            # Guard: refuse to render a DM whose linked company carries the
            # LinkedIn-Clearbit corruption fingerprint. The `company` string
            # in that case is a real-looking but wrong employer name (e.g.
            # "Prolec GE" backed by a record whose domain is linkedin.com).
            # Letting the row through would ship a poisoned [Company]
            # substitution that the placeholder check below cannot catch.
            if attio.is_person_company_corrupted(attrs["record_id"]):
                click.echo(
                    f"  ⚠ Skipping {name} — linked company '{company}' shows "
                    f"LinkedIn-Clearbit fingerprint. record_id={attrs['record_id']}"
                )
                results["skipped_corrupted_company"] += 1
                continue
            persona = Persona.from_attio(attrs.get("persona", "operations_leaders"))
            try:
                language = resolve_language(
                    attrs, persona=persona.value, dm_step=step.value
                )
            except MissingLanguageError as exc:
                escalate(
                    type="missing_language",
                    idempotency_key=f"missing_lang|{attrs['record_id']}|{step.value}",
                    payload={
                        "record_id": str(attrs["record_id"]),
                        "persona": persona.value,
                        "language_value": exc.language,
                        "dm_step": step.value,
                        "error_msg": str(exc),
                    },
                    attio=attio,
                )
                click.echo(
                    f"  ⚠ Skipping {name or attrs['record_id']} ({step.value}): "
                    f"{exc} — missing_language queue row opened.",
                    err=True,
                )
                results["skipped_missing_language"] += 1
                continue
            # PR-16 (B-PD-005): MissingMessageError → missing_copy queue
            # row + skip. Pre-PR-16 silent Spanish fallback would have
            # shipped wrong-language DMs.
            try:
                template = get_message(
                    persona, language, step,
                    record_id=str(attrs["record_id"]),
                )
            except MissingMessageError as exc:
                escalate(
                    type="missing_copy",
                    idempotency_key=f"missing_copy|{attrs['record_id']}|{step.value}",
                    payload={
                        "record_id": str(attrs["record_id"]),
                        "persona": exc.persona or "",
                        "language": exc.language or "",
                        "dm_step": exc.dm_step or step.value,
                        "variant": exc.variant or "default",
                        "error_msg": str(exc),
                    },
                    attio=attio,
                )
                click.echo(
                    f"  ⚠ Skipping {name or attrs['record_id']} ({step.value}): "
                    f"{exc} — missing_copy queue row opened.",
                    err=True,
                )
                results["skipped_missing_copy"] += 1
                continue
            # PR-14: company or "" coerce prevents TypeError (see fold-in).
            message = personalize(
                template,
                name.split()[0] if name else "",
                company or "",
                industry=get_industry_label(industry_raw, language),
                language=language,
            )
            rows.append({
                "linkedInUrl": linkedin_url,
                "message": message,
                "entry_id": attrs["entry_id"],
                "record_id": attrs["record_id"],  # PR-13: for post-send company tally
                "name": name,
                "company": company,
                "title": title,
                "current_stage": attrs["stage"],
            })

        # Collapse duplicate LinkedIn URLs from duplicate Attio entries so the
        # same prospect never receives two copies of the same DM.
        rows, dropped = _dedupe_by_linkedin_url(rows)
        if dropped:
            click.echo(f"  Dropped {len(dropped)} duplicate {step.value} URL(s): {dropped[:3]}{'...' if len(dropped) > 3 else ''}")

        if not rows:
            # All rows for this step were filtered (missing url, dedup, or
            # corruption guard). Nothing to send for this step — don't burn
            # a PB launch on an empty Google Sheet.
            click.echo(f"  No {step.value} rows remain after filtering; skipping batch.")
            continue

        # Pre-send guard — abort the batch if any message still contains a
        # [...] placeholder. Safer to fail loudly than to ship literal tokens.
        _assert_no_unresolved_placeholders(rows, step.value)

        if dry_run:
            for row in rows:
                click.echo(
                    f"\n  [DRY RUN] {step.value} -> {row.get('name', '?')} @ {row.get('company', '?')} "
                    f"[stage={row.get('current_stage', '?')}]"
                )
                click.echo(f"    title: {row.get('title', '') or '(no title)'}")
                click.echo(f"    url: {row['linkedInUrl']}")
                click.echo(f"    msg: {row['message']}")
            results["dry_run"][step.value] = len(rows)
            continue

        # Write DM batch to Google Sheet and launch phantom
        sheet_url = write_prospects_to_sheet(rows)
        click.echo(f"  Wrote {len(rows)} {step.value} messages to Google Sheet.")
        launch_args = {
            "spreadsheetUrl": sheet_url,
            "message": "#message#",  # per-row from sheet column
            **_pb_session_args(),
        }
        # PR-17 B-SD-006: reserve capacity BEFORE PB is touched. The lease
        # captures the intent ("we are about to send N"); the post-launch
        # confirm_lease commits the actual sent_count. Drift refunds to
        # capacity. Trim already enforced the per-step cap above, so this
        # reservation should always succeed — a CapacityExhausted here
        # means external state drift (a parallel process mutated the
        # daily_run counter between trim and reserve) and must propagate.
        lease_token: str | None = daily_run.reserve_send("messages", len(rows))
        # PR-17 fold-in (5/6 QA convergence): try/finally spans the ENTIRE
        # reserve → confirm region so any exception between launch and
        # confirm (PBRunFailed/PBRunTimeout from wait_for_completion, raise
        # from download_result_csv, ValueError/KeyError from
        # parse_send_outcome) releases the lease. Prior code only guarded
        # pb.launch_agent — a wait_for_completion timeout (max_wait=1800)
        # would have left the reservation permanently held for the rest of
        # the run, silencing DM2/DM3 batches (§3.1 quota leak).
        try:
            # F-PR-5: typed launch + raises-on-error wait. PBRunFailed
            # bubbles up so the operator sees the failure instead of
            # silently skipping the batch.
            launch = pb.launch_agent(message_sender_id, launch_args)
            # Block until this DM batch finishes before launching the next
            # step (or anything else) — avoids PB workspace parallel cap.
            completion = pb.wait_for_completion(
                launch, poll_interval=15, max_wait=1800
            )

            # F-PR-5 advance gate (§3.1 chokepoint). Stage MAY advance iff
            # csv_status == "Message sent" AND container_id matches THIS
            # launch AND sent_count >= 1. Prior policy ("advance every
            # queued URL on PB success") was the §3.1 violation by
            # omission — PB silent-drops dropped from "Accepted" Attio
            # to ghost-advance, suppressing tomorrow's re-attempt.
            #
            # wait_for_completion has returned — PB ran and the DMs in this
            # batch may have been PHYSICALLY SENT. Any raise from here on
            # (CSV download, parse, or the confirm_lease PATCH) hits the
            # `finally` below and REFUNDS the messages lease, leaving the
            # cap uncharged for sends that actually went out. Mirror Part
            # A's post-launch charge-failure echo so the abort never looks
            # like a clean pre-send failure.
            try:
                result_csv = pb.download_result_csv(launch)
                requested_urls = {
                    _normalize_linkedin_url(row["linkedInUrl"])
                    for row in rows
                    if row.get("linkedInUrl")
                }
                outcome = parse_send_outcome(
                    launch=launch,
                    completion=completion,
                    csv_text=result_csv,
                    requested_urls=requested_urls,
                )

                # PR-17 B-SD-006: confirm the lease with PB-reported
                # sent_count AS SOON AS sent_count is known. Both branches
                # (advance-pass per-row updates + advance-fail
                # emit_pb_silent_no_op) charge capacity by what PB actually
                # sent. Setting lease_token=None tells finally not to
                # release an already-consumed lease.
                assert lease_token is not None
                daily_run.confirm_lease(
                    lease_token, confirmed_count=outcome.sent_count
                )
                lease_token = None
            except Exception:
                click.echo(
                    f"  ❌ ERROR: PB Message Sender launch for {step.value} "
                    f"completed but post-send processing FAILED (CSV download / "
                    f"parse / charging the daily_run row). The {len(rows)} DM(s) "
                    f"in this batch MAY HAVE BEEN PHYSICALLY SENT but are now "
                    f"UNCHARGED (the messages lease refunds on this abort). "
                    f"Verify today's sends in the LinkedIn inbox and reconcile the "
                    f"daily_run messages_sent counter before the next run.",
                    err=True,
                )
                raise
        finally:
            # Release the lease only if confirm_lease didn't consume it.
            # The reserve→launch→wait→csv→parse→confirm span can raise at
            # any point; this guarantees the reservation refunds to
            # capacity if any step fails after the reservation.
            if lease_token is not None:
                daily_run.release_lease(lease_token)

        next_stage = NEXT_STAGE[step]
        today_str = today.isoformat()
        updated = 0

        if not should_advance_batch(launch, outcome):
            click.echo(
                f"  ⚠ PB Message Sender advance gate FAILED for "
                f"{step.value} (csv_status={outcome.csv_status}, "
                f"sent={outcome.sent_count}, "
                f"requested={outcome.requested_count}); opening "
                f"pb_silent_no_op queue row and skipping Attio "
                f"updates for this batch."
            )
            current_experiment_id_for_dm = get_current_experiment_id()
            emit_pb_silent_no_op(
                launch,
                outcome,
                attio=attio,
                audit_logger=audit_logger,
                experiment_id=current_experiment_id_for_dm,
            )
            results.setdefault("dry_skipped", {})[step.value] = (
                outcome.requested_count
            )
            continue

        # Advance gate passed. Per-row advance:
        # - PB-confirmed sent → flip stage + bump dm_step (next-day
        #   queue won't re-pick this row)
        # - PB-flagged-skipped (InMail-required, "Can't send") → open a
        #   `pb_inmail_dead_end` queue row AND move the row to UNREACHABLE
        #   (stage-only) so the sequencer stops re-queuing the same DM every
        #   run. dm_step is deliberately NOT bumped — that would inflate
        #   `dm_response_rate` denominators in `learn.py::_per_step_rates`
        #   (rows with `dm_step >= n` would count as "received DM-n" even
        #   though no message was delivered). UNREACHABLE gates the row out
        #   of future sends; the queue row lets the operator rescue it.
        # - PB-unreported → no state mutation, retried tomorrow.
        current_experiment_id_for_dm = get_current_experiment_id()
        pb_unreported: list[str] = []
        pb_flagged_skipped: list[str] = []
        pb_park_failed: list[str] = []
        for row in rows:
            key = _normalize_linkedin_url(row["linkedInUrl"])
            is_pb_sent = key in outcome.sent_urls
            is_pb_skipped = key in outcome.skipped_urls

            if is_pb_skipped:
                pb_flagged_skipped.append(row["linkedInUrl"])
                emit_pb_inmail_dead_end(
                    launch,
                    linkedin_url=key,
                    dm_step=step.value,
                    pb_status="skipped_in_csv",
                    attio=attio,
                    audit_logger=audit_logger,
                    experiment_id=current_experiment_id_for_dm,
                )
                # Wave-2-A: move the prospect to UNREACHABLE so the sequencer
                # STOPS re-queuing this same DM every run (the Daniel/Nissan
                # InMail-required loop). Write ONLY `stage` — NOT `dm_step` or
                # `last_contact_date` — so the undelivered DM is never counted
                # as "received" in learn.py::_per_step_rates denominators (the
                # measurement guard the comment above protects). UNREACHABLE
                # (rank 90) gates the row out of all future sends; the
                # dead-end queue row above lets the operator rescue it.
                # Park EVERY duplicate entry for this URL (multi-entry rows
                # exist — the 2026-04-21 dedup history). Branch on the result
                # like the sibling callers (pb_send_recovery, OON park): if a
                # park write fails, AttioWriter has already DLQ'd + escalated +
                # tallied in helper_escalate_failures, but the row stays at its
                # DM stage and would be re-picked next run. PB re-blocks the
                # same InMail-required DM (never delivers), so this is a wasted
                # retry, not a duplicate send — but surface it explicitly
                # rather than fire-and-forget.
                park_ok = True
                for entry_id in row.get("entry_ids") or [row.get("entry_id")]:
                    if not entry_id:
                        continue
                    if not _attio_advance_with_escalation(
                        attio=attio,
                        entry_id=entry_id,
                        entry_attributes={"stage": PipelineStage.UNREACHABLE.value},
                        list_id=list_id,
                        linkedin_url=row.get("linkedInUrl", ""),
                        today=today_str,
                        step_label=f"{step.value}_inmail_dead_end",
                        writer_module="workflows.daily_check.run_dm_sequencing",
                        prior_stage=row.get("current_stage")
                                    or row.get("stage")
                                    or STAGE_FOR_DM[step].value,
                        person_record_id=row.get("record_id"),
                        audit_logger=audit_logger,
                        escalate_failures=helper_escalate_failures,
                    ):
                        park_ok = False
                if not park_ok:
                    pb_park_failed.append(row["linkedInUrl"])
                continue
            if not is_pb_sent:
                # PB didn't confirm send for this URL. Per §3.1, do
                # NOT advance — leave the row at its current stage
                # so tomorrow retries.
                pb_unreported.append(row["linkedInUrl"])
                continue

            attrs_to_update = {
                "dm_step": int(step.value.replace("dm", "")),
                "last_contact_date": today_str,
                "stage": next_stage.value,
            }
            # PR-12 (B-PD-007): stamp the forward-only cadence floor for
            # the NEXT step. compute_next_eligible_send_date returns None
            # for DM3 (no DM4 exists in v1 cadence) — leave the attribute
            # untouched in that case so the value resolves to NULL or
            # whatever PR-39's NURTURE math chooses. The write owner per
            # §3.15 is workflows.daily_check.run_dm_sequencing (this
            # function), already registered.
            next_eligible = compute_next_eligible_send_date(
                last_contact_date=today, just_sent_step=step.value
            )
            if next_eligible is not None:
                attrs_to_update["next_eligible_send_date"] = next_eligible.isoformat()
            # PR-13 (§3.15): tally Companies.last_outreach_at + 3 siblings
            # IMMEDIATELY after the prospect advance, BEFORE the next due
            # row's throttle check evaluates — Round-4 D32 multi-thread
            # ABM safety requires the write to settle before sibling
            # persons at the same company are scored for this run.
            # 2026-06-09 desync-invariant: extracted to _finalize_confirmed_dm_send
            # which aggregates advance outcomes and emits dm_person_advance_desync
            # on failure while keeping the tally unconditional.
            updated += _finalize_confirmed_dm_send(
                attio=attio,
                row=row,
                step=step,
                attrs_to_update=attrs_to_update,
                list_id=list_id,
                today=today,
                today_str=today_str,
                experiment_id=current_experiment_id_for_dm,
                audit_logger=audit_logger,
                escalate_failures=helper_escalate_failures,
            )

        if pb_flagged_skipped:
            click.echo(
                f"  ⚠ PB flagged {len(pb_flagged_skipped)} {step.value} "
                f"URL(s) as skipped (InMail/etc) — `pb_inmail_dead_end` "
                f"queue rows opened and prospects parked at UNREACHABLE "
                f"(removed from the DM queue; operator can rescue via the "
                f"queue row):"
            )
            for u in pb_flagged_skipped[:5]:
                click.echo(f"      {u}")
        if pb_park_failed:
            # The UNREACHABLE park write FAILED for these (AttioWriter DLQ'd +
            # escalated). They remain at their DM stage and will be re-attempted
            # next run; PB re-blocks the same InMail DM so nothing is re-sent,
            # but the operator should reconcile the attio_write_failed rows.
            click.echo(
                f"  ❌ {len(pb_park_failed)}/{len(pb_flagged_skipped)} "
                f"{step.value} UNREACHABLE park write(s) FAILED — still at DM "
                f"stage, reconcile attio_write_failed before next run:",
                err=True,
            )
            for u in pb_park_failed[:5]:
                click.echo(f"      {u}", err=True)
        if pb_unreported:
            click.echo(
                f"  ⚠ PB CSV omits {len(pb_unreported)}/{len(rows)} "
                f"{step.value} URL(s) — stage NOT advanced (per §3.1 "
                f"advance gate); tomorrow's run will retry these."
            )
            # Per-URL audit signal so the next-day drift detector can
            # correlate. No queue row — chronic per-URL unreported is
            # a softer signal than batch-level pb_silent_no_op, and a
            # row per URL would flood the queue. Audit-only.
            if audit_logger is not None:
                for unreported_url in pb_unreported:
                    audit_logger.event(
                        "pb_url_unreported",
                        container_id=launch.container_id,
                        linkedin_url=_normalize_linkedin_url(unreported_url),
                        dm_step=step.value,
                        experiment_id=current_experiment_id_for_dm,
                    )

        # PR-17 charged the daily_run lease right after parse_send_outcome
        # via confirm_lease(token, confirmed_count=outcome.sent_count).
        # The legacy ``record_messages`` path was removed in PR-17 fold-in.
        #
        # L3-7: results[step.value] is the Attio-confirmed sent count
        # (rows PB confirmed AND we successfully advanced in Attio), not
        # the queue depth. A separate `{step}_queued` key carries the
        # prepared count so callers can detect send-phantom gaps.
        # Using `updated` (Attio advances) rather than outcome.sent_count
        # (raw PB CSV count) avoids inflating the summary when the CSV
        # contains historic rows beyond this batch's requested_urls.
        results[step.value] = updated
        results[f"{step.value}_queued"] = len(rows)
        click.echo(
            f"  Queued {len(rows)} {step.value} messages to PB. "
            f"Attio advanced: {updated}/{len(rows)}. "
            f"PB-reported sent: {outcome.sent_count}, "
            f"PB-flagged skipped: {len(pb_flagged_skipped)}, "
            f"PB-unreported: {len(pb_unreported)}."
        )

    if results["skipped_missing_language"]:
        click.echo(
            f"  Skipped {results['skipped_missing_language']} prospect(s) with "
            f"missing/invalid language — see `missing_language` Operator "
            f"Review Queue rows."
        )
    if helper_escalate_failures:
        # Wave-1.6.3: paging-level rollup of swallowed escalate() failures
        # raised inside the post-PB-send helpers across the DM1/DM2/DM3
        # batches. Per-row WARN already named each; mirrors the FIX-A
        # summary in run_connection_requests.
        sites = ", ".join(
            sorted({f["site"] for f in helper_escalate_failures})
        )
        click.echo(
            f"  ❌ ERROR: {len(helper_escalate_failures)} attio_write_failed "
            f"escalate() call(s) failed during the DM sequencing batches "
            f"(sites: {sites}). Per-row stage advances behave per their "
            f"original semantics (failed advance → row stays at prior "
            f"stage; failed tally → throttle write lost this cycle) but "
            f"the operator review queue is missing the reconciliation "
            f"rows. Inspect the per-row WARN log lines above for the "
            f"underlying exceptions.",
            err=True,
        )
    # Wet runs advanced entries this run — a reused snapshot would show
    # pre-advance dm_step and fabricate divergences; refetch. Dry runs
    # advance nothing (the dry branch continues before any PB/Attio
    # write), so the run's snapshot is still valid — reuse it.
    if not dry_run:
        _raw_entries_snapshot = None  # also frees one 50k snapshot of peak RSS before the sweep refetches
    _consistency_sweep_epilogue(results, entries_snapshot=_raw_entries_snapshot)
    return results


# ── PR-17 B-SD-011: run-end summary ─────────────────────────────────

# Thresholds for the starvation signal. Tuned to the current pipeline
# size (~600 active prospects, ~25 invites/day): under 3 due for a step
# means the cohort that step depends on isn't refilling fast enough.
_STARVATION_DUE_FLOOR = 3


def _classify_starvation_signal(
    due_dm1: int, due_dm2: int, due_dm3: int
) -> StarvationSignal:
    """Map per-step due counts to the ``starvation_signal`` select.

    ``healthy`` when all steps clear ``_STARVATION_DUE_FLOOR`` (3 is
    healthy, 2 is low). ``low_dm{1,2,3}`` when exactly one step is
    starved. ``multi_low`` when two or more steps are starved.
    """
    low = [
        step for step, count in (("dm1", due_dm1), ("dm2", due_dm2), ("dm3", due_dm3))
        if count < _STARVATION_DUE_FLOOR
    ]
    if not low:
        return "healthy"
    if len(low) == 1:
        signal: StarvationSignal = f"low_{low[0]}"  # type: ignore[assignment]
        return signal
    return "multi_low"


def _count_degree_unknown_today(
    crm: "CRMProvider", today: date
) -> int | None:
    """Count ``degree_unknown`` Operator Review Queue rows opened today.

    The aggregator half of the §4.2 Round-4 D12 producer-consumer pair:
    PR-15 emits ONE queue row per missing prospect during the pre-invite
    scrape; PR-17 sums them into the daily_run summary. Queries Attio at
    run-end so the count is current even if PR-15 emitted partway
    through the run.

    PR-17 fold-in (silent-failure-hunter BLOCKING): returns ``None`` on
    transport failure rather than ``0``. A silent-fallback to ``0``
    would let an operator scan daily_run rows and see a clean zero on
    a day when 40 degree_unknown rows actually got queued — direct §0
    #9 violation. Caller (``run_end_summary``) treats ``None`` as
    "unknown" and OMITS the field from the PATCH (Attio rejects JSON null
    on a number column) so the operator can distinguish "no unknowns
    today" (0) from "we don't know" (absent). Stderr WARN ensures the
    failure is operator-visible.
    """
    # NOTE: opened_at is compared at UTC midnight while `today` is the
    # operator-local (America/Lima, UTC-5) day. Rows opened 00:00-05:00Z
    # belong to the prior Lima day, so the count can slightly over-count
    # near the boundary. Accepted: this is a soft observability metric, not
    # a send-gating value — do not "fix" into a TZ-conversion regression.
    # Attio's query DSL requires an explicit $and wrapper for multi-key
    # filters (a bare two-key object 400s). opened_at is a datetime, so
    # the $gte bound must be a full ISO timestamp, not a bare date.
    # The filter body stays vendor-native (the documented filter-shape
    # leak on query_object_records) — only the transport moves to the
    # contract method, which builds the identical {"filter": ..., "limit": 500}
    # query body and POSTs to /objects/operator_review_queue/records/query.
    filters = {
        "$and": [
            {"type": "degree_unknown"},
            {"opened_at": {"$gte": today.isoformat() + "T00:00:00Z"}},
        ],
    }
    try:
        records = crm.query_object_records(
            "operator_review_queue", filters=filters, limit=500
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        import sys
        print(
            f"WARN: degree_unknown aggregation failed: {type(exc).__name__}: {exc}. "
            f"Daily run summary will report degree_unknown_count=None (unknown), "
            f"not 0. Per-prospect degree_unknown queue rows remain available for "
            f"operator triage.",
            file=sys.stderr,
        )
        return None
    return len(records)


def run_end_summary(
    crm: "CRMProvider",
    daily_run: DailyRun,
    *,
    prospect_pool_size: int,
    due_dm1_count: int,
    due_dm2_count: int,
    due_dm3_count: int,
    today: date | None = None,
) -> dict[str, int | str | None]:
    """Write the PR-17 run-end summary attrs onto the daily_run row.

    Called from cli.py at the end of the daily check, after both Part
    A (connection requests) and Part B (DM sequencing) finish — but
    BEFORE the ``open_daily_run`` context manager closes the row.

    Sole writer per §3.15 for: ``prospect_pool_size``, ``due_dm1_count``,
    ``due_dm2_count``, ``due_dm3_count``, ``degree_unknown_count``,
    ``starvation_signal``. ``reply_detection_status`` is owned by PR-19
    (also registered to this module per the writer registry) — not
    written here. ``nurture_silent_skipped_count`` is owned by PR-39's
    nurture path.

    Returns the dict of values written, so the caller can log them.
    """
    today = today or date.today()
    degree_unknown_count = _count_degree_unknown_today(crm, today)
    starvation_signal = _classify_starvation_signal(
        due_dm1_count, due_dm2_count, due_dm3_count
    )
    # degree_unknown_count=None means the aggregation query failed. It is
    # carried in the returned dict for logging but OMITTED from the PATCH
    # below (Attio rejects JSON null on a number column), so the operator
    # distinguishes "no unknowns today" (0) from "we don't know" (absent).
    values: dict[str, int | str | None] = {
        "prospect_pool_size": prospect_pool_size,
        "due_dm1_count": due_dm1_count,
        "due_dm2_count": due_dm2_count,
        "due_dm3_count": due_dm3_count,
        "degree_unknown_count": degree_unknown_count,
        "starvation_signal": starvation_signal,
    }
    # Wave-2-B §3.15 cleanup: the pre-Wave-2 direct PATCH bypassed
    # AttioWriter and the write-owner registry. The daily_run summary
    # attrs are all registered to this function so the registry-route
    # is a no-op for the gate but it brings the path under the same
    # retry / DLQ / queue-row behavior the rest of the codebase
    # already relies on.
    # Attio rejects JSON null on a number column, so degree_unknown_count's
    # "unknown" sentinel (None) is OMITTED from the PATCH rather than written
    # as null. The omission is scoped to this one known-nullable counter — a
    # None on any other counter is a real bug, so it is left in and the write
    # fails loudly. The returned dict still carries None for operator-facing
    # logging (absence in Attio reads as "unknown").
    updates = {
        k: val for k, val in values.items()
        if not (k == "degree_unknown_count" and val is None)
    }

    from clients.attio_writer import (
        AttioMonotonicityViolation,
        AttioTerminalClassRegression,
        AttioWriter,
        UnauthorizedAttioWriteError,
        WriteIntent,
    )
    writer = AttioWriter(attio=crm)
    try:
        writer.apply(WriteIntent(
            object="daily_run",
            record_id=daily_run.record_id,
            updates=updates,
            prior_values={},
            writer_module="workflows.daily_check.run_end_summary",
        ))
    except (
        UnauthorizedAttioWriteError,
        AttioMonotonicityViolation,
        AttioTerminalClassRegression,
    ):
        # Pre-write rejections are config/programming bugs (write-owner
        # registry drift, illegal stage transition) that open NO triage
        # queue row. They must fail loudly, not be masked — run_end_summary
        # writes no `stage`, so these cannot legitimately fire here.
        # open_daily_run still closes the row (failed) + releases the lock.
        raise
    except Exception as exc:
        # Operational failure (transport / permanent HTTP / rate-limit):
        # AttioWriter has already opened an attio_write_failed queue row via
        # _dlq_and_escalate. An observability-only write must NOT abort the
        # run or block daily_run closure — surface a loud WARN and continue.
        import sys
        print(
            f"WARN: run-end summary write failed ({type(exc).__name__}: {exc}). "
            f"daily_run summary counters not persisted this cycle; the run still "
            f"completes. See the attio_write_failed queue row for operator triage.",
            file=sys.stderr,
        )
    return values


def compute_due_dm_counts(
    crm: "CRMProvider",
    cache: RecordCache | None = None,
    today: date | None = None,
) -> dict[str, int]:
    """Compute the PR-17 run-end input counts without sending any DMs.

    Mirrors ``run_dm_sequencing``'s queue-building loop without the
    cap-aware trim, the PB launch, or the Attio writes — operator
    visibility for the summary should reflect cohort SIZES, not the
    cap-limited cohort actually contacted today. Returns:
        {prospect_pool_size, due_dm1_count, due_dm2_count, due_dm3_count}
    """
    today = today or date.today()
    # ``_get_all_entries_parsed`` is the shared dict-based parse path (also
    # consumed by threshold_calibration / starvation), keyed off the raw
    # ``AttioClient.parse_entry``; it stays on the concrete client, so reach
    # the inner client via the Attio escape hatch at this boundary. Imported
    # lazily to avoid pulling weekly_prospect's module tree at daily_check
    # import time (and any attendant circular-import hazard).
    from workflows.weekly_prospect import _attio_inner_client
    all_parsed = _get_all_entries_parsed(_attio_inner_client(crm))
    if cache is None:
        cache = RecordCache(crm)

    pool = sum(
        1 for a in all_parsed
        if a.get("stage") in {s.value for s in PipelineStage}
    )

    due = {"dm1": 0, "dm2": 0, "dm3": 0}
    for attrs in all_parsed:
        stage_str = attrs.get("stage")
        try:
            stage = PipelineStage(stage_str)
        except ValueError:
            continue
        if not is_send_eligible(attrs):
            continue
        last_date_str = attrs.get("last_contact_date")
        if not last_date_str:
            continue
        # PR-17 fold-in (code-reviewer IMPORTANT): match
        # run_dm_sequencing's behavior — let date.fromisoformat raise
        # on a malformed value instead of silently skipping the row.
        last_date = date.fromisoformat(str(last_date_str)[:10])
        pending = get_pending_dms(
            stage,
            last_date,
            today,
            dm_step=int(attrs.get("dm_step") or 0),
        )
        if pending == MessageStep.DM1:
            due["dm1"] += 1
        elif pending == MessageStep.DM2:
            due["dm2"] += 1
        elif pending == MessageStep.DM3:
            due["dm3"] += 1

    return {
        "prospect_pool_size": pool,
        "due_dm1_count": due["dm1"],
        "due_dm2_count": due["dm2"],
        "due_dm3_count": due["dm3"],
    }


def compute_dm1_sent_cohort_by_date(
    attio: AttioClient,
    today: date | None = None,
    window_business_days: int = 5,
) -> list[tuple[str, int]]:
    """DM1-Sent-stage rows grouped by send-date, last N business days.

    Read-only operator-visibility helper for the run-end summary. Counts
    rows CURRENTLY at DM1_SENT, keyed by the date DM1 went out: ``dm1_sent_at``
    when present, falling back to ``last_contact_date`` for legacy rows that
    predate the PR-9a per-step timestamp (for a DM1_SENT-stage row the last
    contact IS the DM1 send, so the fallback is exact).

    Why this exists: the run-end summary reports ``due_dm{1,2,3}`` (how many
    are DUE) but not how many were SENT per day, so a healthy daily cohort
    can be unreadable inside a DM1_SENT stage total that also holds same-day
    re-prospected duplicates. Bucketing by send-date makes each day's true
    cohort size legible at a glance.

    Returns ``[(YYYY-MM-DD, count), ...]`` sorted by date ascending, limited
    to send-dates within the last ``window_business_days`` business days
    (inclusive of today). Rows with no/unparseable send-date, or a send-date
    outside the window, are omitted.
    """
    today = today or date.today()
    counts: dict[str, int] = {}
    skipped_unparseable = 0
    for attrs in _get_all_entries_parsed(attio):
        if attrs.get("stage") != PipelineStage.DM1_SENT.value:
            continue
        raw = attrs.get("dm1_sent_at") or attrs.get("last_contact_date")
        if not raw:
            continue
        day = str(raw)[:10]
        try:
            sent = date.fromisoformat(day)
        except ValueError:
            # A DM1_SENT row with a corrupt send-date is itself a data-quality
            # anomaly AND would silently shrink the very cohort this helper
            # exists to make legible — count it and surface it below rather
            # than dropping it without a trace.
            skipped_unparseable += 1
            continue
        # business_days_between counts Mon-Fri strictly after `sent` up to
        # `today`: 0 for today, 1 for the prior business day, etc. Keep the
        # most recent `window_business_days` (today included); drop future
        # dates and anything older than the window.
        if sent > today or business_days_between(sent, today) >= window_business_days:
            continue
        counts[day] = counts.get(day, 0) + 1
    if skipped_unparseable:
        click.echo(
            f"  ⚠ {skipped_unparseable} DM1_SENT row(s) had an unparseable "
            f"send-date and were excluded from the cohort table.",
            err=True,
        )
    return sorted(counts.items())


def check_responses_manual(attio: AttioClient) -> None:
    """Manual response check: prompt user to input which prospects responded."""
    list_id = os.environ.get("ATTIO_LIST_ID", "")
    all_parsed = _get_all_entries_parsed(attio)
    cache = RecordCache(attio)

    dm_stages = {
        PipelineStage.DM1_SENT.value,
        PipelineStage.DM2_SENT.value,
        PipelineStage.DM3_SENT.value,
    }

    dm_prospects = []
    for attrs in all_parsed:
        if attrs["stage"] in dm_stages:
            name, company, _, _, _ = cache.get(attrs["record_id"])
            dm_prospects.append({**attrs, "name": name, "company": company})

    if not dm_prospects:
        click.echo("No prospects currently in DM stages.")
        return

    click.echo(f"\n{len(dm_prospects)} prospects in DM stages:\n")
    for i, p in enumerate(dm_prospects, 1):
        click.echo(f"  {i}. {p['name']} ({p['company']}) -- {p['stage']}")

    response = click.prompt(
        "\nEnter numbers of prospects who responded (comma-separated), or 'q' to quit",
        default="q",
    )
    if response.lower() == "q":
        return

    responded_indices = [int(x.strip()) - 1 for x in response.split(",") if x.strip().isdigit()]

    for idx in responded_indices:
        if 0 <= idx < len(dm_prospects):
            prospect = dm_prospects[idx]
            message_text = click.prompt(f"\nPaste response from {prospect['name']}", default="")

            if message_text:
                result = classify_response(message_text)
                click.echo(f"  Classification: {result['classification']}")
                click.echo(f"  Action: {result['suggested_action']}")
                click.echo(f"  Summary: {result['summary']}")

                new_stage = PipelineStage.RESPONDED.value
                if result["classification"] == "negative":
                    new_stage = PipelineStage.NOT_INTERESTED.value

                # Wave-2-B §3.15: route through AttioWriter so the
                # manual operator path obeys the registry +
                # monotonicity gates the rest of the codebase relies
                # on. ``prospect["stage"]`` is the current DM-stage
                # the operator picked from the menu so monotonicity
                # has a real prior to compare against.
                #
                # Wave-2-B fix-up (silent-failure-hunter CRITICAL-4):
                # interactive operator UX — when an AttioWriter
                # exception fires on prospect N, the OPERATOR is
                # waiting at the terminal. Surface the error with the
                # entry_id + prior stage so they can fix it manually,
                # and CONTINUE to prospect N+1 instead of dropping
                # their remaining selections via a raw traceback.
                # Programmer-bug class (registry / monotonicity /
                # terminal-class) still halts because the operator
                # needs to file a bug, not retry.
                from clients.attio_writer import (
                    AttioError,
                    AttioMonotonicityViolation,
                    AttioPermanentError,
                    AttioRateLimitExhausted,
                    AttioTerminalClassRegression,
                    AttioWriter,
                    UnauthorizedAttioWriteError,
                    WriteIntent,
                )
                _writer = AttioWriter(attio=attio)
                try:
                    _writer.apply(WriteIntent(
                        object="linkedin_outreach",
                        record_id=prospect["entry_id"],
                        updates={"stage": new_stage},
                        prior_values={"stage": prospect.get("stage")},
                        writer_module="workflows.daily_check.check_responses_manual",
                        is_list_entry=True,
                        list_id=list_id,
                        # Wave-2-B fix-up (multi-agent I-4):
                        # underlying person record_id for navigation.
                        companion_record_id=prospect.get("record_id"),
                    ))
                except (UnauthorizedAttioWriteError,
                        AttioMonotonicityViolation,
                        AttioTerminalClassRegression):
                    # Code bug — halt so the operator files it.
                    raise
                except (AttioPermanentError, AttioRateLimitExhausted) as exc:
                    click.echo(
                        f"  ⚠ Attio rejected the stage flip for "
                        f"{prospect['name']!r} (entry "
                        f"{prospect['entry_id']}, current "
                        f"stage={prospect.get('stage')!r} → "
                        f"{new_stage!r}): {type(exc).__name__}: {exc}. "
                        f"AttioWriter opened an attio_write_failed "
                        f"queue row. Continuing with remaining "
                        f"selections — fix this entry manually after "
                        f"the interactive batch finishes.",
                        err=True,
                    )
                    continue
                except AttioError as exc:
                    # Catch-all defense for any AttioError subclass we
                    # didn't enumerate above. Operator-visible, no
                    # raw traceback, batch continues.
                    click.echo(
                        f"  ⚠ AttioWriter error for "
                        f"{prospect['name']!r}: {type(exc).__name__}: "
                        f"{exc}. Continuing with remaining selections.",
                        err=True,
                    )
                    continue
                click.echo(f"  -> Moved to '{new_stage}'")

                try:
                    attio.create_note(
                        record_id=prospect["record_id"],
                        title=f"Response -- {result['classification']}",
                        content=(
                            f"Message: {message_text}\n\n"
                            f"Classification: {result['classification']}\n"
                            f"Action: {result['suggested_action']}\n"
                            f"Summary: {result['summary']}"
                        ),
                    )
                except (httpx.HTTPStatusError, httpx.RequestError) as note_exc:
                    # Stage already advanced; note is forensic-only.
                    click.echo(
                        f"  ⚠ Audit note creation failed for "
                        f"{prospect['name']!r} but stage advance "
                        f"already landed: {note_exc}. Add note "
                        f"manually if needed.",
                        err=True,
                    )
