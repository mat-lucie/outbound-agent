# Getting started — your first run

This is the step-by-step guide to go from a fresh clone to your first (safe,
dry-run) outbound batch. Budget **60–90 minutes** the first time — most of it is
creating accounts and copying keys, not editing code.

> **What works today, honestly.** The smooth, fully-tested path is **Attio** as
> your CRM. "Bring your own CRM" (HubSpot, Pipedrive, …) works for the core
> read/write methods via the `/onboard` agent but has known rough edges — see
> [docs/LIMITATIONS.md](docs/LIMITATIONS.md). If you just want to try the engine,
> use Attio first.
>
> **This is a power tool, not a SaaS.** It automates LinkedIn outreach via
> PhantomBuster (which uses your LinkedIn session cookie) and can send email. You
> are responsible for how you use it — see [Safety & compliance](#safety--compliance)
> at the bottom **before** you send anything for real.

---

## 0. Before you start — what you'll need

| You need | Why | Cost |
|----------|-----|------|
| **Python 3.11+** | Runs the engine | free |
| **A CRM with an API** | The system of record for your pipeline. Attio is the bundled, tested choice. | Attio has a free tier; the operator-review queue features want a paid tier |
| **A PhantomBuster account** | Drives the LinkedIn automation (invites, DMs, inbox scraping). You'll create up to 4 "phantoms". | paid plan |
| **A LinkedIn account** | The account outreach is sent from. You'll copy its session cookie into PhantomBuster. | free seat works; Sales Navigator unlocks weekly prospecting from saved searches |
| **(Optional) Claude Code** | Needed for the guided `/onboard` setup **and** for the LLM "tiebreaker" on borderline prospects. The engine runs without it (manual config + deterministic scoring). | — |
| **(Optional) A Resend account** | Only for hot-lead alert + weekly report emails. The engine degrades gracefully without it. | free tier |

You do **not** need an Anthropic API key for the basic engine — the LLM
tiebreaker runs through a Claude Code session, not the SDK. Without it, prospects
in the borderline score band fall back to deterministic scoring.

---

## 1. Install

```bash
git clone <this-repo> outbound-agent
cd outbound-agent

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e '.[dev]'
```

Sanity check — the test suite should be green:

```bash
python -m pytest -q
# expect: all tests pass (3,400+ passed, ~15 skipped, 0 failed)
```

The CLI is now installed as `sales` (or run `python cli.py` directly).

---

## 2. Gather your credentials

Copy the example env file and fill it in. **Secrets only ever live in `.env`**
(which is git-ignored) — never in the committed `config/*.yaml` files.

```bash
cp .env.example .env
```

Fill these **required** values in `.env`:

| Variable | Where to get it |
|----------|-----------------|
| `ATTIO_API_KEY` | Attio → Settings → Developers → API keys (scopes: Records, List Entries, List Configuration, Notes — read/write) |
| `ATTIO_LIST_ID` | The ID of your pipeline list in Attio (the "LinkedIn Outreach" list) |
| `PHANTOMBUSTER_API_KEY` | PhantomBuster → Settings → API |
| `PB_LI_SESSION_COOKIE` | Your LinkedIn `li_at` session cookie (DevTools → Application → Cookies → linkedin.com). **Rotates periodically** — see [docs/runbooks/phantombuster-cookie-rotation.md](docs/runbooks/phantombuster-cookie-rotation.md). |
| `PB_LI_USER_AGENT` | Your browser's User-Agent string (must match the browser the cookie came from) |
| `PB_SEARCH_EXPORT_ID`, `PB_NETWORK_BOOSTER_ID`, `PB_MESSAGE_SENDER_ID`, `PB_PROFILE_SCRAPER_ID` | The phantom IDs from step 3 (you won't have these until you create the phantoms) |

Optional values (`RESEND_API_KEY`, `RESEND_FROM_ADDRESS`, `REPORT_EMAIL`, Sales
Navigator vars) can stay blank for a first run.

### (Optional) Gmail email-response detection

**Off by default.** When enabled, the engine reads your Gmail inbox (read-only)
to detect prospect *email* replies — powering follow-up-radar reply detection
and Phase 0.6 stage flips — so an email "No thanks" or "let's talk" stops or
advances the sequence just like a LinkedIn reply. Skip this for a first run;
the engine degrades gracefully without it (email detection simply doesn't run).

To turn it on:

1. Install the optional extra: `pip install -e '.[gmail]'` (pulls
   `google-api-python-client`; the core install stays lean without it).
2. Reuse the same Google OAuth client secret as Sheets
   (`credentials/google-oauth.json`), then mint a **separate**, read-only
   Gmail token once — an interactive browser consent:

   ```bash
   python -m clients.gmail
   ```

   This writes `credentials/gmail-authorized-user.json` (git-ignored, scope
   `gmail.readonly`). It stays independent of the Sheets token on purpose, so
   enabling Gmail never forces re-consent on the DM-send path.

If the token is absent or unreadable, the client raises
`GmailCredentialsMissing` and the email-detection path skips cleanly — never a
crash. Re-run `python -m clients.gmail` to re-enable.

---

## 3. Create the PhantomBuster phantoms

PhantomBuster runs the actual LinkedIn actions. You create a handful of
"phantoms" (pre-built automations) in your PB dashboard, connect them to your
LinkedIn cookie, and copy each phantom's ID back into `.env`.

**Follow the walkthrough: [docs/onboarding/phantombuster-setup.md](docs/onboarding/phantombuster-setup.md)** — it lists each phantom by its PhantomBuster store name and what to configure.

> Cookies expire roughly every ~2 weeks. When invites/DMs silently stop sending,
> the cookie is usually the culprit — re-capture it per the rotation runbook.

---

## 4. Configure the engine

Each config file has a committed template (`config/*.example.yaml`) and a
git-ignored live copy (`config/*.yaml`) that the engine actually reads. You have
two ways to produce the live copies:

### Path A — guided (recommended, requires Claude Code)

Open `skills/onboard/SKILL.md` in a **Claude Code session** and follow it. The
`/onboard` agent interviews you, writes `config/crm.yaml`, `config/icp.yaml`,
`config/phantombuster.yaml`, and (for non-Attio CRMs) generates + conformance-tests
a CRM adapter. This is the least error-prone path.

### Path B — manual

```bash
cp config/crm.example.yaml           config/crm.yaml
cp config/icp.example.yaml           config/icp.yaml
cp config/phantombuster.example.yaml config/phantombuster.yaml
cp config/outreach.example.yaml      config/outreach.yaml
```

Then edit each live copy:
- **`config/crm.yaml`** — `vendor: attio` and the credential env-var *names*
  (the defaults already point at `ATTIO_API_KEY` / `ATTIO_LIST_ID`).
- **`config/phantombuster.yaml`** — your phantom IDs (or leave them referencing
  the `.env` vars).
- **`config/icp.yaml`** — **your** ICP (see the critical step below).
- **`config/outreach.yaml`** — your operational knobs: daily invite/DM caps,
  batch size, DM + nurture cadence, lane priority, weekend send-days, throttle
  window. The shipped defaults are sane starting points — tune them to your
  sending reputation and list size. If you skip this, the engine runs on those
  defaults.

See [config/README.md](config/README.md) for the full convention.

---

## 5. ⚠️ Replace the shipped placeholder content (do this before any real send)

A fresh install ships **NEUTRAL placeholder content** in `content/` — the
persona/message/email files have the right keys and structure but the copy is a
`REPLACE_THIS_TEMPLATE` placeholder, not a real pitch. A **send gate** enforces
this: any **live** send (`sales daily`, `sales weekly`, `email-daily`,
`email-wave2`) is **BLOCKED** with a clear error while the placeholder sentinel
is still present. `--dry-run` is exempt so you can inspect the structure first.

You have two ways to supply real content:

**Option A — edit `content/` in place.** Replace, at minimum:

- **`config/icp.yaml`** — your real ICP. The shipped `config/icp.example.yaml`
  is a **neutral, generic-B2B** template, so if you skip this the engine scores
  prospects against placeholder criteria, not yours. See
  **`examples/acme/config/icp.yaml`** for a fully worked, opinionated reference
  ICP (the synthetic Acme example operator).
- **`content/messages.json`** — your LinkedIn DM sequence (keep the `[Name]` /
  `[Company]` tokens; remove every `REPLACE_THIS_TEMPLATE`).
- **`content/emails.json`** — your email templates (keep the `{{first_name}}` /
  `{{company}}` tokens; remove every `REPLACE_THIS_TEMPLATE`).
- **`content/personas.json`**, the `content/*-midmarket-targets.json` lists,
  **`content/synthetic_personas.json`**, **`content/evidence_refs.json`**, and
  **`sales-program.md`** — your segments, target lists, and outreach voice/rules.
- In `.env`: **`RESEND_FROM_ADDRESS`** and **`REPORT_EMAIL`**.

**Option B — point `OUTBOUND_CONTENT_DIR` at a filled-in directory.** Set the
env var to any directory that holds your own `messages.json`, `emails.json`,
`personas.json`, etc. The four content loaders honor it; absent the var they
read repo-root `content/`. The bundled **`examples/acme/content/`** is a
complete, synthetic filled-in set you can point at to see the gate clear:

```bash
export OUTBOUND_CONTENT_DIR="$PWD/examples/acme/content"
```

> Tip: `sales health-check` reports `Content: FAIL` while the placeholder
> sentinel is still present, so you find out before your first live run — not
> when the send gate trips.

---

## 6. Verify your setup

```bash
sales health-check
```

This checks CRM + PhantomBuster connectivity. Fix anything it flags before
moving on. (If you get a `ConfigError`, it names the missing key/file — usually a
blank `.env` value or a `config/*.yaml` you haven't copied yet.)

---

## 7. First run — always dry-run first

Dry-run prints exactly what *would* happen without sending anything. Always do
this before a live run.

```bash
sales weekly --dry-run     # prospecting: who would be scored + added to the pipeline
sales daily --dry-run      # cadence: who would get an invite (Part A) or a DM (Part B)
```

Read the output carefully: are the prospects on-ICP? Is the message copy yours
(not the bundled example copy)? Are the volumes sane? When it looks right, drop `--dry-run` to go
live.

---

## 8. The daily / weekly operating loop

Once you're live, this is the rhythm:

| When | Command | What it does |
|------|---------|--------------|
| **Weekly** | `sales weekly` | Pull new profiles (from a Sales Navigator saved search), score them against your ICP, queue passers. |
| **Weekly** | `sales sales-approve` | Review the borderline prospects the scorer wasn't sure about; approve/reject. |
| **Daily** | `sales daily` | Send the day's connection invites (Part A) + DMs to people who accepted (Part B). |
| **Daily** | `sales check-responses` | Detect replies and advance pipeline stages (fires hot-lead alerts if Resend is configured). |
| **Weekly** | `sales report --send` | Email a pipeline summary (omit `--send` to just print it). |
| **Periodically** | `sales learn` | Surface response patterns to inform ICP tuning. |

Run `sales <command> --help` for flags (e.g. `sales daily --batch-size 25`).

> **Weekends:** a common policy is invites-only on Sat/Sun (skip DMs). The script
> does not auto-gate this — it's an operator discipline.

---

## Where the engine keeps state

Run state (audit logs, run locks, caches) lives under **`~/.outbound-agent/`** in
your home directory. It's created automatically. (The directory name still
carries the original brand and is slated to be renamed; for now, that's where to
look when debugging a run.)

---

## Troubleshooting first-run issues

| Symptom | Likely cause |
|---------|--------------|
| `ConfigError: ... not set` | A required `.env` value is blank, or you haven't `cp`'d a `config/*.example.yaml` to its live `.yaml`. |
| `Unknown CRM vendor '...'` | A leftover/edited `config/crm.yaml` has a `vendor` the factory doesn't support. Set `vendor: attio`. |
| Invites/DMs silently stop sending | LinkedIn cookie expired — re-capture per the cookie-rotation runbook. |
| Health-check fails on PhantomBuster | Phantom IDs in `.env` don't match your PB dashboard, or the API key is wrong. |
| Borderline prospects all scored by "fallback" | Expected if you're not running a Claude Code LLM-dispatch session — deterministic scoring is the fallback. |

---

## Safety & compliance

Read this before sending anything for real:

- **LinkedIn automation violates LinkedIn's Terms of Service** and can get your
  account restricted or banned. The engine has per-day caps as a safety measure,
  but you accept that risk by running it.
- **The email path is compliance-*capable*, but you must configure + operate it.**
  The engine enforces cross-channel suppression on send, refuses to send live
  without `EMAIL_PHYSICAL_ADDRESS` (CAN-SPAM postal address), appends a footer
  with that address + an opt-out line, and emits a `List-Unsubscribe` mailto
  header (one-click in Gmail/Outlook → your `EMAIL_UNSUBSCRIBE_MAILTO` inbox).
  **Your responsibilities:** set the email env vars (`EMAIL_PHYSICAL_ADDRESS`,
  `EMAIL_FROM`, `EMAIL_UNSUBSCRIBE_MAILTO`); **monitor the unsubscribe inbox** and
  run `sales email-unsubscribe <email>` to honor each opt-out promptly. **Optional
  operator infra for full automation** (not shipped — you stand it up): a hosted
  one-click unsubscribe HTTP endpoint (RFC 8058 `List-Unsubscribe-Post`) and a
  Resend bounce/complaint webhook that flips contacts to UNSUBSCRIBED. Until you
  wire those, the mailto + manual `email-unsubscribe` path is the supported,
  compliant flow. **Full posture + your obligations: [COMPLIANCE.md](COMPLIANCE.md)**
  (also [docs/LIMITATIONS.md](docs/LIMITATIONS.md)).
- **You are the data controller.** Prospect data, consent, opt-outs, and local
  outreach regulations are your responsibility.
