"""Pipeline-owned ingest cursor for accumulating PhantomBuster result CSVs.

Why this exists (the weekly-recycling RCA — a measured 98% of one run's
passes were people the pipeline had already scored):

PhantomBuster keys BOTH its per-search resume position AND its
processed-profiles dedup database on the result CSV *filename*. The
weekly SN scrape used to pass a fresh `csvName` on every launch
(PR #179's defense against PB serving a frozen cached file), which
reset PB to page 1 of every saved search — so every weekly run
re-exported the same top-N people.

Restoring a STABLE per-search `csvName` fixes PB's side: the phantom
resumes where it left off and APPENDS to one accumulating file. But it
re-opens the hazard PR #179 closed: PB can log "already scraped",
append ZERO rows, and still serve the (unchanged) old file, which the
pipeline would ingest as if it were fresh.

This module is the replacement defense. We keep our OWN cursor — how
many rows of each accumulating file WE have already consumed — so the
pipeline never re-ingests old rows and a zero-row append is visible as
`file_total == our_cursor` instead of looking like fresh data.

The cursor deliberately records rows CONSUMED BY US, not rows present
in the file. That asymmetry is the async-safety property, and it is why
the ADVANCE lives in the caller (`run_weekly_prospecting`'s per-search
loop) and not in the downloader: the cursor moves only after
`_process_prospects` has returned without raising. A PB container that
times out on our side but finishes later and appends rows — or an
ingest that dies mid-scoring — leaves `file_total > cursor`, so the
NEXT weekly run picks the rows up as its delta. Nothing is silently
lost by a failure; at worst rows are re-served and absorbed by the
ingest-side dedup in `_process_prospects` (in_list_canonical_urls /
seen_urls / name_index).

State file: `<repo root>/exports/scrape_cursors.json`, a flat map

    {"wk-operations-leaders-mexico": {"consumed_rows": 380,
                                      "sn_url_sha8": "1f4b90c2",
                                      "last_row_url": "https://…/in/x",
                                      "updated_at": "2026-08-31T12:00:00+00:00"}}

`sn_url_sha8` and `last_row_url` are integrity anchors, both optional
for backward compatibility with entries written before they existed:

  * `sn_url_sha8` catches an operator swapping the saved-search URL
    behind a csvName. The old cursor then indexes a different search's
    rows, so we reset to 0. (PB's OWN file-side resume position for the
    replaced URL is out of our control — the reset warning says so.)
  * `last_row_url` catches the file being rebuilt with a DIFFERENT
    prefix at the same or greater length, which a row count alone
    cannot see. Mismatch → reset to 0 and re-consume.

Corruption is LOUD. A missing file (or a csv_name we have never seen)
legitimately means "consumed nothing yet" → 0. A file we cannot parse,
or an entry whose shape is wrong, means the state is untrustworthy —
returning 0 there would silently re-ingest an entire search, so we
raise instead and let an operator look.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# Anchored to the repo root, NOT the process cwd: the weekly is invoked from
# cron, from skills, and by hand from assorted directories, and a cwd-relative
# path would silently start a FRESH cursor (→ re-ingest the whole accumulating
# file) whenever the caller's cwd differed.
#
# Residual, accepted: a git worktree checkout is its own repo root and so has
# its own cursor file. A wet run from a worktree therefore re-consumes rows the
# main checkout already ingested. The run-provenance staleness check is the
# guard against that; the re-ingest itself is absorbed by the ingest-side dedup
# in `_process_prospects`, but it still burns LLM budget on already-scored
# people. Run wet weeklies from the main checkout.
_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CURSOR_PATH = _REPO_ROOT / "exports" / "scrape_cursors.json"


class CursorStateCorruptError(RuntimeError):
    """The cursor state file exists but cannot be trusted.

    Raised instead of degrading to 0 — a silent 0 would re-consume every
    row of an accumulating CSV and flood the qualifier with people we
    already scored.
    """


@dataclass(frozen=True)
class CursorRead:
    """What we know about a csv_name's ingest position at read time.

    `url_changed` is a *finding*, not an error: the caller decides how to
    say so. It already carries the reset (consumed_rows is 0 when the
    stored search URL no longer matches), so a caller that ignores the
    flag still behaves safely — it just says nothing about why.
    """

    consumed_rows: int
    last_row_url: str | None = None
    url_changed: bool = False


def url_fingerprint(sn_url: str) -> str:
    """Short stable fingerprint of a saved-search URL (cursor integrity key)."""
    return hashlib.sha256(sn_url.encode()).hexdigest()[:8]


def _resolve(path: Path | None) -> Path:
    return DEFAULT_CURSOR_PATH if path is None else path


def _load_state(path: Path) -> dict:
    """Read the whole cursor map. Missing file → empty map; bad file → raise."""
    if not path.exists():
        return {}
    try:
        raw = path.read_text()
    except OSError as exc:
        raise CursorStateCorruptError(
            f"cursor state {path} exists but could not be read: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    # An empty/whitespace file is a truncated write, not "no cursors yet" —
    # `advance_cursor` only ever lands a complete file via os.replace, so
    # emptiness means something else damaged it.
    if not raw.strip():
        raise CursorStateCorruptError(
            f"cursor state {path} is empty — likely a truncated write; "
            "inspect it before re-running the weekly scrape"
        )
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CursorStateCorruptError(
            f"cursor state {path} is not valid JSON ({exc}) — inspect it "
            "before re-running the weekly scrape"
        ) from exc
    if not isinstance(state, dict):
        raise CursorStateCorruptError(
            f"cursor state {path} must be a JSON object, got {type(state).__name__}"
        )
    return state


def read_cursor_state(
    csv_name: str, path: Path | None = None, *, sn_url: str | None = None
) -> CursorRead:
    """Full ingest position for `csv_name`, including the integrity anchors.

    Returns `consumed_rows == 0` when the state file is absent, carries no
    entry for `csv_name`, or (when `sn_url` is given) records a DIFFERENT
    search URL than the one about to be launched — the last case also sets
    `url_changed` so the caller can say so loudly.

    Raises `CursorStateCorruptError` when the file exists but is unreadable
    or the entry's shape is wrong.
    """
    state = _load_state(_resolve(path))
    entry = state.get(csv_name)
    if entry is None:
        return CursorRead(consumed_rows=0)
    if not isinstance(entry, dict):
        raise CursorStateCorruptError(
            f"cursor entry for {csv_name!r} must be an object, "
            f"got {type(entry).__name__}"
        )
    consumed = entry.get("consumed_rows")
    # `isinstance(True, int)` is True in Python — exclude bools explicitly so a
    # hand-edited `true` doesn't read as a cursor of 1.
    if isinstance(consumed, bool) or not isinstance(consumed, int) or consumed < 0:
        raise CursorStateCorruptError(
            f"cursor entry for {csv_name!r} has invalid consumed_rows="
            f"{consumed!r}; expected a non-negative int"
        )

    stored_url_sha = entry.get("sn_url_sha8")
    if (
        sn_url is not None
        and isinstance(stored_url_sha, str)
        and stored_url_sha != url_fingerprint(sn_url)
    ):
        # The saved search behind this csvName was replaced. Our count indexes
        # rows of the OLD search; carrying it forward would skip the new
        # search's first N people forever.
        return CursorRead(consumed_rows=0, url_changed=True)

    last_row_url = entry.get("last_row_url")
    if not isinstance(last_row_url, str) or not last_row_url:
        last_row_url = None
    return CursorRead(consumed_rows=consumed, last_row_url=last_row_url)


def read_cursor(csv_name: str, path: Path | None = None) -> int:
    """Rows of `csv_name` this pipeline has already consumed.

    Thin accessor over `read_cursor_state` for callers (and operators) that
    only want the count.
    """
    return read_cursor_state(csv_name, path).consumed_rows


def advance_cursor(
    csv_name: str,
    new_total_rows: int,
    path: Path | None = None,
    *,
    sn_url: str | None = None,
    last_row_url: str | None = None,
) -> None:
    """Record that we have now consumed `new_total_rows` rows of `csv_name`.

    Call this ONLY after the rows have actually been ingested — the caller
    in `run_weekly_prospecting` advances after `_process_prospects` returns
    without raising. See the module docstring for why that ordering is the
    async-safety property.

    `sn_url` and `last_row_url` are the integrity anchors checked on the next
    read (search-URL swap and file-prefix rebuild respectively).

    Not monotonic by contract: the shrink path (PB storage reset, file
    recreated from scratch) legitimately moves the cursor DOWN to the new
    smaller total. Written atomically (tmp file in the same directory +
    `os.replace` + a directory fsync) so a crash mid-write can never leave a
    half-JSON file that the next run would have to treat as corrupt.

    Deliberately NOT reusing `workflows.llm_dispatch._atomic_write_json`:
    that writer belongs to the dispatch lane and promoting it into a shared
    module would couple two otherwise-independent subsystems for ~10 lines.
    Reviewed and left local on purpose.
    """
    if isinstance(new_total_rows, bool) or not isinstance(new_total_rows, int):
        raise TypeError(f"new_total_rows must be an int, got {new_total_rows!r}")
    if new_total_rows < 0:
        raise ValueError(f"new_total_rows must be non-negative, got {new_total_rows}")

    target = _resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    state = _load_state(target)
    entry: dict[str, object] = {
        "consumed_rows": new_total_rows,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    if sn_url is not None:
        entry["sn_url_sha8"] = url_fingerprint(sn_url)
    if last_row_url:
        entry["last_row_url"] = last_row_url
    state[csv_name] = entry

    fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        # Leave no orphan tmp file behind — the directory is `exports/`, which
        # operators read by eye. Cleanup must never mask the real exception.
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise

    # fsync the DIRECTORY too: os.replace makes the rename atomic, but on a
    # crash before the directory entry is flushed the rename itself can be
    # lost, resurrecting the previous cursor and re-ingesting a delta.
    # Suppressed rather than fatal — directory fds are not fsync-able on every
    # platform/filesystem, and a durability best-effort must not fail a run
    # whose rows are already ingested.
    with contextlib.suppress(OSError):
        dir_fd = os.open(str(target.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
