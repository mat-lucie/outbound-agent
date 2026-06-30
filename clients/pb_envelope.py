"""Typed PhantomBuster envelopes + advance-gate predicate.

The §3.1 hard-red-line (no re-sends, no re-invites) requires that
stage NEVER advance for a prospect row whose send was not actually
delivered. PB's per-URL telemetry is unreliable — the fetch-output
log can omit successful sends; result.csv gets overwritten on the
next agent run — so the advance gate keys to PB's CSV `status`
column with a 3-condition predicate.

Send-phantom callers (Message Sender, Network Booster) compose:

    launch = pb.launch_agent(agent_id, args)            # → PBLaunch
    completion = pb.wait_for_completion(launch)         # → PBCompletion (raises on err/timeout)
    csv_text = pb.download_result_csv(launch)           # container-keyed on happy path
    outcome = parse_send_outcome(launch, completion, csv_text, requested_urls)
    if should_advance_batch(launch, outcome):
        for url in outcome.sent_urls: ... advance Attio ...
    else:
        emit_pb_silent_no_op(launch, outcome, attio, audit_logger)
        return  # no state mutation

Read-only callers (Profile Scraper, Inbox Scraper, Search Export)
skip `parse_send_outcome` + the gate — they consume `PBCompletion`
and the raw CSV directly. The typed launch + raises-on-error wait
+ container-keyed CSV download still apply.

The advance gate predicate:

    stage MAY advance iff
        outcome.csv_status == "Message sent"
        AND outcome.container_id == launch.container_id
        AND outcome.sent_count >= 1

All three required. See `should_advance_batch`.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal
from urllib.parse import unquote

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(frozen=True)
class PBLaunch:
    """Typed envelope for a PhantomBuster agent launch.

    `container_id` is the run-scoped identifier returned by PB's
    `/agents/launch` endpoint. It uniquely identifies THIS launch
    (subsequent re-launches of the same agent get different container
    ids), so result CSVs can be matched back to the specific request
    that produced them.
    """

    container_id: str
    agent_id: str
    launched_at: datetime
    arguments_sha256: str
    request_id: str | None = None  # PB doesn't always echo a request id


@dataclass(frozen=True)
class PBCompletion:
    """Typed envelope for a PhantomBuster agent run that finished
    without error (the error case raises `PBRunFailed`)."""

    container_id: str
    status: Literal["finished"]
    log_output: str
    raw_output: dict


@dataclass(frozen=True)
class SendOutcome:
    """Typed envelope for a PB send-phantom (Message Sender, Network
    Booster) batch result.

    `csv_status` is the batch-level verdict — a 4-state literal that
    drives the advance gate. Per-URL detail lives in `sent_urls` /
    `skipped_urls` so callers can match individual prospect rows back
    to their delivery state.

    `next_day_drift_key` is the correlation key consumed by the
    next-day drift detector (lands separately) to spot rows that
    "advanced today, missing tomorrow."

    Cross-field invariants enforced in `__post_init__`:
    - `sent_count == len(sent_urls)`
    - `csv_status == "Message sent"` ⟹ `sent_urls` is non-empty
    - `csv_status ∈ {"Empty", "Error", "Skipped"}` ⟹ `sent_urls` is empty

    These invariants are upheld by `parse_send_outcome` by
    construction, but the `__post_init__` defends against a future
    second constructor (or a test fixture) introducing drift.
    """

    container_id: str
    csv_status: Literal["Message sent", "Skipped", "Error", "Empty"]
    sent_count: int
    requested_count: int
    drift_skipped_reason: str | None
    next_day_drift_key: str
    sent_urls: frozenset[str] = field(default_factory=frozenset)
    skipped_urls: frozenset[str] = field(default_factory=frozenset)
    # True when the send phantom reported its input as already-processed
    # (Auto Connect dedup: "We already processed every profile from this
    # spreadsheet"). This is the EXPLICIT already-invited signal — distinct
    # from a generic Skipped (cap/error) — that the Part-A advance gate uses
    # to advance prospects to CONNECTION_SENT. Never set on a generic skip.
    already_processed: bool = False

    def __post_init__(self) -> None:
        if self.sent_count != len(self.sent_urls):
            raise ValueError(
                f"SendOutcome invariant: sent_count={self.sent_count} "
                f"!= len(sent_urls)={len(self.sent_urls)}"
            )
        if self.csv_status == "Message sent" and not self.sent_urls:
            raise ValueError(
                "SendOutcome invariant: csv_status='Message sent' "
                "requires non-empty sent_urls"
            )
        if (
            self.csv_status in ("Empty", "Error", "Skipped")
            and self.sent_urls
        ):
            raise ValueError(
                f"SendOutcome invariant: csv_status={self.csv_status!r} "
                f"forbids non-empty sent_urls (got "
                f"{len(self.sent_urls)} rows)"
            )


class PBError(Exception):
    """Base for all PhantomBuster client errors.

    Callers can `except PBError:` to catch any PB-side failure — both
    `PBRunFailed` (PB reported `status="error"`) and `PBRunTimeout`
    (poll exhaustion). Future client errors (rate-limit, auth) should
    also subclass this so the exception surface stays unified.
    """


def _scrub_profile_urls(text: str) -> str:
    """Replace linkedin.com/in/<slug> URLs with a placeholder.

    Used to sanitize PB log tails before embedding them in exception
    messages that flow into audit logs, DLQ rows, and queue serializations.
    The raw log_tail attribute retains the full text for in-process use.
    (L10-8 audit fix.)
    """
    import re
    return re.sub(
        r"https?://(?:www\.)?linkedin\.com/in/[^\s,\"'>\]]+",
        "<profile-url>",
        text,
    )


class PBRunFailed(PBError):
    """Raised by `wait_for_completion` when PB reports `status="error"`.

    Replaces the prior contract where the caller had to check
    `output.get("status") == "error"` after the wait returned, which
    several callers forgot to do (silent-error swallow).

    The exception message has profile URLs scrubbed so it is safe to
    emit into audit logs and DLQ rows. The raw ``log_tail`` attribute
    retains the full text for in-process diagnostics. (L10-8 audit fix.)
    """

    def __init__(
        self,
        container_id: str,
        agent_id: str,
        log_tail: str,
    ) -> None:
        scrubbed = _scrub_profile_urls(log_tail[-300:])
        super().__init__(
            f"PB run {container_id} (agent={agent_id}) reported status=error. "
            f"Log tail: {scrubbed}"
        )
        self.container_id = container_id
        self.agent_id = agent_id
        self.log_tail = log_tail


class PBRunTimeout(PBError):
    """Raised by `wait_for_completion` when polling exhausts `max_wait`.

    Carries the LAST observed status + raw output so the caller can
    decide whether to salvage partial state or drop the batch.
    Callers MUST NOT write confirmed counts derived from
    `last_observed_output` to Attio without simultaneously escalating
    the uncertainty — partial counts must be either escalated or
    dropped, never silently committed.
    """

    def __init__(
        self,
        container_id: str,
        agent_id: str,
        elapsed_seconds: int,
        last_observed_status: str | None = None,
        last_observed_output: dict | None = None,
    ) -> None:
        super().__init__(
            f"PB run {container_id} (agent={agent_id}) did not finish in "
            f"{elapsed_seconds}s "
            f"(last observed status: {last_observed_status or 'none'})"
        )
        self.container_id = container_id
        self.agent_id = agent_id
        self.elapsed_seconds = elapsed_seconds
        self.last_observed_status = last_observed_status
        self.last_observed_output = last_observed_output


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def hash_arguments(arguments: dict | None) -> str:
    """Stable SHA-256 of the launch arguments dict.

    Recorded on `PBLaunch.arguments_sha256` so the audit log can
    correlate "what did we ask for" with "what did we get" across
    container ids — e.g. detect when an executor mistakenly re-launches
    with stale args.
    """
    if not arguments:
        return hashlib.sha256(b"{}").hexdigest()
    payload = json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _normalize_url_for_match(url: str) -> str:
    """Same normalization `workflows.daily_check_helpers._normalize_linkedin_url`
    applies, restated here so `clients/pb_envelope.py` has zero
    dependencies on `workflows/`.

    Keeps the URL scheme intact (e.g. `https://linkedin.com/in/x`) so the
    keys produced here match what callers' own normalization produces.
    Diverging from that helper would break the advance gate's per-URL
    match — `tests/test_pb_envelope.py::TestUrlNormalizationParity`
    pins the equivalence.
    """
    if not url:
        return ""
    return unquote(url).replace("://www.", "://").rstrip("/").lower()


def parse_send_outcome(
    launch: PBLaunch,
    completion: PBCompletion,
    csv_text: str | None,
    requested_urls: set[str],
    *,
    next_day_drift_key: str | None = None,
) -> SendOutcome:
    """Wrap a finished send-phantom run into a `SendOutcome`.

    Resolution of `csv_status`:

    - `"Empty"` — no CSV came back at all (PB swallowed it, or the
      phantom finished without writing one), OR the CSV had a header
      but zero data rows.
    - `"Error"` — `csv.DictReader` raised `csv.Error` while parsing
      (malformed CSV). Kept distinct from "Skipped" so audit logs can
      distinguish "PB returned junk" from "PB returned valid CSV
      reporting no sends." Both fail the advance gate identically.
    - `"Skipped"` — CSV parsed cleanly, rows exist, but ZERO rows are
      "Message sent" (everything was InMail-required, "Can't send",
      etc.)
    - `"Message sent"` — at least one row reports "Message sent"

    The advance gate (`should_advance_batch`) requires the
    `"Message sent"` state — anything else fails the gate.
    """
    sent_urls: set[str] = set()
    skipped_urls: set[str] = set()
    drift_skipped_reason: str | None = None

    if not csv_text:
        csv_status: Literal["Message sent", "Skipped", "Error", "Empty"] = "Empty"
        drift_skipped_reason = "pb_returned_no_csv"
    else:
        try:
            reader = csv.DictReader(io.StringIO(csv_text))
            rows_seen = 0
            for row in reader:
                rows_seen += 1
                url = (
                    row.get("query")
                    or row.get("linkedinProfileUrl")
                    or row.get("linkedInUrl")
                    or row.get("profileUrl")
                    or ""
                )
                if not url:
                    continue
                status = (row.get("status") or "").strip().lower()
                key = _normalize_url_for_match(url)
                if status == "message sent":
                    sent_urls.add(key)
                else:
                    skipped_urls.add(key)
            if rows_seen == 0:
                csv_status = "Empty"
                drift_skipped_reason = "pb_csv_had_zero_rows"
            elif sent_urls:
                csv_status = "Message sent"
            else:
                csv_status = "Skipped"
                drift_skipped_reason = "pb_csv_zero_sent_rows"
        except csv.Error as exc:
            # Malformed CSV is treated as Error — we can't trust the
            # rows. Caller advance gate will reject (csv_status !=
            # "Message sent"), no stage advances.
            csv_status = "Error"
            drift_skipped_reason = f"pb_csv_parse_error: {exc!s}"[:200]

    sent_count = len(sent_urls)
    requested_count = len(requested_urls)
    drift_key = next_day_drift_key or _build_drift_key(launch, requested_urls)

    # If sent_count fell short of requested_count but the CSV said
    # "Message sent", surface the gap reason — operator wants to see
    # whether PB dropped rows silently or some inputs were skipped.
    if csv_status == "Message sent" and sent_count < requested_count:
        drift_skipped_reason = (
            f"pb_sent_{sent_count}_of_{requested_count}_requested"
        )

    # Detect the Auto Connect dedup signal from the run log. This is the
    # deterministic phantom status line for "every input profile was already
    # processed (invited) in a prior launch" — a cap/error skip emits a
    # different message, so this marker is safe to treat as already-invited.
    already_processed = (
        "already processed every profile" in (completion.log_output or "").lower()
    )

    return SendOutcome(
        container_id=launch.container_id,
        csv_status=csv_status,
        sent_count=sent_count,
        requested_count=requested_count,
        drift_skipped_reason=drift_skipped_reason,
        next_day_drift_key=drift_key,
        sent_urls=frozenset(sent_urls),
        skipped_urls=frozenset(skipped_urls),
        already_processed=already_processed,
    )


def _build_drift_key(launch: PBLaunch, requested_urls: set[str]) -> str:
    """Construct a deterministic drift-correlation key.

    The next-day drift detector queries the audit log for entries
    that advanced stage today and matches them against tomorrow's
    `dm_step` / `last_observed_degree`. The key is `agent_id` +
    `launched_at` date + a stable hash of the requested URL set — so
    re-launches of the same batch produce the same key on the same
    day, while a re-launch of the same URL set tomorrow produces a
    different key (intentional: scoping drift to a single day is the
    correct behavior for next-day detection).
    """
    urls_hash = hashlib.sha256(
        ",".join(sorted(requested_urls)).encode()
    ).hexdigest()[:12]
    return f"{launch.agent_id}:{launch.launched_at.date().isoformat()}:{urls_hash}"


def should_advance_batch(launch: PBLaunch, outcome: SendOutcome) -> bool:
    """The §3.1 chokepoint predicate.

    Stage MAY advance for prospects in this batch iff ALL THREE hold:

    1. `outcome.csv_status == "Message sent"` — PB confirms at least
       one row was delivered
    2. `outcome.container_id == launch.container_id` — the outcome is
       wired to THIS launch, not a stale CSV from a prior run
    3. `outcome.sent_count >= 1` — at least one actual send is in the
       sent_urls set

    Returns False on any miss; callers must take the dry-skip path
    (see `emit_pb_silent_no_op`).

    Defense-by-construction note: today, `parse_send_outcome` always
    sets `outcome.container_id = launch.container_id`, so condition 2
    is effectively always True when callers follow the documented
    compose pattern. The check is the *contract* — a future caller
    that synthesizes a `SendOutcome` from a cached/replayed CSV (or
    misnames a launch+outcome pair in a refactor) MUST be caught
    here. Container-keyed CSV download (`download_result_csv(launch)`
    in `clients/phantombuster.py`) is the stale-CSV guard at the
    fetch layer; this is the second layer.
    """
    return (
        outcome.csv_status == "Message sent"
        and outcome.container_id == launch.container_id
        and outcome.sent_count >= 1
    )


# Markers that mean the LinkedIn session itself failed — invites did NOT go out.
# These (and ONLY these) must block the invite optimistic-advance below. The
# benign "please check on LinkedIn that you can manually invite profiles"
# warning is NOT here: operator-confirmed, the invites still send through it.
_AUTH_FAILURE_MARKERS = (
    "no valid credentials",
    "network-cookie-invalid",
    "can't connect to linkedin with this session cookie",
    "session cookie not valid",
    "session expired",
)

# LinkedIn throughput/restriction signals — invites are silently DROPPED (not
# sent) when these fire, so they must ALSO block the optimistic advance.
# Unlike auth failures these are RECURRING for an active outreach account
# (the weekly invite cap is hit routinely). Advancing on a cap would falsely
# mark un-sent prospects CONNECTION_SENT and Phase 0 would wait forever for an
# acceptance that can never arrive — a silent, permanent pipeline loss.
# (The benign "verify the request was correctly sent" warning is NOT here.)
_CAP_RESTRICTION_MARKERS = (
    "weekly invitation limit",
    "reached the weekly",
    "invitation limit reached",
    "too many pending invitation",
    "withdraw some of your pending",
    "account is restricted",
    "temporarily restricted",
    "commercial use limit",
)


# Profile-scraper dedup refusal markers. PB keys per-agent processing state
# on the result CSV filename; when every submitted profile is already in
# that file's database the container logs one of these and appends ZERO
# rows. Callers that need re-scrape semantics (degree-flip detection) must
# treat a marker hit as "no fresh data", never as a confident result.
SCRAPER_DEDUP_MARKERS: tuple[str, ...] = (
    "already scraped",
    "already processed",
)


def has_scraper_dedup_marker(log_output: str | None) -> bool:
    """True if a container log shows the phantom refused to re-scrape."""
    low = (log_output or "").lower()
    return any(marker in low for marker in SCRAPER_DEDUP_MARKERS)


def invite_launch_authenticated(completion: PBCompletion) -> bool:
    """True unless the Network Booster log shows a LinkedIn auth failure.

    The Network Booster (Auto Connect) result.csv is NOT a reliable per-run
    send report — it has no `status` column (success is an empty `error`
    field), accumulates rows across runs, and keeps stale timestamps on
    cache-hit profiles. So `parse_send_outcome` (built for the Message Sender
    schema) always reports `Skipped`/0 for invites, and the advance gate never
    flips PROSPECT→CONNECTION_SENT — invites re-queue forever and Phase 0 never
    watches for their acceptance.

    Per the 2026-06-05 decision, the invite path advances OPTIMISTICALLY: a
    clean, authenticated launch is taken as proof the requested invites were
    sent (LinkedIn dedups re-invites; the degree check reconciles edge cases).
    The one hard gate is authentication — a dead cookie ("No valid
    credentials") means nothing sent, so we must NOT advance.
    """
    log = (completion.log_output or "").lower()
    return not any(marker in log for marker in _AUTH_FAILURE_MARKERS)


# Sentinel stamped on the outcome when invites are advanced on log-confidence
# (not CSV confirmation). Flows through to the daily-run audit so a wave of
# false advances from an un-listed PB failure string is RECOVERABLE.
INVITE_OPTIMISTIC_ADVANCE = "invite_optimistic_advance"


def invite_launch_advanceable(completion: PBCompletion) -> bool:
    """True only if a clean Network Booster launch may optimistically advance.

    Blocks on BOTH auth failure (dead cookie → nothing sent) AND any LinkedIn
    cap/restriction signal (weekly invite limit, pending ceiling, account
    restriction → invites silently dropped). Either means the invites did not
    go out, so advancing would falsely mark un-sent prospects CONNECTION_SENT
    and Phase 0 would wait forever for an acceptance that cannot come.
    """
    log = (completion.log_output or "").lower()
    return not any(
        marker in log
        for marker in (*_AUTH_FAILURE_MARKERS, *_CAP_RESTRICTION_MARKERS)
    )


def compute_invite_outcome(
    outcome: SendOutcome,
    completion: PBCompletion,
    requested_urls: set[str],
) -> SendOutcome:
    """Invite-path override of the (unreliable) parsed Network Booster outcome.

    Authoritative advance: on a clean launch (authenticated, no cap/restriction)
    with at least one requested invite, mark ALL requested URLs as sent so the
    standard per-row advance path runs (with correct experiment-cohort
    stamping), stamping `drift_skipped_reason=INVITE_OPTIMISTIC_ADVANCE` so the
    advance is auditable. Otherwise return the parsed outcome unchanged:
      - `already_processed` → the existing Pattern-A branch handles it.
      - auth failure / cap / restriction → stays Skipped → gate fails →
        pb_silent_no_op (invites re-queue; never a false CONNECTION_SENT).
      - no requested invites (recheck-only batch) → nothing to advance.

    Advancing every requested row on a clean launch is deliberate: withholding
    rows that the phantom physically invited but omitted from its per-launch log
    (log-format drift OR its own already-processed dedup) left them at PROSPECT
    and re-fed them forever — the daily re-selection leak, with pending invites
    piling up on LinkedIn. The explicit per-launch profile-count argument removed
    the silent truncation that once motivated a `requested ∩ processed` gate, so
    a row absent from the parsed list is now a benign artifact, not an
    un-attempted row. The HARD blocks remain in `invite_launch_advanceable`
    (auth failure / cap / restriction → invites did NOT go out → no advance),
    and the pre-invite degree check reconciles the rare ghost via
    `hasPendingInvitation`.

    DM batches must NOT use this — they have a reliable `status` column and go
    through `parse_send_outcome` + `should_advance_batch` directly.
    """
    if outcome.already_processed:
        return outcome
    if not requested_urls:
        return outcome
    if not invite_launch_advanceable(completion):
        return outcome
    return replace(
        outcome,
        csv_status="Message sent",
        sent_urls=frozenset(requested_urls),
        skipped_urls=frozenset(),
        sent_count=len(requested_urls),
        drift_skipped_reason=INVITE_OPTIMISTIC_ADVANCE,
    )
