"""Unit tests for workflows/pb_send_recovery.py.

All PB and Attio calls are mocked — no network access, no Attio writes.

Scenarios covered:
  1. killed-run    — entry at DM2_SENT + CSV row "Message sent" today → recovered
  2. already-recorded — same CSV row but entry.last_contact_date == today → SKIP
  3. failed-row    — CSV row with error field → never recovered
  4. no-match      — URL in CSV has no list entry → skipped_no_entry
  5. max-stage     — entry at DM3_SENT → not advanced past DM3
  6. dry_run       — computes summary, zero update_list_entry calls
  7. no-csv-line   — PB output has no "CSV saved at" → returns early

All tests use a minimal CSV builder so the shape mirrors the real Message
Sender output without pulling in any external fixture files.
"""
from __future__ import annotations

import csv
import io
from unittest.mock import MagicMock, patch

import pytest

from models.pipeline import PipelineStage
from workflows.pb_send_recovery import recover_unrecorded_dm_sends

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TODAY = "2026-06-01"
_YESTERDAY = "2026-05-31"
_LIST_ID = "test-list-id"
_MSG_SENDER_ID = "pb-agent-123"


def _make_csv_text(*rows: dict) -> str:
    """Build a minimal Message Sender result.csv string."""
    if not rows:
        return ""
    fieldnames = list(rows[0].keys())
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def _make_sent_row(
    url: str = "https://linkedin.com/in/test-user",
    timestamp: str = _TODAY,
    message: str = "Hola!",
) -> dict:
    return {
        "linkedinProfileUrl": url,
        "fullName": "Test User",
        "timestamp": timestamp,
        "status": "Message sent",
        "message": message,
        "error": "",
    }


def _make_failed_row(
    url: str = "https://linkedin.com/in/test-user",
    error: str = "InMail required",
) -> dict:
    return {
        "linkedinProfileUrl": url,
        "fullName": "Test User",
        "timestamp": _TODAY,
        "status": "Error",
        "message": "",
        "error": error,
    }


def _make_pb_output(csv_url: str | None = None) -> dict:
    """Return a dict mimicking pb.get_output() response.

    If csv_url is given, the output text includes the '✅ CSV saved at <url>'
    line that the Message Sender phantom prints on success.
    """
    if csv_url:
        output_text = (
            "Processing profiles...\n"
            f"✅ CSV saved at {csv_url}\n"
            "Done.\n"
        )
    else:
        output_text = "Processing profiles...\nNo sends made.\n"
    return {
        "output": output_text,
        "status": "finished",
        "isAgentRunning": False,
        "containerId": "container-abc",
    }


def _make_entry(
    entry_id: str,
    record_id: str,
    stage: str,
    last_contact_date: str | None = None,
) -> dict:
    """Build a raw Attio list-entry dict that parse_entry can decode."""
    entry_values: dict = {
        "stage": [
            {
                "status": {"title": stage},
            }
        ],
    }
    if last_contact_date is not None:
        entry_values["last_contact_date"] = [{"value": last_contact_date}]

    return {
        "entry_id": entry_id,
        "parent_record_id": record_id,
        "entry_values": entry_values,
    }


def _make_person_record(record_id: str, linkedin_url: str) -> dict:
    """Build a minimal Attio person record dict."""
    return {
        "id": {"record_id": record_id},
        "values": {
            "linkedin": [{"value": linkedin_url}],
        },
    }


