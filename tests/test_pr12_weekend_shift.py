"""PR-12: typed MissingLanguageError + advance_off_weekend +
next_eligible_send_date.

Covers the three pieces shipped together as PR-12:

  1. models.business_calendar.advance_off_weekend — forward-only weekend
     shift (Sat -> Mon, Sun -> Mon, weekday unchanged). Distinct from
     the existing shift_off_weekend (Sat -> Fri, Sun -> Mon).
  2. models.resolution.resolve_language — typed replacement for the
     silent `Language(attrs.get("language", "en"))` default. Raises
     MissingLanguageError with structured fields for operator triage.
  3. models.resolution.compute_next_eligible_send_date — forward-only
     cadence floor for the next DM step, stored on Attio after each
     confirmed send.

Reference week (Mon 2026-03-09 .. Sun 2026-03-15):
    Mon 03-09, Tue 03-10, Wed 03-11, Thu 03-12, Fri 03-13, Sat 03-14,
    Sun 03-15.

Per the plan's Round-4 D31 contract, the three canonical Sat -> Mon
edge cases (Fri DM-N+1 via business-day math, Sat DM-N+1, Sun DM-N+1)
all land on Monday. These are distinct fixtures, not a single
"weekend -> Mon" assertion — the lens-QA roster (prospect-daily-QA +
math-QA) inspects each branch.
"""

from datetime import date
from typing import TYPE_CHECKING

import pytest

from models.business_calendar import advance_off_weekend, shift_off_weekend
from models.enums import Language
from models.resolution import (
    MissingLanguageError,
    compute_next_eligible_send_date,
    resolve_language,
)

if TYPE_CHECKING:
    from workflows.audit import AuditLogger

# ==================================================================
# advance_off_weekend — forward-only weekend shift
# ==================================================================


class TestAdvanceOffWeekend:
    """B-PD-007: stored cadence floor must never SHORTEN under a
    weekend collision. Forward-only shift is the only safe semantic
    for an Attio-stored attribute that the §3.1 no-resend invariant
    consults on every run.
    """

    def test_saturday_advances_to_monday(self):
        # Sat 03-14 -> Mon 03-16 (+2 days)
        assert advance_off_weekend(date(2026, 3, 14)) == date(2026, 3, 16)

    def test_sunday_advances_to_monday(self):
        # Sun 03-15 -> Mon 03-16 (+1 day)
        assert advance_off_weekend(date(2026, 3, 15)) == date(2026, 3, 16)

    @pytest.mark.parametrize(
        "weekday",
        [
            date(2026, 3, 9),   # Mon
            date(2026, 3, 10),  # Tue
            date(2026, 3, 11),  # Wed
            date(2026, 3, 12),  # Thu
            date(2026, 3, 13),  # Fri
        ],
    )
    def test_weekday_unchanged(self, weekday: date):
        assert advance_off_weekend(weekday) == weekday

    def test_diverges_from_shift_off_weekend_on_saturday(self):
        """shift_off_weekend(Sat) is Fri; advance_off_weekend(Sat) is
        Mon. The functions deliberately do NOT have the same semantic
        — PR-12 must not silently re-purpose the existing helper."""
        sat = date(2026, 3, 14)
        assert shift_off_weekend(sat) == date(2026, 3, 13)   # Fri
        assert advance_off_weekend(sat) == date(2026, 3, 16)  # Mon

    def test_agrees_with_shift_off_weekend_on_sunday(self):
        """Both functions agree on Sun -> Mon; only Sat differs."""
        sun = date(2026, 3, 15)
        assert shift_off_weekend(sun) == advance_off_weekend(sun)

    def test_does_not_mutate_input(self):
        d = date(2026, 3, 14)
        _ = advance_off_weekend(d)
        assert d == date(2026, 3, 14)


# ==================================================================
# compute_next_eligible_send_date — the three Round-4 D31 cases
# ==================================================================


