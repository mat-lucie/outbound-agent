"""PR-9b: dmN_sent_at backfill from PB history + Attio notes + last_contact_date.

Coverage:
  - Source-priority chain: PB-history present → wins; PB absent + notes present
    → notes wins; both absent + last_contact_date present → fallback wins;
    all absent → NULL output (explicit §0 #9 invariant).
  - Inference confidence assignments per priority level.
  - Idempotency: second run logs rows_modified == 0.
  - Soft-delete skip: merged_into != None rows are excluded from rows_modified
    (still counted in rows_examined).
  - ReclassificationRun/MigrationRun correlation: both rows share run_id.
  - NULL-output explicit: when all sources fail, the field is NOT written.
  - --dry-run flag: zero Attio writes, logs distribution.
  - --apply flag: writes and emits run rows.
  - Writer registry: scripts.backfill_per_step_timestamps is a declared
    writer for dm1/dm2/dm3_sent_at per §3.15.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.backfill_per_step_timestamps import (  # noqa: E402
    _infer_timestamps,  # noqa: E402
    _load_pb_history,  # noqa: E402
    _noon_utc,  # noqa: E402
    _normalize_url,  # noqa: E402
    _timestamps_from_last_contact,  # noqa: E402
    _timestamps_from_notes,  # noqa: E402
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _entry(**kwargs: Any) -> dict:
    """Build a parse_entry-shaped dict for testing."""
    base: dict = {
        "entry_id": "ent-001",
        "record_id": "rec-001",
        "stage": None,
        "dm_step": None,
        "last_contact_date": None,
        "dm1_sent_at": None,
        "dm2_sent_at": None,
        "dm3_sent_at": None,
        "canonical_linkedin_url": "https://www.linkedin.com/in/test-user",
        "merged_into": None,
    }
    base.update(kwargs)
    return base


def _mock_attio(notes_by_record: dict[str, list[dict]] | None = None) -> MagicMock:
    """Build a mock AttioClient with optional per-record note data."""
    attio = MagicMock()
    notes_by_record = notes_by_record or {}

    def _list_notes(record_id: str, parent_object: str = "people", limit: int = 50) -> list[dict]:
        return notes_by_record.get(record_id, [])

    attio.list_notes_for_record.side_effect = _list_notes
    attio.update_list_entry.return_value = {}

    def _parse_entry(raw: dict) -> dict:
        return raw

    attio.parse_entry = MagicMock(side_effect=_parse_entry)
    return attio


def _make_note(title: str, created_at: str = "2026-04-10T10:00:00Z") -> dict:
    return {
        "title": title,
        "created_at": created_at,
    }


# ── _normalize_url ────────────────────────────────────────────────────────────


class TestNormalizeUrl:
    def test_strips_trailing_slash(self):
        assert _normalize_url("https://www.linkedin.com/in/foo/") == \
            "https://www.linkedin.com/in/foo"

    def test_lowercases(self):
        assert _normalize_url("https://LinkedIn.com/in/Foo") == \
            "https://linkedin.com/in/foo"

    def test_none_returns_empty_string(self):
        assert _normalize_url(None) == ""

    def test_empty_string_returns_empty(self):
        assert _normalize_url("") == ""


# ── _load_pb_history ──────────────────────────────────────────────────────────


class TestLoadPbHistory:
    def test_returns_empty_when_dir_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "scripts.backfill_per_step_timestamps.PB_HISTORY_DIR",
            tmp_path / "nonexistent",
        )
        assert _load_pb_history() == {}

    def test_parses_valid_jsonl(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "scripts.backfill_per_step_timestamps.PB_HISTORY_DIR",
            tmp_path,
        )
        lines = [
            json.dumps({"url": "https://www.linkedin.com/in/alice", "step": "dm1", "sent_at": "2026-04-05T10:00:00Z"}),
            json.dumps({"url": "https://www.linkedin.com/in/alice", "step": "dm2", "sent_at": "2026-04-10T10:00:00Z"}),
            json.dumps({"url": "https://www.linkedin.com/in/bob", "step": "dm1", "sent_at": "2026-04-06T09:00:00Z"}),
        ]
        (tmp_path / "history-2026-04.jsonl").write_text("\n".join(lines) + "\n")
        result = _load_pb_history()
        assert result["https://www.linkedin.com/in/alice"]["dm1"] == "2026-04-05T10:00:00Z"
        assert result["https://www.linkedin.com/in/alice"]["dm2"] == "2026-04-10T10:00:00Z"
        assert result["https://www.linkedin.com/in/bob"]["dm1"] == "2026-04-06T09:00:00Z"

    def test_keeps_earliest_send_per_step(self, tmp_path, monkeypatch):
        """Two JSONL lines for the same URL+step → earliest wins (first actual send)."""
        monkeypatch.setattr(
            "scripts.backfill_per_step_timestamps.PB_HISTORY_DIR",
            tmp_path,
        )
        lines = [
            json.dumps({"url": "https://linkedin.com/in/alice", "step": "dm1", "sent_at": "2026-04-10T10:00:00Z"}),
            json.dumps({"url": "https://linkedin.com/in/alice", "step": "dm1", "sent_at": "2026-04-05T09:00:00Z"}),  # earlier
        ]
        (tmp_path / "history.jsonl").write_text("\n".join(lines) + "\n")
        result = _load_pb_history()
        assert result["https://linkedin.com/in/alice"]["dm1"] == "2026-04-05T09:00:00Z"

    def test_skips_malformed_lines(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "scripts.backfill_per_step_timestamps.PB_HISTORY_DIR",
            tmp_path,
        )
        lines = [
            "not json at all",
            json.dumps({"url": "https://linkedin.com/in/alice", "step": "dm1", "sent_at": "2026-04-05T09:00:00Z"}),
            json.dumps({"url": "", "step": "dm1", "sent_at": "2026-04-05T09:00:00Z"}),  # missing url
            json.dumps({"url": "https://linkedin.com/in/alice", "step": "dm4", "sent_at": "2026-04-05T09:00:00Z"}),  # invalid step
        ]
        (tmp_path / "history.jsonl").write_text("\n".join(lines) + "\n")
        result = _load_pb_history()
        assert "https://linkedin.com/in/alice" in result
        assert "dm4" not in result.get("https://linkedin.com/in/alice", {})

    def test_empty_dir_returns_empty_dict(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "scripts.backfill_per_step_timestamps.PB_HISTORY_DIR",
            tmp_path,
        )
        assert _load_pb_history() == {}


# ── _timestamps_from_notes ────────────────────────────────────────────────────


class TestTimestampsFromNotes:
    def test_dm1_note_found(self):
        attio = _mock_attio({"rec-001": [_make_note("DM 1 sent", "2026-04-08T10:00:00Z")]})
        result = _timestamps_from_notes(attio, "rec-001")
        assert result["dm1_sent_at"] == "2026-04-08T10:00:00Z"
        assert result["dm2_sent_at"] is None
        assert result["dm3_sent_at"] is None

    def test_multiple_notes_each_captured(self):
        attio = _mock_attio({
            "rec-001": [
                _make_note("DM 1 sent", "2026-04-05T10:00:00Z"),
                _make_note("DM 2 sent", "2026-04-12T10:00:00Z"),
                _make_note("DM 3 sent", "2026-04-19T10:00:00Z"),
            ]
        })
        result = _timestamps_from_notes(attio, "rec-001")
        assert result["dm1_sent_at"] == "2026-04-05T10:00:00Z"
        assert result["dm2_sent_at"] == "2026-04-12T10:00:00Z"
        assert result["dm3_sent_at"] == "2026-04-19T10:00:00Z"

    def test_case_insensitive_title_matching(self):
        attio = _mock_attio({"rec-001": [_make_note("dm1 sent", "2026-04-05T10:00:00Z")]})
        result = _timestamps_from_notes(attio, "rec-001")
        assert result["dm1_sent_at"] == "2026-04-05T10:00:00Z"

    def test_unrecognised_title_ignored(self):
        attio = _mock_attio({"rec-001": [_make_note("Some other note", "2026-04-05T10:00:00Z")]})
        result = _timestamps_from_notes(attio, "rec-001")
        assert all(v is None for v in result.values())

    def test_attio_error_returns_all_none(self):
        attio = MagicMock()
        attio.list_notes_for_record.side_effect = ConnectionError("attio down")
        result = _timestamps_from_notes(attio, "rec-001")
        assert all(v is None for v in result.values())

    def test_empty_notes_returns_all_none(self):
        attio = _mock_attio({"rec-001": []})
        result = _timestamps_from_notes(attio, "rec-001")
        assert all(v is None for v in result.values())


# ── _timestamps_from_last_contact ─────────────────────────────────────────────


class TestTimestampsFromLastContact:
    def test_dm3_stage_uses_lcd(self):
        entry = _entry(stage="DM3 Sent", dm_step="dm3", last_contact_date="2026-04-20")
        result = _timestamps_from_last_contact(entry)
        assert result["dm3_sent_at"] == _noon_utc(
            __import__("datetime").date(2026, 4, 20)
        )
        assert result["dm1_sent_at"] is None
        assert result["dm2_sent_at"] is None

    def test_dm2_stage_uses_lcd(self):
        entry = _entry(stage="DM2 Sent", dm_step="dm2", last_contact_date="2026-04-15")
        result = _timestamps_from_last_contact(entry)
        assert result["dm2_sent_at"] is not None
        assert result["dm1_sent_at"] is None
        assert result["dm3_sent_at"] is None

    def test_dm1_stage_uses_lcd(self):
        entry = _entry(stage="DM1 Sent", dm_step="dm1", last_contact_date="2026-04-10")
        result = _timestamps_from_last_contact(entry)
        assert result["dm1_sent_at"] is not None
        assert result["dm2_sent_at"] is None
        assert result["dm3_sent_at"] is None

    def test_missing_lcd_returns_all_none(self):
        entry = _entry(stage="DM1 Sent", dm_step="dm1", last_contact_date=None)
        result = _timestamps_from_last_contact(entry)
        assert all(v is None for v in result.values())

    def test_no_dm_stage_returns_all_none(self):
        entry = _entry(stage="Accepted", dm_step=None, last_contact_date="2026-04-10")
        result = _timestamps_from_last_contact(entry)
        assert all(v is None for v in result.values())

    def test_dm_step_takes_priority_over_stage_title(self):
        """dm_step is the authoritative signal; stage title is fallback."""
        entry = _entry(stage="DM1 Sent", dm_step="dm3", last_contact_date="2026-04-20")
        result = _timestamps_from_last_contact(entry)
        # dm_step="dm3" → dm3_sent_at should be populated.
        assert result["dm3_sent_at"] is not None
        assert result["dm1_sent_at"] is None


# ── _infer_timestamps — source-priority chain ─────────────────────────────────


class TestInferTimestamps:
    def test_pb_history_wins_when_present(self, tmp_path, monkeypatch):
        """Priority 1: PB history supplies all three timestamps → confidence HIGH."""
        monkeypatch.setattr(
            "scripts.backfill_per_step_timestamps.PB_HISTORY_DIR",
            tmp_path,
        )
        url = "https://www.linkedin.com/in/alice"
        pb_data = {
            "dm1": "2026-04-05T10:00:00Z",
            "dm2": "2026-04-12T10:00:00Z",
            "dm3": "2026-04-19T10:00:00Z",
        }
        (tmp_path / "h.jsonl").write_text(
            "\n".join(
                json.dumps({"url": url, "step": k, "sent_at": v})
                for k, v in pb_data.items()
            )
        )
        pb_history = _load_pb_history()

        entry = _entry(
            dm1_sent_at=None,
            dm2_sent_at=None,
            dm3_sent_at=None,
            dm_step="dm3",
            stage="DM3 Sent",
            last_contact_date="2026-04-20",
            canonical_linkedin_url=url,
        )
        attio = _mock_attio()
        updates, confidence = _infer_timestamps(entry, pb_history, attio, dry_run=True)

        assert confidence == "high"
        assert updates["dm1_sent_at"] == "2026-04-05T10:00:00Z"
        assert updates["dm2_sent_at"] == "2026-04-12T10:00:00Z"
        assert updates["dm3_sent_at"] == "2026-04-19T10:00:00Z"

    def test_notes_win_when_pb_absent(self):
        """Priority 2: no PB history, notes have dm1 → notes wins, confidence MEDIUM."""
        entry = _entry(
            dm1_sent_at=None,
            dm2_sent_at=None,
            dm3_sent_at=None,
            dm_step="dm1",
            stage="DM1 Sent",
            last_contact_date="2026-04-10",
            canonical_linkedin_url="https://www.linkedin.com/in/bob",
            record_id="rec-bob",
        )
        attio = _mock_attio({
            "rec-bob": [_make_note("DM 1 sent", "2026-04-08T10:00:00Z")]
        })
        updates, confidence = _infer_timestamps(entry, {}, attio, dry_run=False)
        assert confidence == "medium"
        assert updates.get("dm1_sent_at") == "2026-04-08T10:00:00Z"

    def test_lcd_fallback_when_pb_and_notes_absent(self):
        """Priority 3: no PB, no notes → last_contact_date fallback, confidence LOW."""
        entry = _entry(
            dm1_sent_at=None,
            dm2_sent_at=None,
            dm3_sent_at=None,
            dm_step="dm2",
            stage="DM2 Sent",
            last_contact_date="2026-04-15",
            canonical_linkedin_url="https://www.linkedin.com/in/carol",
            record_id="rec-carol",
        )
        attio = _mock_attio()  # no notes
        updates, confidence = _infer_timestamps(entry, {}, attio, dry_run=False)
        assert confidence == "low"
        # Only dm2 should be filled (the highest step implied by dm_step).
        assert updates.get("dm2_sent_at") is not None
        assert "dm1_sent_at" not in updates or updates["dm1_sent_at"] is None

    def test_all_sources_fail_returns_empty_updates_and_none_confidence(self):
        """§0 #9 invariant: when ALL sources fail, updates is empty, confidence='none'."""
        entry = _entry(
            dm1_sent_at=None,
            dm2_sent_at=None,
            dm3_sent_at=None,
            dm_step=None,
            stage="DM1 Sent",
            last_contact_date=None,  # no fallback
            canonical_linkedin_url=None,  # no PB history key
            record_id="rec-nobody",
        )
        attio = _mock_attio()  # empty notes
        updates, confidence = _infer_timestamps(entry, {}, attio, dry_run=False)
        assert confidence == "none"
        assert updates == {}, (
            "§0 #9: all_sources_failed must produce empty updates, "
            "NOT a fabricated default timestamp."
        )

    def test_all_already_set_returns_empty_updates_and_skipped_confidence(self):
        """Idempotency: when all three already set, return empty updates and
        the distinct 'skipped' confidence (PR-9b fold-in IMP #3): already-set
        rows must NOT inflate the 'high' counter — 'high' is reserved for
        rows that actually consumed PB-tier data via inference."""
        entry = _entry(
            dm1_sent_at="2026-04-05T10:00:00Z",
            dm2_sent_at="2026-04-12T10:00:00Z",
            dm3_sent_at="2026-04-19T10:00:00Z",
        )
        attio = _mock_attio()
        updates, confidence = _infer_timestamps(entry, {}, attio, dry_run=True)
        assert updates == {}
        assert confidence == "skipped", (
            "Already-set rows must return 'skipped' (not 'high') — "
            "confidence_high must reflect rows that actually used PB data."
        )

    def test_partial_fill_only_missing_slots(self):
        """dm1 already set; dm2 can be inferred via notes; dm3 absent → partial fill."""
        entry = _entry(
            dm1_sent_at="2026-04-05T10:00:00Z",  # already set
            dm2_sent_at=None,
            dm3_sent_at=None,
            dm_step="dm2",
            stage="DM2 Sent",
            canonical_linkedin_url=None,
            record_id="rec-partial",
        )
        attio = _mock_attio({
            "rec-partial": [_make_note("DM 2 sent", "2026-04-12T10:00:00Z")]
        })
        updates, confidence = _infer_timestamps(entry, {}, attio, dry_run=False)
        # dm1 was already set — must NOT appear in updates.
        assert "dm1_sent_at" not in updates
        assert updates.get("dm2_sent_at") == "2026-04-12T10:00:00Z"

    def test_pb_takes_priority_over_notes(self, tmp_path, monkeypatch):
        """PB history beats Attio notes when both are present."""
        monkeypatch.setattr(
            "scripts.backfill_per_step_timestamps.PB_HISTORY_DIR",
            tmp_path,
        )
        url = "https://www.linkedin.com/in/dave"
        (tmp_path / "h.jsonl").write_text(
            json.dumps({"url": url, "step": "dm1", "sent_at": "2026-04-05T08:00:00Z"}) + "\n"
        )
        pb_history = _load_pb_history()
        entry = _entry(
            dm1_sent_at=None,
            dm2_sent_at=None,
            dm3_sent_at=None,
            canonical_linkedin_url=url,
            record_id="rec-dave",
        )
        attio = _mock_attio({
            "rec-dave": [_make_note("DM 1 sent", "2026-04-10T10:00:00Z")]  # later date
        })
        updates, confidence = _infer_timestamps(entry, pb_history, attio, dry_run=False)
        # PB provides 2026-04-05; notes provide 2026-04-10. PB should win.
        assert confidence == "high"
        assert updates.get("dm1_sent_at") == "2026-04-05T08:00:00Z"

    def test_pb_dry_run_skips_notes_lookup(self):
        """In dry_run mode notes should NOT be fetched (network avoidance)."""
        attio = MagicMock()
        entry = _entry(
            dm1_sent_at=None,
            dm2_sent_at=None,
            dm3_sent_at=None,
            canonical_linkedin_url=None,
            record_id="rec-x",
        )
        _infer_timestamps(entry, {}, attio, dry_run=True)
        attio.list_notes_for_record.assert_not_called()


