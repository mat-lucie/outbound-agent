"""Tests for workflows/quality_gate.py — rule-based scoring."""

from unittest.mock import MagicMock, patch

from workflows.quality_gate import classify_response, render_qualification_prompt, score_prospect


class TestScoreProspect:
    def test_high_score_large_company_decision_maker(self):
        # Size is a search-level credit now (2026-07-06 RCA) — a strong
        # decision-maker qualifies through a size-scoped persona config.
        result = score_prospect({
            "name": "Carlos Mendoza",
            "title": "VP of Operations",
            "company": "Grupo Bimbo",
            "location": "Mexico City, Mexico",
        }, persona_config={
            "key": "operations_leaders",
            "enterprise_mode": True,
            "search_size_credit": 15,
        })
        assert result["pass"] is True
        assert result["score"] >= 60
        assert result["persona"] == "executive_sponsors"
        assert result["language"] == "es"

    def test_low_score_small_company(self):
        result = score_prospect({
            "name": "John Smith",
            "title": "Junior Analyst",
            "company": "Small Startup",
            "location": "New York, USA",
            "employee_count": 50,
        })
        assert result["score"] < 60

    def test_competitor_rejected(self):
        result = score_prospect({
            "name": "Jane Doe",
            "title": "Sales Director at Siemens Opcenter",
            "company": "Siemens",
            "location": "Munich, Germany",
            "employee_count": 10000,
        })
        assert result["score"] < 70  # Penalized for competitor

    def test_d2c_director_rejected(self):
        # Regression: 2026-05-06 Luis Felipe Moreirao @ Whirlpool slipped past
        # the gate — "Director" alone scored +28 decision-maker points and
        # nothing penalized the D2C/sales-domain qualifier. Sales-side roles
        # don't own production scheduling decisions; they're never buyers.
        result = score_prospect({
            "name": "Luis Felipe",
            "title": "D2C Director",
            "company": "Whirlpool",
            "location": "São Paulo, Brazil",
            "employee_count": 70000,
        })
        assert result["pass"] is False
        assert result["verdict_path"] == "deterministic_reject_sales_role"

    def test_sales_director_rejected_even_at_large_manufacturer(self):
        result = score_prospect({
            "name": "Pedro Vargas",
            "title": "Director Comercial",
            "company": "Cementra",
            "location": "Monterrey, Mexico",
            "employee_count": 40000,
            "industry": "Manufacturing",
        })
        assert result["pass"] is False
        assert result["verdict_path"] == "deterministic_reject_sales_role"

    def test_marketing_vp_rejected(self):
        result = score_prospect({
            "name": "Ana Souza",
            "title": "VP Marketing LATAM",
            "company": "Nestlé",
            "location": "São Paulo, Brazil",
            "employee_count": 50000,
        })
        assert result["pass"] is False
        assert result["verdict_path"] == "deterministic_reject_sales_role"

    def test_sop_director_passes_despite_sales_keyword(self):
        # S&OP is an ops/supply-chain function, not a sales role. Title contains
        # "Sales" via the "Sales and Operations Planning" phrase — must NOT
        # trigger the sales-role disqualifier.
        result = score_prospect({
            "name": "Roberto Díaz",
            "title": "Director Sales and Operations Planning LATAM",
            "company": "Unilever",
            "location": "Mexico City, Mexico",
            "employee_count": 30000,
        })
        assert result["verdict_path"] != "deterministic_reject_sales_role"

    def test_salesforce_admin_not_sales_role(self):
        # Word-boundary regression: "Salesforce Administrator" must NOT trigger
        # the sales-role disqualifier — "salesforce" is a product name, not a
        # sales role. Before _match_any_word, substring matching on "sales"
        # captured "salesforce" and other unrelated terms.
        result = score_prospect({
            "name": "Alex Kim",
            "title": "Salesforce Administrator",
            "company": "Random Manufacturer",
            "location": "Monterrey, Mexico",
            "employee_count": 3000,
        })
        assert result["verdict_path"] != "deterministic_reject_sales_role"

    def test_wholesales_not_sales_role(self):
        # Edge case: "Wholesales" contains "sales" as substring but is not a
        # sales role — kept here to lock in the word-boundary contract.
        result = score_prospect({
            "name": "Maria Lopez",
            "title": "Wholesales Coordinator",
            "company": "Distribuidora Industrial",
            "location": "Mexico City, Mexico",
            "employee_count": 1000,
        })
        assert result["verdict_path"] != "deterministic_reject_sales_role"

    def test_salesian_university_not_sales_role(self):
        # Salesian = religious order, not sales. Academic title goes through
        # the academic-keyword path instead.
        result = score_prospect({
            "name": "Padre Antonio",
            "title": "Professor at Salesian University",
            "company": "Salesian University",
            "location": "Lima, Peru",
            "employee_count": 5000,
        })
        assert result["verdict_path"] != "deterministic_reject_sales_role"

    def test_production_coordinator_at_large_manufacturer_rejected(self):
        # The bug pattern: pre-PR-23, "Production Coordinator at Acme Foods Foods"
        # would have scored size +28 + role +24 (domain influencer) + competitor
        # +20 + industry +12 = 84 → deterministic_pass before the junior/IC guard
        # was added.  Post-PR-23, the ops_in_industrial joint case would give
        # +30 instead (size +28, ops_in_industrial +30, competitor +20 = 78), but
        # "coordinator" triggers is_junior_ic first so it routes to
        # deterministic_reject_junior_ic regardless.  The Haiku qualifier prompt
        # explicitly disqualifies coordinators as Junior/IC roles; this test locks
        # in that the deterministic path agrees.
        result = score_prospect({
            "name": "Maria González",
            "title": "Production Coordinator",
            "company": "Acme Foods",
            "location": "Monterrey, Mexico",
            "employee_count": 30000,
            "industry": "Food & Beverage",
        })
        assert result["pass"] is False
        assert result["verdict_path"] == "deterministic_reject_junior_ic"

    def test_coordinator_does_not_get_decision_maker_credit(self):
        # Audit-log hygiene: "coordinator" / "coordinador" / "coordenador" used
        # to substring-match "coo" inside DECISION_MAKER_KEYWORDS, so a
        # "Production Coordinator" briefly scored +28 as decision-maker before
        # is_junior_ic -40 corrected the verdict. The final verdict was right,
        # but the reasons list carried a phantom "Decision-maker title:" line.
        # DECISION_MAKER_EXEMPTIONS now suppresses the false credit so the
        # reasons list reflects only the signal that actually fires.
        for title in ("Production Coordinator", "Coordinador de Producción", "Coordenador de Produção"):
            result = score_prospect({
                "name": "Test",
                "title": title,
                "company": "Acme Foods",
                "location": "Monterrey, Mexico",
                "employee_count": 30000,
                "industry": "Food & Beverage",
            })
            joined = " ".join(result["reasons"]).lower()
            assert "decision-maker" not in joined, (
                f"{title!r} must not appear as a decision-maker. reasons={result['reasons']}"
            )
            # is_junior_ic must still be the verdict driver, not decision-maker.
            assert result["verdict_path"] == "deterministic_reject_junior_ic"
            # Role component is "Relevant domain" (+12), not decision-maker (+28).
            assert result["score_breakdown"]["role"] != 28

    def test_coo_acronym_still_matches_decision_maker(self):
        # Regression guard for the DECISION_MAKER_EXEMPTIONS fix above: the
        # 3-letter "coo" substring must still pick up legitimate COO titles.
        for title in ("COO", "Group COO", "COO, LATAM"):
            result = score_prospect({
                "name": "Test",
                "title": title,
                "company": "Acme Foods",
                "location": "Monterrey, Mexico",
                "employee_count": 30000,
                "industry": "Food & Beverage",
            })
            joined = " ".join(result["reasons"]).lower()
            assert "decision-maker" in joined, (
                f"{title!r} must still be tagged decision-maker. reasons={result['reasons']}"
            )
            assert result["score_breakdown"]["role"] == 28, (
                f"{title!r} role component must be +28 (decision-maker). "
                f"breakdown={result['score_breakdown']}"
            )

    def test_data_analyst_rejected(self):
        result = score_prospect({
            "name": "Carlos Ruiz",
            "title": "Data Analyst",
            "company": "Grupo Bimbo",
            "location": "Mexico City, Mexico",
            "employee_count": 30000,
        })
        assert result["pass"] is False
        assert result["verdict_path"] == "deterministic_reject_junior_ic"

    def test_senior_coordinator_not_auto_rejected(self):
        # Exemption: a "Senior Coordinator" or "Lead Coordinator" has
        # enough seniority signal to escape the junior-IC penalty and go
        # through the normal scoring path. Verdict can still reject for
        # other reasons (location, size, etc.), but NOT via the IC route.
        result = score_prospect({
            "name": "Roberto Díaz",
            "title": "Senior Production Coordinator",
            "company": "Cementra",
            "location": "Monterrey, Mexico",
            "employee_count": 40000,
        })
        assert result["verdict_path"] != "deterministic_reject_junior_ic"

    def test_engineer_iii_not_auto_rejected(self):
        # Only engineer i / engineer ii hit the junior-IC penalty. Engineer III
        # and Senior Engineer are allowed through the normal scoring path.
        result = score_prospect({
            "name": "Ana López",
            "title": "Engineer III, Manufacturing",
            "company": "Whirlpool",
            "location": "São Paulo, Brazil",
            "employee_count": 70000,
        })
        assert result["verdict_path"] != "deterministic_reject_junior_ic"

    def test_director_with_coordinator_word_not_rejected(self):
        # Exemption: "Director of Production Coordination" includes
        # "coordinat..." but the seniority signal (director) overrides.
        result = score_prospect({
            "name": "Pedro Vargas",
            "title": "Director of Production Coordination",
            "company": "Cementra",
            "location": "Monterrey, Mexico",
            "employee_count": 40000,
        })
        assert result["verdict_path"] != "deterministic_reject_junior_ic"

    def test_digitalization_persona(self):
        result = score_prospect({
            "name": "Ana Garcia",
            "title": "Digital Transformation Manager",
            "company": "Cementra",
            "location": "Monterrey, Mexico",
            "employee_count": 3000,
        })
        assert result["persona"] == "digitalization_champions"
        assert result["language"] == "es"

    def test_portuguese_language_detection(self):
        result = score_prospect({
            "name": "Pedro Silva",
            "title": "Director de Operações",
            "company": "Ambev",
            "location": "São Paulo, Brazil",
            "employee_count": 8000,
        })
        assert result["language"] == "pt"

    def test_english_default_language(self):
        result = score_prospect({
            "name": "Wei Zhang",
            "title": "Plant Manager",
            "company": "Foxconn",
            "location": "Shenzhen, China",
            "employee_count": 50000,
        })
        assert result["language"] == "en"

    def test_employee_count_input_ignored(self):
        # 2026-07-06 RCA: SN exports never carried a headcount column, so
        # per-row employee_count is gone from scoring entirely. A leftover
        # legacy key must not change the result.
        base = {
            "name": "Test",
            "title": "Director of Manufacturing",
            "company": "Big Corp",
            "location": "USA",
        }
        without_key = score_prospect(dict(base))
        with_key = score_prospect({**base, "employee_count": "5001-10000"})
        assert with_key["score"] == without_key["score"]
        assert with_key["score_breakdown"] == without_key["score_breakdown"]

    def test_score_clamped_to_100(self):
        result = score_prospect({
            "name": "Test",
            "title": "VP of Operations and Manufacturing",
            "company": "Huge Corp",
            "location": "Mexico",
            "employee_count": 50000,
        })
        assert result["score"] <= 100