def _setup_attio_and_pb(
    entries: list[dict],
    person_map: dict[str, dict],  # record_id → person dict
    csv_text: str,
    csv_url: str = "https://phantombuster.s3.amazonaws.com/org/agent/result.csv",
) -> tuple[MagicMock, MagicMock]:
    """Return (attio_mock, pb_mock) wired for a standard recovery run.

    attio.query_list_entries → entries
    attio.search_person_by_linkedin(url) → the person whose linkedin matches
        (canonicalized), else None
    attio.update_list_entry → MagicMock (tracks calls)
    pb.get_output → _make_pb_output(csv_url)
    _download_csv is patched at module level (not through pb)
    """
    from clients.attio import _canonical_linkedin_url

    attio = MagicMock()
    attio.query_list_entries.return_value = entries
    attio.update_list_entry = MagicMock(return_value={})

    # url (canonical) → person, derived from each person's stored linkedin value
    url_to_person: dict[str, dict] = {}
    for person in person_map.values():
        li = (person.get("values", {}) or {}).get("linkedin") or []
        if li and isinstance(li[0], dict):
            url_to_person[_canonical_linkedin_url(li[0].get("value", ""))] = person
    attio.search_person_by_linkedin.side_effect = (
        lambda url: url_to_person.get(_canonical_linkedin_url(url))
    )

    pb = MagicMock()
    pb.get_output.return_value = _make_pb_output(csv_url)

    return attio, pb


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class TestKilledRunRecovery:
    """Scenario 1: process killed after PB sent DM3 but before Attio write."""

    def test_dm2_to_dm3_recovered(self) -> None:
        """Entry at DM2_SENT + today's CSV row → advanced to DM3_SENT."""
        url = "https://linkedin.com/in/test-user"
        record_id = "rec-001"
        entry = _make_entry("entry-001", record_id, "DM2 Sent", last_contact_date=_YESTERDAY)
        person = _make_person_record(record_id, url)
        csv_text = _make_csv_text(_make_sent_row(url=url, timestamp=_TODAY))
        csv_url = "https://phantombuster.s3.amazonaws.com/x/y/result.csv"

        attio, pb = _setup_attio_and_pb([entry], {record_id: person}, csv_text, csv_url)

        with patch(
            "workflows.pb_send_recovery._download_csv", return_value=csv_text
        ):
            result = recover_unrecorded_dm_sends(
                attio, pb, _MSG_SENDER_ID, _LIST_ID, _TODAY, dry_run=False
            )

        assert result["recovered"] == 1
        assert result["skipped_already_recorded"] == 0
        assert result["skipped_failed"] == 0
        assert result["skipped_no_entry"] == 0
        assert result["skipped_write_failed"] == 0

        # The advance now routes through the §3.15 AttioWriter path
        # (_attio_advance_with_escalation), which calls update_list_entry
        # with KEYWORD args. Reaching this call at all proves pb_send_recovery
        # is a registry-authorized writer for stage/dm_step/dmN_sent_at/
        # last_contact_date — an unregistered module would have raised
        # UnauthorizedAttioWriteError before any write.
        attio.update_list_entry.assert_called_once()
        kwargs = attio.update_list_entry.call_args.kwargs
        assert kwargs["entry_id"] == "entry-001"
        payload = kwargs["entry_attributes"]
        assert payload["stage"] == PipelineStage.DM3_SENT.value
        assert payload["dm_step"] == 3
        assert payload["dm3_sent_at"] == _TODAY
        assert payload["last_contact_date"] == _TODAY
        assert kwargs["list_id"] == _LIST_ID

    def test_accepted_to_dm1_recovered(self) -> None:
        """Entry at ACCEPTED + today's CSV row → advanced to DM1_SENT."""
        url = "https://linkedin.com/in/another-user"
        record_id = "rec-002"
        entry = _make_entry("entry-002", record_id, "Accepted", last_contact_date=_YESTERDAY)
        person = _make_person_record(record_id, url)
        csv_text = _make_csv_text(_make_sent_row(url=url, timestamp=_TODAY))
        csv_url = "https://phantombuster.s3.amazonaws.com/x/y/result.csv"

        attio, pb = _setup_attio_and_pb([entry], {record_id: person}, csv_text, csv_url)

        with patch("workflows.pb_send_recovery._download_csv", return_value=csv_text):
            result = recover_unrecorded_dm_sends(
                attio, pb, _MSG_SENDER_ID, _LIST_ID, _TODAY, dry_run=False
            )

        assert result["recovered"] == 1
        payload = attio.update_list_entry.call_args.kwargs["entry_attributes"]
        assert payload["stage"] == PipelineStage.DM1_SENT.value
        assert payload["dm_step"] == 1
        assert payload["dm1_sent_at"] == _TODAY


