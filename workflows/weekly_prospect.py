"""Weekly prospecting workflow: export, qualify, and load new prospects."""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, TypedDict

import click
import httpx

from clients.attio import AttioClient, _canonical_linkedin_url, first_option_title
from clients.pb_config import li_session_cookie, li_user_agent_raw
from models.business_calendar import add_business_days
from models.campaign import load_personas
from models.experiment import get_current_experiment_id
from models.pipeline import PipelineStage
from workflows.cadence import derive_cadence_lane
from workflows.company_matcher import (
    extract_real_domain,
    find_company_record,
    match_or_create_company,
    normalize_company_name,
)
from workflows.escalation import escalate
from workflows.identity_match import normalize_for_match
from workflows.industry_classifier import build_anthropic_client
from workflows.quality_gate import (
    DECISION_MAKER_ROLE_CREDIT,
    DETERMINISTIC_PASS_PATHS,
    DETERMINISTIC_PASS_THRESHOLD,
    DISQUALIFIER_VERDICT_PATHS,
    INDUSTRY_BONUS_IN_ICP,
    NON_COMPETITOR_CREDIT,
    score_band,
    score_prospect,
)

if TYPE_CHECKING:
    # Annotation-only: the CRM seam dataclasses + the PB client type. With
    # `from __future__ import annotations` every annotation is a string, so
    # these never need to exist at runtime in this module.
    from clients.crm.base import CRMProvider, Entry
    from clients.phantombuster import PhantomBusterClient

logger = logging.getLogger(__name__)

# Default fresh-prospect quarantine: a newly-committed prospect cannot
# receive its first invite for this many business days. Two days is the
# operator-review window — long enough that a human can flip
# stage→PARTNER_INTRO or NOT_INTERESTED before any outbound fires, short
# enough that the daily run keeps a healthy invite queue. Override via
# OUTBOUND_PROSPECT_QUARANTINE_BDAYS.
PROSPECT_QUARANTINE_BUSINESS_DAYS_DEFAULT = 2

EXPORTS_DIR = Path("exports")

# Runtime content directory — see models/campaign.py for the OUTBOUND_CONTENT_DIR
# contract. Defaults to the repo-root `content/` when the env var is unset.
CONTENT_DIR = Path(
    os.environ.get("OUTBOUND_CONTENT_DIR")
    or (Path(__file__).resolve().parent.parent / "content")
)


def _attio_inner_client(crm: CRMProvider) -> AttioClient:
    """Resolve the raw ``AttioClient`` behind a provider (Attio escape hatch).

    Used at every §7 boundary where a migrated command tree must hand off to
    an UNMIGRATED Attio-only call path that still requires the concrete client
    — either because it reads/writes raw vendor shapes (e.g.
    ``industry_classifier.backfill_missing_industries`` via raw
    ``record["values"]`` + ``update_company``; the daily send loops via
    ``AttioClient.parse_entry`` / ``_person_to_company``) or because it depends
    on transport semantics the contract does not model (e.g.
    ``daily_run.open_daily_run``'s non-retrying ``_client.request`` +
    ``ConcurrentRunInAttio`` collision exception). Mirrors ``AttioWriter``'s
    ``getattr(self._crm, "inner_client", None)`` pattern.

    The provider's ``inner_client`` property is the Attio-specific handle
    (deliberately NOT on the ``CRMProvider`` ABC); a non-Attio provider has no
    such handle, so this raises a clear error rather than silently mis-routing
    the unmigrated Attio-only helper. These call sites are tracked debt the
    vendor-neutral exception-model increment will delete (it will also relocate
    this helper to a neutral home — it currently lives here only because the
    weekly slice defined it first; cli.py + daily_check.py import it from here).
    """
    inner = getattr(crm, "inner_client", None)
    if inner is None:
        raise TypeError(
            "An unmigrated Attio-only call path requires the raw AttioClient "
            "escape hatch, but the configured CRMProvider "
            f"({type(crm).__name__}) exposes no `inner_client`. The bundled "
            "Attio adapter (AttioProvider) provides one; a non-Attio provider "
            "must migrate the remaining Attio-coupled call paths first."
        )
    return inner


def _check_all_persona_target_lists_fresh(
    personas_data: dict,
    crm: CRMProvider,
) -> None:
    """Step 0 of the weekly batch — iterate persona target lists and
    fail-loud on any file >60d old.

    Post-QA convergence (silent-failure B-2 + pr-test I-2): COLLECTS
    all stale results across personas, writes a queue row per stale
    file via `check_target_list_freshness`, then raises a SINGLE
    `WeeklyTargetListStaleError` carrying the full set. Operators
    see all stale lists in one terminal error and refresh them in
    one cycle rather than discovering them one-at-a-time across N
    weekly invocations.

    Personas without a `target_company_list` config (legacy enterprise
    personas) are skipped silently — they're not file-backed.

    Missing target file is a different failure class (operator
    misconfig); skipped here so the existing downstream
    `_load_target_company_names` empty-set handling applies.

    Path containment (GTM QA fold): asserts the resolved path stays
    under `CONTENT_DIR`. Catches `target_company_list` typos that
    inject `..` traversal — those are operator-config bugs that
    should fail loud, not silently load a wrong file.
    """
    from models.business_calendar import operator_today
    from models.freshness import FreshnessStatus, WeeklyTargetListStaleError
    from workflows.target_freshness import check_target_list_freshness

    today = operator_today()
    seen_paths: set[Path] = set()
    stale_results: list = []
    for _persona_key, persona in (personas_data or {}).items():
        # Paused personas (active: false) are excluded from the weekly
        # run entirely, so their backing target lists must not gate the
        # batch — a stale list for a lane we're not harvesting should
        # never halt the lanes we are. Mirrors the same guard in
        # `_get_all_searches` so both chokepoints agree on what's live. (PR-226)
        if not persona.get("active", True):
            continue
        target_list_key = persona.get("target_company_list", "")
        if not target_list_key:
            continue
        target_path = CONTENT_DIR / f"{target_list_key}-targets.json"
        # Path containment guard (GTM QA fold).
        resolved = target_path.resolve()
        if not resolved.is_relative_to(CONTENT_DIR.resolve()):
            raise ValueError(
                f"target_company_list {target_list_key!r} resolves outside "
                f"CONTENT_DIR ({resolved} not under {CONTENT_DIR.resolve()})"
            )
        if target_path in seen_paths:
            continue
        seen_paths.add(target_path)
        if not target_path.exists():
            continue
        try:
            check_target_list_freshness(target_path, today, attio=crm)
        except WeeklyTargetListStaleError as err:
            # Multi-STALE collection. The freshness function already
            # wrote the queue row before raising; we just accumulate
            # the result and continue iterating so the operator gets
            # full coverage in one cycle.
            stale_results.extend(err.results)
            # Sanity: every accumulated result must be STALE.
            assert all(
                r.status is FreshnessStatus.STALE for r in stale_results
            )

    if stale_results:
        raise WeeklyTargetListStaleError(stale_results)


def _load_target_company_names(target_list_key: str) -> set[str]:
    """Load normalized company name fragments from a targets JSON file.

    Splits names on '/' and parentheses to capture all variants.
    Returns lowercase fragments for substring matching.
    """
    targets_file = CONTENT_DIR / f"{target_list_key}-targets.json"
    if not targets_file.exists():
        return set()

    with open(targets_file) as f:
        data = json.load(f)

    fragments: set[str] = set()
    skip_tiers = {"disqualified_with_reason", "research_queue", "_meta", "summary_counts"}
    for tier_key, entries in data.items():
        if tier_key in skip_tiers or not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name", "")
            if not name:
                continue
            # Split on / and parentheses to get all variant names
            parts = re.split(r"[/()]", name)
            for part in parts:
                cleaned = part.strip().lower()
                if len(cleaned) >= 3:  # skip very short fragments
                    fragments.add(cleaned)
    return fragments


def _matches_target_company(company_name: str, target_fragments: set[str]) -> bool:
    """Check if a PB profile's company matches any target company fragment.

    Uses bidirectional substring matching: the profile company contains
    a target fragment, or a target fragment contains the profile company.
    """
    if not company_name:
        return False
    cn = company_name.strip().lower()
    if not cn:
        return False
    for frag in target_fragments:
        shorter, longer = (cn, frag) if len(cn) <= len(frag) else (frag, cn)
        if shorter in longer and len(shorter) >= max(5, len(longer) * 0.4):
            return True
    return False


def _normalize_person_name(name: str) -> str:
    """Normalize a person name for the name+company duplicate gate.

    URL-keyed dedup can't catch a re-created prospect under a different
    LinkedIn vanity slug (same human, new URL — the PR-241 René case).
    This normalizer feeds the secondary name+company gate so those get caught.

    Steps:
      1. Strip anything after a SPACE-ANCHORED decorative separator (" - ",
         " | ") — LinkedIn names carry a title/company suffix ("René - CEO",
         "René | Acme Foods"). Separators are space-anchored so a compound surname
         ("Núñez-Vidal") and a parenthetical nickname ("René (Beto) de la
         Cruz") are NOT truncated.
      2. Diacritic fold + lowercase + whitespace collapse — delegated to
         `identity_match.normalize_for_match`, the canonical accent-fold for
         this codebase, so this does not add yet another near-duplicate
         normalizer.

    Pure function (module-level, testable). Idempotent.
    """
    if not name:
        return ""
    # Cut at the first space-anchored decorative separator only.
    for sep in (" - ", " | "):
        idx = name.find(sep)
        if idx != -1:
            name = name[:idx]
    return normalize_for_match(name)


# A normalized-name → list of (record_id, canonical_url, company_name) for
# every person already in the pipeline list. Company is resolved eagerly here
# (the fork's `bulk_fetch_persons` returns full Records, so `extract_person_info`
# yields the company name in the same pass with no extra fetch — unlike the
# upstream, which left it lazy because company was a separate record reference).
NameIndex = dict[str, list[tuple[str, str, str]]]


def _build_name_index(crm: CRMProvider, existing_entries: list[Entry]) -> NameIndex:
    """Build the run-start normalized-name → records map for the dedup gate.

    Why a LOCAL index and not a live search (empirically verified upstream):
    Attio's `search_people(name $contains ...)` is accent-SENSITIVE, so a
    suffixed ASCII needle ("Sam Rivera - Managing Director") does NOT match
    the stored accented "Sam Rivera" — the exact suffix/accent variant this
    gate exists to catch. Applying `_normalize_person_name` to BOTH sides at
    build time bridges accents AND suffixes by construction, and the per-
    candidate check becomes an O(1) dict lookup with no network call.

    One bulk read of the pipeline's person records at run start goes through the
    vendor-neutral `CRMProvider` contract (`bulk_fetch_persons` →
    `{record_id: Record}`, `extract_person_info` → normalized name/company/URL).
    Per-record fetch failures are isolated by `bulk_fetch_persons` and simply
    leave that record out of the index (it then can't be a dedup hit — the same
    degrade-open posture the URL gate takes). A malformed record that raises in
    `extract_person_info` is skipped for the same reason.
    """
    record_ids = {e.record_id for e in existing_entries if e.record_id}
    index: NameIndex = {}
    if not record_ids:
        return index
    persons = crm.bulk_fetch_persons(record_ids)
    extract_failures = 0
    for rid, record in persons.items():
        try:
            info = crm.extract_person_info(record)
        except Exception as exc:  # noqa: BLE001 — degrade open, skip this record
            extract_failures += 1
            logger.warning(
                "name_index: could not extract info for record_id=%r (%s: %s) "
                "— left out of the dedup gate",
                rid, type(exc).__name__, exc,
            )
            continue
        norm_name = _normalize_person_name(info.name or "")
        if not norm_name:
            continue
        canonical = _canonical_linkedin_url(info.linkedin_url) if info.linkedin_url else ""
        index.setdefault(norm_name, []).append((rid, canonical, info.company or ""))
    # Aggregate alarm for PARTIAL systemic degradation: the per-record warnings
    # above scatter, and the "index empty" alarm below only fires on TOTAL
    # emptiness. A non-trivial fraction of records silently dropped still
    # weakens the dedup gate (each dropped record can't be a dedup hit), so
    # surface the count once as a single summary line.
    if extract_failures:
        logger.warning(
            "name_index: %d of %d fetched record(s) skipped on "
            "extract_person_info failure — the name+company dedup gate is "
            "weaker this run (each skipped record can't be a dedup hit)",
            extract_failures, len(persons),
        )
    if existing_entries and not index:
        logger.warning(
            "name_index empty: %d entries scanned, 0 resolved to a person "
            "name — the name+company dedup gate will not fire this run",
            len(existing_entries),
        )
    return index


def _find_name_company_duplicate(
    name_index: NameIndex,
    candidate_name: str,
    candidate_company: str,
    *,
    candidate_canonical_url: str,
) -> str | None:
    """Return the record_id of a suspected URL-variant duplicate, or None.

    O(1) lookup against the prebuilt `name_index` (no live search). A hit is a
    duplicate iff:
      - its normalized name matches the candidate's (accents + LinkedIn suffix
        already folded on both sides at index-build time), AND
      - its canonical LinkedIn URL differs from the candidate's (a same-URL hit
        is the URL-gate's job — never flag it here), AND
      - its company matches the candidate's (case/accent-insensitive).

    Company is compared from the index (resolved eagerly at build time). Never
    auto-merges — the caller stages the hit for operator review.
    """
    norm_candidate = _normalize_person_name(candidate_name)
    if not norm_candidate:
        return None
    norm_company = _normalize_person_name(candidate_company)
    if not norm_company:
        # Without a company we cannot distinguish a real duplicate from a
        # namesake — do not stage on name alone (over-eager on common names).
        return None
    for hit_rid, hit_canonical, hit_company in name_index.get(norm_candidate, []):
        # Same canonical URL → this is the URL gate's territory, not ours.
        if hit_canonical and hit_canonical == candidate_canonical_url:
            continue
        if _normalize_person_name(hit_company) == norm_company:
            return hit_rid
    return None


# The reprospect_review CSV carries two row shapes: layer-3 rows (existing
# person, no list entry) which have NO "reason" key, and name+company-gate rows
# which DO. "reason" is in fieldnames and restval="" fills the layer-3 rows, so
# DictWriter's default extrasaction='raise' never trips (a single staged
# name+company duplicate used to ValueError the whole finalize phase).
_REPROSPECT_REVIEW_FIELDNAMES = [
    "name", "company", "title", "linkedin_url", "record_id",
    "score", "persona", "language", "reason",
]