class TestMidMarketPersonaScoring:
    """Tests for target_company_mode scoring (mid-market ICP)."""

    MIDMARKET_PERSONA = {
        "key": "mx_midmarket_manufacturing",
        "target_company_mode": True,
        "search_headcount_filter": "51-200 / 201-500",
        "search_size_credit": 15,
    }

    def test_decision_maker_passes_with_search_credit(self):
        """Decision-maker + the search's structural size credit passes.

        Per-row headcount bands are gone (2026-07-06 RCA): the SN saved
        search's headcount facet guarantees the size band, so the credit is
        lane-level, not per-prospect."""
        result = score_prospect({
            "name": "Carlos Madrazo",
            "title": "Director de Operaciones",
            "company": "Conservas San Miguel",
            "location": "Guanajuato, Mexico",
        }, persona_config=self.MIDMARKET_PERSONA)
        assert result["pass"] is True
        assert result["score"] >= 60
        assert "size scoped by sn search" in " ".join(result["reasons"]).lower()
        # Persona should be the triggering persona, NOT title-routed
        assert result["persona"] == "mx_midmarket_manufacturing"

    def test_no_persona_config_size_abstains(self):
        """Without persona_config there is no declared search scoping, so the
        size component abstains (0) instead of granting a fake default —
        the 2026-07-06 RCA killed the +10/+20/+22 unknown-size offsets."""
        result = score_prospect({
            "name": "Test",
            "title": "VP of Operations",
            "company": "Big Corp",
            "location": "Mexico City, Mexico",
        })
        assert result["score_breakdown"]["size"] == 0
        joined = " ".join(result["reasons"]).lower()
        assert "company size unknown (search not size-scoped)" in joined
        # 0 size + 28 decision-maker + 20 non-competitor = 48 → below the
        # 60 pass line: an unscoped source cannot pass on title alone.
        assert result["pass"] is False

    def test_mid_market_gm_passes(self):
        """Gerente General at a mid-market family manufacturer is a decision-maker."""
        result = score_prospect({
            "name": "Luis Ramírez",
            "title": "Gerente General",
            "company": "Envases del Bajío",
            "location": "León, Mexico",
        }, persona_config=self.MIDMARKET_PERSONA)
        assert result["pass"] is True
        assert "decision-maker" in " ".join(result["reasons"]).lower()
        assert "size scoped by sn search" in " ".join(result["reasons"]).lower()

    def test_mid_market_gerente_geral_pt_passes(self):
        """Portuguese 'Gerente Geral' at a Brazilian mid-market manufacturer."""
        result = score_prospect({
            "name": "Pedro Costa",
            "title": "Gerente Geral",
            "company": "Embalagens Paulista",
            "location": "Campinas, Brazil",
        }, persona_config=self.MIDMARKET_PERSONA)
        assert result["pass"] is True
        assert result["language"] == "pt"
        assert "decision-maker" in " ".join(result["reasons"]).lower()


class TestEnterprisePersonaScoring:
    """Tests for enterprise_mode scoring (Lane 2 — global multinationals in LATAM)."""

    ENTERPRISE_PERSONA = {
        "key": "operations_leaders",
        "enterprise_mode": True,
        "search_headcount_filter": "501-1,000 / 1,001-5,000 / 5,001-10,000 / 10,001+",
        "search_size_credit": 15,
    }

    def test_enterprise_gm_unknown_size_passes(self):
        """GM at a multinational should qualify via the search-scoped size
        credit — the most common shape of an enterprise export."""
        result = score_prospect({
            "name": "Jorge Vazquez",
            "title": "General Manager",
            "company": "L'Oréal Brasil",
            "location": "São Paulo, Brazil",
        }, persona_config=self.ENTERPRISE_PERSONA)
        assert result["pass"] is True
        joined = " ".join(result["reasons"]).lower()
        assert "size scoped by sn search" in joined
        assert "decision-maker" in joined
        # Enterprise rows are classified by title: "General Manager" → operations_leaders
        assert result["persona"] == "operations_leaders"

    def test_enterprise_country_head_passes(self):
        """Regional/LATAM head at an enterprise-scoped search should pass."""
        result = score_prospect({
            "name": "Ignacio Delgado",
            "title": "Head of Manufacturing LATAM",
            "company": "Honeywell",
            "location": "Mexico City, Mexico",
        }, persona_config=self.ENTERPRISE_PERSONA)
        assert result["pass"] is True
        assert result["score"] >= 60
        assert "size scoped by sn search" in " ".join(result["reasons"]).lower()

    def test_enterprise_auto_tier_1_passes(self):
        """Auto Tier 1 enterprise plant director is now in ICP (Lane 2 only)."""
        result = score_prospect({
            "name": "Alberto Sanchez",
            "title": "Plant Director",
            "company": "Martinrea International",
            "location": "Saltillo, Mexico",
            "employee_count": 3000,
        }, persona_config=self.ENTERPRISE_PERSONA)
        assert result["pass"] is True

    def test_global_head_rejected(self):
        """Global-scope executives don't take cold outreach — reject outright."""
        result = score_prospect({
            "name": "Test",
            "title": "Global Head of Operations",
            "company": "Procter & Gamble",
            "location": "Cincinnati, USA",
            "employee_count": 100000,
        }, persona_config=self.ENTERPRISE_PERSONA)
        assert result["pass"] is False
        joined = " ".join(result["reasons"]).lower()
        assert "global-scope executive" in joined
        assert "out of latam scope" in joined
        assert "decision-maker title" not in joined

    def test_global_with_region_override_not_demoted(self):
        """'Global Operations Mexico' is actually a regional role despite 'global'."""
        result = score_prospect({
            "name": "Test",
            "title": "VP Global Operations Mexico",
            "company": "Stark Industrial",
            "location": "São Paulo, Brazil",
            "employee_count": 80000,
        }, persona_config=self.ENTERPRISE_PERSONA)
        assert result["pass"] is True
        joined = " ".join(result["reasons"]).lower()
        assert "decision-maker" in joined

    def test_global_with_mexico_override_not_demoted(self):
        """Regional head whose title references Mexico is NOT demoted."""
        result = score_prospect({
            "name": "Test",
            "title": "Global Director - Mexico",
            "company": "Nestlé",
            "location": "Mexico City, Mexico",
            "employee_count": 300000,
        }, persona_config=self.ENTERPRISE_PERSONA)
        assert result["pass"] is True
        joined = " ".join(result["reasons"]).lower()
        assert "decision-maker" in joined
        assert "out of latam scope" not in joined

    def test_enterprise_academic_still_rejected(self):
        """Professors and academics don't buy scheduling software."""
        result = score_prospect({
            "name": "Eduardo Gomes",
            "title": "Strategic Management MBA Professor",
            "company": "Universidade Presbiteriana Mackenzie",
            "location": "São Paulo, Brazil",
            "employee_count": 5000,
        }, persona_config=self.ENTERPRISE_PERSONA)
        assert result["pass"] is False

    def test_enterprise_ypo_member_still_rejected(self):
        """'YPO Member' is not an operational role — stays rejected."""
        result = score_prospect({
            "name": "Test",
            "title": "YPO Member",
            "company": "YPO",
            "location": "Mexico City, Mexico",
            "employee_count": "",
        }, persona_config=self.ENTERPRISE_PERSONA)
        assert result["pass"] is False

    def test_mutually_exclusive_flags_raise(self):
        """A persona_config with both flags set is a configuration bug and must
        raise loudly instead of silently routing through the wrong lane."""
        import pytest
        bad_persona = {
            "key": "broken",
            "target_company_mode": True,
            "enterprise_mode": True,
        }
        with pytest.raises(ValueError, match="mutually exclusive"):
            score_prospect({
                "name": "Test",
                "title": "General Manager",
                "company": "Any",
                "location": "Mexico",
                "employee_count": 5000,
            }, persona_config=bad_persona)