class TestAlreadyRecorded:
    """Scenario 2: idempotency — send date already in Attio."""

    def test_same_day_send_skipped(self) -> None:
        """last_contact_date == send_date → SKIP, zero Attio writes."""
        url = "https://linkedin.com/in/test-user"
        record_id = "rec-001"
        # last_contact_date == _TODAY (send already recorded)
        entry = _make_entry("entry-001", record_id, "DM2 Sent", last_contact_date=_TODAY)
        person = _make_person_record(record_id, url)
        csv_text = _make_csv_text(_make_sent_row(url=url, timestamp=_TODAY))
        csv_url = "https://phantombuster.s3.amazonaws.com/x/y/result.csv"

        attio, pb = _setup_attio_and_pb([entry], {record_id: person}, csv_text, csv_url)

        with patch("workflows.pb_send_recovery._download_csv", return_value=csv_text):
            result = recover_unrecorded_dm_sends(
                attio, pb, _MSG_SENDER_ID, _LIST_ID, _TODAY, dry_run=False
            )

        assert result["recovered"] == 0
        assert result["skipped_already_recorded"] == 1
        attio.update_list_entry.assert_not_called()

    def test_future_contact_date_skipped(self) -> None:
        """last_contact_date > send_date is also an already-recorded signal."""
        url = "https://linkedin.com/in/test-user"
        record_id = "rec-001"
        entry = _make_entry("entry-001", record_id, "DM2 Sent", last_contact_date="2026-06-02")
        person = _make_person_record(record_id, url)
        csv_text = _make_csv_text(_make_sent_row(url=url, timestamp=_TODAY))
        csv_url = "https://phantombuster.s3.amazonaws.com/x/y/result.csv"

        attio, pb = _setup_attio_and_pb([entry], {record_id: person}, csv_text, csv_url)

        with patch("workflows.pb_send_recovery._download_csv", return_value=csv_text):
            result = recover_unrecorded_dm_sends(
                attio, pb, _MSG_SENDER_ID, _LIST_ID, _TODAY, dry_run=False
            )

        assert result["recovered"] == 0
        assert result["skipped_already_recorded"] == 1
        attio.update_list_entry.assert_not_called()


class TestFailedRow:
    """Scenario 3: CSV row with error → never recovered."""

    def test_inmail_required_skipped(self) -> None:
        url = "https://linkedin.com/in/test-user"
        record_id = "rec-001"
        entry = _make_entry("entry-001", record_id, "DM2 Sent", last_contact_date=_YESTERDAY)
        person = _make_person_record(record_id, url)
        csv_text = _make_csv_text(_make_failed_row(url=url, error="InMail required"))
        csv_url = "https://phantombuster.s3.amazonaws.com/x/y/result.csv"

        attio, pb = _setup_attio_and_pb([entry], {record_id: person}, csv_text, csv_url)

        with patch("workflows.pb_send_recovery._download_csv", return_value=csv_text):
            result = recover_unrecorded_dm_sends(
                attio, pb, _MSG_SENDER_ID, _LIST_ID, _TODAY, dry_run=False
            )

        assert result["examined"] == 0  # failed rows don't count as examined
        assert result["recovered"] == 0
        assert result["skipped_failed"] == 1
        attio.update_list_entry.assert_not_called()

    def test_error_column_non_empty_skips_even_if_status_ok(self) -> None:
        """Any non-empty 'error' field → skip, regardless of status value."""
        url = "https://linkedin.com/in/test-user"
        record_id = "rec-001"
        entry = _make_entry("entry-001", record_id, "DM2 Sent", last_contact_date=_YESTERDAY)
        person = _make_person_record(record_id, url)
        row = {
            "linkedinProfileUrl": url,
            "fullName": "Test User",
            "timestamp": _TODAY,
            "status": "Message sent",   # status looks fine
            "message": "Hola!",
            "error": "partial failure",  # but error is non-empty
        }
        csv_text = _make_csv_text(row)
        csv_url = "https://phantombuster.s3.amazonaws.com/x/y/result.csv"

        attio, pb = _setup_attio_and_pb([entry], {record_id: person}, csv_text, csv_url)

        with patch("workflows.pb_send_recovery._download_csv", return_value=csv_text):
            result = recover_unrecorded_dm_sends(
                attio, pb, _MSG_SENDER_ID, _LIST_ID, _TODAY, dry_run=False
            )

        assert result["recovered"] == 0
        assert result["skipped_failed"] == 1
        attio.update_list_entry.assert_not_called()


