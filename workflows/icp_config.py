"""Typed loader for the deterministic ICP scoring inputs.

The quality gate's keyword lists, industry buckets, numeric weights, score
bands, and geography lists are operator-defined ICP DATA — they describe WHO
the operator sells to. This module externalizes those VALUES into
``config/icp.yaml`` (falling back to the shipped ``config/icp.example.yaml``
default) and exposes them as a typed :class:`ICPConfig`.

The scoring MATH/LOGIC stays in ``workflows/quality_gate.py`` and is unchanged
by this externalization — only the SOURCE of the values moved from hardcoded
literals to config.

Load rule (per config/README.md convention):

    config/icp.yaml         (operator's live ICP, gitignored)      — if present
    config/icp.example.yaml (shipped NEUTRAL template — replace it) — fallback

Fails loudly with ``ConfigError`` when neither file exists or a required
section/key is missing — never a silent default that masks a misconfiguration.
"""

from dataclasses import dataclass
from typing import Any

from clients.settings import ConfigError, config_dir, load_yaml


@dataclass(frozen=True)
class ICPConfig:
    """Typed, immutable snapshot of the deterministic ICP scoring inputs.

    Every field maps 1:1 to a former module-level constant in
    ``workflows.quality_gate``. Keyword lists preserve their source order
    (load-bearing for the disqualifier families that pick the earliest
    positional match). Frozen so a loaded config can't be mutated mid-run.
    """

    # Persona / role keywords
    digitalization_keywords: list[str]
    executive_keywords: list[str]
    operations_keywords: list[str]
    decision_maker_keywords: list[str]
    decision_maker_exemptions: list[str]
    influencer_keywords: list[str]

    # Geography
    global_executive_keywords: list[str]
    latam_geo_override: list[str]
    pt_locations: list[str]
    es_locations: list[str]
    en_locations: list[str]

    # Industry buckets
    in_icp_industries: frozenset[str]
    off_icp_industries: frozenset[str]

    # Numeric weights
    industry_bonus_in_icp: int
    industry_penalty_off_icp: int
    ops_in_industrial_combined: int

    # Thresholds / bands
    score_band_labels: tuple[str, ...]

    # Disqualifier families
    competitor_keywords: list[str]
    academic_keywords: list[str]
    consultant_keywords: list[str]
    sales_role_keywords: list[str]
    sales_role_ops_exemptions: list[str]
    junior_ic_keywords: list[str]
    junior_ic_exemptions: list[str]
    hr_keywords: list[str]
    finance_keywords: list[str]
    innovation_keywords: list[str]
    pe_keywords: list[str]
    state_owned_keywords: list[str]
    consulting_firm_keywords: list[str]
    consulting_title_keywords: list[str]
    ops_override_keywords: list[str]
    # PR-222 Rec E: high-confidence COMPANY/EMPLOYER disqualifier families
    # (academic institutions, government, healthcare providers, direct
    # competitors, freelance/no-employer). Company-keyed hard rejects, no
    # OPS_OVERRIDE bypass — the disqualifying fact is the employer, not the
    # title. See workflows.quality_gate._match_disqualifier.
    academic_company_keywords: list[str]
    government_keywords: list[str]
    healthcare_provider_keywords: list[str]
    competitor_company_keywords: list[str]
    freelance_employer_keywords: list[str]
    # PR-238: medical / regulatory / clinical-affairs TITLE disqualifier family.
    # Title-keyed (a pharma/biotech clinical-affairs function is not the
    # ops/production buyer), OPS_OVERRIDE disjoint-span bypass still rescues
    # genuine manufacturing-ops titles.
    medical_regulatory_title_keywords: list[str]

    # LLM qualifier prompt slots (P2b). The narrative, ICP-specific pieces of
    # the LLM tiebreaker's system prompt. The render lives in
    # ``workflows.quality_gate`` via ``config/prompts/qualifier.md.j2``; the
    # JSON output contract stays STATIC in that template (engine contract, not
    # operator-tunable ICP).
    qualifier_product_summary: str
    qualifier_geography_requirement: str
    qualifier_lanes: list[str]
    qualifier_disqualifiers: list[str]
    # The two ICP-lane glosses rendered into the static `icp_lane is 1 for ICP 1
    # (...), 2 for ICP 2 (...)` line. Exactly two short strings; operator data so
    # the original operator's "enterprise/$500M+" / "mid-market" don't leak into every prompt.
    qualifier_lane_labels: list[str]


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    """Return ``raw[name]`` as a mapping, or raise ConfigError."""
    value = raw.get(name)
    if not isinstance(value, dict):
        raise ConfigError(
            f"ICP config section {name!r} is missing or not a mapping "
            f"(got {type(value).__name__}). See config/icp.example.yaml."
        )
    return value


