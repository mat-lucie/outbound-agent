# Security Policy

`outbound-agent` is pre-1.0 software. Security fixes land on the default branch;
there are no backported release lines yet.

## Reporting a vulnerability

**Do not open a public issue for security problems.** Use GitHub's private
vulnerability reporting: **Security → Report a vulnerability** on this
repository. Include:

- a description of the issue and its impact,
- steps to reproduce (a minimal proof-of-concept if possible),
- the affected file(s)/commit.

You'll get an acknowledgement within a few business days. Please give a
reasonable window to ship a fix before any public disclosure.

In scope: anything that leaks credentials or prospect data, lets an attacker run
code or exfiltrate data through the engine, or bypasses the safety/compliance
gates. Out of scope: the inherent risks of running outreach automation (see
[COMPLIANCE.md](COMPLIANCE.md)) and third-party services (Attio, PhantomBuster,
Resend, Anthropic) — report those to the respective vendor.

## How secrets are handled

The engine is built so secrets never enter version control:

- **Secrets live only in `.env`** (git-ignored). YAML config files store the
  *name* of an environment variable, never the value — e.g. `api_key_env: ATTIO_API_KEY`.
  `clients/settings.py` resolves the name from `os.environ` at runtime.
- **A secrets gate runs in the test suite.** `scripts/check_no_secrets.py`
  (and `tests/test_no_secrets_gate.py`) scan the tree for known secret-value
  shapes (API keys, OAuth tokens, PEM keys, LinkedIn cookies, etc.) and fail if
  any are found — including inside `examples/`. Run it before every commit:
  `python scripts/check_no_secrets.py`.
- **Never commit a real `.env`, cookie, API key, or live prospect export.** If
  you believe a secret was committed, rotate it immediately and report it via
  the private vulnerability channel above.

## Data handling

You (the operator) are the data controller for any prospect data the engine
touches. The engine stores run state and caches under `~/.outbound-agent/` on your
machine and writes to your own CRM. Treat prospect PII per the regulations that
apply to you — see [COMPLIANCE.md](COMPLIANCE.md).
