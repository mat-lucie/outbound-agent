"""Behavioral tests for the optional Botdog event-ingestion workflow.

Exercises ``workflows/botdog_ingest.py`` against a fake Sender plus
injected parsed entries, with the CRM advance path
(``daily_check._attio_advance_with_escalation``) monkeypatched to a
capture double. No network, no real CRM, no real home directory.

Covered, and why each area is load-bearing:

- event -> attribute translation per event type (the whole point of the
  workflow: a transport event becomes an event-confirmed stage advance);
- the SCOPE GUARD — only ``send_channel == botdog`` rows may be touched;
  a pb/channel-less row or an unmatched URL is skipped and counted, never
  flipped, because those rows are owned by the scrape-based phases and a
  double-write there is prospect-visible;
- IDEMPOTENCY — the poll deliberately overlaps, so re-applying a settled
  event must be a counted no-op rather than a second advance;
- the CURSOR contract: read/write/first-run catch-up, plus the
  failure/scope-skip WATERMARK and its HOLD CAP (an event that cannot be
  applied must be re-polled next run, but one permanently unmatchable
  lead must not pin the cursor forever);
- the UNREAD-LEAD HOLD — a lead the transport never read emits NO
  events, so the watermark cannot speak for it; the cursor must be held
  at its previous value (or not written at all on an unbounded first
  poll) instead of advancing past events nobody ever saw;
- the UNLEDGERED-SEND CROSSCHECK — a message-sent event with no local
  submission record means the cadence is being driven by something the
  operator cannot see in the run log; it must advance anyway and count
  loudly;
- the report-only staleness watchdog (botdog rows have no scrape-based
  net watching them).

All identities here are synthetic (``acme-*``).
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from clients.sender import LeadEvent, LeadEventBatch
from models.pipeline import PipelineStage
from workflows import botdog_ingest, botdog_ledger
from workflows.botdog_ingest import ingest_botdog_events

# Synthetic identities — the whole suite runs against these.
URL_ALICE = "https://linkedin.com/in/acme-alice"
URL_BOB = "https://linkedin.com/in/acme-bob"
URL_GHOST = "https://linkedin.com/in/acme-ghost"
URL_DANA = "https://linkedin.com/in/acme-dana"

# ── helpers ───────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolated_botdog_state(monkeypatch, tmp_path):
    """Point BOTH pieces of local state at a per-test directory.

    Two independent files would otherwise be read/written under the real
    ``~/.outbound-agent``:

    * the submission LEDGER — the unledgered-send crosscheck and the
      stale-DM watchdog both READ it, so without isolation every
      assertion here would depend on the operator's production state;
    * the poll CURSOR — ``ingest_botdog_events`` WRITES it on any
      non-dry-run, so an unisolated test run would clobber the operator's
      real cursor and silently skip or re-poll live events.

    Tests that assert on the cursor file itself re-point it at their own
    ``tmp_path`` via ``_run(tmp=...)``; those setattrs stack on this
    baseline.
    """
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    monkeypatch.setattr(botdog_ledger, "LEDGER_DIR", ledger_dir)
    monkeypatch.setattr(
        botdog_ledger, "LEDGER_FILE", ledger_dir / "botdog_submissions.json"
    )

    state_dir = tmp_path / "state"
    monkeypatch.setattr(botdog_ingest, "POLL_STATE_DIR", state_dir)
    monkeypatch.setattr(
        botdog_ingest, "POLL_STATE_FILE", state_dir / "botdog_poll.json"
    )
    return ledger_dir


class _FakeSender:
    """Sender stub: fetch_events returns a canned list; records `since`.

    With ``leads_unread=0`` it returns a PLAIN list — the shape of a
    transport that carries no completeness signal (PB) — so the ingest's
    tolerance of that shape stays covered. With a non-zero count it
    returns the richer `LeadEventBatch`, like the Botdog transport.
    """

    def __init__(self, events: list[LeadEvent], *, leads_unread: int = 0):
        self._events = events
        self._leads_unread = leads_unread
        self.since_calls: list[object] = []

    def fetch_events(self, since):
        self.since_calls.append(since)
        if not self._leads_unread:
            return list(self._events)
        return LeadEventBatch(self._events, leads_unread=self._leads_unread)

    def send_invites(self, batch):  # pragma: no cover - protocol filler
        raise NotImplementedError

    def send_dm(self, prospect, message):  # pragma: no cover
        raise NotImplementedError


def _ev(event_type: str, url: str, day: int | None, month: int = 6) -> LeadEvent:
    occurred = (
        datetime(2026, month, day, 12, 0, tzinfo=UTC)
        if day is not None
        else None
    )
    return LeadEvent(
        event_type=event_type,
        lead_linkedin_url=url,
        occurred_at=occurred,
        raw={"type": event_type},
    )


def _entry(
    *,
    entry_id="entry-acme-1",
    record_id="rec-acme-1",
    url=URL_ALICE,
    stage=PipelineStage.PROSPECT.value,
    dm_step=None,
    send_channel="botdog",
    experiment_id=None,
    **extra,
) -> dict:
    entry = {
        "entry_id": entry_id,
        "record_id": record_id,
        "stage": stage,
        "dm_step": dm_step,
        "canonical_linkedin_url": url,
        "vanity_url_slug": url.rsplit("/", 1)[-1],
        "send_channel": send_channel,
        "experiment_id": experiment_id,
        "dm1_sent_at": None,
        "dm2_sent_at": None,
        "dm3_sent_at": None,
    }
    entry.update(extra)
    return entry


def _run(
    monkeypatch,
    *,
    events,
    entries,
    dry_run=False,
    capture=None,
    tmp=None,
    leads_unread=0,
):
    """Run ingest with a fake sender + injected entries; capture advances."""
    if capture is None:
        capture = MagicMock(return_value=True)
    monkeypatch.setattr(
        "workflows.daily_check._attio_advance_with_escalation", capture
    )
    if tmp is not None:
        # Cursor state the test asserts on directly. Without `tmp` the
        # autouse fixture's per-test state dir is already in force, so no
        # run here can ever touch the real ~/.outbound-agent.
        monkeypatch.setattr(botdog_ingest, "POLL_STATE_DIR", tmp)
        monkeypatch.setattr(botdog_ingest, "POLL_STATE_FILE", tmp / "botdog_poll.json")
    sender = _FakeSender(events, leads_unread=leads_unread)
    report = ingest_botdog_events(
        MagicMock(),
        sender=sender,
        dry_run=dry_run,
        now=datetime(2026, 6, 20, 9, 0, tzinfo=UTC),
        entries_provider=lambda attio: entries,
    )
    return report, capture, sender


def _last_attrs(capture) -> dict:
    return capture.call_args.kwargs["entry_attributes"]


# ── event → attr translation ──────────────────────────────────────────


class TestTranslation:
    def test_invitation_sent_confirms_connection_sent(self, monkeypatch):
        entry = _entry(stage=PipelineStage.PROSPECT.value)
        report, cap, _ = _run(
            monkeypatch,
            events=[_ev("LEAD_INVITATION_SENT", URL_ALICE, 15)],
            entries=[entry],
        )
        attrs = _last_attrs(cap)
        assert attrs["stage"] == PipelineStage.CONNECTION_SENT.value
        assert attrs["last_contact_date"] == "2026-06-15"
        assert report["applied"] == 1

    def test_invitation_accepted_flips_accepted_and_freezes_experiment(
        self, monkeypatch
    ):
        entry = _entry(
            stage=PipelineStage.CONNECTION_SENT.value, experiment_id="exp-7"
        )
        report, cap, _ = _run(
            monkeypatch,
            events=[_ev("LEAD_INVITATION_ACCEPTED", URL_ALICE, 15)],
            entries=[entry],
        )
        attrs = _last_attrs(cap)
        assert attrs["stage"] == PipelineStage.ACCEPTED.value
        # mirrors _phase0_accepted_update: frozen_at stamped when experiment set
        assert attrs["experiment_id_frozen_at"] == "accepted"
        assert cap.call_args.kwargs["writer_module"] == (
            "workflows.botdog_ingest.apply_lead_events"
        )

    def test_accepted_without_experiment_flips_stage_only(self, monkeypatch):
        entry = _entry(stage=PipelineStage.CONNECTION_SENT.value, experiment_id=None)
        _report, cap, _ = _run(
            monkeypatch,
            events=[_ev("LEAD_INVITATION_ACCEPTED", URL_ALICE, 15)],
            entries=[entry],
        )
        attrs = _last_attrs(cap)
        assert attrs["stage"] == PipelineStage.ACCEPTED.value
        assert "experiment_id_frozen_at" not in attrs

    def test_message_sent_advances_accepted_to_dm1(self, monkeypatch):
        entry = _entry(stage=PipelineStage.ACCEPTED.value, dm_step="dm0")
        report, cap, _ = _run(
            monkeypatch,
            events=[_ev("LEAD_MESSAGE_SENT", URL_ALICE, 15)],
            entries=[entry],
        )
        attrs = _last_attrs(cap)
        assert attrs["stage"] == PipelineStage.DM1_SENT.value
        assert attrs["dm_step"] == 1
        assert attrs["dm1_sent_at"] == "2026-06-15"
        assert attrs["last_contact_date"] == "2026-06-15"
        assert "next_eligible_send_date" in attrs
        assert report["applied"] == 1

    def test_message_sent_catch_up_two_steps(self, monkeypatch):
        """Missed runs: DM1 already recorded, two new message-sent events
        (DM2 then DM3) advance to DM3_SENT stamping both intermediate dates."""
        entry = _entry(
            stage=PipelineStage.DM1_SENT.value,
            dm_step="dm1",
            dm1_sent_at="2026-06-10",
        )
        _report, cap, _ = _run(
            monkeypatch,
            events=[
                _ev("LEAD_MESSAGE_SENT", URL_ALICE, 15),
                _ev("LEAD_MESSAGE_SENT", URL_ALICE, 18),
            ],
            entries=[entry],
        )
        attrs = _last_attrs(cap)
        assert attrs["stage"] == PipelineStage.DM3_SENT.value
        assert attrs["dm_step"] == 3
        assert attrs["dm2_sent_at"] == "2026-06-15"
        assert attrs["dm3_sent_at"] == "2026-06-18"

    def test_message_sent_at_dm3_is_cadence_complete_noop(self, monkeypatch):
        entry = _entry(
            stage=PipelineStage.DM3_SENT.value, dm_step="dm3", dm3_sent_at="2026-06-10"
        )
        report, cap, _ = _run(
            monkeypatch,
            events=[_ev("LEAD_MESSAGE_SENT", URL_ALICE, 15)],
            entries=[entry],
        )
        cap.assert_not_called()
        assert report["cadence_complete_noop"] == 1
        assert report["applied"] == 0

    def test_message_replied_moves_to_responded(self, monkeypatch):
        entry = _entry(stage=PipelineStage.DM1_SENT.value, dm_step="dm1")
        report, cap, _ = _run(
            monkeypatch,
            events=[_ev("LEAD_MESSAGE_REPLIED", URL_ALICE, 15)],
            entries=[entry],
        )
        attrs = _last_attrs(cap)
        assert attrs["stage"] == PipelineStage.RESPONDED.value
        assert attrs["response_received_at"] == "2026-06-15"
        assert report["applied"] == 1


# ── scope guard ───────────────────────────────────────────────────────


class TestScopeGuard:
    def test_pb_channel_entry_is_skipped(self, monkeypatch):
        entry = _entry(stage=PipelineStage.CONNECTION_SENT.value, send_channel="pb")
        report, cap, _ = _run(
            monkeypatch,
            events=[_ev("LEAD_INVITATION_ACCEPTED", URL_ALICE, 15)],
            entries=[entry],
        )
        cap.assert_not_called()
        assert report["skipped_scope_pb"] == 1
        assert report["applied"] == 0

    def test_none_channel_entry_is_skipped(self, monkeypatch):
        entry = _entry(stage=PipelineStage.CONNECTION_SENT.value, send_channel=None)
        report, cap, _ = _run(
            monkeypatch,
            events=[_ev("LEAD_INVITATION_ACCEPTED", URL_ALICE, 15)],
            entries=[entry],
        )
        cap.assert_not_called()
        assert report["skipped_scope_pb"] == 1

    def test_unmatched_url_is_skipped(self, monkeypatch):
        entry = _entry(url=URL_BOB)
        report, cap, _ = _run(
            monkeypatch,
            events=[_ev("LEAD_INVITATION_ACCEPTED", URL_ALICE, 15)],
            entries=[entry],
        )
        cap.assert_not_called()
        assert report["skipped_scope_unmatched"] == 1

    def test_botdog_entry_applies(self, monkeypatch):
        entry = _entry(stage=PipelineStage.CONNECTION_SENT.value, send_channel="botdog")
        report, cap, _ = _run(
            monkeypatch,
            events=[_ev("LEAD_INVITATION_ACCEPTED", URL_ALICE, 15)],
            entries=[entry],
        )
        cap.assert_called_once()
        assert report["applied"] == 1


# ── idempotency ───────────────────────────────────────────────────────


class TestIdempotency:
    def test_already_accepted_reapply_is_noop(self, monkeypatch):
        entry = _entry(stage=PipelineStage.ACCEPTED.value)
        report, cap, _ = _run(
            monkeypatch,
            events=[_ev("LEAD_INVITATION_ACCEPTED", URL_ALICE, 15)],
            entries=[entry],
        )
        cap.assert_not_called()
        assert report["skipped_idempotent"] == 1
        assert report["applied"] == 0

    def test_message_sent_reapply_same_date_is_noop(self, monkeypatch):
        """DM1 already recorded at the event's date → re-poll adds nothing."""
        entry = _entry(
            stage=PipelineStage.DM1_SENT.value,
            dm_step="dm1",
            dm1_sent_at="2026-06-15",
        )
        report, cap, _ = _run(
            monkeypatch,
            events=[_ev("LEAD_MESSAGE_SENT", URL_ALICE, 15)],
            entries=[entry],
        )
        cap.assert_not_called()
        assert report["applied"] == 0
        assert report["skipped_idempotent"] == 1

    def test_replied_already_responded_is_noop(self, monkeypatch):
        entry = _entry(stage=PipelineStage.RESPONDED.value, dm_step="dm1")
        report, cap, _ = _run(
            monkeypatch,
            events=[_ev("LEAD_MESSAGE_REPLIED", URL_ALICE, 15)],
            entries=[entry],
        )
        cap.assert_not_called()
        assert report["skipped_idempotent"] == 1