def _str_list(section: dict[str, Any], key: str, *, section_name: str) -> list[str]:
    """Return ``section[key]`` as a list[str], or raise ConfigError."""
    value = section.get(key)
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ConfigError(
            f"ICP config key {section_name}.{key!r} must be a list of strings. "
            f"See config/icp.example.yaml."
        )
    return value


def _non_empty_str_list(
    section: dict[str, Any], key: str, *, section_name: str
) -> list[str]:
    """Return ``section[key]`` as a non-empty list of non-blank strings.

    Like :func:`_str_list` but additionally rejects an empty list and any
    element that is ``""`` or whitespace-only — a blank bullet (e.g. an empty
    disqualifier) would silently erode the qualifier prompt. Raises
    :class:`ConfigError` on any violation.
    """
    value = _str_list(section, key, section_name=section_name)
    if not value:
        raise ConfigError(
            f"ICP config key {section_name}.{key!r} must be a non-empty list. "
            f"See config/icp.example.yaml."
        )
    if any(not x.strip() for x in value):
        raise ConfigError(
            f"ICP config key {section_name}.{key!r} must not contain empty or "
            f"whitespace-only elements. See config/icp.example.yaml."
        )
    return value


def _str(section: dict[str, Any], key: str, *, section_name: str) -> str:
    """Return ``section[key]`` as a non-empty str, or raise ConfigError."""
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(
            f"ICP config key {section_name}.{key!r} must be a non-empty string. "
            f"See config/icp.example.yaml."
        )
    return value


def _int(section: dict[str, Any], key: str, *, section_name: str) -> int:
    """Return ``section[key]`` as an int, or raise ConfigError."""
    value = section.get(key)
    # bool is an int subclass; reject it explicitly so a stray `true` doesn't
    # silently become 1.
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(
            f"ICP config key {section_name}.{key!r} must be an integer "
            f"(got {type(value).__name__}). See config/icp.example.yaml."
        )
    return value


def _config_name() -> str:
    """Pick which YAML to load: live ``icp.yaml`` if present, else the example.

    Mirrors the config/README.md convention — the gitignored live copy wins,
    and the tracked ``icp.example.yaml`` is the shipped default (a NEUTRAL
    generic-B2B template the operator is expected to replace; the original worked
    reference lives at examples/acme/config/icp.yaml). With no live config, the
    neutral template is the active ICP.
    """
    base = config_dir()
    if (base / "icp.yaml").is_file():
        return "icp"
    if (base / "icp.example.yaml").is_file():
        return "icp.example"
    raise ConfigError(
        f"No ICP config found in {base}: expected icp.yaml or icp.example.yaml. "
        f"Copy icp.example.yaml to icp.yaml and edit it, or restore the template."
    )


