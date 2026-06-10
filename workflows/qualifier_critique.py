"""LLM critique step for the ICP auto-research loop.

Karpathy-style: take the deterministic DiagnosticReport, hand it to Claude
Sonnet alongside the live `quality_gate.py` rules, and ask for ONE concrete
change with a hypothesis. The system rubric is prompt-cached so weekly runs
amortize the ~600-token rubric cost across all calls.

Hard rules enforced in the prompt + the parser:
  - ONE change only.
  - Action must be in the allowed set.
  - Predicted lift ≥ 3pp for any non-no_change action (else fall back to no_change).
  - Modifiable surface is `workflows/quality_gate.py` (or a `handoff_to_sales_learn`).

The propose_change entry point never raises — any LLM failure or malformed
output collapses to a `no_change` proposal so the weekly run keeps moving.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from workflows.qualifier_diagnostic import DiagnosticReport

logger = logging.getLogger(__name__)

CRITIQUE_MODEL = "claude-sonnet-4-6"
CRITIQUE_MAX_TOKENS = 800

# Minimum predicted lift to justify any code-touching action. Below this we
# coerce to `no_change` so the operator isn't asked to merge marginal PRs.
MIN_PREDICTED_LIFT_PP = 3

VALID_ACTIONS = frozenset({
    "weight_change",
    "keyword_change",
    "threshold_change",
    "handoff_to_sales_learn",
    "no_change",
})


# ── Rubric (cached system prompt) ─────────────────────────────────────────────
#
# Kept terse on purpose — the LLM doesn't need our entire codebase, it needs
# the constraints, the modifiable surface, and the output format. The diagnostic
# report (per-run, uncached) carries the variable evidence.

_RUBRIC = """You are an ICP qualifier auto-research analyst for a B2B sales agent. You
receive a weekly funnel diagnostic and must propose at most ONE concrete change
to the qualifier.

# ICP definitions (do not alter — these are the system's invariants)

The operator's ICP — the lanes that pass, the target buyer titles, and the hard
disqualifiers — is defined in config/icp.yaml (falling back to
config/icp.example.yaml) and is loaded into the deterministic scorer. Treat
those definitions as fixed invariants: your job is to tune HOW the scorer
weights and matches signals, never to redefine WHO the ICP is.

# Modifiable surface

You may propose ONE change to `workflows/quality_gate.py`:

- weight_change: adjust a numeric weight (e.g., INDUSTRY_BONUS_IN_ICP, score
  added to a band like 28/22/18/12/5/2)
- keyword_change: add/remove a single keyword in a list
  (DECISION_MAKER_KEYWORDS, OPERATIONS_KEYWORDS, DIGITALIZATION_KEYWORDS,
  COMPETITOR_KEYWORDS, ACADEMIC_KEYWORDS, CONSULTANT_KEYWORDS)
- threshold_change: shift one of the cutoffs (40, 60, 75) by at most 5 points
- handoff_to_sales_learn: when the diagnostic says the leak is the message,
  not the qualifier — refer to the existing /sales-learn DM-variant loop
  (no quality_gate.py change)
- no_change: if no candidate clears the 3pp predicted-lift bar, return this

# Hard rules

1. ONE change per proposal. Multi-variable changes are forbidden.
2. Predicted lift ≥ 3pp on DM response rate, OR action = no_change.
3. Justify with the actual disagreement examples from the diagnostic, not
   abstract reasoning.
4. NEVER alter the ICP definitions or hard-disqualifier rules — only weights,
   keywords, and thresholds inside the deterministic scoring.
5. NEVER propose changes to messages.json, personas.json, or any file other
   than workflows/quality_gate.py (use handoff_to_sales_learn for message work).

# Output format

Reply with EXACTLY one JSON object and nothing else:

