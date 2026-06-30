"""Tests for scripts/remediate_stale_connection_sent_20260615.py."""

from __future__ import annotations

import importlib

from models.pipeline import PipelineStage

mod = importlib.import_module("scripts.remediate_stale_connection_sent_20260615")


def _entry(stage: str, last_contact: str | None, rid: str = "rec-1") -> dict:
    return {
        "stage": stage,
        "last_contact_date": last_contact,
        "record_id": rid,
        "entry_id": f"ent-{rid}",
    }


class TestSelectStale:
    def test_selects_only_connection_sent_older_than_cutoff(self):
        cutoff = "2026-05-01"
        rows = [
            _entry(PipelineStage.CONNECTION_SENT.value, "2026-04-01", "old"),     # stale
            _entry(PipelineStage.CONNECTION_SENT.value, "2026-06-01", "recent"),  # within window
            _entry(PipelineStage.CONNECTION_SENT.value, "2026-05-01", "edge"),    # == cutoff, NOT stale
            _entry(PipelineStage.ACCEPTED.value, "2026-01-01", "accepted"),       # wrong stage
            _entry(PipelineStage.DM1_SENT.value, "2026-01-01", "dm1"),            # wrong stage
        ]
        selected = mod._select_stale(rows, cutoff)
        assert {r["record_id"] for r in selected} == {"old"}

    def test_missing_last_contact_date_is_not_selected(self):
        """No send date → cannot prove staleness → conservatively left alone."""
        cutoff = "2026-05-01"
        rows = [
            _entry(PipelineStage.CONNECTION_SENT.value, None, "none"),
            _entry(PipelineStage.CONNECTION_SENT.value, "", "empty"),
        ]
        assert mod._select_stale(rows, cutoff) == []

    def test_truncates_timestamp_to_date(self):
        cutoff = "2026-05-01"
        rows = [
            _entry(PipelineStage.CONNECTION_SENT.value, "2026-04-30T23:59:59Z", "ts"),
        ]
        assert {r["record_id"] for r in mod._select_stale(rows, cutoff)} == {"ts"}


def test_writer_module_is_registered_for_stage():
    """The script's writer_module must be an authorized `stage` writer, or
    every live write would raise UnauthorizedAttioWriteError."""
    from clients.attio_writer_registry import get_authorized_writers

    owners = get_authorized_writers("linkedin_outreach", "stage")
    assert owners is not None
    assert mod.WRITER_MODULE in owners
