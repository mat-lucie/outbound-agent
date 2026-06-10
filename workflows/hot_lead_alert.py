"""Hot-lead alert emit (PR-18 B-SD-002 + B-SD-003 + B-SD-012).

When a prospect's reply gets classified as ``positive`` (or
``manual_unclassified`` with prospect_score >= 70 — PR-20's path), an
operator must be alerted ASAP so the lead doesn't decay sitting in the
queue. ``emit_hot_lead`` is the canonical entry point.

# Channel ordering (load-bearing — §3.8 agent-first / pipeline-leakage red line)

1. **Operator Review Queue row FIRST (synchronous, durable, MUST
   succeed):** open ``hot_lead_positive_reply`` queue row with payload
   ``{record_id, response_classification, prospect_score,
   message_excerpt, thread_url, fallback_used: bool}``. If the queue
   write fails, ``emit_hot_lead`` raises and the run halts — operator
   visibility is the durability guarantee.

2. **Resend HTML email SECOND (asynchronous best-effort):** sent AFTER
   the queue write returns success. If Resend errors, set
   ``fallback_used=True`` on the queue row (via a follow-up PATCH) and
   open a secondary ``resend_delivery_failed`` escalation. The operator
   already has the queue row; the email is a convenience nudge.

Rationale (per plan §5 Wave 1C PR-18 scope): Resend-first design loses
the lead if Resend is down; queue-first guarantees durability via Attio
per §3.8 agent-first ("state lives where agents query it; humans are
escalation targets, not integrators").

# Defensive routing split (B-SD-003)

Defensive replies route to the ``DEFENSIVE_HOLD`` stage (F-PR-1) rather
than ``RESPONDED``. They are NOT hot leads — the prospect signaled
reactance, not interest. ``emit_hot_lead`` MUST NOT be called for
defensive classifications; the routing happens in
``workflows.detect_responses`` (the caller's responsibility).
"""

from __future__ import annotations

import contextlib
import html
import os
from typing import TYPE_CHECKING

import httpx

from workflows.escalation import escalate

if TYPE_CHECKING:
    from clients.attio import AttioClient
    from clients.resend_client import ResendClient


# Trigger threshold for hot-lead emit on `manual_unclassified` replies:
# operator-graded uncertainty fires only when prospect quality is high
# enough that the missed positive cost outweighs the false-alarm cost.
# PR-20 lands the manual_unclassified write path; PR-18 codifies the
# gate so PR-20's emit call has a known threshold.
HOT_LEAD_MANUAL_UNCLASSIFIED_SCORE_FLOOR = 70

# Configurable via env so demos/tests can redirect alerts.
_HOT_LEAD_TO_ADDRESS = (
    os.environ.get("OUTBOUND_HOT_LEAD_EMAIL")
    or "ops@example.com"
)


class HotLeadEmitFailed(RuntimeError):
    """Raised when the queue-row write fails — operator visibility is
    the durability guarantee, so the run halts rather than silently
    moving on with a hot lead nobody sees."""


def should_emit_hot_lead(
    response_classification: str,
    prospect_score: int | None,
) -> bool:
    """Return True iff the reply qualifies for hot-lead alert.

    PR-18 B-SD-002 trigger conditions:
    - ``response_classification == 'positive'`` (always)
    - ``response_classification == 'manual_unclassified'`` AND
      ``prospect_score >= HOT_LEAD_MANUAL_UNCLASSIFIED_SCORE_FLOOR``
      (PR-20's path; PR-18 codifies the gate for PR-20 to call).

    Defensive replies route to ``DEFENSIVE_HOLD`` instead — the caller
    must NOT call this with ``response_classification='defensive'``.
    """
    if response_classification == "positive":
        return True
    if response_classification == "manual_unclassified":
        score = prospect_score if prospect_score is not None else 0
        return score >= HOT_LEAD_MANUAL_UNCLASSIFIED_SCORE_FLOOR
    return False


