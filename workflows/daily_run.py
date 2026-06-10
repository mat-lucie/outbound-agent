"""CRM-backed daily run record + cross-machine concurrent-run guard (F-PR-8).

Replaces ``~/.outbound-agent/daily_limits.json`` two-phase reserve/confirm
(per §4.2 B-SD-006) with a CRM ``daily_run`` object record keyed on
``(run_date, machine_id)``. The CRM uniqueness constraint provides the
cross-machine half of the concurrent-run guard whose per-machine half
ships in F-PR-7: a second invocation from a DIFFERENT machine on the
same ``run_date`` collides on ``uniqueness_key`` and raises
``ConcurrentRunInAttio``; a second invocation from the SAME machine is
caught by F-PR-7's local flock before the CRM ever sees it.

# CRM seam

This module talks to the system of record exclusively through the
vendor-neutral ``CRMProvider`` contract (``clients/crm/base.py``) — it carries
NO direct ``AttioClient`` path. Every read/write goes through the generic
object-record API (``query_object_records`` / ``create_object_record`` /
``update_object_record``), and the cross-machine collision is detected by
catching the neutral ``UniquenessConflictError`` the provider raises on a
uniqueness-constraint violation (the Attio adapter maps its 400/409
uniqueness body to that type; the in-memory ``FakeProvider`` simulates it).
Those writes route through the adapter's retrying request layer, so a
transient 429/5xx retries rather than wedging a counter or status write.

# Capacity counters

Three counters live on the daily_run row: ``connections_sent``,
``messages_sent``, ``visits_sent``. ``record_send(kind, n)`` increments
them atomically (via PATCH-with-current-value); ``remaining(kind)``
reports the gap to the daily cap. ``reserve_send(kind, n)`` returns a
lease token for two-phase semantics — see §4.2 B-SD-006:

    with open_daily_run(attio, run_id="...") as run:
        lease = run.reserve_send("messages", 5)
        # ... do the send ...
        if success:
            run.confirm_lease(lease)  # increments messages_sent by 5
        else:
            run.release_lease(lease)  # rolls the reservation back

The lease lives only in process memory; if the process crashes
mid-flight the lease is implicitly released and the counter stays where
it was at the last ``confirm_lease`` call. This is the deliberate
trade-off: false-negatives (under-counting a send that actually
happened and crashed before confirm) over false-positives (over-counting
a send that was reserved but never executed).

# Recheck-cache observations

``record_observation(linkedin_record_id, degree)`` is the Attio-resident
replacement for ``~/.outbound-agent/recheck_cache.json``; it writes the
``last_observed_degree`` + ``last_observed_at`` attrs on a LinkedIn
Outreach record. The local JSON file remains as a thin regenerable
cache (see ``workflows/recheck_cache.py``).
"""

from __future__ import annotations

import os
import socket
import sys
import time
import traceback
import uuid
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Literal

import click
import httpx

from clients.crm.exceptions import UniquenessConflictError
from clients.outreach_config import load_outreach_config

if TYPE_CHECKING:
    from collections.abc import Iterator

    from clients.crm.base import CRMProvider, Record


# Daily safety caps. Sourced from config/outreach.yaml via the shared loader —
# the single source of truth, identical to ``workflows/safety_limits.py``.
_OUTREACH = load_outreach_config()
MAX_CONNECTIONS_PER_DAY = _OUTREACH.invites_per_day
MAX_MESSAGES_PER_DAY = _OUTREACH.dms_per_day
MAX_VISITS_PER_DAY = _OUTREACH.visits_per_day

# A same-machine running row older than this many hours is considered stale
# (the prior process died without closing) and is taken over by the new run.
# Cross-machine stale rows are NEVER auto-taken-over — we can't verify that
# the other machine is truly dead. Configurable via outreach.yaml
# `staleness.stale_run_takeover_hours`; falls back to 3 if absent.
# (L4-4 audit fix.)
STALE_RUN_TAKEOVER_HOURS: int = _OUTREACH.stale_run_takeover_hours

SendKind = Literal["connections", "messages", "visits"]
_KIND_TO_ATTR: dict[SendKind, str] = {
    "connections": "connections_sent",
    "messages": "messages_sent",
    "visits": "visits_sent",
}
_KIND_TO_CAP: dict[SendKind, int] = {
    "connections": MAX_CONNECTIONS_PER_DAY,
    "messages": MAX_MESSAGES_PER_DAY,
    "visits": MAX_VISITS_PER_DAY,
}

DEFAULT_MACHINE_ID_ENV = "OUTBOUND_MACHINE_ID"


