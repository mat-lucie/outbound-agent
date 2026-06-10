"""Tests for scripts/backfill_prospect_committed_at.py."""

from __future__ import annotations

from unittest.mock import MagicMock

from scripts.backfill_prospect_committed_at import (
    _backfill_prospect_committed_at,
    _entry_attrs,
    _parse_proxy_date,
)


def _entry(entry_id: str, *, values: dict | None = None, created_at: str | None = None) -> dict:
    """Build an Attio /entries/query-shaped dict."""
    return {
        "id": {"entry_id": entry_id, "record_id": f"rec-{entry_id}"},
        "parent_record_id": f"rec-{entry_id}",
        "created_at": created_at,
        "entry_values": values or {},
    }


def _vals(**kwargs) -> dict:
    """Build an `entry_values` dict in Attio's [{value: X}] shape."""
    return {k: [{"value": v}] for k, v in kwargs.items() if v is not None}


def _mock_attio_with_entries(entries: list[dict], *, captured: list[dict] | None = None) -> MagicMock:
    """Mock AttioClient: query_list_entries returns `entries`; update_list_entry
    appends the call to `captured` (if provided) for assertion."""
    attio = MagicMock()
    attio.query_list_entries.return_value = entries
    captured = captured if captured is not None else []
    def _update(*, entry_id, entry_attributes, list_id):
        captured.append({
            "entry_id": entry_id,
            "attrs": entry_attributes,
            "list_id": list_id,
        })
        return {"id": {"entry_id": entry_id}}
    attio.update_list_entry.side_effect = _update
    attio.captured = captured  # type: ignore[attr-defined]
    # _request is unused in this code path but MagicMock provides a stub.
    return attio


def _mock_run(*, dry_run: bool = False) -> MagicMock:
    """Mock MigrationRunWriter — record per-row methods."""
    run = MagicMock()
    run.dry_run = dry_run
    run.rows_examined = 0
    run.rows_modified = 0
    run.rows_skipped_idempotent = 0
    run.rows_failed = 0
    run.modified = []
    run.failed = []

    def _examine() -> None:
        run.rows_examined += 1
    def _skip_idempotent() -> None:
        run.rows_skipped_idempotent += 1
    def _mark_modified(*, record_id, object="linkedin_outreach") -> None:
        run.rows_modified += 1
        run.modified.append((record_id, object))
    def _mark_failed(*, record_id, error) -> None:
        run.rows_failed += 1
        run.failed.append((record_id, str(error)))

    run.examine.side_effect = _examine
    run.skip_idempotent.side_effect = _skip_idempotent
    run.mark_modified.side_effect = _mark_modified
    run.mark_failed.side_effect = _mark_failed
    return run


class TestEntryAttrs:
    def test_extracts_first_value(self):
        flat = _entry_attrs(_entry("e1", values=_vals(quality_score=70, dm_step=1)))
        assert flat == {"quality_score": 70, "dm_step": 1}

    def test_handles_missing_values(self):
        flat = _entry_attrs(_entry("e1"))
        assert flat == {}


class TestParseProxyDate:
    def test_parses_iso_date(self):
        from datetime import date

        assert _parse_proxy_date("2026-05-21") == date(2026, 5, 21)

    def test_parses_iso_datetime(self):
        from datetime import date

        assert _parse_proxy_date("2026-05-21T12:00:00Z") == date(2026, 5, 21)

    def test_garbage_returns_none(self):
        assert _parse_proxy_date("not-a-date") is None
        assert _parse_proxy_date("") is None
        assert _parse_proxy_date(None) is None


