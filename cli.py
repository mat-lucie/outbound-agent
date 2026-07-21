"""CLI entry point for the Outbound Agent."""

import contextlib
import os
import sys
from datetime import date
from typing import Any

import click
import httpx
from dotenv import load_dotenv

# Add project root to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

load_dotenv()

from clients.outreach_config import load_outreach_config  # noqa: E402 — after sys.path/dotenv setup
from clients.pb_config import load_pb_config  # noqa: E402 — after sys.path/dotenv setup

# Module-level so the send-dms reattach is monkeypatchable as
# ``cli.attach_daily_run`` in tests (the name the gate guards resolve through).
from workflows.daily_run import attach_daily_run  # noqa: E402 — after sys.path/dotenv setup


@contextlib.contextmanager
def _maybe_resend_client():
    """Yield a ``ResendClient`` if ``RESEND_API_KEY`` is set, else None.

    PR-18 B-SD-012 wiring: production wants the hot-lead alert email
    channel live; dev/test environments without the key get the
    queue-row-only path. ``ResendClient.__init__`` raises ``KeyError``
    on missing env, so we guard explicitly here rather than catching
    the constructor exception inside detect_responses.
    """
    from clients.resend_client import ResendClient

    if not os.environ.get("RESEND_API_KEY"):
        yield None
        return
    client = ResendClient()
    try:
        yield client
    finally:
        client.close()


@contextlib.contextmanager
def _attio_client():
    """Yield the raw ``AttioClient``, constructed through the CRM factory.

    This is the cli's CRM construction seam (P1c). Construction now flows
    through ``clients.crm.factory.get_crm_provider`` — which reads
    ``config/crm.yaml`` when present and otherwise defaults to Attio-from-env,
    the pre-seam behavior. The factory builds the inner ``AttioClient`` and
    composes an ``AttioProvider`` around it; for this increment we hand the
    *raw* inner client (``bundle.attio``) to the workflow call sites unchanged,
    so no workflow signature or behavior changes. Later increments swap callers
    to ``bundle.provider`` and drop this raw accessor.

    The bundle's exit closes the inner client, matching today's
    ``with AttioClient() as attio:`` lifecycle exactly.
    """
    from clients.crm.factory import get_crm_provider

    with get_crm_provider() as bundle:
        yield bundle.attio


@contextlib.contextmanager
def _crm_provider():
    """Yield the vendor-neutral ``CRMProvider``, constructed through the factory.

    This is the migrated counterpart to ``_attio_client`` (P1c-increment-2). A
    command whose whole call-tree reads the CRM only through the ``CRMProvider``
    contract uses this accessor and receives ``bundle.provider`` instead of the
    raw ``bundle.attio`` client. Commands not yet migrated keep using
    ``_attio_client``; both share the same factory + lifecycle, so the bundle's
    exit closes the inner client either way. As more slices migrate, callers move
    from ``_attio_client`` to ``_crm_provider`` one command at a time.
    """
    from clients.crm.factory import get_crm_provider

    with get_crm_provider() as bundle:
        yield bundle.provider


class _DryRunDailyRun:
    """Stub DailyRun for cli.py's --dry-run mode (PR-17).

    PR-17 made ``daily_run`` required on ``run_dm_sequencing`` (removed
    the legacy ``record_messages`` None-branch fallback). Dry-run mode
    still needs a handle so the queue-building / trim-to-cap logic can
    query ``remaining("messages")``, but it must NOT open a real Attio
    daily_run row — that would burn the ``(run_date, machine_id)``
    uniqueness slot and block the real same-day invocation.
    """

    record_id = "dry-run"

    def remaining(self, kind: str) -> int:
        from workflows.daily_run import (
            MAX_CONNECTIONS_PER_DAY,
            MAX_MESSAGES_PER_DAY,
            MAX_VISITS_PER_DAY,
        )
        return {
            "messages": MAX_MESSAGES_PER_DAY,
            "connections": MAX_CONNECTIONS_PER_DAY,
            "visits": MAX_VISITS_PER_DAY,
        }.get(kind, 0)

    def reserve_send(self, kind: str, count: int) -> str:
        return "dry-run-token"

    def confirm_lease(self, token: str, confirmed_count: int | None = None) -> None:
        pass

    def release_lease(self, token: str) -> None:
        pass

    def get_reply_detection_status(self) -> str | None:
        # PR-19 fold-in (salesman-daily-QA-build19 BLOCKING): dry-run
        # must not crash with AttributeError when run_dm_sequencing
        # calls this as its first line. Dry-run always returns None so
        # the Part-B short-circuit guard (`== "failed"`) is False and
        # the function proceeds to its existing `if dry_run: continue`
        # branches.
        return None

    def set_reply_detection_status(self, status: str) -> None:
        # Dry-run no-op: never writes Attio. Phase 0.5 is itself
        # skipped in dry-run mode (cli.py gates it on mode.is_dry_run()),
        # so this is defensive — if a future code path ever calls it
        # under dry-run, no Attio write fires.
        pass


@click.group()
def cli():
    """Outbound Agent -- LinkedIn outreach automation."""
    pass


