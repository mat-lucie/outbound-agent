"""Funnel diagnostic for the ICP qualifier.

Pulls all list entries from Attio, stratifies DM-response rate (and the
upstream connect-acceptance rate) by score-breakdown components, and emits
a verdict identifying whether the leak is an ICP problem, a message problem,
both, or whether the data is too thin to decide.

Used by:
  - `cli.py weekly-diagnostic` — operator-facing markdown report.
  - `workflows.weekly_brain` — appends a section to the Sunday brain.
  - `workflows.qualifier_critique` — feeds the LLM critique a structured
    DiagnosticReport so the proposal references real disagreement examples.

Pure pandas + scipy. No LLM calls in this module — instrument once, decide
crisp, hand off to the critique step for proposals.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pandas as pd

from models.pipeline import PipelineStage
from workflows.quality_gate import score_band

if TYPE_CHECKING:
    from datetime import date

logger = logging.getLogger(__name__)


# Stages for funnel-rate denominators / numerators.
# Aligned with workflows/learn.py — kept consistent so cohort metrics elsewhere
# don't drift apart from this diagnostic.
_DMED_STAGES = frozenset({
    PipelineStage.DM1_SENT.value,
    PipelineStage.DM2_SENT.value,
    PipelineStage.DM3_SENT.value,
    PipelineStage.RESPONDED.value,
    PipelineStage.CALL_BOOKED.value,
    PipelineStage.QUALIFIED.value,
    PipelineStage.NOT_INTERESTED.value,
})
_RESPONDED_STAGES = frozenset({
    PipelineStage.RESPONDED.value,
    PipelineStage.CALL_BOOKED.value,
    PipelineStage.QUALIFIED.value,
})
# Connect-acceptance numerator: stages that imply the connect was accepted.
# Anything from ACCEPTED forward counts as accepted.
_ACCEPTED_OR_BEYOND = frozenset(_DMED_STAGES | {PipelineStage.ACCEPTED.value})
# Connect-acceptance denominator: anything that's been sent a connect request
# (including those still waiting). Everything from CONNECTION_SENT forward.
_CONNECT_ELIGIBLE = frozenset(_ACCEPTED_OR_BEYOND | {PipelineStage.CONNECTION_SENT.value})


# Default min-N per stratum. Below this the stratum is dropped — Wilson CI
# explodes and we'd misread noise as signal. 15 is the same threshold
# learn.py uses for cohort maturity.
DEFAULT_MIN_N = 15

# Default healthy thresholds for the verdict logic.
DEFAULT_BASELINE_RESPONSE_RATE = 0.06  # ~"baseline-v0" + a couple of point estimates
DEFAULT_HEALTHY_ACCEPT_RATE = 0.30
DEFAULT_LOW_RESPONSE_RATE = 0.05


# ── Wilson 95% confidence interval ────────────────────────────────────────────

def _wilson_ci(succ: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion. 95% CI by default.

    Robust at the boundaries (handles 0/n and n/n without div-by-zero).
    Returns (0.0, 1.0) for total=0 — widest possible "no information" CI.
    """
    if total <= 0:
        return (0.0, 1.0)
    if succ < 0 or succ > total:
        raise ValueError(f"Invalid Wilson inputs: {succ}/{total}")
    p = succ / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    lo = max(0.0, center - half)
    hi = min(1.0, center + half)
    return (lo, hi)


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class StratumStat:
    """One row of the stratification table — counts and rates per segment."""
    key: dict[str, str]
    n_dmed: int
    n_responded: int
    n_defensive: int
    dm_response_rate: float
    dm_response_ci: tuple[float, float]
    defensive_rate: float
    defensive_ci: tuple[float, float]
    connect_accept_rate: float
    connect_accept_ci: tuple[float, float]
    n_connection_eligible: int

    def label(self) -> str:
        """Human-readable summary of the segment key."""
        if not self.key:
            return "_(unsegmented — score_breakdown empty or absent)_"
        return " · ".join(f"{k}={v}" for k, v in self.key.items())