class ConcurrentRunInAttio(Exception):
    """Raised when ``open_daily_run`` collides on ``(run_date, machine_id)``.

    The ``is_unique`` constraint on the ``uniqueness_key`` attribute means a
    create from a second machine surfaces as the provider's neutral
    ``UniquenessConflictError``. ``open_daily_run`` catches that and re-raises
    THIS typed exception (named for history; the source is now vendor-neutral)
    so the caller (cli.py / slash command) can exit ``EXIT_TEMPFAIL`` with the
    existing-row forensics rather than crashing on a raw error.
    """

    def __init__(self, run_date: str, machine_id: str, existing: dict | None) -> None:
        self.run_date = run_date
        self.machine_id = machine_id
        self.existing = existing
        msg = (
            f"daily_run uniqueness collision: another machine is already running "
            f"on {run_date!r} for machine_id {machine_id!r}."
        )
        if existing:
            age_note = ""
            started_at = existing.get("started_at", "")
            if started_at:
                try:
                    started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                    age_hours = (datetime.now(UTC) - started).total_seconds() / 3600
                    age_note = f", age={age_hours:.1f}h"
                except (ValueError, TypeError):
                    pass
            msg += (
                f" Existing row: hostname={existing.get('hostname')!r}, "
                f"started_at={existing.get('started_at')!r}{age_note}, "
                f"run_id={existing.get('run_id')!r}."
            )
        super().__init__(msg)


_GENERIC_HOSTNAME_PATTERNS = (
    "macbook-pro.local",
    "macbook-air.local",
    "macbookpro.local",
    "imac.local",
    "localhost",
)


def derive_machine_id(override: str | None = None) -> str:
    """Return the canonical machine identifier for this process.

    Resolution: explicit override > ``$OUTBOUND_MACHINE_ID`` env >
    stable hash of
    ``socket.gethostname()``. Two laptops sharing the same operator
    account are distinct machines even if the hostname looks generic —
    but if the hostname matches a common default ("MacBook-Pro.local",
    "localhost", etc.) AND no env override is set, we emit a UserWarning
    so the operator sets ``$OUTBOUND_MACHINE_ID`` before two
    identical-named laptops collide on ``(run_date, machine_id)``.
    """
    if override and override.strip():
        return override.strip()
    env = os.environ.get(DEFAULT_MACHINE_ID_ENV, "").strip()
    if env:
        return env
    hostname = socket.gethostname()
    if hostname.lower() in _GENERIC_HOSTNAME_PATTERNS:
        warnings.warn(
            f"socket.gethostname() returned the generic value {hostname!r}; "
            f"two laptops with the same hostname would collide on the daily_run "
            f"uniqueness_key. Set $OUTBOUND_MACHINE_ID to a stable per-laptop "
            f"identifier to disambiguate.",
            UserWarning,
            stacklevel=2,
        )
    return hostname


def _uniqueness_key(run_date: str, machine_id: str) -> str:
    return f"{run_date}|{machine_id}"


def _released_uniqueness_key(
    status: str, run_date: str, machine_id: str, record_id: str
) -> str:
    """The uniqueness_key a row carries once it leaves ``status=running``.

    Appending the terminal status frees the 2-part running-lock
    (``{run_date}|{machine_id}``) so the next same-date/machine
    ``open_daily_run`` POST no longer collides on the Attio unique
    constraint. The row's own ``record_id`` (a globally-unique Attio UUID)
    is the final segment so two terminal rows can NEVER collide — using the
    run_id instead would risk a same-date collision when the OS recycles a
    PID between a failed run and its retry (run_id is ``…-{pid}``), which
    would 409 the close PATCH and re-wedge the lock this fix exists to free.
    """
    return f"{status}|{run_date}|{machine_id}|{record_id}"


@dataclass
class _Lease:
    kind: SendKind
    count: int


