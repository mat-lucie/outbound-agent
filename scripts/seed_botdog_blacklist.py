#!/usr/bin/env python3
"""Seed Botdog's blacklist from the CRM's never-contact set.

# Why

Botdog inherits none of PhantomBuster's Network-Booster internal dedup
memory. Before the first Botdog send, Botdog must be told the full "never
(re)contact" set that lives partly in the CRM and partly in PB's opaque
state. A missed seed means Botdog could re-invite someone already burned,
or — worse — cold-contact an organisation the operator has hard-blocked.

# The never-contact set (see `classify_seed_category`)

From every ``linkedin_outreach`` list entry (INCLUDING merged-away losers
— they are the whole point), a row is seeded when it is anything other
than a fresh, still-contactable prospect:

  * ``denylist``   — the company (or person) matches the operator's
                     configured never-contact denylist
                     (``config/botdog.yaml`` →
                     ``blacklist.denylist_companies``). ALWAYS seeded, at
                     ANY stage (even PROSPECT / PARTNER_INTRO): a hard
                     block does not depend on where the row sits in the
                     funnel.
  * ``merged``     — ``merged_into`` is set (union-merge loser; the
                     winner carries its state). Never re-touch.
  * ``suppressed`` — ``suppress_re_engagement`` is set (cross-channel
                     "no" — the hard red line).
  * ``declined``   — stage in {Not Interested, Defensive Hold,
                     Unreachable}: said no, or undeliverable.
  * ``contacted``  — stage rank >= CONNECTION_SENT: ever invited / DMed /
                     responded / booked / qualified. Everyone already
                     touched.

A PROSPECT or PARTNER_INTRO row that is NOT denylisted / merged /
suppressed is a still-contactable lead (PROSPECT is exactly who Botdog
SHOULD reach; PARTNER_INTRO is partner-owned and never injected into a
Botdog campaign) — such rows are NOT seeded.

# Idempotency + safety

Rerunnable: the blacklist collection is fetched-or-created by name, and
URLs already present in it are skipped (read from the dedicated entries
endpoint). Batches are capped at ``MAX_LEADS_PER_BATCH`` (the client's
guard) and each batch's failure is recorded and surfaced — a partial
failure is LOUD and exits non-zero, never a silent partial seed.

Three fail-closed guards:

  * ``unresolved_identity`` — a row whose company AND person name are both
    empty was cleared past the denylist check on no evidence. Dry-run
    prints the bucket prominently; ``--apply`` REFUSES to run (exit 2)
    until it is empty. See ``UNRESOLVED_BUCKET``. ALWAYS ARMED — a
    denylist that is empty TODAY is one config edit away from being
    populated, and the rows this bucket names are unidentifiable either
    way; an operator must never learn the guard was dormant by
    discovering a hard-blocked company in a campaign.
  * ``skipped_no_url_denylist`` — a DENYLISTED row with no resolvable
    LinkedIn URL cannot be blacklisted at all (the blacklist keys on
    URL), so the hard block would be silently ABSENT from the seed.
    Counted separately from the benign ``skipped_no_url`` and refuses
    ``--apply`` (exit 2). See ``DENYLIST_NO_URL_BUCKET``.
  * duplicate-collection assertion — after a create, the collection must
    re-fetch to EXACTLY ONE match, else ``BlacklistResolutionError``
    (exit 2). A DTO shape-miss must not quietly mint a second collection
    holding half the never-contact set.

# Running

    # Preview (default, read-only — reads the CRM, writes NOTHING to Botdog):
    python scripts/seed_botdog_blacklist.py

    # Seed the Botdog blacklist (operator-run only):
    python scripts/seed_botdog_blacklist.py --apply

Constraint: ``--apply`` is the ONLY path that writes to Botdog. Default is
dry-run. Run this BEFORE any Botdog send.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clients.attio import AttioClient, _canonical_linkedin_url  # noqa: E402
from clients.botdog import (  # noqa: E402
    MAX_LEADS_PER_BATCH,
    BotdogClient,
    BotdogError,
    blacklist_name,
    select_blacklist,
)
from clients.botdog_config import load_botdog_config  # noqa: E402
from models.pipeline import STAGE_RANK, PipelineStage  # noqa: E402
from workflows.record_cache import (  # noqa: E402
    RecordCache,
    preload_pipeline_persons,
)

# Stages that mean "already contacted" — rank >= CONNECTION_SENT. PROSPECT
# and PARTNER_INTRO (rank 0) are the only never-contacted-by-us stages.
_CONTACTED_MIN_RANK = STAGE_RANK[PipelineStage.CONNECTION_SENT]

# Explicit-decline / undeliverable terminals (a subset of "contacted",
# surfaced as their own category so the breakdown separates "said no" from
# "in flight").
_DECLINE_STAGES: frozenset[str] = frozenset({
    PipelineStage.NOT_INTERESTED.value,
    PipelineStage.DEFENSIVE_HOLD.value,
    PipelineStage.UNREACHABLE.value,
})

# Category order is also precedence order for `classify_seed_category`.
CATEGORIES = ("denylist", "merged", "suppressed", "declined", "contacted")

# Fail-closed bucket. The denylist hard block keys on the person record's
# company/name — so a row whose company AND name both resolve to
# None/empty cannot be checked against it at all. Such a row is not
# "clean", it is UNKNOWN: it could be a denylisted organisation. `--apply`
# refuses to run while this bucket is non-empty, so the operator triages
# the rows (fix the CRM record, or confirm the person) instead of seeding
# a set that silently omits a hard-blocked company.
#
# ALWAYS ARMED — never gated on whether a denylist is configured. Gating
# it means the default install (no `denylist_companies`) runs the guard
# dormant forever and `--apply` never refuses, so the first operator to
# add a denylist entry inherits a seed built while the check was off.
# An unidentifiable row is unidentifiable regardless of config.
UNRESOLVED_BUCKET = "unresolved_identity"

# Second fail-closed bucket: a row classified `denylist` whose LinkedIn
# URL is unresolvable. The Botdog blacklist keys on URL ONLY, so this row
# CANNOT be blacklisted — the operator's hard block would be silently
# missing from the seed while the run reported success. Kept out of the
# generic `skipped_no_url` counter (which is benign: those rows are merely
# already-contacted) so a hard block never hides inside a routine number.
DENYLIST_NO_URL_BUCKET = "skipped_no_url_denylist"

# Cap on the sample of unresolved record ids printed for triage.
UNRESOLVED_SAMPLE_LIMIT = 10


@dataclass(frozen=True)
class SeedLead:
    """One never-contact lead destined for the Botdog blacklist."""

    canonical_url: str
    category: str
    record_id: str


def denylist_tokens() -> tuple[str, ...]:
    """The operator's configured never-contact name tokens (lowercased)."""
    return load_botdog_config().denylist_company_tokens


