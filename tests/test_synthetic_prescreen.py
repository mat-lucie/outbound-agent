"""Tests for the synthetic pre-screen variant evaluator.

All tests use mocked anthropic clients so no live API calls happen.
The live-eval validation step lives in the plan doc (Step 8.6) and is
run manually when iterating on the prompt.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from workflows.synthetic_prescreen import (
    MODEL,
    CoerceRangeViolation,
    _coerce_range,
    load_synthetic_personas,
    score_variant_against_persona,
    score_variant_matrix,
)

# ── Mock helpers ─────────────────────────────────────────────────────


def _mock_sonnet_returning(response_json: str):
    """Build a mock Anthropic client that returns the given JSON."""
    client = MagicMock()
    block = MagicMock()
    block.type = "text"
    block.text = response_json
    message = MagicMock()
    message.content = [block]
    client.messages.create.return_value = message
    return client


V0_PATRICIA_OPENER = (
    "Hola Patricia, gracias por aceptar mi invitación. Una pregunta directa - "
    "en Globex cómo llevan la programación de producción? Te pregunto "
    "porque en todas las plantas que visitamos, incluso las que ya tienen "
    "SAP bien montado, eso sigue viviendo en Excel. Les pasa igual?"
)

V1_CURIOSITY_OPENER = (
    "Hola Patricia, gracias por aceptar. Soy de Acme, hacemos IA para cadena "
    "de suministro en manufactura. Una pregunta: en Globex hoy, el plan "
    "lo arma el programador desde cero, o ya hay un sistema que lo propone y "
    "él lo revisa? ¿Qué tan eficiente es?"
)


# ── Personas JSON ────────────────────────────────────────────────────


class TestPersonasFile:
    def test_four_personas_defined(self):
        personas = load_synthetic_personas()
        assert set(personas.keys()) == {
            "patricia_defensive_senior",
            "family_ceo_curious",
            "coo_actively_searching",
            "fernando_regional_ops_director",
        }

    def test_every_persona_has_required_fields(self):
        required = {"name", "context", "mindset", "reaction_triggers", "reaction_defusers"}
        personas = load_synthetic_personas()
        for key, persona in personas.items():
            missing = required - set(persona.keys())
            assert not missing, f"{key} missing fields: {missing}"
            assert isinstance(persona["reaction_triggers"], list) and persona["reaction_triggers"]
            assert isinstance(persona["reaction_defusers"], list) and persona["reaction_defusers"]


# ── score_variant_against_persona ────────────────────────────────────


class TestScoreVariantAgainstPersona:
    def test_v0_against_patricia_scores_high_defensive(self):
        """Mock-based sanity check — verifies the envelope round-trip."""
        client = _mock_sonnet_returning(json.dumps({
            "defensive_likelihood": [0.7, 0.9],
            "engagement_likelihood": [0.05, 0.2],
            "reasoning": "Diagnosis premise triggers reactance",
        }))
        result = score_variant_against_persona(
            variant_text=V0_PATRICIA_OPENER,
            persona_key="patricia_defensive_senior",
            anthropic_client=client,
        )
        assert result["defensive_likelihood"][0] >= 0.5
        assert result["engagement_likelihood"][1] <= 0.5
        assert "reactance" in result["reasoning"].lower()

    def test_v1_against_patricia_scores_low_defensive(self):
        client = _mock_sonnet_returning(json.dumps({
            "defensive_likelihood": [0.1, 0.3],
            "engagement_likelihood": [0.4, 0.6],
            "reasoning": "Curiosity frame defuses reactance",
        }))
        result = score_variant_against_persona(
            variant_text=V1_CURIOSITY_OPENER,
            persona_key="patricia_defensive_senior",
            anthropic_client=client,
        )
        assert result["defensive_likelihood"][1] < 0.5

    def test_unknown_persona_raises(self):
        client = _mock_sonnet_returning("{}")
        with pytest.raises(ValueError, match="Unknown synthetic persona"):
            score_variant_against_persona(
                variant_text="anything",
                persona_key="does_not_exist",
                anthropic_client=client,
            )

    def test_system_prompt_contains_persona_details(self):
        """The persona's context and triggers must reach the model."""
        client = _mock_sonnet_returning(json.dumps({
            "defensive_likelihood": [0.1, 0.2],
            "engagement_likelihood": [0.1, 0.2],
            "reasoning": "x",
        }))
        score_variant_against_persona(
            variant_text="test opener",
            persona_key="patricia_defensive_senior",
            anthropic_client=client,
        )
        call_kwargs = client.messages.create.call_args.kwargs
        system = call_kwargs["system"]
        assert "Senior Digital Lead" in system
        assert "Diagnostic premises" in system  # a reaction trigger
        assert "Curiosity-led" in system  # a reaction defuser
        # Opener text reaches the user message
        user_msg = call_kwargs["messages"][0]
        assert "test opener" in user_msg["content"]

    def test_default_model_is_sonnet(self):
        client = _mock_sonnet_returning(json.dumps({
            "defensive_likelihood": [0.1, 0.2],
            "engagement_likelihood": [0.1, 0.2],
            "reasoning": "x",
        }))
        score_variant_against_persona(
            variant_text="hi",
            persona_key="family_ceo_curious",
            anthropic_client=client,
        )
        assert client.messages.create.call_args.kwargs["model"] == MODEL
        assert "sonnet" in MODEL

    def test_point_estimate_coerced_to_range(self):
        """If the model returns a single float instead of [min, max], widen it."""
        client = _mock_sonnet_returning(json.dumps({
            "defensive_likelihood": 0.42,
            "engagement_likelihood": 0.3,
            "reasoning": "x",
        }))
        result = score_variant_against_persona(
            variant_text="hi",
            persona_key="coo_actively_searching",
            anthropic_client=client,
        )
        assert result["defensive_likelihood"] == [0.42, 0.42]
        assert result["engagement_likelihood"] == [0.3, 0.3]

    def test_missing_defensive_field_raises(self):
        """A missing defensive_likelihood must NOT silently become [0.0, 0.0]
        — that would make a reactance-inducing variant LOOK safe in the
        pre-screen, which is exactly the failure mode Task 8 exists to
        prevent.
        """
        client = _mock_sonnet_returning(json.dumps({
            "engagement_likelihood": [0.4, 0.6],
            "reasoning": "forgot defensive",
        }))
        with pytest.raises(RuntimeError, match="defensive_likelihood"):
            score_variant_against_persona(
                variant_text="hi",
                persona_key="family_ceo_curious",
                anthropic_client=client,
            )

    def test_string_score_raises(self):
        """Model returning a stringified number must raise, not be coerced."""
        client = _mock_sonnet_returning(json.dumps({
            "defensive_likelihood": "0.8",
            "engagement_likelihood": [0.1, 0.2],
            "reasoning": "x",
        }))
        with pytest.raises(RuntimeError, match="defensive_likelihood"):
            score_variant_against_persona(
                variant_text="hi",
                persona_key="family_ceo_curious",
                anthropic_client=client,
            )

    def test_wrong_length_list_raises(self):
        client = _mock_sonnet_returning(json.dumps({
            "defensive_likelihood": [0.1, 0.2, 0.3],  # three-element list
            "engagement_likelihood": [0.4, 0.6],
            "reasoning": "x",
        }))
        with pytest.raises(RuntimeError, match="defensive_likelihood"):
            score_variant_against_persona(
                variant_text="hi",
                persona_key="family_ceo_curious",
                anthropic_client=client,
            )

    def test_boolean_score_raises(self):
        """bool is a subclass of int, but must NOT be coerced to 1.0/0.0."""
        client = _mock_sonnet_returning(json.dumps({
            "defensive_likelihood": True,
            "engagement_likelihood": [0.4, 0.6],
            "reasoning": "x",
        }))
        with pytest.raises(RuntimeError, match="defensive_likelihood"):
            score_variant_against_persona(
                variant_text="hi",
                persona_key="family_ceo_curious",
                anthropic_client=client,
            )

    def test_fenced_json_is_parsed(self):
        client = _mock_sonnet_returning(
            '```json\n{"defensive_likelihood": [0.1, 0.2], '
            '"engagement_likelihood": [0.5, 0.7], "reasoning": "ok"}\n```'
        )
        result = score_variant_against_persona(
            variant_text="hi",
            persona_key="family_ceo_curious",
            anthropic_client=client,
        )
        assert result["defensive_likelihood"] == [0.1, 0.2]
        assert result["engagement_likelihood"] == [0.5, 0.7]