class TestEnterprisePersonaReclassification:
    """Enterprise-mode rows must be classified by TITLE, not stamped with the
    search persona. A Sales Nav search (e.g. digitalization_champions) returns
    mixed titles — CEOs, GMs, plant directors — so binding the search persona
    over-assigns it and corrupts downstream segmentation."""

    ENTERPRISE_DIGI = {"key": "digitalization_champions", "enterprise_mode": True}

    def test_enterprise_ceo_not_mislabeled_digitalization(self):
        """A CEO harvested from the digitalization_champions search must NOT
        inherit that persona — it has no digital signal in the title."""
        result = score_prospect({
            "name": "Maria Lopez",
            "title": "Chief Executive Officer",
            "company": "Grupo Bimbo",
            "location": "Mexico City, Mexico",
            "employee_count": 5000,
        }, persona_config=self.ENTERPRISE_DIGI)
        assert result["persona"] != "digitalization_champions"
        assert result["persona"] in ("operations_leaders", "executive_sponsors")

    def test_enterprise_ops_exec_routes_to_executive_sponsor(self):
        """A VP Operations pulled from the digitalization search re-routes to
        executive_sponsors by title, not the search persona."""
        result = score_prospect({
            "name": "Luis Reyes",
            "title": "VP Operations",
            "company": "Nestle Mexico",
            "location": "Mexico City, Mexico",
            "employee_count": 5000,
        }, persona_config=self.ENTERPRISE_DIGI)
        assert result["persona"] == "executive_sponsors"

    def test_enterprise_genuine_innovation_stays_digitalization(self):
        """A real digital-transformation title is still correctly classified
        as digitalization_champions — guard against over-correction."""
        result = score_prospect({
            "name": "Ana Garcia",
            "title": "Head of Digital Transformation",
            "company": "Cementra",
            "location": "Monterrey, Mexico",
            "employee_count": 5000,
        }, persona_config=self.ENTERPRISE_DIGI)
        assert result["persona"] == "digitalization_champions"

    def test_midmarket_persona_preserved_not_reclassified(self):
        """target_company_mode rows MUST keep the curated midmarket persona —
        _classify_persona only emits enterprise personas, so re-routing a
        midmarket prospect by title would mislabel it (and break the
        cross-search upgrade path)."""
        result = score_prospect({
            "name": "Carlos Madrazo",
            "title": "Chief Executive Officer",
            "company": "Conservas San Miguel",
            "location": "Guanajuato, Mexico",
            "employee_count": 350,
        }, persona_config={"key": "mx_midmarket_manufacturing", "target_company_mode": True})
        assert result["persona"] == "mx_midmarket_manufacturing"


class TestClassifyResponse:
    def test_positive_response(self):
        result = classify_response("Sounds interesting, let's schedule a demo next week")
        assert result["classification"] == "positive"

    def test_negative_response(self):
        result = classify_response("No thanks, not interested at this time")
        assert result["classification"] == "negative"

    def test_question_response(self):
        result = classify_response("How much does it cost?")
        assert result["classification"] == "question"

    def test_neutral_response(self):
        result = classify_response("Thanks for the message")
        assert result["classification"] == "neutral"

    def test_spanish_positive(self):
        result = classify_response("Me interesa, platiquemos la próxima semana")
        assert result["classification"] == "positive"

    def test_portuguese_negative(self):
        result = classify_response("Não tenho interesse, obrigado")
        assert result["classification"] == "negative"