@cli.command()
@click.option("--dry-run", is_flag=True, help="Preview actions without executing")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts (for cron)")
@click.option("--batch-size", default=lambda: load_outreach_config().invite_batch_size, help="Max connection requests per run (default: config/outreach.yaml → caps.invite_batch_size; capped by caps.invites_per_day)")
@click.option("--network-booster-id", default=lambda: load_pb_config().network_booster_id or None, help="PhantomBuster Network Booster agent ID (default: config/phantombuster.yaml → PB_NETWORK_BOOSTER_ID)")
@click.option("--message-sender-id", default=lambda: load_pb_config().message_sender_id or None, help="PhantomBuster Message Sender agent ID (default: config/phantombuster.yaml → PB_MESSAGE_SENDER_ID)")
@click.option("--profile-scraper-id", default=lambda: load_pb_config().profile_scraper_id or None, help="PhantomBuster Profile Scraper agent ID, legacy backend — documented-dead: the agent was deleted from the PB workspace; only useful with a re-deployed phantom (default: config/phantombuster.yaml → PB_PROFILE_SCRAPER_ID)")
@click.option("--sales-nav-profile-scraper-id", default=lambda: load_pb_config().sales_nav_profile_scraper_id or None, help="PhantomBuster Sales Navigator Profile Scraper agent ID, sales_nav backend (the default) (default: config/phantombuster.yaml → PB_SALES_NAV_PROFILE_SCRAPER_ID)")
@click.option("--inbox-scraper-id", default=lambda: load_pb_config().inbox_scraper_id or None, help="PhantomBuster Inbox Scraper agent ID (default: config/phantombuster.yaml → PB_INBOX_SCRAPER_ID)")
@click.option("--skip-dms", is_flag=True, help="Skip Part B DM sequencing (connections only)")
@click.option("--force-weekend", is_flag=True, help="Override the Mon-Fri-only outreach rule")
def daily(dry_run, yes, batch_size, network_booster_id, message_sender_id, profile_scraper_id, sales_nav_profile_scraper_id, inbox_scraper_id, skip_dms, force_weekend):
    """Daily check: send connections, queue DMs, detect responses."""
    from clients.phantombuster import PhantomBusterClient
    from models.business_calendar import is_send_day, operator_today
    from models.run_mode import RunMode
    from workflows.audit import AuditLogger
    from workflows.daily_check import (
        compute_dm1_sent_cohort_by_date,
        compute_due_dm_counts,
        detect_accepted_connections,
        run_connection_requests,
        run_dm_sequencing,
        run_end_summary,
    )
    from workflows.daily_check_helpers import _VALID_BACKENDS
    from workflows.daily_run import (
        ConcurrentRunInAttio,
        DailyRun,
        MalformedDailyRunRow,
        open_daily_run,
    )
    from workflows.detect_responses import NoCSVHalt, detect_responses
    from workflows.escalation import escalate
    from workflows.metrics import DailyRunMetrics
    from workflows.record_cache import RecordCache, preload_pipeline_persons
    from workflows.run_lock import (
        EXIT_TEMPFAIL,
        RunLockHeld,
        acquire_run_lock,
        log_lock_refused,
    )
    from workflows.safety_limits import get_status
    from workflows.starvation import evaluate_pipeline_starvation
    from workflows.weekly_prospect import _attio_inner_client

    mode = RunMode.from_dry_run_flag(dry_run)
    metrics = DailyRunMetrics()

    click.echo("=== Outbound Agent -- Daily Check ===\n")
    click.echo(f"Mode: {mode.value}\n")
    click.echo(f"Safety limits:\n{get_status()}\n")

    # PR-11 pre-flight (§0 invariant #9): if two or more experiments are
    # `running`, `get_current_experiment_id` raises
    # MultipleRunningExperimentsError. Surface that early — before the run
    # lock + Attio client open — so the operator gets a clean stderr abort
    # instead of a mid-flight traceback from one of three call sites in
    # workflows.daily_check (run_connection_requests, run_dm_sequencing).
    #
    # Only the multi-running ambiguity is a hard abort. Any other failure
    # (transient Attio error, malformed TSV cache, missing credentials in a
    # test harness) is left for the in-flight call sites to handle — they
    # already do, and silently swallowing here would mask real bugs the test
    # suite asserts against.
    from models.experiment import (
        MultipleRunningExperimentsError,
        get_current_experiment_id,
    )
    try:
        _ = get_current_experiment_id()
    except MultipleRunningExperimentsError as exc:
        click.echo(
            f"ABORT: cannot run daily — {exc} "
            "Close one experiment in Attio (or in experiments.tsv) and re-run.",
            err=True,
        )
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 — see comment above
        # Defer to in-flight error handling, but emit a one-line debug log so
        # the operator can distinguish pre-flight failures (e.g., Attio down,
        # missing creds) from in-flight failures further down the run.
        click.echo(
            f"[daily pre-flight] experiment check skipped: "
            f"{type(exc).__name__}: {exc}",
            err=True,
        )

    # operator_today() reads OUTBOUND_TZ (default America/Lima)
    # so a UTC cron clock doesn't shift the quarantine + send-day calendar.
    today = operator_today()
    # Policy: invites run every day; DMs are Mon-Fri only (override with --force-weekend).
    dms_allowed = is_send_day(today) or force_weekend
    lock_name = "sales-daily"
    run_id = f"{lock_name}-{today.isoformat()}-{os.getpid()}"

    # Lock acquired BEFORE the AuditLogger opens so a refused run doesn't
    # pollute the audit log with an aborted-start row.
    # try/finally guarantees `metrics.render()` fires even when an
    # unhandled exception escapes the run — the operator's only signal
    # for partial-state runs lives in those counters.
    try:
        with acquire_run_lock(lock_name, run_id=run_id), \
                AuditLogger(workflow="daily_check", dry_run=dry_run) as audit_logger, \
                _crm_provider() as crm, PhantomBusterClient() as pb:
            # PR-17 B-SD-001: open the daily_run row before any send-path
            # work. F-PR-8's ``(run_date, machine_id)`` uniqueness raises
            # ConcurrentRunInAttio on collision — surface as typed
            # ``daily_run_collision`` operator escalation and exit
            # EX_TEMPFAIL so a same-day second invocation backs off
            # without overwriting state. Dry-run uses ``_DryRunDailyRun``
            # stub so the trim path still has a ``remaining()`` source
            # without burning the uniqueness slot.
            # open_daily_run now talks to the CRM through the vendor-neutral
            # provider (its uniqueness-collision guard catches the provider's
            # UniquenessConflictError, not a raw httpx error), so it takes the
            # provider directly — no inner-client conversion here anymore.
            daily_run_cm = (
                contextlib.nullcontext(_DryRunDailyRun())
                if mode.is_dry_run()
                else open_daily_run(crm, run_id=run_id, run_date=today)
            )

            try:
                with daily_run_cm as daily_run:
                    # Bulk-preload person records once so all four phases share a warm cache.
                    # Skips ~593 redundant get_person calls per run vs. the per-phase cache pattern.
                    list_id = os.environ.get("ATTIO_LIST_ID", "")
                    entries = crm.query_list_entries(list_id=list_id)
                    all_record_ids = {e.record_id for e in entries}
                    all_record_ids.discard(None)
                    all_record_ids.discard("")
                    click.echo(f"Preloading {len(all_record_ids)} pipeline person records...")
                    cache = RecordCache(crm)
                    preloaded = preload_pipeline_persons(
                        crm, cache, all_record_ids, metrics=metrics,
                    )
                    click.echo(f"  Preloaded {preloaded} records.\n")

                    # Pre-flight: Attio schema-drift gate (wet path only).
                    # Before ANY send, verify the schema manifest is valid and
                    # every shipped/verified attribute actually exists in
                    # production Attio. This is the gate that would have caught
                    # the 2026-05-28 partial-send halt, where merged code wrote
                    # experiment_id_frozen_at while the attribute was still
                    # undeployed. Runs before Phase 0 so a schema gap aborts
                    # the run before any PB launch or Attio mutation.
                    if not mode.is_dry_run():
                        click.echo("--- Pre-flight: Attio schema-drift check ---")
                        from scripts.validate_attio_schema_deltas import (
                            DEFAULT_MANIFEST,
                            check_attio_shipped,
                            load_manifest,
                            validate,
                        )
                        _manifest = load_manifest(DEFAULT_MANIFEST)
                        _schema_errors = validate(_manifest) + check_attio_shipped(
                            _manifest, statuses=frozenset({"shipped", "verified"})
                        )
                        if _schema_errors:
                            for _e in _schema_errors:
                                click.echo(f"  ✗ {_e}", err=True)
                            raise click.ClickException(
                                f"Attio schema-drift pre-flight FAILED "
                                f"({len(_schema_errors)} error(s) above). Refusing "
                                f"to send. Deploy/repair the schema (e.g. `python "
                                f"scripts/migrate_attio_linkedin_outreach_schema.py "
                                f"--apply`) and re-run /sales-daily in a fresh shell."
                            )
                        click.echo(
                            "  ✓ manifest valid + all shipped/verified attrs live\n"
                        )

                    # Crash-recovery scan: reconcile any DMs that PB sent but
                    # Attio never recorded (process-killed-mid-run scenario).
                    # Runs before Phase 0 so prior-run unrecorded sends are
                    # reconciled before any new sends fire.  Best-effort: a
                    # PB API hiccup must not block the daily run.
                    click.echo("--- Pre-run: DM send crash recovery ---")
                    if message_sender_id:
                        # Recovery now routes its advance through the §3.15
                        # AttioWriter path, which RE-RAISES the programmer-bug
                        # class (unauthorized-writer / monotonicity /
                        # terminal-class) precisely so the run HALTS. Those must
                        # NOT be downgraded to a best-effort warning: a recovery
                        # that mis-advances (or fails to advance) leaves the DM
                        # sequencer free to re-send — the §3.1 duplicate-DM red
                        # line. Let them propagate; only infra hiccups (PB fetch
                        # / CSV download) are best-effort below.
                        from clients.attio_writer import (
                            AttioMonotonicityViolation,
                            AttioTerminalClassRegression,
                            UnauthorizedAttioWriteError,
                        )
                        from workflows.pb_send_recovery import (
                            recover_unrecorded_dm_sends,
                        )
                        try:
                            # recover_unrecorded_dm_sends parses entries via the
                            # raw ``AttioClient.parse_entry`` classmethod and
                            # advances through ``_attio_advance_with_escalation``
                            # — Attio-coupled, daily-only, unmigrated. Convert at
                            # the boundary (escape hatch) to keep it byte-identical.
                            recovery_summary = recover_unrecorded_dm_sends(
                                _attio_inner_client(crm),
                                pb,
                                message_sender_id=message_sender_id,
                                list_id=list_id,
                                today=today.isoformat(),
                                dry_run=dry_run,
                            )
                        except (
                            UnauthorizedAttioWriteError,
                            AttioMonotonicityViolation,
                            AttioTerminalClassRegression,
                        ):
                            raise
                        except Exception as _rec_exc:  # noqa: BLE001
                            # Best-effort: a PB API / CSV-download hiccup must
                            # not block the daily run.
                            click.echo(
                                f"  [pb_send_recovery] WARNING: recovery scan failed "
                                f"({type(_rec_exc).__name__}: {_rec_exc}); "
                                f"continuing daily run.",
                                err=True,
                            )
                        else:
                            recovered = recovery_summary.get("recovered", 0)
                            write_failed = recovery_summary.get(
                                "skipped_write_failed", 0
                            )
                            if recovered:
                                click.echo(
                                    f"  Recovered {recovered} unrecorded DM "
                                    f"send(s) from last run."
                                )
                            if write_failed:
                                # Loud: these entries are stranded at their prior
                                # stage and WILL be re-DM'd next run unless the
                                # operator reconciles the attio_write_failed rows.
                                click.echo(
                                    f"  ⚠ {write_failed} unrecorded send(s) FAILED "
                                    f"to record — reconcile attio_write_failed "
                                    f"queue rows before the next run.",
                                    err=True,
                                )
                            if not recovered and not write_failed:
                                click.echo("  No unrecorded sends to recover.")
                            click.echo("")
                    else:
                        click.echo("  Skipping (no PB_MESSAGE_SENDER_ID set)\n")

                    # Phase 0: Live-check CONNECTION_SENT profiles for acceptances.
                    # Gate: when the SN backend is selected, require the SN
                    # scraper id; otherwise the legacy id. The two scrapers
                    # have different argument shapes and different CSV column
                    # contracts, and the workspace currently only has the SN
                    # phantom deployed (the legacy phantom was deleted as part
                    # of the same migration that introduced PRE_INVITE_DEGREE_CHECK_BACKEND).
                    click.echo("--- Phase 0: Detect Accepted Connections ---")
                    phase0_backend = load_pb_config().degree_check_backend_raw.strip()
                    if phase0_backend not in _VALID_BACKENDS:
                        # Validate the VALUE at the routing gate. A typo'd
                        # backend would otherwise route Phase 0 to the legacy
                        # id; with that id unset, Phase 0 (and the SN health
                        # gate, which keys on the same comparison) silently
                        # skips — and on a day with an empty invite pool
                        # nothing downstream calls the validating resolver,
                        # so the run exits 0 with acceptance detection off.
                        raise click.ClickException(
                            f"PRE_INVITE_DEGREE_CHECK_BACKEND="
                            f"{phase0_backend!r} is not a valid backend "
                            f"({' | '.join(_VALID_BACKENDS)}) — fix .env; "
                            "refusing to guess which scraper to launch."
                        )
                    phase0_required_id = (
                        sales_nav_profile_scraper_id
                        if phase0_backend == "sales_nav"
                        else profile_scraper_id
                    )
                    phase0_missing_env = (
                        "PB_SALES_NAV_PROFILE_SCRAPER_ID"
                        if phase0_backend == "sales_nav"
                        else "PB_PROFILE_SCRAPER_ID"
                    )
                    if mode.is_dry_run():
                        click.echo("Skipping (dry run)\n")
                        metrics.pb_launches_skipped_dry_run += 1
                    elif phase0_required_id:
                        metrics.pb_launches_attempted += 1
                        # detect_accepted_connections is daily-only but deeply
                        # Attio-coupled (_get_all_entries_parsed + cache reads
                        # priming attio._person_to_company + _attio_advance);
                        # keep it on the raw client via the escape hatch.
                        accept_result = detect_accepted_connections(
                            _attio_inner_client(crm), pb, profile_scraper_id, cache=cache,
                            sales_nav_profile_scraper_id=sales_nav_profile_scraper_id,
                        )
                        _phase0_deferred = accept_result.get("deferred", 0)
                        _phase0_deferred_note = (
                            f" ({_phase0_deferred} stale profile(s) beyond the "
                            f"per-run scrape cap, deferred to subsequent runs)"
                            if _phase0_deferred
                            else ""
                        )
                        click.echo(
                            f"Auto-detected {accept_result.get('accepted', 0)} "
                            f"accepted connections.{_phase0_deferred_note}\n"
                        )
                    else:
                        click.echo(f"Skipping (no {phase0_missing_env} set for backend={phase0_backend})\n")

                    # Phase 0.5: Detect message responses.
                    click.echo("--- Phase 0.5: Detect Message Responses ---")
                    if mode.is_dry_run():
                        click.echo("Skipping (dry run)\n")
                        metrics.pb_launches_skipped_dry_run += 1
                    elif inbox_scraper_id:
                        metrics.pb_launches_attempted += 1
                        # PR-18 B-SD-012 wiring: ResendClient enables the
                        # hot-lead email alert channel. Queue-row
                        # durability holds either way (resend=None still
                        # opens the operator queue row); the email is
                        # the convenience nudge that fires only when
                        # ``RESEND_API_KEY`` is set.
                        with _maybe_resend_client() as resend_client:
                            try:
                                # detect_responses is §7-shared (imported by
                                # hot_lead_alert / escalation_schemas) and stays
                                # on the raw AttioClient; convert at the boundary.
                                resp_result = detect_responses(
                                    _attio_inner_client(crm), pb, inbox_scraper_id,
                                    cache=cache, resend=resend_client,
                                    daily_run=daily_run if isinstance(daily_run, DailyRun) else None,
                                )
                            except NoCSVHalt as exc:
                                # PR-19 B-SD-005: typed halt with exit
                                # code 2 (operator-visible non-success;
                                # distinct from EX_TEMPFAIL=75 for lock
                                # contention). The pb_csv_empty queue
                                # row + reply_detection_status='failed'
                                # were already written by detect_responses
                                # before it raised.
                                click.echo(
                                    f"  ⚠ Phase 0.5 halted: {exc}",
                                    err=True,
                                )
                                metrics.warn(
                                    f"pb_csv_empty halt: container={exc.container_id}"
                                )
                                raise SystemExit(2) from exc
                        click.echo(f"Detected {resp_result.get('detected', 0)} new responses.\n")
                    else:
                        click.echo("Skipping (no PB_INBOX_SCRAPER_ID set)\n")

                    # Pipeline-starvation check (PR-43).
                    click.echo("--- Pipeline starvation check ---")
                    if mode.is_dry_run():
                        click.echo("Skipping (dry run)\n")
                    else:
                        try:
                            # evaluate_pipeline_starvation reads entries through
                            # the shared raw _get_all_entries_parsed path, so it
                            # takes the inner AttioClient (escape hatch).
                            starvation_summary = evaluate_pipeline_starvation(
                                _attio_inner_client(crm), today
                            )
                            fired = starvation_summary.get("triggers_fired") or []
                            metrics.starvation_triggers_fired += len(fired)
                            if fired:
                                click.echo(
                                    f"⚠ Starvation triggers fired: {', '.join(fired)} "
                                    f"(invite_eligible_pool="
                                    f"{starvation_summary.get('invite_eligible_pool')}). "
                                    f"Queue row(s) opened.\n"
                                )
                            else:
                                click.echo(
                                    f"OK (pool={starvation_summary.get('invite_eligible_pool')}, "
                                    f"runway="
                                    f"{starvation_summary.get('runway_bdays_remaining')} bdays)\n"
                                )
                        except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                            # Wave-1.6 FIX-3a: narrow catch + escalate + re-raise.
                            # The pre-Wave-1.6 broad `except Exception` swallowed
                            # this same path with a click.echo warning, defeating
                            # `evaluate_pipeline_starvation`'s documented `raise`
                            # contract (workflows/starvation.py:16) and the §0
                            # invariant #9 (no silent fallbacks). Only transient
                            # Attio failures (timeout / 5xx) escalate-and-re-raise;
                            # programmer bugs (KeyError, TypeError) bubble up
                            # unfiltered as they did before.
                            transient = (
                                isinstance(exc, httpx.TimeoutException)
                                or (
                                    isinstance(exc, httpx.HTTPStatusError)
                                    and exc.response.status_code in {502, 503, 504}
                                )
                            )
                            if not transient:
                                raise
                            escalate(
                                type="pipeline_starvation_check_failed",
                                idempotency_key=(
                                    f"starvation-check-failed|{today.isoformat()}"
                                ),
                                payload={
                                    "today": today.isoformat(),
                                    "error_class": type(exc).__name__,
                                    "error_msg": str(exc),
                                    "context": "evaluator_failed_to_run",
                                },
                                attio=crm,
                            )
                            raise

                    # PR-B.7: Sales Nav health pre-flight (only when
                    # backend=sales_nav). Checks env, runs a 1-profile
                    # Sales Nav scrape + 1-thread inbox scrape to confirm
                    # cookies coexist before the operator approves any
                    # invite batch. Closes the GTM-lens cookie-rotation
                    # gap — see plan reflective-singing-waterfall.md PR-B.7.
                    if load_pb_config().degree_check_backend_raw.strip() == "sales_nav":
                        if mode.is_dry_run():
                            # Wave-1.6-ext FIX-1' (adversarial SB-1):
                            # the health pre-flight invokes
                            # validate_sales_nav_health.quick_check()
                            # which launches REAL PB Sales Nav Profile
                            # Scraper + Inbox Scraper containers. Same
                            # dry-run discipline as FIX-1 in
                            # pre_invite_check.py — under --dry-run no
                            # live PB calls fire, period. Operators who
                            # want to validate Sales Nav cookies invoke
                            # `python scripts/validate_sales_nav_health.py`
                            # directly. See done-qa-adversarial round-2
                            # finding F1 (cli.py:348-367 was bypassing
                            # FIX-1 whenever backend=sales_nav).
                            click.echo(
                                "--- Pre-flight: Sales Nav health check "
                                "(SKIPPED in --dry-run) ---"
                            )
                            metrics.pb_launches_skipped_dry_run += 2
                        else:
                            click.echo("--- Pre-flight: Sales Nav health check ---")
                            from scripts.validate_sales_nav_health import quick_check
                            rc, summary = quick_check()
                            click.echo(summary)
                            if rc == 1:
                                # FAIL: stop the daily run. Operator must rotate
                                # cookies. (The old advice to flip
                                # PRE_INVITE_DEGREE_CHECK_BACKEND=regular is dead:
                                # the legacy Profile Scraper agent was deleted from
                                # the PB workspace — there is no backend to roll
                                # back to.) Per
                                # docs/runbooks/phantombuster-cookie-rotation.md.
                                raise click.ClickException(
                                    "Sales Nav pre-flight FAILED — fix the named "
                                    "issue above (typically rotate cookies via "
                                    "docs/runbooks/phantombuster-cookie-rotation.md), "
                                    "then re-run /sales-daily in a fresh shell."
                                )
                            # rc=2 (WARN) prints the warning and continues —
                            # caller still gets to approve the batch interactively.

                    # Part A: Connection requests + re-checks.
                    click.echo("--- Part A: Connection Requests ---")
                    if network_booster_id:
                        # run_connection_requests is daily-only but Attio-quirk
                        # coupled (throttle reads attio.get_company raw dict +
                        # attio._person_to_company, _pre_invite_degree_check,
                        # _get_all_entries_parsed); escape hatch at the boundary.
                        conn_result = run_connection_requests(
                            _attio_inner_client(crm), pb, network_booster_id,
                            batch_size=batch_size, dry_run=dry_run, auto_confirm=yes, cache=cache,
                            profile_scraper_id=profile_scraper_id,
                            sales_nav_profile_scraper_id=sales_nav_profile_scraper_id,
                            audit_logger=audit_logger,
                            today=today,
                            # _DryRunDailyRun structurally satisfies DailyRun
                            # here the same way it does for run_dm_sequencing.
                            daily_run=daily_run,  # type: ignore[arg-type]
                        )
                    else:
                        click.echo("Skipping connections (no PB_NETWORK_BOOSTER_ID set)")
                        conn_result = {"sent": 0, "reason": "no_agent_id"}

                    # Part B: DM sequencing (Mon-Fri only)
                    click.echo("\n--- Part B: DM Sequencing ---")
                    if not dms_allowed:
                        click.echo(f"Weekend ({today}) — skipping DM sends. Use --force-weekend to override.")
                        dm_result: dict[str, Any] = {"dm1": 0, "dm2": 0, "dm3": 0, "reason": "weekend"}
                    elif skip_dms:
                        click.echo("Skipping DMs (--skip-dms)")
                        dm_result = {"dm1": 0, "dm2": 0, "dm3": 0, "reason": "skip_dms"}
                    elif message_sender_id:
                        # _DryRunDailyRun structurally satisfies DailyRun
                        # for run_dm_sequencing's needs but isn't a
                        # subclass — accept the type mismatch.
                        # run_dm_sequencing shares run_connection_requests'
                        # Attio-quirk coupling (throttle, is_person_company_corrupted,
                        # _get_all_entries_parsed); escape hatch at the boundary.
                        dm_result = run_dm_sequencing(
                            _attio_inner_client(crm), pb, message_sender_id,
                            dry_run=dry_run, auto_confirm=yes, cache=cache,
                            audit_logger=audit_logger,
                            daily_run=daily_run,  # type: ignore[arg-type]
                        )
                    else:
                        click.echo("Skipping DMs (no PB_MESSAGE_SENDER_ID set)")
                        dm_result = {"dm1": 0, "dm2": 0, "dm3": 0, "reason": "no_agent_id"}

                    # PR-17 B-SD-011: write the run-end summary onto the
                    # daily_run row before context exit. Skipped on
                    # dry-run because daily_run is a ``_DryRunDailyRun``
                    # stub with no real Attio record_id.
                    if not mode.is_dry_run() and isinstance(daily_run, DailyRun):
                        due_counts = compute_due_dm_counts(
                            crm, cache=cache, today=today
                        )
                        run_summary = run_end_summary(
                            crm, daily_run,
                            prospect_pool_size=due_counts["prospect_pool_size"],
                            due_dm1_count=due_counts["due_dm1_count"],
                            due_dm2_count=due_counts["due_dm2_count"],
                            due_dm3_count=due_counts["due_dm3_count"],
                            today=today,
                        )
                        click.echo(
                            f"\n  Run-end summary: pool={run_summary['prospect_pool_size']}, "
                            f"due_dm1={run_summary['due_dm1_count']}, "
                            f"due_dm2={run_summary['due_dm2_count']}, "
                            f"due_dm3={run_summary['due_dm3_count']}, "
                            f"degree_unknown={run_summary['degree_unknown_count']}, "
                            f"signal={run_summary['starvation_signal']}."
                        )
                        # Read-only cohort visibility: how many rows landed in
                        # DM1_SENT per send-date over the last business week, so
                        # a genuine daily cohort is legible against same-day
                        # re-prospected duplicates inflating the stage total.
                        dm1_cohort = compute_dm1_sent_cohort_by_date(
                            _attio_inner_client(crm), today=today
                        )
                        if dm1_cohort:
                            breakdown = ", ".join(
                                f"{day}={count}" for day, count in dm1_cohort
                            )
                            click.echo(
                                f"  DM1 Sent cohort by send-date (last 5 business "
                                f"days): {breakdown}."
                            )
                        else:
                            click.echo(
                                "  DM1 Sent cohort by send-date (last 5 business "
                                "days): none."
                            )

                    click.echo("\n=== Daily Check Complete ===")
                    click.echo(f"Connections sent: {conn_result.get('sent', 0)}")
                    total_dms = dm_result.get("dm1", 0) + dm_result.get("dm2", 0) + dm_result.get("dm3", 0)
                    click.echo(f"DMs sent: {total_dms}")
            except MalformedDailyRunRow as exc:
                # The pre-open same-day scan (multi-row incident guard)
                # fails closed on a prior row with a corrupt counter.
                # EXIT_TEMPFAIL keeps launchd's retry/backoff semantics;
                # the WARN + this message are the operator's signal to fix
                # or archive the bad row in the CRM.
                click.echo(
                    f"  ⚠ REFUSE: {exc} Fix or archive the bad daily_run "
                    f"row in your CRM, then re-run. Exiting EX_TEMPFAIL (75).",
                    err=True,
                )
                raise SystemExit(EXIT_TEMPFAIL) from exc
            except ConcurrentRunInAttio as exc:
                # PR-17 fold-in (engineer-QA IMPORTANT): use exc.run_date,
                # not today.isoformat(). The exception carries the exact
                # string the Attio uniqueness_key was opened with.
                collision_key = f"{exc.run_date}|{exc.machine_id}"
                # PR-17 fold-in (silent-failure-hunter IMPORTANT): if
                # escalate() itself fails, still exit 75 so launchd
                # retries instead of seeing a raw traceback.
                try:
                    escalate(
                        type="daily_run_collision",
                        idempotency_key=collision_key,
                        payload={
                            "run_date": exc.run_date,
                            "machine_id": exc.machine_id,
                            "attempted_run_id": run_id,
                            "existing": exc.existing or {},
                        },
                        attio=crm,
                    )
                except Exception as escalate_exc:
                    click.echo(
                        f"  ⚠ daily_run_collision escalation FAILED to "
                        f"open queue row: "
                        f"{type(escalate_exc).__name__}: {escalate_exc}. "
                        f"Original collision: {collision_key}. Exiting "
                        f"EX_TEMPFAIL anyway so launchd retries.",
                        err=True,
                    )
                click.echo(
                    f"  ⚠ daily_run uniqueness collision on "
                    f"{collision_key} — another machine is already "
                    f"running. Opening daily_run_collision queue row "
                    f"and exiting EX_TEMPFAIL (75).",
                    err=True,
                )
                raise SystemExit(EXIT_TEMPFAIL) from exc
    except RunLockHeld as exc:
        log_lock_refused(lock_name, exc)
        raise SystemExit(EXIT_TEMPFAIL) from exc
    finally:
        # Survives any unhandled exception escaping the try-block above.
        click.echo("", err=True)
        click.echo(metrics.render(), err=True)