@dataclass
class DisagreementExample:
    """One prospect where qualifier verdict and outcome diverged."""
    record_id: str
    name: str
    title: str
    company: str
    quality_score: int
    outcome: str  # "responded", "defensive", "no_reply"
    score_breakdown_text: str  # short summary of the breakdown


@dataclass
class DiagnosticReport:
    """Top-level report consumed by weekly_brain and qualifier_critique."""
    verdict: str  # "icp" | "message" | "both" | "insufficient"
    rationale: str
    overall_accept_rate: float
    overall_response_rate: float
    overall_defensive_rate: float
    n_total_dmed: int
    n_total_connect_eligible: int
    top_segments: list[StratumStat] = field(default_factory=list)
    bottom_segments: list[StratumStat] = field(default_factory=list)
    disagreement_high_no_reply: list[DisagreementExample] = field(default_factory=list)
    disagreement_low_yes_reply: list[DisagreementExample] = field(default_factory=list)
    since_date: date | None = None

    def to_markdown(self) -> str:
        return _render_markdown(self)


# ── DataFrame construction ────────────────────────────────────────────────────

_BD_FIELDS = ("size", "role", "industry", "competitor")


def _parse_breakdown_reasons(raw: Any) -> dict[str, str]:
    """Turn a `score_breakdown` JSON-or-dict into bd_size/bd_role/bd_industry/bd_competitor.

    `score_breakdown.reasons` is an ordered list whose conventional positions
    map to size, role, competitor, industry (per quality_gate.py order). We
    keep only the first reason per dimension as the segment label — the
    operator gets human-readable strings ("Decision-maker title") in the
    diagnostic table without us needing to re-derive the keyword logic.
    """
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return {}
    if not isinstance(raw, dict):
        return {}
    reasons = raw.get("reasons") or []
    if not isinstance(reasons, list):
        return {}
    out: dict[str, str] = {}
    # Conventional positions in quality_gate.py: size first, role second,
    # competitor/exclusion third, industry fourth (when present).
    keys = ("bd_size", "bd_role", "bd_competitor", "bd_industry")
    for i, k in enumerate(keys):
        if i < len(reasons):
            out[k] = str(reasons[i])
    return out


def _entries_to_dataframe(entries: list[dict]) -> pd.DataFrame:
    """Flatten parsed Attio entries into the columns the diagnostic groups on.

    Prefers native fields (post-Phase-1 schema migration) when present;
    otherwise falls back to client-side `score_breakdown` JSON parsing.
    Both paths produce identical column shape.
    """
    rows: list[dict] = []
    for e in entries:
        bd = _parse_breakdown_reasons(e.get("score_breakdown"))
        score = e.get("quality_score")
        # Prefer native band when present (post-migration entries); else derive.
        band = e.get("quality_score_band") or score_band(score)
        rows.append({
            "record_id": e.get("record_id", ""),
            "stage": e.get("stage", "") or "",
            "persona": e.get("persona") or "",
            "language": e.get("language") or "",
            "scoring_lane": e.get("scoring_lane") or "",
            "verdict_path": e.get("verdict_path") or "",
            "quality_score": int(score) if score is not None else None,
            "quality_score_band": band,
            "icp_lane_persisted": e.get("icp_lane_persisted"),
            "response_classification": e.get("response_classification"),
            "name": e.get("name") or "",
            "title": e.get("title") or "",
            "company": e.get("company") or "",
            "bd_size": bd.get("bd_size", ""),
            "bd_role": bd.get("bd_role", ""),
            "bd_industry": bd.get("bd_industry", ""),
            "bd_competitor": bd.get("bd_competitor", ""),
        })
    return pd.DataFrame(rows)


def _is_dmed(stage: str) -> bool:
    return stage in _DMED_STAGES