class TestComputeNextEligibleSendDate:
    """Three Sat/Sun weekend-shift fixtures + clean-landing baselines.

    Round-4 D31 names three canonical edge cases (Fri/Sat/Sun → Mon),
    but with DM_TIMING={DM2: 5, DM3: 10} no `last_contact_date` value
    produces a Friday TARGET — both deltas land on a different
    weekday-of-week. The two genuine weekend-shift cases that arise
    from PR-12's cadence math are:

      - Mon last_contact + 5d (DM1→DM2) = Sat → Mon
      - Tue last_contact + 5d (DM1→DM2) = Sun → Mon

    plus their DM2→DM3 (10d delta) equivalents:

      - Wed last_contact + 10d = Sat → Mon
      - Thu last_contact + 10d = Sun → Mon

    The "Fri DM-N+1 → Mon" Round-4 D31 case appears in the
    connection_note → DM1 path (1-day delta), which PR-12 deliberately
    does NOT write to `next_eligible_send_date` (the helper returns
    None for "connection_note" — the in-memory `dm_sequencer` floor
    governs DM1 eligibility). The Fri→Mon Round-4 case is therefore
    covered by `tests/test_business_calendar.py`'s `shift_off_weekend`
    suite + `tests/test_dm_sequencer.py`, not here. A clean-landing
    baseline (Fri+5=Wed) is kept for regression coverage of the
    no-shift code path.
    """

    # ---- Genuine Sat→Mon (Mon last_contact + 5d) ----

    def test_monday_dm1_to_dm2_saturday_target_shifts_to_monday(self):
        # Mon 03-09 + 5 days = Sat 03-14 -> Mon 03-16 (Sat -> Mon).
        result = compute_next_eligible_send_date(
            last_contact_date=date(2026, 3, 9), just_sent_step="dm1"
        )
        assert result == date(2026, 3, 16)

    # ---- Genuine Sun→Mon (Tue last_contact + 5d) ----

    def test_tuesday_dm1_to_dm2_sunday_target_shifts_to_monday(self):
        # Tue 03-10 + 5 days = Sun 03-15 -> Mon 03-16 (Sun -> Mon).
        result = compute_next_eligible_send_date(
            last_contact_date=date(2026, 3, 10), just_sent_step="dm1"
        )
        assert result == date(2026, 3, 16)

    # ---- Clean-landing baseline (Fri last_contact + 5d = Wed) ----

    def test_friday_dm1_to_dm2_lands_on_wednesday(self):
        # Fri 03-13 + 5 days = Wed 03-18 (clean weekday, no shift).
        # Regression coverage for the no-shift code path.
        result = compute_next_eligible_send_date(
            last_contact_date=date(2026, 3, 13), just_sent_step="dm1"
        )
        assert result == date(2026, 3, 18)

    # ---- DM2 -> DM3 (10-day) variants ----

    def test_dm2_to_dm3_monday_target_unchanged(self):
        # Fri 03-13 + 10 days = Mon 03-23 (no shift; 10 days lands clean).
        result = compute_next_eligible_send_date(
            last_contact_date=date(2026, 3, 13), just_sent_step="dm2"
        )
        assert result == date(2026, 3, 23)

    def test_dm2_to_dm3_saturday_target_shifts_to_monday(self):
        # Wed 03-11 + 10 days = Sat 03-21 -> Mon 03-23.
        result = compute_next_eligible_send_date(
            last_contact_date=date(2026, 3, 11), just_sent_step="dm2"
        )
        assert result == date(2026, 3, 23)

    def test_dm2_to_dm3_sunday_target_shifts_to_monday(self):
        # Thu 03-12 + 10 days = Sun 03-22 -> Mon 03-23.
        result = compute_next_eligible_send_date(
            last_contact_date=date(2026, 3, 12), just_sent_step="dm2"
        )
        assert result == date(2026, 3, 23)

    # ---- Terminal / unknown steps ----

    def test_dm3_returns_none(self):
        """DM3 is terminal in v1 cadence. NURTURE re-engagement (PR-39)
        uses nurture_re_eligible_at, not next_eligible_send_date."""
        result = compute_next_eligible_send_date(
            last_contact_date=date(2026, 3, 11), just_sent_step="dm3"
        )
        assert result is None

    def test_connection_note_returns_none(self):
        """The connection-note step doesn't write next_eligible_send_date
        — PR-12 surgical scope. DM1's eligibility is still computed by
        the in-memory dm_sequencer floor at acceptance time."""
        result = compute_next_eligible_send_date(
            last_contact_date=date(2026, 3, 11),
            just_sent_step="connection_note",
        )
        assert result is None

    def test_unknown_step_returns_none(self):
        result = compute_next_eligible_send_date(
            last_contact_date=date(2026, 3, 11), just_sent_step="dm99"
        )
        assert result is None


# ==================================================================
# resolve_language — typed B-PD-001 replacement for silent fallback
# ==================================================================


class TestResolveLanguageSuccess:
    """Happy paths: each Language enum value resolves correctly."""

    def test_returns_spanish(self):
        assert resolve_language({"language": "es"}) == Language.ES

    def test_returns_english(self):
        assert resolve_language({"language": "en"}) == Language.EN

    def test_returns_portuguese(self):
        assert resolve_language({"language": "pt"}) == Language.PT

    def test_already_language_enum_passes_through(self):
        """If a caller already constructed the enum, accept it as-is."""
        assert resolve_language({"language": Language.PT}) == Language.PT


