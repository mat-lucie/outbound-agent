"""Language resolution + cadence math helpers (PR-12).

This module collects the typed contracts that prospect-daily uses to
turn Attio attribute dicts into safe, well-typed values for sending DMs:

  * resolve_language(attrs) -> Language
      Raises MissingLanguageError when the language attribute is null,
      empty, or not a member of the Language enum. NO silent fallback.

  * compute_next_eligible_send_date(last_contact_date, just_sent_step) -> date | None
      Computes the canonical "earliest legitimate send" floor as a
      stored Attio attribute. Uses advance_off_weekend (forward-only)
      so the cadence floor is never SHORTENED by a weekend collision.

# Why this module exists

Plan §3.15 names `models.resolution.resolve_language` as the PRIMARY
write-owner for `linkedin_outreach.language`; the existing PROSPECT-commit
path (workflows.weekly_prospect._build_prospect_entry_attrs) is the
secondary writer. Both paths must agree on what a "valid language" is,
so the validation lives here.

# B-PD-001 root cause

`Language(attrs.get("language", "en"))` in workflows.daily_check at the
connection-note and DM render sites silently defaulted Mexican prospects
with no language attribute to English. The §0 #9 invariant — "missing
data → None or typed error" — forbids the silent default.
resolve_language raises MissingLanguageError instead; the caller catches
it, opens a `missing_language` Operator Review Queue row, and skips the
prospect for the current run.
"""

from __future__ import annotations

from datetime import date, timedelta

from clients.outreach_config import load_outreach_config
from models.business_calendar import advance_off_weekend
from models.enums import Language


class MissingLanguageError(Exception):
    """Raised by `resolve_language()` when the language attribute is
    missing, null, empty, or not a valid `Language` enum value.

    Carries structured fields so the caller's escalation payload can
    surface every triage axis without parsing a message string. Shape
    mirrors PR-16's `MissingMessageError` (sans `variant`, which is a
    message-level concept not relevant to language resolution).
    """

    def __init__(
        self,
        *,
        persona: str | None = None,
        language: str | None = None,
        dm_step: str | None = None,
        record_id: str | None = None,
    ) -> None:
        self.persona = persona
        self.language = language
        self.dm_step = dm_step
        self.record_id = record_id
        super().__init__(self._format())

    def _format(self) -> str:
        parts: list[str] = []
        if self.persona is not None:
            parts.append(f"persona={self.persona!r}")
        parts.append(f"language={self.language!r}")
        if self.dm_step is not None:
            parts.append(f"dm_step={self.dm_step!r}")
        if self.record_id is not None:
            parts.append(f"record_id={self.record_id!r}")
        return "missing or invalid language: " + ", ".join(parts)


# Acceptable values for the Attio `language` attribute, derived from the
# Language enum. Module-level frozenset so the hot path is allocation-free.
_VALID_LANGUAGE_CODES: frozenset[str] = frozenset(lang.value for lang in Language)


def resolve_language(
    attrs: dict,
    *,
    persona: str | None = None,
    dm_step: str | None = None,
) -> Language:
    """Resolve the language to use for an outreach message.

    Reads the `language` field from an Attio entry's attribute dict and
    returns the corresponding `Language` enum member. The pre-PR-12
    "default to English on absence" behavior
    (`Language(attrs.get("language", "en"))`) is GONE — missing or
    unrecognized language values raise `MissingLanguageError` instead.

    Args:
        attrs: Attio list-entry attribute dict (the dict shape that
            `_get_all_entries_parsed` yields). Reads `language` and
            `record_id`; both optional from the dict's perspective.
        persona: optional persona key for richer error payloads. Not
            required for resolution itself — language is independent
            of persona.
        dm_step: optional step label (e.g. "dm1", "connection_note")
            for error payload context.

    Returns:
        The matching `Language` enum member.

    Raises:
        MissingLanguageError: language attribute is missing, empty, or
            not a member of the Language enum. Carries structured fields
            for operator triage.
    """
    raw = attrs.get("language")
    record_id = attrs.get("record_id")

    if isinstance(raw, Language):
        return raw
    if raw is None or raw == "":
        raise MissingLanguageError(
            persona=persona,
            language=None,
            dm_step=dm_step,
            record_id=record_id,
        )
    if not isinstance(raw, str) or raw not in _VALID_LANGUAGE_CODES:
        raise MissingLanguageError(
            persona=persona,
            language=str(raw),
            dm_step=dm_step,
            record_id=record_id,
        )
    return Language(raw)


# Days from the just-sent step's send date (which becomes the new
# `last_contact_date`) to the next step's eligibility window opening.
# Keyed by JUST-SENT step (vs dm_sequencer.DM_TIMING's next-step keying) so the
# math reads in the caller's direction ("I just sent DMn — when can DMn+1 fire?").
# Derives from the SAME config/outreach.yaml cadence scalars as DM_TIMING, so
# _NEXT_STEP_DELTA_DAYS["dm1"] == DM_TIMING[DM2] and ["dm2"] == DM_TIMING[DM3]
# hold by construction (drift-guard in test_pr12_weekend_shift.py).
_OUTREACH = load_outreach_config()
_NEXT_STEP_DELTA_DAYS: dict[str, int] = {
    "dm1": _OUTREACH.cadence_dm1_to_dm2_days,   # DM1 -> DM2: == DM_TIMING[DM2]
    "dm2": _OUTREACH.cadence_dm2_to_dm3_days,   # DM2 -> DM3: == DM_TIMING[DM3]
    # DM3 has no next step in the v1 cadence. NURTURE re-engagement
    # (PR-39) writes `nurture_re_eligible_at`, NOT
    # `next_eligible_send_date`.
}


def compute_next_eligible_send_date(
    last_contact_date: date,
    just_sent_step: str,
) -> date | None:
    """Compute the `next_eligible_send_date` stored attribute for a row
    that just received the given DM step.

    Args:
        last_contact_date: the new `last_contact_date` value (== today
            for a row whose DM just confirmed-send).
        just_sent_step: lowercase step label — "dm1", "dm2", or "dm3".
            Other values return None.

    Returns:
        The earliest weekday on which the NEXT DM can fire, or `None`
        when there is no next step (DM3 terminal in v1 cadence).

    Weekend shift semantics:
        Uses `advance_off_weekend` (Sat -> Mon, Sun -> Mon). The cadence
        floor stored in Attio is forward-only by construction — a Sat
        target moves +2 days, never -1. The existing
        `dm_sequencer.earliest_send_date` uses `shift_off_weekend` (Sat
        -> Fri) for its in-memory eligibility check, which can produce
        a date one day EARLIER than the floor we store here. Consumers
        that read the stored attribute therefore see a strictly safer
        (later-or-equal) eligibility date than the local computation.

    Holiday calendar:
        Not handled in v1. MX/PE/CO public holidays falling on the
        computed target weekday are NOT detected by this function; a
        future PR will add holiday-aware logic and open a
        `configuration_decision` queue row (decision_key=`cadence_policy`)
        on first encounter of an unresolvable date. The seam is the
        public escalation type registered in workflows.escalation_schemas.
    """
    delta = _NEXT_STEP_DELTA_DAYS.get(just_sent_step)
    if delta is None:
        return None
    target = last_contact_date + timedelta(days=delta)
    return advance_off_weekend(target)
