"""Tests for PR-25: IndustryVerdict dataclass + classify_with_confidence.

Tests are organized into the spec-mandated cases:
- IndustryVerdict dataclass (frozen, hashable, shape)
- classify_with_confidence: confirmed / low_confidence / unknown paths
- Threshold edge: confidence == 0.7 → "confirmed" (≥ not >)
- cost_usd_estimated is Decimal, value > 0
- quality_gate._industry_score status-awareness
- quality_gate.score_prospect passes status when available
- Schema delta yaml has both entries under `object: companies`
- Writer registry maps both attrs under "companies" key
- ReclassificationRunWriter integration
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from workflows.industry_classifier import (
    HAIKU_COST_PER_CALL_USD,
    IndustryVerdict,
    classify_with_confidence,
)
from workflows.quality_gate import _industry_score, score_prospect

# ── Helpers ──────────────────────────────────────────────────────────────────

def _mock_confidence_response(label: str, confidence: float, reasoning: str = "test") -> MagicMock:
    """Build a mock Anthropic response with JSON confidence payload."""
    payload = json.dumps({"label": label, "confidence": confidence, "reasoning": reasoning})
    block = MagicMock()
    block.type = "text"
    block.text = payload
    resp = MagicMock()
    resp.content = [block]
    return resp


def _confidence_client(label: str, confidence: float, reasoning: str = "test") -> MagicMock:
    """Return a mock anthropic_client for classify_with_confidence tests."""
    client = MagicMock()
    client.messages.create.return_value = _mock_confidence_response(label, confidence, reasoning)
    return client


# ── IndustryVerdict dataclass ─────────────────────────────────────────────────

class TestIndustryVerdictDataclass:
    def test_instantiation_confirmed(self):
        v = IndustryVerdict(
            status="confirmed",
            confidence=0.9,
            vertical="Automotive",
            reasoning="Vehicle manufacturer",
            model_used="claude-haiku-4-5-20251001",
            cost_usd_estimated=Decimal("0.0001"),
        )
        assert v.status == "confirmed"
        assert v.confidence == 0.9
        assert v.vertical == "Automotive"
        assert v.reasoning == "Vehicle manufacturer"

    def test_instantiation_unknown(self):
        v = IndustryVerdict(
            status="unknown",
            confidence=0.0,
            vertical=None,
            reasoning=None,
            model_used="claude-haiku-4-5-20251001",
            cost_usd_estimated=Decimal("0"),
        )
        assert v.status == "unknown"
        assert v.vertical is None
        assert v.reasoning is None

    def test_instantiation_low_confidence(self):
        v = IndustryVerdict(
            status="low_confidence",
            confidence=0.5,
            vertical="Chemicals",
            reasoning="Maybe chemicals",
            model_used="claude-haiku-4-5-20251001",
            cost_usd_estimated=Decimal("0.0001"),
        )
        assert v.status == "low_confidence"
        assert v.vertical == "Chemicals"

    def test_frozen_immutable(self):
        v = IndustryVerdict(
            status="confirmed",
            confidence=0.9,
            vertical="Mining",
            reasoning="Test",
            model_used="claude-haiku-4-5-20251001",
            cost_usd_estimated=Decimal("0.0001"),
        )
        with pytest.raises((AttributeError, TypeError)):
            v.status = "unknown"  # type: ignore[misc]

    def test_hashable(self):
        v = IndustryVerdict(
            status="confirmed",
            confidence=0.9,
            vertical="Mining",
            reasoning="Test",
            model_used="claude-haiku-4-5-20251001",
            cost_usd_estimated=Decimal("0.0001"),
        )
        # Should be usable as a set/dict key without TypeError
        s = {v}
        assert v in s

    def test_cost_usd_estimated_is_decimal(self):
        v = IndustryVerdict(
            status="confirmed",
            confidence=0.9,
            vertical="Pharma",
            reasoning="Pharma company",
            model_used="claude-haiku-4-5-20251001",
            cost_usd_estimated=HAIKU_COST_PER_CALL_USD,
        )
        assert isinstance(v.cost_usd_estimated, Decimal)
        assert v.cost_usd_estimated > Decimal("0")

    def test_haiku_cost_constant_is_decimal(self):
        assert isinstance(HAIKU_COST_PER_CALL_USD, Decimal)
        assert HAIKU_COST_PER_CALL_USD > 0


# ── classify_with_confidence: confirmed path ─────────────────────────────────

class TestClassifyWithConfidenceConfirmed:
    def test_high_confidence_returns_confirmed(self):
        """LLM returns valid label with confidence ≥ 0.7 → status=confirmed."""
        client = _confidence_client("Automotive", 0.9)
        verdict = classify_with_confidence("Toyota", anthropic_client=client)
        assert verdict.status == "confirmed"
        assert verdict.vertical == "Automotive"
        assert verdict.confidence == 0.9

    def test_threshold_boundary_exactly_0_7_is_confirmed(self):
        """confidence == 0.7 → confirmed (uses ≥, not >)."""
        client = _confidence_client("Food & Beverage", 0.7)
        verdict = classify_with_confidence(
            "Bimbo", anthropic_client=client, confidence_threshold=0.7
        )
        assert verdict.status == "confirmed"

    def test_case_insensitive_label_normalization(self):
        """LLM returns 'automotive' (lowercase) → normalized to 'Automotive'."""
        client = _confidence_client("automotive", 0.85)
        verdict = classify_with_confidence("Toyota", anthropic_client=client)
        assert verdict.status == "confirmed"
        assert verdict.vertical == "Automotive"

    def test_cost_usd_estimated_populated(self):
        """Confirmed verdict has cost_usd_estimated > 0."""
        client = _confidence_client("Pharma", 0.9)
        verdict = classify_with_confidence("Pfizer", anthropic_client=client)
        assert isinstance(verdict.cost_usd_estimated, Decimal)
        assert verdict.cost_usd_estimated > Decimal("0")

    def test_reasoning_is_populated(self):
        client = _confidence_client("Chemicals", 0.88, reasoning="Chemical manufacturer")
        verdict = classify_with_confidence("BASF", anthropic_client=client)
        assert verdict.reasoning == "Chemical manufacturer"

    def test_model_used_is_haiku(self):
        from workflows.industry_classifier import CLASSIFIER_MODEL
        client = _confidence_client("Mining", 0.9)
        verdict = classify_with_confidence("Codelco", anthropic_client=client)
        assert verdict.model_used == CLASSIFIER_MODEL

    def test_domain_hint_passed_to_prompt(self):
        client = _confidence_client("Food & Beverage", 0.9)
        classify_with_confidence("Bimbo", domain="bimbo.com", anthropic_client=client)
        call_args = client.messages.create.call_args
        messages = call_args.kwargs["messages"]
        assert "Domain: bimbo.com" in messages[0]["content"]

    def test_no_domain_hint_omitted_from_prompt(self):
        client = _confidence_client("Packaging", 0.9)
        classify_with_confidence("SealedAir", domain=None, anthropic_client=client)
        call_args = client.messages.create.call_args
        messages = call_args.kwargs["messages"]
        assert "Domain:" not in messages[0]["content"]


# ── classify_with_confidence: low_confidence path ────────────────────────────

class TestClassifyWithConfidenceLowConfidence:
    def test_below_threshold_returns_low_confidence(self):
        """LLM confidence < 0.7 → status=low_confidence, vertical still populated."""
        client = _confidence_client("Chemicals", 0.5)
        with patch("workflows.industry_classifier._emit_industry_low_confidence"):
            verdict = classify_with_confidence("Unclear Corp", anthropic_client=client)
        assert verdict.status == "low_confidence"
        assert verdict.vertical == "Chemicals"  # LLM's best guess preserved
        assert verdict.confidence == 0.5

    def test_low_confidence_emits_queue_row(self):
        """Low confidence triggers escalation queue emit."""
        client = _confidence_client("Textiles", 0.45, reasoning="Might be textiles")
        with patch("workflows.industry_classifier._emit_industry_low_confidence") as mock_emit:
            verdict = classify_with_confidence("Fuzzy Corp", anthropic_client=client)
        assert verdict.status == "low_confidence"
        mock_emit.assert_called_once()
        args = mock_emit.call_args
        assert args[0][0] == "Fuzzy Corp"  # company_name
        assert args[0][1] == "Textiles"     # suggested_vertical
        assert args[0][2] == pytest.approx(0.45)

    def test_custom_threshold_respected(self):
        """threshold kwarg overrides the default 0.7."""
        client = _confidence_client("Mining", 0.75)
        with patch("workflows.industry_classifier._emit_industry_low_confidence") as mock_emit:
            verdict = classify_with_confidence(
                "CoalCo", anthropic_client=client, confidence_threshold=0.8
            )
        assert verdict.status == "low_confidence"
        mock_emit.assert_called_once()

    def test_low_confidence_just_below_threshold(self):
        """confidence = 0.699 (just below 0.7) → low_confidence."""
        client = _confidence_client("Oil & Gas", 0.699)
        with patch("workflows.industry_classifier._emit_industry_low_confidence"):
            verdict = classify_with_confidence("OilCo", anthropic_client=client)
        assert verdict.status == "low_confidence"


# ── classify_with_confidence: unknown path ───────────────────────────────────

class TestClassifyWithConfidenceUnknown:
    def test_empty_company_name_returns_unknown(self):
        """Empty company_name → unknown, no LLM call, cost=Decimal('0')."""
        client = MagicMock()
        verdict = classify_with_confidence("", anthropic_client=client)
        assert verdict.status == "unknown"
        assert verdict.vertical is None
        assert verdict.confidence == 0.0
        # cost is Decimal("0") — fold-in convergence (pr-test + math-QA):
        # any code path that copies HAIKU_COST_PER_CALL_USD into _unknown
        # would silently inflate the F-PR-9 budget ledger for no-op calls.
        from decimal import Decimal
        assert verdict.cost_usd_estimated == Decimal("0")
        client.messages.create.assert_not_called()

    def test_none_company_name_returns_unknown(self):
        """None company_name → unknown, no LLM call."""
        client = MagicMock()
        verdict = classify_with_confidence(None, anthropic_client=client)
        assert verdict.status == "unknown"
        assert verdict.vertical is None
        client.messages.create.assert_not_called()

    def test_invalid_label_returns_unknown(self):
        """LLM returns a label not in INDUSTRY_LABELS → unknown."""
        client = _confidence_client("Robotics", 0.9)
        verdict = classify_with_confidence("FANUC", anthropic_client=client)
        assert verdict.status == "unknown"
        assert verdict.vertical is None

    def test_api_error_returns_unknown(self):
        """LLM API exception → unknown (not 'Other'), cost=Decimal('0')."""
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("api down")
        verdict = classify_with_confidence("Bimbo", anthropic_client=client)
        assert verdict.status == "unknown"
        assert verdict.vertical is None
        from decimal import Decimal
        assert verdict.cost_usd_estimated == Decimal("0")

    def test_json_parse_error_returns_unknown(self):
        """LLM returns non-JSON response → unknown."""
        block = MagicMock()
        block.type = "text"
        block.text = "Automotive"  # plain text, not JSON
        resp = MagicMock()
        resp.content = [block]
        client = MagicMock()
        client.messages.create.return_value = resp
        verdict = classify_with_confidence("Toyota", anthropic_client=client)
        assert verdict.status == "unknown"

    def test_dispatch_not_enabled_returns_unknown(self, monkeypatch):
        """No dispatch + no client → unknown (not a crash)."""
        from workflows import llm_dispatch as ld
        monkeypatch.setattr(ld, "is_dispatch_enabled", lambda: False)
        verdict = classify_with_confidence("Bimbo")
        assert verdict.status == "unknown"

    def test_dispatch_timeout_returns_unknown(self, monkeypatch):
        """LLMDispatchTimeout → unknown."""
        from workflows import llm_dispatch as ld
        monkeypatch.setattr(ld, "is_dispatch_enabled", lambda: True)

        def _raise_timeout(**kwargs):
            raise ld.LLMDispatchTimeout("industry_classification_confidence", "abc", 300.0)

        monkeypatch.setattr(ld, "request_llm_dispatch", _raise_timeout)
        verdict = classify_with_confidence("Bimbo", use_llm_dispatch=True)
        assert verdict.status == "unknown"
        assert verdict.vertical is None

    def test_dispatch_failed_returns_unknown(self, monkeypatch):
        """LLMDispatchFailed → unknown."""
        from workflows import llm_dispatch as ld
        monkeypatch.setattr(ld, "is_dispatch_enabled", lambda: True)

        def _raise_failed(**kwargs):
            raise ld.LLMDispatchFailed("industry_classification_confidence", "abc", "boom")

        monkeypatch.setattr(ld, "request_llm_dispatch", _raise_failed)
        verdict = classify_with_confidence("Bimbo", use_llm_dispatch=True)
        assert verdict.status == "unknown"

    def test_cost_ceiling_exhausted_returns_unknown(self, monkeypatch):
        """CostCeilingExhausted → unknown + cost_ceiling_breached queue emitted."""
        from workflows import llm_dispatch as ld
        monkeypatch.setattr(ld, "is_dispatch_enabled", lambda: True)

        def _raise_ceiling(**kwargs):
            raise ld.CostCeilingExhausted(
                step="industry_classification_confidence",
                cap=0.50,
                consumed=0.51,
            )

        monkeypatch.setattr(ld, "request_llm_dispatch", _raise_ceiling)
        with patch("workflows.industry_classifier._emit_cost_ceiling_breached") as mock_emit:
            verdict = classify_with_confidence("Bimbo", use_llm_dispatch=True)
        assert verdict.status == "unknown"
        mock_emit.assert_called_once()

    def test_dispatch_path_confirmed(self, monkeypatch):
        """Dispatch path (no client) returns confirmed verdict on valid JSON response."""
        from workflows import llm_dispatch as ld
        from workflows.llm_dispatch import LLMDispatchResult
        monkeypatch.setattr(ld, "is_dispatch_enabled", lambda: True)

        payload = json.dumps({"label": "Automotive", "confidence": 0.9, "reasoning": "Cars"})
        mock_result = LLMDispatchResult(
            dispatch_id="test-dispatch-id",
            success=True,
            raw_text=payload,
        )

        def _mock_dispatch(**kwargs):
            return mock_result

        monkeypatch.setattr(ld, "request_llm_dispatch", _mock_dispatch)
        verdict = classify_with_confidence("Toyota", use_llm_dispatch=True)
        assert verdict.status == "confirmed"
        assert verdict.vertical == "Automotive"
        assert verdict.confidence == pytest.approx(0.9)


# ── quality_gate._industry_score status awareness ────────────────────────────

class TestIndustryScoreStatusAware:
    def test_confirmed_in_icp_returns_bonus(self):
        """status=confirmed + in-ICP industry → +12 bonus."""
        delta, reason = _industry_score("Food & Beverage", status="confirmed")
        assert delta == 12
        assert "In-ICP" in reason

    def test_confirmed_off_icp_returns_penalty(self):
        """status=confirmed + Other → -25 penalty."""
        delta, reason = _industry_score("Other", status="confirmed")
        assert delta == -25
        assert "Off-ICP" in reason

    def test_low_confidence_returns_zero_regardless_of_label(self):
        """status=low_confidence → (0, abstain reason) even for in-ICP label."""
        delta, reason = _industry_score("Food & Beverage", status="low_confidence")
        assert delta == 0
        assert "abstained" in reason.lower()
        assert "low_confidence" in reason

    def test_unknown_status_returns_zero(self):
        """status=unknown → (0, abstain reason)."""
        delta, reason = _industry_score("Other", status="unknown")
        assert delta == 0
        assert "abstained" in reason.lower()
        assert "unknown" in reason

    def test_default_status_confirmed_preserves_backcompat(self):
        """Default status='confirmed' preserves pre-PR-25 behavior."""
        # No status kwarg — should behave as before
        delta_default, _ = _industry_score("Automotive")
        delta_explicit, _ = _industry_score("Automotive", status="confirmed")
        assert delta_default == delta_explicit

    def test_none_industry_with_confirmed_is_neutral(self):
        """None industry + confirmed → neutral (pre-PR-25 behavior preserved)."""
        delta, reason = _industry_score(None, status="confirmed")
        assert delta == 0
        assert reason == ""

    def test_empty_industry_with_confirmed_is_neutral(self):
        delta, reason = _industry_score("", status="confirmed")
        assert delta == 0

    def test_low_confidence_other_does_not_penalize(self):
        """Low-confidence 'Other' must NOT apply the -25 penalty (§0 #9)."""
        delta, reason = _industry_score("Other", status="low_confidence")
        assert delta == 0, "low_confidence Other must not apply the -25 off-ICP penalty"


# ── quality_gate.score_prospect passes industry_vertical_status ──────────────

class TestScoreProspectIndustryStatus:
    _BASE = {
        "name": "Test Person",
        "title": "Director de Manufactura",
        "company": "TestCorp",
        "location": "Mexico",
        "employee_count": 5000,
    }

    def test_no_status_defaults_to_confirmed_behavior(self):
        """Without industry_vertical_status, confirmed behavior (pre-PR-25 compat)."""
        data = {**self._BASE, "industry": "Food & Beverage"}
        result = score_prospect(data)
        breakdown = result["score_breakdown"]
        # industry component should be +12
        assert breakdown["industry"] == 12

    def test_confirmed_status_applies_bonus(self):
        data = {
            **self._BASE,
            "industry": "Food & Beverage",
            "industry_vertical_status": "confirmed",
        }
        result = score_prospect(data)
        assert result["score_breakdown"]["industry"] == 12

    def test_low_confidence_status_neutralizes_bonus(self):
        """industry_vertical_status=low_confidence → industry delta = 0."""
        data = {
            **self._BASE,
            "industry": "Food & Beverage",
            "industry_vertical_status": "low_confidence",
        }
        result = score_prospect(data)
        assert result["score_breakdown"]["industry"] == 0

    def test_unknown_status_neutralizes_penalty(self):
        """industry_vertical_status=unknown + 'Other' → delta = 0, no -25 penalty."""
        data = {
            **self._BASE,
            "industry": "Other",
            "industry_vertical_status": "unknown",
        }
        result = score_prospect(data)
        assert result["score_breakdown"]["industry"] == 0

    def test_low_confidence_reason_mentions_abstained(self):
        data = {
            **self._BASE,
            "industry": "Automotive",
            "industry_vertical_status": "low_confidence",
        }
        result = score_prospect(data)
        reasons_text = " ".join(result["reasons"])
        assert "abstained" in reasons_text.lower()


# ── Schema delta YAML: object must be 'companies' ────────────────────────────

class TestSchemaDeltaObjectIsCompanies:
    MANIFEST_PATH = (
        Path(__file__).resolve().parent.parent / "docs" / "attio_schema_deltas.yaml"
    )

    def _load_manifest(self) -> dict:
        return yaml.safe_load(self.MANIFEST_PATH.read_text()) or {}

    def test_industry_vertical_status_is_on_companies(self):
        manifest = self._load_manifest()
        entries = manifest.get("attributes", [])
        pr25_status = [
            e for e in entries
            if e.get("slug") == "industry_vertical_status" and e.get("pr_id") == "PR-25"
        ]
        assert len(pr25_status) == 1, "Expected exactly one PR-25 industry_vertical_status entry"
        assert pr25_status[0]["object"] == "companies", (
            f"Expected object=companies, got {pr25_status[0]['object']!r} — "
            "PR-25 spec (plan §5 line 733) puts industry attrs on companies, not linkedin_outreach"
        )

    def test_industry_vertical_confidence_is_on_companies(self):
        manifest = self._load_manifest()
        entries = manifest.get("attributes", [])
        pr25_conf = [
            e for e in entries
            if e.get("slug") == "industry_vertical_confidence" and e.get("pr_id") == "PR-25"
        ]
        assert len(pr25_conf) == 1
        assert pr25_conf[0]["object"] == "companies"

    def test_no_linkedin_outreach_pr25_entries_remain(self):
        """Confirm the old linkedin_outreach entries for PR-25 were removed."""
        manifest = self._load_manifest()
        entries = manifest.get("attributes", [])
        bad = [
            e for e in entries
            if e.get("pr_id") == "PR-25" and e.get("object") == "linkedin_outreach"
        ]
        assert bad == [], (
            f"Found PR-25 entries still on linkedin_outreach: {bad} — "
            "these should have been updated to companies"
        )

    def test_industry_vertical_status_options(self):
        manifest = self._load_manifest()
        entries = manifest.get("attributes", [])
        entry = next(
            (e for e in entries
             if e.get("slug") == "industry_vertical_status" and e.get("pr_id") == "PR-25"),
            None,
        )
        assert entry is not None
        assert set(entry["options"]) == {"confirmed", "unknown", "low_confidence"}


# ── Writer registry: object must be 'companies' ───────────────────────────────

class TestWriterRegistryCompaniesKey:
    def test_industry_vertical_status_registered_on_companies(self):
        from clients.attio_writer_registry import WRITE_OWNER_REGISTRY
        assert ("companies", "industry_vertical_status") in WRITE_OWNER_REGISTRY, (
            "Expected ('companies', 'industry_vertical_status') in WRITE_OWNER_REGISTRY — "
            "PR-25 moved this from linkedin_outreach to companies"
        )

    def test_industry_vertical_confidence_registered_on_companies(self):
        from clients.attio_writer_registry import WRITE_OWNER_REGISTRY
        assert ("companies", "industry_vertical_confidence") in WRITE_OWNER_REGISTRY

    def test_old_linkedin_outreach_entries_removed(self):
        from clients.attio_writer_registry import WRITE_OWNER_REGISTRY
        assert ("linkedin_outreach", "industry_vertical_status") not in WRITE_OWNER_REGISTRY, (
            "linkedin_outreach entry for industry_vertical_status should be removed (PR-25)"
        )
        assert ("linkedin_outreach", "industry_vertical_confidence") not in WRITE_OWNER_REGISTRY

    def test_write_owner_points_to_classify_with_confidence(self):
        from clients.attio_writer_registry import WRITE_OWNER_REGISTRY
        owner = WRITE_OWNER_REGISTRY[("companies", "industry_vertical_status")]
        owner_str = owner if isinstance(owner, str) else " ".join(owner)
        assert "classify_with_confidence" in owner_str


# ── ReclassificationRunWriter integration ────────────────────────────────────

class TestReclassificationRunWriterIntegration:
    """Verify the classify_with_confidence batch path uses ReclassificationRunWriter
    following the F-PR-3.7 usage pattern (docstring lines 22-40)."""

    def test_batch_run_context_manager_calls_examine_and_mark_success(self):
        """Simulate the batch path: confirmed verdicts → mark_success."""
        from workflows.reclassification_run_writer import ReclassificationRunWriter

        writer = MagicMock(spec=ReclassificationRunWriter)
        writer.__enter__ = MagicMock(return_value=writer)
        writer.__exit__ = MagicMock(return_value=False)
        writer.confidence_threshold = 0.7

        client = _confidence_client("Automotive", 0.9)

        with writer as run:
            run.examine()
            verdict = classify_with_confidence("Toyota", anthropic_client=client)
            if verdict.status == "confirmed":
                run.mark_success(record_id="rec-toyota", value=verdict.vertical)
            else:
                run.mark_abstain(record_id="rec-toyota", reason=verdict.status)

        writer.examine.assert_called_once()
        writer.mark_success.assert_called_once_with(record_id="rec-toyota", value="Automotive")

    def test_batch_run_abstains_on_low_confidence(self):
        """Low-confidence verdicts → mark_abstain."""
        from workflows.reclassification_run_writer import ReclassificationRunWriter

        writer = MagicMock(spec=ReclassificationRunWriter)
        writer.__enter__ = MagicMock(return_value=writer)
        writer.__exit__ = MagicMock(return_value=False)

        client = _confidence_client("Chemicals", 0.4)

        with (
            patch("workflows.industry_classifier._emit_industry_low_confidence"),
            writer as run,
        ):
            run.examine()
            verdict = classify_with_confidence("UnclearCo", anthropic_client=client)
            if verdict.status == "confirmed":
                run.mark_success(record_id="rec-unclear", value=verdict.vertical)
            else:
                run.mark_abstain(record_id="rec-unclear", reason=verdict.status)

        writer.mark_abstain.assert_called_once_with(
            record_id="rec-unclear", reason="low_confidence"
        )
        writer.mark_success.assert_not_called()

    def test_batch_run_abstains_on_unknown(self):
        """Unknown verdicts (empty company) → mark_abstain."""
        from workflows.reclassification_run_writer import ReclassificationRunWriter

        writer = MagicMock(spec=ReclassificationRunWriter)
        writer.__enter__ = MagicMock(return_value=writer)
        writer.__exit__ = MagicMock(return_value=False)

        with writer as run:
            run.examine()
            verdict = classify_with_confidence("", anthropic_client=MagicMock())
            if verdict.status == "confirmed":
                run.mark_success(record_id="rec-empty", value=verdict.vertical)
            else:
                run.mark_abstain(record_id="rec-empty", reason=verdict.status)

        writer.mark_abstain.assert_called_once_with(
            record_id="rec-empty", reason="unknown"
        )


# ── IndustryLowConfidencePayload TypedDict registered ────────────────────────

class TestIndustryLowConfidenceEscalation:
    def test_payload_typeddict_registered_in_escalation_schemas(self):
        from workflows.escalation_schemas import ESCALATION_SCHEMAS
        assert "industry_low_confidence" in ESCALATION_SCHEMAS

    def test_payload_typeddict_has_required_fields(self):
        from workflows.escalation_schemas import ESCALATION_SCHEMAS, IndustryLowConfidencePayload
        assert ESCALATION_SCHEMAS["industry_low_confidence"] is IndustryLowConfidencePayload
        required = IndustryLowConfidencePayload.__required_keys__
        assert "company_name" in required
        assert "suggested_vertical" in required
        assert "confidence" in required
        assert "reasoning" in required
        assert "model_used" in required

    def test_industry_low_confidence_in_escalation_types(self):
        from workflows.escalation_schemas import ESCALATION_TYPES_SET
        assert "industry_low_confidence" in ESCALATION_TYPES_SET


class TestEmitIndustryLowConfidenceExceptionDiscipline:
    """FU-3: `_emit_industry_low_confidence` originally caught every
    `Exception` to keep the main classify flow alive on transient
    escalation failure. That over-caught programmer errors (ImportError,
    TypeError, AttributeError) too — silently demoting real bugs to
    warning lines while the operator queue row never appeared.

    The tightened except re-raises programmer-error exception classes
    and SystemExit/KeyboardInterrupt, while keeping the original
    log-and-continue behavior for transient runtime exceptions.
    """

    def test_import_error_propagates(self):
        """ImportError raised inside the escalate call must propagate
        out of `_emit_industry_low_confidence` rather than being demoted
        to a warning. Locks the FU-3 fix: catching ImportError as a
        generic transient failure used to hide real packaging or
        renaming regressions."""
        from unittest.mock import patch

        from workflows.industry_classifier import _emit_industry_low_confidence

        with patch(
            "workflows.escalation.escalate",
            side_effect=ImportError("simulated programmer error"),
        ), pytest.raises(ImportError, match="simulated programmer error"):
            _emit_industry_low_confidence(
                "Acme Foods", "Food & Beverage", 0.55, "ambiguous",
            )

    def test_type_error_propagates(self):
        """TypeError signals a call-shape regression (caller passing the
        wrong type, escalate signature changing under us). Must surface."""
        from unittest.mock import patch

        from workflows.industry_classifier import _emit_industry_low_confidence

        with patch(
            "workflows.escalation.escalate",
            side_effect=TypeError("escalate() missing positional argument"),
        ), pytest.raises(TypeError, match="missing positional argument"):
            _emit_industry_low_confidence(
                "Acme Foods", "Food & Beverage", 0.55, None,
            )

    def test_attribute_error_propagates(self):
        """AttributeError typically signals a refactor that renamed an
        attribute on a shared object. Must surface."""
        from unittest.mock import patch

        from workflows.industry_classifier import _emit_industry_low_confidence

        with patch(
            "workflows.escalation.escalate",
            side_effect=AttributeError("'AttioClient' has no attribute 'foo'"),
        ), pytest.raises(AttributeError, match="has no attribute"):
            _emit_industry_low_confidence(
                "Acme Foods", "Food & Beverage", 0.55, "",
            )

    def test_runtime_error_still_swallowed_with_warning(self, caplog):
        """The original log-and-continue behavior for transient runtime
        failures (network blip, Attio 5xx, anything that isn't a contract
        violation) must stay intact — escalation failure must NOT abort
        the main classify flow when the verdict itself is still valid."""
        from unittest.mock import patch

        from workflows.industry_classifier import _emit_industry_low_confidence

        with patch(
            "workflows.escalation.escalate",
            side_effect=RuntimeError("attio 502 bad gateway"),
        ):
            # Should NOT raise — caught by the generic except branch.
            with caplog.at_level("WARNING"):
                _emit_industry_low_confidence(
                    "Acme Foods", "Food & Beverage", 0.55, "",
                )
            # Tightened per QA: assert the full formatted record (level +
            # company name + underlying error) so a regression that drops
            # context fields or downgrades the level is caught.
            matching = [
                rec for rec in caplog.records
                if rec.levelname == "WARNING"
                and "escalation emit failed" in rec.getMessage()
                and "Acme Foods" in rec.getMessage()
                and "attio 502" in rec.getMessage()
            ]
            assert matching, (
                f"expected one WARNING with company name and underlying "
                f"error; got {[(r.levelname, r.getMessage()) for r in caplog.records]}"
            )

    def test_escalation_schema_error_propagates(self):
        """EscalationSchemaError is a TypeError subclass (per
        escalation_schemas.py:871) — payload contract drift must be loud,
        not silently swallowed. Locks the FU-3 review fold-in: the
        programmer-error tier catches and re-raises TypeError, so schema
        drift surfaces."""
        from unittest.mock import patch

        from workflows.escalation_schemas import EscalationSchemaError
        from workflows.industry_classifier import _emit_industry_low_confidence

        with patch(
            "workflows.escalation.escalate",
            side_effect=EscalationSchemaError("payload missing required field"),
        ), pytest.raises(EscalationSchemaError, match="missing required field"):
            _emit_industry_low_confidence(
                "Acme Foods", "Food & Beverage", 0.55, "",
            )

    def test_unknown_escalation_type_propagates(self):
        """UnknownEscalationType is loud-by-design per its docstring —
        silently dropping an escalation with an unknown type violates
        §3 hard constraint #9. Locks the FU-3 review fold-in."""
        from unittest.mock import patch

        from workflows.escalation_schemas import UnknownEscalationType
        from workflows.industry_classifier import _emit_industry_low_confidence

        with patch(
            "workflows.escalation.escalate",
            side_effect=UnknownEscalationType("type='bogus' not in ESCALATION_TYPES"),
        ), pytest.raises(UnknownEscalationType, match="not in ESCALATION_TYPES"):
            _emit_industry_low_confidence(
                "Acme Foods", "Food & Beverage", 0.55, "",
            )

    def test_missing_decision_key_propagates(self):
        """MissingDecisionKey is loud-by-design per its docstring — same
        rationale as UnknownEscalationType. Locks the FU-3 review fold-in."""
        from unittest.mock import patch

        from workflows.escalation_schemas import MissingDecisionKey
        from workflows.industry_classifier import _emit_industry_low_confidence

        with patch(
            "workflows.escalation.escalate",
            side_effect=MissingDecisionKey("decision_key required"),
        ), pytest.raises(MissingDecisionKey, match="decision_key required"):
            _emit_industry_low_confidence(
                "Acme Foods", "Food & Beverage", 0.55, "",
            )


class TestEmitCostCeilingBreachedExceptionDiscipline:
    """W2A-2: ``_emit_cost_ceiling_breached`` is the sibling of
    ``_emit_industry_low_confidence`` and originally caught every
    ``Exception`` to keep the main classify flow alive on transient
    escalation failure. That over-caught programmer errors (ImportError,
    TypeError, AttributeError) too — silently demoting real bugs to
    warning lines while the cost-ceiling queue row never appeared.

    Mirrors the FU-3 (PR #111) tightening on ``_emit_industry_low_confidence``
    verbatim: programmer errors and loud-by-design escalation contract
    violations re-raise; only transient runtime exceptions log-and-continue.
    """

    @staticmethod
    def _err() -> Exception:
        """A minimal stand-in for ``CostCeilingExhausted`` — only the
        ``cap`` / ``consumed`` attributes are read by the emit helper.
        Plain ``Exception`` so the test doesn't depend on the dispatch
        module's public surface."""
        err = Exception("budget exhausted")
        err.cap = 1.0
        err.consumed = 1.25
        return err

    def test_import_error_propagates(self):
        """ImportError raised inside the escalate call must propagate
        out of `_emit_cost_ceiling_breached` rather than being demoted
        to a warning. A missing module is a packaging / refactor regression,
        not a transient escalation failure."""
        from unittest.mock import patch

        from workflows.industry_classifier import _emit_cost_ceiling_breached

        with patch(
            "workflows.escalation.escalate",
            side_effect=ImportError("simulated programmer error"),
        ), pytest.raises(ImportError, match="simulated programmer error"):
            _emit_cost_ceiling_breached("Acme Foods", self._err())

    def test_type_error_propagates(self):
        """TypeError signals a call-shape regression (caller passing the
        wrong type, escalate signature changing under us). Must surface.
        Also covers EscalationSchemaError (a TypeError subclass) — payload
        contract drift must be loud, not silently swallowed."""
        from unittest.mock import patch

        from workflows.industry_classifier import _emit_cost_ceiling_breached

        with patch(
            "workflows.escalation.escalate",
            side_effect=TypeError("escalate() missing positional argument"),
        ), pytest.raises(TypeError, match="missing positional argument"):
            _emit_cost_ceiling_breached("Acme Foods", self._err())

    def test_attribute_error_propagates(self):
        """AttributeError typically signals a refactor that renamed an
        attribute on a shared object. Must surface."""
        from unittest.mock import patch

        from workflows.industry_classifier import _emit_cost_ceiling_breached

        with patch(
            "workflows.escalation.escalate",
            side_effect=AttributeError("'AttioClient' has no attribute 'foo'"),
        ), pytest.raises(AttributeError, match="has no attribute"):
            _emit_cost_ceiling_breached("Acme Foods", self._err())

    def test_unknown_escalation_type_propagates(self):
        """UnknownEscalationType is loud-by-design — silently dropping an
        escalation with an unknown type violates §3 hard constraint #9."""
        from unittest.mock import patch

        from workflows.escalation_schemas import UnknownEscalationType
        from workflows.industry_classifier import _emit_cost_ceiling_breached

        with patch(
            "workflows.escalation.escalate",
            side_effect=UnknownEscalationType("type='bogus' not in ESCALATION_TYPES"),
        ), pytest.raises(UnknownEscalationType, match="not in ESCALATION_TYPES"):
            _emit_cost_ceiling_breached("Acme Foods", self._err())

    def test_runtime_error_still_swallowed_with_warning(self, caplog):
        """The original log-and-continue behavior for transient runtime
        failures (network blip, Attio 5xx) must stay intact — escalation
        failure must NOT abort the cost-ceiling flow when the underlying
        verdict (cost exhausted, return _unknown) is still valid."""
        from unittest.mock import patch

        from workflows.industry_classifier import _emit_cost_ceiling_breached

        with patch(
            "workflows.escalation.escalate",
            side_effect=RuntimeError("attio 502 bad gateway"),
        ):
            with caplog.at_level("WARNING"):
                _emit_cost_ceiling_breached("Acme Foods", self._err())
            # Assert the formatted record carries the company name + the
            # underlying error message so a regression that drops context
            # is caught (mirrors the FU-3 fold-in pattern).
            matching = [
                rec for rec in caplog.records
                if rec.levelname == "WARNING"
                and "cost_ceiling_breached escalation failed" in rec.getMessage()
                and "Acme Foods" in rec.getMessage()
                and "attio 502" in rec.getMessage()
            ]
            assert matching, (
                f"expected one WARNING with company name and underlying "
                f"error; got {[(r.levelname, r.getMessage()) for r in caplog.records]}"
            )

    def test_escalation_schema_error_propagates(self):
        """EscalationSchemaError is a TypeError subclass (per
        escalation_schemas.py) — payload contract drift must be loud, not
        silently swallowed. A dedicated test (vs. relying on the TypeError
        case) guards against a future refactor that demotes
        EscalationSchemaError to a non-TypeError ancestor: the subclass
        relationship is load-bearing for the tier shape, and asymmetry
        with the FU-3 sibling class would otherwise invite drift."""
        from unittest.mock import patch

        from workflows.escalation_schemas import EscalationSchemaError
        from workflows.industry_classifier import _emit_cost_ceiling_breached

        with patch(
            "workflows.escalation.escalate",
            side_effect=EscalationSchemaError("payload missing required field"),
        ), pytest.raises(EscalationSchemaError, match="missing required field"):
            _emit_cost_ceiling_breached("Acme Foods", self._err())

    def test_missing_decision_key_propagates(self):
        """MissingDecisionKey is loud-by-design per its docstring — same
        rationale as UnknownEscalationType. The two exceptions share the
        ``except (UnknownEscalationType, MissingDecisionKey)`` tuple, and
        a future refactor that accidentally drops one from the tuple would
        otherwise pass the existing UnknownEscalationType test silently.
        Mirrors ``test_missing_decision_key_propagates`` in the FU-3 sibling
        class."""
        from unittest.mock import patch

        from workflows.escalation_schemas import MissingDecisionKey
        from workflows.industry_classifier import _emit_cost_ceiling_breached

        with patch(
            "workflows.escalation.escalate",
            side_effect=MissingDecisionKey("decision_key required"),
        ), pytest.raises(MissingDecisionKey, match="decision_key required"):
            _emit_cost_ceiling_breached("Acme Foods", self._err())
