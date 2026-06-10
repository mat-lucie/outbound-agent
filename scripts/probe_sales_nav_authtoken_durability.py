"""Probe whether Sales Navigator profile URLs are durable across viewer sessions.

This script answers one architectural question for the Sales Nav migration:
**when we store a `linkedin.com/sales/lead/<id>,<authToken>` URL on an Attio
Person record, will it still resolve degree weeks later — or is the embedded
authToken viewer-session-scoped and we must refresh weekly?**

The answer locks the schema shape in `scripts/migrate_attio_add_sales_nav_url.py`:
- `durable`   → single `sales_nav_url` text attribute, no refresh needed
- `ephemeral` → `sales_nav_url` + `sales_nav_url_captured_at` timestamp +
                  weekly refresh via the `/sales-nav-backfill` skill
- `inconclusive` → default to ephemeral shape (safer)

## How it works

Input: a CSV of `(sales_nav_url, captured_at, expected_name)` rows. Provide
historical URLs of varying ages (older → newer). Sources, in order of
preference:

1. PB Sales Nav Inbox Scraper historical output CSVs (see
   `workflows/detect_responses.py:720` for the scrape mechanics — the inbox
   scrape emits `linkedin.com/sales/people/<id>` URLs that match the form).
2. Prior `/sales-daily` PB run artifacts (search PB dashboard).
3. Capture 5-10 URLs of varying ages manually from a live Sales Navigator
   browser session: open Sales Nav lead pages dated yesterday, last week, last
   month, last quarter; copy the URL bar; paste into the CSV.

The script launches the Sales Nav Profile Scraper (`PB_SALES_NAV_PROFILE_SCRAPER_ID`)
against the URLs using TODAY's cookies (`PB_LI_SALES_NAV_SESSION_COOKIE` +
`PB_LI_SALES_NAV_LI_A_COOKIE`), then for each input row records:

- `degree_resolved` — did the scraper return a non-empty degree cell?
- `name_match`     — does the scraper's returned full name match the input
                     `expected_name` (fuzzy, ratio ≥ 0.85 OR exact first+last
                     token match)?
- `csv_row_present`— did the scraper produce a CSV row at all for this URL?

The verdict is computed from the age distribution of successes:
- All rows (any age) succeed on both `degree_resolved` AND `name_match` →
  **`durable`**
- Rows below a clear age threshold succeed, rows above do not →
  **`ephemeral_after_N_days`** (where N is the breakpoint)
- Mixed/unpredictable → **`inconclusive`**

Output: `~/.outbound-agent/probe/sales-nav-authtoken-durability-YYYY-MM-DD.json`
containing the verdict, per-row results, and the input it ran against.
A human-readable summary is also printed to stdout.

## Usage

    python3 scripts/probe_sales_nav_authtoken_durability.py --csv urls.csv
    python3 scripts/probe_sales_nav_authtoken_durability.py --csv urls.csv --dry-run
    python3 scripts/probe_sales_nav_authtoken_durability.py --csv urls.csv --limit 5

CSV columns (header row required):

    sales_nav_url,captured_at,expected_name
    "https://linkedin.com/sales/lead/123,abc",2026-04-15,Maria González
    "https://linkedin.com/sales/lead/456,def",2026-05-20,John Smith

Sales Navigator URLs contain a comma between `<id>` and `<authToken>`, so the
`sales_nav_url` column MUST be quoted with double quotes. Python's `csv` module
treats unquoted commas as delimiters and will silently mis-split the row.

`captured_at` is ISO-8601 (YYYY-MM-DD). `expected_name` is the full name
LinkedIn displayed for that profile at capture time (used for the name-match
sanity check — closes adversarial finding F4 risk for the probe path too).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from difflib import SequenceMatcher
from pathlib import Path

# Ensure project-root imports work when run as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

from clients.pb_envelope import PBRunFailed, PBRunTimeout  # noqa: E402
from clients.phantombuster import PhantomBusterClient  # noqa: E402

PROBE_DIR = Path.home() / ".outbound-agent" / "probe"
NAME_MATCH_THRESHOLD = 0.85

# Age breakpoints (in days) the verdict logic considers when classifying
# ephemeral-vs-durable. If all rows older than the largest breakpoint fail
# AND all rows newer than the smallest succeed, the verdict is
# `ephemeral_after_N_days` with N at the failing breakpoint.
AGE_BREAKPOINTS_DAYS = (7, 14, 30, 60)


@dataclass(frozen=True)
class InputRow:
    sales_nav_url: str
    captured_at: date
    expected_name: str


@dataclass
class ProbeResult:
    input: InputRow
    age_days: int
    degree_resolved: bool = False
    name_match: bool = False
    csv_row_present: bool = False
    observed_degree: str = ""
    observed_name: str = ""
    name_match_ratio: float = 0.0
    error: str = ""


@dataclass
class ProbeReport:
    run_at: str
    scraper_id: str
    input_rows: int
    results: list[ProbeResult] = field(default_factory=list)
    verdict: str = "inconclusive"
    verdict_detail: str = ""
    container_id: str = ""


# --- Input loading ---


def load_input_csv(path: Path, limit: int | None) -> list[InputRow]:
    """Load the (sales_nav_url, captured_at, expected_name) input CSV."""
    rows: list[InputRow] = []
    with path.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        required = {"sales_nav_url", "captured_at", "expected_name"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"input CSV {path} missing required columns: {sorted(missing)}"
            )
        for row in reader:
            url = (row.get("sales_nav_url") or "").strip()
            captured = (row.get("captured_at") or "").strip()
            name = (row.get("expected_name") or "").strip()
            if not url or not captured or not name:
                continue
            # Sales Nav URLs embed a comma between <id> and <authToken>. If
            # the user forgot to quote the URL column, the URL we got here is
            # only the prefix. Detect this and fail loud — the wrong URL
            # would just waste a PB launch.
            if "/sales/lead/" in url and "," not in url:
                raise ValueError(
                    f"row {len(rows) + 1}: sales_nav_url looks truncated — "
                    f"got '{url}' with no comma. Sales Nav URLs always contain "
                    f'an authToken after a comma. Quote the URL column with "..." '
                    f"in the input CSV."
                )
            try:
                captured_date = date.fromisoformat(captured)
            except ValueError as exc:
                raise ValueError(
                    f"row {len(rows) + 1}: invalid captured_at '{captured}' "
                    f"(expected YYYY-MM-DD): {exc}"
                ) from exc
            rows.append(
                InputRow(
                    sales_nav_url=url,
                    captured_at=captured_date,
                    expected_name=name,
                )
            )
            if limit and len(rows) >= limit:
                break
    return rows


# --- Name matching ---


def name_match_ratio(a: str, b: str) -> float:
    """Lowercase + token-set ratio. Used for the fuzzy name-match check.

    Returns 1.0 if first+last tokens are exact match (order-insensitive),
    else SequenceMatcher ratio on the lowercased full strings.
    """
    if not a or not b:
        return 0.0
    a_tokens = set(re.findall(r"\w+", a.lower()))
    b_tokens = set(re.findall(r"\w+", b.lower()))
    # First+last exact-token match dominates: handles "Maria González" vs
    # "Maria E. González" (extra middle initial). The parens fence the
    # subset-OR so empty-token edge cases don't short-circuit past the
    # cardinality check (style-review nit from PR-B.9 battery).
    if a_tokens and b_tokens and (a_tokens <= b_tokens or b_tokens <= a_tokens):
        if len(a_tokens & b_tokens) >= 2:
            return 1.0
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def name_is_match(a: str, b: str) -> tuple[bool, float]:
    """Return (is_match, ratio)."""
    ratio = name_match_ratio(a, b)
    return (ratio >= NAME_MATCH_THRESHOLD, ratio)


# --- PB launch + parsing ---


def _pb_sales_nav_session_args() -> dict:
    """Sales Nav session args. Inline here (probe-only) — the production
    helper lives in workflows/daily_check_helpers.py and is added by PR-B.2.

    Raises if either Sales Nav cookie is missing — fail-loud is correct for
    a probe.
    """
    li_at = os.environ.get("PB_LI_SALES_NAV_SESSION_COOKIE", "").strip()
    li_a = os.environ.get("PB_LI_SALES_NAV_LI_A_COOKIE", "").strip()
    ua = os.environ.get("PB_LI_USER_AGENT", "").strip()
    if not li_at:
        raise SystemExit(
            "ERROR: PB_LI_SALES_NAV_SESSION_COOKIE not set. "
            "Capture li_at from a logged-in Sales Nav browser session "
            "(DevTools → Application → Cookies → linkedin.com) and add to .env. "
            "See docs/runbooks/phantombuster-cookie-rotation.md."
        )
    if not li_a:
        raise SystemExit(
            "ERROR: PB_LI_SALES_NAV_LI_A_COOKIE not set. "
            "Capture li_a from the same Sales Nav DevTools view as li_at."
        )
    args: dict = {"sessionCookie": li_at, "li_a": li_a}
    if ua:
        args["userAgent"] = ua
    return args


def launch_probe_scrape(
    pb: PhantomBusterClient,
    scraper_id: str,
    urls: list[str],
) -> tuple[str, str]:
    """Launch the Sales Nav Profile Scraper against `urls`.

    Returns `(container_id, csv_text)`. Raises `PBRunFailed` /
    `PBRunTimeout` on failure — caller decides how to report.

    Argument shape: the Sales Nav Profile Scraper accepts a `profileUrls`
    array OR a `spreadsheetUrl`. The probe uses `profileUrls` for simplicity
    (no Google Sheets setup needed). If PB rejects this shape with a 400 on
    your specific phantom build, swap to `spreadsheetUrl` using the
    `write_prospects_to_sheet()` helper in `workflows/daily_check.py:331`.
    Document which argument shape your phantom expects in
    `scripts/probe_pb_phantom_contracts.py` output (PR-A.A3).
    """
    args = {
        "profileUrls": urls,
        **_pb_sales_nav_session_args(),
    }
    launch = pb.launch_agent(scraper_id, args)
    pb.wait_for_completion(launch, poll_interval=10, max_wait=300)
    csv_text = pb.download_result_csv(launch) or ""
    return launch.container_id, csv_text


def parse_scrape_csv(csv_text: str) -> list[dict]:
    """Parse the Sales Nav Profile Scraper output CSV into list of dicts."""
    if not csv_text:
        return []
    return list(csv.DictReader(io.StringIO(csv_text)))


def find_csv_row_for_url(rows: list[dict], target_url: str) -> dict | None:
    """Locate the CSV row corresponding to a given input Sales Nav URL.

    Sales Nav scraper outputs typically echo the input URL under one of:
    `profileUrl`, `linkedinProfileUrl`, `query`, `defaultProfileUrl`,
    `salesNavigatorUrl`. Try each in order.
    """
    target_lower = target_url.strip().lower()
    candidate_cols = (
        "profileUrl",
        "linkedinProfileUrl",
        "query",
        "defaultProfileUrl",
        "salesNavigatorUrl",
        "input",
    )
    for row in rows:
        for col in candidate_cols:
            value = (row.get(col) or "").strip().lower()
            if value and value == target_lower:
                return row
    return None


def extract_degree(row: dict) -> str:
    """Return the degree cell from a scraper output row, tolerating column
    name variation."""
    for col in ("degree", "connectionDegree", "networkDistance"):
        value = (row.get(col) or "").strip()
        if value:
            return value
    return ""


def extract_full_name(row: dict) -> str:
    """Return the full name from a scraper output row, tolerating column
    name variation."""
    for col in ("fullName", "name", "displayName"):
        value = (row.get(col) or "").strip()
        if value:
            return value
    first = (row.get("firstName") or "").strip()
    last = (row.get("lastName") or "").strip()
    return f"{first} {last}".strip()


# --- Verdict ---


def compute_verdict(results: list[ProbeResult]) -> tuple[str, str]:
    """Classify the run as durable | ephemeral_after_N_days | inconclusive."""
    if not results:
        return "inconclusive", "no results"

    # A row "succeeds" if it returned the right person's degree.
    def succeeded(r: ProbeResult) -> bool:
        return r.degree_resolved and r.name_match

    failure_count = sum(1 for r in results if not succeeded(r))
    success_count = len(results) - failure_count

    if failure_count == 0:
        max_age = max(r.age_days for r in results)
        return (
            "durable",
            f"all {success_count} URLs resolved correctly (oldest tested: {max_age}d)",
        )

    if success_count == 0:
        return (
            "ephemeral_after_N_days",
            f"all {failure_count} URLs failed regardless of age — likely "
            "either cookies/phantom-id is wrong OR the URL form is always "
            "ephemeral. Inspect per-row errors before trusting this verdict.",
        )

    # Look for a clean age breakpoint. For each candidate, check whether all
    # younger rows succeed and all older rows fail.
    sorted_results = sorted(results, key=lambda r: r.age_days)
    for breakpoint in AGE_BREAKPOINTS_DAYS:
        younger = [r for r in sorted_results if r.age_days <= breakpoint]
        older = [r for r in sorted_results if r.age_days > breakpoint]
        if not younger or not older:
            continue
        if all(succeeded(r) for r in younger) and all(
            not succeeded(r) for r in older
        ):
            return (
                f"ephemeral_after_{breakpoint}_days",
                f"clean breakpoint at {breakpoint}d — {len(younger)} younger "
                f"URLs all resolved; {len(older)} older URLs all failed",
            )

    return (
        "inconclusive",
        f"mixed results without a clean age breakpoint "
        f"({success_count}/{len(results)} succeeded). "
        f"Default to ephemeral schema (safer). "
        f"Run with more URLs spanning more ages to disambiguate.",
    )


# --- Main ---


def write_report(report: ProbeReport, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_at": report.run_at,
        "scraper_id": report.scraper_id,
        "container_id": report.container_id,
        "input_rows": report.input_rows,
        "verdict": report.verdict,
        "verdict_detail": report.verdict_detail,
        "name_match_threshold": NAME_MATCH_THRESHOLD,
        "age_breakpoints_days": list(AGE_BREAKPOINTS_DAYS),
        "results": [
            {
                "sales_nav_url": r.input.sales_nav_url,
                "captured_at": r.input.captured_at.isoformat(),
                "expected_name": r.input.expected_name,
                "age_days": r.age_days,
                "degree_resolved": r.degree_resolved,
                "name_match": r.name_match,
                "csv_row_present": r.csv_row_present,
                "observed_degree": r.observed_degree,
                "observed_name": r.observed_name,
                "name_match_ratio": round(r.name_match_ratio, 3),
                "error": r.error,
            }
            for r in report.results
        ],
    }
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=False))


def print_summary(report: ProbeReport, output_path: Path) -> None:
    print("\n=== Sales Nav authToken durability probe ===")
    print(f"Run at:        {report.run_at}")
    print(f"Scraper ID:    {report.scraper_id}")
    print(f"Container ID:  {report.container_id or '<dry-run>'}")
    print(f"Input rows:    {report.input_rows}")
    print(f"\nVERDICT: {report.verdict}")
    print(f"  → {report.verdict_detail}")
    print("\nPer-row results:")
    for r in report.results:
        flag = "✓" if r.degree_resolved and r.name_match else "✗"
        print(
            f"  {flag} age={r.age_days:>4}d  degree={r.observed_degree or '-':<5}  "
            f"name_match={r.name_match} (ratio={r.name_match_ratio:.2f})  "
            f"{r.input.expected_name[:30]}"
        )
        if r.error:
            print(f"      ERROR: {r.error}")
    print(f"\nWrote: {output_path}")
    print("\nSchema decision:")
    if report.verdict == "durable":
        print("  → PR-A.A4 schema: single `sales_nav_url` text attribute.")
    elif report.verdict.startswith("ephemeral_after_"):
        print(
            "  → PR-A.A4 schema: `sales_nav_url` + `sales_nav_url_captured_at` "
            "timestamp + weekly refresh in /sales-nav-backfill skill."
        )
    else:
        print(
            "  → PR-A.A4 schema: DEFAULT to ephemeral shape (safer). "
            "Re-run with more diverse-age URLs to disambiguate."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--csv",
        type=Path,
        required=True,
        help="Path to input CSV (columns: sales_nav_url, captured_at, expected_name)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap input rows (debug)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate input + env, but skip PB launch",
    )
    args = parser.parse_args(argv)

    load_dotenv()
    scraper_id = os.environ.get("PB_SALES_NAV_PROFILE_SCRAPER_ID", "").strip()
    if not scraper_id:
        print(
            "ERROR: PB_SALES_NAV_PROFILE_SCRAPER_ID not set in .env.\n"
            "Look up the phantom ID in the PB dashboard.",
            file=sys.stderr,
        )
        return 1

    try:
        input_rows = load_input_csv(args.csv, args.limit)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR loading input CSV: {exc}", file=sys.stderr)
        return 1
    if not input_rows:
        print("ERROR: input CSV produced zero usable rows.", file=sys.stderr)
        return 1

    today = date.today()
    results: list[ProbeResult] = [
        ProbeResult(input=r, age_days=(today - r.captured_at).days)
        for r in input_rows
    ]
    report = ProbeReport(
        run_at=datetime.now(UTC).isoformat(),
        scraper_id=scraper_id,
        input_rows=len(results),
        results=results,
    )

    if args.dry_run:
        report.verdict = "inconclusive"
        report.verdict_detail = "dry-run: no PB launch issued"
    else:
        # Validate cookies before the wet launch — fail loud, do not waste a
        # PB run on a malformed config.
        _pb_sales_nav_session_args()
        urls = [r.input.sales_nav_url for r in results]
        with PhantomBusterClient() as pb:
            try:
                container_id, csv_text = launch_probe_scrape(
                    pb, scraper_id, urls
                )
                report.container_id = container_id
            except (PBRunFailed, PBRunTimeout) as exc:
                report.verdict = "inconclusive"
                report.verdict_detail = f"PB launch failed: {type(exc).__name__}: {exc}"
                for r in results:
                    r.error = f"{type(exc).__name__}"
                output_path = _output_path(today)
                write_report(report, output_path)
                print_summary(report, output_path)
                return 2

        scraper_rows = parse_scrape_csv(csv_text)
        for r in results:
            csv_row = find_csv_row_for_url(scraper_rows, r.input.sales_nav_url)
            if csv_row is None:
                r.error = "no matching CSV row"
                continue
            r.csv_row_present = True
            r.observed_degree = extract_degree(csv_row)
            r.observed_name = extract_full_name(csv_row)
            r.degree_resolved = bool(r.observed_degree)
            r.name_match, r.name_match_ratio = name_is_match(
                r.input.expected_name, r.observed_name
            )

        verdict, detail = compute_verdict(results)
        report.verdict = verdict
        report.verdict_detail = detail

    output_path = _output_path(today)
    write_report(report, output_path)
    print_summary(report, output_path)
    return 0


def _output_path(today: date) -> Path:
    return PROBE_DIR / f"sales-nav-authtoken-durability-{today.isoformat()}.json"


if __name__ == "__main__":
    sys.exit(main())