def _write_reprospect_review_csv(path, rows: list[dict]) -> None:
    """Write staged re-prospect candidates to `path` (mixed row shapes safe)."""
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=_REPROSPECT_REVIEW_FIELDNAMES, restval="",
        )
        writer.writeheader()
        writer.writerows(rows)


def _get_all_searches(personas_data: dict) -> list[tuple[str, str, str]]:
    """Get all valid SN search URLs across personas and geos.

    Returns list of (persona_key, geo_key, sn_url) tuples.
    Enterprise (Tier 1, enterprise_mode=true) runs first so their profiles
    are claimed before mid-market (Tier 2) processes the same PB CSV.
    """
    all_searches = []
    paused = []
    for persona_key, persona in personas_data.items():
        # Skip paused personas (active: false) entirely — re-activating a lane
        # is a one-line config flip, but while paused its saved searches must
        # not run. Mirrors the guard in _check_all_persona_target_lists_fresh. (PR-226)
        if not persona.get("active", True):
            paused.append(persona_key)
            continue
        sn_urls = persona.get("search_queries", {}).get("sn_search_urls", {})
        for geo_key, url in sn_urls.items():
            if url and "PLACEHOLDER" not in url:
                all_searches.append((persona_key, geo_key, url))
    # Surface paused lanes so an operator never mistakes a silently
    # skipped persona for a search that produced nothing.
    if paused:
        click.echo(
            f"⏸ {len(paused)} persona(s) paused (active: false), excluded "
            f"from this run: {', '.join(sorted(paused))}",
            err=True,
        )
    # Enterprise (ICP 1) runs first so they claim profiles before mid-market (ICP 2) can mis-tag them.
    all_searches.sort(key=lambda x: 0 if personas_data.get(x[0], {}).get("enterprise_mode") else 1)
    return all_searches


def _join_name(raw: dict) -> str:
    """Join firstName + lastName from SN Export CSV."""
    first = raw.get("firstName", "")
    last = raw.get("lastName", "")
    return f"{first} {last}".strip()


