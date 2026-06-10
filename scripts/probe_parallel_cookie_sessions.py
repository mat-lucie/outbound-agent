"""Probe whether running the Sales Navigator Profile Scraper invalidates the
regular LinkedIn session cookie used by the other phantoms.

Closes adversarial review finding F6: LinkedIn's concurrent-session detection
may invalidate `PB_LI_SESSION_COOKIE` when PhantomBuster's IP pool runs both a
regular-LI session AND a Sales-Nav session against the same account at
overlapping times. If that happens mid-day, the cascade kills the regular
Profile Scraper, Network Booster, Message Sender, and Inbox Scraper all at
once — far worse than the migration's intended scope.

## How it works

Three-phase health check, all read-only:

1. **PRE-CHECK** — launch the SN Inbox Scraper (`PB_INBOX_SCRAPER_ID`) with
   `numberOfThreadsToScrape=1`. Uses `PB_LI_SESSION_COOKIE` (regular li_at).
   Verifies the regular cookie was good *before* the migration's new path
   does anything.

2. **SALES NAV TEST** — launch the Sales Nav Profile Scraper
   (`PB_SALES_NAV_PROFILE_SCRAPER_ID`) on a single user-provided test URL.
   Uses `PB_LI_SALES_NAV_*` cookies. If `--known-good-url` is omitted, this
   phase is skipped with a warning — the probe is then incomplete (it only
   confirms the regular cookie *currently* works, not whether the parallel
   Sales Nav session would break it).

3. **POST-CHECK** — re-launch the SN Inbox Scraper with the regular cookie.
   If this returns 401 (or HTTP 200 from PB but with `lastEndStatus="error"`
   citing auth), the parallel-session hypothesis is confirmed: the migration
   must use a SEPARATE LinkedIn account for Sales Nav cookies, not the same
   account as the regular phantoms.

Verdict:
- `coexist_ok` — both pre-check and post-check succeed
- `parallel_session_conflict` — pre-check succeeded but post-check failed in
  a way consistent with cookie invalidation
- `incomplete` — phase 2 was skipped, OR pre-check itself failed (meaning
  regular cookie was already broken before this probe)

Output: `~/.outbound-agent/probe/parallel-cookie-sessions-YYYY-MM-DD.json`

## Usage

    python3 scripts/probe_parallel_cookie_sessions.py
    python3 scripts/probe_parallel_cookie_sessions.py --known-good-url "https://linkedin.com/sales/lead/..."
    python3 scripts/probe_parallel_cookie_sessions.py --dry-run

Production gate: per plan reflective-singing-waterfall.md PR-A verification, if
this probe returns `parallel_session_conflict`, STOP the migration and escalate
to the operator. Do not proceed to schema migration or backfill.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

from clients.pb_envelope import PBRunFailed, PBRunTimeout  # noqa: E402
from clients.phantombuster import PhantomBusterClient  # noqa: E402

PROBE_DIR = Path.home() / ".outbound-agent" / "probe"


@dataclass
class PhaseResult:
    phase: str
    status: str  # "ok" | "failed" | "skipped"
    detail: str = ""
    container_id: str = ""
    last_end_status: str = ""
    last_end_message: str = ""


@dataclass
class ProbeReport:
    run_at: str
    phases: list[PhaseResult] = field(default_factory=list)
    verdict: str = "incomplete"
    verdict_detail: str = ""


def _regular_session_args() -> dict:
    cookie = os.environ.get("PB_LI_SESSION_COOKIE", "").strip()
    if not cookie:
        raise SystemExit("ERROR: PB_LI_SESSION_COOKIE not set.")
    ua = os.environ.get("PB_LI_USER_AGENT", "").strip()
    args: dict = {"sessionCookie": cookie}
    if ua:
        args["userAgent"] = ua
    return args


def _sales_nav_session_args() -> dict:
    li_at = os.environ.get("PB_LI_SALES_NAV_SESSION_COOKIE", "").strip()
    li_a = os.environ.get("PB_LI_SALES_NAV_LI_A_COOKIE", "").strip()
    if not li_at or not li_a:
        raise SystemExit(
            "ERROR: PB_LI_SALES_NAV_SESSION_COOKIE and PB_LI_SALES_NAV_LI_A_COOKIE both required."
        )
    ua = os.environ.get("PB_LI_USER_AGENT", "").strip()
    args: dict = {"sessionCookie": li_at, "li_a": li_a}
    if ua:
        args["userAgent"] = ua
    return args


def run_inbox_health_check(
    pb: PhantomBusterClient, scraper_id: str, phase_name: str
) -> PhaseResult:
    """Launch SN Inbox Scraper with limit=1 — cheapest read-only check of the
    regular cookie."""
    try:
        launch = pb.launch_agent(
            scraper_id,
            {
                "inboxFilter": "all",
                "numberOfThreadsToScrape": 1,
                **_regular_session_args(),
            },
        )
        completion = pb.wait_for_completion(
            launch, poll_interval=10, max_wait=180
        )
        # Inspect output for LinkedIn auth errors. PB returns status=finished
        # even when LinkedIn 401s the cookie — the failure is in the log.
        last_output = pb.get_container_output(launch.container_id, launch.agent_id)
        log = (last_output.get("output", "") or "").lower()
        if "401" in log or "session cookie" in log or "li_at" in log and "expired" in log:
            return PhaseResult(
                phase=phase_name,
                status="failed",
                detail="PB completed but log indicates LinkedIn auth failure",
                container_id=launch.container_id,
                last_end_status=last_output.get("lastEndStatus", ""),
                last_end_message=last_output.get("lastEndMessage", ""),
            )
        return PhaseResult(
            phase=phase_name,
            status="ok",
            detail="inbox scrape completed; regular cookie accepted",
            container_id=launch.container_id,
            last_end_status=last_output.get("lastEndStatus", ""),
        )
    except PBRunFailed as exc:
        return PhaseResult(
            phase=phase_name,
            status="failed",
            detail=f"PBRunFailed: {exc}",
            container_id=getattr(exc, "container_id", ""),
        )
    except PBRunTimeout as exc:
        return PhaseResult(
            phase=phase_name,
            status="failed",
            detail=f"PBRunTimeout after {exc.elapsed_seconds}s — last_status={exc.last_observed_status}",
            container_id=exc.container_id,
        )


def run_sales_nav_test(
    pb: PhantomBusterClient,
    scraper_id: str,
    test_url: str,
) -> PhaseResult:
    """Launch Sales Nav Profile Scraper on the single test URL."""
    try:
        launch = pb.launch_agent(
            scraper_id,
            {"profileUrls": [test_url], **_sales_nav_session_args()},
        )
        completion = pb.wait_for_completion(
            launch, poll_interval=10, max_wait=300
        )
        last_output = pb.get_container_output(launch.container_id, launch.agent_id)
        return PhaseResult(
            phase="sales_nav_test",
            status="ok",
            detail="Sales Nav scrape completed; new cookie accepted",
            container_id=launch.container_id,
            last_end_status=last_output.get("lastEndStatus", ""),
        )
    except PBRunFailed as exc:
        return PhaseResult(
            phase="sales_nav_test",
            status="failed",
            detail=f"PBRunFailed: {exc}",
            container_id=getattr(exc, "container_id", ""),
        )
    except PBRunTimeout as exc:
        return PhaseResult(
            phase="sales_nav_test",
            status="failed",
            detail=f"PBRunTimeout after {exc.elapsed_seconds}s",
            container_id=exc.container_id,
        )


def compute_verdict(phases: list[PhaseResult]) -> tuple[str, str]:
    by_phase = {p.phase: p for p in phases}
    pre = by_phase.get("pre_check")
    sales_nav = by_phase.get("sales_nav_test")
    post = by_phase.get("post_check")

    if not pre or pre.status != "ok":
        return (
            "incomplete",
            "PRE-CHECK failed: regular cookie was already broken before this "
            "probe. Rotate PB_LI_SESSION_COOKIE first, then re-run.",
        )
    if not sales_nav or sales_nav.status == "skipped":
        return (
            "incomplete",
            "Sales Nav test phase was skipped (no --known-good-url provided). "
            "Probe only confirmed the regular cookie currently works; did NOT "
            "test whether a parallel Sales Nav session would invalidate it.",
        )
    if sales_nav.status != "ok":
        return (
            "incomplete",
            f"Sales Nav scrape itself failed: {sales_nav.detail}. "
            "Cannot test cookie coexistence — fix the Sales Nav setup first "
            "(check phantom ID + Sales Nav cookies).",
        )
    if not post or post.status != "ok":
        return (
            "parallel_session_conflict",
            f"POST-CHECK failed after a successful Sales Nav scrape: "
            f"{post.detail if post else 'phase missing'}. The Sales Nav "
            "session likely invalidated the regular cookie. STOP migration "
            "and use a separate LinkedIn account for Sales Nav.",
        )
    return (
        "coexist_ok",
        "Regular cookie still works after a Sales Nav scrape — cookies "
        "coexist safely. Migration can proceed.",
    )


def write_report(report: ProbeReport, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_at": report.run_at,
        "verdict": report.verdict,
        "verdict_detail": report.verdict_detail,
        "phases": [
            {
                "phase": p.phase,
                "status": p.status,
                "detail": p.detail,
                "container_id": p.container_id,
                "last_end_status": p.last_end_status,
                "last_end_message": p.last_end_message,
            }
            for p in report.phases
        ],
    }
    output_path.write_text(json.dumps(payload, indent=2))


def print_summary(report: ProbeReport, output_path: Path) -> None:
    print("\n=== Parallel cookie sessions probe ===")
    print(f"Run at:  {report.run_at}\n")
    for p in report.phases:
        flag = {"ok": "✓", "failed": "✗", "skipped": "—"}.get(p.status, "?")
        print(f"  {flag} {p.phase:20s} status={p.status:8s} {p.detail}")
    print(f"\nVERDICT: {report.verdict}")
    print(f"  → {report.verdict_detail}")
    print(f"\nWrote: {output_path}")
    if report.verdict == "parallel_session_conflict":
        print("\n*** STOP THE MIGRATION ***")
        print("Per plan reflective-singing-waterfall.md PR-A verification,")
        print("a parallel_session_conflict verdict requires the Sales Nav")
        print("cookies to come from a SEPARATE LinkedIn account.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--known-good-url",
        type=str,
        default=None,
        help=(
            "Sales Nav profile URL (linkedin.com/sales/lead/<id>,<auth>) to "
            "use as the test target. Quote the URL because it contains a "
            "comma. If omitted, phase 2 is skipped and the verdict will be "
            "'incomplete'."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate env, but skip all PB launches.",
    )
    args = parser.parse_args(argv)

    load_dotenv()
    inbox_scraper_id = os.environ.get("PB_INBOX_SCRAPER_ID", "").strip()
    sales_nav_scraper_id = os.environ.get(
        "PB_SALES_NAV_PROFILE_SCRAPER_ID", ""
    ).strip()
    if not inbox_scraper_id:
        print("ERROR: PB_INBOX_SCRAPER_ID not set.", file=sys.stderr)
        return 1
    if not sales_nav_scraper_id:
        print(
            "ERROR: PB_SALES_NAV_PROFILE_SCRAPER_ID not set.", file=sys.stderr
        )
        return 1

    # Validate cookies upfront — fail loud, no wasted launches.
    _regular_session_args()
    _sales_nav_session_args()

    report = ProbeReport(run_at=datetime.now(UTC).isoformat())

    if args.dry_run:
        report.phases.append(
            PhaseResult(
                phase="pre_check",
                status="skipped",
                detail="dry-run: skipped PB launch",
            )
        )
        report.phases.append(
            PhaseResult(
                phase="sales_nav_test",
                status="skipped",
                detail="dry-run: skipped PB launch",
            )
        )
        report.phases.append(
            PhaseResult(
                phase="post_check",
                status="skipped",
                detail="dry-run: skipped PB launch",
            )
        )
        report.verdict = "incomplete"
        report.verdict_detail = "dry-run: all phases skipped"
    else:
        with PhantomBusterClient() as pb:
            report.phases.append(
                run_inbox_health_check(pb, inbox_scraper_id, "pre_check")
            )
            # Only attempt phase 2 if pre_check passed AND a test URL exists.
            if (
                report.phases[-1].status == "ok"
                and args.known_good_url
            ):
                report.phases.append(
                    run_sales_nav_test(
                        pb, sales_nav_scraper_id, args.known_good_url
                    )
                )
                report.phases.append(
                    run_inbox_health_check(
                        pb, inbox_scraper_id, "post_check"
                    )
                )
            elif report.phases[-1].status == "ok" and not args.known_good_url:
                report.phases.append(
                    PhaseResult(
                        phase="sales_nav_test",
                        status="skipped",
                        detail="--known-good-url not provided",
                    )
                )
                report.phases.append(
                    PhaseResult(
                        phase="post_check",
                        status="skipped",
                        detail="skipped because phase 2 was skipped",
                    )
                )
        verdict, detail = compute_verdict(report.phases)
        report.verdict = verdict
        report.verdict_detail = detail

    output_path = (
        PROBE_DIR / f"parallel-cookie-sessions-{date.today().isoformat()}.json"
    )
    write_report(report, output_path)
    print_summary(report, output_path)
    return 0 if report.verdict in ("coexist_ok", "incomplete") else 2


if __name__ == "__main__":
    sys.exit(main())
