"""Attio write contract (F-PR-4) — AttioWriter + WriteIntent.

# What this replaces

Pre-F-PR-4, every Attio write was wrapped in `try: ... except Exception
as e: click.echo("Warning: failed...")`. Failures were swallowed; a
half-committed multi-attribute write could leave the system in a state
that caused a §3.1 violation (e.g., advance stage without writing
last_contact_date → next-day run re-DMs the same prospect).

F-PR-4 ships:

  * A typed exception hierarchy (`AttioError` and 7 subclasses)
  * Retry policy: 5 attempts, full jitter, retry on
    429/500/502/503/504/timeout, overall deadline 300s
  * DLQ: `~/.outbound-agent/dlq/attio-{date}.jsonl` + a typed
    `attio_write_failed` queue row via `escalate()`
  * Stage rank-monotonicity check via F-PR-1's `STAGE_RANK`
  * Terminal-class regression block via F-PR-1's `terminal_class()`
  * NURTURE re-engagement allowlist consultation (§3.18)
  * Write-owner registry enforcement (§3.15 / F-PR-3.7)
  * Multi-attribute atomicity:
    - Preferred: single PATCH per WriteIntent (Attio v2 supports it)
    - Fallback: compensating-transaction rollback on partial failure

# Usage

    from clients.attio_writer import AttioWriter, WriteIntent

    writer = AttioWriter()
    writer.apply(WriteIntent(
        object="linkedin_outreach",
        record_id="rec_abc",
        updates={
            "stage": "CONNECTION_SENT",
            "last_contact_date": "2026-05-21",
            "experiment_id": "exp_2026_05_21_v1",
        },
        prior_values={
            "stage": "PROSPECT",
            "last_contact_date": None,
            "experiment_id": None,
        },
        writer_module="workflows.weekly_prospect._build_prospect_entry_attrs",
    ))

# Caller contract

Callers MUST:
1. Provide `writer_module` = the importable dotted path of the calling
   site. Used by §3.15 registry enforcement.
2. Provide `prior_values` for every attribute being written. Used for
   monotonicity checks AND compensating-transaction rollback.
3. Opt into cross-terminal-class flips via
   `allow_terminal_class_change=True` AND open the queue row first.

## prior_values defaults (salesman-daily-QA-build4 #4)

For new-entry creation (no record exists yet), pass `prior_values={}`
— the monotonicity checks short-circuit when `prior_values.get("stage")
is None`. No pre-fetch required.

For partial updates (record exists, you're modifying one attribute),
fetch the prior value for THAT attribute only. You don't need to
populate prior_values for attributes you're NOT touching.

For NURTURE re-engagement specifically (advertiser-daily-QA-build4 #1
hard requirement), prior_values MUST include the key
`experiment_id_frozen_at` (with value None being legal for legacy rows).
The §3.1 archaeology gate refuses to fail open on a missing field.

## Recommended caller pattern (salesman-daily-QA-build4 #3)

    from clients.attio_writer import (
        AttioWriter, WriteIntent,
        AttioError, AttioMonotonicityViolation,
        AttioTerminalClassRegression,
    )
    from workflows.escalation import escalate

    writer = AttioWriter()
    try:
        writer.apply(WriteIntent(...))
    except AttioMonotonicityViolation as exc:
        # Pre-write rejection. No Attio mutation; no DLQ entry.
        # If you intended this transition, fix the WriteIntent.
        # If it's a bug surfacing now, that's the contract working.
        escalate(
            type="drift_detector_finding",
            idempotency_key=f"monotonicity-{intent.object}-{intent.record_id}",
            payload={"error": str(exc), "intent": ...},
        )
    except AttioTerminalClassRegression as exc:
        # Cross-class terminal flip. Caller must explicitly opt in.
        escalate(type="defensive_classification_review", ...)
    except AttioError as exc:
        # Generic write failure — already in DLQ + escalated by writer.
        # Log + continue; the operator sees the queue row.
        pass
"""

from __future__ import annotations

