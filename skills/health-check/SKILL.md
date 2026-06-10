---
name: health-check
description: Ad-hoc connectivity check across Attio and PhantomBuster. Read-only — no state mutations.
---

# /health-check

Operator runs this whenever something feels off. Verifies that:

1. `ATTIO_API_KEY` resolves to a usable session.
2. The Operator Review Queue object is queryable (proves API + auth).
3. `PB_SESSION_COOKIE` constructs a `PhantomBusterClient` without raising.

Exit code 0 on healthy; 1 on any reachability failure.

## Invocation

```bash
sales health-check
```

Read-only; no MCP-scope canary required. If you need the full
Attio MCP read+list+write+delete canary, run `/sales-daily --dry-run`
which includes the step-0 canary by design.
