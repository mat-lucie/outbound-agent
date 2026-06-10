#!/usr/bin/env python3
"""Backfill dm1_sent_at, dm2_sent_at, dm3_sent_at on LinkedIn Outreach entries.

# Why this exists

PR-9a added dm1_sent_at / dm2_sent_at / dm3_sent_at to the LinkedIn Outreach
schema. run_dm_sequencing now stamps them on every new DM send. Historical
rows — all entries that reached DM1/DM2/DM3 before PR-9a shipped — carry
NULL. PR-10's _per_step_rates excludes NULL rows from Bayesian denominators
to avoid fabricated data. This backfill recovers timestamps from three
prioritised sources so the pre-PR-9a cohort is not permanently excluded.

# Source-priority chain (§0 #9 invariant: no silent fallbacks)

For each dmN_sent_at that is NULL on a winner row, attempt in order:

  1. PB action history — ~/.outbound-agent/pb_history/ JSONL files (if present).
     Each file is a snapshot of PhantomBuster Message Sender container logs
     with per-URL send timestamps. Confidence: HIGH.

  2. Attio note created_at — list_notes_for_record() for the parent person.
     Notes with title matching "DM 1 sent" / "DM 2 sent" / "DM 3 sent" give
     the note's created_at as a proxy for the actual send. Confidence: MEDIUM.

  3. last_contact_date fallback — if the entry has a last_contact_date and
     the entry is at a DM-sent stage, use noon-UTC of that date as the
     timestamp for the HIGHEST dm step reached. Earlier steps get None unless
     PB/notes supplied them. Confidence: LOW.

If all three sources fail for a given dmN, the field stays NULL. This is
NOT a bug — NULL is the correct output per §0 #9: "No silent fallbacks."

# Skipped rows

- Rows with merged_into set (PR-9.5 soft-deleted losers) — examined but NOT
  modified. These rows are excluded from the active pipeline; writing to them
  wastes Attio quota and could confuse forensics scans.

# Idempotency (§9.4)

A row where all three timestamps already have values is skipped
(rows_skipped_idempotent). A second run logs rows_modified == 0.

# Run rows

Opens ReclassificationRunWriter first (§3.12: captures inference confidence
distribution). Passes rec.run_id into MigrationRunWriter via the
`reclassification_run_id=` constructor arg (§3.13: captures rollback pointer
and per-record last_migrated_by stamps). The two run rows are correlated
via the `reclassification_run_id` text field on the Migration Run row —
operators querying Migration Run rows can join back to Reclassification Run
to recover the per-row confidence/abstain/error breakdown.

# Note-tier disambiguation caveat

`_timestamps_from_notes` uses note TITLE matching alone to disambiguate
which DM step a note refers to. A note titled "DM 1 sent" on a non-DM
event WOULD be mis-attributed — this is why the notes tier carries
MEDIUM (not HIGH) confidence. The `_NOTE_TITLE_TO_STEP` patterns
currently have NO writer in the codebase (no automation emits these
note titles); the tier is anticipating future automation or manually-
authored operator notes.

# Exit codes

  0  — success (all examined rows modified or skipped; 0 failures)
  1  — partial failure (some rows failed; see failure_details on MigrationRun)
  75 — EX_TEMPFAIL: Attio MCP scope failure (cannot list entries)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from clients.attio import AttioClient  # noqa: E402
from workflows.migration_run_writer import MigrationRunWriter  # noqa: E402
from workflows.reclassification_run_writer import ReclassificationRunWriter  # noqa: E402

# ── Exit codes ──────────────────────────────────────────────────────────────

EX_OK = 0
EX_PARTIAL = 1
EX_TEMPFAIL = 75  # POSIX EX_TEMPFAIL — transient/scope failure

# ── DM step → dm_step slug values ──────────────────────────────────────────

_DM_STAGE_TITLES = {"DM1 Sent", "DM2 Sent", "DM3 Sent"}

# Which dm_step values imply at least N DMs were sent.
_STEP_MIN_SENT: dict[int, set[str]] = {
    1: {"dm1", "dm2", "dm3"},
    2: {"dm2", "dm3"},
    3: {"dm3"},
}

# ── PB history helpers ──────────────────────────────────────────────────────

PB_HISTORY_DIR = Path.home() / ".outbound-agent" / "pb_history"


def _load_pb_history(
    counters: dict[str, int] | None = None,
) -> dict[str, dict[str, str]]:
    """Load PB history JSONL files from PB_HISTORY_DIR.

    Returns {normalized_url: {dm1: iso_ts, dm2: iso_ts, dm3: iso_ts}}
    where keys are only present when a timestamp was recorded.

    Each JSONL line must have shape:
      {"url": <str>, "step": <"dm1"|"dm2"|"dm3">, "sent_at": <iso8601>}

    PR-9b fold-in (silent-failure MED-5): if `counters` is supplied, this
    function writes `pb_lines_examined` and `pb_lines_skipped_malformed`
    so operators can detect when a PB export schema change causes silent
    under-coverage. Unreadable PB files are logged on stderr (MED-4) so
    a transient FS issue isn't invisible.

    Unknown shapes are skipped (counted in pb_lines_skipped_malformed) —
    the file format is best-effort forensic data; missing or malformed
    lines don't abort the run.
    """
    if not PB_HISTORY_DIR.exists():
        return {}
    history: dict[str, dict[str, str]] = {}
    for path in sorted(PB_HISTORY_DIR.glob("*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            # PR-9b fold-in (silent-failure MED-4): surface unreadable PB
            # history files so a transient FS issue isn't invisible.
            print(
                f"WARNING: PB history file {str(path)!r} unreadable — "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if counters is not None:
                counters["pb_lines_examined"] = counters.get("pb_lines_examined", 0) + 1
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                if counters is not None:
                    counters["pb_lines_skipped_malformed"] = (
                        counters.get("pb_lines_skipped_malformed", 0) + 1
                    )
                continue
            url = rec.get("url", "").strip() if isinstance(rec, dict) else ""
            step = rec.get("step", "").strip() if isinstance(rec, dict) else ""
            sent_at = rec.get("sent_at", "").strip() if isinstance(rec, dict) else ""
            if not url or step not in ("dm1", "dm2", "dm3") or not sent_at:
                if counters is not None:
                    counters["pb_lines_skipped_malformed"] = (
                        counters.get("pb_lines_skipped_malformed", 0) + 1
                    )
                continue
            entry = history.setdefault(url, {})
            # Keep the EARLIEST timestamp per step (first actual send wins).
            if step not in entry or sent_at < entry[step]:
                entry[step] = sent_at
    return history


def _normalize_url(url: str | None) -> str:
    """Return a comparable form of a LinkedIn URL (lowercase, trailing-slash stripped)."""
    if not url:
        return ""
    return url.strip().rstrip("/").lower()


# ── Attio note helpers ──────────────────────────────────────────────────────

_NOTE_TITLE_TO_STEP: dict[str, int] = {
    "dm 1 sent": 1,
    "dm1 sent": 1,
    "dm 2 sent": 2,
    "dm2 sent": 2,
    "dm 3 sent": 3,
    "dm3 sent": 3,
}


def _timestamps_from_notes(
    attio: AttioClient, record_id: str
) -> dict[str, str | None]:
    """Look up dm1/dm2/dm3 send timestamps from Attio notes on this person.

    Searches note titles (case-insensitive) for "DM N sent" patterns and
    uses the note's created_at as a proxy for the actual send time.
    Returns {dm1_sent_at: ..., dm2_sent_at: ..., dm3_sent_at: ...} where
    values are ISO8601 strings or None when no matching note was found.

    Title-matching is the SOLE disambiguator — a note titled "DM 1 sent"
    on a non-DM event would be misattributed. This is why the notes
    tier is MEDIUM (not HIGH) confidence in the source-priority chain.

    PR-9b fold-in (silent-failure HIGH-1): if list_notes_for_record
    raises (transient Attio outage), log on stderr so the operator can
    see that ALL notes-tier rows are silently demoted to LCD/none
    confidence. Without this, an outage looks identical to "no notes
    matched" — confidence_low/none rows silently inflate.
    """
    result: dict[str, str | None] = {
        "dm1_sent_at": None,
        "dm2_sent_at": None,
        "dm3_sent_at": None,
    }
    try:
        notes = attio.list_notes_for_record(record_id, parent_object="people", limit=50)
    except Exception as exc:  # noqa: BLE001
        print(
            f"WARNING: list_notes_for_record({record_id!r}) failed — "
            f"{type(exc).__name__}: {exc}; falling through to "
            f"last_contact_date for this row",
            file=sys.stderr,
        )
        return result
    for note in notes:
        title: str = ""
        if isinstance(note, dict):
            title_field = note.get("title") or note.get("headline") or ""
            if isinstance(title_field, list) and title_field:
                title = str(title_field[0].get("value", "")) if isinstance(title_field[0], dict) else str(title_field[0])
            elif isinstance(title_field, str):
                title = title_field
        step_n = _NOTE_TITLE_TO_STEP.get(title.strip().lower())
        if step_n is None:
            continue
        key = f"dm{step_n}_sent_at"
        created_at = note.get("created_at") or note.get("created_at_dt")
        if created_at and result[key] is None:
            result[key] = str(created_at)
    return result


# ── last_contact_date fallback helper ───────────────────────────────────────


def _noon_utc(d: date) -> str:
    """Return an ISO8601 UTC datetime at noon on the given date."""
    return datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=UTC).isoformat()


def _date_from_str(raw: str | None) -> date | None:
    """Parse an Attio date/datetime string to a date.date. Returns None on failure."""
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _timestamps_from_last_contact(
    entry: dict,
) -> dict[str, str | None]:
    """Fallback: use last_contact_date (noon UTC) as the timestamp for the
    HIGHEST dm step reached by the entry. Earlier steps get None.

    §0 #9: We only populate the step implied by the current stage — we
    cannot fabricate earlier steps without a source.
    """
    result: dict[str, str | None] = {
        "dm1_sent_at": None,
        "dm2_sent_at": None,
        "dm3_sent_at": None,
    }
    lcd = _date_from_str(entry.get("last_contact_date"))
    if lcd is None:
        return result
    dm_step_val: str = str(entry.get("dm_step") or "").strip().lower()
    # Determine which is the highest step from dm_step or stage.
    if dm_step_val in ("dm3",):
        result["dm3_sent_at"] = _noon_utc(lcd)
    elif dm_step_val in ("dm2",):
        result["dm2_sent_at"] = _noon_utc(lcd)
    elif dm_step_val in ("dm1",):
        result["dm1_sent_at"] = _noon_utc(lcd)
    else:
        # Fall back to stage title.
        stage: str = str(entry.get("stage") or "").strip()
        if stage == "DM3 Sent":
            result["dm3_sent_at"] = _noon_utc(lcd)
        elif stage == "DM2 Sent":
            result["dm2_sent_at"] = _noon_utc(lcd)
        elif stage == "DM1 Sent":
            result["dm1_sent_at"] = _noon_utc(lcd)
    return result


# ── Per-row inference engine ─────────────────────────────────────────────────

# PR-9b fold-in (5-agent convergence IMP #3): "skipped" is the distinct
# bucket for already-fully-populated rows — they bypass inference entirely
# and shouldn't inflate the confidence_high counter (which is reserved for
# rows that actually used the PB tier). "none" is reserved for rows where
# all three inference tiers failed to produce a value (§0 #9 NULL output).
InferenceConfidence = Literal["high", "medium", "low", "none", "skipped"]

# Confidence "worst-used" ordering — when a single row mixes sources
# (e.g., PB fills dm1, notes fill dm2), report the LOWEST source actually
# consumed so the per-row summary doesn't overstate. "skipped" is treated
# as out-of-band (never compared).
_CONFIDENCE_RANK: dict[InferenceConfidence, int] = {
    "high": 3,
    "medium": 2,
    "low": 1,
    "none": 0,
    "skipped": -1,
}


def _worst_of(
    a: InferenceConfidence, b: InferenceConfidence,
) -> InferenceConfidence:
    """Return the worst (lowest-rank) of two confidence levels, ignoring 'none'.

    When fold-in stamps a slot via a fallback tier after a higher tier
    already contributed, the row's reported confidence must downgrade to
    the worst tier used. 'none' means 'not yet contributed' and is
    treated as ineligible — it can't downgrade a real source."""
    if a == "none":
        return b
    if b == "none":
        return a
    return a if _CONFIDENCE_RANK[a] <= _CONFIDENCE_RANK[b] else b


