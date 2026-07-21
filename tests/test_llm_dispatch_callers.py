"""Integration tests: 4 migrated LLM callers routing through dispatch.

Verifies each caller's `use_llm_dispatch=True` path produces the same
return-shape as the legacy `anthropic_client=mock` path. The dispatch
loop is driven by a background thread that simulates the parent
slash-command skill writing canned outputs to the outbox.
"""

from __future__ import annotations

import json
import threading
import time
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def dispatch_env(tmp_path, monkeypatch):
    """Redirect DEFAULT_INBOX_DIR + DEFAULT_OUTBOX_DIR + enable dispatch.

    Wave-2-B note: the autouse ``_isolate_attio_api_key`` conftest
    fixture deletes ``ATTIO_API_KEY`` for every test. The Wave-2-B
    ``LLMBudgetLedger.try_reserve`` now raises
    ``LLMBudgetLedgerNotInitialized`` when dispatch is enabled AND
    ``ATTIO_API_KEY`` is unset (closes the §3.7 silent-fallback
    leak). Tests that exercise dispatch via this fixture aren't
    validating budget enforcement, so set
    ``OUTBOUND_LLM_BUDGET_LEDGER_DISABLED=1`` to short-circuit the
    ledger consult.
    """
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    monkeypatch.setattr("workflows.llm_dispatch.DEFAULT_INBOX_DIR", inbox)
    monkeypatch.setattr("workflows.llm_dispatch.DEFAULT_OUTBOX_DIR", outbox)
    monkeypatch.setattr("workflows.llm_dispatch.DEFAULT_TIMEOUT_S", 3.0)
    monkeypatch.setattr("workflows.llm_dispatch.DEFAULT_POLL_INTERVAL_S", 0.05)
    monkeypatch.setenv("OUTBOUND_LLM_BUDGET_LEDGER_DISABLED", "1")
    return inbox, outbox


def _drive_outbox_once(inbox: Path, outbox: Path, raw_text: str) -> threading.Thread:
    """Background thread: wait for any inbox file, write a single matching
    outbox file with `raw_text` as the subagent response."""
    from workflows.llm_dispatch import write_dispatch_response

    def _worker() -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            files = list(inbox.glob("*.json")) if inbox.exists() else []
            if files:
                time.sleep(0.05)
                req = json.loads(files[0].read_text())
                write_dispatch_response(
                    outbox,
                    dispatch_id=req["dispatch_id"],
                    step=req["step"],
                    raw_text=raw_text,
                    success=True,
                )
                return
            time.sleep(0.01)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return t


# ── quality_gate._llm_qualify ─────────────────────────────────────────────


def test_quality_gate_borderline_via_dispatch(dispatch_env):
    """score_prospect borderline path with use_llm_dispatch=True routes
    through request_llm_dispatch and parses the subagent's JSON output.

    Per engineer + salesman-daily + pr-test convergence (4 agents): the
    test prospect is verified to score in the 40-75 borderline band
    (score=64 at fixture-design time) so the dispatch path is exercised
    unconditionally. If the scoring weights drift such that the prospect
    no longer borderline-routes, the assertion below fails loud — better
    than a conditional that silently passes when the dispatch never runs.
    """
    from workflows.quality_gate import score_prospect

    inbox, outbox = dispatch_env
    _drive_outbox_once(
        inbox,
        outbox,
        raw_text='{"pass": true, "icp_lane": 2, "rationale": "borderline yes"}',
    )

    # Prospect data engineered to land in the 40-75 borderline band
    # (score=64 with the current scorer weights).
    result = score_prospect(
        {
            "name": "Test Person",
            "title": "Operations Manager",
            "company": "MidCo Manufacturing",
            "location": "Bogotá",
            "employee_count": 500,
            "linkedin_url": "https://linkedin.com/in/test",
        },
        use_llm_dispatch=True,
    )
    # Unconditional assertions — if scoring weights drift such that this
    # prospect no longer borderline-routes, this test fails loud.
    assert result.get("verdict_path", "").startswith("borderline"), (
        f"Test prospect was expected to land in borderline band but "
        f"scored verdict_path={result.get('verdict_path')!r} "
        f"(score={result.get('score')!r}). Adjust the fixture so this "
        f"test still exercises the dispatch path."
    )
    assert result["pass"] is True
    assert result["icp_lane"] == 2
    assert "borderline yes" in (result.get("llm_rationale") or "")


