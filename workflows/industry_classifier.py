"""Industry classifier: map a company name + optional domain to one of the 11
INDUSTRY_LABELS defined in `models.campaign`.

Classify companies into industry verticals using Haiku. Output is constrained
to the 11 labels in `models.campaign.INDUSTRY_LABELS`. An invalid or
API-failure response returns 'Other', which the DM personalizer handles via the
generic fallback — so a classification miss is a silent downgrade, never a
runtime error.

PR-25 adds ``classify_with_confidence`` which returns an ``IndustryVerdict``
dataclass (status/confidence/vertical/reasoning). The existing
``classify_industry`` function is preserved unchanged for back-compat.
"""

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

import httpx

if TYPE_CHECKING:
    from workflows.migration_run_writer import MigrationRunWriter
    from workflows.reclassification_run_writer import ReclassificationRunWriter

logger = logging.getLogger(__name__)

CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"
CLASSIFIER_MAX_TOKENS = 40

# PR-25: confidence-aware classifier max tokens (JSON response is larger)
CLASSIFIER_CONFIDENCE_MAX_TOKENS = 150

# PR-25: per-call cost estimate for Haiku (input ~200tok, output ~40tok, system cached).
# Haiku pricing: $0.25/$1.25 per M tokens (input/output).
# Estimate: (200 * 0.25 + 40 * 1.25) / 1_000_000 ≈ $0.00010
# Decimal avoids float precision loss on accumulation across many calls.
HAIKU_COST_PER_CALL_USD: Decimal = Decimal("0.0001")

# Confidence threshold below which a verdict is classified as "low_confidence".
# NOTE: distinct from ReclassificationRunWriter.confidence_threshold (typically 0.8),
# which is the BATCH-write gate. A verdict can be `status="confirmed"` here (≥0.7)
# but still mark_abstain'd by a stricter batch runner. The two thresholds serve
# different purposes — per-call status determination vs batch write-back gating.
DEFAULT_CONFIDENCE_THRESHOLD = 0.7


@dataclass(frozen=True)
class IndustryVerdict:
    """Typed return value from classify_with_confidence.

    ``status`` is the load-bearing field:
    - ``"confirmed"``      — LLM returned a valid label AND confidence ≥ threshold.
    - ``"low_confidence"`` — LLM returned a valid label BUT confidence < threshold.
                             ``vertical`` is populated (the LLM's best guess) but
                             downstream consumers MUST check status before trusting it.
    - ``"unknown"``        — dispatch failed, LLM returned an invalid label, or
                             company_name was empty. ``vertical=None``.

    ``cost_usd_estimated`` uses ``Decimal`` (not float) to avoid precision
    loss on accumulation across many calls. NOTE: ``ReclassificationRunWriter
    .add_cost`` currently accepts ``float`` — callers passing this field into
    the writer must convert via ``float(verdict.cost_usd_estimated)``.

    ``__post_init__`` enforces three invariants to prevent construction of
    structurally-invalid verdicts:
      - ``status="unknown"`` requires ``vertical is None``
      - ``status="confirmed"`` requires ``vertical is not None``
      - ``confidence`` must be in ``[0.0, 1.0]``
    """
    status: Literal["confirmed", "unknown", "low_confidence"]
    confidence: float
    vertical: str | None
    reasoning: str | None
    model_used: str
    cost_usd_estimated: Decimal

    def __post_init__(self) -> None:
        if self.status == "unknown" and self.vertical is not None:
            raise ValueError(
                f"IndustryVerdict status='unknown' requires vertical=None, "
                f"got vertical={self.vertical!r}"
            )
        if self.status == "confirmed" and self.vertical is None:
            raise ValueError(
                "IndustryVerdict status='confirmed' requires vertical to be populated"
            )
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                f"IndustryVerdict confidence must be in [0.0, 1.0], "
                f"got {self.confidence!r}"
            )

