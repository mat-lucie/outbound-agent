"""Quality gate: rule-based prospect scoring and classification.

Deterministic pattern matching handles the clear-cut cases (score < 40 → reject,
score > 75 → pass). Borderline scores (40–75) can be resolved by an optional
Haiku LLM gate — callers pass an `anthropic_client`; without one, the gate
falls back to the deterministic threshold.
"""

import json
import logging
import re
from typing import Literal, cast

from clients.settings import ConfigError, config_dir
from workflows.icp_config import ICPConfig, load_icp_config

logger = logging.getLogger(__name__)

# Haiku is the cheapest reasoning-capable model — plenty for a single-step
# ICP classification over ~300 tokens of prospect data.
QUALIFIER_MODEL = "claude-haiku-4-5-20251001"
QUALIFIER_MAX_TOKENS = 300


# ── ICP scoring inputs (externalized to config) ──────────────────────
# P2a: the keyword lists, industry buckets, numeric weights, score bands, and
# geography lists below are operator-defined ICP DATA, loaded at import from
# config/icp.yaml (falling back to the shipped config/icp.example.yaml = current
# original operator ICP). The module-level names are preserved so every call site downstream
# is unchanged — only the SOURCE of each value moved from a hardcoded literal to
# config. The scoring MATH/LOGIC below is byte-identical to the pre-P2a code.
# With no live config/icp.yaml, behavior == today.
_ICP = load_icp_config()


# ── Persona classification rules ─────────────────────────────────────

DIGITALIZATION_KEYWORDS = _ICP.digitalization_keywords

EXECUTIVE_KEYWORDS = _ICP.executive_keywords

OPERATIONS_KEYWORDS = _ICP.operations_keywords

# Generic influencer titles (manager / lead / gerente / jefe / líder). Scored
# below decision-maker but above plain domain relevance.
INFLUENCER_KEYWORDS = _ICP.influencer_keywords

# Decision-maker titles (apply to all lanes). Substring matching via _match_any,
# so shorter forms subsume longer ones ("director" covers "director general",
# "director de operaciones", etc; "president" covers "vicepresidente").
DECISION_MAKER_KEYWORDS = _ICP.decision_maker_keywords

# Substring-match false-positive guards on DECISION_MAKER_KEYWORDS. "coo" inside
# DECISION_MAKER_KEYWORDS matches "coordinator" / "coordinador" / "coordenador"
# via substring, briefly tagging junior coordinators as decision-makers (+28)
# before is_junior_ic later applies -40. The final verdict is correct, but the
# intermediate +28 pollutes the audit log ("Decision-maker title: Production
# Coordinator" appears alongside the junior-IC penalty). These exemptions
# suppress the false decision-maker credit so the reasons list reflects only the
# signal that actually fires. Mirrors the JUNIOR_IC_EXEMPTIONS / SALES_ROLE_OPS_EXEMPTIONS
# pattern below.
DECISION_MAKER_EXEMPTIONS = _ICP.decision_maker_exemptions

# Foot-gun guard: if a future contributor adds an exemption term directly to
# DECISION_MAKER_KEYWORDS thinking it should match, is_decision_maker would
# silently evaluate to False (True AND NOT True) with no log explaining why.
# Catch the overlap at import time instead.
assert not (
    set(DECISION_MAKER_KEYWORDS) & set(DECISION_MAKER_EXEMPTIONS)
), "DECISION_MAKER_KEYWORDS and DECISION_MAKER_EXEMPTIONS must be disjoint"

# Global-scope titles that drop out of scope for LATAM cold outreach. A global
# VP doesn't take a cold message from a Mexican vendor — the real buyer is the
# country or regional head. Titles that ALSO mention a LATAM geography are
# exempt (e.g., "VP Global Operations LATAM" is a regional head).
GLOBAL_EXECUTIVE_KEYWORDS = _ICP.global_executive_keywords
LATAM_GEO_OVERRIDE = _ICP.latam_geo_override

# ── Industry ICP buckets ─────────────────────────────────────────────
# Maps the 11 INDUSTRY_LABELS (see models.campaign.INDUSTRY_LABELS) into
# scoring buckets. In-ICP labels are physical-product manufacturers the original operator
# sells to — these get a +12 boost. "Other" is the classifier's signal for
# "not a manufacturer" (tech, finance, services, distribution, retail, etc.)
# and gets a -25 penalty so off-ICP prospects fall below the 60 DM threshold.
# Unknown / missing industry is neutral so the scorer behaves the same as
# pre-industry-aware code when classification hasn't run yet.

#
# Membership is case-sensitive: callers must pass the canonical capitalized
# label ("Food & Beverage", not "food & beverage").  The Haiku industry
# classifier returns canonical strings today; PR-25 codifies this contract
# via the IndustryVerdict dataclass.
IN_ICP_INDUSTRIES = _ICP.in_icp_industries
OFF_ICP_INDUSTRIES = _ICP.off_icp_industries

INDUSTRY_BONUS_IN_ICP = _ICP.industry_bonus_in_icp
INDUSTRY_PENALTY_OFF_ICP = _ICP.industry_penalty_off_icp
# Combined bonus when an ops-domain influencer is at an in-ICP industrial
# company.  The two signals are correlated — an ops/plant/production role is
# inherently industry-coded, so strict additivity (+24 role + +12 industry
# = +36) over-rewards the joint case.  Replace with a single joint signal
# of +30 when BOTH conditions fire.  Non-joint paths (decision-maker + ICP,
# generic influencer + ICP, digi-influencer + ICP, ops-influencer + non-ICP)
# keep their individual contributions.
OPS_IN_INDUSTRIAL_COMBINED = _ICP.ops_in_industrial_combined

# ── Deterministic-pass geometry (PR-227) ─────────────────────────────
# The strict gate for a deterministic pass in score_prospect (score must be
# STRICTLY greater). Named so downstream reachability math (see
# weekly_prospect.DETERMINISTIC_REACHABLE_MIN_CREDIT) derives from the same
# number the verdict branch uses instead of re-encoding a literal 75.
DETERMINISTIC_PASS_THRESHOLD = 75
# The two largest non-size, non-joint component credits — the "best hand" a
# prospect can hold besides the search-scoped size credit and the industry
# bonus. Named for the same reason: the reachability line is derived from
# these, and an unnamed literal drifting in this file would silently
# invalidate the alarm gating in weekly_prospect.
DECISION_MAKER_ROLE_CREDIT = 28
NON_COMPETITOR_CREDIT = 20

# Bump whenever the component geometry changes in a way that shifts the score
# scale (see the scale_version stamp in score_prospect): analytics that pool
# quality_score across scale versions are comparing different rulers.
SCORING_SCALE_VERSION = "2026-07-search-credit"


# ── Score-band helper (Phase 1 auto-research) ───────────────────────
# Single source of truth so the writer (weekly_prospect.py) and the reader
# (workflows/qualifier_diagnostic.py) bin scores identically. Bands track
# the verdict-path branches in score_prospect: <40 deterministic_reject,
# 40-59 borderline-reject side, 60-75 borderline-pass side, >75 deterministic
# pass. Encoded as text so Attio can use it as a Single-Select.

SCORE_BAND_LABELS = _ICP.score_band_labels


def score_band(score: int | float | None) -> str | None:
    """Return the discrete band label for a quality score, or None if missing."""
    if score is None:
        return None
    s = int(score)
    if s < 40:
        return "<40"
    if s < 60:
        return "40-59"
    if s <= DETERMINISTIC_PASS_THRESHOLD:
        return "60-75"
    return ">75"


def _industry_score(
    industry: str | None,
    status: Literal["confirmed", "unknown", "low_confidence"] = "confirmed",
) -> tuple[int, str]:
    """Return (delta, reason) for the company's industry vertical.

    PR-25: ``status`` is the classification confidence status from
    ``IndustryVerdict.status`` ∈ {confirmed, unknown, low_confidence}.
    When status is not "confirmed", the classifier explicitly abstained
    (low confidence or dispatch failure) — treat as neutral so a shaky
    classification doesn't silently penalize as "Other" or reward as in-ICP.

    Default ``status="confirmed"`` preserves back-compat for callers that
    were written before PR-25 and don't carry the new status attr yet.

    The delta is added to the running prospect score. Unknown / empty
    industry returns (0, "") so it's a no-op — only adds a reason once we
    have a real signal.
    """
    # PR-25: abstain on low-confidence or unknown status (§0 #9 — no silent fallbacks).
    if status in ("unknown", "low_confidence"):
        return 0, f"Industry classification abstained (status={status})"
    if not industry:
        return 0, ""
    if industry in IN_ICP_INDUSTRIES:
        return INDUSTRY_BONUS_IN_ICP, f"In-ICP industry (+{INDUSTRY_BONUS_IN_ICP}): {industry}"
    if industry in OFF_ICP_INDUSTRIES:
        return INDUSTRY_PENALTY_OFF_ICP, f"Off-ICP industry ({INDUSTRY_PENALTY_OFF_ICP}): {industry}"
    # Unknown label (drift between classifier and scorer): treat as neutral
    # but log a reason so it's debuggable in score_breakdown.
    return 0, f"Unrecognized industry label: {industry}"


# ── Competitor / exclusion keywords ──────────────────────────────────

COMPETITOR_KEYWORDS = _ICP.competitor_keywords

ACADEMIC_KEYWORDS = _ICP.academic_keywords

CONSULTANT_KEYWORDS = _ICP.consultant_keywords