@cli.command(name="send-dms")
@click.option("--dry-run", is_flag=True, help="Resolve + print the DM list; send nothing")
@click.option("--yes", "-y", is_flag=True, help="Send the resolved DMs (re-runs response detection first)")
@click.option("--batch-size", default=25, help="Reserved for cap math parity with daily")
@click.option("--message-sender-id", envvar="PB_MESSAGE_SENDER_ID", help="PhantomBuster Message Sender agent ID")
@click.option("--inbox-scraper-id", envvar="PB_INBOX_SCRAPER_ID", help="PhantomBuster Inbox Scraper agent ID (response re-detect)")
@click.option("--force-weekend", is_flag=True, help="Override the Mon-Fri-only DM rule")
def send_dms(dry_run, yes, batch_size, message_sender_id, inbox_scraper_id, force_weekend):
    """Send-DMs phase: reattach to today's daily_run row and send Part B only.

    Runs ONLY after `daily --yes --skip-dms` opened the day. `--dry-run`
    resolves + prints the DM list against committed CRM state (sends nothing);
    `--yes` re-runs response detection then sends. The reattach
    (workflows.daily_run.attach_daily_run) binds the EXISTING daily_run row so
    the per-day message cap survives across the invite and DM phases. A bare
    `send-dms` with neither flag is an interactive confirm (run_dm_sequencing
    prompts before sending).
    """
    from clients.phantombuster import PhantomBusterClient
    from models.business_calendar import is_send_day, operator_today
    from models.run_mode import RunMode
    from workflows.audit import AuditLogger
    from workflows.daily_check import run_dm_sequencing
    from workflows.daily_run import (
        MalformedDailyRunRow,
        NoDailyRunRow,
        ReopenCollision,
    )
    from workflows.detect_responses import NoCSVHalt, detect_responses
    from workflows.metrics import DailyRunMetrics
    from workflows.record_cache import RecordCache, preload_pipeline_persons
    from workflows.run_lock import (
        EXIT_TEMPFAIL,
        RunLockHeld,
        acquire_run_lock,
        log_lock_refused,
    )
    from workflows.weekly_prospect import _attio_inner_client

    mode = RunMode.from_dry_run_flag(dry_run)
    metrics = DailyRunMetrics()

    click.echo("=== Outbound Agent -- Send DMs ===\n")
    click.echo(f"Mode: {mode.value}\n")

    # Experiment pre-flight (parity with daily): a >1 running-experiment
    # ambiguity is a hard abort BEFORE any send. Other failures defer.
    from models.experiment import (
        MultipleRunningExperimentsError,
        get_current_experiment_id,
    )
    try:
        _ = get_current_experiment_id()
    except MultipleRunningExperimentsError as exc:
        click.echo(
            f"ABORT: cannot send DMs — {exc} Close one experiment and re-run.",
            err=True,
        )
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 — defer to in-flight handling
        click.echo(
            f"[send-dms pre-flight] experiment check skipped: "
            f"{type(exc).__name__}: {exc}",
            err=True,
        )

    today = operator_today()
    lock_name = "sales-daily"  # SAME lock as daily — serializes step 1 / step 2
    run_id = f"send-dms-{today.isoformat()}-{os.getpid()}"

    try:
        with acquire_run_lock(lock_name, run_id=run_id), \
                AuditLogger(workflow="send_dms", dry_run=dry_run) as audit_logger, \
                _crm_provider() as crm, PhantomBusterClient() as pb:
            # Schema-drift pre-flight (wet path only) — parity with daily.
            if not mode.is_dry_run():
                click.echo("--- Pre-flight: Attio schema-drift check ---")
                from scripts.validate_attio_schema_deltas import (
                    DEFAULT_MANIFEST,
                    check_attio_shipped,
                    load_manifest,
                    validate,
                )
                _manifest = load_manifest(DEFAULT_MANIFEST)
                _schema_errors = validate(_manifest) + check_attio_shipped(
                    _manifest, statuses=frozenset({"shipped", "verified"})
                )
                if _schema_errors:
                    for _e in _schema_errors:
                        click.echo(f"  ✗ {_e}", err=True)
                    raise click.ClickException(
                        f"Attio schema-drift pre-flight FAILED "
                        f"({len(_schema_errors)} error(s) above). Refusing to send."
                    )
                click.echo("  ✓ manifest valid + all shipped/verified attrs live\n")

            # Reattach to today's existing daily_run row (never open a new one).
            # --dry-run attaches read-only: a preview must never reopen a
            # terminal row and re-close it "completed", silently rewriting a
            # prior "failed" status.
            try:
                with attach_daily_run(
                    crm, run_id=run_id, run_date=today,
                    read_only=mode.is_dry_run(),
                ) as daily_run:
                    # --- EARLY GUARD: staleness ---
                    if daily_run.run_date != today.isoformat():
                        click.echo(
                            f"  ⚠ REFUSE: bound daily_run row is for "
                            f"{daily_run.run_date}, not today ({today.isoformat()}). "
                            f"Run `daily --skip-dms` today first.",
                            err=True,
                        )
                        raise SystemExit(EXIT_TEMPFAIL)

                    # --- EARLY GUARD: weekend (invites-only days refuse DMs) ---
                    if not is_send_day(today) and not force_weekend:
                        click.echo(
                            f"  ⚠ REFUSE: {today} is a weekend — DMs are invites-"
                            f"only policy. Use --force-weekend to override.",
                            err=True,
                        )
                        raise SystemExit(EXIT_TEMPFAIL)

                    # --- EARLY GUARD: reply-status fail-closed ---
                    # `!= "ok"` (covers None/missing/failed) — NOT `== "failed"`.
                    status = daily_run.get_reply_detection_status()
                    if status != "ok":
                        click.echo(
                            f"  ⚠ REFUSE: reply_detection_status={status!r} (not "
                            f"'ok'). Today's inbox scrape did not complete cleanly; "
                            f"do not send. Re-run `daily --skip-dms` to re-detect.",
                            err=True,
                        )
                        raise SystemExit(EXIT_TEMPFAIL)

                    list_id = os.environ.get("ATTIO_LIST_ID", "")
                    entries = crm.query_list_entries(list_id=list_id)
                    all_record_ids = {e.record_id for e in entries}
                    all_record_ids.discard(None)
                    all_record_ids.discard("")
                    cache = RecordCache(crm)
                    preload_pipeline_persons(
                        crm, cache, all_record_ids, metrics=metrics
                    )

                    # --- Re-detect responses BEFORE sending (close review-gap) ---
                    # WET --yes only: --dry-run must NEVER trigger a live PB inbox
                    # scrape (parity with daily, which gates detection on
                    # `not mode.is_dry_run()`).
                    if yes and not mode.is_dry_run() and inbox_scraper_id:
                        click.echo("--- Re-detect: response detection (close review-gap race) ---")
                        with _maybe_resend_client() as resend_client:
                            try:
                                detect_responses(
                                    _attio_inner_client(crm), pb, inbox_scraper_id,
                                    cache=cache, resend=resend_client,
                                    daily_run=daily_run,
                                )
                            except NoCSVHalt as exc:
                                # A failed re-scrape aborts BEFORE any send.
                                click.echo(
                                    f"  ⚠ Response re-detect halted: {exc}", err=True
                                )
                                raise SystemExit(2) from exc
                        # --- RE-CHECK the guard on the POST-re-detect value:
                        # detect_responses overwrote reply_detection_status; the
                        # stale step-1 value is gone.
                        status = daily_run.get_reply_detection_status()
                        if status != "ok":
                            click.echo(
                                f"  ⚠ REFUSE: post-re-detect reply_detection_status="
                                f"{status!r} (not 'ok'). Aborting before any DM.",
                                err=True,
                            )
                            raise SystemExit(EXIT_TEMPFAIL)
                    elif yes and not mode.is_dry_run() and not inbox_scraper_id:
                        # WET send with no inbox-scraper-id: the review-gap race-
                        # closer can't run. Sending on the step-1 reply status alone
                        # is still guarded by the early reply-status check, but the
                        # operator must SEE that the race was not re-closed — never
                        # skip this silently.
                        click.echo(
                            "  ⚠ --yes set but no inbox-scraper-id — skipping "
                            "response re-detect; sending on step-1 reply status only "
                            "(review-gap race NOT re-closed).",
                            err=True,
                        )

                    if not message_sender_id:
                        click.echo("Skipping DMs (no PB_MESSAGE_SENDER_ID set)")
                        return

                    click.echo("\n--- Part B: DM Sequencing ---")
                    run_dm_sequencing(
                        _attio_inner_client(crm), pb, message_sender_id,
                        dry_run=dry_run, auto_confirm=yes, cache=cache,
                        audit_logger=audit_logger,
                        daily_run=daily_run,
                    )
            except NoDailyRunRow as exc:
                click.echo(f"  ⚠ {exc} Exiting EX_TEMPFAIL.", err=True)
                raise SystemExit(EXIT_TEMPFAIL) from exc
            except ReopenCollision as exc:
                click.echo(f"  ⚠ {exc} Exiting EX_TEMPFAIL.", err=True)
                raise SystemExit(EXIT_TEMPFAIL) from exc
            except MalformedDailyRunRow as exc:
                # Fail-closed guard: a fetched row with a missing/unparseable
                # counter is untrustworthy — defaulting it to 0 would silently
                # reset the message cap and permit a 60-DM breach. Refuse to send
                # (same EX_TEMPFAIL treatment as the other reattach failures).
                click.echo(
                    f"  ⚠ {exc} Today's daily_run row is untrustworthy — refusing "
                    f"to send. Exiting EX_TEMPFAIL.",
                    err=True,
                )
                raise SystemExit(EXIT_TEMPFAIL) from exc
    except RunLockHeld as exc:
        log_lock_refused(lock_name, exc)
        raise SystemExit(EXIT_TEMPFAIL) from exc
    finally:
        click.echo("", err=True)
        click.echo(metrics.render(), err=True)