# ── score_variant_matrix ─────────────────────────────────────────────


class TestScoreVariantMatrix:
    def test_full_grid_returns_all_pairs(self):
        client = _mock_sonnet_returning(json.dumps({
            "defensive_likelihood": [0.2, 0.4],
            "engagement_likelihood": [0.4, 0.6],
            "reasoning": "neutral",
        }))
        variants = {"v0": "opener 0", "v1": "opener 1"}
        personas = ["patricia_defensive_senior", "family_ceo_curious"]
        matrix = score_variant_matrix(
            variants=variants,
            persona_keys=personas,
            anthropic_client=client,
        )
        assert set(matrix.keys()) == {"v0", "v1"}
        for variant_scores in matrix.values():
            assert set(variant_scores.keys()) == set(personas)
        assert client.messages.create.call_count == 4

    def test_default_personas_are_all_four(self):
        client = _mock_sonnet_returning(json.dumps({
            "defensive_likelihood": [0.1, 0.2],
            "engagement_likelihood": [0.1, 0.2],
            "reasoning": "x",
        }))
        matrix = score_variant_matrix(
            variants={"v1": "opener"},
            anthropic_client=client,
        )
        assert set(matrix["v1"].keys()) == {
            "patricia_defensive_senior",
            "family_ceo_curious",
            "coo_actively_searching",
            "fernando_regional_ops_director",
        }
        assert client.messages.create.call_count == 4


