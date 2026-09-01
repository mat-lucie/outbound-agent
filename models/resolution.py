"""Language resolution + cadence math helpers (PR-12).

This module collects the typed contracts that prospect-daily uses to
turn Attio attribute dicts into safe, well-typed values for sending DMs:

  * resolve_language(attrs) -> Language
      Raises MissingLanguageError when the language attribute is null,
      empty, or not a member of the Language enum. NO silent fallback.

  * resolve_language_with_source(attrs, ...) -> (Language, LanguageSource)
      Same resolution, plus WHY that language was chosen. The source is
      computed in memory from signals the caller already holds — it is
      never stored on the CRM, so there is nothing to keep in sync.

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

# Person-level override (HQ is the wrong key for multinationals)

Company HQ country seeds the stored entry `language`
(`scripts/backfill_language.py`) and is the ONLY corroborating signal the
send-time guard has. That key is wrong for LATAM-based staff of non-LATAM
multinationals: a Portuguese-speaking director at a company whose true HQ
is in Europe resolves to an HQ-derived expectation of EN. Backfilling
company HQ country makes those rows look CORROBORATED while being wrong.

`people.language` (a per-person select on the `people` object) is the fix:
an explicit per-person truth that outranks any company-derived inference.
Precedence, highest first:

    1. person override  — `people.language`, set by a human who checked
    2. company HQ       — the HQ-derived expectation, when it is ES/PT
    3. lane default     — the stored entry value nothing corroborates

The override is a NARROW exception list: it stays empty for the
overwhelming majority of people and is set only where HQ or location gets
it wrong. An empty override is not a data gap — it means "the inferred
language was never contradicted".
"""

from __future__ import annotations

from datetime import date, timedelta
from enum import Enum

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


class LanguageSource(Enum):
    """WHY a resolved language was chosen.

    Surfaced in the send-dms dry-run so the operator's language check
    reads as a reason rather than a bare "lane-default" flag. Computed in
    memory on every resolve from signals the caller already holds; never
    written to the CRM, so it cannot go stale.

    Values are the operator-facing labels printed in the dry-run line.
    """

    PERSON_OVERRIDE = "person-override"
    """`people.language` was set. A human checked this person; it wins."""

    COMPANY_HQ = "company-hq"
    """The linked company's HQ country implies ES or PT, and the stored
    language agrees. Real corroboration — HQ maps to a LATAM language
    only for LATAM country codes."""

    COMPANY_HQ_DISAGREES = "company-hq-disagrees"
    """The company's HQ country implies ES or PT and the stored language
    is the OTHER LATAM language (es↔pt) — e.g. a Brazilian GM at a
    Mexico-HQ company. Never a send-blocker: person-level truth outranks
    company HQ, so `language_mismatch_verdict` deliberately lets es↔pt
    pass. But it is not corroboration either, so it is reported honestly
    and left in the unverified set. Recording the correct value in
    `people.language` is what resolves it permanently."""

    COMPANY_HQ_CATCHALL = "company-hq-catchall"
    """The company HAS an HQ country, but it maps to EN. Not corroboration:
    `detect_language_from_country` returns "en" for EVERY non-LATAM code,
    so this bucket cannot distinguish a genuine English expectation from
    a Portuguese-speaking LATAM director at a European-HQ multinational.
    Treated as unverified, like LANE_DEFAULT, so backfilling company HQ
    country can never silence the operator warning on these rows."""

    LANE_DEFAULT = "lane-default"
    """No person override and no usable HQ country — the stored language
    is whatever the qualifier's location heuristic guessed. Unverified."""

    OVERRIDE_UNUSABLE = "override-unusable"
    """`people.language` IS set, but to a value the copy library cannot
    render — the CRM select may offer codes with no templates behind
    them. Without this source such a row would be indistinguishable from
    one nobody ever checked, so a human's deliberate (if unusable) input
    would vanish silently. Never changes the language; it only makes the
    discard visible."""

    OVERRIDE_READ_FAILED = "override-read-failed"
    """The override could not be READ (transient CRM error). Distinct
    from "no override" on purpose: absence is the normal answer and can
    resolve to a corroborated source that prints nothing, so collapsing a
    failed read into absence would make a lost override look HEALTHIER
    than an ordinary row. Always unverified, and — unlike every other
    source — reported on the wet path too."""

    US_MODE = "us-mode"
    """The us_mode scoring lane is English by construction. Not an
    inference, so it needs no corroboration and never warns."""