# ── _backfill_timestamps — integration-level (via module-level function) ─────


class TestBackfillTimestampsIntegration:
    """Integration tests exercising the full _backfill_timestamps loop with
    mock Attio and mocked run writers."""

    def _mk_writer(self) -> MagicMock:
        """Build a MigrationRunWriter-shaped mock."""
        w = MagicMock()
        w.run_id = "mig-test-0001"
        w.rows_examined = 0
        w.rows_modified = 0
        w.rows_skipped_idempotent = 0
        w.rows_failed = 0

        def _examine():
            w.rows_examined += 1

        def _skip_idem():
            w.rows_skipped_idempotent += 1

        def _mark_mod(**_):
            w.rows_modified += 1

        def _mark_fail(**_):
            w.rows_failed += 1

        w.examine.side_effect = _examine
        w.skip_idempotent.side_effect = _skip_idem
        w.mark_modified.side_effect = _mark_mod
        w.mark_failed.side_effect = _mark_fail
        return w

    def _mk_rec_writer(self) -> MagicMock:
        """Build a ReclassificationRunWriter-shaped mock."""
        w = MagicMock()
        w.run_id = "rec-test-0001"
        return w

    def test_soft_deleted_rows_counted_in_examined_not_modified(self):
        """merged_into != None → examined, NOT modified (soft-delete skip)."""
        from scripts.backfill_per_step_timestamps import _backfill_timestamps

        raw_entries = [
            _entry(
                entry_id="ent-loser",
                record_id="rec-loser",
                merged_into="rec-winner",
                dm1_sent_at=None,
                dm2_sent_at=None,
                dm3_sent_at=None,
            ),
        ]

        attio = MagicMock()
        attio.query_list_entries.return_value = raw_entries

        # Patch parse_entry at module level so _backfill_timestamps gets back
        # the raw dict unmodified (our dicts already have the right shape).
        with patch("clients.attio.AttioClient.parse_entry", side_effect=lambda e: e):
            mig = self._mk_writer()
            rec = self._mk_rec_writer()

            summary = _backfill_timestamps(
                attio, rec, mig,
                list_id="list-001",
                dry_run=True,
            )

        assert summary["rows_examined"] == 1
        assert summary["rows_skipped_soft_deleted"] == 1
        assert mig.rows_modified == 0
        attio.update_list_entry.assert_not_called()

    def test_idempotent_rows_skipped(self):
        """Rows with all three already set → skipped, rows_modified == 0."""
        from scripts.backfill_per_step_timestamps import _backfill_timestamps

        raw_entries = [
            _entry(
                entry_id="ent-full",
                dm1_sent_at="2026-04-05T10:00:00Z",
                dm2_sent_at="2026-04-12T10:00:00Z",
                dm3_sent_at="2026-04-19T10:00:00Z",
            ),
        ]

        attio = MagicMock()
        attio.query_list_entries.return_value = raw_entries
        mig = self._mk_writer()
        rec = self._mk_rec_writer()

        with patch("clients.attio.AttioClient.parse_entry", side_effect=lambda e: e):
            summary = _backfill_timestamps(
                attio, rec, mig,
                list_id="list-001",
                dry_run=False,
            )

        assert summary["rows_skipped_idempotent"] == 1
        assert mig.rows_modified == 0
        attio.update_list_entry.assert_not_called()

    def test_all_sources_fail_no_write(self):
        """§0 #9: when all sources fail, update_list_entry is NOT called."""
        from scripts.backfill_per_step_timestamps import _backfill_timestamps

        raw_entries = [
            _entry(
                entry_id="ent-no-source",
                record_id="rec-no-source",
                dm1_sent_at=None,
                dm2_sent_at=None,
                dm3_sent_at=None,
                stage="DM1 Sent",
                dm_step=None,
                last_contact_date=None,
                canonical_linkedin_url=None,
            ),
        ]
        attio = MagicMock()
        attio.query_list_entries.return_value = raw_entries
        attio.list_notes_for_record.return_value = []

        mig = self._mk_writer()
        rec = self._mk_rec_writer()

        with patch("clients.attio.AttioClient.parse_entry", side_effect=lambda e: e):
            summary = _backfill_timestamps(
                attio, rec, mig,
                list_id="list-001",
                dry_run=False,
            )

        attio.update_list_entry.assert_not_called()
        assert mig.rows_modified == 0
        assert summary["confidence_none"] == 1

    def test_lcd_fallback_triggers_write(self):
        """last_contact_date fallback → update_list_entry called with inferred ts."""
        from scripts.backfill_per_step_timestamps import _backfill_timestamps

        raw_entries = [
            _entry(
                entry_id="ent-lcd",
                record_id="rec-lcd",
                dm1_sent_at=None,
                dm2_sent_at=None,
                dm3_sent_at=None,
                stage="DM1 Sent",
                dm_step="dm1",
                last_contact_date="2026-04-10",
                canonical_linkedin_url=None,
            ),
        ]
        attio = MagicMock()
        attio.query_list_entries.return_value = raw_entries
        attio.list_notes_for_record.return_value = []
        attio.update_list_entry.return_value = {}

        mig = self._mk_writer()
        rec = self._mk_rec_writer()

        with patch("clients.attio.AttioClient.parse_entry", side_effect=lambda e: e):
            summary = _backfill_timestamps(
                attio, rec, mig,
                list_id="list-001",
                dry_run=False,
            )

        attio.update_list_entry.assert_called_once()
        call_kwargs = attio.update_list_entry.call_args.kwargs
        assert "dm1_sent_at" in call_kwargs["entry_attributes"]
        assert summary["confidence_low"] == 1

    def test_dry_run_does_not_call_update_list_entry(self):
        """--dry-run: zero writes to Attio even when timestamps can be inferred."""
        from scripts.backfill_per_step_timestamps import _backfill_timestamps

        raw_entries = [
            _entry(
                entry_id="ent-dryrun",
                record_id="rec-dryrun",
                dm1_sent_at=None,
                dm2_sent_at=None,
                dm3_sent_at=None,
                stage="DM1 Sent",
                dm_step="dm1",
                last_contact_date="2026-04-10",
                canonical_linkedin_url=None,
            ),
        ]
        attio = MagicMock()
        attio.query_list_entries.return_value = raw_entries
        attio.list_notes_for_record.return_value = []
        attio.update_list_entry.return_value = {}

        mig = self._mk_writer()
        rec = self._mk_rec_writer()

        with patch("clients.attio.AttioClient.parse_entry", side_effect=lambda e: e):
            _backfill_timestamps(
                attio, rec, mig,
                list_id="list-001",
                dry_run=True,
            )

        attio.update_list_entry.assert_not_called()

    def test_attio_write_error_marks_failed(self):
        """If update_list_entry raises, mark_failed is called and rows_modified stays 0."""
        from scripts.backfill_per_step_timestamps import _backfill_timestamps

        raw_entries = [
            _entry(
                entry_id="ent-err",
                record_id="rec-err",
                dm1_sent_at=None,
                dm_step="dm1",
                stage="DM1 Sent",
                last_contact_date="2026-04-10",
                canonical_linkedin_url=None,
            ),
        ]
        attio = MagicMock()
        attio.query_list_entries.return_value = raw_entries
        attio.list_notes_for_record.return_value = []
        attio.update_list_entry.side_effect = ConnectionError("attio down")

        mig = self._mk_writer()
        rec = self._mk_rec_writer()

        with patch("clients.attio.AttioClient.parse_entry", side_effect=lambda e: e):
            summary = _backfill_timestamps(
                attio, rec, mig,
                list_id="list-001",
                dry_run=False,
            )

        assert summary["rows_failed"] == 1
        assert mig.rows_modified == 0


