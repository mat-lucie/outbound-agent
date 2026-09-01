"""Botdog REST API client — the OPTIONAL alternative delivery transport.

Thin transport client for ``https://api.botdog.co/v1``. PhantomBuster owns
sending in this engine (``clients/sender.py``); this client exists so an
operator who prefers Botdog has a typed, hardened surface to wire it
through, and so the event-ingest drain has something to poll. Nothing in
the engine constructs it unless the operator opted in.

Modeled on ``clients/phantombuster.py``: one ``httpx.Client``, a thin
``_request()`` chokepoint, typed in-module errors in the
``clients/pb_envelope.py`` style.

The typed contract:

- Every HTTP error surfaces as a ``BotdogError`` subclass — callers
  ``except BotdogError:`` for any Botdog-side failure, never a bare
  ``httpx.HTTPStatusError``. ``status_code`` + a scrubbed ``body_snippet``
  ride on the exception for audit logs.
- TRANSPORT-LEVEL failures are typed too: a connect/read timeout or any
  other ``httpx.RequestError``, and a 2xx body that is not decodable JSON,
  both raise ``BotdogError`` naming the underlying exception class.
  Neither is retried — a POST whose response never arrived may have landed
  server-side, and a blind retry would double-submit. The 429 path is the
  ONLY retry.
- HTTP 429 is retried INSIDE ``_request`` with a bounded backoff that
  honors ``Retry-After`` when present (Botdog rate-limits per key per 60s
  window). Exhausting the schedule raises ``BotdogRateLimited``. Unlike
  PB's per-endpoint launch retry, the retry applies to ALL endpoints —
  Botdog 429s any route, not just launches.
- HTTP 409 maps to ``BotdogLeadConflict`` (lead already exists). The
  add_to_campaign response may ALSO report per-lead conflicts inside a 2xx
  body; ``add_leads_to_campaign`` returns those per-lead results raw so
  callers (the sender's idempotency layer) decide.

Response schemas are only PARTLY pinned. List endpoints paginate as
``{"data": [...], "nextCursor": "<opaque>"}`` with the next page requested
as ``?cursor=<nextCursor>`` (followed by ``_request_paginated``), and
``GET /v1/leads/{id}`` returns flat first-class timestamps (``invitedAt`` /
``connectedAt`` / ``repliedAt`` / ``withdrawnAt`` / ``stoppedAt``) plus
``linkedinProfile``, ``campaignId``, ``customAttributes``, ``hasReplied``
and an ``events`` array. Everything else stays unverified. Methods
therefore return raw dicts / defensively-extracted lists rather than
invented field contracts; ``BotdogBatchResult`` is the one thin wrapper,
keeping the raw payload accessible alongside the best-effort per-lead list.
Tighten shapes only against observed live responses.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import httpx

from clients.botdog_config import DEFAULT_BLACKLIST_NAME, load_botdog_config
from clients.pb_envelope import _scrub_profile_urls

BASE_URL = "https://api.botdog.co/v1"

# Hard API cap on POST /v1/leads/add_to_campaign. Callers split their own
# batches — `add_leads_to_campaign` raises ValueError above this rather than
# auto-splitting, so a caller can never silently launch more requests (and
# burn more of the add_to_campaign rate budget) than it reviewed.
MAX_LEADS_PER_BATCH = 100

# Hard API cap on message text, enforced by `validate_message_text` in the
# methods that send text (`send_message`, `reply`). The API's validator
# counts UTF-16 code units (a JS string length), so astral-plane chars —
# emoji — count as 2; the guard measures the same way. Violations raise
# `BotdogInvalidMessage` before any request goes out (same stance as the
# >100 batch guard).
MAX_MESSAGE_TEXT_CHARS = 8000

# Page cap for cursor-paginated list endpoints. Botdog answers 25 rows per
# page, so 40 pages ~= 1000 rows. Hitting it with a cursor still live raises
# rather than truncating: a partial lead list is exactly the silent failure
# the loud check exists to prevent.
MAX_PAGES = 40

# Blacklist entries paginate the same way but a never-contact set is far
# larger than a campaign or a lead filter — thousands of rows is normal. The
# default 25/page x 40 = 1000-row ceiling would truncate it (and
# `_request_paginated` fails loud rather than truncating). So the blacklist
# read asks for the max page size the endpoint allows (`limit` 1-100) and
# lifts the page cap to keep a generous, still-bounded ceiling:
# 100 x 200 = 20,000 rows.
BLACKLIST_PAGE_SIZE = 100
BLACKLIST_MAX_PAGES = 200


def blacklist_name() -> str:
    """The blacklist collection holding the operator's never-contact set.

    Fetched-or-created BY NAME (case-insensitive), so the name is the
    idempotency key and must stay stable across runs.

    HOME: this resolver lives with the client, not with the seeding script,
    because TWO callers must agree on the answer —
    ``scripts/seed_botdog_blacklist`` (writes the collection) and the
    pre-send presence gate in
    ``workflows.daily_check_helpers.assert_botdog_blacklist_seeded`` (reads
    it — a helper an operator wires into a send path they build; no engine
    path calls it). If they ever disagreed, the gate would pass on a
    collection the
    seed never filled and Botdog could cold-contact a never-contact
    company. Operator-overridable via ``config/botdog.yaml``
    (``blacklist.collection_name``).
    """
    return load_botdog_config().blacklist_name or DEFAULT_BLACKLIST_NAME


# ---------------------------------------------------------------------
# Typed errors (pb_envelope style)
# ---------------------------------------------------------------------


class BotdogError(Exception):
    """Base for all Botdog client errors.

    Callers can `except BotdogError:` to catch any Botdog-side failure.
    Carries the HTTP status and a scrubbed body snippet (profile URLs
    replaced, same rule as `PBRunFailed`) so the message is safe to emit
    into audit logs and run reports.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body_snippet: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body_snippet = body_snippet


