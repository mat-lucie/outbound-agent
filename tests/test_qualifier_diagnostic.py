"""Tests for workflows/qualifier_diagnostic.py.

Covers:
  - Wilson 95% CI math on the boundaries.
  - Verdict routing across all 5 branches (ICP, message, both, insufficient).
  - Stratification with native fields and JSON-fallback parsing.
  - Disagreement-example collection.
  - Markdown rendering smoke test.
"""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock

from models.pipeline import PipelineStage
from workflows.qualifier_diagnostic import (
    DiagnosticReport,
    StratumStat,
    _collect_disagreements,
    _decide_verdict,
    _entries_to_dataframe,
    _stratify,
    _wilson_ci,
    diagnose_funnel,
)

# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_entry(
    *,
    stage: str = PipelineStage.RESPONDED.value,
    bd_size: str = "Mid-market sweet spot",
    bd_role: str = "Decision-maker title",
    bd_industry: str = "Manufacturing",
    bd_competitor: str = "Not a competitor",
    scoring_lane: str = "enterprise_mode",
    verdict_path: str = "enterprise_pass",
    quality_score: int = 80,
    quality_score_band: str | None = None,
    response_classification: str | None = None,
    persona: str = "operations_leaders",
    name: str = "Carlos Mendoza",
    title: str = "VP Operations",
    company: str = "Grupo Bimbo",
    record_id: str = "rid-1",
    icp_lane_persisted: int | None = None,
    use_native_band: bool = False,
) -> dict:
    """Produce a parsed Attio entry dict matching AttioClient.parse_entry's shape."""
    breakdown = {
        "size": 28,
        "role": 28,
        "competitor": 20,
        "industry": 12,
        "total": quality_score,
        # Reason order matches workflows/quality_gate.py: size, role, competitor, industry.
        "reasons": [bd_size, bd_role, bd_competitor, bd_industry],
    }
    return {
        "entry_id": f"eid-{record_id}",
        "record_id": record_id,
        "stage": stage,
        "persona": persona,
        "language": "es",
        "dm_step": 1,
        "quality_score": quality_score,
        "last_contact_date": None,
        "experiment_id": None,
        "response_classification": response_classification,
        "last_response_text": None,
        "score_breakdown": json.dumps(breakdown),
        "scoring_lane": scoring_lane,
        "verdict_path": verdict_path,
        "llm_rationale": None,
        "icp_lane_persisted": icp_lane_persisted,
        "quality_score_band": quality_score_band if use_native_band else None,
        "defensive_score": None,
        "engagement_score": None,
        "name": name,
        "title": title,
        "company": company,
    }


def _attio_mock(entries: list[dict]) -> MagicMock:
    raw = [{"_raw": e} for e in entries]
    attio = MagicMock()
    attio.query_list_entries.return_value = raw
    attio.parse_entry.side_effect = lambda e: e["_raw"]
    return attio


# ── Wilson CI math ────────────────────────────────────────────────────────────

def test_wilson_ci_zero_successes() -> None:
    lo, hi = _wilson_ci(0, 10)
    assert lo == 0.0
    # Upper bound for 0/10 with 95% confidence is ~0.278
    assert 0.25 < hi < 0.32


def test_wilson_ci_full_successes() -> None:
    lo, hi = _wilson_ci(10, 10)
    assert hi == 1.0
    assert 0.68 < lo < 0.75


def test_wilson_ci_zero_total_returns_zero_one() -> None:
    # No data — return widest CI rather than div-by-zero.
    lo, hi = _wilson_ci(0, 0)
    assert lo == 0.0
    assert hi == 1.0


def test_wilson_ci_midpoint() -> None:
    # 50/100 should center near 0.5
    lo, hi = _wilson_ci(50, 100)
    assert 0.40 < lo < 0.42
    assert 0.58 < hi < 0.60


# ── Stratification ────────────────────────────────────────────────────────────

