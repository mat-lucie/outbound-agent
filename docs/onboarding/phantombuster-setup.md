# PhantomBuster setup (first-time operator runbook)

This walks a first-time operator from a fresh PhantomBuster account to a
working outbound engine: create the phantoms, capture their IDs into
`config/phantombuster.yaml`, pull the LinkedIn cookies + user-agent into
`.env`, set the API key, and pick the degree-check backend.

> **Config model.** Non-secret values (phantom IDs, the degree-check backend)
> live in `config/phantombuster.yaml`. Secrets (API key, cookies, user-agent)
> live in `.env`. The loader resolves each non-secret value **yaml → env var →
> default**, so you can configure either way — but the yaml is the recommended
> home for the IDs and the env is the only home for secrets. A missing
> `config/phantombuster.yaml` is fine; the engine falls back to the `PB_*` env
> vars.

---

## 0. Prerequisites

- A PhantomBuster account (any paid plan that allows the phantoms below).
- A LinkedIn account the phantoms will act as.
- For the `sales_nav` degree-check backend only: a LinkedIn **Sales Navigator**
  seat on that same account.

---

## 1. Get your PhantomBuster API key

1. PhantomBuster dashboard → **Settings → API**.
2. Copy the **API key**.
3. Put it in `.env` (copy `.env.example` to `.env` first if you haven't):

   ```
   PHANTOMBUSTER_API_KEY=<your-api-key>
   ```

The API key is a secret — it stays in `.env`, never in the yaml.

---

## 2. Create the phantoms and capture their IDs

Create each phantom below from the PhantomBuster phantom store. After creating
a phantom, find its **Phantom ID** — it's in the URL
(`.../phantoms/<ID>/...`) and under the phantom's **Settings**.

| Phantom (PB store name) | What it does | Daily/weekly stage |
| --- | --- | --- |
| **Sales Navigator Search Export** | Sales Nav search → CSV of profiles | weekly: prospect export |
| **LinkedIn Network Booster** | Sends connection invites | daily Part A |
| **LinkedIn Message Sender** | Sends DMs to 1st-degree connections | daily Part B |
| **LinkedIn Profile Scraper** | Connection-degree check (legacy backend) | daily pre-invite + Phase 0, `regular` backend |
| **Sales Navigator Profile Scraper** | Connection-degree check (Sales Nav backend) | daily pre-invite + Phase 0, `sales_nav` backend |
| **(Sales Nav) Inbox Scraper** | Detects message replies | daily Phase 0.5 |
| **Post commenter and liker scraper** (workflow) | Pain-signal discovery — OPTIONAL | daily Phase 0.9, `sales pain-signal` |

> The **Sales Navigator URL Converter** phantom is **not needed** — the Sales
> Nav Profile Scraper auto-converts `/in/<slug>` URLs internally. Leave its
> slot blank.

> **The pain-signal workflow is optional and off by default.** Skip it unless
> you are enabling the pain-signal lane (GETTING_STARTED.md → *(Optional)
> Pain-signal discovery lane*). If you do enable it, capture the **worker**
> phantom ids inside the workflow — the post extractor, the commenters worker
> and the likers worker — and NOT the workflow parent's id. The parent is an
> orchestrator shell: launching it via the API is a silent no-op that re-serves
> its cumulative leads database, and changing its launch type breaks every
> worker launch. Only the posts worker is required; blank commenters/likers
> ids run the lane in posters-only mode, announced on every run.

Now copy the template and paste each ID:

```
cp config/phantombuster.example.yaml config/phantombuster.yaml
```

Edit `config/phantombuster.yaml`:

```yaml
agents:
  search_export: "1234567890123456"
  network_booster: "1234567890123457"
  message_sender: "1234567890123458"
  profile_scraper: "1234567890123459"
  sales_nav_profile_scraper: "1234567890123460"   # MUST differ from profile_scraper
  sales_nav_url_converter: ""                       # not needed
  inbox_scraper: "1234567890123461"

  # Optional — only when the pain-signal lane is enabled. WORKER ids, never
  # the workflow parent's id.
  pain_posts_worker: ""            # required when the lane is on
  pain_commenters_worker: ""       # blank = posters-only, announced loudly
  pain_likers_worker: ""           # blank = posters-only, announced loudly
```

`config/phantombuster.yaml` is gitignored — your IDs stay local. (You can
alternatively set the matching `PB_*` env vars in `.env` instead of the yaml;
the yaml wins when both are present.)

> **Hard rule:** `sales_nav_profile_scraper` must be a **different** phantom ID
> than `profile_scraper`. The two emit different CSV schemas; the loader rejects
> an accidental duplicate to prevent the Sales Nav code path silently running
> the legacy scraper.

---

## 3. Capture the regular LinkedIn session cookie + user-agent

The phantoms authenticate with your LinkedIn `li_at` cookie and a matching
browser User-Agent. These are secrets — they go in `.env`.

1. Sign in to <https://www.linkedin.com> in Chrome (same account the phantoms
   run as).
