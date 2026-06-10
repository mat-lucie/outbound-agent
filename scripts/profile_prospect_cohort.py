"""Profile the PROSPECT-stage cohort in the sales pipeline.

Read-only. Prints a terminal-friendly report covering score histogram,
persona/language/industry breakdowns, pipeline age, and top-20 highest-scored
prospects.

Usage:
  python3 scripts/profile_prospect_cohort.py
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import click
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent))

from clients.attio import AttioClient  # noqa: E402

# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_float(val) -> float | None:
    """Coerce a quality_score value to float, or None."""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _score_bin(score: float | None) -> str:
    if score is None:
        return "null"
    if score < 40:
        return "<40"
    if score < 60:
        return "40–59"
    if score < 70:
        return "60–69"
    if score < 80:
        return "70–79"
    if score < 90:
        return "80–89"
    return "90+"


def _age_bucket(created_at: str | None) -> str:
    """Bucket entry age in days. Returns None if timestamp unavailable."""
    if not created_at:
        return None
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        now = datetime.now(UTC)
        days = (now - dt).days
        if days <= 7:
            return "0–7d"
        if days <= 30:
            return "8–30d"
        if days <= 90:
            return "31–90d"
        return "90d+"
    except (ValueError, TypeError):
        return None


def _pct(n: int, total: int) -> str:
    if total == 0:
        return "0.0%"
    return f"{n / total * 100:.1f}%"


def _bar(n: int, total: int, width: int = 20) -> str:
    filled = round(n / total * width) if total else 0
    return "█" * filled + "░" * (width - filled)


def _section(title: str) -> None:
    click.echo(f"\n{'─' * 60}")
    click.echo(f"  {title}")
    click.echo(f"{'─' * 60}")


# ── Main ──────────────────────────────────────────────────────────────────────

@click.command()
def main() -> None:
    """Profile PROSPECT-stage cohort in the pipeline."""
    list_id = os.environ["ATTIO_LIST_ID"]

    click.echo("═" * 60)
    click.echo("  PROSPECT COHORT PROFILE")
    click.echo(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    click.echo("═" * 60)

    with AttioClient() as attio:
        click.echo(f"\nFetching all list entries from {list_id}…")
        all_entries = attio.query_list_entries(list_id=list_id, limit=50000)
        click.echo(f"  Total entries in pipeline: {len(all_entries)}")

        # Filter to PROSPECT stage
        prospects = [e for e in all_entries if AttioClient.parse_entry(e).get("stage") == "Prospect"]
        total = len(prospects)
        click.echo(f"  PROSPECT entries: {total}")

        if total == 0:
            click.echo("\nNo PROSPECT entries found. Nothing to profile.")
            return

        # Parse all entries
        parsed_entries: list[dict] = []
        for entry in prospects:
            p = AttioClient.parse_entry(entry)
            p["_raw_created_at"] = entry.get("created_at") or entry.get("id", {}).get("created_at")
            parsed_entries.append(p)

        # Collect record IDs to resolve names/companies
        record_ids = [p["record_id"] for p in parsed_entries if p.get("record_id")]
        click.echo(f"\nResolving {len(record_ids)} person records (may take a moment)…")

        record_info: dict[str, tuple[str, str, str, str]] = {}
        for rec_id in record_ids:
            if not rec_id:
                continue
            try:
                person = attio.get_person(rec_id)
                if person:
                    record_info[rec_id] = attio.extract_record_info(person)
                else:
                    # PR-14 (B-PD-006): align with the new RecordInfo
                    # contract — None for missing name/company/industry,
                    # "" for linkedin_url and title.
                    record_info[rec_id] = (None, None, "", None, "")
            except Exception:
                record_info[rec_id] = (None, None, "", None, "")

        click.echo(f"  Resolved {len(record_info)} records.")

        # ── Aggregate data ────────────────────────────────────────────────────
        score_bins: Counter = Counter()
        scores_valid: list[float] = []
        persona_counts: Counter = Counter()
        language_counts: Counter = Counter()
        industry_counts: Counter = Counter()
        age_counts: Counter = Counter()
        age_available = False
        null_score_count = 0
        null_persona_count = 0
        null_language_count = 0

        enriched: list[dict] = []

        for p in parsed_entries:
            score_raw = p.get("quality_score")
            score = _safe_float(score_raw)
            persona = p.get("persona") or "null"
            language = p.get("language") or "null"
            rec_id = p.get("record_id", "")
            # PR-14: tuple has 5 elements now (name, company, linkedin,
            # industry, title) — pre-PR-14 destructure missed `title`.
            # Industry stays "Unknown" in this analytics script when
            # unset (analytics-only display string, not a contract).
            name, company, _, industry, _ = record_info.get(
                rec_id, (None, None, "", None, ""),
            )
            industry = industry or "Unknown"
            created_at = p.get("_raw_created_at")

            bin_ = _score_bin(score)
            score_bins[bin_] += 1
            if score is not None:
                scores_valid.append(score)
            else:
                null_score_count += 1

            if persona == "null":
                null_persona_count += 1
            persona_counts[persona] += 1

            if language == "null":
                null_language_count += 1
            language_counts[language] += 1

            industry_counts[industry] += 1

            age_bucket = _age_bucket(created_at)
            if age_bucket:
                age_available = True
                age_counts[age_bucket] += 1
            elif created_at is None:
                age_counts["no_timestamp"] += 1

            enriched.append({
                "name": name,
                "company": company,
                "score": score,
                "persona": persona,
                "language": language,
                "industry": industry,
                "created_at": created_at,
            })

        dm_eligible = sum(1 for s in scores_valid if s >= 60)
        strong_pass = sum(1 for s in scores_valid if s >= 75)
        avg_score = sum(scores_valid) / len(scores_valid) if scores_valid else 0.0

        # ── REPORT ────────────────────────────────────────────────────────────

        _section("1. TOTALS & SCORE SUMMARY")
        click.echo(f"  Total PROSPECT entries : {total}")
        click.echo(f"  With valid score       : {len(scores_valid)}  ({_pct(len(scores_valid), total)} of cohort)")
        click.echo(f"  Null / missing score   : {null_score_count}  ({_pct(null_score_count, total)} of cohort)")
        click.echo(f"  Average score (valid)  : {avg_score:.1f}")
        click.echo(f"  DM-eligible (≥60)      : {dm_eligible}  ({_pct(dm_eligible, total)} of cohort)")
        click.echo(f"  Strong pass (≥75)      : {strong_pass}  ({_pct(strong_pass, total)} of cohort)")
        click.echo(f"  Days of cap at 25/day  : {total // 25} days")

        _section("2. SCORE HISTOGRAM")
        bin_order = ["<40", "40–59", "60–69", "70–79", "80–89", "90+", "null"]
        for bin_ in bin_order:
            n = score_bins.get(bin_, 0)
            bar = _bar(n, total)
            click.echo(f"  {bin_:>7}  {bar}  {n:>4}  ({_pct(n, total):>5})")

        _section("3. PERSONA BREAKDOWN")
        for persona, count in persona_counts.most_common():
            bar = _bar(count, total)
            click.echo(f"  {str(persona)[:20]:<22} {bar}  {count:>4}  ({_pct(count, total):>5})")
        if null_persona_count:
            click.echo(f"\n  ⚠  {null_persona_count} entries have no persona set ({_pct(null_persona_count, total)})")

        _section("4. LANGUAGE BREAKDOWN")
        for lang, count in language_counts.most_common():
            bar = _bar(count, total)
            click.echo(f"  {str(lang)[:20]:<22} {bar}  {count:>4}  ({_pct(count, total):>5})")
        if null_language_count:
            click.echo(f"\n  ⚠  {null_language_count} entries have no language set ({_pct(null_language_count, total)})")

        _section("5. INDUSTRY TOP 15")
        for industry, count in industry_counts.most_common(15):
            bar = _bar(count, total)
            click.echo(f"  {str(industry)[:28]:<30} {bar}  {count:>4}  ({_pct(count, total):>5})")

        _section("6. PIPELINE AGE")
        if age_available:
            age_order = ["0–7d", "8–30d", "31–90d", "90d+"]
            for bucket in age_order:
                n = age_counts.get(bucket, 0)
                bar = _bar(n, total)
                click.echo(f"  {bucket:<8}  {bar}  {n:>4}  ({_pct(n, total):>5})")
            no_ts = age_counts.get("no_timestamp", 0)
            if no_ts:
                click.echo(f"  no_ts     {'░' * 20}  {no_ts:>4}  ({_pct(no_ts, total):>5})")
        else:
            click.echo("  created_at timestamp not exposed in entry payload — age bucketing skipped.")

        _section("7. TOP 20 HIGHEST-SCORED PROSPECTS")
        ranked = sorted(
            [e for e in enriched if e["score"] is not None],
            key=lambda x: x["score"],
            reverse=True,
        )[:20]
        click.echo(f"  {'#':<3} {'Name':<22} {'Company':<24} {'Score':>5}  {'Persona':<16} {'Industry'}")
        click.echo(f"  {'─'*3} {'─'*22} {'─'*24} {'─'*5}  {'─'*16} {'─'*20}")
        for i, e in enumerate(ranked, 1):
            name = str(e["name"])[:21]
            company = str(e["company"])[:23]
            persona = str(e["persona"])[:15]
            industry = str(e["industry"])[:25]
            score = f"{e['score']:.0f}"
            click.echo(f"  {i:<3} {name:<22} {company:<24} {score:>5}  {persona:<16} {industry}")

        click.echo(f"\n{'═' * 60}")
        click.echo("  END OF REPORT")
        click.echo(f"{'═' * 60}\n")


if __name__ == "__main__":
    sys.exit(main())
