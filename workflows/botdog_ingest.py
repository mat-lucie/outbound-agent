"""Botdog event-ingestion workflow — the OPTIONAL poll-based drain.

Poll-based (NO webhooks) counterpart to the PhantomBuster detection
phases. PhantomBuster owns sending in this engine, so nothing stamps
``send_channel=botdog`` on its own: this workflow only ever touches rows
an operator stamped deliberately, and it is invoked only when the Botdog
event drain is switched on. For `send_channel == botdog` prospects it REPLACES the scrape-
based Phase 0 (accepts) and Phase 0.5 (replies) with Botdog lead-event
history, and replaces `INVITE_OPTIMISTIC_ADVANCE` with event-confirmed
stage/dm_step advances.

Data flow per run
-----------------
1. Read the last-poll cursor from ``~/.outbound-agent/botdog_poll.json``
   (mirrors ``workflows/safety_limits.py`` local-state conventions).
2. Poll ``BotdogSender.fetch_events(since=cursor - POLL_OVERLAP)``
   (sender injected; defaults to ``daily_check._build_botdog_sender``
   — the same lazy constructor the send path uses, so pure-PB runs
   never touch it).
3. Group events by normalized LinkedIn URL, resolve each to an Attio
   ``linkedin_outreach`` list entry, and apply event-confirmed advances
   through the SAME authorized-writer path the PB pipeline uses
   (``daily_check._attio_advance_with_escalation`` → ``AttioWriter``).
4. Advance the cursor to the poll start time (skipped on dry-run),
   held back by the un-applied-event watermark and bounded by the
   hold cap — see "Event-loss defense in depth" below.
5. Scan the entries index for botdog rows that have gone quiet
   (report-only watchdog — the Phase 0 stale-escalation net does not
   cover botdog-channel rows).

Event-loss defense in depth
---------------------------
Three independent mechanisms, each covering the gap the previous one
leaves:

- OVERLAP (short gaps, hours): every poll asks for events since
  ``cursor - BOTDOG_POLL_OVERLAP`` (48h), not since the cursor. Clock
  skew between our machine and Botdog, an event written with a
  slightly-earlier ``occurredAt`` than its visibility time, or a
  crash between apply and cursor-write would otherwise drop events
  that fall just behind the cursor. Application is idempotent, so
  re-polling costs one API call and some no-op counters.
- WATERMARK (medium gaps, days): when a lead's advance FAILS, or its
  events are actionable but SCOPE-SKIPPED (pb-channel or unmatched —
  e.g. an Attio row whose ``send_channel`` stamp has not landed yet,
  or a person record still being created), the cursor is held at the
  earliest such event so the next run sees it again.
- UNREAD LEADS (the events we never saw at all): a lead the transport
  could not READ — dropped by its per-poll detail-fetch budget, or its
  detail fetch raised — emits NO events, so the watermark above has
  nothing to hold with. ``fetch_events`` reports those as
  ``LeadEventBatch.leads_unread``; a non-zero count pins the cursor to
  its PREVIOUS value (or skips the write entirely on a first,
  unbounded poll), so the next run re-reads them instead of aging
  their real events past the poll floor. Surfaced as ``leads_unread``
  in the run report — never a silent ``failures=0``.
- HOLD CAP (bounds the tail): a lead that will NEVER resolve — a
  Botdog lead manually added outside our pipeline, so no Attio row
  will ever match it — would pin the watermark forever, and every
  future run would re-poll from that frozen point. The effective
  cursor therefore never lags more than
  ``BOTDOG_MAX_CURSOR_HOLD_DAYS`` (7) behind ``now``. When the cap
  binds, the run report and stderr name how many events are being
  released BEYOND RECOVERY — they will not be polled again.

Scope guard (CRITICAL)
---------------------------
An advance is applied ONLY when the resolved entry's ``send_channel``
is ``botdog``. A ``pb`` (drain-pool) or channel-less entry, or an
unmatched URL, is skipped and counted — never flipped. Flipping a
PB-drain prospect from a Botdog event would double-write against
Phase 0 / 0.5, which keep serving the drain pool. See the module-level
note in ``detect_accepted_connections`` / ``detect_responses`` for the
reverse guard (those phases skip botdog-channel rows where natural).

Idempotency & overlap tolerance
-------------------------------
Every advance reconciles the entry toward an ABSOLUTE target and is
guarded so re-applying an event whose target state already matches is a
no-op:

- invite-sent / accept / reply flips are applied only when the entry's
  current stage rank is *below* the target rank (forward-only; the
  AttioWriter monotonicity gate is the backstop).
- message-sent advances are computed from the entry's current
  ``dm_step`` plus the count of message-sent events whose timestamp is
  strictly newer than the already-recorded step's ``dm{N}_sent_at`` —
  so a re-polled (already-applied) send contributes zero, while a run
  of missed runs catches up multiple steps at once.

dm_step inference (documented limitation)
-----------------------------------------
Botdog's ``LEAD_MESSAGE_SENT`` event does not name which DM it was — it
reports only "a message was sent". The step is therefore inferred from
the Attio entry's current ``dm_step`` (the event confirms the NEXT
step): ``ACCEPTED``/``dm_step 0`` → DM1, ``dm_step 1`` → DM2,
``dm_step 2`` → DM3. A lead already at DM3 has no DM4 — extra
message-sent events there are counted (``cadence_complete_noop``), not
applied. Message-sent events WITHOUT a parseable timestamp cannot be
ordered against recorded sends; they advance a lead only from the
un-DM'd baseline (``dm_step 0``, using the poll date) and are otherwise
counted (``undated_message_sent_skipped``) rather than risk a runaway
advance — surfaced loudly in the run report.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import click

from clients.attio import AttioClient, _vanity_url_slug
from clients.pb_envelope import _normalize_url_for_match
from models.business_calendar import add_business_days
from models.campaign import DM_STEP_NUMBER, MessageStep
from models.pipeline import PipelineStage, dm_step_int, stage_rank
from workflows.daily_check_helpers import (
    SEND_CHANNEL_BOTDOG,
    _resolve_send_channel,
)
from workflows.dm_sequencer import NEXT_STAGE
from workflows.pre_invite_check import _IMMUTABLE_FROZEN_AT_VALUES

if TYPE_CHECKING:
    from collections.abc import Callable

    from clients.sender import LeadEvent, Sender
    from workflows.audit import AuditLogger

# ── canonical Botdog webhook / lead-event vocabulary ──────────────────
# The lead-event history uses the same event names as the webhooks.
# Matched case-insensitively (map defensively); unknown types are
# ignored + counted.
EVENT_INVITATION_SENT = "LEAD_INVITATION_SENT"
EVENT_INVITATION_ACCEPTED = "LEAD_INVITATION_ACCEPTED"
EVENT_MESSAGE_SENT = "LEAD_MESSAGE_SENT"
EVENT_MESSAGE_REPLIED = "LEAD_MESSAGE_REPLIED"
EVENT_NONCAMPAIGN_INBOX = "NON_CAMPAIGN_LEAD_INBOX_MESSAGE_RECEIVED"
# Derived from the lead detail's `withdrawnAt` (see
# `clients.sender.BOTDOG_DETAIL_TIMESTAMP_EVENTS`): the invitation was
# pulled back — by Botdog's own withdrawal policy, by LinkedIn, or by
# hand — so it will never be accepted.
#
# KNOWN BUT NON-ADVANCING, DELIBERATELY. This pass COUNTS withdrawals
# and names them; it makes NO Attio state change. Parking a withdrawn
# prospect (Unreachable, or a re-invite lane) is a real pipeline
# decision with prospect-facing consequences, and the trial has not yet
# shown how often Botdog withdraws or why — so the first pass gives the
# operator visibility, not an automated verdict. DESIGN NOTE for a
# future pass: park them, don't silently drop them.
EVENT_INVITATION_WITHDRAWN = "LEAD_INVITATION_WITHDRAWN"

KNOWN_EVENT_TYPES: frozenset[str] = frozenset({
    EVENT_INVITATION_SENT,
    EVENT_INVITATION_ACCEPTED,
    EVENT_MESSAGE_SENT,
    EVENT_MESSAGE_REPLIED,
    EVENT_NONCAMPAIGN_INBOX,
    EVENT_INVITATION_WITHDRAWN,
})

# The authorized-writer identity for every CRM advance this
# workflow issues. Registered in clients/attio_writer_registry.py for
# stage, dm_step, dm1/2/3_sent_at, last_contact_date,
# next_eligible_send_date, response_received_at, experiment_id_frozen_at.
WRITER_MODULE = "workflows.botdog_ingest.apply_lead_events"

# Poll-cursor local state (mirrors safety_limits.LIMITS_DIR/FILE).
POLL_STATE_DIR = Path.home() / ".outbound-agent"
POLL_STATE_FILE = POLL_STATE_DIR / "botdog_poll.json"

# ── event-loss defense in depth (see module docstring) ────────────────
# How far BEHIND the stored cursor each poll actually asks. Two days
# absorbs clock skew + a missed run without operator action; the cost
# of over-polling is one API call and some idempotent no-ops.
BOTDOG_POLL_OVERLAP = timedelta(hours=48)

# Hard bound on how far the failure/scope-skip watermark may hold the
# cursor back. Without it, ONE permanently-unmatchable lead (a Botdog
# lead added by hand, never mirrored into Attio) pins the cursor
# forever and every run re-polls the whole tail. 7 days is a week of
# daily runs to notice and fix the underlying mismatch, which the
# stale-watchdog below also surfaces.
BOTDOG_MAX_CURSOR_HOLD_DAYS = 7

# ── staleness watchdog thresholds (report-only) ───────────────────────
# A botdog-channel invite that has sat at Connection Sent this long
# without an accept or a visible withdrawal is worth a human look:
# Botdog may have silently stopped sending, or the accept event may
# not be reaching us.
BOTDOG_STALE_INVITE_DAYS = 10
# A DM we SUBMITTED to Botdog this many business days ago with no
# matching dm{N}_sent_at advance means the message-sent event never
# arrived — the duplicate-send guard is (correctly) blocking a re-send,
# so the prospect is stuck until someone investigates.
BOTDOG_STALE_DM_BDAYS = 3
# Sample size echoed for each loud counter — enough to start an
# investigation, not enough to bury the run log.
_SAMPLE_URLS = 5

# Reverse of DM_STEP_NUMBER for the DM steps (1/2/3 → MessageStep).
_STEP_BY_NUMBER: dict[int, MessageStep] = {
    n: step
    for step, n in DM_STEP_NUMBER.items()
    if step in (MessageStep.DM1, MessageStep.DM2, MessageStep.DM3)
}
_MAX_DM_STEP = 3


# ── cursor state ──────────────────────────────────────────────────────


def _as_utc(when: datetime) -> datetime:
    """Coerce a datetime to UTC-aware.

    Naive and aware datetimes raise TypeError on comparison, and this
    module compares them constantly (``min``/``sorted`` over event
    timestamps, cursor arithmetic). Every datetime that crosses into
    the workflow — from the cursor file, from a caller's ``now``, from
    a transport's ``LeadEvent`` — goes through here, so a single
    offset-less value can never abort a whole poll. Offset-less input
    is assumed UTC, matching ``clients.sender._event_time``.
    """
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def read_cursor() -> datetime | None:
    """Return the last-poll timestamp, or None on first run / corrupt state.

    A missing or unparseable cursor resolves to None, which triggers a
    full catch-up poll (``since=None``). Because event application is
    idempotent, over-polling is always safe — losing the cursor never
    corrupts state, it only re-does work.
    """
    if not POLL_STATE_FILE.exists():
        return None
    try:
        with open(POLL_STATE_FILE) as f:
            data = json.load(f)
        raw = data.get("last_poll_at")
        if not raw:
            return None
        # Coerced to UTC-aware: a cursor written by an older build (or
        # hand-edited) could be offset-less, and a naive cursor would
        # blow up every comparison against aware event timestamps.
        return _as_utc(datetime.fromisoformat(str(raw)))
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        click.echo(
            f"  ⚠ Botdog poll cursor unreadable ({type(exc).__name__}: "
            f"{exc}) — treating as first run (full catch-up poll). "
            f"Idempotent application makes this safe.",
            err=True,
        )
        return None


def write_cursor(when: datetime) -> None:
    """Persist the poll cursor (atomic replace; mirrors safety_limits)."""
    POLL_STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"last_poll_at": when.isoformat()}
    tmp = POLL_STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, POLL_STATE_FILE)


# ── entry resolution ──────────────────────────────────────────────────


def _build_url_index(
    entries: list[dict],
) -> tuple[dict[str, dict], dict[str, dict]]:
    """Index parsed entries by normalized canonical URL and vanity slug.

    Botdog-channel prospects are new/post-cutover rows, so
    ``canonical_linkedin_url`` (and the vanity slug) are stamped at
    PROSPECT-commit. Legacy rows with neither are simply unreachable by
    URL match → their events count as ``skipped_scope_unmatched`` (never
    a wrong-record write).
    """
    by_url: dict[str, dict] = {}
    by_slug: dict[str, dict] = {}
    for entry in entries:
        canonical = entry.get("canonical_linkedin_url")
        if canonical:
            by_url.setdefault(_normalize_url_for_match(canonical), entry)
        slug = entry.get("vanity_url_slug")
        if slug:
            by_slug.setdefault(str(slug), entry)
    return by_url, by_slug


def _resolve_entry(
    url: str, by_url: dict[str, dict], by_slug: dict[str, dict]
) -> dict | None:
    """Resolve a normalized event URL to a parsed entry (canonical then slug)."""
    if not url:
        return None
    entry = by_url.get(url)
    if entry is not None:
        return entry
    slug = _vanity_url_slug(url)
    return by_slug.get(slug) if slug else None


def _event_date(event: LeadEvent) -> date | None:
    return event.occurred_at.date() if event.occurred_at is not None else None


# ── per-lead application ──────────────────────────────────────────────


class _Report(dict):
    """Run report with a couple of convenience bumpers (still a plain dict)."""

    def bump(self, key: str, n: int = 1) -> None:
        self[key] = self.get(key, 0) + n


def _new_report(dry_run: bool) -> _Report:
    r = _Report()
    r.update({
        "dry_run": dry_run,
        "cursor_before": None,
        "cursor_after": None,
        "cursor_age_seconds": None,
        # Effective poll floor actually sent to the transport
        # (cursor - BOTDOG_POLL_OVERLAP); differs from the cursor by
        # design, so both are reported.
        "poll_since": None,
        "cursor_hold_capped": False,
        "cursor_released_events": 0,
        # Leads the transport never READ this run (fetch budget / failed
        # detail read). They emit no events, so they hold the cursor.
        "leads_unread": 0,
        "cursor_write_skipped": False,
        "unledgered_message_sent": 0,
        "unledgered_message_sent_urls": [],
        "stale_botdog_invites": 0,
        "stale_botdog_invite_urls": [],
        "stale_botdog_dms": 0,
        "stale_botdog_dm_urls": [],
        "events_total": 0,
        "events_by_type": {},
        "leads_seen": 0,
        "applied": 0,
        "skipped_scope_pb": 0,
        "skipped_scope_unmatched": 0,
        "skipped_idempotent": 0,
        "unknown_events": 0,
        "noncampaign_events": 0,
        "invitation_withdrawn": 0,
        "invitation_withdrawn_urls": [],
        "undated_message_sent_skipped": 0,
        "cadence_complete_noop": 0,
        "failures": 0,
    })
    return r


def _write_advance(
    *,
    attio: AttioClient,
    entry: dict,
    attrs: dict,
    prior_stage: str | None,
    step_label: str,
    list_id: str,
    dry_run: bool,
    report: _Report,
    audit_logger: AuditLogger | None,
) -> bool:
    """Apply one entry advance (or preview it under dry-run).

    Routes through the SAME ``_attio_advance_with_escalation`` path the
    PB DM/accept advances use, so registry + monotonicity + DLQ /
    attio_write_failed escalation all fire. Returns True when the state
    changed (would change, under dry-run).
    """
    url = entry.get("canonical_linkedin_url") or entry.get("record_id") or "?"
    if dry_run:
        click.echo(
            f"  [dry-run] WOULD advance {url} ({step_label}): {attrs}"
        )
        report.bump("applied")
        return True

    # Deferred import avoids a module-load cycle (daily_check imports many
    # heavy deps and is imported lazily by cli, never imports this module).
    from workflows.daily_check import _attio_advance_with_escalation

    ok = _attio_advance_with_escalation(
        attio=attio,
        entry_id=entry["entry_id"],
        entry_attributes=attrs,
        list_id=list_id,
        linkedin_url=str(url),
        today=date.today().isoformat(),
        step_label=step_label,
        writer_module=WRITER_MODULE,
        prior_stage=prior_stage,
        person_record_id=entry.get("record_id") or None,
        audit_logger=audit_logger,
    )
    if ok:
        report.bump("applied")
    else:
        report.bump("failures")
    return ok


def _plan_message_advance(
    *,
    current_step: int,
    current_step_date: date | None,
    msg_events: list[LeadEvent],
    poll_date: date,
    report: _Report,
) -> tuple[int, dict[int, date]] | None:
    """Compute the target dm_step + per-step send dates from message-sent events.

    Returns ``(target_step, {step_number: send_date})`` for the steps
    newly crossed, or None when nothing advances. Idempotent: only sends
    strictly newer than the already-recorded step's date count, so a
    re-polled send contributes zero.
    """
    if current_step >= _MAX_DM_STEP:
        # DM3 is the cadence terminal (no DM4). Any further message-sent
        # events are real but unrepresentable — count, never advance.
        report.bump("cadence_complete_noop", len(msg_events))
        return None

    dated = sorted(
        (e for e in msg_events if _event_date(e) is not None),
        # `_as_utc` on the sort key: a single offset-less event
        # timestamp among aware ones would otherwise raise TypeError
        # here and abort the whole poll.
        key=lambda e: _as_utc(e.occurred_at),  # type: ignore[arg-type]
    )
    undated = [e for e in msg_events if _event_date(e) is None]

    if current_step_date is None:
        new_dates = [d for e in dated if (d := _event_date(e)) is not None]
    else:
        new_dates = [
            d for e in dated
            if (d := _event_date(e)) is not None and d > current_step_date
        ]

    if not new_dates:
        # No dated advance. Undated sends can only be safely ordered from
        # the un-DM'd baseline (step 0); beyond that they'd risk a runaway
        # advance every run, so count + surface them instead.
        if undated and current_step == 0:
            return 1, {1: poll_date}
        if undated:
            report.bump("undated_message_sent_skipped", len(undated))
        return None

    room = _MAX_DM_STEP - current_step
    crossed = new_dates[:room]
    # Message-sent events beyond DM3 have no representable step.
    overflow = len(new_dates) - len(crossed)
    if overflow:
        report.bump("cadence_complete_noop", overflow)
    if undated:
        # Dated advances took precedence; undated ones can't be ordered.
        report.bump("undated_message_sent_skipped", len(undated))

    target_step = current_step + len(crossed)
    step_dates = {
        current_step + i + 1: crossed[i] for i in range(len(crossed))
    }
    return target_step, step_dates


def _entry_url_keys(entry: dict) -> list[str]:
    """Normalized URL keys this entry could be recorded under.

    The submission ledger keys on the DM row's ``linkedInUrl`` (which
    comes from the PERSON record's linkedin field), while this module
    matches on the list entry's ``canonical_linkedin_url``. Both
    normalize identically, but they are separate stored fields that can
    drift — so a lookup tries every form the entry offers before
    concluding "no ledger entry".
    """
    keys = []
    for value in (
        entry.get("canonical_linkedin_url"),
        entry.get("linkedin_url"),
    ):
        key = _normalize_url_for_match(str(value or ""))
        if key and key not in keys:
            keys.append(key)
    return keys


def _crosscheck_message_ledger(
    *,
    entry: dict,
    step_dates: dict[int, date],
    poll_date: date,
    report: _Report,
) -> None:
    """Count message-sent advances with no local submission record.

    WHY: every DM this pipeline sends is written to
    the local Botdog submission ledger at submission time. A
    ``LEAD_MESSAGE_SENT`` event for a (url, step) we have NO record of
    submitting means the send did not come from here — a message typed
    by hand in Botdog's UI or LinkedIn, a conversational reply, or a
    campaign someone configured outside the pipeline. Those sends still
    consume the prospect's cadence, so the advance is real and MUST be
    applied; but the operator needs to know their cadence is being
    driven by something they cannot see in the run log.

    VISIBILITY, NOT BLOCKING — deliberately. The ledger is
    machine-local: a fresh checkout, a wiped home directory, a second
    operator seat, or a pruned entry (>30 days) all legitimately have
    no record. Refusing to advance on a ledger miss would wedge the
    pipeline on exactly those benign cases, leaving prospects frozen
    mid-cadence with no recovery path. So we advance and count loudly.
    """
    from workflows.botdog_ledger import recent_submission

    url_keys = _entry_url_keys(entry)
    display = (
        entry.get("canonical_linkedin_url")
        or entry.get("record_id")
        or "?"
    )
    for step_num in sorted(step_dates):
        step_value = _STEP_BY_NUMBER[step_num].value
        # `today=poll_date` (not the wall clock): the lookback window
        # must be measured from the run's own reference date, or an
        # injected/back-dated run would read every submission as expired
        # and report the whole batch as unledgered.
        if any(
            recent_submission(key, step_value, today=poll_date) is not None
            for key in url_keys
        ):
            continue
        report.bump("unledgered_message_sent")
        samples = report.setdefault("unledgered_message_sent_urls", [])
        label = f"{display} ({step_value})"
        if len(samples) < _SAMPLE_URLS and label not in samples:
            samples.append(label)
        click.echo(
            f"  ⚠ Botdog reported {step_value} sent to {display} but no "
            f"local submission ledger entry exists — advancing anyway "
            f"(the send is real), but this cadence step did NOT come "
            f"from this pipeline on this machine. Check for manual / "
            f"conversational sends in Botdog, or a lost ledger.",
            err=True,
        )


def apply_lead_events(
    *,
    attio: AttioClient,
    entry: dict,
    events: list[LeadEvent],
    list_id: str,
    dry_run: bool,
    poll_date: date,
    report: _Report,
    audit_logger: AuditLogger | None = None,
) -> None:
    """Reconcile ONE resolved botdog-channel entry against its events.

    Applies, in forward-rank order, the strongest transitions the events
    confirm: invite-sent → accepted → message-sent DM advance → replied.
    Each sub-step is guarded so an already-satisfied target is a no-op
    (counted as ``skipped_idempotent`` only when nothing at all changed
    for the lead).
    """
    from workflows.daily_check import (  # deferred (cycle-free)
        _confirmed_dm_advance_attrs,
        _phase0_accepted_update,
    )

    # Local mirror of the entry's advancing state.
    cur_stage = str(entry.get("stage") or PipelineStage.PROSPECT.value)
    try:
        cur_rank = stage_rank(cur_stage)
    except ValueError:
        # Unknown/corrupt stage — cannot reason about monotonicity safely.
        click.echo(
            f"  ⚠ Botdog ingest: entry {entry.get('entry_id')!r} has an "
            f"unrecognized stage {cur_stage!r}; skipping (data bug).",
            err=True,
        )
        report.bump("failures")
        return
    cur_step = dm_step_int(entry.get("dm_step"))

    types_present = {e.event_type.upper() for e in events}
    changed = False

    # 1. invitation-sent → confirm Connection Sent (drops optimistic advance).
    if EVENT_INVITATION_SENT in types_present and cur_rank < stage_rank(
        PipelineStage.CONNECTION_SENT
    ):
        invite_dates = [
            d for e in events
            if e.event_type.upper() == EVENT_INVITATION_SENT
            and (d := _event_date(e)) is not None
        ]
        contact = (max(invite_dates) if invite_dates else poll_date).isoformat()
        attrs = {
            "stage": PipelineStage.CONNECTION_SENT.value,
            "last_contact_date": contact,
        }
        # PARITY WITH THE PB INVITE PATH: when the
        # row carries an experiment_id, freeze the cohort at
        # "connection_sent" exactly as
        # daily_check.run_connection_requests does on a successful PB
        # invite. Botdog stamps experiment_id at SUBMISSION time; this
        # event is the confirmation that the invite really went out, so
        # it is the correct freeze point. Without it, botdog-cohort rows
        # would carry an unfrozen experiment_id and the learning loop
        # would measure them against a different (later) freeze
        # boundary than their PB counterparts — silently skewing every
        # A/B comparison across the migration. Guarded on
        # experiment_id, mirroring the PB path: no stamp on
        # no-experiment rows — and, like the PB path, never overwriting
        # an already-terminal frozen_at (cohort immutability).
        if (
            entry.get("experiment_id") is not None
            and entry.get("experiment_id_frozen_at")
            not in _IMMUTABLE_FROZEN_AT_VALUES
        ):
            attrs["experiment_id_frozen_at"] = "connection_sent"
        if _write_advance(
            attio=attio, entry=entry, attrs=attrs, prior_stage=cur_stage,
            step_label="botdog_invitation_sent", list_id=list_id,
            dry_run=dry_run, report=report, audit_logger=audit_logger,
        ):
            cur_stage = PipelineStage.CONNECTION_SENT.value
            cur_rank = stage_rank(cur_stage)
            changed = True

    # 2. invitation-accepted → ACCEPTED (mirrors Phase 0 _phase0_accepted_update).
    if EVENT_INVITATION_ACCEPTED in types_present and cur_rank < stage_rank(
        PipelineStage.ACCEPTED
    ):
        accept_attrs = _phase0_accepted_update(
            {**entry, "linkedin_url": entry.get("canonical_linkedin_url", "")},
            label="Botdog event accepted flip",
        )
        if _write_advance(
            attio=attio, entry=entry, attrs=accept_attrs, prior_stage=cur_stage,
            step_label="botdog_invitation_accepted", list_id=list_id,
            dry_run=dry_run, report=report, audit_logger=audit_logger,
        ):
            cur_stage = PipelineStage.ACCEPTED.value
            cur_rank = stage_rank(cur_stage)
            changed = True

    # 3. message-sent → event-confirmed DM advance (may catch up multiple steps).
    msg_events = [
        e for e in events if e.event_type.upper() == EVENT_MESSAGE_SENT
    ]
    if msg_events and cur_rank < stage_rank(PipelineStage.RESPONDED):
        cur_step_date = _current_step_date(entry, cur_step)
        plan = _plan_message_advance(
            current_step=cur_step,
            current_step_date=cur_step_date,
            msg_events=msg_events,
            poll_date=poll_date,
            report=report,
        )
        if plan is not None:
            target_step, step_dates = plan
            # Crosscheck BEFORE writing: report-only, never blocks the
            # advance (see the helper's docstring).
            _crosscheck_message_ledger(
                entry=entry,
                step_dates=step_dates,
                poll_date=poll_date,
                report=report,
            )
            target_enum = _STEP_BY_NUMBER[target_step]
            last_date = step_dates[target_step]
            attrs = _confirmed_dm_advance_attrs(
                step=target_enum,
                next_stage=NEXT_STAGE[target_enum],
                today=last_date,
                today_str=last_date.isoformat(),
            )
            # Stamp intermediate steps' sent_at when catching up >1 step.
            for step_num, sent_date in step_dates.items():
                enum = _STEP_BY_NUMBER[step_num]
                attrs.setdefault(f"{enum.value}_sent_at", sent_date.isoformat())
            if _write_advance(
                attio=attio, entry=entry, attrs=attrs, prior_stage=cur_stage,
                step_label=f"botdog_{target_enum.value}_sent", list_id=list_id,
                dry_run=dry_run, report=report, audit_logger=audit_logger,
            ):
                cur_stage = NEXT_STAGE[target_enum].value
                cur_rank = stage_rank(cur_stage)
                cur_step = target_step
                changed = True

    # 4. message-replied → Responded + response_received_at (mirror detect_responses).
    if EVENT_MESSAGE_REPLIED in types_present and cur_rank < stage_rank(
        PipelineStage.RESPONDED
    ):
        reply_dates = [
            d for e in events
            if e.event_type.upper() == EVENT_MESSAGE_REPLIED
            and (d := _event_date(e)) is not None
        ]
        received = (max(reply_dates) if reply_dates else poll_date).isoformat()
        attrs = {
            "stage": PipelineStage.RESPONDED.value,
            "response_received_at": received,
        }
        if _write_advance(
            attio=attio, entry=entry, attrs=attrs, prior_stage=cur_stage,
            step_label="botdog_message_replied", list_id=list_id,
            dry_run=dry_run, report=report, audit_logger=audit_logger,
        ):
            changed = True

    if not changed:
        # Every transition the events imply was already reflected — the
        # overlap-tolerant no-op case (re-polled already-applied events).
        report.bump("skipped_idempotent")


def _current_step_date(entry: dict, current_step: int) -> date | None:
    """Parse the already-recorded ``dm{N}_sent_at`` for the current step."""
    if current_step < 1 or current_step > _MAX_DM_STEP:
        return None
    raw = entry.get(f"dm{current_step}_sent_at")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


# ── staleness watchdog (report-only) ──────────────────────────────────

_DM_STAGES = frozenset({
    PipelineStage.ACCEPTED.value,
    PipelineStage.DM1_SENT.value,
    PipelineStage.DM2_SENT.value,
    PipelineStage.DM3_SENT.value,
})


def _parse_date(raw: object) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def scan_stale_botdog_rows(
    entries: list[dict], *, today: date, report: _Report
) -> None:
    """Count botdog-channel rows whose transport has gone quiet.

    WHY: the PB pipeline's stale nets — the
    ``stale_connection_sent`` queue row and the Phase-0 escalations —
    key off SCRAPE evidence, which botdog-channel rows never produce
    (they are explicitly held out of Phase 0 / 0.5). A botdog invite
    that Botdog silently stopped sending, or an accept event that never
    reaches us, therefore ages forever with nothing watching. Same for a
    DM: the duplicate-send guard blocks a re-send until the message-sent
    event confirms, so a lost event freezes the prospect permanently and
    QUIETLY — the guard is doing its job, which is exactly what makes
    the silence dangerous.

    Two buckets, both REPORT-ONLY — no writes, no escalation objects,
    no queue rows. This is a visibility instrument for the trial, not
    an automated remediation: a wrong automated action on a
    prospect-facing cadence is worse than a loud counter, and the right
    response (check Botdog's dashboard, check the campaign is running,
    check the poll) is a human judgement call.

      a) INVITES: stage Connection Sent with ``last_contact_date``
         older than ``BOTDOG_STALE_INVITE_DAYS`` — neither accepted nor
         visibly withdrawn.
      b) DMs: a ledger submission at least ``BOTDOG_STALE_DM_BDAYS``
         business days old whose ``dm{N}_sent_at`` never landed — the
         message-sent event is missing.
    """
    from workflows.botdog_ledger import PRUNE_AFTER_DAYS, recent_submission

    for entry in entries:
        if _resolve_send_channel(entry) != SEND_CHANNEL_BOTDOG:
            continue
        stage = str(entry.get("stage") or "")
        display = (
            entry.get("canonical_linkedin_url")
            or entry.get("record_id")
            or "?"
        )

        if stage == PipelineStage.CONNECTION_SENT.value:
            contacted = _parse_date(entry.get("last_contact_date"))
            if (
                contacted is not None
                and (today - contacted).days > BOTDOG_STALE_INVITE_DAYS
            ):
                report.bump("stale_botdog_invites")
                samples = report.setdefault("stale_botdog_invite_urls", [])
                if len(samples) < _SAMPLE_URLS:
                    samples.append(f"{display} (sent {contacted.isoformat()})")
            continue

        if stage not in _DM_STAGES:
            continue
        url_keys = _entry_url_keys(entry)
        if not url_keys:
            continue
        for step_num in range(1, _MAX_DM_STEP + 1):
            if entry.get(f"dm{step_num}_sent_at"):
                continue  # advance landed — nothing stuck
            step_value = _STEP_BY_NUMBER[step_num].value
            submitted = next(
                (
                    when
                    for key in url_keys
                    if (
                        when := recent_submission(
                            key,
                            step_value,
                            within_days=PRUNE_AFTER_DAYS,
                            today=today,
                        )
                    )
                    is not None
                ),
                None,
            )
            if submitted is None:
                continue
            if add_business_days(submitted, BOTDOG_STALE_DM_BDAYS) > today:
                continue  # still inside the normal confirmation window
            report.bump("stale_botdog_dms")
            samples = report.setdefault("stale_botdog_dm_urls", [])
            if len(samples) < _SAMPLE_URLS:
                samples.append(
                    f"{display} ({step_value} submitted "
                    f"{submitted.isoformat()})"
                )

    if report.get("stale_botdog_invites"):
        click.echo(
            f"  ⚠ {report['stale_botdog_invites']} botdog invite(s) still "
            f"at Connection Sent after {BOTDOG_STALE_INVITE_DAYS}+ days "
            f"with no accept event: "
            f"{'; '.join(report.get('stale_botdog_invite_urls') or [])}. "
            f"Check the Botdog campaign is running and the account is "
            f"connected — no scrape-based net watches these rows.",
            err=True,
        )
    if report.get("stale_botdog_dms"):
        click.echo(
            f"  ⚠ {report['stale_botdog_dms']} botdog DM(s) submitted "
            f"{BOTDOG_STALE_DM_BDAYS}+ business days ago with no "
            f"message-sent event: "
            f"{'; '.join(report.get('stale_botdog_dm_urls') or [])}. "
            f"These prospects are FROZEN mid-cadence — the duplicate-send "
            f"guard blocks a re-send until the event confirms. "
            f"Investigate the Botdog send queue / event poll.",
            err=True,
        )


# ── top-level workflow ────────────────────────────────────────────────


def ingest_botdog_events(
    attio: AttioClient,
    sender: Sender | None = None,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    audit_logger: AuditLogger | None = None,
    entries_provider: Callable[[AttioClient], list[dict]] | None = None,
) -> dict:
    """Poll Botdog lead events and apply event-confirmed Attio advances.

    Only ``send_channel == botdog`` entries are touched (scope
    guard); pb-channel and unmatched leads are skipped + counted (and
    their events hold the cursor — see the module docstring). Returns
    a LOUD run report (per-event-type counts, applied vs skipped vs
    unknown, leads the transport never read, cursor age + effective poll
    floor, unledgered sends, stale rows, failures) — this path is
    prospect-state-critical and must never drift silently.

    ``sender`` defaults to ``daily_check._build_botdog_sender()`` (lazy —
    only constructed here, so a pure-PB run that never calls this stays
    key-free). ``entries_provider`` defaults to
    ``_get_all_entries_parsed``; both are injectable for tests.
    """
    now = _as_utc(now or datetime.now(UTC))
    report = _new_report(dry_run)

    cursor = read_cursor()
    report["cursor_before"] = cursor.isoformat() if cursor else None
    if cursor is not None:
        report["cursor_age_seconds"] = (now - cursor).total_seconds()

    if sender is None:
        from workflows.daily_check import _build_botdog_sender
        sender = _build_botdog_sender()

    # OVERLAP POLL: ask for events since
    # `cursor - BOTDOG_POLL_OVERLAP`, not since the cursor. The cursor
    # bookkeeping itself is unchanged — this only widens what we LOOK
    # at, so clock skew, a late-visible event, or a crash between apply
    # and cursor-write can't drop events into a blind spot. Safe by
    # construction: every advance below reconciles toward an absolute
    # target and re-applying a settled event is a counted no-op.
    poll_since = None if cursor is None else cursor - BOTDOG_POLL_OVERLAP
    report["poll_since"] = poll_since.isoformat() if poll_since else None

    events = sender.fetch_events(since=poll_since)
    # COMPLETENESS SIGNAL: leads the transport could not read this run
    # (detail-fetch budget, unreadable lead). They contribute NO events,
    # so `held_event_times` below can never speak for them — the cursor
    # is held separately (see the cursor block). A transport without the
    # signal (a plain list, e.g. PB) leaves nothing unread by definition.
    leads_unread = int(getattr(events, "leads_unread", 0) or 0)
    report["leads_unread"] = leads_unread
    report["events_total"] = len(events)
    by_type: dict[str, int] = {}
    for ev in events:
        by_type[ev.event_type] = by_type.get(ev.event_type, 0) + 1
    report["events_by_type"] = by_type

    # Group by normalized URL.
    groups: dict[str, list[LeadEvent]] = {}
    for ev in events:
        groups.setdefault(ev.lead_linkedin_url, []).append(ev)
    report["leads_seen"] = len(groups)

    if entries_provider is None:
        from workflows.daily_check_helpers import _get_all_entries_parsed
        entries_provider = _get_all_entries_parsed
    entries = entries_provider(attio)
    by_url, by_slug = _build_url_index(entries)
    list_id = os.environ.get("ATTIO_LIST_ID", "")

    # DATED timestamps of every actionable event this run did NOT apply:
    # a failed advance, or a scope-skip. The cursor is held at the
    # earliest of them so the next run polls them again instead of
    # skipping them forever (fetch_events keeps occurred >= since).
    # Undated events self-heal — fetch_events returns occurred_at=None
    # events regardless of the cursor.
    #
    # SCOPE-SKIPS ARE HELD TOO: the common skip is a
    # RACE, not a permanent verdict — a botdog lead whose Attio row
    # hasn't been stamped `send_channel=botdog` yet, or whose person
    # record is still being created. Dropping those events would lose a
    # real accept/reply forever. Permanently unmatchable leads are
    # bounded by the hold cap below.
    held_event_times: list[datetime] = []

    def _hold(lead_events: list[LeadEvent]) -> None:
        held_event_times.extend(
            _as_utc(e.occurred_at)
            for e in lead_events
            if e.occurred_at is not None
        )

    for url, lead_events in groups.items():
        # Count-only events (no state change) — surfaced, never silent.
        actionable = [
            e for e in lead_events
            if e.event_type.upper() in {
                EVENT_INVITATION_SENT, EVENT_INVITATION_ACCEPTED,
                EVENT_MESSAGE_SENT, EVENT_MESSAGE_REPLIED,
            }
        ]
        for e in lead_events:
            etype = e.event_type.upper()
            if etype == EVENT_NONCAMPAIGN_INBOX:
                report.bump("noncampaign_events")
            elif etype == EVENT_INVITATION_WITHDRAWN:
                # KNOWN, counted, never advancing (see the constant's
                # note): a withdrawal must not read as an "unknown
                # event" — that counter is the alarm for vocabulary
                # drift, and burying a known type in it would make real
                # drift invisible.
                report.bump("invitation_withdrawn")
                samples = report.setdefault("invitation_withdrawn_urls", [])
                if len(samples) < _SAMPLE_URLS and url not in samples:
                    samples.append(url or "?")
            elif etype not in KNOWN_EVENT_TYPES:
                report.bump("unknown_events")

        if not actionable:
            continue

        entry = _resolve_entry(url, by_url, by_slug)
        if entry is None:
            report.bump("skipped_scope_unmatched")
            _hold(actionable)
            continue
        # Scope guard: ONLY botdog-channel entries. A pb/None channel
        # entry is a drain-pool prospect owned by Phase 0 / 0.5 — never
        # flip it from a Botdog event. Follows the SHARED resolver
        # rather than a raw attribute compare, so this guard, the send
        # path and the Phase-0.5 skip can never disagree about which
        # transport owns a row.
        if _resolve_send_channel(entry) != SEND_CHANNEL_BOTDOG:
            report.bump("skipped_scope_pb")
            _hold(actionable)
            continue

        failures_before = report.get("failures", 0)
        apply_lead_events(
            attio=attio,
            entry=entry,
            events=actionable,
            list_id=list_id,
            dry_run=dry_run,
            poll_date=now.date(),
            report=report,
            audit_logger=audit_logger,
        )
        if report.get("failures", 0) > failures_before:
            _hold(actionable)

    if report.get("invitation_withdrawn"):
        click.echo(
            f"  ⚠ {report['invitation_withdrawn']} Botdog invitation "
            f"withdrawal event(s) — these invites were pulled back and "
            f"will never be accepted: "
            f"{'; '.join(report.get('invitation_withdrawn_urls') or [])}. "
            f"NO Attio change was made (counted only); decide by hand "
            f"whether to park or re-invite them.",
            err=True,
        )

    scan_stale_botdog_rows(entries, today=now.date(), report=report)

    if leads_unread:
        click.echo(
            f"  ⚠ Botdog event poll INCOMPLETE: {leads_unread} lead(s) "
            f"were never read this run (detail-fetch budget or an "
            f"unreadable lead — see the poll warnings above). They "
            f"produced NO events, so the ingest cursor is HELD at its "
            f"previous value; their events are re-polled next run. "
            f"A count that stays non-zero every run means the lead set "
            f"is too big for the per-poll budget — narrow it (campaign "
            f"filter) or raise the budget deliberately.",
            err=True,
        )

    if not dry_run and leads_unread and cursor is None:
        # UNREAD LEADS ON A FIRST (unbounded) POLL: there is no previous
        # cursor to hold at, and writing ANY cursor would put a floor
        # under the next poll — below which the unread leads' real events
        # would sit forever. Skip the write so the next run is unbounded
        # again. Loud, and reported.
        report["cursor_write_skipped"] = True
        click.echo(
            f"  ⚠ Botdog cursor NOT written: this was an unbounded first "
            f"poll and {leads_unread} lead(s) went unread. Writing a "
            f"cursor now would hide their events below the next poll's "
            f"floor. The next run polls unbounded again.",
            err=True,
        )
    elif not dry_run:
        # Advance to `now`, EXCEPT hold at the earliest un-applied event
        # so its lead is retried next run (idempotent). A dry-run never
        # moves the cursor — we never advance PAST unprocessed state.
        watermark = min(held_event_times) if held_event_times else None
        if leads_unread and cursor is not None:
            # Unread leads emit no events, so the watermark above cannot
            # represent them. Hold at the PREVIOUS cursor — the last
            # point we know was fully read — so nothing they may carry
            # ages past the poll floor. The hold cap below still bounds
            # this, so a permanently unreadable lead cannot pin the
            # cursor forever without an announcement.
            watermark = cursor if watermark is None else min(watermark, cursor)
        safe_cursor = now if watermark is None else min(now, watermark)

        # HOLD CAP: never let the cursor lag more than
        # BOTDOG_MAX_CURSOR_HOLD_DAYS behind now. One never-resolvable
        # lead (manually added in Botdog, no Attio row will ever match)
        # would otherwise pin the cursor permanently. Releasing is a
        # REAL LOSS — those events are never polled again — so it is
        # announced with the count, not swallowed.
        floor = now - timedelta(days=BOTDOG_MAX_CURSOR_HOLD_DAYS)
        if safe_cursor < floor:
            released = sum(1 for ts in held_event_times if ts < floor)
            report["cursor_hold_capped"] = True
            report["cursor_released_events"] = released
            click.echo(
                f"  ⚠ Botdog cursor hold CAP reached: the un-applied "
                f"watermark ({safe_cursor.isoformat()}) is more than "
                f"{BOTDOG_MAX_CURSOR_HOLD_DAYS} days behind now. "
                f"Releasing the cursor to {floor.isoformat()} — "
                f"{released} known event(s) older than that will NEVER "
                f"be polled again (beyond recovery — plus anything "
                f"carried by leads that went unread). This means a lead "
                f"has been failing, scope-skipped, or unreadable every "
                f"run for a week: find it (see scope-skip / failure / "
                f"leads-unread counts above) and fix the Attio row, the "
                f"write path, or the poll budget.",
                err=True,
            )
            safe_cursor = floor

        write_cursor(safe_cursor)
        report["cursor_after"] = safe_cursor.isoformat()

    return report


def format_report(report: dict) -> str:
    """One-block human-readable run summary for the daily log."""
    age = report.get("cursor_age_seconds")
    age_str = f"{age / 3600:.1f}h" if isinstance(age, (int, float)) else "n/a"
    by_type = report.get("events_by_type") or {}
    type_str = ", ".join(f"{k}={v}" for k, v in sorted(by_type.items())) or "none"
    extras = ""
    if report.get("leads_unread"):
        extras += (
            f"\n  ⚠ INCOMPLETE poll: {report['leads_unread']} lead(s) "
            f"never read (no events from them) — cursor held"
            + (
                " (not written: unbounded first poll)"
                if report.get("cursor_write_skipped")
                else ""
            )
            + "."
        )
    if report.get("cursor_hold_capped"):
        extras += (
            f"\n  ⚠ cursor hold CAP hit — "
            f"{report.get('cursor_released_events', 0)} event(s) released "
            f"beyond recovery (never re-polled)."
        )
    if report.get("unledgered_message_sent"):
        extras += (
            f"\n  ⚠ unledgered message-sent: "
            f"{report['unledgered_message_sent']} (advanced anyway) — "
            f"{'; '.join(report.get('unledgered_message_sent_urls') or [])}"
        )
    if report.get("invitation_withdrawn"):
        extras += (
            f"\n  ⚠ invitations withdrawn: "
            f"{report['invitation_withdrawn']} (counted only, no Attio "
            f"change) — "
            f"{'; '.join(report.get('invitation_withdrawn_urls') or [])}"
        )
    if report.get("stale_botdog_invites"):
        extras += (
            f"\n  ⚠ stale botdog invites (>{BOTDOG_STALE_INVITE_DAYS}d at "
            f"Connection Sent): {report['stale_botdog_invites']} — "
            f"{'; '.join(report.get('stale_botdog_invite_urls') or [])}"
        )
    if report.get("stale_botdog_dms"):
        extras += (
            f"\n  ⚠ stale botdog DMs (submitted "
            f"{BOTDOG_STALE_DM_BDAYS}+ business days ago, no event): "
            f"{report['stale_botdog_dms']} — "
            f"{'; '.join(report.get('stale_botdog_dm_urls') or [])}"
        )
    return (
        f"Botdog ingest{' (dry-run)' if report.get('dry_run') else ''}: "
        f"{report.get('events_total', 0)} events across "
        f"{report.get('leads_seen', 0)} lead(s) [{type_str}]; cursor age {age_str}. "
        f"leads-unread={report.get('leads_unread', 0)} "
        f"applied={report.get('applied', 0)} "
        f"idempotent-skip={report.get('skipped_idempotent', 0)} "
        f"scope-skip(pb)={report.get('skipped_scope_pb', 0)} "
        f"scope-skip(unmatched)={report.get('skipped_scope_unmatched', 0)} "
        f"unknown={report.get('unknown_events', 0)} "
        f"non-campaign={report.get('noncampaign_events', 0)} "
        f"withdrawn={report.get('invitation_withdrawn', 0)} "
        f"undated-msg-skip={report.get('undated_message_sent_skipped', 0)} "
        f"cadence-complete={report.get('cadence_complete_noop', 0)} "
        f"unledgered-msg={report.get('unledgered_message_sent', 0)} "
        f"stale-invites={report.get('stale_botdog_invites', 0)} "
        f"stale-dms={report.get('stale_botdog_dms', 0)} "
        f"failures={report.get('failures', 0)}"
        + extras
    )