def _launch_and_download(
    pb: PhantomBusterClient,
    search_export_id: str,
    sn_url: str,
    batch_size: int,
) -> str | None:
    """Launch PB Search Export for a single URL and return CSV text."""
    li_cookie = li_session_cookie()
    li_ua = li_user_agent_raw()
    if not li_cookie:
        click.echo("Error: PB_LI_SESSION_COOKIE not set.")
        return None

    # PR #179 pattern applied to the weekly scrape: PB keys its
    # processed-inputs dedup database on the result CSV filename. A static
    # csvName (the default "result") makes a re-run of the same saved search
    # return PB's frozen cached set instead of re-scraping. A fresh per-launch
    # name busts that dedup AND yields a CSV with only this run's rows.
    from workflows.daily_check_helpers import _fresh_csv_name
    csv_name = _fresh_csv_name("wk")

    launch_args = {
        "inputType": "salesNavigatorSearchUrl",
        "salesNavigatorSearchUrl": sn_url,
        "numberOfResultsPerSearch": batch_size,
        "numberOfLinesPerLaunch": batch_size,
        "removeDuplicateProfiles": True,
        "numberOfProfiles": 200,
        "csvName": csv_name,
        "identities": [{
            "sessionCookie": li_cookie,
            "userAgent": li_ua or "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        }],
    }
    launch = pb.launch_agent(search_export_id, launch_args)

    click.echo("  Waiting for export to complete...")
    pb.wait_for_completion(launch, poll_interval=15, max_wait=600)

    # F-PR-5: CSV keyed to launch.container_id, not "latest". The per-launch
    # csvName MUST be passed here too, or the agent-scoped fallback fetches a
    # stale file (see clients/phantombuster.download_result_csv).
    return pb.download_result_csv(launch, csv_name=csv_name)


def _quarantine_business_days() -> int:
    """Read the quarantine business-day window from env, with default."""
    from models.env import env_int_positive

    return env_int_positive(
        "OUTBOUND_PROSPECT_QUARANTINE_BDAYS",
        PROSPECT_QUARANTINE_BUSINESS_DAYS_DEFAULT,
        allow_zero=True,
    )


# ── PR-28: weekly-finalize idempotency + ICP-2 geo enforcement ───────────
#
# Two defensive layers PROSPECT-commit goes through before a list-entry
# write:
#
#   1. `enforce_icp_lane_geo` — the persona configs for ICP-2 are
#      curated for LATAM mfg (MX/CL/CO primary). If a PB CSV bleeds a
#      non-LATAM profile into the ICP-2 lane, the gate emits a typed
#      `icp2_geo_violation` queue row and SKIPS the commit.
#
#   2. `weekly_finalize_idempotent` — pre-commit 14-day idempotency
#      check. If the same canonical LinkedIn URL has a `last_contact_date`
#      within the prior 14 days on the LinkedIn Outreach list, the
#      commit is a no-op (a re-run of `/sales-weekly` within a 2-week
#      window does NOT double-commit the same prospect).
#
# Both helpers are pure-ish (geo touches `escalate()`; idempotency
# touches `attio.search_list_entries`) — the orchestrator passes them
# the live attio client and they fail-loud on transport errors. The
# `_build_prospect_entry_attrs` writer stamps `week_starting` (Monday
# of `today`) on every committed entry for batch traceability.

# LATAM country names ES/PT/EN, matched against the LAST comma-separated
# token of a free-form LinkedIn location string. Lowercase comparison.
#
# Post-QA convergence (3 of 6 agents flagged §0 #9 silent fallback): the
# previous design used BARE city substrings ("lima", "santiago",
# "guadalajara", "monterrey", "bogota") that were trivially defeated by
# US towns of the same name (Lima OH, Santiago CA, Bogota NJ, Monterrey
# CA, Mexico MO, Peru IN). The gate is the LAST line of defense before
# an ICP-2 non-LATAM record commits, so substring permissiveness IS the
# silent fallback. The fix: parse the location string into comma-
# separated tokens, treat the last token as the country, and match
# country-only against this set. City matches are no longer accepted
# unless an explicit country also appears.
_LATAM_COUNTRY_TOKENS: frozenset[str] = frozenset({
    "argentina", "bolivia", "brasil", "brazil", "chile", "colombia",
    "costa rica", "cuba", "ecuador", "el salvador", "guatemala",
    "honduras", "mexico", "méxico", "nicaragua", "panama", "panamá",
    "paraguay", "peru", "perú", "puerto rico",
    "republica dominicana", "república dominicana", "dominican republic",
    "uruguay", "venezuela",
})


def _monday_of(d: date) -> date:
    """Return the Monday of the ISO week containing `d`. Used as the
    canonical `week_starting` value on every PROSPECT entry committed
    in the same weekly batch.
    """
    return d - timedelta(days=d.weekday())


def _location_is_latam(location: str | None) -> bool:
    """Structured LATAM-country check on a free-form location string.

    Splits on `,` and checks ONLY the LAST comma-separated token
    against `_LATAM_COUNTRY_TOKENS`. The last-token rule mirrors the
    canonical LinkedIn-location shape "city, [state,] country" — the
    country sits in the final slot. ANY-token matching (the pre-fold
    design) created false positives where a US city named after a
    LATAM country ("Mexico, Missouri") matched the country token in
    the wrong position.

    - "Lima, Ohio, United States" → last "united states" → False
    - "Lima, Peru" → last "peru" → True
    - "Mexico, Missouri, United States" → last "united states" → False
    - "Mexico, Missouri" → last "missouri" → False
    - "Peru, Indiana" → last "indiana" → False
    - "Santiago de Compostela, Galicia, Spain" → last "spain" → False
    - "Santiago, Chile" → last "chile" → True
    - "Mexico City, Mexico" → last "mexico" → True
    - "Mexico" (single token) → token "mexico" → True

    Empty/None location returns False (ICP-2 fail-safe — the gate
    treats unknown locations as non-LATAM so the operator triages
    from the queue row).
    """
    if not location:
        return False
    tokens = [tok.strip().lower() for tok in location.split(",") if tok.strip()]
    if not tokens:
        return False
    return tokens[-1] in _LATAM_COUNTRY_TOKENS


def enforce_icp_lane_geo(
    prospect_data: dict,
    score_result: dict,
    *,
    crm: CRMProvider,
) -> bool:
    """Gate PROSPECT-commit for ICP-2 prospects: location must be LATAM.

    Returns True when the commit should proceed (lane is ICP-1, or
    lane is ICP-2 and location is LATAM). Returns False AND emits a
    typed `icp2_geo_violation` queue row when an ICP-2 prospect has a
    non-LATAM location — the commit must NOT happen.

    The ICP-2 lane is recognized via either `score_result["icp_lane"]`
    (numeric, populated by score_prospect after the PR-28 fix for
    deterministic-pass paths) or `score_result["scoring_lane"]`
    (string, `"target_company_mode"` for ICP-2). Both checks run so
    legacy callers that build a partial score_result still gate
    correctly.

    The queue row is idempotent on `linkedin_url` so a re-run of the
    weekly batch over the same CSV doesn't open duplicate rows for
    the same prospect.
    """
    icp_lane = score_result.get("icp_lane")
    scoring_lane = score_result.get("scoring_lane")
    is_icp2 = icp_lane == 2 or scoring_lane == "target_company_mode"
    if not is_icp2:
        return True

    location = prospect_data.get("location") or ""
    if _location_is_latam(location):
        return True

    linkedin_url = prospect_data.get("linkedin_url") or ""
    escalate(
        type="icp2_geo_violation",
        idempotency_key=linkedin_url,
        payload={
            "linkedin_url": linkedin_url,
            "title": str(prospect_data.get("title") or ""),
            "company": str(prospect_data.get("company") or ""),
            "location": str(location),
            "icp_lane": int(icp_lane) if icp_lane is not None else 2,
            "scoring_lane": str(scoring_lane or ""),
        },
        attio=crm,
    )
    return False


_RECENT_OUTREACH_WINDOW_DAYS = 14


class WeeklyCandidate(NamedTuple):
    """PROSPECT-commit candidate. NamedTuple gives caller-typo
    detection at the orchestrator boundary (code-reviewer +
    type-design QA convergence on the ad-hoc dict shape).
    """
    prospect_data: dict
    score_result: dict
    raw: dict


class WeeklyFinalizeSummary(TypedDict):
    """Output contract for `weekly_finalize_idempotent`.

    Per type-design QA: the five-key shape is a downstream contract
    consumed by PR-30 weekly report + the `/sales-weekly` CLI summary.
    A typo on a counter increment now surfaces at mypy time rather
    than silently creating a sixth key.

    `skipped_urls` is the per-URL audit trail for `idempotent_skipped`
    (silent-failure-hunter B-2): an operator can answer "which 30
    URLs got skipped and why" without grepping logs. `malformed_input`
    splits the count of unusable candidates (empty/invalid linkedin_url)
    from the count of real Attio write failures.
    """
    candidates: int
    idempotent_skipped: int
    icp2_geo_skipped: int
    committed: int
    write_errors: int
    malformed_input: int
    skipped_urls: list[str]
    # connectionDegree routing (from the Sales Nav export, free — no scrape):
    # `accepted_first_degree` is the subset of `committed` that was already a
    # 1st-degree connection and committed straight at ACCEPTED (DM cadence)
    # instead of PROSPECT. `skipped_uninvitable` counts Out-of-Network rows
    # dropped entirely (LinkedIn won't allow an invite — committing them would
    # only pollute the pool + waste daily degree-check scrapes).
    accepted_first_degree: int
    skipped_uninvitable: int


def _load_recent_outreach_map(
    crm: CRMProvider,
    list_id: str,
    cutoff_date: date,
) -> dict[str, date]:
    """Single CRM query that returns `{canonical_url: latest_last_contact}`
    for every LinkedIn Outreach entry with `last_contact_date >= cutoff_date`.

    Post-QA convergence (code-reviewer I-1 + silent-failure B-1): the
    pre-fold design called `crm.query_list_entries` once per candidate
    (50-200× per batch) AND raised mid-loop on any transport error,
    aborting the batch with no progress checkpoint. Hoisted to a single
    call here; callers receive an O(1) dict and the orchestrator wraps
    the fetch in a single try/except.

    Reads off the normalized `Entry` dataclass: `query_list_entries`
    returns `list[Entry]`, so the per-step signals come from
    `entry.attributes` (the same flat slug-keyed shape `parse_entry`
    produced) — `parse_entry` is dropped at the boundary.

    The LinkedIn URL is read from `canonical_linkedin_url`, the key
    `parse_entry` / `Entry.attributes` actually emit (there is no
    `linkedin_url` key). It is then run through `_canonical_linkedin_url`
    so the map keys match the canonical form the lookup side
    (`weekly_finalize_idempotent`) uses for `if canonical in recent_map`.

    Bug history: this previously read `linkedin_url`, a key the entry never
    carries, so `if not url: continue` fired on every entry and the map was
    ALWAYS `{}` — the 14-day re-prospect guard a silent no-op. (The second
    dedup layer — `in_list_record_ids` membership + `search_person_by_linkedin`
    in `_process_prospects` — still blocked re-prospecting of anyone currently
    on the list, so blast radius was bounded to window-based skips and people
    who had left the list.)
    """
    entries = crm.query_list_entries(list_id=list_id)
    out: dict[str, date] = {}
    entries_with_canonical = 0  # how many entries carry a usable canonical URL
    for entry in entries:
        url = entry.attributes.get("canonical_linkedin_url") or ""
        if not url:
            continue
        canonical = _canonical_linkedin_url(url)
        if not canonical:
            continue
        entries_with_canonical += 1
        last_contact = entry.attributes.get("last_contact_date")
        if not last_contact:
            continue
        try:
            lc_date = date.fromisoformat(str(last_contact)[:10])
        except (ValueError, TypeError):
            continue
        if lc_date < cutoff_date:
            continue
        # Keep the most-recent date per URL.
        if canonical not in out or lc_date > out[canonical]:
            out[canonical] = lc_date
    # Observability: distinguish the SILENT-BUG fingerprint from a benign quiet
    # window. The bug this fix closed was NULL `canonical_linkedin_url` on every
    # entry → the map keyed on a dead field → always `{}`. The TELL is
    # `entries_with_canonical == 0` against a non-empty list. A map that is empty
    # only because no one was contacted in the last 14 days (canonical present,
    # but no recent last_contact_date) is NOT a bug — escalating it would be a
    # false alarm that trains the operator to ignore the signal (the very way the
    # original bug hid). So escalate ONLY on the zero-canonical fingerprint; log
    # the benign quiet-window case at info.
    if entries and entries_with_canonical == 0:
        logger.warning(
            "recent_outreach_map: %d entries scanned, 0 carry a usable "
            "canonical_linkedin_url — the 14-day re-prospect guard is a NO-OP "
            "this run (the dead-guard fingerprint). cutoff=%s",
            len(entries),
            cutoff_date,
        )
        # Fix 2b: surface the dead-guard fingerprint in the operator review
        # queue so the no-op is visible within one run (logging alone hid it for
        # months). Swallow escalate failures — the guard must never crash the
        # weekly run on an escalation transport error.
        try:
            escalate(
                type="recent_outreach_map_empty",
                idempotency_key=f"recent-outreach-map-empty|{cutoff_date.isoformat()}",
                payload={
                    "entries_scanned": len(entries),
                    "entries_with_canonical": entries_with_canonical,
                    "cutoff_date": cutoff_date.isoformat(),
                },
                attio=crm,
            )
        except Exception as esc_exc:  # noqa: BLE001 — guard must not crash the run
            logger.warning(
                "could not open recent_outreach_map_empty escalation: %s: %s",
                type(esc_exc).__name__,
                esc_exc,
            )
    elif not out:
        logger.info(
            "recent_outreach_map empty but %d entries carry canonical_url — "
            "benign quiet window (no contact in last 14d, cutoff=%s), guard intact",
            entries_with_canonical,
            cutoff_date,
        )
    else:
        logger.info(
            "recent_outreach_map: %d recent-contact URLs (from %d entries, cutoff=%s)",
            len(out),
            len(entries),
            cutoff_date,
        )
    return out


def _has_recent_outreach(
    crm: CRMProvider,
    canonical_url: str,
    cutoff_date: date,
    *,
    list_id: str,
) -> bool:
    """Single-prospect idempotency check.

    Now takes `list_id` as a required keyword arg (was reading from
    `os.environ` — code-reviewer N-7 + type-design + silent-failure
    convergence). The map-based fast path in `weekly_finalize_idempotent`
    uses `_load_recent_outreach_map` directly; this helper remains
    the single-prospect entry point for tests + ad-hoc callers.
    """
    recent = _load_recent_outreach_map(crm, list_id, cutoff_date)
    return canonical_url in recent


def _connection_degree(raw: dict) -> str:
    """Normalize the Sales Nav export `connectionDegree` column.

    Returns one of: ``"1st"`` | ``"2nd"`` | ``"3rd"`` | ``"out_of_network"`` |
    ``""`` (unknown/missing). Unknown is treated as invitable (PROSPECT) by
    callers — fail-open, since a missing degree shouldn't drop a real prospect.
    """
    d = (raw.get("connectionDegree") or "").strip().lower()
    if d in ("1st", "1"):
        return "1st"
    if d in ("2nd", "2"):
        return "2nd"
    if d in ("3rd", "3", "3rd+"):
        return "3rd"
    if "out of network" in d or d in ("oon", "out_of_network"):
        return "out_of_network"
    return ""


def weekly_finalize_idempotent(
    crm: CRMProvider,
    list_id: str,
    today: str,
    candidates: list[WeeklyCandidate] | list[dict],
    *,
    dry_run: bool = False,
    existing_entries: list[Entry] | None = None,
    in_list_record_ids: set[str] | None = None,
    anthropic_client=None,
) -> WeeklyFinalizeSummary:
    """PROSPECT-commit orchestrator with 14-day idempotency + ICP-2
    geo enforcement.

    `candidates` accepts either `WeeklyCandidate` NamedTuples (preferred)
    or raw dicts with keys `prospect_data` / `score_result` / `raw` for
    backwards compatibility with existing call sites.

    Per candidate:
      1. ICP-2 geo gate — non-LATAM ICP-2 prospects emit
         `icp2_geo_violation` and are skipped (no Attio query).
      2. 14-day idempotency — `_load_recent_outreach_map` is called ONCE
         up-front; per-candidate check is an O(1) dict lookup. Wrap the
         fetch in a single try/except so a transport failure aborts
         cleanly with `summary["aborted_reason"]` instead of crashing
         the batch mid-loop.
      3. Commit via `_commit_prospect` — `_build_prospect_entry_attrs`
         stamps `week_starting` automatically.

    Returns `WeeklyFinalizeSummary` with per-stage counts.
    """
    today_date = date.fromisoformat(today)
    cutoff = today_date - timedelta(days=_RECENT_OUTREACH_WINDOW_DAYS)
    summary: WeeklyFinalizeSummary = {
        "candidates": len(candidates),
        "idempotent_skipped": 0,
        "icp2_geo_skipped": 0,
        "committed": 0,
        "write_errors": 0,
        "malformed_input": 0,
        "skipped_urls": [],
        "accepted_first_degree": 0,
        "skipped_uninvitable": 0,
    }

    if existing_entries is None:
        existing_entries = []
    if in_list_record_ids is None:
        in_list_record_ids = set()

    # Hoisted single-call fetch + map build. Failures here are operator-
    # visible via the exception (typed RuntimeError) — the batch
    # short-circuits with a clean summary rather than partial state.
    recent_map: dict[str, date]
    try:
        recent_map = _load_recent_outreach_map(crm, list_id, cutoff)
    except Exception:  # noqa: BLE001 — propagation boundary
        # Any transport error here would have killed the prior per-candidate
        # loop. Re-raise after logging so the operator sees the stack trace
        # alongside the cleaned-up partial summary.
        logger.exception(
            "weekly_finalize_idempotent: failed to load recent-outreach map; "
            "no candidates processed",
        )
        raise

    for cand in candidates:
        # Support both NamedTuple and legacy dict shape.
        if isinstance(cand, WeeklyCandidate):
            prospect_data = cand.prospect_data
            score_result = cand.score_result
            raw = cand.raw
        else:
            prospect_data = cand["prospect_data"]
            score_result = cand["score_result"]
            raw = cand.get("raw", {})

        # Geo gate (ICP-2 only). Failures emit the queue row + skip.
        if not enforce_icp_lane_geo(prospect_data, score_result, crm=crm):
            summary["icp2_geo_skipped"] += 1
            continue

        # Per-candidate idempotency check — O(1) dict lookup against the
        # hoisted map. Malformed canonical URLs are a data-quality
        # signal, not a write failure — bucketed separately.
        canonical = _canonical_linkedin_url(prospect_data.get("linkedin_url") or "")
        if not canonical:
            summary["malformed_input"] += 1
            logger.warning(
                "weekly_finalize_idempotent: candidate has malformed/empty "
                "linkedin_url: %r",
                prospect_data,
            )
            continue
        if canonical in recent_map:
            summary["idempotent_skipped"] += 1
            summary["skipped_urls"].append(canonical)
            logger.info(
                "weekly_finalize_idempotent: skipped %s (last_contact_date=%s "
                "within %d-day window)",
                canonical, recent_map[canonical], _RECENT_OUTREACH_WINDOW_DAYS,
            )
            continue

        # connectionDegree routing — the Sales Nav export carries the degree
        # for free, so we never spend a LinkedIn visit to learn it:
        #  - Out of Network: LinkedIn won't allow an invite. Skip the commit
        #    entirely so the prospect never pollutes the invite pool or burns a
        #    daily pre-invite degree-check scrape on someone unreachable.
        #  - 1st degree: already connected. Commit straight at ACCEPTED so they
        #    enter the DM cadence instead of a dead invite queue.
        #  - 2nd / 3rd / unknown: normal PROSPECT (invitable).
        degree = _connection_degree(raw)
        if degree == "out_of_network":
            summary["skipped_uninvitable"] += 1
            continue
        commit_stage = (
            PipelineStage.ACCEPTED.value if degree == "1st"
            else PipelineStage.PROSPECT.value
        )

        if dry_run:
            summary["committed"] += 1
            if degree == "1st":
                summary["accepted_first_degree"] += 1
            continue

        ok = _commit_prospect(
            crm, prospect_data, raw, score_result, list_id, today,
            stage_name=commit_stage,
            anthropic_client=anthropic_client,
            existing_entries=existing_entries,
            in_list_record_ids=in_list_record_ids,
        )
        if ok:
            summary["committed"] += 1
            if degree == "1st":
                summary["accepted_first_degree"] += 1
        else:
            summary["write_errors"] += 1

    return summary


def _build_prospect_entry_attrs(
    score_result: dict, today: str, *, record_id: str | None = None
) -> dict:
    """Build the Attio list-entry attribute dict for a new PROSPECT-stage entry.

    Includes the core scoring signals (persona, language, score) plus the
    optional scorer-debug fields (score_breakdown, scoring_lane, verdict_path,
    llm_rationale) when they're present. Optional fields are skipped when
    None/empty so callers that build a partial score_result (e.g. agent-verdict
    commit path) don't pollute Attio with empty strings.

    Two §3.1 defense attrs are written on every fresh commit:
    - `prospect_committed_at` (datetime, UTC) — exact commit moment, the
      canonical PROSPECT-stage origin timestamp. Sole writer per the
      §3.15 registry.
    - `invite_eligible_after` (date) — `today + quarantine business days`.
      `daily_check.run_connection_requests` + `pre_invite_check` gate
      every invite on `is_invite_eligible(entry, today)`.

    PR-21 experiment cohort stamping (PROSPECT-commit):
    - If exactly one experiment is running, stamps `experiment_id` +
      `experiment_id_frozen_at="prospect"` into the attrs dict.
    - If zero experiments are running (returns None), OMITS both keys from
      attrs entirely (do NOT write NULL or a sentinel — leave fields absent
      so Attio keeps them NULL). Logs to stderr with record_id so PR-22
      archaeology can retroactively stamp these rows.
    - If multiple experiments are running, propagates
      `MultipleRunningExperimentsError` — the caller's pre-flight guard
      in cli.py catches this before any prospects commit.

    `record_id` is required for the None-running log (can be "" if unknown
    at call time; prefer passing the actual record_id when available).
    """
    current_experiment_id = get_current_experiment_id()  # may raise MultipleRunningExperimentsError
    attrs: dict = {
        "persona": score_result["persona"],
        "language": score_result["language"],
        "quality_score": score_result["score"],
        "dm_step": 0,
        # NOTE: last_contact_date is intentionally NOT stamped at commit. A fresh
        # PROSPECT has had zero contact; the field is written for real only when
        # an invite is actually sent (daily_check.run_connection_requests). The
        # canonical PROSPECT-stage origin timestamp is prospect_committed_at below.
        "prospect_committed_at": datetime.now(UTC).isoformat(),
        "invite_eligible_after": add_business_days(
            date.fromisoformat(today), _quarantine_business_days()
        ).isoformat(),
        # PR-28: batch-traceability — the Monday of `today` (the operator
        # invocation date), so weekly cohorts can be queried by
        # `week_starting` for the per-cohort math substrate.
        "week_starting": _monday_of(date.fromisoformat(today)).isoformat(),
    }
    if score_result.get("score_breakdown") is not None:
        attrs["score_breakdown"] = json.dumps(score_result["score_breakdown"])
    if score_result.get("scoring_lane"):
        attrs["scoring_lane"] = score_result["scoring_lane"]
        # PR-39: also stamp the typed cadence_lane derived from scoring_lane,
        # so the daily-check lane-rank ordering reads the typed attribute
        # instead of parsing the legacy free-form string at the call site.
        attrs["cadence_lane"] = derive_cadence_lane(score_result["scoring_lane"])
    if score_result.get("verdict_path"):
        attrs["verdict_path"] = score_result["verdict_path"]
    if score_result.get("llm_rationale"):
        attrs["llm_rationale"] = score_result["llm_rationale"]
    # Phase 1 auto-research denormalized fields. icp_lane is only set when
    # the borderline LLM gate fired (40-75 band); deterministic verdicts
    # leave it None so we don't infer a lane that wasn't actually classified.
    band = score_band(score_result.get("score"))
    if band:
        attrs["quality_score_band"] = band
    if score_result.get("icp_lane") is not None:
        attrs["icp_lane_persisted"] = score_result["icp_lane"]

    # PR-21 PROSPECT-commit experiment stamping.
    # current_experiment_id was resolved at the top of this function.
    # None → OMIT both keys (Lesson 1: no sentinel, no write).
    # str → stamp both keys (experiment_id + experiment_id_frozen_at="prospect").
    if current_experiment_id is not None:
        attrs["experiment_id"] = current_experiment_id
        attrs["experiment_id_frozen_at"] = "prospect"
    else:
        # No running experiment. Omit both fields (leave NULL in Attio).
        # Log with record_id so PR-22 archaeology can retroactively stamp
        # these rows. Both fields stay absent from attrs — no sentinel write.
        click.echo(
            f"[PR-21] PROSPECT-commit: no active experiment — "
            f"experiment_id/frozen_at omitted for "
            f"record_id={record_id or 'unknown'!r}. "
            "PR-22 archaeology will retroactively stamp this row.",
            err=True,
        )

    return attrs


# Phase 1 entry-attribute keys that may not exist in Attio yet (until the
# operator has run scripts/migrate_attio_schema.py). add_list_entry retries
# without them on a 400 so the core entry creation still lands.
#
# Quarantine attrs (prospect_committed_at + invite_eligible_after) are
# DELIBERATELY NOT in this fallback set: a PROSPECT committed without
# them would be immediately invite-eligible the same day per
# is_invite_eligible's missing-attr → True semantics — that's the exact
# §3.1 same-day-commit-then-invite failure mode the quarantine exists to
# prevent. Better to hard-fail the prospect commit on an unmigrated
# schema (loud, recoverable: run the migration and re-commit) than to
# silently ship without quarantine.
_PHASE1_ENTRY_KEYS = (
    "quality_score_band",
    "icp_lane_persisted",
)


def _safe_add_list_entry(
    crm: CRMProvider,
    *,
    record_id: str,
    stage_name: str,
    entry_attributes: dict,
    list_id: str,
    existing_entries: list[Entry] | None,
) -> Entry:
    """Wrap add_list_entry so an unknown-attribute 400 doesn't lose the entry.

    Both paths return ``crm.add_list_entry(...)``, which contractually returns
    an ``Entry`` (never ``None``); the caller still gates the dedup-cache append
    on ``new_entry.record_id`` to skip a degenerate empty-body entry.
    """
    try:
        return crm.add_list_entry(
            record_id=record_id,
            stage_name=stage_name,
            entry_attributes=entry_attributes,
            list_id=list_id,
            existing_entries=existing_entries,
        )
    except httpx.HTTPStatusError as he:
        if he.response.status_code != 400 or not any(
            k in entry_attributes for k in _PHASE1_ENTRY_KEYS
        ):
            raise
        fallback = {k: v for k, v in entry_attributes.items() if k not in _PHASE1_ENTRY_KEYS}
        click.echo(
            "      → Attio rejected Phase 1 attrs (schema migration not run yet?); "
            "retrying without quality_score_band/icp_lane_persisted",
            err=True,
        )
        return crm.add_list_entry(
            record_id=record_id,
            stage_name=stage_name,
            entry_attributes=fallback,
            list_id=list_id,
            existing_entries=existing_entries,
        )


def _commit_prospect(
    crm: CRMProvider,
    prospect_data: dict,
    raw: dict,
    score_result: dict,
    list_id: str,
    today: str,
    *,
    stage_name: str = PipelineStage.PROSPECT.value,
    anthropic_client=None,
    existing_entries: list[Entry] | None = None,
    in_list_record_ids: set[str] | None = None,
    summary: dict | None = None,
) -> bool:
    """Upsert a qualified prospect to Attio and add them to the pipeline list.

    `stage_name` is the initial pipeline stage for the new list entry — PROSPECT
    for normal (2nd/3rd-degree, invitable) prospects, ACCEPTED for 1st-degree
    (already-connected) prospects routed straight into the DM cadence.

    Returns True on success, False on failure (write error or missing record_id).

    `existing_entries` and `in_list_record_ids` are caller-owned caches the
    weekly run uses to avoid an Attio full-list scan per prospect. We mutate
    them in place after a successful add so a re-encounter of the same person
    later in the run (via a different URL variant) finds the just-created
    entry and upserts instead of POSTing a duplicate. Without this update the
    snapshot goes stale during the run — the same root cause behind the
    2026-04-21 duplicate-record incident.
    """
    name_parts = prospect_data["name"].split(maxsplit=1)
    first_name = name_parts[0] if name_parts else prospect_data["name"]
    last_name = name_parts[1] if len(name_parts) > 1 else ""

    company_domain = extract_real_domain(raw)

    # PR-25 follow-up: _process_prospects resolved industry (CRM lookup or
    # ingest-time classification) before scoring — reuse it here so a brand-new
    # company is CREATEd with the label + status already attached instead of
    # burning a second classification. The classify fallback below only serves
    # callers that commit without going through _process_prospects enrichment
    # (tests passing a mock client).
    industry = prospect_data.get("industry")
    industry_status = prospect_data.get("industry_vertical_status")
    if industry is None and anthropic_client is not None:
        from workflows.industry_classifier import classify_industry
        industry = classify_industry(
            prospect_data["company"],
            domain=company_domain or None,
            anthropic_client=anthropic_client,
        )
        if industry:
            industry_status = "low_confidence"

    # §7 boundary: `match_or_create_company` (company_matcher.py) is an
    # UNMIGRATED helper shared with `backfill_companies.backfill_import`; it
    # reads raw Attio record shapes (`result["id"]["record_id"]`) and writes
    # via `create_company`/`create_note`, so it still requires the concrete
    # client. Hand it the provider's raw inner client (the documented Attio
    # escape hatch) rather than ripple its signature into the unmigrated
    # caller. Returns the company record_id string exactly as before.
    company_rid = match_or_create_company(
        _attio_inner_client(crm),
        prospect_data["company"],
        domain=company_domain or None,
        industry_vertical=industry,
        industry_status=industry_status,
    )

    person_attrs: dict = {
        "name": [{"first_name": first_name, "last_name": last_name, "full_name": prospect_data["name"]}],
        "linkedin": prospect_data["linkedin_url"],
        "job_title": prospect_data["title"],
    }
    if company_rid:
        person_attrs["company"] = [{"target_object": "companies", "target_record_id": company_rid}]

    try:
        record = crm.upsert_person(matching_attribute="linkedin", attributes=person_attrs)
    except httpx.HTTPStatusError as e:
        # Swallow ONLY validation errors (400) — those are per-prospect data
        # issues we can keep the run going through. Re-raise auth (401),
        # permission (403), rate-limit (429), and server (5xx) errors so the
        # whole weekly run fails loud instead of silently logging "write
        # error" for every prospect while Attio is unreachable.
        if e.response.status_code != 400:
            raise
        click.echo(
            f"      → Upsert 400 validation error for {prospect_data['linkedin_url']}: "
            f"{e.response.text[:200]}"
        )
        return False

    # upsert_person now returns a normalized `Record` (not raw Attio JSON);
    # the record id is the dataclass field, not `record["id"]["record_id"]`.
    record_id = record.record_id
    if not record_id:
        click.echo("      → Failed to upsert Attio record")
        return False

    # Supply accounting (2026-06-16 root cause): a record already in the
    # pipeline list at run start is being re-stamped, not newly sourced — it
    # adds zero net-new supply. Classify BEFORE the add (in_list_record_ids is
    # mutated below) so the weekly summary can tell real supply from churn.
    already_listed = (
        in_list_record_ids is not None and record_id in in_list_record_ids
    )
    # PR-207 (re-stamp incident): the snapshot-membership check above is a
    # silent no-op when the caller passes no `in_list_record_ids` — exactly the
    # `weekly_finalize_cmd` borderline-commit path. With the snapshot absent, an
    # existing pipeline entry (even a terminal/Responded one) got re-stamped
    # back to a fresh stage/dm_step=0, wiping cadence depth. When no snapshot
    # was supplied, ground the guard in CRM truth instead: if the record already
    # owns ANY entry in this list, weekly must not touch it. Prefer the in-memory
    # `existing_entries` cache (the finalize path now supplies it); fall back to a
    # targeted list read only when neither cache is present. FAIL CLOSED — if the
    # read errors, skip rather than re-stamp a live entry. The bulk weekly run
    # always passes `in_list_record_ids`, so this adds zero per-prospect work
    # there — it keeps its authoritative single run-start snapshot.
    if not already_listed and in_list_record_ids is None:
        if existing_entries is not None:
            record_entries = [e for e in existing_entries if e.record_id == record_id]
        else:
            try:
                all_entries = crm.query_list_entries(list_id=list_id)
            except httpx.HTTPStatusError:
                click.echo(
                    f"      → Re-stamp guard lookup failed for {record_id}; "
                    "skipping commit rather than risk re-stamping a live entry"
                )
                return False
            record_entries = [e for e in all_entries if e.record_id == record_id]
        if record_entries:
            already_listed = True
    if summary is not None:
        key = "restamped_existing" if already_listed else "net_new_created"
        summary[key] = summary.get(key, 0) + 1

    # Fix 1 (weekly re-stamp cadence wipe): a record already in the pipeline
    # list is owned by the daily cadence engine. Re-stamping its entry here via
    # _safe_add_list_entry → add_list_entry PATCH overwrites stage→Prospect/
    # Accepted and dm_step→0, wiping cadence depth. Skip the add entirely —
    # weekly must never rewrite a record the daily cadence owns. This mirrors
    # the _process_prospects skip and the in-run dedup lesson. The record is
    # already in in_list_record_ids and existing_entries; leave both as-is.
    if already_listed:
        return True

    entry_attrs = _build_prospect_entry_attrs(score_result, today, record_id=record_id)
    # Fix 2a: stamp canonical_linkedin_url on commit. This is the key the
    # 14-day re-prospect guard (_load_recent_outreach_map) reads — it was NULL
    # on 100% of list entries, making that guard a silent no-op. Stamping it on
    # every net-new commit populates the guard going forward.
    canonical = _canonical_linkedin_url(prospect_data.get("linkedin_url") or "")
    if canonical:
        entry_attrs["canonical_linkedin_url"] = canonical

    new_entry = _safe_add_list_entry(
        crm,
        record_id=record_id,
        stage_name=stage_name,
        entry_attributes=entry_attrs,
        list_id=list_id,
        existing_entries=existing_entries,
    )
    if in_list_record_ids is not None:
        in_list_record_ids.add(record_id)
    # Append to the batch dedup cache only for a real entry. Pre-migration
    # new_entry was a raw dict (empty {} = falsy → not appended); a normalized
    # Entry is ALWAYS truthy, so gate on record_id to keep the old behavior:
    # an empty-body write (degenerate Entry with record_id="") must NOT enter
    # the cache, where it would be inert but spurious.
    if existing_entries is not None and new_entry and new_entry.record_id:
        existing_entries.append(new_entry)
    return True


# ── PR-27: company HQ country classifier (B-PW-GLOBAL) ──────────────────
#
# Per plan §5 Wave 1E PR-27 + Round-4 D4. A Haiku-backed classifier that
# detects a company's headquarters country, plus a confidence score and
# a convenience `is_latam` flag. Non-LATAM HQs score down in ICP1 (cold
# outreach to a non-LATAM-HQ multinational is high-risk — the buyer is
# usually at the LATAM subsidiary, not the global HQ).
#
# The classifier is REGISTERED as sole writer for `companies.company_hq_country`
# and `companies.company_hq_confidence` in `clients/attio_writer_registry.py`
# at the canonical path `workflows.weekly_prospect.classify_company_hq`
# (manifest entries in `docs/attio_schema_deltas.yaml`). Per §0 invariant
# #11, the LLM call surfaces to the parent slash-command session via
# `workflows.llm_dispatch.request_llm_dispatch`; the F-PR-9
# `LLMBudgetLedger` gates cost.

# LATAM country set used by `_is_latam_country` for the convenience flag.
# Lower-cased for case-insensitive comparison. Includes Spanish/Portuguese/
# English forms so the Haiku response doesn't have to match a canonical
# spelling exactly. Pruning out micro-states keeps the set tight to the
# the shipped ICP geography.
_LATAM_COUNTRIES_LOWER: frozenset[str] = frozenset({
    # Spanish
    "argentina", "bolivia", "chile", "colombia", "costa rica", "cuba",
    "república dominicana", "republica dominicana", "ecuador",
    "el salvador", "guatemala", "honduras", "méxico", "mexico",
    "nicaragua", "panamá", "panama", "paraguay", "perú", "peru",
    "puerto rico",  # GTM-QA scope: PR-HQ companies count as LATAM ICP
    "uruguay", "venezuela",
    # Portuguese / Brazil
    "brasil", "brazil",
    # English / mixed
    "dominican republic",
})

HQ_CLASSIFIER_MAX_TOKENS = 60

HQ_CLASSIFIER_SYSTEM_PROMPT = """You identify the headquarters country of a company.

Return ONLY a single JSON object on one line, nothing else — no markdown, no preamble:
{"country": "<country>", "confidence": <0.0-1.0>}

Rules:
- `country` is the global headquarters country (NOT the local subsidiary).
  Use the canonical English country name when in doubt: "United States",
  "Germany", "Mexico", "Brazil", "France", "Switzerland", "Japan", "Spain",
  etc. For LATAM HQs use the country name in Spanish or English ("Mexico",
  "Brasil", "Colombia") — either is accepted.
- `confidence` is a float 0.0-1.0 reflecting how certain you are. Use
  >=0.85 for major multinationals you've heard of; 0.5-0.8 for likely
  LATAM mid-market firms; <0.5 when the company name is generic or
  ambiguous.
- If you don't know the country, return `{"country": "unknown",
  "confidence": 0.0}`.
- Do not wrap in markdown. Do not include explanations. Single line JSON only."""


@dataclass(frozen=True)
class HQClassificationResult:
    """Outcome of `classify_company_hq`.

    `country` is the Haiku-returned country name (lower-cased, stripped)
    on a confirmed classification, or `None` for the explicit-unknown
    case (Haiku said "I don't know"). Distinguish from `classify_company_hq`
    returning `None` itself, which means a transient / parse failure.

    `confidence` is a float in [0.0, 1.0]. Out-of-range or non-finite
    inputs raise `ValueError` via `__post_init__` so a hand-built result
    cannot smuggle in an invalid value.

    `is_latam` is a derived `@property`, not a constructor field, so the
    invariant `is_latam ↔ country is in the canonical LATAM set` cannot
    be violated by construction (type-design QA convergence).
    """
    country: str | None
    confidence: float

    def __post_init__(self) -> None:
        # NaN / +inf / -inf evade the bare `max/min` clamp at Python's
        # comparison semantics — `min(1.0, nan) == 1.0`. Reject all
        # non-finite values; the parser clamps real-valued inputs into
        # range, so this only fires for hand-built results.
        import math
        if not math.isfinite(self.confidence):
            raise ValueError(
                f"confidence must be a finite float in [0.0, 1.0]; "
                f"got {self.confidence!r}"
            )
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                f"confidence must be in [0.0, 1.0]; got {self.confidence!r}"
            )

    @property
    def is_latam(self) -> bool:
        """`True` iff `country` is in the canonical LATAM set. Derived
        from `country` so the invariant cannot be violated by construction.
        """
        return _is_latam_country(self.country)


def _is_latam_country(country: str | None) -> bool:
    if not country:
        return False
    return country.strip().lower() in _LATAM_COUNTRIES_LOWER


def _parse_hq_response(raw_text: str, company_name: str) -> HQClassificationResult | None:
    """Parse Haiku JSON response. Returns None on parse failure so the
    caller treats it as data-missing (mirrors `classify_industry`'s
    None-vs-Other semantics — a parse error is NOT a confirmed unknown).
    """
    import math

    text = (raw_text or "").strip()
    # Defensive: strip markdown code fences if Haiku ignored the
    # single-line rule.
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError) as err:
        logger.warning(
            "HQ classifier returned unparseable JSON for %r (%s): %r",
            company_name, type(err).__name__, raw_text,
        )
        return None
    if not isinstance(parsed, dict):
        logger.warning(
            "HQ classifier returned non-object for %r: %r",
            company_name, parsed,
        )
        return None
    country_raw = parsed.get("country")
    if not isinstance(country_raw, str) or not country_raw.strip():
        logger.warning(
            "HQ classifier returned missing/non-string country for %r: %r",
            company_name, parsed,
        )
        return None
    country = country_raw.strip().lower()
    confidence_raw = parsed.get("confidence", 0.0)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        logger.warning(
            "HQ classifier returned non-numeric confidence for %r: %r",
            company_name, parsed,
        )
        confidence = 0.0
    # Math-QA convergence: NaN / Infinity from Haiku's permissive JSON
    # encoding would silently promote to confidence=1.0 under bare clamp
    # semantics. Reject non-finite values to 0.0 BEFORE the clamp.
    if not math.isfinite(confidence):
        logger.warning(
            "HQ classifier returned non-finite confidence for %r: %r",
            company_name, confidence,
        )
        confidence = 0.0
    # Clamp into [0.0, 1.0] — Haiku occasionally returns 1.0+ or
    # negative for hedging. Don't bounce on it; clamp + continue.
    confidence = max(0.0, min(1.0, confidence))
    if country == "unknown":
        # Explicit-unknown: Haiku said "I don't know" with a confidence
        # score. Distinguish from a parse error (which returns None).
        # The backfill path uses this distinction to skip the Attio
        # write entirely (vs. writing a sentinel string that would
        # block re-classification next sweep — cross-QA convergence
        # on the §0 #9 silent-fallback risk).
        return HQClassificationResult(country=None, confidence=confidence)
    return HQClassificationResult(country=country, confidence=confidence)