@cli.command()
@click.option("--dry-run", is_flag=True, help="Score prospects without writing to Attio")
@click.option("--batch-size", default=100, help="Max prospects to export")
@click.option("--search-export-id", default=lambda: load_pb_config().search_export_id or None, help="PhantomBuster Search Export agent ID (default: config/phantombuster.yaml → PB_SEARCH_EXPORT_ID)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts (for cron)")
def weekly(dry_run, batch_size, search_export_id, yes):
    """Weekly prospecting: export, qualify, and load new prospects."""
    from clients.phantombuster import PhantomBusterClient
    from workflows.run_lock import (
        EXIT_TEMPFAIL,
        RunLockHeld,
        acquire_run_lock,
        log_lock_refused,
    )
    from workflows.weekly_prospect import run_weekly_prospecting

    click.echo("=== Outbound Agent -- Weekly Prospecting ===\n")

    # PR-21 pre-flight (mirrors daily's §0 invariant #9 guard): if two or more
    # experiments are `running`, abort BEFORE any prospects commit so we never
    # get partial-batch state with mixed cohort tags. Any other exception
    # (transient Attio, missing credentials) is left for in-flight handling.
    from models.experiment import (
        MultipleRunningExperimentsError,
        get_current_experiment_id,
    )
    try:
        _ = get_current_experiment_id()
    except MultipleRunningExperimentsError as exc:
        click.echo(
            f"ABORT: cannot run weekly — {exc}. Close one experiment in Attio first.",
            err=True,
        )
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 — defer to in-flight error handling
        # One-line debug log so a pre-flight failure (Attio down, missing
        # creds) is distinguishable from in-flight failures further down
        # the run. Mirrors daily pre-flight.
        click.echo(
            f"[weekly pre-flight] experiment check skipped: "
            f"{type(exc).__name__}: {exc}",
            err=True,
        )

    if not search_export_id:
        click.echo("Error: PB_SEARCH_EXPORT_ID not set. Set it in .env or pass --search-export-id.")
        raise SystemExit(1)

    lock_name = "sales-weekly"
    run_id = f"{lock_name}-{date.today().isoformat()}-{os.getpid()}"
    try:
        with acquire_run_lock(lock_name, run_id=run_id), \
                _crm_provider() as crm, PhantomBusterClient() as pb:
            run_weekly_prospecting(
                crm, pb, search_export_id,
                batch_size=batch_size, dry_run=dry_run,
            )
    except RunLockHeld as exc:
        log_lock_refused(lock_name, exc)
        raise SystemExit(EXIT_TEMPFAIL) from exc


@cli.command()
@click.option(
    "--send", is_flag=True,
    help="Actually send the report email (default: dry-run; sidecar Attio row is always written).",
)
@click.option("--email", envvar="REPORT_EMAIL", default="ops@example.com", help="Report recipient")
def report(send, email):
    """Weekly KPI report: compute metrics, write JSON sidecar, optionally email.

    PR-30: defaults to DRY-RUN per §0 #9 (no silent side-effects).
    Operators must pass --send explicitly to dispatch the Resend email.
    The Weekly KPI Snapshot JSON sidecar is written to
    reports/weekly-kpi/<week_starting>.json on every run so subagents
    read historical reports by `week_starting`.
    """
    from clients.crm.factory import get_crm_provider
    from clients.resend_client import ResendClient
    from workflows.weekly_report import run_weekly_report

    dry_run = not send
    click.echo("=== Outbound Agent -- Weekly Report ===\n")
    if dry_run:
        click.echo(
            "[DRY-RUN MODE] Sidecar row will be written; email will NOT "
            "be sent. Pass --send to dispatch the email.",
        )

    with get_crm_provider() as bundle:
        assert bundle.attio is not None, "Attio adapter required for weekly report"
        resend = None if dry_run else ResendClient()
        try:
            run_weekly_report(
                bundle.attio,
                resend,
                report_email=email,
                dry_run=dry_run,
                crm=bundle.provider,
            )
        finally:
            if resend:
                resend.close()


@cli.command("check-responses")
def check_responses():
    """Manual response check: input which prospects responded."""
    from workflows.daily_check import check_responses_manual

    click.echo("=== Outbound Agent -- Check Responses ===\n")

    with _attio_client() as attio:
        check_responses_manual(attio)


@cli.command()
def limits():
    """Show current daily safety limits."""
    from workflows.safety_limits import get_status

    click.echo("=== Daily Safety Limits ===\n")
    click.echo(get_status())


@cli.command()
def canary():
    """§3.20 Attio scope canary — verify the REST write+delete path is live.

    Every skill's Step-0 preflight runs this AFTER an MCP read-liveness check
    (whoami + list-lists). It does a create-note → delete-note round-trip
    through the REST client, exercising the SAME ATTIO_API_KEY credential the
    daily run mutates prospect data with. The round-trip targets a dedicated,
    inert canary record (CANARY_PERSON_RECORD_ID) — never a real prospect.

    Exit 0 = full read+write+delete scope confirmed. Non-zero + a typed
    `mcp_scope_insufficient` line on stderr = halt the skill before any
    real Attio write.
    """
    from clients.attio import AttioClient
    from clients.attio_writer_registry import CANARY_PERSON_RECORD_ID

    click.echo("=== Attio scope canary (REST write+delete round-trip) ===\n")

    record_id = CANARY_PERSON_RECORD_ID
    if not record_id:
        click.echo(
            "mcp_scope_insufficient: canary_record_unconfigured — "
            "CANARY_PERSON_RECORD_ID is empty in clients/attio_writer_registry.py. "
            "Create an inert Person record (not in the linkedin_outreach list) "
            "and pin its id before running any skill.",
            err=True,
        )
        raise SystemExit(1)

    # Missing credential is itself a scope problem — emit the typed line
    # rather than letting AttioClient() raise a bare KeyError the skill
    # can't recognize.
    if not os.environ.get("ATTIO_API_KEY"):
        click.echo(
            "mcp_scope_insufficient: attio_api_key_unset — ATTIO_API_KEY is not "
            "in the environment (.env); cannot run the scope canary.",
            err=True,
        )
        raise SystemExit(1)

    note_id = None
    with AttioClient() as attio:
        # ---- write leg ----
        click.echo(
            f"Running round-trip on canary record {record_id} "
            "(create → delete; may take up to ~35s per leg if Attio is degraded)…"
        )
        try:
            note = attio.create_note(
                record_id,
                "Outbound agent scope canary",
                "Transient read+write+delete scope check. Auto-deleted; safe to ignore.",
                parent_object="people",
            )
            # Guard `note` itself (not note.get("id")) — this is the most
            # safety-critical line: a non-dict return must fall to None and
            # fail closed, never raise past the contract.
            note_id = note.get("id", {}).get("note_id") if isinstance(note, dict) else None
        except Exception as exc:  # noqa: BLE001 — any failure here means write scope is not live
            click.echo(
                f"mcp_scope_insufficient: write leg failed on canary record "
                f"{record_id} — {type(exc).__name__}: {exc}",
                err=True,
            )
            raise SystemExit(1) from exc

        if not note_id:
            click.echo(
                "mcp_scope_insufficient: write leg returned no note_id "
                f"(canary record {record_id}) — cannot verify delete scope.",
                err=True,
            )
            raise SystemExit(1)

        # ---- delete leg ----
        try:
            deleted = attio.delete_note(note_id)
        except Exception as exc:  # noqa: BLE001 — delete scope is not live
            click.echo(
                f"mcp_scope_insufficient: delete leg failed — note {note_id} on "
                f"canary record {record_id} was created but NOT deleted "
                f"({type(exc).__name__}: {exc}). Orphan note left behind; "
                f"delete it manually (DELETE /v2/notes/{note_id}).",
                err=True,
            )
            raise SystemExit(1) from exc

        if not deleted:
            click.echo(
                f"mcp_scope_insufficient: delete leg reported note {note_id} as "
                f"not present (404/absent) — the note we just created could not "
                f"be deleted; scope unverifiable.",
                err=True,
            )
            raise SystemExit(1)

    click.echo(
        f"OK — read+write+delete confirmed. Round-trip note {note_id} "
        f"created and deleted on canary record {record_id}."
    )