def _infer_timestamps(
    entry: dict,
    pb_history: dict[str, dict[str, str]],
    attio: AttioClient | None,
    *,
    dry_run: bool,
) -> tuple[dict[str, str], InferenceConfidence]:
    """Infer missing dmN_sent_at timestamps for a single entry.

    Returns (updates, confidence) where:
    - updates: {dm1_sent_at: ..., dm2_sent_at: ..., dm3_sent_at: ...}
      containing ONLY the keys that were NULL and now have a value.
      Values are always non-None strings — the dict only carries slots
      where inference actually produced a timestamp. Keys that remain
      NULL are absent from updates (do NOT write NULL over NULL — that
      would count as a modification).
    - confidence: "high" (PB only), "medium" (PB + notes mix or notes
      only), "low" (anything mixed with lcd or lcd-only), "none" (all
      failed), "skipped" (row was already fully populated, no inference
      ran).

    PR-9b fold-in (5-agent convergence IMP #3):
      - Mixed-source rows downgrade to the worst tier actually used
        (`_worst_of`). PB fills dm1, notes fill dm2 → "medium".
      - Already-set rows return "skipped" so their counter is distinct
        from the "high" rows that actually consumed PB data.

    §0 #9: If all sources fail, updates is empty and confidence is "none".
    This is the explicit NULL-output contract.
    """
    current = {
        "dm1_sent_at": entry.get("dm1_sent_at"),
        "dm2_sent_at": entry.get("dm2_sent_at"),
        "dm3_sent_at": entry.get("dm3_sent_at"),
    }

    # Which slots need filling?
    needs = {k for k, v in current.items() if not v}
    if not needs:
        # Idempotent — caller will skip_idempotent. Distinct from "high"
        # so the confidence counters reflect actual inference outcomes.
        return {}, "skipped"

    updates: dict[str, str] = {}
    used_confidence: InferenceConfidence = "none"

    # --- Priority 1: PB history ---
    url = _normalize_url(entry.get("canonical_linkedin_url") or entry.get("linkedin_url"))
    pb_data = pb_history.get(url, {}) if url else {}

    pb_updates: dict[str, str] = {}
    for slot in needs:
        step_key = slot.replace("_sent_at", "")  # "dm1" etc.
        if step_key in pb_data:
            pb_updates[slot] = pb_data[step_key]

    if pb_updates:
        updates.update(pb_updates)
        used_confidence = _worst_of(used_confidence, "high")
        needs -= set(pb_updates.keys())

    if not needs:
        return updates, used_confidence

    # --- Priority 2: Attio notes ---
    if attio is not None and not dry_run:
        record_id = entry.get("record_id", "")
        if record_id:
            note_timestamps = _timestamps_from_notes(attio, record_id)
            note_updates: dict[str, str] = {}
            for slot in needs:
                val = note_timestamps.get(slot)
                if val:
                    note_updates[slot] = val
            if note_updates:
                updates.update(note_updates)
                used_confidence = _worst_of(used_confidence, "medium")
                needs -= set(note_updates.keys())

    if not needs:
        return updates, used_confidence

    # --- Priority 3: last_contact_date fallback ---
    lcd_timestamps = _timestamps_from_last_contact(entry)
    lcd_updates: dict[str, str] = {}
    for slot in needs:
        val = lcd_timestamps.get(slot)
        if val:
            lcd_updates[slot] = val
    if lcd_updates:
        updates.update(lcd_updates)
        used_confidence = _worst_of(used_confidence, "low")
        needs -= set(lcd_updates.keys())

    # Any remaining slots in `needs` have no source — stay NULL per §0 #9.
    return updates, used_confidence