class TestNoMatchingEntry:
    """Scenario 4: URL in CSV has no list entry."""

    def test_unknown_url_skips_no_crash(self) -> None:
        url = "https://linkedin.com/in/unknown-person"
        csv_text = _make_csv_text(_make_sent_row(url=url, timestamp=_TODAY))
        csv_url = "https://phantombuster.s3.amazonaws.com/x/y/result.csv"

        # Entry index is empty (no entries with that URL)
        attio, pb = _setup_attio_and_pb([], {}, csv_text, csv_url)

        with patch("workflows.pb_send_recovery._download_csv", return_value=csv_text):
            result = recover_unrecorded_dm_sends(
                attio, pb, _MSG_SENDER_ID, _LIST_ID, _TODAY, dry_run=False
            )

        assert result["examined"] == 1
        assert result["skipped_no_entry"] == 1
        assert result["recovered"] == 0
        attio.update_list_entry.assert_not_called()


class TestMaxStageNotAdvanced:
    """Scenario 5: entry at DM3_SENT → not advanced past DM3."""

    def test_dm3_sent_not_advanced_to_responded(self) -> None:
        url = "https://linkedin.com/in/test-user"
        record_id = "rec-001"
        entry = _make_entry("entry-001", record_id, "DM3 Sent", last_contact_date=_YESTERDAY)
        person = _make_person_record(record_id, url)
        csv_text = _make_csv_text(_make_sent_row(url=url, timestamp=_TODAY))
        csv_url = "https://phantombuster.s3.amazonaws.com/x/y/result.csv"

        attio, pb = _setup_attio_and_pb([entry], {record_id: person}, csv_text, csv_url)

        with patch("workflows.pb_send_recovery._download_csv", return_value=csv_text):
            result = recover_unrecorded_dm_sends(
                attio, pb, _MSG_SENDER_ID, _LIST_ID, _TODAY, dry_run=False
            )

        assert result["recovered"] == 0
        assert result["skipped_no_transition"] == 1
        # Critically: no RESPONDED stage write
        attio.update_list_entry.assert_not_called()

    def test_responded_not_advanced(self) -> None:
        """Entries already at RESPONDED or beyond are skipped."""
        url = "https://linkedin.com/in/test-user"
        record_id = "rec-001"
        entry = _make_entry("entry-001", record_id, "Responded", last_contact_date=_YESTERDAY)
        person = _make_person_record(record_id, url)
        csv_text = _make_csv_text(_make_sent_row(url=url, timestamp=_TODAY))
        csv_url = "https://phantombuster.s3.amazonaws.com/x/y/result.csv"

        attio, pb = _setup_attio_and_pb([entry], {record_id: person}, csv_text, csv_url)

        with patch("workflows.pb_send_recovery._download_csv", return_value=csv_text):
            result = recover_unrecorded_dm_sends(
                attio, pb, _MSG_SENDER_ID, _LIST_ID, _TODAY, dry_run=False
            )

        assert result["recovered"] == 0
        assert result["skipped_no_transition"] == 1
        attio.update_list_entry.assert_not_called()


class TestDryRun:
    """Scenario 6: dry_run=True → compute summary, zero update_list_entry."""

    def test_dry_run_computes_but_does_not_write(self) -> None:
        url = "https://linkedin.com/in/test-user"
        record_id = "rec-001"
        entry = _make_entry("entry-001", record_id, "DM1 Sent", last_contact_date=_YESTERDAY)
        person = _make_person_record(record_id, url)
        csv_text = _make_csv_text(_make_sent_row(url=url, timestamp=_TODAY))
        csv_url = "https://phantombuster.s3.amazonaws.com/x/y/result.csv"

        attio, pb = _setup_attio_and_pb([entry], {record_id: person}, csv_text, csv_url)

        with patch("workflows.pb_send_recovery._download_csv", return_value=csv_text):
            result = recover_unrecorded_dm_sends(
                attio, pb, _MSG_SENDER_ID, _LIST_ID, _TODAY, dry_run=True
            )

        # Summary shows 1 would-be recovery
        assert result["recovered"] == 1
        assert result["examined"] == 1
        # But NO actual Attio write
        attio.update_list_entry.assert_not_called()


