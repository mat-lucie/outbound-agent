"""End-of-run company-tally consistency sweep (2026-06-09 design §2).

The company throttle tally is written unconditionally after a confirmed
send (it records reality and keeps sibling ABM protection intact), so a
failed person advance leaves the entry behind the tally. This sweep
converges every divergent entry toward the tally — source of truth for
what was actually sent — the same day, instead of waiting for the
throttle to trip on the desync.

`advance_fn` is injected (workflows.daily_check passes
`_attio_advance_with_escalation`) to avoid a daily_check import cycle
and to keep the write path's monotonicity / write-owner / terminal-
class guards on every repair.

The 3-day window (not today-only) covers runs killed between the tally
write and the sweep, and weekend gaps.
"""

import sys
from collections.abc import Callable
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from clients.attio import AttioClient
from clients.attio_writer import AttioMonotonicityViolation, AttioTerminalClassRegression
from clients.attio_writer_registry import UnauthorizedAttioWriteError
from clients.outreach_config import load_outreach_config
from models.campaign import MessageStep
from models.resolution import compute_next_eligible_send_date
from workflows.dm_sequencer import NEXT_STAGE, STAGE_FOR_DM
from workflows.escalation import escalate

if TYPE_CHECKING:
    from workflows.audit import AuditLogger

_SWEEP_CONFIG = load_outreach_config()

# How far back the sweep looks for stamped companies. Operator knob
# (config/outreach.yaml `sweep.window_days`, default 3). The window must
# stay <= throttle.company_window_days: the throttle blocks any second
# send to a stamped company inside its window, which is what guarantees
# a stamp can't be overwritten before the sweep sees it.
SWEEP_WINDOW_DAYS = _SWEEP_CONFIG.sweep_window_days

# Each failed repair write can burn AttioWriter's full retry budget
# (~300s). During an Attio outage or with an invalid option value, a
# 3-day window of divergent companies would serialize into hours of
# retries — after this many CONSECUTIVE repair_write_failed outcomes,
# stop attempting repairs and escalate the rest without writes. Operator
# knob (config/outreach.yaml `sweep.repair_circuit_threshold`, default 3).
REPAIR_CIRCUIT_THRESHOLD = _SWEEP_CONFIG.sweep_repair_circuit_threshold

# Per-step dm_step floor a stamped company implies for the stamped
# person's entry. Mirrors the PR-13 throttle self-exemption: an Invite
# (CONNECTION_SENT) stamp carries no DM floor.
_SELF_EXEMPT_REQUIRED_DM_STEP = {
    "DM1": 1,
    "DM2": 2,
    "DM3": 3,
}

_STEP_TITLE_TO_MESSAGE_STEP = {
    "DM1": MessageStep.DM1,
    "DM2": MessageStep.DM2,
    "DM3": MessageStep.DM3,
}

# The floor map and this step map must cover the same titles; drift
# produces a KeyError mid-sweep.
assert set(_STEP_TITLE_TO_MESSAGE_STEP) == set(_SELF_EXEMPT_REQUIRED_DM_STEP)


def _extract_step_title(raw: object) -> str | None:
    """Read the select-attr title from `companies.last_outreach_step`.

    Tolerates every degraded shape Attio (or a manual edit) can produce;
    returns None when unreadable so the caller fails closed."""
    items = raw if isinstance(raw, list) else [raw]
    first = items[0] if items else None
    if not isinstance(first, dict):
        return None
    option = first.get("option")
    if isinstance(option, dict) and option.get("title"):
        return str(option["title"])
    # Flat shapes: {"value": "DM1"} or {"status": "DM1"}.
    for key in ("value", "status", "title"):
        val = first.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def _divergence_payload(divergence: dict, reason: str) -> dict:
    """Build a validated-ready DmPersonAdvanceDesyncPayload dict for the
    normal escalation path (all fields available at the call site)."""
    return {**divergence, "reason": reason}