# ── unknown / non-campaign events ─────────────────────────────────────


class TestUnknownEvents:
    def test_noncampaign_inbox_counted_not_applied(self, monkeypatch):
        entry = _entry(stage=PipelineStage.DM1_SENT.value, dm_step="dm1")
        report, cap, _ = _run(
            monkeypatch,
            events=[
                _ev(
                    "NON_CAMPAIGN_LEAD_INBOX_MESSAGE_RECEIVED",
                    URL_ALICE,
                    15,
                )
            ],
            entries=[entry],
        )
        cap.assert_not_called()
        assert report["noncampaign_events"] == 1
        assert report["applied"] == 0

    def test_unknown_type_counted_not_applied(self, monkeypatch):
        entry = _entry(stage=PipelineStage.DM1_SENT.value, dm_step="dm1")
        report, cap, _ = _run(
            monkeypatch,
            events=[_ev("SOME_FUTURE_EVENT", URL_ALICE, 15)],
            entries=[entry],
        )
        cap.assert_not_called()
        assert report["unknown_events"] == 1
        assert report["applied"] == 0

    def test_withdrawn_counted_never_unknown_never_applied(
        self, monkeypatch, capsys
    ):
        """A withdrawal is a KNOWN, non-advancing event: counted with its
        own report key + a loud line, no CRM change, and never folded
        into `unknown_events` (that counter is the vocabulary-drift
        alarm, and burying a known type in it would hide real drift)."""
        entry = _entry(stage=PipelineStage.CONNECTION_SENT.value)
        report, cap, _ = _run(
            monkeypatch,
            events=[_ev("LEAD_INVITATION_WITHDRAWN", URL_ALICE, 15)],
            entries=[entry],
        )
        cap.assert_not_called()
        assert report["invitation_withdrawn"] == 1
        assert report["unknown_events"] == 0
        assert report["applied"] == 0
        assert report["invitation_withdrawn_urls"] == [URL_ALICE]
        assert "invitation withdrawal event(s)" in capsys.readouterr().err
        assert "withdrawn=1" in botdog_ingest.format_report(report)

    def test_withdrawn_does_not_block_a_real_advance(self, monkeypatch):
        """Withdrawn is non-advancing, not poisonous: an accept arriving
        alongside it still applies."""
        entry = _entry(stage=PipelineStage.CONNECTION_SENT.value)
        report, cap, _ = _run(
            monkeypatch,
            events=[
                _ev("LEAD_INVITATION_WITHDRAWN", URL_ALICE, 14),
                _ev("LEAD_INVITATION_ACCEPTED", URL_ALICE, 15),
            ],
            entries=[entry],
        )
        assert report["invitation_withdrawn"] == 1
        assert report["applied"] == 1
        assert cap.call_count == 1

    def test_events_by_type_report(self, monkeypatch):
        entry = _entry(stage=PipelineStage.CONNECTION_SENT.value)
        report, _cap, _ = _run(
            monkeypatch,
            events=[
                _ev("LEAD_INVITATION_ACCEPTED", URL_ALICE, 15),
                _ev("SOME_FUTURE_EVENT", URL_ALICE, 15),
            ],
            entries=[entry],
        )
        assert report["events_by_type"]["LEAD_INVITATION_ACCEPTED"] == 1
        assert report["events_by_type"]["SOME_FUTURE_EVENT"] == 1
        assert report["events_total"] == 2