def classify_company_hq(
    company_name: str | None,
    domain: str | None = None,
    *,
    anthropic_client=None,
    use_llm_dispatch: bool = False,
) -> HQClassificationResult | None:
    """Classify a company's global HQ country via Haiku.

    Returns an `HQClassificationResult` on a successful classification
    (including the explicit-unknown case where `country=None,
    confidence=<n>`), or `None` when the call could not run (dispatch
    off + no client, dispatch timeout, cost-ceiling exhausted, parse
    error). The None-vs-result distinction matches `classify_industry`:
    None = data-missing (retry later), result-with-`country=None` =
    Haiku confirmed it doesn't know.

    Per §0 invariant #11, the production path is the F-PR-9 file-based
    LLM dispatch (`OUTBOUND_USE_LLM_DISPATCH=1` set by the parent skill).
    The `anthropic_client` kwarg is the test-injection seam — mirrors
    `classify_industry` and the rest of the classifier suite.
    """
    if not company_name:
        logger.warning("classify_company_hq called with empty company_name")
        return None

    user_parts = [f"Company: {company_name}"]
    if domain:
        user_parts.append(f"Domain: {domain}")
    user_content = "\n".join(user_parts)

    if anthropic_client is None:
        from workflows.llm_dispatch import (
            CostCeilingExhausted,
            LLMDispatchFailed,
            LLMDispatchTimeout,
            is_dispatch_enabled,
            request_llm_dispatch,
        )
        if not (use_llm_dispatch or is_dispatch_enabled()):
            return None
        try:
            dispatch_result = request_llm_dispatch(
                step="company_hq_classifier",
                prompt=user_content,
                system=HQ_CLASSIFIER_SYSTEM_PROMPT,
                model_class="haiku",
                max_tokens=HQ_CLASSIFIER_MAX_TOKENS,
                schema_hint=(
                    'Return ONE single-line JSON object: '
                    '{"country": "<name>", "confidence": <0.0-1.0>}'
                ),
            )
            raw_text = dispatch_result.raw_text
        except (LLMDispatchTimeout, LLMDispatchFailed, CostCeilingExhausted) as err:
            # Cost-ceiling caught alongside transient dispatch errors —
            # matches `classify_industry`'s semantics (treat as data-
            # missing so callers retry next sweep). Operators surface
            # cost-ceiling separately via dispatch log breadcrumbs.
            logger.warning(
                "HQ classifier dispatch error (%s: %s) for %r — treating as unknown",
                type(err).__name__, err, company_name,
            )
            return None
        return _parse_hq_response(raw_text, company_name)

    # Test-injection path: legacy Anthropic SDK shape, mirrors
    # `_llm_qualify` and `classify_industry`.
    try:
        response = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=HQ_CLASSIFIER_MAX_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": HQ_CLASSIFIER_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_content}],
        )
        raw_text = next(
            (getattr(b, "text", "") for b in (getattr(response, "content", []) or [])
             if getattr(b, "type", None) == "text"),
            "",
        )
        return _parse_hq_response(raw_text, company_name)
    except Exception as err:  # noqa: BLE001 — test client boundary
        logger.warning(
            "HQ classifier client error (%s: %s) for %r — treating as unknown",
            type(err).__name__, err, company_name,
        )
        return None