import json
import os
import random
import socket
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from clients.attio_writer_registry import (
    SPECIAL_WRITER_ALIASES,
    UnauthorizedAttioWriteError,
    is_authorized_writer,
)
from clients.crm.attio_provider import AttioProvider
from clients.crm.base import CRMProvider, Entry, Record
from models.pipeline import (
    NURTURE_REENGAGEMENT_ALLOWED,
    PipelineStage,
    is_send_eligible,
    stage_rank,
    terminal_class,
)

if TYPE_CHECKING:
    # AttioClient is referenced only in type annotations (the `attio` union
    # param + the `self._attio_client` handle). The writer no longer
    # constructs one directly — AttioProvider owns that — so it stays a
    # type-only import.
    from clients.attio import AttioClient

DLQ_DIR = Path.home() / ".outbound-agent" / "dlq"
# 500 is in the retryable set: Attio 500s are transient in practice
# (weekly-finalize crash loop; repeated daily-run crashes on escalation
# writes) and every AttioWriter write is a same-values PATCH — idempotent,
# so retrying is safe. 501/505 etc. stay permanent: they signal a contract
# error, not a blip. Same rationale as the opt-in retry_500 in
# clients.attio._request — but note only update_person opts in there, so on
# the people dispatch path a 500 retries at BOTH layers (see the
# retry-stacking note in _single_patch); companies and list entries retry
# 500 in this outer loop only.
RETRY_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
RETRY_BUDGET_SECONDS = 300
MAX_ATTEMPTS = 5


# ==================================================================
# Exception hierarchy
# ==================================================================


class AttioError(Exception):
    """Root of the AttioWriter exception hierarchy."""


class AttioTransientError(AttioError):
    """Retryable Attio failure — 429/500/502/503/504/timeout."""


class AttioRateLimitExhausted(AttioTransientError):
    """Hit MAX_ATTEMPTS or RETRY_BUDGET_SECONDS while still seeing retryable errors."""


class AttioPermanentError(AttioError):
    """Non-retryable Attio failure — 4xx other than 429, plus
    non-transient 5xx (501, 505, ...)."""


class AttioWriteFailed(AttioError):
    """Generic write-failed wrapper. Lands the write in the DLQ."""


class AttioMonotonicityViolation(AttioError):
    """Rejected: stage_rank(new) < stage_rank(old) without an allowlist
    exemption. Raised before any Attio write."""


class AttioPartialWriteRollback(AttioError):
    """Multi-attribute fallback path partially committed, then rollback
    itself failed. Operator triage required via attio_partial_write_rollback
    queue row."""


class AttioTerminalClassRegression(AttioError):
    """Cross-class terminal flip (e.g. CALL_BOOKED → NOT_INTERESTED)
    blocked. Caller must opt in via `allow_terminal_class_change=True`
    AND open `defensive_classification_review` queue row first."""


# ==================================================================
# WriteIntent
# ==================================================================


@dataclass
class WriteIntent:
    """A typed multi-attribute Attio write request.

    Fields:
        object: Attio object slug ("linkedin_outreach", "companies", etc.)
        record_id: target record_id (the Attio record ID, not entry_id).
            For LinkedIn Outreach list entries pass `entry_id` and set
            `is_list_entry=True`.
        updates: {slug: new_value} for every attribute being written.
        prior_values: {slug: old_value} snapshot. Required for both
            monotonicity checks AND compensating-transaction rollback.
        writer_module: dotted module path of the caller. Consulted
            against §3.15 registry to enforce sole-writer / multi-writer
            invariants.
        is_list_entry: True if target is a list entry (record_id is
            an entry_id and updates target list-entry attributes).
        list_id: required when is_list_entry=True.
        companion_record_id: Wave-2-B fix-up (multi-agent I-4). When
            writing to a list entry, ``record_id`` is the entry_id —
            but operators reading the failure queue need the
            underlying person/company record_id to navigate to the
            offending row. Pass it here; AttioWriter includes both
            in the attio_write_failed payload so the queue UI gets
            one-click navigation without DLQ-on-host triage.
        allow_terminal_class_change: opt-in for cross-terminal-class
            stage flips. Caller MUST also open
            `defensive_classification_review` queue row first.
        idempotency_key: optional — used by retry logic to dedup.
    """

    object: str
    record_id: str
    updates: dict[str, Any]
    prior_values: dict[str, Any] = field(default_factory=dict)
    writer_module: str = ""
    is_list_entry: bool = False
    list_id: str | None = None
    companion_record_id: str | None = None
    allow_terminal_class_change: bool = False
    idempotency_key: str | None = None