# ── dry-run ───────────────────────────────────────────────────────────


class TestDryRun:
    def test_dry_run_writes_nothing_and_reports(self, monkeypatch, tmp_path):
        entry = _entry(stage=PipelineStage.CONNECTION_SENT.value)
        report, cap, _ = _run(
            monkeypatch,
            events=[_ev("LEAD_INVITATION_ACCEPTED", URL_ALICE, 15)],
            entries=[entry],
            dry_run=True,
            tmp=tmp_path,
        )
        # No real advance call; would-apply counted.
        cap.assert_not_called()
        assert report["applied"] == 1
        assert report["dry_run"] is True
        # Cursor NOT advanced on dry-run — we never move past state we
        # only previewed.
        assert not (tmp_path / "botdog_poll.json").exists()
        assert report["cursor_after"] is None


# ── disabled transport ────────────────────────────────────────────────


class TestTransportDisabled:
    """`enabled: false` in the operator's config means every Botdog
    surface is inert. The builder answers None; the drain must then touch
    nothing — no CRM read, no cursor move — rather than crash or poll."""

    def test_none_sender_short_circuits_before_any_work(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(botdog_ingest, "POLL_STATE_DIR", tmp_path)
        monkeypatch.setattr(
            botdog_ingest, "POLL_STATE_FILE", tmp_path / "botdog_poll.json"
        )
        monkeypatch.setattr(
            "workflows.daily_check._build_botdog_sender", lambda: None
        )
        entries = MagicMock(side_effect=AssertionError("must not read CRM"))

        report = ingest_botdog_events(
            MagicMock(),
            now=datetime(2026, 6, 20, 9, 0, tzinfo=UTC),
            entries_provider=entries,
        )

        assert report["transport_disabled"] is True
        assert report["cursor_after"] is None
        assert not (tmp_path / "botdog_poll.json").exists()
        entries.assert_not_called()
        assert "SKIPPED" in botdog_ingest.format_report(report)


# ── cursor read/write/catch-up ────────────────────────────────────────


class TestCursor:
    def test_write_then_read_roundtrip(self, monkeypatch, tmp_path):
        monkeypatch.setattr(botdog_ingest, "POLL_STATE_DIR", tmp_path)
        monkeypatch.setattr(
            botdog_ingest, "POLL_STATE_FILE", tmp_path / "botdog_poll.json"
        )
        when = datetime(2026, 6, 20, 9, 0, tzinfo=UTC)
        botdog_ingest.write_cursor(when)
        assert botdog_ingest.read_cursor() == when

    def test_missing_cursor_is_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            botdog_ingest, "POLL_STATE_FILE", tmp_path / "nope.json"
        )
        assert botdog_ingest.read_cursor() is None

    def test_corrupt_cursor_is_none(self, monkeypatch, tmp_path):
        """A lost cursor must degrade to a full catch-up poll, never a
        crash: application is idempotent, so over-polling is free."""
        f = tmp_path / "botdog_poll.json"
        f.write_text("{not json")
        monkeypatch.setattr(botdog_ingest, "POLL_STATE_FILE", f)
        assert botdog_ingest.read_cursor() is None

    def test_cursor_passed_as_since_and_advanced(self, monkeypatch, tmp_path):
        monkeypatch.setattr(botdog_ingest, "POLL_STATE_DIR", tmp_path)
        monkeypatch.setattr(
            botdog_ingest, "POLL_STATE_FILE", tmp_path / "botdog_poll.json"
        )
        prior = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
        botdog_ingest.write_cursor(prior)
        report, _cap, sender = _run(
            monkeypatch,
            events=[],
            entries=[],
            tmp=tmp_path,
        )
        # Overlap poll: fetch_events is called with the stored cursor
        # MINUS the overlap window, while the cursor bookkeeping itself
        # is unchanged (both are reported).
        assert sender.since_calls == [prior - botdog_ingest.BOTDOG_POLL_OVERLAP]
        assert report["poll_since"] == (
            prior - botdog_ingest.BOTDOG_POLL_OVERLAP
        ).isoformat()
        # cursor advanced to `now`.
        assert report["cursor_after"] == "2026-06-20T09:00:00+00:00"
        assert report["cursor_before"] == "2026-06-01T00:00:00+00:00"
        assert botdog_ingest.read_cursor() == datetime(
            2026, 6, 20, 9, 0, tzinfo=UTC
        )

    def test_catch_up_first_run_polls_since_none(self, monkeypatch, tmp_path):
        report, _cap, sender = _run(
            monkeypatch, events=[], entries=[], tmp=tmp_path
        )
        assert sender.since_calls == [None]
        assert report["cursor_before"] is None