# Sales / commercial / marketing roles. The shipped program sells to manufacturing OPERATIONS
# (VP Ops, Plant Director, Director de Manufactura) — the people who own
# production scheduling. A sales/commercial/marketing director sits on the
# revenue side of the prospect's business and does not buy production tools.
# Hard disqualifier even when seniority would otherwise score the title as a
# decision-maker. Word-boundary matched via _match_any_word to avoid e.g.
# "sales" matching "salesforce" / "wholesales" / "salesian university".
SALES_ROLE_KEYWORDS = _ICP.sales_role_keywords

# Exemptions: titles where a sales-keyword is incidental, not the actual role.
# "Sales & Operations Planning" (S&OP) is a core ops/supply-chain function in
# manufacturing — they ARE buyers. Same for "Director Comercial y de
# Operaciones" if the ops side is explicit. If any of these exemption phrases
# is present, treat the title as ops, not sales.
SALES_ROLE_OPS_EXEMPTIONS = _ICP.sales_role_ops_exemptions

# Junior / IC roles. The Haiku qualifier prompt explicitly disqualifies these
# ("Junior/IC roles (Analyst, Coordinator, Engineer without manager scope)"),
# but the deterministic scorer was scoring them as influencers — at an in-ICP
# enterprise (size +28, ops_in_industrial +30 joint-case, competitor +20), the
# total (78) clears the >75 deterministic-pass threshold so the LLM gate never
# fires.  Hard-disqualify here to keep the two paths consistent.
# (Pre-PR-23 math was role +24 + industry +12 = +36 additive; PR-23 replaced
# the ops-influencer-at-industrial joint case with OPS_IN_INDUSTRIAL_COMBINED
# +30 — the junior/IC guard remains necessary because 28+30+20 = 78 > 75.)
#
# Word-boundary matched so "Senior Coordinator" / "Lead Engineer" / "Engineer
# III" don't trigger via the IC keyword. The exemption phrase list below
# captures titles where a junior/IC keyword is paired with a seniority signal
# strong enough to override.
JUNIOR_IC_KEYWORDS = _ICP.junior_ic_keywords

# If any of these substrings appear in the title, the junior-IC keyword is
# subsumed by a seniority/management signal — e.g. "Senior Coordinator",
# "Lead Engineer II", "Head of Analyst Team". Substring match (not word
# boundary) so multi-token phrases like "head of" still catch.
JUNIOR_IC_EXEMPTIONS = _ICP.junior_ic_exemptions

# ── PR-26: Expanded deterministic disqualifiers ──────────────────────
# Disqualifier families that the prior Haiku qualifier prompt named but
# the deterministic scorer let through. Each family fires a typed
# `disqualifier_match` Operator Review Queue row (workflows/weekly_prospect.py
# emits) so operators can audit keyword false-positives without losing the
# rejection. Company-keyed families (state-owned utilities, PE firms,
# consulting firms) get no OPS_OVERRIDE bypass; title-keyed families
# (HR, Finance, Innovation/R&D — but NOT Consulting, see below) are
# bypassed by OPS_OVERRIDE when the title is also manufacturing-ops-coded,
# e.g. "Plant Manager - HR Liaison".
#
# Word-boundary match (`_match_any_word`) on TITLE keywords prevents
# false-positives like "finance" matching "refinance". Company-name
# checks for STATE_OWNED and PE use substring match (`_match_any`) since
# legal-entity names vary ("Petróleos Mexicanos" / "Pemex Refinación"
# both should hit). CONSULTING_FIRM_KEYWORDS is the exception: it is
# word-boundary matched because its short brand tokens ("ey", "pwc",
# "bcg") would substring-fire inside unrelated names ("Hershey",
# "Monterrey") — when adding consulting keywords, use whole words or
# phrases, never truncated stems (a stem like "deloit" can never match
# in word-boundary mode).

# English compound forms (bare `hr` was removed — it false-matched shift-window
# titles like "24/7 hr ops planner"). Spanish + Portuguese forms follow.
HR_KEYWORDS = _ICP.hr_keywords

FINANCE_KEYWORDS = _ICP.finance_keywords

# Spanish bare `vinculación`/`vinculacion` tokens were dropped (LATAM
# commercial-liaison roles share the word) — restricted to academic forms.
INNOVATION_KEYWORDS = _ICP.innovation_keywords

# Private equity firm names. Working AT a PE firm (vs. at a portfolio company
# that happens to be PE-owned) is the deterministic-rejectable case; the
# rollup-portfolio detection is left to the LLM qualifier since it requires
# parent-company resolution we don't have at scoring time.
#
# GTM-QA convergence: the previous bare `"private equity"` substring caught
# unrelated companies like "LATAM Private Equity Holdings" or thought-
# leadership outlets. Restrict to qualified forms naming a firm structure.
PE_KEYWORDS = _ICP.pe_keywords

# LATAM state-owned enterprises and public utilities. Procurement runs via
# public tender, not cold outreach, so cold messaging is wasted regardless
# of contact seniority.
STATE_OWNED_KEYWORDS = _ICP.state_owned_keywords

# Consulting / professional-services firms (COMPANY-keyed). Consultancies are
# typically out of ICP for plant/operations outreach — a consultancy partner
# getting the operations-fit DM is prospect-facing embarrassment. The industry
# classifier can't catch this at qualification time: it only runs after a
# prospect passes, and its taxonomy has no professional-services label
# (consultancies fall into "Other").
#
# Word-boundary matched (unlike STATE_OWNED/PE substring matching) because the
# short brand tokens ("ey", "pwc", "bcg") would otherwise fire inside unrelated
# names ("Hershey", "Monterrey"). Word-boundary still catches legal-entity
# variants ("EY Brasil", "PwC México", "Accenture Brazil"). Glued-ampersand
# variants ("E&Y", "Ernst&Young", "strategy&") are matched via the
# conditional-boundary logic in _find_first_match.
CONSULTING_FIRM_KEYWORDS = _ICP.consulting_firm_keywords

# Consulting-coded TITLE keywords. Catches consultants at companies the firm
# list misses (boutiques, independents, in-house "Industries Lead" org
# structures). NOT bypassed by OPS_OVERRIDE — an ops phrase next to a
# consulting keyword means ops CONSULTING ("Supply Chain Consulting Director",
# "Operations Consultant"): they sell to operators, they don't run operations.
# The disjoint-span bypass rule would wrongly admit exactly those titles, so
# `_match_disqualifier` short-circuits before the bypass.
# ("advisor"/"advisory"/"freelance" stay on the soft CONSULTANT_KEYWORDS path —
# too ambiguous for a hard reject.)
CONSULTING_TITLE_KEYWORDS = _ICP.consulting_title_keywords

# OPS_OVERRIDE bypass: titles containing any of these are manufacturing-ops
# coded enough that an HR/Finance/Innovation keyword match is treated as
# incidental (the title's dominant signal is ops, e.g. "Plant Manager - HR
# Liaison" is an ops role with HR side-duty). Bypass is INTENTIONALLY
# narrow — generic "operations" alone is excluded because "HR Operations" /
# "Finance Operations" are still HR/finance-driven, not manufacturing.
# Company-based disqualifiers (state-owned, PE) ignore OPS_OVERRIDE since
# the procurement path doesn't shift based on the contact's title.
#
# Cross-QA convergence (silent-failure-hunter + prospect-weekly-QA): the
# previous list contained partial-word stems ("manufactur", "producción",
# "produção", "supply chain", "scheduling") and shared phrases ("director
# de operaciones") that substring-matched in titles where the disqualifier
# family was the dominant role — e.g. "HR Manager - Manufacturing
# Division" (manufacturing is divisional context, not the role) and
# "Director de Operaciones Financieras" (financial-side ops, not mfg).
# Both bypassed silently → §0 #9 silent-fallback violation.
#
# Fold: restrict OPS_OVERRIDE to ROLE-LEVEL phrases only. Context stems
# are gone. Matching switched to word-boundary (`_match_any_word`) to
# avoid substring leakage. The bypass now fires only when the title
# contains a phrase that strongly indicates the person's PRIMARY job is
# manufacturing operations.
OPS_OVERRIDE_KEYWORDS = _ICP.ops_override_keywords

# ── PR-222 Rec E: high-confidence COMPANY / EMPLOYER disqualifier families ──
# Academics, government/state entities, hospitals, direct competitors, and
# freelancers are deterministic from the company NAME alone. Each fires a typed
# `disqualifier_*` Operator Review Queue row (reversible, auditable) — same
# machinery as STATE_OWNED / CONSULTING_FIRM, with NO OPS_OVERRIDE bypass (the
# disqualifying fact is the employer, not the title). Company-name families use
# substring match since legal-entity forms vary; the competitor and freelance
# families are word-boundary matched because their short tokens would otherwise
# substring-fire inside unrelated names. Values are operator ICP DATA, sourced
# from config (config/icp.yaml → icp.example.yaml).
ACADEMIC_COMPANY_KEYWORDS = _ICP.academic_company_keywords
GOVERNMENT_KEYWORDS = _ICP.government_keywords
HEALTHCARE_PROVIDER_KEYWORDS = _ICP.healthcare_provider_keywords
COMPETITOR_COMPANY_KEYWORDS = _ICP.competitor_company_keywords
FREELANCE_EMPLOYER_KEYWORDS = _ICP.freelance_employer_keywords