class TestHybridHaikuGate:
    """Tests for the hybrid Haiku gate in score_prospect().

    Contract:
    - score < 40 → auto-reject, LLM never called
    - score > 75 → auto-pass, LLM never called
    - 40 ≤ score ≤ 75 with anthropic_client → LLM decides
    - 40 ≤ score ≤ 75 without anthropic_client → deterministic fallback (>= 60)
    - LLM exception → falls back to deterministic pass verdict
    """

    ENTERPRISE_PERSONA = {
        "key": "operations_leaders",
        "enterprise_mode": True,
        "search_size_credit": 15,
    }

    # ── helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _mock_client_returning(json_body: str) -> MagicMock:
        """Build a fake anthropic client whose messages.create returns the given JSON."""
        client = MagicMock()
        fake_block = MagicMock()
        fake_block.type = "text"
        fake_block.text = json_body
        fake_response = MagicMock()
        fake_response.content = [fake_block]
        client.messages.create.return_value = fake_response
        return client

    # ── gate behavior tests ─────────────────────────────────────────

    def test_low_score_never_calls_llm(self):
        """score < 40 must short-circuit to pass=False without invoking the LLM."""
        client = MagicMock()
        result = score_prospect(
            {
                "name": "x",
                "title": "Junior Analyst",
                "company": "Small Shop",
                "location": "Unknown",
                "employee_count": 10,
            },
            anthropic_client=client,
        )
        assert result["score"] < 40
        assert result["pass"] is False
        client.messages.create.assert_not_called()

    def test_high_score_never_calls_llm(self):
        """score > 75 must short-circuit to pass=True without invoking the LLM.

        Shipped configs sit at search_size_credit=15 (auto-pass deliberately
        unreachable — 2026-07-06 calibration), so this contract test injects a
        credit above the line: 30 + 28 (decision-maker) + 20 = 78 > 75."""
        client = MagicMock()
        result = score_prospect(
            {
                "name": "x",
                "title": "Director de Operaciones",
                "company": "BigCo",
                "location": "Mexico City, Mexico",
            },
            persona_config={
                "key": "operations_leaders",
                "enterprise_mode": True,
                "search_size_credit": 30,
            },
            anthropic_client=client,
        )
        assert result["score"] > 75
        assert result["pass"] is True
        client.messages.create.assert_not_called()

    def test_borderline_no_client_uses_deterministic(self):
        """Borderline + no anthropic_client → pass is score >= 60."""
        result = score_prospect(
            {
                "name": "x",
                "title": "Plant Manager",
                "company": "Subsidiary",
                "location": "Mexico City, Mexico",
                "employee_count": 200,
            },
            persona_config=self.ENTERPRISE_PERSONA,
        )
        # This case scores 59 under the new bands — borderline, should fall back to
        # deterministic threshold of 60 and therefore fail.
        assert 40 <= result["score"] <= 75
        assert result["pass"] is (result["score"] >= 60)
        assert "icp_lane" not in result
        assert "llm_rationale" not in result

    def test_borderline_llm_overrides_to_pass(self):
        """LLM returns pass=true → result.pass should be True even if score < 60."""
        client = self._mock_client_returning(
            '{"pass": true, "icp_lane": 2, "rationale": "LATAM plant director at global parent"}'
        )
        result = score_prospect(
            {
                "name": "x",
                "title": "Plant Manager",
                "company": "Honeywell Subsidiary",
                "location": "Mexico City, Mexico",
                "employee_count": 200,
            },
            persona_config=self.ENTERPRISE_PERSONA,
            anthropic_client=client,
        )
        assert 40 <= result["score"] <= 75
        assert result["pass"] is True
        assert result["icp_lane"] == 2
        assert "LATAM plant director" in result["llm_rationale"]
        client.messages.create.assert_called_once()

    def test_borderline_llm_overrides_to_fail(self):
        """LLM returns pass=false → result.pass should be False even if score >= 60."""
        client = self._mock_client_returning(
            '{"pass": false, "icp_lane": 1, "rationale": "Consultant disguised as operator"}'
        )
        # Build a borderline case that would deterministically pass (~60-75)
        result = score_prospect(
            {
                "name": "x",
                "title": "Director de Manufactura",
                # NOTE: was "Consulting LLC" (consultant +8 pulled the score
                # into the borderline band). Since disqualifier_consulting a
                # consulting company name hard-rejects before the LLM gate —
                # use a neutral company; size (+22) + role (+28) + non-
                # competitor (+20) = 70 still lands 40-75.
                "company": "Grupo Borde",
                "location": "Mexico City, Mexico",
                "employee_count": 1500,
            },
            persona_config=self.ENTERPRISE_PERSONA,
            anthropic_client=client,
        )
        assert 40 <= result["score"] <= 75
        assert result["pass"] is False
        assert result["icp_lane"] == 1
        client.messages.create.assert_called_once()

    def test_llm_exception_falls_back_to_deterministic(self):
        """If the anthropic call raises, the gate must not propagate — it should
        return a usable dict (pass=False from the LLM fallback path)."""
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("network exploded")
        result = score_prospect(
            {
                "name": "x",
                "title": "Plant Manager",
                "company": "Subsidiary",
                "location": "Mexico City, Mexico",
                "employee_count": 200,
            },
            persona_config=self.ENTERPRISE_PERSONA,
            anthropic_client=client,
        )
        # Fallback path: LLM returned pass=False + a rationale tagged with the error
        assert result["pass"] is False
        assert result["llm_rationale"].startswith("LLM qualifier error")
        client.messages.create.assert_called_once()

    def test_llm_malformed_json_falls_back(self):
        """Malformed LLM output must not crash — same safe fallback shape."""
        client = self._mock_client_returning("not a json blob at all")
        result = score_prospect(
            {
                "name": "x",
                "title": "Plant Manager",
                "company": "Subsidiary",
                "location": "Mexico City, Mexico",
                "employee_count": 200,
            },
            persona_config=self.ENTERPRISE_PERSONA,
            anthropic_client=client,
        )
        assert result["pass"] is False
        assert "LLM qualifier error" in result["llm_rationale"]

    def test_llm_failure_sets_borderline_llm_error_verdict_path(self):
        """LLM failure must be distinguishable from real reject in Attio.

        verdict_path="borderline_llm_error" lets operators retry these
        prospects later instead of treating them as rejected. Conflating
        the two would silently lose every borderline whose Haiku call hit
        a rate limit.
        """
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("rate limited")
        result = score_prospect(
            {
                "name": "x",
                "title": "Plant Manager",
                "company": "Subsidiary",
                "location": "Mexico City, Mexico",
                "employee_count": 200,
            },
            persona_config=self.ENTERPRISE_PERSONA,
            anthropic_client=client,
        )
        assert result["verdict_path"] == "borderline_llm_error"
        # PR-222 Rec D: a non-staging caller (explicit anthropic_client, no
        # agent_gate) now fails CLOSED — the borderline is rejected, never
        # committed unvetted at score>=60.
        assert result["pass"] is False

    def test_llm_real_reject_sets_borderline_reject_verdict_path(self):
        """When Haiku actually returns pass=false, verdict_path is borderline_reject
        (not borderline_llm_error). Confirms the LLM-failure sentinel doesn't leak."""
        client = self._mock_client_returning(
            '{"pass": false, "icp_lane": 2, "rationale": "out of scope"}'
        )
        result = score_prospect(
            {
                "name": "x",
                "title": "Plant Manager",
                "company": "Subsidiary",
                "location": "Mexico City, Mexico",
                "employee_count": 200,
            },
            persona_config=self.ENTERPRISE_PERSONA,
            anthropic_client=client,
        )
        assert result["verdict_path"] == "borderline_reject"
        assert result["pass"] is False


class TestLedgerUnavailableRouting:
    """PR-216 incident follow-up: a budget-ledger INFRA failure (Attio
    error while reserving) is not a prospect-quality signal and must not
    silently reject borderlines. When the caller supports the agent
    staging path (agent_gate=True — the weekly flow), fail OPEN to
    staging: the prospect lands in weekly_borderline_<date>.jsonl and the
    operator qualifies it via the existing weekly-finalize loop. No LLM
    dispatch happens without a reservation, so §3.7 is not bypassed.

    Cap exhaustion (CostCeilingExhausted) stays fail-closed — unchanged.
    Generic dispatch failures (timeout, subagent error) keep the
    borderline_llm_error contract — unchanged.
    """

    ENTERPRISE_PERSONA = {
        "key": "operations_leaders",
        "enterprise_mode": True,
    }
    # Plant Manager @ 200-emp subsidiary → borderline band (40-75), score < 60.
    BORDERLINE_PROSPECT = {
        "name": "x",
        "title": "Plant Manager",
        "company": "Subsidiary",
        "location": "Mexico City, Mexico",
        "employee_count": 200,
    }

    def _ledger_unavailable(self):
        from workflows.llm_dispatch import LLMBudgetLedgerUnavailable
        return LLMBudgetLedgerUnavailable(
            "quality_gate_haiku", RuntimeError("Attio 400 on ledger create"),
        )

    def test_agent_gate_stages_on_ledger_unavailable(self, monkeypatch):
        monkeypatch.setenv("OUTBOUND_USE_LLM_DISPATCH", "1")
        with patch(
            "workflows.llm_dispatch.request_llm_dispatch",
            side_effect=self._ledger_unavailable(),
        ):
            result = score_prospect(
                dict(self.BORDERLINE_PROSPECT),
                persona_config=self.ENTERPRISE_PERSONA,
                agent_gate=True,
            )
        assert result["needs_agent_qualification"] is True
        assert result["pass"] is None
        assert result["ledger_unavailable"] is True
        qp = result["qualification_prompt"]
        assert qp["system"] and qp["user"]
        # verdict_path stays None until the agent fills it in (staging contract).
        assert result.get("verdict_path") is None

    def test_no_agent_gate_llm_error_fails_closed(self, monkeypatch):
        """PR-222 Rec D: callers without a staging path now fail CLOSED — a
        non-agent_gate LLM/ledger error rejects the borderline (never an
        unvetted score>=60 commit) while keeping the retryable
        borderline_llm_error verdict for operator visibility."""
        monkeypatch.setenv("OUTBOUND_USE_LLM_DISPATCH", "1")
        with patch(
            "workflows.llm_dispatch.request_llm_dispatch",
            side_effect=self._ledger_unavailable(),
        ):
            result = score_prospect(
                dict(self.BORDERLINE_PROSPECT),
                persona_config=self.ENTERPRISE_PERSONA,
                agent_gate=False,
            )
        assert result["verdict_path"] == "borderline_llm_error"
        assert result["pass"] is False

    def test_generic_dispatch_failure_stages_under_agent_gate(self, monkeypatch):
        """PR-222 Rec D: a transient (non-ledger) dispatch failure is an infra
        blip, not a prospect-quality signal, so with a staging-capable caller it
        now fails OPEN to staging too — carrying the distinct `llm_error_staged`
        flag (vs the ledger case's `ledger_unavailable`). Pre-222 this kept
        borderline_llm_error at score>=60; that unvetted-commit path is gone."""
        from workflows.llm_dispatch import LLMDispatchFailed
        monkeypatch.setenv("OUTBOUND_USE_LLM_DISPATCH", "1")
        with patch(
            "workflows.llm_dispatch.request_llm_dispatch",
            side_effect=LLMDispatchFailed("quality_gate_haiku", "abc123", "subagent died"),
        ):
            result = score_prospect(
                dict(self.BORDERLINE_PROSPECT),
                persona_config=self.ENTERPRISE_PERSONA,
                agent_gate=True,
            )
        assert result["needs_agent_qualification"] is True
        assert result["pass"] is None
        assert result["llm_error_staged"] is True
        assert result.get("ledger_unavailable") is None
        assert result.get("verdict_path") is None

    def test_cost_exhausted_stages_under_agent_gate(self, monkeypatch):
        """PR-222 Rec D: a cost-ceiling breach is an INFRA signal, not a
        prospect-quality one — with a staging-capable caller it now fails OPEN
        to staging (mirroring the ledger treatment) instead of committing an
        unvetted borderline at score>=60. Distinct `cost_exhausted_staged`
        flag so the bucket stays greppable apart from the ledger case."""
        from decimal import Decimal as _D

        from workflows.llm_dispatch import CostCeilingExhausted as _CCE
        monkeypatch.setenv("OUTBOUND_USE_LLM_DISPATCH", "1")
        with patch(
            "workflows.llm_dispatch.request_llm_dispatch",
            side_effect=_CCE("quality_gate_haiku", _D("5.00"), _D("5.00")),
        ):
            result = score_prospect(
                dict(self.BORDERLINE_PROSPECT),
                persona_config=self.ENTERPRISE_PERSONA,
                agent_gate=True,
            )
        assert result["needs_agent_qualification"] is True
        assert result["pass"] is None
        assert result["cost_exhausted_staged"] is True
        # verdict_path stays None until the agent fills it in (staging contract).
        assert result.get("verdict_path") is None

    def test_cost_exhausted_no_agent_gate_fails_closed(self, monkeypatch):
        """Without a staging caller a cost-ceiling breach fails CLOSED to a
        reject (never an unvetted commit), keeping its own verdict path."""
        from decimal import Decimal as _D

        from workflows.llm_dispatch import CostCeilingExhausted as _CCE
        monkeypatch.setenv("OUTBOUND_USE_LLM_DISPATCH", "1")
        with patch(
            "workflows.llm_dispatch.request_llm_dispatch",
            side_effect=_CCE("quality_gate_haiku", _D("5.00"), _D("5.00")),
        ):
            result = score_prospect(
                dict(self.BORDERLINE_PROSPECT),
                persona_config=self.ENTERPRISE_PERSONA,
                agent_gate=False,
            )
        assert result.get("needs_agent_qualification") is None
        assert result["pass"] is False
        assert result["verdict_path"] == "borderline_cost_exhausted"