# NOTE: Labels below must stay in sync with models.campaign.INDUSTRY_LABELS.keys().
# VALID_LABELS (constructed from INDUSTRY_LABELS.keys()) is the validator;
# this prompt text is what Haiku sees. Drift here means a new label is defined
# but Haiku never learns about it and always routes to "Other".
CLASSIFIER_SYSTEM_PROMPT = """You classify companies into manufacturing industry verticals.

Return ONLY one of these labels — exactly as written, case-sensitive, with & not 'and':

- Manufacturing (general industrial / discrete / mixed manufacturing)
- Food & Beverage (food processing, beverages, dairy, confectionery)
- Construction (civil engineering, building materials, infrastructure)
- Oil & Gas (upstream, downstream, petrochemical refining)
- Mining (metals extraction, mineral processing, quarrying)
- Automotive (vehicle assembly, auto parts, tier-1/tier-2 suppliers)
- Textiles (apparel, fabric, garments, fiber production)
- Packaging (containers, boxes, labels, flexible packaging)
- Chemicals (industrial chemicals, specialty chemicals, fertilizers)
- Pharma (pharmaceuticals, biotech, medical devices, nutraceuticals)
- Other (any company that is not a manufacturer)

Rules:
- Return ONLY the label, nothing else — no punctuation, no explanation.
- If the company is not a manufacturer, return Other.
- Never invent a label outside the list above."""


def build_classifier_payload(
    label: str,
    *,
    source: str = "haiku_classifier",
    status: str = "low_confidence",
) -> dict:
    """Canonical CRM company write payload for a classifier-assigned industry.

    Single owner of the PR-25 provenance contract shared by every writer
    (backfill, ingest-time write-back, company CREATE stamp, and the bulk
    apply script): ``low_confidence`` carries ``confidence=0.0`` and abstains
    from the industry score bonus until confirmed; ``confirmed`` omits the
    confidence stamp (the confirmation itself is the signal). (PR-225)
    """
    payload: dict[str, object] = {
        "industry_vertical": label,
        "industry_source": source,
        "industry_classified_at": date.today().isoformat(),
        "industry_vertical_status": status,
    }
    if status == "low_confidence":
        payload["industry_vertical_confidence"] = 0.0
    return payload


def build_anthropic_client():
    """DEPRECATED (F-PR-9). Returns None unconditionally.

    Per §0 invariant #11, the engine no longer holds ``ANTHROPIC_API_KEY``
    or imports the ``anthropic`` SDK. Production LLM steps surface to
    the parent slash-command session via
    ``workflows.llm_dispatch.request_llm_dispatch``, gated by the
    ``OUTBOUND_USE_LLM_DISPATCH=1`` env var the skill exports before
    invoking ``cli.py``.

    Kept for caller compatibility (cli.py / weekly_prospect call this
    and pass the result through to ``classify_industry`` etc.) — those
    workflow functions take the dispatch path when ``anthropic_client``
    is None and dispatch is enabled. A future PR can remove the
    callers + delete this function.
    """
    return None


def classify_industry(
    company_name: str | None,
    domain: str | None = None,
    *,
    anthropic_client=None,
    use_llm_dispatch: bool = False,
) -> str | None:
    """Classify a company into one of the 11 INDUSTRY_LABELS.

    Returns one of the canonical label strings from INDUSTRY_LABELS.keys()
    OR None when classification could not run (dispatch off + no client,
    dispatch timeout, parse error). Returns "Other" only when the LLM
    CONFIRMED the company is not a manufacturer.

    The None-vs-"Other" distinction is load-bearing: the scorer applies a
    -25 penalty for "Other" (real off-ICP signal), but treats None as
    neutral (data missing). Conflating the two would silently penalize real
    manufacturers any time Haiku rate-limits or the network blips.

    A falsy company_name (empty string or None) returns None — there's
    nothing to classify, treat as data missing.

    Args:
        use_llm_dispatch: F-PR-9 production flag. When True and no
            ``anthropic_client`` is injected, surfaces to the parent
            slash-command session via ``request_llm_dispatch`` (§0
            invariant #11). When False (default, safe for tests), the
            no-client path returns None.
    """
    from models.campaign import INDUSTRY_LABELS

    valid_labels = set(INDUSTRY_LABELS.keys())

    if not company_name:
        logger.warning("classify_industry called with empty company_name")
        return None

    user_parts = [f"Company: {company_name}"]
    if domain is not None:
        user_parts.append(f"Domain: {domain}")
    user_content = "\n".join(user_parts)

    if anthropic_client is None:
        from workflows.llm_dispatch import (
            CostCeilingExhausted,
            LLMDispatchFailed,
            LLMDispatchTimeout,
            is_dispatch_enabled,
            request_llm_dispatch,
        )
        if not (use_llm_dispatch or is_dispatch_enabled()):
            return None
        try:
            dispatch_result = request_llm_dispatch(
                step="industry_classification",
                prompt=user_content,
                system=CLASSIFIER_SYSTEM_PROMPT,
                model_class="haiku",
                max_tokens=CLASSIFIER_MAX_TOKENS,
                schema_hint=(
                    f"Return ONE label from: {sorted(valid_labels)}. "
                    f"No quotes, no JSON wrapper, just the label."
                ),
            )
            raw_text = dispatch_result.raw_text
        except (LLMDispatchTimeout, LLMDispatchFailed, CostCeilingExhausted) as err:
            # Cost-ceiling caught alongside transient dispatch errors —
            # preserves the None-vs-"Other" semantics so the scorer
            # treats the company as data-missing (neutral) instead of
            # penalizing it. Operators surface cost-ceiling separately
            # via the dispatch log breadcrumbs.
            logger.warning(
                "Industry classifier dispatch error (%s: %s) for %r — treating as unknown",
                type(err).__name__, err, company_name,
            )
            return None
        return _normalize_label(raw_text, valid_labels, company_name)

    try:
        response = anthropic_client.messages.create(
            model=CLASSIFIER_MODEL,
            max_tokens=CLASSIFIER_MAX_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": CLASSIFIER_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_content}],
        )

        # Extract first text block (mirrors quality_gate._llm_qualify)
        raw_text = next(
            (getattr(b, "text", "") for b in (getattr(response, "content", []) or [])
             if getattr(b, "type", None) == "text"),
            "",
        )
        return _normalize_label(raw_text, valid_labels, company_name)

    except Exception as err:  # noqa: BLE001
        logger.warning(
            "Industry classifier API error (%s: %s) for %r — treating as unknown, not Other",
            type(err).__name__, err, company_name,
        )
        return None