# ── PR-238: medical / regulatory / clinical-affairs TITLE disqualifier family ──
# A pharma/biotech clinical-affairs function (medical/regulatory affairs,
# pharmacovigilance, clinical operations, market access) runs drug safety /
# regulatory submissions / clinical programs — NOT a plant's production
# schedule. The industry classifier keeps pharma MANUFACTURERS in-ICP, so the
# gap is at the TITLE level. Word-boundary matched (like the other title
# families); a genuine manufacturing-ops title is rescued by the OPS_OVERRIDE
# disjoint-span bypass. Values are operator ICP DATA, sourced from config.
MEDICAL_REGULATORY_TITLE_KEYWORDS = _ICP.medical_regulatory_title_keywords

# ── PR-298: integrator / service-provider COMPANY family (DESCRIPTION-keyed) ──
# "IS a buyer" vs "SELLS to buyers". Every signal the scorer reads about a small
# automation integrator points the wrong way: its CRM categories are the ICP's
# own categories, its people carry plant-coded titles, and its company NAME is
# often a bare brand with no service token to catch. The one field that states
# the business model is the company DESCRIPTION.
#
# The family is a CONJUNCTION — a service-provider DESCRIPTION at a company the
# industry classifier already labelled off-ICP. Neither half is sufficient
# alone. Description-alone is far too loose: large equipment MAKERS that run
# real plants describe themselves in exactly these words, so a description-only
# predicate hard-rejects in-ICP manufacturers. Off-ICP-alone is far too broad to
# hard-reject on (it is a scoring penalty by design, not a rejection).
#
# Deliberately NOT keyed on company size, though size is the intuitive
# discriminator. Headcount/revenue are premium CRM enrichment attributes that a
# base API entitlement reads as empty on every record, so a conjunction keyed on
# one could never fire in production — a gate that looks like a fix and is
# structurally a no-op. If the enrichment entitlement is present, size becomes an
# additional precision lever worth revisiting.
#
# ABSTAIN, never default: no description, or an industry label that is missing /
# in-ICP / unrecognized / classified "unknown", yields NO match.
#
# Coverage is genuinely partial, and it is worth being precise about where. The
# description comes from CRM enrichment, absent for a company the pipeline has
# never seen — so a brand-new integrator is NOT caught at first ingest. It
# becomes catchable once the CRM enriches the company and the row is re-scored on
# a later weekly. Companies already committed before this family shipped are
# never re-scored at all; scripts/audit_integrator_prospects.py sweeps those
# read-only, and reports rather than acts.
#
# Word-boundary matched (like the other keyword families) so short tokens
# ("integrator") cannot substring-fire inside unrelated prose. Values are
# operator ICP DATA, sourced from config (config/icp.yaml → icp.example.yaml).
INTEGRATOR_DESCRIPTION_KEYWORDS = _ICP.integrator_description_keywords

# Carve-outs — description phrases in which the company asserts that IT runs
# production. A company that both integrates and manufactures is judged on the
# plant it owns, so any of these suppresses the family.
#
# Every configured phrase must carry its own SUBJECT (the config comments spell
# out why): a bare stem like "manufactur" reads as a carve-out inside "a systems
# integrator serving discrete MANUFACTURERS", and a bare genitive like
# "manufacturer of" matches the OEM an integrator RESELLS. Matching is substring
# (not word-boundary), so a shorter phrase subsumes its plural.
INTEGRATOR_MANUFACTURER_CARVEOUTS = _ICP.integrator_manufacturer_carveouts


def _find_first_match(
    text: str, keywords: list[str], *, word_boundary: bool
) -> tuple[int, int, str] | None:
    """Return `(start, end, matched_keyword)` of the earliest hit, or None.

    Position-aware variant of `_match_any` / `_match_any_word`. Used by
    `_match_disqualifier` to detect span overlap between the OPS_OVERRIDE
    phrase and the disqualifier keyword. When the spans overlap, the
    ops_override bypass is invalid — the ops phrase is being MODIFIED by
    the disqualifier keyword (e.g. `director de operaciones financieras`
    where "operaciones" is shared between the ops phrase and the finance
    keyword), so the disqualifier wins.
    """
    best: tuple[int, int, str] | None = None
    if word_boundary:
        for kw in keywords:
            # `\b` is only a valid anchor next to a word char: `\bstrategy&\b`
            # can never match "strategy& méxico" because `&\b` demands a word
            # char after the ampersand. Apply each boundary only when the
            # keyword's edge is a word char — a no-op for every plain-word
            # keyword, and the only way edge-symbol keywords ("strategy&")
            # can match at all.
            prefix = r"\b" if re.match(r"\w", kw) else ""
            suffix = r"\b" if re.search(r"\w$", kw) else ""
            pattern = re.compile(f"{prefix}{re.escape(kw)}{suffix}", re.IGNORECASE)
            m = pattern.search(text)
            if m and (best is None or m.start() < best[0]):
                best = (m.start(), m.end(), kw)
        return best
    text_lower = text.lower()
    for kw in keywords:
        idx = text_lower.find(kw.lower())
        if idx >= 0 and (best is None or idx < best[0]):
            best = (idx, idx + len(kw), kw)
    return best


def _is_integrator_service_provider(
    description: str,
    industry: str | None,
    industry_status: str = "confirmed",
) -> str | None:
    """Return the matched description keyword when the company reads as a
    service provider rather than an in-ICP operator, else None.

    Both halves are required and neither is defaulted:

      * `industry` must be an OFF_ICP_INDUSTRIES label — the industry
        classifier's own verdict that this company is not the kind of business
        the operator sells to. A missing, in-ICP, or unrecognized label
        abstains.
      * `industry_status` must not be "unknown". `unknown` means the classifier
        abstained or its dispatch failed — there is no verdict to conjoin with,
        so the family abstains too. `score_prospect` maps any unrecognized
        status string to "unknown", so schema drift lands here and fails safe.
      * `description` must carry a service-provider phrase and no carve-out —
        a company that says it runs plants is judged on the plants.

    `low_confidence` IS accepted here, unlike the abstain in `_industry_score`,
    and the difference is deliberate. The ingest-time classifier stamps
    `low_confidence` on everything it labels, and only the manual
    `industry-approve` CLI ever writes `confirmed` — so a confirmed-only
    predicate would fire on operator-approved rows and on NOTHING the pipeline
    classifies itself, i.e. it would be silent on exactly the newly-ingested
    companies this family exists to catch. That is a dead gate, the same class
    of failure as keying on an unreadable enrichment attribute.

    The reason it is safe to relax here and not in `_industry_score`: there the
    label moves the score as a SINGLE signal, so a shaky classification silently
    moves a number on its own. Here it is one half of a conjunction whose other
    half is an explicit, human-readable business-model statement. A false hard
    reject needs a classifier error AND a service-provider description AND no
    operator self-assertion — and the family opens a typed Operator Review Queue
    row, so the rejection is auditable and reversible.
    """
    if not description:
        return None
    if not industry or industry not in OFF_ICP_INDUSTRIES:
        return None
    if industry_status == "unknown":
        return None
    if _match_any(description, INTEGRATOR_MANUFACTURER_CARVEOUTS):
        return None
    hit = _find_first_match(
        description, INTEGRATOR_DESCRIPTION_KEYWORDS, word_boundary=True
    )
    return hit[2] if hit is not None else None


