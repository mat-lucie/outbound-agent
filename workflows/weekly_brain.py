"""Weekly brain — measures last week's cohorts, pre-screens candidate variants,
and writes a markdown proposal for operator approval.

Invoked by the `cli.py weekly-brain` command on Sunday evenings. The output
is a markdown file in `docs/experiments/` following the format documented
in DESIGN_SPEC.md > Guidelines for Markdown Proposals:

- Lead with the decision the operator has to make
- Show variant copy inline (not linked)
- Show last cohort's defensive replies inline
- State synthetic scores as ranges, not point estimates

PR-32 adds an agent-readable JSON sidecar (``<experiment_id>-proposal.json``)
emitted alongside the markdown, plus an optional ``open_proposal_pr`` helper
that drives a ``gh pr create`` flow via ``subprocess.run`` (per Round-4 D8 —
``mcp__github__*`` may be unavailable, so we use the gh CLI for portability).
``variant_proposal_pending`` queue rows are then opened so ``sales-approve``
can poll them later.

This module is a pure orchestration layer — all the measurement lives in
workflows/learn.py, all the scoring lives in workflows/synthetic_prescreen.py.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from workflows.learn import measure_cohorts

if TYPE_CHECKING:
    from collections.abc import Callable
from workflows.synthetic_prescreen import (
    load_synthetic_personas,
    score_variant_matrix,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "docs" / "experiments"
MESSAGES_PATH = PROJECT_ROOT / "content" / "messages.json"

# How many defensive reply samples to show in the proposal (per persona).
DEFENSIVE_SAMPLE_LIMIT = 3

# DM1 variants we currently run. Extend here when new arms ship.
DM1_VARIANT_KEYS = ("dm1", "dm1_v1", "dm1_v3")


@dataclass
class VariantScore:
    variant_id: str
    variant_text: str
    scores_by_persona: dict  # synthetic persona key → {defensive, engagement, reasoning}


def load_dm1_variants(messages_path: Path | None = None) -> dict[str, dict[str, str]]:
    """Return {sales_persona_key: {variant_id: dm1_text_es}} for every persona.

    We only pre-screen the Spanish DM1 strings — Spanish is the dominant
    language in the LATAM cohort and the synthetic personas are Spanish-
    speaking. English and Portuguese variants ride along via the V1/V3
    arms once the Spanish proposal is approved.
    """
    path = messages_path or MESSAGES_PATH
    with open(path, encoding="utf-8") as f:
        messages = json.load(f)

    variants_by_persona: dict[str, dict[str, str]] = {}
    for persona_key, persona_data in messages.items():
        variants: dict[str, str] = {}
        for variant_key in DM1_VARIANT_KEYS:
            step = persona_data.get(variant_key)
            if not step:
                continue
            es_text = step.get("es")
            if es_text:
                variants[variant_key] = es_text
        if variants:
            variants_by_persona[persona_key] = variants
    return variants_by_persona


def collect_defensive_samples(
    attio,
    experiment_id: str | None = None,
    persona: str | None = None,
    limit: int = DEFENSIVE_SAMPLE_LIMIT,
) -> list[str]:
    """Return up to `limit` defensive reply texts from Attio.

    Pulls from the same list entries measure_cohorts uses. Filtering is
    applied BEFORE the limit so a generous limit with a narrow filter
    returns all matching samples rather than truncating at non-matching
    entries.

    Args:
        attio: AttioClient (real or mock)
        experiment_id: If set, only return samples from this experiment cohort.
        persona: If set, only return samples from this sales persona. The
            proposal shows defensive samples per sales persona so the operator
            can see which reframe the reactance is pushing back on.
        limit: Max samples to return.
    """
    # fail_if_truncated (PR-220): sampling from a silently truncated sweep skews
    # the defensive-reply pool; fail loudly so the limit gets raised instead.
    raw_entries = attio.query_list_entries(limit=5000, fail_if_truncated=True)
    parsed = [attio.parse_entry(e) for e in raw_entries]
    # §3.11 soft-delete filter: skip union-merge losers. Their winners
    # carry the response_classification; reading the loser would double-
    # count a single defensive reply when sampling for variant proposals.
    parsed = [entry for entry in parsed if not entry.get("merged_into")]

    samples: list[str] = []
    for entry in parsed:
        if experiment_id and entry.get("experiment_id") != experiment_id:
            continue
        if persona and entry.get("persona") != persona:
            continue
        if entry.get("response_classification") != "defensive":
            continue
        text = entry.get("last_response_text")
        if not text:
            continue
        samples.append(text)
        if len(samples) >= limit:
            break
    return samples


def _format_range(rng: list[float]) -> str:
    lo, hi = rng
    return f"[{lo:.2f}, {hi:.2f}]"


def _format_score_row(evaluator: str, scores: dict) -> str:
    return (
        f"| {evaluator} "
        f"| {_format_range(scores['defensive_likelihood'])} "
        f"| {_format_range(scores['engagement_likelihood'])} "
        f"| {scores.get('reasoning', '').strip()[:80]} |"
    )


def _format_variant_block(
    variant_id: str,
    variant_text: str,
    scores_by_persona: dict,
) -> str:
    lines = [
        f"#### Variant `{variant_id}`",
        "",
        "```",
        variant_text,
        "```",
        "",
        "**Synthetic pre-screen (scored vs. 3 evaluator personas):**",
        "",
        "| Evaluator | Defensive likelihood | Engagement likelihood | Reasoning |",
        "|---|---|---|---|",
    ]
    for persona_key, scores in scores_by_persona.items():
        lines.append(_format_score_row(persona_key, scores))
    lines.append("")
    return "\n".join(lines)


def _format_cohort_summary(cohort: dict) -> str:
    """Render a cohort's per-step rates with explicit n_observed.

    Per-step rates show n_observed alongside the posterior mean so the
    operator can distinguish "9% from n=2 (prior-dominated)" from
    "9% from n=200 (converged)". §0 #9 invariant — surface uncertainty.
    """
    rate = cohort.get("dm_response_rate", 0.0)
    defensive_rate = cohort.get("defensive_rate", 0.0)

    # Per-step breakdown with provenance (n_observed)
    step_lines = []
    for step in (1, 2, 3):
        posterior = cohort.get(f"dm{step}_posterior")
        if posterior is not None:
            n_obs = posterior.get("n_observed", 0)
            step_rate = posterior.get("mean", 0.0)
        else:
            n_obs = cohort.get(f"dm{step}_received", 0)
            step_rate = cohort.get(f"dm{step}_response_rate", 0.0) or 0.0
        marker = " ⚠ prior-dominated" if n_obs < 5 else ""
        step_lines.append(
            f"  - dm{step}: {step_rate:.1%} (n={n_obs}){marker}"
        )

    # PR-22 disclosure: surface archaeology-inferred cohort members alongside
    # the live DM'd count so operators can distinguish "freshly-running
    # cohort" from "mostly historical attribution".
    archaeology_count = cohort.get("archaeology_inferred_count", 0)
    archaeology_line = (
        f" · Archaeology-inferred: {archaeology_count}"
        if archaeology_count > 0
        else ""
    )

    return (
        f"- Experiment: `{cohort['experiment_id']}`\n"
        f"- DM'd: {cohort['dmed']} · Responded: {cohort['responded']} "
        f"· Response rate: {rate:.1%}{archaeology_line}\n"
        f"- Defensive rate: {defensive_rate:.1%}\n"
        + "\n".join(step_lines) + "\n"
        f"- Mature: {cohort['is_mature']} · Days running: {cohort['days_running']}"
    )


def _format_defensive_samples(samples: list[str]) -> str:
    if not samples:
        return "_No defensive replies observed in this cohort._\n"
    lines = []
    for text in samples:
        quoted = text.replace("\n", " ").strip()
        lines.append(f"> {quoted}")
        lines.append("")
    return "\n".join(lines)


def _q75(rng: list[float]) -> float:
    """75th percentile of a [min, max] range.

    For a uniform distribution over [a, b], the 75th percentile is
    a + 0.75 * (b - a). Used for defensive ranking: a wide uncertainty
    range like [0.10, 0.80] yields q75 = 0.625, penalizing it more
    conservatively than midpoint (0.45). This surfaces variants whose
    worst-case defensive outcome is bounded, not just whose expected value
    is low.
    """
    a, b = rng[0], rng[1]
    return a + 0.75 * (b - a)


def _q25(rng: list[float]) -> float:
    """25th percentile of a [min, max] range.

    For a uniform distribution over [a, b], the 25th percentile is
    a + 0.25 * (b - a). Used for engagement ranking: a wide uncertainty
    range like [0.20, 0.80] yields q25 = 0.35, penalizing it more
    conservatively than midpoint (0.50). This surfaces variants whose
    worst-case engagement outcome is bounded below, not just whose
    expected value is high.
    """
    a, b = rng[0], rng[1]
    return a + 0.25 * (b - a)


def _avg_q75_defensive(v: VariantScore) -> float:
    """Average q75 defensive score across all synthetic personas.

    B-MW-RANK (PR-24): uses the 75th percentile instead of midpoint.
    A variant with a wide defensive uncertainty range [0.10, 0.80] gets
    q75 = 0.625 rather than midpoint 0.45, penalizing wide ranges more
    conservatively. Only variants whose WORST-CASE defensive posture is
    bounded surface as top recommendations — not just variants that look
    good on average.
    """
    q75s = [
        _q75(s["defensive_likelihood"])
        for s in v.scores_by_persona.values()
    ]
    return sum(q75s) / len(q75s) if q75s else 1.0


def _avg_q25_engagement(v: VariantScore) -> float:
    """Average q25 engagement score across all synthetic personas.

    B-MW-RANK (PR-24): uses the 25th percentile instead of midpoint.
    A variant with wide engagement uncertainty [0.20, 0.80] gets
    q25 = 0.35 rather than midpoint 0.50, penalizing wide ranges
    conservatively. Variants with narrow, consistently-high engagement
    ranges rank above variants with wide ranges whose expected value
    happens to be similar.
    """
    q25s = [
        _q25(s["engagement_likelihood"])
        for s in v.scores_by_persona.values()
    ]
    return sum(q25s) / len(q25s) if q25s else 0.0


def _recommend_variant(variant_scores: dict[str, VariantScore]) -> tuple[str, str]:
    """Pick the variant with the lowest mean q75-defensive score across evaluators,
    tie-broken by highest mean q25-engagement score.

    B-MW-RANK (PR-24): switched from midpoint to q75/q25 to penalize
    wide uncertainty ranges on the worst-side end of each axis. A variant
    with defensive uncertainty [0.10, 0.80] has midpoint 0.45 but q75
    0.625 — the q75 ranking correctly treats the wide range as a risk
    signal, not just "expected to be moderate." Same logic applies to
    engagement via q25: a variant with [0.20, 0.80] has midpoint 0.50
    but q25 0.35, reflecting that the downside engagement outcome is
    genuinely uncertain.

    Deliberate departure from the prior midpoint design: midpoint was
    symmetric (treats wide and narrow ranges identically); q75/q25 is
    conservative (penalizes width on the loss-side of each dimension).
    The prior midpoint tests pinning the exact formula are updated
    to match.

    Returns:
        (variant_id, rationale)
    """
    if not variant_scores:
        return "", "No variants to recommend."

    ranked = sorted(
        variant_scores.values(),
        key=lambda v: (_avg_q75_defensive(v), -_avg_q25_engagement(v)),
    )
    best = ranked[0]
    rationale = (
        f"Variant `{best.variant_id}` has the lowest mean q75-defensive "
        f"({_avg_q75_defensive(best):.2f}) with q25-engagement "
        f"({_avg_q25_engagement(best):.2f}). Recommend shipping."
    )
    return best.variant_id, rationale


def build_proposal_markdown(
    experiment_id: str,
    variants_by_persona: dict[str, dict[str, str]],
    scored: dict[str, dict[str, VariantScore]],
    cohorts: list[dict],
    defensive_samples_by_persona: dict[str, list[str]],
) -> str:
    """Render the full proposal markdown.

    Args:
        experiment_id: Target experiment id (e.g. "exp-003").
        variants_by_persona: sales_persona_key → variant_id → variant_text
        scored: sales_persona_key → variant_id → VariantScore
        cohorts: output of measure_cohorts
        defensive_samples_by_persona: sales_persona_key → list[str]
    """
    today = date.today().isoformat()

    lines: list[str] = [
        f"# Experiment Proposal — `{experiment_id}`",
        "",
        f"**Date:** {today}",
        "**Status:** Draft for approval",
        "",
        "## Decision",
        "",
    ]

    for persona_key, variant_scores in scored.items():
        recommended_id, rationale = _recommend_variant(variant_scores)
        lines.append(
            f"**{persona_key}:** ship `{recommended_id}`. {rationale}"
        )
    lines.append("")

    lines.extend(["## Candidate Variants", ""])
    for persona_key, variant_scores in scored.items():
        lines.append(f"### Sales persona: `{persona_key}`")
        lines.append("")
        for variant_id, vs in variant_scores.items():
            lines.append(_format_variant_block(
                variant_id=variant_id,
                variant_text=vs.variant_text,
                scores_by_persona=vs.scores_by_persona,
            ))

    lines.extend(["## Last Cohort Performance", ""])
    if not cohorts:
        lines.append("_No cohort data available yet._")
        lines.append("")
    else:
        for cohort in cohorts:
            lines.append(_format_cohort_summary(cohort))
            lines.append("")

    lines.extend(["## Defensive Reply Samples", ""])
    if not any(defensive_samples_by_persona.values()):
        lines.append("_No defensive replies observed this cycle._")
        lines.append("")
    else:
        for persona_key, samples in defensive_samples_by_persona.items():
            if not samples:
                continue
            lines.append(f"### `{persona_key}`")
            lines.append("")
            lines.append(_format_defensive_samples(samples))

    lines.extend([
        "## How to approve",
        "",
        "1. Review the recommended variant against the synthetic scores.",
        "2. Read the defensive samples — do they still match the V1/V3 reframe?",
        "3. If approved, run `sales-approve` to merge the experiment branch.",
        "",
    ])

    return "\n".join(lines)


# ============================================================
# PR-33 — DM3 delayed-revisit verdict (REMOVED Wave-2-A 2026-06-01)
# ============================================================
#
# `apply_dm3_revisit_verdict` wrote ``verdict_v1`` / ``verdict_v2`` /
# ``dm3_window_settles_at`` to the Attio ``experiment`` object. That object
# was descoped (never deployed, 404 live), and the function had zero
# production callers — it was dead code that would have crashed on the live
# 404. Removed along with its Attio-record parsers and constants. The
# writer-registry entries for these three slugs are retained for schema
# symmetry per the Wave-2-A plan.


def build_proposal_json_sidecar(
    *,
    experiment_id: str,
    variants_by_persona: dict[str, dict[str, str]],
    scored: dict[str, dict[str, VariantScore]],
    cohorts: list[dict],
    defensive_samples_by_persona: dict[str, list[str]],
) -> dict:
    """Build the agent-readable JSON sidecar payload (PR-32).

    Subagents and the ``sales-approve`` CLI consume this instead of parsing
    the human-readable markdown. Every datapoint that drives the
    recommendation is exposed structurally — synthetic scores, defensive
    samples, cohort evidence, the ranked recommendation.
    """
    recommendations: dict[str, dict] = {}
    for persona_key, variant_scores in scored.items():
        recommended_id, rationale = _recommend_variant(variant_scores)
        recommendations[persona_key] = {
            "recommended_variant_id": recommended_id,
            "rationale": rationale,
            "ranking": [
                {
                    "variant_id": v.variant_id,
                    "q75_defensive": _avg_q75_defensive(v),
                    "q25_engagement": _avg_q25_engagement(v),
                    "variant_text": v.variant_text,
                    "scores_by_persona": v.scores_by_persona,
                }
                for v in sorted(
                    variant_scores.values(),
                    key=lambda v: (_avg_q75_defensive(v), -_avg_q25_engagement(v)),
                )
            ],
        }

    return {
        "experiment_id": experiment_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "recommendations": recommendations,
        "candidate_variants": variants_by_persona,
        "cohort_evidence": cohorts,
        "defensive_samples": defensive_samples_by_persona,
    }


@dataclass
class ProposalPROpenResult:
    """Return shape of ``open_proposal_pr``. Captures both the PR URL and
    the branch name so the ``variant_proposal_pending`` queue row payload
    can carry both for ``sales-approve``."""

    pr_url: str
    branch_name: str


_REPO_ROOT = PROJECT_ROOT


def _run_subprocess(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Subprocess.run wrapper. Always check=True; surfaces stderr in the
    raised CalledProcessError so operator can debug from the traceback.

    Extracted so tests can monkeypatch this single seam instead of every
    subprocess.run call site."""
    return subprocess.run(
        args,
        cwd=cwd or _REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def open_proposal_pr(
    *,
    experiment_id: str,
    proposal_paths: list[Path],
    base_branch: str = "main",
    run_cmd: Callable[..., subprocess.CompletedProcess] = _run_subprocess,
) -> ProposalPROpenResult:
    """Open a draft PR carrying the proposal markdown + JSON sidecar.

    Per plan §5 PR-32 (Round-4 D8): uses ``gh pr create`` via subprocess
    rather than the ``mcp__github__*`` MCP because the MCP may be
    unavailable in arbitrary execution environments. ``run_cmd`` is
    injectable so tests can mock the subprocess layer.

    Steps:
        1. Create a branch ``experiment/<experiment_id>-proposal`` from
           ``base_branch``.
        2. ``git add`` each path in ``proposal_paths``.
        3. ``git commit`` (skip if nothing staged → propagate the
           subprocess error so the caller sees the no-op).
        4. ``git push -u origin <branch>``.
        5. ``gh pr create --draft --base <base> --head <branch>``.

    Returns: ``ProposalPROpenResult(pr_url, branch_name)``.

    Raises:
        subprocess.CalledProcessError: any git/gh failure bubbles up with
            stderr — §0 #9, no silent-swallow.
    """
    branch_name = f"experiment/{experiment_id}-proposal"

    run_cmd(["git", "checkout", "-b", branch_name, base_branch])
    run_cmd(["git", "add", *[str(p) for p in proposal_paths]])
    run_cmd([
        "git", "commit",
        "-m", f"chore(brain): proposal sidecar for {experiment_id}",
    ])
    run_cmd(["git", "push", "-u", "origin", branch_name])

    pr_title = f"weekly_brain proposal: {experiment_id}"
    pr_body = (
        f"Auto-generated by ``workflows.weekly_brain.run_weekly_brain`` for "
        f"experiment ``{experiment_id}``.\n\n"
        f"Review the proposal markdown + JSON sidecar in this PR's diff, "
        f"then run ``sales-approve {experiment_id} --approve`` (or "
        f"``--reject --rationale='...'``) to merge or close."
    )
    result = run_cmd([
        "gh", "pr", "create",
        "--draft",
        "--base", base_branch,
        "--head", branch_name,
        "--title", pr_title,
        "--body", pr_body,
    ])
    pr_url = (result.stdout or "").strip().splitlines()[-1] if result.stdout else ""
    if not pr_url.startswith("http"):
        # §0 #9: no silent fallback to an empty pr_url; if gh's stdout
        # didn't end with a URL, surface the failure so the operator can
        # debug the gh output shape rather than landing a broken queue row.
        raise RuntimeError(
            f"gh pr create did not return a recognizable URL; got {pr_url!r}"
        )
    return ProposalPROpenResult(pr_url=pr_url, branch_name=branch_name)


def run_weekly_brain(
    attio,
    *,
    experiment_id: str,
    variants_by_persona: dict[str, dict[str, str]] | None = None,
    output_dir: Path | None = None,
    anthropic_client=None,
    dry_run: bool = False,
    open_pr: bool = False,
    pr_opener: Callable[..., ProposalPROpenResult] = open_proposal_pr,
) -> Path:
    """Run the weekly brain and write a markdown proposal.

    Args:
        attio: AttioClient (real or mock)
        experiment_id: The experiment_id for the proposal (e.g. "exp-003")
        variants_by_persona: Injectable for tests. Defaults to loading
            DM1 variants from content/messages.json.
        output_dir: Where to write the proposal markdown. Defaults to
            docs/experiments/ at the project root.
        anthropic_client: Injectable for tests.
        dry_run: If True, the markdown is returned but not written to disk.
        open_pr: PR-32 — if True, ``open_proposal_pr`` is invoked and a
            ``variant_proposal_pending`` queue row is opened. Disabled by
            default so existing tests + the weekly-brain CLI don't open a
            PR every run. Operator opts in via ``--open-pr``.
        pr_opener: injectable PR opener (for tests).

    Returns:
        Path to the written proposal markdown (or the intended path if dry_run).
    """
    if variants_by_persona is None:
        variants_by_persona = load_dm1_variants()

    cohorts = measure_cohorts(attio)

    synthetic_persona_keys = list(load_synthetic_personas().keys())

    scored: dict[str, dict[str, VariantScore]] = {}
    for sales_persona_key, variants in variants_by_persona.items():
        matrix = score_variant_matrix(
            variants=variants,
            persona_keys=synthetic_persona_keys,
            anthropic_client=anthropic_client,
        )
        scored[sales_persona_key] = {
            variant_id: VariantScore(
                variant_id=variant_id,
                variant_text=variants[variant_id],
                scores_by_persona=persona_scores,
            )
            for variant_id, persona_scores in matrix.items()
        }

    # Scope defensive samples to the specific persona AND experiment so the
    # proposal shows exactly what reactance the operator is trying to fix
    # for that persona, not a global bag of samples attributed to whoever
    # happens to be in the dict.
    defensive_samples_by_persona: dict[str, list[str]] = {}
    for sales_persona_key in variants_by_persona:
        defensive_samples_by_persona[sales_persona_key] = collect_defensive_samples(
            attio,
            experiment_id=experiment_id,
            persona=sales_persona_key,
            limit=DEFENSIVE_SAMPLE_LIMIT,
        )

    markdown = build_proposal_markdown(
        experiment_id=experiment_id,
        variants_by_persona=variants_by_persona,
        scored=scored,
        cohorts=cohorts,
        defensive_samples_by_persona=defensive_samples_by_persona,
    )

    out_dir = output_dir or DEFAULT_OUTPUT_DIR
    out_path = out_dir / f"{experiment_id}-proposal.md"
    json_path = out_dir / f"{experiment_id}-proposal.json"

    sidecar = build_proposal_json_sidecar(
        experiment_id=experiment_id,
        variants_by_persona=variants_by_persona,
        scored=scored,
        cohorts=cohorts,
        defensive_samples_by_persona=defensive_samples_by_persona,
    )

    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path.write_text(markdown, encoding="utf-8")
        json_path.write_text(
            json.dumps(sidecar, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

        if open_pr:
            # PR-32: open the proposal PR + open variant_proposal_pending so
            # sales-approve can pick it up. Imported lazily to avoid a hard
            # dependency on workflows.escalation for the common no-PR path.
            from workflows.escalation import escalate

            result = pr_opener(
                experiment_id=experiment_id,
                proposal_paths=[out_path, json_path],
            )
            escalate(
                type="variant_proposal_pending",
                idempotency_key=experiment_id,
                payload={
                    "experiment_id": experiment_id,
                    "pr_url": result.pr_url,
                    "branch_name": result.branch_name,
                    "proposal_markdown_path": str(
                        out_path.relative_to(_REPO_ROOT)
                        if out_path.is_relative_to(_REPO_ROOT)
                        else out_path
                    ),
                    "proposal_json_path": str(
                        json_path.relative_to(_REPO_ROOT)
                        if json_path.is_relative_to(_REPO_ROOT)
                        else json_path
                    ),
                    "opened_by": "workflows.weekly_brain.run_weekly_brain",
                },
                attio=attio,
            )

    return out_path
