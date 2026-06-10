"""Tests for the LLM-based reply classifier.

The classifier's plan contract: `classify_reply_llm(opener_text, reply_text)`
ALWAYS returns a valid dict, falling back to the keyword classifier
on any failure (missing API key, transient error, parse error, invalid
enum). The `source` field distinguishes the two paths.

We mock `anthropic.Anthropic` so these tests never hit the real API.
Classification accuracy is tracked separately via live eval runs against
the synthetic pre-screen (Task 8).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from workflows.response_classifier import (
    CLASSIFIER_MODEL,
    CLASSIFIER_SYSTEM_PROMPT,
    VALID_CLASSIFICATIONS,
    classify_reply_llm,
)

# ── Mock helpers ─────────────────────────────────────────────────────


def _mock_response(text: str):
    """Build a mock Anthropic Messages response with a single text block."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    response = MagicMock()
    response.content = [block]
    return response


def _mock_client_returning(text: str):
    client = MagicMock()
    client.messages.create.return_value = _mock_response(text)
    return client


def _mock_client_raising(exc):
    client = MagicMock()
    client.messages.create.side_effect = exc
    return client


def _valid_llm_json(
    classification: str = "positive",
    defensive_score: float = 0.1,
    engagement_score: float = 0.8,
    reasoning: str = "test",
) -> str:
    return json.dumps({
        "classification": classification,
        "reasoning": reasoning,
        "defensive_score": defensive_score,
        "engagement_score": engagement_score,
    })


PATRICIA_OPENER = (
    "Hola Patricia, gracias por aceptar mi invitación. Una pregunta directa - "
    "en Novo Nordisk cómo llevan la programación de producción? Te pregunto "
    "porque en todas las plantas que visitamos, incluso las que ya tienen "
    "SAP bien montado, eso sigue viviendo en Excel. Les pasa igual?"
)
PATRICIA_REPLY = (
    "Hola Andrés no tenemos plantas de producción en Mexico. Pero estoy "
    "segura que no viven en Excel con toda certeza. Saludos"
)


# ── API key handling (fallback to keyword) ──────────────────────────