class DailyRun:
    """Handle to an in-flight daily_run Attio row.

    Created by ``open_daily_run``. Not constructed directly by callers.
    """

    def __init__(
        self,
        crm: CRMProvider,
        record_id: str,
        run_date: str,
        machine_id: str,
        run_id: str,
        initial_counters: dict[SendKind, int],
    ) -> None:
        self._crm = crm
        self.record_id = record_id
        self.run_date = run_date
        self.machine_id = machine_id
        self.run_id = run_id
        self._counters: dict[SendKind, int] = dict(initial_counters)
        # Reservations are process-local. A crash drops them on the floor
        # and the on-disk counters stay where they were — that's the
        # deliberate B-SD-006 trade-off (under-count over double-count).
        self._reservations: dict[str, _Lease] = {}
        # PR-19 B-SD-005: reply-detection status. Reads/writes go through
        # set_reply_detection_status / get_reply_detection_status so
        # Part-B (DM sequencing) can short-circuit on a failed Phase
        # 0.5 inbox scrape. None = not yet evaluated (no scrape ran).
        self._reply_detection_status: str | None = None

    # ── capacity reporting ──────────────────────────────────────────

    def remaining(self, kind: SendKind) -> int:
        """How many more of `kind` we can send today, accounting for
        confirmed-AND-reserved (treats reservations as already-spent
        so callers don't over-commit while a parallel send is in flight)."""
        reserved = sum(
            lease.count for lease in self._reservations.values() if lease.kind == kind
        )
        return max(0, _KIND_TO_CAP[kind] - self._counters[kind] - reserved)

    def can_send(self, kind: SendKind, count: int = 1) -> bool:
        return self.remaining(kind) >= count

    # ── two-phase lease ──────────────────────────────────────────────

    def reserve_send(self, kind: SendKind, count: int) -> str:
        """Reserve `count` of `kind` capacity. Returns a lease token.

        Confirm with ``confirm_lease(token)`` to commit the count to
        Attio; release with ``release_lease(token)`` to roll back.
        Raises ``CapacityExhausted`` if the reservation would exceed
        the daily cap.
        """
        if not self.can_send(kind, count):
            raise CapacityExhausted(kind, count, self.remaining(kind))
        token = uuid.uuid4().hex
        self._reservations[token] = _Lease(kind=kind, count=count)
        return token

    def confirm_lease(self, token: str, confirmed_count: int | None = None) -> None:
        """Commit a reservation to Attio. Idempotent on unknown token.

        ``confirmed_count`` supports the PR-17 partial-confirmation path
        used by the PB send loop: a lease for ``len(due_rows)`` may
        confirm fewer when PB reports ``sent_count < requested_count``.
        The drift (``lease.count - confirmed_count``) is refunded to
        daily capacity — preserves the §3.1 invariant that quota
        consumed = sends actually executed (not sends optimistically
        requested). ``confirmed_count=None`` (default) commits the full
        reservation, matching pre-PR-17 callers.

        If the Attio PATCH fails with a transport error, the lease is
        re-instated and the in-memory counter rolled back so process
        state stays consistent with Attio. Mirrors F-PR-6's
        narrow-catch + propagate pattern: schema-drift / programming
        errors propagate uncaught.
        """
        lease = self._reservations.pop(token, None)
        if lease is None:
            return
        commit_count = lease.count if confirmed_count is None else confirmed_count
        if commit_count < 0 or commit_count > lease.count:
            # Programming bug — surface loudly. Reinstate the lease so a
            # caller that catches this can retry with a valid count.
            self._reservations[token] = lease
            raise ValueError(
                f"confirmed_count={commit_count!r} must be in [0, {lease.count}] "
                f"for lease.kind={lease.kind!r}"
            )
        self._counters[lease.kind] += commit_count
        try:
            self._patch_counters()
        except (httpx.HTTPStatusError, httpx.RequestError):
            # Roll back in-memory state to keep it consistent with Attio;
            # the caller can retry (or surface the error to the operator).
            self._counters[lease.kind] -= commit_count
            self._reservations[token] = lease
            raise

    def release_lease(self, token: str) -> None:
        """Roll back a reservation. Idempotent on unknown token."""
        self._reservations.pop(token, None)

    # ── PR-19 B-SD-005: reply detection status ───────────────────────

    def set_reply_detection_status(self, status: str) -> None:
        """Write ``reply_detection_status`` to the daily_run row.

        Called from Phase 0.5 (``workflows.detect_responses``) on the
        no-CSV halt path BEFORE the process exits 2. Part-B (DM
        sequencing) reads via ``get_reply_detection_status`` and
        short-circuits when the status is ``"failed"`` — prevents DM3
        from firing while an unread reply sits in the inbox (a direct
        §3.1 no-resend protection).

        Valid values mirror the Attio select options:
        ``"ok"`` | ``"no_csv"`` | ``"partial"`` | ``"halted"`` |
        ``"failed"``.

        Routed through the provider's ``update_object_record`` (the adapter's
        retrying layer) so a transient 429 does not leave
        reply_detection_status unwritten while in-memory says ok. (L4-2 audit
        fix.)
        """
        self._reply_detection_status = status
        self._crm.update_object_record(
            "daily_run",
            self.record_id,
            {"reply_detection_status": status},
        )

    def get_reply_detection_status(self) -> str | None:
        """Return the in-process cached status; ``None`` if unset."""
        return self._reply_detection_status

    # ── direct (non-leased) recording ────────────────────────────────

    def record_send(self, kind: SendKind, count: int = 1) -> None:
        """Increment the counter directly. Use when the send semantics
        are confirmed (e.g., post-PB advance-gate) and the two-phase
        lease pattern would just add latency."""
        self._counters[kind] += count
        self._patch_counters()

    # ── recheck-cache observation ────────────────────────────────────

    def record_observation(
        self,
        linkedin_record_id: str,
        degree: str | None,
        observed_at: date | None = None,
    ) -> None:
        """Write ``last_observed_degree`` + ``last_observed_at`` on a
        LinkedIn Outreach record. Replaces ``recheck_cache.record``."""
        observed = (observed_at or date.today()).isoformat()
        normalized = _normalize_degree(degree)
        values: dict = {"last_observed_at": observed}
        if normalized:
            values["last_observed_degree"] = normalized
        # Route through the provider's retrying write layer (L4-2 audit fix).
        self._crm.update_object_record(
            "linkedin_outreach", linkedin_record_id, values
        )

    # ── internals ────────────────────────────────────────────────────

    def _patch_counters(self) -> None:
        values = {
            attr: self._counters[kind] for kind, attr in _KIND_TO_ATTR.items()
        }
        # Route through the provider's retrying write layer so a transient 429
        # doesn't leave counters un-committed (confirm_lease depends on this
        # landing to prevent cap-breach). (L4-2 audit fix.)
        self._crm.update_object_record("daily_run", self.record_id, values)

    def _close(self, status: Literal["completed", "failed", "aborted"], failure_details: str | None = None) -> None:
        values: dict = {
            "completed_at": datetime.now(UTC).isoformat(),
            "status": status,
            # Release the running-lock atomically with the close: the next
            # same-date/machine run re-uses the 2-part key on open, which
            # no longer collides because this row now carries the released
            # key keyed on this row's record_id. Same PATCH = no window
            # where the row is closed but still holding the lock.
            "uniqueness_key": _released_uniqueness_key(
                status, self.run_date, self.machine_id, self.record_id
            ),
        }
        if failure_details is not None:
            values["failure_details"] = failure_details[:4096]
        self._crm.update_object_record("daily_run", self.record_id, values)


