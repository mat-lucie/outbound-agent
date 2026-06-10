"""Tests for workflows/run_lock.py (F-PR-7).

Covers acquire / held / cleanup-on-exception / PID-reuse / EX_TEMPFAIL
exit semantics. fcntl.flock is per-file-description on Linux + macOS,
so we exercise "lock held" by opening the lock file with a second file
descriptor in the same test process — no subprocess gymnastics needed.

After the QA fold-in, the reclaim path was removed: `fcntl.flock`
returning `BlockingIOError` is the kernel's authoritative "someone
else has it" signal, and we trust it without trying to second-guess
from the pidfile. The pidfile lives in a sibling `{name}.pid` file
(written atomically via tempfile + `os.rename`) so concurrent readers
never see a torn write.
"""

from __future__ import annotations

import fcntl
import json
import os
from unittest.mock import patch

import httpx  # noqa: F401  # transitively imported by some path; keep nearby ergonomics
import psutil
import pytest

from workflows.run_lock import (
    EXIT_TEMPFAIL,
    RunLockHeld,
    _holder_is_stale,
    _read_holder,
    acquire_run_lock,
    run_with_lock,
)


@pytest.fixture
def lock_dir(tmp_path):
    """Isolated lock directory for each test. Avoids ~/.outbound-agent pollution."""
    d = tmp_path / "locks"
    d.mkdir()
    return d


def _hold_lock_externally(lock_path):
    """Acquire a flock on `lock_path` from an out-of-band fd.

    Returns the fd so the test can close it (release flock) at teardown.
    Used to simulate "another process holds the lock" without spawning
    subprocesses.
    """
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def _write_pidfile(pid_file, run_id: str = "other-run", pid: int | None = None) -> None:
    """Helper: write a syntactically-valid pidfile to disk."""
    proc = psutil.Process()
    pid_file.write_text(
        json.dumps(
            {
                "name": "sales-daily",
                "pid": pid if pid is not None else proc.pid,
                "create_time": proc.create_time(),
                "hostname": "test",
                "run_id": run_id,
                "acquired_at": 0.0,
            }
        )
    )


# ── acquire/release happy path ───────────────────────────────────────────────


def test_acquire_lock_when_no_existing_lock_succeeds(lock_dir):
    with acquire_run_lock("sales-daily", run_id="run-abc", lock_dir=lock_dir) as holder:
        assert holder["name"] == "sales-daily"
        assert holder["pid"] == os.getpid()
        assert holder["run_id"] == "run-abc"
        # pidfile present at `{name}.pid` (NOT `.lock`) and matches the yielded holder.
        on_disk = _read_holder(lock_dir / "sales-daily.pid")
        assert on_disk == holder
        # Lock file (the flock target) is intentionally empty.
        assert (lock_dir / "sales-daily.lock").read_bytes() == b""


def test_pidfile_removed_after_release(lock_dir):
    with acquire_run_lock("sales-daily", lock_dir=lock_dir):
        assert (lock_dir / "sales-daily.pid").exists()
    assert not (lock_dir / "sales-daily.pid").exists()


def test_lock_released_when_body_raises(lock_dir):
    """Standard context-manager invariant: if the body raises, the lock
    must still release. Without this guarantee, a single bug in the
    daily run wedges every subsequent invocation until manual cleanup."""
    with pytest.raises(RuntimeError, match="boom"), acquire_run_lock("sales-daily", lock_dir=lock_dir):
        raise RuntimeError("boom")
    # Re-acquire must succeed.
    with acquire_run_lock("sales-daily", lock_dir=lock_dir) as holder:
        assert holder["pid"] == os.getpid()


def test_acquire_lock_supports_multiple_namespaces(lock_dir):
    """Different namespaces are independent — daily + weekly can hold simultaneously."""
    with (
        acquire_run_lock("sales-daily", lock_dir=lock_dir),
        acquire_run_lock("sales-weekly", lock_dir=lock_dir),
    ):
        assert (lock_dir / "sales-daily.pid").exists()
        assert (lock_dir / "sales-weekly.pid").exists()