def _is_responded(stage: str) -> bool:
    return stage in _RESPONDED_STAGES


def _is_accepted(stage: str) -> bool:
    return stage in _ACCEPTED_OR_BEYOND


def _is_connect_eligible(stage: str) -> bool:
    return stage in _CONNECT_ELIGIBLE


# ── Stratification ────────────────────────────────────────────────────────────

# Dimensions to group on. The product is small enough (≤ a few hundred groups)
# that we don't need to pre-prune.
_STRAT_DIMENSIONS = ("bd_role", "bd_industry", "scoring_lane")


def _stratify(df: pd.DataFrame, min_n: int = DEFAULT_MIN_N) -> list[StratumStat]:
    """Group entries by stratification dimensions and compute per-stratum rates.

    Drops strata with n_dmed < min_n — Wilson CI is unstable below ~15 and we'd
    misread noise as signal.
    """
    if df.empty:
        return []
    df = df.copy()
    df["_dmed"] = df["stage"].map(_is_dmed)
    df["_responded"] = df["stage"].map(_is_responded)
    df["_defensive"] = df["response_classification"].fillna("") == "defensive"
    df["_accepted"] = df["stage"].map(_is_accepted)
    df["_connect_eligible"] = df["stage"].map(_is_connect_eligible)

    out: list[StratumStat] = []
    grouped = df.groupby(list(_STRAT_DIMENSIONS), dropna=False)
    for group_key, sub in grouped:
        n_dmed = int(sub["_dmed"].sum())
        if n_dmed < min_n:
            continue
        n_responded = int(sub["_responded"].sum())
        n_defensive = int(sub["_defensive"].sum())
        n_connect_eligible = int(sub["_connect_eligible"].sum())
        n_accepted = int(sub["_accepted"].sum())

        rate_resp = n_responded / n_dmed if n_dmed else 0.0
        rate_def = n_defensive / n_dmed if n_dmed else 0.0
        rate_acc = n_accepted / n_connect_eligible if n_connect_eligible else 0.0

        key = dict(zip(_STRAT_DIMENSIONS, group_key, strict=False))
        # Drop empty-string keys to keep the table compact
        key = {k: v for k, v in key.items() if v not in ("", None)}

        out.append(StratumStat(
            key=key,
            n_dmed=n_dmed,
            n_responded=n_responded,
            n_defensive=n_defensive,
            dm_response_rate=rate_resp,
            dm_response_ci=_wilson_ci(n_responded, n_dmed),
            defensive_rate=rate_def,
            defensive_ci=_wilson_ci(n_defensive, n_dmed),
            connect_accept_rate=rate_acc,
            connect_accept_ci=_wilson_ci(n_accepted, n_connect_eligible),
            n_connection_eligible=n_connect_eligible,
        ))
    return out


# ── Verdict logic ─────────────────────────────────────────────────────────────

def _cis_overlap(a: tuple[float, float], b: tuple[float, float]) -> bool:
    """Return True if intervals a and b overlap (or touch)."""
    return not (a[1] < b[0] or b[1] < a[0])