class CapacityExhausted(Exception):
    """Raised by ``reserve_send`` when the requested count exceeds the
    daily cap (after accounting for prior reservations + confirmed sends)."""

    def __init__(self, kind: SendKind, requested: int, available: int) -> None:
        self.kind = kind
        self.requested = requested
        self.available = available
        super().__init__(
            f"Capacity exhausted for {kind!r}: requested {requested}, only {available} available today."
        )


class NoDailyRunRow(Exception):
    """Raised by ``attach_daily_run`` when no daily_run row exists for
    ``(run_date, machine_id)`` today. The DM step must never run without
    its ``daily --skip-dms`` predecessor having opened the day, so the
    caller exits EX_TEMPFAIL with "run `daily --skip-dms` first"."""

    def __init__(self, run_date: str, machine_id: str) -> None:
        self.run_date = run_date
        self.machine_id = machine_id
        super().__init__(
            f"No daily_run row for {run_date!r} on machine {machine_id!r}. "
            f"Run `daily --skip-dms` first to open the day."
        )


class ReopenCollision(Exception):
    """Raised when the reopen-write (terminal → running, re-keying to the
    2-part running-lock) collides because a concurrent step-1 retry already
    holds that key. Fail-closed: the caller exits EX_TEMPFAIL, sends nothing."""

    def __init__(self, run_date: str, machine_id: str, record_id: str) -> None:
        self.run_date = run_date
        self.machine_id = machine_id
        self.record_id = record_id
        super().__init__(
            f"Cannot reopen daily_run row {record_id!r} for {run_date!r} on "
            f"{machine_id!r}: the 2-part running-lock is held by a concurrent "
            f"run. Fail-closed — try again shortly."
        )


class MalformedDailyRunRow(Exception):
    """Raised by ``attach_daily_run`` when a fetched daily_run row is missing
    or has an unparseable capacity counter (``messages_sent`` /
    ``connections_sent`` / ``visits_sent``).

    ``open_daily_run`` ALWAYS writes all three counters (0 at minimum) and
    ``_patch_counters`` rewrites them on every send, so an absent / null /
    non-numeric counter on an EXISTING row signals a malformed or partial
    CRM response — never a legitimate state. Defaulting it to 0 would
    silently reset the cap to 0/30 and permit up to 60 DMs against the
    30/day cap, the exact breach this feature prevents. Fail closed: do not
    bind the run. The caller treats this as EX_TEMPFAIL (distinct, catchable
    type) and sends nothing."""

    def __init__(self, attr: str, record_id: str) -> None:
        self.attr = attr
        self.record_id = record_id
        super().__init__(
            f"daily_run row {record_id!r} has a missing or unparseable "
            f"counter {attr!r}. open_daily_run always writes this counter, so "
            f"its absence signals a malformed CRM response — failing closed "
            f"to avoid silently resetting the message cap to 0."
        )


_DEGREE_NORMALIZATION = {
    "1st": "first",
    "first": "first",
    "2nd": "second",
    "second": "second",
    "3rd": "third",
    "third": "third",
    "out_of_network": "unknown",
    "unknown": "unknown",
}


def _normalize_degree(raw: str | None) -> str | None:
    if not raw:
        return None
    return _DEGREE_NORMALIZATION.get(str(raw).strip().lower())


