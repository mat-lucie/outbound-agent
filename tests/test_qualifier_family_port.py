"""Port coverage for PR-222 / PR-230 / PR-238 / PR-240 (feat/port-qualifier-family).

Focused, config-agnostic assertions on the MECHANISMS ported into the engine.
Keyword/term lists themselves are operator ICP DATA (config-sourced); these
tests exercise the machinery under the synthetic Acme ICP pinned by conftest.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from models.enums import Language
from workflows.daily_check import (
    expected_language_for_entry,
    language_mismatch_verdict,
)
from workflows.quality_gate import _ICP, _match_disqualifier, score_prospect

_IN_ICP = next(iter(_ICP.in_icp_industries))
_ENTERPRISE = {"enterprise_mode": True, "key": "operations_leaders"}


def _confirmed(title: str) -> dict:
    return {
        "title": title,
        "company": "Globex",
        "location": "Mexico City, Mexico",
        "employee_count": 6000,
        "industry": _IN_ICP,
        "industry_vertical_status": "confirmed",
    }


# ── PR-230: joint-bonus double-count fix + decomposition invariant ──────────


class TestJointBonusDoubleCount:
    def test_hybrid_dm_ops_title_takes_dm_credit_not_joint(self):
        # A title matching BOTH decision-maker and ops-influencer must score as
        # DM (role credit) and NEVER stack the +OPS_IN_INDUSTRIAL joint bonus.
        result = score_prospect(
            _confirmed("VP General Manager Production"), persona_config=_ENTERPRISE
        )
        bd = result["score_breakdown"]
        assert bd["role"] > 0  # decision-maker credit was written
        assert bd["ops_in_industrial"] == 0  # joint bonus did NOT fire

    def test_pure_ops_influencer_joint_still_fires(self):
        result = score_prospect(
            _confirmed("Production Manager"), persona_config=_ENTERPRISE
        )
        bd = result["score_breakdown"]
        assert bd["ops_in_industrial"] == _ICP.ops_in_industrial_combined
        assert bd["role"] == 0

    def test_breakdown_always_reconciles(self):
        # The invariant that would have caught the double-count on day one:
        # numeric components sum to the (clamped) total for a spread of shapes.
        titles = [
            "VP General Manager Production",
            "Production Manager",
            "Director de Operaciones",
            "Team Lead",
            "Warehouse Worker",
            "Global Operations Manager",
        ]
        for t in titles:
            bd = score_prospect(_confirmed(t), persona_config=_ENTERPRISE)[
                "score_breakdown"
            ]
            numeric = sum(
                v
                for k, v in bd.items()
                if k != "total" and isinstance(v, int)
            )
            assert max(0, min(100, numeric)) == bd["total"], (t, bd)


# ── PR-222 Rec E + PR-238: new disqualifier families ────────────────────────


class TestNewDisqualifierFamilies:
    def test_competitor_company_is_hard_reject(self):
        # Distinct from the scoring-time competitor list (which only withholds a
        # bonus): a competitor EMPLOYER is a deterministic hard reject.
        slug, _ = _match_disqualifier("vp operations", "rival systems")
        assert slug == "disqualifier_competitor"

    def test_academic_company_family(self):
        slug, _ = _match_disqualifier("head of innovation", "riverside university")
        assert slug == "disqualifier_academic"

    def test_healthcare_provider_family(self):
        slug, _ = _match_disqualifier("head of quality", "riverside general hospital")
        assert slug == "disqualifier_healthcare"

    def test_freelance_employer_family(self):
        slug, _ = _match_disqualifier("operations consultant", "freelance")
        # consultant title also matches, but the company-keyed freelance family
        # runs first — either way it's a deterministic reject.
        assert slug in ("disqualifier_freelance", "disqualifier_consulting")

    def test_medical_regulatory_title_family(self):
        slug, _ = _match_disqualifier("medical affairs country lead", "globex pharma")
        assert slug == "disqualifier_medical_regulatory"

    def test_medreg_ops_override_bypass_rescues_manufacturing_ops(self):
        # A genuine manufacturing-ops title carrying an incidental regulatory
        # token is rescued by the OPS_OVERRIDE disjoint-span bypass.
        assert (
            _match_disqualifier(
                "plant manager, regulatory affairs liaison", "wayne manufacturing"
            )
            is None
        )

    def test_state_owned_wins_over_new_families_on_overlap(self):
        # Ordering: state/pe/consulting keep priority over the Rec E families.
        slug, _ = _match_disqualifier("director of operations", "ministry of industry")
        assert slug == "disqualifier_state_owned"


# ── PR-240: fail-closed language guard helpers ──────────────────────────────


class TestLanguageMismatchVerdict:
    def test_en_stored_on_latam_expected_flags(self):
        assert language_mismatch_verdict(Language.EN, Language.ES, "enterprise_mode")
        assert language_mismatch_verdict(Language.EN, Language.PT, "enterprise_mode")

    def test_es_pt_person_override_never_flags(self):
        # Person-level language truth outranks company HQ; es↔pt is benign.
        assert not language_mismatch_verdict(Language.PT, Language.ES, "enterprise_mode")
        assert not language_mismatch_verdict(Language.ES, Language.PT, "enterprise_mode")

    def test_hq_derived_en_is_never_an_expectation(self):
        # HQ-derived "en" is an unusable catch-all; a stored es/pt with an
        # HQ-"en" expectation must not flag.
        assert not language_mismatch_verdict(Language.ES, Language.EN, "enterprise_mode")

    def test_us_mode_non_en_flags(self):
        assert language_mismatch_verdict(Language.ES, Language.EN, "us_mode")

    def test_us_mode_en_ok(self):
        assert not language_mismatch_verdict(Language.EN, Language.EN, "us_mode")

    def test_undeterminable_expected_never_flags(self):
        assert not language_mismatch_verdict(Language.EN, None, "enterprise_mode")


class TestExpectedLanguageForEntry:
    def _attio(self, hq_country):
        attio = MagicMock()
        attio._person_to_company = {"rec1": "co1"}
        attio.company_hq_country_code.return_value = hq_country
        return attio

    def test_us_mode_short_circuits_to_english_no_fetch(self):
        attio = self._attio(None)
        result = expected_language_for_entry(
            attio, {"record_id": "rec1", "scoring_lane": "us_mode"}, MagicMock()
        )
        assert result is Language.EN
        attio.company_hq_country_code.assert_not_called()

    def test_hq_country_maps_to_language(self):
        attio = self._attio("MX")
        result = expected_language_for_entry(
            attio,
            {"record_id": "rec1", "scoring_lane": "enterprise_mode"},
            MagicMock(),
        )
        assert result is Language.ES

    def test_no_linked_company_fails_open(self):
        attio = MagicMock()
        attio._person_to_company = {}
        result = expected_language_for_entry(
            attio,
            {"record_id": "rec1", "scoring_lane": "enterprise_mode"},
            MagicMock(),
        )
        assert result is None

    def test_missing_hq_country_fails_open(self):
        attio = self._attio(None)
        result = expected_language_for_entry(
            attio,
            {"record_id": "rec1", "scoring_lane": "enterprise_mode"},
            MagicMock(),
        )
        assert result is None


class TestCompanyHqCountryCode:
    """The Attio getter that seeds the guard — fail-open contract."""

    def _client(self):
        from clients.attio import AttioClient

        return AttioClient.__new__(AttioClient)

    def test_reads_country_code_from_dict_shape(self):
        c = self._client()
        c._company_hq_country_cache = {}
        c.get_company = MagicMock(
            return_value={"values": {"hq_country_code": [{"country_code": "mx"}]}}
        )
        assert c.company_hq_country_code("co1") == "MX"

    def test_empty_company_id_returns_none(self):
        c = self._client()
        c._company_hq_country_cache = {}
        assert c.company_hq_country_code("") is None

    def test_missing_attribute_returns_none(self):
        c = self._client()
        c._company_hq_country_cache = {}
        c.get_company = MagicMock(return_value={"values": {}})
        assert c.company_hq_country_code("co1") is None

    def test_fetch_error_fails_open(self):
        c = self._client()
        c._company_hq_country_cache = {}
        c.get_company = MagicMock(side_effect=RuntimeError("boom"))
        assert c.company_hq_country_code("co1") is None

    def test_result_is_cached(self):
        c = self._client()
        c._company_hq_country_cache = {}
        c.get_company = MagicMock(
            return_value={"values": {"hq_country_code": [{"country_code": "BR"}]}}
        )
        assert c.company_hq_country_code("co1") == "BR"
        assert c.company_hq_country_code("co1") == "BR"
        c.get_company.assert_called_once()