# ── ReclassificationRun / MigrationRun correlation ─────────────────────────


class TestRunCorrelation:
    """Both run rows must share the same run_id — the rec_run.run_id is
    passed as a correlation key to the mig_run (materialised in the PR via
    the audit_log_path convention).
    """

    def test_reclassification_run_created_before_migration_run(self):
        """The ReclassificationRunWriter context must be opened first, so its
        run_id is available for the MigrationRunWriter constructor.
        This is a structural assertion on the main() flow — we test it by
        instantiating both writers in a mock session and checking that the
        rec_run.run_id is non-empty before the mig_run is entered.
        """
        from workflows.migration_run_writer import MigrationRunWriter
        from workflows.reclassification_run_writer import ReclassificationRunWriter

        mock_attio = MagicMock()
        mock_attio._request.return_value = {
            "data": {"id": {"record_id": "rec-test-abc"}}
        }

        rec_run_id = None

        with ReclassificationRunWriter(
            classifier_module="scripts.backfill_per_step_timestamps",
            model_used="none",
            input_attr="canonical_linkedin_url",
            output_attr="dm1_sent_at,dm2_sent_at,dm3_sent_at",
            dry_run=True,
            attio=mock_attio,
        ) as rec:
            rec_run_id = rec.run_id
            with MigrationRunWriter(
                script_name="scripts/backfill_per_step_timestamps.py",
                rollback_script_path="scripts/rollback_per_step_timestamps.py",
                dry_run=True,
                attio=mock_attio,
            ) as mig:
                # Both run_ids are non-empty at this point.
                assert rec_run_id is not None
                assert len(rec_run_id) > 0
                assert mig.run_id is not None
                assert len(mig.run_id) > 0


