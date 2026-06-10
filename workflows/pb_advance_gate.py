"""Advance-gate dry-skip helpers.

When `should_advance_batch(launch, outcome)` returns False, callers
MUST take the dry-skip path:

1. Emit a `pb_silent_no_op` Operator Review Queue row via
   `workflows.escalation.escalate` (idempotent on container_id).
2. Log `next_day_drift_key` to the per-run audit log so the
   next-day drift detector can correlate "yesterday tried to send,
   today nothing arrived."
3. Return WITHOUT mutating prospect state — no `last_contact_date`
   write, no `dmN_sent_at` write, no `stage` flip.

`emit_pb_inmail_dead_end` is the per-URL analog used inside the
gate-pass branch when individual rows are explicitly marked skipped
by PB (InMail-required, Already-1st-degree, Invite-limit-reached,
etc.). Same dry-skip semantics, idempotent on (URL, step).

Callers compose:

    if should_advance_batch(launch, outcome):
        # ... per-row Attio writes keyed off outcome.sent_urls ...
        # ... + emit_pb_inmail_dead_end for outcome.skipped_urls ...
    else:
        emit_pb_silent_no_op(launch, outcome, attio=attio, audit_logger=log)
        return  # no state mutation
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from workflows.escalation import escalate

if TYPE_CHECKING:
    from clients.attio import AttioClient
    from clients.pb_envelope import PBLaunch, SendOutcome
    from workflows.audit import AuditLogger


def emit_pb_silent_no_op(
    launch: PBLaunch,
    outcome: SendOutcome,
    *,
    attio: AttioClient | None = None,
    audit_logger: AuditLogger | None = None,
    experiment_id: str | None = None,
) -> None:
    """Emit the queue row + audit log entry for a failed advance gate.

    `attio` is forwarded to `escalate`. `audit_logger` is optional —
    when provided, this function appends a `pb_silent_no_op` event
    carrying `next_day_drift_key` so the next-day drift detector can
    join on it. `experiment_id` attributes the no-op to a specific
    experiment cohort without a secondary Attio lookup.

    Idempotency: `escalate(type="pb_silent_no_op",
    idempotency_key=container_id)` means re-running the same daily
    batch returns the existing queue row instead of opening a
    duplicate. Operators see one row per stuck container, not N.

    `skipped_urls` is included in the payload as a list (Attio's
    JSON serializer doesn't accept frozensets) so the drift detector
    knows exactly which prospects were affected.
    """
    skipped_urls_list = sorted(outcome.skipped_urls)
    escalate(
        type="pb_silent_no_op",
        idempotency_key=launch.container_id,
        payload={
            "container_id": launch.container_id,
            "agent_id": launch.agent_id,
            "launched_at": launch.launched_at.isoformat(),
            "arguments_sha256": launch.arguments_sha256,
            "requested_count": outcome.requested_count,
            "sent_count": outcome.sent_count,
            "csv_status": outcome.csv_status,
            "drift_skipped_reason": outcome.drift_skipped_reason,
            "next_day_drift_key": outcome.next_day_drift_key,
            "experiment_id": experiment_id,
            "skipped_urls": skipped_urls_list,
        },
        attio=attio,
    )

    if audit_logger is not None:
        audit_logger.event(
            "pb_silent_no_op",
            container_id=launch.container_id,
            agent_id=launch.agent_id,
            requested_count=outcome.requested_count,
            sent_count=outcome.sent_count,
            csv_status=outcome.csv_status,
            drift_skipped_reason=outcome.drift_skipped_reason,
            next_day_drift_key=outcome.next_day_drift_key,
            experiment_id=experiment_id,
            skipped_urls=skipped_urls_list,
        )


def emit_pb_inmail_dead_end(
    launch: PBLaunch,
    linkedin_url: str,
    dm_step: str,
    pb_status: str,
    *,
    attio: AttioClient | None = None,
    audit_logger: AuditLogger | None = None,
    experiment_id: str | None = None,
) -> None:
    """Emit a per-URL `pb_inmail_dead_end` queue row.

    Called by `daily_check.run_dm_sequencing` and
    `daily_check.run_connection_requests` when PB's CSV explicitly
    marks a prospect's URL as skipped (InMail-required, "Can't send",
    Already-1st-degree, Invite-limit-reached, etc.). `dm_step` is NOT
    bumped for these rows — an undelivered DM must not inflate
    response-rate denominators. Wave-2-A: the caller now ALSO parks the
    prospect at UNREACHABLE (stage-only), so it leaves the DM/invite
    queue instead of looping; this row is the operator's record, no
    longer the only thing preventing an infinite retry.

    The escalation gives the operator a persistent record per
    (URL, step) so they can rescue the prospect (move it forward off
    UNREACHABLE) if the LinkedIn constraint later clears. Idempotency on
    `f"{linkedin_url}|{dm_step}"` means repeat retries collapse to one row.
    """
    escalate(
        type="pb_inmail_dead_end",
        idempotency_key=f"{linkedin_url}|{dm_step}",
        payload={
            "linkedin_url": linkedin_url,
            "dm_step": dm_step,
            "container_id": launch.container_id,
            "pb_status": pb_status,
            "experiment_id": experiment_id,
        },
        attio=attio,
    )

    if audit_logger is not None:
        audit_logger.event(
            "pb_inmail_dead_end",
            linkedin_url=linkedin_url,
            dm_step=dm_step,
            container_id=launch.container_id,
            pb_status=pb_status,
            experiment_id=experiment_id,
        )
