"""Tests for the `disqualifier_integrator` family (PR-298).

Regression shape: the weekly qualifier passed an "Industrial Digitalization
Projects Coordinator" at a ~30-person industrial-automation INTEGRATOR and
cadenced them through DM3. The company SELLS to manufacturers; it is not one.
Its CRM categories (Machinery, Industrials & Manufacturing) and the plant-coded
title both read as in-ICP, and its company NAME is a bare brand with no service
token to catch — only the company DESCRIPTION states the business model.

The family is a CONJUNCTION — a service-provider description at a company the
industry classifier already labelled off-ICP — and abstains when either half is
missing. The tests pin both the firing case and every abstain, because a
keyword family that silently defaults half its predicate is a dead gate.

The conjunction is NOT keyed on company size despite size being the intuitive
discriminator: headcount/revenue are premium CRM enrichment attributes that a
base API entitlement reads as empty on every record, so such a gate could never
fire. See the note in quality_gate's integrator block.

Every identity below is synthetic.
"""

from __future__ import annotations

from clients.attio import first_text_value
from workflows.escalation_schemas import ESCALATION_SCHEMAS, ESCALATION_TYPES_SET
from workflows.quality_gate import (
    DISQUALIFIER_VERDICT_PATHS,
    OFF_ICP_INDUSTRIES,
    VERDICT_PATHS,
    _is_integrator_service_provider,
    _match_disqualifier,
    score_prospect,
)

# A CRM-enrichment blurb in the shape the family exists to catch: an integrator
# describing the automation solutions it sells INTO plants (truncated the way
# enrichment stores it).
INTEGRATOR_DESCRIPTION = (
    "Contoso Automação Industrial is a leading provider of industrial "
    "automation solutions in the region, specializing in Industry 4.0. They "
    "support digital transformation by developing apps, providing real-time "
    "information, and enhancing performance, co..."
).lower()

ENTERPRISE_PERSONA = {
    "key": "operations_leaders",
    "enterprise_mode": True,
    "search_size_credit": 15,
    "search_headcount_filter": "1001-5000",
}


# -- Registry invariants -------------------------------------------------


class TestRegistry:
    def test_slug_registered_in_both_registries(self):
        assert "disqualifier_integrator" in VERDICT_PATHS
        assert "disqualifier_integrator" in DISQUALIFIER_VERDICT_PATHS

    def test_disqualifier_subset_still_holds(self):
        assert DISQUALIFIER_VERDICT_PATHS.issubset(VERDICT_PATHS)

    def test_family_routes_to_the_operator_review_queue(self):
        """Membership in DISQUALIFIER_VERDICT_PATHS is what makes
        weekly_prospect open a typed `disqualifier_match` row — without it the
        rejection would be unauditable."""
        assert "disqualifier_match" in ESCALATION_TYPES_SET
        assert ESCALATION_SCHEMAS.get("disqualifier_match") is not None

    def test_industry_half_keys_on_the_off_icp_label(self):
        """The industry half reuses the scorer's own OFF_ICP_INDUSTRIES set —
        the classifier's verdict that the company is not the kind of business
        the operator sells to — so the two consumers of that label can never
        drift apart."""
        assert "Other" in OFF_ICP_INDUSTRIES


# -- The regression case -------------------------------------------------


class TestIntegratorRegression:
    def test_matcher_fires_on_the_integrator_record(self):
        match = _match_disqualifier(
            "industrial digitalization projects coordinator",
            "contoso automação",
            description_lower=INTEGRATOR_DESCRIPTION,
            industry="Other",
        )
        assert match is not None
        slug, keyword = match
        assert slug == "disqualifier_integrator"
        assert keyword == "automação industrial"

    def test_score_prospect_rejects_with_the_typed_verdict(self):
        result = score_prospect(
            {
                "name": "Bruno Teixeira",
                "title": "Industrial Digitalization Projects Coordinator",
                "company": "Contoso Automação",
                "location": "São Paulo, Brazil",
                "company_description": INTEGRATOR_DESCRIPTION,
                "industry": "Other",
            },
            ENTERPRISE_PERSONA,
        )
        assert result["pass"] is False
        assert result["verdict_path"] == "disqualifier_integrator"
        assert result["disqualifier_keyword"] == "automação industrial"

    def test_without_the_family_the_prospect_is_not_deterministically_rejected_here(self):
        """Pins WHY the family is needed: strip the two company fields and the
        same prospect no longer carries an integrator verdict — the
        title/company/category signals alone never say 'service provider'."""
        result = score_prospect(
            {
                "name": "Bruno Teixeira",
                "title": "Industrial Digitalization Projects Coordinator",
                "company": "Contoso Automação",
                "location": "São Paulo, Brazil",
            },
            ENTERPRISE_PERSONA,
        )
        assert result["verdict_path"] != "disqualifier_integrator"