# ── Writer registry: scripts.backfill_per_step_timestamps ────────────────────


class TestWriterRegistry:
    """§3.15: scripts.backfill_per_step_timestamps must be registered as a
    multi-writer for dm1_sent_at, dm2_sent_at, dm3_sent_at.
    """

    @pytest.mark.parametrize("attr", ["dm1_sent_at", "dm2_sent_at", "dm3_sent_at"])
    def test_backfill_script_is_authorised_writer(self, attr: str):
        from clients.attio_writer_registry import is_authorized_writer
        assert is_authorized_writer(
            "linkedin_outreach",
            attr,
            "scripts.backfill_per_step_timestamps",
        ), (
            f"scripts.backfill_per_step_timestamps must be a registered "
            f"writer for linkedin_outreach.{attr} per §3.15 backfill exception."
        )

    @pytest.mark.parametrize("attr", ["dm1_sent_at", "dm2_sent_at", "dm3_sent_at"])
    def test_daily_check_remains_primary_writer(self, attr: str):
        from clients.attio_writer_registry import is_authorized_writer
        assert is_authorized_writer(
            "linkedin_outreach",
            attr,
            "workflows.daily_check.run_dm_sequencing",
        ), (
            f"workflows.daily_check.run_dm_sequencing must remain PRIMARY "
            f"writer for linkedin_outreach.{attr}"
        )


# ── NULL-output contract — §0 #9 ─────────────────────────────────────────────


class TestNullOutputContract:
    """Explicit tests for the §0 #9 invariant:
    NULL is the correct output when all sources fail.
    The field must NOT be written rather than defaulting to a sentinel.
    """

    def test_no_write_when_all_sources_fail(self):
        """Directly test _infer_timestamps: empty dict return, no default substitution."""
        entry = _entry(
            dm1_sent_at=None,
            dm2_sent_at=None,
            dm3_sent_at=None,
            stage="DM2 Sent",
            dm_step=None,
            last_contact_date=None,
            canonical_linkedin_url=None,
            record_id=None,
        )
        attio = _mock_attio()
        updates, confidence = _infer_timestamps(entry, {}, attio, dry_run=False)
        assert updates == {}
        assert confidence == "none"
        # Explicitly assert that no sentinel/default date was substituted.
        for v in updates.values():
            assert v is not None, (
                "§0 #9: updates dict should be empty, not populated with NULL values. "
                "Silent fallbacks are prohibited."
            )

    def test_updates_dict_contains_no_none_values_when_non_empty(self):
        """When updates IS non-empty, every value must be a real string (no None markers)."""
        entry = _entry(
            dm1_sent_at=None,
            dm2_sent_at=None,
            dm3_sent_at=None,
            stage="DM1 Sent",
            dm_step="dm1",
            last_contact_date="2026-04-10",
        )
        attio = _mock_attio()
        updates, _ = _infer_timestamps(entry, {}, attio, dry_run=False)
        for key, val in updates.items():
            assert val is not None, (
                f"{key} in updates must be a non-None string — "
                "absent slots are simply not included in the dict."
            )


# ── MigrationRunWriter compliance guard ─────────────────────────────────────