@cli.command()
def pipeline():
    """Show current pipeline status from Attio."""
    from clients.attio import AttioClient
    from models.pipeline import PipelineStage

    click.echo("=== Outbound Agent Pipeline ===\n")

    with _attio_client() as attio:
        list_id = os.environ.get("ATTIO_LIST_ID", "")
        entries = attio.query_list_entries(list_id=list_id)

        counts: dict[str, int] = {}
        for entry in entries:
            stage = AttioClient.parse_entry(entry)["stage"] or "Unknown"
            counts[stage] = counts.get(stage, 0) + 1

        total = sum(counts.values())
        click.echo(f"Total entries: {total}\n")
        for stage_enum in PipelineStage:
            count = counts.get(stage_enum.value, 0)
            bar = "#" * count
            click.echo(f"  {stage_enum.value:20s} {count:3d} {bar}")


@cli.command("backfill-export")
def backfill_export_cmd():
    """Export pipeline records missing company links for PB enrichment."""
    from workflows.backfill_companies import backfill_export

    click.echo("=== Outbound Agent -- Backfill Export ===\n")

    with _crm_provider() as crm:
        file_path = backfill_export(crm)

    click.echo("\nDone. Run this CSV through PhantomBuster LinkedIn Profile Scraper,")
    click.echo(f"then import with: python3 cli.py backfill-import <pb_output.csv> --export-csv {file_path}")


@cli.command("backfill-import")
@click.argument("pb_csv", type=click.Path(exists=True))
@click.option("--export-csv", required=True, type=click.Path(exists=True), help="Original export CSV from backfill-export")
def backfill_import_cmd(pb_csv, export_csv):
    """Import PB-enriched data and link companies in Attio."""
    from workflows.backfill_companies import backfill_import

    click.echo("=== Outbound Agent -- Backfill Import ===\n")

    with _attio_client() as attio:
        summary = backfill_import(attio, pb_csv, export_csv)

    click.echo(f"\nDone. {summary['linked']} records linked to companies.")


@cli.command("backfill-industries")
@click.option("--dry-run", is_flag=True, help="Classify without writing to Attio")
@click.option("--limit", type=int, default=None, help="Max companies to classify in this run")
def backfill_industries_cmd(dry_run: bool, limit: int | None):
    """Classify companies missing industry_vertical via Haiku."""
    from workflows.industry_classifier import (
        backfill_missing_industries,
        build_anthropic_client,
    )

    click.echo("=== Outbound Agent -- Backfill Industries ===\n")

    # Per §0 invariant #11, build_anthropic_client returns None and the engine
    # does NOT hold ANTHROPIC_API_KEY. The classifier routes to the LLM
    # dispatch path when OUTBOUND_USE_LLM_DISPATCH=1 (set by the parent
    # skill). Without dispatch enabled, classify_industry returns None
    # for every company, which the loop reports as `api_errors`.
    anthropic_client = build_anthropic_client()
    if not os.environ.get("OUTBOUND_USE_LLM_DISPATCH"):
        click.echo(
            "WARNING: OUTBOUND_USE_LLM_DISPATCH is not set. Industry classifier "
            "will return None for all companies (no LLM access). Invoke via the "
            "parent slash-command skill (which exports the env var) for real "
            "classification, or pass a mock client from a test."
        )

    with _attio_client() as attio:
        summary = backfill_missing_industries(
            attio,
            anthropic_client=anthropic_client,
            limit=limit,
            dry_run=dry_run,
        )

    click.echo("")
    click.echo(f"Total scanned: {summary['total_scanned']}")
    click.echo(f"Missing industry: {summary['missing']}")
    click.echo(f"Classified:      {summary['classified']}")
    click.echo(f"Written:         {summary['written']}" + (" (dry run)" if dry_run else ""))
    click.echo(f"Skipped:         {summary['skipped']}")
    click.echo(f"API errors:      {summary['api_errors']}")


@cli.command("email-import")
@click.argument("csv_path", type=click.Path(exists=True), default="exports/tier1_email_campaign_2026-04-07.csv")
@click.option("--dry-run", is_flag=True, help="Preview imports without writing to Attio")
def email_import_cmd(csv_path, dry_run):
    """Import email campaign contacts from CSV into Attio."""
    from workflows.email_campaign import import_contacts

    click.echo("=== Outbound Agent -- Email Import ===\n")

    with _attio_client() as attio:
        import_contacts(attio, csv_path, dry_run=dry_run)


@cli.command("email-daily")
@click.option("--dry-run", is_flag=True, help="Preview sends without emailing")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts (for cron)")
@click.option("--force-weekend", is_flag=True, help="Override the Mon-Fri-only outreach rule")
def email_daily_cmd(dry_run, yes, force_weekend):
    """Daily email drip: send sequenced emails to campaign contacts."""
    from clients.resend_client import ResendClient
    from workflows.email_campaign import run_email_daily
    from workflows.run_lock import (
        EXIT_TEMPFAIL,
        RunLockHeld,
        acquire_run_lock,
        log_lock_refused,
    )

    click.echo("=== Outbound Agent -- Email Daily ===\n")

    # email-wave2 shares this namespace — both write contact email state
    # and must mutually exclude. See cli.py::email_wave2_cmd.
    lock_name = "sales-email-daily"
    run_id = f"{lock_name}-{date.today().isoformat()}-{os.getpid()}"
    try:
        with acquire_run_lock(lock_name, run_id=run_id), _attio_client() as attio:
            resend = None if dry_run else ResendClient()
            try:
                run_email_daily(
                    attio, resend, dry_run=dry_run, auto_confirm=yes, force_weekend=force_weekend
                )
            finally:
                if resend:
                    resend.close()
    except RunLockHeld as exc:
        log_lock_refused(lock_name, exc)
        raise SystemExit(EXIT_TEMPFAIL) from exc


@cli.command("email-unsubscribe")
@click.argument("email")
def email_unsubscribe_cmd(email):
    """Honor an opt-out: mark a contact UNSUBSCRIBED so they're never emailed again."""
    from workflows.email_compliance import unsubscribe_email

    click.echo("=== Outbound Agent -- Email Unsubscribe ===\n")
    with _attio_client() as attio:
        record_id = unsubscribe_email(attio, email)
    if record_id:
        click.echo(f"  Unsubscribed {email} (record {record_id}). They will not be emailed again.")
    else:
        click.echo(f"  No contact found with email {email}. Nothing to do.")


@cli.command("email-wave2")
@click.option("--dry-run", is_flag=True, help="Preview sends without emailing")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts (for cron)")
@click.option("--force-weekend", is_flag=True, help="Override the Mon-Fri-only outreach rule")
@click.option("--max", "max_n", type=int, default=100, show_default=True, help="Max contacts to send in one run")
def email_wave2_cmd(dry_run, yes, force_weekend, max_n):
    """Wave-2 re-engage blast: send a fresh email to contacts stalled mid-sequence."""
    from clients.resend_client import ResendClient
    from workflows.run_lock import (
        EXIT_TEMPFAIL,
        RunLockHeld,
        acquire_run_lock,
        log_lock_refused,
    )
    from workflows.wave2_blast import run_wave2_blast

    click.echo("=== Outbound Agent -- Wave 2 Blast ===\n")

    # Shares the sales-email-daily namespace with `email-daily` — both
    # write contact email state and must mutually exclude. Concurrent
    # runs were a real §3.1-adjacent risk (two waves of email on the
    # same contact in the same minute).
    lock_name = "sales-email-daily"
    run_id = f"sales-email-wave2-{date.today().isoformat()}-{os.getpid()}"
    try:
        with acquire_run_lock(lock_name, run_id=run_id), _attio_client() as attio:
            resend = None if dry_run else ResendClient()
            try:
                run_wave2_blast(
                    attio,
                    resend,
                    dry_run=dry_run,
                    auto_confirm=yes,
                    force_weekend=force_weekend,
                    max_n=max_n,
                )
            finally:
                if resend:
                    resend.close()
    except RunLockHeld as exc:
        log_lock_refused(lock_name, exc)
        raise SystemExit(EXIT_TEMPFAIL) from exc


@cli.command("email-association")
@click.option("--dry-run", is_flag=True, help="Preview sends without emailing")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts (for cron)")
@click.option("--force-weekend", is_flag=True, help="Override the Mon-Fri-only outreach rule")
def email_association_cmd(dry_run, yes, force_weekend):
    """Send one-shot association outreach emails (e.g., AFAMO partnership ask). Idempotent."""
    from clients.resend_client import ResendClient
    from workflows.association_outreach import run_association_outreach

    click.echo("=== Outbound Agent -- Association Outreach ===\n")

    resend = None if dry_run else ResendClient()
    try:
        run_association_outreach(
            resend, dry_run=dry_run, auto_confirm=yes, force_weekend=force_weekend
        )
    finally:
        if resend:
            resend.close()


@cli.command()
@click.option("--dry-run", is_flag=True, help="Measure and report without updating experiment statuses")
def learn(dry_run):
    """Measure experiment cohorts and evaluate results."""
    from workflows.learn import apply_verdict, evaluate_experiments, measure_cohorts, to_experiment_verdict

    click.echo("=== Outbound Agent -- Experiment Learning ===\n")

    # L4-3: acquire a run lock so concurrent invocations of `learn` serialize
    # rather than racing over experiment TSV writes and operator_review_queue
    # openings. Uses the same EXIT_TEMPFAIL (75) pattern as daily/weekly.
    from workflows.run_lock import (
        EXIT_TEMPFAIL,
        RunLockHeld,
        acquire_run_lock,
    )

    lock_name = "sales-learn"
    run_id = f"{lock_name}-{date.today().isoformat()}-{os.getpid()}"

    try:
        with acquire_run_lock(lock_name, run_id=run_id), _attio_client() as attio:
            # 1. Measure all cohorts
            click.echo("--- Cohort Measurements ---")
            cohorts = measure_cohorts(attio)
            for c in cohorts:
                maturity = "MATURE" if c["is_mature"] else f"immature ({c['days_running']}d)"
                click.echo(
                    f"  {c['experiment_id']:30s}  "
                    f"n={c['cohort_size']:3d}  "
                    f"DM'd={c['dmed']:3d}  "
                    f"resp={c['responded']:2d}  "
                    f"rate={c['dm_response_rate']:.1%}  "
                    f"[{maturity}]"
                )
                for n in (1, 2, 3):
                    received = c.get(f"dm{n}_received", 0)
                    replies = c.get(f"dm{n}_replies", 0)
                    if received == 0:
                        click.echo(f"      DM{n}: n/a")
                    else:
                        rate = c.get(f"dm{n}_response_rate", 0.0)
                        click.echo(f"      DM{n}: {replies}/{received} = {rate:.1%}")

            # 2. Evaluate running experiments
            click.echo("\n--- Experiment Verdicts ---")
            verdicts = evaluate_experiments(cohorts)
            if not verdicts:
                click.echo("  No running experiments with mature cohorts to evaluate.")
            for v in verdicts:
                line = (
                    f"  {v['experiment_id']:30s}  "
                    f"verdict={v['verdict']:12s}  "
                    f"rate={v['step_rate']:.1%} vs baseline={v['step_baseline_rate']:.1%}"
                )
                if v.get("step", "overall") != "overall":
                    line += f" (driven by {v['rate_metric']})"
                click.echo(line)

            # 3. Update experiment statuses (if not dry-run).
            # PR-31 / Wave-2-A: persist all 4 terminal verdicts (WON / LOST /
            # REJECTED_NULL / REJECTED_DEFENSIVE) to experiments.tsv (system of
            # record) via apply_verdict; REJECTED_DEFENSIVE additionally opens
            # variant_paused_defensive in the (live) operator_review_queue.
            # apply_verdict now owns the single TSV write for every verdict — the
            # freshly-measured cohort metrics are forwarded via `metrics=v` so the
            # persisted terminal row reflects the measured cohort, not the running
            # row's placeholders. There is NO separate tail mirror (a second
            # append_experiment would duplicate the row — append is not upsert).
            if not dry_run:
                for v in verdicts:
                    eval_verdict_str = v["verdict"]
                    exp_verdict = to_experiment_verdict(eval_verdict_str)
                    if exp_verdict is None:
                        # Non-terminal verdict (e.g. evaluator emits a new state we
                        # don't yet persist). §0 #9: silent skip is correct only
                        # for unrecognized terminal-states; surface a breadcrumb on
                        # stderr so operators can grep for data-quality signals.
                        click.echo(
                            f"  Skipped {v['experiment_id']}: "
                            f"verdict={eval_verdict_str!r} is not in ExperimentVerdict.",
                            err=True,
                        )
                        continue

                    result = apply_verdict(
                        v["experiment_id"], exp_verdict, attio=attio, metrics=v,
                    )
                    msg = (
                        f"  Updated {v['experiment_id']} → "
                        f"{result['status_written']}"
                    )
                    if result["queue_opened"]:
                        msg += " [variant_paused_defensive queued]"
                    click.echo(msg)

    except RunLockHeld as exc:
        click.echo(f"ERROR: {exc}", err=True)
        sys.exit(EXIT_TEMPFAIL)

    click.echo("\n=== Learning Complete ===")


