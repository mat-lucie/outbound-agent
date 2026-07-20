"""Automated response detection via PhantomBuster SN Inbox Scraper.

The SN Inbox Scraper returns Sales Navigator URLs (linkedin.com/sales/people/...)
which don't match the vanity URLs stored in Attio (linkedin.com/in/...).
Matching is done by participant name instead.
"""

import csv
import io
import os
import unicodedata
from datetime import date
from typing import TYPE_CHECKING

import click
import httpx

from clients.attio import AttioClient
from clients.phantombuster import PhantomBusterClient

if TYPE_CHECKING:
    from clients.resend_client import ResendClient
    from workflows.daily_run import DailyRun
from models.campaign import (
    Language,
    MessageStep,
    MissingMessageError,
    Persona,
    get_message,
    load_messages,
    personalize,
)
from models.pipeline import STAGE_RANK, PipelineStage
from workflows.daily_check_helpers import _pb_session_args
from workflows.escalation import escalate
from workflows.hot_lead_alert import (
    HotLeadEmitFailed,
    emit_hot_lead,
    should_emit_hot_lead,
)
from workflows.record_cache import RecordCache
from workflows.response_classifier import classify_reply_llm

MAX_RESPONSE_TEXT_LEN = 1000

# Number of messages we expect to have sent in a thread for a prospect at
# each DM stage. Used with `totalMessageCount` from SN Inbox Scraper to
# detect manual replies: when `isLastMessageFromMe=true` and the thread has
# more messages than we'd expect, the prospect must have replied AND we
# replied back manually.
EXPECTED_OUR_MESSAGES: dict[str, int] = {
    PipelineStage.DM1_SENT.value: 1,
    PipelineStage.DM2_SENT.value: 2,
    PipelineStage.DM3_SENT.value: 3,
}

# PR-20 B-SD-008: false-positive guard for connection-acceptance notes.
# When LinkedIn surfaces a connection-acceptance note from the prospect
# (the auto-message that fires when they accept our invite), it looks
# like a real reply but isn't an actionable signal. Heuristic:
#   - prospect has dm_step == 0 (we've never sent a DM)
#   - thread has total_messages == 1 (only the acceptance note)
#   - the message body is short (< _CONNECTION_NOTE_MAX_CHARS)
# When all three hold, set ``had_connection_note=True`` on the entry
# and SKIP classification. Operators see the flag in Attio; the
# prospect remains at ACCEPTED for the regular DM1 cadence.
#
# Detection isn't perfect — a prospect who replies with a one-line
# "thanks!" right after accepting could be misclassified. The
# alternative (classify everything) over-fires hot-lead alerts on
# pure acceptances. The MAX_CHARS threshold is tunable; documented
# here so a future PR can refine with LinkedIn API metadata if PB
# adds the signal.
_CONNECTION_NOTE_MAX_CHARS = 100


def _looks_like_connection_note(
    *, dm_step: int, total_messages: int, last_body: str
) -> bool:
    """PR-20 B-SD-008 heuristic — see ``_CONNECTION_NOTE_MAX_CHARS`` doc.

    Three signals must coincide:
    1. ``dm_step == 0`` — we've never sent a DM, so the prospect's
       only inbox message can only be the acceptance note.
    2. ``total_messages == 1`` — single message in the thread.
    3. ``len(last_body) < _CONNECTION_NOTE_MAX_CHARS`` — connection
       notes are typically short generic acknowledgements.

    All three together is the conservative signal — fires only when
    we're confident this is the LinkedIn auto-note, not a real reply.
    """
    return (
        dm_step == 0
        and total_messages == 1
        and 0 < len(last_body) < _CONNECTION_NOTE_MAX_CHARS
    )


def _empty_counts() -> dict:
    """Canonical zeroed counts dict. Shape is part of the public contract —
    downstream callers (daily CLI, tests) rely on these keys always being
    present even when no prospects are processed.
    """
    return {
        "detected": 0,
        "positive": 0,
        "negative": 0,
        "question": 0,
        "neutral": 0,
        "defensive": 0,
        "classifier_llm": 0,
        "classifier_keyword": 0,
        "attio_update_failed": 0,
        "drift_detected": 0,
        "drift_auto_repaired": 0,
        "drift_repair_failed": 0,
    }


def _mark_reply_detection_ok(daily_run: "DailyRun | None") -> None:
    """Record reply_detection_status='ok' on a CLEAN response-detection
    completion.

    'ok' means 'ran cleanly' (incl. zero prospects to check); None means
    'never ran'. send-dms's cross-process fail-closed guard (!= 'ok')
    depends on this distinction. Write failure is non-fatal (worst case:
    Part B held back, not let through) — log loudly, don't raise.
    """
    if daily_run is None:
        return
    try:
        daily_run.set_reply_detection_status("ok")
    except (httpx.HTTPStatusError, httpx.RequestError) as patch_exc:
        click.echo(
            f"  ⚠ daily_run.reply_detection_status='ok' write failed "
            f"({type(patch_exc).__name__}: {patch_exc}). Part-B may not "
            f"proceed on the next run; re-run when the CRM is healthy.",
            err=True,
        )


# Stage that corresponds to each dm_step when last message is from us.
# Mirrors the standalone reconstruct_cadence script — keep in sync.
_STAGE_FOR_DM_STEP: dict[int, str] = {
    0: PipelineStage.ACCEPTED.value,
    1: PipelineStage.DM1_SENT.value,
    2: PipelineStage.DM2_SENT.value,
    3: PipelineStage.DM3_SENT.value,
}


def _infer_stage_from_thread(total: int, is_from_me: bool) -> str:
    """Infer the correct pipeline stage from a LinkedIn thread snapshot.

    - n=0: ACCEPTED (no DM activity yet)
    - last msg from prospect: RESPONDED (terminal, manual review)
    - last msg from us: DMn Sent (dm_step = min(n, 3))
    """
    if total == 0:
        return PipelineStage.ACCEPTED.value
    if not is_from_me:
        return PipelineStage.RESPONDED.value
    return _STAGE_FOR_DM_STEP.get(min(total, 3), PipelineStage.DM3_SENT.value)


# Number of inbox threads to scrape. Sized to cover the full SN conversation
# history (typical the operator workload: ~150). Used both for response detection AND
# the cadence drift check below — single scrape, dual purpose.
INBOX_SCRAPE_LIMIT = 200

# Drift detection uses the canonical STAGE_RANK from models.pipeline (F-PR-1).
# Terminal stages are pre-filtered via _TERMINAL_STAGES_FOR_DRIFT below before
# we reach STAGE_RANK lookups, so terminal ranks (90/95/99/101/200) never
# appear in the drift comparison — only non-terminal funnel ranks (0–6) do.


_TERMINAL_STAGES_FOR_DRIFT = {
    PipelineStage.RESPONDED.value,
    PipelineStage.DEFENSIVE_HOLD.value,
    PipelineStage.NOT_INTERESTED.value,
    PipelineStage.CALL_BOOKED.value,
    PipelineStage.QUALIFIED.value,
    # Wave-2-A: UNREACHABLE is terminal (undeliverable). Shield it from the
    # drift detector like every other terminal — otherwise an UNREACHABLE row
    # whose name matches a scraped thread would emit spurious drift and could
    # be auto-repaired (un-parked) back toward an active stage.
    PipelineStage.UNREACHABLE.value,
}


def _detect_cadence_drift(
    threads: list[dict],
    name_to_full_pipeline: dict[str, list[dict]],
) -> list[dict]:
    """Cross-reference inbox threads against the full pipeline to surface
    cadence drift (where Attio state disagrees with LinkedIn thread evidence).

    Returns a list of drift records. Doesn't auto-fix anything — drift repair
    is an explicit action via scripts/reconstruct_cadence_from_linkedin_*.py.
    Treating this as advisory means a transient PB scrape gap can't cause a
    silent state corruption.
    """
    drifts: list[dict] = []
    for row in threads:
        participant_name = row.get("participantFullName", "").strip()
        if not participant_name:
            continue
        try:
            total = int((row.get("totalMessageCount") or "0").strip())
        except ValueError:
            total = 0
        is_from_me = row.get("isLastMessageFromMe", "").strip().lower() == "true"

        matched = name_to_full_pipeline.get(_normalize_name(participant_name)) or []
        for attrs in matched:
            cur_stage = attrs.get("stage") or ""
            cur_step = int(attrs.get("dm_step") or 0)
            # Terminal stages: only check dm_step (stage is sticky).
            if cur_stage in _TERMINAL_STAGES_FOR_DRIFT:
                inferred_step = min(total if is_from_me else max(total - 1, 1), 3) if total > 0 else 0
                if inferred_step > cur_step:
                    drifts.append({
                        "kind": "terminal_dm_step_low",
                        "name": participant_name,
                        "company": attrs.get("company_name", ""),
                        "current_stage": cur_stage,
                        "current_dm_step": cur_step,
                        "thread_total": total,
                        "thread_last_from_me": is_from_me,
                        "inferred_dm_step": inferred_step,
                        "inferred_stage": cur_stage,  # stage stays sticky in terminal
                        "record_id": attrs.get("record_id"),
                        "entry_id": attrs.get("entry_id"),
                    })
                continue
            # Non-terminal: full state comparison.
            if total == 0:
                # No messages but Attio claims thread-bearing stage — drift.
                # cur_stage is a string from Attio; pre-filtered to non-terminal
                # by line 103, so the PipelineStage coercion only fails for
                # genuinely unknown stages (treat as rank 0).
                try:
                    cur_rank = STAGE_RANK[PipelineStage(cur_stage)]
                except (KeyError, ValueError):
                    cur_rank = 0
                if cur_step > 0 or cur_rank >= 3:
                    drifts.append({
                        "kind": "attio_overstates_thread_empty",
                        "name": participant_name,
                        "company": attrs.get("company_name", ""),
                        "current_stage": cur_stage,
                        "current_dm_step": cur_step,
                        "thread_total": 0,
                        "thread_last_from_me": False,
                        "inferred_dm_step": 0,
                        "inferred_stage": _infer_stage_from_thread(0, False),
                        "record_id": attrs.get("record_id"),
                        "entry_id": attrs.get("entry_id"),
                    })
                continue
            inferred_step = min(total if is_from_me else max(total - 1, 1), 3)
            inferred_stage = _infer_stage_from_thread(total, is_from_me)
            if inferred_step > cur_step:
                drifts.append({
                    "kind": "attio_dm_step_lower_than_thread",
                    "name": participant_name,
                    "company": attrs.get("company_name", ""),
                    "current_stage": cur_stage,
                    "current_dm_step": cur_step,
                    "thread_total": total,
                    "thread_last_from_me": is_from_me,
                    "inferred_dm_step": inferred_step,
                    "inferred_stage": inferred_stage,
                    "record_id": attrs.get("record_id"),
                    "entry_id": attrs.get("entry_id"),
                })
            elif inferred_step < cur_step:
                drifts.append({
                    "kind": "attio_dm_step_higher_than_thread",
                    "name": participant_name,
                    "company": attrs.get("company_name", ""),
                    "current_stage": cur_stage,
                    "current_dm_step": cur_step,
                    "thread_total": total,
                    "thread_last_from_me": is_from_me,
                    "inferred_dm_step": inferred_step,
                    "inferred_stage": inferred_stage,
                    "record_id": attrs.get("record_id"),
                    "entry_id": attrs.get("entry_id"),
                })
    return drifts