def test_entries_to_dataframe_uses_native_fields_when_present() -> None:
    e = _make_entry(
        quality_score=80,
        quality_score_band=">75",
        use_native_band=True,
    )
    df = _entries_to_dataframe([e])
    assert df.iloc[0]["quality_score_band"] == ">75"


def test_entries_to_dataframe_falls_back_to_score_band_derivation() -> None:
    # Legacy entry with no native band → derive from quality_score
    e = _make_entry(quality_score=65, use_native_band=False)
    df = _entries_to_dataframe([e])
    assert df.iloc[0]["quality_score_band"] == "60-75"


def test_entries_to_dataframe_parses_score_breakdown_json() -> None:
    e = _make_entry()
    df = _entries_to_dataframe([e])
    row = df.iloc[0]
    assert row["bd_role"] == "Decision-maker title"
    assert row["bd_industry"] == "Manufacturing"


def test_stratify_groups_and_computes_rates() -> None:
    # 20 prospects in segment A, 8 responded
    seg_a = [
        _make_entry(
            stage=(PipelineStage.RESPONDED.value if i < 8 else PipelineStage.DM1_SENT.value),
            bd_role="Decision-maker title",
            record_id=f"a{i}",
        )
        for i in range(20)
    ]
    # 20 prospects in segment B, 2 responded
    seg_b = [
        _make_entry(
            stage=(PipelineStage.RESPONDED.value if i < 2 else PipelineStage.DM1_SENT.value),
            bd_role="Influencer title",
            record_id=f"b{i}",
        )
        for i in range(20)
    ]
    df = _entries_to_dataframe(seg_a + seg_b)
    strata = _stratify(df, min_n=15)

    a = next(s for s in strata if s.key.get("bd_role") == "Decision-maker title")
    b = next(s for s in strata if s.key.get("bd_role") == "Influencer title")
    assert a.n_dmed == 20
    assert a.n_responded == 8
    assert abs(a.dm_response_rate - 0.40) < 1e-6
    assert b.n_dmed == 20
    assert b.n_responded == 2
    assert abs(b.dm_response_rate - 0.10) < 1e-6


def test_stratify_drops_strata_below_min_n() -> None:
    # Only 5 dmed in segment A — below min_n=15 default
    seg_a = [
        _make_entry(stage=PipelineStage.DM1_SENT.value, bd_role="Niche", record_id=f"a{i}")
        for i in range(5)
    ]
    seg_b = [
        _make_entry(stage=PipelineStage.DM1_SENT.value, bd_role="Decision-maker title", record_id=f"b{i}")
        for i in range(20)
    ]
    df = _entries_to_dataframe(seg_a + seg_b)
    strata = _stratify(df, min_n=15)
    role_keys = {s.key.get("bd_role") for s in strata}
    assert "Niche" not in role_keys
    assert "Decision-maker title" in role_keys


# ── Verdict routing ───────────────────────────────────────────────────────────

def _stub_stratum(role: str, n_dmed: int, n_responded: int) -> StratumStat:
    rate = (n_responded / n_dmed) if n_dmed else 0.0
    lo, hi = _wilson_ci(n_responded, n_dmed)
    return StratumStat(
        key={"bd_role": role},
        n_dmed=n_dmed,
        n_responded=n_responded,
        n_defensive=0,
        dm_response_rate=rate,
        dm_response_ci=(lo, hi),
        defensive_rate=0.0,
        defensive_ci=(0.0, 0.0),
        connect_accept_rate=0.5,
        connect_accept_ci=(0.4, 0.6),
        n_connection_eligible=n_dmed * 2,
    )


def test_verdict_icp_problem_when_top_bottom_cis_dont_overlap() -> None:
    # Top: 10/20 = 50%. Bottom: 1/20 = 5%. CIs do NOT overlap.
    strata = [
        _stub_stratum("decision_maker", 20, 10),
        _stub_stratum("low_role", 20, 1),
        _stub_stratum("medium", 20, 5),
    ]
    verdict, _rationale = _decide_verdict(
        strata, overall_accept_rate=0.40, overall_response_rate=0.20, overall_defensive_rate=0.05
    )
    assert verdict == "icp"