def _match_disqualifier(
    title_lower: str,
    company_lower: str,
    *,
    description_lower: str = "",
    industry: str | None = None,
    industry_status: str = "confirmed",
) -> tuple[str, str] | None:
    """Return `(verdict_path_slug, matched_keyword)` for this prospect, or None.

    `description_lower` / `industry` / `industry_status` describe the parent
    company and feed the integrator / service-provider family only (see
    INTEGRATOR_DESCRIPTION_KEYWORDS). `description_lower` defaults to empty and
    `industry` to None, so callers that have no company record — tests, repair
    utilities, the ad-hoc audit scripts — keep their pre-PR-298 behaviour
    exactly: that family abstains rather than guessing.

    Company-based checks (state-owned, PE, consulting firms) run first
    and cannot be bypassed by OPS_OVERRIDE — procurement path is
    deterministic from the company name. Title-based checks (Finance,
    HR, Innovation, Consulting) follow; Consulting is exempt from the
    bypass (see CONSULTING_TITLE_KEYWORDS). For the other three the
    OPS_OVERRIDE bypass is applied position-aware: the bypass
    fires ONLY when the matched OPS_OVERRIDE phrase is DISJOINT from
    the matched disqualifier keyword. When they overlap (e.g.
    `director de operaciones financieras` shares "operaciones"), the
    ops phrase is being modified by the disqualifier suffix and the
    bypass is invalid — disqualifier wins (silent-failure + prospect-
    weekly QA convergence on §0 #9 silent fallback).

    The second tuple element is the specific phrase that triggered.
    The typed `DisqualifierMatchPayload` records it on the Operator
    Review Queue row so operators audit false-positives directly.
    """
    # Company-based (no OPS_OVERRIDE bypass).
    co_state = _find_first_match(company_lower, STATE_OWNED_KEYWORDS, word_boundary=False)
    if co_state is not None:
        return ("disqualifier_state_owned", co_state[2])
    co_pe = _find_first_match(company_lower, PE_KEYWORDS, word_boundary=False)
    if co_pe is not None:
        return ("disqualifier_pe", co_pe[2])
    # Word-boundary (not substring like the two above) so the short brand
    # tokens ("ey", "pwc", "bcg") can't fire inside unrelated company names.
    co_consult = _find_first_match(company_lower, CONSULTING_FIRM_KEYWORDS, word_boundary=True)
    if co_consult is not None:
        return ("disqualifier_consulting", co_consult[2])
    # PR-222 Rec E company/employer families (no OPS_OVERRIDE bypass — the
    # disqualifying fact is the employer, not the title). Competitor and
    # freelance use word-boundary matching for their short tokens; academic /
    # government / healthcare-provider use substring because legal-entity forms
    # vary. Ordered AFTER state/pe/consulting so those keep priority on overlap.
    co_competitor = _find_first_match(
        company_lower, COMPETITOR_COMPANY_KEYWORDS, word_boundary=True
    )
    if co_competitor is not None:
        return ("disqualifier_competitor", co_competitor[2])
    co_academic = _find_first_match(
        company_lower, ACADEMIC_COMPANY_KEYWORDS, word_boundary=False
    )
    if co_academic is not None:
        return ("disqualifier_academic", co_academic[2])
    co_government = _find_first_match(
        company_lower, GOVERNMENT_KEYWORDS, word_boundary=False
    )
    if co_government is not None:
        return ("disqualifier_government", co_government[2])
    co_healthcare = _find_first_match(
        company_lower, HEALTHCARE_PROVIDER_KEYWORDS, word_boundary=False
    )
    if co_healthcare is not None:
        return ("disqualifier_healthcare", co_healthcare[2])
    co_freelance = _find_first_match(
        company_lower, FREELANCE_EMPLOYER_KEYWORDS, word_boundary=True
    )
    if co_freelance is not None:
        return ("disqualifier_freelance", co_freelance[2])
    # Integrator / service provider (PR-298). Runs LAST among the company
    # families so a company that is also state-owned / a named consultancy / an
    # academic institution keeps its more specific slug — this one is the
    # residual "sells to our buyers" catch. Reads the company DESCRIPTION, not
    # the name, and abstains without both halves.
    integrator_kw = _is_integrator_service_provider(
        description_lower, industry, industry_status
    )
    if integrator_kw is not None:
        return ("disqualifier_integrator", integrator_kw)
    # Title-based: pick the EARLIEST match across the families so
    # the verdict_path reflects the dominant signal in the title.
    title_matches: list[tuple[str, tuple[int, int, str]]] = []
    fin = _find_first_match(title_lower, FINANCE_KEYWORDS, word_boundary=True)
    if fin is not None:
        title_matches.append(("disqualifier_finance", fin))
    hr = _find_first_match(title_lower, HR_KEYWORDS, word_boundary=True)
    if hr is not None:
        title_matches.append(("disqualifier_hr", hr))
    innov = _find_first_match(title_lower, INNOVATION_KEYWORDS, word_boundary=True)
    if innov is not None:
        title_matches.append(("disqualifier_innovation", innov))
    consult = _find_first_match(title_lower, CONSULTING_TITLE_KEYWORDS, word_boundary=True)
    if consult is not None:
        title_matches.append(("disqualifier_consulting", consult))
    # PR-238: medical / regulatory / clinical-affairs title family. Behaves like
    # the other bypassable title families — the OPS_OVERRIDE disjoint-span rule
    # below rescues a genuine manufacturing-ops title that only incidentally
    # carries a "medical" token (e.g. medical-device manufacturing ops).
    medreg = _find_first_match(
        title_lower, MEDICAL_REGULATORY_TITLE_KEYWORDS, word_boundary=True
    )
    if medreg is not None:
        title_matches.append(("disqualifier_medical_regulatory", medreg))
    if not title_matches:
        return None
    slug, (dq_start, dq_end, dq_kw) = min(title_matches, key=lambda x: x[1][0])
    # Consulting is exempt from OPS_OVERRIDE: an ops phrase alongside a
    # consulting keyword means ops CONSULTING ("Supply Chain Consulting
    # Director") — the disjoint-span rule below would wrongly bypass it.
    if slug == "disqualifier_consulting":
        return (slug, dq_kw)
    # OPS_OVERRIDE position-aware bypass.
    ops = _find_first_match(title_lower, OPS_OVERRIDE_KEYWORDS, word_boundary=True)
    if ops is not None:
        ops_start, ops_end, _ = ops
        disjoint = (ops_end <= dq_start) or (dq_end <= ops_start)
        if disjoint:
            # The bypass clears the BYPASSABLE families only. A consulting
            # match elsewhere in the title is exempt and must still fire:
            # in "Plant Director - HR Consultant" the earliest-match pick
            # is HR, the ops phrase bypasses HR — but the title is still a
            # consultant's. Without this fallback the consulting signal was
            # silently discarded whenever another family matched earlier.
            for c_slug, (_, _, c_kw) in title_matches:
                if c_slug == "disqualifier_consulting":
                    return (c_slug, c_kw)
            return None
    return (slug, dq_kw)


# Canonical registry of every `verdict_path` value `score_prospect` may
# assign to its result. PR-26 adds the `disqualifier_*` slugs (six families); the
# rest were already in use (just inline as string literals).
# `tests/test_pr26_disqualifiers.py::test_score_prospect_verdict_paths_in_registry`
# enforces that any future addition shows up here so the set stays
# discoverable and operator-facing dashboards don't silently drop new buckets.
VERDICT_PATHS: frozenset[str] = frozenset({
    "deterministic_reject",
    "deterministic_reject_sales_role",
    "deterministic_reject_junior_ic",
    "enterprise_pass",
    "target_pass",
    "borderline_pass",
    "borderline_reject",
    "borderline_llm_error",
    "borderline_cost_exhausted",
    # PR-26 disqualifiers (+ consulting)
    "disqualifier_hr",
    "disqualifier_finance",
    "disqualifier_innovation",
    "disqualifier_pe",
    "disqualifier_state_owned",
    "disqualifier_consulting",
    # PR-222 Rec E company/employer families
    "disqualifier_academic",
    "disqualifier_government",
    "disqualifier_healthcare",
    "disqualifier_competitor",
    "disqualifier_freelance",
    # PR-238 medical / regulatory / clinical-affairs title family
    "disqualifier_medical_regulatory",
    # PR-298: small integrator / service provider — "sells to our buyers",
    # not "is one" (DESCRIPTION-keyed, conjoined with an off-ICP industry).
    "disqualifier_integrator",
})

# The verdict paths score_prospect emits for a DETERMINISTIC pass (>75 gate,
# no LLM involved). Single source of truth for "was this pass deterministic?"
# — consumers (e.g. weekly_prospect's deterministic_qualified counter) import
# this instead of hand-listing the paths, so a future lane's pass path cannot
# silently fall out of the count. (PR-227)
DETERMINISTIC_PASS_PATHS: frozenset[str] = frozenset({
    "enterprise_pass",
    "target_pass",
})

DISQUALIFIER_VERDICT_PATHS: frozenset[str] = frozenset({
    "disqualifier_hr",
    "disqualifier_finance",
    "disqualifier_innovation",
    "disqualifier_pe",
    "disqualifier_state_owned",
    "disqualifier_consulting",
    # PR-222 Rec E company/employer families
    "disqualifier_academic",
    "disqualifier_government",
    "disqualifier_healthcare",
    "disqualifier_competitor",
    "disqualifier_freelance",
    # PR-238 medical / regulatory / clinical-affairs title family
    "disqualifier_medical_regulatory",
    # PR-298: small integrator / service provider — "sells to our buyers",
    # not "is one" (DESCRIPTION-keyed, conjoined with an off-ICP industry).
    "disqualifier_integrator",
})


# ── Language detection by location ───────────────────────────────────

PT_LOCATIONS = _ICP.pt_locations
ES_LOCATIONS = _ICP.es_locations
EN_LOCATIONS = _ICP.en_locations


def _match_any(text: str, keywords: list[str]) -> bool:
    """Check if any keyword appears in text (case-insensitive, substring).

    Substring match is intentional for keyword lists where shorter forms
    subsume longer ones (e.g. "director" covers "director general",
    "director de operaciones"). Use _match_any_word when you need to
    avoid false-positives like "sales" matching "salesforce".
    """
    text_lower = text.lower()
    return any(kw in text_lower for kw in keywords)


_WORD_BOUNDARY_RE_CACHE: dict[tuple[str, ...], "re.Pattern[str]"] = {}


def _match_any_word(text: str, keywords: list[str]) -> bool:
    """Check if any keyword appears in text as a whole word (case-insensitive).

    Use this for keyword lists where substring matching would cause real
    false-positives — e.g. SALES_ROLE_KEYWORDS where "sales" must not match
    "salesforce", "wholesales", "salesian". Word boundaries are Python's \\b,
    which treats hyphens and underscores as word characters (so "pre-sales"
    is one token, "b2b director" stays a phrase).

    The compiled regex is cached per-keyword-list so repeated scoring calls
    don't recompile.
    """
    key = tuple(keywords)
    pattern = _WORD_BOUNDARY_RE_CACHE.get(key)
    if pattern is None:
        # Sort longest-first so "engineer ii" wins over "engineer" when both
        # are present in the same list (the alternation is regex-greedy).
        alternation = "|".join(re.escape(k) for k in sorted(keywords, key=len, reverse=True))
        pattern = re.compile(rf"\b(?:{alternation})\b", re.IGNORECASE)
        _WORD_BOUNDARY_RE_CACHE[key] = pattern
    return bool(pattern.search(text))


def _classify_persona(title: str) -> str:
    """Classify prospect into a persona based on title."""
    title_lower = title.lower()

    # Executive check first (most specific — only if they're in a relevant domain)
    if _match_any(title_lower, EXECUTIVE_KEYWORDS) and _match_any(title_lower, OPERATIONS_KEYWORDS + ["supply", "manufactur", "operation"]):
        return "executive_sponsors"

    # Digitalization check
    if _match_any(title_lower, DIGITALIZATION_KEYWORDS):
        return "digitalization_champions"

    # Default to operations leaders
    return "operations_leaders"


