"""Smoke-test the PB Sales Navigator Profile Scraper end-to-end.

Pass a LinkedIn ``/in/`` URL (the phantom auto-converts to ``/sales/lead/``
internally — verified 2026-05-25). The script:

1. Loads ``PB_SALES_NAV_PROFILE_SCRAPER_ID`` + ``PB_LI_SALES_NAV_SESSION_COOKIE``
   from ``.env``
2. Reads the phantom's saved arguments via ``pb.get_agent()`` (required —
   the phantom rejects partial arg objects)
3. Injects the env cookie into ``identities[0].sessionCookie``
4. Overrides ``spreadsheetUrl`` with the input URL and
   ``numberOfProfilesPerLaunch=1``
5. Launches, waits for completion, downloads CSV
6. Prints column headers + key fields (``connectionDegree``,
   ``hasPendingInvitation``, ``fullName``, ``linkedinProfileUrl``,
   ``salesNavigatorUrl``)

Use this after rotating Sales Nav cookies to confirm the new value works
before running ``/sales-daily``. Functional replacement for the legacy
``scripts/debug_profile_scraper.py`` once
``PRE_INVITE_DEGREE_CHECK_BACKEND=sales_nav`` is the production setting.

Usage::

    python3 scripts/debug_sales_nav_profile_scraper.py https://linkedin.com/in/<slug>
    python3 scripts/debug_sales_nav_profile_scraper.py https://linkedin.com/in/<slug> --json
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

from clients.pb_envelope import PBRunFailed, PBRunTimeout  # noqa: E402
from clients.phantombuster import PhantomBusterClient  # noqa: E402


def _build_launch_args(pb: PhantomBusterClient, scraper_id: str, url: str) -> dict:
    """Read saved phantom args, inject env cookie, override scrape input."""
    agent = pb.get_agent(scraper_id)
    raw = agent.get("argument") or "{}"
    saved = json.loads(raw) if isinstance(raw, str) else raw
    cookie = os.environ.get("PB_LI_SALES_NAV_SESSION_COOKIE", "").strip()
    if not cookie:
        raise SystemExit(
            "ERROR: PB_LI_SALES_NAV_SESSION_COOKIE not set. See "
            "docs/runbooks/phantombuster-cookie-rotation.md."
        )
    ua = os.environ.get("PB_LI_USER_AGENT", "").strip()
    identities = saved.get("identities") or [{}]
    identities[0]["sessionCookie"] = cookie
    if ua:
        identities[0]["userAgent"] = ua
    return {
        **saved,
        "identities": identities,
        "spreadsheetUrl": url,
        "numberOfProfilesPerLaunch": 1,
    }


def _print_human(csv_text: str) -> None:
    print(f"CSV size: {len(csv_text)} bytes\n")
    reader = csv.DictReader(io.StringIO(csv_text))
    print("=== COLUMN HEADERS ===")
    print(reader.fieldnames)
    print()
    for row in reader:
        print("=== KEY FIELDS ===")
        for col in (
            "connectionDegree",
            "hasPendingInvitation",
            "fullName",
            "firstName",
            "lastName",
            "headline",
            "currentCompanyName",
            "linkedinProfileUrl",
            "salesNavigatorUrl",
            "query",
            "numberOfSharedConnections",
        ):
            if col in row:
                print(f"  {col:<32s} = {row[col]!r}")
        print()


def _print_json(csv_text: str) -> None:
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    print(json.dumps(rows, indent=2, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "url",
        help="LinkedIn /in/<slug> URL (or a Sales Nav /sales/lead/ URL — both work)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full CSV as JSON (default: human summary)",
    )
    parser.add_argument(
        "--max-wait",
        type=int,
        default=300,
        help="Seconds to wait for PB completion (default 300)",
    )
    args = parser.parse_args(argv)

    load_dotenv()
    scraper_id = os.environ.get(
        "PB_SALES_NAV_PROFILE_SCRAPER_ID", ""
    ).strip()
    if not scraper_id:
        print("ERROR: PB_SALES_NAV_PROFILE_SCRAPER_ID not set.", file=sys.stderr)
        return 1

    with PhantomBusterClient() as pb:
        launch_args = _build_launch_args(pb, scraper_id, args.url)
        print(f"Launching against {args.url!r}...")
        try:
            launch = pb.launch_agent(scraper_id, launch_args)
            print(f"  container_id: {launch.container_id}")
            print(f"  waiting for completion (max {args.max_wait}s)...")
            pb.wait_for_completion(launch, poll_interval=10, max_wait=args.max_wait)
        except PBRunFailed as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            return 2
        except PBRunTimeout as exc:
            print(
                f"TIMEOUT after {exc.elapsed_seconds}s "
                f"(last_status={exc.last_observed_status})",
                file=sys.stderr,
            )
            return 2
        csv_text = pb.download_result_csv(launch) or ""

    if not csv_text:
        print("ERROR: PB returned empty CSV.", file=sys.stderr)
        return 3

    if args.json:
        _print_json(csv_text)
    else:
        _print_human(csv_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