# ── failure surfacing ─────────────────────────────────────────────────


class TestFailures:
    def test_write_failure_counted_not_swallowed(self, monkeypatch):
        entry = _entry(stage=PipelineStage.CONNECTION_SENT.value)
        capture = MagicMock(return_value=False)  # advance failed / DLQ'd
        report, cap, _ = _run(
            monkeypatch,
            events=[_ev("LEAD_INVITATION_ACCEPTED", URL_ALICE, 15)],
            entries=[entry],
            capture=capture,
        )
        assert report["failures"] == 1
        assert report["applied"] == 0

    def test_failed_advance_holds_cursor_for_retry(self, monkeypatch, tmp_path):
        """A failed advance must NOT let the cursor jump to `now` — else the
        event (occurred < now) is never re-fetched and the advance is lost.
        The cursor is held at the earliest failed event's timestamp."""
        entry = _entry(stage=PipelineStage.CONNECTION_SENT.value)
        capture = MagicMock(return_value=False)  # advance fails
        report, _cap, _ = _run(
            monkeypatch,
            events=[_ev("LEAD_INVITATION_ACCEPTED", URL_ALICE, 15)],
            entries=[entry],
            capture=capture,
            tmp=tmp_path,
        )
        assert report["failures"] == 1
        # Held at the failed event's ts (2026-06-15T12:00), NOT now (06-20T09).
        assert report["cursor_after"] == "2026-06-15T12:00:00+00:00"
        assert botdog_ingest.read_cursor() == datetime(
            2026, 6, 15, 12, 0, tzinfo=UTC
        )

    def test_clean_run_advances_cursor_to_now(self, monkeypatch, tmp_path):
        """No failures → cursor advances fully to `now` (no unnecessary
        re-polling of already-applied events)."""
        entry = _entry(stage=PipelineStage.CONNECTION_SENT.value)
        report, _cap, _ = _run(
            monkeypatch,
            events=[_ev("LEAD_INVITATION_ACCEPTED", URL_ALICE, 15)],
            entries=[entry],
            tmp=tmp_path,
        )
        assert report["failures"] == 0
        assert report["cursor_after"] == "2026-06-20T09:00:00+00:00"


