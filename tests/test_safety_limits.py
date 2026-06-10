"""Tests for workflows/safety_limits.py — daily action caps."""

import json
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

from workflows.safety_limits import (
    MAX_CONNECTIONS_PER_DAY,
    MAX_MESSAGES_PER_DAY,
    MAX_VISITS_PER_DAY,
    can_send_connections,
    can_send_messages,
    get_remaining,
    get_status,
    record_connections,
    record_messages,
)


def _patch_limits_file(tmp_path):
    """Patch LIMITS_DIR and LIMITS_FILE to use a temp directory."""
    return patch.multiple(
        "workflows.safety_limits",
        LIMITS_DIR=tmp_path,
        LIMITS_FILE=tmp_path / "daily_limits.json",
    )


class TestLimits:
    def test_max_connections(self):
        assert MAX_CONNECTIONS_PER_DAY == 25

    def test_max_messages(self):
        assert MAX_MESSAGES_PER_DAY == 30

    def test_max_visits(self):
        assert MAX_VISITS_PER_DAY == 50


class TestCanSend:
    def test_can_send_connections_when_empty(self):
        with tempfile.TemporaryDirectory() as tmp, _patch_limits_file(Path(tmp)):
            assert can_send_connections(1)
            assert can_send_connections(25)

    def test_cannot_exceed_connection_limit(self):
        with tempfile.TemporaryDirectory() as tmp, _patch_limits_file(Path(tmp)):
            assert not can_send_connections(26)

    def test_can_send_messages_when_empty(self):
        with tempfile.TemporaryDirectory() as tmp, _patch_limits_file(Path(tmp)):
            assert can_send_messages(1)
            assert can_send_messages(30)

    def test_cannot_exceed_message_limit(self):
        with tempfile.TemporaryDirectory() as tmp, _patch_limits_file(Path(tmp)):
            assert not can_send_messages(31)


class TestRecording:
    def test_record_connections_decreases_remaining(self):
        with tempfile.TemporaryDirectory() as tmp, _patch_limits_file(Path(tmp)):
            remaining_before = get_remaining()["connections"]
            record_connections(5)
            remaining_after = get_remaining()["connections"]
            assert remaining_after == remaining_before - 5

    def test_record_messages_cumulative(self):
        with tempfile.TemporaryDirectory() as tmp, _patch_limits_file(Path(tmp)):
            record_messages(10)
            record_messages(5)
            remaining = get_remaining()["messages"]
            assert remaining == MAX_MESSAGES_PER_DAY - 15

    def test_blocks_after_limit_reached(self):
        with tempfile.TemporaryDirectory() as tmp, _patch_limits_file(Path(tmp)):
            record_connections(25)
            assert not can_send_connections(1)


class TestDailyReset:
    def test_resets_on_new_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with _patch_limits_file(tmp_path):
                record_connections(20)
                # Simulate yesterday's data
                data = json.loads((tmp_path / "daily_limits.json").read_text())
                data["date"] = "2025-01-01"
                (tmp_path / "daily_limits.json").write_text(json.dumps(data))
                # Should reset
                assert can_send_connections(25)


class TestGetStatus:
    def test_status_contains_date(self):
        with tempfile.TemporaryDirectory() as tmp, _patch_limits_file(Path(tmp)):
            status = get_status()
            assert date.today().isoformat() in status

    def test_status_contains_counts(self):
        with tempfile.TemporaryDirectory() as tmp, _patch_limits_file(Path(tmp)):
            record_connections(3)
            status = get_status()
            assert "3/25" in status