class TestEnterpriseBandsCorrection:
    """Historical class: the per-row enterprise headcount bands (and the ERP
    company_headline bonus) were removed 2026-07-06 — SN exports never carried
    the columns they read (see TestSearchScopedSizeCredit). Only the LATAM-
    override coverage remains."""

    ENTERPRISE_PERSONA = {
        "key": "operations_leaders",
        "enterprise_mode": True,
        "search_size_credit": 15,
    }

    def test_latam_override_includes_chile_peru_colombia(self):
        """'Global Operations Chile' is a regional role, not out-of-scope."""
        for country in ("Chile", "Peru", "Colombia"):
            result = score_prospect(
                {
                    "name": "x",
                    "title": f"Global Operations {country}",
                    "company": "Multinational Corp",
                    "location": "Santiago, Chile",
                },
                persona_config=self.ENTERPRISE_PERSONA,
            )
            joined = " ".join(result["reasons"]).lower()
            assert "out of latam scope" not in joined, f"{country} should trigger LATAM override"


class TestAgentGate:
    """Tests for the agent_gate=True sentinel path in score_prospect().

    Contract:
    - borderline + agent_gate=True + no client → sentinel (pass=None, needs_agent_qualification=True)
    - borderline + agent_gate=False + no client → deterministic fallback (unchanged)
    - borderline + anthropic_client passed + agent_gate=True → LLM path takes priority (client wins)
    - render_qualification_prompt returns correct shape for all persona modes
    """

    ENTERPRISE_PERSONA = {
        "key": "operations_leaders",
        "enterprise_mode": True,
        "search_size_credit": 15,
    }
    MIDMARKET_PERSONA = {
        "key": "mx_midmarket_manufacturing",
        "target_company_mode": True,
        "search_size_credit": 15,
    }

    # Borderline prospect (enterprise mode, no industry set):
    # search size credit +15, plant manager +24 (individual path, no industry),
    # not competitor +20 = 59 (borderline). No industry → the joint
    # ops_in_industrial case does NOT fire; individual role +24 applies.
    BORDERLINE_PROSPECT = {
        "name": "Test User",
        "title": "Plant Manager",
        "company": "Subsidiary",
        "location": "Mexico City, Mexico",
    }

    def test_borderline_agent_gate_true_no_client_returns_sentinel(self):
        """Borderline + agent_gate=True + no client → sentinel result."""
        result = score_prospect(
            self.BORDERLINE_PROSPECT,
            persona_config=self.ENTERPRISE_PERSONA,
            agent_gate=True,
        )
        assert 40 <= result["score"] <= 75
        assert result["pass"] is None
        assert result["needs_agent_qualification"] is True
        assert "qualification_prompt" in result
        prompt = result["qualification_prompt"]
        assert "system" in prompt
        assert "user" in prompt
        assert len(prompt["system"]) > 0
        assert len(prompt["user"]) > 0

    def test_borderline_agent_gate_false_no_client_uses_deterministic(self):
        """Borderline + agent_gate=False + no client → deterministic fallback (unchanged)."""
        result = score_prospect(
            self.BORDERLINE_PROSPECT,
            persona_config=self.ENTERPRISE_PERSONA,
            agent_gate=False,
        )
        assert 40 <= result["score"] <= 75
        assert result["pass"] is (result["score"] >= 60)
        assert "needs_agent_qualification" not in result
        assert "qualification_prompt" not in result

    def test_borderline_anthropic_client_wins_over_agent_gate(self):
        """When anthropic_client is passed, LLM path takes priority regardless of agent_gate."""
        client = MagicMock()
        fake_block = MagicMock()
        fake_block.type = "text"
        fake_block.text = '{"pass": true, "icp_lane": 1, "rationale": "enterprise LATAM plant director"}'
        fake_response = MagicMock()
        fake_response.content = [fake_block]
        client.messages.create.return_value = fake_response

        result = score_prospect(
            self.BORDERLINE_PROSPECT,
            persona_config=self.ENTERPRISE_PERSONA,
            anthropic_client=client,
            agent_gate=True,
        )
        assert 40 <= result["score"] <= 75
        # LLM path: pass=True (from mock), not sentinel
        assert result["pass"] is True
        assert "needs_agent_qualification" not in result
        client.messages.create.assert_called_once()

    def test_render_qualification_prompt_enterprise_mode(self):
        """render_qualification_prompt with enterprise_mode returns 'enterprise' persona mode."""
        prompt = render_qualification_prompt(
            {"name": "Jorge", "title": "GM", "company": "LATAM Co", "location": "Mexico", "employee_count": 500},
            {"enterprise_mode": True},
        )
        assert "system" in prompt
        assert "user" in prompt
        assert len(prompt["system"]) > 0
        assert "Persona mode: enterprise" in prompt["user"]

    def test_render_qualification_prompt_midmarket_mode(self):
        """render_qualification_prompt with target_company_mode returns 'midmarket' persona mode."""
        prompt = render_qualification_prompt(
            {"name": "Ana", "title": "Director", "company": "Fab SA", "location": "Chile", "employee_count": 300},
            {"target_company_mode": True},
        )
        assert "Persona mode: midmarket" in prompt["user"]

    def test_render_qualification_prompt_legacy_mode(self):
        """render_qualification_prompt with no config returns 'legacy' persona mode."""
        prompt = render_qualification_prompt(
            {"name": "Bob", "title": "COO", "company": "Corp", "location": "Colombia", "employee_count": 1000},
            None,
        )
        assert "Persona mode: legacy" in prompt["user"]

    def test_render_qualification_prompt_includes_all_fields(self):
        """render_qualification_prompt user content includes all prospect fields.

        Employee count left the prompt 2026-07-06: the weekly path never sets
        it (SN exports carry no headcount column), so the line rendered a
        permanent 'unknown' — dead prompt weight."""
        prospect = {
            "name": "Carlos Mendez",
            "title": "VP Operations",
            "company": "Grupo Industrial",
            "location": "Monterrey, Mexico",
        }
        prompt = render_qualification_prompt(prospect, None)
        user = prompt["user"]
        assert "Carlos Mendez" in user
        assert "VP Operations" in user
        assert "Grupo Industrial" in user
        assert "Monterrey, Mexico" in user
        assert "Employee count" not in user