def test_quality_gate_borderline_dispatch_timeout_routes_llm_error(
    dispatch_env, monkeypatch
):
    """Borderline + dispatch timeout → verdict_path='borderline_llm_error',
    NOT a silent reject. Per silent-failure-hunter HIGH: operators must
    be able to spot transient Haiku failures and re-qualify, rather than
    treat them as deterministic rejections."""
    from workflows.quality_gate import score_prospect

    # Force timeout fast — no worker drives the outbox.
    monkeypatch.setattr("workflows.llm_dispatch.DEFAULT_TIMEOUT_S", 0.1)

    result = score_prospect(
        {
            "name": "Test Person",
            "title": "Operations Manager",
            "company": "MidCo Manufacturing",
            "location": "Bogotá",
            "employee_count": 500,
            "linkedin_url": "https://linkedin.com/in/test",
        },
        use_llm_dispatch=True,
    )
    assert result["verdict_path"] == "borderline_llm_error"
    assert "LLM qualifier error" in (result.get("llm_rationale") or "")
    # The deterministic-threshold fallback decides `pass` (score=64 >=60).
    assert result["pass"] is True


def test_quality_gate_borderline_cost_exhausted_routes_distinct_path(
    dispatch_env, monkeypatch
):
    """Borderline + cost-ceiling exhausted → verdict_path=
    'borderline_cost_exhausted', distinct from transient LLM errors.
    Per silent-failure-hunter HIGH + type-design IMPORTANT-4: cost-
    ceiling breaches must surface as their own bucket so operators
    don't conflate them with rate-limit blips."""
    from workflows.llm_dispatch import CostCeilingExhausted
    from workflows.quality_gate import score_prospect

    def _refuse(step, cost, **_kwargs):
        # Wave-2-B fix-up (multi-agent B4): try_reserve now accepts
        # require_attio_ledger=True from request_llm_dispatch. Swallow
        # extras so the mock matches the new sig.
        raise CostCeilingExhausted(step, cap=0.05, consumed=0.06)

    monkeypatch.setattr(
        "workflows.llm_dispatch.LLMBudgetLedger.try_reserve",
        staticmethod(_refuse),
    )

    result = score_prospect(
        {
            "name": "Test Person",
            "title": "Operations Manager",
            "company": "MidCo Manufacturing",
            "location": "Bogotá",
            "employee_count": 500,
            "linkedin_url": "https://linkedin.com/in/test",
        },
        use_llm_dispatch=True,
    )
    assert result["verdict_path"] == "borderline_cost_exhausted"
    assert "cost ceiling exhausted" in (result.get("llm_rationale") or "").lower()
    # Deterministic-threshold fallback decides `pass` (score=64 >=60).
    assert result["pass"] is True


# ── response_classifier.classify_reply_llm ─────────────────────────────


def test_response_classifier_via_dispatch(dispatch_env):
    from workflows.response_classifier import classify_reply_llm

    inbox, outbox = dispatch_env
    _drive_outbox_once(
        inbox,
        outbox,
        raw_text=(
            '{"classification": "defensive", "reasoning": "pushback",'
            ' "defensive_score": 0.85, "engagement_score": 0.1,'
            ' "suggested_action": "stop", "summary": "defensive"}'
        ),
    )
    result = classify_reply_llm(
        opener_text="Hola, una pregunta directa…",
        reply_text="Estoy segura que eso no aplica a nosotros.",
        use_llm_dispatch=True,
    )
    assert result["classification"] == "defensive"
    assert result["source"] == "llm"
    assert result["defensive_score"] == 0.85


def test_response_classifier_dispatch_failure_falls_back_to_keyword(
    dispatch_env, monkeypatch
):
    """A dispatch timeout falls back to the keyword classifier — same
    contract as the legacy 'transient API error' path."""
    from workflows.response_classifier import classify_reply_llm

    monkeypatch.setattr("workflows.llm_dispatch.DEFAULT_TIMEOUT_S", 0.1)
    # Don't drive the outbox; let it time out.
    result = classify_reply_llm(
        opener_text="Hello",
        reply_text="No me interesa, gracias.",
        use_llm_dispatch=True,
    )
    assert result["source"] == "keyword"
    assert "dispatch failed" in result["reasoning"]


