# PhantomBuster cookie rotation

Sales Navigator runs use a different cookie pair than the rest of the
phantoms. Rotate every ~14 days, or when `scripts/validate_sales_nav_health.py`
returns `FAIL`.

| Cookie env var | LinkedIn cookie | Used by |
| --- | --- | --- |
| `PB_LI_SESSION_COOKIE` | `li_at` from a regular LinkedIn session | Search Export, Network Booster, Message Sender, SN Inbox Scraper, debug Profile Scraper |
| `PB_LI_SALES_NAV_SESSION_COOKIE` | `li_at` from a Sales Navigator session | Sales Nav Profile Scraper (PR-B), Sales Nav URL Converter (PR-A backfill + enrichment hook) |
| `PB_LI_SALES_NAV_LI_A_COOKIE` | `li_a` from a Sales Navigator session | Sales Nav Profile Scraper, Sales Nav URL Converter |

The Sales Nav `li_at` value may or may not match `PB_LI_SESSION_COOKIE`
depending on whether both phantoms run as the same LinkedIn account. Keep
them in separate env vars so rotating one does not cross-contaminate the
other.

## Capturing the Sales Nav cookies

1. Sign in to <https://www.linkedin.com/sales/> in Chrome (use the same
   account PhantomBuster phantoms log in as).
2. Open DevTools → **Application** → **Cookies** → `https://www.linkedin.com`.
3. Find `li_at`. Copy the **Value** column.
4. Find `li_a`. Copy the **Value** column.
5. Paste into `.env`:
   ```
   PB_LI_SALES_NAV_SESSION_COOKIE=<paste li_at value here>
   PB_LI_SALES_NAV_LI_A_COOKIE=<paste li_a value here>
   ```
6. Verify the new cookies work:
   ```
   python3 scripts/validate_sales_nav_health.py
   ```
   (Added in PR-B subtask 7. Until that lands, you can use
   `scripts/debug_sales_nav_profile_scraper.py` instead.)

## The F6 risk: parallel-session invalidation

LinkedIn's concurrent-session detection may invalidate the regular
`PB_LI_SESSION_COOKIE` when PhantomBuster's IP pool runs both a regular-LI
session AND a Sales Nav session against the same account. If that happens,
the **other 5 phantoms break** — Network Booster cannot send invites,
Message Sender cannot DM, SN Inbox Scraper cannot detect replies.

**Always run after a Sales Nav cookie rotation:**

```
python3 scripts/probe_parallel_cookie_sessions.py --known-good-url "<a Sales Nav URL>"
```

This runs three phases:

1. SN Inbox Scraper with `PB_LI_SESSION_COOKIE` (read-only inbox dip)
2. Sales Nav Profile Scraper with the new Sales Nav cookies
3. SN Inbox Scraper with `PB_LI_SESSION_COOKIE` again

If phase 3 fails after phase 1 succeeded, the migration must use a
**separate LinkedIn account** for the Sales Nav cookies. STOP and escalate
to Mat — do not run `/sales-daily` until this is resolved.

## Rotation cadence

LinkedIn invalidates `li_at` and `li_a` periodically. Symptoms in production:

- `_pre_invite_degree_check` emits `degree_unknown` for 100% of prospects
- Sales Nav Profile Scraper container returns CSV with 0 rows
- `validate_sales_nav_health.py` returns `WARN: cookies expire in N days`
  or `FAIL: rotate cookies now`

Rotate proactively when WARN appears; don't wait for FAIL.

## Operator pre-flight (added in PR-B.7)

`scripts/validate_sales_nav_health.py` is wedged into `/sales-daily` Phase 0
so the operator sees cookie health (and "days until rotation due") **before**
approving any batch. If pre-flight returns FAIL mid-day, flip back to the
legacy backend via `.env`:

```
PRE_INVITE_DEGREE_CHECK_BACKEND=regular
```

…then run `/sales-daily` in a fresh shell. Rotate Sales Nav cookies the same
day; flip back to `sales_nav` after `validate_sales_nav_health.py` returns
OK.

## Recovery from orphaned probe state

The Attio contracts probe (`scripts/probe_pb_phantom_contracts.py`) creates a
temporary `__sales_nav_url_probe` attribute on the `people` object and
deletes it on success. If the probe crashed mid-run, the attribute may be
orphaned. Clean up with:

```
python3 scripts/probe_pb_phantom_contracts.py --cleanup-only
```
