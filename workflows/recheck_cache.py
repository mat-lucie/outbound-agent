"""Per-URL cache of recent LinkedIn checks (degree scrapes + profile visits).

Three callsites consult this cache to avoid burning PhantomBuster runs on
profiles we already saw within the TTL window:

  - Phase 0 (Profile Scraper for accept detection)
  - Pre-invite degree check (Profile Scraper for Pattern A/B guard)
  - Part A re-checks (Network Booster visits, no degree info)

Cache entries carry an optional ``degree`` ("1st"/"2nd"/"3rd"). When degree
is known and fresh, callers short-circuit the PB launch entirely (flip to
ACCEPTED, skip the invite, or skip the re-visit).

Thread/process safety (L4-5 audit fix):
  - ``_save`` uses an atomic tempfile + os.replace so a crash during write
    never truncates the cache file — readers see old or fully-written new.
  - All load-mutate-save cycles (``record_many``, ``clear``) hold an
    ``fcntl.flock`` exclusive lock on a lock file so concurrent processes
    don't race on the same JSON payload.
"""

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager, suppress
from datetime import date, timedelta
from pathlib import Path

import click

CACHE_FILE = Path.home() / ".outbound-agent" / "recheck_cache.json"
# Lock file sits next to the cache so it shares the same parent directory
# (must exist — we create it on first use just like the cache itself).
_LOCK_FILE = Path.home() / ".outbound-agent" / "recheck_cache.lock"

# A profile checked within this many days is considered fresh.
RECHECK_TTL_DAYS = 3

# Older entries are pruned on load to keep the file bounded.
RETENTION_DAYS = 14


def _normalize_url(url: str) -> str:
    """Match the normalization used in workflows/daily_check.py."""
    from urllib.parse import unquote

    return unquote(url).replace("://www.", "://").rstrip("/").lower()


def _today() -> str:
    return date.today().isoformat()


def _cutoff(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


def _load() -> dict[str, dict]:
    if not CACHE_FILE.exists():
        return {}
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        click.echo(
            f"WARN: recheck cache at {CACHE_FILE} is unreadable "
            f"({type(exc).__name__}: {exc}); treating as empty.",
            err=True,
        )
        return {}
    cutoff = _cutoff(RETENTION_DAYS)
    return {
        url: entry
        for url, entry in data.items()
        if isinstance(entry, dict) and (entry.get("checked_at") or "") >= cutoff
    }


@contextmanager
def _exclusive_lock():
    """Acquire an exclusive fcntl lock on the cache lock-file.

    Ensures that concurrent processes don't race on the load-mutate-save
    cycle. The lock file is created on first use. (L4-5 audit fix.)
    """
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_LOCK_FILE, "w") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)


def _save(data: dict[str, dict]) -> None:
    """Atomic write: serialize to a temp file, then os.replace into place.

    A crash during write never truncates the cache — readers see the old
    file or the fully-written new one. (L4-5 audit fix.)
    """
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    dir_path = str(CACHE_FILE.parent)
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp_path, CACHE_FILE)
    except Exception:
        # Clean up the temp file on failure so we don't litter.
        with suppress(OSError):
            os.unlink(tmp_path)
        raise


def lookup(url: str) -> dict | None:
    """Return cache entry if fresh (within RECHECK_TTL_DAYS), else None."""
    entry = _load().get(_normalize_url(url))
    if not entry:
        return None
    if (entry.get("checked_at") or "") < _cutoff(RECHECK_TTL_DAYS):
        return None
    return entry


def partition(urls: list[str]) -> tuple[dict[str, dict], list[str]]:
    """Split URLs into (fresh cache hits, stale URLs that need PB).

    Returns:
        fresh: {original_url: cache_entry} for URLs with a recent check.
        stale: [original_url, ...] preserving input order, for everything else.
    """
    cache = _load()
    cutoff = _cutoff(RECHECK_TTL_DAYS)
    fresh: dict[str, dict] = {}
    stale: list[str] = []
    for url in urls:
        entry = cache.get(_normalize_url(url))
        if entry and (entry.get("checked_at") or "") >= cutoff:
            fresh[url] = entry
        else:
            stale.append(url)
    return fresh, stale


def record_many(items: dict[str, str | None]) -> None:
    """Bulk-write checks. ``items`` maps URL → degree (None = visit-only).

    Holds an exclusive lock for the entire load-mutate-save cycle so
    concurrent processes don't race on the same JSON payload. (L4-5 fix.)
    """
    if not items:
        return
    with _exclusive_lock():
        cache = _load()
        today = _today()
        for url, degree in items.items():
            norm = _normalize_url(url)
            entry = cache.get(norm, {})
            entry["checked_at"] = today
            if degree:
                entry["degree"] = degree
            cache[norm] = entry
        _save(cache)


def record(url: str, degree: str | None = None) -> None:
    """Single-URL convenience wrapper around :func:`record_many`."""
    record_many({url: degree})


def clear(urls: list[str] | None = None) -> list[str]:
    """Remove cache entries to force a fresh re-scrape on the next run.

    ``urls=None`` clears the entire cache (every profile re-scrapes next run).
    Otherwise only the given URLs are removed. Returns the normalized URLs
    actually removed.

    Zero-risk: this only forces PhantomBuster re-scrapes — it mutates no
    pipeline/cadence/Attio state. Used by the Pattern-A one-time stuck-cohort
    drain so the Layer-1 hasPendingInvitation flip can advance already-invited
    prospects that were cached as plain 2nd/3rd-degree within the TTL window.

    Holds an exclusive lock for the entire load-mutate-save cycle. (L4-5 fix.)
    """
    with _exclusive_lock():
        cache = _load()
        removed: list[str]
        if urls is None:
            removed = list(cache.keys())
            _save({})
            return removed
        removed = []
        for url in urls:
            norm = _normalize_url(url)
            if norm in cache:
                del cache[norm]
                removed.append(norm)
        _save(cache)
        return removed