# Sources that mean "nobody verified this language" — the operator should
# eyeball the row before approving the send. Kept as a set (not an
# `in (A, B)` at each call site) so the warn/no-warn policy lives in ONE
# place; adding a source forces an explicit decision here.
UNVERIFIED_LANGUAGE_SOURCES: frozenset[LanguageSource] = frozenset({
    LanguageSource.COMPANY_HQ_CATCHALL,
    LanguageSource.COMPANY_HQ_DISAGREES,
    LanguageSource.LANE_DEFAULT,
    LanguageSource.OVERRIDE_UNUSABLE,
    LanguageSource.OVERRIDE_READ_FAILED,
})

# Sources that indicate the override MACHINERY failed, as opposed to an
# inference nobody corroborated. These are data-integrity signals, not
# review noise, so they are reported on the wet path too — a dry-run-only
# warning cannot catch an override lost between preview and send.
BROKEN_OVERRIDE_SOURCES: frozenset[LanguageSource] = frozenset({
    LanguageSource.OVERRIDE_UNUSABLE,
    LanguageSource.OVERRIDE_READ_FAILED,
})

# Sentinel returned by the CRM client's person-language getter when the
# read itself failed. A str (not None, not a Language code) so it flows
# through the same parameter as a real value: `coerce_language` rejects
# it like any other unusable value, and `classify_language_source`
# recognizes it by identity to report the failure specifically.
LANGUAGE_OVERRIDE_READ_FAILED = "<override-read-failed>"


def should_report_language_source(source: LanguageSource, *, dry_run: bool) -> bool:
    """Whether the operator should be told about this row's language source.

    The whole warn/stay-silent policy, in one testable predicate:

      * verified sources (person override, corroborating HQ, us_mode lane)
        never report — re-warning a checked row every run just trains the
        operator to skim past the line;
      * unverified inferences report on DRY RUNS, where the operator is
        reviewing a queue and can act on them;
      * BROKEN overrides report on BOTH. Dry-run and wet are separate
        processes with separate caches, so the wet run re-reads every
        override and one transient error can lose an override that was
        present at preview. A dry-run-only warning cannot catch that.

    Never gates a send — reporting only.
    """
    if source not in UNVERIFIED_LANGUAGE_SOURCES:
        return False
    return dry_run or source in BROKEN_OVERRIDE_SOURCES


def has_person_override(raw: object) -> bool:
    """True iff `raw` is a person override the send path will actually use.

    The single definition of "this row was human-checked". Both language
    guards call it directly rather than inferring it from
    `classify_language_source`'s return value, so the mismatch-suppression
    decision can never drift with that function's branch ordering.
    """
    return coerce_language(raw) is not None


def coerce_language(raw: object) -> Language | None:
    """Best-effort `Language` from an arbitrary stored value, or None.

    Fail-open by construction: returns None for anything that is not a
    recognized code, INCLUDING values that are legal in the CRM but absent
    from the `Language` enum. A `people.language` select may carry options
    the agent has no copy for — those must degrade to "no override" rather
    than crash a send or ship the wrong language.
    """
    if isinstance(raw, Language):
        return raw
    if not isinstance(raw, str):
        return None
    code = raw.strip().lower()
    if code not in _VALID_LANGUAGE_CODES:
        return None
    return Language(code)


