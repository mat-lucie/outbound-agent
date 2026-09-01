"""Golden regression: the LLM qualifier prompt renders byte-exact from config.

The LLM tiebreaker's system prompt (``QUALIFIER_SYSTEM_PROMPT``) is assembled by
a Jinja2 template (``config/prompts/qualifier.md.j2``) whose narrative ICP slots
(product summary, geography requirement, the two ICP-lane blocks, the
hard-disqualifier bullets) come from the ``qualifier_prompt:`` section of
``config/icp.yaml`` via :class:`ICPConfig`. The JSON output contract stays STATIC
in the template (engine contract, not operator-tunable ICP).

Two proofs live here:

1. ``test_acme_prompt_byte_identical`` — with the synthetic Acme config the
   rendered prompt EQUALS ``EXPECTED_PROMPT`` below character-for-character.
   ``EXPECTED_PROMPT`` was REGENERATED from the Acme reference config; it is a
   self-consistent baseline (any whitespace/character drift in the render fails
   the test), not the retired pre-refactor equivalence proof.

2. ``test_render_is_operator_driven`` — rendering with a DIFFERENT, minimal
   ``qualifier_prompt`` config (via a temp ``OUTBOUND_CONFIG_DIR``) produces a
   prompt carrying those custom values, proving the prompt is genuinely
   operator-driven and "works anyways" for a brand-new ICP.
"""

import textwrap

import pytest

from clients.settings import ConfigError
from workflows.icp_config import load_icp_config
from workflows.quality_gate import (
    QUALIFIER_SYSTEM_PROMPT,
    _render_qualifier_system_prompt,
)

# A minimal but complete icp.example.yaml body (every section ICPConfig
# validates must be present). Tests below override just the slot under test.
_BASE_TOY_YAML = textwrap.dedent(
    """\
    roles:
      digitalization_keywords: ["digital"]
      executive_keywords: ["ceo"]
      operations_keywords: ["operations"]
      decision_maker_keywords: ["director"]
      decision_maker_exemptions: ["coordinator"]
      influencer_keywords: ["manager"]
    geography:
      global_executive_keywords: ["global "]
      latam_geo_override: ["latam"]
      pt_locations: ["brazil"]
      es_locations: ["mexico"]
      en_locations: ["usa"]
    industries:
      in_icp: ["Manufacturing"]
      off_icp: ["Other"]
    weights:
      industry_bonus_in_icp: 12
      industry_penalty_off_icp: -25
      ops_in_industrial_combined: 30
    thresholds:
      score_band_labels: ["<40", "40-59", "60-75", ">75"]
    disqualifiers:
      competitor_keywords: ["competitor inc"]
      academic_keywords: ["professor"]
      consultant_keywords: ["consultant"]
      sales_role_keywords: ["sales"]
      sales_role_ops_exemptions: ["s&op"]
      junior_ic_keywords: ["analyst"]
      junior_ic_exemptions: ["senior"]
      hr_keywords: ["chro"]
      finance_keywords: ["cfo"]
      innovation_keywords: ["r&d"]
      pe_keywords: ["private equity fund"]
      state_owned_keywords: ["government of"]
      consulting_firm_keywords: ["accenture"]
      consulting_title_keywords: ["consultant"]
      ops_override_keywords: ["plant manager"]
      academic_company_keywords: ["university"]
      government_keywords: ["municipality of"]
      healthcare_provider_keywords: ["hospital"]
      competitor_company_keywords: ["rival systems"]
      freelance_employer_keywords: ["freelance"]
      medical_regulatory_title_keywords: ["medical affairs"]
      integrator_description_keywords: ["systems integrator"]
      integrator_manufacturer_carveouts: ["we manufacture"]
    """
)

