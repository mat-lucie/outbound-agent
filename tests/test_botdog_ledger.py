"""Local Botdog submission ledger — layer 1 of the duplicate-send guard.

The ledger records "we submitted `step` for `url` on `date`" so the next
run does NOT re-compose and re-submit the same DM while the Botdog
message-sent event is still in flight. Layer 2 is the CRM
`next_eligible_send_date` floor the caller writes at submission time,
which survives a lost/never-existed ledger (fresh machine, second
operator seat).

The autouse fixture below redirects LEDGER_DIR / LEDGER_FILE to a
per-test tmp_path, so nothing here touches the operator's real
``~/.outbound-agent`` ledger.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from workflows import botdog_ledger
from workflows.botdog_ledger import (
    PRUNE_AFTER_DAYS,
    recent_submission,
    record_submission,
)

_URL = "linkedin.com/in/acme-alice"
_TODAY = date(2026, 8, 21)


@pytest.fixture(autouse=True)
def _isolate_botdog_ledger(monkeypatch, tmp_path):
    """Redirect the submission ledger to a per-test temp path.

    Both constants are patched: ``_save`` mkdirs LEDGER_DIR and writes its
    tmp file there, so patching only LEDGER_FILE would still leave
    temp/lock debris in the operator's real ``~/.outbound-agent``.
    """
    ledger_dir = tmp_path / "ledger"
    monkeypatch.setattr(botdog_ledger, "LEDGER_DIR", ledger_dir)
    monkeypatch.setattr(
        botdog_ledger, "LEDGER_FILE", ledger_dir / "botdog_submissions.json"
    )


class TestLedgerIsolation:
    def test_fixture_points_the_ledger_away_from_the_real_home(self) -> None:
        """Guard the guard: if this ever resolves back under the real home
        directory, every test below would be writing the operator's live
        ledger."""
        record_submission(_URL, "dm1", _TODAY)
        assert botdog_ledger.LEDGER_FILE.exists()
        assert not str(botdog_ledger.LEDGER_FILE).startswith(
            str(Path.home() / ".outbound-agent")
        )


class TestRecordAndLookup:
    def test_unrecorded_url_has_no_recent_submission(self):
        assert recent_submission(_URL, "dm1", today=_TODAY) is None

    def test_recorded_submission_is_found(self):
        record_submission(_URL, "dm1", _TODAY)
        assert recent_submission(_URL, "dm1", today=_TODAY) == _TODAY

    def test_lookup_is_scoped_per_step(self):
        """A submitted DM1 must not suppress the (legitimately different)
        DM2 that follows it in the cadence."""
        record_submission(_URL, "dm1", _TODAY)
        assert recent_submission(_URL, "dm1", today=_TODAY) == _TODAY
        assert recent_submission(_URL, "dm2", today=_TODAY) is None

    def test_lookup_is_scoped_per_url(self):
        record_submission(_URL, "dm1", _TODAY)
        assert (
            recent_submission("linkedin.com/in/acme-bob", "dm1", today=_TODAY)
            is None
        )

    def test_submission_outside_window_does_not_block(self):
        old = _TODAY - timedelta(days=20)
        record_submission(_URL, "dm1", old)
        assert (
            recent_submission(_URL, "dm1", within_days=14, today=_TODAY)
            is None
        )
        # ...but a wider window still sees it (entry is not yet pruned).
        assert (
            recent_submission(_URL, "dm1", within_days=25, today=_TODAY) == old
        )

    def test_submission_on_the_window_boundary_still_blocks(self):
        boundary = _TODAY - timedelta(days=14)
        record_submission(_URL, "dm1", boundary)
        assert (
            recent_submission(_URL, "dm1", within_days=14, today=_TODAY)
            == boundary
        )

    def test_re_submission_resets_the_window(self):
        record_submission(_URL, "dm1", _TODAY - timedelta(days=10))
        record_submission(_URL, "dm1", _TODAY)
        assert recent_submission(_URL, "dm1", today=_TODAY) == _TODAY

    def test_empty_url_is_a_no_op_both_ways(self):
        record_submission("", "dm1", _TODAY)
        assert recent_submission("", "dm1", today=_TODAY) is None
        assert not botdog_ledger.LEDGER_FILE.exists()


class TestPruning:
    def test_entries_older_than_prune_window_are_dropped_on_write(self):
        ancient = _TODAY - timedelta(days=PRUNE_AFTER_DAYS + 1)
        record_submission("linkedin.com/in/acme-ancient", "dm1", ancient)
        # A later write triggers the prune.
        record_submission(_URL, "dm1", _TODAY)

        stored = json.loads(
            botdog_ledger.LEDGER_FILE.read_text()
        )["submissions"]
        assert list(stored) == [f"{_URL}|dm1"]

    def test_entries_inside_prune_window_survive(self):
        recent = _TODAY - timedelta(days=PRUNE_AFTER_DAYS - 1)
        record_submission("linkedin.com/in/acme-recent", "dm1", recent)
        record_submission(_URL, "dm1", _TODAY)

        stored = json.loads(
            botdog_ledger.LEDGER_FILE.read_text()
        )["submissions"]
        assert set(stored) == {
            "linkedin.com/in/acme-recent|dm1",
            f"{_URL}|dm1",
        }


class TestCorruptionTolerance:
    def test_unreadable_file_degrades_to_empty_and_warns(self, capsys):
        botdog_ledger.LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        botdog_ledger.LEDGER_FILE.write_text("{not json")

        assert recent_submission(_URL, "dm1", today=_TODAY) is None
        assert "unreadable" in capsys.readouterr().err

    def test_unparseable_stored_date_is_ignored(self):
        botdog_ledger.LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        botdog_ledger.LEDGER_FILE.write_text(
            json.dumps({"submissions": {f"{_URL}|dm1": "not-a-date"}})
        )
        assert recent_submission(_URL, "dm1", today=_TODAY) is None

    def test_write_is_atomic_and_leaves_no_temp_files(self):
        record_submission(_URL, "dm1", _TODAY)
        leftovers = list(
            botdog_ledger.LEDGER_DIR.glob(".botdog_submissions-*.tmp")
        )
        assert leftovers == []
        assert json.loads(
            botdog_ledger.LEDGER_FILE.read_text()
        )["submissions"]