def _detect_language(location: str, name: str = "") -> str:
    """Detect language from location and name."""
    loc_lower = location.lower()

    if _match_any(loc_lower, PT_LOCATIONS):
        return "pt"
    if _match_any(loc_lower, ES_LOCATIONS):
        return "es"
    if _match_any(loc_lower, EN_LOCATIONS):
        return "en"

    # Default to English if location is unclear
    return "en"


# ── LLM qualifier system prompt (externalized to a Jinja2 template) ──────
# P2b: the narrative, ICP-specific pieces of the LLM tiebreaker's system prompt
# (product summary, geography requirement, the two ICP-lane blocks, and the
# hard-disqualifier bullets) are operator-defined ICP DATA, loaded from the
# `qualifier_prompt:` section of config/icp.{yaml,example.yaml} via ICPConfig.
# They render through config/prompts/qualifier.md.j2, which assembles them with
# the STATIC engine scaffolding — including the JSON output contract, which is
# the shape `_llm_qualify` parses, NOT operator-tunable ICP. For the shipped
# config the rendered prompt equals the pre-P2b hardcoded string byte-for-byte
# (proved by tests/test_qualifier_prompt_golden.py). The module-level name
# QUALIFIER_SYSTEM_PROMPT is preserved so every call site below is unchanged.
_QUALIFIER_TEMPLATE_NAME = "qualifier.md.j2"


def _render_qualifier_system_prompt(icp: ICPConfig) -> str:
    """Render the LLM qualifier system prompt from the ICP config slots.

    Loads ``config/prompts/<_QUALIFIER_TEMPLATE_NAME>`` (the live override if an
    operator placed one in their ``OUTBOUND_CONFIG_DIR``, else the shipped
    template) and fills the ``qualifier_prompt`` slots from ``icp``. Raises
    :class:`ConfigError` if the template file is missing — never silently falls
    back to an empty or partial prompt that would degrade the qualifier.

    Whitespace is load-bearing: Jinja2 ``trim_blocks``/``lstrip_blocks`` stay
    OFF so the template's literal newlines are preserved exactly. For the original operator's
    config this returns the pre-P2b string byte-for-byte.
    """
    # Imported lazily so the module imports even if jinja2 weren't installed in
    # an exotic deploy; in practice jinja2 is a declared dependency (P2).
    from jinja2 import (
        Environment,
        FileSystemLoader,
        StrictUndefined,
        select_autoescape,
    )

    prompts_dir = config_dir() / "prompts"
    template_path = prompts_dir / _QUALIFIER_TEMPLATE_NAME
    if not template_path.is_file():
        raise ConfigError(
            f"LLM qualifier prompt template not found: {template_path}. "
            f"Restore config/prompts/{_QUALIFIER_TEMPLATE_NAME} or point "
            f"OUTBOUND_CONFIG_DIR at a config dir that contains it."
        )
    env = Environment(
        loader=FileSystemLoader(str(prompts_dir)),
        autoescape=select_autoescape(enabled_extensions=(), default=False),
        keep_trailing_newline=True,
        # Turn a typo'd template variable (or a loop over a misspelled name) from
        # a SILENT empty render into a loud UndefinedError — a degraded qualifier
        # prompt must fail loudly, not ship blank. The shipped template uses no
        # conditionals on absent vars, so this stays byte-identical for the shipped config.
        undefined=StrictUndefined,
    )
    template = env.get_template(_QUALIFIER_TEMPLATE_NAME)
    return template.render(
        product_summary=icp.qualifier_product_summary,
        geography_requirement=icp.qualifier_geography_requirement,
        lanes=icp.qualifier_lanes,
        disqualifiers=icp.qualifier_disqualifiers,
        qualifier_lane_labels=icp.qualifier_lane_labels,
    )


QUALIFIER_SYSTEM_PROMPT = _render_qualifier_system_prompt(_ICP)


def render_qualification_prompt(prospect_data: dict, persona_config: dict | None) -> dict:
    """Render the Haiku qualification prompt as a {system, user} pair.

    Returned dict is directly usable by both `_llm_qualify` (for in-process Haiku
    calls) and the agent-driven staging path (where the calling agent dispatches
    Haiku itself via a subagent). Keeps the prompt construction in one place.
    """
    mode = "enterprise" if (persona_config or {}).get("enterprise_mode") else (
        "midmarket" if (persona_config or {}).get("target_company_mode") else "legacy"
    )
    user_content = (
        f"PROSPECT:\n"
        f"- Name: {prospect_data.get('name', '')}\n"
        f"- Title: {prospect_data.get('title', '')}\n"
        f"- Company: {prospect_data.get('company', '')}\n"
        f"- Location: {prospect_data.get('location', '')}\n"
        f"- Persona mode: {mode}\n"
    )
    return {"system": QUALIFIER_SYSTEM_PROMPT, "user": user_content}