class TestBackfill:
    def test_skips_rows_with_existing_attr(self):
        """Per §9.4 idempotency: rows where prospect_committed_at is
        already set must skip with `run.skip_idempotent()`."""
        entries = [
            _entry("e1", values=_vals(prospect_committed_at="2026-05-01T00:00:00Z")),
            _entry("e2", values=_vals(prospect_committed_at="2026-05-15T00:00:00Z")),
        ]
        attio = _mock_attio_with_entries(entries)
        run = _mock_run(dry_run=False)

        summary = _backfill_prospect_committed_at(attio, run, list_id="lst-x")

        assert run.rows_examined == 2
        assert run.rows_skipped_idempotent == 2
        assert run.rows_modified == 0
        assert summary["needed_backfill"] == 0
        assert attio.captured == []  # no writes

    def test_fills_from_last_contact_date(self):
        entries = [
            _entry("e1", values=_vals(last_contact_date="2026-04-10")),
        ]
        attio = _mock_attio_with_entries(entries)
        run = _mock_run(dry_run=False)

        summary = _backfill_prospect_committed_at(attio, run, list_id="lst-x")

        assert summary["filled_from_last_contact_date"] == 1
        assert run.rows_modified == 1
        assert len(attio.captured) == 1
        written = attio.captured[0]["attrs"]
        assert written["invite_eligible_after"] == "2026-04-10"
        assert written["prospect_committed_at"].startswith("2026-04-10T12:00:00")

    def test_falls_back_to_created_at(self):
        entries = [
            _entry("e1", values=_vals(), created_at="2025-12-15T00:00:00Z"),
        ]
        attio = _mock_attio_with_entries(entries)
        run = _mock_run(dry_run=False)

        summary = _backfill_prospect_committed_at(attio, run, list_id="lst-x")

        assert summary["filled_from_created_at"] == 1
        assert run.rows_modified == 1
        written = attio.captured[0]["attrs"]
        assert written["invite_eligible_after"] == "2025-12-15"

    def test_both_missing_marks_failed(self):
        """No usable signal → mark_failed, no write. Operator can audit
        the gap; we don't fabricate a date."""
        entries = [
            _entry("e-empty", values=_vals(), created_at=None),
        ]
        attio = _mock_attio_with_entries(entries)
        run = _mock_run(dry_run=False)

        summary = _backfill_prospect_committed_at(attio, run, list_id="lst-x")

        assert run.rows_failed == 1
        assert run.rows_modified == 0
        assert attio.captured == []
        assert summary["needed_backfill"] == 1

    def test_dry_run_marks_but_does_not_write(self):
        entries = [
            _entry("e1", values=_vals(last_contact_date="2026-04-10")),
        ]
        attio = _mock_attio_with_entries(entries)
        run = _mock_run(dry_run=True)

        _backfill_prospect_committed_at(attio, run, list_id="lst-x")

        assert run.rows_modified == 1
        assert attio.captured == []  # NO writes under dry_run

    def test_idempotent_second_run(self):
        """First run fills the gap; second run with the SAME entries
        (now bearing the attr) should skip all of them. This is the §9.4
        idempotency contract — the script's whole reason for existence
        as a one-shot backfill."""
        entries_first = [
            _entry("e1", values=_vals(last_contact_date="2026-04-10")),
            _entry("e2", values=_vals(last_contact_date="2026-04-12")),
        ]
        run1 = _mock_run(dry_run=False)
        attio1 = _mock_attio_with_entries(entries_first)
        _backfill_prospect_committed_at(attio1, run1, list_id="lst-x")
        assert run1.rows_modified == 2

        # Second run: each entry now carries prospect_committed_at (as
        # written by run1). Build the same shape with the attr present.
        entries_second = [
            _entry("e1", values=_vals(
                last_contact_date="2026-04-10",
                prospect_committed_at="2026-04-10T12:00:00",
            )),
            _entry("e2", values=_vals(
                last_contact_date="2026-04-12",
                prospect_committed_at="2026-04-12T12:00:00",
            )),
        ]
        run2 = _mock_run(dry_run=False)
        attio2 = _mock_attio_with_entries(entries_second)
        _backfill_prospect_committed_at(attio2, run2, list_id="lst-x")

        assert run2.rows_modified == 0
        assert run2.rows_skipped_idempotent == 2
        assert attio2.captured == []
