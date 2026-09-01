"""BotdogSender — the submission-only alternative transport.

Exercises `BotdogSender` against a mocked `BotdogClient`: campaign-role
resolution, the `customAttributes` payload, the idempotency pre-check,
per-lead / per-chunk failure handling, ghost-submission reconciliation,
DM lead resolution + the campaign-fallback seam, and the DETAIL-based
`fetch_events` derivation (list rows carry no events — see
`TestFetchEvents`). Every call goes through the injected client mock — no
HTTP, no live API, no operator config.

`PBSender`'s conformance to the same `Sender` protocol is pinned
separately in `tests/test_sender_protocol.py`.
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from clients import sender as sender_mod
from clients.botdog import BotdogError
from clients.botdog_config import BotdogConfig
from clients.pb_envelope import _normalize_url_for_match as _norm
from clients.sender import (
    BOTDOG_INVITE_NOTE_VARIABLE,
    BotdogSender,
    BotdogSubmitOutcome,
    _lead_url,
    invite_campaign_roles,
)

# Synthetic prospect identities — this repo is public; no real prospect,
# company, or campaign identifier ever appears in a test.
_ACME_PEOPLE = ("alice", "bob", "carol", "dave")


def _url(i: int) -> str:
    return f"https://linkedin.com/in/acme-{_ACME_PEOPLE[i % len(_ACME_PEOPLE)]}"


def _campaigns(mapping: dict[str, str]):
    """A `campaign_id_for_role` accessor, the shape `BotdogConfig.campaign_id`
    has: role slug in, campaign id or None out."""
    return lambda role: mapping.get(role)


def _batch_result(raw=None, lead_results=None, *, urls=None):
    """Fake `add_leads_to_campaign` result.

    `urls` builds a per-lead body whose COUNT matches the chunk, which is
    what keeps `send_invites` on the per-lead classification path. A bare
    `_batch_result()` (empty `lead_results`) is the unverified-shape case
    and triggers the membership reconciliation instead.
    """
    r = MagicMock()
    r.raw = raw if raw is not None else {}
    if lead_results is None and urls is not None:
        lead_results = [{"linkedinUrl": u, "status": "ok"} for u in urls]
    r.lead_results = lead_results or []
    return r


def _invite_row(i: int, *, language: str = "es", message: str = "hola"):
    return {
        "linkedInUrl": _url(i),
        "name": _ACME_PEOPLE[i % len(_ACME_PEOPLE)].title(),
        "title": "VP Operations",
        "company": "Acme Foods",
        "language": language,
        "message": message,
    }


class TestInviteNoteVariableContract:
    """The invite-note variable is half of a contract whose other half
    lives in the Botdog UI (each campaign's note template references
    `{{inviteMessage}}`). A rename here is silent in production — the API
    accepts the lead and LinkedIn gets an empty note — so it must break a
    test instead."""

    def test_constant_value_is_pinned(self):
        assert BOTDOG_INVITE_NOTE_VARIABLE == "inviteMessage"

    def test_payload_uses_the_constant_as_the_custom_attribute_key(self):
        client = MagicMock()
        client.get_campaign_leads.return_value = []
        client.add_leads_to_campaign.return_value = _batch_result(
            urls=[_url(0)]
        )
        sender = BotdogSender(
            client,
            campaign_id_for_role=_campaigns({"invite": "acme-campaign-invite"}),
        )
        sender.send_invites([_invite_row(0, message="Hola", language=None)])
        _camp, leads = client.add_leads_to_campaign.call_args.args
        assert list(leads[0]["customAttributes"]) == [
            BOTDOG_INVITE_NOTE_VARIABLE
        ]
        assert leads[0]["customAttributes"][BOTDOG_INVITE_NOTE_VARIABLE] == (
            "Hola"
        )


# ---------------------------------------------------------------------------
# campaign role resolution
# ---------------------------------------------------------------------------


class TestInviteCampaignRoles:
    def test_language_specific_then_fallback(self):
        assert invite_campaign_roles("es") == ("invite_es", "invite")
        assert invite_campaign_roles("en") == ("invite_en", "invite")

    def test_no_language_uses_fallback_only(self):
        assert invite_campaign_roles(None) == ("invite",)


class TestBotdogConfigIsTheCampaignAccessor:
    """`campaign_id_for_role` is a plain callable, and the operator-config
    accessor (`BotdogConfig.campaign_id`) is the one the wiring passes.
    Pinning that shape here keeps the sender free of any config import
    while proving the two halves still fit."""

    def _config(self):
        return BotdogConfig(
            enabled=True,
            campaigns={
                "invite_es": "acme-campaign-invite-es",
                "invite": "acme-campaign-invite",
            },
        )

    def test_bound_method_resolves_the_language_campaign(self):
        client = MagicMock()
        client.get_campaign_leads.return_value = []
        client.add_leads_to_campaign.return_value = _batch_result(
            urls=[_url(0)]
        )
        config = self._config()
        sender = BotdogSender(
            client, campaign_id_for_role=config.campaign_id
        )

        sender.send_invites([_invite_row(0)])

        camp_id, _ = client.add_leads_to_campaign.call_args.args
        assert camp_id == "acme-campaign-invite-es"

    def test_unmapped_role_resolves_to_none(self):
        assert self._config().campaign_id("invite_pt") is None

    def test_campaign_ids_property_feeds_the_scoped_event_poll(self):
        config = self._config()
        assert config.campaign_ids == (
            "acme-campaign-invite-es", "acme-campaign-invite"
        )
        client = MagicMock()
        client.get_campaign_leads.return_value = []
        sender = BotdogSender(
            client,
            campaign_id_for_role=config.campaign_id,
            campaign_ids=config.campaign_ids,
        )

        assert sender.fetch_events(None) == []
        assert client.get_campaign_leads.call_count == 2
        client.get_leads.assert_not_called()


# ---------------------------------------------------------------------------
# send_invites
# ---------------------------------------------------------------------------


class TestSendInvites:
    def test_resolves_language_campaign_and_sends_invite_note(self):
        client = MagicMock()
        client.get_campaign_leads.return_value = []
        client.add_leads_to_campaign.return_value = _batch_result(
            urls=[_url(0)]
        )
        sender = BotdogSender(
            client,
            campaign_id_for_role=_campaigns(
                {"invite_es": "acme-campaign-invite-es"}
            ),
        )

        out = sender.send_invites([_invite_row(0, message="Hi Alice")])

        assert isinstance(out, BotdogSubmitOutcome)
        assert out.submitted_count == 1
        client.add_leads_to_campaign.assert_called_once()
        camp_id, leads = client.add_leads_to_campaign.call_args.args
        assert camp_id == "acme-campaign-invite-es"
        assert leads[0]["linkedinUrl"] == _url(0)
        assert leads[0]["customAttributes"] == {"inviteMessage": "Hi Alice"}

    def test_falls_back_to_generic_role_when_language_unset(self):
        client = MagicMock()
        client.get_campaign_leads.return_value = []
        client.add_leads_to_campaign.return_value = _batch_result(
            urls=[_url(0)]
        )
        sender = BotdogSender(
            client,
            campaign_id_for_role=_campaigns({"invite": "acme-campaign-invite"}),
        )

        # Row language is es, but only the generic role is configured.
        sender.send_invites([_invite_row(0)])

        camp_id, _ = client.add_leads_to_campaign.call_args.args
        assert camp_id == "acme-campaign-invite"

    def test_unresolvable_role_raises_before_any_submit(self):
        # A config gap is an operator error to fix, not a per-lead failure
        # to swallow — and it must raise before anything is submitted so
        # the caller's cap lease refunds cleanly.
        client = MagicMock()
        sender = BotdogSender(client, campaign_id_for_role=_campaigns({}))

        with pytest.raises(ValueError) as excinfo:
            sender.send_invites([_invite_row(0)])

        message = str(excinfo.value)
        assert "no Botdog campaign configured" in message
        # The message must name the file the operator has to edit.
        assert "config/botdog.yaml" in message
        client.add_leads_to_campaign.assert_not_called()

    def test_idempotency_skips_existing_lead(self):
        client = MagicMock()
        client.get_campaign_leads.return_value = [{"linkedinUrl": _url(0)}]
        client.add_leads_to_campaign.return_value = _batch_result(
            urls=[_url(1)]
        )
        sender = BotdogSender(
            client,
            campaign_id_for_role=_campaigns(
                {"invite_es": "acme-campaign-invite-es"}
            ),
        )

        out = sender.send_invites([_invite_row(0), _invite_row(1)])

        assert out.skipped_existing_count == 1
        assert _norm(_url(0)) in out.skipped_existing_urls
        # Only the fresh lead is submitted.
        _, leads = client.add_leads_to_campaign.call_args.args
        assert [lead["linkedinUrl"] for lead in leads] == [_url(1)]

    def test_precheck_failure_fails_closed_no_submit(self):
        client = MagicMock()
        client.get_campaign_leads.side_effect = BotdogError(
            "boom", status_code=500
        )
        sender = BotdogSender(
            client,
            campaign_id_for_role=_campaigns(
                {"invite_es": "acme-campaign-invite-es"}
            ),
        )

        out = sender.send_invites([_invite_row(0)])

        assert out.submitted_count == 0
        assert out.failed_count == 1
        client.add_leads_to_campaign.assert_not_called()

    def test_add_failure_marks_chunk_failed_not_raised(self):
        client = MagicMock()
        client.get_campaign_leads.return_value = []
        client.add_leads_to_campaign.side_effect = BotdogError(
            "429", status_code=429
        )
        sender = BotdogSender(
            client,
            campaign_id_for_role=_campaigns(
                {"invite_es": "acme-campaign-invite-es"}
            ),
        )

        out = sender.send_invites([_invite_row(0)])

        assert out.submitted_count == 0
        assert out.failed_count == 1


class TestGhostSubmittedReconciliation:
    """When the 2xx body's per-lead count diverges from what we sent,
    campaign membership — the ground truth the idempotency pre-check
    already trusts — decides who was really submitted.

    Without this, the unverified response shape (the COMMON case: zero
    extractable per-lead entries) made every lead default to "submitted",
    charging the invite lease and stamping the Botdog send channel for
    leads Botdog might not hold.
    """

    def _sender(self, client):
        return BotdogSender(
            client,
            campaign_id_for_role=_campaigns(
                {"invite_es": "acme-campaign-invite-es"}
            ),
        )

    def test_count_mismatch_refetches_and_confirms_present_leads(
        self, capsys
    ):
        client = MagicMock()
        # Pre-check: empty. Membership re-check: the lead really landed.
        client.get_campaign_leads.side_effect = [
            [],
            [{"linkedinUrl": _url(0)}],
        ]
        client.add_leads_to_campaign.return_value = _batch_result()  # 0 results

        out = self._sender(client).send_invites([_invite_row(0)])

        assert out.submitted_count == 1
        assert out.failed_count == 0
        assert client.get_campaign_leads.call_count == 2
        captured = capsys.readouterr()
        assert "re-checking campaign membership" in captured.err
        assert "Membership re-check" in captured.out

    def test_count_mismatch_marks_absent_leads_unconfirmed(self, capsys):
        client = MagicMock()
        client.get_campaign_leads.side_effect = [[], []]  # never landed
        client.add_leads_to_campaign.return_value = _batch_result()

        out = self._sender(client).send_invites([_invite_row(0)])

        assert out.submitted_count == 0
        assert out.failed == (
            (_norm(_url(0)), "unconfirmed_by_membership"),
        )
        assert "re-checking campaign membership" in capsys.readouterr().err

    def test_partial_mismatch_splits_by_membership(self):
        client = MagicMock()
        client.get_campaign_leads.side_effect = [
            [],
            [{"linkedinUrl": _url(1)}],
        ]
        # One per-lead entry for a two-lead chunk — still a mismatch.
        client.add_leads_to_campaign.return_value = _batch_result(
            urls=[_url(1)]
        )

        out = self._sender(client).send_invites(
            [_invite_row(0), _invite_row(1)]
        )

        assert out.submitted_urls == frozenset({_norm(_url(1))})
        assert out.failed == (
            (_norm(_url(0)), "unconfirmed_by_membership"),
        )

    def test_refetch_failure_fails_closed(self, capsys):
        client = MagicMock()
        client.get_campaign_leads.side_effect = [
            [],
            BotdogError("membership down", status_code=503),
        ]
        client.add_leads_to_campaign.return_value = _batch_result()

        out = self._sender(client).send_invites([_invite_row(0)])

        # Unverifiable == not submitted: no charge, no stamp; the
        # pre-check makes the next run's retry safe.
        assert out.submitted_count == 0
        assert out.failed_count == 1
        assert "unconfirmed_by_membership" in out.failed[0][1]
        assert "membership re-check FAILED" in capsys.readouterr().err

    def test_matching_count_keeps_per_lead_classification(self):
        """No divergence → the body is real data; membership is NOT
        re-fetched and the existing verdicts stand."""
        client = MagicMock()
        client.get_campaign_leads.return_value = []
        client.add_leads_to_campaign.return_value = _batch_result(
            lead_results=[
                {"linkedinUrl": _url(0), "status": "duplicate"},
                {"linkedinUrl": _url(1), "status": "ok"},
            ]
        )

        out = self._sender(client).send_invites(
            [_invite_row(0), _invite_row(1)]
        )

        assert out.skipped_existing_urls == frozenset({_norm(_url(0))})
        assert out.submitted_urls == frozenset({_norm(_url(1))})
        # Only the pre-check fetch — no reconciliation round trip.
        assert client.get_campaign_leads.call_count == 1


# ---------------------------------------------------------------------------
# send_dm
# ---------------------------------------------------------------------------


class TestSendDm:
    def test_resolves_lead_and_submits_message(self):
        client = MagicMock()
        client.get_leads.return_value = [
            {"id": "lead-1", "linkedinUrl": _url(0)}
        ]
        client.send_message.return_value = {"ok": True}
        sender = BotdogSender(client, campaign_id_for_role=_campaigns({}))

        out = sender.send_dm({"linkedInUrl": _url(0)}, "hey")

        assert out.submitted_count == 1
        client.send_message.assert_called_once_with(text="hey", lead_id="lead-1")

    def test_missing_url_is_a_surfaced_failure(self):
        client = MagicMock()
        sender = BotdogSender(client, campaign_id_for_role=_campaigns({}))

        out = sender.send_dm({"linkedInUrl": ""}, "hey")

        assert out.submitted_count == 0
        assert out.failed[0][1] == "missing_linkedin_url"
        client.get_leads.assert_not_called()

    def test_lead_not_found_is_surfaced_failure_seam(self):
        # The campaign-fallback seam: today an unresolvable lead is a loud
        # per-prospect failure (the row stays queued), never dead fallback
        # code that silently drops the send.
        client = MagicMock()
        client.get_leads.return_value = []  # no matching lead
        sender = BotdogSender(client, campaign_id_for_role=_campaigns({}))

        out = sender.send_dm(
            {"linkedInUrl": "https://linkedin.com/in/acme-nobody"}, "hey"
        )

        assert out.submitted_count == 0
        assert out.failed[0][1] == "lead_not_found"
        client.send_message.assert_not_called()

    def test_send_error_is_captured_not_raised(self):
        client = MagicMock()
        client.get_leads.return_value = [
            {"id": "lead-1", "linkedinUrl": _url(0)}
        ]
        client.send_message.side_effect = BotdogError("down", status_code=503)
        sender = BotdogSender(client, campaign_id_for_role=_campaigns({}))

        out = sender.send_dm({"linkedInUrl": _url(0)}, "hey")

        assert out.submitted_count == 0
        assert "send_message_failed" in out.failed[0][1]

    def test_invalid_text_fails_row_before_any_api_call(self):
        # Text is validated before the lead lookup: a bad render costs
        # zero API calls and gets its own reason, distinct from
        # send_message_failed (which means Botdog was actually asked).
        client = MagicMock()
        sender = BotdogSender(client, campaign_id_for_role=_campaigns({}))

        out = sender.send_dm({"linkedInUrl": _url(0)}, "")

        assert out.submitted_count == 0
        assert out.failed[0][1].startswith("invalid_message_text")
        client.get_leads.assert_not_called()
        client.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# fetch_events
# ---------------------------------------------------------------------------


def _events_client(details: list[dict], *, rows: list[dict] | None = None):
    """Client stub for the DETAIL-based event poll.

    `details` are full `GET /v1/leads/{id}` payloads (each needs an
    `id`); the lead LIST answers thin rows — which is the transport's
    reality: list rows carry NO event data and no timestamps beyond
    `createdAt`.
    """
    by_id = {d["id"]: d for d in details}
    client = MagicMock()
    client.get_leads.return_value = rows if rows is not None else [
        {"id": d["id"], "createdAt": "2026-08-01T00:00:00Z"} for d in details
    ]
    client.get_lead.side_effect = lambda lead_id: by_id[lead_id]
    return client


def _sender_for(client):
    return BotdogSender(client, campaign_id_for_role=_campaigns({}))


class TestFetchEvents:
    """Events are derived from lead DETAILS, not from list rows: a list
    row carries no `events` array and no state timestamps, so reading
    embedded events off list rows yields ZERO events forever. The
    detail's flat `invitedAt` / `connectedAt` / `repliedAt` /
    `withdrawnAt` are the primary source."""

    def test_synthesizes_events_from_detail_timestamps(self):
        client = _events_client([
            {
                "id": "lead-1",
                "linkedinProfile": _url(0),
                "invitedAt": "2026-08-10T10:00:00Z",
                "connectedAt": "2026-08-12T11:00:00Z",
                "repliedAt": "2026-08-14T12:00:00Z",
                "withdrawnAt": "2026-08-15T13:00:00Z",
                "events": [],
            }
        ])

        events = _sender_for(client).fetch_events(None)

        client.get_lead.assert_called_once_with("lead-1")
        assert [(e.event_type, e.occurred_at) for e in events] == [
            ("LEAD_INVITATION_SENT", datetime(2026, 8, 10, 10, tzinfo=UTC)),
            ("LEAD_INVITATION_ACCEPTED", datetime(2026, 8, 12, 11, tzinfo=UTC)),
            ("LEAD_MESSAGE_REPLIED", datetime(2026, 8, 14, 12, tzinfo=UTC)),
            ("LEAD_INVITATION_WITHDRAWN", datetime(2026, 8, 15, 13, tzinfo=UTC)),
        ]
        assert {e.lead_linkedin_url for e in events} == {_norm(_url(0))}

    def test_absent_timestamps_synthesize_nothing(self):
        client = _events_client([
            {
                "id": "lead-1",
                "linkedinProfile": _url(0),
                "invitedAt": "2026-08-10T10:00:00Z",
                "connectedAt": None,
                "repliedAt": "",
            }
        ])

        events = _sender_for(client).fetch_events(None)

        assert [e.event_type for e in events] == ["LEAD_INVITATION_SENT"]

    def test_events_array_entries_are_still_translated(self):
        """If Botdog ever populates the array, those real events win —
        this is the ONLY path a LEAD_MESSAGE_SENT can arrive on (the
        detail DTO has no per-message sent timestamp)."""
        client = _events_client([
            {
                "id": "lead-1",
                "linkedinProfile": _url(0),
                "events": [
                    {"eventType": "LEAD_MESSAGE_SENT",
                     "occurredAt": "2026-08-20T10:00:00Z"},
                ],
            }
        ])

        events = _sender_for(client).fetch_events(None)

        assert [e.event_type for e in events] == ["LEAD_MESSAGE_SENT"]
        assert events[0].occurred_at == datetime(2026, 8, 20, 10, tzinfo=UTC)

    def test_no_double_synthesis_when_array_already_has_the_type(self):
        """The array entry is the richer record — synthesizing the same
        transition again would report one accept as two."""
        client = _events_client([
            {
                "id": "lead-1",
                "linkedinProfile": _url(0),
                "connectedAt": "2026-08-12T11:00:00Z",
                "invitedAt": "2026-08-10T10:00:00Z",
                "events": [
                    {"eventType": "LEAD_INVITATION_ACCEPTED",
                     "occurredAt": "2026-08-12T11:30:00Z"},
                ],
            }
        ])

        events = _sender_for(client).fetch_events(None)

        types = [e.event_type for e in events]
        assert types.count("LEAD_INVITATION_ACCEPTED") == 1
        # The array's timestamp survives, not the synthesized one.
        accepted = next(
            e for e in events if e.event_type == "LEAD_INVITATION_ACCEPTED"
        )
        assert accepted.occurred_at == datetime(2026, 8, 12, 11, 30, tzinfo=UTC)
        assert "LEAD_INVITATION_SENT" in types  # unrelated type still synthesized

    def test_since_filters_older_events(self):
        client = _events_client([
            {
                "id": "lead-1",
                "linkedinProfile": _url(0),
                "invitedAt": "2026-08-01T00:00:00Z",
                "connectedAt": "2026-08-20T00:00:00Z",
            }
        ])

        events = _sender_for(client).fetch_events(
            datetime(2026, 8, 15, tzinfo=UTC)
        )

        assert [e.event_type for e in events] == ["LEAD_INVITATION_ACCEPTED"]

    def test_undated_array_event_is_kept_regardless_of_since(self):
        client = _events_client([
            {
                "id": "lead-1",
                "linkedinProfile": _url(0),
                "events": [{"eventType": "no_timestamp"}],
            }
        ])

        events = _sender_for(client).fetch_events(
            datetime(2026, 8, 15, tzinfo=UTC)
        )

        assert [e.event_type for e in events] == ["no_timestamp"]
        assert events[0].occurred_at is None

    def test_unparseable_detail_timestamp_is_loud_not_silent(self, capsys):
        client = _events_client([
            {
                "id": "lead-1",
                "linkedinProfile": _url(0),
                "invitedAt": "not-a-date",
            }
        ])

        events = _sender_for(client).fetch_events(None)

        assert events == []
        assert "unparseable invitedAt" in capsys.readouterr().err

    def test_detail_fetch_failure_is_loud_and_does_not_abort_the_poll(
        self, capsys
    ):
        client = _events_client([
            {"id": "lead-1", "linkedinProfile": _url(0),
             "connectedAt": "2026-08-12T11:00:00Z"},
            {"id": "lead-2", "linkedinProfile": _url(1),
             "connectedAt": "2026-08-13T11:00:00Z"},
        ])
        details = client.get_lead.side_effect

        def _flaky(lead_id):
            if lead_id == "lead-1":
                raise BotdogError("detail down", status_code=503)
            return details(lead_id)

        client.get_lead.side_effect = _flaky

        events = _sender_for(client).fetch_events(None)

        # The healthy lead still reports.
        assert [e.lead_linkedin_url for e in events] == [_norm(_url(1))]
        err = capsys.readouterr().err
        assert "detail fetch failed for lead lead-1" in err
        assert "1 of 2 lead detail(s) could not be read" in err
        # COMPLETENESS SIGNAL: the unreadable lead emitted no events, so
        # the poll must tell the ingest it was INCOMPLETE — otherwise the
        # ingest advances its cursor past events it never saw.
        assert events.leads_unread == 1

    def test_lead_row_without_id_counts_as_unread(self, capsys):
        client = _events_client(
            [{"id": "lead-1", "linkedinProfile": _url(0),
              "connectedAt": "2026-08-12T11:00:00Z"}],
            rows=[{"id": "lead-1"}, {"createdAt": "2026-08-01T00:00:00Z"}],
        )

        events = _sender_for(client).fetch_events(None)

        assert events.leads_unread == 1
        assert "lead row with no id" in capsys.readouterr().err

    def test_fully_read_poll_reports_nothing_unread(self):
        client = _events_client([
            {"id": "lead-1", "linkedinProfile": _url(0),
             "connectedAt": "2026-08-12T11:00:00Z"},
        ])

        assert _sender_for(client).fetch_events(None).leads_unread == 0

    def test_unread_count_survives_the_since_filter(self):
        """The `since` filter drops EVENTS, never the completeness signal
        — a filtered poll that left leads unread is still incomplete."""
        client = _events_client([
            {"id": "lead-1", "linkedinProfile": _url(0),
             "connectedAt": "2026-08-12T11:00:00Z"},
            {"id": "lead-2", "linkedinProfile": _url(1),
             "connectedAt": "2026-08-13T11:00:00Z"},
        ])
        details = client.get_lead.side_effect

        def _flaky(lead_id):
            if lead_id == "lead-1":
                raise BotdogError("detail down", status_code=503)
            return details(lead_id)

        client.get_lead.side_effect = _flaky

        events = _sender_for(client).fetch_events(
            datetime(2026, 12, 1, tzinfo=UTC)
        )

        assert list(events) == []  # every event is below the floor
        assert events.leads_unread == 1


class TestFetchEventsDetailCap:
    """COST GUARD: one detail request per lead bounds the poll's runtime
    (the API allows 60 req/min). Skipped leads are NAMED — an invisible
    lead is an invisible accept."""

    def _rows(self, n: int, *, replied_index: int | None = None):
        rows = []
        for i in range(n):
            row = {"id": f"lead-{i}", "createdAt": "2026-08-01T00:00:00Z"}
            if replied_index is not None and i == replied_index:
                row["hasReplied"] = True
            rows.append(row)
        return rows

    def _client(self, rows):
        client = MagicMock()
        client.get_leads.return_value = rows
        client.get_lead.side_effect = lambda lead_id: {
            "id": lead_id,
            "linkedinProfile": f"https://linkedin.com/in/acme-{lead_id}",
        }
        return client

    def test_cap_limits_detail_fetches_and_says_how_many_were_skipped(
        self, capsys, monkeypatch
    ):
        monkeypatch.setattr(sender_mod, "BOTDOG_MAX_DETAIL_FETCHES", 3)
        client = self._client(self._rows(5))

        events = _sender_for(client).fetch_events(None)

        assert client.get_lead.call_count == 3
        err = capsys.readouterr().err
        assert "2 lead(s) NOT polled this run" in err
        # Capped-out leads are UNREAD, not "nothing happened": the count
        # rides back so the ingest holds its cursor instead of aging
        # their events past the next poll's floor.
        assert events.leads_unread == 2

    def test_replied_leads_are_polled_first(self, capsys, monkeypatch):
        """PRIORITIZATION CHOICE: `hasReplied` is the only activity hint
        the LIST row carries, and a reply is the event with a human
        waiting — so those leads jump the queue when the cap binds."""
        monkeypatch.setattr(sender_mod, "BOTDOG_MAX_DETAIL_FETCHES", 1)
        client = self._client(self._rows(4, replied_index=3))

        _sender_for(client).fetch_events(None)

        client.get_lead.assert_called_once_with("lead-3")

    def test_no_cap_message_when_under_the_budget(self, capsys):
        client = self._client(self._rows(2))

        _sender_for(client).fetch_events(None)

        assert "NOT polled this run" not in capsys.readouterr().err


class TestLeadUrlProbe:
    """`linkedinProfile` is the real field on both list rows and details.
    Missing it made every probe return "" — which silently broke the
    invite idempotency pre-check (no campaign member ever matched, so
    existing leads looked absent and would be RE-INVITED)."""

    def test_linkedin_profile_is_probed(self):
        assert _lead_url({"linkedinProfile": _url(0)}) == _url(0)

    def test_legacy_probes_still_work(self):
        assert _lead_url({"linkedinUrl": _url(0)}) == _url(0)
        assert _lead_url({"nothing": "here"}) == ""

    def test_idempotency_precheck_matches_on_linkedin_profile(self):
        """End-to-end guard: a campaign member returned in the real shape
        must be recognized as already-present, not re-submitted."""
        client = MagicMock()
        client.get_campaign_leads.return_value = [
            {"id": "lead-1", "linkedinProfile": _url(0)}
        ]
        sender = BotdogSender(
            client,
            campaign_id_for_role=_campaigns(
                {"invite_es": "acme-campaign-invite-es"}
            ),
        )

        out = sender.send_invites([_invite_row(0)])

        assert out.skipped_existing_count == 1
        client.add_leads_to_campaign.assert_not_called()


# ── offset-less timestamps are coerced to UTC-aware ────────────────────


class TestNaiveTimestampCoercion:
    """A Botdog DTO timestamp with no offset used to produce a NAIVE
    datetime. Mixed with the aware ones, it raised TypeError inside the
    ingest watermark math (`sorted()` / `min()`) and aborted the whole
    poll. Offset-less values are now assumed UTC — for the detail
    timestamps as well as the events array."""

    def _sender(self, raw_ts: str):
        return _sender_for(_events_client([
            {
                "id": "lead-1",
                "linkedinProfile": _url(0),
                "connectedAt": raw_ts,
            }
        ]))

    def test_offsetless_datetime_becomes_utc_aware(self):
        events = self._sender("2026-08-20T10:00:00").fetch_events(None)
        assert events[0].occurred_at == datetime(
            2026, 8, 20, 10, 0, tzinfo=UTC
        )
        assert events[0].occurred_at.tzinfo is not None

    def test_bare_date_becomes_utc_aware(self):
        events = self._sender("2026-08-20").fetch_events(None)
        assert events[0].occurred_at == datetime(2026, 8, 20, tzinfo=UTC)

    def test_explicit_offset_is_preserved(self):
        events = self._sender("2026-08-20T10:00:00-05:00").fetch_events(None)
        assert events[0].occurred_at == datetime(
            2026, 8, 20, 15, 0, tzinfo=UTC
        )

    def test_offsetless_event_compares_against_aware_since(self):
        """The `since` filter itself compares occurred_at < since — with
        a naive value that raised TypeError and fell into the keep-all
        branch. Now it filters correctly."""
        sender = self._sender("2026-08-01T00:00:00")
        assert sender.fetch_events(datetime(2026, 8, 15, tzinfo=UTC)) == []

    def test_offsetless_event_survives_ingest_watermark_math(
        self, monkeypatch, tmp_path
    ):
        """End-to-end guard: an offset-less event flows through
        fetch_events into ingest_botdog_events' failure-watermark math
        (min() over event timestamps) without a naive/aware TypeError."""
        from workflows import botdog_ingest, botdog_ledger
        from workflows.botdog_ingest import ingest_botdog_events

        # Keep the ledger crosscheck / stale-row watchdog off the
        # operator's real submission ledger.
        monkeypatch.setattr(
            botdog_ledger, "LEDGER_FILE", tmp_path / "submissions.json"
        )
        monkeypatch.setattr(botdog_ingest, "POLL_STATE_DIR", tmp_path)
        monkeypatch.setattr(
            botdog_ingest, "POLL_STATE_FILE", tmp_path / "botdog_poll.json"
        )
        monkeypatch.setattr(
            "workflows.daily_check._attio_advance_with_escalation",
            MagicMock(return_value=False),  # force the watermark path
        )
        entry = {
            "entry_id": "e1",
            "record_id": "r1",
            "stage": "Connection Sent",
            "dm_step": None,
            "canonical_linkedin_url": _url(0),
            "vanity_url_slug": "acme-alice",
            "send_channel": "botdog",
            "experiment_id": None,
            "dm1_sent_at": None,
            "dm2_sent_at": None,
            "dm3_sent_at": None,
        }
        client = _events_client([
            {
                "id": "lead-1",
                "linkedinProfile": _url(0),
                # offset-less detail timestamp AND an aware array event,
                # in the same lead
                "connectedAt": "2026-08-18T09:00:00",
                "events": [
                    {"eventType": "LEAD_MESSAGE_SENT",
                     "occurredAt": "2026-08-19T09:00:00Z"},
                ],
            }
        ])
        sender = _sender_for(client)

        report = ingest_botdog_events(
            MagicMock(),
            sender=sender,
            now=datetime(2026, 8, 20, 9, 0, tzinfo=UTC),
            entries_provider=lambda crm: [entry],
        )

        assert report["failures"] >= 1
        # Watermark held at the OFFSET-LESS event (the earliest one),
        # proving it was comparable rather than crashing the run.
        assert report["cursor_after"] == "2026-08-18T09:00:00+00:00"


class TestFetchEventsCampaignScope:
    """Campaign-scoped event poll: with campaign_ids set, fetch_events
    unions get_campaign_leads per campaign (deduped) and never calls the
    unfiltered get_leads (which paginates past the cap on accounts
    holding 1000+ unrelated leads)."""

    def test_scoped_poll_uses_campaign_leads_and_dedupes(self):
        client = MagicMock()
        shared = {"id": "L1", "linkedinProfile": _url(0),
                  "hasReplied": False}
        client.get_campaign_leads.side_effect = [
            [shared, {"id": "L2", "linkedinProfile": _url(1),
                      "hasReplied": False}],
            [shared],  # same lead visible in the second campaign → deduped
        ]
        client.get_lead.side_effect = lambda lid: {
            "id": lid, "linkedinProfile": f"https://linkedin.com/in/acme-{lid}",
            "invitedAt": "2026-08-22T10:00:00Z", "events": [],
        }
        sender = BotdogSender(
            client, campaign_id_for_role=_campaigns({}),
            campaign_ids=(
                "acme-campaign-invite-es",
                "acme-campaign-invite-en",
                "acme-campaign-invite-es",  # dupes collapse
            ),
        )

        events = sender.fetch_events(None)

        assert client.get_leads.call_count == 0
        assert client.get_campaign_leads.call_count == 2  # each campaign once
        assert client.get_lead.call_count == 2            # L1 deduped
        assert {e.event_type for e in events} == {"LEAD_INVITATION_SENT"}

    def test_no_campaign_ids_falls_back_to_full_scan(self):
        client = MagicMock()
        client.get_leads.return_value = []
        sender = BotdogSender(client, campaign_id_for_role=_campaigns({}))

        assert sender.fetch_events(None) == []
        client.get_leads.assert_called_once()
        client.get_campaign_leads.assert_not_called()