def _normalize_label(
    raw_text: str, valid_labels: set[str], company_name: str
) -> str | None:
    """Match the LLM's raw output to a canonical label or return None.

    Hoisted from the inline post-processing so the dispatch + client
    paths share identical normalization logic.
    """
    text = raw_text.strip().strip('."\' `')
    if text in valid_labels:
        return text
    for label in valid_labels:
        if text.lower() == label.lower():
            return label
    logger.warning(
        "Industry classifier returned invalid label %r for %r — treating as unknown",
        text,
        company_name,
    )
    return None


CLASSIFIER_CONFIDENCE_PROMPT = """You classify companies into manufacturing industry verticals.

Return a JSON object with exactly these fields — nothing else, no markdown:
{
  "label": "<label from the list below>",
  "confidence": <float 0.0–1.0>,
  "reasoning": "<one short sentence>"
}

Valid labels (case-sensitive, use & not 'and'):
- Manufacturing (general industrial / discrete / mixed manufacturing)
- Food & Beverage (food processing, beverages, dairy, confectionery)
- Construction (civil engineering, building materials, infrastructure)
- Oil & Gas (upstream, downstream, petrochemical refining)
- Mining (metals extraction, mineral processing, quarrying)
- Automotive (vehicle assembly, auto parts, tier-1/tier-2 suppliers)
- Textiles (apparel, fabric, garments, fiber production)
- Packaging (containers, boxes, labels, flexible packaging)
- Chemicals (industrial chemicals, specialty chemicals, fertilizers)
- Pharma (pharmaceuticals, biotech, medical devices, nutraceuticals)
- Other (any company that is not a manufacturer)

Rules:
- Return ONLY the JSON object — no preamble, no markdown fencing.
- confidence is your certainty that the label is correct (1.0 = certain).
- If the company is not a manufacturer, label = Other.
- Never invent a label outside the list above."""


