"""qualify_borderline.py — LLM-qualify borderline prospects from a scored JSON.

Uses the `claude` CLI subprocess (Haiku 4.5) to run parallel qualification
calls against all borderline prospects produced by score_referral_csv.py.
Auth is handled by the claude CLI itself (macOS Keychain) — no ANTHROPIC_API_KEY needed.

Subprocess safety: uses asyncio.create_subprocess_exec (not shell=True) so args
are passed as a list — no shell injection possible.

Usage:
    python scripts/qualify_borderline.py \\
        --scored exports/referrals-scored.json \\
        --output exports/referrals-qualified.json \\
        [--concurrency 20] \\
        [--max-runtime-min 30]
"""

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

# ── Bootstrap ─────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent

# ── Constants ─────────────────────────────────────────────────────────────────

CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude"
MODEL = "claude-haiku-4-5-20251001"

# Approximate token estimates for cost projection (informational only — no API billing from CLI)
EST_INPUT_TOKENS_PER_CALL = 600   # system + user prompt
EST_OUTPUT_TOKENS_PER_CALL = 80   # JSON response

# Haiku 4.5 pricing (per million tokens) — for reference estimate only
INPUT_PRICE_PER_MTOK = 1.00
OUTPUT_PRICE_PER_MTOK = 5.00

# ICP lane persona groupings (mirrors score_referral_csv.py)
ICP1_PERSONA_NAMES = {
    "digitalization_champions",
    "operations_leaders",
    "executive_sponsors",
}
ICP2_PERSONA_NAMES = {
    "mx_midmarket_manufacturing",
    "co_midmarket_manufacturing",
    "cl_midmarket_manufacturing",
}

# ── System prompt ─────────────────────────────────────────────────────────────

def _load_system_prompt() -> str:
    """Load the qualifier system prompt from /tmp/qualifier_system_prompt.txt."""
    prompt_path = Path("/tmp/qualifier_system_prompt.txt")
    if prompt_path.exists():
        return prompt_path.read_text(encoding="utf-8").strip()
    raise FileNotFoundError(
        "Qualifier system prompt not found at /tmp/qualifier_system_prompt.txt. "
        "Please ensure the file exists before running qualification."
    )


# ── Persona mode derivation ───────────────────────────────────────────────────

def _derive_persona_mode(best_persona: str) -> str:
    """Derive the persona_mode string from the best_persona label."""
    if best_persona in ICP1_PERSONA_NAMES:
        return "enterprise"
    if best_persona in ICP2_PERSONA_NAMES:
        return "midmarket"
    return "legacy"


# ── User message builder ──────────────────────────────────────────────────────

def _build_user_message(prospect: dict) -> str:
    """Build the user message content for a single prospect."""
    best_persona = prospect.get("best_persona", "")
    mode = _derive_persona_mode(best_persona)
    employee_count = prospect.get("employee_count", "") or "unknown"

    return (
        f"PROSPECT:\n"
        f"- Name: {prospect.get('name', '')}\n"
        f"- Title: {prospect.get('title', '')}\n"
        f"- Company: {prospect.get('company', '')}\n"
        f"- Location: {prospect.get('location', '')}\n"
        f"- Employee count: {employee_count}\n"
        f"- Persona mode: {mode}\n"
    )


# ── Cost estimation ───────────────────────────────────────────────────────────

def _estimate_cost(n: int) -> float:
    """Estimate total USD cost for n Haiku calls (approximate — no API billing visible from CLI).

    CLI overhead adds ~500ms per call. Total wall time approx n / concurrency * 2s.
    """
    input_cost = n * EST_INPUT_TOKENS_PER_CALL * (INPUT_PRICE_PER_MTOK / 1_000_000)
    output_cost = n * EST_OUTPUT_TOKENS_PER_CALL * (OUTPUT_PRICE_PER_MTOK / 1_000_000)
    return input_cost + output_cost


# ── Response parser ───────────────────────────────────────────────────────────

_JSON_BLOCK_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)


def _parse_qualifier_response(raw_text: str) -> dict:
    """Parse the Haiku JSON response: {pass: bool, icp_lane: 1|2, rationale: str}.

    The claude CLI wraps responses in markdown code fences (```json ... ```).
    We use regex to extract the first {...} JSON block from stdout regardless
    of whether it's bare JSON or inside code fences.

    Returns a dict with those keys. On parse failure, returns a parse_error record.
    """
    text = raw_text.strip()

    # Strategy 1: find first {...} JSON block via regex (handles code fences transparently)
    match = _JSON_BLOCK_RE.search(text)
    if match:
        try:
            parsed = json.loads(match.group(0))
            return {
                "pass": bool(parsed.get("pass", False)),
                "icp_lane": int(parsed.get("icp_lane", 1)) if parsed.get("icp_lane") is not None else 1,
                "rationale": str(parsed.get("rationale", "")),
            }
        except (json.JSONDecodeError, ValueError):
            pass

    # Strategy 2: strip markdown fences and try full parse
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(text)
        return {
            "pass": bool(parsed.get("pass", False)),
            "icp_lane": int(parsed.get("icp_lane", 1)) if parsed.get("icp_lane") is not None else 1,
            "rationale": str(parsed.get("rationale", "")),
        }
    except (json.JSONDecodeError, KeyError, ValueError):
        return {
            "pass": False,
            "icp_lane": 1,
            "rationale": "parse_error",
        }