def test_lockfile_has_restrictive_mode(lock_dir):
    """Lockfile + pidfile contain operator data (PID + hostname + run_id);
    on a shared host, world-readable would leak. Mode is 0o600."""
    with acquire_run_lock("sales-daily", lock_dir=lock_dir):
        pid_mode = (lock_dir / "sales-daily.pid").stat().st_mode & 0o777
        assert pid_mode == 0o600


# ── held-by-live-process ─────────────────────────────────────────────────────


def test_acquire_lock_raises_when_held_by_live_process(lock_dir):
    """Another fd holds flock + a pidfile records a live process. Candidate
    raises RunLockHeld; the kernel's flock state is authoritative."""
    lock_path = lock_dir / "sales-daily.lock"
    pid_path = lock_dir / "sales-daily.pid"
    fd = _hold_lock_externally(lock_path)
    try:
        _write_pidfile(pid_path, run_id="other-run")
        with (
            pytest.raises(RunLockHeld) as excinfo,
            acquire_run_lock("sales-daily", lock_dir=lock_dir),
        ):
            pass
        assert excinfo.value.holder is not None
        assert excinfo.value.holder["run_id"] == "other-run"
        assert excinfo.value.name == "sales-daily"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_acquire_lock_raises_when_held_even_if_pidfile_missing(lock_dir):
    """flock returns BlockingIOError = live holder, period. Even if the
    pidfile happens to be missing (e.g., holder still mid-write), the
    kernel says someone has the lock — never silently steal it. This
    is the exact race the original reclaim path got wrong."""
    lock_path = lock_dir / "sales-daily.lock"
    fd = _hold_lock_externally(lock_path)
    try:
        # Note: NO pidfile written. The candidate would have seen an empty
        # pidfile under the old "reclaim on stale" logic and stolen the lock.
        with (
            pytest.raises(RunLockHeld) as excinfo,
            acquire_run_lock("sales-daily", lock_dir=lock_dir),
        ):
            pass
        assert excinfo.value.holder is None
        assert excinfo.value.name == "sales-daily"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_stale_pidfile_with_no_live_flock_acquires_cleanly(lock_dir):
    """A previous run crashed without cleanup: pidfile is on disk (dead PID),
    flock was released by the kernel on process death. New candidate should
    flock cleanly and overwrite the stale pidfile."""
    pid_path = lock_dir / "sales-daily.pid"
    _write_pidfile(pid_path, run_id="crashed", pid=99_999_999)
    with acquire_run_lock("sales-daily", lock_dir=lock_dir) as holder:
        assert holder["pid"] == os.getpid()
        # Pidfile is overwritten with our holder, not the ghost's.
        assert _read_holder(pid_path) == holder


# ── stale-detection (forensic helper, not authoritative) ─────────────────────


def test_holder_is_stale_when_pid_missing():
    assert _holder_is_stale(None) is True
    assert _holder_is_stale({}) is True
    assert _holder_is_stale({"pid": "not-an-int"}) is True


def test_holder_is_stale_when_pid_does_not_exist():
    assert _holder_is_stale({"pid": 99_999_999, "create_time": 0.0}) is True


def test_holder_is_fresh_when_pid_alive_and_create_time_matches():
    """The current test process's pidfile is NOT stale."""
    proc = psutil.Process()
    assert _holder_is_stale({"pid": proc.pid, "create_time": proc.create_time()}) is False


def test_holder_is_stale_on_pid_reuse():
    """Same PID but a different create_time → another process took the slot."""
    proc = psutil.Process()
    assert _holder_is_stale(
        {"pid": proc.pid, "create_time": proc.create_time() - 1_000_000}
    ) is True


def test_holder_is_fresh_within_create_time_tolerance():
    """Sub-tolerance drift (psutil float precision across reads) is NOT stale."""
    proc = psutil.Process()
    # 0.1s drift well below the 1.0s tolerance.
    assert _holder_is_stale(
        {"pid": proc.pid, "create_time": proc.create_time() + 0.1}
    ) is False


def test_holder_is_stale_when_psutil_access_denied():
    """A process we can't introspect (e.g., other-user owned) is treated
    as stale so an operator-owned daily run isn't permanently blocked by
    an unrelated root process happening to reuse a PID."""
    holder = {"pid": 1, "create_time": 0.0}  # PID 1 always exists
    with patch.object(psutil.Process, "create_time", side_effect=psutil.AccessDenied()):
        assert _holder_is_stale(holder) is True