def _llm_qualify(
    prospect_data: dict,
    persona_config: dict | None,
    anthropic_client=None,
) -> dict:
    """Call Haiku to qualify a borderline prospect.

    Returns a dict shaped {"pass": bool, "icp_lane": int|None, "rationale":
    str, "llm_failed": bool}. Never raises — any exception is caught and
    flagged via `llm_failed=True` so the caller can distinguish a real
    Haiku reject from a transient API outage.

    The caller (score_prospect) routes `llm_failed=True` to a distinct
    `verdict_path="borderline_llm_error"` and falls back to the
    deterministic threshold for `pass`. Without this distinction, a
    rate-limit or network blip silently flagged borderline prospects as
    rejected and lost the operator's ability to retry them.

    # F-PR-9 LLM dispatch migration

    When ``anthropic_client is None`` (production default), the call
    surfaces to the parent Claude Code slash-command session via
    ``workflows.llm_dispatch.request_llm_dispatch`` — the engine does NOT
    import anthropic or hold ANTHROPIC_API_KEY per §0 invariant #11.

    Tests that need a deterministic mock LLM response continue to pass
    a ``MagicMock``-shaped client via ``anthropic_client=...``; that
    legacy path uses ``client.messages.create(...)`` exactly as before
    so existing test fixtures don't need to change.
    """
    try:
        prompt = render_qualification_prompt(prospect_data, persona_config)
        user_content = prompt["user"]
        if anthropic_client is None:
            from workflows.llm_dispatch import (
                CostCeilingExhausted,
                LLMBudgetLedgerUnavailable,
                request_llm_dispatch,
            )

            try:
                result = request_llm_dispatch(
                    step="quality_gate_haiku",
                    prompt=user_content,
                    system=QUALIFIER_SYSTEM_PROMPT,
                    model_class="haiku",
                    max_tokens=QUALIFIER_MAX_TOKENS,
                    schema_hint=(
                        'Return JSON: {"pass": bool, "icp_lane": int|null, "rationale": str}'
                    ),
                )
            except LLMBudgetLedgerUnavailable as exc:
                # Ledger INFRA failure — the cap could not be consulted,
                # so the dispatch never ran. Distinct from a real LLM
                # failure and from cap exhaustion: the caller
                # (score_prospect) routes this to the agent staging path
                # when agent_gate=True (PR-216 incident fix) instead
                # of rejecting the prospect.
                logger.warning(
                    "LLM qualifier budget ledger unavailable (step=%s): %s",
                    exc.step, exc.error,
                )
                return {
                    "pass": False,
                    "icp_lane": None,
                    "rationale": f"LLM budget ledger unavailable: {exc.error}",
                    "llm_failed": True,
                    "ledger_unavailable": True,
                }
            except CostCeilingExhausted as exc:
                # Distinct verdict_path so operators see cost-ceiling
                # breaches separately from transient LLM errors (per
                # silent-failure-hunter HIGH + type-design IMPORTANT-4).
                # The caller (score_prospect) detects this signature and
                # routes to verdict_path="borderline_cost_exhausted".
                logger.warning(
                    "LLM qualifier cost ceiling exhausted (step=%s cap=$%.4f consumed=$%.4f)",
                    exc.step, exc.cap, exc.consumed,
                )
                return {
                    "pass": False,
                    "icp_lane": None,
                    "rationale": (
                        f"LLM qualifier cost ceiling exhausted: cap=${exc.cap:.4f}, "
                        f"consumed=${exc.consumed:.4f}"
                    ),
                    "llm_failed": True,
                    "cost_exhausted": True,
                }
            raw_text = result.raw_text
        else:
            # Test-injection path: legacy Anthropic SDK shape preserved.
            response = anthropic_client.messages.create(
                model=QUALIFIER_MODEL,
                max_tokens=QUALIFIER_MAX_TOKENS,
                system=[
                    {
                        "type": "text",
                        "text": QUALIFIER_SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_content}],
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
        stripped = raw_text.strip()
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
        parsed = json.loads(stripped)
        return {
            "pass": bool(parsed.get("pass", False)),
            "icp_lane": int(parsed["icp_lane"]) if parsed.get("icp_lane") is not None else None,
            "rationale": str(parsed.get("rationale", "")).strip(),
            "llm_failed": False,
        }
    except Exception as err:  # noqa: BLE001 — this is the fallback boundary
        logger.warning("LLM qualifier failed (%s): %s", type(err).__name__, err)
        return {
            "pass": False,
            "icp_lane": None,
            "rationale": f"LLM qualifier error: {type(err).__name__}",
            "llm_failed": True,
        }


def score_prospect(
    prospect_data: dict,
    persona_config: dict | None = None,
    *,
    anthropic_client=None,
    agent_gate: bool = False,
    use_llm_dispatch: bool = False,
    **_kwargs,
) -> dict:
    """Score a prospect using rule-based criteria.

    Args:
        prospect_data: Dict with keys: name, title, company, location,
                       linkedin_url, etc.
        persona_config: Optional persona config (from personas.json). Two
                       dedicated modes:
                       - target_company_mode=true → Lane 1 mid-market scoring
                         (curated target company list).
                       - enterprise_mode=true → Lane 2 enterprise subsidiary
                         scoring.
                       The size component reads `search_size_credit` /
                       `search_headcount_filter` from the config (the saved
                       search's headcount facet is the size signal — see the
                       size block below); no config → size abstains.
                       In target_company_mode the curated persona key is assigned
                       directly. In enterprise_mode the persona is re-classified
                       by title (the search returns mixed titles, so binding the
                       search persona over-assigns it).

    Returns:
        Dict with score, pass, persona, language, reasons.
    """
    title = str(prospect_data.get("title", ""))
    company = str(prospect_data.get("company", ""))
    location = str(prospect_data.get("location", ""))
    name = str(prospect_data.get("name", ""))
    industry = prospect_data.get("industry") or None
    # PR-25: read industry_vertical_status from prospect_data when present.
    # Default "confirmed" for back-compat with prospects that predate PR-25 and
    # don't carry the new attr — existing scoring behavior is preserved.
    #
    # PR-25 follow-up (PR-225): weekly_prospect._resolve_company_industry now
    # populates `industry` + `industry_vertical_status` into prospect_data at
    # ingest (company lookup, classify-on-miss), so the abstain gate is live
    # for new ingest. The "confirmed" default below remains for labels that
    # predate the status attr (scrape / manual / operator-set) — those carry no
    # status and keep pre-PR-25 face-value semantics.
    _raw_status = prospect_data.get("industry_vertical_status") or "confirmed"
    # A status outside the known set (operator typo / future schema drift in
    # the CRM select) must abstain, not silently score as confirmed.
    industry_vertical_status: Literal["confirmed", "unknown", "low_confidence"] = (
        cast("Literal['confirmed', 'unknown', 'low_confidence']", _raw_status)
        if _raw_status in ("confirmed", "unknown", "low_confidence")
        else "unknown"
    )
    score = 0
    reasons: list[str] = []
    component_scores: dict[str, int] = {}

    target_company_mode = bool(persona_config and persona_config.get("target_company_mode"))
    enterprise_mode = bool(persona_config and persona_config.get("enterprise_mode"))
    if target_company_mode and enterprise_mode:
        raise ValueError(
            "persona_config has both target_company_mode and enterprise_mode set — "
            "these are mutually exclusive lanes (see sales-program.md)."
        )

    size_score_before = score
    # 1. Company size (0-30 points) — search-scoped structural credit. (PR-227)
    #
    # Sales-search exports carry NO headcount column, so per-row employee_count
    # was empty on 100% of production rows and the old per-prospect bands
    # silently defaulted on everyone (2026-07-06 RCA: deterministic
    # qualification was mathematically impossible since April). The size signal
    # actually lives in the saved search: every search is scoped with a
    # company-headcount facet, so every exported row already passed the size
    # filter. The persona config declares that scoping and the credit it earns:
    #   search_size_credit      — points granted to every row from this search
    #   search_headcount_filter — the headcount facet, echoed into the reasons
    # No declared scoping → ABSTAIN (0 points, mirroring low-confidence
    # industry): a default that fires on 100% of inputs is an offset, not a
    # score. The credit value is calibration-gated — see the RCA fix and the
    # PR that introduced this block before changing it.
    search_size_credit = (persona_config or {}).get("search_size_credit")
    if search_size_credit is not None:
        try:
            credit = int(search_size_credit)
        except (ValueError, TypeError):
            # A bare int() error names neither the key nor the file — make
            # the config typo (e.g. the headcount facet string pasted into
            # the credit key) diagnosable from the message alone.
            raise ValueError(
                f"search_size_credit={search_size_credit!r} in the persona "
                "config is not an integer — expected 0-30 (personas.json)."
            ) from None
        if not 0 <= credit <= 30:
            raise ValueError(
                f"search_size_credit={credit} out of the size component's "
                "0-30 range — check the persona config."
            )
        score += credit
        headcount_filter = (persona_config or {}).get(
            "search_headcount_filter", "declared in persona config"
        )
        reasons.append(
            f"Size scoped by SN search (+{credit}): headcount {headcount_filter}"
        )
    else:
        reasons.append("Company size unknown (search not size-scoped): abstained")

    component_scores["size"] = score - size_score_before
    role_score_before = score
    # 2. Role fit (0-30 points)
    title_lower = title.lower()
    is_decision_maker = (
        _match_any(title_lower, DECISION_MAKER_KEYWORDS)
        and not _match_any(title_lower, DECISION_MAKER_EXEMPTIONS)
    )
    # NOTE: coordinator / coordinador / analyst removed from this list as of
    # 2026-05-12 — those are junior/IC titles and now route through the
    # is_junior_ic hard disqualifier below. The Haiku qualifier prompt
    # explicitly rejects them; keeping them as influencers here let them
    # deterministic-pass at in-ICP industries (size+role+industry > 75).
    is_influencer = _match_any(title_lower, INFLUENCER_KEYWORDS)
    is_digi_domain = _match_any(title_lower, DIGITALIZATION_KEYWORDS)
    is_ops_domain = _match_any(title_lower, OPERATIONS_KEYWORDS)

    # Global-scope executives (not LATAM-anchored) are out of reach for LATAM
    # cold outreach — they don't take our calls, and burning outreach budget on
    # them dilutes the signal. Score as out-of-scope (+2), not as a demoted
    # influencer. Exempt if title explicitly references a LATAM geography —
    # "VP Global Operations LATAM" is a regional head despite the word "global".
    is_global_only = (
        _match_any(title_lower, GLOBAL_EXECUTIVE_KEYWORDS)
        and not _match_any(title_lower, LATAM_GEO_OVERRIDE)
    )

    # PR-23: detect the ops-influencer-at-industrial joint case.  The two
    # signals are correlated: an ops/plant/production role is inherently
    # industry-coded, so strict additivity (role +24 + industry +12 = +36)
    # over-rewards the joint case.  The combined OPS_IN_INDUSTRIAL_COMBINED
    # bonus fires ONLY for:  is_influencer AND is_ops_domain AND in-ICP
    # industry.  Digi-influencers at ICP companies are conceptually orthogonal
    # to industry vertical (a Digital Transformation Manager is just as
    # valuable at a food company as a pharma company) and keep individual
    # deltas.  Non-ops paths (decision-maker, generic influencer, digi-only)
    # and off-ICP / unknown industry all keep their individual contributions.
    # PR-25: ops_industrial_joint only fires when industry status is "confirmed".
    # A low_confidence or unknown classification must not trigger the combined
    # +30 bonus — that would reward an unverified industry signal.
    #
    # PR-230 double-count fix: the joint flag must ALSO exclude titles the role
    # chain classifies FIRST (global-only, decision-maker) — the step-2 elif
    # chain gives those their own role credit, but step 4's joint branch keyed
    # on this flag alone stacked +30 ON TOP of the +28 decision-maker credit for
    # hybrid titles ("VP General Manager ... (production)" matches both),
    # producing totals whose breakdown didn't reconcile and deterministic passes
    # the calibration disabled. The joint case is an INFLUENCER-tier signal; this
    # makes the flag match the branch the role chain actually takes.
    is_ops_industrial_joint = (
        not is_global_only
        and not is_decision_maker
        and is_influencer
        and is_ops_domain
        and bool(industry)
        and industry in IN_ICP_INDUSTRIES
        and industry_vertical_status == "confirmed"
    )

    if is_global_only:
        score += 2
        reasons.append(f"Global-scope executive (out of LATAM scope): {title}")
    elif is_decision_maker:
        score += DECISION_MAKER_ROLE_CREDIT
        reasons.append(f"Decision-maker title: {title}")
    elif is_ops_industrial_joint:
        # Joint signal: ops-influencer at in-ICP industrial company.  The role
        # and industry bonuses are correlated; combined OPS_IN_INDUSTRIAL_COMBINED
        # replaces both.  component_scores["role"] and ["industry"] are kept at 0;
        # the joint bonus lives in component_scores["ops_in_industrial"] so it is
        # greppable and the decomposition reconciles with the total.
        # (No score added here; full bonus applied in step 4 so the industry block
        # can set component_scores["ops_in_industrial"] and suppress ["industry"].)
        reasons.append(
            f"Ops role at in-ICP industrial company"
            f" (+{OPS_IN_INDUSTRIAL_COMBINED}): {title} @ {industry}"
        )
    elif is_influencer and (is_digi_domain or is_ops_domain):
        # Innovation/ops managers are key contacts — score between decision-maker and generic influencer
        score += 24
        reasons.append(f"Domain influencer: {title}")
    elif is_influencer:
        score += 18
        reasons.append(f"Influencer title: {title}")
    elif is_digi_domain or is_ops_domain:
        score += 12
        reasons.append(f"Relevant domain: {title}")
    else:
        score += 2
        reasons.append(f"Low role fit: {title}")

    # In the joint case the role bonus is intentionally 0 here; the combined
    # +30 is applied in step 4 (which also writes component_scores["role"]=0
    # explicitly).  Skipping this delta-write in the joint path keeps step 4
    # the single load-bearing site for role/industry/ops_in_industrial keys —
    # if a future edit adds an incidental score to the joint branch above,
    # step 4 stays the source of truth instead of silently double-counting.
    if not is_ops_industrial_joint:
        component_scores["role"] = score - role_score_before
    competitor_score_before = score
    # 3. Competitor/exclusion check (0-20 points, additive)
    is_competitor = _match_any(title_lower + " " + company.lower(), COMPETITOR_KEYWORDS)
    is_academic = _match_any(title_lower, ACADEMIC_KEYWORDS)
    is_consultant = _match_any(title_lower + " " + company.lower(), CONSULTANT_KEYWORDS)

    if is_competitor:
        reasons.append("Competitor/vendor detected")
    elif is_academic:
        score += 5
        reasons.append("Academic (low priority)")
    elif is_consultant:
        score += 8
        reasons.append("Consultant (moderate priority)")
    else:
        score += NON_COMPETITOR_CREDIT
        reasons.append("Not a competitor")

    component_scores["competitor"] = score - competitor_score_before

    # 3b. Sales / commercial / marketing role hard disqualifier.
    # The shipped program's buyer is the ops tier (VP Ops, Plant Director, Director de
    # Manufactura). A sales/comercial/D2C/marketing director sits on the
    # revenue side and is not a buyer of production-scheduling tooling, no
    # matter how senior. Force into deterministic_reject by penalizing -40 —
    # short-circuited later before the LLM gate.
    #
    # Word-boundary matched (not substring) so "Salesforce Administrator",
    # "Wholesales Manager", and "Salesian University" don't trigger via
    # incidental "sales" inside another word. Exemptions list stays substring
    # because S&OP phrases vary widely.
    is_sales_role = (
        _match_any_word(title_lower, SALES_ROLE_KEYWORDS)
        and not _match_any(title_lower, SALES_ROLE_OPS_EXEMPTIONS)
    )
    if is_sales_role:
        sales_penalty_before = score
        score -= 40
        reasons.append(f"Sales/commercial role disqualifier (-40): {title}")
        component_scores["sales_role_penalty"] = score - sales_penalty_before

    # 3c. Junior / IC role hard disqualifier (mirror of 3b).
    # Coordinators, analysts, junior engineers, assistants, associates,
    # specialists. The Haiku qualifier prompt rejects these explicitly; the
    # deterministic path used to score them as influencers and let them
    # pass at in-ICP enterprise companies (84pts → enterprise_pass without
    # ever reaching the LLM). Penalty mirrors the sales-role pattern.
    # Exempt titles where a seniority signal is present (Senior Coordinator,
    # Lead Engineer, Head of Analyst Team, Director, Manager, VP, etc.).
    is_junior_ic = (
        _match_any_word(title_lower, JUNIOR_IC_KEYWORDS)
        and not _match_any(title_lower, JUNIOR_IC_EXEMPTIONS)
    )
    if is_junior_ic:
        ic_penalty_before = score
        score -= 40
        reasons.append(f"Junior/IC role disqualifier (-40): {title}")
        component_scores["junior_ic_penalty"] = score - ic_penalty_before

    # 4. Industry ICP fit. Bonus for in-ICP manufacturers (e.g. Food & Beverage,
    # Pharma, Chemicals), penalty for "Other" (non-manufacturer per the
    # Haiku classifier — tech, finance, services, distribution). Unknown is
    # neutral so the scorer behaves as before until industry is populated.
    #
    # PR-23 joint case: when is_ops_industrial_joint fired in step 2, both the
    # role bonus (+24) and the industry bonus (+12) are suppressed.  Instead,
    # OPS_IN_INDUSTRIAL_COMBINED (+30) is applied here as its own component so
    # the decomposition is greppable and the total reconciles.  The combined
    # reason was already appended in step 2; no second reason is added here.
    if is_ops_industrial_joint:
        score += OPS_IN_INDUSTRIAL_COMBINED
        component_scores["role"] = 0       # explicit: role contributed nothing individually
        component_scores["industry"] = 0   # explicit: industry contributed nothing individually
        component_scores["ops_in_industrial"] = OPS_IN_INDUSTRIAL_COMBINED
    else:
        industry_delta, industry_reason = _industry_score(industry, status=industry_vertical_status)
        score += industry_delta
        if industry_reason:
            reasons.append(industry_reason)
        component_scores["industry"] = industry_delta
        # Default the joint key to 0 so component_scores has an invariant
        # shape — downstream consumers can do `breakdown["ops_in_industrial"]`
        # without a `.get()` guard regardless of which path fired.
        component_scores["ops_in_industrial"] = 0

    # Classify persona and language.
    #
    # Midmarket (target_company_mode): keep the curated search persona.
    # _classify_persona only emits the three ENTERPRISE personas, so re-routing
    # a midmarket prospect by title would mislabel it — and the cross-search
    # upgrade path (weekly_prospect.py persona_upgraded_to_midmarket) relies on
    # the curated key being preserved here.
    #
    # Enterprise (enterprise_mode) + legacy: classify by the prospect's own
    # title. Enterprise Sales Nav searches return mixed titles (CEOs, GMs, plant
    # directors), so stamping the search persona over-assigns it — e.g. every
    # row from the digitalization_champions search became digitalization_champions
    # regardless of title, collapsing diagnostic segmentation into one cell.
    if target_company_mode and persona_config and persona_config.get("key"):
        persona = persona_config["key"]
    else:
        persona = _classify_persona(title)
    language = _detect_language(location, name)

    # Decomposition invariant (PR-230 double-count fix): every point in the
    # score must be attributed to exactly one component. The joint-bonus
    # double-count shipped totals whose breakdowns didn't sum to the total —
    # wrong scores that LOOKED explainable. Fail loud on any future drift: a
    # breakdown that doesn't reconcile is a scoring bug, not a display bug.
    _component_sum = sum(component_scores.values())
    if _component_sum != score:
        raise RuntimeError(
            f"score_prospect decomposition does not reconcile: components sum "
            f"to {_component_sum} but score is {score} "
            f"(title={title!r}, breakdown={component_scores}). This is a "
            "scoring bug — every delta must be written to component_scores."
        )

    final_score = max(0, min(100, score))
    component_scores["total"] = final_score

    scoring_lane = (
        "target_company_mode" if target_company_mode
        else "enterprise_mode" if enterprise_mode
        else "legacy"
    )

    result: dict = {
        "score": final_score,
        "pass": final_score >= 60,
        "persona": persona,
        "language": language,
        "reasons": reasons,
        # Scale version (PR-227, measurement-integrity lens): the search-credit
        # change shifted every lane's scale, so any consumer pooling scores
        # across the change date (threshold_calibration ROC, diagnostic bands)
        # mixes two scales. This stamp lets them segment or refuse. Bump it
        # whenever the component geometry changes again.
        "score_breakdown": {
            **component_scores,
            "scale_version": SCORING_SCALE_VERSION,
            "reasons": reasons,
        },
        "scoring_lane": scoring_lane,
        "verdict_path": None,
    }

    # PR-26: Expanded deterministic disqualifiers (HR/Finance/Innovation/PE/
    # State-owned, + Consulting). Run BEFORE the sales/junior_ic short-circuits
    # so that a prospect at a state-owned enterprise with title "Sales Director"
    # gets the more informative `disqualifier_state_owned` verdict (procurement-
    # path is the actionable signal for the operator; the sales-role overlap is
    # incidental). Company-based families ignore OPS_OVERRIDE; HR/Finance/
    # Innovation bypass when manufacturing-ops keywords dominate the title;
    # Consulting is bypass-exempt — see CONSULTING_TITLE_KEYWORDS.
    # PR-298: the integrator family additionally reads the parent company's
    # description, paired with the industry label already resolved above (so the
    # two consumers of that label can never disagree).
    # weekly_prospect._enrich_prospect_industry stamps company_description;
    # callers without a company record leave it absent and the family abstains
    # (see _is_integrator_service_provider).
    disqualifier_match = _match_disqualifier(
        title_lower,
        company.lower(),
        description_lower=str(prospect_data.get("company_description") or "").lower(),
        industry=industry,
        industry_status=industry_vertical_status,
    )

    # Hybrid gate: deterministic for clear-cut, Haiku for borderline.
    # Cost control — the LLM is NEVER called for score < 40 or score > 75.
    # `verdict_path` records WHICH branch decided the verdict (deterministic vs
    # borderline pass/reject). The LLM's `icp_lane` (1|2) is a separate concept
    # — see `_llm_qualify` — and is only set when the LLM was actually called.
    if disqualifier_match is not None:
        # Hard short-circuit. The score still reflects whatever the standard
        # signals computed (size + role + competitor + industry), but the
        # verdict_path tells the operator-review path which family triggered.
        # weekly_prospect._process_prospects converts this into a typed
        # `disqualifier_match` Operator Review Queue row carrying the
        # matched_keyword so operators can audit the false-positive risk.
        slug, matched_keyword = disqualifier_match
        result["pass"] = False
        result["verdict_path"] = slug
        result["disqualifier_keyword"] = matched_keyword
    elif is_sales_role:
        # Hard short-circuit: sales/commercial roles never pass, even if size +
        # industry + company-name keywords push score back into the borderline
        # band. Skip the LLM call entirely — the answer is fixed.
        result["pass"] = False
        result["verdict_path"] = "deterministic_reject_sales_role"
    elif is_junior_ic:
        # Same short-circuit logic for junior/IC roles. Order: sales-role
        # wins on tie (a "Sales Coordinator" classifies as sales, not IC) —
        # matches the dominant signal in the title.
        result["pass"] = False
        result["verdict_path"] = "deterministic_reject_junior_ic"
    elif final_score < 40 and not (
        # Adversarial-QA rescue (PR-227, pipeline-leakage lens): a
        # DECISION-MAKER title pushed under the reject line SOLELY by the
        # off-ICP industry penalty must reach the LLM band, not die in a
        # silent deterministic reject. The "Other" label is a name-only
        # classification (conglomerates / holding companies that own real
        # plants mislabel into it) and the LLM qualifier prompt is the
        # designed check against exactly that misjudgment. Guarded to the
        # decisive case only: without the industry penalty the row would have
        # cleared 40 on its own. Falling through to the borderline/LLM branch
        # keeps the row reviewable; the score itself is untouched.
        is_decision_maker
        and component_scores.get("industry", 0) < 0
        and final_score - component_scores["industry"] >= 40
    ):
        result["pass"] = False
        result["verdict_path"] = "deterministic_reject"
    elif final_score > DETERMINISTIC_PASS_THRESHOLD:
        result["pass"] = True
        result["verdict_path"] = "enterprise_pass" if enterprise_mode else "target_pass"
        # PR-28 icp_lane_persisted fix: deterministic pass implies the
        # lane from persona_config. Previously the field was left None
        # for deterministic verdicts on the rationale that "no LLM ran",
        # but the LANE itself IS known (it's structural to the persona
        # config that drove the harvest). Setting it explicitly so
        # icp_lane_persisted is populated for every committed prospect,
        # not just borderline-LLM-classified ones.
        if enterprise_mode:
            result["icp_lane"] = 1
        elif target_company_mode:
            result["icp_lane"] = 2
    else:  # borderline 40–75
        # Three paths to LLM qualification: explicit client (tests),
        # dispatch (F-PR-9 production), or deterministic fallback.
        # Dispatch fires when either the explicit flag OR the env var
        # `OUTBOUND_USE_LLM_DISPATCH=1` is set (skill exports it before
        # invoking cli.py daily).
        from workflows.llm_dispatch import is_dispatch_enabled
        run_llm = (
            anthropic_client is not None
            or use_llm_dispatch
            or is_dispatch_enabled()
        )
        if run_llm:
            llm_result = _llm_qualify(prospect_data, persona_config, anthropic_client)
            result["icp_lane"] = llm_result.get("icp_lane")
            result["llm_rationale"] = llm_result.get("rationale")
            if llm_result.get("ledger_unavailable") and agent_gate:
                # Budget-ledger INFRA failure with a staging-capable caller
                # (the weekly flow): fail OPEN to the agent staging path.
                # The prospect lands in weekly_borderline_<date>.jsonl and
                # the operator qualifies it via weekly-finalize — an infra
                # outage is not a prospect-quality signal, and no dispatch
                # ran so §3.7 is not bypassed (PR-216 incident fix:
                # borderlines were silently rejected with no artifact).
                # Cap exhaustion never reaches this branch — it carries
                # cost_exhausted, handled fail-closed below.
                result["pass"] = None  # sentinel: pending agent qualification
                result["needs_agent_qualification"] = True
                result["qualification_prompt"] = render_qualification_prompt(
                    prospect_data, persona_config,
                )
                result["ledger_unavailable"] = True
                # verdict_path stays None until the agent fills it in —
                # same contract as the agent_gate staging branch below.
            elif llm_result.get("cost_exhausted"):
                # Cost-ceiling breach mid-run: no LLM verdict exists. Same class
                # of infra signal as the ledger outage above — an unqualified
                # borderline must NOT commit at the deterministic threshold
                # (score>=60 is a coin-flip). PR-222 Rec D: fail CLOSED to
                # STAGING, mirroring the ledger treatment — with a staging-
                # capable caller (agent_gate: the weekly flow) the prospect
                # lands in weekly_borderline_<date>.jsonl for operator
                # qualification. Distinct `cost_exhausted_staged` flag so the
                # staged bucket is greppable apart from the ledger/error
                # buckets. Cap exhaustion never calls the LLM, so spend
                # semantics stay fail-closed. Without a staging caller (tests)
                # fail CLOSED to reject rather than commit unvetted at >=60.
                if agent_gate:
                    result["pass"] = None  # sentinel: pending agent qualification
                    result["needs_agent_qualification"] = True
                    result["qualification_prompt"] = render_qualification_prompt(
                        prospect_data, persona_config,
                    )
                    result["cost_exhausted_staged"] = True
                    # verdict_path stays None until the agent fills it in.
                else:
                    result["pass"] = False
                    result["verdict_path"] = "borderline_cost_exhausted"
            elif llm_result.get("llm_failed"):
                # Transient Haiku failure (rate-limit, parse error): no verdict
                # exists. PR-222 Rec D: same fail-CLOSED-to-STAGING treatment as
                # cost-exhaustion — an infra blip is not a prospect-quality
                # signal, so stage (not commit, not silently reject) when a
                # staging-capable caller is present. Distinct `llm_error_staged`
                # flag for its own bucket. Without a staging caller (tests) fail
                # CLOSED to reject rather than commit unvetted at score>=60.
                if agent_gate:
                    result["pass"] = None  # sentinel: pending agent qualification
                    result["needs_agent_qualification"] = True
                    result["qualification_prompt"] = render_qualification_prompt(
                        prospect_data, persona_config,
                    )
                    result["llm_error_staged"] = True
                    # verdict_path stays None until the agent fills it in.
                else:
                    result["pass"] = False
                    result["verdict_path"] = "borderline_llm_error"
            else:
                result["pass"] = llm_result["pass"]
                result["verdict_path"] = "borderline_pass" if llm_result["pass"] else "borderline_reject"
        elif agent_gate:
            # Agent-driven path: stage for the calling agent to qualify via Haiku subagent
            result["pass"] = None  # sentinel: pending agent qualification
            result["needs_agent_qualification"] = True
            result["qualification_prompt"] = render_qualification_prompt(prospect_data, persona_config)
            # verdict_path stays None until the agent fills it in
        else:
            # No client — fall back to deterministic threshold (safe for tests)
            result["pass"] = final_score >= 60
            result["verdict_path"] = "borderline_pass" if final_score >= 60 else "borderline_reject"

    return result


def classify_response(message_text: str, **_kwargs) -> dict:
    """Classify a prospect's response message using keyword matching.

    For nuanced classification, run check-responses interactively via Claude Code.

    Returns:
        Dict with classification, suggested_action, and summary.
    """
    text = message_text.lower().strip()

    # Negative signals (check first — strongest signal)
    negative_keywords = [
        "no interest", "not interested", "no me interesa", "não tenho interesse",
        "remove me", "stop", "unsubscribe", "wrong person", "persona equivocada",
        "no thanks", "no gracias", "não obrigado", "already have", "ya tenemos",
    ]
    if _match_any(text, negative_keywords):
        return {
            "classification": "negative",
            "suggested_action": "Move to Not Interested. Do not follow up.",
            "summary": "Prospect declined or asked to stop messaging.",
        }

    # Defensive / reactance signals — prospect is pushing back on a premise,
    # not genuinely declining. Short refutations with emphatic certainty
    # language. Check AFTER negative (polite "not interested" should stay
    # negative) but BEFORE positive (defensive replies often mention the
    # company in ways that pattern-match positive keywords).
    #
    # TODO: upgrade to LLM classifier (workflows/response_classifier.py) when
    # available. Keyword detection is ~60% accurate on reactance; the LLM
    # classifier is the primary path and this is the fallback.
    defensive_keywords = [
        # Spanish
        "estoy segura que no", "estoy seguro que no", "con toda certeza",
        "en absoluto", "no es nuestro caso", "eso no aplica",
        "no es así", "te equivocas", "no aplica a nosotros",
        # English
        "not accurate", "that's not", "actually we", "we don't have that",
        "incorrect", "wrong assumption", "that's wrong",
        # Portuguese
        "não é o nosso caso", "isso não se aplica", "está enganado",
        "não se aplica", "em absoluto",
    ]
    if _match_any(text, defensive_keywords):
        return {
            "classification": "defensive",
            "suggested_action": (
                "Stop automated sequence for this prospect. Send humble "
                "recovery reply manually. Do NOT retry opener variant on "
                "this prospect."
            ),
            "summary": (
                "Prospect pushed back on the premise of the opener "
                "(reactance — not genuine rejection)."
            ),
        }

    # Positive signals
    positive_keywords = [
        "interested", "interesado", "interessado", "let's talk", "platiquemos",
        "vamos conversar", "show me", "demo", "schedule", "agendar", "agenda",
        "tell me more", "cuéntame más", "me conta mais", "available",
        "disponible", "disponível", "sounds good", "suena bien", "parece bom",
        "send me", "envíame", "me manda", "my calendar", "mi calendario",
    ]
    if _match_any(text, positive_keywords):
        return {
            "classification": "positive",
            "suggested_action": "Reply personally. Book a diagnosis call ASAP.",
            "summary": "Prospect showed interest or asked for more info.",
        }

    # Question signals
    question_keywords = [
        "how much", "cuánto cuesta", "quanto custa", "pricing", "precio",
        "preço", "what does", "qué hace", "o que faz", "how does",
        "cómo funciona", "como funciona", "?",
    ]
    if _match_any(text, question_keywords):
        return {
            "classification": "question",
            "suggested_action": "Answer their question, then ask for a call.",
            "summary": "Prospect asked a question about the product.",
        }

    # Default to neutral
    return {
        "classification": "neutral",
        "suggested_action": "Review manually — no clear signal detected.",
        "summary": "Polite acknowledgment or unclear intent.",
    }