# Regenerated golden — the EXACT QUALIFIER_SYSTEM_PROMPT rendered from the Acme
# reference config. Self-consistent baseline; regenerate deliberately only when
# the template scaffolding or the Acme qualifier_prompt slots change.
EXPECTED_PROMPT = """You qualify B2B LinkedIn prospects for Acme (a B2B SaaS platform for mid-to-large enterprises).

Two ICP lanes — classify into exactly one. The prospect should sit at a company in your served market. Adjust or remove this gate to match where you actually sell.

**ICP 1 — Enterprise (PRIMARY):** Larger organizations with an established buying committee and budget for new tooling. Target buyer is the leader who owns the relevant function (e.g. VP / Director / Head of the team that would adopt the product), not an individual contributor.

**ICP 2 — Mid-market (SECONDARY):** Smaller, faster-moving companies where a single decision-maker (founder, owner, department head) can approve a purchase. Title is a clear decision-maker for the relevant function.

**Hard disqualifiers (always fail):**
- Consultants, freelancers, advisors (unless clearly an operational role inside a target company)
- Academics, professors, researchers, students
- Direct competitors and vendors selling a substitute product
- Individuals with no authority or influence over the relevant buying decision
- Roles in functions your product does not serve
- Non-profits and institutions outside your commercial market (adjust if they are in-scope for you)
- Junior / individual-contributor roles without manager scope
- Sales / commercial / marketing roles — they sit on the revenue side and rarely own the buying decision for an operational tool. **Exception:** roles that explicitly own the function your product serves.

**Output format:** respond with a JSON object and nothing else:
{"pass": <true|false>, "icp_lane": <1|2>, "rationale": "<one short sentence>"}

`icp_lane` is 1 for ICP 1 (enterprise), 2 for ICP 2 (mid-market). If pass=false, still return your best guess at which lane this prospect was a candidate for. Do not wrap the JSON in markdown. Only the JSON object."""


def test_acme_prompt_byte_identical():
    """Module-level QUALIFIER_SYSTEM_PROMPT == the regenerated golden.

    The module renders the prompt once at import from the Acme reference config.
    This asserts that render is byte-for-byte identical to EXPECTED_PROMPT —
    every newline, bullet, bold marker, em-dash, and space. Any drift fails.
    """
    assert QUALIFIER_SYSTEM_PROMPT == EXPECTED_PROMPT


def test_render_from_acme_config_matches_golden():
    """Re-rendering from a freshly loaded Acme config also matches the golden.

    Belt-and-suspenders alongside the import-time check: proves the render
    helper is deterministic and config-sourced, not a one-off import artifact.
    """
    rendered = _render_qualifier_system_prompt(load_icp_config())
    assert rendered == EXPECTED_PROMPT


