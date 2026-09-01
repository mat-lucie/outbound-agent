"""Local submission ledger for the Botdog send path (duplicate-send guard).

WHY THIS EXISTS
---------------
Botdog delivery is SUBMISSION-only: `BotdogSender.send_dm` returns as soon
as Botdog accepts the message, and the CRM stage/dm_step advance lands
LATER, when `workflows.botdog_ingest` sees the message-sent event. Between
those two moments the prospect's CRM row still looks "DM due" to
`run_dm_sequencing`, so the next daily run would compose and submit the
SAME DM again — a §3.1 red-line double-send visible to the prospect.

This module is layer 1 of a two-layer guard:

  1. THIS LEDGER — a local record of "we submitted `step` for `url` on
     `date`". Cheap, immediate, survives across runs on the same machine.
     `recent_submission` blocks a re-send inside the lookback window.
  2. A CRM `next_eligible_send_date` floor written at submission time
     (today + 3 business days). Survives a lost/never-existed ledger
     (fresh machine, wiped home dir, second operator seat) because it
     lives in the shared source of truth. The event-confirmed advance
     later OVERWRITES it with the real cadence floor.

Neither layer alone is sufficient: the ledger is machine-local, and the
CRM floor is a coarse bound that a stage change could interact with.
Together they bound re-sends in both the "same machine, next morning" case
and the "different machine / lost state" case.

Storage: ``~/.outbound-agent/botdog_submissions.json`` (same directory
convention as `workflows.safety_limits`), written via a tmp-file +
`os.replace` atomic swap so an interrupted write can never leave a
truncated JSON file that would silently reset the guard.

NOTE: no send path in this engine submits to Botdog (PhantomBuster owns
sending), so nothing writes this ledger today. It ships with the optional
transport so an operator who wires Botdog gets the duplicate-send guard
with it, and so `botdog_ingest`'s unledgered-send crosscheck has a ledger
to read.

[Consumer: agent] — operator-facing surfacing of these skips happens at
the call site, not here.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path

LEDGER_DIR = Path.home() / ".outbound-agent"
LEDGER_FILE = LEDGER_DIR / "botdog_submissions.json"

# How long a submission blocks a re-send by default. Botdog's own send
# schedule plus the event-poll cadence means a submitted DM normally
# confirms within a day or two; 14 days is a deliberately generous
# no-double-send window — a stuck submission that never confirms should be
# investigated by an operator, not silently re-sent.
DEFAULT_WITHIN_DAYS = 14

# Entries older than this are dropped on the next write. Longer than
# DEFAULT_WITHIN_DAYS so a caller passing a wider window still sees real
# data, short enough that the file stays small forever.
PRUNE_AFTER_DAYS = 30


def _key(normalized_url: str, step: str) -> str:
    return f"{normalized_url}|{step}"


def _load() -> dict[str, str]:
    """Read the ledger; a missing or unreadable file reads as empty.

    A corrupt file degrades to "no known submissions" rather than crashing
    the daily run — but that is the UNSAFE direction (it would permit a
    re-send), which is exactly why the CRM floor written by the caller is
    the second layer. The corruption is surfaced to the operator on stderr
    instead of being swallowed silently.
    """
    if not LEDGER_FILE.exists():
        return {}
    try:
        with open(LEDGER_FILE) as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        import sys
        print(
            f"WARNING: botdog submission ledger at {LEDGER_FILE} is "
            f"unreadable ({type(exc).__name__}: {exc}); treating as empty. "
            f"Re-send protection this run relies on the CRM "
            f"next_eligible_send_date floor alone.",
            file=sys.stderr,
        )
        return {}
    submissions = data.get("submissions")
    if not isinstance(submissions, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in submissions.items()
        if isinstance(value, str)
    }


def _save(submissions: dict[str, str]) -> None:
    """Persist the ledger via tmp-file + atomic `os.replace`."""
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=LEDGER_DIR, prefix=".botdog_submissions-", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump({"submissions": submissions}, handle, indent=2)
        os.replace(tmp_path, LEDGER_FILE)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def _prune(submissions: dict[str, str], today: date) -> dict[str, str]:
    """Drop entries older than PRUNE_AFTER_DAYS (and unparseable ones)."""
    cutoff = today - timedelta(days=PRUNE_AFTER_DAYS)
    kept: dict[str, str] = {}
    for key, raw in submissions.items():
        try:
            when = date.fromisoformat(raw[:10])
        except ValueError:
            continue
        if when >= cutoff:
            kept[key] = raw
    return kept


def record_submission(normalized_url: str, step: str, when: date) -> None:
    """Record that `step` was submitted to Botdog for `normalized_url`.

    `normalized_url` MUST come from the same normalizer the caller uses for
    lookups (`clients.pb_envelope._normalize_url_for_match`, exposed in
    daily_check as `_normalize_linkedin_url`) — a raw URL recorded here
    would never match a normalized lookup and the guard would be a no-op.

    A later submission for the same (url, step) overwrites the stored date,
    so the lookback window always measures from the MOST RECENT submission.
    Prunes entries older than PRUNE_AFTER_DAYS on every write.
    """
    if not normalized_url:
        return
    submissions = _prune(_load(), when)
    submissions[_key(normalized_url, step)] = when.isoformat()
    _save(submissions)


def recent_submission(
    normalized_url: str,
    step: str,
    within_days: int = DEFAULT_WITHIN_DAYS,
    *,
    today: date | None = None,
) -> date | None:
    """Return the date `step` was last submitted for `normalized_url`
    within the lookback window, or None when no such submission exists.

    A non-None return means the caller MUST NOT re-send: Botdog already
    accepted this exact step for this prospect and the delivery event
    simply has not been ingested yet.
    """
    if not normalized_url:
        return None
    raw = _load().get(_key(normalized_url, step))
    if not raw:
        return None
    try:
        when = date.fromisoformat(raw[:10])
    except ValueError:
        return None
    reference = today or date.today()
    if (reference - when).days > within_days:
        return None
    return when