class TestMigrationWriterCompliance:
    """The CI guard test_migration_writer_compliance.py asserts that every
    scripts/backfill_*.py uses MigrationRunWriter. This test verifies that
    backfill_per_step_timestamps.py imports and uses MigrationRunWriter so
    it stays out of LEGACY_SCRIPTS.
    """

    def test_script_imports_migration_run_writer(self):
        import importlib
        mod = importlib.import_module("scripts.backfill_per_step_timestamps")
        assert hasattr(mod, "MigrationRunWriter") or "MigrationRunWriter" in dir(mod), (
            "scripts.backfill_per_step_timestamps must import MigrationRunWriter "
            "per the §3.13 compliance guard."
        )

    def test_script_imports_reclassification_run_writer(self):
        import importlib
        mod = importlib.import_module("scripts.backfill_per_step_timestamps")
        assert hasattr(mod, "ReclassificationRunWriter") or "ReclassificationRunWriter" in dir(mod), (
            "scripts.backfill_per_step_timestamps must import ReclassificationRunWriter "
            "per the §3.12 reclassification run protocol."
        )


# ── PR-9b fold-in: confidence-counter semantics (5-agent convergence) ───────


class TestMixedSourceConfidence:
    """PR-9b fold-in IMP #3a (5-agent convergence): when a single row mixes
    sources, the reported confidence must downgrade to the worst tier
    actually consumed — not stay at the first-contributor."""

    def test_pb_dm1_and_notes_dm2_reports_medium(self):
        """PB fills dm1 (HIGH); notes fill dm2 (MEDIUM). Row reports MEDIUM
        — the LCD downgrade rule applies to mixed sources too."""
        entry = _entry(
            dm1_sent_at=None,
            dm2_sent_at=None,
            dm3_sent_at=None,
            canonical_linkedin_url="https://www.linkedin.com/in/mixed",
            record_id="rec-mixed",
        )
        pb_history = {"https://www.linkedin.com/in/mixed": {"dm1": "2026-04-05T10:00:00Z"}}
        attio = _mock_attio({
            "rec-mixed": [_make_note("DM 2 sent", "2026-04-12T10:00:00Z")]
        })
        updates, confidence = _infer_timestamps(entry, pb_history, attio, dry_run=False)
        assert "dm1_sent_at" in updates
        assert "dm2_sent_at" in updates
        assert confidence == "medium", (
            "Mixed PB+notes must downgrade to the worst tier used (medium), "
            "not stay at the first contributor (high)."
        )

    def test_pb_dm1_and_lcd_dm2_reports_low(self):
        """PB fills dm1 (HIGH); LCD fills dm2 (LOW). Row reports LOW."""
        entry = _entry(
            dm1_sent_at=None,
            dm2_sent_at=None,
            dm3_sent_at=None,
            canonical_linkedin_url="https://www.linkedin.com/in/mixed2",
            record_id="rec-mixed2",
            dm_step="dm2",
            stage="DM2 Sent",
            last_contact_date="2026-04-15",
        )
        pb_history = {"https://www.linkedin.com/in/mixed2": {"dm1": "2026-04-05T10:00:00Z"}}
        attio = _mock_attio({"rec-mixed2": []})  # no notes
        updates, confidence = _infer_timestamps(entry, pb_history, attio, dry_run=False)
        assert "dm1_sent_at" in updates
        assert "dm2_sent_at" in updates
        assert confidence == "low", (
            "PB + LCD mix must downgrade to LOW (worst-used), "
            "not stay at HIGH (first contributor)."
        )

    def test_notes_dm1_and_lcd_dm2_reports_low(self):
        """Notes fill dm1 (MEDIUM); LCD fills dm2 (LOW). Row reports LOW."""
        entry = _entry(
            dm1_sent_at=None,
            dm2_sent_at=None,
            dm3_sent_at=None,
            canonical_linkedin_url=None,
            record_id="rec-notes-lcd",
            dm_step="dm2",
            stage="DM2 Sent",
            last_contact_date="2026-04-15",
        )
        attio = _mock_attio({
            "rec-notes-lcd": [_make_note("DM 1 sent", "2026-04-05T10:00:00Z")]
        })
        updates, confidence = _infer_timestamps(entry, {}, attio, dry_run=False)
        assert "dm1_sent_at" in updates
        assert "dm2_sent_at" in updates
        assert confidence == "low"


class TestIdempotentDoesNotInflateConfidenceHigh:
    """PR-9b fold-in IMP #3b: already-populated rows return the dedicated
    'skipped' confidence so the high/medium/low counters only reflect
    inference outcomes, not bypasses."""

    def test_summary_skipped_separate_from_high(self):
        """confidence_high counts ONLY rows that consumed PB data via
        inference. Already-set rows are counted in confidence_skipped."""
        from scripts.backfill_per_step_timestamps import _backfill_timestamps

        raw_entries = [
            _entry(
                entry_id="ent-already-set",
                record_id="rec-already-set",
                dm1_sent_at="2026-04-05T10:00:00Z",
                dm2_sent_at="2026-04-12T10:00:00Z",
                dm3_sent_at="2026-04-19T10:00:00Z",
            ),
        ]
        attio = MagicMock()
        attio.query_list_entries.return_value = raw_entries

        with patch("clients.attio.AttioClient.parse_entry", side_effect=lambda e: e):
            mig = MagicMock()
            mig.run_id = "mig-x"
            mig.rows_examined = 0
            mig.rows_modified = 0
            mig.rows_skipped_idempotent = 0
            mig.rows_failed = 0
            mig.examine.side_effect = lambda: None
            mig.skip_idempotent.side_effect = lambda: None
            mig.skip_excluded.side_effect = lambda **_: None
            mig.mark_modified.side_effect = lambda **_: None
            mig.mark_failed.side_effect = lambda **_: None
            rec = MagicMock()
            rec.run_id = "rec-x"

            summary = _backfill_timestamps(
                attio, rec, mig, list_id="list-001", dry_run=True,
            )
        assert summary["confidence_skipped"] == 1
        assert summary["confidence_high"] == 0, (
            "PR-9b fold-in IMP #3b: already-populated rows MUST NOT "
            "inflate the confidence_high counter."
        )


# ── PR-9b fold-in: soft-delete counter semantics ────────────────────────────


