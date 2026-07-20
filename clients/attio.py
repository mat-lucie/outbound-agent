"""Attio CRM API v2 client."""

import logging
import os
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import unquote

import httpx

from models.pipeline import STAGE_RANK, PipelineStage

logger = logging.getLogger(__name__)


class AttioResultTruncated(RuntimeError):
    """A paginated query hit its ``limit`` with records (possibly) remaining.

    Raised only when the caller passed ``fail_if_truncated=True`` — full-sweep
    callers prefer a loud failure over a silently incomplete result set
    (PR-234: suppression sweeps, per-stage campaign queries, and list-scan
    exports must not drop the tail past their fetch ceiling).
    """


def _is_stage_regression(prior_stage: str, new_stage: str) -> bool:
    """True iff ``new_stage`` ranks strictly below ``prior_stage``.

    Defense-in-depth backstop for the weekly re-stamp cadence-desync class: a
    caller PATCHing an existing list entry to a LOWER stage (e.g. Accepted over
    DM3 Sent) would wipe cadence depth. Unknown/garbage stage strings fall back
    to rank 0 (mirrors the ``_filter_and_rank_entries_for_record`` defensive
    fallback), so a malformed prior never reads as "advanced" and a malformed
    new stage is treated as the lowest rank.

    NOTE on the CRM-provider boundary: this client is the vendor layer, and it
    is mapping-unaware by design — the ``AttioProvider`` translates canonical
    ``PipelineStage`` values to vendor option labels BEFORE handing them here.
    Under the shipped IDENTITY ``stage_mapping`` (vendor label == canonical
    value) both the incoming ``new_stage`` and the stored ``prior_stage`` parse
    cleanly via ``PipelineStage(...)`` and the rank comparison is exact. Under a
    NON-identity mapping the stored/incoming strings are vendor labels that
    ``PipelineStage(...)`` cannot parse, so both fall to rank 0 and this backstop
    safely NO-OPs (it never strips a legitimate advance, only fails to catch a
    regression). The comparison is internally consistent either way — both sides
    come from the same label space — so the guard can never misfire and corrupt
    a forward write; the monotonicity gate at the writer boundary remains the
    primary defense for non-identity deployments.
    """
    def _rank(stage_str: str) -> int:
        try:
            return STAGE_RANK[PipelineStage(stage_str)]
        except (KeyError, ValueError):
            return 0

    return _rank(new_stage) < _rank(prior_stage)


# Substrings that mark a company record as carrying LinkedIn's own Clearbit
# enrichment instead of the prospect's real employer. Matches subdomains
# (br.linkedin.com, etc.) without admitting "linkedin.com.example.co".
_LINKEDIN_DOMAIN_TOKENS: tuple[str, ...] = (
    "linkedin.com",
)
# A company record whose `linkedin` field points at LinkedIn's own company
# page is a tell that Clearbit resolved `linkedin.com` itself.
_LINKEDIN_SELF_COMPANY_URL = "linkedin.com/company/linkedin"


def is_linkedin_clearbit_corrupted(
    company_record: dict,
    field_slug: Callable[[str], str] | None = None,
) -> bool:
    """Detect the LinkedIn-Clearbit corruption fingerprint on a company record.

    Pre-2026-04-11 ingestion treated PhantomBuster's
    `companyUrl=https://linkedin.com/company/<slug>` as a literal company
    domain (`linkedin.com`). Clearbit then enriched that domain with
    LinkedIn's own profile data, while the `name` field was overwritten
    from PB's `companyName`. The resulting record looks like a real
    employer by `name` but carries LinkedIn's clearbit payload everywhere
    else — and personalising a DM with `[Company]` ships the real-looking
    fake name.

    Two signals, ORed:
        - `domains` contains a value matching `linkedin.com` (or a subdomain)
        - `linkedin` field points at `linkedin.com/company/linkedin`

    The real LinkedIn record (name = "LinkedIn") is excluded so this guard
    never false-positives on it. We don't ship DMs to LinkedIn itself.

    ``field_slug`` resolves the two CONFIGURABLE engine fields this guard reads
    to the workspace's vendor slugs: the company web-domain (``company_domain``)
    and the LinkedIn-profile field (``linkedin_url``). It defaults to the
    identity resolver, so a direct call (the unit tests) reads the canonical
    Attio slugs ``domains`` / ``linkedin`` byte-identically to pre-seam.
    ``extract_record_info`` passes the client's ``_field_slug`` so an operator's
    renamed slugs are honored. The company-``name`` read stays the LITERAL
    ``"name"`` — there is no documented field_mapping key for company name
    (``full_name`` is the PERSON name), so routing it would invent a mapping.
    """
    if field_slug is None:
        # Default resolver = the canonical Attio slugs, so a direct call reads
        # "domains" / "linkedin" exactly as the pre-seam code did (byte-identical).
        # Use the SAME forgiving .get(field, field) idiom as AttioClient._field_slug
        # (not a strict __getitem__): an unmapped engine field passes through
        # unchanged, so a future read added here can't KeyError on this default
        # path while the injected (production) path stays green and masks it.
        _canonical = {"company_domain": "domains", "linkedin_url": "linkedin"}
        resolve = lambda f: _canonical.get(f, f)  # noqa: E731
    else:
        resolve = field_slug
    domains_slug = resolve("company_domain")
    linkedin_slug = resolve("linkedin_url")
    values = company_record.get("values", {})

    name_data = values.get("name") or []
    name_value = ""
    if name_data:
        first = name_data[0]
        name_value = (first.get("value", "") or "") if isinstance(first, dict) else str(first)
    if name_value.strip().lower() == "linkedin":
        return False

    for entry in values.get(domains_slug) or []:
        domain = entry.get("domain", "") if isinstance(entry, dict) else str(entry)
        domain_l = domain.lower()
        for token in _LINKEDIN_DOMAIN_TOKENS:
            if domain_l == token or domain_l.endswith("." + token):
                return True

    for entry in values.get(linkedin_slug) or []:
        url = entry.get("value", "") if isinstance(entry, dict) else str(entry)
        if _LINKEDIN_SELF_COMPANY_URL in url.lower():
            return True

    return False