# ── scope-skip watermark + hold cap ───────────────────────────────────


class TestScopeSkipWatermark:
    """A scope-skipped actionable event is usually a RACE (the CRM row
    hasn't been stamped `send_channel=botdog` yet, or its person record is
    still being created), so its timestamp holds the cursor exactly like a
    failed advance — otherwise the accept/reply is polled once, skipped,
    and lost forever."""

    def test_scope_skipped_pb_event_holds_cursor(self, monkeypatch, tmp_path):
        entry = _entry(
            stage=PipelineStage.CONNECTION_SENT.value, send_channel="pb"
        )
        report, _cap, _ = _run(
            monkeypatch,
            events=[_ev("LEAD_INVITATION_ACCEPTED", URL_ALICE, 15)],
            entries=[entry],
            tmp=tmp_path,
        )
        assert report["skipped_scope_pb"] == 1
        assert report["cursor_after"] == "2026-06-15T12:00:00+00:00"

    def test_unmatched_url_event_holds_cursor(self, monkeypatch, tmp_path):
        report, _cap, _ = _run(
            monkeypatch,
            events=[_ev("LEAD_INVITATION_ACCEPTED", URL_ALICE, 15)],
            entries=[_entry(url=URL_BOB)],
            tmp=tmp_path,
        )
        assert report["skipped_scope_unmatched"] == 1
        assert report["cursor_after"] == "2026-06-15T12:00:00+00:00"

    def test_hold_cap_releases_with_loud_warning(
        self, monkeypatch, tmp_path, capsys
    ):
        """A lead that can never match (added by hand in the transport, so
        no CRM row will ever exist) must not pin the cursor forever — the
        cap releases it, loudly and counted, because release is a real
        loss of events."""
        # Events 30 days before `now` (2026-06-20) — far past the 7-day cap.
        report, _cap, _ = _run(
            monkeypatch,
            events=[
                _ev("LEAD_INVITATION_ACCEPTED", URL_GHOST, 21, month=5),
                _ev("LEAD_MESSAGE_REPLIED", URL_GHOST, 22, month=5),
            ],
            entries=[],
            tmp=tmp_path,
        )
        floor = datetime(2026, 6, 20, 9, 0, tzinfo=UTC) - timedelta(
            days=botdog_ingest.BOTDOG_MAX_CURSOR_HOLD_DAYS
        )
        assert report["cursor_hold_capped"] is True
        assert report["cursor_released_events"] == 2
        assert report["cursor_after"] == floor.isoformat()
        err = capsys.readouterr().err
        assert "cursor hold CAP reached" in err
        assert "beyond recovery" in err

    def test_hold_within_cap_is_not_released(self, monkeypatch, tmp_path):
        """Inside the cap window the watermark still holds fully."""
        report, _cap, _ = _run(
            monkeypatch,
            events=[_ev("LEAD_INVITATION_ACCEPTED", URL_DANA, 18)],
            entries=[],
            tmp=tmp_path,
        )
        assert report["cursor_hold_capped"] is False
        assert report["cursor_after"] == "2026-06-18T12:00:00+00:00"