def backfill_company_hq_for_missing(
    attio: AttioClient,
    *,
    anthropic_client=None,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Scan Attio companies, classify HQ country for any missing the
    `company_hq_country` attribute, and PATCH the company record.

    Idempotent — only touches records without a populated
    `company_hq_country`. Re-running the same week with the same
    classifier version is a no-op for records that succeeded; records
    that returned `None` (parse error / dispatch failure / cost
    ceiling) stay null so the next sweep picks them up.

    Args:
        attio: AttioClient instance.
        anthropic_client: Test seam. Production runs with None and
            depends on the F-PR-9 dispatch path (skill exports
            `$OUTBOUND_USE_LLM_DISPATCH=1`).
        limit: If set, stop after classifying this many missing
            records. `total_scanned` still counts all paged.
        dry_run: Count but do not write to Attio.

    Returns: summary dict with keys total_scanned, missing, classified,
        written, api_errors, skipped, latam_count, non_latam_count,
        confirmed_unknown_count. The invariant
        `classified == latam_count + non_latam_count + confirmed_unknown_count`
        holds — every classification lands in exactly one of the three
        buckets (math + GTM + prospect-weekly QA convergence).
    """
    summary: dict = {
        "total_scanned": 0,
        "missing": 0,
        "classified": 0,
        "written": 0,
        "api_errors": 0,
        "skipped": 0,
        "latam_count": 0,
        "non_latam_count": 0,
        "confirmed_unknown_count": 0,
    }

    # Same pagination cap as backfill_missing_industries. If your sales
    # DB grows past 2000 companies, both will need to switch to paged
    # iteration.
    BACKFILL_SCAN_LIMIT = 2000
    all_companies = attio.search_companies(filter_=None, limit=BACKFILL_SCAN_LIMIT)
    summary["total_scanned"] = len(all_companies)
    if len(all_companies) >= BACKFILL_SCAN_LIMIT:
        logger.warning(
            "backfill_company_hq_for_missing: pagination cap of %d hit — "
            "some companies may have been missed.",
            BACKFILL_SCAN_LIMIT,
        )

    missing_records: list[dict] = []
    for record in all_companies:
        values = record.get("values", {})
        hq = values.get("company_hq_country", [])
        if not hq:
            missing_records.append(record)
            continue
        # hq is present but may be empty.
        first = hq[0] if isinstance(hq, list) else hq
        if isinstance(first, dict):
            v = first.get("value") or first.get("text")
            if not v:
                missing_records.append(record)

    summary["missing"] = len(missing_records)

    to_classify = missing_records if limit is None else missing_records[:limit]

    for i, record in enumerate(to_classify, 1):
        values = record.get("values", {})

        name_data = values.get("name", [])
        if not name_data:
            summary["skipped"] += 1
            continue
        name = name_data[0].get("value", "") if isinstance(name_data[0], dict) else ""
        if not name:
            summary["skipped"] += 1
            continue

        domain = None
        domain_data = values.get("domains", [])
        if domain_data and isinstance(domain_data, list):
            first_domain = domain_data[0]
            if isinstance(first_domain, dict):
                domain = first_domain.get("domain") or first_domain.get("value")

        record_id = record.get("id", {}).get("record_id", "")
        if not record_id:
            summary["skipped"] += 1
            continue

        result = classify_company_hq(name, domain, anthropic_client=anthropic_client)
        if result is None:
            # Transient — leave the field null so the next sweep retries.
            summary["api_errors"] += 1
            continue
        summary["classified"] += 1

        if result.country is None:
            # Explicit-unknown: Haiku said "I don't know". Mirror the
            # `classify_industry` precedent — SKIP THE WRITE so the
            # record stays null and a future improved prompt or a
            # later sweep can re-classify. Writing a `"unknown"`
            # sentinel here would block re-classification on the next
            # sweep via the populated-field skip, permanently pinning
            # the record. (Cross-QA convergence on §0 #9.)
            summary["confirmed_unknown_count"] += 1
        elif result.is_latam:
            summary["latam_count"] += 1
            if not dry_run:
                _write_hq_attrs(attio, record_id, name, result, summary)
        else:
            summary["non_latam_count"] += 1
            if not dry_run:
                _write_hq_attrs(attio, record_id, name, result, summary)

        if i % 20 == 0:
            logger.info("HQ classifier sweep: classified %d/%d", i, len(to_classify))

        if not dry_run and i < len(to_classify):
            # Skip sleep in dry-run (no network calls) and after the
            # final record (no next iteration to space out).
            time.sleep(0.2)

    return summary


def _write_hq_attrs(
    attio: AttioClient,
    record_id: str,
    company_name: str,
    result: HQClassificationResult,
    summary: dict,
) -> None:
    """Patch the company record with the HQ classification result.

    Broader httpx-exception catch than `update_company`'s
    `HTTPStatusError`-only contract — also catches `RequestError` /
    `TimeoutException` so a transient network blip mid-sweep doesn't
    kill the whole batch (silent-failure-hunter QA convergence). Both
    arms increment `api_errors` and log a per-record WARNING; the run
    continues so the summary's per-bucket counters stay meaningful.
    """
    update_attrs = {
        "company_hq_country": result.country,
        "company_hq_confidence": result.confidence,
    }
    # Wave-2-B §3.15 cleanup: route through AttioWriter. The pre-Wave-2
    # bypass used attio.update_company directly so the company_hq_*
    # writer-module registration was documentation-only. Registry-
    # attributed to the parent classify_company_hq writer per the
    # manifest (the helper just stamps the result of that classifier).
    from clients.attio_writer import (
        AttioError as _AttioError_hq,
    )
    from clients.attio_writer import (
        AttioWriter as _AttioWriter_hq,
    )
    from clients.attio_writer import (
        WriteIntent as _WriteIntent_hq,
    )
    _hq_writer = _AttioWriter_hq(attio=attio)
    try:
        _hq_writer.apply(_WriteIntent_hq(
            object="companies",
            record_id=record_id,
            updates=update_attrs,
            prior_values={},
            writer_module="workflows.weekly_prospect.classify_company_hq",
        ))
        summary["written"] += 1
    except _AttioError_hq as err:
        # AttioWriter already DLQ'd + opened the attio_write_failed
        # queue row. Tally locally for the sweep summary line; the
        # operator surface is the queue row.
        logger.warning(
            "backfill_company_hq_for_missing: AttioWriter failed for "
            "%r (%s): %s — continuing sweep",
            company_name, record_id, err,
        )
        summary["api_errors"] += 1
    except httpx.HTTPStatusError as err:
        # Defense in depth: should not fire after the AttioWriter
        # route, but kept so a future regression that re-introduces
        # a raw httpx error stays operator-visible.
        logger.warning(
            "backfill_company_hq_for_missing: update_company HTTP %s "
            "for %r (%s): %s",
            err.response.status_code, company_name, record_id, err,
        )
        summary["api_errors"] += 1
    except (httpx.RequestError, httpx.TimeoutException) as err:
        logger.warning(
            "backfill_company_hq_for_missing: update_company network "
            "error for %r (%s): %s — continuing sweep",
            company_name, record_id, err,
        )
        summary["api_errors"] += 1


def _open_disqualifier_match_row(
    crm: CRMProvider,
    prospect_data: dict,
    score_result: dict,
) -> None:
    """Open a typed `disqualifier_match` Operator Review Queue row.

    Idempotency key `f"{linkedin_url}|{verdict_path}"` makes re-runs
    no-ops via F-PR-3's escalate() contract. The `matched_keyword`
    field carries the specific phrase that triggered the family,
    enabling operator audit of keyword false-positives.
    """
    linkedin_url = prospect_data.get("linkedin_url") or ""
    verdict_path = score_result.get("verdict_path") or ""
    payload = {
        "linkedin_url": linkedin_url,
        "title": str(prospect_data.get("title") or ""),
        "company": str(prospect_data.get("company") or ""),
        "verdict_path": verdict_path,
        "matched_keyword": str(score_result.get("disqualifier_keyword") or ""),
        "score": int(score_result.get("score") or 0),
    }
    escalate(
        type="disqualifier_match",
        idempotency_key=f"{linkedin_url}|{verdict_path}",
        payload=payload,
        attio=crm,
    )


def _load_in_list_canonical_urls(
    crm: CRMProvider, existing_entries: list[Entry]
) -> set[str]:
    """Build the set of canonical LinkedIn URLs already in the pipeline list.

    Reads the entry-level `canonical_linkedin_url` where present (free — the
    entries are already fetched), and resolves the rest from the parent person
    records in a single bulk read (legacy entries predate that attribute, so
    relying on it alone would miss exactly the old records that get recycled).
    Per-record fetch failures are isolated by the provider's `bulk_fetch_persons`
    and simply leave that URL out of the set — the candidate then falls back to
    the live-search dedup, so a partial CRM outage degrades to current behaviour,
    never worse.

    The bulk read and the per-person URL extraction both go through the
    vendor-neutral `CRMProvider` contract (`bulk_fetch_persons` →
    `{record_id: Record}`, `extract_person_info` → `RecordInfo.linkedin_url`)
    so this stays adapter-agnostic.
    """
    urls: set[str] = set()
    need_lookup: set[str] = set()
    for entry in existing_entries:
        entry_url = entry.attributes.get("canonical_linkedin_url") or ""
        canonical = _canonical_linkedin_url(entry_url) if entry_url else ""
        if canonical:
            urls.add(canonical)
        elif entry.record_id:
            need_lookup.add(entry.record_id)

    if need_lookup:
        persons = crm.bulk_fetch_persons(need_lookup)
        resolved = 0
        for person in persons.values():
            url = crm.extract_person_info(person).linkedin_url
            canonical = _canonical_linkedin_url(url) if url else ""
            if canonical:
                urls.add(canonical)
                resolved += 1
        # Surface partial under-coverage loudly: a bulk-fetch outage or
        # missing `linkedin` fields silently shrinks the dedup set, letting
        # those records fall back to the flaky live search (the exact recycle
        # path this fix closes). Better to see it than to silently regress.
        if resolved < len(need_lookup):
            logger.warning(
                "in_list_canonical_urls: %d/%d gap records did not resolve to a "
                "URL (bulk-fetch failure or missing linkedin) — dedup under-covers "
                "them; they fall back to live-search",
                len(need_lookup) - resolved, len(need_lookup),
            )

    if existing_entries and not urls:
        logger.warning(
            "in_list_canonical_urls empty: %d entries scanned, 0 resolved to a "
            "canonical URL — the recycle-fix dedup will not fire this run",
            len(existing_entries),
        )
    return urls


def _resolve_company_industry(
    attio: AttioClient,
    company_name: str,
    domain: str | None,
    cache: dict[str, tuple[str | None, str | None]],
    *,
    dry_run: bool,
    anthropic_client=None,
    summary: dict | None = None,
) -> tuple[str | None, str | None]:
    """Resolve `(industry_vertical, industry_vertical_status)` for a prospect's
    parent company, classifying at ingest time when the company has no label yet.

    Resolution order (PR-25 follow-up — closes the TODO at quality_gate.py
    `score_prospect`, which read `industry`/`industry_vertical_status` from
    prospect_data that nothing populated):

    1. Company record exists in the CRM with industry_vertical → return its
       label + status. A label with no status returns status=None, which
       score_prospect treats as "confirmed" — deliberate back-compat with
       pre-PR-25 rows (scrape / manual / operator-confirmed labels never carried
       a status).
    2. Company exists without a label → classify via the LLM-dispatch path and
       PATCH the company with the same payload backfill_missing_industries
       writes (label + haiku_classifier provenance + low_confidence + 0.0).
    3. No company record yet → classify; the label rides prospect_data into
       _commit_prospect, which stamps it on the company CREATE.

    Classification and the write-back are skipped in dry_run (no LLM spend, no
    CRM writes) — dry-run scores may therefore lack the industry component for
    never-seen companies, consistent with dry-run being a content QA pass, not
    a score preview.

    Returns (None, None) when the company is unknown and unclassifiable —
    score_prospect treats missing industry as neutral, never as "Other".
    Transient per-company CRM errors degrade to (None, None) with a warning;
    auth errors (401/403) propagate so the run fails loud. (PR-225)
    """
    if not company_name:
        return None, None
    cache_key = normalize_company_name(company_name) or company_name.strip().lower()
    if cache_key in cache:
        return cache[cache_key]

    from workflows.industry_classifier import build_classifier_payload, classify_industry

    result: tuple[str | None, str | None] = (None, None)
    try:
        record = find_company_record(attio, company_name, domain or None)
        if record is not None:
            values = record.get("values", {})
            label: str | None = first_option_title(values.get("industry_vertical"))
            if label:
                status = first_option_title(values.get("industry_vertical_status"))
                result = (label, status or None)
            elif not dry_run:
                label = classify_industry(
                    company_name, domain or None, anthropic_client=anthropic_client
                )
                if label:
                    record_id = record.get("id", {}).get("record_id", "")
                    if record_id:
                        attio.update_company(record_id, build_classifier_payload(label))
                    result = (label, "low_confidence")
                    if summary is not None:
                        summary["industry_classified_at_ingest"] = (
                            summary.get("industry_classified_at_ingest", 0) + 1
                        )
        elif not dry_run:
            # No company record yet — classify now so the score carries the
            # industry signal; _commit_prospect stamps the label on CREATE.
            label = classify_industry(
                company_name, domain or None, anthropic_client=anthropic_client
            )
            if label:
                result = (label, "low_confidence")
                if summary is not None:
                    summary["industry_classified_at_ingest"] = (
                        summary.get("industry_classified_at_ingest", 0) + 1
                    )
    except httpx.HTTPStatusError as err:
        if err.response.status_code in (401, 403):
            raise  # auth failure — every later call fails too; abort loud
        logger.warning(
            "_resolve_company_industry: HTTP %s for %r — scoring without industry",
            err.response.status_code, company_name,
        )
        if summary is not None:
            summary["industry_resolve_errors"] = summary.get("industry_resolve_errors", 0) + 1
    except (httpx.ConnectError, httpx.TimeoutException) as err:
        logger.warning(
            "_resolve_company_industry: network error for %r — scoring without "
            "industry: %s", company_name, err,
        )
        if summary is not None:
            summary["industry_resolve_errors"] = summary.get("industry_resolve_errors", 0) + 1

    cache[cache_key] = result
    return result


def _enrich_prospect_industry(
    attio: AttioClient,
    prospect_data: dict,
    raw: dict,
    cache: dict[str, tuple[str | None, str | None]],
    *,
    dry_run: bool,
    anthropic_client=None,
    summary: dict | None = None,
) -> None:
    """Stamp `industry` / `industry_vertical_status` onto prospect_data from
    the parent company (see _resolve_company_industry). Mutates in place; the
    status key is only set when a status exists — an absent key means
    score_prospect's back-compat "confirmed" default applies. (PR-225)"""
    industry, industry_status = _resolve_company_industry(
        attio,
        prospect_data["company"],
        extract_real_domain(raw),
        cache,
        dry_run=dry_run,
        anthropic_client=anthropic_client,
        summary=summary,
    )
    if industry:
        prospect_data["industry"] = industry
        if industry_status:
            prospect_data["industry_vertical_status"] = industry_status


def _process_prospects(
    prospects_raw: list[dict],
    crm: CRMProvider,
    list_id: str,
    today: str,
    dry_run: bool,
    summary: dict,
    seen_urls: set[str],
    in_list_record_ids: set[str],
    persona_config: dict | None = None,
    borderline_stage: list | None = None,
    reprospect_review: list | None = None,
    *,
    anthropic_client=None,
    existing_entries: list[Entry] | None = None,
    seen_urls_midmarket: set[str] | None = None,
    in_list_canonical_urls: set[str] | None = None,
    name_index: NameIndex | None = None,
    industry_cache: dict[str, tuple[str | None, str | None]] | None = None,
) -> None:
    """Score, dedup, and load prospects into Attio. Mutates summary and seen_urls in place.

    When persona_config has target_company_mode=true, scoring uses mid-market-optimized
    size bands and assigns the triggering persona directly rather than re-routing by title.
    Profiles whose company doesn't match the target list are skipped (PB accumulates
    results across runs, so the CSV often contains profiles from previous searches).

    Cross-search persona upgrade: when a prospect already processed by an
    earlier enterprise_mode search is now matched by a target_company_mode
    (midmarket) search, re-score with the midmarket persona_config and
    upgrade the existing borderline entry in place. Midmarket has the
    strictest filter (explicit target company list), so its persona tag is
    always more correct than the enterprise broad-keyword match. Within
    enterprise mode, first-match still wins — acceptable given the three
    enterprise personas share the same DM intent (ICP 1).
    """
    is_midmarket = bool(persona_config and persona_config.get("target_company_mode"))
    if industry_cache is None:
        industry_cache = {}
    # Industry enrichment (PR-225) needs the raw Attio escape hatch. A CRM
    # provider without one (a non-Attio provider, or a spec'd test double)
    # simply scores without the ingest-time industry signal (neutral) — the
    # loud failure still fires at _commit_prospect when a real write happens.
    try:
        _attio: AttioClient | None = _attio_inner_client(crm)
    except TypeError:
        _attio = None

    # Load target company filter when in target_company_mode
    target_fragments: set[str] = set()
    if is_midmarket:
        # is_midmarket is True only when persona_config is truthy (line ~1509),
        # so it cannot be None here — narrow for the type checker.
        assert persona_config is not None
        target_list_key = persona_config.get("target_company_list", "")
        if target_list_key:
            target_fragments = _load_target_company_names(target_list_key)
            if target_fragments:
                click.echo(f"    [Target company filter active: {len(target_fragments)} name fragments loaded]")

    for raw in prospects_raw:
        prospect_data = {
            "name": raw.get("fullName", raw.get("name", "")) or _join_name(raw),
            "title": raw.get("title", raw.get("headline", raw.get("currentPosition", ""))),
            "company": raw.get("company", raw.get("companyName", raw.get("currentCompanyName", ""))),
            "location": raw.get("location", ""),
            "linkedin_url": raw.get("defaultProfileUrl", raw.get("linkedinProfileUrl", raw.get("linkedInUrl", raw.get("profileUrl", "")))),
            # No employee_count: SN search exports carry no headcount column
            # (the old 4-column fallback chain silently returned "" on 100% of
            # rows — 2026-07-06 RCA). Company size is a search-level signal now:
            # score_prospect reads `search_size_credit` from the persona config
            # instead of per-row data. (PR-227)
        }

        if not prospect_data["linkedin_url"]:
            continue

        # Target company filter: skip profiles not on the curated list
        if target_fragments and not _matches_target_company(prospect_data["company"], target_fragments):
            summary.setdefault("filtered_out", 0)
            summary["filtered_out"] += 1
            continue

        # Canonicalize LinkedIn URL using the same transform Attio applies
        # internally (lowercase, strip www., strip trailing slash, decode %xx).
        # Sharing this transform keeps the in-run dedup set aligned with
        # Attio's stored form — avoids the duplicate-record class of bug
        # caused by URL-form drift across percent-encoded accented chars.
        url = _canonical_linkedin_url(prospect_data["linkedin_url"])
        prospect_data["linkedin_url"] = url

        # In-run dedup (PB CSV accumulates across launches).
        #
        # When a midmarket search now matches a prospect that was first
        # processed by an enterprise search, upgrade the existing borderline
        # entry's persona/lane/breakdown to the midmarket result. Midmarket
        # has the curated target-company-list filter, so its tag is more
        # specific than enterprise broad-keyword matches. Tracked via
        # `seen_urls_midmarket` (None when caller hasn't opted in, in which
        # case we preserve the legacy behavior).
        if url in seen_urls:
            summary["duplicates"] += 1
            if (
                is_midmarket
                and seen_urls_midmarket is not None
                and url not in seen_urls_midmarket
                and borderline_stage is not None
            ):
                if _attio is not None:
                    _enrich_prospect_industry(
                        _attio, prospect_data, raw, industry_cache,
                        dry_run=dry_run, anthropic_client=anthropic_client,
                        summary=summary,
                    )
                new_result = score_prospect(
                    prospect_data, persona_config=persona_config, agent_gate=True,
                )
                if new_result.get("needs_agent_qualification"):
                    for entry in borderline_stage:
                        if entry["linkedin_url"] == url:
                            entry["persona"] = new_result["persona"]
                            entry["language"] = new_result["language"]
                            entry["score"] = new_result["score"]
                            entry["qualification_prompt"] = new_result["qualification_prompt"]
                            entry["score_breakdown"] = new_result.get("score_breakdown")
                            entry["scoring_lane"] = new_result.get("scoring_lane")
                            seen_urls_midmarket.add(url)
                            summary.setdefault("persona_upgraded_to_midmarket", 0)
                            summary["persona_upgraded_to_midmarket"] += 1
                            click.echo(
                                f"      → [PERSONA UPGRADE] {prospect_data['name']} "
                                f"→ {new_result['persona']} (was enterprise)"
                            )
                            break
            continue
        seen_urls.add(url)
        if is_midmarket and seen_urls_midmarket is not None:
            seen_urls_midmarket.add(url)

        # PR-25 follow-up: resolve the parent company's industry (CRM lookup,
        # classify-at-ingest when missing) before scoring so the industry
        # component and abstain gate actually fire for new ingest. Runs after
        # the dedup guard so cross-launch duplicate rows never pay the lookup;
        # the shared cache makes same-company re-encounters free.
        if _attio is not None:
            _enrich_prospect_industry(
                _attio, prospect_data, raw, industry_cache,
                dry_run=dry_run, anthropic_client=anthropic_client, summary=summary,
            )

        click.echo(f"    Scoring: {prospect_data['name']} ({prospect_data['company']})...")
        score_result = score_prospect(prospect_data, persona_config=persona_config, agent_gate=True)
        summary["scored"] += 1
        # Signal-health counters (PR-227): a component that abstains/misses on
        # ~100% of a run means a dead signal (the 2026-07-06 RCA class of
        # failure) — the end-of-run SIGNAL alarm reads these. Size "abstained"
        # means the persona declared NO search_size_credit; a configured credit
        # of 0 is a deliberate zero, not an abstain, and must not alarm.
        if (persona_config or {}).get("search_size_credit") is None:
            summary.setdefault("size_abstained", 0)
            summary["size_abstained"] += 1
        if not prospect_data.get("industry"):
            summary.setdefault("industry_missing", 0)
            summary["industry_missing"] += 1

        if score_result.get("needs_agent_qualification"):
            # Borderline — stage for agent-driven Haiku qualification
            name = prospect_data["name"]
            score = score_result["score"]
            click.echo(f"      → [AGENT QUALIFY] {name} — staged (score={score})")
            summary.setdefault("borderline_staged", 0)
            summary["borderline_staged"] += 1
            # PR-222 Rec D: attribute infra-driven stagings to their own bucket
            # (a normal no-client borderline sets none of these flags). Keeps
            # the cost-ceiling / transient-error / ledger-outage cases greppable
            # apart from routine borderline staging.
            if score_result.get("cost_exhausted_staged"):
                summary.setdefault("cost_exhausted_staged", 0)
                summary["cost_exhausted_staged"] += 1
            if score_result.get("llm_error_staged"):
                summary.setdefault("llm_error_staged", 0)
                summary["llm_error_staged"] += 1
            if score_result.get("ledger_unavailable"):
                summary.setdefault("ledger_unavailable_staged", 0)
                summary["ledger_unavailable_staged"] += 1
            if borderline_stage is not None:
                borderline_stage.append({
                    "linkedin_url": prospect_data["linkedin_url"],
                    "prospect_data": prospect_data,
                    "raw_csv_row": raw,
                    "persona": score_result["persona"],
                    "language": score_result["language"],
                    "score": score_result["score"],
                    "qualification_prompt": score_result["qualification_prompt"],
                    "score_breakdown": score_result.get("score_breakdown"),
                    "scoring_lane": score_result.get("scoring_lane"),
                })
            continue

        if not score_result["pass"]:
            click.echo(f"      → Rejected (score: {score_result['score']}): {', '.join(score_result['reasons'][:2])}")
            summary["rejected"] += 1
            verdict_path = score_result.get("verdict_path") or "unknown"
            summary["rejected_by_path"][verdict_path] = (
                summary["rejected_by_path"].get(verdict_path, 0) + 1
            )
            # PR-26: open a typed Operator Review Queue row when the rejection
            # came from one of the keyword disqualifier families. The queue
            # row exists so operators can audit keyword false-positives that
            # OPS_OVERRIDE did NOT bypass — the rejection stands, but the
            # operator now has per-prospect visibility on what fired and why.
            # Idempotency on `(linkedin_url, verdict_path)` is enforced by
            # F-PR-3's escalate() so weekly re-runs are no-ops.
            if verdict_path in DISQUALIFIER_VERDICT_PATHS:
                _open_disqualifier_match_row(crm, prospect_data, score_result)
            continue

        summary["qualified"] += 1
        if score_result.get("verdict_path") in DETERMINISTIC_PASS_PATHS:
            # Deterministic passes counted apart from LLM borderline passes —
            # this run-level number was invisible before the 2026-07-06 RCA
            # (0 deterministic qualifies for 3 months, unnoticed). Membership
            # comes from quality_gate.DETERMINISTIC_PASS_PATHS so a future
            # lane's pass path cannot fall out of the count. (PR-227)
            summary.setdefault("deterministic_qualified", 0)
            summary["deterministic_qualified"] += 1

        # Authoritative dedup (2026-06-16 recycle fix): check the candidate's
        # canonical URL against the set of URLs already in the pipeline, built
        # once at run start from the list itself. This is immune to the
        # eventual-consistency misses of the live `search_person_by_linkedin`
        # below — a miss there used to let an already-listed record fall through
        # to _commit_prospect, which re-stamped its existing entry's cohort
        # fields (week_starting/prospect_committed_at/invite_eligible_after/
        # dm_step) and recycled it as a "fresh" PROSPECT (Pattern-A pollution).
        # Catching it here means a search miss can no longer recycle an
        # existing record.
        canonical = _canonical_linkedin_url(prospect_data["linkedin_url"])
        if in_list_canonical_urls and canonical and canonical in in_list_canonical_urls:
            click.echo("      → Already in pipeline (canonical-URL match) — skipping")
            summary["duplicates"] += 1
            continue

        existing = crm.search_person_by_linkedin(prospect_data["linkedin_url"])
        if existing:
            record_id = existing.record_id
            if record_id in in_list_record_ids:
                click.echo("      → Already in pipeline list, skipping")
                summary["duplicates"] += 1
                continue
            # Person record exists in Attio but no current pipeline list entry.
            # Almost always means a prior cadence cycle whose list entry was
            # cleaned up (dedup, terminal-state pruning) — re-adding as PROSPECT
            # erases the cadence history and can re-invite already-connected or
            # dismissed people. Stage for manual review instead.
            #
            # Caused the 2026-05-08 stale-accept incident: 3 records re-added by
            # the 2026-05-03 weekly run on top of person records whose prior
            # entries were removed in the 2026-04-21 dedup. They got "invited"
            # again, LinkedIn no-op'd (existing connections), Phase 0 mistook
            # the no-op for fresh acceptance.
            click.echo(
                "      → In Attio, no current list entry — staged for manual review "
                "(prior cadence may exist; not auto-prospecting)"
            )
            summary.setdefault("reprospect_review", 0)
            summary["reprospect_review"] += 1
            if reprospect_review is not None:
                reprospect_review.append({
                    "name": prospect_data["name"],
                    "company": prospect_data["company"],
                    "title": prospect_data["title"],
                    "linkedin_url": prospect_data["linkedin_url"],
                    "record_id": record_id,
                    "score": score_result.get("score"),
                    "persona": score_result.get("persona"),
                    "language": score_result.get("language"),
                })
            continue

        # Secondary dedup gate (PR-241 René RCA): URL-keyed dedup above cannot
        # catch a re-created prospect under a DIFFERENT LinkedIn vanity slug
        # (same human, new URL). Look the candidate up in the run-start name
        # index (normalized on both sides so accents + LinkedIn suffixes bridge
        # by construction — the old live search was accent-SENSITIVE and missed
        # exactly these variants). A hit under a different URL that shares a
        # company is a suspected URL-variant duplicate. Do NOT auto-merge —
        # stage into the same reprospect_review flow as layer 3 so an operator
        # decides.
        dup_record_id = None
        if name_index is not None:
            dup_record_id = _find_name_company_duplicate(
                name_index,
                prospect_data["name"], prospect_data["company"],
                candidate_canonical_url=canonical,
            )
        if dup_record_id:
            click.echo(
                f"      → name+company match with {dup_record_id} — suspected "
                f"URL-variant duplicate; staged for manual review (not committed)"
            )
            summary.setdefault("reprospect_review", 0)
            summary["reprospect_review"] += 1
            if reprospect_review is not None:
                reprospect_review.append({
                    "name": prospect_data["name"],
                    "company": prospect_data["company"],
                    "title": prospect_data["title"],
                    "linkedin_url": prospect_data["linkedin_url"],
                    "record_id": dup_record_id,
                    "score": score_result.get("score"),
                    "persona": score_result.get("persona"),
                    "language": score_result.get("language"),
                    "reason": (
                        f"name+company match — suspected URL-variant duplicate "
                        f"of {dup_record_id}"
                    ),
                })
            continue

        if dry_run:
            click.echo(
                f"      → [DRY RUN] Would add: score={score_result['score']}, "
                f"persona={score_result['persona']}, lang={score_result['language']}"
            )
            continue

        # Upsert by LinkedIn URL so repeated runs never create duplicates,
        # even if search_person_by_linkedin missed due to transient errors
        # or URL-encoding drift (e.g. accented characters).
        ok = _commit_prospect(
            crm, prospect_data, raw, score_result, list_id, today,
            anthropic_client=anthropic_client,
            existing_entries=existing_entries,
            in_list_record_ids=in_list_record_ids,
            summary=summary,
        )
        if not ok:
            summary.setdefault("write_errors", 0)
            summary["write_errors"] += 1
            continue

        summary["added"] += 1
        click.echo(
            f"      → Added: score={score_result['score']}, "
            f"persona={score_result['persona']}, lang={score_result['language']}"
        )


def _is_supply_starved(summary: dict, dry_run: bool) -> bool:
    """True when a real (non-dry) run qualified candidates but sourced zero
    net-new prospects — every qualified candidate was already in the pipeline
    (re-stamped on commit, skipped as a duplicate, or staged for re-prospect
    review). This is the 2026-06-16 silent-starvation signature: the run looks
    successful ("773 scored / 417 passing") while adding nothing.
    """
    if dry_run:
        return False
    if not summary.get("qualified"):
        return False
    return summary.get("net_new_created", 0) == 0


# Minimum scored-prospect count before the deterministic-qualifier and
# dead-signal alarms are allowed to fire — a tiny run legitimately produces
# zeros and 100%-missing rates, and alarming on it trains operators to
# ignore the alarm. (PR-227)
SIGNAL_ALARM_MIN_SCORED = 50
# A signal that is missing/abstained on more than this share of a run is a
# dead signal (the 2026-07-06 RCA class: industry unwired, size defaulting
# on 100% of rows for 3 months, unnoticed).
SIGNAL_DEAD_THRESHOLD = 0.9

# Smallest search_size_credit at which a deterministic pass is geometrically
# reachable, DERIVED from the scorer's own named constants: the best non-size
# hand is decision-maker role + non-competitor + confirmed in-ICP industry,
# and the pass gate is strictly > DETERMINISTIC_PASS_THRESHOLD. Deriving (not
# hardcoding) means a recalibration of any component in quality_gate moves
# this line automatically instead of silently invalidating the alarm gating
# below. The 2026-07-06 calibration measured the auto-pass cell below the 80%
# enable bar, so shipped configs deliberately sit one below the line. (PR-227)
DETERMINISTIC_REACHABLE_MIN_CREDIT = (
    DETERMINISTIC_PASS_THRESHOLD + 1
    - (DECISION_MAKER_ROLE_CREDIT + NON_COMPETITOR_CREDIT + INDUSTRY_BONUS_IN_ICP)
)


def _deterministic_pass_reachable(
    personas_data: dict, searched_persona_keys: set[str] | None = None
) -> bool:
    """True when a persona that actually contributed rows makes the
    deterministic pass geometrically reachable (see
    DETERMINISTIC_REACHABLE_MIN_CREDIT). While every searched credit sits
    below the line, 0 deterministic qualifications is the *configured*
    outcome, not a dead signal — the alarm must not fire on it.

    `searched_persona_keys` scopes the check to the personas this run
    harvested (from _get_all_searches): a high-credit persona whose search
    never ran cannot produce passes, so counting it would arm a false alarm
    (and paused/deprecated personas are naturally excluded). (PR-227)"""
    for key, persona in (personas_data or {}).items():
        if searched_persona_keys is not None and key not in searched_persona_keys:
            continue
        credit = persona.get("search_size_credit")
        if credit is None:
            continue
        try:
            if int(credit) >= DETERMINISTIC_REACHABLE_MIN_CREDIT:
                return True
        except (ValueError, TypeError):
            # Malformed credit — load_personas validation rejects these at
            # startup; a hand-built dict reaching here must not crash the
            # end-of-run alarm block after commits already happened.
            continue
    return False


def _run_alarm_eligible(summary: dict, dry_run: bool) -> bool:
    """Shared gate for the signal-health alarms: only real (wet) runs of
    meaningful size may alarm. Dry runs skip industry classification (no LLM
    spend) so their miss-rates are 100% by construction, and tiny runs
    legitimately produce zeros — alarming on either trains operators to
    ignore the alarm. (PR-227)"""
    return not dry_run and summary.get("scored", 0) >= SIGNAL_ALARM_MIN_SCORED


def _is_deterministically_dead(summary: dict, dry_run: bool) -> bool:
    """True when a real run scored a meaningful batch and the deterministic
    scorer qualified nobody — every pass came from the LLM gate. Only
    meaningful when the persona configs make a deterministic pass reachable
    at all (caller gates on _deterministic_pass_reachable). (PR-227)"""
    if not _run_alarm_eligible(summary, dry_run):
        return False
    return summary.get("deterministic_qualified", 0) == 0


def _dead_signals(summary: dict, dry_run: bool) -> list[str]:
    """Names of scoring signals missing/abstained on >90% of scored rows.
    Dry-run industry misses are expected (classification is skipped to avoid
    LLM spend), so only wet runs report. (PR-227)"""
    if not _run_alarm_eligible(summary, dry_run):
        return []
    scored = summary["scored"]
    dead = []
    if summary.get("industry_missing", 0) / scored > SIGNAL_DEAD_THRESHOLD:
        dead.append(f"industry (missing on {summary['industry_missing']}/{scored})")
    if summary.get("size_abstained", 0) / scored > SIGNAL_DEAD_THRESHOLD:
        dead.append(f"size (abstained on {summary['size_abstained']}/{scored})")
    return dead


def run_weekly_prospecting(
    crm: CRMProvider,
    pb: PhantomBusterClient,
    search_export_id: str,
    batch_size: int = 100,
    dry_run: bool = False,
    code_provenance: dict | None = None,
) -> dict:
    """Execute the weekly prospecting workflow across all saved searches.

    Iterates all persona × geo combinations, launching a PB Search Export
    for each, then scoring, deduplicating, and loading qualified prospects
    into Attio.

    Args:
        crm: Vendor-neutral CRM provider.
        pb: PhantomBuster API client.
        search_export_id: PhantomBuster agent ID for LinkedIn Search Export.
        batch_size: Max prospects to export per search.
        dry_run: If True, score but don't write to Attio or launch PhantomBuster.
        code_provenance: Optional {sha, branch, dirty, behind_origin_main}
            from workflows.run_provenance — stamped into every staged
            borderline entry and the summary so a stale-code run is
            detectable post-hoc (PR-228).

    Returns:
        Summary dict with counts.
    """
    anthropic_client = build_anthropic_client()

    personas_data = load_personas()

    # PR-29 step 0: target-list freshness gate. Iterate every
    # persona's configured target-list key and check the underlying
    # JSON file's mtime against the WARN/STALE bands. A STALE file
    # (>60d) raises `WeeklyTargetListStaleError` BEFORE any prospect
    # harvest — the queue row is written first so the operator's
    # visibility into the stale state is durable even if the
    # exception terminates this process.
    #
    # PR-44 cross-track stub: `/audit-reminder-sweep` will invoke
    # `check_target_list_freshness` directly (without entering
    # `run_weekly_prospecting`) so an operator can audit list
    # freshness ad-hoc. The skill points at
    # `models.freshness.check_target_list_freshness` — keep that
    # path stable.
    _check_all_persona_target_lists_fresh(personas_data, crm)

    searches = _get_all_searches(personas_data)

    summary: dict[str, Any] = {
        "exported": 0,
        "scored": 0,
        "qualified": 0,
        "duplicates": 0,
        "rejected": 0,
        "added": 0,
        # Supply accounting: of the commits counted in `added`, how many were
        # genuine net-new pipeline entries vs re-stamps of records that were
        # already in the list at run start. `added` conflates the two.
        "net_new_created": 0,
        "restamped_existing": 0,
        "borderline_staged": 0,
        # PR-222 Rec D: of the borderlines staged (fail-open), how many landed
        # there because an INFRA signal — not a quality signal — blocked LLM
        # vetting. Distinct buckets so the operator can tell a cost-ceiling
        # breach from a transient Haiku error from a budget-ledger outage,
        # instead of all three vanishing into borderline_staged. A borderline
        # staged for lack of a client (the normal case) increments none of them.
        "cost_exhausted_staged": 0,
        "llm_error_staged": 0,
        "ledger_unavailable_staged": 0,
        "reprospect_review": 0,
        # Signal health (PR-227, 2026-07-06 RCA): deterministic passes counted
        # apart from LLM borderline passes, plus per-signal abstain/miss rates
        # the end-of-run alarms read.
        "deterministic_qualified": 0,
        "size_abstained": 0,
        "industry_missing": 0,
        # Per-verdict-path rejection counts, surfaced in the run summary so
        # sales-weekly / sales-learn can see which filter caught how many.
        # E.g. `deterministic_reject_sales_role` flags wrong-role leakage trends.
        "rejected_by_path": {},
    }
    borderline_stage: list[dict] = []
    reprospect_review: list[dict] = []

    if not searches:
        click.echo("No valid Sales Navigator search URLs configured.")
        return summary

    click.echo(f"=== Running {len(searches)} searches ===\n")

    # Phase 0: Backfill companies missing industry_vertical so new DMs
    # substitute specific industry labels instead of the generic fallback.
    #
    # §7 boundary: `backfill_missing_industries` is an UNMIGRATED helper
    # (industry_classifier.py) shared with the daily run + scripts; it reads
    # raw Attio object-record shapes (`record["values"]`) and writes via
    # `update_company`, so it still requires the concrete `AttioClient`.
    # We hand it the provider's raw inner client (the documented Attio escape
    # hatch) rather than ripple its signature into the unmigrated callers.
    if anthropic_client is not None and not dry_run:
        from workflows.industry_classifier import backfill_missing_industries
        click.echo("--- Phase 0: Backfill missing industry_vertical ---")
        backfill_summary = backfill_missing_industries(
            _attio_inner_client(crm), anthropic_client=anthropic_client
        )
        click.echo(
            f"  Scanned {backfill_summary['total_scanned']}, "
            f"classified {backfill_summary['classified']}, "
            f"wrote {backfill_summary['written']}, "
            f"errors {backfill_summary['api_errors']}.\n"
        )

    list_id = os.environ.get("ATTIO_LIST_ID", "")
    today = date.today().isoformat()
    seen_urls: set[str] = set()
    # Tracks URLs already processed by a midmarket (target_company_mode)
    # search. Used by `_process_prospects` to skip the persona-upgrade pass
    # once a prospect has the midmarket tag — see that function for the
    # full upgrade logic.
    seen_urls_midmarket: set[str] = set()

    # Pre-load all record IDs already in the pipeline list so we can check
    # membership in O(1) without a per-prospect API call.
    click.echo("Loading existing pipeline list...")
    existing_entries = crm.query_list_entries(list_id=list_id)
    in_list_record_ids: set[str] = {e.record_id for e in existing_entries}
    click.echo(f"  {len(in_list_record_ids)} records already in pipeline.\n")

    # Authoritative canonical-URL set for the recycle fix: the live
    # search_person_by_linkedin dedup is eventual-consistent and misses
    # already-listed records, which then get recycled/re-stamped. This set
    # (built once from the list itself, resolving URLs the entry doesn't carry
    # via one bulk read) lets _process_prospects skip them deterministically.
    in_list_canonical_urls = _load_in_list_canonical_urls(crm, existing_entries)
    click.echo(f"  {len(in_list_canonical_urls)} canonical URLs resolved for dedup.\n")

    # Run-start name index for the name+company duplicate gate (PR-241 René
    # RCA). Built ONCE from the pipeline's person records so the per-candidate
    # check is an O(1) local lookup — a live Attio name search is accent-
    # SENSITIVE and misses the suffix/accent variants this gate exists to catch.
    # See _build_name_index for the full rationale.
    name_index = _build_name_index(crm, existing_entries)
    click.echo(f"  {len(name_index)} distinct names indexed for name+company dedup.\n")

    # Industry resolution cache shared across ALL searches in this run — the
    # same company routinely surfaces in multiple SN searches (enterprise +
    # midmarket personas over one geo); sharing prevents duplicate CRM lookups
    # and duplicate LLM classifications of the same company. (PR-225)
    industry_cache: dict[str, tuple[str | None, str | None]] = {}

    for i, (persona_key, geo_key, sn_url) in enumerate(searches, 1):
        click.echo(f"[{i}/{len(searches)}] {persona_key} / {geo_key}")
        click.echo(f"  URL: {sn_url[:80]}...")

        # Build per-persona config (passed through to score_prospect for target_company_mode)
        persona_config = dict(personas_data.get(persona_key, {}))
        persona_config["key"] = persona_key

        # Launch and download CSV. The whole per-search body is guarded
        # (PR-227, SRE lens): an unguarded raise here used to skip every
        # end-of-run alarm block — the exact safety net the 2026-07-06 RCA
        # added. A failed search now logs, counts, and continues so the
        # signal-health verdict always runs on whatever WAS scored. Auth
        # failures (401/403) still abort loud — every later call fails too.
        try:
            if not dry_run:
                csv_text = _launch_and_download(pb, search_export_id, sn_url, batch_size)
                if not csv_text:
                    click.echo("  No results. Skipping.\n")
                    continue
            else:
                click.echo("  [DRY RUN] Skipping PhantomBuster launch.\n")
                continue

            # Parse CSV
            prospects_raw = list(csv.DictReader(io.StringIO(csv_text)))
            summary["exported"] += len(prospects_raw)
            click.echo(f"  Exported {len(prospects_raw)} prospects.")

            # Process
            _process_prospects(
                prospects_raw, crm, list_id, today, dry_run, summary, seen_urls,
                in_list_record_ids=in_list_record_ids,
                persona_config=persona_config, borderline_stage=borderline_stage,
                reprospect_review=reprospect_review,
                anthropic_client=anthropic_client,
                existing_entries=existing_entries,
                seen_urls_midmarket=seen_urls_midmarket,
                in_list_canonical_urls=in_list_canonical_urls,
                name_index=name_index,
                industry_cache=industry_cache,
            )
        except httpx.HTTPStatusError as err:
            if err.response.status_code in (401, 403):
                raise  # auth failure cascades — abort the whole run loud
            summary.setdefault("searches_aborted", 0)
            summary["searches_aborted"] += 1
            logger.exception(
                "search %s/%s aborted (HTTP %s) — continuing with remaining searches",
                persona_key, geo_key, err.response.status_code,
            )
            click.echo(
                f"  ⚠ search aborted (HTTP {err.response.status_code}) — "
                "continuing; see summary.",
                err=True,
            )
        except Exception:  # noqa: BLE001 — alarms must still run at end of run
            summary.setdefault("searches_aborted", 0)
            summary["searches_aborted"] += 1
            logger.exception(
                "search %s/%s aborted — continuing with remaining searches",
                persona_key, geo_key,
            )
            click.echo(
                "  ⚠ search aborted (unexpected error) — continuing; see summary.",
                err=True,
            )
        click.echo()

    # Write borderline JSONL (even for dry-run — the agent wants to see them)
    if borderline_stage:
        borderline_path = EXPORTS_DIR / f"weekly_borderline_{today}.jsonl"
        borderline_path.parent.mkdir(parents=True, exist_ok=True)
        with borderline_path.open("w") as f:
            for entry in borderline_stage:
                if code_provenance:
                    # PR-228: each staged row records the code that scored it —
                    # a stale-checkout run is otherwise only diagnosable by
                    # forensic fingerprinting of these files.
                    entry["code_version"] = code_provenance
                f.write(json.dumps(entry) + "\n")
        click.echo(f"  Staged {len(borderline_stage)} borderlines → {borderline_path}")

    if reprospect_review:
        review_path = EXPORTS_DIR / f"weekly_reprospect_review_{today}.csv"
        review_path.parent.mkdir(parents=True, exist_ok=True)
        _write_reprospect_review_csv(review_path, reprospect_review)
        click.echo(f"  Staged {len(reprospect_review)} re-prospect candidates for review → {review_path}")

    # Summary
    click.echo("--- Weekly Prospecting Summary ---")
    if code_provenance:
        from workflows.run_provenance import format_provenance
        click.echo(f"Code:       {format_provenance(code_provenance)}")
    click.echo(f"Searches:   {len(searches)}")
    click.echo(f"Exported:   {summary['exported']}")
    click.echo(f"Scored:     {summary['scored']}")
    click.echo(f"Qualified:  {summary['qualified']}")
    click.echo(
        f"   ├ deterministic (scorer alone): {summary.get('deterministic_qualified', 0)}"
    )
    click.echo(
        f"   └ via LLM borderline gate: "
        f"{summary['qualified'] - summary.get('deterministic_qualified', 0)}"
    )
    # Signal health (PR-227, 2026-07-06 RCA): industry resolution + size
    # scoping. Framed as coverage, not deficit — "missing" read as a defect on
    # healthy runs (unclassifiable companies are neutral-scored by design, not
    # lost).
    _ind_missing = summary.get("industry_missing", 0)
    _size_abst = summary.get("size_abstained", 0)
    click.echo(
        f"Signals:    industry resolved on {summary['scored'] - _ind_missing}"
        f"/{summary['scored']} scored ({_ind_missing} unclassifiable — neutral)"
        f" · size abstained on {_size_abst}/{summary['scored']}"
        + (" (dry-run: industry classification skipped — 0 resolved is expected)"
           if dry_run else "")
    )
    if summary.get("searches_aborted"):
        click.echo(
            f"⚠ Aborted:   {summary['searches_aborted']} search(es) failed mid-run "
            "and were skipped — counts above cover only completed searches.",
            err=True,
        )
    if summary.get("industry_classified_at_ingest") or summary.get("industry_resolve_errors"):
        click.echo(
            f"            {summary.get('industry_classified_at_ingest', 0)} industries "
            f"classified at ingest · {summary.get('industry_resolve_errors', 0)} resolve errors"
        )
    click.echo(f"Duplicates: {summary['duplicates']}")
    click.echo(f"Rejected:   {summary['rejected']}")
    if summary["rejected_by_path"]:
        for path, count in sorted(
            summary["rejected_by_path"].items(), key=lambda kv: -kv[1]
        ):
            click.echo(f"            {count} via {path}")
    if summary.get("filtered_out"):
        click.echo(f"Filtered:   {summary['filtered_out']} (not on target company list)")
    click.echo(f"Staged:     {summary['borderline_staged']} (borderline — pending agent qualification)")
    if summary.get("persona_upgraded_to_midmarket"):
        click.echo(
            f"Upgraded:   {summary['persona_upgraded_to_midmarket']} "
            f"(borderline persona promoted from enterprise → midmarket on cross-search match)"
        )
    if summary.get("reprospect_review"):
        click.echo(f"Re-review:  {summary['reprospect_review']} (existing person, no list entry — needs manual triage)")
    net_new = summary.get("net_new_created", 0)
    restamped = summary.get("restamped_existing", 0)
    click.echo(f"Added:      {summary['added']}")
    click.echo(f"   ├ net-new pipeline entries: {net_new}")
    click.echo(f"   └ already-listed records re-stamped: {restamped}")

    # Loud supply alarm: a run that qualifies candidates but sources zero
    # net-new prospects means the saved searches are exhausted (re-scoring the
    # existing pool). Without this the run reads as success — "773 scored / 417
    # passing" — while adding nothing (2026-06-16 silent starvation).
    if _is_supply_starved(summary, dry_run):
        click.echo("", err=True)
        click.echo(
            "⚠️  SUPPLY ALARM: 0 net-new prospects entered the pipeline this run.",
            err=True,
        )
        click.echo(
            f"   {summary['qualified']} qualified, but none were net-new — all "
            f"were already in the pipeline: {restamped} re-stamped, "
            f"{summary.get('duplicates', 0)} duplicates, "
            f"{summary.get('reprospect_review', 0)} staged for re-prospect review.",
            err=True,
        )
        click.echo(
            "   The Sales Nav saved searches are likely exhausted — refresh the "
            "configured search inputs (the persona search URLs).",
            err=True,
        )

    # Deterministic-qualifier alarm (PR-227, 2026-07-06 RCA): the scorer ran 0
    # deterministic qualifications on every weekly for 3 months and nothing
    # noticed — a hard zero from a component that is supposed to classify is a
    # bug signal, not a statistic.
    searched_persona_keys = {persona_key for persona_key, _geo, _url in searches}
    autopass_reachable = _deterministic_pass_reachable(
        personas_data, searched_persona_keys
    )
    if autopass_reachable and _is_deterministically_dead(summary, dry_run):
        click.echo("", err=True)
        click.echo(
            "⚠️  DETERMINISTIC QUALIFIER ALARM: 0 deterministic qualifications "
            f"across {summary['scored']} scored prospects.",
            err=True,
        )
        click.echo(
            "   Every pass this run came from the LLM borderline gate. A scoring "
            "signal has likely died (missing industry labels, missing "
            "search_size_credit in personas.json, or a threshold drift).",
            err=True,
        )
    elif not autopass_reachable and summary.get("deterministic_qualified", 0) == 0:
        # The suppressed state must itself be visible: "configured off" and
        # "died again" look identical (0) without this line — the exact
        # observability gap that hid the original 3-month failure.
        click.echo(
            f"ℹ  Deterministic auto-pass is configured OFF for this run's "
            f"personas (every search_size_credit < "
            f"{DETERMINISTIC_REACHABLE_MIN_CREDIT}) — 0 deterministic "
            "qualifications is the expected outcome, not a dead signal.",
            err=True,  # same stream as the ⚠ alarm this line stands in for
        )

    # Dead-signal alarm (same RCA): a component missing on >90% of a run's
    # rows is structurally dead, whatever each individual row's score says.
    dead_signals = _dead_signals(summary, dry_run)
    if dead_signals:
        click.echo("", err=True)
        click.echo(
            f"⚠️  SIGNAL ALARM: {len(dead_signals)} scoring signal(s) dead this run: "
            + "; ".join(dead_signals),
            err=True,
        )
        click.echo(
            "   A signal that defaults/misses on nearly every row contributes "
            "nothing and silently caps scores. Check industry ingest wiring "
            "(PR-225) and search_size_credit persona config.",
            err=True,
        )

    if summary["borderline_staged"] > 0:
        n = summary["borderline_staged"]
        click.echo(f"\n=== AGENT HANDOFF — {n} borderlines staged ===")
        click.echo(f"File: exports/weekly_borderline_{today}.jsonl")
        click.echo("\nNext steps:")
        click.echo("1. For each JSONL entry, call Haiku 4.5 with system+user payload.")
        click.echo("   Model: claude-haiku-4-5-20251001, max_tokens=300.")
        click.echo("   Parse JSON response into {pass, icp_lane, rationale}.")
        click.echo(f"2. Write verdicts to exports/weekly_verdicts_{today}.jsonl")
        click.echo('   (one JSON per line: {"linkedin_url": "...", "pass": bool, "icp_lane": 1|2, "rationale": "..."})')
        click.echo(f"3. Run: python3 cli.py weekly-finalize --batch {today}")

    return summary
