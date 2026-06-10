"""Tests for scripts/purge_old_exports.py (L10-5 audit addition).

Covers:
- Age-based selection: files older than --days N are candidates.
- .gitkeep exclusion.
- weekly_borderline_*.jsonl exclusion within 30 days.
- --dry-run (default): lists, does not delete.
- --delete: actually removes files.
- Empty directory: no-op.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Import the importable selection function (kept pure / dependency-free).
# sys.path manipulation must precede the import — isort: skip_file is not
# appropriate here because the rest of the imports are standard. Use the
# per-line noqa instead.
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from purge_old_exports import main, select_files_to_purge  # noqa: E402, I001


# ── Helpers ──────────────────────────────────────────────────────────


def _mtime_days_ago(path: Path, days: float) -> None:
    """Stamp a file's mtime so it appears `days` old."""
    import os
    ts = (datetime.now(tz=UTC) - timedelta(days=days)).timestamp()
    os.utime(path, (ts, ts))


def _touch(path: Path, days_old: float = 0) -> Path:
    path.write_text("x")
    if days_old:
        _mtime_days_ago(path, days_old)
    return path


# ── select_files_to_purge ─────────────────────────────────────────────


class TestSelectionLogic:
    def test_old_file_is_candidate(self, tmp_path):
        f = _touch(tmp_path / "old_report.csv", days_old=65)
        result = select_files_to_purge(tmp_path, max_age_days=60)
        assert f in result

    def test_recent_file_excluded(self, tmp_path):
        f = _touch(tmp_path / "recent_report.csv", days_old=5)
        result = select_files_to_purge(tmp_path, max_age_days=60)
        assert f not in result

    def test_gitkeep_always_excluded(self, tmp_path):
        gk = _touch(tmp_path / ".gitkeep", days_old=200)
        result = select_files_to_purge(tmp_path, max_age_days=60)
        assert gk not in result

    def test_borderline_file_recent_excluded(self, tmp_path):
        """weekly_borderline_*.jsonl newer than 30 days is kept even if
        it's older than the max_age_days cutoff."""
        f = _touch(tmp_path / "weekly_borderline_2026-01-01.jsonl", days_old=25)
        result = select_files_to_purge(tmp_path, max_age_days=20, borderline_keep_days=30)
        assert f not in result

    def test_borderline_file_old_is_candidate(self, tmp_path):
        """weekly_borderline_*.jsonl older than both cutoffs IS a candidate."""
        f = _touch(tmp_path / "weekly_borderline_2026-01-01.jsonl", days_old=35)
        result = select_files_to_purge(tmp_path, max_age_days=30, borderline_keep_days=30)
        assert f in result

    def test_empty_directory_returns_empty(self, tmp_path):
        result = select_files_to_purge(tmp_path, max_age_days=60)
        assert result == []

    def test_nonexistent_directory_returns_empty(self, tmp_path):
        result = select_files_to_purge(tmp_path / "nonexistent", max_age_days=60)
        assert result == []

    def test_subdirectories_not_included(self, tmp_path):
        """Subdirectories are not listed as purge candidates."""
        sub = tmp_path / "subdir"
        sub.mkdir()
        result = select_files_to_purge(tmp_path, max_age_days=60)
        assert sub not in result

    def test_multiple_old_files_all_returned(self, tmp_path):
        files = [_touch(tmp_path / f"f{i}.csv", days_old=90) for i in range(5)]
        result = select_files_to_purge(tmp_path, max_age_days=60)
        assert set(files) == set(result)


# ── CLI: dry-run (default) ────────────────────────────────────────────


class TestCLIDryRun:
    def test_dry_run_lists_candidates_does_not_delete(self, tmp_path, capsys):
        old = _touch(tmp_path / "old.csv", days_old=90)
        rc = main(["--days", "60", "--exports-dir", str(tmp_path)])
        assert rc == 0
        assert old.exists(), "Dry-run must not delete files"
        out = capsys.readouterr().out
        assert "old.csv" in out

    def test_dry_run_empty_reports_nothing_to_purge(self, tmp_path, capsys):
        rc = main(["--exports-dir", str(tmp_path)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Nothing to purge" in out


# ── CLI: --delete ─────────────────────────────────────────────────────


class TestCLIDelete:
    def test_delete_removes_old_files(self, tmp_path):
        old = _touch(tmp_path / "old.csv", days_old=90)
        rc = main(["--days", "60", "--delete", "--exports-dir", str(tmp_path)])
        assert rc == 0
        assert not old.exists()

    def test_delete_keeps_recent_files(self, tmp_path):
        recent = _touch(tmp_path / "recent.csv", days_old=5)
        rc = main(["--days", "60", "--delete", "--exports-dir", str(tmp_path)])
        assert rc == 0
        assert recent.exists()

    def test_delete_keeps_gitkeep(self, tmp_path):
        gk = _touch(tmp_path / ".gitkeep", days_old=200)
        rc = main(["--days", "60", "--delete", "--exports-dir", str(tmp_path)])
        assert rc == 0
        assert gk.exists()