@contextmanager
def open_daily_run(
    crm: CRMProvider,
    run_id: str,
    run_date: date | None = None,
    machine_id: str | None = None,
) -> Iterator[DailyRun]:
    """Create (or reattach to) the daily_run row for today, yield a handle.

    Cross-machine guard: the create sets ``uniqueness_key = f'{run_date}|
    {machine_id}'`` with ``is_unique=True``. A second machine on the
    same date collides — the provider raises ``UniquenessConflictError``,
    which we surface as ``ConcurrentRunInAttio``. The local flock from
    F-PR-7 catches the same-machine case before this code runs, so the
    same machine never collides with itself here.

    On context exit (whether normal or exception), the row's
    ``completed_at`` + ``status`` are written so a forensics scan can
    distinguish completed / failed / aborted runs from a still-running
    one.
    """
    actual_date = run_date or date.today()
    actual_machine = derive_machine_id(machine_id)
    date_str = actual_date.isoformat()
    key = _uniqueness_key(date_str, actual_machine)

    values = {
        "run_id": run_id,
        "run_date": date_str,
        "machine_id": actual_machine,
        "uniqueness_key": key,
        "hostname": socket.gethostname(),
        "started_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "process_id": os.getpid(),
        "connections_sent": 0,
        "messages_sent": 0,
        "visits_sent": 0,
    }

    try:
        record = crm.create_object_record("daily_run", values)
    except UniquenessConflictError as exc:
        existing = _query_existing_row(crm, key)
        # L4-4: same-machine stale row takeover.
        # A stuck status=running row from the same machine (same machine_id)
        # that is older than STALE_RUN_TAKEOVER_HOURS means the prior
        # process crashed without closing. We mark it abandoned so the new
        # run can open fresh. Cross-machine rows are never taken over
        # automatically — we can't verify the other machine is dead.
        if existing and _is_stale_same_machine(existing, actual_machine):
            stale_record_id = existing.get("record_id", "")
            if stale_record_id:
                _takeover_stale_row(crm, stale_record_id, date_str, actual_machine)
                # Retry the create now that the lock is freed. A racing process
                # can grab the freed key between takeover and this retry, so the
                # retry can itself raise raw UniquenessConflictError — surface it
                # as ConcurrentRunInAttio (the documented contract) instead of
                # leaking the vendor exception past cli's handler.
                try:
                    record = crm.create_object_record("daily_run", values)
                except UniquenessConflictError as retry_exc:
                    raise ConcurrentRunInAttio(
                        date_str, actual_machine, _query_existing_row(crm, key)
                    ) from retry_exc
            else:
                raise ConcurrentRunInAttio(date_str, actual_machine, existing) from exc
        else:
            raise ConcurrentRunInAttio(date_str, actual_machine, existing) from exc

    record_id = record.record_id
    run = DailyRun(
        crm=crm,
        record_id=record_id,
        run_date=date_str,
        machine_id=actual_machine,
        run_id=run_id,
        initial_counters={"connections": 0, "messages": 0, "visits": 0},
    )
    try:
        yield run
        # Success-path close uses the same best-effort + stderr-WARN
        # pattern as the failure path so a transient close-PATCH outage
        # doesn't leak an Attio HTTP error to the caller (whose body
        # already succeeded). Stuck status=running rows are forensic
        # debt; the WARN is the operator's signal to investigate.
        _close_with_warn(run, "completed", None)
    except BaseException as exc:
        # Non-Exception (KeyboardInterrupt, SystemExit) → aborted; Exception → failed.
        status: Literal["completed", "failed", "aborted"] = (
            "failed" if isinstance(exc, Exception) else "aborted"
        )
        _close_with_warn(run, status, _format_exc(exc))
        raise


_CLOSE_RETRY_ATTEMPTS = 3
_CLOSE_RETRY_BACKOFF_BASE = 5  # seconds; doubles each attempt


def _close_with_warn(
    run: DailyRun,
    status: Literal["completed", "failed", "aborted"],
    failure_details: str | None,
) -> None:
    """Best-effort close-PATCH with 3× retry, then stderr WARN on failure.

    The close PATCH normally re-keys ``uniqueness_key`` to the released
    form (see ``_close``), freeing the running-lock atomically. If that
    PATCH fails after all retries, the row stays ``status=running`` STILL
    HOLDING the 2-part lock and blocks the operator's next same-day
    invocation. The WARN tells the operator the exact manual remedy: re-key
    the stuck row. (L4-4 audit fix: retry before giving up.)
    """
    last_exc: Exception | None = None
    for attempt in range(_CLOSE_RETRY_ATTEMPTS):
        try:
            run._close(status, failure_details=failure_details)
            return  # success
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            last_exc = exc
            if attempt < _CLOSE_RETRY_ATTEMPTS - 1:
                wait = _CLOSE_RETRY_BACKOFF_BASE * (2 ** attempt)
                time.sleep(wait)

    released = _released_uniqueness_key(
        status, run.run_date, run.machine_id, run.record_id
    )
    print(
        f"WARN: daily_run._close({status!r}) failed after {_CLOSE_RETRY_ATTEMPTS} "
        f"attempts for record {run.record_id!r}: "
        f"{type(last_exc).__name__}: {last_exc}. "
        f"Row is stuck in status=running and still holds the lock "
        f"{_uniqueness_key(run.run_date, run.machine_id)!r}. To unblock "
        f"the next same-day run, PATCH its uniqueness_key to {released!r}.",
        file=sys.stderr,
    )