class TestNoCsvLine:
    """Scenario 7: PB output has no 'CSV saved at' line → return early."""

    def test_no_csv_url_returns_empty_summary(self) -> None:
        attio = MagicMock()
        pb = MagicMock()
        # Output with no CSV line
        pb.get_output.return_value = {"output": "No sends today.\n", "status": "finished"}

        result = recover_unrecorded_dm_sends(
            attio, pb, _MSG_SENDER_ID, _LIST_ID, _TODAY, dry_run=False
        )

        assert result["examined"] == 0
        assert result["recovered"] == 0
        # Should not even query list entries
        attio.query_list_entries.assert_not_called()
        attio.update_list_entry.assert_not_called()

    def test_empty_output_returns_early(self) -> None:
        attio = MagicMock()
        pb = MagicMock()
        pb.get_output.return_value = {}  # no 'output' key

        result = recover_unrecorded_dm_sends(
            attio, pb, _MSG_SENDER_ID, _LIST_ID, _TODAY, dry_run=False
        )

        assert result["recovered"] == 0
        attio.query_list_entries.assert_not_called()


class TestMixedBatch:
    """Multiple rows: one recoverable, one failed, one already recorded."""

    def test_mixed_batch_counts(self) -> None:
        url_ok = "https://linkedin.com/in/user-ok"
        url_failed = "https://linkedin.com/in/user-fail"
        url_recorded = "https://linkedin.com/in/user-recorded"

        rid_ok = "rec-ok"
        rid_recorded = "rec-rec"

        entries = [
            _make_entry("eid-ok", rid_ok, "DM1 Sent", last_contact_date=_YESTERDAY),
            _make_entry("eid-rec", rid_recorded, "DM2 Sent", last_contact_date=_TODAY),
        ]
        person_map = {
            rid_ok: _make_person_record(rid_ok, url_ok),
            rid_recorded: _make_person_record(rid_recorded, url_recorded),
        }

        rows = [
            _make_sent_row(url=url_ok, timestamp=_TODAY),
            _make_failed_row(url=url_failed),
            _make_sent_row(url=url_recorded, timestamp=_TODAY),
        ]
        csv_text = _make_csv_text(*rows)
        csv_url = "https://phantombuster.s3.amazonaws.com/x/y/result.csv"

        attio, pb = _setup_attio_and_pb(entries, person_map, csv_text, csv_url)

        with patch("workflows.pb_send_recovery._download_csv", return_value=csv_text):
            result = recover_unrecorded_dm_sends(
                attio, pb, _MSG_SENDER_ID, _LIST_ID, _TODAY, dry_run=False
            )

        assert result["examined"] == 2      # ok + recorded (failed not counted)
        assert result["recovered"] == 1     # only url_ok
        assert result["skipped_already_recorded"] == 1
        assert result["skipped_failed"] == 1
        assert result["skipped_no_entry"] == 0  # url_failed is a failed row, not a no_entry

        attio.update_list_entry.assert_called_once()
        kwargs = attio.update_list_entry.call_args.kwargs
        assert kwargs["entry_id"] == "eid-ok"
        assert kwargs["entry_attributes"]["stage"] == PipelineStage.DM2_SENT.value


class TestUrlCanonicalization:
    """URL variant in CSV is canonicalized before matching."""

    def test_www_prefix_variant_still_matches(self) -> None:
        """CSV URL with www. matches entry stored without www."""
        canonical_url = "https://linkedin.com/in/test-user"
        csv_url_variant = "https://www.linkedin.com/in/test-user/"  # www + trailing slash

        record_id = "rec-001"
        entry = _make_entry("entry-001", record_id, "DM2 Sent", last_contact_date=_YESTERDAY)
        person = _make_person_record(record_id, canonical_url)

        csv_text = _make_csv_text(_make_sent_row(url=csv_url_variant, timestamp=_TODAY))
        csv_url = "https://phantombuster.s3.amazonaws.com/x/y/result.csv"

        attio, pb = _setup_attio_and_pb([entry], {record_id: person}, csv_text, csv_url)

        with patch("workflows.pb_send_recovery._download_csv", return_value=csv_text):
            result = recover_unrecorded_dm_sends(
                attio, pb, _MSG_SENDER_ID, _LIST_ID, _TODAY, dry_run=False
            )

        assert result["recovered"] == 1
        attio.update_list_entry.assert_called_once()


