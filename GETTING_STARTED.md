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

3. Provision the CRM schema Phase 0.6 writes to. On the bundled Attio adapter,
   run `python3 scripts/setup_attio_schema.py --feature phase06` (idempotent,
   safe to re-run; `--dry-run` previews). On a bring-your-own CRM, add the
   equivalent attributes by hand. Without them, every Phase 0.6 write 400s.
   The first three attributes below are written only by
   `workflows.detect_email_responses`; `email_last_resend_id` is stamped by the
   email lane (`workflows.email_campaign.run_email_daily`). The two terminal
   `email_campaign_stage` select options (`email_responded`,
   `email_not_interested`) are also seeded onto the existing
   `email_campaign_stage` attribute so the detector can flip a replier out of
   the drip.

| Object | Attribute | Type | Purpose |
|--------|-----------|------|---------|
| people | `email_response_classification` | select (`positive`/`question`/`neutral`/`negative`/`defensive`) | Classifier verdict for a detected inbound email reply. |
| people | `email_response_received_at` | datetime | Gmail internalDate of the reply; doubles as the one-detection-ever idempotency marker. |
| people | `last_email_response_text` | long_text | Reply body (quoted history stripped, truncated to 1000 chars). |
| people | `email_last_resend_id` | text | Resend message id of the most recent outreach email (forward-compat thread matching). |

Once the token exists and the schema is provisioned, Phase 0.6 runs inside the
daily flow. Set `OUTBOUND_DISABLE_EMAIL_RESPONSE_DETECTION=1` to force it off
even when a token is present.

### (Optional) Follow-up Radar

**Off by default.** The Follow-up Radar (PR-211/214/247) surfaces warm-but-stale
accounts — replied, call booked, demo'd, or open deal — that went quiet, ranks
them, and (in the skill layer) drafts review-ready follow-ups (never auto-sent).
A fresh install never touches its attributes and the `followup-*` commands
fail-closed-clean without them, so you can ignore this for a first run.

To enable it your CRM needs these attributes provisioned. On the bundled Attio
adapter, run `python3 scripts/setup_attio_schema.py --feature radar` (idempotent,
safe to re-run; `--dry-run` previews). On a bring-your-own CRM, add the
equivalent attributes by hand. Sole in-engine writer of every attribute below is
`workflows.followup_state`; `is_partner` is operator-seeded by hand.

| Object | Attribute | Type | Purpose |
|--------|-----------|------|---------|
| people (LinkedIn Outreach list) **and** deals | `followup_draft_at` | datetime | When a follow-up review-draft was last generated (a newer real touch supersedes it). |
| people **and** deals | `followup_draft_id` | text | Draft id of the last follow-up draft (dedup + digest link). |
| people **and** deals | `followup_snooze_until` | date | Radar skips the account until this date (inclusive). |
| people **and** deals | `followup_muted` | checkbox | Permanently exclude from the radar. |
| people **and** deals | `followup_callback_date` | date | Deferral tickler — suppress until this date, then hard-surface. |
| people **and** deals | `awaiting_reply_since` | date | Most recent unanswered send (WAITING lane; 60-day TTL). |
| people **and** deals | `awaiting_reply_thread_id` | text | Advisory email thread id of the unanswered send. |
| people **and** deals | `awaiting_reply_note_id` | text | Id of the one canonical waiting-context note per record. |
| people **and** deals | `awaiting_reply_nudge_count` | number | Nudge drafts this cycle (hard max 2). |
| deals | `referred_by` | text | Referring partner's canonical lowercase email. |
| deals | `last_verified_touch` | date | Skill-verified true last-touch (highest-precedence recency source). |
| people | `is_partner` | checkbox | Partner roster flag — **seeded manually by the operator**, no engine writes. |

Requires Gmail email-response detection (above) for the conversation-ledger
side; without it the radar still runs on CRM-resident signals and simply skips
the email sweep.

#### The 90-day Gmail conversation sweep

`scripts/gmail_sweep.py` is the radar's blind-spot cover: the engine only sees
accounts that have a CRM record, while the sweep reads the last 90 days of
Gmail directly and reports every live counterparty with its true recency and
direction-of-ball. Run it by hand or from your review skill; it prints one JSON
object on stdout (warnings go to stderr, so the output stays parseable) and
writes nothing anywhere.

```bash
python3 scripts/gmail_sweep.py
```