# -- Conjunction: both halves required -----------------------------------


class TestConjunctionAbstains:
    def test_abstains_without_an_industry_label(self):
        """The company has never been classified. No industry half → no
        verdict, never a default."""
        assert _is_integrator_service_provider(INTEGRATOR_DESCRIPTION, None) is None
        assert _is_integrator_service_provider(INTEGRATOR_DESCRIPTION, "") is None

    def test_abstains_without_description(self):
        assert _is_integrator_service_provider("", "Other") is None

    def test_abstains_when_classified_as_a_manufacturer(self):
        """The measured guard: large automation-EQUIPMENT makers match the
        description keywords and classify as Manufacturing. They run real
        plants — the industry half is what excludes them, and a
        description-only predicate would hard-reject them."""
        assert _is_integrator_service_provider(
            "global leader in industrial automation and robotics.", "Manufacturing"
        ) is None

    def test_abstains_on_an_unrecognized_label(self):
        """Drift between classifier and scorer must widen the pool (abstain),
        never hard-reject on a label the gate does not understand."""
        assert _is_integrator_service_provider(
            INTEGRATOR_DESCRIPTION, "Professional Services"
        ) is None

    def test_abstains_on_an_unknown_classification(self):
        """`unknown` means the classifier abstained or its dispatch failed —
        no verdict to conjoin with. score_prospect maps any unrecognized status
        string to "unknown", so schema drift fails safe here."""
        assert _is_integrator_service_provider(
            INTEGRATOR_DESCRIPTION, "Other", "unknown"
        ) is None

    def test_accepts_low_confidence(self):
        """Deliberately NOT abstaining on low_confidence, unlike
        _industry_score's gate. The ingest-time classifier stamps
        low_confidence on everything it labels and only the manual
        industry-approve CLI writes confirmed — so a confirmed-only predicate
        would be silent on every company the pipeline classifies itself, which
        is exactly the newly-ingested population this family exists to catch.
        Safe here because the label is one half of a conjunction, not a lone
        signal moving a score."""
        assert _is_integrator_service_provider(
            INTEGRATOR_DESCRIPTION, "Other", "low_confidence"
        ) == "automação industrial"

    def test_matcher_abstains_when_company_fields_absent(self):
        """Every caller that has no company record — repair utilities, the
        audit scripts, tests — must keep pre-PR-298 behaviour."""
        assert _match_disqualifier("plant manager", "acme foods") is None


# -- Manufacturer carve-out ----------------------------------------------


class TestManufacturerCarveout:
    def test_small_manufacturer_using_solutions_language_survives(self):
        """A real manufacturer whose blurb says 'solutions provider' is
        rescued by the carve-out — it says it manufactures."""
        description = (
            "acme packaging is a manufacturer of flexible packaging and a "
            "solutions provider to the food industry."
        )
        assert _is_integrator_service_provider(description, "Other") is None

    def test_automation_hardware_maker_survives(self):
        """Builds automation gear IN A PLANT — in-ICP, not an integrator."""
        description = (
            "we design and manufacture industrial automation components at "
            "our factory in monterrey."
        )
        assert _is_integrator_service_provider(description, "Other") is None

    def test_spanish_self_assertion_carveout(self):
        description = (
            "somos fabricante de equipos con soluciones de automatización "
            "para la industria alimentaria."
        )
        assert _is_integrator_service_provider(description, "Other") is None

    def test_adjectival_self_description_carveout(self):
        """"is a manufacturer" does not survive an interposed adjective, so the
        adjectival forms carry this case."""
        description = (
            "acme is a leading manufacturer of steel tubing and a "
            "solutions provider to the construction industry."
        )
        assert _is_integrator_service_provider(description, "Other") is None

    def test_bare_genitive_is_not_a_carveout(self):
        """Naming the OEM you resell is standard integrator copy. A bare
        "manufacturer of" / "fabricante de" matches that vendor, not the
        company — so those forms must NOT rescue an integrator."""
        for description in (
            "systems integrator and authorized distributor for contoso, "
            "manufacturer of industrial automation equipment.",
            "integrador de sistemas y distribuidor del fabricante de "
            "equipos fabrikam.",
        ):
            assert _is_integrator_service_provider(description, "Other") is not None, (
                f"bare genitive wrongly rescued an integrator: {description!r}"
            )

    def test_own_production_inflections_are_carved_out(self):
        """Singular / first-person forms whose sibling was already listed."""
        for description in (
            "we run our plant in guadalajara and offer engineering "
            "services to industrial customers.",
            "operamos nuestras fábricas en la región y brindamos servicios "
            "de ingeniería.",
            "producimos alimentos congelados y damos servicios de "
            "ingeniería a la industria.",
        ):
            assert _is_integrator_service_provider(description, "Other") is None, (
                f"own-production claim was not carved out: {description!r}"
            )

    def test_portuguese_carveout(self):
        description = (
            "somos fabricante de embalagens e prestadora de serviços "
            "de engenharia para clientes industriais."
        )
        assert _is_integrator_service_provider(description, "Other") is None

    def test_serving_manufacturers_is_not_a_carveout(self):
        """The carve-out must key on "we manufacture", never on a bare
        `manufactur` stem: "serving discrete manufacturers" is what an
        integrator writes about ITS CUSTOMERS, and a stem carve-out silently
        un-catches the whole population this family exists for."""
        for description in (
            "a systems integrator serving discrete manufacturers.",
            "we help manufacturers digitize their plants.",
            "consulting for manufacturing companies across the region.",
        ):
            assert _is_integrator_service_provider(description, "Other") is not None, (
                f"carve-out wrongly rescued an integrator: {description!r}"
            )