# ── CoerceRangeViolation (B-MW-COERCE) ───────────────────────────────


class TestCoerceRangeViolation:
    """PR-24: _coerce_range MUST raise CoerceRangeViolation on out-of-range
    input — no silent clamping (§0 #9).
    """

    # ---- Exception class invariants ----

    def test_coerce_range_violation_is_subclass_of_runtime_error(self):
        """Must subclass RuntimeError, NOT ValueError (PR-21 v1 lesson).

        A ValueError subclass can be swallowed by broad except ValueError:
        guards in callers. RuntimeError propagates correctly through all
        Acme dispatch/orchestration layers.
        """
        assert issubclass(CoerceRangeViolation, RuntimeError)

    def test_coerce_range_violation_not_subclass_of_value_error(self):
        assert not issubclass(CoerceRangeViolation, ValueError)

    def test_coerce_range_violation_carries_attrs(self):
        """The exception must carry .value, .min_expected, .max_expected
        so callers can construct queue row payloads without extra context.
        """
        err = CoerceRangeViolation(1.5, 0.0, 1.0)
        assert err.value == 1.5
        assert err.min_expected == 0.0
        assert err.max_expected == 1.0

    def test_coerce_range_violation_str_includes_value_and_bounds(self):
        err = CoerceRangeViolation(-0.1, 0.0, 1.0)
        msg = str(err)
        assert "-0.1" in msg
        assert "0.0" in msg
        assert "1.0" in msg

    # ---- Range list inputs ----

    def test_negative_min_in_list_raises(self):
        with pytest.raises(CoerceRangeViolation) as exc_info:
            _coerce_range([-0.5, 0.7], "defensive_likelihood")
        assert exc_info.value.value == -0.5
        assert exc_info.value.min_expected == 0.0
        assert exc_info.value.max_expected == 1.0

    def test_max_exceeds_1_in_list_raises(self):
        with pytest.raises(CoerceRangeViolation) as exc_info:
            _coerce_range([0.3, 1.5], "engagement_likelihood")
        assert exc_info.value.value == 1.5

    def test_negative_max_in_list_raises(self):
        """Both components negative — only the first is caught."""
        with pytest.raises(CoerceRangeViolation):
            _coerce_range([-0.2, -0.1], "defensive_likelihood")

    def test_both_out_of_range_raises_on_first(self):
        """When both components are out of range, the first is reported."""
        with pytest.raises(CoerceRangeViolation) as exc_info:
            _coerce_range([-0.5, 2.0], "engagement_likelihood")
        assert exc_info.value.value == -0.5

    def test_exactly_0_0_is_in_range(self):
        result = _coerce_range([0.0, 0.0], "defensive_likelihood")
        assert result == [0.0, 0.0]

    def test_exactly_1_0_is_in_range(self):
        result = _coerce_range([1.0, 1.0], "engagement_likelihood")
        assert result == [1.0, 1.0]

    def test_valid_range_passes_through(self):
        result = _coerce_range([0.2, 0.8], "defensive_likelihood")
        assert result == [0.2, 0.8]

    # ---- Scalar (point estimate) inputs ----

    def test_scalar_below_0_raises(self):
        with pytest.raises(CoerceRangeViolation) as exc_info:
            _coerce_range(-0.01, "engagement_likelihood")
        assert exc_info.value.value == -0.01

    def test_scalar_above_1_raises(self):
        with pytest.raises(CoerceRangeViolation) as exc_info:
            _coerce_range(1.1, "defensive_likelihood")
        assert exc_info.value.value == 1.1

    def test_scalar_exactly_1_0_is_valid(self):
        result = _coerce_range(1.0, "defensive_likelihood")
        assert result == [1.0, 1.0]

    # ---- Custom bounds ----

    def test_custom_bounds_accepted(self):
        """kwargs allow testability with non-[0,1] contracts."""
        result = _coerce_range([0.5, 2.0], "x", min_expected=0.0, max_expected=5.0)
        assert result == [0.5, 2.0]

    def test_custom_bounds_violation_raises(self):
        with pytest.raises(CoerceRangeViolation) as exc_info:
            _coerce_range([0.5, 2.0], "x", min_expected=0.0, max_expected=1.5)
        assert exc_info.value.value == 2.0
        assert exc_info.value.max_expected == 1.5

    # ---- Existing shape checks still work ----

    def test_malformed_shape_raises_runtime_error_not_coerce_violation(self):
        """Shape violations raise plain RuntimeError, not CoerceRangeViolation.
        Callers can distinguish bound violations from shape violations.
        """
        with pytest.raises(RuntimeError) as exc_info:
            _coerce_range("not-a-number", "defensive_likelihood")
        assert not isinstance(exc_info.value, CoerceRangeViolation)

    def test_boolean_still_raises_runtime_error_not_coerce_violation(self):
        """bool shape violation → plain RuntimeError (existing behavior)."""
        with pytest.raises(RuntimeError) as exc_info:
            _coerce_range(True, "defensive_likelihood")
        assert not isinstance(exc_info.value, CoerceRangeViolation)


