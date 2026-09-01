#!/usr/bin/env python3
"""Purge old files from the exports/ directory.

Default behaviour is a *dry-run* that lists what would be deleted.
Pass ``--delete`` to actually remove files.

Selection rules:
  - Files older than ``--days N`` (default 60) are candidates.
  - ``.gitkeep`` is always excluded.
  - ``scrape_cursors.json`` is always excluded (live ingest state, not an
    export artifact — see workflows/scrape_cursor.py).
  - Files matching ``weekly_borderline_*.jsonl`` that are newer than
    30 days are excluded (they are forensic records; keep the recent ones).

Usage:
    python3 scripts/purge_old_exports.py [--days N] [--delete] [--exports-dir PATH]

Exit codes:
    0 — success (dry-run or actual delete completed without errors)
    1 — unexpected error
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatch
from pathlib import Path


def _should_keep(
    path: Path,
    cutoff: datetime,
    borderline_cutoff: datetime,
) -> bool:
    """Return True if this file should be KEPT (not purged).

    ``path`` is a file under the exports directory (not a directory itself).
    """
    name = path.name

    # Always keep .gitkeep.
    if name == ".gitkeep":
        return True

    # Always keep the weekly ingest cursor. It is live pipeline STATE, not an
    # export artifact: deleting it makes the next weekly re-consume every
    # accumulating PB result file from row 0 (workflows/scrape_cursor.py). It
    # is also naturally old — a drained search's entry is never rewritten.
    if name == "scrape_cursors.json":
        return True

    # Keep weekly borderline files that are still recent.
    if fnmatch(name, "weekly_borderline_*.jsonl"):
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        except OSError:
            # If we can't stat the file, keep it to be safe.
            return True
        if mtime >= borderline_cutoff:
            return True

    # Age check: keep files newer than cutoff.
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return True
    return mtime >= cutoff


def select_files_to_purge(
    exports_dir: Path,
    *,
    max_age_days: int = 60,
    borderline_keep_days: int = 30,
    now: datetime | None = None,
) -> list[Path]:
    """Return a sorted list of files that should be purged.

    Factored out of ``main`` so tests can exercise the selection logic
    without touching the filesystem or sys.argv. (L10-5 audit addition.)

    Args:
        exports_dir: Directory to scan (non-recursive — top-level files only).
        max_age_days: Files older than this many days are candidates.
        borderline_keep_days: ``weekly_borderline_*.jsonl`` files newer than
            this many days are excluded even if they'd otherwise be purged.
        now: Override "current time" for testing; defaults to UTC now.
    """
    effective_now = now or datetime.now(tz=UTC)
    cutoff = effective_now - timedelta(days=max_age_days)
    borderline_cutoff = effective_now - timedelta(days=borderline_keep_days)

    candidates: list[Path] = []
    if not exports_dir.exists():
        return candidates

    for entry in sorted(exports_dir.iterdir()):
        if not entry.is_file():
            continue
        if not _should_keep(entry, cutoff, borderline_cutoff):
            candidates.append(entry)

    return candidates


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Purge old files from the exports/ directory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--days",
        type=int,
        default=60,
        metavar="N",
        help="Delete files older than N days (default: 60).",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        default=False,
        help=(
            "Actually delete the files. Without this flag the script only "
            "lists what would be removed (dry-run, the default)."
        ),
    )
    parser.add_argument(
        "--exports-dir",
        type=Path,
        default=None,
        help=(
            "Path to the exports directory "
            "(default: exports/ relative to the script's project root)."
        ),
    )
    args = parser.parse_args(argv)

    # Resolve the exports directory: default is <project-root>/exports/
    # (__file__ is scripts/purge_old_exports.py; parent.parent = project root.)
    exports_dir = (
        args.exports_dir if args.exports_dir is not None
        else Path(__file__).parent.parent / "exports"
    )

    to_purge = select_files_to_purge(exports_dir, max_age_days=args.days)

    if not to_purge:
        print("Nothing to purge.")
        return 0

    if not args.delete:
        print(f"Dry-run: {len(to_purge)} file(s) would be deleted (pass --delete to remove):")
        for p in to_purge:
            try:
                mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=UTC)
                age_days = (datetime.now(tz=UTC) - mtime).days
            except OSError:
                age_days = -1
            print(f"  {p.name}  ({age_days}d old)")
        return 0

    # Actual delete.
    errors = 0
    for p in to_purge:
        try:
            p.unlink()
            print(f"Deleted: {p.name}")
        except OSError as exc:
            print(f"ERROR: could not delete {p.name}: {exc}", file=sys.stderr)
            errors += 1

    if errors:
        print(f"\n{errors} error(s) occurred.", file=sys.stderr)
        return 1

    print(f"\nDeleted {len(to_purge)} file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