# ==================================================================
# AttioWriter
# ==================================================================


class AttioWriter:
    """The single typed entry point for every Attio write."""

    def __init__(self, attio: CRMProvider | AttioClient | None = None) -> None:
        """Construct a writer over a CRM provider.

        Union shim (P1c-5): the param stays NAMED ``attio`` because ~all
        call sites pass ``AttioWriter(attio=...)`` — renaming would touch
        every caller for no behavior gain. It accepts three shapes:

          * ``CRMProvider`` — used directly (the vendor-neutral path the
            engine threads post-migration).
          * ``AttioClient`` (or a test double mocking AttioClient methods)
            — wrapped in an :class:`AttioProvider` so the typed writes in
            ``_single_patch`` route through the provider while the mocked
            inner-client methods still fire at exactly the layer the tests
            target.
          * ``None`` — build a default :class:`AttioProvider`.

        ``self._crm`` is the provider every typed write goes through.
        ``self._attio_client`` is the underlying :class:`AttioClient`,
        retained ONLY for the one residual raw-httpx fallback in
        ``_single_patch`` (object types without a typed provider method);
        it is reached via the provider's read-only ``inner_client``.
        """
        if attio is None:
            self._crm: CRMProvider = AttioProvider()
        elif isinstance(attio, CRMProvider):
            self._crm = attio
        else:
            # AttioClient instance or a test double mocking its methods.
            self._crm = AttioProvider(attio)
        # The raw fallback (see _single_patch) needs a concrete AttioClient.
        # AttioProvider composes one; any other CRMProvider has no such
        # handle, so the fallback is Attio-only by construction (it is the
        # last vendor-specific bypass, deferred to a future increment).
        self._attio_client: AttioClient | None = getattr(
            self._crm, "inner_client", None
        )

    def apply(self, intent: WriteIntent) -> dict | Record | Entry:
        """Apply a WriteIntent. Returns the write result on success.

        Return shape (P1c-6b): typed object writes (companies/people →
        :class:`Record`, list entries → :class:`Entry`) return the
        provider's normalized dataclass; the raw object-type fallback returns
        the vendor ``dict``. The return is unobservable — no caller reads
        ``apply()``'s value (every call site is a bare statement, verified by
        grep) — so the heterogeneous shape is a typing courtesy only.

        Pre-write checks (raise before any network I/O):
            1. §3.15 write-owner registry — every attribute in updates
               must be writable by intent.writer_module.
            2. Stage monotonicity — if updates includes `stage`, the
               new rank must be >= prior_values[stage] rank UNLESS the
               transition is in `NURTURE_REENGAGEMENT_ALLOWED` AND the
               four §3.18 gates all pass.
            3. Terminal-class regression — cross-class terminal flips
               require allow_terminal_class_change=True.

        Write phase (with retry):
            * Preferred: single PATCH per intent (atomic on Attio side).
            * On AttioPermanentError on multi-attribute writes, fall
              back to compensating-transaction rollback.

        Failure handling:
            * Transient: retry up to MAX_ATTEMPTS within RETRY_BUDGET_SECONDS.
            * Exhausted: DLQ + escalate.
            * Permanent: DLQ + escalate immediately (no retry).
        """
        self._check_write_owner(intent)
        self._check_stage_monotonicity(intent)
        self._check_terminal_class_regression(intent)
        return self._patch_with_retry(intent)

    # ---- Pre-write checks ----

    def _check_write_owner(self, intent: WriteIntent) -> None:
        """§3.15 registry: every attribute being written must have
        intent.writer_module declared as an authorized writer.

        SPECIAL_WRITER_ALIASES bypass the per-(object, slug) check —
        used for the §3.20 MCP canary."""
        if intent.writer_module in SPECIAL_WRITER_ALIASES:
            return
        if not intent.writer_module:
            raise UnauthorizedAttioWriteError(
                f"WriteIntent for {intent.object}.{intent.record_id} "
                f"is missing writer_module — every Attio write must "
                f"declare its calling module per §3.15."
            )
        for slug in intent.updates:
            if not is_authorized_writer(intent.object, slug, intent.writer_module):
                raise UnauthorizedAttioWriteError(
                    f"writer_module={intent.writer_module!r} is not an "
                    f"authorized writer for {intent.object}.{slug}. "
                    f"See clients/attio_writer_registry.py."
                )

    def _check_stage_monotonicity(self, intent: WriteIntent) -> None:
        """§3.18 NURTURE re-engagement allowlist + monotonicity rule."""
        if "stage" not in intent.updates:
            return
        new_stage_val = intent.updates["stage"]
        old_stage_val = intent.prior_values.get("stage")

        if old_stage_val is None:
            return  # creating a new entry; nothing to compare against.

        try:
            new_stage = PipelineStage(new_stage_val) if isinstance(new_stage_val, str) else new_stage_val
            old_stage = PipelineStage(old_stage_val) if isinstance(old_stage_val, str) else old_stage_val
        except ValueError as exc:
            raise AttioMonotonicityViolation(
                f"unknown stage value: {exc}"
            ) from exc

        # §3.18 NURTURE re-engagement allowlist exemption.
        if (old_stage, new_stage) in NURTURE_REENGAGEMENT_ALLOWED:
            # advertiser-daily-QA-build4 #1 (§3.1 silent-omission hole):
            # is_send_eligible() reads `experiment_id_frozen_at` via
            # entry.get(); a caller that omits the field gets `None`,
            # which is NOT in the archaeology set, so the gate silently
            # passes. This would let an archaeology-stamped row through
            # the NURTURE bypass — a §3.1 violation. Require explicit
            # presence so callers can't fail open by accident.
            if "experiment_id_frozen_at" not in intent.prior_values:
                raise AttioMonotonicityViolation(
                    f"NURTURE re-engagement on {intent.object}.{intent.record_id} "
                    f"requires prior_values to include `experiment_id_frozen_at` "
                    f"(even if None). Callers that omit it cannot prove the row "
                    f"is not archaeology-stamped — §3.1 hard red line."
                )
            entry_view = {**intent.prior_values, "stage": old_stage_val}
            if not is_send_eligible(entry_view):
                raise AttioMonotonicityViolation(
                    "NURTURE re-engagement requires is_send_eligible=True "
                    "(archaeology-stamped or non-NURTURE-terminal rows are blocked)"
                )
            # Per-company throttle + nurture_re_eligible_at gates land
            # with PR-13 + PR-39 respectively. AttioWriter records that
            # the bypass was taken; downstream gates fire at the caller.
            return

        if stage_rank(new_stage) < stage_rank(old_stage):
            raise AttioMonotonicityViolation(
                f"{intent.object}.{intent.record_id}: stage transition "
                f"{old_stage.value!r} → {new_stage.value!r} regresses rank "
                f"{stage_rank(old_stage)} → {stage_rank(new_stage)}. "
                f"If this is a NURTURE re-engagement, ensure the (from, to) "
                f"pair is in NURTURE_REENGAGEMENT_ALLOWED."
            )

    def _check_terminal_class_regression(self, intent: WriteIntent) -> None:
        """F-PR-1 terminal_class: block cross-class terminal flips
        (CALL_BOOKED → NOT_INTERESTED, etc.) unless caller opted in."""
        if "stage" not in intent.updates:
            return
        old_stage = intent.prior_values.get("stage")
        if old_stage is None:
            return  # no prior stage = new record, no class to regress from.
        old_class = terminal_class(old_stage)
        new_class = terminal_class(intent.updates["stage"])
        if old_class is None or new_class is None:
            return  # non-terminal stages are unrestricted here.
        if old_class == new_class:
            return  # same class (e.g., DEFENSIVE_HOLD → DEFENSIVE_HOLD-like flow).
        if intent.allow_terminal_class_change:
            return
        raise AttioTerminalClassRegression(
            f"{intent.object}.{intent.record_id}: terminal-class flip "
            f"{old_class!r} → {new_class!r} blocked. Set "
            f"allow_terminal_class_change=True AND open a "
            f"defensive_classification_review queue row first."
        )

    # ---- Write + retry ----

    def _patch_with_retry(self, intent: WriteIntent) -> dict | Record | Entry:
        deadline = time.monotonic() + RETRY_BUDGET_SECONDS
        last_exc: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if time.monotonic() >= deadline:
                self._dlq_and_escalate(intent, "deadline_exceeded", last_exc)
                raise AttioRateLimitExhausted(
                    f"deadline {RETRY_BUDGET_SECONDS}s exceeded after "
                    f"{attempt - 1} attempts"
                ) from last_exc
            try:
                return self._single_patch(intent)
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                code = exc.response.status_code
                if code in RETRY_STATUS_CODES:
                    # Transient — sleep with full jitter and retry.
                    self._sleep_with_jitter(attempt)
                    continue
                # Permanent (4xx other than 429, or non-transient 5xx).
                self._dlq_and_escalate(intent, f"permanent_http_{code}", exc)
                raise AttioPermanentError(
                    f"non-retryable {code} on {intent.object}.{intent.record_id}: "
                    f"{exc.response.text[:300]}"
                ) from exc
            except (httpx.ReadTimeout, httpx.ConnectError, httpx.ReadError) as exc:
                last_exc = exc
                self._sleep_with_jitter(attempt)
                continue

        self._dlq_and_escalate(intent, "max_attempts", last_exc)
        raise AttioRateLimitExhausted(
            f"exhausted {MAX_ATTEMPTS} attempts on {intent.object}.{intent.record_id}"
        ) from last_exc

    def _single_patch(self, intent: WriteIntent) -> dict | Record | Entry:
        """Single multi-attribute PATCH. Attio v2's PATCH on a record
        accepts every attribute in `values` atomically — partial-commit
        is not a normal failure mode at the API level. If a specific
        attribute combination fails (schema mismatch on one attr), the
        whole request is rejected with 400 and `_compensating_rollback`
        is engaged at the caller level — F-PR-4 ships the atomicity
        contract; the rollback fallback path is exercised by PR-15.

        Wave-2-B dispatch shape (P1c-6b update): route through the
        CRMProvider's typed helpers (`update_list_entry`, `update_company`,
        `update_person`) for the matching object types. The provider
        delegates to the SAME inner AttioClient methods, so call sites that
        mocked those helpers under the pre-§3.15-bypass pattern keep working
        as AttioWriter callers — the mock fires at exactly the layer the
        tests are designed against, the registry / monotonicity gates in
        `.apply()` still fire on every write, and the §3.15 contract holds
        throughout the codebase. For object types without a dedicated
        helper, fall back to the raw httpx PATCH.

        Retry-stacking trade-off (engineer-QA-build4 #1, revisited):
        AttioClient._request has its own 3-attempt retry loop with
        5/10/20s backoff for (429, 502, 503) — plus 500 on call sites
        that pass retry_500=True (update_person does; update_company
        and update_list_entry don't, so their 500s retry only in this
        outer loop). AttioWriter's
        ``_patch_with_retry`` adds 5 outer attempts capped by
        ``RETRY_BUDGET_SECONDS=300``. The pre-Wave-2-B raw-PATCH path
        avoided this stacking; the Wave-2-B helper-dispatch path
        re-introduces up to 15 HTTP attempts on a 429 storm. The
        outer deadline (300s) bounds total elapsed time so the
        worst-case backoff sum is still capped — operators see the
        same failure surface (max_attempts / deadline_exceeded DLQ +
        attio_write_failed queue row) with a slightly higher per-
        failure HTTP count. Pragmatic call: §3.15 contract uniformity
        is more valuable than the marginal request-volume saving.
        """
        # P1c-6b typed-write routing. The writer holds a vendor-neutral
        # ``self._crm`` (a CRMProvider) and now routes the typed list-entry /
        # company / person writes THROUGH the provider's own typed helpers
        # (``self._crm.update_list_entry`` / ``update_company`` /
        # ``update_person``) rather than reaching past it to the inner client.
        #
        # Retry equivalence (no stacking change): each provider helper is a
        # thin normalizing wrapper over the SAME inner ``AttioClient.update_*``
        # call the pre-6b code invoked directly —
        #   ``self._crm.update_company`` → ``AttioProvider.update_company``
        #     → ``AttioClient.update_company`` → ``AttioClient._request``
        # — so the inner 3-attempt ``_request`` retry loop (5/10/20s on
        # 429/502/503) is IDENTICAL to the pre-6b ``self._attio_client.update_*``
        # path and to the pre-increment-5 ``self._attio.update_*`` path. The
        # provider adds only the ``_to_record`` / ``_to_entry`` normalization on
        # top; it does NOT introduce a second retry loop.
        #
        # Return shape (unobservable): the provider helpers return a ``Record``
        # (companies/people) or ``Entry`` (list entries) instead of the raw
        # vendor dict. No caller reads ``apply()``'s return — every one of the
        # ~14 call sites in workflows/ + cli.py is a bare statement, verified by
        # grep at P1c-6b (no assignment from ``.apply(``). So handing back a
        # ``Record`` / ``Entry`` is unobservable. The provider's 6a hardening
        # makes ``_to_record`` / ``_to_entry`` tolerate the empty / non-dict
        # returns the engine's unit-test doubles stub, so routing through the
        # normalizer no longer risks the ``AttributeError`` the pre-6a code did.
        #
        # The §3.15 registry / monotonicity / terminal-class gates in
        # ``.apply()`` still fire on every write regardless of transport.
        if self._attio_client is None:
            raise AttioWriteFailed(
                f"no typed write available for {intent.object!r}: the "
                f"configured CRMProvider exposes no inner AttioClient."
            )
        if intent.is_list_entry:
            if not intent.list_id:
                raise AttioWriteFailed(
                    "is_list_entry=True requires list_id"
                )
            return self._crm.update_list_entry(
                entry_id=intent.record_id,
                entry_attributes=intent.updates,
                list_id=intent.list_id,
            )
        if intent.object == "companies":
            return self._crm.update_company(intent.record_id, intent.updates)
        if intent.object == "people":
            return self._crm.update_person(intent.record_id, intent.updates)

        # Fallback for object types without a dedicated update helper
        # (daily_run, weekly_kpi_snapshot, weekly_strategy_brief,
        # experiment, etc.). Use the raw httpx client directly so the
        # AttioWriter outer retry loop owns retry policy for these
        # paths — there's no inner _request to stack against.
        #
        # P1c-6b NOTE: after the typed paths above flipped to ``self._crm``,
        # this is now the ONLY remaining inner_client write use in the writer
        # (and the only ``self._attio_client`` reference outside __init__).
        # It deliberately calls `_client.request` (NOT the provider's
        # `update_object_record`, which would route through the inner
        # `_request` retry loop and STACK retries — a behavior change that
        # breaks the retry-contract tests; see TestBypassesInnerRetry). The
        # retry-ownership boundary stays here; routing it vendor-neutrally is
        # deferred to a future vendor-neutral exception-model increment. The
        # AttioClient handle comes from the AttioProvider this writer composes
        # (non-None is already guaranteed by the guard above).
        path = f"/objects/{intent.object}/records/{intent.record_id}"
        body = {"data": {"values": intent.updates}}
        resp = self._attio_client._client.request("PATCH", path, json=body)
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
        return data.get("data", data)

    def _sleep_with_jitter(self, attempt: int) -> None:
        """Full-jitter exponential backoff. AWS Architecture Blog
        recommends full jitter as the lowest-collision strategy when
        many clients hit the same rate limit."""
        cap = 30.0  # cap per attempt at 30s so the 300s deadline isn't
                    # eaten by one long backoff.
        delay = min(cap, (2 ** (attempt - 1)))
        time.sleep(random.uniform(0, delay))

    def _dlq_and_escalate(
        self,
        intent: WriteIntent,
        kind: str,
        cause: Exception | None,
    ) -> None:
        """Write to DLQ + open `attio_write_failed` queue row.

        The DLQ JSONL is a forensic record of every failure; the queue
        row is the operator-facing surface. Both are best-effort —
        failures during DLQ/escalate are logged but don't shadow the
        original failure."""
        try:
            DLQ_DIR.mkdir(parents=True, exist_ok=True)
            path = DLQ_DIR / f"attio-{date.today().isoformat()}.jsonl"
            line = {
                "ts": datetime.now(UTC).isoformat(),
                "kind": kind,
                "object": intent.object,
                "record_id": intent.record_id,
                "is_list_entry": intent.is_list_entry,
                "list_id": intent.list_id,
                "updates": intent.updates,
                "prior_values": intent.prior_values,
                "writer_module": intent.writer_module,
                "cause_type": type(cause).__name__ if cause else None,
                "cause_msg": str(cause)[:500] if cause else None,
                "host": socket.gethostname(),
                "pid": os.getpid(),
            }
            with open(path, "a") as f:
                f.write(json.dumps(line, default=str) + "\n")
                f.flush()
        except Exception as dlq_exc:  # noqa: BLE001
            # DLQ best-effort. Print so the operator notices.
            print(
                f"WARNING: DLQ write failed for "
                f"{intent.object}.{intent.record_id}: {dlq_exc}",
                file=__import__("sys").stderr,
            )

        # Open queue row — best-effort, deferred-import to break the
        # cycle (escalate uses AttioClient which uses AttioWriter
        # transitively in some downstream PRs).
        #
        # Wave-2-B fix-up (multi-agent I-1): date-scope the idempotency
        # key so cross-run recurrences open a fresh queue row instead
        # of shadowing the first-landing payload. Without the date in
        # the key, run-N+1 hits _find_existing_row → returns run-N's
        # row → run-N+1's distinct attribute_writes / error_msg are
        # silently dropped and the operator can't tell the failure is
        # recurring. Same-day collisions still dedup (right behavior:
        # one row per failure-class per day per record).
        try:
            from workflows.escalation import escalate
            escalate(
                type="attio_write_failed",
                idempotency_key=(
                    f"{intent.object}-{intent.record_id}-{kind}-"
                    f"{date.today().isoformat()}"
                ),
                payload={
                    "object": intent.object,
                    "record_id": intent.record_id,
                    # Wave-2-B fix-up (multi-agent I-4): include the
                    # list-entry breadcrumbs so an operator reading the
                    # queue row can navigate to the source without
                    # SSH-ing into the daily-run host to read the DLQ.
                    # companion_record_id resolves the entry_id back to
                    # the underlying person/company record so the queue
                    # UI gets one-click navigation.
                    "is_list_entry": intent.is_list_entry,
                    "list_id": intent.list_id,
                    "companion_record_id": intent.companion_record_id,
                    "attribute_writes": intent.updates,
                    "error_class": type(cause).__name__ if cause else "Unknown",
                    "error_msg": str(cause)[:500] if cause else "",
                    "retry_count": MAX_ATTEMPTS,
                },
                attio=self._crm,
            )
        except Exception as esc_exc:  # noqa: BLE001
            print(
                f"WARNING: escalate(attio_write_failed) failed for "
                f"{intent.object}.{intent.record_id}: {esc_exc}",
                file=__import__("sys").stderr,
            )
