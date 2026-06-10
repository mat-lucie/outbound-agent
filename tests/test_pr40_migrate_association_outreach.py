"""PR-40 tests — association_outreach JSON → Attio migration + the new
``stamp_outreach_channel`` writer on cross_channel_suppression."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from scripts.migrate_association_outreach_to_attio import (
    _deprecate_file,
    _load_contacts,
    _person_outreach_channels,
    _resolve_person,
    migrate_one,
)
from workflows.cross_channel_suppression import (
    OUTREACH_CHANNEL_ASSOCIATION,
    OUTREACH_CHANNEL_EMAIL,
    OUTREACH_CHANNEL_LINKEDIN,
    OUTREACH_CHANNELS,
    stamp_outreach_channel,
)

# ────────────────────────────────────────────────────────────────────────
# OUTREACH_CHANNELS invariants
# ────────────────────────────────────────────────────────────────────────


class TestOutreachChannelConstants:
    def test_three_channels(self):
        assert frozenset({"linkedin", "email", "association_outreach"}) == OUTREACH_CHANNELS

    def test_named_constants_match_set(self):
        assert OUTREACH_CHANNEL_LINKEDIN in OUTREACH_CHANNELS
        assert OUTREACH_CHANNEL_EMAIL in OUTREACH_CHANNELS
        assert OUTREACH_CHANNEL_ASSOCIATION in OUTREACH_CHANNELS


# ────────────────────────────────────────────────────────────────────────
# stamp_outreach_channel writer
# ────────────────────────────────────────────────────────────────────────


def _attio_with_channels(channels: list[str]) -> MagicMock:
    """Build an Attio mock whose Person record returns the given outreach_channel."""
    attio = MagicMock()
    # No get_person helper exists; PR-40 falls back to _request("GET", ...).
    del attio.get_person  # force the fallback path
    attio._request.return_value = {
        "data": {
            "id": {"record_id": "p-1"},
            "values": {
                "outreach_channel": [
                    {"option": {"title": c}} for c in channels
                ],
            },
        }
    }
    return attio


class TestStampOutreachChannel:
    def test_appends_new_channel(self):
        writer = MagicMock()
        attio = _attio_with_channels([OUTREACH_CHANNEL_LINKEDIN])
        out = stamp_outreach_channel(
            writer, attio,
            person_record_id="p-1",
            channel=OUTREACH_CHANNEL_EMAIL,
        )
        assert out is True
        writer.apply.assert_called_once()
        intent = writer.apply.call_args.args[0]
        assert intent.object == "people"
        assert intent.record_id == "p-1"
        # Union of existing + new, sorted
        assert intent.updates["outreach_channel"] == [
            OUTREACH_CHANNEL_EMAIL, OUTREACH_CHANNEL_LINKEDIN,
        ]
        assert intent.is_list_entry is False

    def test_idempotent_when_already_present(self):
        writer = MagicMock()
        attio = _attio_with_channels([OUTREACH_CHANNEL_EMAIL, OUTREACH_CHANNEL_LINKEDIN])
        out = stamp_outreach_channel(
            writer, attio,
            person_record_id="p-1",
            channel=OUTREACH_CHANNEL_EMAIL,
        )
        assert out is False
        writer.apply.assert_not_called()

    def test_invalid_channel_raises(self):
        writer = MagicMock()
        attio = _attio_with_channels([])
        with pytest.raises(ValueError, match="channel="):
            stamp_outreach_channel(
                writer, attio,
                person_record_id="p-1",
                channel="invalid_channel",
            )
        writer.apply.assert_not_called()

    def test_empty_prior_writes_single_channel(self):
        writer = MagicMock()
        attio = _attio_with_channels([])
        stamp_outreach_channel(
            writer, attio,
            person_record_id="p-1",
            channel=OUTREACH_CHANNEL_LINKEDIN,
        )
        intent = writer.apply.call_args.args[0]
        assert intent.updates["outreach_channel"] == [OUTREACH_CHANNEL_LINKEDIN]

    def test_writer_module_override_routes_to_caller(self):
        """The migration script needs to attribute writes to itself for
        forensic tracing — the writer_module kwarg lets it override."""
        writer = MagicMock()
        attio = _attio_with_channels([])
        stamp_outreach_channel(
            writer, attio,
            person_record_id="p-1",
            channel=OUTREACH_CHANNEL_LINKEDIN,
            writer_module="scripts.migrate_association_outreach_to_attio",
        )
        intent = writer.apply.call_args.args[0]
        assert intent.writer_module == "scripts.migrate_association_outreach_to_attio"

    def test_prior_fetch_lives_inside_writer_not_caller(self):
        """Footgun guard: caller does NOT pass prior_channels. The writer
        fetches the current state from Attio so a caller can't accidentally
        wipe other channels by passing an empty prior."""
        writer = MagicMock()
        attio = _attio_with_channels(["email"])  # Person already has email
        stamp_outreach_channel(
            writer, attio,
            person_record_id="p-1",
            channel=OUTREACH_CHANNEL_LINKEDIN,
        )
        intent = writer.apply.call_args.args[0]
        # The union MUST contain both — even though caller didn't supply prior.
        assert intent.updates["outreach_channel"] == ["email", "linkedin"]


# ────────────────────────────────────────────────────────────────────────
# _load_contacts + _resolve_person
# ────────────────────────────────────────────────────────────────────────


class TestLoadContacts:
    def test_missing_file_returns_empty(self, tmp_path):
        path = tmp_path / "missing.json"
        assert _load_contacts(path) == []

    def test_empty_contacts_returns_empty(self, tmp_path):
        path = tmp_path / "empty.json"
        path.write_text('{"_meta": {}, "contacts": []}')
        assert _load_contacts(path) == []

    def test_contacts_returned_as_list(self, tmp_path):
        path = tmp_path / "one.json"
        path.write_text('{"contacts": [{"email": "x@y.com"}]}')
        out = _load_contacts(path)
        assert out == [{"email": "x@y.com"}]


class TestResolvePerson:
    def test_email_match_takes_precedence(self):
        attio = MagicMock()
        attio.search_people.return_value = [{"id": {"record_id": "p-by-email"}}]
        attio.search_person_by_linkedin.return_value = {"id": {"record_id": "p-by-linkedin"}}
        out = _resolve_person(attio, {"email": "x@y.com", "linkedin": "https://l/x"})
        assert out["id"]["record_id"] == "p-by-email"
        attio.search_person_by_linkedin.assert_not_called()

    def test_linkedin_fallback_when_email_misses(self):
        attio = MagicMock()
        attio.search_people.return_value = []
        attio.search_person_by_linkedin.return_value = {"id": {"record_id": "p-by-linkedin"}}
        out = _resolve_person(attio, {"email": "x@y.com", "linkedin": "https://l/x"})
        assert out["id"]["record_id"] == "p-by-linkedin"

    def test_neither_returns_none(self):
        attio = MagicMock()
        attio.search_people.return_value = []
        attio.search_person_by_linkedin.return_value = None
        out = _resolve_person(attio, {"email": "x@y.com"})
        assert out is None


# ────────────────────────────────────────────────────────────────────────
# _person_outreach_channels parser
# ────────────────────────────────────────────────────────────────────────


class TestPersonOutreachChannelsParser:
    def test_no_values_returns_empty(self):
        assert _person_outreach_channels({}) == []

    def test_extracts_select_titles(self):
        record = {
            "values": {
                "outreach_channel": [
                    {"option": {"title": "linkedin"}},
                    {"option": {"title": "email"}},
                ]
            }
        }
        assert _person_outreach_channels(record) == ["linkedin", "email"]

    def test_handles_malformed_items_silently(self):
        # Malformed entries (non-dict) just don't contribute — the Attio
        # writer is the trusted source so corrupted reads stay benign.
        record = {
            "values": {
                "outreach_channel": [
                    "garbage",
                    {"option": {"title": "email"}},
                    {"no_option_key": "x"},
                ]
            }
        }
        assert _person_outreach_channels(record) == ["email"]


# ────────────────────────────────────────────────────────────────────────
# migrate_one
# ────────────────────────────────────────────────────────────────────────


def _person_record(record_id: str, channels: list[str]) -> dict:
    return {
        "id": {"record_id": record_id},
        "values": {
            "outreach_channel": [
                {"option": {"title": c}} for c in channels
            ]
        }
    }


class TestMigrateOne:
    def _attio_with_person(self, person: dict | None):
        attio = MagicMock()
        attio.search_people.return_value = [person] if person else []
        attio.search_person_by_linkedin.return_value = None
        # stamp_outreach_channel calls the writer's internal fetch
        # (get_person or _request) to read current outreach_channel.
        # Wire BOTH so the fold's prior-fetch path resolves correctly.
        del attio.get_person  # force the _request fallback path
        if person is not None:
            attio._request.return_value = {"data": person}
        else:
            attio._request.return_value = {"data": {}}
        return attio

    def _writer_mock(self):
        return MagicMock()

    def _run_mock(self):
        run = MagicMock()
        return run

    def test_patches_new_channel(self):
        attio = self._attio_with_person(_person_record("p-1", []))
        writer = self._writer_mock()
        run = self._run_mock()

        out = migrate_one(
            {"email": "x@y.com"},
            attio=attio, writer=writer, run=run, dry_run=False,
        )
        assert out == "patched"
        writer.apply.assert_called_once()
        run.examine.assert_called_once()
        run.mark_modified.assert_called_once_with(record_id="p-1")

    def test_idempotent_when_already_stamped(self):
        attio = self._attio_with_person(
            _person_record("p-1", [OUTREACH_CHANNEL_ASSOCIATION]),
        )
        writer = self._writer_mock()
        run = self._run_mock()

        out = migrate_one(
            {"email": "x@y.com"},
            attio=attio, writer=writer, run=run, dry_run=False,
        )
        assert out == "skipped_idempotent"
        writer.apply.assert_not_called()
        run.skip_idempotent.assert_called_once()

    def test_no_match_skips(self):
        attio = self._attio_with_person(None)
        writer = self._writer_mock()
        run = self._run_mock()

        out = migrate_one(
            {"email": "ghost@y.com"},
            attio=attio, writer=writer, run=run, dry_run=False,
        )
        assert out == "skipped_no_match"
        writer.apply.assert_not_called()

    def test_dry_run_does_not_patch(self):
        attio = self._attio_with_person(_person_record("p-1", []))
        writer = self._writer_mock()
        run = self._run_mock()

        out = migrate_one(
            {"email": "x@y.com"},
            attio=attio, writer=writer, run=run, dry_run=True,
        )
        assert out == "would_patch"
        writer.apply.assert_not_called()

    def test_failure_returns_failed_tag_not_reraised(self):
        """Per fold-in: migrate_one catches the exception, marks the run,
        and returns the 'failed' tag so the outer rename-gate can observe
        real failure counts. The MigrationRunWriter row still carries the
        forensic mark_failed call."""
        attio = self._attio_with_person(_person_record("p-1", []))
        writer = self._writer_mock()
        writer.apply.side_effect = RuntimeError("attio 500")
        run = self._run_mock()

        out = migrate_one(
            {"email": "x@y.com"},
            attio=attio, writer=writer, run=run, dry_run=False,
        )
        assert out == "failed"
        run.mark_failed.assert_called_once()


# ────────────────────────────────────────────────────────────────────────
# _deprecate_file rename
# ────────────────────────────────────────────────────────────────────────


class TestDeprecateFile:
    def test_renames_with_deprecated_suffix(self, tmp_path):
        path = tmp_path / "association_outreach.json"
        path.write_text('{"contacts": []}')
        target = _deprecate_file(path)
        assert target.name == "association_outreach.json.deprecated"
        assert target.exists()
        assert not path.exists()

    def test_idempotent_when_already_renamed(self, tmp_path):
        # A second run should not crash when the source is already gone.
        path = tmp_path / "association_outreach.json"
        target = path.with_suffix(path.suffix + ".deprecated")
        target.write_text('{"contacts": []}')
        # path does NOT exist, target does — second invocation
        out = _deprecate_file(path)
        assert out == target