# Drift kinds that are safe to auto-repair (directionally monotonic: only ever
# advance cadence forward, never backward). The kinds NOT in this set
# (attio_dm_step_higher_than_thread, attio_overstates_thread_empty) remain
# advisory because a transient PB scrape gap could falsely under-report
# cadence and a regress-style auto-repair would destroy real state.
_AUTO_REPAIR_KINDS: frozenset[str] = frozenset({
    "attio_dm_step_lower_than_thread",
    "terminal_dm_step_low",
})


def _apply_cadence_repairs(
    attio: AttioClient,
    list_id: str,
    drifts: list[dict],
    today_iso: str,
) -> tuple[int, int]:
    """Auto-repair drift records whose kind is in `_AUTO_REPAIR_KINDS`.

    For each repair, updates the Attio list entry (stage + dm_step + last
    contact date) and writes an audit note on the person record. Only the
    monotonically-forward kinds are touched — never decrement `dm_step` or
    flip away from a terminal stage. Returns (applied, failed) counts.

    Skips entries missing `entry_id` defensively (shouldn't happen with the
    upstream `_detect_cadence_drift` change, but keeps this helper safe to
    call with hand-built drift lists from tests).
    """
    applied = 0
    failed = 0
    # Required keys for the auto-repair to construct the update payload.
    # If a drift dict is malformed (e.g. test scaffolding or future refactor
    # regression), skip it loudly rather than masking a KeyError as a
    # generic "failed" repair.
    _REQUIRED = ("inferred_stage", "inferred_dm_step", "current_stage",
                 "current_dm_step", "thread_total", "thread_last_from_me")
    for d in drifts:
        if d.get("kind") not in _AUTO_REPAIR_KINDS:
            continue
        entry_id = d.get("entry_id")
        record_id = d.get("record_id")
        if not entry_id or not record_id:
            continue
        missing = [k for k in _REQUIRED if k not in d]
        if missing:
            failed += 1
            click.echo(
                f"  ✗ Cadence auto-repair: malformed drift record "
                f"(missing keys {missing}) for {d.get('name', '?')}",
                err=True,
            )
            continue
        # Split state-write and audit-note writes so a partial success
        # (state-write OK, audit-note fails) doesn't get silently counted
        # as "failed" while the entry is already modified in Attio. The
        # audit note is best-effort forensics; the state-write is what
        # affects Part A/B's view of the world.
        # Wave-2-B §3.15 cleanup: route through AttioWriter so the
        # registry sees the cadence auto-repair path as an authorized
        # writer for stage/dm_step/last_contact_date. The pre-Wave-2
        # bypass let the auto-repair flip stages with zero registry
        # consultation; a future refactor that introduced a backward
        # stage transition would not have tripped any gate.
        from clients.attio_writer import (
            AttioError,
            AttioMonotonicityViolation,
            AttioTerminalClassRegression,
            AttioWriter,
            UnauthorizedAttioWriteError,
            WriteIntent,
        )
        _writer = AttioWriter(attio=attio)
        try:
            _writer.apply(WriteIntent(
                object="linkedin_outreach",
                record_id=entry_id,
                updates={
                    "stage": d["inferred_stage"],
                    "dm_step": d["inferred_dm_step"],
                    "last_contact_date": today_iso,
                },
                prior_values={"stage": d.get("current_stage")},
                writer_module="workflows.detect_responses._apply_cadence_repairs",
                is_list_entry=True,
                list_id=list_id,
                # Wave-2-B fix-up (multi-agent I-4): underlying person
                # record_id for one-click navigation from the
                # attio_write_failed queue row.
                companion_record_id=record_id,
            ))
        except UnauthorizedAttioWriteError:
            # Caller bug (registry/writer_module mismatch) — propagate.
            # This must NEVER reach production; a halt is the right
            # response so the operator files a fix.
            raise
        except AttioMonotonicityViolation as mv:
            # Wave-2-B follow-up I-2: data-drift signal (NOT a code
            # bug). The cadence auto-repair tried to flip a stage
            # backward — which the monotonicity gate correctly blocks.
            # Open a typed drift_detector_finding queue row so the
            # operator triages the underlying drift, then continue
            # the batch (one bad drift record must not halt all
            # other auto-repairs in this run).
            failed += 1
            try:  # noqa: SIM105 — best-effort escalate; intentional swallow
                escalate(
                    type="drift_detector_finding",
                    idempotency_key=(
                        f"cadence-repair-monotonicity|{entry_id}|"
                        f"{date.today().isoformat()}"
                    ),
                    payload={
                        "site": "_apply_cadence_repairs",
                        "entry_id": entry_id,
                        "record_id": record_id,
                        "current_stage": d.get("current_stage"),
                        "inferred_stage": d.get("inferred_stage"),
                        "error": str(mv),
                        "name": d.get("name", "?"),
                    },
                    attio=attio,
                )
            except Exception:  # noqa: BLE001 — best-effort escalate
                pass
            click.echo(
                f"  ⚠ Cadence auto-repair monotonicity-rejected for "
                f"{d.get('name', '?')}: {mv}. drift_detector_finding "
                f"queue row opened — operator triage required.",
                err=True,
            )
            continue
        except AttioTerminalClassRegression as tcr:
            # Same shape as AttioMonotonicityViolation but routed to
            # the defensive_classification_review queue slug (the
            # canonical surface for cross-terminal-class flips).
            failed += 1
            try:  # noqa: SIM105 — best-effort escalate; intentional swallow
                escalate(
                    type="defensive_classification_review",
                    idempotency_key=(
                        f"cadence-repair-terminal-class|{entry_id}|"
                        f"{date.today().isoformat()}"
                    ),
                    payload={
                        "site": "_apply_cadence_repairs",
                        "entry_id": entry_id,
                        "record_id": record_id,
                        "current_stage": d.get("current_stage"),
                        "inferred_stage": d.get("inferred_stage"),
                        "error": str(tcr),
                        "name": d.get("name", "?"),
                    },
                    attio=attio,
                )
            except Exception:  # noqa: BLE001 — best-effort escalate
                pass
            click.echo(
                f"  ⚠ Cadence auto-repair terminal-class-rejected for "
                f"{d.get('name', '?')}: {tcr}. defensive_classification_"
                f"review queue row opened — operator triage required.",
                err=True,
            )
            continue
        except AttioError as e:
            # AttioWriter already DLQ'd + escalated via
            # `attio_write_failed`; we just tally the failure for the
            # caller's summary line and continue.
            failed += 1
            click.echo(
                f"  ✗ Cadence auto-repair FAILED (state-write) for "
                f"{d.get('name', '?')}: {type(e).__name__}: {e}",
                err=True,
            )
            continue
        except (httpx.HTTPStatusError, httpx.RequestError,
                httpx.TimeoutException) as e:
            # Defense in depth: if a raw httpx error ever leaked
            # through AttioWriter, preserve the pre-Wave-2 fail-loud-
            # stderr behavior so the auto-repair gap is still visible.
            failed += 1
            click.echo(
                f"  ✗ Cadence auto-repair FAILED (state-write) for "
                f"{d.get('name', '?')}: {type(e).__name__}: {e}",
                err=True,
            )
            continue
        # State-write succeeded — entry is already modified in Attio.
        # Count as applied even if audit-note creation fails (we don't
        # want to flip "applied" → "failed" and trigger a re-repair on
        # next run when the state IS correct).
        applied += 1
        try:
            attio.create_note(
                record_id=record_id,
                title=f"Cadence auto-repair {today_iso}",
                content=(
                    f"Phase 0.5 reconciled Attio with LinkedIn thread state.\n\n"
                    f"Before: stage={d['current_stage']}, dm_step={d['current_dm_step']}\n"
                    f"After:  stage={d['inferred_stage']}, dm_step={d['inferred_dm_step']}\n"
                    f"Thread evidence: totalMessageCount={d['thread_total']}, "
                    f"isLastMessageFromMe={d['thread_last_from_me']}\n"
                    f"Drift kind: {d['kind']}\n"
                ),
            )
        except (httpx.HTTPStatusError, httpx.RequestError,
                httpx.TimeoutException) as e:
            # State write already succeeded — don't flip applied → failed.
            # Surface the audit-note gap on stderr so operators see the
            # forensics hole; the data-quality report (PR-43.5) will pick
            # it up via the missing-note count.
            click.echo(
                f"  ⚠ Cadence auto-repair: state updated for "
                f"{d.get('name', '?')} but audit note failed "
                f"({type(e).__name__}: {e}). Forensics gap — investigate "
                f"if Attio note quota / network is healthy.",
                err=True,
            )
    return applied, failed


def _normalize_name(name: str) -> str:
    """Normalize a name for fuzzy matching.

    Steps applied (§4.1 diacritic fix, PR-10):
    1. NFKD decomposition — separates base letters from combining marks
       (e.g. 'é' → 'e' + COMBINING ACUTE ACCENT).
    2. Strip combining characters (Unicode categories 'Mn' and 'Cf'):
       'Mn' removes accent marks, 'Cf' removes invisible format
       characters like zero-width joiners (U+200D) and word joiners
       (U+2060) that LinkedIn occasionally injects into pasted names.
    3. Lowercase + collapse whitespace — standard pre-existing normalization.

    This allows 'José' to match 'Jose', 'Müller' to match 'Muller', etc.
    The function is idempotent: normalizing an already-normalized string
    returns the same string.

    Known gap (silent-failure HIGH-5): characters with stroke marks
    encoded as single code points — Ł (U+0141), Đ (U+0110), Ø (U+00D8) —
    do not decompose under NFKD and therefore do not match their ASCII
    equivalents. The Polish/Croatian/Norwegian cohort is rare enough in
    the ICP that the explicit-mapping fix is deferred until we have data
    showing it's needed.
    """
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(
        ch for ch in decomposed
        if unicodedata.category(ch) not in ("Mn", "Cf")
    )
    return " ".join(ascii_only.lower().strip().split())


_SELF_ECHO_MIN_OVERLAP = 0.75