@cli.command("detect-bad-companies")
def detect_bad_companies_cmd():
    """Detect pipeline records linked to companies with linkedin.com domain."""
    from workflows.backfill_companies import detect_bad_company_links

    click.echo("=== Outbound Agent -- Detect Bad Company Links ===\n")

    with _crm_provider() as crm:
        file_path = detect_bad_company_links(crm)

    click.echo("\nReview the CSV, then run:")
    click.echo(f"  python3 cli.py repair-companies --csv {file_path}")


@cli.command("repair-companies")
@click.option("--csv", "detect_csv", required=True, type=click.Path(exists=True), help="CSV from detect-bad-companies")
@click.option("--sales-nav-profile-scraper-id", envvar="PB_SALES_NAV_PROFILE_SCRAPER_ID", required=True, help="PhantomBuster Sales Navigator Profile Scraper agent ID (the legacy Profile Scraper agent was deleted from the PB workspace)")
@click.option("--dry-run", is_flag=True, help="Show what would be repaired without making changes")
def repair_companies_cmd(detect_csv, sales_nav_profile_scraper_id, dry_run):
    """Repair pipeline records with bad company links via PB re-scraping."""
    import csv as csv_mod

    from clients.phantombuster import PhantomBusterClient
    from workflows.backfill_companies import repair_bad_companies

    click.echo("=== Outbound Agent -- Repair Bad Company Links ===\n")

    if dry_run:
        with open(detect_csv, newline="", encoding="utf-8") as f:
            rows = list(csv_mod.DictReader(f))
        click.echo(f"Would repair {len(rows)} records:")
        for row in rows:
            click.echo(f"  {row['name']} — currently: {row.get('current_wrong_company', '?')}")
        from workflows.backfill_companies import REPAIR_MAX_PROFILES_PER_LAUNCH
        if len(rows) > REPAIR_MAX_PROFILES_PER_LAUNCH:
            click.echo(
                f"\nNote: the wet run scrapes at most "
                f"{REPAIR_MAX_PROFILES_PER_LAUNCH} profiles per launch — "
                f"{len(rows) - REPAIR_MAX_PROFILES_PER_LAUNCH} row(s) would be "
                f"deferred to a follow-up detect/repair cycle."
            )
        return

    from workflows.daily_check_helpers import SalesNavConfigError

    try:
        with _attio_client() as attio, PhantomBusterClient() as pb:
            summary = repair_bad_companies(attio, pb, sales_nav_profile_scraper_id, detect_csv)
    except SalesNavConfigError as exc:
        # Missing SN cookie / dead-or-mistyped SN scraper id — operator
        # config problems, not crashes. Surface the named fix, not a
        # traceback.
        raise click.ClickException(str(exc)) from exc

    if summary.get("error"):
        # The scrape produced no CSV — nothing was re-linked. Distinct from
        # the zero-counter success over an already-clean detect CSV.
        click.echo(
            "\n=== Repair FAILED: scrape returned no CSV — nothing was "
            "re-linked. Re-run repair-companies (the detect CSV is still "
            "valid). ===",
            err=True,
        )
        if summary.get("deferred"):
            click.echo(
                f"Deferred: {summary['deferred']} row(s) beyond the per-launch "
                f"scrape cap remain queued behind the failed batch.",
                err=True,
            )
        raise SystemExit(1)

    click.echo("\n=== Repair Complete ===")
    click.echo(f"Linked: {summary['linked']}, Failed: {summary['failed']}, Skipped: {summary['skipped']}")
    if summary.get("deferred"):
        click.echo(
            f"Deferred: {summary['deferred']} row(s) beyond the per-launch scrape "
            f"cap — re-run detect-bad-companies, then repair-companies again."
        )


@cli.command("reconcile-pending-invites")
@click.option("--sales-nav-profile-scraper-id", envvar="PB_SALES_NAV_PROFILE_SCRAPER_ID", required=True, help="PhantomBuster Sales Navigator Profile Scraper agent ID")
@click.option("--dry-run/--wet", "dry_run", default=True, help="Preview PROSPECT candidates without scraping or writing (default). Pass --wet to scrape and apply.")
@click.option("--force", "force_rescrape", is_flag=True, help="Bypass the recheck-cache TTL and re-scrape every PROSPECT candidate")
@click.option("--batch-size", default=25, show_default=True, type=int, help="Max profiles scraped this run (PB cost bound)")
def reconcile_pending_invites_cmd(sales_nav_profile_scraper_id, dry_run, force_rescrape, batch_size):
    """Flip PROSPECT rows that are already pending on LinkedIn to CONNECTION_SENT.

    Phase C backlog-clearer for the re-selection leak. On --wet, re-scrapes
    PROSPECT rows via Sales Nav and flips ONLY those returning
    hasPendingInvitation=true (a fresh, positive already-invited signal — never
    ghost-advances). Defaults to --dry-run (candidate preview, no scrape/write);
    review, then re-run with --wet.
    """
    from clients.attio import AttioClient
    from clients.phantombuster import PhantomBusterClient
    from workflows.daily_check_helpers import SalesNavConfigError
    from workflows.pending_invite_reconciliation import (
        run_pending_invite_reconciliation,
    )
    from workflows.pre_invite_check import ConfigError

    click.echo("=== Sales Agent -- Reconcile Pending Invites ===\n")
    list_id = os.environ.get("ATTIO_LIST_ID", "")
    if not list_id:
        raise click.ClickException("ATTIO_LIST_ID is not set.")

    try:
        with AttioClient() as attio, PhantomBusterClient() as pb:
            summary = run_pending_invite_reconciliation(
                attio=attio,
                pb=pb,
                sales_nav_profile_scraper_id=sales_nav_profile_scraper_id,
                list_id=list_id,
                dry_run=dry_run,
                force_rescrape=force_rescrape,
                batch_size=batch_size,
            )
    except (SalesNavConfigError, ConfigError) as exc:
        # Config failures (e.g. a missing Sales Nav session cookie on the wet
        # scrape path) surface the named fix, not a traceback. No write happened.
        raise click.ClickException(str(exc)) from exc

    click.echo(f"\nSummary: {summary}")
    if dry_run and summary.get("candidates"):
        click.echo(
            f"\n{summary['candidates']} candidate(s) previewed. "
            f"Re-run with --wet to scrape and flip the genuinely-pending rows."
        )


@cli.command("detect-deal-dupes")
@click.option(
    "--out",
    "out_path",
    default="deal_dedup_report.json",
    show_default=True,
    type=click.Path(dir_okay=False),
    help="Where to write the JSON report",
)
def detect_deal_dupes_cmd(out_path: str):
    """Scan Attio deals for duplicates → emit deal_dedup_report.json.

    Read-only. Bucket by associated_company UUID (auto-safe) and by normalized
    name with diverging UUIDs (conflict; needs the operator to review).
    """
    import json as json_mod

    from scripts.attio_dedup import build_deal_report

    click.echo("=== Outbound Agent -- Detect Deal Dupes ===\n")
    with _attio_client() as attio:
        report = build_deal_report(attio)

    with open(out_path, "w") as f:
        json_mod.dump(report, f, indent=2, default=str)

    s = report["summary"]
    click.echo(
        f"\nSummary: {s['deals_groups_auto_safe']} auto-safe + "
        f"{s['deals_groups_conflict']} conflict groups → "
        f"{s['deals_records_to_delete']} records would be deleted (auto-safe only).",
    )
    click.echo(f"\nReport: {out_path}")
    if s["deals_groups_conflict"]:
        click.echo(
            "Review conflict_groups in the report. Move each into "
            "approved_conflict_groups (to merge) or skipped_groups (to leave alone) "
            "before running apply-deal-dedup."
        )
    click.echo("\nNext: python3 cli.py apply-deal-dedup --report " + out_path)


@cli.command("apply-deal-dedup")
@click.option("--report", "report_path", required=True, type=click.Path(exists=True, dir_okay=False), help="Path to deal_dedup_report.json")
@click.option("--pretend", is_flag=True, help="Exercise code paths without calling Attio write APIs")
@click.option("--log", "log_path", default="deal_dedup_apply_log.jsonl", show_default=True, help="Path for the apply log (JSONL)")
def apply_deal_dedup_cmd(report_path: str, pretend: bool, log_path: str):
    """Apply a triaged deal_dedup_report.json. Writes a JSONL audit log."""
    import json as json_mod

    from scripts.attio_dedup import apply_deal_report

    click.echo("=== Outbound Agent -- Apply Deal Dedup ===\n")
    report = json_mod.loads(__import__("pathlib").Path(report_path).read_text())
    totals = apply_deal_report(report, pretend=pretend, log_path=log_path)
    click.echo(
        f"\nApplied: {totals['deals_groups']} deal groups, "
        f"{totals['errors']} errors → log: {log_path}",
    )
    if totals["errors"]:
        raise SystemExit(1)


@cli.command("detect-deal-contact-gaps")
@click.option(
    "--out",
    "out_path",
    default="deal_contact_backfill_report.csv",
    show_default=True,
    type=click.Path(dir_okay=False),
    help="Where to write the CSV report",
)
@click.option(
    "--only-empty",
    is_flag=True,
    help="Restrict to deals where associated_people is currently empty",
)
def detect_deal_contact_gaps_cmd(out_path: str, only_empty: bool):
    """Scan deals for missing contact links → emit deal_contact_backfill_report.csv.

    For each deal, finds candidate people that point at the same company UUID
    but are not yet linked. Scores each candidate via quality_gate and labels
    rows: link / review / skip-disqualified / already-linked.
    """
    from workflows.repair_deal_contacts import detect_deal_contact_gaps

    click.echo("=== Outbound Agent -- Detect Deal Contact Gaps ===\n")
    with _attio_client() as attio:
        summary = detect_deal_contact_gaps(attio, out_path=out_path, only_empty=only_empty)

    click.echo(f"Scanned: {summary['deals_scanned']} deals, {summary['people_indexed']} people")
    click.echo(
        f"Candidates: {summary['candidate_pairs']} (link={summary['link']}, "
        f"review={summary['review']}, skip-disqualified={summary['skip_disqualified']}, "
        f"already-linked={summary['already_linked']})"
    )
    click.echo(f"Deals with no associated_company: {summary['deals_with_no_company']}")
    click.echo(f"\nReport: {summary['report_path']}")
    click.echo(
        "\nReview rows marked 'review' or 'skip-disqualified' in the CSV.\n"
        f"Next: python3 cli.py apply-deal-contact-backfill --report {summary['report_path']}"
    )


@cli.command("apply-deal-contact-backfill")
@click.option("--report", "report_path", required=True, type=click.Path(exists=True, dir_okay=False), help="Path to deal_contact_backfill_report.csv")
@click.option("--pretend", is_flag=True, help="Exercise code paths without calling Attio write APIs")
@click.option("--log", "log_path", default="deal_contact_link_audit.jsonl", show_default=True, help="Path for the audit log (JSONL)")
def apply_deal_contact_backfill_cmd(report_path: str, pretend: bool, log_path: str):
    """Apply rows marked 'link' in the report — PATCH each deal's associated_people."""
    from workflows.repair_deal_contacts import apply_deal_contact_backfill

    click.echo("=== Outbound Agent -- Apply Deal Contact Backfill ===\n")
    with _attio_client() as attio:
        totals = apply_deal_contact_backfill(
            attio, report_path=report_path, pretend=pretend, log_path=log_path,
        )

    click.echo(
        f"\nDeals touched: {totals['deals_touched']}, "
        f"people linked: {totals['people_linked']}, "
        f"skipped (idempotent): {totals['deals_skipped_idempotent']}, "
        f"errors: {totals['errors']} → log: {log_path}",
    )
    if totals["errors"]:
        raise SystemExit(1)