def _decide_verdict(
    strata: list[StratumStat],
    *,
    overall_accept_rate: float,
    overall_response_rate: float,
    overall_defensive_rate: float,
    baseline_response_rate: float = DEFAULT_BASELINE_RESPONSE_RATE,
    healthy_accept_rate: float = DEFAULT_HEALTHY_ACCEPT_RATE,
    low_response_rate: float = DEFAULT_LOW_RESPONSE_RATE,
) -> tuple[str, str]:
    """Return (verdict, rationale).

    Verdicts:
      "icp"          — top-vs-bottom 95% CIs don't overlap (qualifier signal)
      "message"      — CIs overlap, accept rate healthy, reply rate low
      "both"         — ICP signal AND overall reply rate below baseline
      "insufficient" — fewer than 2 strata clear min_n
    """
    if len(strata) < 2:
        return "insufficient", (
            f"Only {len(strata)} stratum cleared min-N. "
            "Need ≥ 2 to compare segments — wait for more cohorts."
        )

    sorted_strata = sorted(strata, key=lambda s: s.dm_response_rate)
    bottom = sorted_strata[0]
    top = sorted_strata[-1]
    icp_signal = not _cis_overlap(top.dm_response_ci, bottom.dm_response_ci)

    if icp_signal:
        gap_pp = (top.dm_response_rate - bottom.dm_response_rate) * 100.0
        rationale_icp = (
            f"Top stratum ({top.label()}) reply rate {top.dm_response_rate:.1%} "
            f"vs bottom ({bottom.label()}) {bottom.dm_response_rate:.1%} — "
            f"95% CIs do not overlap (gap {gap_pp:.1f}pp). "
            "Qualifier discriminates poorly post-DM, tuning has signal."
        )
        # If the OVERALL reply rate is also depressed below baseline, both
        # signals are present — nudge to "both" so the operator gets the
        # broader framing.
        if overall_response_rate < baseline_response_rate:
            return "both", (
                rationale_icp + f" Overall reply rate {overall_response_rate:.1%} "
                f"is below baseline {baseline_response_rate:.1%} — "
                "message work is also worth pursuing."
            )
        return "icp", rationale_icp

    if (
        overall_accept_rate >= healthy_accept_rate
        and overall_response_rate < low_response_rate
    ):
        return "message", (
            f"Top vs bottom CIs overlap (no ICP signal). "
            f"Acceptance {overall_accept_rate:.1%} ≥ {healthy_accept_rate:.0%} "
            f"and reply rate {overall_response_rate:.1%} < {low_response_rate:.0%}. "
            "Qualifier picks fine, message fails — hand to /sales-learn."
        )

    return "insufficient", (
        f"No CI separation across strata, and overall metrics don't trip the "
        f"message-problem threshold (accept {overall_accept_rate:.1%}, "
        f"reply {overall_response_rate:.1%}). Wait for more data."
    )


# ── Disagreement examples ─────────────────────────────────────────────────────

def _disagreement_text(row: pd.Series) -> str:
    """Short breakdown summary for the markdown table."""
    parts = [
        row.get("bd_role", ""),
        row.get("bd_industry", ""),
        row.get("scoring_lane", ""),
    ]
    return " · ".join(p for p in parts if p)


def _collect_disagreements(
    df: pd.DataFrame, limit: int = 10
) -> tuple[list[DisagreementExample], list[DisagreementExample]]:
    """Find prospects where the qualifier verdict and the outcome diverged."""
    if df.empty:
        return [], []

    df = df.copy()
    df["_dmed"] = df["stage"].map(_is_dmed)
    df["_responded"] = df["stage"].map(_is_responded)

    high_no = df[(df["quality_score"].fillna(0) >= 75) & df["_dmed"] & ~df["_responded"]]
    low_yes = df[(df["quality_score"].fillna(0) < 60) & df["_responded"]]

    high_no_sorted = high_no.sort_values("quality_score", ascending=False).head(limit)
    low_yes_sorted = low_yes.sort_values("quality_score", ascending=True).head(limit)

    def _row_to_ex(row: pd.Series, outcome: str) -> DisagreementExample:
        return DisagreementExample(
            record_id=str(row.get("record_id", "")),
            name=str(row.get("name", "")),
            title=str(row.get("title", "")),
            company=str(row.get("company", "")),
            quality_score=int(row.get("quality_score", 0) or 0),
            outcome=outcome,
            score_breakdown_text=_disagreement_text(row),
        )

    high_no_list = [_row_to_ex(r, "no_reply") for _, r in high_no_sorted.iterrows()]
    low_yes_list = [_row_to_ex(r, "responded") for _, r in low_yes_sorted.iterrows()]
    return high_no_list, low_yes_list


# ── Markdown rendering ────────────────────────────────────────────────────────