# ── Single call with retry ────────────────────────────────────────────────────

async def _qualify_one(
    prospect: dict,
    system_prompt: str,
    semaphore: asyncio.Semaphore,
    start_time: float,
    max_runtime_sec: float,
) -> dict:
    """Qualify a single prospect via claude CLI subprocess.

    Returns the prospect dict with qualifier_result merged.
    Retries once on transient errors (non-zero exit, empty stdout).
    Uses asyncio.create_subprocess_exec (not shell=True) — args passed as list,
    no shell injection possible.
    """
    # Prefer pre-built qualification_prompt from JSONL; fall back to rebuilding from prospect_data
    qp = prospect.get("qualification_prompt") or {}
    if isinstance(qp, dict) and qp.get("user"):
        user_msg = qp["user"]
        prompt_system = qp.get("system") or system_prompt
    else:
        flat = {**prospect.get("prospect_data", {}), "best_persona": prospect.get("persona", "")}
        user_msg = _build_user_message(flat)
        prompt_system = system_prompt

    async def _call() -> str:
        if time.monotonic() - start_time > max_runtime_sec:
            raise TimeoutError("max-runtime-min exceeded")

        proc = await asyncio.create_subprocess_exec(
            CLAUDE_BIN,
            "-p", user_msg,
            "--model", MODEL,
            "--append-system-prompt", prompt_system,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"claude exited {proc.returncode}: {stderr[:200]}")
        if not stdout:
            raise RuntimeError("claude returned empty stdout")
        return stdout

    async with semaphore:
        try:
            raw = await _call()
            qualifier_result = _parse_qualifier_response(raw)
        except TimeoutError:
            qualifier_result = {
                "pass": False,
                "icp_lane": 1,
                "rationale": "api_error: max_runtime_exceeded",
            }
        except RuntimeError:
            # Retry once after a short backoff
            await asyncio.sleep(2)
            try:
                raw = await _call()
                qualifier_result = _parse_qualifier_response(raw)
            except Exception as retry_exc:
                qualifier_result = {
                    "pass": False,
                    "icp_lane": 1,
                    "rationale": f"api_error: {retry_exc}",
                }
        except Exception as exc:
            qualifier_result = {
                "pass": False,
                "icp_lane": 1,
                "rationale": f"api_error: {exc}",
            }

    return {**prospect, "qualifier_result": qualifier_result}


# ── Parallel runner ───────────────────────────────────────────────────────────

async def _qualify_all(
    prospects: list[dict],
    system_prompt: str,
    concurrency: int,
    max_runtime_sec: float,
) -> list[dict]:
    """Run all qualification calls in parallel, respecting concurrency limit."""
    semaphore = asyncio.Semaphore(concurrency)
    total = len(prospects)
    start_time = time.monotonic()

    results: list[dict | None] = [None] * total
    rejected_so_far = 0

    tasks = [
        asyncio.create_task(
            _qualify_one(prospect, system_prompt, semaphore, start_time, max_runtime_sec)
        )
        for prospect in prospects
    ]

    for i, future in enumerate(asyncio.as_completed(tasks), start=1):
        result = await future
        results[i - 1] = result

        if not result["qualifier_result"]["pass"]:
            rejected_so_far += 1

        if i % 25 == 0 or i == total:
            print(
                f"  Qualified {i}/{total} "
                f"({rejected_so_far} rejected so far)"
            )

    return [r for r in results if r is not None]


# ── Output writer ─────────────────────────────────────────────────────────────

def _write_output(
    results: list[dict],
    scored_input_path: str,
    output_path: str,
) -> None:
    """Write verdicts as JSONL — one {linkedin_url, pass, rationale, icp_lane} per line.

    weekly-finalize reads this file line-by-line and looks up verdicts by linkedin_url.
    """
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for r in results:
            qr = r.get("qualifier_result", {})
            f.write(json.dumps({
                "linkedin_url": r.get("linkedin_url", ""),
                "pass": qr.get("pass", False),
                "rationale": qr.get("rationale", ""),
                "icp_lane": qr.get("icp_lane", 1),
            }) + "\n")


# ── Summary printer ───────────────────────────────────────────────────────────