# -- Language coverage ---------------------------------------------------


class TestLanguageCoverage:
    def test_english_systems_integrator(self):
        assert _is_integrator_service_provider(
            "a systems integrator serving discrete manufacturers.", "Other"
        ) == "systems integrator"

    def test_spanish_automatizacion_industrial(self):
        assert _is_integrator_service_provider(
            "empresa de automatización industrial con sede en querétaro.", "Other"
        ) == "automatización industrial"

    def test_portuguese_integrador_de_sistemas(self):
        assert _is_integrator_service_provider(
            "integrador de sistemas para a indústria de alimentos.", "Other"
        ) == "integrador de sistemas"

    def test_consulting_description(self):
        assert _is_integrator_service_provider(
            "operations consulting for industrial clients.", "Other"
        ) is not None


# -- Precision guards ----------------------------------------------------


class TestNoFalsePositives:
    def test_bare_possessive_client_words_do_not_fire(self):
        """Every B2B manufacturer writes "our clients". Those bare possessives
        are not business-model statements, and a hit here short-circuits AHEAD
        of the decision-maker LLM rescue — so it would hard-reject a real plant
        director with no second look. Client words count only when bound to a
        service verb ("helps its clients")."""
        for description in (
            "producimos alimentos congelados en tres plantas y entregamos "
            "a nuestros clientes en toda la región.",
            "we ship to our clients across north america.",
            "entregamos aos nossos clientes em todo o país.",
        ):
            assert _is_integrator_service_provider(description, "Other") is None, (
                f"bare possessive fired: {description!r}"
            )

    def test_decision_maker_still_reaches_the_llm_band(self):
        """End-to-end form of the above: the decision-maker rescue routes a
        decision-maker pushed under the deterministic-reject floor by the
        off-ICP penalty to the LLM. The integrator family must not steal that
        prospect by short-circuiting ahead of it."""
        result = score_prospect(
            {
                "name": "Ana Ruiz",
                "title": "Director de Operaciones",
                "company": "Alimentos del Valle",
                "location": "Monterrey, Mexico",
                "industry": "Other",
                "company_description": (
                    "producimos alimentos congelados en tres plantas y "
                    "entregamos a nuestros clientes en toda la región."
                ),
            },
            ENTERPRISE_PERSONA,
        )
        assert result["verdict_path"] != "disqualifier_integrator"

    def test_plain_manufacturer_description_does_not_fire(self):
        description = (
            "northwind dairy processes and distributes dairy products with "
            "20 production sites."
        )
        assert _is_integrator_service_provider(description, "Other") is None

    def test_short_token_does_not_substring_fire(self):
        """Word-boundary matching: 'integrator' must not fire inside a longer
        word."""
        assert _is_integrator_service_provider(
            "our disintegrators grind polymer pellets.", "Other"
        ) is None

    def test_ordering_more_specific_company_family_still_wins(self):
        """A named consultancy keeps `disqualifier_consulting`; the integrator
        family runs last and is the residual catch."""
        match = _match_disqualifier(
            "operations lead",
            "northwind consulting",
            description_lower="a global consulting firm and solutions provider.",
            industry="Other",
        )
        assert match is not None
        assert match[0] == "disqualifier_consulting"


# -- CRM text-attribute parsing helper -----------------------------------