class TestIndustryAwareScoring:
    """Industry contributes to score: in-ICP +12, off-ICP -25, unknown 0."""

    MIDMARKET_PERSONA = {
        "key": "mx_midmarket_manufacturing",
        "target_company_mode": True,
        "search_size_credit": 15,
    }

    def _base_prospect(self, **overrides) -> dict:
        base = {
            "name": "Carlos Madrazo",
            "title": "Director de Operaciones",
            "company": "Conservas San Miguel",
            "location": "Guanajuato, Mexico",
        }
        base.update(overrides)
        return base

    def test_in_icp_industry_boosts_score(self):
        """In-ICP industry adds +12, lifting the DM baseline 63 → 75.

        75 is deliberately NOT > 75: at the shipped search_size_credit=15 the
        deterministic auto-pass stays off (2026-07-06 calibration), so the best
        hand still routes to the LLM band."""
        baseline = score_prospect(self._base_prospect(), persona_config=self.MIDMARKET_PERSONA)
        boosted = score_prospect(
            self._base_prospect(industry="Food & Beverage"),
            persona_config=self.MIDMARKET_PERSONA,
        )
        assert boosted["score"] == baseline["score"] + 12
        assert boosted["score"] == 75
        assert boosted["verdict_path"] == "borderline_pass"
        assert any("In-ICP" in r for r in boosted["reasons"])

    def test_off_icp_industry_drops_below_dm_threshold(self):
        """Off-ICP ('Other') applies -25 penalty, dropping pass-eligible below 60."""
        baseline = score_prospect(self._base_prospect(), persona_config=self.MIDMARKET_PERSONA)
        penalized = score_prospect(
            self._base_prospect(industry="Other"),
            persona_config=self.MIDMARKET_PERSONA,
        )
        assert penalized["score"] == baseline["score"] - 25
        assert penalized["score"] < 60
        # 63 - 25 = 38: under 40, but the prospect is a DECISION-MAKER whose
        # drop below the line is entirely the industry penalty — the
        # adversarial-QA rescue routes that cell to the LLM band instead of
        # a silent deterministic reject (no client here → borderline_reject).
        assert penalized["verdict_path"] == "borderline_reject"

    def test_unknown_industry_is_neutral(self):
        """Missing/None industry leaves the score unchanged from pre-industry behavior."""
        baseline = score_prospect(self._base_prospect(), persona_config=self.MIDMARKET_PERSONA)
        explicit_none = score_prospect(
            self._base_prospect(industry=None),
            persona_config=self.MIDMARKET_PERSONA,
        )
        empty_string = score_prospect(
            self._base_prospect(industry=""),
            persona_config=self.MIDMARKET_PERSONA,
        )
        assert explicit_none["score"] == baseline["score"]
        assert empty_string["score"] == baseline["score"]
        assert explicit_none["score_breakdown"]["industry"] == 0

    def test_off_icp_does_not_underflow_below_zero(self):
        """A weak prospect + off-ICP penalty should clamp at 0, not go negative."""
        # Use an "operator" title — low role-fit but not a Junior/IC keyword,
        # so the verdict routes through the plain deterministic_reject band
        # rather than the new junior-IC short-circuit. The test's intent is
        # the clamping behavior, not the path attribution.
        weak = self._base_prospect(
            title="Machine Operator",     # low role-fit, no IC keyword
            industry="Other",             # -25
        )
        result = score_prospect(weak, persona_config=self.MIDMARKET_PERSONA)
        assert result["score"] >= 0
        assert result["pass"] is False
        # Score should land in deterministic_reject band (<40)
        assert result["score"] < 40
        assert result["verdict_path"] == "deterministic_reject"

    def test_score_breakdown_is_persisted_in_result(self):
        """The result dict carries score_breakdown + scoring_lane for Attio persistence."""
        result = score_prospect(
            self._base_prospect(industry="Pharma"),
            persona_config=self.MIDMARKET_PERSONA,
        )
        breakdown = result["score_breakdown"]
        assert breakdown["industry"] == 12
        assert breakdown["size"] == 15      # search_size_credit from persona config
        assert breakdown["role"] == 28      # Director = decision-maker
        assert breakdown["competitor"] == 20  # not a competitor
        assert breakdown["total"] == result["score"]
        assert "reasons" in breakdown
        assert result["scoring_lane"] == "target_company_mode"