def test_render_is_operator_driven(tmp_path, monkeypatch):
    """A different operator's qualifier_prompt config yields a different prompt.

    Points OUTBOUND_CONFIG_DIR at a temp config dir carrying a toy ICP whose
    qualifier_prompt slots are clearly NOT the shipped example's, plus the template,
    and asserts the rendered prompt carries the custom product/geography/lane/
    disqualifier values. Proves the prompt is operator-driven, not hardwired.
    """
    # A minimal but complete icp.example.yaml: every section ICPConfig validates
    # must be present, but only qualifier_prompt carries the toy ICP we assert on.
    toy_yaml = _BASE_TOY_YAML + textwrap.dedent(
        """\
        qualifier_prompt:
          product_summary: "WidgetCo (CNC machine uptime SaaS for EU automotive)"
          geography_requirement: "The buyer MUST run a German or Austrian plant floor."
          lanes:
            - "**Lane A — Tier-1 supplier:** owns a stamping line."
            - "**Lane B — Job shop:** under 50 machines, owner-operator."
          disqualifiers:
            - "Robotics integrators and SI partners."
            - "Pure-play distributors with no machining."
          lane_labels:
            - "tier-1 supplier"
            - "job shop"
        """
    )
    config_dir = tmp_path / "config"
    (config_dir / "prompts").mkdir(parents=True)
    (config_dir / "icp.example.yaml").write_text(toy_yaml, encoding="utf-8")

    # Reuse the SHIPPED template — the slots are operator data, the scaffolding
    # (incl. the JSON contract) is engine contract and stays identical.
    from clients.settings import config_dir as live_config_dir

    monkeypatch.delenv("OUTBOUND_CONFIG_DIR", raising=False)
    shipped_template = (
        live_config_dir() / "prompts" / "qualifier.md.j2"
    ).read_text(encoding="utf-8")
    (config_dir / "prompts" / "qualifier.md.j2").write_text(
        shipped_template, encoding="utf-8"
    )

    monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(config_dir))
    rendered = _render_qualifier_system_prompt(load_icp_config())

    # Carries the toy operator's values...
    assert "WidgetCo (CNC machine uptime SaaS for EU automotive)" in rendered
    assert "The buyer MUST run a German or Austrian plant floor." in rendered
    assert "**Lane A — Tier-1 supplier:** owns a stamping line." in rendered
    assert "**Lane B — Job shop:** under 50 machines, owner-operator." in rendered
    assert "- Robotics integrators and SI partners." in rendered
    assert "- Pure-play distributors with no machining." in rendered
    # ...including the operator's own lane glosses in the output-format line.
    assert "1 for ICP 1 (tier-1 supplier), 2 for ICP 2 (job shop)." in rendered
    # ...and NONE of the original operator narrative leaked through — neither the
    # blocks nor the lane glosses that used to be hardcoded in the template.
    assert "Lucie" not in rendered
    assert "LATAM" not in rendered
    assert "$500M+" not in rendered
    assert "mid-market" not in rendered
    # ...while the STATIC engine contract is still present, unchanged.
    assert '{"pass": <true|false>, "icp_lane": <1|2>, "rationale": "<one short sentence>"}' in rendered
    assert "Do not wrap the JSON in markdown. Only the JSON object." in rendered


def _write_toy_config(
    tmp_path, monkeypatch, qualifier_prompt_block: str, *, template: str | None = None
):
    """Write a complete toy config dir (base + given qualifier_prompt) and point
    OUTBOUND_CONFIG_DIR at it. Uses the shipped template unless one is given."""
    from clients.settings import config_dir as live_config_dir

    monkeypatch.delenv("OUTBOUND_CONFIG_DIR", raising=False)
    shipped_template = (
        live_config_dir() / "prompts" / "qualifier.md.j2"
    ).read_text(encoding="utf-8")

    config_dir = tmp_path / "config"
    (config_dir / "prompts").mkdir(parents=True)
    (config_dir / "icp.example.yaml").write_text(
        _BASE_TOY_YAML + textwrap.dedent(qualifier_prompt_block), encoding="utf-8"
    )
    (config_dir / "prompts" / "qualifier.md.j2").write_text(
        template if template is not None else shipped_template, encoding="utf-8"
    )
    monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(config_dir))
    return config_dir


# A valid qualifier_prompt block reused by the validation tests below; each test
# mutates one slot to the invalid shape under test.
_VALID_QP_BLOCK = """\
qualifier_prompt:
  product_summary: "WidgetCo SaaS"
  geography_requirement: "Runs an EU plant."
  lanes:
    - "**Lane A:** owns a line."
    - "**Lane B:** small shop."
  disqualifiers:
    - "Integrators."
    - "Distributors."
  lane_labels:
    - "tier-1"
    - "job shop"
"""


def test_render_raises_on_undefined_template_var(tmp_path, monkeypatch):
    """StrictUndefined turns a typo'd template var into a loud UndefinedError.

    A template that references an undefined name must NOT silently render an
    empty string (which would degrade the qualifier prompt); it must raise.
    """
    from jinja2 import UndefinedError

    bad_template = "Prompt for {{ produkt_summary }}.\n"  # typo'd var name
    _write_toy_config(
        tmp_path, monkeypatch, _VALID_QP_BLOCK, template=bad_template
    )
    with pytest.raises(UndefinedError):
        _render_qualifier_system_prompt(load_icp_config())