It needs the same `[gmail]` extra and read-only token as email-response
detection, plus your own mail domains — set `OUTBOUND_INTERNAL_DOMAINS`
(comma-separated) or let it fall back to the domain in `EMAIL_FROM` /
`EMAIL_REPLY_TO`. With neither, it exits with an error instead of guessing:
without knowing which addresses are yours it cannot tell a sent message from a
received one, and every thread would come back as "you owe a reply".

Two things worth knowing before you tune it:

- **Silencing is structural.** Robots, vendors and bulk senders are dropped
  from the sweep entirely — a silenced counterparty leaves no trace in the
  output. That is why the filter is an allow-list of ESP registrable domains
  rather than a shape rule: a corporate `mail.<brand>.com` and
  `mail.sendgrid.net` are indistinguishable by shape, and the cost of guessing
  is a hidden prospect. Add your own tools with
  `OUTBOUND_SWEEP_VENDOR_DOMAINS`, and only domains that can never be a
  counterparty.
- **Incomplete is reported, not hidden.** `sweep_complete: false` means an API
  error, a repeated page token, or the page cap stopped the sweep early and
  counterparties may be MISSING; `failed_thread_ids` names the threads that
  could not be fetched. Anything consuming the JSON should say so rather than
  present a partial sweep as a complete one.

### (Optional) Migration provenance back-pointers

**Off by default.** Only needed if you run the `scripts/migrate_*` or
`scripts/backfill_*` scripts. Each of those opens a `MigrationRunWriter`, which
stamps `last_migrated_by` on every record it modifies so you can trace a mutated
row back to the run that touched it. Without the attribute the stamp PATCH is
rejected: the migration itself still succeeds and exits 0, but every run prints
a back-pointer WARNING — a warning that always fires is a warning operators stop
reading, including the times it means a real forensics gap.

On the bundled Attio adapter, run `python3 scripts/setup_attio_schema.py
--feature provenance` (idempotent, safe to re-run; `--dry-run` previews). On a
bring-your-own CRM, add the equivalent attributes by hand. Sole writer:
`workflows.migration_run_writer.MigrationRunWriter`. Forward-only — records
modified before you provision this stay un-stamped.

| Object | Attribute | Type | Purpose |
|--------|-----------|------|---------|
| people | `last_migrated_by` | record reference → `migration_run` | Migration Run that last modified this person. |
| companies | `last_migrated_by` | record reference → `migration_run` | Migration Run that last modified this company. |

### (Optional) Botdog delivery transport

