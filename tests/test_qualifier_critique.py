"""Tests for workflows/qualifier_critique.py."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from workflows.qualifier_critique import (
    VALID_ACTIONS,
    CritiqueProposal,
    _build_user_content,
    _parse_proposal,
    propose_change,
)
from workflows.qualifier_diagnostic import (
    DiagnosticReport,
    DisagreementExample,
    StratumStat,
)

# ── Helpers ────────────────────────────────────────────────────────────────────

def _stub_report(verdict: str = "icp") -> DiagnosticReport:
    top = StratumStat(
        key={"bd_role": "Decision-maker title", "bd_industry": "Manufacturing"},
        n_dmed=20, n_responded=10, n_defensive=1,
        dm_response_rate=0.50, dm_response_ci=(0.32, 0.68),
        defensive_rate=0.05, defensive_ci=(0.0, 0.24),
        connect_accept_rate=0.4, connect_accept_ci=(0.3, 0.5),
        n_connection_eligible=50,
    )
    bot = StratumStat(
        key={"bd_role": "Influencer title", "bd_industry": "Other"},
        n_dmed=20, n_responded=1, n_defensive=2,
        dm_response_rate=0.05, dm_response_ci=(0.01, 0.24),
        defensive_rate=0.10, defensive_ci=(0.0, 0.30),
        connect_accept_rate=0.4, connect_accept_ci=(0.3, 0.5),
        n_connection_eligible=50,
    )
    high_no = [
        DisagreementExample(
            record_id="r1", name="A B", title="VP Ops", company="Bimbo",
            quality_score=88, outcome="no_reply",
            score_breakdown_text="Decision-maker · Manufacturing",
        )
    ]
    low_yes = [
        DisagreementExample(
            record_id="r2", name="C D", title="Manager", company="Cementra",
            quality_score=55, outcome="responded",
            score_breakdown_text="Influencer · Construction",
        )
    ]
    return DiagnosticReport(
        verdict=verdict,
        rationale="test rationale",
        overall_accept_rate=0.4, overall_response_rate=0.10, overall_defensive_rate=0.05,
        n_total_dmed=80, n_total_connect_eligible=200,
        top_segments=[top], bottom_segments=[bot],
        disagreement_high_no_reply=high_no,
        disagreement_low_yes_reply=low_yes,
    )


def _mock_anthropic(response_json: dict) -> MagicMock:
    """Build a mock anthropic client whose messages.create returns response_json."""
    client = MagicMock()
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = json.dumps(response_json)
    response = MagicMock()
    response.content = [text_block]
    client.messages.create.return_value = response
    return client


# ── Prompt construction ───────────────────────────────────────────────────────

def test_build_user_content_includes_verdict_and_disagreements() -> None:
    report = _stub_report()
    content = _build_user_content(report)
    assert "icp" in content.lower() or "ICP" in content
    assert "Decision-maker" in content
    assert "VP Ops" in content
    assert "Bimbo" in content


def test_build_user_content_for_message_verdict_calls_out_handoff_hint() -> None:
    report = _stub_report(verdict="message")
    content = _build_user_content(report)
    assert "message" in content.lower()


# ── Proposal parsing ──────────────────────────────────────────────────────────

def test_parse_proposal_valid_weight_change() -> None:
    raw = json.dumps({
        "action": "weight_change",
        "target_file": "workflows/quality_gate.py",
        "specific_change": "INDUSTRY_PENALTY_OFF_ICP: -25 → -30",
        "hypothesis": "Off-ICP industries reply 3x less.",
        "predicted_lift_pp": 4,
        "why_not_others": "Considered lowering threshold (rejected).",
    })
    p = _parse_proposal(raw)
    assert p.action == "weight_change"
    assert p.predicted_lift_pp == 4
    assert "INDUSTRY_PENALTY" in p.specific_change


def test_parse_proposal_valid_no_change() -> None:
    raw = json.dumps({
        "action": "no_change",
        "target_file": "",
        "specific_change": "",
        "hypothesis": "Cohort too thin to justify.",
        "predicted_lift_pp": 0,
        "why_not_others": "All candidates predicted < 3pp lift.",
    })
    p = _parse_proposal(raw)
    assert p.action == "no_change"
    assert p.predicted_lift_pp == 0


def test_parse_proposal_strips_code_fences() -> None:
    raw = '```json\n' + json.dumps({
        "action": "handoff_to_sales_learn",
        "target_file": "content/messages.json",
        "specific_change": "DM1 copy variant",
        "hypothesis": "Acceptance OK, reply rate flat — copy issue.",
        "predicted_lift_pp": 0,
        "why_not_others": "Qualifier showed no segment signal.",
    }) + '\n```'
    p = _parse_proposal(raw)
    assert p.action == "handoff_to_sales_learn"


def test_parse_proposal_invalid_action_falls_back() -> None:
    raw = json.dumps({
        "action": "summon_demon",  # invalid
        "target_file": "workflows/quality_gate.py",
        "specific_change": "Reverse polarity of neutron flow",
        "hypothesis": "h",
        "predicted_lift_pp": 50,
        "why_not_others": "",
    })
    p = _parse_proposal(raw)
    assert p.action == "no_change"
    assert "invalid action" in p.hypothesis.lower() or "fallback" in p.hypothesis.lower()


def test_parse_proposal_malformed_json_falls_back() -> None:
    p = _parse_proposal("not json at all {{{")
    assert p.action == "no_change"


def test_parse_proposal_predicted_lift_below_threshold_for_action_falls_back() -> None:
    raw = json.dumps({
        "action": "weight_change",
        "target_file": "workflows/quality_gate.py",
        "specific_change": "Tiny tweak",
        "hypothesis": "Marginal",
        "predicted_lift_pp": 1,  # below 3pp threshold
        "why_not_others": "",
    })
    p = _parse_proposal(raw)
    assert p.action == "no_change"


def test_valid_actions_includes_known_set() -> None:
    assert "weight_change" in VALID_ACTIONS
    assert "keyword_change" in VALID_ACTIONS
    assert "threshold_change" in VALID_ACTIONS
    assert "handoff_to_sales_learn" in VALID_ACTIONS
    assert "no_change" in VALID_ACTIONS


# ── End-to-end propose_change ──────────────────────────────────────────────────

def test_propose_change_happy_path() -> None:
    report = _stub_report()
    client = _mock_anthropic({
        "action": "weight_change",
        "target_file": "workflows/quality_gate.py",
        "specific_change": "INDUSTRY_PENALTY_OFF_ICP: -25 → -30",
        "hypothesis": "Off-ICP industries reply 3x less.",
        "predicted_lift_pp": 4,
        "why_not_others": "Other candidates predicted lower lift.",
    })
    p = propose_change(report, client)
    assert isinstance(p, CritiqueProposal)
    assert p.action == "weight_change"
    # Verify system prompt was cached (cache_control on first system block)
    call_kwargs = client.messages.create.call_args.kwargs
    system_blocks = call_kwargs.get("system")
    assert system_blocks, "messages.create called without system kwarg"
    assert any(
        isinstance(block, dict)
        and block.get("cache_control", {}).get("type") == "ephemeral"
        for block in system_blocks
    )


def test_propose_change_llm_exception_returns_no_change() -> None:
    report = _stub_report()
    client = MagicMock()
    client.messages.create.side_effect = RuntimeError("API down")
    p = propose_change(report, client)
    assert p.action == "no_change"
    assert p.predicted_lift_pp == 0
    assert "error" in p.hypothesis.lower() or "unavailable" in p.hypothesis.lower()


def test_propose_change_skips_llm_for_insufficient_verdict() -> None:
    report = _stub_report(verdict="insufficient")
    client = MagicMock()
    p = propose_change(report, client)
    # Should short-circuit with no_change without calling the LLM
    assert p.action == "no_change"
    client.messages.create.assert_not_called()