def matches_denylist(
    company: str | None, name: str | None, tokens: tuple[str, ...]
) -> bool:
    """True when the company (or, defensively, the person name) matches a
    configured never-contact token.

    Substring match on lowercased tokens — a denylisted row is seeded
    regardless of pipeline stage (the hard block).
    """
    if not tokens:
        return False
    haystacks = [h.lower() for h in (company, name) if h]
    return any(token in hay for hay in haystacks for token in tokens)


def _stage_rank(stage: object) -> int | None:
    """Rank for a raw stage string, or None if unresolvable."""
    if not isinstance(stage, str) or not stage:
        return None
    try:
        return STAGE_RANK[PipelineStage(stage)]
    except (ValueError, KeyError):
        return None


def classify_seed_category(
    attrs: dict,
    company: str | None,
    name: str | None,
    tokens: tuple[str, ...] = (),
) -> str | None:
    """Return the never-contact category for a parsed entry, or None when
    the row is a still-contactable prospect (not seeded).

    Precedence (a row can match several signals): denylist > merged >
    suppressed > declined > contacted. The denylist is checked FIRST and
    ignores stage entirely — a hard block never yields to funnel position.
    """
    if matches_denylist(company, name, tokens):
        return "denylist"
    if attrs.get("merged_into"):
        return "merged"
    if attrs.get("suppress_re_engagement"):
        return "suppressed"
    stage = attrs.get("stage")
    if isinstance(stage, str) and stage in _DECLINE_STAGES:
        return "declined"
    rank = _stage_rank(stage)
    if rank is not None and rank >= _CONTACTED_MIN_RANK:
        return "contacted"
    return None


def _row_url(attrs: dict, linkedin_url: str | None) -> str:
    """Best LinkedIn URL for a row: the entry's canonical URL when present,
    else the person record's URL. Returns canonical form (empty string when
    neither is usable)."""
    raw = attrs.get("canonical_linkedin_url") or linkedin_url or ""
    return _canonical_linkedin_url(str(raw))