def _normalize_for_echo(text: str) -> list[str]:
    """Normalize a message body for self-echo token comparison.

    Lowercase, accent-fold (NFKD + strip combining marks), drop the greeting
    up to the first comma (so "Hola [Name], ..." and "Hola César, ..." align),
    strip the [Name]/[Company] template placeholders, and split into tokens.
    """
    if "," in text:
        text = text.split(",", 1)[1]
    text = text.replace("[Name]", " ").replace("[Company]", " ")
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_only = "".join(
        ch for ch in decomposed
        if unicodedata.category(ch) not in ("Mn", "Cf")
    )
    return [t for t in "".join(
        c if c.isalnum() or c.isspace() else " " for c in ascii_only.lower()
    ).split() if t]


# Self-echo guard template cache. `load_messages` is an unguarded open+json.load;
# calling it per manual-reply candidate meant a missing/corrupt messages.json
# crashed ALL of detect_responses. We tokenize every template ONCE and cache the
# (template_id, token_set) list. `None` is a cached sentinel meaning "load failed
# this run" — the guard then fails OPEN (see below).
_UNSET = object()
_SELF_ECHO_TEMPLATES: object = _UNSET
_SELF_ECHO_WARNED = False


def _reset_self_echo_template_cache() -> None:
    """Clear the memoized template token sets (test hook / long-lived procs)."""
    global _SELF_ECHO_TEMPLATES, _SELF_ECHO_WARNED
    _SELF_ECHO_TEMPLATES = _UNSET
    _SELF_ECHO_WARNED = False


def _self_echo_templates() -> list[tuple[str, frozenset[str]]] | None:
    """Load + tokenize every DM template ONCE. Returns None on load failure.

    Cached across the run so `load_messages()` (an unguarded file read) is hit
    at most once, not once per manual-reply candidate. On failure we cache
    `None` and return it — the caller fails OPEN and warns once.
    """
    global _SELF_ECHO_TEMPLATES
    if _SELF_ECHO_TEMPLATES is not _UNSET:
        return _SELF_ECHO_TEMPLATES  # type: ignore[return-value]
    try:
        messages = load_messages()
    except Exception:  # noqa: BLE001 — degrade OPEN, do not crash detection
        _SELF_ECHO_TEMPLATES = None
        return None
    built: list[tuple[str, frozenset[str]]] = []
    for persona, steps in messages.items():
        if not isinstance(steps, dict):
            continue
        for step, langs in steps.items():
            if not isinstance(langs, dict):
                continue
            for lang, template in langs.items():
                if not isinstance(template, str):
                    continue
                tokens = frozenset(_normalize_for_echo(template))
                if not tokens:
                    continue
                built.append((f"{persona}/{step}/{lang}", tokens))
    _SELF_ECHO_TEMPLATES = built
    return built


def _looks_like_self_echo(last_body: str) -> str | None:
    """Return a matched template id if `last_body` echoes one of OUR templates.

    The SN Inbox Scraper exposes no per-message senders — a thread of N messages
    all from us (the PR-241 dup-DM1 case) is arithmetically indistinguishable
    from (N-1)-ours + 1-theirs. Before the count heuristic flips a prospect to
    Responded, compare the last message body against every DM template: a
    token-overlap ratio ≥ `_SELF_ECHO_MIN_OVERLAP` against any template means
    the "reply" is actually our own copy echoed back.

    Returns the matching template id (``"{persona}/{step}/{lang}"``) or None.

    Fails OPEN: if the template set can't load (missing/corrupt messages.json),
    warn LOUDLY once and return None so manual-reply flips proceed WITHOUT the
    self-echo check this run. Crashing the whole run to stay closed is worse;
    the warning makes the degraded state visible.
    """
    body_tokens = set(_normalize_for_echo(last_body))
    if not body_tokens:
        return None
    templates = _self_echo_templates()
    if templates is None:
        global _SELF_ECHO_WARNED
        if not _SELF_ECHO_WARNED:
            _SELF_ECHO_WARNED = True
            click.echo(
                "  ⚠ self-echo guard offline: could not load DM templates "
                "(missing/corrupt messages.json) — manual-reply flips proceed "
                "WITHOUT the self-echo check this run.",
                err=True,
            )
        return None

    best_id: str | None = None
    best_ratio = 0.0
    for template_id, tmpl_tokens in templates:
        # Fraction of the TEMPLATE reproduced in the body. An echo is our full
        # template → ratio ≈ 1.0; a short genuine reply reproduces few template
        # tokens → low ratio. Denominator is the template (not the shorter set)
        # so a 2-word reply can't trivially score 1.0 against a long template.
        overlap = len(body_tokens & tmpl_tokens)
        ratio = overlap / len(tmpl_tokens)
        if ratio > best_ratio:
            best_ratio = ratio
            best_id = template_id
    if best_ratio >= _SELF_ECHO_MIN_OVERLAP:
        return best_id
    return None


def _reconstruct_opener(
    persona_value: str | None,
    language_value: str | None,
    prospect_name: str,
    company: str,
) -> str:
    """Rebuild the DM1 opener for the prospect's persona/language.

    Used as context for the LLM classifier — the same reply can be defensive
    or neutral depending on what it was replying to. We reconstruct the V0
    baseline opener (dm1) because the prospect's current arm assignment is
    not persisted per-entry; V0 is a close approximation across all arms
    for the purposes of classification, and wrong-but-close is better than
    no opener context at all.

    Falls back to a generic sentence if the persona or language is missing
    or unrecognized.
    """
    try:
        persona = Persona(persona_value) if persona_value else Persona.OPERATIONS_LEADERS
        language = Language(language_value) if language_value else Language.ES
        template = get_message(persona, language, MessageStep.DM1)
        first_name = prospect_name.split()[0] if prospect_name else "there"
        return personalize(template, first_name, company or "your company")
    except (ValueError, KeyError, MissingMessageError):
        # PR-16 fold-in (prospect-daily-QA-build16 BLOCKING): PR-16
        # changed get_message from silent-Spanish-fallback to raising
        # MissingMessageError. This call site is classifier-context
        # only (not an outbound send) — falling back to a generic
        # Spanish opener is the correct §0 #9 carve-out per the
        # function docstring "wrong-but-close is better than no opener
        # context". The fallback is observable via this code path
        # itself, not via a queue row, because the prospect's actual
        # outbound was never sent — there's no per-prospect missing
        # copy event to surface here.
        #
        # Brand-/domain-neutral generic opener: the genericized engine has no
        # hardcoded product pitch or language to fall back to. A short neutral
        # outreach line is enough classifier context ("what was this a reply
        # to?") without leaking any operator-specific copy.
        return "Hi, following up on my earlier message — would love your thoughts."