# ── Main backfill logic ───────────────────────────────────────────────────────


def _backfill_timestamps(
    attio: AttioClient,
    rec_run: ReclassificationRunWriter,
    mig_run: MigrationRunWriter,
    *,
    list_id: str,
    dry_run: bool,
) -> dict:
    """Walk all active list entries and backfill dmN_sent_at.

    Returns a summary dict for operator logging.

    PR-9b fold-in (silent-failure HIGH-2): the list-entries query is
    wrapped in a NARROW except clause covering only Attio-specific
    transient errors. TypeError/AttributeError/KeyError/ImportError
    propagate as code bugs — silently converting those to EX_TEMPFAIL
    would hide real defects.
    """
    try:
        raw_entries = attio.query_list_entries(list_id=list_id, limit=50000)
    except (
        httpx.HTTPError,
        httpx.HTTPStatusError,
        httpx.ConnectError,
        httpx.ReadTimeout,
        httpx.ReadError,
        ConnectionError,
        TimeoutError,
        OSError,
    ) as exc:
        raise RuntimeError(f"failed to query list entries: {exc}") from exc

    parsed_entries = [AttioClient.parse_entry(e) for e in raw_entries]

    # PR-9b fold-in (silent-failure MED-5): PB line counters track
    # examined/skipped to detect silent under-coverage from PB schema drift.
    pb_counters: dict[str, int] = {
        "pb_lines_examined": 0,
        "pb_lines_skipped_malformed": 0,
    }
    pb_history = _load_pb_history(counters=pb_counters)

    summary: dict[str, int] = {
        "rows_examined": 0,
        "rows_skipped_soft_deleted": 0,
        "rows_skipped_idempotent": 0,
        "rows_modified": 0,
        "rows_failed": 0,
        "confidence_high": 0,
        "confidence_medium": 0,
        "confidence_low": 0,
        "confidence_none": 0,
        # PR-9b fold-in (5-agent convergence IMP #3): already-set rows
        # are tracked separately so they don't inflate confidence_high.
        "confidence_skipped": 0,
        "pb_lines_examined": pb_counters["pb_lines_examined"],
        "pb_lines_skipped_malformed": pb_counters["pb_lines_skipped_malformed"],
    }

    for entry in parsed_entries:
        mig_run.examine()
        rec_run.examine()
        summary["rows_examined"] += 1

        # PR-9b fold-in (5-agent convergence IMP #4): soft-deleted losers
        # are out-of-scope, not "already at target value" — use
        # skip_excluded so the Migration Run row reflects the correct
        # semantic, and rec_run.mark_abstain so its batch_size accounting
        # stays balanced (batch_size == success + abstain + error).
        if entry.get("merged_into"):
            mig_run.skip_excluded(reason="soft_deleted_loser")
            rec_run.mark_abstain(
                record_id=entry.get("entry_id", ""),
                reason="soft_deleted_loser",
            )
            summary["rows_skipped_soft_deleted"] += 1
            continue

        entry_id = entry.get("entry_id", "")
        record_id = entry.get("record_id", "")

        # Idempotency check: all three already set → skip.
        already_set = (
            entry.get("dm1_sent_at")
            and entry.get("dm2_sent_at")
            and entry.get("dm3_sent_at")
        )
        if already_set:
            mig_run.skip_idempotent()
            rec_run.mark_abstain(
                record_id=entry_id,
                reason="all_timestamps_already_set",
            )
            summary["rows_skipped_idempotent"] += 1
            # PR-9b fold-in IMP #3b: also track the "skipped" confidence
            # bucket so the per-row summary breakdown is exhaustive.
            summary["confidence_skipped"] += 1
            continue

        updates, confidence = _infer_timestamps(
            entry, pb_history, attio, dry_run=dry_run
        )

        summary[f"confidence_{confidence}"] += 1

        if not updates:
            # All sources failed — explicit NULL output per §0 #9.
            rec_run.mark_abstain(
                record_id=entry_id,
                reason="all_sources_failed",
            )
            continue

        if dry_run:
            # In dry-run, count the would-be modification for reporting.
            mig_run.mark_modified(record_id=entry_id, object="schema")
            rec_run.mark_success(record_id=entry_id)
            summary["rows_modified"] += 1
            continue

        # Apply the updates.
        try:
            attio.update_list_entry(
                entry_id=entry_id,
                entry_attributes=updates,
                list_id=list_id,
            )
        except Exception as exc:  # noqa: BLE001
            mig_run.mark_failed(record_id=entry_id, error=exc)
            rec_run.mark_error(record_id=entry_id, error=exc)
            summary["rows_failed"] += 1
            continue

        # Back-pointer stamp: last_migrated_by targets the parent person record.
        mig_run.mark_modified(
            record_id=record_id or entry_id,
            object="people" if record_id else "schema",
        )
        rec_run.mark_success(record_id=record_id or entry_id)
        summary["rows_modified"] += 1

    return summary


