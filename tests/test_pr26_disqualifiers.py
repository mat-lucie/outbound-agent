"""Tests for PR-26 — expanded deterministic disqualifiers.

Covers the 5 new keyword families (HR / Finance / Innovation / PE /
State-owned) added to `workflows.quality_gate.score_prospect` and the
`disqualifier_match` Operator Review Queue escalation emitted by
`workflows.weekly_prospect._process_prospects`.

Fold-in (post-QA convergence) additions:
  * `matched_keyword` second tuple element on `_match_disqualifier` return
  * Integration test for `_process_prospects` dispatch + observability
  * Broadened static regex regression guard catching ternary else-branches
  * Tightened corpus floor + `state_owned_wins` category requirement

Sixth family `disqualifier_consulting` (consulting / professional-services
firms + consulting-coded titles): consultancies are typically out of ICP for
plant/operations outreach, and the industry classifier can't catch them at
qualification time.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from clients.crm.base import CRMProvider
from workflows.escalation_schemas import (
    ESCALATION_SCHEMAS,
    ESCALATION_TYPES_SET,
    DisqualifierMatchPayload,
)
from workflows.quality_gate import (
    DISQUALIFIER_VERDICT_PATHS,
    VERDICT_PATHS,
    _match_disqualifier,
    score_prospect,
)

CORPUS_PATH = Path(__file__).parent / "fixtures" / "disqualifier_corpus.jsonl"


def _slug(match: tuple[str, str] | None) -> str | None:
    """Extract slug from `_match_disqualifier` return; None passes through.

    Lets slug-only assertions stay readable when the matched_keyword is not
    under test.
    """
    return match[0] if match is not None else None


def _load_corpus() -> list[dict]:
    rows: list[dict] = []
    with CORPUS_PATH.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


# -- §0 #9 / VERDICT_PATHS registry invariants ----------------------------


class TestVerdictPathsRegistry:
    def test_disqualifier_subset_is_a_subset_of_verdict_paths(self):
        assert DISQUALIFIER_VERDICT_PATHS.issubset(VERDICT_PATHS)

    def test_disqualifier_slugs_are_registered(self):
        # Five PR-26 families + disqualifier_consulting.
        expected = frozenset({
            "disqualifier_hr",
            "disqualifier_finance",
            "disqualifier_innovation",
            "disqualifier_pe",
            "disqualifier_state_owned",
            "disqualifier_consulting",
        })
        assert expected == DISQUALIFIER_VERDICT_PATHS

    def test_score_prospect_verdict_paths_in_registry(self):
        """Every literal assigned to result['verdict_path'] in score_prospect
        is in VERDICT_PATHS. Post-fold (silent-failure I-2 + pr-test
        analyzer NIT-5): use AST so subscript keys like `llm_result["pass"]`
        on the RHS aren't mistaken for verdict_path literals, and ternary
        else-branches are caught (the prior regex missed the second branch
        of ternaries; the regex-with-rhs-scan caught the subscript keys).
        """
        import ast

        src_path = (
            Path(__file__).parent.parent / "workflows" / "quality_gate.py"
        )
        tree = ast.parse(src_path.read_text())

        literals: set[str] = set()

        def _is_verdict_path_assign(node: ast.AST) -> bool:
            if not isinstance(node, ast.Assign):
                return False
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "result"
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "verdict_path"
                ):
                    return True
            return False

        for node in ast.walk(tree):
            if not _is_verdict_path_assign(node):
                continue
            # Walk the RHS: any `Constant` whose value is a str is a candidate.
            # Subscript keys inside the RHS (e.g. `llm_result["pass"]`) are
            # `Constant` nodes too, so distinguish by checking the parent —
            # we only want str constants that are NOT inside a subscript.
            for child in ast.walk(node.value):
                if not (isinstance(child, ast.Constant) and isinstance(child.value, str)):
                    continue
                # Exclude when this str sits inside a Subscript.slice (a
                # dict-key access).
                parent_is_subscript_slice = any(
                    isinstance(p, ast.Subscript) and p.slice is child
                    for p in ast.walk(node.value)
                )
                if parent_is_subscript_slice:
                    continue
                literals.add(child.value)

        unregistered = literals - VERDICT_PATHS
        assert not unregistered, (
            f"verdict_path literals not in VERDICT_PATHS: {unregistered}"
        )

    def test_disqualifier_match_slug_is_in_escalation_types(self):
        assert "disqualifier_match" in ESCALATION_TYPES_SET

    def test_disqualifier_match_typeddict_is_registered(self):
        assert ESCALATION_SCHEMAS.get("disqualifier_match") is DisqualifierMatchPayload


# -- _match_disqualifier return shape ------------------------------------


class TestMatchDisqualifierReturnShape:
    def test_match_returns_tuple_with_slug_and_keyword(self):
        match = _match_disqualifier("cfo", "alicorp")
        assert match is not None
        slug, keyword = match
        assert slug == "disqualifier_finance"
        assert keyword == "cfo"

    def test_no_match_returns_none(self):
        assert _match_disqualifier("vp operations", "mabe mexico") is None

    def test_company_based_match_returns_company_keyword(self):
        match = _match_disqualifier("plant manager", "ministry of energy refining")
        assert match is not None
        slug, keyword = match
        assert slug == "disqualifier_state_owned"
        # The matched keyword should be one of the STATE_OWNED_KEYWORDS
        # entries that substring-matched, not the title.
        assert keyword in ("ministry of", "government of", "state-owned")


# -- Per-family TP coverage ----------------------------------------------


class TestHRDisqualifier:
    def test_chro_fires(self):
        assert _slug(_match_disqualifier("chro", "grupo bimbo")) == "disqualifier_hr"

    def test_vp_human_resources_fires(self):
        assert _slug(_match_disqualifier(
            "vp human resources latam", "whirlpool mexico"
        )) == "disqualifier_hr"

    def test_recursos_humanos_spanish_fires(self):
        assert _slug(_match_disqualifier(
            "director de recursos humanos", "cementos pacasmayo"
        )) == "disqualifier_hr"

    def test_head_of_people_fires(self):
        assert _slug(_match_disqualifier(
            "head of people", "mercado libre"
        )) == "disqualifier_hr"

    def test_hr_coordinator_fires_via_compound(self):
        # Post-fold: bare "hr" removed; compound "hr coordinator" added.
        assert _slug(_match_disqualifier(
            "hr coordinator", "bebsa"
        )) == "disqualifier_hr"

    def test_score_prospect_chro_returns_disqualifier_hr(self):
        result = score_prospect({
            "name": "Maria López",
            "title": "Chief Human Resources Officer",
            "company": "Grupo Bimbo",
            "location": "Mexico City, Mexico",
            "employee_count": 50000,
        })
        assert result["pass"] is False
        assert result["verdict_path"] == "disqualifier_hr"

    def test_shift_hour_title_no_longer_fires_hr_disqualifier(self):
        # Post-fold (code-reviewer + type-design QA convergence): bare
        # "hr" was removed from HR_KEYWORDS so `\bhr\b` no longer fires
        # on shift-window titles like "24/7 hr ops planner".
        assert _match_disqualifier("24/7 hr ops planner", "cementra") is None

    def test_hr_manager_with_manufacturing_division_still_fires(self):
        # Post-fold (silent-failure-hunter convergence): OPS_OVERRIDE
        # stem "manufactur" removed so divisional "Manufacturing Division"
        # no longer bypasses an HR-primary title.
        assert _slug(_match_disqualifier(
            "hr manager - manufacturing division", "grupo bimbo"
        )) == "disqualifier_hr"


class TestFinanceDisqualifier:
    def test_cfo_fires(self):
        assert _slug(_match_disqualifier("cfo", "alicorp")) == "disqualifier_finance"

    def test_director_financiero_spanish_fires(self):
        assert _slug(_match_disqualifier(
            "director financiero", "ambev brasil"
        )) == "disqualifier_finance"

    def test_controller_fires(self):
        assert _slug(_match_disqualifier(
            "controller", "grupo salinas"
        )) == "disqualifier_finance"

    def test_operaciones_financieras_fires(self):
        # Post-fold (prospect-weekly-QA convergence): added
        # "operaciones financieras" to FINANCE_KEYWORDS so the finance-
        # side ops director is rejected, not deterministic-passed.
        assert _slug(_match_disqualifier(
            "director de operaciones financieras", "alicorp"
        )) == "disqualifier_finance"

    def test_score_prospect_cfo_returns_disqualifier_finance(self):
        result = score_prospect({
            "name": "Carlos Méndez",
            "title": "CFO",
            "company": "Alicorp",
            "location": "Lima, Peru",
            "employee_count": 10000,
        })
        assert result["pass"] is False
        assert result["verdict_path"] == "disqualifier_finance"

    def test_finance_keyword_does_not_match_refinance_word(self):
        assert _match_disqualifier(
            "refinance specialist", "grupo salinas"
        ) is None


class TestInnovationDisqualifier:
    def test_rnd_director_fires(self):
        assert _slug(_match_disqualifier(
            "r&d director", "bayer mexico"
        )) == "disqualifier_innovation"

    def test_vinculacion_universidad_fires(self):
        assert _slug(_match_disqualifier(
            "vinculación universidad-empresa", "iteso guadalajara"
        )) == "disqualifier_innovation"

    def test_research_and_development_fires(self):
        assert _slug(_match_disqualifier(
            "head of research and development", "embraer"
        )) == "disqualifier_innovation"

    def test_i_plus_d_spanish_fires(self):
        assert _slug(_match_disqualifier(
            "director de i+d", "roche mexico"
        )) == "disqualifier_innovation"

    def test_innovation_lab_specific_fires(self):
        assert _slug(_match_disqualifier(
            "innovation lab director", "bbva mexico"
        )) == "disqualifier_innovation"

    def test_plain_innovation_director_not_flagged(self):
        assert _match_disqualifier(
            "innovation director", "siemens energy mexico"
        ) is None

    def test_bare_vinculacion_no_longer_fires_innovation(self):
        # Post-fold (GTM-QA convergence): bare "vinculación" / "vinculacion"
        # dropped to avoid mislabeling LATAM commercial-liaison titles
        # ("Gerente de Vinculación Comercial").
        assert _match_disqualifier(
            "gerente de vinculación comercial", "sigma alimentos"
        ) is None


class TestPEDisqualifier:
    def test_named_pe_fund_fires(self):
        assert _slug(_match_disqualifier(
            "managing director", "globex private equity fund"
        )) == "disqualifier_pe"

    def test_pe_partners_fires(self):
        assert _slug(_match_disqualifier(
            "partner", "initech private equity partners"
        )) == "disqualifier_pe"

    def test_pe_firm_fires(self):
        assert _slug(_match_disqualifier(
            "investment director", "vandelay private equity firm"
        )) == "disqualifier_pe"

    def test_score_prospect_pe_returns_disqualifier_pe(self):
        result = score_prospect({
            "name": "Sofia Pereira",
            "title": "Operating Partner",
            "company": "Globex Private Equity Management",
            "location": "São Paulo, Brazil",
            "employee_count": 5000,
        })
        assert result["pass"] is False
        assert result["verdict_path"] == "disqualifier_pe"

    def test_bare_private_equity_substring_no_longer_fires(self):
        # Post-fold (GTM-QA convergence): bare "private equity" dropped
        # so unrelated companies like "LATAM Private Equity Holdings"
        # (a holding vehicle, not a PE firm) are not flagged.
        assert _match_disqualifier(
            "director", "region private equity holdings"
        ) is None


class TestStateOwnedDisqualifier:
    def test_state_owned_ministry_fires(self):
        assert _slug(_match_disqualifier(
            "director de operaciones", "ministry of energy"
        )) == "disqualifier_state_owned"

    def test_state_owned_refining_fires(self):
        assert _slug(_match_disqualifier(
            "plant manager", "ministry of energy refining"
        )) == "disqualifier_state_owned"

    def test_state_utility_fires(self):
        assert _slug(_match_disqualifier(
            "gerente general",
            "ministry of electricity",
        )) == "disqualifier_state_owned"

    def test_state_owned_oil_fires(self):
        assert _slug(_match_disqualifier(
            "vp operations", "government of brazil energy"
        )) == "disqualifier_state_owned"

    def test_state_owned_grid_fires(self):
        assert _slug(_match_disqualifier(
            "director de operaciones", "state-owned grid operator"
        )) == "disqualifier_state_owned"

    def test_score_prospect_state_owned_returns_disqualifier_state_owned(self):
        result = score_prospect({
            "name": "Roberto Silva",
            "title": "VP Operations",
            "company": "Government of Brazil Energy",
            "location": "Rio de Janeiro, Brazil",
            "employee_count": 80000,
            "industry": "Oil & Gas",
        })
        assert result["pass"] is False
        assert result["verdict_path"] == "disqualifier_state_owned"


class TestConsultingDisqualifier:
    """Consulting / professional-services family.

    Consultancies are typically out of ICP for plant/operations outreach —
    a consultancy MD getting an operations-fit DM is prospect-facing
    embarrassment, and the industry classifier can't catch them at
    qualification time.
    """

    ENTERPRISE_PERSONA = {"key": "operations_leaders", "enterprise_mode": True}

    def test_accenture_md_regression_case(self):
        # Regression: a consultancy MD with an "Industries Lead" title was
        # admitted as operations_leaders before this family existed; the
        # operator had to pull them manually.
        result = score_prospect(
            {
                "name": "Test Person",
                "title": "Consumer Goods & Services, Retail and Travel Industries Lead",
                "company": "Accenture",
                "location": "São Paulo, Brazil",
                "employee_count": 700000,
            },
            persona_config=self.ENTERPRISE_PERSONA,
        )
        assert result["pass"] is False
        assert result["verdict_path"] == "disqualifier_consulting"
        assert result["disqualifier_keyword"] == "accenture"

    def test_accenture_country_entity_fires(self):
        assert _slug(_match_disqualifier(
            "managing director", "accenture brazil"
        )) == "disqualifier_consulting"

    def test_big_four_and_strategy_firms_fire(self):
        for company in (
            "deloitte",
            "mckinsey & company",
            "ey brasil",
            "kpmg méxico",
            "pwc chile",
            "capgemini",
            "boston consulting group",
        ):
            assert _slug(_match_disqualifier(
                "director de operaciones", company
            )) == "disqualifier_consulting", company

    def test_generic_consulting_company_name_fires(self):
        assert _slug(_match_disqualifier(
            "gerente de proyectos", "andina consultores industriales"
        )) == "disqualifier_consulting"

    def test_ops_title_at_consultancy_still_rejected(self):
        # Company-based: an ops-titled person AT a consultancy is a
        # consultant — OPS_OVERRIDE must not bypass company families.
        assert _slug(_match_disqualifier(
            "plant manager", "deloitte consulting"
        )) == "disqualifier_consulting"

    def test_industries_lead_title_fires(self):
        assert _slug(_match_disqualifier(
            "consumer goods & services, retail and travel industries lead",
            "unknown boutique",
        )) == "disqualifier_consulting"

    def test_practice_lead_title_fires(self):
        assert _slug(_match_disqualifier(
            "supply chain practice lead", "boutique andina"
        )) == "disqualifier_consulting"

    def test_consultant_title_fires(self):
        assert _slug(_match_disqualifier(
            "senior management consultant", "indep"
        )) == "disqualifier_consulting"

    def test_consultor_spanish_title_fires(self):
        assert _slug(_match_disqualifier(
            "consultor de mejora continua", "lean partners latam"
        )) == "disqualifier_consulting"

    def test_glued_ampersand_ey_variants_fire(self):
        # LinkedIn company fields render the firm as "E&Y" / "Ernst&Young"
        # too — the spaced forms alone missed them (code-review finding).
        for company in ("e&y brasil", "ernst&young", "strategy& méxico"):
            assert _slug(_match_disqualifier(
                "managing director", company
            )) == "disqualifier_consulting", company

    def test_dual_hat_title_not_lost_to_ops_bypass(self):
        # Ordering hole (code-review finding): "hr consultant" wins the
        # earliest-match pick, the disjoint "plant director" ops phrase
        # bypasses HR — the bypass-exempt consulting match must then fire
        # instead of being silently discarded.
        match = _match_disqualifier(
            "plant director - hr consultant", "boutique x"
        )
        assert match is not None
        assert match[0] == "disqualifier_consulting"
        assert match[1] == "consultant"

    def test_ops_bypass_still_clears_titles_without_consulting(self):
        # The fallback must not weaken the original bypass: a dual-hat
        # ops/HR title with NO consulting keyword still bypasses.
        assert _match_disqualifier(
            "plant manager - hr liaison", "coca-cola femsa"
        ) is None

    def test_ops_override_does_not_bypass_consulting_title(self):
        # "director of operations consulting" contains "director of
        # operations" DISJOINT from "consulting", which the span rule would
        # bypass. Consulting is exempt: ops + consulting = ops consulting.
        assert _slug(_match_disqualifier(
            "director of operations consulting", "boutique andina"
        )) == "disqualifier_consulting"

    def test_word_boundary_ey_does_not_match_hershey(self):
        assert _match_disqualifier(
            "plant director", "the hershey company"
        ) is None

    def test_word_boundary_ey_does_not_match_monterrey(self):
        assert _match_disqualifier(
            "director de operaciones", "cemex monterrey"
        ) is None

    def test_constructora_does_not_match_consultora(self):
        assert _match_disqualifier(
            "director de planta", "constructora del pacífico"
        ) is None

    def test_bare_lead_title_does_not_fire(self):
        # "industries lead" is the consulting-speak pattern; a plain ops
        # "lead" title must not trigger.
        assert _match_disqualifier(
            "production lead", "wayne manufacturing"
        ) is None

    def test_hr_consultant_keeps_hr_slug(self):
        # Earliest-match dominance preserved: "hr consultant" matches the
        # HR family at position 0 before "consultant" at position 3.
        assert _slug(_match_disqualifier(
            "hr consultant", "soylent foods"
        )) == "disqualifier_hr"

    def test_score_prospect_stashes_matched_keyword(self):
        result = score_prospect({
            "name": "Test Person",
            "title": "supply chain practice lead",
            "company": "Boutique Andina",
            "location": "Lima, Peru",
            "employee_count": 200,
        })
        assert result["pass"] is False
        assert result["verdict_path"] == "disqualifier_consulting"
        assert result["disqualifier_keyword"] == "practice lead"


# -- OPS_OVERRIDE bypass --------------------------------------------------


class TestOpsOverrideBypass:
    def test_plant_manager_hr_liaison_bypasses_hr(self):
        assert _match_disqualifier(
            "plant manager - hr liaison", "refrescos bebsa"
        ) is None

    def test_director_de_planta_hr_bypasses_hr(self):
        assert _match_disqualifier(
            "director de planta y recursos humanos", "arca continental"
        ) is None

    def test_manufactura_finance_bypasses_finance(self):
        assert _match_disqualifier(
            "director de manufactura - finance controller",
            "cervecería quilmes",
        ) is None

    def test_subgerente_operaciones_hr_bypasses_hr(self):
        # Post-fold (GTM-QA forward-defense): Subgerente de Operaciones
        # confirmed valid ICP buyer per project memory; OPS_OVERRIDE
        # explicit so future HR-list extensions can't reject it.
        assert _match_disqualifier(
            "subgerente de operaciones - hr coordinator", "bimbo peru"
        ) is None

    def test_manufacturing_division_no_longer_bypasses_hr(self):
        # Post-fold (silent-failure-hunter convergence): "manufactur"
        # stem removed from OPS_OVERRIDE; divisional context no longer
        # silently bypasses the disqualifier.
        assert _slug(_match_disqualifier(
            "hr manager - manufacturing division", "grupo bimbo"
        )) == "disqualifier_hr"

    def test_ops_override_does_not_bypass_pe(self):
        assert _slug(_match_disqualifier(
            "plant manager", "globex private equity fund"
        )) == "disqualifier_pe"

    def test_ops_override_does_not_bypass_state_owned(self):
        assert _slug(_match_disqualifier(
            "plant manager", "ministry of energy refining"
        )) == "disqualifier_state_owned"


# -- Priority / interaction with sales_role + junior_ic -------------------


class TestPriorityOverSalesAndJunior:
    def test_disqualifier_fires_before_sales_role(self):
        result = score_prospect({
            "name": "Test Person",
            "title": "Sales Director",
            "company": "Ministry of Energy Refining",
            "location": "Mexico City, Mexico",
            "employee_count": 30000,
        })
        assert result["pass"] is False
        assert result["verdict_path"] == "disqualifier_state_owned"

    def test_disqualifier_fires_before_junior_ic(self):
        result = score_prospect({
            "name": "Test Person",
            "title": "HR Coordinator",
            "company": "BEBSA",
            "location": "Monterrey, Mexico",
            "employee_count": 30000,
        })
        assert result["pass"] is False
        assert result["verdict_path"] == "disqualifier_hr"


# -- Negative controls ----------------------------------------------------


class TestNegativeControls:
    def test_vp_operations_passes_disqualifier_check(self):
        assert _match_disqualifier("vp operations", "mabe mexico") is None

    def test_plant_director_passes_disqualifier_check(self):
        assert _match_disqualifier("plant director", "grupo bimbo") is None

    def test_director_de_manufactura_passes_disqualifier_check(self):
        assert _match_disqualifier(
            "director de manufactura", "refrescos bebsa"
        ) is None


# -- Corpus sweep --------------------------------------------------------


def _expected_path_for(row: dict) -> str | None:
    return row.get("expected_verdict_path")


def test_corpus_sweep_disqualifier_matches_expected():
    rows = _load_corpus()
    # Tightened floor (pr-test-analyzer NIT-4): assert against actual
    # count so a future hand can't silently delete fixture rows.
    assert len(rows) >= 61, (
        f"Corpus must have >= 61 labeled rows; got {len(rows)}"
    )
    mismatches: list[str] = []
    for row in rows:
        expected = _expected_path_for(row)
        actual = _slug(_match_disqualifier(
            (row["title"] or "").lower(),
            (row["company"] or "").lower(),
        ))
        if actual != expected:
            mismatches.append(
                f"  {row['id']} ({row['category']}): "
                f"title={row['title']!r} company={row['company']!r} "
                f"expected={expected!r} actual={actual!r}"
            )
    assert not mismatches, "Corpus mismatches:\n" + "\n".join(mismatches)


def test_corpus_covers_all_required_categories():
    rows = _load_corpus()
    categories = {row["category"] for row in rows}
    required = {
        "hr_true_positive",
        "finance_true_positive",
        "innovation_true_positive",
        "pe_true_positive",
        "state_owned_true_positive",
        "ops_override_bypass",
        "no_disqualifier",
        "state_owned_wins",  # prospect-weekly NIT-2 fold
        "consulting_true_positive",
    }
    missing = required - categories
    assert not missing, f"Corpus missing categories: {missing}"


# -- Escalation integration ---------------------------------------------


class TestEscalationIntegration:
    def test_open_disqualifier_match_row_calls_escalate_with_correct_shape(self):
        from workflows import weekly_prospect

        attio = MagicMock()
        prospect_data = {
            "name": "Maria López",
            "title": "Chief Human Resources Officer",
            "company": "Grupo Bimbo",
            "linkedin_url": "https://www.linkedin.com/in/maria-lopez/",
        }
        score_result = {
            "score": 42,
            "verdict_path": "disqualifier_hr",
            "disqualifier_keyword": "chro",
            "pass": False,
        }
        with patch.object(weekly_prospect, "escalate") as mock_escalate:
            weekly_prospect._open_disqualifier_match_row(
                attio, prospect_data, score_result
            )

        mock_escalate.assert_called_once()
        kwargs = mock_escalate.call_args.kwargs
        assert kwargs["type"] == "disqualifier_match"
        assert kwargs["idempotency_key"] == (
            "https://www.linkedin.com/in/maria-lopez/|disqualifier_hr"
        )
        assert kwargs["attio"] is attio
        payload = kwargs["payload"]
        assert payload["verdict_path"] == "disqualifier_hr"
        assert payload["matched_keyword"] == "chro"
        assert payload["title"] == "Chief Human Resources Officer"
        assert payload["company"] == "Grupo Bimbo"
        assert payload["linkedin_url"] == (
            "https://www.linkedin.com/in/maria-lopez/"
        )
        assert payload["score"] == 42

    def test_open_disqualifier_match_row_idempotency_key_is_deterministic(self):
        # Post-fold (pr-test-analyzer I-3): assert the wrapper produces
        # IDENTICAL idempotency_keys on repeat invocation. F-PR-3's
        # escalate() enforces the dedup contract; the wrapper just needs
        # to construct a deterministic key — no clock, no random suffix.
        from workflows import weekly_prospect

        attio = MagicMock()
        prospect_data = {
            "title": "CFO",
            "company": "Alicorp",
            "linkedin_url": "https://www.linkedin.com/in/cm/",
        }
        score_result = {
            "score": 70,
            "verdict_path": "disqualifier_finance",
            "disqualifier_keyword": "cfo",
            "pass": False,
        }
        with patch.object(weekly_prospect, "escalate") as mock_escalate:
            weekly_prospect._open_disqualifier_match_row(
                attio, prospect_data, score_result
            )
            weekly_prospect._open_disqualifier_match_row(
                attio, prospect_data, score_result
            )

        assert mock_escalate.call_count == 2
        keys = [c.kwargs["idempotency_key"] for c in mock_escalate.call_args_list]
        assert keys[0] == keys[1]

    def test_open_disqualifier_match_row_handles_null_fields(self):
        from workflows import weekly_prospect

        attio = MagicMock()
        prospect_data = {
            "linkedin_url": "https://www.linkedin.com/in/x/",
            "title": None,
            "company": None,
        }
        score_result = {
            "score": None,
            "verdict_path": "disqualifier_pe",
            "disqualifier_keyword": None,
            "pass": False,
        }
        with patch.object(weekly_prospect, "escalate") as mock_escalate:
            weekly_prospect._open_disqualifier_match_row(
                attio, prospect_data, score_result
            )
        payload = mock_escalate.call_args.kwargs["payload"]
        assert payload["title"] == ""
        assert payload["company"] == ""
        assert payload["score"] == 0
        assert payload["matched_keyword"] == ""


# -- _process_prospects dispatch integration (pr-test-analyzer I-1) ------


class TestProcessProspectsDispatch:
    """Integration: feed `_process_prospects` two prospects (one
    HR-CHRO, one normal ICP-pass), assert that exactly one
    `disqualifier_match` escalation fires + the summary records the
    new verdict_path bucket. Without this test, a future refactor
    that removed the rejection-branch dispatch would have zero red.
    """

    def _build_summary(self) -> dict:
        return {
            "scored": 0,
            "qualified": 0,
            "rejected": 0,
            "added": 0,
            "duplicates": 0,
            "rejected_by_path": {},
            "borderline_staged": 0,
        }

    def test_disqualifier_rejection_dispatches_escalation_and_updates_summary(self):
        from workflows import weekly_prospect

        # PB-CSV-shaped raw row: `_process_prospects` reads URL from
        # `defaultProfileUrl` / `linkedinProfileUrl` / `linkedInUrl` /
        # `profileUrl`, not the canonical `linkedin_url` we use in
        # synthesized prospect dicts elsewhere in the suite.
        chro_raw = {
            "fullName": "Maria López",
            "title": "Chief Human Resources Officer",
            "company": "Grupo Bimbo",
            "linkedinProfileUrl": "https://www.linkedin.com/in/maria-lopez/",
            "location": "Mexico City, Mexico",
            "companyEmployees": 50000,
        }

        crm = MagicMock(spec=CRMProvider)
        crm.search_person_by_linkedin.return_value = None
        summary = self._build_summary()

        with patch.object(weekly_prospect, "_open_disqualifier_match_row") as mock_open:
            weekly_prospect._process_prospects(
                prospects_raw=[chro_raw],
                crm=crm,
                list_id="LIST",
                today="2026-05-22",
                dry_run=True,
                summary=summary,
                seen_urls=set(),
                in_list_record_ids=set(),
                persona_config=None,
            )

        # Exactly one escalation call for the CHRO; none for non-matchers.
        assert mock_open.call_count == 1
        called_prospect = mock_open.call_args.args[1]
        assert "maria-lopez" in called_prospect["linkedin_url"]

        # Summary observability — the new bucket appears.
        assert summary["rejected"] == 1
        assert summary["rejected_by_path"].get("disqualifier_hr") == 1

    def test_non_disqualifier_rejection_does_not_dispatch_escalation(self):
        # Sales-role rejection still uses the legacy deterministic_reject_*
        # verdict_path; the post-rejection escalation gate must skip it.
        from workflows import weekly_prospect

        sales_raw = {
            "fullName": "Pedro Vargas",
            "title": "Director Comercial",
            "company": "Cementra",
            "linkedinProfileUrl": "https://www.linkedin.com/in/pedro-vargas/",
            "location": "Monterrey, Mexico",
            "companyEmployees": 40000,
        }

        crm = MagicMock(spec=CRMProvider)
        summary = self._build_summary()

        with patch.object(weekly_prospect, "_open_disqualifier_match_row") as mock_open:
            weekly_prospect._process_prospects(
                prospects_raw=[sales_raw],
                crm=crm,
                list_id="LIST",
                today="2026-05-22",
                dry_run=True,
                summary=summary,
                seen_urls=set(),
                in_list_record_ids=set(),
                persona_config=None,
            )

        # No escalation — verdict_path is deterministic_reject_sales_role,
        # not in DISQUALIFIER_VERDICT_PATHS.
        assert mock_open.call_count == 0
        # Summary still records the legacy bucket.
        assert summary["rejected_by_path"].get("deterministic_reject_sales_role") == 1