class TestFirstTextValue:
    def test_parses_the_text_attribute_shape(self):
        assert first_text_value([{"value": "Acme makes widgets"}]) == "Acme makes widgets"

    def test_empty_and_malformed_are_empty_string(self):
        assert first_text_value(None) == ""
        assert first_text_value([]) == ""
        assert first_text_value([{}]) == ""


# -- Production input path (weekly_prospect._resolve_company_signals) -----
#
# The helper tests above all hand the description in directly. These cover the
# only path that supplies it in production, which every pre-existing test
# patches out via `_enrich_prospect_industry`.


class TestCompanySignalsResolution:
    @staticmethod
    def _company(**values) -> dict:
        return {"id": {"record_id": "c1"}, "values": values}

    def _resolve(self, record, **kwargs):
        from unittest.mock import patch

        from workflows import weekly_prospect

        with patch.object(weekly_prospect, "find_company_record", return_value=record):
            return weekly_prospect._resolve_company_signals(
                object(), "Contoso Automação", None, {}, dry_run=True, **kwargs
            )

    def test_reads_description_and_industry_off_the_record(self):
        signals = self._resolve(self._company(
            description=[{"value": "provider of industrial automation solutions"}],
            industry_vertical=[{"option": {"title": "Other"}}],
            industry_vertical_status=[{"option": {"title": "low_confidence"}}],
        ))
        assert signals.description == "provider of industrial automation solutions"
        assert signals.industry == "Other"
        assert signals.industry_status == "low_confidence"

    def test_clearbit_corrupted_record_yields_no_description(self):
        """A corrupted record carries LinkedIn's OWN enrichment payload under
        the real employer's name. Scoring the integrator family on it would
        hard-reject a manufacturer using LinkedIn's blurb — a confident wrong
        answer. The pipeline-written industry label stays trusted."""
        signals = self._resolve(self._company(
            name=[{"value": "Real Manufacturer SA"}],
            domains=[{"domain": "linkedin.com"}],
            description=[{"value": "linkedin is a professional network and "
                                   "solutions provider for recruiters"}],
            industry_vertical=[{"option": {"title": "Other"}}],
        ))
        assert signals.description == ""
        assert signals.industry == "Other"

    def test_missing_company_record_abstains_on_every_signal(self):
        signals = self._resolve(None)
        assert signals.description == ""
        assert signals.industry is None
        assert signals.industry_status is None

    def test_enrich_omits_the_key_entirely_when_there_is_no_description(self):
        """Absent key, never "" — score_prospect distinguishes "no signal" from
        "empty signal", and the precedent keys behave the same way."""
        from unittest.mock import patch

        from workflows import weekly_prospect

        prospect_data = {"company": "Contoso Automação"}
        with patch.object(
            weekly_prospect, "_resolve_company_signals",
            return_value=weekly_prospect.CompanySignals(),
        ):
            weekly_prospect._enrich_prospect_industry(
                object(), prospect_data, {}, {}, dry_run=True,
            )
        assert "company_description" not in prospect_data
        assert "industry" not in prospect_data

    def test_enrich_truncates_the_description(self):
        """prospect_data is serialized whole into the borderline export."""
        from unittest.mock import patch

        from workflows import weekly_prospect

        prospect_data = {"company": "Contoso Automação"}
        with patch.object(
            weekly_prospect, "_resolve_company_signals",
            return_value=weekly_prospect.CompanySignals(description="x" * 4000),
        ):
            weekly_prospect._enrich_prospect_industry(
                object(), prospect_data, {}, {}, dry_run=True,
            )
        assert len(prospect_data["company_description"]) == 500

    def test_end_to_end_description_reaches_the_verdict(self):
        """The full production chain: company record -> CompanySignals ->
        prospect_data -> score_prospect -> typed reject."""
        from unittest.mock import patch

        from workflows import weekly_prospect

        prospect_data = {"company": "Contoso Automação",
                         "title": "Gerente de Projetos",
                         "location": "São Paulo, Brazil"}
        record = self._company(
            description=[{"value": "Contoso Automação Industrial is a "
                                   "leading provider of industrial automation "
                                   "solutions in the region."}],
            industry_vertical=[{"option": {"title": "Other"}}],
            industry_vertical_status=[{"option": {"title": "low_confidence"}}],
        )
        with patch.object(weekly_prospect, "find_company_record", return_value=record):
            weekly_prospect._enrich_prospect_industry(
                object(), prospect_data, {}, {}, dry_run=True,
            )
        result = score_prospect(prospect_data, ENTERPRISE_PERSONA)
        assert result["verdict_path"] == "disqualifier_integrator"