2. DevTools → **Application → Cookies → `https://www.linkedin.com`** → find
   `li_at`, copy its **Value**.
3. Get your browser's User-Agent: DevTools **Console** → run
   `navigator.userAgent` → copy the string.
4. Paste both into `.env`:

   ```
   PB_LI_SESSION_COOKIE=<li_at value>
   PB_LI_USER_AGENT=<navigator.userAgent value>
   ```

If `PB_LI_USER_AGENT` is left blank, the engine falls back to a shipped default
Chrome UA — but matching your real browser reduces LinkedIn friction.

These cookies expire periodically. When a run starts failing auth, re-capture
`li_at`.

---

## 4. Choose the degree-check backend

The pre-invite degree check (and Phase 0 accepted-connection detection) can run
on either scraper. Set `pre_invite_degree_check_backend` in
`config/phantombuster.yaml`:

```yaml
pre_invite_degree_check_backend: regular   # or: sales_nav
```

- **`regular`** (default) — uses `profile_scraper` + `PB_LI_SESSION_COOKIE`.
  This is the simplest setup; if you're just getting started, leave it here.
- **`sales_nav`** — uses `sales_nav_profile_scraper` + the Sales Nav cookies
  (next section). Only flip to this after step 5 is done and the health check
  passes.

Rollback is "set it back to `regular` and re-run in a fresh shell" — the value
is read live on each run, no redeploy needed.

---

## 5. (Only for `sales_nav`) Capture the Sales Navigator cookies

The Sales Nav scraper needs a **separate** cookie pair so a rotation of the
Sales Nav session doesn't invalidate the regular phantoms. Full procedure +
the parallel-session caveat are in
[`docs/runbooks/phantombuster-cookie-rotation.md`](../runbooks/phantombuster-cookie-rotation.md).
Short version:

1. Sign in to <https://www.linkedin.com/sales/> in Chrome.
2. DevTools → **Application → Cookies → `https://www.linkedin.com`** → copy
   `li_at` and `li_a`.
3. Paste into `.env`:

   ```
   PB_LI_SALES_NAV_SESSION_COOKIE=<li_at from the Sales Nav session>
   PB_LI_SALES_NAV_LI_A_COOKIE=<li_a from the Sales Nav session>
   ```
4. Verify the cookies + phantom coexist:

   ```
   python3 scripts/validate_sales_nav_health.py
   ```

   Only flip `pre_invite_degree_check_backend` to `sales_nav` once this returns
   `OK`.

---

## 6. Verify

With `config/phantombuster.yaml` and `.env` filled in:

```bash
# Confirm the loader resolves your IDs + backend (no PB launch, no secrets shown):
python -c "from clients.pb_config import load_pb_config; c = load_pb_config(); \
print('search_export:', c.search_export_id); \
print('network_booster:', c.network_booster_id); \
print('backend:', c.degree_check_backend_raw)"

# Dry-run the daily flow (no live sends):
python cli.py daily --dry-run
```

A dry run prints `Skipping (no PB_*_ID set)` for any phantom whose ID is still
blank — fill those in and re-run.

---

## Related runbooks

- [`docs/runbooks/phantombuster-cookie-rotation.md`](../runbooks/phantombuster-cookie-rotation.md)
  — rotating the regular + Sales Nav cookies (~every 14 days), and the
  parallel-session invalidation risk.
- `config/README.md` — the yaml/env config convention (secrets stay in `.env`).
- `config/phantombuster.example.yaml` — the annotated template you copied in
  step 2.
