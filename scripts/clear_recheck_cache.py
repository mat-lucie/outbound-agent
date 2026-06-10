#!/usr/bin/env python3
"""One-time recheck-cache clear — Pattern-A stuck-cohort drain.

Forces a fresh pre-invite scrape so the Layer-1 hasPendingInvitation flip can
advance already-invited prospects out of PROSPECT (they were cached as plain
2nd/3rd-degree within the TTL window, which bypassed the pending gate).

ZERO RISK: the recheck cache is a local JSON file
(~/.outbound-agent/recheck_cache.json). Clearing it only causes PhantomBuster
re-scrapes on the next run — it mutates NO pipeline/cadence/Attio state.

    # Preview (default):
    python scripts/clear_recheck_cache.py
    # Clear ALL entries:
    python scripts/clear_recheck_cache.py --apply
    # Clear specific URLs only:
    python scripts/clear_recheck_cache.py --apply --url <u1> --url <u2>
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from workflows import recheck_cache  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually clear the cache. Default is a dry-run preview.",
    )
    parser.add_argument(
        "--url", action="append", default=[],
        help="Specific URL(s) to clear. Omit to clear the entire cache.",
    )
    args = parser.parse_args(argv)
    urls = args.url or None

    existing = recheck_cache._load()
    if urls is None:
        target = list(existing.keys())
    else:
        target = [
            recheck_cache._normalize_url(u)
            for u in urls
            if recheck_cache._normalize_url(u) in existing
        ]

    if not args.apply:
        print(
            f"[dry-run] would clear {len(target)} cache entr(y/ies) "
            f"(of {len(existing)} total). Pass --apply to clear."
        )
        return 0

    removed = recheck_cache.clear(urls)
    print(f"cleared {len(removed)} cache entr(y/ies).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