def _canonical_linkedin_url(url: str) -> str:
    """Return the canonical form of a LinkedIn profile URL.

    Attio's text-field filter is exact-string match: `iñigo-marchal` and
    `i%C3%B1igo-marchal` are *not* the same, nor are `www.linkedin.com` and
    `linkedin.com`, nor are trailing-slash variants. Without normalization,
    every time PB emits a subtly different URL form for the same profile we
    fail the upsert's duplicate check and create a new record.

    Canonical form: URL-decoded, no `www.` prefix, no trailing slash,
    lowercase scheme. LinkedIn slugs themselves are case-insensitive, so we
    also lowercase the path.
    """
    if not url:
        return ""
    decoded = unquote(url).strip()
    # Lowercase scheme but preserve the rest until we reach the path
    if "://" in decoded:
        scheme, rest = decoded.split("://", 1)
        decoded = f"{scheme.lower()}://{rest}"
    decoded = decoded.replace("://www.", "://")
    decoded = decoded.rstrip("/")
    return decoded.lower()


def _vanity_url_slug(url: str) -> str:
    """Extract the vanity slug from a LinkedIn profile URL.

    Given 'https://linkedin.com/in/mateo-lt-12345' returns 'mateo-lt-12345'.
    Given 'https://linkedin.com/in/mateo-lt-12345/' returns 'mateo-lt-12345'.
    Non-/in/ URLs (company pages, etc.) return ''.

    Uses the canonical form so the slug is always lowercase and URL-decoded.
    Returns '' when the URL is empty, malformed, or not a profile URL.
    """
    if not url:
        return ""
    canonical = _canonical_linkedin_url(url)
    # Expect: scheme://linkedin.com/in/<slug>
    # or: linkedin.com/in/<slug> (no scheme)
    for prefix in (
        "https://linkedin.com/in/",
        "http://linkedin.com/in/",
        "linkedin.com/in/",
    ):
        if canonical.startswith(prefix):
            slug = canonical[len(prefix):].strip("/")
            slug = slug.split("?", 1)[0]
            return slug
    return ""


def _linkedin_url_variants(url: str) -> list[str]:
    """Return every variant of a LinkedIn URL that Attio may have stored.

    Attio's exact-string filter plus historical PB inconsistency means the
    same profile can live under several forms. Try the canonical form first,
    then fall back to with-www and with-trailing-slash variants. Also try
    the raw input in case it was stored URL-encoded.
    """
    if not url:
        return []
    canonical = _canonical_linkedin_url(url)
    variants: list[str] = [canonical]
    if "://" in canonical:
        scheme, rest = canonical.split("://", 1)
        with_www = f"{scheme}://www.{rest}"
        variants.append(with_www)
        variants.append(with_www + "/")
    variants.append(canonical + "/")
    raw = url.strip()
    if raw and raw not in variants:
        variants.append(raw)
    # De-duplicate, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for v in variants:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