def _invariant_violation_payload(
    company: dict,
    error_class: str,
    today_iso: str,
) -> dict:
    """Build a validated-ready DmPersonAdvanceDesyncPayload dict for the
    halt-class invariant-violation path.

    The invariant violation is caught in the outer company loop, so
    per-person locals (entry_id, stamped_step, etc.) are not in scope.
    Required fields are sentinel-filled; ``company_id`` is extracted
    from the company dict.
    """
    return {
        "company_id": _company_record_id(company),
        "person_record_id": "",
        "stamped_step": "",
        "stamp_date": today_iso,
        "entry_id": "",
        "person_dm_step": 0,
        "person_stage": "",
        "intended_attrs": {},
        "reason": "invariant_violation",
        "error_class": error_class,
    }


def _first_item(raw: object) -> dict | None:
    items = raw if isinstance(raw, list) else [raw]
    first = items[0] if items else None
    return first if isinstance(first, dict) else None


def _entry_value(entry: dict, slug: str) -> Any:
    """Tolerant read of one attribute value off a list entry, accepting
    the value/option/status shapes Attio returns (mirrors
    throttle._extract_step_title's tolerance)."""
    values = entry.get("entry_values") or entry.get("values") or {}
    first = _first_item(values.get(slug))
    if first is None:
        return None
    if first.get("value") is not None:
        return first["value"]
    for key in ("option", "status"):
        nested = first.get(key)
        if isinstance(nested, dict) and nested.get("title"):
            return nested["title"]
    return first.get("title")


def _company_record_id(company: dict) -> str:
    return (company.get("id") or {}).get("record_id", "")