class BotdogRateLimited(BotdogError):
    """Raised when HTTP 429 persists past the bounded backoff schedule.

    `retry_after` is the last `Retry-After` value Botdog sent (seconds), or
    None when the header was absent/unparseable — callers deciding whether
    to defer the batch to the next run can key off it.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = 429,
        body_snippet: str = "",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(
            message, status_code=status_code, body_snippet=body_snippet
        )
        self.retry_after = retry_after


class BotdogLeadConflict(BotdogError):
    """Raised on HTTP 409 — the lead already exists in Botdog.

    A conflict is an idempotency signal, not a failure: the caller's
    pre-check missed an existing lead. Callers must log + surface the skip
    (never silent), not treat it as a delivery. Per-lead conflicts reported
    inside a 2xx add_to_campaign body do NOT raise — they come back in
    `BotdogBatchResult.lead_results` for the caller to classify.
    """


class BotdogInvalidMessage(BotdogError):
    """Raised before any request when message text violates the API's
    documented constraints (empty, or over the 8000-UTF-16-unit cap).

    A `BotdogError` subclass so senders keep their single
    `except BotdogError:` contract — bad message text is a data-driven
    per-prospect failure, unlike the bare-ValueError precondition guards
    (batch size, missing linkedinUrl), which signal caller bugs and are
    meant to crash loudly.
    """


def validate_message_text(text: str) -> None:
    # .strip(): whitespace-only must fail here too, or the batch-level blank
    # guard (workflows.daily_check_helpers._assert_no_blank_messages, which
    # strips) would have no transport backstop for the one blank definition
    # it uses.
    if not text.strip():
        raise BotdogInvalidMessage("Botdog message text must not be empty")
    try:
        utf16_units = len(text.encode("utf-16-le")) // 2
    except UnicodeEncodeError as exc:
        # Unpaired surrogates (reachable via JSON escapes in upstream data)
        # are unsendable; surface them as the same per-row
        # invalid_message_text failure instead of letting a raw
        # UnicodeEncodeError escape send_dm's never-raise contract and abort
        # the whole batch loop.
        raise BotdogInvalidMessage(
            f"Botdog message text contains unencodable characters "
            f"(unpaired surrogate): {exc}"
        ) from exc
    if utf16_units > MAX_MESSAGE_TEXT_CHARS:
        raise BotdogInvalidMessage(
            f"Botdog message text is {utf16_units} UTF-16 units — the "
            f"API caps text at {MAX_MESSAGE_TEXT_CHARS}"
        )


# ---------------------------------------------------------------------
# Thin response wrapper
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class BotdogBatchResult:
    """Typed thin wrapper for an add_to_campaign batch response.

    Botdog's exact response DTO is unverified, so `lead_results` is a
    best-effort extraction of the per-lead result list from the common
    envelope keys (empty when none is recognizable) and `raw` retains the
    full payload for audit/debugging and defensive callers.
    """

    raw: dict
    lead_results: tuple[dict, ...]


# Cursor markers — the KNOWN paging shape: `GET /v1/leads` answers
# `{"data": [...25 rows], "nextCursor": "<opaque>"}` and the next page is
# requested as `?cursor=<nextCursor>`; the last page carries a null/absent
# cursor. `_request_paginated` follows these, so callers that go through it
# pass `cursor_handled=True` and the marker is NOT a failure for them. Any
# OTHER caller (single-shot reads: accounts, campaigns) still fails loud on
# a live cursor — it would be silently reading only page 1.
_CURSOR_MARKERS = ("nextCursor", "next", "cursor")

# Envelope keys that betray a paginated response we do NOT know how to
# follow. These list DTOs remain unverified; if any of these rides
# alongside the list we extract, page 1 is not the whole answer and every
# caller that treats the result as complete (idempotency pre-checks, event
# polls, blacklist reconciliation) is silently wrong.
_UNKNOWN_PAGINATION_MARKERS = ("page", "totalPages", "hasMore")

_PAGINATION_MARKERS = (*_CURSOR_MARKERS, *_UNKNOWN_PAGINATION_MARKERS)


def _has_pagination_marker(
    data: dict, rows: list[dict], *, cursor_handled: bool = False
) -> bool:
    """True when `data` carries an UNHANDLED pagination signal.

    A marker present but explicitly empty (`None` / `False` / `""`) is the
    API saying "no further pages" — not a signal. `total` counts only when
    it exceeds the rows we actually got. When `cursor_handled` is set the
    caller is `_request_paginated`, which follows the cursor itself, so the
    cursor markers are ignored here.
    """
    markers = (
        _UNKNOWN_PAGINATION_MARKERS if cursor_handled else _PAGINATION_MARKERS
    )
    for key in markers:
        if key in data and data[key] not in (None, False, "", 0):
            return True
    total = data.get("total")
    return (
        isinstance(total, int)
        and not isinstance(total, bool)
        and total > len(rows)
    )


def _next_cursor(data: object) -> str | None:
    """The opaque next-page token from a list envelope, or None on the last
    page.

    Only an opaque TOKEN is followed. A marker holding a full URL is a
    `next`-link contract we do not implement, so it raises rather than
    being posted back as a `?cursor=` value (which would silently return
    page 1 forever).
    """
    if not isinstance(data, dict):
        return None
    for key in _CURSOR_MARKERS:
        value = data.get(key)
        if not isinstance(value, str) or not value:
            continue
        if value.startswith(("http://", "https://", "/")):
            raise BotdogError(
                f"paginated response uses a link-style {key!r} "
                f"({value[:80]!r}) — only opaque cursor tokens are "
                f"followed; pin the real shape before scaling."
            )
        return value
    return None


def _extract_list(
    data: object, *keys: str, cursor_handled: bool = False
) -> list[dict]:
    """Best-effort list extraction from an uncertain response shape.

    Accepts either a bare JSON array or an envelope dict keyed by any of
    `keys` (tried in order). Returns [] when nothing matches — callers
    treating [] as "no data" must keep `raw` access for forensics rather
    than assuming the API confirmed emptiness.

    UNHANDLED PAGINATION IS A LOUD FAILURE. The cursor shape (`nextCursor`
    + `?cursor=`) is followed by `_request_paginated`, which passes
    `cursor_handled=True` so its per-page extraction accepts a live cursor.
    Every OTHER marker (`page` / `totalPages` / `hasMore` / a `total` above
    the row count), and a cursor reaching a caller that does NOT follow it,
    still raises `BotdogError`: handing back page 1 as the whole set would
    make an idempotency pre-check miss existing leads and re-invite them,
    and an event poll silently drop events. The fix for a new marker is to
    implement its paging against observed traffic, not to relax the check.

    Note for the POST path (`add_leads_to_campaign`): raising here can fire
    AFTER a submission landed server-side. That is safe — the caller
    records the chunk as failed and re-queues it, and the
    campaign-membership idempotency pre-check stops the retry from
    double-inviting.
    """
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                rows = [row for row in value if isinstance(row, dict)]
                if _has_pagination_marker(
                    data, rows, cursor_handled=cursor_handled
                ):
                    marker_keys = sorted(
                        k for k in (*_PAGINATION_MARKERS, "total") if k in data
                    )
                    raise BotdogError(
                        f"paginated response detected — pagination "
                        f"unsupported; pin the real shape before scaling "
                        f"(list key {key!r}, {len(rows)} row(s), marker "
                        f"key(s) {marker_keys})"
                    )
                return rows
    return []


def collection_lead_count(collection: dict) -> int | None:
    """Lead count for a blacklist-collection payload, or None when the
    payload reports none.

    Prefers the `leadCount` field, falling back to the other count
    spellings and finally to len() of an embedded `leads` list
    (older/embedded shapes). None means "unknown", never "empty" — a real
    populated collection whose payload omits the count must not be mistaken
    for an empty one.
    """
    for key in ("leadCount", "leadsCount", "leads_count", "count", "size"):
        value = collection.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    leads = collection.get("leads")
    if isinstance(leads, list):
        return len(leads)
    return None


def select_blacklist(collections: list[dict], name: str) -> dict | None:
    """Pick the canonical collection named `name`, or None when none match.

    Among case-insensitive name matches, returns the one with the greatest
    `leadCount` (ties -> first seen). Duplicate same-named collections
    exist in the wild — an empty duplicate alongside the populated one.
    Selecting by max count means a stray EMPTY duplicate can never shadow
    the seeded set: neither the pre-send gate nor the seeder resolves to
    it. An unknown count sorts as 0, so a populated collection still wins
    over an explicit-zero duplicate, while a lone unknown-count collection
    is still selected (presence is enough — the gate decides emptiness).
    """
    matches = [
        bl for bl in collections
        if isinstance(bl, dict)
        and isinstance(bl.get("name"), str)
        and bl["name"].strip().lower() == name.strip().lower()
    ]
    if not matches:
        return None
    return max(matches, key=lambda bl: collection_lead_count(bl) or 0)


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """Parse `Retry-After` (delta-seconds form) from a 429 response.

    Returns None when absent or unparseable (Botdog documents the seconds
    form; the HTTP-date form falls through to the fallback schedule rather
    than being mis-parsed).
    """
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


class BotdogClient:
    """Client for the Botdog REST API v1.

    Transport only — batch assembly, review gates, cap leases, and all CRM
    advance logic stay with the callers (`clients/sender.py` seam). No
    method here mutates pipeline state.
    """

    BASE_URL = BASE_URL

    # Bounded 429 backoff fallback when Botdog omits `Retry-After`. Limits
    # reset per 60s window, so the schedule spans a bit more than one full
    # window (~1.9 min total) before giving up — mirrors the shape of PB's
    # launch backoff, shortened because a Botdog window is 60s, not a
    # phantom-slot wait.
    _RATE_LIMIT_BACKOFF_SCHEDULE = (5, 15, 30, 65)  # seconds

    # Honor `Retry-After` only up to this bound — the retry loop must stay
    # bounded even if the API sends a pathological header value.
    _RETRY_AFTER_CAP_SECONDS = 120.0

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ["BOTDOG_API_KEY"]
        self._client = httpx.Client(
            base_url=self.BASE_URL,
            headers={
                "x-api-key": self.api_key,
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    def _request(self, method: str, path: str, **kwargs) -> dict:
        """One HTTP round-trip with typed errors + bounded 429 retry.

        429 -> wait (`Retry-After` when present and sane, else the fallback
        schedule) and retry; schedule exhaustion raises
        `BotdogRateLimited`. Any other non-2xx raises immediately: 409 as
        `BotdogLeadConflict`, everything else as `BotdogError`. Empty
        bodies (e.g. 204) return {} like the PB client.

        TRANSPORT + DECODE failures raise `BotdogError` naming the
        underlying exception class, and are NEVER retried: a POST whose
        response we never saw (connect/read timeout, connection reset) may
        already have created leads server-side, so a retry here would
        double-submit. Callers own the retry decision — on the send path
        that means the lead re-queues and the campaign-membership pre-check
        makes the next attempt safe.
        """
        retry_after: float | None = None
        for attempt in range(len(self._RATE_LIMIT_BACKOFF_SCHEDULE) + 1):
            try:
                resp = self._client.request(method, path, **kwargs)
            except httpx.RequestError as exc:
                raise BotdogError(
                    f"Botdog API transport failure on {method} {path}: "
                    f"{type(exc).__name__}: {exc} (not retried — the "
                    f"request may have landed server-side)"
                ) from exc
            if resp.status_code == 429:
                retry_after = _retry_after_seconds(resp)
                if attempt == len(self._RATE_LIMIT_BACKOFF_SCHEDULE):
                    break  # schedule exhausted -> raise below
                wait_s = min(
                    retry_after
                    if retry_after is not None
                    else float(self._RATE_LIMIT_BACKOFF_SCHEDULE[attempt]),
                    self._RETRY_AFTER_CAP_SECONDS,
                )
                print(
                    f"  Botdog rate limit hit on {method} {path}; "
                    f"waiting {wait_s:g}s "
                    f"(attempt {attempt + 1}/"
                    f"{len(self._RATE_LIMIT_BACKOFF_SCHEDULE)})..."
                )
                time.sleep(wait_s)
                continue
            if resp.is_error:
                snippet = _scrub_profile_urls(resp.text[:300])
                message = (
                    f"Botdog API HTTP {resp.status_code} on "
                    f"{method} {path}: {snippet}"
                )
                if resp.status_code == 409:
                    raise BotdogLeadConflict(
                        message,
                        status_code=resp.status_code,
                        body_snippet=snippet,
                    )
                raise BotdogError(
                    message,
                    status_code=resp.status_code,
                    body_snippet=snippet,
                )
            if not resp.content:
                return {}
            try:
                data = resp.json()
            except ValueError as exc:
                # A 2xx whose body isn't JSON (HTML error page from a proxy,
                # truncated response). Typed + loud, never retried and never
                # silently treated as an empty body — "no data" and
                # "unparseable data" are different facts.
                snippet = _scrub_profile_urls(resp.text[:300])
                raise BotdogError(
                    f"Botdog API returned undecodable JSON on "
                    f"{method} {path} (HTTP {resp.status_code}, "
                    f"{type(exc).__name__}: {exc}): {snippet}",
                    status_code=resp.status_code,
                    body_snippet=snippet,
                ) from exc
            return data if isinstance(data, dict) else {"data": data}
        raise BotdogRateLimited(
            f"Botdog rate limit on {method} {path} persisted past "
            f"{len(self._RATE_LIMIT_BACKOFF_SCHEDULE)} retries",
            retry_after=retry_after,
        )

    def _request_paginated(
        self,
        path: str,
        *,
        params: dict | None = None,
        keys: tuple[str, ...] = ("leads", "results", "data"),
        max_pages: int = MAX_PAGES,
    ) -> list[dict]:
        """GET every page of a cursor-paginated list endpoint.

        The shape: `{"data": [...25 rows], "nextCursor": "<opaque>"}`, next
        page requested as `?cursor=<nextCursor>`, last page carrying a
        null/absent cursor. Rows accumulate across pages in server order.

        TRUNCATION IS NEVER SILENT. Two loud failures bound the loop:
        exhausting `max_pages` while a cursor is still live raises
        `BotdogError` (rather than returning a partial list that a caller
        would treat as complete), and a server repeating the same cursor
        raises too (an endless-loop guard — 40 identical pages would
        otherwise burn the whole rate-limit budget and hand back duplicated
        rows).

        Pacing is `_request`'s job: the 429 backoff already honors Botdog's
        per-minute window, so this loop needs no sleep of its own.
        """
        query = dict(params or {})
        rows: list[dict] = []
        cursor: str | None = None
        for _ in range(max_pages):
            if cursor is not None:
                query["cursor"] = cursor
            # Fresh dict per call: `query` is mutated between pages, and
            # httpx (plus any test asserting call args) must see the params
            # THIS page was requested with, not the final state.
            data = self._request("GET", path, params=dict(query))
            rows.extend(_extract_list(data, *keys, cursor_handled=True))
            next_cursor = _next_cursor(data)
            if not next_cursor:
                return rows
            if next_cursor == cursor:
                raise BotdogError(
                    f"Botdog pagination on GET {path} repeated the same "
                    f"cursor ({len(rows)} row(s) so far) — refusing to "
                    f"loop; the server is not advancing."
                )
            cursor = next_cursor
        raise BotdogError(
            f"Botdog pagination on GET {path} hit the {max_pages}-page cap "
            f"with a live cursor still pending ({len(rows)} row(s) "
            f"collected). Refusing to return a TRUNCATED list — a partial "
            f"lead set silently breaks idempotency pre-checks and event "
            f"polls. Raise MAX_PAGES (or filter the query) deliberately."
        )

    # -----------------------------------------------------------------
    # Leads
    # -----------------------------------------------------------------

    def add_leads_to_campaign(
        self, campaign_id: str, leads: list[dict]
    ) -> BotdogBatchResult:
        """Add up to `MAX_LEADS_PER_BATCH` leads to a campaign.

        Each lead is a LeadCreateDto dict: `linkedinUrl` (required),
        optional `name` / `title` / `company` / `location`, and
        `customAttributes` key-value pairs (e.g. the invite-note variable).
        Raises ValueError on an oversized batch or a lead missing
        `linkedinUrl` — the caller splits/repairs; this method never
        auto-splits (silent extra requests would bypass what the caller
        reviewed and burn the add_to_campaign rate budget).

        Returns the per-lead results wrapped in `BotdogBatchResult`.
        Per-lead conflicts inside a 2xx body land in `lead_results`; a
        whole-request 409 raises `BotdogLeadConflict` via `_request`.
        """
        if len(leads) > MAX_LEADS_PER_BATCH:
            raise ValueError(
                f"add_leads_to_campaign: batch of {len(leads)} exceeds the "
                f"API cap of {MAX_LEADS_PER_BATCH}; caller must split "
                f"(never auto-split — see method docstring)."
            )
        missing = [
            i for i, lead in enumerate(leads) if not lead.get("linkedinUrl")
        ]
        if missing:
            raise ValueError(
                f"add_leads_to_campaign: lead(s) at index {missing} missing "
                f"required 'linkedinUrl'."
            )
        data = self._request(
            "POST",
            "/leads/add_to_campaign",
            json={"campaignId": campaign_id, "leads": leads},
        )
        return BotdogBatchResult(
            raw=data,
            lead_results=tuple(_extract_list(data, "results", "leads", "data")),
        )

    def get_leads(self, **filters) -> list[dict]:
        """List ALL leads (every page), filtered by the given query params
        (e.g. `linkedinUrl=...`, `campaignId=...`). Filter names pass
        through verbatim — the API's filter vocabulary is not pinned here.

        Paginates: the endpoint answers 25 rows per page plus a
        `nextCursor` (see `_request_paginated`). Callers get the whole set
        or a loud `BotdogError` — never a silent page 1.
        """
        return self._request_paginated("/leads", params=filters)

    def get_lead(self, lead_id: str) -> dict:
        """Get one lead's DETAIL — the poll source for event-confirmed
        advances.

        Unlike a list row, the detail carries first-class flat timestamps
        (`invitedAt` / `connectedAt` / `repliedAt` / `withdrawnAt` /
        `stoppedAt`) plus `campaignId`, `customAttributes`,
        `linkedinProfile`, `hasReplied` and an `events` array.
        `BotdogSender.fetch_events` derives its events from those
        timestamps — list rows carry no event data at all. Not paginated
        (single object).
        """
        return self._request("GET", f"/leads/{lead_id}")

    # -----------------------------------------------------------------
    # Messaging
    # -----------------------------------------------------------------

    def send_message(self, *, text: str, lead_id: str) -> dict:
        """Send a direct message via POST /v1/messages.

        Request DTO pinned against the published OpenAPI spec: exactly
        `leadId` + `text`, both required, `text` non-empty and <= 8000
        UTF-16 units. The API rejects any other property ("property X
        should not exist"), so nothing else may ride along — no accountId,
        no passthrough extras. Text-constraint violations raise
        `BotdogInvalidMessage` here rather than burning a request.
        """
        validate_message_text(text)
        return self._request(
            "POST", "/messages", json={"leadId": lead_id, "text": text}
        )

    def reply(self, conversation_id: str, text: str) -> dict:
        """Reply inside an existing conversation
        (POST /v1/conversations/{id}/reply). Body is `text` only, same
        constraints as `send_message` — per the published OpenAPI spec, but
        UNVERIFIED against live traffic (reply has no production caller
        here). Re-check this DTO on the wire before wiring reply into a
        live flow."""
        validate_message_text(text)
        return self._request(
            "POST",
            f"/conversations/{conversation_id}/reply",
            json={"text": text},
        )

    # -----------------------------------------------------------------
    # Blacklist (collections + leads-in-collection)
    # -----------------------------------------------------------------

    def get_blacklists(self) -> list[dict]:
        """List ALL blacklist collections, every page (GET /v1/blacklist).

        The endpoint is cursor-paginated — `{"data": [...], "nextCursor":
        ...}` — with a `leadCount` per collection and NO embedded entries:
        entry lists live at `GET /v1/blacklist/{id}/leads`
        (`get_blacklist_leads`). Reading only page 1 would silently miss
        collections, so this goes through `_request_paginated` (whole set
        or a loud `BotdogError`).
        """
        return self._request_paginated(
            "/blacklist", keys=("blacklists", "results", "data")
        )

    def get_blacklist_leads(self, blacklist_id: str) -> list[dict]:
        """List ALL leads in one blacklist collection, every page
        (GET /v1/blacklist/{id}/leads).

        The dedicated entries endpoint — `GET /v1/blacklist` does not embed
        entries. Same cursor shape as `/leads`: `{"data": [...25 rows],
        "nextCursor": ...}`, each entry carrying `id` / `name` /
        `linkedinProfile` / `company` / `createdAt`. The idempotent-seed
        already-present check and any blacklist reconciliation read this —
        so completeness matters: callers get the whole set or a loud
        `BotdogError`, never a silent page 1.

        Reads at the max `limit` and a lifted page cap
        (`BLACKLIST_PAGE_SIZE` / `BLACKLIST_MAX_PAGES`) because a
        never-contact set is far larger than the default 1000-row ceiling.
        """
        return self._request_paginated(
            f"/blacklist/{blacklist_id}/leads",
            params={"limit": BLACKLIST_PAGE_SIZE},
            max_pages=BLACKLIST_MAX_PAGES,
        )

    def create_blacklist(self, name: str) -> dict:
        """Create a blacklist collection (POST /v1/blacklist)."""
        return self._request("POST", "/blacklist", json={"name": name})

    def add_to_blacklist(self, blacklist_id: str, leads: list[dict]) -> dict:
        """Add leads to an existing blacklist collection
        (POST /v1/blacklist/{id}/leads).

        The seeding script composes `get_blacklists` / `create_blacklist` +
        this. Lead dicts pass through verbatim (`linkedinUrl`-keyed, same
        LeadCreateDto shape as campaigns).
        """
        return self._request(
            "POST", f"/blacklist/{blacklist_id}/leads", json={"leads": leads}
        )

    # -----------------------------------------------------------------
    # Accounts + limits
    # -----------------------------------------------------------------

    def get_accounts(self) -> list[dict]:
        """List connected LinkedIn accounts (GET /v1/accounts)."""
        data = self._request("GET", "/accounts")
        return _extract_list(data, "accounts", "results", "data")

    def get_account_limits(self, account_id: str) -> dict:
        """Read one account's send limits (GET /v1/accounts/{id}/limits)."""
        return self._request("GET", f"/accounts/{account_id}/limits")

    def set_account_limits(self, account_id: str, caps: dict) -> dict:
        """Set an account's send limits to match OUR cap config
        (PATCH /v1/accounts/{id}/limits). Caps pass through verbatim.

        Policy note: only the explicit limits-sync command calls this —
        never silently from a run.
        """
        return self._request(
            "PATCH", f"/accounts/{account_id}/limits", json=caps
        )

    # -----------------------------------------------------------------
    # Campaigns
    # -----------------------------------------------------------------

    def get_campaigns(self) -> list[dict]:
        """List campaigns (GET /v1/campaigns)."""
        data = self._request("GET", "/campaigns")
        return _extract_list(data, "campaigns", "results", "data")

    def get_campaign_leads(self, campaign_id: str) -> list[dict]:
        """List ALL the leads in one campaign, every page
        (GET /v1/campaigns/{id}/leads).

        Paginated like `/leads` — and completeness matters most here: this
        is the invite idempotency pre-check's ground truth, so a truncated
        page 1 would re-invite leads already in the campaign.
        """
        return self._request_paginated(f"/campaigns/{campaign_id}/leads")

    # -----------------------------------------------------------------
    # Health
    # -----------------------------------------------------------------

    def health(self) -> dict:
        """API health probe (GET /v1/health) — the run-start liveness
        check."""
        return self._request("GET", "/health")

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