def classify_language_source(
    language: Language | str,
    *,
    person_override: object = None,
    hq_expected: Language | None = None,
    scoring_lane: str | None = None,
) -> LanguageSource:
    """Explain WHY `language` is the language for this row.

    Pure — no I/O, no CRM reads. Split from `resolve_language` so the
    send path can classify the value it ALREADY resolved without a second
    resolution (and so the many call sites that patch `resolve_language`
    in tests keep working untouched).

    Args:
        language: the resolved language, as returned by `resolve_language`.
        person_override: raw `people.language` value. Any value that
            `coerce_language` accepts means the row was human-checked.
        hq_expected: the HQ-derived expectation from
            `workflows.daily_check.expected_language_for_entry`, or None
            when undeterminable. Explanation only — the HQ signal never
            retranslates a row, because HQ-derived EN is an unusable
            catch-all bucket (see `LanguageSource.COMPANY_HQ_CATCHALL`).
        scoring_lane: the entry's `scoring_lane`; "us_mode" is English by
            construction and so is never an inference to corroborate.
    """
    if has_person_override(person_override):
        return LanguageSource.PERSON_OVERRIDE
    # Both checks come BEFORE the lane/HQ branches: a broken override is a
    # fact about this row that no amount of company-side corroboration
    # makes safe, and reporting `company-hq` here would tell the operator
    # the row is verified when the one human-set signal was just lost.
    if person_override is LANGUAGE_OVERRIDE_READ_FAILED:
        return LanguageSource.OVERRIDE_READ_FAILED
    # `isinstance(str)` per the getter's `str | None` contract: a non-str
    # here is out-of-contract (in practice a test double), and treating it
    # as a real operator-set value would manufacture warnings from mocks.
    if isinstance(person_override, str) and person_override.strip():
        return LanguageSource.OVERRIDE_UNUSABLE
    if (scoring_lane or "") == "us_mode":
        return LanguageSource.US_MODE
    if hq_expected is None:
        return LanguageSource.LANE_DEFAULT
    if hq_expected is Language.EN:
        return LanguageSource.COMPANY_HQ_CATCHALL
    # Coerced, not compared raw: the send path's `language` is a Language in
    # production, but several test fakes patch `resolve_language` to return a
    # bare code string. A shape difference must not masquerade as a real
    # es↔pt divergence.
    if hq_expected is not coerce_language(language):
        return LanguageSource.COMPANY_HQ_DISAGREES
    return LanguageSource.COMPANY_HQ


def resolve_language_with_source(
    attrs: dict,
    *,
    person_override: object = None,
    hq_expected: Language | None = None,
    scoring_lane: str | None = None,
    persona: str | None = None,
    dm_step: str | None = None,
) -> tuple[Language, LanguageSource]:
    """`resolve_language` + `classify_language_source` in one call.

    Convenience for callers that resolve and explain in the same breath
    (audit scripts, backfills). The send path calls the two halves
    separately — see `classify_language_source`.

    Raises:
        MissingLanguageError: propagated from `resolve_language` when
            there is no usable override AND no valid stored language.
    """
    language = resolve_language(
        attrs,
        person_override=person_override,
        persona=persona,
        dm_step=dm_step,
    )
    source = classify_language_source(
        language,
        person_override=person_override,
        hq_expected=hq_expected,
        scoring_lane=scoring_lane,
    )
    return language, source


def resolve_language(
    attrs: dict,
    *,
    person_override: object = None,
    persona: str | None = None,
    dm_step: str | None = None,
) -> Language:
    """Resolve the language to use for an outreach message.

    Reads the `language` field from an Attio entry's attribute dict and
    returns the corresponding `Language` enum member. The pre-PR-12
    "default to English on absence" behavior
    (`Language(attrs.get("language", "en"))`) is GONE — missing or
    unrecognized language values raise `MissingLanguageError` instead.

    A valid `person_override` outranks the stored entry value (see the
    module docstring): it is the one signal a human set deliberately,
    where every other input is an inference from company HQ or profile
    location.

    Args:
        attrs: Attio list-entry attribute dict (the dict shape that
            `_get_all_entries_parsed` yields). Reads `language` and
            `record_id`; both optional from the dict's perspective.
        person_override: raw `people.language` value for this prospect
            (str, `Language`, or None). Wins over `attrs["language"]`
            when valid. Invalid values — including select options the
            copy library has no templates for — are ignored, and
            resolution falls through to the stored entry value exactly
            as if no override existed.
        persona: optional persona key for richer error payloads. Not
            required for resolution itself — language is independent
            of persona.
        dm_step: optional step label (e.g. "dm1", "connection_note")
            for error payload context.

    Returns:
        The matching `Language` enum member.

    Raises:
        MissingLanguageError: no usable `person_override` AND the stored
            language attribute is missing, empty, or not a member of the
            Language enum. Carries structured fields for operator triage.
            A valid override RESCUES a row with no stored language — a
            human-checked value is strictly better evidence than the
            absent one it replaces.
    """
    override = coerce_language(person_override)
    if override is not None:
        return override

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