def test_holder_is_stale_when_psutil_no_such_process():
    """TOCTOU race: pid_exists True, then NoSuchProcess on create_time access."""
    holder = {"pid": 1, "create_time": 0.0}
    with patch.object(psutil.Process, "create_time", side_effect=psutil.NoSuchProcess(1)):
        assert _holder_is_stale(holder) is True


# ── run_with_lock + EXIT_TEMPFAIL ────────────────────────────────────────────


def test_run_with_lock_returns_func_return_when_available(lock_dir):
    sentinel = []

    def work():
        sentinel.append("ran")
        return 0

    rc = run_with_lock("sales-daily", work, lock_dir=lock_dir)
    assert rc == 0
    assert sentinel == ["ran"]


def test_run_with_lock_exits_75_when_lock_held(lock_dir, capsys):
    """When another live process holds the lock, run_with_lock returns 75
    instead of crashing — gives a single point of exit-code control."""
    lock_path = lock_dir / "sales-daily.lock"
    pid_path = lock_dir / "sales-daily.pid"
    fd = _hold_lock_externally(lock_path)
    try:
        _write_pidfile(pid_path, run_id="other")
        called = []
        rc = run_with_lock(
            "sales-daily", lambda: called.append("x") or 0, lock_dir=lock_dir
        )
        assert rc == EXIT_TEMPFAIL
        assert called == []  # func must NOT run when lock is refused
        captured = capsys.readouterr()
        assert "EX_TEMPFAIL" in captured.err
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ── cli.py integration: exit 75 on RunLockHeld ───────────────────────────────


def test_cli_daily_exits_75_when_lock_held(lock_dir, monkeypatch):
    """End-to-end: `cli daily` with a held flock exits 75 without doing
    any work. Catches regressions where the click wrap accidentally swallows
    RunLockHeld into a non-75 exit."""
    from click.testing import CliRunner

    monkeypatch.setattr("workflows.run_lock.DEFAULT_LOCK_DIR", lock_dir)
    fd = _hold_lock_externally(lock_dir / "sales-daily.lock")
    try:
        _write_pidfile(lock_dir / "sales-daily.pid", run_id="held-by-test")
        # Late import so the monkeypatch takes effect.
        from cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["daily"])
        assert result.exit_code == EXIT_TEMPFAIL
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_cli_weekly_exits_75_when_lock_held(lock_dir, monkeypatch):
    from click.testing import CliRunner

    monkeypatch.setattr("workflows.run_lock.DEFAULT_LOCK_DIR", lock_dir)
    fd = _hold_lock_externally(lock_dir / "sales-weekly.lock")
    try:
        _write_pidfile(lock_dir / "sales-weekly.pid", run_id="held-by-test")
        from cli import cli
        runner = CliRunner()
        # search_export_id env unset would short-circuit before the lock —
        # provide a dummy so the lock check is what raises.
        result = runner.invoke(cli, ["weekly", "--search-export-id", "dummy"])
        assert result.exit_code == EXIT_TEMPFAIL
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_cli_email_daily_exits_75_when_lock_held(lock_dir, monkeypatch):
    from click.testing import CliRunner

    monkeypatch.setattr("workflows.run_lock.DEFAULT_LOCK_DIR", lock_dir)
    fd = _hold_lock_externally(lock_dir / "sales-email-daily.lock")
    try:
        _write_pidfile(lock_dir / "sales-email-daily.pid", run_id="held-by-test")
        from cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["email-daily", "--dry-run"])
        assert result.exit_code == EXIT_TEMPFAIL
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_cli_email_wave2_shares_email_daily_namespace(lock_dir, monkeypatch):
    """email-wave2 + email-daily both write contact email state; they
    must mutually exclude via the shared `sales-email-daily` namespace."""
    from click.testing import CliRunner

    monkeypatch.setattr("workflows.run_lock.DEFAULT_LOCK_DIR", lock_dir)
    # Hold the email-daily lock externally; email-wave2 must see it.
    fd = _hold_lock_externally(lock_dir / "sales-email-daily.lock")
    try:
        _write_pidfile(lock_dir / "sales-email-daily.pid", run_id="held-by-test")
        from cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["email-wave2", "--dry-run"])
        assert result.exit_code == EXIT_TEMPFAIL
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