class TestApiKeyHandling:
    def test_missing_api_key_falls_back_to_keyword(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        result = classify_reply_llm(PATRICIA_OPENER, PATRICIA_REPLY)
        # Patricia's reply hits the keyword defensive bucket
        assert result["classification"] == "defensive"
        assert result["source"] == "keyword"
        assert "keyword fallback" in result["reasoning"]

    def test_empty_api_key_falls_back(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        result = classify_reply_llm("any opener", "No gracias, no me interesa.")
        assert result["source"] == "keyword"
        assert result["classification"] == "negative"

    def test_whitespace_api_key_falls_back(self, monkeypatch):
        """A stray whitespace .env shouldn't trigger a real API call."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
        result = classify_reply_llm("any opener", "Interesado! Agendemos.")
        assert result["source"] == "keyword"
        assert result["classification"] == "positive"


# ── LLM happy-path classification ───────────────────────────────────


class TestLlmHappyPath:
    @pytest.fixture(autouse=True)
    def _set_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    def test_defensive_envelope(self):
        client = _mock_client_returning(_valid_llm_json(
            classification="defensive",
            defensive_score=0.92,
            engagement_score=0.05,
            reasoning="Refutes premise",
        ))
        result = classify_reply_llm(
            PATRICIA_OPENER, PATRICIA_REPLY, anthropic_client=client
        )
        assert result["classification"] == "defensive"
        assert result["defensive_score"] == 0.92
        assert result["engagement_score"] == 0.05
        assert result["source"] == "llm"
        assert "Stop automated sequence" in result["suggested_action"]

    def test_positive_envelope(self):
        client = _mock_client_returning(_valid_llm_json(
            classification="positive", defensive_score=0.0, engagement_score=0.9,
        ))
        result = classify_reply_llm(
            opener_text="any opener",
            reply_text="Me interesa, agendemos demo.",
            anthropic_client=client,
        )
        assert result["classification"] == "positive"
        assert result["source"] == "llm"
        assert "diagnosis call" in result["suggested_action"]

    def test_all_five_labels_round_trip(self):
        for label in VALID_CLASSIFICATIONS:
            client = _mock_client_returning(_valid_llm_json(classification=label))
            result = classify_reply_llm("o", "r", anthropic_client=client)
            assert result["classification"] == label
            assert result["source"] == "llm"


# ── LLM fallback paths ──────────────────────────────────────────────


class TestFallbackPaths:
    @pytest.fixture(autouse=True)
    def _set_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    def test_transient_rate_limit_falls_back(self):
        class RateLimitError(Exception):
            pass
        client = _mock_client_raising(RateLimitError("slow down"))
        result = classify_reply_llm(
            PATRICIA_OPENER, PATRICIA_REPLY, anthropic_client=client
        )
        assert result["source"] == "keyword"
        assert result["classification"] == "defensive"

    def test_timeout_falls_back(self):
        client = _mock_client_raising(TimeoutError("took too long"))
        result = classify_reply_llm(
            PATRICIA_OPENER, PATRICIA_REPLY, anthropic_client=client
        )
        assert result["source"] == "keyword"

    def test_non_transient_exception_raises(self):
        """A RuntimeError/ValueError from a bug is NOT silently caught."""
        client = _mock_client_raising(RuntimeError("programmer bug"))
        with pytest.raises(RuntimeError, match="programmer bug"):
            classify_reply_llm(
                PATRICIA_OPENER, PATRICIA_REPLY, anthropic_client=client
            )

    def test_malformed_json_falls_back(self):
        client = _mock_client_returning("this is not json at all")
        result = classify_reply_llm(
            PATRICIA_OPENER, PATRICIA_REPLY, anthropic_client=client
        )
        assert result["source"] == "keyword"
        assert result["classification"] == "defensive"  # keyword catches Patricia

    def test_invalid_classification_label_falls_back(self):
        client = _mock_client_returning(_valid_llm_json(classification="wibble"))
        result = classify_reply_llm(
            PATRICIA_OPENER, PATRICIA_REPLY, anthropic_client=client
        )
        assert result["source"] == "keyword"
        assert result["classification"] in VALID_CLASSIFICATIONS

    def test_fallback_preserves_reply_context(self):
        """The keyword fallback runs on the REPLY (not the opener) so the
        defensive signal is caught."""
        client = _mock_client_raising(TimeoutError())
        result = classify_reply_llm(
            opener_text="ignored",
            reply_text="estoy segura que no viven en Excel con toda certeza",
            anthropic_client=client,
        )
        assert result["classification"] == "defensive"

    def test_fenced_json_parsed_successfully(self):
        client = _mock_client_returning(
            "```json\n" + _valid_llm_json(classification="positive") + "\n```"
        )
        result = classify_reply_llm(
            "opener", "Interesado", anthropic_client=client
        )
        assert result["classification"] == "positive"
        assert result["source"] == "llm"


# ── Prompt structure ────────────────────────────────────────────────


class TestPromptStructure:
    @pytest.fixture(autouse=True)
    def _set_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    def test_both_opener_and_reply_reach_model(self):
        client = _mock_client_returning(_valid_llm_json())
        classify_reply_llm(
            opener_text="unique-opener-marker-abc",
            reply_text="unique-reply-marker-xyz",
            anthropic_client=client,
        )
        user_msg = client.messages.create.call_args.kwargs["messages"][0]
        assert "unique-opener-marker-abc" in user_msg["content"]
        assert "unique-reply-marker-xyz" in user_msg["content"]
        # OPENER label must appear before REPLY so the model can distinguish
        assert user_msg["content"].index("OPENER") < user_msg["content"].index("REPLY")

    def test_system_prompt_uses_cache_control(self):
        client = _mock_client_returning(_valid_llm_json())
        classify_reply_llm("o", "r", anthropic_client=client)
        system = client.messages.create.call_args.kwargs["system"]
        assert isinstance(system, list)
        assert system[0]["type"] == "text"
        assert system[0]["cache_control"] == {"type": "ephemeral"}
        assert system[0]["text"] == CLASSIFIER_SYSTEM_PROMPT

    def test_default_model_is_haiku(self):
        client = _mock_client_returning(_valid_llm_json())
        classify_reply_llm("o", "r", anthropic_client=client)
        assert client.messages.create.call_args.kwargs["model"] == CLASSIFIER_MODEL
        assert "haiku" in CLASSIFIER_MODEL

    def test_taxonomy_covers_all_five_labels(self):
        """Each valid label must be described in the system prompt — a
        regression test against the prompt drifting out of sync with
        VALID_CLASSIFICATIONS.
        """
        for label in VALID_CLASSIFICATIONS:
            assert f"**{label}**" in CLASSIFIER_SYSTEM_PROMPT

    def test_prompt_explains_opener_context_matters(self):
        """The whole reason Task 4 takes opener is so the model can
        distinguish neutral-in-isolation replies that are defensive in
        context. The prompt must say so.
        """
        assert "in context" in CLASSIFIER_SYSTEM_PROMPT
        assert "premise" in CLASSIFIER_SYSTEM_PROMPT


# ── Keyword-fallback envelope shape ─────────────────────────────────


class TestKeywordFallbackShape:
    def test_fallback_result_has_all_expected_fields(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        result = classify_reply_llm("o", "No gracias, no me interesa.")
        required = {
            "classification",
            "reasoning",
            "defensive_score",
            "engagement_score",
            "suggested_action",
            "summary",
            "source",
        }
        assert required.issubset(set(result.keys()))
        assert result["source"] == "keyword"
        assert 0.0 <= result["defensive_score"] <= 1.0
        assert 0.0 <= result["engagement_score"] <= 1.0

    def test_fallback_defensive_reply_scores_high_defensive(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        result = classify_reply_llm("o", "estoy segura que no aplica")
        assert result["classification"] == "defensive"
        assert result["defensive_score"] == 1.0

    def test_fallback_positive_reply_scores_high_engagement(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        result = classify_reply_llm("o", "Me interesa, agendemos demo")
        assert result["classification"] == "positive"
        assert result["engagement_score"] == 1.0