# ── industry_classifier.classify_industry ──────────────────────────────


def test_industry_classifier_via_dispatch(dispatch_env):
    from workflows.industry_classifier import classify_industry

    inbox, outbox = dispatch_env
    _drive_outbox_once(inbox, outbox, raw_text="Food & Beverage")

    result = classify_industry(
        "Acme Foods",
        domain="acme-foods.com",
        use_llm_dispatch=True,
    )
    # Depends on INDUSTRY_LABELS containing "Food & Beverage" (it does
    # in the current production catalog). The normalizer matches the
    # exact string in INDUSTRY_LABELS.keys().
    from models.campaign import INDUSTRY_LABELS

    valid = set(INDUSTRY_LABELS.keys())
    if "Food & Beverage" in valid:
        assert result == "Food & Beverage"
    else:
        # Label not in catalog → normalizer returns None
        assert result is None


def test_industry_classifier_dispatch_timeout_returns_none(dispatch_env, monkeypatch):
    """A dispatch timeout returns None (NOT 'Other') so the scorer
    doesn't penalize the company. Preserves the None-vs-Other contract."""
    from workflows.industry_classifier import classify_industry

    monkeypatch.setattr("workflows.llm_dispatch.DEFAULT_TIMEOUT_S", 0.1)
    result = classify_industry("Some Co", use_llm_dispatch=True)
    assert result is None


# ── synthetic_prescreen.score_variant_against_persona ───────────────────


def test_synthetic_prescreen_via_dispatch(dispatch_env):
    from workflows.synthetic_prescreen import (
        load_synthetic_personas,
        score_variant_against_persona,
    )

    inbox, outbox = dispatch_env
    _drive_outbox_once(
        inbox,
        outbox,
        raw_text=(
            '{"defensive_likelihood": [0.2, 0.4], '
            '"engagement_likelihood": [0.5, 0.7], '
            '"reasoning": "neutral, slight curiosity"}'
        ),
    )
    personas = load_synthetic_personas()
    persona_key = next(iter(personas.keys()))
    result = score_variant_against_persona(
        variant_text="Una pregunta directa sobre su proceso de producción.",
        persona_key=persona_key,
        use_llm_dispatch=True,
    )
    assert result["defensive_likelihood"] == [0.2, 0.4]
    assert result["engagement_likelihood"] == [0.5, 0.7]


def test_synthetic_prescreen_raises_when_no_client_no_dispatch():
    """The deterministic invariant: synthetic_prescreen has no
    keyword/None fallback. With no client AND dispatch disabled, it
    MUST raise — silently returning a fabricated score would let a
    reactance-inducing variant slip into a weekly cohort."""
    from workflows.synthetic_prescreen import (
        load_synthetic_personas,
        score_variant_against_persona,
    )

    personas = load_synthetic_personas()
    persona_key = next(iter(personas.keys()))
    with pytest.raises(RuntimeError, match="dispatch disabled"):
        score_variant_against_persona(
            variant_text="opener",
            persona_key=persona_key,
            use_llm_dispatch=False,
        )


# ── env var trigger ──────────────────────────────────────────────────────


def test_response_classifier_env_var_enables_dispatch(dispatch_env, monkeypatch):
    """OUTBOUND_USE_LLM_DISPATCH=1 is sufficient to route classify_reply_llm
    through dispatch — no caller code change needed in production."""
    from workflows.response_classifier import classify_reply_llm

    monkeypatch.setenv("OUTBOUND_USE_LLM_DISPATCH", "1")
    inbox, outbox = dispatch_env
    _drive_outbox_once(
        inbox,
        outbox,
        raw_text=(
            '{"classification": "positive", "reasoning": "interest",'
            ' "defensive_score": 0.1, "engagement_score": 0.8,'
            ' "suggested_action": "book", "summary": "positive"}'
        ),
    )
    result = classify_reply_llm(
        opener_text="Hola",
        reply_text="¡Interesado! Agendemos.",
        # Note: use_llm_dispatch=False (default) — env var is the trigger.
    )
    assert result["source"] == "llm"
    assert result["classification"] == "positive"