def test_empty_disqualifier_element_rejected(tmp_path, monkeypatch):
    """A disqualifiers list with blank elements raises ConfigError, not silence."""
    block = _VALID_QP_BLOCK.replace(
        '  disqualifiers:\n    - "Integrators."\n    - "Distributors."\n',
        '  disqualifiers:\n    - ""\n    - ""\n',
    )
    _write_toy_config(tmp_path, monkeypatch, block)
    with pytest.raises(ConfigError, match="disqualifiers"):
        load_icp_config()


def test_blank_lane_element_rejected(tmp_path, monkeypatch):
    """A lanes list with a whitespace-only element raises ConfigError."""
    block = _VALID_QP_BLOCK.replace(
        '  lanes:\n    - "**Lane A:** owns a line."\n    - "**Lane B:** small shop."\n',
        '  lanes:\n    - "**Lane A:** owns a line."\n    - "   "\n',
    )
    _write_toy_config(tmp_path, monkeypatch, block)
    with pytest.raises(ConfigError, match="lanes"):
        load_icp_config()


def test_lane_labels_must_be_exactly_two(tmp_path, monkeypatch):
    """lane_labels with the wrong count raises ConfigError."""
    block = _VALID_QP_BLOCK.replace(
        '  lane_labels:\n    - "tier-1"\n    - "job shop"\n',
        '  lane_labels:\n    - "only-one"\n',
    )
    _write_toy_config(tmp_path, monkeypatch, block)
    with pytest.raises(ConfigError, match="lane_labels"):
        load_icp_config()


def test_missing_consulting_firm_keywords_raises(tmp_path, monkeypatch):
    """Dropping the required `consulting_firm_keywords` family raises ConfigError.

    The two consulting families are required scoring inputs (loaded with the
    same `_str_list` validator as the other disqualifier families) — a missing
    one must fail loud, not silently disable the family.
    """
    monkeypatch.delenv("OUTBOUND_CONFIG_DIR", raising=False)
    body = _BASE_TOY_YAML.replace(
        '  consulting_firm_keywords: ["accenture"]\n', ""
    ) + _VALID_QP_BLOCK
    assert "consulting_firm_keywords" not in body
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "icp.example.yaml").write_text(body, encoding="utf-8")
    monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(config_dir))
    with pytest.raises(ConfigError, match="consulting_firm_keywords"):
        load_icp_config()


def test_missing_consulting_title_keywords_raises(tmp_path, monkeypatch):
    """Dropping the required `consulting_title_keywords` family raises ConfigError."""
    monkeypatch.delenv("OUTBOUND_CONFIG_DIR", raising=False)
    body = _BASE_TOY_YAML.replace(
        '  consulting_title_keywords: ["consultant"]\n', ""
    ) + _VALID_QP_BLOCK
    assert "consulting_title_keywords" not in body
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "icp.example.yaml").write_text(body, encoding="utf-8")
    monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(config_dir))
    with pytest.raises(ConfigError, match="consulting_title_keywords"):
        load_icp_config()


def test_shipped_and_acme_qualifier_templates_are_byte_identical():
    """The operator-neutral qualifier template is duplicated in two places and
    must NOT drift.

    config/prompts/qualifier.md.j2 is the shipped default; the suite renders the
    byte-identity golden from examples/acme/config/prompts/qualifier.md.j2
    (the conftest OUTBOUND_CONFIG_DIR pin points there). The template is pure
    operator-neutral scaffolding — only the ICP slots differ between operators —
    so the two copies must stay byte-identical. If they drift, the shipped
    default could rot while the golden (rendered from the examples/acme copy)
    stays green.
    """
    from pathlib import Path

    from workflows import quality_gate as _qg

    repo_root = Path(_qg.__file__).resolve().parent.parent
    shipped = (repo_root / "config" / "prompts" / "qualifier.md.j2").read_text(
        encoding="utf-8"
    )
    acme = (
        repo_root / "examples" / "acme" / "config" / "prompts" / "qualifier.md.j2"
    ).read_text(encoding="utf-8")
    assert shipped == acme