class TestSoftDeleteCounterSemantics:
    """PR-9b fold-in IMP #4 (5-agent convergence): soft-deleted (merged_into
    set) rows are excluded-from-scope, not idempotent. They must:
      - increment skip_excluded on the Migration Run (NOT skip_idempotent)
      - emit a rec_run.mark_abstain so batch_size accounting stays balanced
      - be surfaced separately in the stdout summary."""

    def test_soft_deleted_row_not_counted_as_idempotent(self):
        from scripts.backfill_per_step_timestamps import _backfill_timestamps

        raw_entries = [
            _entry(
                entry_id="ent-loser",
                record_id="rec-loser",
                merged_into="rec-winner",
                dm1_sent_at=None,
            ),
        ]
        attio = MagicMock()
        attio.query_list_entries.return_value = raw_entries

        with patch("clients.attio.AttioClient.parse_entry", side_effect=lambda e: e):
            mig = MagicMock()
            mig.examine.side_effect = lambda: None
            mig.skip_idempotent.side_effect = lambda: None
            mig.skip_excluded.side_effect = lambda **_: None
            mig.mark_modified.side_effect = lambda **_: None
            mig.mark_failed.side_effect = lambda **_: None
            rec = MagicMock()
            rec.run_id = "rec-x"

            _backfill_timestamps(
                attio, rec, mig, list_id="list-001", dry_run=True,
            )

        # The soft-deleted row must use skip_excluded, NOT skip_idempotent.
        mig.skip_excluded.assert_called_once()
        mig.skip_idempotent.assert_not_called()

    def test_soft_deleted_row_emits_rec_run_abstain(self):
        """rec_run.batch_size == abstain + success + error must hold —
        soft-deleted rows examined-but-not-modified must produce an
        abstain so the balance is preserved."""
        from scripts.backfill_per_step_timestamps import _backfill_timestamps

        raw_entries = [
            _entry(
                entry_id="ent-loser",
                record_id="rec-loser",
                merged_into="rec-winner",
                dm1_sent_at=None,
            ),
        ]
        attio = MagicMock()
        attio.query_list_entries.return_value = raw_entries

        with patch("clients.attio.AttioClient.parse_entry", side_effect=lambda e: e):
            mig = MagicMock()
            mig.examine.side_effect = lambda: None
            mig.skip_idempotent.side_effect = lambda: None
            mig.skip_excluded.side_effect = lambda **_: None
            rec = MagicMock()
            rec.run_id = "rec-x"

            _backfill_timestamps(
                attio, rec, mig, list_id="list-001", dry_run=True,
            )

        # rec_run.mark_abstain called for the soft-deleted row.
        rec.mark_abstain.assert_called_once()
        abstain_kwargs = rec.mark_abstain.call_args.kwargs
        assert abstain_kwargs["reason"] == "soft_deleted_loser"

    def test_skip_excluded_reason_carries_soft_delete_label(self):
        """The skip_excluded() reason must be 'soft_deleted_loser' so
        postmortem queries can filter on it."""
        from scripts.backfill_per_step_timestamps import _backfill_timestamps

        raw_entries = [
            _entry(entry_id="ent-loser", merged_into="rec-winner"),
        ]
        attio = MagicMock()
        attio.query_list_entries.return_value = raw_entries

        with patch("clients.attio.AttioClient.parse_entry", side_effect=lambda e: e):
            mig = MagicMock()
            mig.examine.side_effect = lambda: None
            mig.skip_excluded.side_effect = lambda **_: None
            rec = MagicMock()
            rec.run_id = "rec-x"
            _backfill_timestamps(attio, rec, mig, list_id="list-001", dry_run=True)

        kwargs = mig.skip_excluded.call_args.kwargs
        assert kwargs["reason"] == "soft_deleted_loser"


# ── PR-9b fold-in: run_id cross-reference (data-steward + math BLOCKING #1) ─


class TestRunIdCrossReference:
    """PR-9b fold-in BLOCKING #1 (data-steward + math 2-agent):
    main() must instantiate MigrationRunWriter with
    reclassification_run_id=rec_run.run_id so the Migration Run row
    carries a real Attio cross-reference (not just stderr log lines)."""

    def test_main_passes_rec_run_id_to_migration_run_writer(self):
        """The Migration Run row body must include reclassification_run_id
        equal to the rec_run.run_id from the same backfill invocation."""
        from scripts import backfill_per_step_timestamps as script_mod

        # Stub Attio so we can capture the Migration Run create payload.
        captured: dict = {}

        def _stub_request(method, path, json=None, **_kwargs):
            if method == "POST" and path.endswith("/objects/migration_run/records"):
                captured["mig_payload"] = json["data"]["values"]
                return {"data": {"id": {"record_id": "mig_row_001"}}}
            if method == "POST" and path.endswith("/objects/reclassification_run/records"):
                captured["rec_payload"] = json["data"]["values"]
                return {"data": {"id": {"record_id": "rec_row_001"}}}
            if method == "PATCH":
                return {"data": {}}
            return {"data": []}

        mock_attio = MagicMock()
        mock_attio._request.side_effect = _stub_request
        mock_attio.query_list_entries.return_value = []
        mock_attio.parse_entry.side_effect = lambda e: e

        class _FakeAttioClient:
            def __new__(cls, *args, **kwargs):
                return mock_attio

            @staticmethod
            def parse_entry(e):
                return e

        with patch.object(script_mod, "AttioClient", _FakeAttioClient):
            rc = script_mod.main(["--apply", "--accept-responder-censoring", "--list-id", "list-test"])

        assert rc == 0
        assert "mig_payload" in captured
        assert "rec_payload" in captured
        # The cross-reference must be the rec_run.run_id from THIS run.
        mig = captured["mig_payload"]
        rec = captured["rec_payload"]
        assert mig["reclassification_run_id"] == rec["run_id"], (
            "Migration Run row's reclassification_run_id must equal the "
            "Reclassification Run's run_id — this is the join key for "
            "forensics consumers (data-steward + math BLOCKING #1)."
        )


# ── PR-9b fold-in: main() exit codes (pr-test HIGH-1) ───────────────────────


class TestMainEntryPoint:
    """PR-9b fold-in (pr-test HIGH-1): main() must return distinct exit
    codes for success / partial / scope-failure paths so cron consumers
    can branch correctly."""

    def _stub_attio(self, *, entries=None, query_raises=None, update_raises=None):
        mock_attio = MagicMock()
        if query_raises is not None:
            mock_attio.query_list_entries.side_effect = query_raises
        else:
            mock_attio.query_list_entries.return_value = entries or []
        if update_raises is not None:
            mock_attio.update_list_entry.side_effect = update_raises
        else:
            mock_attio.update_list_entry.return_value = {}
        mock_attio.list_notes_for_record.return_value = []
        # parse_entry must be a passthrough so the flat _entry() dict survives.
        # The script accesses AttioClient.parse_entry as a CLASS method, but
        # patch.object(script_mod, "AttioClient", ...) replaces the class with
        # this MagicMock — so we wire the static method here too.
        mock_attio.parse_entry.side_effect = lambda e: e

        def _stub_request(method, path, json=None, **_kwargs):
            if method == "POST" and "migration_run/records" in path:
                return {"data": {"id": {"record_id": "mig_row"}}}
            if method == "POST" and "reclassification_run/records" in path:
                return {"data": {"id": {"record_id": "rec_row"}}}
            if method == "PATCH":
                return {"data": {}}
            return {"data": []}

        mock_attio._request.side_effect = _stub_request
        return mock_attio

    def _patched_attio_client(self, script_mod, mock_attio):
        """Return a fake AttioClient class: constructor returns mock_attio,
        and parse_entry is a real passthrough static method."""
        # Wrap so AttioClient() returns mock_attio AND AttioClient.parse_entry
        # is the real passthrough (not a MagicMock attribute that yields
        # a MagicMock for every key lookup).
        class _FakeAttioClient:
            def __new__(cls, *args, **kwargs):
                return mock_attio

            @staticmethod
            def parse_entry(e):
                return e

        return patch.object(script_mod, "AttioClient", _FakeAttioClient)

    def test_main_returns_EX_OK_on_success(self):
        from scripts import backfill_per_step_timestamps as script_mod
        attio = self._stub_attio(entries=[])
        with self._patched_attio_client(script_mod, attio):
            rc = script_mod.main(["--apply", "--accept-responder-censoring", "--list-id", "list-test"])
        assert rc == 0

    def test_main_returns_EX_PARTIAL_when_rows_fail(self):
        from scripts import backfill_per_step_timestamps as script_mod
        # One entry that needs filling; update_list_entry raises.
        entries = [
            _entry(
                entry_id="ent-fail",
                record_id="rec-fail",
                dm1_sent_at=None,
                dm_step="dm1",
                stage="DM1 Sent",
                last_contact_date="2026-04-10",
                canonical_linkedin_url=None,
            ),
        ]
        attio = self._stub_attio(
            entries=entries,
            update_raises=ConnectionError("attio down"),
        )
        # parse_entry passthrough so the flat _entry() dict survives the
        # AttioClient.parse_entry() call inside _backfill_timestamps.
        with self._patched_attio_client(script_mod, attio):
            rc = script_mod.main(["--apply", "--accept-responder-censoring", "--list-id", "list-test"])
        assert rc == 1, "Row-level failures must surface as EX_PARTIAL (1)."

    def test_main_returns_EX_TEMPFAIL_on_scope_failure(self):
        import httpx

        from scripts import backfill_per_step_timestamps as script_mod
        attio = self._stub_attio(
            query_raises=httpx.ConnectError("attio scope failure"),
        )
        with self._patched_attio_client(script_mod, attio):
            rc = script_mod.main(["--apply", "--accept-responder-censoring", "--list-id", "list-test"])
        assert rc == 75, (
            "Attio scope failure (cannot list entries) must surface as "
            "EX_TEMPFAIL (75)."
        )