def _is_stale_same_machine(existing: dict, machine_id: str) -> bool:
    """Return True when the colliding row is from the same machine AND its
    started_at is older than STALE_RUN_TAKEOVER_HOURS.

    Cross-machine rows always return False — we can't verify the other machine
    died, so we never auto-take over. (L4-4 audit fix.)
    """
    if existing.get("machine_id") != machine_id:
        return False
    started_at = existing.get("started_at", "")
    if not started_at:
        return False
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        age = datetime.now(UTC) - started
        return age > timedelta(hours=STALE_RUN_TAKEOVER_HOURS)
    except (ValueError, TypeError):
        return False


def _takeover_stale_row(
    crm: CRMProvider,
    stale_record_id: str,
    run_date: str,
    machine_id: str,
) -> None:
    """Mark a stale running row as abandoned, freeing its uniqueness_key lock.

    Logs loudly to stderr. (L4-4 audit fix.)
    """
    released_key = _released_uniqueness_key("abandoned", run_date, machine_id, stale_record_id)
    print(
        f"WARN: daily_run stale-run takeover: record {stale_record_id!r} for "
        f"{run_date!r}/{machine_id!r} has been running longer than "
        f"{STALE_RUN_TAKEOVER_HOURS}h — marking it abandoned.",
        file=sys.stderr,
    )
    crm.update_object_record(
        "daily_run",
        stale_record_id,
        {
            "status": "abandoned",
            "completed_at": datetime.now(UTC).isoformat(),
            "uniqueness_key": released_key,
            "failure_details": (
                f"[stale-run takeover] Process did not close this row within "
                f"{STALE_RUN_TAKEOVER_HOURS}h; assumed crashed. "
                f"Taken over by new run on {datetime.now(UTC).isoformat()}."
            )[:4096],
        },
    )


def _query_existing_row(crm: CRMProvider, key: str) -> dict | None:
    """Look up the daily_run row that collided with us. Returns parsed
    forensics suitable for the ConcurrentRunInAttio message; None if the
    lookup itself fails (don't crash on the error path).

    Reads through the provider's ``query_object_records``; the ``filters`` body
    is the vendor-native uniqueness_key equality (the documented filter-shape
    leak). Forensics fields come off the normalized ``Record.attributes``.
    """
    try:
        rows = crm.query_object_records(
            "daily_run", filters={"uniqueness_key": key}, limit=1
        )
    except (httpx.HTTPStatusError, httpx.RequestError, KeyError):
        return None
    if not rows:
        return None
    row = rows[0]
    attrs = row.attributes
    return {
        "hostname": _attr_text(attrs, "hostname"),
        "started_at": _attr_text(attrs, "started_at"),
        "run_id": _attr_text(attrs, "run_id"),
        # L4-4: include machine_id + record_id for stale-row takeover.
        "machine_id": _attr_text(attrs, "machine_id"),
        "record_id": row.record_id,
    }


def _attr_text(attributes: dict, key: str) -> str:
    """Read a normalized ``Record.attributes`` value as a plain string ("" when
    absent/None). The provider already flattened the vendor shape to a scalar."""
    value = attributes.get(key)
    return "" if value is None else str(value)


# ── #165 reattach primitive (DM approval gate) ───────────────────────────────


def _first_select(attributes: dict, key: str) -> str:
    """Read a SELECT attr (e.g. daily_run.status, reply_detection_status) from a
    normalized ``Record.attributes`` map, as a plain string ("" when
    absent/None).

    The provider's normalization (``object_record_first_value``) unwraps an
    Attio SELECT — returned as ``[{"option": {"title": <v>}, ...}]`` — to its
    ``option.title`` scalar, so by the time we read it here the value is already
    the title string (NOT a nested option dict). Reading the cross-process
    status this way is what lets ``send-dms``'s fail-closed ``!= "ok"`` guard see
    the real persisted value instead of a stale None (the cross-process DOA bug).
    """
    value = attributes.get(key)
    return "" if value is None else str(value)


def _first_int(attributes: dict, key: str) -> int:
    """Lenient parse of a numeric attr from ``Record.attributes``; 0 if
    absent/null/non-numeric.

    This is the parser for OPTIONAL / general fields where absence is a
    legitimate "not set yet" state. The daily_run capacity counters
    (``messages_sent`` / ``connections_sent`` / ``visits_sent``) do NOT use this
    path — for an existing row they are always present, so a missing counter is
    a malformed response and must fail closed via ``_strict_counter`` (see
    ``_attach_from_row``)."""
    value = attributes.get(key)
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _strict_counter(attributes: dict, key: str, record_id: str) -> int:
    """Strict parse of a daily_run capacity counter from a fetched row's
    normalized ``Record.attributes``.

    ``open_daily_run`` always writes the counters (0 at minimum) and
    ``_patch_counters`` rewrites them on every send, so on an EXISTING row a
    present ``0`` is valid but an absent / null / non-numeric value is never
    legitimate — it signals a malformed/partial CRM response. Fail closed
    (raise ``MalformedDailyRunRow``) rather than defaulting to 0, which would
    silently reset the cap and permit a 60-DM cap breach."""
    if key not in attributes:
        raise MalformedDailyRunRow(key, record_id)
    value = attributes[key]
    if value is None:
        raise MalformedDailyRunRow(key, record_id)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise MalformedDailyRunRow(key, record_id) from None