# ── CLI entry point ───────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would be written without touching Attio (default if neither flag given).",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Write inferred timestamps to Attio and emit Migration / Reclassification Run rows.",
    )
    parser.add_argument(
        "--list-id",
        default=os.environ.get("ATTIO_LIST_ID", ""),
        help="LinkedIn Outreach list id (default: ATTIO_LIST_ID env).",
    )
    args = parser.parse_args(argv)

    # Default to dry-run if neither flag is set.
    dry_run = not args.apply

    if not args.list_id:
        print(
            "error: --list-id required (or set ATTIO_LIST_ID env)",
            file=sys.stderr,
        )
        return 2

    try:
        attio = AttioClient()
    except Exception as exc:  # noqa: BLE001
        print(f"error: failed to initialise Attio client: {exc}", file=sys.stderr)
        return EX_TEMPFAIL

    with ReclassificationRunWriter(
        classifier_module="scripts.backfill_per_step_timestamps",
        model_used="none",  # no LLM — rule-based inference only
        input_attr="canonical_linkedin_url,last_contact_date,dm_step",
        output_attr="dm1_sent_at,dm2_sent_at,dm3_sent_at",
        dry_run=dry_run,
        attio=attio,
    ) as rec_run, MigrationRunWriter(
        script_name="scripts/backfill_per_step_timestamps.py",
        rollback_script_path="scripts/rollback_per_step_timestamps.py",
        dry_run=dry_run,
        attio=attio,
        audit_log_path=str(
            Path.home() / ".outbound-agent" / "audit" / "backfill_per_step_timestamps.jsonl"
        ),
        # PR-9b fold-in BLOCKING #1: pass the rec_run.run_id so the
        # Migration Run row carries a real cross-reference to its
        # Reclassification Run row. The two are joined for forensics
        # via the reclassification_run_id text field.
        reclassification_run_id=rec_run.run_id,
    ) as mig_run:
        try:
            summary = _backfill_timestamps(
                attio,
                rec_run,
                mig_run,
                list_id=args.list_id,
                dry_run=dry_run,
            )
        except RuntimeError as exc:
            if "failed to query list entries" in str(exc):
                print(f"error: Attio scope failure — {exc}", file=sys.stderr)
                return EX_TEMPFAIL
            raise

    print(
        f"backfill_per_step_timestamps (dry_run={dry_run}): {summary}; "
        f"rows_examined={mig_run.rows_examined} "
        f"rows_modified={mig_run.rows_modified} "
        f"rows_skipped_idempotent={mig_run.rows_skipped_idempotent} "
        f"rows_skipped_soft_deleted={summary.get('rows_skipped_soft_deleted', 0)} "
        f"rows_failed={mig_run.rows_failed} "
        f"pb_lines_examined={summary.get('pb_lines_examined', 0)} "
        f"pb_lines_skipped_malformed={summary.get('pb_lines_skipped_malformed', 0)} "
        f"rec_run_id={rec_run.run_id} "
        f"mig_run_id={mig_run.run_id}"
    )

    return EX_PARTIAL if mig_run.rows_failed else EX_OK


if __name__ == "__main__":
    sys.exit(main())