def collect_seed_leads(
    entries_parsed: list[dict],
    record_info_by_id: dict[str, tuple[str | None, str | None, str]],
    tokens: tuple[str, ...] = (),
) -> tuple[list[SeedLead], dict[str, int], list[str]]:
    """Pure core: map parsed entries + resolved person info to the deduped
    seed-lead list, a category breakdown, and the fail-closed
    unresolved-identity record ids.

    ``record_info_by_id`` maps record_id → (name, company, linkedin_url).
    Rows with no resolvable LinkedIn URL are counted under
    ``breakdown["skipped_no_url"]`` (they cannot be blacklisted by URL —
    surfaced, never silently dropped), EXCEPT denylisted ones, which get
    their own ``breakdown[DENYLIST_NO_URL_BUCKET]``: an unblacklistable
    hard block is a refusal, not a routine skip. Dedup is by canonical
    URL; the first-seen category wins.

    Third return value = record ids of rows this function decided NOT to
    seed while BOTH their company and name were empty (see
    ``UNRESOLVED_BUCKET``). Scoped to the not-seeded rows on purpose: a row
    that IS seeded lands on the blacklist whatever its identity, so its
    blank company changes no outcome — but a not-seeded row with no
    identity was cleared past the denylist check on no evidence. Armed
    unconditionally, `tokens` or not (see ``UNRESOLVED_BUCKET``).
    """
    breakdown: dict[str, int] = {c: 0 for c in CATEGORIES}
    breakdown["skipped_no_url"] = 0
    breakdown[DENYLIST_NO_URL_BUCKET] = 0
    seen: set[str] = set()
    leads: list[SeedLead] = []
    unresolved: list[str] = []
    for attrs in entries_parsed:
        record_id = str(attrs.get("record_id") or "")
        name, company, linkedin_url = record_info_by_id.get(
            record_id, (None, None, "")
        )
        category = classify_seed_category(attrs, company, name, tokens)
        if category is None:
            if not (company or "").strip() and not (name or "").strip():
                # Fall back to the entry id when the row carries no
                # record_id: an empty string in the triage sample gives the
                # operator nothing to look up, which makes the fail-closed
                # refusal unactionable for exactly the row it is about.
                unresolved.append(
                    record_id
                    or str(attrs.get("entry_id") or "")
                    or "<unidentified row>"
                )
            continue
        url = _row_url(attrs, linkedin_url)
        if not url:
            # A denylisted row without a URL is UNBLACKLISTABLE — the hard
            # block simply would not exist in the seed. Its own counter,
            # its own refusal (see DENYLIST_NO_URL_BUCKET).
            bucket = (
                DENYLIST_NO_URL_BUCKET
                if category == "denylist"
                else "skipped_no_url"
            )
            breakdown[bucket] += 1
            continue
        if url in seen:
            continue
        seen.add(url)
        breakdown[category] += 1
        leads.append(
            SeedLead(canonical_url=url, category=category, record_id=record_id)
        )
    return leads, breakdown, unresolved


def resolve_record_info(
    attio: AttioClient, record_ids: set[str]
) -> dict[str, tuple[str | None, str | None, str]]:
    """Bulk-resolve (name, company, linkedin_url) for every record_id.

    Needed for ALL rows (not just contacted ones) because the denylist
    block keys on company and must catch a denylisted row at any stage.
    Uses the same bulk preload the daily run uses.
    """
    cache = RecordCache(attio)
    preload_pipeline_persons(attio, cache, {r for r in record_ids if r})
    info: dict[str, tuple[str | None, str | None, str]] = {}
    for record_id in record_ids:
        if not record_id:
            continue
        name, company, linkedin_url, _industry, _title = cache.get(record_id)
        info[record_id] = (name, company, linkedin_url)
    return info


def _blacklist_id(blacklist: dict) -> str:
    """Extract the id from a blacklist-collection dict (shape unverified —
    try the common keys)."""
    for key in ("id", "blacklistId", "blacklist_id", "_id"):
        value = blacklist.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            inner = value.get("id") or value.get("blacklist_id")
            if isinstance(inner, str) and inner:
                return inner
    return ""


class BlacklistResolutionError(RuntimeError):
    """The named blacklist collection could not be resolved to exactly one.

    Raised after a create that did not converge. Carries a raw-response
    snippet so the operator can see the shape the API actually returned.
    """


def _matching_blacklists(blacklists: list[dict], name: str) -> list[dict]:
    """Every collection whose name matches ``name`` (case-insensitive)."""
    return [
        bl for bl in blacklists
        if isinstance(bl, dict)
        and isinstance(bl.get("name"), str)
        and bl["name"].strip().lower() == name.strip().lower()
    ]