def _handle_manual_reply(
    attio: AttioClient,
    list_id: str,
    actionable: list[dict],
    participant_name: str,
    our_last_message: str,
    total_messages: int,
    expected_messages: int,
    counts: dict,
    resend: "ResendClient | None" = None,
) -> None:
    """Route a thread where we manually replied to a prospect's reply.

    SN Inbox Scraper only surfaces the last message body. When that's ours,
    we don't have the prospect's reply text to classify — but the thread has
    more messages than our automated sequence sent, so a reply must have
    happened. Move to RESPONDED so the sequence stops and flag for human
    review.

    PR-20 B-SD-007: writes ``response_classification="manual_unclassified"``
    on the entry (was only writing ``stage`` pre-PR-20) so downstream
    consumers — especially ``learn.py``'s ``_per_step_rates`` denominator
    math — can distinguish "operator hasn't classified yet" from
    "classifier ran". Opens ``manual_reply_unclassified`` Operator
    Review Queue row so the operator sees the unclassified entry in
    the queue UI (idempotent on ``f"manual_reply|{record_id}"``).

    If ``quality_score >= 70``, also fires PR-18's ``emit_hot_lead`` on
    the ``manual_unclassified`` branch — the hot-lead gate accepts
    this classification when score is high enough (the trigger is
    documented in ``workflows.hot_lead_alert.should_emit_hot_lead``).
    """
    new_stage = PipelineStage.RESPONDED.value
    counts["detected"] += 1
    suffix = f" ({len(actionable)} entries)" if len(actionable) > 1 else ""
    click.echo(
        f"  Manual reply detected from {participant_name} "
        f"({total_messages} messages in thread, expected {expected_messages} from us, "
        f"we replied back last) -> moving to '{new_stage}'{suffix}. "
        f"Marking response_classification=manual_unclassified for operator triage."
    )

    truncated_our_msg = our_last_message[:MAX_RESPONSE_TEXT_LEN]
    # Wave-2-B §3.15 cleanup: AttioWriter route. The PB-skipped
    # `manual_unclassified` value still doesn't exist in production
    # Attio's response_classification enum (Wave-2 schema deploy lands
    # it), so writes here still 400 — AttioWriter catches that as
    # AttioPermanentError, DLQ's, and opens an attio_write_failed
    # queue row via its built-in escalation. The site-specific 4xx
    # branch below catches AttioPermanentError to preserve the per-
    # entry continue + counts tally.
    from clients.attio_writer import (
        AttioError,
        AttioMonotonicityViolation,
        AttioPermanentError,
        AttioRateLimitExhausted,
        AttioTerminalClassRegression,
        AttioWriter,
        UnauthorizedAttioWriteError,
        WriteIntent,
    )
    _writer = AttioWriter(attio=attio)
    for attrs in actionable:
        try:
            _writer.apply(WriteIntent(
                object="linkedin_outreach",
                record_id=attrs["entry_id"],
                updates={
                    "stage": new_stage,
                    "response_classification": "manual_unclassified",
                },
                prior_values={"stage": attrs.get("stage")},
                writer_module="workflows.detect_responses._handle_manual_reply",
                is_list_entry=True,
                list_id=list_id,
                # Wave-2-B fix-up (multi-agent I-4): underlying person
                # record_id for queue-row navigation.
                companion_record_id=str(attrs.get("record_id", "")) or None,
            ))
            # Wave-2-B fix-up (silent-failure-hunter HIGH-2): note
            # creation is NOT routed through AttioWriter (notes are
            # forensic, not state-bearing). Wrap it in its own narrow
            # except so a note failure isn't mis-attributed as a
            # stage-write failure by the outer httpx.HTTPStatusError
            # handler — the stage advance already landed.
            try:
                attio.create_note(
                    record_id=attrs["record_id"],
                    title="Manual reply detected -- review thread",
                    content=(
                        f"Thread has {total_messages} messages but we only sent "
                        f"{expected_messages} automated DMs at this stage — and "
                        f"the last message in the thread is ours. The prospect "
                        f"replied at some point and we replied back manually.\n\n"
                        f"Our last reply: {truncated_our_msg}\n\n"
                        f"The prospect's reply body is not available via the SN "
                        f"Inbox Scraper (only the last message is exposed). Stage "
                        f"moved to Responded with response_classification="
                        f"manual_unclassified. Review the thread on LinkedIn and "
                        f"set Qualified, Not Interested, or another stage manually."
                    ),
                )
            except (httpx.HTTPStatusError,
                    httpx.RequestError,
                    httpx.TimeoutException) as _note_exc:
                # Stage IS already advanced; missing audit note is a
                # forensics gap, not a §3.1 risk.
                click.echo(
                    f"  ⚠ Manual-reply stage flip landed for "
                    f"{participant_name} (entry {attrs['entry_id']}) "
                    f"but audit note failed: "
                    f"{type(_note_exc).__name__}: {_note_exc}. "
                    f"Forensics gap — investigate if Attio note quota "
                    f"or network is healthy.",
                    err=True,
                )
        except UnauthorizedAttioWriteError:
            # Caller bug — propagate. The registry mismatch must be
            # fixed in code, not papered over with a queue row.
            raise
        except AttioMonotonicityViolation as mv:
            # Wave-2-B follow-up I-2: data-drift signal (e.g.
            # actionable row has stage=CALL_BOOKED but the manual-reply
            # detector queued it for RESPONDED). Open a typed
            # drift_detector_finding queue row and CONTINUE the batch
            # — one bad row mustn't drop manual_reply_unclassified
            # queue writes for rows 4-10 in the actionable list.
            counts.setdefault("drift_detected", 0)
            counts["drift_detected"] += 1
            try:  # noqa: SIM105 — best-effort escalate; intentional swallow
                escalate(
                    type="drift_detector_finding",
                    idempotency_key=(
                        f"manual-reply-monotonicity|"
                        f"{attrs['entry_id']}|"
                        f"{date.today().isoformat()}"
                    ),
                    payload={
                        "site": "_handle_manual_reply",
                        "entry_id": attrs["entry_id"],
                        "record_id": str(attrs.get("record_id", "")),
                        "current_stage": attrs.get("stage"),
                        "intended_stage": new_stage,
                        "error": str(mv),
                        "participant_name": participant_name,
                    },
                    attio=attio,
                )
            except Exception:  # noqa: BLE001 — best-effort escalate
                pass
            click.echo(
                f"  ⚠ Manual-reply monotonicity-rejected for "
                f"{participant_name} (entry {attrs['entry_id']}): {mv}. "
                f"drift_detector_finding queue row opened — operator "
                f"triage required. Continuing batch.",
                err=True,
            )
            continue
        except AttioTerminalClassRegression as tcr:
            counts.setdefault("drift_detected", 0)
            counts["drift_detected"] += 1
            try:  # noqa: SIM105 — best-effort escalate; intentional swallow
                escalate(
                    type="defensive_classification_review",
                    idempotency_key=(
                        f"manual-reply-terminal-class|"
                        f"{attrs['entry_id']}|"
                        f"{date.today().isoformat()}"
                    ),
                    payload={
                        "site": "_handle_manual_reply",
                        "entry_id": attrs["entry_id"],
                        "record_id": str(attrs.get("record_id", "")),
                        "current_stage": attrs.get("stage"),
                        "intended_stage": new_stage,
                        "error": str(tcr),
                        "participant_name": participant_name,
                    },
                    attio=attio,
                )
            except Exception:  # noqa: BLE001 — best-effort escalate
                pass
            click.echo(
                f"  ⚠ Manual-reply terminal-class-rejected for "
                f"{participant_name}: {tcr}. defensive_classification_"
                f"review queue row opened — operator triage required.",
                err=True,
            )
            continue
        except AttioPermanentError as ape:
            # AttioWriter saw a 4xx and already DLQ'd + escalated via
            # attio_write_failed. Continue the loop so other rows
            # process; the queue row is the operator surface.
            counts["attio_update_failed"] += 1
            click.echo(
                f"  ERROR: Attio rejected manual-reply update for "
                f"{participant_name} (entry {attrs['entry_id']}): {ape}. "
                f"AttioWriter opened attio_write_failed queue row.",
                err=True,
            )
            continue
        except (AttioRateLimitExhausted, AttioError):
            # Transient retry-exhaustion or other AttioWriter terminal
            # failure: AttioWriter already DLQ'd + escalated. Propagate
            # so the run halts and operator sees the typed exception —
            # matches the pre-Wave-2 5xx-propagation behavior.
            counts["attio_update_failed"] += 1
            raise
        except httpx.HTTPStatusError as he:
            # Wave-1.6-ext FIX-3b' (adversarial SB-2): narrow to
            # HTTPStatusError and surface 4xx via attio_write_failed.
            # Pre-Wave-1.6-ext this branch caught HTTPStatusError +
            # RequestError + TimeoutException with only a counter
            # increment — same silent swallow risk class as FIX-3b
            # at L1055 but at the manual_unclassified branch. Attio
            # writes `response_classification='manual_unclassified'`
            # (value missing from the production response_classification
            # select enum — see done-qa-adversarial F5) and
            # `stage='Responded'`; every such write 400s. Silent
            # tally-only swallow left the row at DM3_SENT eligible for
            # re-engagement — same §3.1 risk FIX-3b's docstring names.
            #
            # Wave-2 will deploy the missing schema; this fix makes the
            # gap operator-visible via the queue. Continue the batch
            # loop on 4xx so other rows still process; let 5xx propagate
            # as infra failure.
            counts["attio_update_failed"] += 1
            status_code = (
                he.response.status_code if he.response is not None else 0
            )
            body_excerpt = ""
            if he.response is not None:
                try:
                    body_excerpt = he.response.text[:500]
                except Exception:  # noqa: BLE001 — body read best-effort
                    body_excerpt = "<unreadable response body>"
            click.echo(
                f"  ERROR: Attio rejected manual-reply update for "
                f"{participant_name} (entry {attrs['entry_id']}, "
                f"status={status_code}): {he}",
                err=True,
            )
            if 400 <= status_code < 500:
                escalate(
                    type="attio_write_failed",
                    idempotency_key=(
                        f"detect-responses-manual-attio-{status_code}"
                        f"|{attrs.get('entry_id', 'unknown')}"
                    ),
                    payload={
                        "object": "linkedin_outreach",
                        "record_id": str(attrs.get("record_id", "")),
                        "attribute_writes": {
                            "stage": new_stage,
                            "response_classification": "manual_unclassified",
                            "_response_body_excerpt": body_excerpt,
                        },
                        "error_class": type(he).__name__,
                        "error_msg": str(he),
                        "retry_count": 0,
                    },
                    attio=attio,
                )
            else:
                # 5xx (transient infra) → bubble up so operator sees the
                # halt and the run doesn't silently lose row state.
                raise
            # Skip queue row + hot-lead emit when Attio write failed —
            # entry state is unchanged, so downstream signals would
            # point at stale data (operator confusion + duplicate work).
            continue
        except (httpx.RequestError, httpx.TimeoutException) as e:
            # PR-20 fold-in (silent-failure-hunter BLOCKING): narrowed
            # from ``except Exception`` to match PR-18's hardening at
            # hot_lead_alert.py — schema drift (KeyError on
            # ``attrs["entry_id"]``, TypeError on a malformed payload)
            # must propagate as a programmer bug, not silently rebrand
            # as a "transient network error" and leave the run logged
            # as healthy with ``attio_update_failed=N``.
            counts["attio_update_failed"] += 1
            click.echo(
                f"  ERROR: failed to update Attio for {participant_name} "
                f"(entry {attrs['entry_id']}): {e}",
                err=True,
            )
            # Skip queue row + hot-lead emit when Attio write failed —
            # entry state is unchanged, so downstream signals would
            # point at stale data (operator confusion + duplicate work).
            continue

        # PR-20 B-SD-007: open `manual_reply_unclassified` queue row so
        # operators see the entry in the queue UI alongside the Attio
        # note. Idempotent on ``f"manual_reply|{record_id}"`` — re-detecting
        # the same manual reply refreshes the row.
        try:
            escalate(
                type="manual_reply_unclassified",
                idempotency_key=f"manual_reply|{attrs['record_id']}",
                payload={
                    "record_id": attrs["record_id"],
                    "entry_id": attrs["entry_id"],
                    "prospect_name": participant_name,
                    "our_last_message": truncated_our_msg,
                    "total_messages": total_messages,
                    "expected_messages": expected_messages,
                },
                attio=attio,
            )
        except (httpx.HTTPStatusError, httpx.RequestError) as q_exc:
            click.echo(
                f"  ⚠ manual_reply_unclassified queue write failed "
                f"({type(q_exc).__name__}: {q_exc}); Attio note + stage "
                f"flip are the durable record.",
                err=True,
            )

        # PR-20 + PR-18 integration: hot-lead alert fires on
        # manual_unclassified when prospect_score >= 70 — the gate
        # function in hot_lead_alert.should_emit_hot_lead encodes
        # this threshold. Pre-PR-20 the manual-reply path never
        # invoked emit_hot_lead, so high-score manual replies sat in
        # the queue without an operator-visible alert.
        if should_emit_hot_lead("manual_unclassified", attrs.get("quality_score")):
            try:
                emit_hot_lead(
                    record_id=attrs["record_id"],
                    response_classification="manual_unclassified",
                    prospect_score=attrs.get("quality_score"),
                    # No reply body available — the queue row + Attio
                    # note carry the manual context. The "excerpt"
                    # here is our reply (the only message we have).
                    message_excerpt=f"[Our reply only — prospect's reply not in scrape] {truncated_our_msg}",
                    thread_url=attrs.get("linkedin_url", ""),
                    attio=attio,
                    resend=resend,  # PR-20 fold-in: threaded from detect_responses
                    prospect_name=participant_name,
                    prospect_company=attrs.get("company_name", ""),
                )
            except HotLeadEmitFailed as e:
                click.echo(
                    f"  CRITICAL: hot-lead emit failed for manual_unclassified "
                    f"{participant_name}: {e}",
                    err=True,
                )
                raise


class NoCSVHalt(RuntimeError):
    """PR-19 B-SD-005: Phase 0.5 inbox scrape returned no CSV.

    Raised by ``detect_responses`` after opening the ``pb_csv_empty``
    Operator Review Queue row and writing
    ``daily_run.reply_detection_status='failed'``. cli.py catches this
    and exits 2 (operator-visible non-success, distinct from
    EX_TEMPFAIL=75 used for lock contention).

    Halting prevents DM3 from firing on a row whose reply may already
    be sitting in the inbox — direct §3.1 no-resend protection.
    """

    def __init__(self, container_id: str, scrape_attempt_id: str) -> None:
        self.container_id = container_id
        self.scrape_attempt_id = scrape_attempt_id
        super().__init__(
            f"SN Inbox Scraper returned no CSV (container_id={container_id!r}, "
            f"scrape_attempt_id={scrape_attempt_id!r}); reply-detection halted."
        )