# ── leads the transport never read ────────────────────────────────────


class TestUnreadLeadHold:
    """A lead the transport could not READ (detail-fetch budget, failed
    detail GET) emits NO events, so the failure/scope-skip watermark has
    nothing to hold with. Advancing the cursor to `now` anyway would put
    that lead's real accept/reply permanently below the next poll's floor
    — a total, silent loss with `failures=0` in the report. The poll's
    `leads_unread` signal therefore pins the cursor."""

    def test_unread_leads_hold_cursor_at_previous_value(
        self, monkeypatch, tmp_path
    ):
        prior = datetime(2026, 6, 18, tzinfo=UTC)  # inside the hold cap
        monkeypatch.setattr(botdog_ingest, "POLL_STATE_DIR", tmp_path)
        monkeypatch.setattr(
            botdog_ingest, "POLL_STATE_FILE", tmp_path / "botdog_poll.json"
        )
        botdog_ingest.write_cursor(prior)

        report, _cap, _ = _run(
            monkeypatch,
            events=[],
            entries=[],
            tmp=tmp_path,
            leads_unread=3,
        )

        assert report["leads_unread"] == 3
        # Held at the last point we know was fully read — NOT `now`.
        assert report["cursor_after"] == prior.isoformat()
        assert botdog_ingest.read_cursor() == prior

    def test_applied_events_still_apply_while_the_cursor_is_held(
        self, monkeypatch, tmp_path
    ):
        """Holding the cursor must not block this run's work — the
        readable leads still advance; only the cursor waits."""
        prior = datetime(2026, 6, 18, tzinfo=UTC)  # inside the hold cap
        monkeypatch.setattr(botdog_ingest, "POLL_STATE_DIR", tmp_path)
        monkeypatch.setattr(
            botdog_ingest, "POLL_STATE_FILE", tmp_path / "botdog_poll.json"
        )
        botdog_ingest.write_cursor(prior)

        report, cap, _ = _run(
            monkeypatch,
            events=[_ev("LEAD_INVITATION_ACCEPTED", URL_ALICE, 15)],
            entries=[_entry(stage=PipelineStage.CONNECTION_SENT.value)],
            tmp=tmp_path,
            leads_unread=1,
        )

        assert report["applied"] == 1
        assert cap.called
        assert report["cursor_after"] == prior.isoformat()

    def test_unread_leads_on_unbounded_first_poll_write_no_cursor(
        self, monkeypatch, tmp_path, capsys
    ):
        """With no prior cursor the poll was unbounded. Writing ANY
        cursor now would put a floor under the next poll — below which the
        unread leads' events sit forever. So nothing is written."""
        report, _cap, _ = _run(
            monkeypatch,
            events=[],
            entries=[],
            tmp=tmp_path,
            leads_unread=2,
        )

        assert report["cursor_write_skipped"] is True
        assert report["cursor_after"] is None
        assert botdog_ingest.read_cursor() is None
        assert "cursor NOT written" in capsys.readouterr().err

    def test_unread_hold_is_still_bounded_by_the_hold_cap(
        self, monkeypatch, tmp_path
    ):
        """A permanently unreadable lead must not pin the cursor forever
        — the same cap that bounds the failure watermark bounds this."""
        prior = datetime(2026, 5, 1, tzinfo=UTC)  # 50 days before `now`
        monkeypatch.setattr(botdog_ingest, "POLL_STATE_DIR", tmp_path)
        monkeypatch.setattr(
            botdog_ingest, "POLL_STATE_FILE", tmp_path / "botdog_poll.json"
        )
        botdog_ingest.write_cursor(prior)

        report, _cap, _ = _run(
            monkeypatch,
            events=[],
            entries=[],
            tmp=tmp_path,
            leads_unread=1,
        )

        floor = datetime(2026, 6, 20, 9, 0, tzinfo=UTC) - timedelta(
            days=botdog_ingest.BOTDOG_MAX_CURSOR_HOLD_DAYS
        )
        assert report["cursor_hold_capped"] is True
        assert report["cursor_after"] == floor.isoformat()

    def test_incomplete_poll_is_loud_and_in_the_report_line(
        self, monkeypatch, tmp_path, capsys
    ):
        report, _cap, _ = _run(
            monkeypatch,
            events=[],
            entries=[],
            tmp=tmp_path,
            leads_unread=4,
        )
        assert "poll INCOMPLETE" in capsys.readouterr().err
        line = botdog_ingest.format_report(report)
        assert "leads-unread=4" in line
        assert "INCOMPLETE poll" in line

    def test_dry_run_never_writes_a_cursor_even_with_unread_leads(
        self, monkeypatch, tmp_path
    ):
        report, _cap, _ = _run(
            monkeypatch,
            events=[],
            entries=[],
            tmp=tmp_path,
            dry_run=True,
            leads_unread=2,
        )
        assert report["leads_unread"] == 2
        assert report["cursor_after"] is None
        assert report["cursor_write_skipped"] is False
        assert botdog_ingest.read_cursor() is None

    def test_transport_without_the_signal_advances_normally(
        self, monkeypatch, tmp_path
    ):
        """A plain list (a transport with no completeness signal, e.g. PB)
        means nothing was left unread — the cursor advances as before."""
        report, _cap, _ = _run(
            monkeypatch, events=[], entries=[], tmp=tmp_path
        )
        assert report["leads_unread"] == 0
        assert report["cursor_after"] == "2026-06-20T09:00:00+00:00"