def resolve_blacklist(botdog: BotdogClient, name: str) -> tuple[str, set[str]]:
    """Fetch-or-create the named blacklist collection.

    Returns ``(blacklist_id, already_present_canonical_urls)``. The
    already-present set is read from the dedicated entries endpoint
    (``get_blacklist_leads``); ``GET /v1/blacklist`` does not embed
    entries. A read failure degrades to an empty set — re-adding is safe
    (Botdog dedups), we just cannot report skips.

    DUPLICATE-SAFE RESOLUTION: an empty duplicate of the seeded collection
    exists in the wild. On the existing path ``select_blacklist`` picks the
    POPULATED collection by max ``leadCount`` — so the seed re-adds INTO
    the same collection the pre-send gate reads, never into a stray empty
    duplicate.

    POST-CREATE ASSERTION: the create response DTO is unverified, so
    ``_blacklist_id`` could mis-read it and every run would then create
    ANOTHER collection — each seeding a fraction of the never-contact set
    while the script reported success, and the gate in
    ``daily_check_helpers`` would pass on whichever one it found first. So
    after a create, re-fetch and require EXACTLY ONE match; zero (the
    create silently did nothing) or several (we just made a duplicate)
    raise ``BlacklistResolutionError`` with the raw snippet.
    """
    existing = select_blacklist(botdog.get_blacklists(), name)
    if existing is not None:
        blacklist_id = _blacklist_id(existing)
        return blacklist_id, _fetch_existing_urls(botdog, blacklist_id)

    created = botdog.create_blacklist(name)
    matches = _matching_blacklists(botdog.get_blacklists(), name)
    if len(matches) != 1:
        raise BlacklistResolutionError(
            f"blacklist {name!r} did not resolve to exactly one collection "
            f"after create — found {len(matches)}. "
            + (
                "The create reported success but nothing landed (or the "
                "listing does not show it): seeding would write into a "
                "collection nobody reads."
                if not matches else
                "DUPLICATE collections now exist: a partial never-contact "
                "set in each, and the pre-send gate would pass on the "
                "wrong one. Delete the extras in the Botdog UI, then "
                "re-run."
            )
            + f" Raw create response: {str(created)[:300]}"
        )
    blacklist_id = _blacklist_id(matches[0])
    return blacklist_id, _fetch_existing_urls(botdog, blacklist_id)


def _fetch_existing_urls(botdog: BotdogClient, blacklist_id: str) -> set[str]:
    """Canonical URLs already in a blacklist collection, read from the
    dedicated entries endpoint (``GET /v1/blacklist/{id}/leads``).

    Entries carry the URL under ``linkedinProfile``; the other spellings
    are tried defensively. A ``BotdogError`` degrades to an empty set (LOUD
    warning) rather than aborting the seed: re-adding is safe (Botdog
    dedups), so a failed read only disables the already-present skip for
    this run — the never-contact set still lands.
    """
    try:
        leads = botdog.get_blacklist_leads(blacklist_id)
    except BotdogError as exc:
        print(
            f"  ⚠ could not read existing blacklist entries "
            f"[{type(exc).__name__}: {exc}] — already-present skip disabled "
            f"this run (re-adds are safe, Botdog dedups).",
            file=sys.stderr,
        )
        return set()
    urls: set[str] = set()
    for lead in leads:
        if not isinstance(lead, dict):
            continue
        raw = (
            lead.get("linkedinProfile")
            or lead.get("linkedinUrl")
            or lead.get("linkedin_url")
            or ""
        )
        canon = _canonical_linkedin_url(str(raw))
        if canon:
            urls.add(canon)
    return urls


