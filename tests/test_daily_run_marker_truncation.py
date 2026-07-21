"""Adversarial-QA regression: the [superseded] marker must survive the
4096-char failure_details clamp, or _archive_stray_rows re-PATCHes the
same stray forever (the idempotency guard never trips)."""

from unittest.mock import MagicMock

from clients.crm.base import Record
from workflows.daily_run import _SUPERSEDED_MARKER, _archive_stray_rows


def _stray_row(record_id: str, details: str) -> Record:
    """A normalized terminal (failed) daily_run ``Record`` — scalar attributes,
    as the provider flattens them before the engine sees them."""
    attributes = {
        "status": "failed",
        "failure_details": details,
        "run_date": "2026-07-10",
        "machine_id": "mat",
        "uniqueness_key": "2026-07-10|mat",
    }
    return Record(
        record_id=record_id,
        object="daily_run",
        attributes=attributes,
        raw={"id": {"record_id": record_id}, "values": attributes},
    )


def test_marker_survives_clamped_traceback():
    crm = MagicMock()
    long_details = "x" * 4096  # already at the _close clamp
    row = _stray_row("r-long", long_details)

    marked = _archive_stray_rows(crm, [row], active_record_id="r-active")
    assert marked == 1
    patched = crm.update_object_record.call_args.args[2]["failure_details"]
    assert _SUPERSEDED_MARKER in patched
    assert len(patched) <= 4096

    # Feed the persisted value back, as the CRM would on the next run: the
    # idempotency guard must now skip (no second PATCH).
    crm.update_object_record.reset_mock()
    row2 = _stray_row("r-long", patched)
    marked2 = _archive_stray_rows(crm, [row2], active_record_id="r-active")
    assert marked2 == 0
    crm.update_object_record.assert_not_called()