def emit_hot_lead(
    *,
    record_id: str,
    response_classification: str,
    prospect_score: int | None,
    message_excerpt: str,
    thread_url: str,
    attio: AttioClient,
    resend: ResendClient | None = None,
    prospect_name: str | None = None,
    prospect_company: str | None = None,
) -> dict:
    """Open the hot-lead queue row, then attempt Resend (best-effort).

    The queue write is synchronous and load-bearing — it MUST succeed
    or this function raises ``HotLeadEmitFailed`` and the daily run
    halts. The Resend email is asynchronous best-effort: if it fails,
    the queue row gets a follow-up ``fallback_used=True`` patch and a
    secondary ``resend_delivery_failed`` escalation is opened, but the
    primary signal (queue row) is already durable.

    Returns the queue-row payload as written for caller-side logging.
    Idempotency: queue row uniqueness on ``(type='hot_lead_positive_reply',
    idempotency_key=record_id)`` — re-emit for the same prospect is a
    no-op refresh, not a duplicate alert.
    """
    # ─── Step 1: queue row (synchronous, durable, MUST succeed) ─────
    payload = {
        "record_id": record_id,
        "response_classification": response_classification,
        "prospect_score": prospect_score if prospect_score is not None else 0,
        "message_excerpt": message_excerpt[:500],  # truncate for queue UI
        "thread_url": thread_url,
        "fallback_used": False,
    }
    try:
        escalate(
            type="hot_lead_positive_reply",
            idempotency_key=record_id,
            payload=payload,
            attio=attio,
        )
    except (
        httpx.HTTPStatusError,
        httpx.RequestError,
        httpx.TimeoutException,
    ) as exc:
        # Network/transport failure on the queue write. The queue row IS
        # the durability guarantee — without it the operator has no
        # signal that this hot lead exists. Cannot proceed.
        # PR-18 fold-in (silent-failure-hunter Finding 4): narrowed from
        # ``except Exception`` so schema bugs (UnknownEscalationType,
        # MissingDecisionKey, EscalationSchemaError) propagate raw
        # instead of being rebranded as HotLeadEmitFailed.
        raise HotLeadEmitFailed(
            f"Failed to open hot_lead_positive_reply queue row for "
            f"record_id={record_id!r}: {type(exc).__name__}: {exc}. "
            f"Cannot proceed — operator must see this lead via the queue."
        ) from exc

    # ─── Step 2: Resend HTML email (async best-effort) ───────────────
    if resend is None:
        # Caller didn't supply a Resend client (test path or operator
        # explicitly disabled email). Queue row alone is sufficient.
        return payload

    subject_label = response_classification.upper()
    name_label = prospect_name or "unknown"
    company_label = prospect_company or "unknown company"
    # Truncation widths (PR-18 fold-in — code-reviewer Finding 6):
    #   500 = queue UI single-line preview budget
    #  1000 = email blockquote readable-height budget
    #   200 = error column fit on the resend_delivery_failed queue row
    html_body = (
        f"<h2>🔥 Hot lead — {subject_label} reply</h2>"
        f"<p><strong>{html.escape(name_label)}</strong> at "
        f"<strong>{html.escape(company_label)}</strong> "
        f"(score: {prospect_score if prospect_score is not None else 'unknown'})</p>"
        f"<blockquote>{html.escape(message_excerpt[:1000])}</blockquote>"
        f"<p><a href=\"{html.escape(thread_url, quote=True)}\">Open conversation</a></p>"
        f"<p style=\"color:#888;font-size:0.9em\">Sent by Outbound Agent "
        f"(PR-18). The Operator Review Queue row is the durable record — "
        f"this email is a convenience nudge.</p>"
    )
    try:
        resend.send_email(
            to=_HOT_LEAD_TO_ADDRESS,
            subject=f"🔥 Hot lead [{subject_label}]: {name_label} @ {company_label}",
            html=html_body,
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as resend_exc:
        # PR-18 fold-in (silent-failure-hunter Findings 1+2+6 +
        # code-reviewer Finding 1): the prior PATCH-to-mutate-payload_json
        # path could silently overwrite the queue row's rich payload with
        # a 1-key minimal dict on Attio read failure. Dropped entirely.
        # The secondary ``resend_delivery_failed`` escalation IS the
        # durable record of the Resend failure — operators correlate the
        # two rows via the ``hot_lead|{record_id}`` namespace prefix.
        # ``payload["fallback_used"] = True`` mutates the in-memory dict
        # returned to the caller (used for test assertions); we no
        # longer attempt to round-trip it through Attio.
        # PR-18 fold-in (silent-failure-hunter Finding 3): removed
        # ``KeyError`` from this except tuple — no traceable code path
        # raises it from ``resend.send_email``.
        payload["fallback_used"] = True
        # Secondary escalation is best-effort; the primary queue row
        # already exists. Suppress only network errors here — schema
        # bugs propagate as before. The ``hot_lead|`` prefix on the
        # idempotency_key prevents collision with PR-30's weekly-report
        # Resend failures (which key on ``f"weekly_report|{week_starting}"``).
        with contextlib.suppress(
            httpx.HTTPStatusError, httpx.RequestError, httpx.TimeoutException
        ):
            escalate(
                type="resend_delivery_failed",
                idempotency_key=f"hot_lead|{record_id}",
                payload={
                    "recipient_email": _HOT_LEAD_TO_ADDRESS,
                    "send_attempt_at": _now_iso(),
                    "resend_error_code": f"{type(resend_exc).__name__}: {resend_exc}"[:200],
                    "kpi_snapshot_week_starting": "",  # not applicable to hot-lead path
                },
                attio=attio,
            )

    return payload


def _now_iso() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat()