class TestResolveLanguageRaises:
    """B-PD-001: every silent-fallback site is now a typed raise."""

    def test_missing_key_raises(self):
        with pytest.raises(MissingLanguageError) as exc_info:
            resolve_language({"persona": "operations_leaders"})
        assert exc_info.value.language is None

    def test_null_value_raises(self):
        with pytest.raises(MissingLanguageError) as exc_info:
            resolve_language({"language": None})
        assert exc_info.value.language is None

    def test_empty_string_raises(self):
        with pytest.raises(MissingLanguageError) as exc_info:
            resolve_language({"language": ""})
        assert exc_info.value.language is None

    def test_invalid_code_raises(self):
        """Languages outside the {es, en, pt} set must fail loud.
        A "fr" prospect should never receive a silent fall-through to
        Spanish or English — that's the §0 #9 violation B-PD-001
        addresses."""
        with pytest.raises(MissingLanguageError) as exc_info:
            resolve_language({"language": "fr"})
        # The rejected value is preserved in the structured field
        # so the operator queue row shows WHAT was rejected.
        assert exc_info.value.language == "fr"

    def test_non_string_value_raises(self):
        with pytest.raises(MissingLanguageError) as exc_info:
            resolve_language({"language": 42})
        assert exc_info.value.language == "42"

    def test_error_carries_persona_field(self):
        with pytest.raises(MissingLanguageError) as exc_info:
            resolve_language(
                {"language": None}, persona="operations_leaders"
            )
        assert exc_info.value.persona == "operations_leaders"

    def test_error_carries_dm_step_field(self):
        with pytest.raises(MissingLanguageError) as exc_info:
            resolve_language({"language": None}, dm_step="dm2")
        assert exc_info.value.dm_step == "dm2"

    def test_error_carries_record_id_from_attrs(self):
        with pytest.raises(MissingLanguageError) as exc_info:
            resolve_language(
                {"language": None, "record_id": "rec_abc123"}
            )
        assert exc_info.value.record_id == "rec_abc123"

    def test_error_str_includes_all_set_fields(self):
        """Operator-facing exception string should include every set
        field so triage from the JSONL audit log is one-shot."""
        err = MissingLanguageError(
            persona="executive_sponsors",
            language="zz",
            dm_step="connection_note",
            record_id="rec_xyz",
        )
        s = str(err)
        assert "persona='executive_sponsors'" in s
        assert "language='zz'" in s
        assert "dm_step='connection_note'" in s
        assert "record_id='rec_xyz'" in s


# ==================================================================
# Escalation TypedDict registration — missing_language has a payload
# ==================================================================


class TestMissingLanguageEscalationSchema:
    """The missing_language slug already lived in ESCALATION_TYPES (it
    was reserved at F-PR-3); PR-12 must ALSO register the payload
    TypedDict so the runtime validator catches malformed escalations
    instead of letting them through."""

    def test_schema_registered(self):
        from workflows.escalation_schemas import (
            ESCALATION_SCHEMAS,
            MissingLanguagePayload,
        )

        assert ESCALATION_SCHEMAS.get("missing_language") is MissingLanguagePayload

    def test_payload_required_fields(self):
        """All five fields must be present at runtime; the type system
        permits None for `persona` and `language_value` but the keys
        themselves are required (TypedDict total=True default)."""
        from workflows.escalation_schemas import MissingLanguagePayload

        required = MissingLanguagePayload.__required_keys__
        assert required == {
            "record_id",
            "persona",
            "language_value",
            "dm_step",
            "error_msg",
        }


# ==================================================================
# DM_TIMING drift-protection (fold-in from code-reviewer + comment-analyzer)
# ==================================================================


class TestNextStepDeltaAlignsWithDmTiming:
    """Catches drift between `models.resolution._NEXT_STEP_DELTA_DAYS`
    and `workflows.dm_sequencer.DM_TIMING`. Both encode the same
    cadence facts (DM2 = 5 days, DM3 = 10 days) but with different
    keying directions. If the operator ever retunes a cadence step in DM_TIMING
    without touching `_NEXT_STEP_DELTA_DAYS`, this test fails — the
    stored Attio floor would silently keep using stale values.
    """

    def test_dm1_to_dm2_matches_dm_timing(self):
        from models.campaign import MessageStep
        from models.resolution import _NEXT_STEP_DELTA_DAYS
        from workflows.dm_sequencer import DM_TIMING

        assert _NEXT_STEP_DELTA_DAYS["dm1"] == DM_TIMING[MessageStep.DM2]

    def test_dm2_to_dm3_matches_dm_timing(self):
        from models.campaign import MessageStep
        from models.resolution import _NEXT_STEP_DELTA_DAYS
        from workflows.dm_sequencer import DM_TIMING

        assert _NEXT_STEP_DELTA_DAYS["dm2"] == DM_TIMING[MessageStep.DM3]