**Off by default, and it does not send.** PhantomBuster is the engine's only
wired send transport: every invite and DM goes out through the PB phantoms. The
repo also ships a complete, hardened surface for [Botdog](https://botdog.co) —
a REST client, a submission-only sender, a poll-based event-ingest drain, a
local submission ledger, an account-limit sync, and a never-contact blacklist
seeder — for operators who want to route sends through it themselves. No code
path in the engine routes a send to Botdog, and no `.env` value can make it.

What the flag actually does: `BOTDOG_SEND_ENABLED=true` turns on **Phase 0.7**,
a read-only event drain. It polls Botdog lead events so rows stamped
`send_channel=botdog` can absorb their confirming accept / DM-advance / reply
events. It never sends. Leave it off and the phase is a one-line skip; an
ordinary run then never needs `BOTDOG_API_KEY` or a `config/botdog.yaml`.
Both switches must be on: the drain also requires `enabled: true` in
`config/botdog.yaml`. `enabled: false` means every Botdog surface is inert, and
the drain then prints a skip line and polls nothing.

The `send_channel` stamp is the safety interlock. A row stamped `botdog` is
held **out** of PB invites and DMs *and* out of the Phase 0 / 0.5 scrape
detectors — it receives no outreach from any transport, and the run says so
loudly on every pass (per-step DM hold-out counts, an invite exclusion count,
and a residual census across all stages). That is deliberate: a prospect a
Botdog campaign still holds must never get a second first-touch from PB.
Re-stamp such rows `send_channel=pb` only after the Botdog campaigns are paused
and their leads removed. Missing/unset resolves to `pb`, so a fresh install is
unaffected.

To wire it up:

1. `cp config/botdog.example.yaml config/botdog.yaml`, fill in your campaign
   ids, set `enabled: true`. Validation is fail-loud — `enabled: true` with no
   campaigns, or with a `REPLACE_WITH_...` placeholder left in, raises at load
   time rather than submitting prospects into a campaign that does not exist.
2. Put `BOTDOG_API_KEY` in `.env`.
3. `python3 scripts/setup_attio_schema.py --feature botdog` (idempotent) to add
   the `send_channel` attribute.
4. **Seed the never-contact set before any Botdog send.**
   `python3 scripts/seed_botdog_blacklist.py` previews; `--apply` writes. Botdog
   inherits none of PhantomBuster's internal never-contact memory, so without
   this it can re-invite someone you already burned. Add any organisation you
   must never contact on any channel to `blacklist.denylist_companies` in
   `config/botdog.yaml` — those are seeded at any pipeline stage.

   The repo ships a pre-send gate for this,
   `workflows.daily_check_helpers.assert_botdog_blacklist_seeded`: it raises
   unless the collection exists and is non-empty
   (`BOTDOG_SKIP_BLACKLIST_CHECK=1` is a loud emergency override, never a
   silent one). **It is a helper you must wire in, not an automatic check.**
   Nothing in this engine calls it — the engine sends through PhantomBuster,
   and Phase 0.7 is a read-only drain that sends nothing. If you build a
   Botdog send path, call the gate yourself before the first send; otherwise
   the never-contact set is never verified.

| Object | Attribute | Type | Purpose |
|--------|-----------|------|---------|
| linkedin_outreach | `send_channel` | select (`pb` \| `botdog`) | Which transport owns this prospect's sends. Unset = `pb`. Read-only routing state — no engine code writes it. |

### (Optional) Pain-signal discovery lane

**Off by default, on two independent switches.** The normal supply lane is
`sales weekly`: a Sales Navigator saved search, scored against your ICP. The
pain-signal lane is a second, narrower source — it searches LinkedIn *posts*
for phrases your buyers actually use, then treats the people who **wrote**,
**commented on**, or **reacted to** a matching post as prospect candidates.
They go through the exact same qualify pipeline (ICP scoring, all four dedup
layers, the re-prospect review stage), and the lane **commits at Prospect stage
only** — it never sends anything.

Both switches must be on before a single scrape runs:

1. `OUTBOUND_PAIN_SIGNAL_ENABLED=1` in `.env` (strict — only the literal `1`).
2. `content/pain_keywords.json` must be **approved by you**. The shipped file
   is a placeholder: it carries the `REPLACE_THIS_TEMPLATE` sentinel and
   `status: "placeholder"`, and the lane refuses on *either* alone. Flipping
   the status without replacing the queries still refuses — otherwise you would
   be searching, and inviting off, the engine's own template text.

```bash
# preview — scrapes run (PhantomBuster spend) but nothing is written
OUTBOUND_PAIN_SIGNAL_ENABLED=1 python3 cli.py pain-signal --dry-run
```

Read the dry-run output before going wet: every candidate prints with the exact
invite note it would receive. `examples/acme/content/pain_keywords.json` is a
synthetic reference registry showing the file's shape.

Once enabled, the lane also runs as **Phase 0.9** inside `sales daily` — its
24-hour recency window pairs with the daily cadence, and running it inside the
daily lock is what serializes its enrichment scrape against the degree check
(they share a Google Sheet). Leave the flag unset and the phase is a one-line
skip; if the lane errors, the phase warns and the day's invites and DMs
continue unaffected.

Things worth understanding before you enable it:

- **The note makes a claim about the post.** A poster gets a note that
  references the post they wrote; a commenter or liker gets a note that says
  you crossed paths on it, and never that they wrote it. The lane enforces a
  **topic gate** — a post's people are accepted only when the post text
  literally contains an enabled query's phrase (accent- and case-folded,
  word-bounded; every term for a paired query). LinkedIn's "exact phrase"
  content search is not exact, and without this gate you would invite people
  off a work-anniversary post that merely shares vocabulary. The gate proves
  the *phrase* is present; it cannot prove your fixed note copy describes the
  post. Disable any query whose phrase stretches that claim.
- **Recency is client-side and fail-closed.** LinkedIn's `datePosted` search
  filter returns zero results, so the lane filters on each post's timestamp
  itself. A post whose timestamp cannot be parsed is DROPPED, never assumed
  fresh. The window is capped at 168h because the engagement note places the
  post inside the past week — a wider window refuses to run rather than ship a
  time overclaim.
