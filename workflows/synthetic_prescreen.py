"""Synthetic pre-screen for DM variants.

Loads 3 synthetic prospect personas from content/synthetic_personas.json
and asks Claude Sonnet to role-play each one, cold-reading a variant and
scoring it on two dimensions:

- defensive_likelihood: how likely the prospect is to respond with
  reactance-driven pushback (Patricia-type response)
- engagement_likelihood: how likely the prospect is to engage positively

Both scores are returned as ranges [min, max] rather than point estimates,
to be honest about LLM output variance.

Use during sales-weekly to pre-screen candidate variants before
committing to a real-world A/B cohort.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, timedelta
from pathlib import Path

MODEL = "claude-sonnet-4-6"


def _current_week_starting_iso() -> str:
    """ISO Monday of the current week — matches weekly_report._monday_of
    and weekly_prospect._monday_of patterns. Used in the
    cost_ceiling_breached idempotency key and payload so per-call
    emissions dedup correctly within a week.
    """
    today = date.today()
    return (today - timedelta(days=today.weekday())).isoformat()


class CoerceRangeViolation(RuntimeError):
    """LLM returned a numeric value outside the expected bounds.

    Raised by ``_coerce_range`` when the min or max component of a
    returned range (or a scalar point estimate) lies outside
    ``[min_expected, max_expected]``.

    Subclasses ``RuntimeError`` — NOT ``ValueError`` — so callers cannot
    accidentally catch it with a broad ``except ValueError:`` guard
    (lesson from PR-21 v1 reset where a ``ValueError`` subclass was
    swallowed by ``quality_gate``'s outer handler).

    Attributes carry the raw LLM value and the violated bounds so the
    ``coerce_range_violation`` escalation payload can be constructed
    at the call site without additional context-passing.
    """

    def __init__(self, value: float, min_expected: float, max_expected: float) -> None:
        self.value = value
        self.min_expected = min_expected
        self.max_expected = max_expected
        super().__init__(
            f"Value {value!r} outside expected range "
            f"[{min_expected}, {max_expected}]"
        )


MAX_TOKENS = 400

# Runtime content directory — see models/campaign.py for the OUTBOUND_CONTENT_DIR
# contract. Defaults to the repo-root `content/` when the env var is unset.
CONTENT_DIR = Path(
    os.environ.get("OUTBOUND_CONTENT_DIR")
    or (Path(__file__).resolve().parent.parent / "content")
)
SYNTHETIC_PERSONAS_PATH = CONTENT_DIR / "synthetic_personas.json"


def load_synthetic_personas() -> dict:
    with open(SYNTHETIC_PERSONAS_PATH, encoding="utf-8") as f:
        return json.load(f)


def _build_system_prompt(persona: dict) -> str:
    triggers = "\n".join(f"- {t}" for t in persona["reaction_triggers"])
    defusers = "\n".join(f"- {d}" for d in persona["reaction_defusers"])
    return f"""You are role-playing a cold-reader of B2B sales messages on LinkedIn.

PERSONA YOU ARE ROLE-PLAYING:
Name: {persona["name"]}
Context: {persona["context"]}
Mindset: {persona["mindset"]}

Reaction triggers (things that make you defensive):
{triggers}

Reaction defusers (things that make you open up):
{defusers}

TASK:
You will be shown ONE opener message. Cold-read it as this persona would.
Return your honest reaction, expressed as two probability ranges:

1. defensive_likelihood [min, max] — how likely you are to respond with
   defensive pushback (correcting the sender, protecting your company's
   status, refuting the premise).
2. engagement_likelihood [min, max] — how likely you are to engage
   positively (ask a follow-up, request more info, book a call).

Use wide ranges when you are uncertain. Use narrow ranges when you are
confident. Each value must be in [0.0, 1.0]. The two scores are NOT
required to sum to anything — they are independent.

Output ONLY JSON:
{{"defensive_likelihood": [<float>, <float>], "engagement_likelihood": [<float>, <float>], "reasoning": "<one sentence as the persona>"}}

No preamble. No code fences. Just the JSON."""


def _extract_text(response) -> str:
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            return block.text
        if isinstance(block, dict) and block.get("type") == "text":
            return block.get("text", "")
        # MagicMock content blocks in tests store .text directly
        text = getattr(block, "text", None)
        if text is not None:
            return text
    raise RuntimeError(f"Synthetic pre-screen got no text block: {response!r}")


def _parse_json(raw: str) -> dict:
    stripped = raw.strip()
    stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
    stripped = re.sub(r"\s*```$", "", stripped)
    return json.loads(stripped)


def _coerce_range(
    val,
    field_name: str,
    *,
    min_expected: float = 0.0,
    max_expected: float = 1.0,
) -> list[float]:
    """Coerce an LLM-returned value into a [min, max] range.

    A point estimate (single number) is widened to a zero-width range.
    Anything else — missing, null, string, wrong-length list, dict — raises
    RuntimeError. We deliberately do NOT silently default to [0.0, 0.0]:
    a reactance-inducing variant with a malformed score would otherwise be
    mistaken for "no defensive risk" and surface as the top recommendation
    in the weekly brain, which is exactly the failure mode Task 8 exists
    to prevent.

    B-MW-COERCE bound enforcement (PR-24): after coercing shape, each
    component is checked against ``[min_expected, max_expected]``. An
    out-of-range component raises ``CoerceRangeViolation`` — NO silent
    clamping (§0 #9). The system prompt at lines 62-64 already tells the
    LLM "Each value must be in [0.0, 1.0]"; this function enforces the
    contract rather than trusting the model.

    ``min_expected`` / ``max_expected`` are keyword-only kwargs so the
    bounds are explicit at every call site and testable with edge values.
    Default bounds are ``[0.0, 1.0]`` matching the system-prompt contract.
    """
    if isinstance(val, (list, tuple)) and len(val) == 2:
        lo, hi = float(val[0]), float(val[1])
        for component in (lo, hi):
            if not (min_expected <= component <= max_expected):
                raise CoerceRangeViolation(component, min_expected, max_expected)
        # Reversed range guard: q75/q25 percentile math assumes lo <= hi
        # (uniform distribution over [lo, hi]). A reversed range like
        # [0.8, 0.2] would produce q75 = 0.35 — below the stated minimum,
        # which is semantically nonsensical. Treat it as a bound violation
        # (silent-failure-hunter + type-design-analyzer convergence).
        if lo > hi:
            raise CoerceRangeViolation(lo, min_expected, hi)
        return [lo, hi]
    if isinstance(val, bool):
        # bool is a subclass of int in Python — reject explicitly so
        # `true`/`false` in the LLM output doesn't quietly become 1.0/0.0
        raise RuntimeError(
            f"Synthetic pre-screen got boolean for {field_name} — expected "
            f"[min, max] range or numeric point estimate, got {val!r}"
        )
    if isinstance(val, (int, float)):
        scalar = float(val)
        if not (min_expected <= scalar <= max_expected):
            raise CoerceRangeViolation(scalar, min_expected, max_expected)
        return [scalar, scalar]
    raise RuntimeError(
        f"Synthetic pre-screen got malformed {field_name}: expected "
        f"[min, max] range or numeric point estimate, got {val!r}"
    )


def score_variant_against_persona(
    variant_text: str,
    persona_key: str,
    *,
    variant_id: str = "",
    anthropic_client=None,
    model: str = MODEL,
    use_llm_dispatch: bool = False,
) -> dict:
    """Score one variant against one synthetic persona.

    Returns a dict with keys:
        defensive_likelihood: [min, max]
        engagement_likelihood: [min, max]
        reasoning: str

    F-PR-9: when ``anthropic_client`` is None AND ``use_llm_dispatch``
    is True (production), surfaces to the parent slash-command session
    via ``workflows.llm_dispatch.request_llm_dispatch``. The legacy
    Anthropic SDK auto-construct path is removed per §0 invariant #11.
    Test-injection via ``anthropic_client=mock`` is unchanged.

    ``variant_id`` is included in the ``synth_prescreen_call_failed``
    escalation payload and idempotency key so operators can pinpoint
    the failing cell — without it, two variants failing against the
    same persona collapse into a single deduped queue row (4-agent
    QA convergence on PR-24).
    """
    personas = load_synthetic_personas()
    if persona_key not in personas:
        raise ValueError(
            f"Unknown synthetic persona: {persona_key!r}. "
            f"Known personas: {sorted(personas.keys())}"
        )
    persona = personas[persona_key]
    system_prompt = _build_system_prompt(persona)
    user_content = f"OPENER:\n{variant_text}"

    if anthropic_client is None:
        from workflows.llm_dispatch import (
            CostCeilingExhausted,
            LLMDispatchFailed,
            LLMDispatchTimeout,
            is_dispatch_enabled,
            request_llm_dispatch,
        )
        if not (use_llm_dispatch or is_dispatch_enabled()):
            raise RuntimeError(
                "score_variant_against_persona called with no client and "
                "dispatch disabled. Set $OUTBOUND_USE_LLM_DISPATCH=1 (production) "
                "or pass use_llm_dispatch=True (production callers) or inject "
                "a mock anthropic_client (tests)."
            )

        # B-SW-COST partial (PR-24): per-call try/except distinguishes
        # CostCeilingExhausted from generic dispatch/coerce failures so
        # the orchestrator can route each failure type to its operator
        # queue with full context (which variant, which persona).
        #
        # CostCeilingExhausted → emit `cost_ceiling_breached` queue row
        # at the per-call site (PR-24 §5 line 724 spec: "Per-call try/
        # except wraps distinguish CostCeilingExhausted (route to
        # cost_ceiling_breached queue)..."), then re-raise BARE so the
        # type is preserved for the slash-command boundary handler per
        # plan §3.7 5th bullet. escalate() is idempotent via the
        # idempotency key, so an upstream catch that also emits the
        # same row is a no-op.
        #
        # LLMDispatchTimeout / LLMDispatchFailed / CoerceRangeViolation →
        # emit `synth_prescreen_call_failed` queue row then re-raise BARE
        # (preserves the typed exception). Wrapping in plain RuntimeError
        # erases the type and any upstream `except RuntimeError:` would
        # swallow these AND CoerceRangeViolation indistinguishably
        # (silent-failure-hunter BLOCKING-3 convergence).
        try:
            dispatch_result = request_llm_dispatch(
                step="synthetic_prescreen",
                prompt=user_content,
                system=system_prompt,
                model_class="sonnet",
                max_tokens=MAX_TOKENS,
                schema_hint=(
                    'Return JSON: {"defensive_likelihood": [min,max], '
                    '"engagement_likelihood": [min,max], "reasoning": str}'
                ),
            )
        except CostCeilingExhausted as err:
            # Per PR-24 §5 line 724: per-call wrap routes CostCeilingExhausted
            # to cost_ceiling_breached queue. Emit then re-raise BARE so the
            # slash-command boundary handler can also catch + log.
            from workflows.escalation import escalate
            week_starting = _current_week_starting_iso()
            escalate(
                type="cost_ceiling_breached",
                idempotency_key=(
                    f"cost_ceiling|synthetic_prescreen|"
                    f"{week_starting}|{persona_key}"
                ),
                payload={
                    "step": err.step,
                    "week_starting": week_starting,
                    "cap_usd": err.cap,
                    "consumed_usd": err.consumed,
                    "halt_scope": "step",
                },
            )
            raise
        except (LLMDispatchTimeout, LLMDispatchFailed) as err:
            from workflows.escalation import escalate
            escalate(
                type="synth_prescreen_call_failed",
                idempotency_key=(
                    f"synth_prescreen|{variant_id}|{persona_key}|"
                    f"{type(err).__name__}"
                ),
                payload={
                    "persona_key": persona_key,
                    "variant_id": variant_id,
                    "error_class": type(err).__name__,
                    "error_msg": str(err),
                },
            )
            raise
        raw = dispatch_result.raw_text
        parsed = _parse_json(raw)
    else:
        response = anthropic_client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = _extract_text(response)
        parsed = _parse_json(raw)

    try:
        return {
            "defensive_likelihood": _coerce_range(
                parsed.get("defensive_likelihood"), "defensive_likelihood"
            ),
            "engagement_likelihood": _coerce_range(
                parsed.get("engagement_likelihood"), "engagement_likelihood"
            ),
            "reasoning": str(parsed.get("reasoning", "")).strip(),
        }
    except CoerceRangeViolation as err:
        # Bound violation from _coerce_range — the LLM returned a value
        # outside [0.0, 1.0]. Emit `synth_prescreen_call_failed` queue
        # row and re-raise BARE (preserves type). score_variant_matrix
        # currently aborts on the first failing cell — see its docstring.
        if anthropic_client is None:
            # Only escalate in dispatch-path (production); test-injection
            # path (anthropic_client is not None) must not attempt a live
            # Attio write in unit tests. This guard pattern is idiomatic
            # across the codebase (response_classifier, qualifier_critique,
            # weekly_prospect, industry_classifier, quality_gate all use it).
            from workflows.escalation import escalate
            escalate(
                type="synth_prescreen_call_failed",
                idempotency_key=(
                    f"synth_prescreen|{variant_id}|{persona_key}|"
                    f"CoerceRangeViolation|{err.value}"
                ),
                payload={
                    "persona_key": persona_key,
                    "variant_id": variant_id,
                    "error_class": "CoerceRangeViolation",
                    "error_msg": str(err),
                },
            )
        raise


def score_variant_matrix(
    variants: dict[str, str],
    persona_keys: list[str] | None = None,
    *,
    anthropic_client=None,
    model: str = MODEL,
    use_llm_dispatch: bool = False,
) -> dict[str, dict[str, dict]]:
    """Score every (variant × synthetic persona) pair.

    Args:
        variants: mapping from variant_id to variant_text
        persona_keys: which synthetic personas to score against. Defaults
            to all personas in synthetic_personas.json.
        anthropic_client: injectable client for testing.
        model: override model.
        use_llm_dispatch: F-PR-9 production flag; forwarded to each
            score_variant_against_persona call.

    Returns:
        {variant_id: {persona_key: score_dict}}

    Failure semantics (silent-failure-hunter BLOCKING-1 follow-up):
        score_variant_matrix is INTENTIONALLY abort-on-cell-failure.
        A single CoerceRangeViolation / LLMDispatchTimeout /
        LLMDispatchFailed / CostCeilingExhausted from any (variant,
        persona) cell propagates uncaught and the matrix call raises.
        The per-call queue row (synth_prescreen_call_failed or
        cost_ceiling_breached) has already been emitted at the inner
        call site, so the operator sees WHICH cell failed; the matrix
        abort is the explicit signal that the prescreen run terminated
        early. Returning a partial matrix with sentinel `None` cells
        was considered and rejected: build_proposal_markdown's
        VariantScore aggregation cannot meaningfully handle missing
        cells, and silently building a partial proposal would violate
        §0 #9 (no silent fallbacks).
    """
    if persona_keys is None:
        persona_keys = list(load_synthetic_personas().keys())

    matrix: dict[str, dict[str, dict]] = {}
    for variant_id, variant_text in variants.items():
        matrix[variant_id] = {}
        for persona_key in persona_keys:
            matrix[variant_id][persona_key] = score_variant_against_persona(
                variant_text=variant_text,
                persona_key=persona_key,
                variant_id=variant_id,
                anthropic_client=anthropic_client,
                model=model,
                use_llm_dispatch=use_llm_dispatch,
            )
    return matrix