class TestRoleIndustryOrthogonality:
    """PR-23 — B-MW-ORTHO: ops-influencer + in-ICP industry joint case.

    The core change: when BOTH (a) the title is an ops-domain influencer and
    (b) the company's industry is in IN_ICP_INDUSTRIES, the additive
    +24 (role) + +12 (industry) = +36 is replaced by a single combined
    OPS_IN_INDUSTRIAL_COMBINED = +30.  All non-joint paths are unchanged.

    D1 (NARROW interpretation): joint case fires only for is_ops_domain
    influencers.  Digi-domain influencers at ICP companies keep individual
    +24 + +12 = +36 because digital-transformation roles are conceptually
    orthogonal to industry vertical.

    D2 (component_scores): role=0, industry=0, ops_in_industrial=+30 in joint
    cases.  Each key answers "what did THAT signal contribute individually."

    D3 (reasons): joint case emits a single combined reason string instead of
    two individual reason strings.
    """

    ENTERPRISE_PERSONA = {
        "key": "operations_leaders",
        "enterprise_mode": True,
        "search_size_credit": 15,
    }
    MIDMARKET_PERSONA = {
        "key": "mx_midmarket_manufacturing",
        "target_company_mode": True,
        "search_size_credit": 15,
    }

    def _enterprise_prospect(self, **overrides) -> dict:
        base = {
            "name": "Test User",
            "title": "Plant Manager",
            "company": "Alimentos Norte",
            "location": "Monterrey, Mexico",
        }
        base.update(overrides)
        return base

    def test_joint_ops_influencer_at_icp_industry_gets_combined_bonus(self):
        """Ops influencer + in-ICP industry → +30 combined, not +36 additive."""
        result = score_prospect(
            self._enterprise_prospect(industry="Food & Beverage"),
            persona_config=self.ENTERPRISE_PERSONA,
        )
        breakdown = result["score_breakdown"]
        # size=15 (search credit) + ops_in_industrial=30 + competitor=20 = 65
        assert result["score"] == 65
        assert breakdown["ops_in_industrial"] == 30
        assert breakdown["role"] == 0
        assert breakdown["industry"] == 0
        assert breakdown["total"] == result["score"]
        # Defense-in-depth: 65 must land in the 60-75 borderline-pass band,
        # NOT the >75 deterministic-pass band.  A regression that restored the
        # old +36 additive would push the score to 71 — still borderline, but
        # the breakdown asserts above pin the joint shape; the verdict assert
        # locks the band.
        assert result["verdict_path"] == "borderline_pass"

    def test_joint_case_reason_is_single_combined_string(self):
        """Joint case emits one combined reason, not two individual ones."""
        from workflows.quality_gate import OPS_IN_INDUSTRIAL_COMBINED
        result = score_prospect(
            self._enterprise_prospect(industry="Food & Beverage"),
            persona_config=self.ENTERPRISE_PERSONA,
        )
        reasons = result["reasons"]
        # Couple the search to the constant (refactor-safe): the reason MUST
        # contain "(+30)" — if OPS_IN_INDUSTRIAL_COMBINED changes, this test
        # tracks it.  Prose-only substring matches drift; the constant marker
        # does not.
        bonus_marker = f"(+{OPS_IN_INDUSTRIAL_COMBINED})"
        joint_reasons = [r for r in reasons if bonus_marker in r]
        assert len(joint_reasons) == 1, f"Expected 1 reason carrying {bonus_marker}, got: {reasons}"
        # Should NOT have separate "Domain influencer" AND "In-ICP industry" reasons
        assert not any("Domain influencer" in r for r in reasons)
        assert not any("In-ICP industry" in r for r in reasons)
        # The combined reason must name both title and industry
        assert "Plant Manager" in joint_reasons[0]
        assert "Food & Beverage" in joint_reasons[0]

    def test_joint_case_fires_for_all_icp_industries(self):
        """Joint case applies across the full IN_ICP_INDUSTRIES set."""
        from workflows.quality_gate import IN_ICP_INDUSTRIES
        for industry in IN_ICP_INDUSTRIES:
            result = score_prospect(
                self._enterprise_prospect(industry=industry),
                persona_config=self.ENTERPRISE_PERSONA,
            )
            breakdown = result["score_breakdown"]
            assert breakdown["ops_in_industrial"] == 30, (
                f"Expected ops_in_industrial=30 for industry={industry!r}, "
                f"got breakdown={breakdown}"
            )

    def test_ops_influencer_no_industry_uses_individual_role_path(self):
        """Ops influencer with no industry → +24 individual role (no joint case)."""
        result = score_prospect(
            self._enterprise_prospect(industry=None),
            persona_config=self.ENTERPRISE_PERSONA,
        )
        breakdown = result["score_breakdown"]
        # size=15 (search credit) + role=24 + competitor=20 = 59
        assert result["score"] == 59
        assert breakdown["role"] == 24
        assert breakdown["industry"] == 0
        # Joint key is always present (invariant shape); 0 means non-joint path fired.
        assert breakdown["ops_in_industrial"] == 0

    def test_decision_maker_at_icp_industry_keeps_individual_paths(self):
        """Decision-maker + in-ICP industry → +28 + +12 = +40, NOT joint.

        Decision-makers (Director, VP, etc.) are not influencers; the joint
        case requires is_influencer=True.  Their combined contribution is
        larger than the joint bonus because they are higher-value contacts.
        """
        result = score_prospect(
            {
                "name": "Test",
                "title": "Director de Manufactura",
                "company": "Acme Foods",
                "location": "Monterrey, Mexico",
                "industry": "Food & Beverage",
            },
            persona_config=self.ENTERPRISE_PERSONA,
        )
        breakdown = result["score_breakdown"]
        assert breakdown["role"] == 28      # decision-maker
        assert breakdown["industry"] == 12  # individual industry bonus preserved
        assert breakdown["ops_in_industrial"] == 0  # invariant shape; 0 means non-joint
        # size=15 (search credit) + role=28 + competitor=20 + industry=12 = 75
        assert result["score"] == 75

    def test_generic_influencer_at_icp_industry_keeps_individual_paths(self):
        """Generic influencer (no ops/digi domain) + in-ICP industry → +18 + +12 = +30.

        Title like 'Team Lead' has influencer flag but no domain signal, so
        is_ops_domain=False and the joint case does NOT fire.  Individual paths apply.
        """
        result = score_prospect(
            {
                "name": "Test",
                "title": "Team Lead",
                "company": "Pharma Corp",
                "location": "Mexico City, Mexico",
                "industry": "Pharma",
            },
            persona_config=self.ENTERPRISE_PERSONA,
        )
        breakdown = result["score_breakdown"]
        assert breakdown["role"] == 18      # generic influencer, no domain
        assert breakdown["industry"] == 12  # individual industry bonus
        assert breakdown["ops_in_industrial"] == 0  # invariant shape; 0 means non-joint
        # size=15 (search credit) + role=18 + competitor=20 + industry=12 = 65
        assert result["score"] == 65

    def test_digi_influencer_at_icp_industry_keeps_individual_paths(self):
        """NARROW interp: digi-domain influencer + in-ICP industry → +24 + +12 = +36.

        Digital Transformation Manager / Innovation Manager: is_digi_domain=True
        but is_ops_domain=False.  The joint case is NARROW — only ops-domain
        influencers fire it.  Digi roles are conceptually orthogonal to industry
        vertical and keep the full additive score.
        """
        result = score_prospect(
            {
                "name": "Test",
                "title": "Digital Transformation Manager",
                "company": "Alimentos Norte",
                "location": "Monterrey, Mexico",
                "industry": "Food & Beverage",
            },
            persona_config=self.ENTERPRISE_PERSONA,
        )
        breakdown = result["score_breakdown"]
        assert breakdown["role"] == 24      # digi-domain influencer (+24, unchanged)
        assert breakdown["industry"] == 12  # individual industry bonus (+12, unchanged)
        assert breakdown["ops_in_industrial"] == 0  # NARROW: digi never fires joint
        # size=15 (search credit) + role=24 + competitor=20 + industry=12 = 71 (NARROW: no joint)
        assert result["score"] == 71

    def test_ops_influencer_at_off_icp_industry_uses_individual_penalty(self):
        """Ops influencer + off-ICP ('Other') → +24 (role) + -25 (industry) = -1 net.

        Joint case requires industry IN IN_ICP_INDUSTRIES.  Off-ICP triggers
        the individual penalty path, not the joint case.
        """
        result = score_prospect(
            self._enterprise_prospect(industry="Other"),
            persona_config=self.ENTERPRISE_PERSONA,
        )
        breakdown = result["score_breakdown"]
        assert breakdown["role"] == 24       # individual role path
        assert breakdown["industry"] == -25  # individual off-ICP penalty
        assert breakdown["ops_in_industrial"] == 0  # off-ICP never fires joint
        # size=15 (search credit) + role=24 + competitor=20 + industry=-25 = 34
        assert result["score"] == 34

    def test_ops_influencer_at_unknown_industry_uses_individual_role(self):
        """Ops influencer + unknown industry label → +24 (role) + 0 (industry).

        An unrecognized industry label (not in IN_ICP_INDUSTRIES or OFF_ICP_INDUSTRIES)
        is neutral.  The joint case requires explicit in-ICP membership, so it
        does NOT fire for unknown labels.
        """
        result = score_prospect(
            self._enterprise_prospect(industry="Education"),
            persona_config=self.ENTERPRISE_PERSONA,
        )
        breakdown = result["score_breakdown"]
        assert breakdown["role"] == 24
        assert breakdown["industry"] == 0
        assert breakdown["ops_in_industrial"] == 0  # unknown industry never fires joint
        # size=15 (search credit) + role=24 + competitor=20 + industry=0 = 59
        assert result["score"] == 59

    def test_joint_case_reduces_over_count_relative_to_additive(self):
        """Joint combined bonus (+30) is strictly less than old additive (+36)."""
        from workflows.quality_gate import INDUSTRY_BONUS_IN_ICP, OPS_IN_INDUSTRIAL_COMBINED
        old_additive = 24 + INDUSTRY_BONUS_IN_ICP  # +36
        assert old_additive > OPS_IN_INDUSTRIAL_COMBINED, (
            f"Joint bonus {OPS_IN_INDUSTRIAL_COMBINED} must be < additive {old_additive}"
        )

    def test_component_scores_decomposition_reconciles_with_total(self):
        """score_breakdown components must sum to total in joint case."""
        result = score_prospect(
            self._enterprise_prospect(industry="Manufacturing"),
            persona_config=self.ENTERPRISE_PERSONA,
        )
        breakdown = result["score_breakdown"]
        # Enumerate all numeric components (excluding 'total' and 'reasons')
        component_sum = sum(
            v for k, v in breakdown.items()
            if k not in ("total", "reasons") and isinstance(v, int | float)
        )
        assert component_sum == breakdown["total"], (
            f"Components sum to {component_sum} but total={breakdown['total']}: {breakdown}"
        )

    def test_sales_role_disqualifier_overrides_joint_case(self):
        """Sales-role -40 penalty fires AFTER joint case; reject wins regardless.

        A title like 'Sales Operations Manager' at an in-ICP company will
        satisfy the joint predicate (is_influencer + is_ops_domain + in-ICP
        industry) and pick up the +30 ops_in_industrial bonus.  But the
        sales-role keyword guard (-40) fires later and the verdict path
        short-circuits to ``deterministic_reject_sales_role`` — the prospect
        is rejected regardless of score.

        Confirms penalty ordering: a future refactor that moved the joint
        case AFTER the sales-role gate would break this; this test catches it.
        """
        result = score_prospect(
            self._enterprise_prospect(
                title="Sales Operations Manager",
                industry="Food & Beverage",
            ),
            persona_config=self.ENTERPRISE_PERSONA,
        )
        breakdown = result["score_breakdown"]
        # Joint case fired AND sales-role penalty applied:
        assert breakdown["ops_in_industrial"] == 30
        assert breakdown["sales_role_penalty"] == -40
        # Hard reject regardless of score:
        assert result["pass"] is False
        assert result["verdict_path"] == "deterministic_reject_sales_role"

    def test_low_confidence_status_blocks_joint_bonus(self):
        """PR-25 fold-in (2-agent convergence: prospect-weekly + pr-test):
        ops_industrial_joint must NOT fire when industry_vertical_status
        is 'low_confidence' or 'unknown' — a shaky industry classification
        cannot trigger the +30 combined bonus. The guard at line 1041 of
        score_prospect (industry_vertical_status == 'confirmed') gates this.

        Regression sensitivity: relaxing the guard would silently re-enable
        the joint bonus for unverified classifications — the largest single
        delta in the scoring model would fire on unconfirmed signal.
        """
        # Plant Manager + Food & Beverage with low_confidence status — would
        # be the canonical joint case if status were 'confirmed'.
        prospect = {
            **self._enterprise_prospect(industry="Food & Beverage"),
            "industry_vertical_status": "low_confidence",
        }
        result = score_prospect(prospect, persona_config=self.ENTERPRISE_PERSONA)
        breakdown = result["score_breakdown"]
        # Joint bonus blocked
        assert breakdown["ops_in_industrial"] == 0, (
            "low_confidence status must block ops_industrial_joint bonus"
        )
        # Individual role bonus still applies (no abstain on role itself,
        # only on industry signal)
        assert breakdown["role"] == 24, "ops-influencer role bonus preserved"
        # Industry abstained
        assert breakdown["industry"] == 0

    def test_unknown_status_blocks_joint_bonus(self):
        """Same gate also fires for status='unknown' (LLM dispatch failed)."""
        prospect = {
            **self._enterprise_prospect(industry="Food & Beverage"),
            "industry_vertical_status": "unknown",
        }
        result = score_prospect(prospect, persona_config=self.ENTERPRISE_PERSONA)
        breakdown = result["score_breakdown"]
        assert breakdown["ops_in_industrial"] == 0
        assert breakdown["role"] == 24
        assert breakdown["industry"] == 0