def run_company_tally_consistency_sweep(
    *,
    attio: AttioClient,
    list_id: str,
    today: date,
    dry_run: bool,
    advance_fn: Callable[..., bool],
    audit_logger: "AuditLogger | None" = None,
    window_days: int = SWEEP_WINDOW_DAYS,
    entries: list[dict] | None = None,
) -> dict:
    """Verify every company stamped within the window against its
    stamped person's list entry; converge divergence toward the tally.

    Returns a summary count dict (echoed in the run summary — no
    silent zeros; a mass-divergence run shows up as a large
    repaired/escalated count the same evening).

    Multi-entry persons: only the highest-ranked entry is checked/repaired
    (same most-advanced-entry convention as add_list_entry); duplicates stay
    under the PR #170 throttle quarantine.

    entries: when not None, this raw list snapshot is reused instead of
    fetching from Attio. Valid only on write-free exits — the caller is
    responsible for not passing a stale snapshot on the post-send path
    (a stale snapshot would show pre-advance dm_step and produce false
    divergences). When None, the function fetches fresh as before.
    """
    summary: dict[str, Any] = {
        "companies_checked": 0,
        "consistent": 0,
        "repaired": 0,
        "escalated": 0,
        "escalate_failed": 0,
        "skipped_no_floor": 0,
        "skipped_malformed_stamp": 0,
        "dry_run_divergent": 0,
        "company_errors": 0,
        "aborted_invariant_violation": None,
        "entries_unparseable": 0,
        "repair_circuit_open": False,
    }
    # Mutable across _check_one_company calls; resets on every
    # successful repair so only an unbroken failure streak opens the
    # circuit (a mid-streak success means writes are landing again).
    circuit = {"consecutive_repair_write_failures": 0}
    cutoff = today - timedelta(days=window_days)
    # SWEEP_WINDOW_DAYS must stay <= throttle.company_window_days: the
    # throttle blocks any second send to a stamped company inside its window,
    # which is what guarantees a stamp can't be overwritten before the sweep
    # sees it.
    # Attio datetime attrs need a full ISO timestamp for $gte comparison.
    companies = attio.search_companies(
        filter_={"last_outreach_at": {"$gte": cutoff.isoformat() + "T00:00:00Z"}},
        limit=500,
    )
    summary["companies_checked"] = len(companies)
    if len(companies) >= 500:
        summary["company_cap_hit"] = True
        if audit_logger is not None:
            audit_logger.event("consistency_sweep_company_cap_hit")
        else:
            print(
                "WARNING: consistency_sweep hit the 500-company search cap — "
                "some companies may have been skipped.",
                file=sys.stderr,
            )
    if not companies:
        return summary

    # One bulk fetch reused across companies (per-company
    # _find_list_entries_for_record would re-query 50k entries each).
    # Fix 1: when entries is provided (write-free run exit), skip the fetch.
    if entries is not None:
        all_entries: list[dict] = entries
    else:
        all_entries = attio.query_list_entries(list_id=list_id, limit=50000)

    # Fix 2: build a one-pass index so _check_one_company can look up a
    # person's entries in O(1) instead of scanning all_entries per company.
    # The key expression mirrors _filter_and_rank_entries_for_record exactly:
    # AttioClient.parse_entry(e).get("record_id") — which reads
    # entry["parent_record_id"] falling back to entry["id"]["record_id"].
    # Any divergence here would silently miss entries and produce false
    # divergences, so we mirror the client method's extraction verbatim.
    entries_by_record: dict[str, list[dict]] = {}
    first_parse_error_echoed = False
    for e in all_entries:
        try:
            rid = AttioClient.parse_entry(e).get("record_id") or ""
        except Exception as exc:  # noqa: BLE001
            summary["entries_unparseable"] += 1
            # Echo the FIRST parse exception once per sweep so a systematic
            # entry-shape drift (which would silently inflate the counter and
            # miss every divergence) is debuggable, not just a bare tally.
            if not first_parse_error_echoed:
                first_parse_error_echoed = True
                print(
                    f"WARNING: consistency sweep could not parse a list entry "
                    f"({type(exc).__name__}: {exc}); "
                    f"see summary['entries_unparseable'] for the full count.",
                    file=sys.stderr,
                )
            continue
        if rid:
            entries_by_record.setdefault(rid, []).append(e)

    for company in companies:
        try:
            _check_one_company(
                company=company,
                entries_by_record=entries_by_record,
                list_id=list_id,
                today=today,
                dry_run=dry_run,
                advance_fn=advance_fn,
                audit_logger=audit_logger,
                summary=summary,
                attio=attio,
                circuit=circuit,
            )
        except (
            UnauthorizedAttioWriteError,
            AttioMonotonicityViolation,
            AttioTerminalClassRegression,
        ) as exc:
            # Programmer-bug class from a repair write: the sweep itself is
            # regressing state. Stop sweeping, open a queue row, surface
            # loudly — but never crash the run epilogue.
            summary["aborted_invariant_violation"] = type(exc).__name__
            if audit_logger is not None:
                audit_logger.event(
                    "consistency_sweep_invariant_violation",
                    error_class=type(exc).__name__,
                    company_id=_company_record_id(company),
                )
            try:
                escalate(
                    type="dm_person_advance_desync",
                    # error_class in the key: two DISTINCT halt-class
                    # violations on the same day must open separate
                    # operator queue rows, not dedupe into one.
                    idempotency_key=(
                        f"dm-advance-desync|invariant-violation|"
                        f"{type(exc).__name__}|{today.isoformat()}"
                    ),
                    payload=_invariant_violation_payload(
                        company=company,
                        error_class=type(exc).__name__,
                        today_iso=today.isoformat(),
                    ),
                    attio=attio,
                )
            except Exception as esc_exc:  # noqa: BLE001
                print(
                    f"WARNING: escalate(dm_person_advance_desync/"
                    f"invariant_violation) failed: {esc_exc}",
                    file=sys.stderr,
                )
            break
        except Exception as exc:  # noqa: BLE001
            # A RAISING advance_fn lands here without feeding the repair
            # circuit counter — only a False return (retry budget
            # exhausted) counts. Raise-class failures fail fast and are
            # already surfaced via company_errors + audit event. Bind the
            # exception so the audit payload + stderr echo name the class +
            # message — a bare "company_error" with no type is undebuggable.
            summary["company_errors"] += 1
            company_id = _company_record_id(company)
            if audit_logger is not None:
                audit_logger.event(
                    "consistency_sweep_company_error",
                    company_id=company_id,
                    error_class=type(exc).__name__,
                    error_msg=str(exc)[:200],
                )
            print(
                f"WARNING: consistency sweep errored on company "
                f"{company_id!r}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            continue

    if audit_logger is not None:
        audit_logger.event("consistency_sweep_summary", **summary)
    return summary


def _check_one_company(
    *,
    company: dict,
    entries_by_record: dict[str, list[dict]],
    list_id: str,
    today: date,
    dry_run: bool,
    advance_fn: Callable[..., bool],
    audit_logger: "AuditLogger | None",
    summary: dict,
    attio: AttioClient,
    circuit: dict,
) -> None:
    """Check and optionally repair one company's tally/person-entry alignment.

    Mutates *summary* and *circuit* in-place. Raises halt-class exceptions
    (AttioMonotonicityViolation, AttioTerminalClassRegression,
    UnauthorizedAttioWriteError) so the caller can break the loop and open
    an operator queue row.

    *circuit* tracks consecutive repair_write_failed outcomes; at
    REPAIR_CIRCUIT_THRESHOLD the breaker opens
    (summary["repair_circuit_open"]) and remaining divergent companies
    are escalated with reason="repair_circuit_open" without any write
    attempt — each failed write can burn the full AttioWriter retry
    budget, so an open-ended failure streak must not serialize across
    the whole window.
    """
    values = company.get("values") or {}
    company_id = _company_record_id(company)

    stamped_step = _extract_step_title(values.get("last_outreach_step"))
    floor = _SELF_EXEMPT_REQUIRED_DM_STEP.get(stamped_step or "")
    if floor is None:
        # Invite stamps (and absent/unknown steps) carry no dm_step
        # floor — same waiver as the PR #170 throttle exemption.
        summary["skipped_no_floor"] += 1
        return
    # floor is not None iff stamped_step was a known DM title key.
    assert stamped_step is not None  # narrowed: get(stamped_step or "") returned non-None

    stamp = _first_item(values.get("last_outreach_person_id"))
    person_record_id = stamp.get("target_record_id") if stamp else None
    raw_ts = _first_item(values.get("last_outreach_at"))
    ts_value = raw_ts.get("value") if raw_ts else None
    if not person_record_id or not ts_value:
        summary["skipped_malformed_stamp"] += 1
        if audit_logger is not None:
            audit_logger.event(
                "consistency_sweep_malformed_stamp",
                company_id=company_id,
            )
        return

    try:
        stamp_date = date.fromisoformat(str(ts_value)[:10])
    except ValueError:
        summary["skipped_malformed_stamp"] += 1
        if audit_logger is not None:
            audit_logger.event(
                "consistency_sweep_malformed_stamp",
                company_id=company_id,
                reason="unparseable_timestamp",
            )
        return

    # Fix 2: use the pre-built index instead of scanning all_entries per company.
    # The index was keyed by the same parse_entry("record_id") expression the
    # client method uses — ranking logic stays in the client method.
    person_entries = attio._filter_and_rank_entries_for_record(
        entries_by_record.get(person_record_id, []), person_record_id
    )
    entry = person_entries[0] if person_entries else None
    raw_dm_step = _entry_value(entry, "dm_step") if entry else None
    try:
        current_dm_step = int(raw_dm_step or 0)
    except (TypeError, ValueError):
        current_dm_step = 0
    current_stage = (
        str(_entry_value(entry, "stage") or "") if entry else ""
    )

    if entry is not None and current_dm_step >= floor:
        summary["consistent"] += 1
        return

    # Divergent: person entry is behind the tally.
    step = _STEP_TITLE_TO_MESSAGE_STEP[stamped_step]
    target_stage = NEXT_STAGE[step]
    expected_prior = STAGE_FOR_DM[step]
    intended_attrs: dict = {
        "dm_step": floor,
        "stage": target_stage.value,
        "last_contact_date": stamp_date.isoformat(),
    }
    # Stamp the converged step's dm{N}_sent_at from the tally date so the
    # sweep-recorded send stays visible to learn.py's per-step denominators
    # (PR-9b NULL gate excludes unstamped rows). Only fill when currently
    # NULL — an existing stamp from the in-run advance is the truer send
    # date and must not be overwritten by a later tally date.
    sent_at_slug = f"{step.value}_sent_at"
    existing_sent_at = _entry_value(entry, sent_at_slug) if entry else None
    if not existing_sent_at:
        intended_attrs[sent_at_slug] = stamp_date.isoformat()
    next_eligible = compute_next_eligible_send_date(
        last_contact_date=stamp_date, just_sent_step=step.value
    )
    if next_eligible is not None:
        computed = next_eligible.isoformat()
        existing_ne = _entry_value(entry, "next_eligible_send_date") if entry else None
        if isinstance(existing_ne, str) and existing_ne > computed:
            computed = existing_ne  # never pull eligibility EARLIER
        intended_attrs["next_eligible_send_date"] = computed

    entry_id = (
        ((entry.get("id") or {}).get("entry_id", "")) if entry else ""
    )
    divergence = {
        "company_id": company_id,
        "person_record_id": person_record_id,
        "stamped_step": stamped_step,
        "stamp_date": stamp_date.isoformat(),
        "entry_id": entry_id,
        "person_dm_step": current_dm_step,
        "person_stage": current_stage,
        "intended_attrs": intended_attrs,
    }

    if dry_run:
        summary["dry_run_divergent"] += 1
        if audit_logger is not None:
            audit_logger.event(
                "consistency_sweep_divergence_dry_run", **divergence
            )
        return

    # Stage safety: only auto-repair when the write cannot regress
    # the stage (pre-advance stage, or target already reached with
    # a lagging dm_step). Anything else (Responded, Nurture, ...)
    # means a human/another path moved the row — operator decides.
    repair_safe = entry is not None and current_stage in {
        expected_prior.value,
        target_stage.value,
    }
    circuit_open = bool(summary["repair_circuit_open"])
    attempted_repair = repair_safe and not circuit_open
    repaired = False
    if attempted_repair:
        # Halt-class exceptions propagate to the caller loop so the sweep
        # stops and opens an operator queue row.
        repaired = advance_fn(
            attio=attio,
            entry_id=entry_id,
            entry_attributes=intended_attrs,
            list_id=list_id,
            linkedin_url="",
            today=stamp_date.isoformat(),
            step_label=step.value,
            writer_module=(
                "workflows.consistency_sweep."
                "run_company_tally_consistency_sweep"
            ),
            prior_stage=current_stage or expected_prior.value,
            person_record_id=person_record_id,
            audit_logger=audit_logger,
            escalate_failures=None,
        )
    if repaired:
        circuit["consecutive_repair_write_failures"] = 0
        summary["repaired"] += 1
        if audit_logger is not None:
            audit_logger.event(
                "consistency_sweep_repaired", **divergence
            )
        return

    if attempted_repair:
        circuit["consecutive_repair_write_failures"] += 1
        if (
            circuit["consecutive_repair_write_failures"]
            >= REPAIR_CIRCUIT_THRESHOLD
        ):
            summary["repair_circuit_open"] = True
            if audit_logger is not None:
                audit_logger.event(
                    "consistency_sweep_repair_circuit_open",
                    consecutive_failures=circuit[
                        "consecutive_repair_write_failures"
                    ],
                    company_id=company_id,
                )

    reason = (
        "repair_write_failed"
        if attempted_repair
        else (
            "repair_circuit_open"
            if repair_safe  # would have repaired, but the breaker is open
            else ("no_list_entry" if entry is None else "stage_unsafe")
        )
    )
    # Increment and audit AFTER a successful escalate() call so a
    # raising escalate doesn't inflate the count (and the audit event
    # is only emitted when the queue row actually landed). Best-effort
    # (mirrors the invariant-violation escalate at the per-company loop):
    # a transient escalate failure must not crash the sweep epilogue. A
    # swallowed failure is counted in summary["escalate_failed"] so the
    # epilogue warn surfaces the queue gap.
    try:
        escalate(
            type="dm_person_advance_desync",
            idempotency_key=(
                f"dm-advance-desync|{entry_id or person_record_id}|"
                f"{stamp_date.isoformat()}"
            ),
            payload=_divergence_payload(divergence, reason),
            attio=attio,
        )
    except Exception as esc_exc:  # noqa: BLE001 — must not crash the sweep
        summary["escalate_failed"] += 1
        print(
            f"WARNING: escalate(dm_person_advance_desync) failed for "
            f"{entry_id or person_record_id!r}: "
            f"{type(esc_exc).__name__}: {esc_exc}",
            file=sys.stderr,
        )
        return
    summary["escalated"] += 1
    if audit_logger is not None:
        audit_logger.event(
            "consistency_sweep_escalated", reason=reason, **divergence
        )