class TestScoreVariantDispatchRouting:
    """PR-24: per-call try/except routing in score_variant_against_persona.

    Tests use mocked LLM dispatch path (anthropic_client=None + mocked
    request_llm_dispatch). The escalate() call is also mocked so tests
    don't attempt live Attio writes.
    """

    def test_out_of_range_score_raises_coerce_range_violation(self):
        """When the LLM returns an out-of-bounds value, CoerceRangeViolation
        propagates from _coerce_range through score_variant_against_persona.
        """
        client = _mock_sonnet_returning(json.dumps({
            "defensive_likelihood": [-0.5, 0.7],
            "engagement_likelihood": [0.3, 0.6],
            "reasoning": "bad bounds",
        }))
        with pytest.raises(CoerceRangeViolation) as exc_info:
            score_variant_against_persona(
                variant_text="hi",
                persona_key="family_ceo_curious",
                anthropic_client=client,
            )
        assert exc_info.value.value == -0.5

    def test_out_of_range_score_via_anthropic_client_no_escalate(self):
        """When using test-injected client (not dispatch path), CoerceRangeViolation
        still raises but escalate() is NOT called (no live Attio in tests).

        The guard ``if anthropic_client is None:`` before the escalate() call
        inside _coerce_range's except block ensures the test-injection path
        never attempts a live Attio write. We verify this by patching
        workflows.escalation.escalate (where it's imported from) and
        confirming it's not called.
        """
        from unittest.mock import patch

        client = _mock_sonnet_returning(json.dumps({
            "defensive_likelihood": [1.5, 1.8],
            "engagement_likelihood": [0.3, 0.6],
            "reasoning": "out of range",
        }))
        # Patch at the source module — escalate is imported locally inside
        # score_variant_against_persona so we patch where it's defined.
        with patch("workflows.escalation.escalate") as mock_escalate:
            with pytest.raises(CoerceRangeViolation):
                score_variant_against_persona(
                    variant_text="hi",
                    persona_key="coo_actively_searching",
                    anthropic_client=client,
                )
            # escalate NOT called because anthropic_client is not None (test path)
            mock_escalate.assert_not_called()

    # ── Dispatch-path emission tests (fold-in: advertiser-weekly BLOCKING + pr-test IMPORTANT) ──
    #
    # The two tests above use anthropic_client=mock (test-injection path)
    # which skips escalate(). That means the production emission code is
    # un-covered. These four tests close that gap by exercising the
    # dispatch path (anthropic_client=None + mocked request_llm_dispatch)
    # and asserting escalate() IS called with the correct payload.

    def test_dispatch_path_llm_timeout_emits_synth_prescreen_call_failed(self):
        """Dispatch path + LLMDispatchTimeout → escalate(synth_prescreen_call_failed)
        is called with correct payload + the typed exception propagates BARE
        (no RuntimeError wrap that would erase the type).
        """
        from unittest.mock import patch

        from workflows.llm_dispatch import LLMDispatchTimeout

        with (
            patch("workflows.llm_dispatch.request_llm_dispatch",
                  side_effect=LLMDispatchTimeout(
                      "synthetic_prescreen", "disp-1", 30.0)),
            patch("workflows.llm_dispatch.is_dispatch_enabled", return_value=True),
            patch("workflows.escalation.escalate") as mock_escalate,
        ):
            with pytest.raises(LLMDispatchTimeout):
                score_variant_against_persona(
                    variant_text="hi",
                    persona_key="family_ceo_curious",
                    variant_id="v0",
                )
            # Exactly one escalate call with the right slug + payload
            mock_escalate.assert_called_once()
            call_kwargs = mock_escalate.call_args.kwargs
            assert call_kwargs["type"] == "synth_prescreen_call_failed"
            assert call_kwargs["payload"]["persona_key"] == "family_ceo_curious"
            assert call_kwargs["payload"]["variant_id"] == "v0"
            assert call_kwargs["payload"]["error_class"] == "LLMDispatchTimeout"
            # Idempotency key includes variant_id so two variants failing on
            # the same persona don't collapse into one row
            assert "v0" in call_kwargs["idempotency_key"]
            assert "family_ceo_curious" in call_kwargs["idempotency_key"]

    def test_dispatch_path_llm_failed_emits_synth_prescreen_call_failed(self):
        """Dispatch path + LLMDispatchFailed → escalate called + bare re-raise."""
        from unittest.mock import patch

        from workflows.llm_dispatch import LLMDispatchFailed

        with (
            patch("workflows.llm_dispatch.request_llm_dispatch",
                  side_effect=LLMDispatchFailed(
                      "synthetic_prescreen", "disp-2", "subagent crashed")),
            patch("workflows.llm_dispatch.is_dispatch_enabled", return_value=True),
            patch("workflows.escalation.escalate") as mock_escalate,
        ):
            with pytest.raises(LLMDispatchFailed):
                score_variant_against_persona(
                    variant_text="hi",
                    persona_key="coo_actively_searching",
                    variant_id="v1",
                )
            mock_escalate.assert_called_once()
            payload = mock_escalate.call_args.kwargs["payload"]
            assert payload["error_class"] == "LLMDispatchFailed"
            assert payload["variant_id"] == "v1"

    def test_dispatch_path_cost_ceiling_exhausted_emits_cost_ceiling_breached(self):
        """Dispatch path + CostCeilingExhausted → escalate(cost_ceiling_breached)
        called with step/cap/consumed payload + the typed exception propagates BARE.

        Per PR-24 §5 line 724: "Per-call try/except wraps distinguish
        CostCeilingExhausted (route to cost_ceiling_breached queue)..."
        The bare re-raise allows the slash-command boundary handler to
        also catch + log (idempotency key dedups the row).
        """
        from unittest.mock import patch

        from workflows.llm_dispatch import CostCeilingExhausted

        with (
            patch("workflows.llm_dispatch.request_llm_dispatch",
                  side_effect=CostCeilingExhausted(
                      step="synthetic_prescreen", cap=2.50, consumed=2.65)),
            patch("workflows.llm_dispatch.is_dispatch_enabled", return_value=True),
            patch("workflows.escalation.escalate") as mock_escalate,
        ):
            with pytest.raises(CostCeilingExhausted) as exc_info:
                score_variant_against_persona(
                    variant_text="hi",
                    persona_key="family_ceo_curious",
                    variant_id="v0",
                )
            # Bare re-raise preserves the type — not wrapped in RuntimeError
            assert isinstance(exc_info.value, CostCeilingExhausted)
            assert exc_info.value.step == "synthetic_prescreen"
            # cost_ceiling_breached queue row emitted
            mock_escalate.assert_called_once()
            call_kwargs = mock_escalate.call_args.kwargs
            assert call_kwargs["type"] == "cost_ceiling_breached"
            payload = call_kwargs["payload"]
            assert payload["step"] == "synthetic_prescreen"
            assert payload["cap_usd"] == 2.50
            assert payload["consumed_usd"] == 2.65
            assert payload["halt_scope"] == "step"

    def test_dispatch_path_coerce_range_violation_emits_synth_prescreen_call_failed(self):
        """Dispatch path + LLM returns out-of-bounds → CoerceRangeViolation
        path emits escalate + bare re-raise. Variant_id is included in
        payload + idempotency key.
        """
        from unittest.mock import MagicMock, patch

        # Mock request_llm_dispatch to return out-of-range JSON
        dispatch_result = MagicMock()
        dispatch_result.raw_text = json.dumps({
            "defensive_likelihood": [-0.5, 0.7],
            "engagement_likelihood": [0.3, 0.6],
            "reasoning": "bad",
        })

        with (
            patch("workflows.llm_dispatch.request_llm_dispatch", return_value=dispatch_result),
            patch("workflows.llm_dispatch.is_dispatch_enabled", return_value=True),
            patch("workflows.escalation.escalate") as mock_escalate,
        ):
            with pytest.raises(CoerceRangeViolation) as exc_info:
                score_variant_against_persona(
                    variant_text="hi",
                    persona_key="coo_actively_searching",
                    variant_id="v2",
                )
            assert exc_info.value.value == -0.5
            mock_escalate.assert_called_once()
            call_kwargs = mock_escalate.call_args.kwargs
            assert call_kwargs["type"] == "synth_prescreen_call_failed"
            payload = call_kwargs["payload"]
            assert payload["error_class"] == "CoerceRangeViolation"
            assert payload["variant_id"] == "v2"
            assert "v2" in call_kwargs["idempotency_key"]
            assert "-0.5" in call_kwargs["idempotency_key"]