- **Spend is bounded three ways**: only posts with non-zero engagement get an
  engager scrape, at most `max_engager_scrape_posts_per_run` posts get one per
  run, and the assembled batch is capped at `max_engagers_per_run` before the
  enrichment scrape — so one viral post cannot flood your Sales Navigator
  phantom. Three consecutive scrape failures trip a circuit breaker that stops
  further launches while keeping everything already harvested.
- **Your never-contact denylist applies at ingest.** Anything matching
  `blacklist.denylist_companies` in `config/botdog.yaml` is dropped before the
  preview renders and before any enrichment scrape is spent — the headline is
  checked too, since these candidates carry no company until enrichment.

To wire it up:

1. Replace every query in `content/pain_keywords.json` with phrases harvested
   from real buyer conversations, then set `_meta.status: "approved"`,
   `_meta.approved_by` and `_meta.approved_at`.
2. Clone a "post commenter and liker scraper" workflow in PhantomBuster and put
   its **worker** phantom ids — not the workflow parent's — in
   `config/phantombuster.yaml` under `agents.pain_posts_worker` /
   `pain_commenters_worker` / `pain_likers_worker` (or the matching `PB_PAIN_*`
   env vars). The parent is an orchestrator shell whose API launches are
   silent no-ops. Only the posts worker is required; leaving the other two
   blank runs the lane in posters-only mode, and it says so on every run.
3. `python3 scripts/setup_attio_schema.py --feature pain_signal` (idempotent)
   to add the five entry attributes. A wet run refuses before any write if
   they are missing.
4. Add the `pain_signal` group to your `content/messages.json` — a
   `connection_note_poster` (authorship frame) and a `connection_note_liker`
   (engagement frame), per language. A language with no pain copy falls back to
   your persona note, loudly.

| Object | Attribute | Type | Purpose |
|--------|-----------|------|---------|
| linkedin_outreach | `prospect_source` | select (`pain_signal`) | Which discovery lane sourced the prospect. NULL for weekly-search prospects. |
| linkedin_outreach | `pain_source_type` | select (`poster` \| `commenter` \| `liker`) | Relationship to the matched post — selects the note's reference frame. |
| linkedin_outreach | `pain_snippet` | text | Review context: the commenter's own comment, else the post's text (≤280 chars). |
| linkedin_outreach | `source_post_url` | text | The post that surfaced this prospect — verify the note's claim against it. |
| linkedin_outreach | `source_post_at` | text | ISO UTC timestamp of the matched post. |

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
| **Daily** (optional) | `sales pain-signal --dry-run` | Pain-signal discovery — OFF by default; also runs as Phase 0.9 inside `sales daily`. |
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
  The engine enforces cross-channel suppression on send; refuses to send live
  without a CAN-SPAM postal address (`EMAIL_PHYSICAL_ADDRESS`), a resolvable
  sender org, or a configured unsubscribe address; appends a footer with the
  address + a bilingual (EN/ES) opt-out line; and emits a `List-Unsubscribe`
  mailto header (one-click in Gmail/Outlook → your `EMAIL_UNSUBSCRIBE_MAILTO`
  inbox). It also refuses to send live if the local sent-ledger
  (`~/.outbound-agent/email_sent.json`) is corrupt, rather than treating an
  unreadable ledger as empty history and re-emailing the crash window.
  Separately, the drip senders (`email-daily`, `email-wave2`) are **disarmed by
  default** — set `OUTBOUND_EMAIL_ENABLED=1` to arm a live send.
  **Your responsibilities:** set the email env vars (`EMAIL_PHYSICAL_ADDRESS`,
  `EMAIL_FROM`, `EMAIL_SENDER_ORG`, `EMAIL_UNSUBSCRIBE_MAILTO`); **monitor the
  unsubscribe inbox** and run `sales email-unsubscribe <email>` to honor each
  opt-out promptly (it marks every person record sharing that address). **Optional
  operator infra for full automation** (not shipped — you stand it up): a hosted
  one-click unsubscribe HTTP endpoint (RFC 8058 `List-Unsubscribe-Post`) and a
  Resend bounce/complaint webhook that flips contacts to UNSUBSCRIBED. Until you
  wire those, the mailto + manual `email-unsubscribe` path is the supported,
  compliant flow. **Full posture + your obligations: [COMPLIANCE.md](COMPLIANCE.md)**
  (also [docs/LIMITATIONS.md](docs/LIMITATIONS.md)).
- **You are the data controller.** Prospect data, consent, opt-outs, and local
  outreach regulations are your responsibility.
