# Contributing to outbound-agent

Thanks for your interest. This guide covers local setup, the quality gates every
change must pass, and the two most common contributions (adding a CRM adapter,
adjusting operator config).

## Local setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env          # fill in only what you need to run
python -m pytest -q           # should be green
```

## Quality gates (run before every commit)

A change is ready when all of these pass:

```bash
python -m pytest -q -p no:cacheprovider     # full suite green, 0 failed
ruff check <files you changed>              # no NEW lint in files you touched
mypy <source files you changed>             # no NEW type errors in files you touched
python scripts/check_no_secrets.py          # no secrets in the tree
```

Notes:
- **No NEW lint/type errors in files you touch.** The repo carries some
  pre-existing inherited lint/type debt; don't fix unrelated debt in a feature
  PR, but don't add to it either.
- **Tests are required.** New behavior needs tests; bug fixes need a regression
  test. Prefer too many assertions over too few.
- **Don't weaken existing tests** to make a change pass. If an assertion must
  change, that's a behavior change — call it out explicitly in the PR.

## Design principles

- **Config-driven, not hardcoded.** Operator-specific values (ICP, caps,
  cadence, models, copy) belong in `config/*.yaml` or `content/`, with neutral
  shipped defaults and a synthetic reference operator's values under
  `examples/acme/`. Never hardcode one operator's data into engine code.
- **Fail loud, never silent.** Missing/invalid config raises `ConfigError`;
  compliance gaps raise before sending. No silent defaults that mask a
  misconfiguration.
- **Surgical changes.** Touch only what the change needs; match existing style.

## A note on provenance markers

This engine was extracted from an internal sales-automation agent. You'll see
internal-plan provenance scattered through comments, docstrings, and test names:

- **`§N.N` section refs** (e.g. `§3.18`) point at sections of the original
  internal design plan.
- **`PR-NN` labels** (e.g. `PR-26`, `test_pr26_*`) tag the change that introduced
  a behavior; they double as a quick "why does this exist" pointer.
- **`sales-program.md`** is a worked example outreach playbook (debranded from
  the original operator's) kept as a reference for voice/cadence rules.

These are design history, not load-bearing identifiers — treat them as comments.
New code doesn't need to add them.

## Adding a CRM adapter (the main extension point)

The engine talks to your CRM through the `CRMProvider` contract; Attio is the
reference implementation. To add another vendor:

1. Read [`clients/crm/CONTRACT.md`](clients/crm/CONTRACT.md) — the method
   contract and migration notes.
2. Implement a provider modeled on `clients/crm/fake_provider.py` (the minimal
   in-memory reference) — *not* `attio_provider.py`, whose value-wrapping is an
   Attio quirk.
3. Make it pass the conformance suite: `tests/crm/test_provider_contract.py`
   runs the full contract against every registered provider. Register yours and
   get it green.
4. The `/onboard` skill (`skills/onboard/`) can generate a first draft of an
   adapter and point it at the conformance suite — a good starting point.

## Commit + PR conventions

- **Conventional Commits**: `type(scope): summary` (`feat`, `fix`, `docs`,
  `refactor`, `test`, `chore`, …). The body explains *why*, not just *what*.
- Keep PRs focused — one logical change. Re-read your diff before committing;
  every changed line should trace to the stated goal.
- For behavior-sensitive changes (scoring, sending, CRM writes), describe how
  you verified behavior is preserved (golden tests, before/after).

## Compliance-sensitive areas

Changes to the email or LinkedIn send paths must preserve the safety and
compliance gates (per-day caps, suppression, the CAN-SPAM send-gate). Read
[COMPLIANCE.md](COMPLIANCE.md) before touching them.