{
  "action": "<weight_change|keyword_change|threshold_change|handoff_to_sales_learn|no_change>",
  "target_file": "<path or empty>",
  "specific_change": "<concrete diff, e.g. 'INDUSTRY_PENALTY_OFF_ICP: -25 → -30'>",
  "hypothesis": "<1-2 sentences explaining the causal claim>",
  "predicted_lift_pp": <integer 0-15>,
  "why_not_others": "<which other candidates you considered and rejected, in 1-2 sentences>"
}

No prose, no code fences, no preamble. Just the JSON."""


# ── Dataclass ─────────────────────────────────────────────────────────────────

@dataclass
class CritiqueProposal:
    action: str
    target_file: str
    specific_change: str
    hypothesis: str
    predicted_lift_pp: int
    why_not_others: str

    def to_markdown(self) -> str:
        """Render the proposal as a markdown block for the weekly brain."""
        return (
            f"### Auto-research proposal\n\n"
            f"- **Action:** `{self.action}`\n"
            f"- **Target file:** `{self.target_file or 'n/a'}`\n"
            f"- **Specific change:** {self.specific_change or '_n/a_'}\n"
            f"- **Predicted lift:** {self.predicted_lift_pp}pp\n"
            f"- **Hypothesis:** {self.hypothesis}\n"
            f"- **Why not others:** {self.why_not_others or '_n/a_'}\n"
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

_FALLBACK = CritiqueProposal(
    action="no_change",
    target_file="",
    specific_change="",
    hypothesis="LLM critique unavailable or malformed; falling back to no_change.",
    predicted_lift_pp=0,
    why_not_others="",
)


def _strip_fences(text: str) -> str:
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _parse_proposal(raw: str) -> CritiqueProposal:
    """Parse the LLM's JSON response, validating fields. Coerce-to-no_change on bad input."""
    try:
        parsed = json.loads(_strip_fences(raw))
    except (ValueError, json.JSONDecodeError):
        return CritiqueProposal(
            action="no_change",
            target_file="",
            specific_change="",
            hypothesis="LLM output was not valid JSON — fallback to no_change.",
            predicted_lift_pp=0,
            why_not_others="",
        )

    if not isinstance(parsed, dict):
        return _FALLBACK

    action = str(parsed.get("action") or "").strip()
    if action not in VALID_ACTIONS:
        return CritiqueProposal(
            action="no_change",
            target_file=str(parsed.get("target_file") or ""),
            specific_change=str(parsed.get("specific_change") or ""),
            hypothesis=(
                f"LLM returned invalid action '{action}'; fallback to no_change. "
                f"Original hypothesis: {parsed.get('hypothesis', '')[:200]}"
            ),
            predicted_lift_pp=0,
            why_not_others=str(parsed.get("why_not_others") or ""),
        )

    try:
        lift = int(parsed.get("predicted_lift_pp") or 0)
    except (TypeError, ValueError):
        lift = 0

    code_actions = {"weight_change", "keyword_change", "threshold_change"}
    if action in code_actions and lift < MIN_PREDICTED_LIFT_PP:
        return CritiqueProposal(
            action="no_change",
            target_file=str(parsed.get("target_file") or ""),
            specific_change=str(parsed.get("specific_change") or ""),
            hypothesis=(
                f"LLM proposed {action} with lift {lift}pp < {MIN_PREDICTED_LIFT_PP}pp "
                f"threshold; coerced to no_change. Original: "
                f"{parsed.get('hypothesis', '')[:200]}"
            ),
            predicted_lift_pp=lift,
            why_not_others=str(parsed.get("why_not_others") or ""),
        )

    return CritiqueProposal(
        action=action,
        target_file=str(parsed.get("target_file") or ""),
        specific_change=str(parsed.get("specific_change") or ""),
        hypothesis=str(parsed.get("hypothesis") or ""),
        predicted_lift_pp=lift,
        why_not_others=str(parsed.get("why_not_others") or ""),
    )


def _format_segment(seg) -> str:
    return f"{seg.label()} (n={seg.n_dmed}, reply rate {seg.dm_response_rate:.1%})"