def query_todays_row(
    crm: CRMProvider, run_date: str, machine_id: str
) -> Record | None:
    """Return the daily_run row for (run_date, machine_id) regardless of its
    open/closed/released-key state, or None if none exists.

    Filters on the BARE attributes ``run_date`` + ``machine_id`` — NOT the
    composite ``uniqueness_key`` (which ``_query_existing_row`` uses) because a
    closed row's key was re-keyed to the released 4-part form on close
    (``_close`` → ``_released_uniqueness_key``), so a composite-key lookup would
    report "no row" for a day that was in fact opened and closed by step 1.
    Returns the normalized :class:`Record` (``object="daily_run"``).

    Multiple rows can legitimately share the BARE (run_date, machine_id) pair:
    only ONE holds the live 2-part uniqueness_key, but same-day re-runs after a
    close re-key the prior row to the released 4-part form (freeing the bare
    pair), and a stale-run takeover marks the crashed row ``abandoned`` and
    opens a fresh one. So the bare query can return several rows. Fetch a small
    page and choose deterministically: prefer the one whose status reads
    ``running`` (the live row); otherwise the most recent by ``started_at``.
    When more than one row matches, emit a loud operator warning naming the
    chosen record_id so an unexpected duplicate is visible, not silently
    resolved. ``filters`` is the vendor-native equality body (the documented
    filter-shape leak)."""
    rows = crm.query_object_records(
        "daily_run",
        filters={"run_date": run_date, "machine_id": machine_id},
        limit=10,
    )
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]

    # Multiple rows: prefer a "running" row, else the most recent by started_at.
    running = [r for r in rows if _first_select(r.attributes, "status") == "running"]
    if running:
        # If multiple "running" rows exist (a real anomaly), still pick the most
        # recent so the choice is deterministic.
        chosen = max(running, key=lambda r: _attr_text(r.attributes, "started_at"))
    else:
        chosen = max(rows, key=lambda r: _attr_text(r.attributes, "started_at"))
    click.echo(
        f"⚠ WARNING: {len(rows)} daily_run rows matched "
        f"(run_date={run_date!r}, machine_id={machine_id!r}); "
        f"attaching to record_id={chosen.record_id!r} "
        f"(status={_first_select(chosen.attributes, 'status')!r}, "
        f"started_at={_attr_text(chosen.attributes, 'started_at')!r}). "
        f"Inspect the others for a stale takeover or a same-day re-run.",
        err=True,
    )
    return chosen


def _attach_from_row(crm: CRMProvider, run_id: str, row: Record) -> DailyRun:
    """Build a DailyRun bound to an EXISTING fetched row, rehydrating both the
    capacity counters and the reply-detection status.

    THE CAP-BREACH GUARD (spec Top Risk + test #8): ``open_daily_run`` hard-codes
    ``messages_sent: 0``; if this factory forgot to pull the fetched values,
    ``send-dms`` would start at 0/30 and the operator could ship up to 60 DMs
    across the two steps on a 30/day cap. ``initial_counters`` MUST carry the
    persisted ``messages_sent`` / ``connections_sent`` / ``visits_sent`` so
    ``remaining("messages") == 30 - N``.

    Counters are read via the STRICT ``_strict_counter`` path: a missing / null /
    non-numeric counter on an existing row is a malformed response and raises
    ``MalformedDailyRunRow`` (fail closed) rather than defaulting to 0. A present
    ``0`` is valid."""
    attrs = row.attributes
    record_id = row.record_id
    run = DailyRun(
        crm=crm,
        record_id=record_id,
        run_date=_attr_text(attrs, "run_date"),
        machine_id=_attr_text(attrs, "machine_id"),
        run_id=run_id,
        initial_counters={
            "connections": _strict_counter(attrs, "connections_sent", record_id),
            "messages": _strict_counter(attrs, "messages_sent", record_id),
            "visits": _strict_counter(attrs, "visits_sent", record_id),
        },
    )
    # Rehydrate the reply-detection status into the in-memory field that
    # get_reply_detection_status() (and the send-dms guard) reads. "" → None.
    # reply_detection_status is a SELECT attr — read via _first_select so the
    # option.title is found (the cross-process gate depends on it).
    status = _first_select(attrs, "reply_detection_status")
    run._reply_detection_status = status or None
    return run


