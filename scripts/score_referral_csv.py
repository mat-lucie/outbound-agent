"""score_referral_csv.py — Score a LinkedIn People Search Export CSV through all 6 personas.

Usage:
    python scripts/score_referral_csv.py \
        --csv exports/acme-pb-export.csv \
        --output exports/acme-scored.json \
        [--exclude-companies "CompanyA,CompanyB"]

Notes on quality_gate.score_prospect return dict:
    Keys: score, pass, persona, language, reasons
    There is NO 'qualification_prompt' key in the current implementation.
    The spec requested it, but the function doesn't produce one.
    For borderline rows we store the 'reasons' list instead as a proxy for
    what a qualification_prompt would contain.

Notes on agent_gate:
    score_prospect accepts **_kwargs, so agent_gate=True is silently absorbed.
    It has no effect on scoring — the flag is future-reserved in this codebase.
"""

import argparse
import csv
import json
import os
import sys
from collections import Counter

# Add the repo root to sys.path so `from workflows.quality_gate import ...` works.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(REPO_ROOT, ".env"))

from workflows.quality_gate import score_prospect  # noqa: E402

# ── Constants ─────────────────────────────────────────────────────────────────

PERSONAS_PATH = os.path.join(REPO_ROOT, "content", "personas.json")

# ICP lane assignment: first 3 = ICP 1 (enterprise), last 3 = ICP 2 (mid-market)
ICP1_PERSONA_NAMES = [
    "digitalization_champions",
    "operations_leaders",
    "executive_sponsors",
]
ICP2_PERSONA_NAMES = [
    "mx_midmarket_manufacturing",
    "co_midmarket_manufacturing",
    "cl_midmarket_manufacturing",
]
ALL_PERSONA_NAMES = ICP1_PERSONA_NAMES + ICP2_PERSONA_NAMES

# Brazil signals: score only against ICP 1 personas
BRAZIL_SIGNALS = ["brazil", "brasil", " br "]

ACCEPT_THRESHOLD = 75
REJECT_THRESHOLD = 40


# ── Helpers ───────────────────────────────────────────────────────────────────


def _load_personas() -> dict:
    """Load personas.json and return as dict keyed by persona name."""
    with open(PERSONAS_PATH, encoding="utf-8") as f:
        return json.load(f)


def _build_prospect_data(raw: dict) -> dict:
    """Build prospect_data dict from a raw CSV row (LinkedIn Search Export schema).

    Key mappings for LinkedIn People Search Export (PhantomBuster):
      name         ← fullName  (no Sales Nav firstName+lastName split)
      title        ← headline OR jobTitle
      company      ← company
      location     ← location
      linkedin_url ← profileUrl OR linkedinProfileUrl
      employee_count ← "" (Search Export lacks this field)

    Multi-key fallbacks mirror weekly_prospect.py:159-167 exactly.
    """
    name = (
        raw.get("fullName")
        or raw.get("name")
        or (f"{raw.get('firstName', '')} {raw.get('lastName', '')}".strip())
    )
    title = raw.get("title") or raw.get("headline") or raw.get("currentPosition") or ""
    company = (
        raw.get("company")
        or raw.get("companyName")
        or raw.get("currentCompanyName")
        or ""
    )
    location = raw.get("location") or ""
    linkedin_url = (
        raw.get("profileUrl")
        or raw.get("linkedinProfileUrl")
        or raw.get("defaultProfileUrl")
        or raw.get("linkedInUrl")
        or ""
    )
    # Search Export lacks employee count — leave blank so the scorer uses
    # the "unknown company size" default branch (scores 10-22 depending on mode).
    employee_count = raw.get("companyEmployees") or raw.get("employeeCount") or raw.get("numberOfEmployees") or raw.get("companySize") or ""

    return {
        "name": name.strip(),
        "title": title.strip(),
        "company": company.strip(),
        "location": location.strip(),
        "linkedin_url": linkedin_url.strip(),
        "employee_count": employee_count,
    }


def _is_brazil(location: str) -> bool:
    """Return True if location suggests Brazil (BR-only rule: ICP 1 personas only)."""
    loc_lower = location.lower()
    return any(sig in loc_lower for sig in BRAZIL_SIGNALS)


def _company_excluded(company: str, exclude_set: list[str]) -> bool:
    """Case-insensitive substring match against any exclude entry."""
    company_lower = company.lower()
    return any(excl in company_lower for excl in exclude_set)


def _detect_country_language(location: str) -> str:
    """Best-effort country/language label from location string."""
    loc = location.lower()
    if any(s in loc for s in ["brazil", "brasil", "são paulo", "rio de janeiro", "belo horizonte"]):
        return "BR/PT"
    if any(s in loc for s in ["mexico", "méxico", "guadalajara", "monterrey", "cdmx"]):
        return "MX/ES"
    if any(s in loc for s in ["colombia", "bogotá", "bogota", "medellín", "medellin", "cali"]):
        return "CO/ES"
    if any(s in loc for s in ["chile", "santiago", "valparaíso"]):
        return "CL/ES"
    if any(s in loc for s in ["argentina", "buenos aires"]):
        return "AR/ES"
    if any(s in loc for s in ["peru", "perú", "lima"]):
        return "PE/ES"
    if any(s in loc for s in ["spain", "españa", "madrid", "barcelona"]):
        return "ES/ES"
    if any(s in loc for s in ["united states", "usa", "new york", "chicago", "houston"]):
        return "US/EN"
    if not location.strip():
        return "Unknown"
    return "Other"


# ── Core scoring logic ────────────────────────────────────────────────────────