# ── naive timestamps must not crash the watermark math ────────────────


class TestNaiveTimestampTolerance:
    def test_naive_event_time_does_not_crash_watermark(
        self, monkeypatch, tmp_path
    ):
        """A naive occurred_at mixed with aware ones would raise TypeError
        in sorted()/min() and abort the entire poll — every datetime
        crossing into the workflow is coerced to UTC-aware instead."""
        naive = LeadEvent(
            event_type="LEAD_MESSAGE_SENT",
            lead_linkedin_url=URL_ALICE,
            occurred_at=datetime(2026, 6, 15, 12, 0),  # noqa: DTZ001 — the point
            raw={},
        )
        aware = _ev("LEAD_MESSAGE_SENT", URL_ALICE, 16)
        entry = _entry(stage=PipelineStage.ACCEPTED.value, dm_step="dm0")
        capture = MagicMock(return_value=False)  # force the watermark path
        report, _cap, _ = _run(
            monkeypatch,
            events=[naive, aware],
            entries=[entry],
            capture=capture,
            tmp=tmp_path,
        )
        assert report["failures"] == 1
        assert report["cursor_after"] == "2026-06-15T12:00:00+00:00"


# ── ledger crosscheck on message-sent ─────────────────────────────────


class TestLedgerCrosscheck:
    def test_ledgered_advance_is_quiet(self, monkeypatch, capsys):
        botdog_ledger.record_submission(URL_ALICE, "dm1", date(2026, 6, 14))
        entry = _entry(stage=PipelineStage.ACCEPTED.value, dm_step="dm0")
        report, cap, _ = _run(
            monkeypatch,
            events=[_ev("LEAD_MESSAGE_SENT", URL_ALICE, 15)],
            entries=[entry],
        )
        cap.assert_called_once()
        assert report["unledgered_message_sent"] == 0
        assert "no local submission ledger entry" not in capsys.readouterr().err

    def test_unledgered_advance_applies_but_counts_loudly(
        self, monkeypatch, capsys
    ):
        """A send we never submitted (manual/conversational) still consumes
        the prospect's cadence — advance, but make the operator see that
        their cadence is being driven by something outside the run log.
        Visibility, not blocking: the ledger is machine-local, so a fresh
        checkout or a second seat legitimately has no record, and refusing
        to advance would wedge prospects mid-cadence."""
        entry = _entry(stage=PipelineStage.ACCEPTED.value, dm_step="dm0")
        report, cap, _ = _run(
            monkeypatch,
            events=[_ev("LEAD_MESSAGE_SENT", URL_ALICE, 15)],
            entries=[entry],
        )
        cap.assert_called_once()  # NOT blocked
        assert report["applied"] == 1
        assert report["unledgered_message_sent"] == 1
        assert report["unledgered_message_sent_urls"] == [f"{URL_ALICE} (dm1)"]
        assert "no local submission ledger entry" in capsys.readouterr().err

    def test_catch_up_counts_each_unledgered_step(self, monkeypatch):
        botdog_ledger.record_submission(URL_ALICE, "dm2", date(2026, 6, 14))
        entry = _entry(
            stage=PipelineStage.DM1_SENT.value,
            dm_step="dm1",
            dm1_sent_at="2026-06-10",
        )
        report, _cap, _ = _run(
            monkeypatch,
            events=[
                _ev("LEAD_MESSAGE_SENT", URL_ALICE, 15),
                _ev("LEAD_MESSAGE_SENT", URL_ALICE, 18),
            ],
            entries=[entry],
        )
        # dm2 is ledgered, dm3 is not.
        assert report["unledgered_message_sent"] == 1
        assert report["unledgered_message_sent_urls"] == [f"{URL_ALICE} (dm3)"]


# ── invitation-sent freezes the experiment cohort ─────────────────────