def classify_with_confidence(
    company_name: str | None,
    domain: str | None = None,
    *,
    anthropic_client=None,
    use_llm_dispatch: bool = False,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> "IndustryVerdict":
    """Classify a company into one of the 11 INDUSTRY_LABELS with confidence.

    Unlike ``classify_industry``, this function ALWAYS returns an
    ``IndustryVerdict`` — never None. The verdict's ``status`` field carries
    the absence-of-knowledge signal:

    - ``"confirmed"``      when LLM label is valid AND confidence ≥ threshold.
    - ``"low_confidence"`` when LLM label is valid BUT confidence < threshold.
                           ``vertical`` is populated; consumers MUST check status.
    - ``"unknown"``        when dispatch fails, label is invalid, or company_name
                           is empty. ``vertical=None``.

    §0 #9 compliance: no silent fallbacks — low-confidence and dispatch
    failures are surfaced as typed statuses, not coerced to "Other" or None.

    When ``status == "low_confidence"``, the function also emits an
    ``industry_low_confidence`` Operator Review Queue row so operators can
    confirm or override the LLM's best guess.

    Args:
        company_name: Company display name to classify. Empty / None → "unknown".
        domain: Optional domain hint (e.g. "bimbo.com") passed to the LLM.
        anthropic_client: Test-injection path (legacy SDK shape). Production
            Production passes None and uses the dispatch path.
        use_llm_dispatch: Explicit dispatch flag (§0 #11). When False and
            dispatch is not enabled via env, returns ``status="unknown"`` without
            calling Haiku.
        confidence_threshold: Score below which status="low_confidence" (default 0.7).
            Tests can override to exercise both branches deterministically.

    Returns:
        IndustryVerdict (always — never None).
    """
    from models.campaign import INDUSTRY_LABELS

    valid_labels = set(INDUSTRY_LABELS.keys())

    _unknown = IndustryVerdict(
        status="unknown",
        confidence=0.0,
        vertical=None,
        reasoning=None,
        model_used=CLASSIFIER_MODEL,
        cost_usd_estimated=Decimal("0"),
    )

    if not company_name:
        logger.warning("classify_with_confidence called with empty company_name")
        return _unknown

    user_parts = [f"Company: {company_name}"]
    if domain is not None:
        user_parts.append(f"Domain: {domain}")
    user_content = "\n".join(user_parts)

    # ── Dispatch / API call ──────────────────────────────────────────
    raw_text: str | None = None

    if anthropic_client is None:
        from workflows.llm_dispatch import (
            CostCeilingExhausted,
            LLMDispatchFailed,
            LLMDispatchTimeout,
            is_dispatch_enabled,
            request_llm_dispatch,
        )
        if not (use_llm_dispatch or is_dispatch_enabled()):
            return _unknown
        try:
            dispatch_result = request_llm_dispatch(
                step="industry_classification_confidence",
                prompt=user_content,
                system=CLASSIFIER_CONFIDENCE_PROMPT,
                model_class="haiku",
                max_tokens=CLASSIFIER_CONFIDENCE_MAX_TOKENS,
                schema_hint=(
                    f'Return JSON: {{"label": "<one of {sorted(valid_labels)}>",'
                    f' "confidence": <0.0-1.0>, "reasoning": "<sentence>"}}'
                ),
            )
            raw_text = dispatch_result.raw_text
        except (LLMDispatchTimeout, LLMDispatchFailed) as err:
            logger.warning(
                "classify_with_confidence dispatch error (%s: %s) for %r — returning unknown",
                type(err).__name__, err, company_name,
            )
            return _unknown
        except CostCeilingExhausted as err:
            logger.warning(
                "classify_with_confidence cost ceiling exhausted for %r — returning unknown",
                company_name,
            )
            # Route to cost_ceiling_breached queue (per PR-22 lesson)
            _emit_cost_ceiling_breached(company_name, err)
            return _unknown
    else:
        # Test-injection path: legacy Anthropic SDK shape preserved.
        try:
            response = anthropic_client.messages.create(
                model=CLASSIFIER_MODEL,
                max_tokens=CLASSIFIER_CONFIDENCE_MAX_TOKENS,
                system=[
                    {
                        "type": "text",
                        "text": CLASSIFIER_CONFIDENCE_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_content}],
            )
            raw_text = next(
                (getattr(b, "text", "") for b in (getattr(response, "content", []) or [])
                 if getattr(b, "type", None) == "text"),
                "",
            )
        except Exception as err:  # noqa: BLE001
            logger.warning(
                "classify_with_confidence API error (%s: %s) for %r — returning unknown",
                type(err).__name__, err, company_name,
            )
            return _unknown

    # ── Parse JSON response ──────────────────────────────────────────
    if not raw_text:
        logger.warning(
            "classify_with_confidence: empty response for %r — returning unknown",
            company_name,
        )
        return _unknown

    parsed = _parse_confidence_response(raw_text, valid_labels, company_name)
    if parsed is None:
        return _unknown

    label, confidence, reasoning = parsed

    # ── Status determination ─────────────────────────────────────────
    if confidence >= confidence_threshold:
        status: Literal["confirmed", "unknown", "low_confidence"] = "confirmed"
    else:
        status = "low_confidence"
        # Emit operator review queue row for low-confidence results
        _emit_industry_low_confidence(company_name, label, confidence, reasoning)

    return IndustryVerdict(
        status=status,
        confidence=confidence,
        vertical=label,
        reasoning=reasoning,
        model_used=CLASSIFIER_MODEL,
        cost_usd_estimated=HAIKU_COST_PER_CALL_USD,
    )


def _parse_confidence_response(
    raw_text: str,
    valid_labels: set[str],
    company_name: str,
) -> tuple[str, float, str] | None:
    """Parse the JSON confidence response from the LLM.

    Returns (label, confidence, reasoning) tuple or None on parse failure.
    The label is normalized via _normalize_label (case-insensitive, strip quotes).
    """
    # Strip markdown fencing if present (LLM sometimes wraps JSON in ```json ... ```)
    stripped = raw_text.strip()
    stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
    stripped = re.sub(r"\s*```$", "", stripped).strip()

    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        logger.warning(
            "classify_with_confidence: JSON parse error for %r (raw: %r) — returning unknown",
            company_name, raw_text[:200],
        )
        return None

    raw_label = str(data.get("label", "")).strip()
    raw_confidence = data.get("confidence", 0.0)
    reasoning = str(data.get("reasoning", "")).strip()

    # Validate confidence is a number in [0, 1]
    try:
        confidence = float(raw_confidence)
        confidence = max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        logger.warning(
            "classify_with_confidence: invalid confidence %r for %r — returning unknown",
            raw_confidence, company_name,
        )
        return None

    # Normalize label (handles case variants, strips punctuation)
    label = _normalize_label(raw_label, valid_labels, company_name)
    if label is None:
        return None

    return (label, confidence, reasoning)


def _emit_industry_low_confidence(
    company_name: str,
    suggested_vertical: str,
    confidence: float,
    reasoning: str | None,
) -> None:
    """Emit an operator review queue row for low-confidence industry classification.

    Idempotency key: ``f"industry_low_confidence|{company_name}|{vertical}|{band}"``
    where band is a 0.1-rounded bucket of confidence so minor score jitter
    doesn't create duplicate rows.
    """
    # Round confidence to nearest 0.1 for stable idempotency key
    confidence_band = f"{round(confidence, 1):.1f}"
    idempotency_key = (
        f"industry_low_confidence|{company_name}|{suggested_vertical}|{confidence_band}"
    )
    try:
        from workflows.escalation import escalate
        from workflows.escalation_schemas import (
            MissingDecisionKey,
            UnknownEscalationType,
        )
        escalate(
            type="industry_low_confidence",
            idempotency_key=idempotency_key,
            payload={
                "company_name": company_name,
                "suggested_vertical": suggested_vertical,
                "confidence": confidence,
                "reasoning": reasoning or "",
                "model_used": CLASSIFIER_MODEL,
            },
        )
    except (SystemExit, KeyboardInterrupt):
        # Never demote interpreter-control exceptions to a warning.
        raise
    except (UnknownEscalationType, MissingDecisionKey):
        # Loud-by-design ValueError subclasses per their docstrings in
        # escalation_schemas.py — silently dropping an escalation with an
        # unknown type or missing decision_key violates §3 hard constraint
        # #9 (no silent fallbacks). Must re-raise.
        raise
    except (ImportError, TypeError, AttributeError):
        # Programmer errors (missing module, wrong call shape, missing
        # attribute) signal an actual bug — silently swallowing them as
        # "transient escalation failure" hid real regressions during PR-25
        # rollout. This tier also catches EscalationSchemaError (a TypeError
        # subclass raised by _validate_payload_against_typeddict on schema
        # drift) — surfaced intentionally so a payload contract change is
        # loud, not silently dropped. Surface them by re-raising so CI /
        # monitoring sees the crash instead of a misleading "operator queue
        # row missing" report.
        raise
    except Exception as err:  # noqa: BLE001
        # Transient runtime errors (network, Attio 5xx, anything else that
        # isn't a contract violation). Escalation failure must NOT abort
        # the main classify flow on transient infra — the verdict is still
        # valid.
        logger.warning(
            "classify_with_confidence: escalation emit failed for %r: %s",
            company_name, err,
        )


def _emit_cost_ceiling_breached(company_name: str, err: Exception) -> None:
    """Emit cost_ceiling_breached queue row when dispatch exhausts the budget.

    Exception discipline mirrors ``_emit_industry_low_confidence`` (PR #111
    / FU-3): programmer errors and loud-by-design escalation contract
    violations propagate; transient runtime errors log-and-continue so an
    Attio 5xx doesn't abort the main classify flow.
    """
    try:
        from workflows.escalation import escalate
        from workflows.escalation_schemas import (
            MissingDecisionKey,
            UnknownEscalationType,
        )
        escalate(
            type="cost_ceiling_breached",
            idempotency_key=f"industry_confidence_cost_ceiling|{company_name}",
            payload={
                "step": "industry_classification_confidence",
                "cap": getattr(err, "cap", 0.0),
                "consumed": getattr(err, "consumed", 0.0),
                "context": f"classify_with_confidence({company_name!r})",
            },
        )
    except (SystemExit, KeyboardInterrupt):
        # Never demote interpreter-control exceptions to a warning.
        raise
    except (UnknownEscalationType, MissingDecisionKey):
        # Loud-by-design ValueError subclasses per their docstrings in
        # escalation_schemas.py — silently dropping an escalation with an
        # unknown type or missing decision_key violates §3 hard constraint
        # #9 (no silent fallbacks). Must re-raise.
        raise
    except (ImportError, TypeError, AttributeError):
        # Programmer errors (missing module, wrong call shape, missing
        # attribute) signal an actual bug — silently swallowing them as
        # "transient escalation failure" hid real regressions during PR-25
        # rollout. This tier also catches EscalationSchemaError (a TypeError
        # subclass raised by _validate_payload_against_typeddict on schema
        # drift) — surfaced intentionally so a payload contract change is
        # loud, not silently dropped. Surface them by re-raising so CI /
        # monitoring sees the crash instead of a misleading "operator queue
        # row missing" report.
        raise
    except Exception as emit_err:  # noqa: BLE001
        # Transient runtime errors (network, Attio 5xx, anything else that
        # isn't a contract violation). Escalation failure must NOT abort
        # the main classify flow on transient infra — the verdict is still
        # valid.
        logger.warning(
            "classify_with_confidence: cost_ceiling_breached escalation failed for %r: %s",
            company_name, emit_err,
        )


def backfill_missing_industries(
    attio,
    *,
    anthropic_client=None,
    limit: int | None = None,
    dry_run: bool = False,
    mig_run: "MigrationRunWriter | None" = None,
    rc_run: "ReclassificationRunWriter | None" = None,
) -> dict:
    """Scan all companies in Attio, classify any missing industry_vertical using
    classify_industry, and PATCH the company record. Idempotent — only touches
    records without a set industry_vertical.

    Args:
        attio: AttioClient instance.
        anthropic_client: Optional injected client (tests only). Per §0
            invariant #11, the production engine does not hold ANTHROPIC_API_KEY.
            When None, classify_industry routes to the LLM dispatch path
            when ``$OUTBOUND_USE_LLM_DISPATCH=1`` (set by the parent skill).
        limit: If set, stop after classifying this many missing records
            (total_scanned still counts all companies paged).
        dry_run: If True, count but do not write to Attio.
        mig_run: Optional MigrationRunWriter to track row modifications.
        rc_run: Optional ReclassificationRunWriter to track classification results.

    Returns:
        Summary dict with keys: total_scanned, missing, classified, written,
        api_errors, skipped.
    """
    summary = {
        "total_scanned": 0,
        "missing": 0,
        "classified": 0,
        "written": 0,
        "api_errors": 0,
        "skipped": 0,
    }

    # NOTE: bump if total_scanned ever equals the cap (means the pagination
    # cap was hit and some companies may have been missed). Raised 2000→10000
    # after the 2000 cap was hit in production. (PR-225)
    BACKFILL_SCAN_LIMIT = 10000
    all_companies = attio.search_companies(filter_=None, limit=BACKFILL_SCAN_LIMIT)

    summary["total_scanned"] = len(all_companies)
    if len(all_companies) >= BACKFILL_SCAN_LIMIT:
        logger.warning(
            "backfill_missing_industries: pagination cap of %d hit — some companies "
            "may have been missed. Increase BACKFILL_SCAN_LIMIT if the database grew.",
            BACKFILL_SCAN_LIMIT,
        )

    # Filter: keep only records missing industry_vertical
    missing_records = []
    for record in all_companies:
        if mig_run:
            mig_run.examine()
        if rc_run:
            rc_run.examine()

        iv = record.get("values", {}).get("industry_vertical", [])
        if not iv:
            missing_records.append(record)
            continue
        # iv is present but may have an empty option
        first = iv[0]
        if isinstance(first, dict) and not first.get("option", {}).get("title", ""):
            missing_records.append(record)
        else:
            # Record already has industry_vertical set
            if mig_run:
                mig_run.skip_idempotent()
            if rc_run:
                rc_run.mark_success(
                    record_id=record.get("id", {}).get("record_id", "unknown"),
                    object="companies",
                    value=first.get("option", {}).get("title", "unknown") if isinstance(first, dict) else "unknown",
                )

    summary["missing"] = len(missing_records)

    # Apply limit kwarg
    to_classify = missing_records if limit is None else missing_records[:limit]
    n = len(to_classify)

    for i, record in enumerate(to_classify, 1):
        values = record.get("values", {})

        # Extract name
        name_data = values.get("name", [])
        if not name_data:
            summary["skipped"] += 1
            if mig_run:
                mig_run.skip_excluded(reason="missing_name")
            if rc_run:
                rc_run.mark_abstain(record_id="unknown", reason="missing_name")
            continue
        name = name_data[0].get("value", "") if isinstance(name_data[0], dict) else ""
        if not name:
            summary["skipped"] += 1
            if mig_run:
                mig_run.skip_excluded(reason="missing_name")
            if rc_run:
                rc_run.mark_abstain(record_id="unknown", reason="missing_name")
            continue

        # Extract domain (first domain entry)
        domain = None
        domain_data = values.get("domains", [])
        if domain_data and isinstance(domain_data, list):
            first_domain = domain_data[0]
            if isinstance(first_domain, dict):
                domain = first_domain.get("domain") or first_domain.get("value")

        record_id = record.get("id", {}).get("record_id", "")
        if not record_id:
            summary["skipped"] += 1
            if mig_run:
                mig_run.skip_excluded(reason="missing_record_id")
            if rc_run:
                rc_run.mark_abstain(record_id="unknown", reason="missing_record_id")
            continue

        label = classify_industry(name, domain, anthropic_client=anthropic_client)
        if label is None:
            # Classifier failed — skip the write rather than corrupt the
            # company's industry_vertical with a default. The next backfill
            # run picks it up again (still null).
            summary["api_errors"] += 1
            if mig_run:
                mig_run.mark_failed(record_id=record_id, error="classify_industry_returned_none")
            if rc_run:
                rc_run.mark_error(record_id=record_id, error="classify_industry_returned_none")
            continue
        summary["classified"] += 1

        if not dry_run:
            try:
                # PR-25 fold-in: write industry_vertical_status="low_confidence"
                # alongside the legacy industry_vertical. classify_industry returns
                # only the label (no confidence signal), so we cannot mark
                # backfilled rows as "confirmed" — that would silently promote
                # them past the PR-25 abstain gate in quality_gate._industry_score.
                # "low_confidence" with confidence=0.0 surfaces these to operator
                # review (industry_low_confidence queue) for confirmation.
                # The status now plumbs into score_prospect at ingest (PR-225).
                attio.update_company(record_id, build_classifier_payload(label))
                summary["written"] += 1
                if mig_run:
                    mig_run.mark_modified(record_id=record_id, object="companies")
                if rc_run:
                    rc_run.mark_success(record_id=record_id, object="companies", value=label)
            except httpx.HTTPStatusError as err:
                logger.warning(
                    "backfill_missing_industries: update_company failed for %r (%s): %s",
                    name,
                    record_id,
                    err,
                )
                summary["api_errors"] += 1
                if mig_run:
                    mig_run.mark_failed(record_id=record_id, error=str(err))
                if rc_run:
                    rc_run.mark_error(record_id=record_id, error=str(err))
        else:
            # dry_run: count but don't write
            if mig_run:
                mig_run.mark_modified(record_id=record_id, object="companies")
            if rc_run:
                rc_run.mark_success(record_id=record_id, object="companies", value=label)

        if i % 20 == 0:
            print(f"  Classified {i}/{n}...")

        time.sleep(0.2)

    return summary