def score_row(prospect_data: dict, personas: dict) -> dict:
    """Score a single prospect against all applicable personas.

    Returns a dict with:
        persona_scores   {persona_name: score}
        best_persona     str
        best_score       int
        best_icp_lane    "ICP 1" | "ICP 2"
        language         str (from best-persona result)
        reasons          list[str] (from best-persona result)
    """
    location = prospect_data.get("location", "")
    brazil = _is_brazil(location)

    # Brazil: restrict to ICP 1 only
    active_persona_names = ICP1_PERSONA_NAMES if brazil else ALL_PERSONA_NAMES

    persona_scores: dict[str, int] = {}
    persona_results: dict[str, dict] = {}

    for pname in active_persona_names:
        persona_cfg = personas.get(pname)
        if persona_cfg is None:
            continue
        result = score_prospect(
            prospect_data,
            persona_config=persona_cfg,
            agent_gate=True,  # absorbed by **_kwargs — future-reserved
        )
        persona_scores[pname] = result["score"]
        persona_results[pname] = result

    # Pick best
    best_persona = max(persona_scores, key=lambda p: persona_scores[p])
    best_score = persona_scores[best_persona]
    best_result = persona_results[best_persona]
    best_icp_lane = "ICP 1" if best_persona in ICP1_PERSONA_NAMES else "ICP 2"

    return {
        "persona_scores": persona_scores,
        "best_persona": best_persona,
        "best_score": best_score,
        "best_icp_lane": best_icp_lane,
        "language": best_result.get("language", ""),
        "reasons": best_result.get("reasons", []),
    }


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Score a LinkedIn Search Export CSV through all configured personas."
    )
    parser.add_argument("--csv", required=True, help="Path to PB LinkedIn Search Export CSV")
    parser.add_argument("--output", required=True, help="Path for scored JSON output")
    parser.add_argument(
        "--exclude-companies",
        default="",
        help="Comma-separated company substrings to exclude (case-insensitive)",
    )
    args = parser.parse_args()

    # Parse exclusion list
    exclude_companies: list[str] = []
    if args.exclude_companies.strip():
        exclude_companies = [e.strip().lower() for e in args.exclude_companies.split(",") if e.strip()]

    # Load personas
    personas = _load_personas()

    # Validate all 6 expected personas are present
    missing = [p for p in ALL_PERSONA_NAMES if p not in personas]
    if missing:
        print(f"[WARNING] personas.json is missing expected persona(s): {missing}", file=sys.stderr)

    # Read CSV
    csv_path = os.path.abspath(args.csv)
    if not os.path.exists(csv_path):
        print(f"[ERROR] CSV not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    total_rows = len(rows)
    skipped_empty = 0
    skipped_excluded = 0
    accepted: list[dict] = []
    borderline: list[dict] = []
    rejected_count = 0

    country_language_counter: Counter = Counter()

    for raw in rows:
        prospect_data = _build_prospect_data(raw)

        # Skip: missing name or linkedin_url
        if not prospect_data["name"] or not prospect_data["linkedin_url"]:
            skipped_empty += 1
            continue

        # Skip: excluded company
        if exclude_companies and _company_excluded(prospect_data["company"], exclude_companies):
            skipped_excluded += 1
            continue

        # Score
        scoring = score_row(prospect_data, personas)
        best_score = scoring["best_score"]

        # Track country/language breakdown
        country_lang = _detect_country_language(prospect_data.get("location", ""))
        country_language_counter[country_lang] += 1

        # Build output record
        record = {
            **prospect_data,
            "persona_scores": scoring["persona_scores"],
            "best_persona": scoring["best_persona"],
            "best_score": best_score,
            "best_icp_lane": scoring["best_icp_lane"],
            "language": scoring["language"],
        }

        if best_score > ACCEPT_THRESHOLD:
            accepted.append(record)
        elif best_score < REJECT_THRESHOLD:
            rejected_count += 1
            # Rejected rows are counted only, not stored in output (no value in bulk storage)
        else:
            # Borderline (40-75 inclusive)
            # NOTE: score_prospect does not return a 'qualification_prompt' key.
            # We store 'reasons' from the best persona as a proxy for qualification context.
            record["qualification_prompt"] = {
                "note": "score_prospect has no qualification_prompt key; reasons substituted",
                "reasons": scoring["reasons"],
                "best_persona": scoring["best_persona"],
                "best_score": best_score,
            }
            borderline.append(record)

    # Build output JSON
    output = {
        "csv_path": csv_path,
        "total_rows": total_rows,
        "skipped_empty": skipped_empty,
        "skipped_excluded": skipped_excluded,
        "rejected": rejected_count,
        "accepted": accepted,
        "borderline": borderline,
    }

    output_path = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # ── Summary to stdout ──────────────────────────────────────────────────────
    processed = total_rows - skipped_empty - skipped_excluded
    print(f"\n{'='*56}")
    print("  Outbound Agent — CSV Scoring Summary")
    print(f"{'='*56}")
    print(f"  Input CSV   : {csv_path}")
    print(f"  Output JSON : {output_path}")
    print(f"{'─'*56}")
    print(f"  Total rows       : {total_rows}")
    print(f"  Skipped (empty)  : {skipped_empty}")
    print(f"  Skipped (excl.)  : {skipped_excluded}")
    print(f"  Scored           : {processed}")
    print(f"{'─'*56}")
    print(f"  Accepted  (>75)  : {len(accepted)}")
    print(f"  Borderline(40-75): {len(borderline)}")
    print(f"  Rejected  (<40)  : {rejected_count}")
    print(f"{'─'*56}")
    if country_language_counter:
        print(f"  Location breakdown ({processed} scored rows):")
        for label, count in sorted(country_language_counter.items(), key=lambda x: -x[1]):
            print(f"    {label:<14} : {count}")
    print(f"{'='*56}\n")


if __name__ == "__main__":
    main()