class AttioClient:
    """Client for the Attio REST API v2."""

    BASE_URL = "https://api.attio.com/v2"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        field_mapping: dict[str, str] | None = None,
    ):
        self.api_key = api_key or os.environ["ATTIO_API_KEY"]
        # engine-field-name -> vendor attribute slug, for the three documented
        # configurable fields. Defaults to the canonical Attio slugs, which makes
        # every existing `AttioClient(...)` construction byte-identical: each
        # `self._field_slug(...)` call below resolves to the same literal slug the
        # code hardcoded before this seam. An operator whose workspace renames an
        # attribute (e.g. linkedin -> linkedin_profile) overrides it via
        # config/crm.yaml's `field_mapping`, threaded in by the CRM factory.
        self._field_mapping: dict[str, str] = (
            field_mapping
            if field_mapping is not None
            else {
                "linkedin_url": "linkedin",
                "full_name": "name",
                "company_domain": "domains",
            }
        )
        self._client = httpx.Client(
            base_url=self.BASE_URL,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=60.0,
        )
        self._company_cache: dict[str, str] = {}  # company_record_id → name
        self._industry_cache: dict[str, str] = {}  # company_record_id → industry_vertical
        # company_record_id → True iff this company record carries the
        # LinkedIn-Clearbit corruption fingerprint. Populated lazily as
        # extract_record_info fetches each company; consult via
        # is_person_company_corrupted.
        self._company_corruption_cache: dict[str, bool] = {}
        # person_record_id → company_record_id, kept in lockstep with
        # _company_corruption_cache so the corruption lookup can hop from
        # a person id to the right cached flag.
        self._person_to_company: dict[str, str] = {}
        # Count of stage-regressing add_list_entry PATCHes the defense-in-depth
        # backstop neutralized over this client's lifetime. A batch orchestrator
        # can read this after a run and escalate if it fired — escalate() can't
        # be called from here (circular import with workflows.escalation, which
        # imports AttioClient).
        self.stage_regressions_blocked: int = 0

    def _field_slug(self, engine_field: str) -> str:
        """Resolve an engine field name to this workspace's vendor attribute slug.

        Unmapped fields pass through unchanged (an engine field with no explicit
        mapping shares its name with the vendor slug). Under the default mapping
        the three documented fields resolve to their canonical Attio slugs, so
        every call site below is byte-identical to the pre-seam literal.
        """
        return self._field_mapping.get(engine_field, engine_field)

    def _request(
        self,
        method: str,
        path: str,
        retries: int = 3,
        *,
        retry_500: bool = False,
        **kwargs,
    ) -> dict:
        # retry_500 is OPT-IN and only for idempotent call sites (reads,
        # linkedin-keyed assert upserts): Attio 500s are transient in practice
        # (PR-256 weekly-finalize crash loop), but a 500 on a non-idempotent
        # POST (note/record create) may have committed server-side, so blanket
        # retry would risk double-writes.
        retryable = (429, 500, 502, 503) if retry_500 else (429, 502, 503)
        for attempt in range(retries):
            try:
                resp = self._client.request(method, path, **kwargs)
                if resp.status_code in retryable:
                    if attempt < retries - 1:
                        time.sleep(2 ** attempt * 5)
                    continue
                resp.raise_for_status()
                return resp.json() if resp.content else {}
            except (httpx.ReadTimeout, httpx.ReadError, httpx.ConnectError):
                if attempt < retries - 1:
                    time.sleep(2 ** attempt * 5)
                    continue
                raise
        resp.raise_for_status()
        return {}

    # ── People records ────────────────────────────────────────

    def _query_paginated(
        self,
        path: str,
        filter_: dict | None,
        limit: int,
        *,
        fail_if_truncated: bool = False,
    ) -> list[dict]:
        """Shared pagination loop for the record/entry query endpoints.

        Pages through ``path`` until ``limit`` records are collected or the API
        returns a short page (exhausted). With ``fail_if_truncated=True``,
        raises :class:`AttioResultTruncated` when records remain past ``limit``
        (probing one extra page when the count lands exactly on the limit, so a
        complete result never raises). Full-sweep callers (suppression sweep,
        per-stage campaign queries, list-scan exports) use this so a silently
        truncated sweep becomes a loud operator-visible failure (PR-234).
        """
        all_records: list[dict] = []
        page_size = min(limit, 100)
        offset: int = 0
        exhausted = False

        while len(all_records) < limit:
            body: dict = {"limit": page_size, "offset": offset}
            if filter_:
                body["filter"] = filter_
            data = self._request("POST", path, json=body, retry_500=True)
            records = data.get("data", [])
            all_records.extend(records)
            if len(records) < page_size:
                exhausted = True
                break
            offset += len(records)

        if fail_if_truncated and len(all_records) >= limit:
            truncated = len(all_records) > limit
            if not truncated and not exhausted:
                # Exactly ``limit`` records with a full final page — a complete
                # result is indistinguishable from a truncated one without
                # probing the next page. One extra request here (boundary case
                # only) avoids a false-positive crash when the true count lands
                # exactly on the limit.
                body = {"limit": page_size, "offset": offset}
                if filter_:
                    body["filter"] = filter_
                probe = self._request("POST", path, json=body, retry_500=True)
                truncated = bool(probe.get("data", []))
            if truncated:
                raise AttioResultTruncated(
                    f"query {path} hit its scan limit ({limit}) with more "
                    f"records remaining — raise the limit so the sweep sees "
                    f"every record"
                )
        return all_records[:limit]

    def search_people(
        self,
        filter_: dict | None = None,
        limit: int = 50,
        *,
        fail_if_truncated: bool = False,
    ) -> list[dict]:
        """Search person records with optional filter. Auto-paginates."""
        return self._query_paginated(
            "/objects/people/records/query", filter_, limit,
            fail_if_truncated=fail_if_truncated,
        )

    def get_person(self, record_id: str, *, retry_500: bool = True) -> dict | None:
        """Get a person record by its record ID. Returns None if not found.

        ``retry_500=False`` opts back out of transient-500 retry — the
        fail-open bulk fetch uses it so a systemic Attio outage fails fast per
        record instead of blocking hours in backoff sleeps.
        """
        try:
            data = self._request("GET", f"/objects/people/records/{record_id}", retry_500=retry_500)
            return data.get("data", data)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    def bulk_fetch_persons_by_record_ids(
        self, record_ids: set[str], max_workers: int = 8,
        *, metrics: Any = None,
    ) -> dict[str, dict]:
        """Per-record isolation: a single failure is logged + counted
        but never propagated out of the batch. Returns the subset of
        `record_ids` that resolved to a non-None record.

        `metrics` (optional): when supplied, bumps
        `bulk_fetch_records_requested/returned/failed` so the
        end-of-run summary surfaces partial-outage volume rather than
        hiding it in stderr noise.

        Uses a bounded ThreadPoolExecutor scoped to the pipeline; calls
        flow through `_request` which handles 429 backoff.
        """
        if not record_ids:
            return {}
        import sys
        from concurrent.futures import ThreadPoolExecutor, as_completed

        ids = list(record_ids)
        if metrics is not None:
            metrics.bulk_fetch_records_requested += len(ids)
        result: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            # retry_500=False: this path is fail-open per record, so under a
            # systemic 500 outage retrying would burn the full backoff budget
            # per record (hours across a weekly sweep) and still end in the
            # same degraded result — fail fast and let the metrics surface it.
            future_to_id = {
                pool.submit(self.get_person, rid, retry_500=False): rid for rid in ids
            }
            for future in as_completed(future_to_id):
                rid = future_to_id[future]
                try:
                    record = future.result()
                except (httpx.HTTPError, KeyError, TypeError) as exc:
                    # Narrowed catch: transport, missing-key, malformed
                    # response shape. Bugs outside this set (AssertionError,
                    # ValueError from a refactor) still propagate so they
                    # surface immediately instead of silently dropping
                    # every record.
                    if metrics is not None:
                        metrics.bulk_fetch_records_failed += 1
                        metrics.warn(
                            f"bulk_fetch get_person({rid}) failed: "
                            f"{type(exc).__name__}"
                        )
                    print(
                        f"WARNING: bulk_fetch_persons_by_record_ids: "
                        f"get_person({rid}) failed: "
                        f"{type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )
                    continue
                if record is not None:
                    result[rid] = record
                    if metrics is not None:
                        metrics.bulk_fetch_records_returned += 1
        return result

    def search_person_by_linkedin(self, linkedin_url: str) -> dict | None:
        """Find a person record by LinkedIn URL. Returns None if not found.

        Tries every URL variant Attio may have stored: canonical (URL-decoded,
        no www, no trailing slash), with-www, with-trailing-slash, and the
        raw input. Attio's exact-string filter means otherwise-equivalent
        URLs miss their duplicates and the caller creates yet another record.
        """
        if not linkedin_url:
            return None
        seen: set[str] = set()
        for variant in _linkedin_url_variants(linkedin_url):
            if variant in seen:
                continue
            seen.add(variant)
            results = self.search_people(
                filter_={self._field_slug("linkedin_url"): variant}, limit=1
            )
            if results:
                return results[0]
        return None

    def create_person(self, attributes: dict) -> dict:
        """Create a new person record."""
        data = self._request("POST", "/objects/people/records", json={"data": {"values": attributes}})
        return data.get("data", {})

    def update_person(self, record_id: str, attributes: dict) -> dict:
        """Update an existing person record."""
        # Same-values PATCH is idempotent — safe to retry a transient 500.
        data = self._request("PATCH", f"/objects/people/records/{record_id}", json={"data": {"values": attributes}}, retry_500=True)
        return data.get("data", {})

    def upsert_person(self, matching_attribute: str, attributes: dict) -> dict:
        """Upsert a person record: search by matching_attribute value, then create or update.

        Attio's PUT upsert requires the field to have a unique constraint; since 'linkedin'
        is not unique we implement it manually: search first, PATCH if found, POST otherwise.

        NOTE — field_mapping residual: the `linkedin_url` field_mapping covers the
        READ/search side (search_person_by_linkedin honors a renamed slug), but this
        WRITE/dedup path deliberately stays on the literal `linkedin` slug
        (matching_attribute + the canonicalized attrs below). A workspace that has
        BOTH a renamed slug and the canonical `linkedin` slug could split dedup
        silently. Routing this write path is a deferred follow-up — see
        docs/LIMITATIONS.md §1 (residual 2).
        """
        if matching_attribute == "linkedin":
            linkedin_url = attributes.get("linkedin", "")
            # Canonicalize at write time so duplicates on subsequent runs hit
            # the existing record regardless of encoding or trailing-slash
            # variance in PB's output.
            canonical = _canonical_linkedin_url(linkedin_url)
            if canonical:
                attributes = {**attributes, "linkedin": canonical}
            existing = self.search_person_by_linkedin(linkedin_url)
            if existing:
                record_id = existing.get("id", {}).get("record_id", "")
                if record_id:
                    return self.update_person(record_id, attributes)
            return self.create_person(attributes)
        # Fallback: use the native PUT upsert for truly unique attributes (email, record_id)
        # The native upsert is assert-by-key (idempotent) — safe to retry a 500.
        data = self._request(
            "PUT",
            "/objects/people/records",
            params={"matching_attribute": matching_attribute},
            json={"data": {"values": attributes}},
            retry_500=True,
        )
        return data.get("data", {})

    def delete_person(self, record_id: str) -> bool:
        """Delete a person record. Returns True if deleted, False if not found."""
        try:
            self._request("DELETE", f"/objects/people/records/{record_id}")
            return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return False
            raise

    # ── Company records ──────────────────────────────────────

    def search_companies(self, filter_: dict | None = None, limit: int = 50) -> list[dict]:
        """Search company records with optional filter. Auto-paginates."""
        all_records: list[dict] = []
        page_size = min(limit, 100)
        offset: int = 0

        while len(all_records) < limit:
            body: dict = {"limit": page_size, "offset": offset}
            if filter_:
                body["filter"] = filter_
            data = self._request("POST", "/objects/companies/records/query", json=body)
            records = data.get("data", [])
            all_records.extend(records)
            if len(records) < page_size:
                break
            offset += len(records)

        return all_records[:limit]

    def create_company(self, attributes: dict) -> dict:
        """Create a new company record."""
        data = self._request("POST", "/objects/companies/records", json={"data": {"values": attributes}})
        return data.get("data", {})

    def get_company(self, record_id: str) -> dict | None:
        """Get a company record by its record ID. Returns None if not found."""
        try:
            data = self._request("GET", f"/objects/companies/records/{record_id}", retry_500=True)
            return data.get("data", data)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    def get_list_attributes(self, list_id: str) -> list[dict]:
        """Live attribute definitions for a list
        (GET /lists/{list_id}/attributes). Used by the DM-writer
        schema preflight; auto-pagination unnecessary (attribute
        counts are far below the page limit)."""
        data = self._request("GET", f"/lists/{list_id}/attributes")
        return data.get("data", [])

    def get_object_attributes(self, object_slug: str) -> list[dict]:
        """Live attribute definitions for an object
        (GET /objects/{object_slug}/attributes)."""
        data = self._request("GET", f"/objects/{object_slug}/attributes")
        return data.get("data", [])

    def update_company(self, record_id: str, attributes: dict) -> dict:
        """Update an existing company record."""
        data = self._request("PATCH", f"/objects/companies/records/{record_id}", json={"data": {"values": attributes}})
        return data.get("data", {})

    def search_company_by_domain(self, domain: str) -> dict | None:
        """Find a company record by domain. Returns None if not found."""
        results = self.search_companies(
            filter_={self._field_slug("company_domain"): domain},
            limit=1,
        )
        return results[0] if results else None

    def delete_company(self, record_id: str) -> bool:
        """Delete a company record. Returns True if deleted, False if not found."""
        try:
            self._request("DELETE", f"/objects/companies/records/{record_id}")
            return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return False
            raise

    # ── Deal records ──────────────────────────────────────────

    def get_deal(self, record_id: str) -> dict | None:
        """Get a deal record by its record ID. Returns None if not found."""
        try:
            data = self._request("GET", f"/objects/deals/records/{record_id}")
            return data.get("data", data)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    def create_deal(self, attributes: dict) -> dict:
        """Create a new deal record. Mirrors ``create_company``.

        ``attributes`` is the flat dict of Attio attribute slugs → values
        (e.g. ``{"name": "...", "stage": "Lead", "creation_idempotency_key":
        "...", "associated_people": [{"target_object": "people",
        "target_record_id": "..."}]}``). The deal's record_id is in the
        returned ``id.record_id`` field.

        Callers handle idempotency via ``creation_idempotency_key`` —
        ``workflows.deal_creation.create_deal_from_response`` is the
        write-owner-registry-authorized writer for that slug per
        docs/attio_schema_deltas.yaml.
        """
        data = self._request(
            "POST",
            "/objects/deals/records",
            json={"data": {"values": attributes}},
        )
        return data.get("data", {})

    def update_deal(self, record_id: str, attributes: dict) -> dict:
        """Update an existing deal record."""
        data = self._request("PATCH", f"/objects/deals/records/{record_id}", json={"data": {"values": attributes}})
        return data.get("data", {})

    def delete_deal(self, record_id: str) -> bool:
        """Delete a deal record. Returns True if deleted, False if not found."""
        try:
            self._request("DELETE", f"/objects/deals/records/{record_id}")
            return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return False
            raise

    def search_deals(self, filter_: dict | None = None, limit: int = 500) -> list[dict]:
        """Search deal records with optional filter. Auto-paginates."""
        all_records: list[dict] = []
        page_size = min(limit, 100)
        offset: int = 0

        while len(all_records) < limit:
            body: dict = {"limit": page_size, "offset": offset}
            if filter_:
                body["filter"] = filter_
            data = self._request("POST", "/objects/deals/records/query", json=body)
            records = data.get("data", [])
            all_records.extend(records)
            if len(records) < page_size:
                break
            offset += len(records)

        return all_records[:limit]

    # ── List entries (pipeline) ───────────────────────────────

    def query_list_entries(
        self,
        list_id: str | None = None,
        filter_: dict | None = None,
        limit: int = 50000,
        *,
        fail_if_truncated: bool = False,
    ) -> list[dict]:
        """Query entries in a list (pipeline) with optional filters. Auto-paginates.

        With ``fail_if_truncated=True`` a full-list sweep that hits ``limit``
        with entries remaining raises :class:`AttioResultTruncated` instead of
        silently dropping the tail (PR-234).
        """
        lid = list_id or os.environ.get("ATTIO_LIST_ID", "")
        return self._query_paginated(
            f"/lists/{lid}/entries/query", filter_, limit,
            fail_if_truncated=fail_if_truncated,
        )

    def add_list_entry(
        self,
        record_id: str,
        stage_name: str,
        entry_attributes: dict | None = None,
        list_id: str | None = None,
        existing_entries: list[dict] | None = None,
    ) -> dict:
        """Add a person record to the pipeline list, or update the existing
        entry if one already exists for this (record_id, list) pair.

        Attio does not enforce uniqueness of (parent_record_id, list) — you
        can POST the same record repeatedly and get a fresh entry each time.
        The sales workflow calls this every time it (re-)processes a prospect,
        so without an upsert check, old prospects accumulate divergent list
        entries over time (Prospect + Connection Sent + DM1 Sent for the
        same person, stages going out of sync, DMs firing multiple times).

        Behavior:
          - If no entry exists for record_id in this list: POST a fresh entry.
          - If one entry exists: PATCH it with the new stage/attributes.
          - If multiple entries exist: PATCH the most-advanced one and leave
            the duplicates alone (callers can run the list-entry dedup script
            to collapse them).

        `existing_entries`: optional pre-fetched list of all entries in the
        target list. When supplied, the upsert filter runs client-side against
        this list instead of triggering a fresh `query_list_entries(limit=50000)`
        scan per call. Use this in batch flows that already hold the full list.
        """
        lid = list_id or os.environ.get("ATTIO_LIST_ID", "")
        attrs: dict = dict(entry_attributes or {})
        if stage_name:
            attrs["stage"] = stage_name

        if existing_entries is None:
            existing = self._find_list_entries_for_record(record_id, lid)
        else:
            existing = self._filter_and_rank_entries_for_record(existing_entries, record_id)
        if existing:
            target_entry_id = existing[0].get("id", {}).get("entry_id", "")
            if target_entry_id:
                # Defense-in-depth backstop for the weekly re-stamp cadence-desync
                # class: a PATCH that would move the existing entry to a LOWER
                # stage (e.g. Accepted/Prospect over DM3 Sent) wipes cadence
                # depth. When that's detected we strip the cadence-depth PAIR
                # — both `stage` AND `dm_step` — so the write cannot leave the
                # row in the self-contradictory state this guard exists to
                # prevent (stage=DM3 Sent + dm_step=0 is the exact "regressed"
                # fingerprint). Stripping `stage` alone would manufacture that
                # corruption. The other attributes still PATCH. Scope is
                # deliberately the universal cadence pair; the weekly finalize
                # path's own _commit_prospect already-listed skip is the primary
                # fix — this generic client chokepoint must not couple to any
                # caller's attr schema. Log loudly (this must NEVER be silent)
                # and bump a counter so a fired backstop is observable in
                # aggregate without grepping logs. We do NOT escalate() here:
                # workflows.escalation imports AttioClient, so calling it risks a
                # circular import; the batch orchestrator drains
                # `stage_regressions_blocked` and escalates. The fresh-POST branch
                # below needs no guard (a brand-new entry has no prior stage).
                # See _is_stage_regression for the non-identity-mapping caveat
                # (under a non-identity stage_mapping this guard safely no-ops).
                if "stage" in attrs:
                    prior_stage = AttioClient.parse_entry(existing[0]).get("stage", "") or ""
                    if _is_stage_regression(prior_stage, attrs["stage"]):
                        self.stage_regressions_blocked += 1
                        logger.warning(
                            "add_list_entry: dropping regressing stage+dm_step on "
                            "entry_id=%s — prior_stage=%r, attempted new "
                            "stage=%r would lower cadence depth; stage and dm_step "
                            "keys stripped, other attrs still patched "
                            "(stage_regressions_blocked=%d)",
                            target_entry_id,
                            prior_stage,
                            attrs["stage"],
                            self.stage_regressions_blocked,
                        )
                        attrs = {
                            k: v for k, v in attrs.items() if k not in ("stage", "dm_step")
                        }
                return self.update_list_entry(
                    entry_id=target_entry_id,
                    entry_attributes=attrs,
                    list_id=lid,
                )

        body: dict = {
            "data": {
                "parent_record_id": record_id,
                "parent_object": "people",
                "entry_values": attrs,
            }
        }
        data = self._request("POST", f"/lists/{lid}/entries", json=body)
        return data.get("data", {})

    # F-PR-1: stage rank lookup delegates to the canonical STAGE_RANK in
    # models/pipeline.py. Used to pick the "most-advanced" entry when a
    # record has legacy duplicates; unknown stage strings fall back to 0
    # (sort to the end, oldest-created-at tiebreak wins).

    def _find_list_entries_for_record(self, record_id: str, list_id: str) -> list[dict]:
        """Return list entries in `list_id` whose parent is `record_id`,
        sorted highest-stage first. Empty list if none.

        Used by add_list_entry to upsert instead of blindly POSTing a fresh
        duplicate. Falls back to a client-side filter over a bulk query
        because Attio's list-entries API doesn't accept filters on
        parent_record_id directly at v2.
        """
        try:
            entries = self.query_list_entries(list_id=list_id, limit=50000)
        except httpx.HTTPStatusError:
            return []
        return self._filter_and_rank_entries_for_record(entries, record_id)

    def _filter_and_rank_entries_for_record(
        self, entries: list[dict], record_id: str
    ) -> list[dict]:
        """Filter `entries` to those whose parent is `record_id`, sorted
        highest-stage first. Pure client-side; no API call.
        """
        own: list[dict] = []
        for e in entries:
            parsed = AttioClient.parse_entry(e)
            if parsed.get("record_id") == record_id:
                own.append(e)
        if not own:
            return []

        def rank(entry: dict) -> tuple[int, str]:
            parsed = AttioClient.parse_entry(entry)
            stage_str = parsed.get("stage", "") or ""
            try:
                r = STAGE_RANK[PipelineStage(stage_str)]
            except (KeyError, ValueError):
                r = 0
            created = entry.get("created_at", "") or ""
            # Higher rank first, tiebreak by OLDER created_at (preserves history).
            return (-r, created)

        own.sort(key=rank)
        return own

    def update_list_entry(
        self,
        entry_id: str,
        entry_attributes: dict,
        list_id: str | None = None,
    ) -> dict:
        """Update a list entry (change stage, attributes, etc.)."""
        lid = list_id or os.environ.get("ATTIO_LIST_ID", "")
        data = self._request(
            "PATCH",
            f"/lists/{lid}/entries/{entry_id}",
            json={"data": {"entry_values": entry_attributes}},
        )
        return data.get("data", {})

    def delete_list_entry(self, entry_id: str, list_id: str | None = None) -> bool:
        """Delete a list entry. Returns True if deleted, False if not found."""
        lid = list_id or os.environ.get("ATTIO_LIST_ID", "")
        try:
            self._request("DELETE", f"/lists/{lid}/entries/{entry_id}")
            return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return False
            raise

    # ── Notes ─────────────────────────────────────────────────

    def create_note(self, record_id: str, title: str, content: str, parent_object: str = "people") -> dict:
        """Create a note on a person or company record.

        Attio's /v2/notes endpoint requires `format` (either "plaintext" or
        "markdown") paired with a `content` field. Historical versions of
        this client used `content_plaintext`, which the API now rejects with
        HTTP 400 validation_type.
        """
        data = self._request(
            "POST",
            "/notes",
            json={
                "data": {
                    "parent_object": parent_object,
                    "parent_record_id": record_id,
                    "title": title,
                    "format": "plaintext",
                    "content": content,
                },
            },
        )
        return data.get("data", {})

    def list_notes_for_record(
        self, record_id: str, parent_object: str = "people", limit: int = 50
    ) -> list[dict]:
        """List notes attached to a person or company record."""
        data = self._request(
            "GET",
            "/notes",
            params={
                "parent_object": parent_object,
                "parent_record_id": record_id,
                "limit": limit,
            },
        )
        return data.get("data", [])

    def delete_note(self, note_id: str) -> bool:
        """Delete a note. Returns True if deleted, False if not found."""
        try:
            self._request("DELETE", f"/notes/{note_id}")
            return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return False
            raise

    # ── Entry parsing helpers ────────────────────────────────

    @staticmethod
    def object_record_first_value(values: dict, slug: str):
        """First value of a slug on an object-record's `values` shape.

        Object-record reads (Operator Review Queue, Migration Run, Data
        Quality Report, daily_run, etc.) carry plain text/number/dict.
        Both `status.title` (status-typed attrs) and `option.title`
        (SELECT-typed attrs, e.g. `daily_run.status` /
        `reply_detection_status`) are unwrapped to their title string so
        the helper returns a scalar for those without a per-caller branch
        — mirroring the entry-side `_extract_value` select handling. This
        is the read that lets the daily_run reattach path see a select's
        real value through the normalized `Record.attributes` instead of
        a stale `None`.
        """
        items = (values or {}).get(slug) or []
        if not items:
            return None
        item = items[0]
        if isinstance(item, dict):
            return (
                item.get("value")
                or item.get("status", {}).get("title")
                or item.get("option", {}).get("title")
            )
        return item

    @staticmethod
    def parse_entry(entry: dict) -> dict:
        """Extract flat attributes from an Attio list entry."""
        values = entry.get("entry_values", {})
        return {
            "entry_id": entry.get("entry_id", entry.get("id", {}).get("entry_id", "")),
            "record_id": entry.get("parent_record_id", entry.get("id", {}).get("record_id", "")),
            "entry_created_at": entry.get("created_at"),
            "stage": AttioClient._extract_stage(values),
            "persona": AttioClient._extract_value(values, "persona"),
            "language": AttioClient._extract_value(values, "language"),
            "dm_step": AttioClient._extract_value(values, "dm_step"),
            "quality_score": AttioClient._extract_value(values, "quality_score"),
            "last_contact_date": AttioClient._extract_value(values, "last_contact_date"),
            "experiment_id": AttioClient._extract_value(values, "experiment_id"),
            "response_classification": AttioClient._extract_value(values, "response_classification"),
            "last_response_text": AttioClient._extract_value(values, "last_response_text"),
            "score_breakdown": AttioClient._extract_value(values, "score_breakdown"),
            "scoring_lane": AttioClient._extract_value(values, "scoring_lane"),
            "verdict_path": AttioClient._extract_value(values, "verdict_path"),
            "llm_rationale": AttioClient._extract_value(values, "llm_rationale"),
            # Phase 1 auto-research signals — read as None on legacy entries.
            "icp_lane_persisted": AttioClient._extract_value(values, "icp_lane_persisted"),
            "quality_score_band": AttioClient._extract_value(values, "quality_score_band"),
            "defensive_score": AttioClient._extract_value(values, "defensive_score"),
            "engagement_score": AttioClient._extract_value(values, "engagement_score"),
            # Fresh-prospect quarantine attrs. invite_eligible_after gates the
            # daily invite slice via is_invite_eligible; prospect_committed_at
            # is the forensic origin timestamp consumed by the starvation
            # evaluator. Without these extractions both signals would read None
            # for every entry and the §3.1 defense layer becomes a no-op.
            "prospect_committed_at": AttioClient._extract_value(values, "prospect_committed_at"),
            "invite_eligible_after": AttioClient._extract_value(values, "invite_eligible_after"),
            # PR-9a attrs — read by PR-9.5's §3.11 union-merge (per-step
            # timestamps + cohort identity freeze marker + canonical URL).
            # Without extractions every entry would read None and the
            # MAX-non-null per-attribute rule collapses to a no-op.
            "dm1_sent_at": AttioClient._extract_value(values, "dm1_sent_at"),
            "dm2_sent_at": AttioClient._extract_value(values, "dm2_sent_at"),
            "dm3_sent_at": AttioClient._extract_value(values, "dm3_sent_at"),
            "response_received_at": AttioClient._extract_value(values, "response_received_at"),
            "canonical_linkedin_url": AttioClient._extract_value(values, "canonical_linkedin_url"),
            "vanity_url_slug": AttioClient._extract_value(values, "vanity_url_slug"),
            # Cohort archaeology sentinel (§3.10). The DQR counts entries
            # stamped legacy_* as the observability metric for how much
            # of the cohort is locked out of sends; without this
            # extraction the DQR's legacy_archaeology_pool_count would
            # be a silent zero.
            "experiment_id_frozen_at": AttioClient._extract_value(values, "experiment_id_frozen_at"),
            # PR-9.5 dedup soft-delete pointer + cross-channel suppression
            # OR-merge participants. merged_into is a record-reference; the
            # extractor returns the target_record_id string for non-null
            # references (or None for losers that haven't been merged).
            "merged_into": AttioClient._extract_value(values, "merged_into"),
            "suppress_re_engagement": AttioClient._extract_value(values, "suppress_re_engagement"),
            "had_connection_note": AttioClient._extract_value(values, "had_connection_note"),
            # PR-39 cadence policy attributes. nurture_re_eligible_at is
            # the target date (post-DM3 cooldown end) after which the
            # NURTURE → DM1_SENT re-engagement is gated by the §3.18
            # four-gate check. cadence_lane is the typed lane stamp set
            # at PROSPECT-commit (promotes the legacy scoring_lane string).
            "nurture_re_eligible_at": AttioClient._extract_value(values, "nurture_re_eligible_at"),
            "cadence_lane": AttioClient._extract_value(values, "cadence_lane"),
        }

    @staticmethod
    def parse_deal(record: dict) -> dict:
        """Extract flat attributes from an Attio deal record."""
        values = record.get("values", {})

        def first(key: str) -> dict | None:
            arr = values.get(key, [])
            return arr[0] if arr and isinstance(arr, list) else None

        name_obj = first("name")
        stage_obj = first("stage")
        value_obj = first("value")
        company_obj = first("associated_company")
        country_obj = first("country")
        owner_obj = first("owner")

        stage = ""
        if isinstance(stage_obj, dict):
            status = stage_obj.get("status")
            if isinstance(status, dict):
                stage = status.get("title", "")

        deal_value = None
        currency = None
        if isinstance(value_obj, dict):
            deal_value = value_obj.get("currency_value")
            currency = value_obj.get("currency_code")

        return {
            "record_id": record.get("id", {}).get("record_id", ""),
            "name": name_obj.get("value") if isinstance(name_obj, dict) else None,
            "stage": stage,
            "value": deal_value,
            "currency": currency,
            "company_id": company_obj.get("target_record_id") if isinstance(company_obj, dict) else None,
            "country": country_obj.get("country_code") if isinstance(country_obj, dict) else None,
            "owner_id": owner_obj.get("referenced_actor_id") if isinstance(owner_obj, dict) else None,
        }

    @staticmethod
    def _extract_stage(values: dict) -> str:
        """Extract stage name from Attio entry values."""
        stage_data = values.get("stage", [])
        if stage_data and isinstance(stage_data, list):
            return stage_data[0].get("status", {}).get("title", "")
        return ""

    @staticmethod
    def _extract_value(values: dict, key: str):
        """Extract a simple attribute value from Attio entry values."""
        data = values.get(key, [])
        if data and isinstance(data, list):
            item = data[0]
            if not isinstance(item, dict):
                return item
            # Select-type attributes store value under option.title
            if item.get("attribute_type") == "select":
                return item.get("option", {}).get("title")
            return item.get("value", item)
        return None

    def extract_record_info(
        self, record: dict,
    ) -> tuple[str | None, str | None, str, str | None, str]:
        """Extract (name, company, linkedin_url, industry, title) from an Attio person record.

        Resolves record-reference company fields by fetching the linked company.
        Industry comes from the company's industry_vertical field. Title is the
        person's own job_title, surfaced so dry-run output can show it for ICP
        review before sending.
        """
        values = record.get("values", {})

        name_data = values.get(self._field_slug("full_name"), [])
        name = ""
        if name_data:
            first = name_data[0].get("first_name", "")
            last = name_data[0].get("last_name", "")
            name = f"{first} {last}".strip()

        title_data = values.get("job_title", [])
        title = ""
        if title_data:
            tv = title_data[0]
            title = str(tv.get("value", tv) if isinstance(tv, dict) else tv)

        person_record_id = record.get("id", {}).get("record_id", "")
        company = ""
        industry = ""
        company_data = values.get("company", values.get("primary_company", []))
        if company_data and isinstance(company_data, list) and company_data:
            ref = company_data[0]
            if isinstance(ref, dict) and ref.get("target_record_id"):
                cid = ref["target_record_id"]
                if person_record_id:
                    self._person_to_company[person_record_id] = cid
                if cid in self._company_cache:
                    company = self._company_cache[cid]
                    industry = self._industry_cache.get(cid, "")
                else:
                    cr_values: dict = {}
                    try:
                        cr = self._request("GET", f"/objects/companies/records/{cid}")
                        cr_data = cr.get("data", cr)
                        cr_values = cr_data.get("values", {})
                        # COMPANY name stays the LITERAL "name" — no documented
                        # field_mapping key for company name (full_name is the
                        # PERSON name), so routing it would invent a mapping.
                        cr_name = cr_values.get("name", [])
                        if cr_name:
                            company = cr_name[0].get("value", "")
                        iv = cr_values.get("industry_vertical", [])
                        if iv and isinstance(iv[0], dict):
                            industry = iv[0].get("option", {}).get("title", "")
                    except httpx.HTTPStatusError:
                        pass
                    self._company_cache[cid] = company
                    self._industry_cache[cid] = industry
                    self._company_corruption_cache[cid] = (
                        is_linkedin_clearbit_corrupted(
                            {"values": cr_values}, self._field_slug
                        )
                    )
            else:
                company = str(ref.get("value", ref))

        linkedin = ""
        linkedin_data = values.get(self._field_slug("linkedin_url"), [])
        if linkedin_data:
            val = linkedin_data[0]
            linkedin = str(val.get("value", val) if isinstance(val, dict) else val)

        # PR-14 (B-PD-006): return None on missing fields rather than
        # the literal string "Unknown". Downstream consumers MUST use
        # `is None` / `is not None` instead of `== "Unknown"`. Pre-PR-14
        # callers that compared against the literal were all updated
        # in this PR; see workflows/record_cache.py docstring for
        # the rationale.
        return (
            name or None,
            company or None,
            linkedin,
            industry or None,
            title,
        )

    def is_person_company_corrupted(self, person_record_id: str) -> bool:
        """Return True iff this person's linked company carries the
        LinkedIn-Clearbit fingerprint.

        Reads from caches populated by extract_record_info; safe to call
        for any person that the surrounding workflow has already resolved
        via RecordCache.get (the normal Phase A/B path). For unseen
        persons the answer is conservatively False — callers that need a
        guaranteed-fresh check should resolve the person first.
        """
        cid = self._person_to_company.get(person_record_id)
        if not cid:
            return False
        return self._company_corruption_cache.get(cid, False)

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