def detect_responses(
    attio: AttioClient,
    pb: PhantomBusterClient,
    inbox_scraper_id: str | None,
    cache: RecordCache | None = None,
    resend: "ResendClient | None" = None,
    daily_run: "DailyRun | None" = None,
) -> dict:
    """Phase 0.5: Detect message responses via PB SN Inbox Scraper.

    Launches the SN Inbox Scraper phantom (which reads the Sales Navigator inbox),
    then matches responses back to Attio entries by participant name. Classifies
    replies and updates pipeline stages accordingly.

    `cache` may be supplied by the caller to reuse person records pre-fetched
    by earlier phases; if None, a fresh cache is built.

    ``resend`` is the PR-18 hot-lead alert email channel. Production
    cli.py wires a ``ResendClient``; tests pass ``None`` (queue-row
    durability holds independent of email delivery). When ``None``, the
    hot-lead path still fires the operator-review-queue row — only the
    convenience email is skipped.

    Already-processed prospects (RESPONDED, NOT_INTERESTED, DEFENSIVE_HOLD)
    are skipped for idempotency.
    Returns a summary dict with counts.
    """
    if not inbox_scraper_id:
        return {"skipped": True, "reason": "no_inbox_scraper_id"}

    list_id = os.environ.get("ATTIO_LIST_ID", "")
    entries = attio.query_list_entries(list_id=list_id)
    if cache is None:
        cache = RecordCache(attio)

    dm_stages = {
        PipelineStage.DM1_SENT.value,
        PipelineStage.DM2_SENT.value,
        PipelineStage.DM3_SENT.value,
    }
    skip_stages = {
        PipelineStage.RESPONDED.value,
        PipelineStage.NOT_INTERESTED.value,
        # PR-18 fold-in (prospect-daily-QA-build18): include
        # DEFENSIVE_HOLD so a prospect with two list entries (one
        # at DM2_SENT, one already at DEFENSIVE_HOLD) doesn't have
        # the DEFENSIVE_HOLD entry re-processed and re-routed.
        PipelineStage.DEFENSIVE_HOLD.value,
    }

    # Collect all prospects in DM stages
    dm_prospects: list[dict] = []
    for entry in entries:
        attrs = AttioClient.parse_entry(entry)
        if attrs["stage"] not in dm_stages:
            continue
        name, company, _, _, _ = cache.get(attrs["record_id"])
        if not name:
            continue
        attrs["prospect_name"] = name
        attrs["company_name"] = company or ""
        dm_prospects.append(attrs)

    if not dm_prospects:
        click.echo("  No prospects in DM stages to check for responses.")
        # PR-19 cross-process fix: 'no prospects to check' is a CLEAN
        # response-detection completion, not a no-op. send-dms reads
        # reply_detection_status across processes with a fail-closed guard
        # (!= 'ok'); leaving it None here would wrongly block the first DM1s of
        # a campaign (and any all-freshly-ACCEPTED day). Record 'ok' so Part-B
        # can proceed.
        _mark_reply_detection_ok(daily_run)
        return _empty_counts()

    click.echo(f"  Checking {len(dm_prospects)} prospects in DM stages for responses...")

    # Build a name index for matching SN Inbox Scraper results back to Attio
    # entries. Multiple entries can share a normalized name (e.g. two "Carlos
    # López" prospects from different companies, or duplicate Attio records for
    # one person). On a reply match we update *every* entry that shares the
    # name so no one is silently left stuck in DM stage.
    name_to_entries: dict[str, list[dict]] = {}
    for attrs in dm_prospects:
        norm = _normalize_name(attrs["prospect_name"])
        name_to_entries.setdefault(norm, []).append(attrs)

    collisions = {n: e for n, e in name_to_entries.items() if len(e) > 1}
    if collisions:
        click.echo(
            f"  ⚠ Name collisions: {len(collisions)} name(s) match multiple "
            f"Attio entries — all matched entries will be updated on reply."
        )
        for name, entries in list(collisions.items())[:5]:
            companies = ", ".join(e.get("company_name", "?") or "?" for e in entries)
            click.echo(f"      {name} → {len(entries)} entries ({companies})")

    # Build a SECOND name index covering the FULL pipeline (not just DM-stage)
    # — needed for the cadence drift detector below. Cheap to build here: we
    # already have all `entries` loaded.
    name_to_full_pipeline: dict[str, list[dict]] = {}
    for entry in entries:
        attrs = AttioClient.parse_entry(entry)
        name, company, _, _, _ = cache.get(attrs["record_id"])
        if not name:
            continue
        attrs["prospect_name"] = name
        attrs["company_name"] = company or ""
        name_to_full_pipeline.setdefault(_normalize_name(name), []).append(attrs)

    # Launch SN Inbox Scraper (reads the Sales Navigator inbox directly).
    # Bumped from 40 → INBOX_SCRAPE_LIMIT (200) so a single scrape covers all
    # of the operator's ~150 SN conversations. Same scrape feeds both the response
    # detector and the cadence drift detector below — no second PB launch.
    # 2026-06-11 schema-max audit (after the Phase 0 oversized-launch
    # incident): numberOfThreadsToScrape declares minimum=1 and NO maximum
    # (verified via PB API scripts/fetch, script id 2449319061041483), so
    # INBOX_SCRAPE_LIMIT=200 cannot trip the "is more than maximum"
    # whole-launch rejection that capped the profile scrapers.
    click.echo(f"  Launching SN Inbox Scraper (numberOfThreadsToScrape={INBOX_SCRAPE_LIMIT})...")
    launch = pb.launch_agent(inbox_scraper_id, {
        "inboxFilter": "all",
        "numberOfThreadsToScrape": INBOX_SCRAPE_LIMIT,
        **_pb_session_args(),
    })
    pb.wait_for_completion(launch, poll_interval=10, max_wait=900)

    # Download result CSV — keyed to launch.container_id (F-PR-5).
    result_csv = pb.download_result_csv(launch)
    # PR-19 B-SD-005: no-CSV halt mechanics. The pre-PR-19 silent
    # ``return {... "error": "no_csv"}`` was the silent-fallback that
    # let Part-B fire DMs on prospects whose replies might already be
    # sitting in an unread inbox — direct §3.1 risk.
    #
    # New behaviour:
    #   1. Open ``pb_csv_empty`` queue row (durable operator signal).
    #   2. Write ``daily_run.reply_detection_status='failed'``.
    #   3. Raise ``NoCSVHalt`` — cli.py catches it and exits code 2
    #      (distinct from EX_TEMPFAIL=75 used for lock contention).
    if not result_csv:
        container_id = getattr(launch, "container_id", "") or ""
        # PR-19 fold-in (code-reviewer + salesman-daily convergence):
        # ``container_id`` is already unique per PB launch (matches
        # pre_invite_check.py's degree_unknown idempotency pattern). The
        # prior ``f"{container_id}|{int(time.time())}"`` busted dedup
        # — every retry minted a fresh row even for the same incident.
        scrape_attempt_id = container_id or "unknown"
        try:
            escalate(
                type="pb_csv_empty",
                idempotency_key=scrape_attempt_id,
                payload={
                    "container_id": container_id,
                    "scrape_attempt_id": scrape_attempt_id,
                    "expected_min_rows": 1,
                    "observed_rows": 0,
                },
                attio=attio,
            )
        except (httpx.HTTPStatusError, httpx.RequestError):
            # Queue write failed; still proceed to halt. Operator will
            # see the exit-2 status in the run log even without the row.
            click.echo(
                "  ⚠ pb_csv_empty queue row write failed; halting anyway.",
                err=True,
            )
        if daily_run is not None:
            # PR-19 fold-in (silent-failure-hunter + engineer-QA + pr-test
            # 3-agent convergence): a suppressed PATCH failure would
            # silently regress the §3.1 protection PR-19 exists to
            # provide. If Attio doesn't carry ``reply_detection_status
            # ='failed'``, the next run's Part-B short-circuit won't
            # fire and DMs would proceed on a prospect whose reply
            # might be sitting unread. Make the PATCH failure
            # operator-visible (stderr CRITICAL) so they know to
            # manually halt Part-B until the durable record can be
            # written. The pb_csv_empty queue row written above is the
            # secondary durable signal — operators will see it even
            # if this PATCH fails.
            try:
                daily_run.set_reply_detection_status("failed")
            except (httpx.HTTPStatusError, httpx.RequestError) as patch_exc:
                click.echo(
                    f"  ⚠ CRITICAL: daily_run.reply_detection_status='failed' "
                    f"PATCH failed ({type(patch_exc).__name__}: {patch_exc}). "
                    f"Next run's Part-B short-circuit will NOT fire — "
                    f"operator MUST manually halt DM sequencing until "
                    f"the upstream pb_csv_empty cause is resolved.",
                    err=True,
                )
        click.echo(
            f"  ⚠ SN Inbox Scraper returned no CSV (container_id={container_id!r}). "
            f"Opening pb_csv_empty queue row + halting reply detection.",
            err=True,
        )
        raise NoCSVHalt(container_id=container_id, scrape_attempt_id=scrape_attempt_id)

    # Cache parsed CSV rows — re-used by the drift detector after the main
    # response-detection loop, so we don't re-parse the CSV.
    scraped_threads = list(csv.DictReader(io.StringIO(result_csv)))

    # SN Inbox Scraper columns:
    # participantProfileUrl, participantFullName, isLastMessageFromMe,
    # lastMessageBody, lastMessageDate, totalMessageCount, ...
    counts = _empty_counts()
    # Record IDs the response classifier moved to RESPONDED / NOT_INTERESTED
    # in this run. The cadence auto-repair below must skip these so it
    # doesn't overwrite the classifier's stage decision with a stale
    # DM-cadence-inferred stage.
    classifier_touched_record_ids: set[str] = set()
    # Wave-1.6.3 (adversarial follow-up to FIX-C): tally failed
    # `attio_write_failed` escalate() calls in the classification 4xx
    # branch. Kept as a local int (not added to `counts`) because the
    # counts dict shape is public contract for downstream callers.
    classify_escalate_failed_count = 0

    for row in scraped_threads:
        participant_name = row.get("participantFullName", "").strip()
        is_from_me = row.get("isLastMessageFromMe", "").strip().lower()
        last_body = row.get("lastMessageBody", "").strip()
        total_messages_raw = row.get("totalMessageCount", "").strip()

        if not participant_name or not last_body:
            continue

        # Match to Attio entries by name (one name can map to multiple entries)
        norm_name = _normalize_name(participant_name)
        matched_entries = name_to_entries.get(norm_name)
        if not matched_entries:
            continue

        # Idempotency: only act on entries not already in a terminal stage
        actionable = [a for a in matched_entries if a["stage"] not in skip_stages]
        if not actionable:
            continue

        # Manual-reply detection. SN Inbox Scraper only surfaces the LAST
        # message in a thread. When `is_from_me == "true"` two scenarios
        # share that signal:
        #   (a) Just our automated DMs in the thread, no reply yet.
        #   (b) Prospect replied AND we (the operator) replied back manually.
        # `totalMessageCount` disambiguates: in (a) it equals the count of
        # automated DMs we've sent at this stage; in (b) it's strictly more.
        # Date comparison is unreliable for same-day reply scenarios, so we
        # use message count instead.
        if is_from_me == "true":
            primary = actionable[0]
            expected = EXPECTED_OUR_MESSAGES.get(primary["stage"])
            try:
                total_messages = int(total_messages_raw)
            except ValueError:
                total_messages = 0
            # Missing data or unknown stage → fall back to skip (safe default).
            if expected is None or total_messages <= expected:
                continue
            # Self-echo guard (PR-241 César RCA). The count heuristic can't tell
            # a real reply from our own duplicate DM echoed back — the scraper
            # exposes no per-message senders. If the last body matches one of
            # OUR templates, it's a self-echo (the dup-DM1 case): do NOT flip to
            # Responded. A genuine reply body won't match and falls through to
            # _handle_manual_reply unchanged.
            matched_template_id = _looks_like_self_echo(last_body)
            if matched_template_id is not None:
                click.echo(
                    f"  ⚠ Suppressed manual-reply flip for {participant_name}: "
                    f"last message matches our own template "
                    f"{matched_template_id!r} (suspected self-echo from a "
                    f"duplicate DM, not a reply). NOT moving to Responded; "
                    f"escalating for operator review.",
                    err=True,
                )
                counts.setdefault("self_echo_suppressed", 0)
                counts["self_echo_suppressed"] += 1
                _primary = actionable[0]
                try:
                    escalate(
                        type="manual_reply_suppressed_self_echo",
                        idempotency_key=(
                            f"{_primary.get('entry_id') or ''}"
                            f"|{date.today().isoformat()}"
                        ),
                        payload={
                            "record_id": str(_primary.get("record_id") or ""),
                            "entry_id": str(_primary.get("entry_id") or ""),
                            "name": participant_name,
                            "stage": str(_primary.get("stage") or ""),
                            "total_messages": total_messages,
                            "expected": expected,
                            "matched_template_id": matched_template_id,
                        },
                        attio=attio,
                    )
                except Exception as esc_exc:  # noqa: BLE001 — never block detection
                    click.echo(
                        f"  ⚠ escalate(manual_reply_suppressed_self_echo) failed "
                        f"for {participant_name} "
                        f"[{type(esc_exc).__name__}]: {esc_exc}. Row still held "
                        f"out of the Responded flip; continuing.",
                        err=True,
                    )
                continue
            # (b) — manual reply. Move to RESPONDED + note for human triage.
            _handle_manual_reply(
                attio=attio,
                list_id=list_id,
                actionable=actionable,
                participant_name=participant_name,
                our_last_message=last_body,
                total_messages=total_messages,
                expected_messages=expected,
                counts=counts,
                resend=resend,  # PR-20 fold-in: thread the email channel through
            )
            classifier_touched_record_ids.update(
                a["record_id"] for a in actionable if a.get("record_id")
            )
            continue

        # PR-20 B-SD-008 false-positive guard: a "reply" that's actually
        # the LinkedIn auto-connection-note should NOT be classified.
        # Set ``had_connection_note=True`` on the entry and skip — the
        # prospect remains at their current stage for regular DM
        # cadence. See ``_looks_like_connection_note`` heuristic doc.
        primary = actionable[0]
        # PR-20 fold-in (silent-failure-hunter Finding 3 — §0 #9): on
        # malformed dm_step / total_messages, do NOT default to 0 —
        # ``dm_step=0`` is the precise sentinel that opens the
        # connection-note guard, so silent coercion would falsely
        # misroute mid-cadence prospects with garbage state into the
        # had_connection_note=True branch. Skip the guard entirely on
        # coercion failure; let the row fall through to the classifier
        # which has its own error surface.
        primary_dm_step_raw = primary.get("dm_step")
        primary_dm_step: int | None
        try:
            primary_dm_step = (
                int(primary_dm_step_raw)
                if primary_dm_step_raw is not None and primary_dm_step_raw != ""
                else 0  # genuinely missing is defensible — fresh prospect
            )
        except (TypeError, ValueError):
            primary_dm_step = None  # malformed; cannot apply the guard
        primary_total: int | None
        try:
            primary_total = int(total_messages_raw) if total_messages_raw else 0
        except (TypeError, ValueError):
            primary_total = None  # malformed; cannot apply the guard
        if (
            primary_dm_step is not None
            and primary_total is not None
            and _looks_like_connection_note(
                dm_step=primary_dm_step,
                total_messages=primary_total,
                last_body=last_body,
            )
        ):
            click.echo(
                f"  Detected connection-acceptance note from "
                f"{participant_name} (dm_step=0, total_messages=1, "
                f"len={len(last_body)}) — setting had_connection_note=True "
                f"and skipping classification (PR-20 B-SD-008 guard)."
            )
            counts.setdefault("connection_note_skipped", 0)
            counts["connection_note_skipped"] += 1
            # Wave-2-B §3.15 cleanup: route the had_connection_note
            # write through AttioWriter so the registry actually
            # enforces the writer module on this branch. The schema
            # change is single-attr (no stage) so monotonicity is a
            # no-op TODAY — only the registry check fires. Wave-2-B
            # fix-up (silent-failure-hunter CRITICAL-3) adds the
            # programmer-bug-class catch defensively so a future
            # refactor that accidentally adds `stage` to this payload
            # surfaces the violation instead of silently swallowing it.
            from clients.attio_writer import (
                AttioError as _AttioError,
            )
            from clients.attio_writer import (
                AttioMonotonicityViolation as _AttioMonotonicityViolation,
            )
            from clients.attio_writer import (
                AttioTerminalClassRegression as _AttioTerminalClassRegression,
            )
            from clients.attio_writer import (
                AttioWriter as _AttioWriter,
            )
            from clients.attio_writer import (
                UnauthorizedAttioWriteError as _UnauthorizedAttioWriteError,
            )
            from clients.attio_writer import (
                WriteIntent as _WriteIntent,
            )
            _conn_note_writer = _AttioWriter(attio=attio)
            for attrs in actionable:
                try:
                    _conn_note_writer.apply(_WriteIntent(
                        object="linkedin_outreach",
                        record_id=attrs["entry_id"],
                        updates={"had_connection_note": True},
                        prior_values={},
                        writer_module="workflows.detect_responses.detect_responses",
                        is_list_entry=True,
                        list_id=list_id,
                        companion_record_id=str(attrs.get("record_id", "")) or None,
                    ))
                except (_UnauthorizedAttioWriteError,
                        _AttioMonotonicityViolation,
                        _AttioTerminalClassRegression):
                    # Defense in depth — see CRITICAL-3 carve-out
                    # above. Single-attr payload today so these
                    # shouldn't fire; propagate if they ever do.
                    raise
                except _AttioError as e:
                    counts["attio_update_failed"] += 1
                    click.echo(
                        f"  ERROR: failed to write had_connection_note for "
                        f"{participant_name}: {e}",
                        err=True,
                    )
                except (httpx.HTTPStatusError, httpx.RequestError) as e:
                    counts["attio_update_failed"] += 1
                    click.echo(
                        f"  ERROR: failed to write had_connection_note for "
                        f"{participant_name}: {e}",
                        err=True,
                    )
                # PR-20 + PR-52 interaction: the connection-note path
                # is terminal for this prospect this run. Mark the
                # record as classifier-touched so the cadence-drift
                # auto-repair doesn't reinterpret the acceptance note
                # as a "DM1 sent" drift and bump dm_step/stage. Use
                # .get() defensively to match the .update() pattern
                # used elsewhere for the actionable touchpoints.
                if attrs.get("record_id"):
                    classifier_touched_record_ids.add(attrs["record_id"])
            continue

        # Reconstruct the opener the prospect was replying to. The LLM
        # classifier needs both sides of the exchange to distinguish
        # "neutral in isolation" replies from "defensive in context".
        # Use the first actionable entry for opener context — colliding
        # entries with different personas/languages will produce a slightly
        # off opener, but the classifier looks primarily at reply_text.
        opener_text = _reconstruct_opener(
            persona_value=primary.get("persona"),
            language_value=primary.get("language"),
            prospect_name=primary["prospect_name"],
            company=primary.get("company_name", ""),
        )

        # Classify — LLM with internal keyword fallback. Always returns a
        # valid envelope; the `source` field flags which path ran.
        result = classify_reply_llm(opener_text=opener_text, reply_text=last_body)
        classification = result["classification"]
        counts["detected"] += 1

        if classification in counts:
            counts[classification] += 1

        source = result.get("source", "keyword")
        if source == "llm":
            counts["classifier_llm"] += 1
        else:
            counts["classifier_keyword"] += 1

        # Stage routing (PR-18 B-SD-003 defensive split):
        # - negative → NOT_INTERESTED (hard stop, don't retry)
        # - defensive → DEFENSIVE_HOLD (was RESPONDED pre-PR-18) — the
        #   reply is reactance, not a decline. DEFENSIVE_HOLD blocks
        #   automated re-engagement while keeping the prospect out of
        #   "responded" denominators that would mix with positive
        #   replies. Per plan Task 3 + F-PR-1: never retry opener
        #   variant on a defensive responder.
        # - positive / question / neutral → RESPONDED (human picks it up)
        if classification == "negative":
            new_stage = PipelineStage.NOT_INTERESTED.value
        elif classification == "defensive":
            new_stage = PipelineStage.DEFENSIVE_HOLD.value
        else:
            new_stage = PipelineStage.RESPONDED.value

        suffix = f" ({len(actionable)} entries)" if len(actionable) > 1 else ""
        click.echo(
            f"  Response from {participant_name} [{classification}/{source}] "
            f"-> moving to '{new_stage}'{suffix}"
        )

        truncated_body = last_body[:MAX_RESPONSE_TEXT_LEN]

        # Phase 1 auto-research: persist defensive/engagement scores as queryable
        # numeric fields, not just inside the note body. Cast through float() so
        # MagicMock test fixtures and missing keys collapse to a None we skip.
        update_attrs: dict = {
            "stage": new_stage,
            "response_classification": classification,
            "last_response_text": truncated_body,
        }
        try:
            ds = result.get("defensive_score")
            if ds is not None:
                update_attrs["defensive_score"] = float(ds)
        except (TypeError, ValueError):
            pass
        try:
            es = result.get("engagement_score")
            if es is not None:
                update_attrs["engagement_score"] = float(es)
        except (TypeError, ValueError):
            pass

        # Phase 1 fields are added best-effort: if the schema migration hasn't
        # run yet, Attio rejects unknown attributes with 400. We retry without
        # those keys so the critical state transition (stage + classification)
        # still lands. Once the operator has run scripts/migrate_attio_schema.py,
        # the first attempt succeeds and the fallback is unused.
        _PHASE1_RESPONSE_KEYS = ("defensive_score", "engagement_score")

        # Wave-2-B §3.15 cleanup: route both the primary write and the
        # Phase-1 fallback through AttioWriter. The primary write
        # includes defensive_score/engagement_score which the
        # production Attio schema may not yet accept — AttioWriter
        # wraps the 400 as AttioPermanentError; we detect that and
        # retry the fallback via AttioWriter so the registry +
        # monotonicity gates fire on the fallback path too.
        from clients.attio_writer import (
            AttioError as _AttioError_classify,
        )
        from clients.attio_writer import (
            AttioMonotonicityViolation as _AttioMonotonicityViolation_classify,
        )
        from clients.attio_writer import (
            AttioPermanentError as _AttioPermanentError,
        )
        from clients.attio_writer import (
            AttioTerminalClassRegression as _AttioTerminalClassRegression_classify,
        )
        from clients.attio_writer import (
            AttioWriter as _AttioWriter_classify,
        )
        from clients.attio_writer import (
            UnauthorizedAttioWriteError as _UnauthorizedAttioWriteError_classify,
        )
        from clients.attio_writer import (
            WriteIntent as _WriteIntent_classify,
        )
        _classify_writer = _AttioWriter_classify(attio=attio)
        for attrs in actionable:
            try:
                _phase1_present = any(
                    k in update_attrs for k in _PHASE1_RESPONSE_KEYS
                )
                try:
                    _classify_writer.apply(_WriteIntent_classify(
                        object="linkedin_outreach",
                        record_id=attrs["entry_id"],
                        updates=update_attrs,
                        prior_values={"stage": attrs.get("stage")},
                        writer_module="workflows.detect_responses.detect_responses",
                        is_list_entry=True,
                        list_id=list_id,
                        companion_record_id=str(attrs.get("record_id", "")) or None,
                    ))
                except _AttioPermanentError as ape:
                    # Detect the 400-with-Phase1 schema-drift case so the
                    # forward-compat fallback can still run. AttioWriter
                    # stuffs the status code in the message text — pull
                    # from __cause__ which is the original
                    # httpx.HTTPStatusError.
                    _cause = ape.__cause__
                    _code = (
                        _cause.response.status_code
                        if isinstance(_cause, httpx.HTTPStatusError)
                        and _cause.response is not None
                        else None
                    )
                    if _code != 400 or not _phase1_present:
                        raise
                    fallback_attrs = {
                        k: v for k, v in update_attrs.items()
                        if k not in _PHASE1_RESPONSE_KEYS
                    }
                    _classify_writer.apply(_WriteIntent_classify(
                        object="linkedin_outreach",
                        record_id=attrs["entry_id"],
                        updates=fallback_attrs,
                        prior_values={"stage": attrs.get("stage")},
                        writer_module="workflows.detect_responses.detect_responses",
                        is_list_entry=True,
                        list_id=list_id,
                        companion_record_id=str(attrs.get("record_id", "")) or None,
                    ))
                if attrs.get("record_id"):
                    classifier_touched_record_ids.add(attrs["record_id"])
                # Wave-2-B fix-up (silent-failure-hunter HIGH-1): note
                # creation is forensic-only; its failure must NOT be
                # mis-reported as a stage-write failure by the outer
                # httpx.HTTPStatusError handler. The stage advance
                # already landed.
                try:
                    attio.create_note(
                        record_id=attrs["record_id"],
                        title=f"Auto-detected response -- {classification}",
                        content=(
                            f"Message: {last_body}\n\n"
                            f"Classification: {classification} (source: {source})\n"
                            f"Defensive score: {result.get('defensive_score', 0.0):.2f}\n"
                            f"Engagement score: {result.get('engagement_score', 0.0):.2f}\n"
                            f"Reasoning: {result.get('reasoning', '')}\n"
                            f"Action: {result['suggested_action']}\n"
                            f"Summary: {result['summary']}"
                        ),
                    )
                except (httpx.HTTPStatusError,
                        httpx.RequestError,
                        httpx.TimeoutException) as _note_exc:
                    click.echo(
                        f"  ⚠ Classification stage advance landed for "
                        f"{participant_name} (entry {attrs['entry_id']}) "
                        f"but audit note failed: "
                        f"{type(_note_exc).__name__}: {_note_exc}. "
                        f"Forensics gap — investigate.",
                        err=True,
                    )
            except _UnauthorizedAttioWriteError_classify:
                # Caller bug (registry/writer_module mismatch) — halt.
                raise
            except _AttioMonotonicityViolation_classify as mv:
                # Wave-2-B follow-up I-2: data-drift signal. Open a
                # typed drift_detector_finding queue row and CONTINUE
                # so other rows in `actionable` still classify.
                counts.setdefault("drift_detected", 0)
                counts["drift_detected"] += 1
                try:  # noqa: SIM105 — best-effort escalate; intentional swallow
                    escalate(
                        type="drift_detector_finding",
                        idempotency_key=(
                            f"classify-reply-monotonicity|"
                            f"{attrs['entry_id']}|"
                            f"{date.today().isoformat()}"
                        ),
                        payload={
                            "site": "detect_responses.classify_reply",
                            "entry_id": attrs["entry_id"],
                            "record_id": str(attrs.get("record_id", "")),
                            "current_stage": attrs.get("stage"),
                            "intended_stage": new_stage,
                            "intended_classification": classification,
                            "error": str(mv),
                            "participant_name": participant_name,
                        },
                        attio=attio,
                    )
                except Exception:  # noqa: BLE001 — best-effort escalate
                    pass
                click.echo(
                    f"  ⚠ Classify-reply monotonicity-rejected for "
                    f"{participant_name} (entry {attrs['entry_id']}): "
                    f"{mv}. drift_detector_finding queue row opened "
                    f"— operator triage required. Continuing batch.",
                    err=True,
                )
                continue
            except _AttioTerminalClassRegression_classify as tcr:
                counts.setdefault("drift_detected", 0)
                counts["drift_detected"] += 1
                try:  # noqa: SIM105 — best-effort escalate; intentional swallow
                    escalate(
                        type="defensive_classification_review",
                        idempotency_key=(
                            f"classify-reply-terminal-class|"
                            f"{attrs['entry_id']}|"
                            f"{date.today().isoformat()}"
                        ),
                        payload={
                            "site": "detect_responses.classify_reply",
                            "entry_id": attrs["entry_id"],
                            "record_id": str(attrs.get("record_id", "")),
                            "current_stage": attrs.get("stage"),
                            "intended_stage": new_stage,
                            "error": str(tcr),
                            "participant_name": participant_name,
                        },
                        attio=attio,
                    )
                except Exception:  # noqa: BLE001 — best-effort escalate
                    pass
                click.echo(
                    f"  ⚠ Classify-reply terminal-class-rejected for "
                    f"{participant_name}: {tcr}. defensive_"
                    f"classification_review queue row opened.",
                    err=True,
                )
                continue
            except _AttioPermanentError as ape:
                # Wave-2-B: AttioWriter raised this AFTER it already
                # DLQ'd + opened an `attio_write_failed` queue row via
                # its own _dlq_and_escalate. We don't re-escalate here
                # — that would create a duplicate row. Tally + log so
                # the end-of-function summary stays informative; the
                # operator surface is the queue row AttioWriter wrote.
                counts["attio_update_failed"] += 1
                _cause = ape.__cause__
                _code = (
                    _cause.response.status_code
                    if isinstance(_cause, httpx.HTTPStatusError)
                    and _cause.response is not None
                    else 0
                )
                click.echo(
                    f"  ERROR: AttioWriter rejected response update for "
                    f"{participant_name} (entry {attrs['entry_id']}, "
                    f"status={_code}): {ape}. attio_write_failed queue "
                    f"row already opened by AttioWriter.",
                    err=True,
                )
                continue
            except _AttioError_classify as ae:
                # AttioWriter exhausted retries / hit a permanent infra
                # failure. Wave-2-B: discriminate on ``__cause__`` so
                # ConnectError/ReadTimeout exhausted-retries are counted
                # + continued (FIX-C semantic) while 5xx exhausted-
                # retries propagate (FIX-3b semantic — Attio degraded
                # for the whole batch). Both classes already triggered
                # AttioWriter._dlq_and_escalate so the queue row is
                # operator-visible either way.
                #
                # Wave-2-B fix-up (multi-agent I-3): whitelist the
                # swallowable network classes instead of "anything
                # that isn't HTTPStatusError". Pre-fix-up an
                # `AttioWriteFailed` with `__cause__=None` (caller
                # bugs like `is_list_entry=True requires list_id`) OR
                # a deadline-exhausted-before-first-attempt path
                # (`last_exc=None`) would silently route to the
                # count+continue branch — same swallow-class as the
                # 5xx case the FIX-3b semantic was added to prevent.
                counts["attio_update_failed"] += 1
                cause = ae.__cause__
                _SWALLOWABLE_NETWORK = (
                    httpx.ConnectError, httpx.ReadTimeout, httpx.ReadError,
                )
                if not isinstance(cause, _SWALLOWABLE_NETWORK):
                    # Unknown cause (None / HTTPStatusError 5xx / caller
                    # bug / mock wrapper) → propagate so the operator
                    # sees the typed exception instead of silently
                    # bucketing into attio_update_failed.
                    raise
                # Network-class exhaustion → count + continue so other
                # rows still classify.
                click.echo(
                    f"  ERROR: AttioWriter exhausted retries for "
                    f"{participant_name} (entry {attrs['entry_id']}): "
                    f"{type(ae).__name__}: {ae}. attio_write_failed "
                    f"queue row already opened by AttioWriter.",
                    err=True,
                )
                continue
            except httpx.HTTPStatusError as he:
                # Defense in depth: AttioWriter narrows httpx errors
                # into AttioError. This branch fires only if a raw
                # httpx error ever leaks through (e.g. from
                # create_note which is NOT routed through AttioWriter
                # by design — notes are forensic, not state-bearing).
                # Preserve the pre-Wave-2 4xx escalate path so a note
                # failure is still operator-visible.
                counts["attio_update_failed"] += 1
                status_code = (
                    he.response.status_code if he.response is not None else 0
                )
                body_excerpt = ""
                if he.response is not None:
                    try:
                        body_excerpt = he.response.text[:500]
                    except Exception:  # noqa: BLE001 — body read best-effort
                        body_excerpt = "<unreadable response body>"
                click.echo(
                    f"  ERROR: Attio rejected response update for {participant_name} "
                    f"(entry {attrs['entry_id']}, status={status_code}): {he}",
                    err=True,
                )
                if 400 <= status_code < 500:
                    # Wave-1.6.3 (adversarial follow-up to FIX-C): if this
                    # escalate raises (e.g. the same 5xx burst that drove
                    # the response handler's RequestError branch — see
                    # FIX-C at L1170), the propagation kills the outer
                    # for-row classification loop and subsequent rows
                    # orphan. NOT §3.1 (stage stayed at DM_SENT for those
                    # rows, so no re-send risk specifically), but §3 #9
                    # silent batch-truncation. Catch is intentionally
                    # broad; tally for end-of-function summary.
                    try:
                        escalate(
                            type="attio_write_failed",
                            idempotency_key=(
                                f"detect-responses-attio-{status_code}"
                                f"|{attrs.get('entry_id', 'unknown')}"
                                f"|{classification}"
                            ),
                            payload={
                                "object": "linkedin_outreach",
                                "record_id": str(attrs.get("record_id", "")),
                                "attribute_writes": {
                                    **update_attrs,
                                    "_response_body_excerpt": body_excerpt,
                                },
                                "error_class": type(he).__name__,
                                "error_msg": str(he),
                                "retry_count": 0,
                            },
                            attio=attio,
                        )
                    except Exception as esc_exc:  # noqa: BLE001 — see comment above
                        classify_escalate_failed_count += 1
                        click.echo(
                            f"  ⚠ escalate(attio_write_failed) for "
                            f"classification 4xx failed for "
                            f"entry_id={attrs.get('entry_id')!r} "
                            f"classification={classification!r} "
                            f"[{type(esc_exc).__name__}]: {esc_exc}. "
                            f"Continuing batch — subsequent rows still "
                            f"classified. Original 4xx was "
                            f"{type(he).__name__} status={status_code}. "
                            f"End-of-function summary will surface this "
                            f"failure count.",
                            err=True,
                        )
                else:
                    # 5xx (transient infra) → bubble up so the operator sees the
                    # halt and the run doesn't silently lose the row's stage flip.
                    raise
            except (httpx.RequestError, httpx.TimeoutException) as e:
                # Wave-1.6.2 FIX-C (adversarial EXT-SB-4 IMPORTANT):
                # reconcile asymmetry with FIX-3b' at L513. Pre-Wave-1.6.2
                # the classification branch caught HTTPStatusError only;
                # a transient `RequestError`/`TimeoutException` (DNS blip,
                # connection reset, Attio 30s timeout) would propagate
                # and halt the entire batch — even though the per-row
                # classification work should isolate row failures. The
                # manual_unclassified branch at L513/L576 already had the
                # counter+continue pattern; we mirror it here so both
                # branches behave symmetrically on transient network errors.
                # Schema/code bugs (KeyError, AttributeError, TypeError)
                # still propagate as before.
                counts["attio_update_failed"] += 1
                click.echo(
                    f"  ERROR: transient Attio failure for {participant_name} "
                    f"(entry {attrs['entry_id']}) [{type(e).__name__}]: {e}. "
                    f"Continuing batch.",
                    err=True,
                )
                # Skip queue row + hot-lead emit when Attio write failed —
                # entry state is unchanged, so downstream signals would
                # point at stale data (operator confusion + duplicate work).
                continue

        # PR-18 B-SD-002: emit hot-lead alert on positive classification.
        # Queue row FIRST (durable), then Resend email SECOND
        # (best-effort). Defensive replies route to DEFENSIVE_HOLD above
        # and do NOT fire this — by spec. Fires once per thread (not per
        # actionable entry); idempotency_key on record_id means a
        # second actionable entry for the same record would refresh the
        # same queue row, but we de-dup at the loop level to keep the
        # log clean.
        primary_entry = actionable[0]
        if should_emit_hot_lead(classification, primary_entry.get("quality_score")):
            thread_url = (
                row.get("threadUrl")
                or row.get("thread_url")
                or row.get("conversationUrl")
                or row.get("url")
                or primary_entry.get("linkedin_url")
                or ""
            )
            try:
                emit_hot_lead(
                    record_id=primary_entry["record_id"],
                    response_classification=classification,
                    prospect_score=primary_entry.get("quality_score"),
                    message_excerpt=last_body,
                    thread_url=thread_url,
                    attio=attio,
                    resend=resend,  # PR-18 fold-in: threaded from cli.py
                    prospect_name=participant_name,
                    prospect_company=primary_entry.get("company_name", ""),
                )
                counts.setdefault("hot_lead_emitted", 0)
                counts["hot_lead_emitted"] += 1
            except HotLeadEmitFailed as e:
                # The §3.1 durability guarantee failed — the queue row
                # couldn't open. Halt rather than process more replies
                # with no operator visibility of this lead.
                click.echo(
                    f"  CRITICAL: hot-lead queue write failed for "
                    f"{participant_name}: {e}",
                    err=True,
                )
                raise

    if counts["attio_update_failed"] > 0:
        click.echo(
            f"  ⚠ {counts['attio_update_failed']} Attio updates failed — "
            f"classification data for those prospects is lost this cycle.",
            err=True,
        )

    if classify_escalate_failed_count > 0:
        # Wave-1.6.3: paging-level rollup of swallowed escalate() failures
        # in the classification 4xx branch. Per-row WARN already named
        # each failure; mirrors FIX-A's summary in daily_check.py.
        click.echo(
            f"  ❌ ERROR: {classify_escalate_failed_count} "
            f"attio_write_failed escalate() call(s) failed during the "
            f"response classification batch. Stage stays at the prior "
            f"DM_SENT for those rows (no re-send risk — §3.1 holds) but "
            f"the operator review queue is missing the reconciliation "
            f"rows. Inspect the per-row WARN log lines above for the "
            f"underlying exceptions.",
            err=True,
        )

    click.echo(
        f"  Done. Detected {counts['detected']} new responses "
        f"(+{counts['positive']} positive, -{counts['negative']} negative, "
        f"?{counts['question']} question, ~{counts['neutral']} neutral, "
        f"⚠{counts['defensive']} defensive) "
        f"[LLM: {counts['classifier_llm']}, keyword: {counts['classifier_keyword']}]."
    )

    # Cadence drift detection — re-uses the inbox scrape to find any pipeline
    # entry whose Attio state disagrees with thread evidence. Monotonically-
    # forward kinds (attio_dm_step_lower_than_thread, terminal_dm_step_low)
    # are auto-repaired in-place so Part A and Part B see corrected state and
    # don't fire redundant cadence steps. Non-monotonic kinds (overstated,
    # empty-thread-but-claimed) remain advisory — a transient PB scrape gap
    # could falsely under-report cadence, and "auto-decrement" would destroy
    # real state. The auto-repair closes the gap that caused 2026-05-20:
    # Phase 0.5 detected the drift, no one acted on it, Part A flipped 1st-
    # degree prospects to ACCEPTED, Part B sent redundant DM1's on threads
    # that already had 3-5 messages.
    drifts = _detect_cadence_drift(scraped_threads, name_to_full_pipeline)
    counts["drift_detected"] = len(drifts)
    if drifts:
        from datetime import date as _date
        today_iso = _date.today().isoformat()
        auto_repair_candidates = [
            d for d in drifts
            if d.get("kind") in _AUTO_REPAIR_KINDS
            # Skip prospects the response classifier just moved to a
            # terminal stage — its stage decision wins over the cadence-
            # inferred DM stage (RESPONDED/NOT_INTERESTED are terminal).
            and d.get("record_id") not in classifier_touched_record_ids
        ]
        if auto_repair_candidates:
            click.echo(
                f"\n  Auto-repairing {len(auto_repair_candidates)} "
                f"monotonically-forward drift(s) before Part A/B see state..."
            )
            applied, failed = _apply_cadence_repairs(
                attio, list_id, auto_repair_candidates, today_iso,
            )
            counts["drift_auto_repaired"] = applied
            counts["drift_repair_failed"] = failed
            click.echo(f"  ✓ Auto-repaired {applied}; failed {failed}.")
            if failed > 0:
                # §3 hard constraint #9: surface partial-failure loudly.
                # Part A/B still proceed (the entries that DID repair are
                # in a good state; the ones that failed stay in the same
                # pre-PR-52 advisory-only condition — no regression).
                # Operators see the WARN on stderr and can re-run when
                # Attio is healthy.
                click.echo(
                    f"  ⚠ WARN: {failed} cadence auto-repair(s) failed. "
                    f"Affected entries left in advisory-only state. "
                    f"Re-run when transient Attio errors clear.",
                    err=True,
                )

        # Group by kind for a digestible summary.
        by_kind: dict[str, list[dict]] = {}
        for d in drifts:
            by_kind.setdefault(d["kind"], []).append(d)
        advisory_kinds = sorted(k for k in by_kind if k not in _AUTO_REPAIR_KINDS)
        if advisory_kinds:
            click.echo(
                f"\n  ⚠ Advisory drift: {sum(len(by_kind[k]) for k in advisory_kinds)} "
                f"entries flagged for manual review (non-monotonic kinds, not auto-repaired)."
            )
            for kind in advisory_kinds:
                items = by_kind[kind]
                click.echo(f"    {kind} ({len(items)}):")
                for d in items[:5]:
                    cur = f"{d['current_stage']}/dm{d['current_dm_step']}"
                    thr = f"n={d['thread_total']},me={d['thread_last_from_me']}"
                    click.echo(
                        f"      {(d['name'] or '?')[:35]:35s} | {(d['company'] or '')[:25]:25s} | "
                        f"Attio={cur:18s} | thread={thr}"
                    )
                if len(items) > 5:
                    click.echo(f"      ... and {len(items) - 5} more")

    # PR-19 B-SD-005 success path: reply detection completed without the
    # no-CSV halt. Mark the daily_run row so Part-B (DM sequencing) can
    # proceed past the short-circuit guard. ``partial`` is reserved for
    # future use (CSV present but < expected_min_rows); v1 is binary
    # ok/failed.
    # PR-19 fold-in: write failure on the ``ok`` write is less catastrophic than
    # the ``failed`` write (worst case: Part-B is held back, not let through).
    # Log loudly anyway — silent-failure-hunter Finding 2 flagged the suppress as
    # a documented-durability-guarantee violation. Shares the
    # _mark_reply_detection_ok helper with the no-prospects early return.
    _mark_reply_detection_ok(daily_run)

    return counts