def load_icp_config() -> ICPConfig:
    """Load the ICP scoring inputs into a typed :class:`ICPConfig`.

    Reads ``config/icp.yaml`` when present, otherwise the shipped
    ``config/icp.example.yaml`` default. Raises :class:`ConfigError` on a
    missing file, malformed YAML, or a missing/mistyped section or key.
    """
    raw = load_yaml(_config_name())

    roles = _section(raw, "roles")
    geo = _section(raw, "geography")
    industries = _section(raw, "industries")
    weights = _section(raw, "weights")
    thresholds = _section(raw, "thresholds")
    dq = _section(raw, "disqualifiers")
    qp = _section(raw, "qualifier_prompt")

    in_icp = _str_list(industries, "in_icp", section_name="industries")
    off_icp = _str_list(industries, "off_icp", section_name="industries")
    bands = _str_list(thresholds, "score_band_labels", section_name="thresholds")

    lanes = _non_empty_str_list(qp, "lanes", section_name="qualifier_prompt")
    disqualifiers = _non_empty_str_list(
        qp, "disqualifiers", section_name="qualifier_prompt"
    )
    lane_labels = _non_empty_str_list(
        qp, "lane_labels", section_name="qualifier_prompt"
    )
    if len(lane_labels) != 2:
        raise ConfigError(
            "ICP config key 'qualifier_prompt.lane_labels' must be a list of "
            f"exactly 2 strings (got {len(lane_labels)}). "
            "See config/icp.example.yaml."
        )

    return ICPConfig(
        digitalization_keywords=_str_list(roles, "digitalization_keywords", section_name="roles"),
        executive_keywords=_str_list(roles, "executive_keywords", section_name="roles"),
        operations_keywords=_str_list(roles, "operations_keywords", section_name="roles"),
        decision_maker_keywords=_str_list(roles, "decision_maker_keywords", section_name="roles"),
        decision_maker_exemptions=_str_list(roles, "decision_maker_exemptions", section_name="roles"),
        influencer_keywords=_str_list(roles, "influencer_keywords", section_name="roles"),
        global_executive_keywords=_str_list(geo, "global_executive_keywords", section_name="geography"),
        latam_geo_override=_str_list(geo, "latam_geo_override", section_name="geography"),
        pt_locations=_str_list(geo, "pt_locations", section_name="geography"),
        es_locations=_str_list(geo, "es_locations", section_name="geography"),
        en_locations=_str_list(geo, "en_locations", section_name="geography"),
        in_icp_industries=frozenset(in_icp),
        off_icp_industries=frozenset(off_icp),
        industry_bonus_in_icp=_int(weights, "industry_bonus_in_icp", section_name="weights"),
        industry_penalty_off_icp=_int(weights, "industry_penalty_off_icp", section_name="weights"),
        ops_in_industrial_combined=_int(weights, "ops_in_industrial_combined", section_name="weights"),
        score_band_labels=tuple(bands),
        competitor_keywords=_str_list(dq, "competitor_keywords", section_name="disqualifiers"),
        academic_keywords=_str_list(dq, "academic_keywords", section_name="disqualifiers"),
        consultant_keywords=_str_list(dq, "consultant_keywords", section_name="disqualifiers"),
        sales_role_keywords=_str_list(dq, "sales_role_keywords", section_name="disqualifiers"),
        sales_role_ops_exemptions=_str_list(dq, "sales_role_ops_exemptions", section_name="disqualifiers"),
        junior_ic_keywords=_str_list(dq, "junior_ic_keywords", section_name="disqualifiers"),
        junior_ic_exemptions=_str_list(dq, "junior_ic_exemptions", section_name="disqualifiers"),
        hr_keywords=_str_list(dq, "hr_keywords", section_name="disqualifiers"),
        finance_keywords=_str_list(dq, "finance_keywords", section_name="disqualifiers"),
        innovation_keywords=_str_list(dq, "innovation_keywords", section_name="disqualifiers"),
        pe_keywords=_str_list(dq, "pe_keywords", section_name="disqualifiers"),
        state_owned_keywords=_str_list(dq, "state_owned_keywords", section_name="disqualifiers"),
        consulting_firm_keywords=_str_list(dq, "consulting_firm_keywords", section_name="disqualifiers"),
        consulting_title_keywords=_str_list(dq, "consulting_title_keywords", section_name="disqualifiers"),
        ops_override_keywords=_str_list(dq, "ops_override_keywords", section_name="disqualifiers"),
        academic_company_keywords=_str_list(dq, "academic_company_keywords", section_name="disqualifiers"),
        government_keywords=_str_list(dq, "government_keywords", section_name="disqualifiers"),
        healthcare_provider_keywords=_str_list(dq, "healthcare_provider_keywords", section_name="disqualifiers"),
        competitor_company_keywords=_str_list(dq, "competitor_company_keywords", section_name="disqualifiers"),
        freelance_employer_keywords=_str_list(dq, "freelance_employer_keywords", section_name="disqualifiers"),
        medical_regulatory_title_keywords=_str_list(dq, "medical_regulatory_title_keywords", section_name="disqualifiers"),
        qualifier_product_summary=_str(qp, "product_summary", section_name="qualifier_prompt"),
        qualifier_geography_requirement=_str(qp, "geography_requirement", section_name="qualifier_prompt"),
        qualifier_lanes=lanes,
        qualifier_disqualifiers=disqualifiers,
        qualifier_lane_labels=lane_labels,
    )