@cli.command(name="weekly-finalize")
@click.option("--batch", required=True, help="Date batch in YYYY-MM-DD format")
@click.option("--dry-run", is_flag=True, help="Print verdicts without writing to Attio")
def weekly_finalize_cmd(batch: str, dry_run: bool):
    """Commit agent-qualified borderlines to Attio after Haiku verdict pass."""
    import json
    from pathlib import Path

    from workflows.industry_classifier import build_anthropic_client
    from workflows.weekly_prospect import _commit_prospect

    anthropic_client = build_anthropic_client()

    click.echo(f"=== Outbound Agent -- Weekly Finalize ({batch}) ===\n")

    borderline_path = Path("exports") / f"weekly_borderline_{batch}.jsonl"
    verdicts_path = Path("exports") / f"weekly_verdicts_{batch}.jsonl"

    if not borderline_path.exists():
        click.echo(f"Error: borderline file not found: {borderline_path}")
        raise SystemExit(1)
    if not verdicts_path.exists():
        click.echo(f"Error: verdicts file not found: {verdicts_path}")
        raise SystemExit(1)

    with borderline_path.open() as f:
        staged = [json.loads(line) for line in f if line.strip()]

    with verdicts_path.open() as f:
        verdicts_raw = [json.loads(line) for line in f if line.strip()]

    verdicts_by_url = {v["linkedin_url"]: v for v in verdicts_raw}

    list_id = os.environ.get("ATTIO_LIST_ID", "")
    today = batch

    passed = 0
    failed = 0
    missing = 0

    with _crm_provider() as crm:
        # PR-207: pre-fetch the pipeline list once so the truth-based
        # already-listed guard in _commit_prospect resolves in-memory (no
        # per-prospect CRM scan) and a borderline already in the pipeline is
        # never re-stamped back to a fresh stage. This CLI path passed no
        # snapshot, so the re-stamp skip was a silent no-op (re-stamp incident).
        existing_entries = (
            crm.query_list_entries(list_id=list_id) if not dry_run else None
        )
        if existing_entries is not None:
            # Surface the snapshot size — a suspiciously low count is the tell
            # that the guard is running against a truncated/empty list.
            click.echo(
                f"  Pre-fetched {len(existing_entries)} pipeline entries for the "
                "re-stamp guard."
            )
        for entry in staged:
            url = entry["linkedin_url"]
            verdict = verdicts_by_url.get(url)
            if verdict is None:
                missing += 1
                click.echo(f"  [MISSING VERDICT] {entry['prospect_data'].get('name', url)}")
                continue

            name = entry["prospect_data"].get("name", url)
            rationale = verdict.get("rationale", "")

            if not verdict.get("pass"):
                failed += 1
                click.echo(f"      → [AGENT FAIL] {name} — {rationale}")
                continue

            if dry_run:
                click.echo(f"      → [DRY RUN] Would commit: {name} (icp_lane={verdict.get('icp_lane')})")
                passed += 1
                continue

            # PR-28 fold (salesman-weekly QA convergence): propagate
            # `icp_lane` from the Haiku verdict so `icp_lane_persisted`
            # writes through `_build_prospect_entry_attrs` for the
            # borderline-LLM commit path. Pre-fold, the field was
            # echoed to the terminal at line above and then dropped.
            # Full wiring to `weekly_finalize_idempotent` (14-day
            # idempotency + ICP-2 geo gate on the CLI path) is a
            # tracked follow-up — this fold closes the more urgent
            # `icp_lane_persisted` write gap.
            score_result_fields = {
                "persona": entry["persona"],
                "language": entry["language"],
                "score": entry["score"],
                "score_breakdown": entry.get("score_breakdown"),
                "scoring_lane": entry.get("scoring_lane"),
                "verdict_path": "borderline_pass",
                "llm_rationale": rationale,
                "icp_lane": verdict.get("icp_lane"),
            }
            ok = _commit_prospect(
                crm,
                entry["prospect_data"],
                entry["raw_csv_row"],
                score_result_fields,
                list_id,
                today,
                anthropic_client=anthropic_client,
                existing_entries=existing_entries,
            )
            if ok:
                passed += 1
                click.echo(f"      → [AGENT PASS] {name} committed (icp_lane={verdict.get('icp_lane')})")
            else:
                click.echo(f"      → [WRITE ERROR] {name} — failed to commit to Attio")

    click.echo("\n--- Weekly Finalize Summary ---")
    click.echo(f"Staged:    {len(staged)}")
    click.echo(f"Verdicts:  {len(verdicts_raw)}")
    click.echo(f"Passed:    {passed}  (committed to Attio)")
    click.echo(f"Failed:    {failed}  (rejected by agent)")
    click.echo(f"Missing:   {missing}  (staged but no verdict — skipped)")


@cli.command(name="weekly-brain")
@click.option("--experiment-id", required=True, help="Target experiment id, e.g. exp-003")
@click.option("--dry-run", is_flag=True, help="Print the proposal path without writing the file")
@click.option(
    "--open-pr",
    is_flag=True,
    help="PR-32: open a draft PR + variant_proposal_pending queue row after writing the proposal",
)
def weekly_brain_cmd(experiment_id: str, dry_run: bool, open_pr: bool):
    """Run the weekly brain: measure cohorts, pre-screen variants, write proposal."""
    from workflows.weekly_brain import load_dm1_variants, run_weekly_brain

    click.echo(f"=== Outbound Agent Weekly Brain — {experiment_id} ===\n")
    variants_by_persona = load_dm1_variants()
    click.echo(
        f"Loaded {sum(len(v) for v in variants_by_persona.values())} DM1 variants "
        f"across {len(variants_by_persona)} personas."
    )

    with _attio_client() as attio:
        out_path = run_weekly_brain(
            attio,
            experiment_id=experiment_id,
            variants_by_persona=variants_by_persona,
            dry_run=dry_run,
            open_pr=open_pr,
        )

    action = "would write" if dry_run else "wrote"
    click.echo(f"\n✓ {action} proposal: {out_path}")


@cli.command(name="sales-approve")
@click.argument("experiment_id", required=False)
@click.option("--approve", "decision", flag_value="approve", help="Approve + merge the proposal PR")
@click.option("--reject", "decision", flag_value="reject", help="Reject the proposal (requires --rationale)")
@click.option("--rationale", default=None, help="Required for --reject; recorded on the queue row")
def sales_approve_cmd(experiment_id: str | None, decision: str | None, rationale: str | None):
    """PR-32: list / approve / reject pending variant_proposal_pending queue rows.

    Examples:

        sales-approve                        # list pending proposals
        sales-approve exp-003 --approve      # merge PR + close queue row
        sales-approve exp-003 --reject --rationale="reactance still too high"
    """
    from workflows.sales_approve import (
        ProposalNotFoundError,
        approve_proposal,
        list_pending_proposals,
        reject_proposal,
    )

    # The pending-row reads go through the vendor-neutral provider
    # (``bundle.provider``); the approve/reject writes still use the raw
    # client (``bundle.attio``) via ``AttioWriter``. Both come from the same
    # factory/lifecycle, so opening both context managers is safe.
    with _crm_provider() as crm, _attio_client() as attio:
        if experiment_id is None:
            pending = list_pending_proposals(crm)
            if not pending:
                click.echo("No pending variant_proposal_pending rows.")
                return
            click.echo(f"Pending proposals ({len(pending)}):")
            for prop in pending:
                click.echo(
                    f"  {prop['experiment_id']:25s}  PR: {prop['pr_url']}  "
                    f"branch: {prop['branch_name']}"
                )
            return

        if decision is None:
            click.echo(
                "Pass --approve or --reject (--reject also requires --rationale).",
                err=True,
            )
            raise click.exceptions.Exit(2)

        try:
            if decision == "approve":
                result = approve_proposal(experiment_id, crm=crm, attio=attio)
                click.echo(
                    f"✓ Approved + merged {experiment_id} ({result['pr_url']})"
                )
            else:
                if not rationale:
                    click.echo("--reject requires --rationale", err=True)
                    raise click.exceptions.Exit(2)
                result = reject_proposal(
                    experiment_id, crm=crm, attio=attio, rationale=rationale,
                )
                click.echo(
                    f"✓ Rejected {experiment_id} "
                    f"(PR closed: {result['pr_closed']})"
                )
        except ProposalNotFoundError as exc:
            click.echo(f"error: {exc}", err=True)
            raise click.exceptions.Exit(1) from exc


@cli.command(name="industry-approve")
@click.argument("company_name", required=False)
@click.option("--approve", "decision", flag_value="approve", help="Confirm the operator's industry choice")
@click.option("--reject", "decision", flag_value="reject", help="Reject (requires --rationale; Company record stays low_confidence)")
@click.option("--vertical", default=None, help="Required for --approve; one of the 11 INDUSTRY_LABELS")
@click.option("--rationale", default=None, help="Required for --reject; recorded on the queue row")
@click.option("--record-id", "record_id", default=None, help="Override Company record_id when name resolution is ambiguous")
def industry_approve_cmd(
    company_name: str | None,
    decision: str | None,
    vertical: str | None,
    rationale: str | None,
    record_id: str | None,
):
    """FU-2: list / approve / reject pending industry_low_confidence queue rows.

    Examples:

        industry-approve                                       # list pending rows
        industry-approve "Sigma Alimentos" --approve --vertical "Food & Beverage"
        industry-approve "Sigma Alimentos" --reject --rationale="not a manufacturer"
    """
    from workflows.industry_approve import (
        AmbiguousCompanyError,
        CompanyNotFoundError,
        IndustryRowNotFoundError,
        InvalidVerticalError,
        approve_industry_classification,
        list_pending_industry_low_confidence,
        reject_industry_classification,
    )

    # Pending-row reads go through the vendor-neutral provider
    # (``bundle.provider``); the company-resolution search + approve/reject
    # writes still use the raw client (``bundle.attio``). Both share the
    # same factory/lifecycle.
    with _crm_provider() as crm, _attio_client() as attio:
        if company_name is None:
            pending = list_pending_industry_low_confidence(crm)
            if not pending:
                click.echo("No pending industry_low_confidence rows.")
                return
            click.echo(f"Pending industry classifications ({len(pending)}):")
            for row in pending:
                click.echo(
                    f"  {row['company_name']:40s}  suggested: {row['suggested_vertical']:20s}  "
                    f"conf: {row['confidence']:.2f}"
                )
            return

        if decision is None:
            click.echo(
                "Pass --approve --vertical <label> or --reject --rationale <text>.",
                err=True,
            )
            raise click.exceptions.Exit(2)

        try:
            if decision == "approve":
                if not vertical:
                    click.echo("--approve requires --vertical", err=True)
                    raise click.exceptions.Exit(2)
                result = approve_industry_classification(
                    company_name,
                    vertical=vertical,
                    crm=crm,
                    attio=attio,
                    record_id=record_id,
                )
                click.echo(
                    f"✓ Confirmed {company_name!r} as {vertical!r} "
                    f"(company={result['company_record_id']})"
                )
            else:
                if not rationale:
                    click.echo("--reject requires --rationale", err=True)
                    raise click.exceptions.Exit(2)
                reject_industry_classification(
                    company_name, rationale=rationale, crm=crm, attio=attio,
                )
                click.echo(f"✓ Rejected industry suggestion for {company_name!r}")
        except (
            IndustryRowNotFoundError,
            CompanyNotFoundError,
            AmbiguousCompanyError,
            InvalidVerticalError,
        ) as exc:
            click.echo(f"error: {exc}", err=True)
            raise click.exceptions.Exit(1) from exc