def _build_user_content(report: DiagnosticReport) -> str:
    """Assemble the per-run, uncached portion of the prompt."""
    top = "\n".join(f"  - {_format_segment(s)}" for s in report.top_segments) or "  - (none)"
    bot = "\n".join(f"  - {_format_segment(s)}" for s in report.bottom_segments) or "  - (none)"

    high_no = "\n".join(
        f"  - {ex.name} ({ex.title} @ {ex.company}, score={ex.quality_score}): {ex.score_breakdown_text}"
        for ex in report.disagreement_high_no_reply[:10]
    ) or "  - (none)"
    low_yes = "\n".join(
        f"  - {ex.name} ({ex.title} @ {ex.company}, score={ex.quality_score}): {ex.score_breakdown_text}"
        for ex in report.disagreement_low_yes_reply[:10]
    ) or "  - (none)"

    handoff_hint = ""
    if report.verdict == "message":
        handoff_hint = (
            "\n\nThe diagnostic verdict is 'message problem'. The qualifier "
            "appears to discriminate poorly between segments AT THE COPY LEVEL — "
            "consider returning action='handoff_to_sales_learn' rather than "
            "tuning quality_gate.py."
        )
    elif report.verdict == "insufficient":
        handoff_hint = (
            "\n\nThe diagnostic verdict is 'insufficient data'. Strong default "
            "is action='no_change' until more cohorts mature."
        )

    return (
        f"DIAGNOSTIC REPORT\n\n"
        f"Verdict: {report.verdict}\n"
        f"Rationale: {report.rationale}\n\n"
        f"Overall: {report.n_total_dmed} DM'd · "
        f"reply rate {report.overall_response_rate:.1%} · "
        f"defensive rate {report.overall_defensive_rate:.1%} · "
        f"connect accept rate {report.overall_accept_rate:.1%} "
        f"(of {report.n_total_connect_eligible} sent).\n\n"
        f"Top reply-rate segments:\n{top}\n\n"
        f"Bottom reply-rate segments:\n{bot}\n\n"
        f"Disagreement examples (high score, no reply):\n{high_no}\n\n"
        f"Disagreement examples (low score, did reply):\n{low_yes}\n\n"
        f"{handoff_hint}\n\n"
        f"Propose ONE change per the rubric. Output the JSON object only."
    )


# ── Public entry point ────────────────────────────────────────────────────────

def propose_change(report: DiagnosticReport, anthropic_client) -> CritiqueProposal:
    """Call Claude Sonnet for a structured proposal. Never raises."""
    if report.verdict == "insufficient":
        # Short-circuit — no point spending a Sonnet call when we already
        # know we don't have enough data. Saves cost and keeps the brain crisp.
        return CritiqueProposal(
            action="no_change",
            target_file="",
            specific_change="",
            hypothesis=(
                "Diagnostic verdict is 'insufficient data'. Skipping LLM critique "
                "until more cohorts mature."
            ),
            predicted_lift_pp=0,
            why_not_others="",
        )

    if anthropic_client is None:
        return _FALLBACK

    user_content = _build_user_content(report)
    try:
        response = anthropic_client.messages.create(
            model=CRITIQUE_MODEL,
            max_tokens=CRITIQUE_MAX_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": _RUBRIC,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_content}],
        )
    except Exception as err:  # noqa: BLE001 — fallback boundary
        logger.warning("LLM critique failed (%s): %s", type(err).__name__, err)
        return CritiqueProposal(
            action="no_change",
            target_file="",
            specific_change="",
            hypothesis=f"LLM critique error ({type(err).__name__}); fallback to no_change.",
            predicted_lift_pp=0,
            why_not_others="",
        )

    raw_text = ""
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            raw_text = block.text
            break
        if isinstance(block, dict) and block.get("type") == "text":
            raw_text = block.get("text", "")
            break
        text = getattr(block, "text", None)
        if text is not None:
            raw_text = text
            break

    return _parse_proposal(raw_text)


# Re-export Path for test imports if needed.
_PATH = Path(__file__)