def test_verdict_message_problem_when_cis_overlap_and_acceptance_healthy() -> None:
    # All strata around 3-5%, CIs overlap. Accept rate healthy. Reply rate < 5%.
    strata = [
        _stub_stratum("a", 30, 1),
        _stub_stratum("b", 30, 2),
        _stub_stratum("c", 30, 1),
        _stub_stratum("d", 30, 2),
    ]
    verdict, _rationale = _decide_verdict(
        strata, overall_accept_rate=0.45, overall_response_rate=0.04, overall_defensive_rate=0.02
    )
    assert verdict == "message"


def test_verdict_insufficient_data_when_too_few_strata() -> None:
    strata = [_stub_stratum("only_one", 20, 5)]
    verdict, _rationale = _decide_verdict(
        strata, overall_accept_rate=0.40, overall_response_rate=0.10, overall_defensive_rate=0.02
    )
    assert verdict == "insufficient"


def test_verdict_insufficient_data_when_zero_strata() -> None:
    verdict, _rationale = _decide_verdict(
        [], overall_accept_rate=0.0, overall_response_rate=0.0, overall_defensive_rate=0.0
    )
    assert verdict == "insufficient"


def test_verdict_both_when_signal_and_low_overall_rate() -> None:
    # Strong stratum gap (50% vs 3%, CIs definitively don't overlap)
    # AND overall reply rate is below baseline.
    strata = [
        _stub_stratum("decision_maker", 30, 15),  # 50%
        _stub_stratum("low_role", 30, 1),  # 3%
        _stub_stratum("medium", 30, 3),  # 10%
    ]
    verdict, _rationale = _decide_verdict(
        strata, overall_accept_rate=0.40, overall_response_rate=0.04, overall_defensive_rate=0.05,
        baseline_response_rate=0.10,
    )
    # Both ICP signal AND overall reply rate below baseline → "both"
    assert verdict == "both"


# ── Disagreement examples ─────────────────────────────────────────────────────

def test_collect_disagreements_returns_high_score_no_reply_and_inverse() -> None:
    entries = [
        # High score, didn't reply (DM_SENT, no response_classification)
        _make_entry(quality_score=90, stage=PipelineStage.DM1_SENT.value, record_id=f"hi{i}", name=f"Hi{i}")
        for i in range(15)
    ] + [
        # Low score (50), did reply
        _make_entry(quality_score=50, stage=PipelineStage.RESPONDED.value, record_id=f"lo{i}", name=f"Lo{i}")
        for i in range(8)
    ]
    df = _entries_to_dataframe(entries)
    high_no, low_yes = _collect_disagreements(df, limit=10)
    assert len(high_no) == 10  # capped at limit
    assert len(low_yes) == 8
    # All high_no should have no response
    assert all(h.outcome == "no_reply" for h in high_no)
    # All low_yes should have responded
    assert all(ex.outcome == "responded" for ex in low_yes)


# ── End-to-end diagnose_funnel ─────────────────────────────────────────────────

def test_diagnose_funnel_returns_report_with_markdown() -> None:
    entries = [
        _make_entry(stage=PipelineStage.RESPONDED.value, record_id=f"r{i}")
        for i in range(8)
    ] + [
        _make_entry(stage=PipelineStage.DM1_SENT.value, bd_role="Influencer title", record_id=f"d{i}")
        for i in range(20)
    ]
    attio = _attio_mock(entries)
    report = diagnose_funnel(attio, since_date=date(2026, 4, 1))

    assert isinstance(report, DiagnosticReport)
    assert report.verdict in ("icp", "message", "both", "insufficient")
    md = report.to_markdown()
    assert "Funnel diagnostic" in md
    assert report.verdict in md.lower() or "insufficient" in md.lower()