_VERDICT_HEADLINES = {
    "icp": "**Verdict: ICP problem.** The qualifier discriminates poorly post-DM — tuning weights/keywords has signal.",
    "message": "**Verdict: Message problem.** Acceptance is healthy; reply rate is uniformly low. Hand to `/sales-learn`.",
    "both": "**Verdict: Both.** ICP signal AND overall reply rate is depressed.",
    "insufficient": "**Verdict: Insufficient data.** Wait for more cohorts before tuning.",
}


def _format_pct_with_ci(rate: float, ci: tuple[float, float]) -> str:
    return f"{rate:.1%} [{ci[0]:.1%}, {ci[1]:.1%}]"


def _render_stratum_table(strata: list[StratumStat], header: str) -> str:
    if not strata:
        return f"### {header}\n\n_No strata clear min-N._\n"
    lines = [
        f"### {header}",
        "",
        "| Segment | n DM'd | n Responded | DM reply rate (95% CI) | Defensive rate | Connect accept rate |",
        "|---|---|---|---|---|---|",
    ]
    for s in strata:
        lines.append(
            f"| {s.label()} "
            f"| {s.n_dmed} "
            f"| {s.n_responded} "
            f"| {_format_pct_with_ci(s.dm_response_rate, s.dm_response_ci)} "
            f"| {_format_pct_with_ci(s.defensive_rate, s.defensive_ci)} "
            f"| {_format_pct_with_ci(s.connect_accept_rate, s.connect_accept_ci)} |"
        )
    lines.append("")
    return "\n".join(lines)


def _md_cell(text: str) -> str:
    """Escape `|` in markdown table cells. Some prospect titles literally
    contain '|' (multi-role headlines) which would otherwise break the table.
    """
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _render_disagreements(
    examples: list[DisagreementExample], header: str
) -> str:
    if not examples:
        return f"### {header}\n\n_None._\n"
    lines = [f"### {header}", "", "| Score | Name | Title | Company | Breakdown |", "|---|---|---|---|---|"]
    for e in examples:
        lines.append(
            f"| {e.quality_score} "
            f"| {_md_cell(e.name)} "
            f"| {_md_cell(e.title)} "
            f"| {_md_cell(e.company)} "
            f"| {_md_cell(e.score_breakdown_text)} |"
        )
    lines.append("")
    return "\n".join(lines)


def _render_markdown(report: DiagnosticReport) -> str:
    parts = [
        "## Funnel diagnostic + auto-research",
        "",
        _VERDICT_HEADLINES.get(report.verdict, f"Verdict: {report.verdict}"),
        "",
        report.rationale,
        "",
        f"**Overall:** {report.n_total_dmed} DM'd · "
        f"reply rate {report.overall_response_rate:.1%} · "
        f"defensive rate {report.overall_defensive_rate:.1%} · "
        f"connect accept rate {report.overall_accept_rate:.1%} "
        f"(of {report.n_total_connect_eligible} sent).",
        "",
        _render_stratum_table(report.top_segments, "Top reply-rate segments"),
        _render_stratum_table(report.bottom_segments, "Bottom reply-rate segments"),
        _render_disagreements(report.disagreement_high_no_reply, "High score, no reply"),
        _render_disagreements(report.disagreement_low_yes_reply, "Low score, did reply"),
    ]
    return "\n".join(parts)


# ── Public entry point ────────────────────────────────────────────────────────