class TestWriteFailurePath:
    """The §3.15 AttioWriter routing splits write outcomes into two classes
    that recovery must handle differently:
      - infra failure (helper returns False after DLQ + attio_write_failed
        queue row): count distinctly, DO NOT count a false recovery, and
        keep processing the rest of the batch.
      - programmer-bug (registry / monotonicity / terminal-class): the
        helper RE-RAISES; recovery must let it propagate so the daily run
        halts (a mis-/non-advance would let the sequencer re-send = the §3.1
        duplicate-DM red line), not swallow it into skipped_write_failed.
    """

    def test_write_failure_counts_distinctly_and_continues(self) -> None:
        """Helper returns False → recovered stays 0, skipped_write_failed
        increments, and a SECOND eligible row is still attempted (the batch
        is not aborted on one infra failure)."""
        url1 = "https://linkedin.com/in/user-one"
        url2 = "https://linkedin.com/in/user-two"
        e1 = _make_entry("entry-001", "rec-001", "DM2 Sent", last_contact_date=_YESTERDAY)
        e2 = _make_entry("entry-002", "rec-002", "Accepted", last_contact_date=_YESTERDAY)
        p1 = _make_person_record("rec-001", url1)
        p2 = _make_person_record("rec-002", url2)
        csv_text = _make_csv_text(
            _make_sent_row(url=url1, timestamp=_TODAY),
            _make_sent_row(url=url2, timestamp=_TODAY),
        )
        csv_url = "https://phantombuster.s3.amazonaws.com/x/y/result.csv"
        attio, pb = _setup_attio_and_pb(
            [e1, e2], {"rec-001": p1, "rec-002": p2}, csv_text, csv_url
        )

        with patch(
            "workflows.pb_send_recovery._download_csv", return_value=csv_text
        ), patch(
            "workflows.daily_check._attio_advance_with_escalation",
            return_value=False,
        ) as adv:
            result = recover_unrecorded_dm_sends(
                attio, pb, _MSG_SENDER_ID, _LIST_ID, _TODAY, dry_run=False
            )

        assert adv.call_count == 2  # both eligible rows attempted, batch not aborted
        assert result["examined"] == 2
        assert result["recovered"] == 0  # no false recovery
        assert result["skipped_write_failed"] == 2

    def test_programmer_bug_exception_propagates(self) -> None:
        """A registry/monotonicity/terminal-class violation must NOT be
        absorbed into skipped_write_failed — it propagates out so the run
        halts (the cli call site re-raises it deliberately)."""
        from clients.attio_writer import UnauthorizedAttioWriteError

        url = "https://linkedin.com/in/user-one"
        entry = _make_entry("entry-001", "rec-001", "DM2 Sent", last_contact_date=_YESTERDAY)
        person = _make_person_record("rec-001", url)
        csv_text = _make_csv_text(_make_sent_row(url=url, timestamp=_TODAY))
        csv_url = "https://phantombuster.s3.amazonaws.com/x/y/result.csv"
        attio, pb = _setup_attio_and_pb(
            [entry], {"rec-001": person}, csv_text, csv_url
        )

        with patch(
            "workflows.pb_send_recovery._download_csv", return_value=csv_text
        ), patch(
            "workflows.daily_check._attio_advance_with_escalation",
            side_effect=UnauthorizedAttioWriteError("pb_send_recovery not registered"),
        ), pytest.raises(UnauthorizedAttioWriteError):
            recover_unrecorded_dm_sends(
                attio, pb, _MSG_SENDER_ID, _LIST_ID, _TODAY, dry_run=False
            )