def _chunks(items: list[SeedLead], size: int) -> list[list[SeedLead]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _ascii_safe_url(url: str) -> str:
    """Percent-encode any non-ASCII characters in a profile URL's path.

    Already-ASCII URLs (the overwhelming majority) pass through unchanged;
    percent-signs are kept so an already-encoded URL is not double-encoded.
    """
    if url.isascii():
        return url
    from urllib.parse import quote

    return quote(url, safe=":/%-_.~?=&")


def apply_seed(
    botdog: BotdogClient, blacklist_id: str, leads: list[SeedLead]
) -> tuple[int, list[dict]]:
    """Add ``leads`` to the blacklist in <= MAX_LEADS_PER_BATCH batches.

    Returns ``(added_count, failures)``. A batch failure is recorded and
    the loop CONTINUES (other batches still land); the caller exits
    non-zero on any failure so a partial seed is never mistaken for a
    complete one.
    """
    added = 0
    failures: list[dict] = []
    for batch in _chunks(leads, MAX_LEADS_PER_BATCH):
        # Percent-encode non-ASCII vanity slugs (accents, emoji): Botdog's
        # URL validator 400s the ENTIRE batch on one raw-unicode URL, while
        # the percent-encoded form is accepted.
        payload = [
            {"linkedinUrl": _ascii_safe_url(lead.canonical_url)}
            for lead in batch
        ]
        try:
            botdog.add_to_blacklist(blacklist_id, payload)
            added += len(batch)
        except BotdogError as exc:
            failures.append({
                "batch_size": len(batch),
                "first_url": batch[0].canonical_url if batch else "",
                "error": f"{type(exc).__name__}: {exc}",
            })
            print(
                f"  ❌ blacklist batch of {len(batch)} FAILED "
                f"({type(exc).__name__}: {exc}) — RE-RUN to converge; the "
                f"seed is INCOMPLETE.",
                file=sys.stderr,
            )
    return added, failures


def _print_preview(leads: list[SeedLead], breakdown: dict[str, int]) -> None:
    print(
        f"Never-contact seed set: {len(leads)} unique profile(s) "
        f"({ {k: v for k, v in breakdown.items() if v} })"
    )
    for lead in leads[:10]:
        print(f"  - [{lead.category}] {lead.canonical_url}")
    if len(leads) > 10:
        print(f"  ... and {len(leads) - 10} more")
    if breakdown.get(DENYLIST_NO_URL_BUCKET):
        # Rendered on its own line, in stderr, never folded into the
        # counters dict above: this one is a refusal, not a statistic.
        print(
            f"  ⚠ {breakdown[DENYLIST_NO_URL_BUCKET]} DENYLISTED row(s) "
            f"had no resolvable LinkedIn URL — they CANNOT be blacklisted "
            f"(the Botdog blacklist keys on URL), so the operator's hard "
            f"block would be MISSING from this seed.",
            file=sys.stderr,
        )


def _print_unresolved(unresolved: list[str]) -> None:
    """Print the fail-closed unresolved-identity bucket, prominently."""
    print(
        f"\n{'=' * 68}\n"
        f"⚠ {UNRESOLVED_BUCKET.upper()}: {len(unresolved)} row(s) were "
        f"NOT seeded while BOTH their company and person name were empty.\n"
        f"  The never-contact denylist keys on company/name, so these rows "
        f"were cleared past it on NO evidence — any one of them could be a "
        f"denylisted organisation.\n"
        f"  Triage each in the CRM (fill the person record, or confirm the "
        f"company) and re-run. --apply stays REFUSED until this bucket is "
        f"empty.\n"
        f"  Sample record ids: "
        f"{unresolved[:UNRESOLVED_SAMPLE_LIMIT]}"
        + (
            f" ... and {len(unresolved) - UNRESOLVED_SAMPLE_LIMIT} more"
            if len(unresolved) > UNRESOLVED_SAMPLE_LIMIT else ""
        )
        + f"\n{'=' * 68}",
        file=sys.stderr,
    )


def seed(
    attio: AttioClient,
    botdog: BotdogClient | None,
    *,
    dry_run: bool,
    blacklist_id: str | None = None,
) -> dict:
    """Orchestrate: read the CRM → build the seed set → (apply) → report.

    ``blacklist_id``: outage bypass for when ``GET /blacklist`` is failing
    server-side while POST works. When given, skip the GET-based
    ``resolve_blacklist`` entirely and seed straight into that collection
    id. The already-present skip degrades to empty — re-adding is safe,
    Botdog dedups — and the exactly-one assertion is deliberately skipped:
    an operator-supplied id IS the resolution.
    """
    name = blacklist_name()
    tokens = denylist_tokens()
    list_id = os.environ.get("ATTIO_LIST_ID", "")
    raw = attio.query_list_entries(list_id=list_id)
    # Parse EVERY entry — unlike the send path, merged-away losers are kept
    # (they are exactly what must never be re-contacted).
    entries_parsed = [AttioClient.parse_entry(entry) for entry in raw]
    record_ids = {str(a.get("record_id") or "") for a in entries_parsed}
    record_info = resolve_record_info(attio, record_ids)
    leads, breakdown, unresolved = collect_seed_leads(
        entries_parsed, record_info, tokens
    )

    _print_preview(leads, breakdown)

    report: dict = {
        "dry_run": dry_run,
        "entries_scanned": len(entries_parsed),
        "seed_size": len(leads),
        "breakdown": {k: v for k, v in breakdown.items() if v},
    }
    # Fail-closed buckets: each one means the seed WOULD be incomplete in a
    # way the operator cannot see from a success message. Both are reported
    # under dry-run and both refuse `--apply`.
    refusals: list[str] = []
    if unresolved:
        _print_unresolved(unresolved)
        report[UNRESOLVED_BUCKET] = {
            "count": len(unresolved),
            "sample_record_ids": unresolved[:UNRESOLVED_SAMPLE_LIMIT],
        }
        refusals.append(UNRESOLVED_BUCKET)
    denylist_no_url = breakdown.get(DENYLIST_NO_URL_BUCKET, 0)
    if denylist_no_url:
        report[DENYLIST_NO_URL_BUCKET] = denylist_no_url
        refusals.append(DENYLIST_NO_URL_BUCKET)

    if refusals and not dry_run:
        # An unresolvable row could be denylisted; an unblacklistable
        # denylisted row IS one. Either way the operator triages before
        # anything is written to Botdog.
        print(
            f"\n❌ REFUSING to --apply: fail-closed bucket(s) "
            f"{', '.join(refusals)} are non-empty. Triage them first "
            f"(above), then re-run. NOTHING was written to Botdog.",
            file=sys.stderr,
        )
        report["refused"] = ", ".join(refusals)
        report["added"] = 0
        report["skipped_already_present"] = 0
        report["failures"] = []
        return report

    if dry_run or botdog is None:
        print(
            f"\nDRY-RUN: would seed {len(leads)} profile(s) into {name!r}. "
            f"Re-run with --apply to write to Botdog."
        )
        report["added"] = 0
        report["skipped_already_present"] = 0
        report["failures"] = []
        return report

    if blacklist_id:
        already_present: set[str] = set()
        print(
            f"\n⚠ --blacklist-id bypass: seeding directly into "
            f"{blacklist_id!r} without GET-based resolution (already-"
            f"present skip disabled; Botdog-side dedup covers re-adds)."
        )
    else:
        blacklist_id, already_present = resolve_blacklist(botdog, name)
    to_add = [
        lead for lead in leads if lead.canonical_url not in already_present
    ]
    skipped = len(leads) - len(to_add)
    print(
        f"\nBlacklist {name!r} (id={blacklist_id!r}): "
        f"{len(already_present)} already present, seeding {len(to_add)} new "
        f"(skipping {skipped} already-blacklisted)."
    )
    added, failures = apply_seed(botdog, blacklist_id, to_add)
    report["blacklist_id"] = blacklist_id
    report["added"] = added
    report["skipped_already_present"] = skipped
    report["failures"] = failures
    print(
        f"\nSeeded {added}/{len(to_add)} new profile(s)"
        + (f" — {len(failures)} batch(es) FAILED, seed INCOMPLETE."
           if failures else " — seed complete.")
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--apply", action="store_true",
        help="Live run: write the seed to Botdog's blacklist. Default is dry-run.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Explicit dry-run (default; equivalent to omitting --apply).",
    )
    parser.add_argument(
        "--blacklist-id", default=None,
        help=(
            "Outage bypass: seed directly into this Botdog collection id, "
            "skipping GET /blacklist resolution (for when the GET 504s "
            "server-side while POST works). Re-adds are safe (Botdog "
            "dedups)."
        ),
    )
    args = parser.parse_args(argv)

    if args.apply and args.dry_run:
        print(
            "error: pass either --apply or --dry-run, not both "
            "(refusing to guess — --apply would silently win).",
            file=sys.stderr,
        )
        return 2

    dry_run = not args.apply

    try:
        attio = AttioClient()
    except KeyError:
        print("error: ATTIO_API_KEY env var not set", file=sys.stderr)
        return 2

    botdog: BotdogClient | None = None
    if not dry_run:
        try:
            botdog = BotdogClient()
        except KeyError:
            print("error: BOTDOG_API_KEY env var not set", file=sys.stderr)
            return 2

    try:
        report = seed(
            attio, botdog, dry_run=dry_run, blacklist_id=args.blacklist_id
        )
    except BlacklistResolutionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        if botdog is not None:
            botdog.close()

    print("\n" + json.dumps(report, indent=2))
    if report.get("refused"):
        return 2
    return 1 if report.get("failures") else 0


if __name__ == "__main__":
    raise SystemExit(main())
