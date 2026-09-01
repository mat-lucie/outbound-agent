"""Sender seam: transport-agnostic send interface + the PB implementation.

This module is the seam that makes swapping the delivery transport a
routing decision instead of a rewrite:

- `Sender` is the transport protocol every sender implements. It is
  deliberately small — batch assembly, lane split, the pre-invite
  degree check, dry-run preview, the wet confirm gate, cap leases, and
  all CRM advance logic live with the callers in
  `workflows/daily_check.py`; only the launch/collect transport hop
  goes through a sender.
- `PBSender` wraps the PhantomBuster flow exactly as it ran inline
  before this seam existed: sheet write → phantom launch →
  `wait_for_completion` → result CSV download → `parse_send_outcome`
  (plus the `compute_invite_outcome` override on the invite path).
  `fetch_events` returns nothing — PB accept/reply detection stays
  scrape-based (Phase 0 / 0.5).
- PhantomBuster is the DEFAULT and only wired send transport. An
  optional alternative transport may implement the same protocol and
  return submission-shaped outcomes (see `BotdogSubmitOutcome`); such a
  transport is off by default and never routes a send unless an
  operator wires it explicitly.

The PB code in `workflows/daily_check.py` needs more than the protocol's
`SendOutcome`: the advance gate keys on the `PBLaunch` container id,
`emit_pb_silent_no_op` / audit events carry it, and the invite
optimistic-advance diagnostic parses the completion log. So `PBSender`
also exposes PB-shaped per-launch methods (`launch_invite_batch` /
`launch_dm_batch`) returning the full `PBSendResult` envelope; the
protocol methods are thin wrappers over them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import click

from clients.google_sheets import write_prospects_to_sheet
from clients.pb_envelope import (
    PBCompletion,
    PBLaunch,
    SendOutcome,
    _normalize_url_for_match,
    compute_invite_outcome,
    parse_send_outcome,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from clients.phantombuster import PhantomBusterClient


@dataclass(frozen=True)
class LeadEvent:
    """One delivery/engagement event reported by a sender's transport.

    Consumed by an event-ingestion workflow to apply event-confirmed CRM
    advances on a poll-based transport. `raw` retains the transport's
    original event payload for audit/debugging; `occurred_at` is None
    when the transport omits a timestamp.
    """

    event_type: str
    lead_linkedin_url: str
    occurred_at: datetime | None
    raw: dict


@dataclass(frozen=True)
class BotdogSubmitOutcome:
    """SUBMISSION-only outcome for one send call on a queueing transport.

    Reports what the transport ACCEPTED (it will send on its own
    schedule), never what LinkedIn delivered. Deliberately shares NO
    field names with `SendOutcome` — there is no `csv_status`, no
    `sent_urls`, no `sent_count` — so this object can never satisfy
    `should_advance_batch` or the per-row advance loops in
    `workflows/daily_check.py`: any code path that tries to treat a
    submission as a delivery confirmation crashes with AttributeError
    instead of silently advancing a stage. Stage/dm_step advances on a
    submission transport are event-confirmed only.

    URLs are normalized via `_normalize_url_for_match`. `failed` pairs
    each failed URL with a short reason string (safe for run reports —
    reasons come from typed transport errors, which scrub profile URLs).
    `skipped_existing_urls` is the idempotency pre-check result: leads
    already present in the target campaign, logged + surfaced, never
    re-submitted and never charged (a skip must never be silent).
    `raw_results` retains the raw response payloads for audit/forensics.
    """

    submitted_urls: frozenset[str] = frozenset()
    skipped_existing_urls: frozenset[str] = frozenset()
    failed: tuple[tuple[str, str], ...] = ()  # (url, reason)
    campaign_ids: tuple[str, ...] = ()
    raw_results: tuple[dict, ...] = field(default=(), repr=False)

    @property
    def submitted_count(self) -> int:
        return len(self.submitted_urls)

    @property
    def skipped_existing_count(self) -> int:
        return len(self.skipped_existing_urls)

    @property
    def failed_count(self) -> int:
        return len(self.failed)


@runtime_checkable
class Sender(Protocol):
    """Transport protocol for outbound LinkedIn sends.

    Implementations deliver what the caller already assembled and
    approved — they never select, filter, or advance prospects.

    Return types are transport-shaped: `PBSender` returns the delivery
    `SendOutcome` (PB reports per-URL delivery in its result CSV); a
    queueing transport returns `BotdogSubmitOutcome` (submission
    acknowledged — delivery arrives later as events). Callers branch on
    the concrete type they injected; the union keeps the protocol honest
    about both.
    """

    def send_invites(
        self, batch: list[dict]
    ) -> SendOutcome | BotdogSubmitOutcome:
        """Deliver one approved invite batch; report per-URL outcome."""
        ...

    def send_dm(
        self, prospect: dict, message: str
    ) -> SendOutcome | BotdogSubmitOutcome:
        """Deliver one DM to one prospect; report the outcome."""
        ...

    def fetch_events(self, since: datetime | None) -> list[LeadEvent]:
        """Return delivery/engagement events observed since `since`.

        Transports without an event feed (PB) return an empty list —
        their detection stays scrape-based.
        """
        ...


@dataclass(frozen=True)
class PBSendResult:
    """Full PB transport envelope for one phantom launch.

    Carries the `PBLaunch` + `PBCompletion` alongside the parsed
    `SendOutcome` because the PB code in `daily_check` still needs them:
    the advance gate checks the launch container id, the pb_silent_no_op
    / inmail_dead_end queue rows embed the launch, and the invite
    optimistic-advance diagnostic parses the completion log. A queueing
    transport has no equivalent — this envelope is PB-internal.
    """

    launch: PBLaunch
    completion: PBCompletion
    outcome: SendOutcome


class PBSender:
    """`Sender` backed by the PhantomBuster send phantoms — the default.

    Behavior-preserving wrap of the flow previously inlined in
    `workflows/daily_check.py` (`run_connection_requests` /
    `run_dm_sequencing`): same sheet write, same launch args, same poll
    cadence, same CSV parse. Dependencies arrive via constructor
    injection — callers pass `write_sheet` / `session_args` from their
    own module namespace so existing test monkeypatches keep binding.
    """

    def __init__(
        self,
        pb: PhantomBusterClient,
        *,
        network_booster_id: str | None = None,
        message_sender_id: str | None = None,
        write_sheet: Callable[[list[dict]], str] = write_prospects_to_sheet,
        session_args: Callable[[], dict] | None = None,
        poll_interval: int = 15,
        max_wait: int = 1800,
    ) -> None:
        self._pb = pb
        self._network_booster_id = network_booster_id
        self._message_sender_id = message_sender_id
        self._write_sheet = write_sheet
        self._session_args: Callable[[], dict] = session_args or (lambda: {})
        self._poll_interval = poll_interval
        self._max_wait = max_wait

    # ── PB-shaped per-launch transport (the send loops call these) ─────

    def launch_invite_batch(
        self, rows: list[dict], requested_urls: set[str]
    ) -> PBSendResult:
        """One Network Booster launch for an approved invite batch.

        ``rows`` is everything written to the sheet (invites AND the
        CONNECTION_SENT re-check rows that share the launch);
        ``requested_urls`` is only the normalized invite URLs, because
        the outcome parse and the invite override reason about sends,
        not visits.
        """
        if not self._network_booster_id:
            raise ValueError("PBSender: network_booster_id not configured")
        sheet_url = self._write_sheet(rows)
        click.echo(f"  Wrote {len(rows)} rows to Google Sheet.")
        launch_args = {"spreadsheetUrl": sheet_url, **self._session_args()}
        launch = self._pb.launch_agent(self._network_booster_id, launch_args)
        # Block until the phantom finishes — prevents workspace
        # parallel-execution cap when the next phantom (Message Sender)
        # launches in Part B.
        completion = self._pb.wait_for_completion(
            launch, poll_interval=self._poll_interval, max_wait=self._max_wait
        )
        csv_text = self._pb.download_result_csv(launch)
        outcome = parse_send_outcome(
            launch=launch,
            completion=completion,
            csv_text=csv_text,
            requested_urls=requested_urls,
        )
        # Network Booster (invite) override: its result.csv is unreliable
        # for per-run send confirmation (no `status` column, accumulates
        # stale rows), so the parsed outcome is always Skipped/0 and the
        # gate never advanced invites — they re-queued forever and Phase 0
        # never watched for accepts. A clean, authenticated, uncapped
        # launch advances ALL requested invites; a dead-cookie or capped
        # launch stays Skipped → pb_silent_no_op. (DM sequencing keeps the
        # strict CSV gate — its `status` column is reliable.)
        outcome = compute_invite_outcome(outcome, completion, requested_urls)
        return PBSendResult(launch=launch, completion=completion, outcome=outcome)

    def launch_dm_batch(
        self, rows: list[dict], requested_urls: set[str], *, step_label: str
    ) -> PBSendResult:
        """One Message Sender launch for an approved DM batch (one step)."""
        if not self._message_sender_id:
            raise ValueError("PBSender: message_sender_id not configured")
        sheet_url = self._write_sheet(rows)
        click.echo(
            f"  Wrote {len(rows)} {step_label} messages to Google Sheet."
        )
        launch_args = {
            "spreadsheetUrl": sheet_url,
            "message": "#message#",  # per-row from sheet column
            **self._session_args(),
        }
        # Typed launch + raises-on-error wait. PBRunFailed bubbles up so the
        # operator sees the failure instead of silently skipping the batch.
        launch = self._pb.launch_agent(self._message_sender_id, launch_args)
        # Block until this DM batch finishes before launching the next step
        # (or anything else) — avoids PB workspace parallel cap.
        completion = self._pb.wait_for_completion(
            launch, poll_interval=self._poll_interval, max_wait=self._max_wait
        )
        result_csv = self._pb.download_result_csv(launch)
        outcome = parse_send_outcome(
            launch=launch,
            completion=completion,
            csv_text=result_csv,
            requested_urls=requested_urls,
        )
        return PBSendResult(launch=launch, completion=completion, outcome=outcome)

    # ── Sender protocol conformance ────────────────────────────────────

    def send_invites(self, batch: list[dict]) -> SendOutcome:
        requested = {
            _normalize_url_for_match(row["linkedInUrl"])
            for row in batch
            if row.get("linkedInUrl")
        }
        return self.launch_invite_batch(batch, requested).outcome

    def send_dm(self, prospect: dict, message: str) -> SendOutcome:
        row = {**prospect, "message": message}
        requested = (
            {_normalize_url_for_match(row["linkedInUrl"])}
            if row.get("linkedInUrl")
            else set()
        )
        return self.launch_dm_batch(
            [row], requested, step_label=str(row.get("dm_step") or "dm")
        ).outcome

    def fetch_events(self, since: datetime | None) -> list[LeadEvent]:
        # PB has no event feed — accept/reply detection stays scrape-based
        # (Phase 0 / 0.5).
        return []