def diagnose_funnel(
    attio_client,
    since_date: date | None = None,
    *,
    min_n: int = DEFAULT_MIN_N,
    baseline_response_rate: float = DEFAULT_BASELINE_RESPONSE_RATE,
    list_id: str | None = None,
) -> DiagnosticReport:
    """Pull list entries, stratify, decide verdict, return DiagnosticReport.

    `since_date` is currently informational — the underlying Attio query
    pulls all entries (last_contact_date filtering would require a server-
    side filter we don't have today). Future enhancement.
    """
    raw = attio_client.query_list_entries(list_id=list_id) if list_id else attio_client.query_list_entries()
    parsed = [attio_client.parse_entry(e) for e in raw]
    df = _entries_to_dataframe(parsed)

    if df.empty:
        return DiagnosticReport(
            verdict="insufficient",
            rationale="No list entries returned from Attio.",
            overall_accept_rate=0.0,
            overall_response_rate=0.0,
            overall_defensive_rate=0.0,
            n_total_dmed=0,
            n_total_connect_eligible=0,
            since_date=since_date,
        )

    # Overall rates (use the full df, not strata-aggregated).
    df["_dmed"] = df["stage"].map(_is_dmed)
    df["_responded"] = df["stage"].map(_is_responded)
    df["_defensive"] = df["response_classification"].fillna("") == "defensive"
    df["_accepted"] = df["stage"].map(_is_accepted)
    df["_connect_eligible"] = df["stage"].map(_is_connect_eligible)

    n_dmed = int(df["_dmed"].sum())
    n_responded = int(df["_responded"].sum())
    n_defensive = int(df["_defensive"].sum())
    n_accepted = int(df["_accepted"].sum())
    n_connect_eligible = int(df["_connect_eligible"].sum())

    overall_response_rate = n_responded / n_dmed if n_dmed else 0.0
    overall_defensive_rate = n_defensive / n_dmed if n_dmed else 0.0
    overall_accept_rate = n_accepted / n_connect_eligible if n_connect_eligible else 0.0

    strata = _stratify(df, min_n=min_n)
    verdict, rationale = _decide_verdict(
        strata,
        overall_accept_rate=overall_accept_rate,
        overall_response_rate=overall_response_rate,
        overall_defensive_rate=overall_defensive_rate,
        baseline_response_rate=baseline_response_rate,
    )

    sorted_strata = sorted(strata, key=lambda s: s.dm_response_rate, reverse=True)
    top = sorted_strata[:3]
    bottom = list(reversed(sorted_strata[-3:])) if len(sorted_strata) >= 3 else []

    high_no, low_yes = _collect_disagreements(df, limit=10)

    # Fetch name/title/company for the at-most-20 disagreement records so the
    # report is actionable. Cheap — one batched parallel fetch — and only the
    # operator-facing examples need this resolution.
    needed_ids = {ex.record_id for ex in (*high_no, *low_yes) if ex.record_id}
    if needed_ids and hasattr(attio_client, "bulk_fetch_persons_by_record_ids"):
        try:
            persons = attio_client.bulk_fetch_persons_by_record_ids(needed_ids)
        except Exception:  # noqa: BLE001 — name resolution is best-effort
            persons = {}
        for ex_list in (high_no, low_yes):
            for ex in ex_list:
                rec = persons.get(ex.record_id)
                if not rec:
                    continue
                try:
                    name, company, _linkedin, _industry, _title = attio_client.extract_record_info(rec)
                except Exception:  # noqa: BLE001
                    continue
                ex.name = name or ex.name
                ex.company = company or ex.company
                # Job title lives at values.job_title[0].value on the person record
                values = (rec.get("data") or rec).get("values", {})
                title_data = values.get("job_title", [])
                if title_data and isinstance(title_data, list):
                    val = title_data[0]
                    if isinstance(val, dict):
                        ex.title = str(val.get("value", val)) or ex.title

    return DiagnosticReport(
        verdict=verdict,
        rationale=rationale,
        overall_accept_rate=overall_accept_rate,
        overall_response_rate=overall_response_rate,
        overall_defensive_rate=overall_defensive_rate,
        n_total_dmed=n_dmed,
        n_total_connect_eligible=n_connect_eligible,
        top_segments=top,
        bottom_segments=bottom,
        disagreement_high_no_reply=high_no,
        disagreement_low_yes_reply=low_yes,
        since_date=since_date,
    )
