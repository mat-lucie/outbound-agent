# Design Specification — outbound-agent

## Vision
An autonomous sales agent that *learns*: every week it re-reads what happened, forms a hypothesis, and ships a testable variant — with humans in the loop for taste calls, not mechanics.

## Consumer
Primary consumer: **Agent**

This module has no UI. All outputs are consumed programmatically by:
- `sales-daily` / `sales-weekly` / `sales-learn` / `sales-approve` skills (Claude Code agents)
- PhantomBuster, Attio, LinkedIn Sales Navigator (external systems via API/CSV)
- Human-readable markdown proposals in `docs/experiments/` (these ARE consumed by a human, but as artifacts inside an otherwise-agent flow)

Where the module touches humans directly (DM copy read by prospects, markdown proposals read by the operator), human-first rules still apply to the *content* of those artifacts even though the module itself is agent-consumed.

## Audience
Two audiences, each with different expectations:

1. **The operator** — reads sales-weekly proposals, approves PRs via sales-approve, reads sales-daily chat output. Wants: concise, honest, non-theater. Trusts the agent to run mechanics; reserves judgment for copy/strategy decisions.
2. **Prospects** (indirect — never interact with the module, only with the DMs it sends) — senior LATAM manufacturing leaders. Wants: not to be sold to. Reads messages in <5 seconds, rejects anything that smells like automation or templated diagnosis. Human-first design rules apply to DM copy.

## Interface Contracts (the equivalent of visual hierarchy for headless infra)

- **Attio list entries** — single source of truth for prospect state. Every write is idempotent. Keys: `stage`, `experiment_id`, `last_contact_date`, `response_classification` (new), `last_response_text` (new).
- **experiments.tsv** — append-only event log of experiment lifecycle. Never mutated in place; verdicts append new rows.
- **messages.json** — canonical DM copy, shaped `persona × language × step`. Any copy change ships via PR on `autosales/exp-NNN` branch.
- **docs/experiments/exp-NNN-*.md** — markdown proposals sales-weekly writes for the operator to approve before PR creation.

## Anti-patterns
What this module must NEVER do:
- Never send a DM without a tagged `experiment_id` — untagged sends poison cohort measurement.
- Never silently change DM copy outside the `autosales/exp-NNN` branch/PR flow — every copy change is reviewable.
- Never classify responses without writing the classification to Attio — transient classifications can't be measured.
- Never use keyword matching alone for response classification in production — too much nuance is lost (see detect_responses.py LLM classifier).
- Never run the weekly synthetic pre-screen without persisting the scores — otherwise the brain has no memory.
- Never merge an experiment to main without `sales-approve` review — even "obvious" wins go through the gate.

## Guidelines for DM Copy (applies to content/messages.json)
These are the human-first rules as they apply to prospect-facing copy:

- **No diagnosis premise in openers.** Never claim to already know what's wrong at the prospect's company. Ask, don't tell.
- **Compliment is specific or absent.** Vague flattery ("great company") is worse than no compliment. Either say something specific or skip it.
- **Social proof bridges, never judges.** "Others like you struggle with X" is judgment. "Even the best-run plants we see still find edge cases" is a bridge.
- **Give explicit outs.** Every DM should contain at least one permission-to-decline phrase ("or your reality is different", "totally fine if X doesn't apply").
- **One ask per message.** DM1 asks for a conversation, never for a calendar slot. DM2 can ask for a slot. DM3 is a release, not an ask.
- **Read time < 8 seconds.** If a prospect can't skim and decide in 8 seconds, it's too long.

## Guidelines for Markdown Proposals (docs/experiments/*.md)
- **Lead with the decision the operator has to make**, not with the context. First 3 lines = "what are we shipping and why."
- **Show the variant copy inline**, not linked. The operator needs to read it in the same file.
- **Show last cohort's reply examples** — especially the defensive ones. Qualitative data anchors the decision.
- **State the synthetic pre-screen scores as ranges, not point estimates.** E.g., "defensive likelihood 2–4 for Patricia-persona" not "3.1". Honest about noise.

## What's Out of Scope
- Visual design (no UI)
- Typography/color/spacing (no UI)
- Motion/interaction (no UI)
- Component guidelines (no UI)