@cli.command(name="weekly-diagnostic")
@click.option(
    "--since",
    "since_str",
    default=None,
    help="ISO date (YYYY-MM-DD). Currently informational; the diagnostic pulls all entries.",
)
@click.option(
    "--out-dir",
    "out_dir_str",
    default="docs/experiments",
    help="Where to write the markdown report.",
)
@click.option("--no-llm", is_flag=True, help="Skip the Sonnet critique step (diagnostic only)")
def weekly_diagnostic_cmd(since_str: str | None, out_dir_str: str, no_llm: bool):
    """Phase 1 auto-research: stratified funnel diagnostic + LLM critique.

    Writes a markdown report to docs/experiments/<date>-diagnostic.md with
    the verdict (ICP / message / both / insufficient), top/bottom segments,
    disagreement examples, and a single LLM-proposed change with hypothesis.
    """
    from datetime import date as _date
    from pathlib import Path

    from workflows.industry_classifier import build_anthropic_client
    from workflows.qualifier_critique import propose_change
    from workflows.qualifier_diagnostic import diagnose_funnel

    since_date = _date.fromisoformat(since_str) if since_str else None
    today = _date.today()
    out_dir = Path(out_dir_str)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{today.isoformat()}-diagnostic.md"

    click.echo(f"=== Phase 1 auto-research diagnostic — {today.isoformat()} ===\n")

    with _attio_client() as attio:
        click.echo("Pulling list entries from Attio...")
        report = diagnose_funnel(attio, since_date=since_date)

    click.echo(
        f"\nVerdict: {report.verdict}"
        f"\nOverall: {report.n_total_dmed} DM'd · "
        f"reply rate {report.overall_response_rate:.1%} · "
        f"defensive rate {report.overall_defensive_rate:.1%} · "
        f"connect accept rate {report.overall_accept_rate:.1%}\n"
    )

    if no_llm:
        click.echo("--no-llm flag set — skipping LLM critique.\n")
        proposal_md = "_LLM critique skipped (--no-llm)._\n"
    else:
        click.echo("Running LLM critique (Claude Sonnet)...")
        anthropic_client = build_anthropic_client()
        proposal = propose_change(report, anthropic_client)
        click.echo(
            f"  Proposal: {proposal.action} (predicted lift {proposal.predicted_lift_pp}pp)\n"
        )
        proposal_md = proposal.to_markdown()

    body = report.to_markdown() + "\n" + proposal_md
    out_path.write_text(body, encoding="utf-8")
    click.echo(f"\n✓ Wrote {out_path}")
    click.echo(
        "\nNext step: review the markdown, and if the proposal action is "
        "weight_change/keyword_change/threshold_change, open a PR by hand "
        "with the specific_change applied to workflows/quality_gate.py."
    )


@cli.command("replay-run")
@click.argument("run_id")
def replay_run(run_id):
    """Print the resume manifest for a prior run's audit log.

    Operator-facing inspect-only command. Does NOT mutate state — the
    output tells you what was already done so you can plan a follow-up
    run that skips completed record_ids.
    """
    import json

    from workflows.audit import resume_from_audit

    try:
        manifest = resume_from_audit(run_id)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    # Record-id sets aren't JSON-serializable — flatten to sorted lists.
    serializable = dict(manifest)
    for key in ("record_ids_seen", "record_ids_failed"):
        serializable[key] = sorted(manifest[key])
    click.echo(json.dumps(serializable, indent=2, default=str))


@cli.command("audit-tail")
@click.option("--follow", "-f", is_flag=True, help="Stream new lines as they're appended (tail -F semantics)")
@click.option("--run-id", default=None, help="Specific run to tail (default: newest audit file)")
def audit_tail(follow, run_id):
    """Tail the most recent audit file (or a specific run-id)."""
    import time

    from workflows.audit import AUDIT_DIR, _find_audit_path, list_runs

    if run_id:
        try:
            path = _find_audit_path(run_id)
        except FileNotFoundError as exc:
            raise click.ClickException(str(exc)) from exc
    else:
        runs = list_runs(since_days=30)
        if not runs:
            raise click.ClickException(f"No audit files in {AUDIT_DIR}")
        path = runs[0]

    click.echo(f"# tailing {path}")
    with open(path) as f:
        # Stream existing content first.
        for line in f:
            click.echo(line.rstrip())
        if not follow:
            return
        # tail -F: re-read on append, sleeping briefly between polls.
        while True:
            line = f.readline()
            if line:
                click.echo(line.rstrip())
            else:
                time.sleep(0.5)


@cli.command("audit-stats")
@click.option("--since-days", default=7, show_default=True, help="Aggregate over the last N days")
def audit_stats_cmd(since_days):
    """Print aggregated stats over recent audit logs."""
    import json

    from workflows.audit import audit_stats

    stats = audit_stats(since_days=since_days)
    click.echo(json.dumps(stats, indent=2))


@cli.command("audit-list")
@click.option("--since-days", default=7, show_default=True, help="List runs from the last N days")
def audit_list(since_days):
    """List recent audit runs with status and event count.

    Operator's first-stop after a cron failure alert: shows every run_id
    in the window, newest first, so `replay-run <id>` is a single
    additional command away.
    """
    import json

    from workflows.audit import list_runs

    runs = list_runs(since_days=since_days)
    if not runs:
        click.echo(f"# No audit files in the last {since_days} days.")
        return
    for path in runs:
        # path shape: run-{YYYY-MM-DD}-{run_id}.jsonl
        stem = path.stem  # strips .jsonl
        try:
            _, yyyy, mm, dd, run_id = stem.split("-", 4)
            run_date = f"{yyyy}-{mm}-{dd}"
        except ValueError:
            run_date = "?"
            run_id = stem
        # Read last line for status + scan for total event count.
        # Linear scan is O(file) but the operator runs this rarely.
        last_event_name = "?"
        event_count = 0
        crash_msg = ""
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                event_count += 1
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                last_event_name = ev.get("event", "?")
                if last_event_name == "run_crashed":
                    crash_msg = f" — {ev.get('exc_type', '?')}: {ev.get('exc_msg', '')[:60]}"
        status = {
            "run_completed": "completed",
            "run_crashed": "crashed",
        }.get(last_event_name, "in_progress")
        click.echo(f"{run_date}  {run_id}  {status:13s}  events={event_count}{crash_msg}")


# =====================================================================
# Slash-command-driven workflow registry. Each subcommand below is the
# Python entry point invoked by a SKILL.md in `skills/`. Operator
# triggers in Claude Code via the slash command; the skill body
# resolves to the matching `sales <name>` call here.
# =====================================================================


@cli.command("data-quality-report")
@click.option("--period-days", default=7, show_default=True,
              help="Lookback window in days.")
@click.option("--no-write", is_flag=True,
              help="Compute + print the report without writing the Attio row.")
def data_quality_report_cmd(period_days: int, no_write: bool) -> None:
    """Compute the 8-metric Data Quality Report and emit P0/P1 alarms.

    Exit code 0 = clean, 1 = P1 alarm, 70 = P0 alarm (downstream
    consumer halts DM sends). See scripts/data_quality_report.py.
    """
    from scripts.data_quality_report import main as dqr_main

    argv: list[str] = ["--period-days", str(period_days)]
    if no_write:
        argv.append("--no-write")
    raise SystemExit(dqr_main(argv))


@cli.command("threshold-calibration")
@click.option("--dry-run", is_flag=True,
              help="Compute the ROC sweep without opening a recommendation queue row.")
def threshold_calibration_cmd(dry_run: bool) -> None:
    """Run the score-threshold ROC calibration."""
    try:
        from scripts.threshold_calibration import main as tc_main
    except ImportError as exc:
        # Avoid asserting WHY the import failed in operator-visible
        # text — the script may legitimately not be installed yet,
        # OR a real bug could break the import. Either way the
        # operator's recovery is the same: ensure the file is present.
        click.echo(
            f"threshold-calibration script unavailable: {exc}",
            err=True,
        )
        raise SystemExit(2) from exc
    argv: list[str] = []
    if dry_run:
        argv.append("--dry-run")
    raise SystemExit(tc_main(argv))


@cli.command("audit-reminder-sweep")
@click.option("--stale-days", default=7, show_default=True,
              help="Open queue rows older than this many days are reported as stale.")
def audit_reminder_sweep_cmd(stale_days: int) -> None:
    """Scan Operator Review Queue for rows that have been `open` for
    longer than `stale-days` and print a digest the operator can scan
    in one screen. Read-only — no Attio mutations."""
    from datetime import UTC, datetime, timedelta

    from clients.attio import AttioClient

    cutoff = datetime.now(UTC) - timedelta(days=stale_days)
    cutoff_iso = cutoff.isoformat()

    with _attio_client() as attio:
        body = {
            "filter": {
                "status": {"$eq": "open"},
                "opened_at": {"$lte": cutoff_iso},
            },
            "limit": 200,
        }
        data = attio._request(
            "POST",
            "/objects/operator_review_queue/records/query",
            json=body,
        )

    # Validate shape — `data.get("data", [])` would silently treat a
    # missing key as "no rows" and the operator would see "Clean."
    # without ever having queried.
    if not isinstance(data, dict) or "data" not in data:
        click.echo(
            f"error: Attio response shape unexpected: {data!r}", err=True,
        )
        raise SystemExit(1)
    rows = data["data"] or []

    if not rows:
        click.echo(f"No queue rows open for more than {stale_days} day(s). Clean.")
        return

    click.echo(f"=== Stale Operator Review Queue rows (>{stale_days}d) ===\n")
    click.echo(f"{len(rows)} row(s):\n")
    for row in rows:
        values = row.get("values", {}) or {}
        row_type = AttioClient.object_record_first_value(values, "type") or "?"
        idempotency_key = AttioClient.object_record_first_value(
            values, "idempotency_key",
        ) or "?"
        opened_at = AttioClient.object_record_first_value(values, "opened_at") or "?"
        click.echo(f"  type={row_type}  opened={opened_at}  key={idempotency_key}")


@cli.command("health-check")
def health_check_cmd() -> None:
    """Verify Attio + PB connectivity without mutating state.

    Operator runs this ad-hoc when something feels off. Exit 0 on
    healthy, 1 on any reachability failure."""

    failures: list[str] = []
    try:
        with _attio_client() as attio:
            # whoami / health-equivalent: query the operator_review_queue
            # with limit=1. Cheap and proves API + auth work.
            attio._request(
                "POST",
                "/objects/operator_review_queue/records/query",
                json={"limit": 1},
            )
        click.echo("Attio: OK")
    except Exception as exc:
        failures.append(f"Attio: FAIL ({type(exc).__name__}: {exc})")

    try:
        from clients.phantombuster import PhantomBusterClient

        with PhantomBusterClient() as _pb:
            pass  # construction alone verifies env + connection setup
        click.echo("PhantomBuster: OK")
    except Exception as exc:
        failures.append(f"PhantomBuster: FAIL ({type(exc).__name__}: {exc})")

    # Shipped-placeholder content check. Not fatal to connectivity, but a live
    # send would be BLOCKED until these are replaced — surface it loudly here so
    # the operator fixes it before their first real run.
    from workflows.content_guard import content_has_placeholders

    placeholder_files = content_has_placeholders()
    if placeholder_files:
        failures.append(
            "Content: FAIL — shipped placeholder content still in place "
            f"({', '.join(placeholder_files)}). Replace it or set "
            "OUTBOUND_CONTENT_DIR before any live send. See GETTING_STARTED.md."
        )
    else:
        click.echo("Content: OK")

    if failures:
        for f in failures:
            click.echo(f, err=True)
        raise SystemExit(1)
    click.echo("\nAll health checks passed.")


@cli.command("sales-finalize-borderline")
@click.option("--batch", required=True, help="Batch date YYYY-MM-DD.")
@click.option("--dry-run", is_flag=True,
              help="Compute the finalize but skip the Attio writes.")
@click.pass_context
def sales_finalize_borderline_cmd(ctx, batch: str, dry_run: bool) -> None:
    """Finalize the morning's borderline batch with idempotency-key
    collision protection. See workflows/auto_finalize.py for the
    idempotency contract.
    """
    from datetime import date as _date

    from workflows.auto_finalize import (
        FinalizeRunFailed,
        auto_finalize_borderline_batch,
    )

    try:
        batch_date = _date.fromisoformat(batch)
    except ValueError as exc:
        click.echo(f"error: --batch must be YYYY-MM-DD; got {batch!r}", err=True)
        raise SystemExit(2) from exc

    # Invoke the sibling weekly-finalize command directly through
    # Click rather than via subprocess. Direct invocation preserves
    # tracebacks, exit codes, and the Python process — subprocess
    # would truncate stderr at 500 chars and swallow any in-process
    # exception state.
    def _finalize_fn(batch_iso: str, decision_run_id: str) -> dict:
        try:
            ctx.invoke(
                weekly_finalize_cmd,
                batch=batch_iso,
                dry_run=dry_run,
            )
            exit_code = 0
        except SystemExit as exc:
            exit_code = int(exc.code or 0)
        return {
            "exit_code": exit_code,
            "decision_run_id": decision_run_id,
        }

    try:
        with _attio_client() as attio:
            out = auto_finalize_borderline_batch(
                attio,
                batch_date=batch_date,
                finalize_fn=_finalize_fn,
            )
    except FinalizeRunFailed as exc:
        # Surface the inner failure exit code to a calling skill so
        # the operator sees a non-zero exit, not a misleading 0.
        click.echo(
            f"sales-finalize-borderline FAILED: {exc.payload}", err=True,
        )
        raise SystemExit(exc.payload.get("exit_code") or 1) from exc

    click.echo(
        f"action={out['action']}  decision_run_id={out['decision_run_id']}  "
        f"key={out['idempotency_key']}"
    )
    if out["finalize_result"] is not None:
        click.echo(f"finalize_result: {out['finalize_result']}")


if __name__ == "__main__":
    cli()