# ── PR-9b fold-in: §9.4 second-full-run idempotency (pr-test HIGH-2) ────────


class TestSecondFullRunIsNoop:
    """PR-9b fold-in (pr-test HIGH-2): §9.4 idempotency contract — re-running
    the backfill after a successful first run must produce zero writes.
    The previous test_idempotent_rows_skipped only covered pre-populated
    rows; this version walks the full first-then-second sequence with
    actual writes captured in-between."""

    def test_second_full_run_writes_zero_rows(self, tmp_path, monkeypatch):
        from scripts.backfill_per_step_timestamps import _backfill_timestamps

        # Set up PB history that fills ALL three slots so the post-first-run
        # state is truly idempotent (all three populated). On the second run,
        # rows_skipped_idempotent must equal rows_examined.
        monkeypatch.setattr(
            "scripts.backfill_per_step_timestamps.PB_HISTORY_DIR",
            tmp_path,
        )
        url1 = "https://www.linkedin.com/in/alice"
        url2 = "https://www.linkedin.com/in/bob"
        (tmp_path / "h.jsonl").write_text("\n".join([
            json.dumps({"url": url1, "step": "dm1", "sent_at": "2026-04-05T10:00:00Z"}),
            json.dumps({"url": url1, "step": "dm2", "sent_at": "2026-04-12T10:00:00Z"}),
            json.dumps({"url": url1, "step": "dm3", "sent_at": "2026-04-19T10:00:00Z"}),
            json.dumps({"url": url2, "step": "dm1", "sent_at": "2026-04-06T10:00:00Z"}),
            json.dumps({"url": url2, "step": "dm2", "sent_at": "2026-04-13T10:00:00Z"}),
            json.dumps({"url": url2, "step": "dm3", "sent_at": "2026-04-20T10:00:00Z"}),
        ]) + "\n")

        # First-run state: 2 entries needing fill via PB history.
        first_entries = [
            _entry(
                entry_id="ent-1", record_id="rec-1",
                dm1_sent_at=None, dm2_sent_at=None, dm3_sent_at=None,
                canonical_linkedin_url=url1,
            ),
            _entry(
                entry_id="ent-2", record_id="rec-2",
                dm1_sent_at=None, dm2_sent_at=None, dm3_sent_at=None,
                canonical_linkedin_url=url2,
            ),
        ]

        # Simulated Attio store — update_list_entry mutates first_entries
        # in place so the second-run snapshot reflects the writes.
        attio = MagicMock()
        attio.query_list_entries.return_value = first_entries
        attio.list_notes_for_record.return_value = []

        def _apply_update(entry_id: str, entry_attributes: dict, list_id: str) -> dict:
            for e in first_entries:
                if e["entry_id"] == entry_id:
                    e.update(entry_attributes)
                    break
            return {}

        attio.update_list_entry.side_effect = _apply_update

        def _mk_writer():
            w = MagicMock()
            w.run_id = "x"
            w.rows_examined = 0
            w.rows_modified = 0
            w.rows_skipped_idempotent = 0
            w.rows_failed = 0
            w.examine.side_effect = lambda: None
            w.skip_idempotent.side_effect = lambda: None
            w.skip_excluded.side_effect = lambda **_: None
            w.mark_modified.side_effect = lambda **_: None
            w.mark_failed.side_effect = lambda **_: None
            return w

        with patch("clients.attio.AttioClient.parse_entry", side_effect=lambda e: e):
            # First run: rows are filled.
            first_summary = _backfill_timestamps(
                attio, _mk_writer(), _mk_writer(),
                list_id="list-001", dry_run=False,
            )
            first_call_count = attio.update_list_entry.call_count

            # Second run on the same snapshot: idempotent — zero writes.
            attio.update_list_entry.reset_mock()
            second_summary = _backfill_timestamps(
                attio, _mk_writer(), _mk_writer(),
                list_id="list-001", dry_run=False,
            )

        assert first_summary["rows_modified"] == 2, (
            "Sanity: first run must have written both rows for this test to be meaningful."
        )
        assert first_call_count == 2
        # The §9.4 idempotency contract: zero new writes on re-run AND
        # rows_skipped_idempotent == rows_examined (post-first-run all
        # three slots are populated, so the all-three-set fast-path fires).
        assert second_summary["rows_modified"] == 0
        assert second_summary["rows_skipped_idempotent"] == second_summary["rows_examined"]
        assert attio.update_list_entry.call_count == 0, (
            "§9.4: a second full run on the same state must emit zero "
            "update_list_entry calls."
        )


# ── PR-9b fold-in: stage-fallback dm_step=None case (silent-failure NIT-6) ──


class TestLastContactStageFallback:
    """PR-9b fold-in (silent-failure NIT-6): when dm_step is None but stage
    is 'DM2 Sent', the stage-title fallback path in
    _timestamps_from_last_contact must populate dm2_sent_at."""

    def test_stage_fallback_dm_step_none_stage_dm2(self):
        entry = _entry(
            stage="DM2 Sent", dm_step=None, last_contact_date="2026-04-15",
        )
        result = _timestamps_from_last_contact(entry)
        assert result["dm2_sent_at"] is not None, (
            "Stage='DM2 Sent' + dm_step=None must trigger the stage-title "
            "fallback in _timestamps_from_last_contact."
        )
        assert result["dm1_sent_at"] is None
        assert result["dm3_sent_at"] is None

    def test_stage_fallback_dm_step_none_stage_dm3(self):
        entry = _entry(
            stage="DM3 Sent", dm_step=None, last_contact_date="2026-04-20",
        )
        result = _timestamps_from_last_contact(entry)
        assert result["dm3_sent_at"] is not None


# ── PR-9b fold-in: PB line counters (silent-failure MED-5) ──────────────────