def _print_summary(results: list[dict], output_path: str) -> None:
    """Print a post-run summary to stdout."""
    qualified = [r for r in results if r["qualifier_result"]["pass"]]
    rejected = [r for r in results if not r["qualifier_result"]["pass"]]
    total = len(results)

    lane_counter: Counter = Counter()
    for r in qualified:
        lane = r["qualifier_result"].get("icp_lane", "?")
        lane_counter[f"ICP {lane}"] += 1

    location_counter: Counter = Counter()
    for r in qualified:
        loc = r.get("location", "") or "Unknown"
        country_hint = loc.split(",")[-1].strip() if "," in loc else loc.strip()
        location_counter[country_hint or "Unknown"] += 1

    print(f"\n{'=' * 60}")
    print("  Outbound Agent — Qualification Summary")
    print(f"{'=' * 60}")
    print(f"  Output file    : {output_path}")
    print(f"{'─' * 60}")
    print(f"  Total processed : {total}")
    print(f"  Qualified       : {len(qualified)}")
    print(f"  Rejected        : {len(rejected)}")
    print(f"{'─' * 60}")
    if lane_counter:
        print("  ICP lane breakdown (qualified):")
        for lane, count in sorted(lane_counter.items()):
            print(f"    {lane:<10} : {count}")
    if location_counter:
        print("  Location breakdown (qualified):")
        for loc, count in sorted(location_counter.items(), key=lambda x: -x[1])[:15]:
            print(f"    {loc:<25} : {count}")
    print(f"{'=' * 60}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Qualify borderline prospects from a scored JSON using Anthropic Haiku 4.5 "
            "via the `claude` CLI subprocess (no ANTHROPIC_API_KEY required)."
        )
    )
    parser.add_argument(
        "--scored",
        required=True,
        help="Path to scored JSON produced by score_referral_csv.py "
             "(e.g. exports/referrals-scored.json)",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Path for qualified JSON output (e.g. exports/referrals-qualified.json)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=20,
        help="Max parallel claude CLI subprocesses (default: 20)",
    )
    parser.add_argument(
        "--max-runtime-min",
        type=float,
        default=30.0,
        dest="max_runtime_min",
        help="Abort if total wall time exceeds this many minutes (default: 30)",
    )
    args = parser.parse_args()

    max_runtime_sec = args.max_runtime_min * 60

    # ── Load scored JSON ──────────────────────────────────────────────────────
    scored_path = Path(args.scored)
    if not scored_path.exists():
        print(f"[ERROR] Scored JSON not found: {scored_path}", file=sys.stderr)
        sys.exit(1)

    with open(scored_path, encoding="utf-8") as f:
        lines = [ln.strip() for ln in f if ln.strip()]

    # Support both JSONL (one object per line) and legacy {borderline: [...]} format
    if len(lines) == 1:
        scored_data = json.loads(lines[0])
        borderline: list[dict] = scored_data.get("borderline", [scored_data] if "linkedin_url" in scored_data else [])
    else:
        borderline = [json.loads(ln) for ln in lines]

    n = len(borderline)

    if n == 0:
        print("[INFO] No borderline prospects found in scored JSON. Nothing to qualify.")
        sys.exit(0)

    print(f"\n[INFO] Loaded {n} borderline prospects from {scored_path}")

    # ── Cost estimate (approximate — no API billing visible from CLI) ─────────
    estimated_cost = _estimate_cost(n)
    est_wall_time_min = n / args.concurrency * 2 / 60
    print(f"[INFO] Approximate cost estimate: ${estimated_cost:.4f} USD")
    print(f"       ({n} calls x ~{EST_INPUT_TOKENS_PER_CALL} input tokens x "
          f"${INPUT_PRICE_PER_MTOK}/MTok + "
          f"~{EST_OUTPUT_TOKENS_PER_CALL} output tokens x "
          f"${OUTPUT_PRICE_PER_MTOK}/MTok)")
    print(f"       (CLI overhead ~adds 500ms per call; "
          f"total wall time approx {n} / {args.concurrency} x 2s = {est_wall_time_min:.1f} min)")
    print("       (Actual API cost may be lower; billing not visible from CLI)")

    # ── Load system prompt ────────────────────────────────────────────────────
    try:
        system_prompt = _load_system_prompt()
    except FileNotFoundError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    # ── Run qualification ─────────────────────────────────────────────────────
    print(f"\n[INFO] Qualifying {n} prospects "
          f"(concurrency={args.concurrency}, max-runtime={args.max_runtime_min}min)...\n")
    start_time = time.monotonic()

    results = asyncio.run(
        _qualify_all(
            prospects=borderline,
            system_prompt=system_prompt,
            concurrency=args.concurrency,
            max_runtime_sec=max_runtime_sec,
        )
    )

    elapsed = time.monotonic() - start_time
    print(f"\n[INFO] Completed in {elapsed:.1f}s")

    # ── Write output ──────────────────────────────────────────────────────────
    output_path = str(Path(args.output).resolve())
    _write_output(results, str(scored_path.resolve()), output_path)
    print(f"[INFO] Wrote output to {output_path}")

    # ── Print summary ─────────────────────────────────────────────────────────
    _print_summary(results, output_path)


if __name__ == "__main__":
    main()
