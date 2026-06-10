# `examples/acme/` — synthetic reference operator

**Acme** is a fully fabricated example operator: a generic B2B SaaS company with
a synthetic ICP, synthetic personas, fabricated target companies, and neutral
outreach copy. None of this is real business data.

It serves two purposes:

1. **Worked reference** — copy `config/` and `content/` here as a starting point
   when configuring the engine for your own operator, then replace the values.
2. **Test fixture** — the test suite pins `OUTBOUND_CONFIG_DIR` and
   `OUTBOUND_CONTENT_DIR` here (see `tests/conftest.py`). The golden baselines
   (`tests/test_icp_config_golden.py`, `tests/test_qualifier_prompt_golden.py`)
   are regenerated against this synthetic operator: they are self-consistent
   regression guards on the engine's scoring/prompt-rendering logic, not a
   proof about any real operator's values.

## Layout

```
config/
  icp.yaml              # synthetic ICP (mirrors config/icp.example.yaml)
  outreach.yaml         # outreach cadence / caps / lanes
  prompts/qualifier.md.j2  # byte-identical to the shipped template
content/
  personas.json messages.json emails.json targets.json
  synthetic_personas.json evidence_refs.json
  {mx,co,cl}-midmarket-targets.json  holdout/
```