# ==================================================================
# Stored-floor consumer in run_dm_sequencing (fold-in from
# prospect-daily-QA-build12 + pr-test-analyzer + silent-failure-hunter)
# ==================================================================


class TestIsBlockedByStoredFloor:
    """B-PD-003 §3.1 protection: the stored `next_eligible_send_date`
    floor is the only runtime enforcement of the forward-only cadence
    boundary in `run_dm_sequencing`'s queue construction. Coverage
    here exists because a regression silently shortening the floor
    would re-open §3.1 leaks.
    """

    def test_no_stored_floor_does_not_block(self):
        from workflows.daily_check import _is_blocked_by_stored_floor

        attrs = {"record_id": "rec_1"}  # next_eligible_send_date absent
        assert _is_blocked_by_stored_floor(attrs, date(2026, 3, 16)) is False

    def test_empty_string_stored_floor_does_not_block(self):
        from workflows.daily_check import _is_blocked_by_stored_floor

        attrs = {"record_id": "rec_1", "next_eligible_send_date": ""}
        assert _is_blocked_by_stored_floor(attrs, date(2026, 3, 16)) is False

    def test_future_floor_blocks(self):
        """The §3.1 protection: today < stored_floor → caller must skip."""
        from workflows.daily_check import _is_blocked_by_stored_floor

        attrs = {
            "record_id": "rec_1",
            "next_eligible_send_date": "2026-03-20",
        }
        assert _is_blocked_by_stored_floor(attrs, date(2026, 3, 16)) is True

    def test_today_equals_floor_does_not_block(self):
        """Off-by-one verification: a row whose floor IS today should
        be eligible (the floor means "do not send BEFORE", not "skip on")."""
        from workflows.daily_check import _is_blocked_by_stored_floor

        attrs = {
            "record_id": "rec_1",
            "next_eligible_send_date": "2026-03-16",
        }
        assert _is_blocked_by_stored_floor(attrs, date(2026, 3, 16)) is False

    def test_past_floor_does_not_block(self):
        from workflows.daily_check import _is_blocked_by_stored_floor

        attrs = {
            "record_id": "rec_1",
            "next_eligible_send_date": "2026-03-10",
        }
        assert _is_blocked_by_stored_floor(attrs, date(2026, 3, 16)) is False

    def test_datetime_stored_floor_truncates_to_date(self):
        """Attio sometimes stores datetimes for date attributes; the
        helper truncates the first 10 chars to ISO date."""
        from workflows.daily_check import _is_blocked_by_stored_floor

        attrs = {
            "record_id": "rec_1",
            "next_eligible_send_date": "2026-03-20T00:00:00Z",
        }
        assert _is_blocked_by_stored_floor(attrs, date(2026, 3, 16)) is True

    def test_malformed_floor_emits_audit_event_and_does_not_block(self):
        """§0 #9 compliance: a malformed stored value falls through to
        the in-memory eligibility but the silent fall-through is made
        observable via an audit event. No queue row is opened from the
        tight loop; aggregation is a follow-up PR."""
        from typing import cast

        from workflows.daily_check import _is_blocked_by_stored_floor

        events: list[dict] = []

        class _FakeAuditLogger:
            def event(self, kind: str, **fields: object) -> None:
                events.append({"kind": kind, **fields})

        attrs = {
            "record_id": "rec_corrupt",
            "next_eligible_send_date": "not-a-date",
        }
        # Cast via the typing-only AuditLogger import: the helper
        # signature is `AuditLogger | None`, but our fake duck-types
        # `.event(...)` and we want to keep the test allocation-free
        # of a real JSONL writer.
        logger = cast("AuditLogger", _FakeAuditLogger())
        result = _is_blocked_by_stored_floor(attrs, date(2026, 3, 16), logger)

        assert result is False
        assert len(events) == 1
        assert events[0]["kind"] == "malformed_next_eligible_send_date"
        assert events[0]["record_id"] == "rec_corrupt"
        assert events[0]["raw_value"] == "not-a-date"

    def test_malformed_floor_without_audit_logger_does_not_block(self):
        """When audit_logger is None (test/CLI contexts), the helper
        still falls through cleanly — it does NOT crash trying to log."""
        from workflows.daily_check import _is_blocked_by_stored_floor

        attrs = {"record_id": "rec_1", "next_eligible_send_date": "junk"}
        assert _is_blocked_by_stored_floor(attrs, date(2026, 3, 16)) is False
