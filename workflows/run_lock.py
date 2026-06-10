"""Per-machine concurrent-run protection for the engine's slash commands.

Filesystem-local advisory lock via `fcntl.flock` plus a sibling pidfile
(written atomically via tempfile + `os.rename`) holding `(pid,
create_time, hostname, run_id, acquired_at)` for forensics. The flock
is the single source of truth on "is anyone holding it"; the kernel
releases it on process death, so we never need to second-guess
liveness from the pidfile. The pidfile only provides a "held by PID X
on host Y" message for the operator.

Per-machine only. Two laptops running `/sales-daily` under the same
operator account at the same wall-clock minute each see an empty lock
file on their own filesystem; flock succeeds on both. Cross-machine
uniqueness is F-PR-8's job (`daily_run` Attio row keyed on
`(run_date, machine_id)`).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import socket
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import psutil

if TYPE_CHECKING:
    from collections.abc import Iterator

DEFAULT_LOCK_DIR = Path.home() / ".outbound-agent" / "locks"
EXIT_TEMPFAIL = 75  # POSIX EX_TEMPFAIL — "try again later"
# psutil.Process.create_time() returns a float; cross-read precision can drift
# by sub-millisecond on the same process. 1.0s tolerance accommodates that
# without letting a real PID-reuse (different process, different create_time)
# slip through — OS PID reuse on the same exact second is vanishingly rare.
_CREATE_TIME_TOLERANCE_S = 1.0
_LOCK_FILE_MODE = 0o600  # PID + hostname + run_id are operator data; don't world-read.


class RunLockHeld(Exception):
    """Raised when another live process holds the named run lock.

    `flock` returning `BlockingIOError` is the kernel's authoritative
    statement that someone else has the lock. The `holder` attribute
    carries best-effort forensics from the pidfile; it may be `None`
    if the pidfile is missing or mid-write, which does NOT mean the
    lock is reclaimable — the kernel says it isn't.
    """

    def __init__(self, name: str, holder: dict | None) -> None:
        self.name = name
        self.holder = holder
        if holder:
            who = (
                f"PID {holder.get('pid')!r} on {holder.get('hostname')!r} "
                f"since {holder.get('acquired_at')!r} (run_id={holder.get('run_id')!r})"
            )
        else:
            who = "another live process (pidfile not yet readable)"
        super().__init__(f"Run lock {name!r} is held by {who}.")


def _lock_dir(lock_dir: Path | None = None) -> Path:
    base = lock_dir or DEFAULT_LOCK_DIR
    base.mkdir(parents=True, exist_ok=True)
    return base


def _lock_path(name: str, lock_dir: Path | None = None) -> Path:
    """The flock target. Contents are intentionally empty — flock state lives
    in the kernel, the file just provides a stable inode to flock on."""
    return _lock_dir(lock_dir) / f"{name}.lock"


def _pid_path(name: str, lock_dir: Path | None = None) -> Path:
    """The pidfile, separate from the flock target. Written atomically via
    tempfile + os.rename so concurrent readers never see a torn write."""
    return _lock_dir(lock_dir) / f"{name}.pid"


def _read_holder(pid_file: Path) -> dict | None:
    """Read the pidfile JSON; return None if absent or malformed."""
    try:
        text = pid_file.read_text()
    except OSError:
        return None
    if not text.strip():
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _holder_is_stale(holder: dict | None) -> bool:
    """True iff the pidfile's PID + create_time no longer identify a live
    process matching what was recorded.

    NOTE: this function is forensic, not authoritative. The kernel
    decides whether the flock is held via `fcntl.flock`; this helper
    only catches the rare case where someone wants to inspect a pidfile
    independent of an acquire attempt.
    """
    if not holder:
        return True
    pid = holder.get("pid")
    create_time = holder.get("create_time")
    if not isinstance(pid, int):
        return True
    if not psutil.pid_exists(pid):
        return True
    try:
        proc = psutil.Process(pid)
        if not isinstance(create_time, (int, float)):
            # pid_exists already said the PID is alive; without a
            # create_time on disk we can't disprove that it's the same
            # process, so call it fresh.
            return False
        return abs(proc.create_time() - float(create_time)) > _CREATE_TIME_TOLERANCE_S
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return True


def _build_holder(name: str, run_id: str | None) -> dict:
    proc = psutil.Process()
    return {
        "name": name,
        "pid": proc.pid,
        "create_time": proc.create_time(),
        "hostname": socket.gethostname(),
        "run_id": run_id,
        "acquired_at": time.time(),
    }


def _write_pid_file_atomic(pid_file: Path, holder: dict) -> None:
    """Write the pidfile via tempfile + os.rename — POSIX-atomic rename
    means concurrent readers see either the old content or the new,
    never a torn write."""
    tmp_path = pid_file.with_name(f"{pid_file.name}.tmp.{os.getpid()}")
    try:
        tmp_path.write_text(json.dumps(holder))
        os.chmod(str(tmp_path), _LOCK_FILE_MODE)
        os.replace(str(tmp_path), str(pid_file))
    except OSError:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        raise


@contextmanager
def acquire_run_lock(
    name: str = "sales-daily",
    run_id: str | None = None,
    lock_dir: Path | None = None,
) -> Iterator[dict]:
    """Acquire a per-machine advisory lock named ``{name}.lock``.

    Raises ``RunLockHeld`` when ``fcntl.flock`` returns ``BlockingIOError``
    (another live process holds the lock — the kernel is authoritative
    on this). The pidfile is consulted only to surface forensics in the
    exception message.

    The flock is released by the kernel on process death, so a hard-
    crashed holder cannot wedge the next run forever. The pidfile is
    unlinked on normal exit; if it's left behind by a crashed run, the
    next acquirer's atomic-rename write replaces it cleanly.
    """
    lock_file = _lock_path(name, lock_dir)
    pid_file = _pid_path(name, lock_dir)
    fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT, _LOCK_FILE_MODE)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = _read_holder(pid_file)
            raise RunLockHeld(name, holder) from None

        holder = _build_holder(name, run_id)
        _write_pid_file_atomic(pid_file, holder)
        try:
            yield holder
        finally:
            with contextlib.suppress(FileNotFoundError):
                pid_file.unlink()
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def run_with_lock(
    name: str,
    func,
    *args,
    run_id: str | None = None,
    lock_dir: Path | None = None,
    **kwargs,
) -> int:
    """Run ``func(*args, **kwargs)`` under ``acquire_run_lock(name)``.

    Convenience wrapper for callers without nested context managers.
    Returns ``EXIT_TEMPFAIL`` (75) on refusal with a stderr breadcrumb;
    otherwise returns ``func``'s return value.

    `cli.py` does NOT use this — its commands need to nest the lock
    inside an `AuditLogger` / `AttioClient` chain, which is expressed
    more naturally with `with acquire_run_lock(...): ...` directly.
    This helper exists for future single-callable entrypoints + the
    test suite.
    """
    try:
        with acquire_run_lock(name=name, run_id=run_id, lock_dir=lock_dir):
            return func(*args, **kwargs)
    except RunLockHeld as exc:
        log_lock_refused(name, exc)
        return EXIT_TEMPFAIL


def log_lock_refused(name: str, exc: RunLockHeld) -> None:
    """Standard stderr breadcrumb for a refused lock acquire.

    Shared by `run_with_lock` + the cli.py wrap sites so operators see
    a consistent "ERROR: …; another `name` run is in progress; exiting
    75 (EX_TEMPFAIL) so the next invocation can take over." regardless
    of which entrypoint they used.
    """
    print(
        f"ERROR: {exc} Another {name!r} run is in progress; "
        f"exiting {EXIT_TEMPFAIL} (EX_TEMPFAIL) so the next invocation "
        f"can take over once that run finishes.",
        file=sys.stderr,
    )