class TestCoerceRangeReversedBounds:
    """Reversed range guard (silent-failure + type-design convergence).

    A reversed range like [0.8, 0.2] would silently produce q75 = 0.35
    (below the stated minimum), which is semantically nonsensical.
    _coerce_range now treats reversed ranges as a bound violation.
    """

    def test_reversed_range_raises_coerce_range_violation(self):
        with pytest.raises(CoerceRangeViolation) as exc_info:
            _coerce_range([0.8, 0.2], "defensive_likelihood")
        assert exc_info.value.value == 0.8

    def test_reversed_range_via_score_variant_against_persona(self):
        """End-to-end: LLM returns reversed range → CoerceRangeViolation
        propagates up through score_variant_against_persona."""
        client = _mock_sonnet_returning(json.dumps({
            "defensive_likelihood": [0.8, 0.2],
            "engagement_likelihood": [0.3, 0.6],
            "reasoning": "reversed range",
        }))
        with pytest.raises(CoerceRangeViolation):
            score_variant_against_persona(
                variant_text="hi",
                persona_key="family_ceo_curious",
                anthropic_client=client,
            )


class TestScoreVariantMatrixAbortSemantics:
    """Document the abort-on-cell-failure semantics of score_variant_matrix
    (silent-failure-hunter BLOCKING-1 follow-up).

    The matrix loop has no try/except — a single failing (variant, persona)
    cell aborts the entire matrix call and propagates the exception. The
    per-call queue row (synth_prescreen_call_failed or cost_ceiling_breached)
    has already been emitted at the inner call site so the operator sees
    WHICH cell failed; the matrix abort is the explicit signal that the
    prescreen run terminated early. See score_variant_matrix docstring.
    """

    def test_single_cell_coerce_violation_aborts_matrix(self):
        """When the first cell raises CoerceRangeViolation, the matrix call
        propagates the exception. Remaining cells are NOT silently scored
        as missing — the run terminates so build_proposal_markdown cannot
        consume a partial matrix.
        """
        # Mock returns out-of-range on every call (will raise on first cell)
        client = _mock_sonnet_returning(json.dumps({
            "defensive_likelihood": [1.5, 1.8],
            "engagement_likelihood": [0.3, 0.6],
            "reasoning": "bad",
        }))
        with pytest.raises(CoerceRangeViolation):
            score_variant_matrix(
                variants={"v0": "hi", "v1": "hello"},
                persona_keys=["family_ceo_curious", "coo_actively_searching"],
                anthropic_client=client,
            )