class TestInvitationSentFrozenAt:
    """Parity with the PhantomBuster invite path. Botdog stamps
    experiment_id at submission time; the invitation-sent event is the
    confirmation the invite really went out, so it is the correct freeze
    point. Without it, botdog-cohort rows would be measured against a
    different freeze boundary than their PB counterparts and every A/B
    comparison would silently skew."""

    def test_experiment_row_freezes_at_connection_sent(self, monkeypatch):
        entry = _entry(
            stage=PipelineStage.PROSPECT.value, experiment_id="exp-7"
        )
        _report, cap, _ = _run(
            monkeypatch,
            events=[_ev("LEAD_INVITATION_SENT", URL_ALICE, 15)],
            entries=[entry],
        )
        attrs = _last_attrs(cap)
        assert attrs["stage"] == PipelineStage.CONNECTION_SENT.value
        assert attrs["experiment_id_frozen_at"] == "connection_sent"

    def test_no_experiment_row_gets_no_stamp(self, monkeypatch):
        entry = _entry(stage=PipelineStage.PROSPECT.value, experiment_id=None)
        _report, cap, _ = _run(
            monkeypatch,
            events=[_ev("LEAD_INVITATION_SENT", URL_ALICE, 15)],
            entries=[entry],
        )
        assert "experiment_id_frozen_at" not in _last_attrs(cap)

    def test_already_frozen_row_is_not_restamped(self, monkeypatch):
        """Cohort immutability — mirrors the PB invite path's guard."""
        entry = _entry(
            stage=PipelineStage.PROSPECT.value,
            experiment_id="exp-7",
            experiment_id_frozen_at="accepted",
        )
        _report, cap, _ = _run(
            monkeypatch,
            events=[_ev("LEAD_INVITATION_SENT", URL_ALICE, 15)],
            entries=[entry],
        )
        assert "experiment_id_frozen_at" not in _last_attrs(cap)

    def test_registry_authorizes_the_writer(self):
        """Every attribute this workflow advances must name it as an
        authorized writer, or AttioWriter refuses the write at runtime and
        the drain silently advances nothing."""
        from clients.attio_writer_registry import is_authorized_writer

        for slug in (
            "stage",
            "dm_step",
            "dm1_sent_at",
            "dm2_sent_at",
            "dm3_sent_at",
            "last_contact_date",
            "next_eligible_send_date",
            "response_received_at",
            "experiment_id_frozen_at",
        ):
            assert is_authorized_writer(
                "linkedin_outreach", slug, botdog_ingest.WRITER_MODULE
            ), f"{slug} does not authorize {botdog_ingest.WRITER_MODULE}"


# ── staleness watchdog (report-only) ──────────────────────────────────


class TestStaleWatchdog:
    """Botdog-channel rows are held out of the scrape-based phases, so the
    scrape-driven stale nets never see them: a silently-stopped campaign
    or a lost event would age forever with nothing watching. Report-only
    by design — a wrong automated action on a prospect-facing cadence is
    worse than a loud counter."""

    def test_stale_invite_counted(self, monkeypatch, capsys):
        entry = _entry(
            stage=PipelineStage.CONNECTION_SENT.value,
            last_contact_date="2026-06-01",  # 19 days before `now`
        )
        report, cap, _ = _run(monkeypatch, events=[], entries=[entry])
        cap.assert_not_called()  # report-only, no writes
        assert report["stale_botdog_invites"] == 1
        assert report["stale_botdog_invite_urls"] == [
            f"{URL_ALICE} (sent 2026-06-01)"
        ]
        assert "still\n" not in capsys.readouterr().err  # sanity: no crash

    def test_fresh_invite_not_counted(self, monkeypatch):
        entry = _entry(
            stage=PipelineStage.CONNECTION_SENT.value,
            last_contact_date="2026-06-18",
        )
        report, _cap, _ = _run(monkeypatch, events=[], entries=[entry])
        assert report["stale_botdog_invites"] == 0

    def test_pb_channel_invite_is_not_watched(self, monkeypatch):
        """PB rows have their own stale nets — don't double-report."""
        entry = _entry(
            stage=PipelineStage.CONNECTION_SENT.value,
            send_channel="pb",
            last_contact_date="2026-06-01",
        )
        report, _cap, _ = _run(monkeypatch, events=[], entries=[entry])
        assert report["stale_botdog_invites"] == 0

    def test_stale_dm_submission_counted(self, monkeypatch, capsys):
        """A submitted DM whose message-sent event never arrived leaves the
        prospect frozen: the duplicate-send guard (correctly) blocks a
        re-send, which is exactly what makes the silence dangerous."""
        botdog_ledger.record_submission(URL_ALICE, "dm1", date(2026, 6, 5))
        entry = _entry(stage=PipelineStage.ACCEPTED.value, dm_step="dm0")
        report, _cap, _ = _run(monkeypatch, events=[], entries=[entry])
        assert report["stale_botdog_dms"] == 1
        assert "FROZEN mid-cadence" in capsys.readouterr().err

    def test_confirmed_dm_is_not_stale(self, monkeypatch):
        botdog_ledger.record_submission(URL_ALICE, "dm1", date(2026, 6, 5))
        entry = _entry(
            stage=PipelineStage.DM1_SENT.value,
            dm_step="dm1",
            dm1_sent_at="2026-06-06",  # the event landed
        )
        report, _cap, _ = _run(monkeypatch, events=[], entries=[entry])
        assert report["stale_botdog_dms"] == 0

    def test_recent_dm_submission_inside_window_is_not_stale(
        self, monkeypatch
    ):
        botdog_ledger.record_submission(URL_ALICE, "dm1", date(2026, 6, 19))
        entry = _entry(stage=PipelineStage.ACCEPTED.value, dm_step="dm0")
        report, _cap, _ = _run(monkeypatch, events=[], entries=[entry])
        assert report["stale_botdog_dms"] == 0

    def test_format_report_surfaces_stale_buckets(self):
        text = botdog_ingest.format_report({
            "stale_botdog_invites": 2,
            "stale_botdog_invite_urls": ["a", "b"],
            "stale_botdog_dms": 1,
            "stale_botdog_dm_urls": ["c"],
            "unledgered_message_sent": 3,
            "unledgered_message_sent_urls": ["d"],
            "cursor_hold_capped": True,
            "cursor_released_events": 4,
        })
        assert "stale-invites=2" in text
        assert "stale-dms=1" in text
        assert "unledgered-msg=3" in text
        assert "beyond recovery" in text


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