class TestPbHistoryLineCounters:
    """PR-9b fold-in (silent-failure MED-5): _load_pb_history takes an
    optional counters dict so operators can detect when a PB export
    schema change causes silent under-coverage."""

    def test_pb_line_counters_examined_and_malformed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "scripts.backfill_per_step_timestamps.PB_HISTORY_DIR",
            tmp_path,
        )
        lines = [
            json.dumps({"url": "https://linkedin.com/in/x", "step": "dm1", "sent_at": "2026-04-05T09:00:00Z"}),
            "not json at all",
            json.dumps({"url": "", "step": "dm1", "sent_at": "2026-04-05T09:00:00Z"}),  # missing url
            json.dumps({"url": "https://linkedin.com/in/y", "step": "dm5", "sent_at": "2026-04-05T09:00:00Z"}),  # invalid step
        ]
        (tmp_path / "h.jsonl").write_text("\n".join(lines) + "\n")
        counters: dict[str, int] = {}
        _load_pb_history(counters=counters)
        assert counters.get("pb_lines_examined") == 4
        assert counters.get("pb_lines_skipped_malformed") == 3

    def test_pb_line_counters_optional(self, tmp_path, monkeypatch):
        """Calling _load_pb_history without counters must still work
        (backward-compat for any caller that doesn't track them)."""
        monkeypatch.setattr(
            "scripts.backfill_per_step_timestamps.PB_HISTORY_DIR",
            tmp_path,
        )
        (tmp_path / "h.jsonl").write_text(
            json.dumps({"url": "https://linkedin.com/in/x", "step": "dm1", "sent_at": "2026-04-05T09:00:00Z"}) + "\n"
        )
        # No counters passed — must not raise.
        result = _load_pb_history()
        assert "https://linkedin.com/in/x" in result

    def test_pb_unreadable_file_logs_stderr_warning(self, tmp_path, monkeypatch, capsys):
        """When a JSONL file is unreadable (e.g., permission denied), a
        stderr WARNING surfaces the gap (silent-failure MED-4)."""
        monkeypatch.setattr(
            "scripts.backfill_per_step_timestamps.PB_HISTORY_DIR",
            tmp_path,
        )
        bad_file = tmp_path / "bad.jsonl"
        bad_file.write_text("")
        # Make it unreadable. Cleanup via tmp_path teardown.
        import os as _os
        _os.chmod(bad_file, 0o000)
        try:
            _load_pb_history()
            captured = capsys.readouterr()
            assert "WARNING" in captured.err
            assert "PB history file" in captured.err
        finally:
            _os.chmod(bad_file, 0o644)


# ── PR-9b fold-in: notes-tier stderr WARNING (silent-failure HIGH-1) ────────


class TestNotesTierStderrWarning:
    """PR-9b fold-in (silent-failure HIGH-1): when list_notes_for_record
    raises (transient Attio outage), a stderr WARNING fires so operators
    can see all notes-tier rows are silently demoting to LCD/none."""

    def test_attio_outage_emits_stderr_warning(self, capsys):
        attio = MagicMock()
        attio.list_notes_for_record.side_effect = ConnectionError("attio down")
        result = _timestamps_from_notes(attio, "rec-001")
        assert all(v is None for v in result.values())
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert "list_notes_for_record" in captured.err
        assert "rec-001" in captured.err


# ── PR-9b fold-in: narrow scope-failure except (silent-failure HIGH-2) ──────


class TestNarrowScopeFailureExcept:
    """PR-9b fold-in (silent-failure HIGH-2): code bugs (TypeError, etc.)
    must propagate; only Attio transient errors get wrapped to EX_TEMPFAIL."""

    def test_code_bug_propagates_not_wrapped(self):
        """A TypeError in query_list_entries (real code bug) must propagate
        rather than be converted to RuntimeError → EX_TEMPFAIL."""
        from scripts.backfill_per_step_timestamps import _backfill_timestamps

        attio = MagicMock()
        attio.query_list_entries.side_effect = TypeError("argument count mismatch")
        mig = MagicMock()
        mig.examine.side_effect = lambda: None
        rec = MagicMock()
        rec.run_id = "rec-x"

        # TypeError must NOT be caught by the narrow except.
        with pytest.raises(TypeError):
            _backfill_timestamps(attio, rec, mig, list_id="list-001", dry_run=True)

    def test_transient_http_error_is_wrapped(self):
        """A real httpx transient error gets wrapped to RuntimeError so
        main() can return EX_TEMPFAIL."""
        import httpx

        from scripts.backfill_per_step_timestamps import _backfill_timestamps

        attio = MagicMock()
        attio.query_list_entries.side_effect = httpx.ConnectError("attio temporarily down")
        mig = MagicMock()
        mig.examine.side_effect = lambda: None
        rec = MagicMock()
        rec.run_id = "rec-x"

        with pytest.raises(RuntimeError, match="failed to query list entries"):
            _backfill_timestamps(attio, rec, mig, list_id="list-001", dry_run=True)


class TestResponderCensoringGuard:
    """2026-07-15 guard: --apply without PB history rests on the
    last_contact_date source alone, which cannot stamp rows that left the
    DM{N} Sent stages (responders above all). Simulated on a historical
    cohort (252 DM'd / 47 responders): every responder received zero stamps, so
    per-step denominators inflated past SMALL_N_THRESHOLD while successes
    stayed structurally zero — enough for a wet learn to strike a terminal
    verdict on censored data. main() must refuse unless the operator
    explicitly accepts that risk."""

    def _stub_attio(self):
        mock_attio = MagicMock()
        mock_attio.query_list_entries.return_value = []
        mock_attio.list_notes_for_record.return_value = []
        mock_attio.parse_entry.side_effect = lambda e: e
        mock_attio._request.side_effect = lambda *a, **k: {"data": {}}
        return mock_attio

    def _patched(self, script_mod, mock_attio):
        class _FakeAttioClient:
            def __new__(cls, *args, **kwargs):
                return mock_attio

            @staticmethod
            def parse_entry(e):
                return e

        return patch.object(script_mod, "AttioClient", _FakeAttioClient)

    def _no_pb_history(self, script_mod, tmp_path):
        return patch.object(
            script_mod, "PB_HISTORY_DIR", tmp_path / "absent-pb-history"
        )

    def test_apply_refused_without_pb_history(self, tmp_path, capsys):
        from scripts import backfill_per_step_timestamps as script_mod

        with self._no_pb_history(script_mod, tmp_path):
            rc = script_mod.main(["--apply", "--list-id", "list-test"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "censoring" in err
        assert "--accept-responder-censoring" in err

    def test_apply_proceeds_with_override(self, tmp_path):
        from scripts import backfill_per_step_timestamps as script_mod

        attio = self._stub_attio()
        with self._no_pb_history(script_mod, tmp_path), \
                self._patched(script_mod, attio):
            rc = script_mod.main([
                "--apply", "--accept-responder-censoring",
                "--list-id", "list-test",
            ])
        assert rc == 0

    def test_apply_proceeds_when_pb_history_present(self, tmp_path):
        from scripts import backfill_per_step_timestamps as script_mod

        pb_dir = tmp_path / "pb_history"
        pb_dir.mkdir()
        (pb_dir / "run.jsonl").write_text(
            '{"url": "https://linkedin.com/in/x", "step": "dm1", '
            '"sent_at": "2026-06-01T12:00:00Z"}\n',
            encoding="utf-8",
        )
        attio = self._stub_attio()
        with patch.object(script_mod, "PB_HISTORY_DIR", pb_dir), \
                self._patched(script_mod, attio):
            rc = script_mod.main(["--apply", "--list-id", "list-test"])
        assert rc == 0

    def test_dry_run_unaffected_by_guard(self, tmp_path):
        from scripts import backfill_per_step_timestamps as script_mod

        attio = self._stub_attio()
        with self._no_pb_history(script_mod, tmp_path), \
                self._patched(script_mod, attio):
            rc = script_mod.main(["--dry-run", "--list-id", "list-test"])
        assert rc == 0

    def test_apply_refused_when_pb_history_all_malformed(self, tmp_path, capsys):
        """A dir that globs *.jsonl but parses to nothing (the MED-5
        schema-drift case) must be refused the same as an absent dir —
        the guard gates on _load_pb_history() content, not file presence."""
        from scripts import backfill_per_step_timestamps as script_mod

        pb_dir = tmp_path / "pb_history"
        pb_dir.mkdir()
        (pb_dir / "drifted.jsonl").write_text(
            'not json at all\n{"wrong": "shape"}\n', encoding="utf-8"
        )
        with patch.object(script_mod, "PB_HISTORY_DIR", pb_dir):
            rc = script_mod.main(["--apply", "--list-id", "list-test"])
        assert rc == 2
        assert "--accept-responder-censoring" in capsys.readouterr().err