class TestSearchScopedSizeCredit:
    """2026-07-06 RCA fix: company size is a lane-level structural credit
    declared in the persona config (the SN saved search's headcount facet is
    the size signal), never a per-row lookup. No config → abstain. (PR-227)"""

    def _dm_prospect(self, **overrides) -> dict:
        base = {
            "name": "Test",
            "title": "Director de Operaciones",
            "company": "Acme Foods",
            "location": "Monterrey, Mexico",
        }
        base.update(overrides)
        return base

    def test_credit_applied_with_reason_and_component(self):
        result = score_prospect(self._dm_prospect(), persona_config={
            "key": "operations_leaders",
            "enterprise_mode": True,
            "search_size_credit": 15,
            "search_headcount_filter": "501-1,000 / 1,001+",
        })
        assert result["score_breakdown"]["size"] == 15
        joined = " ".join(result["reasons"])
        assert "Size scoped by SN search (+15)" in joined
        assert "501-1,000 / 1,001+" in joined

    def test_no_credit_key_abstains(self):
        result = score_prospect(self._dm_prospect(), persona_config={
            "key": "operations_leaders",
            "enterprise_mode": True,
        })
        assert result["score_breakdown"]["size"] == 0
        assert any("abstained" in r for r in result["reasons"])

    def test_no_persona_config_abstains(self):
        result = score_prospect(self._dm_prospect())
        assert result["score_breakdown"]["size"] == 0
        assert any("abstained" in r for r in result["reasons"])

    def test_credit_out_of_range_raises(self):
        import pytest
        for bad in (31, -1):
            with pytest.raises(ValueError, match="search_size_credit"):
                score_prospect(self._dm_prospect(), persona_config={
                    "key": "operations_leaders",
                    "enterprise_mode": True,
                    "search_size_credit": bad,
                })

    def test_shipped_credit_15_keeps_autopass_off(self):
        """Calibration gate (2026-07-06): the DM + confirmed in-ICP industry
        cell measured below the 80% enable bar — so the shipped credit of 15
        lands the best possible hand on exactly 75, which is NOT > 75:
        everything still routes through the LLM band."""
        result = score_prospect(
            self._dm_prospect(industry="Food & Beverage",
                              industry_vertical_status="confirmed"),
            persona_config={
                "key": "operations_leaders",
                "enterprise_mode": True,
                "search_size_credit": 15,
            })
        assert result["score"] == 75          # 15 + 28 + 20 + 12
        assert result["verdict_path"] == "borderline_pass"

    def test_credit_above_reachability_line_enables_autopass(self):
        """The enable lever: any credit >= 16 makes DM + confirmed in-ICP
        industry cross 75. Raise personas.json only with fresh calibration
        evidence (>= 80% precision in the auto-pass cell)."""
        result = score_prospect(
            self._dm_prospect(industry="Food & Beverage",
                              industry_vertical_status="confirmed"),
            persona_config={
                "key": "operations_leaders",
                "enterprise_mode": True,
                "search_size_credit": 22,
            })
        assert result["score"] == 82          # 22 + 28 + 20 + 12
        assert result["verdict_path"] == "enterprise_pass"

    def test_low_confidence_industry_still_abstains_from_autopass(self):
        """PR-25 abstain composes with the credit: an ingest-classified
        (low_confidence) label contributes 0, so even a high credit cannot
        auto-pass on an unverified industry signal."""
        result = score_prospect(
            self._dm_prospect(industry="Food & Beverage",
                              industry_vertical_status="low_confidence"),
            persona_config={
                "key": "operations_leaders",
                "enterprise_mode": True,
                "search_size_credit": 22,
            })
        assert result["score"] == 70          # 22 + 28 + 20 + 0
        assert result["verdict_path"] in ("borderline_pass", "borderline_reject")


class TestDeterministicGeometryConstants:
    """The reachability line and pass-path registry are derived/shared, not
    re-encoded literals (2026-07-06 code-review findings). (PR-227)"""

    def test_pass_paths_subset_of_registry(self):
        from workflows.quality_gate import DETERMINISTIC_PASS_PATHS, VERDICT_PATHS
        assert DETERMINISTIC_PASS_PATHS <= VERDICT_PATHS

    def test_reachability_boundary_behavior(self):
        """Behavior-derived boundary tie: at MIN-1 the best hand lands exactly
        on the threshold (borderline); at MIN it crosses (deterministic pass).
        If any geometry constant changes without the derivation following,
        this breaks loudly."""
        from workflows.weekly_prospect import DETERMINISTIC_REACHABLE_MIN_CREDIT
        best_hand = {
            "name": "Test",
            "title": "Director de Operaciones",   # decision-maker
            "company": "Acme Foods",               # non-competitor
            "location": "Monterrey, Mexico",
            "industry": "Food & Beverage",         # confirmed in-ICP
            "industry_vertical_status": "confirmed",
        }
        def cfg(credit):
            return {"key": "operations_leaders", "enterprise_mode": True,
                    "search_size_credit": credit}
        at_line = score_prospect(best_hand, persona_config=cfg(DETERMINISTIC_REACHABLE_MIN_CREDIT))
        below_line = score_prospect(best_hand, persona_config=cfg(DETERMINISTIC_REACHABLE_MIN_CREDIT - 1))
        assert at_line["verdict_path"] == "enterprise_pass"
        assert below_line["verdict_path"] == "borderline_pass"

    def test_non_numeric_credit_raises_actionable_error(self):
        import pytest
        with pytest.raises(ValueError, match="search_size_credit"):
            score_prospect({
                "name": "Test", "title": "Director", "company": "Acme",
                "location": "Mexico",
            }, persona_config={
                "key": "operations_leaders", "enterprise_mode": True,
                # the plausible typo: headcount facet string pasted into credit
                "search_size_credit": "501-1,000",
            })


class TestDmOffIcpRescue:
    """Adversarial-QA (pipeline-leakage lens): a decision-maker pushed under
    the 40 reject line SOLELY by the off-ICP industry penalty routes to the
    LLM band instead of a silent deterministic reject. (PR-227)"""

    CFG = {"key": "operations_leaders", "enterprise_mode": True,
           "search_size_credit": 15}

    def test_dm_at_confirmed_other_stages_for_llm(self):
        result = score_prospect({
            "name": "Test", "title": "Director de Operaciones",
            "company": "Grupo Conglomerado",
            "location": "Monterrey, Mexico",
            "industry": "Other", "industry_vertical_status": "confirmed",
        }, persona_config=self.CFG, agent_gate=True)
        # 15 + 28 + 20 - 25 = 38 — under 40, but the penalty was decisive.
        assert result["score"] == 38
        assert result["verdict_path"] != "deterministic_reject"
        assert result.get("needs_agent_qualification") is True

    def test_non_dm_at_confirmed_other_still_rejects(self):
        result = score_prospect({
            "name": "Test", "title": "Production Supervisor",
            "company": "Servicios Generales",
            "location": "Monterrey, Mexico",
            "industry": "Other", "industry_vertical_status": "confirmed",
        }, persona_config=self.CFG, agent_gate=True)
        assert result["score"] < 40
        assert result["verdict_path"] == "deterministic_reject"

    def test_dm_below_40_with_decisive_penalty_reaches_llm(self):
        # A decision-maker whose drop below 40 is entirely the industry
        # penalty reaches the LLM whenever the penalty is decisive, regardless
        # of size credit (no persona config → size 0).
        result = score_prospect({
            "name": "Test", "title": "Director de Operaciones",
            "company": "Acme",
            "location": "Monterrey, Mexico",
            "industry": "Other", "industry_vertical_status": "confirmed",
        }, agent_gate=True)
        # 0 + 28 + 20 - 25 = 23; without the penalty it would be 48 >= 40,
        # so the rescue applies.
        assert result.get("needs_agent_qualification") is True

    def test_scale_version_stamped_in_breakdown(self):
        from workflows.quality_gate import SCORING_SCALE_VERSION
        result = score_prospect({
            "name": "Test", "title": "Director de Operaciones",
            "company": "Acme Foods", "location": "Monterrey, Mexico",
        }, persona_config=self.CFG)
        assert result["score_breakdown"]["scale_version"] == SCORING_SCALE_VERSION