@contextmanager
def attach_daily_run(
    crm: CRMProvider,
    run_id: str,
    run_date: date | None = None,
    machine_id: str | None = None,
    *,
    read_only: bool = False,
) -> Iterator[DailyRun]:
    """Find-or-fail reattach to today's existing daily_run row for the DM step.

    Component 2 state-machine (spec, follow exactly):
      1. (Caller holds the sales-daily flock — same lock `daily` uses.)
      2. Query today's row on the BARE (run_date, machine_id) attrs. None →
         NoDailyRunRow (caller exits EX_TEMPFAIL).
      3. Build the DailyRun via _attach_from_row (rehydrates counters + reply
         status) FIRST — this strict-parses the counters and raises
         MalformedDailyRunRow BEFORE any reopen write. Ordering matters: a
         malformed terminal row must NOT be flipped to running (which would
         leave it holding the lock) only to then fail the rehydrate.
      4. status == "running" (step 1 crashed before close) → reattach as-is,
         do NOT re-key.
      5. status in {completed, failed, aborted} → write {status: running,
         uniqueness_key: "{date}|{machine}"} to reopen for the send portion.
         On a uniqueness collision (a concurrent step-1 retry holds the 2-part
         key) → ReopenCollision (fail-closed).
      6. On context exit, re-close (re-key to released form). If the run
         recorded NO send this session, restore the row's ORIGINAL status
         rather than upgrading a prior "failed" to "completed".

    ``read_only`` (e.g. a --dry-run send-dms, or the no-sender-id early return
    preview): attach to the row as a SNAPSHOT — do NOT reopen and do NOT close
    on exit, so a terminal row's prior status is never rewritten. A missing row
    still raises NoDailyRunRow.
    """
    actual_date = run_date or date.today()
    actual_machine = derive_machine_id(machine_id)
    date_str = actual_date.isoformat()

    row = query_todays_row(crm, date_str, actual_machine)
    if row is None:
        raise NoDailyRunRow(date_str, actual_machine)

    record_id = row.record_id
    # status is a SELECT attr — read via _first_select so a "running" row is
    # recognised and NOT needlessly reopened.
    prior_status = _first_select(row.attributes, "status")

    # Validate / rehydrate FIRST: strict counter parse raises before any write,
    # so a malformed terminal row never gets flipped to "running" and stranded.
    run = _attach_from_row(crm, run_id, row)

    if read_only:
        # Snapshot attach: no reopen, no close — never rewrite a terminal row.
        yield run
        return

    if prior_status != "running":
        # Reopen a closed row: flip status back to running and re-key to the
        # 2-part running-lock for the send portion. The released-key escape
        # hatch (_released_uniqueness_key) is what made this row's key free to
        # re-acquire; if a concurrent step-1 retry grabbed it first the re-key
        # write collides on the unique constraint and we fail closed.
        try:
            crm.update_object_record(
                "daily_run",
                record_id,
                {
                    "status": "running",
                    "uniqueness_key": _uniqueness_key(date_str, actual_machine),
                },
            )
        except UniquenessConflictError:
            raise ReopenCollision(date_str, actual_machine, record_id) from None

    # Snapshot the counter total so a clean exit can tell whether THIS session
    # actually sent anything — if not, we restore the prior status instead of
    # upgrading failed→completed.
    sent_before = sum(run._counters.values())
    try:
        yield run
        clean_status: Literal["completed", "failed", "aborted"] = "completed"
        if sum(run._counters.values()) == sent_before and prior_status in (
            "completed",
            "failed",
            "aborted",
        ):
            # No send this session and the row was terminal → restore its
            # ORIGINAL status rather than silently rewriting failed→completed.
            clean_status = prior_status  # type: ignore[assignment]
        _close_with_warn(run, clean_status, None)
    except BaseException as exc:
        status_out: Literal["completed", "failed", "aborted"] = (
            "failed" if isinstance(exc, Exception) else "aborted"
        )
        _close_with_warn(run, status_out, _format_exc(exc))
        raise


def _format_exc(exc: BaseException) -> str:
    """Format an exception for the daily_run.failure_details long_text."""
    body = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return body or repr(exc)


# Module-exit hook for tests + CLI: re-exposed shim so callers can
# `from workflows.daily_run import record_send` if they hold a handle.
def record_send(run: DailyRun, kind: SendKind, count: int = 1) -> None:
    """Function-style alias for ``run.record_send`` — exists so the
    registry-declared write owner ``workflows.daily_run.record_send`` can
    be matched against a static call site in CI guards."""
    run.record_send(kind, count)


__all__ = [
    "MAX_CONNECTIONS_PER_DAY",
    "MAX_MESSAGES_PER_DAY",
    "MAX_VISITS_PER_DAY",
    "STALE_RUN_TAKEOVER_HOURS",
    "CapacityExhausted",
    "ConcurrentRunInAttio",
    "DailyRun",
    "MalformedDailyRunRow",
    "NoDailyRunRow",
    "ReopenCollision",
    "SendKind",
    "attach_daily_run",
    "derive_machine_id",
    "open_daily_run",
    "query_todays_row",
    "record_send",
]
