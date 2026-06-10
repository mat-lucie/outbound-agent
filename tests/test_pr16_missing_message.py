"""PR-16 (B-PD-005 + B-PD-008): typed MissingMessageError + ROI/OTIF
content claim CI guard.

Covers three pieces:

  1. `models.campaign.get_message` raises `MissingMessageError` when
     the `(persona, language, dm_step)` triple has no body. The
     pre-PR-16 silent Spanish fallback shipped wrong-language DMs
     and violated §0 #9.

  2. `workflows.daily_check` callers catch `MissingMessageError` and
     open `missing_copy` Operator Review Queue rows so the operator
     can triage the gap.

  3. `scripts/check_messages_claims.py` CI guard detects
     unsubstantiated ROI/OTIF/$ claims in `content/messages.json`
     and exits non-zero unless every claim has an entry in
     `content/evidence_refs.json`.

§0 #9 protection: no prospect ever silently receives a wrong-language
or unsubstantiated-claim DM. The audit-trail is the
`missing_copy` queue row for runtime, and the CI guard for ship-time.
"""
from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from models.campaign import (
    MessageStep,
    MissingMessageError,
    Persona,
    get_message,
)
from models.enums import Language

# ==================================================================
# MissingMessageError — structured fields + __str__
# ==================================================================


class TestMissingMessageError:
    def test_carries_all_structured_fields(self):
        exc = MissingMessageError(
            persona="executive_sponsors",
            language="pt",
            dm_step="dm2",
            variant="default",
            record_id="rec_x",
        )
        assert exc.persona == "executive_sponsors"
        assert exc.language == "pt"
        assert exc.dm_step == "dm2"
        assert exc.variant == "default"
        assert exc.record_id == "rec_x"

    def test_str_includes_all_set_fields(self):
        exc = MissingMessageError(
            persona="ops",
            language="zz",
            dm_step="dm3",
            variant="v2",
            record_id="rec_y",
        )
        s = str(exc)
        assert "persona='ops'" in s
        assert "language='zz'" in s
        assert "dm_step='dm3'" in s
        assert "variant='v2'" in s
        assert "record_id='rec_y'" in s


# ==================================================================
# get_message — raise paths
# ==================================================================


class TestGetMessageRaises:
    def test_unknown_persona_raises(self):
        """Persona key missing entirely from messages.json."""
        with patch(
            "models.campaign.load_messages",
            return_value={},
        ):
            with pytest.raises(MissingMessageError) as exc_info:
                get_message(Persona.OPERATIONS_LEADERS, Language.EN, MessageStep.DM1)
            assert exc_info.value.persona == "operations_leaders"
            assert exc_info.value.language == "en"
            assert exc_info.value.dm_step == "dm1"

    def test_unknown_step_raises(self):
        """Step missing under persona."""
        with patch(
            "models.campaign.load_messages",
            return_value={"operations_leaders": {}},
        ), pytest.raises(MissingMessageError):
            get_message(Persona.OPERATIONS_LEADERS, Language.EN, MessageStep.DM1)

    def test_unknown_language_raises_no_silent_spanish_fallback(self):
        """B-PD-005 root cause: pre-PR-16 silently returned step_msgs["es"]
        when the requested language was absent. PR-16 raises instead."""
        with patch(
            "models.campaign.load_messages",
            return_value={"operations_leaders": {"dm1": {"es": "Hola"}}},
        ):
            with pytest.raises(MissingMessageError) as exc_info:
                get_message(Persona.OPERATIONS_LEADERS, Language.EN, MessageStep.DM1)
            assert exc_info.value.language == "en"

    def test_empty_string_body_raises(self):
        """A present-but-empty body is treated as missing — operators
        shouldn't ship empty DMs."""
        with patch(
            "models.campaign.load_messages",
            return_value={"operations_leaders": {"dm1": {"en": ""}}},
        ), pytest.raises(MissingMessageError):
            get_message(Persona.OPERATIONS_LEADERS, Language.EN, MessageStep.DM1)

    def test_present_body_returned(self):
        with patch(
            "models.campaign.load_messages",
            return_value={"operations_leaders": {"dm1": {"en": "Hi there"}}},
        ):
            assert (
                get_message(Persona.OPERATIONS_LEADERS, Language.EN, MessageStep.DM1)
                == "Hi there"
            )

    def test_record_id_threaded_into_error(self):
        with patch(
            "models.campaign.load_messages",
            return_value={"operations_leaders": {"dm1": {"es": "Hola"}}},
        ):
            with pytest.raises(MissingMessageError) as exc_info:
                get_message(
                    Persona.OPERATIONS_LEADERS,
                    Language.EN,
                    MessageStep.DM1,
                    record_id="rec_abc",
                )
            assert exc_info.value.record_id == "rec_abc"


# ==================================================================
# MissingMessagePayload TypedDict registration
# ==================================================================


class TestMissingMessageEscalationSchema:
    def test_schema_registered(self):
        from workflows.escalation_schemas import (
            ESCALATION_SCHEMAS,
            MissingMessagePayload,
        )
        assert ESCALATION_SCHEMAS.get("missing_copy") is MissingMessagePayload

    def test_payload_required_fields(self):
        from workflows.escalation_schemas import MissingMessagePayload

        assert MissingMessagePayload.__required_keys__ == {
            "record_id",
            "persona",
            "language",
            "dm_step",
            "variant",
            "error_msg",
        }


# ==================================================================
# ROI/OTIF claim CI guard
# ==================================================================


class TestCheckMessagesClaims:
    """B-PD-008: every quantitative claim in messages.json must have
    a corresponding entry in evidence_refs.json. The guard runs as a
    pre-commit hook + CI gate."""

    def test_find_claims_detects_otif_improvement(self):
        from scripts.check_messages_claims import find_claims

        result = find_claims("We help you increase OTIF from 85% to 97%.")
        assert len(result) == 1
        assert "otif from 85%" in result[0]

    def test_find_claims_detects_roi_percent(self):
        from scripts.check_messages_claims import find_claims

        # Different ROI phrasings the regex must catch.
        assert find_claims("Achieve 25% ROI in 6 months.")
        assert find_claims("ROI of 30%")
        assert find_claims("ROI de 40%")

    def test_find_claims_skips_casual_mentions(self):
        """`ROI` and `OTIF` as casual mentions (no number) MUST NOT
        trigger — otherwise the guard over-flags operational copy."""
        from scripts.check_messages_claims import find_claims

        assert find_claims("OTIF is something we care about.") == []
        assert find_claims("We care about your ROI") == []
        assert find_claims("Improving on-time delivery without numbers.") == []

    def test_find_claims_detects_dollar_savings(self):
        from scripts.check_messages_claims import find_claims

        assert find_claims("Customers saved $10,000 last year.")
        assert find_claims("Ahorró $25,000 anuales.")

    def test_guard_exits_zero_when_no_claims(self, tmp_path: Path):
        """A messages.json with no quantitative claims passes cleanly."""
        messages = {
            "operations_leaders": {
                "dm1": {
                    "en": "Hi, want to chat about manufacturing scheduling?",
                    "es": "Hola, ¿quieres hablar de programación de producción?",
                },
            },
        }
        msg_path = tmp_path / "messages.json"
        ev_path = tmp_path / "evidence_refs.json"
        msg_path.write_text(json.dumps(messages), encoding="utf-8")
        ev_path.write_text("{}", encoding="utf-8")

        result = subprocess.run(
            [sys.executable, "scripts/check_messages_claims.py",
             "--messages", str(msg_path), "--evidence", str(ev_path)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr

    def test_guard_exits_one_on_unsubstantiated_claim(self, tmp_path: Path):
        """A claim without an evidence entry MUST exit non-zero so the
        pre-commit hook + CI block the merge."""
        messages = {
            "operations_leaders": {
                "dm1": {
                    "en": "Our customers achieve 25% ROI in 6 months.",
                },
            },
        }
        msg_path = tmp_path / "messages.json"
        ev_path = tmp_path / "evidence_refs.json"
        msg_path.write_text(json.dumps(messages), encoding="utf-8")
        ev_path.write_text("{}", encoding="utf-8")  # no evidence

        result = subprocess.run(
            [sys.executable, "scripts/check_messages_claims.py",
             "--messages", str(msg_path), "--evidence", str(ev_path)],
            capture_output=True, text=True,
        )
        assert result.returncode == 1
        assert "unsubstantiated claim" in result.stderr.lower()
        assert "25% roi" in result.stderr.lower()

    def test_guard_passes_when_claim_has_evidence(self, tmp_path: Path):
        """An entry in evidence_refs.json clears the claim."""
        messages = {
            "operations_leaders": {
                "dm1": {
                    "en": "Acme Corp improved OTIF from 85% to 97% with our platform.",
                },
            },
        }
        evidence = {
            "OTIF from 85%": {
                "source": "Sigma 2026Q1 case study",
                "verified_by": "operator@example.com",
                "verified_at": "2026-04-15",
            },
        }
        msg_path = tmp_path / "messages.json"
        ev_path = tmp_path / "evidence_refs.json"
        msg_path.write_text(json.dumps(messages), encoding="utf-8")
        ev_path.write_text(json.dumps(evidence), encoding="utf-8")

        result = subprocess.run(
            [sys.executable, "scripts/check_messages_claims.py",
             "--messages", str(msg_path), "--evidence", str(ev_path)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)

    def test_guard_handles_missing_evidence_file_loudly(self, tmp_path: Path):
        """An absent evidence file is treated as 'no evidence yet';
        claims still fail loud."""
        messages = {
            "operations_leaders": {
                "dm1": {"en": "Achieve 50% ROI guaranteed."},
            },
        }
        msg_path = tmp_path / "messages.json"
        msg_path.write_text(json.dumps(messages), encoding="utf-8")
        # NO evidence file written.

        result = subprocess.run(
            [sys.executable, "scripts/check_messages_claims.py",
             "--messages", str(msg_path), "--evidence", str(tmp_path / "missing.json")],
            capture_output=True, text=True,
        )
        assert result.returncode == 1

    def test_guard_exits_two_on_malformed_messages_json(self, tmp_path: Path):
        msg_path = tmp_path / "messages.json"
        msg_path.write_text("not json", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "scripts/check_messages_claims.py",
             "--messages", str(msg_path)],
            capture_output=True, text=True,
        )
        assert result.returncode == 2

    def test_evidence_refs_file_is_valid_dict(self):
        """`content/evidence_refs.json` MUST be a JSON object (the CI
        guard's malformed-file exit-2 path depends on this).

        PR-16 originally shipped `{}` but the fold-in added entries
        for the existing `OTIF +5-8 points` claims in messages.json
        (GTM-QA-build16 #2 — guard value is theoretical without
        starter evidence). The schema invariant the guard depends on
        is "valid JSON object" — not "empty dict"."""
        from models.campaign import CONTENT_DIR

        ev_path = CONTENT_DIR / "evidence_refs.json"
        assert ev_path.exists()
        with ev_path.open() as f:
            data = json.load(f)
        assert isinstance(data, dict)

    def test_current_messages_json_passes_guard(self):
        """The shipped messages.json must pass the guard on every
        commit — otherwise the pre-commit hook would block every
        future PR. Lock in by running against the live file."""
        result = subprocess.run(
            [sys.executable, "scripts/check_messages_claims.py"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            f"messages.json has unsubstantiated claims:\n{result.stderr}"
        )

    # PR-16 fold-in tests (post-QA round 1) ─────────────────────────

    def test_otif_point_range_pattern_detected(self):
        """PR-16 fold-in (GTM-QA + prospect-daily-QA convergence): the
        shipped messages.json contains `OTIF +5-8 points / puntos /
        pontos` in dm2_v1 for ICP-1 personas. The original regex set
        missed it because it required a `%` anchor. Added a fifth
        pattern; this test locks in detection across all 3 languages."""
        from scripts.check_messages_claims import find_claims

        assert find_claims("OTIF +5-8 points in the first quarter")
        assert find_claims("OTIF +5-8 puntos en el primer trimestre")
        assert find_claims("OTIF +5-8 pontos no primeiro trimestre")
        # Common phrasings beyond the literal `+N-M`:
        assert find_claims("OTIF improved 5 points")
        assert find_claims("OTIF up 8 points")
        # Casual mentions still skip (no number).
        assert find_claims("OTIF is a few points behind plan") == []

    def test_guard_exits_two_on_malformed_evidence_file(self, tmp_path: Path):
        """PR-16 fold-in (silent-failure-hunter IMPORTANT #3):
        evidence_refs.json must be a JSON object. List/string/null
        get exit code 2 + stderr — pre-fold-in this silently
        degraded to an empty set and every claim was flagged
        unsubstantiated without a hint that the schema was wrong."""
        messages = {"operations_leaders": {"dm1": {"en": "Achieve 25% ROI."}}}
        msg_path = tmp_path / "messages.json"
        ev_path = tmp_path / "evidence_refs.json"
        msg_path.write_text(json.dumps(messages), encoding="utf-8")
        ev_path.write_text("[]", encoding="utf-8")  # WRONG: list, not dict

        result = subprocess.run(
            [sys.executable, "scripts/check_messages_claims.py",
             "--messages", str(msg_path), "--evidence", str(ev_path)],
            capture_output=True, text=True,
        )
        assert result.returncode == 2
        assert "must be a JSON object" in result.stderr

    def test_guard_warns_on_evidence_key_case_collision(self, tmp_path: Path):
        """PR-16 fold-in (silent-failure-hunter IMPORTANT #1): two
        evidence keys that differ only in case silently collapsed
        pre-fold-in. The set comprehension hid one metadata blob.
        Fold-in warns loudly when collisions are detected."""
        messages = {"operations_leaders": {"dm1": {"en": "Achieve 25% ROI."}}}
        # Two evidence entries differing only in case.
        evidence = {
            "25% ROI": {"source": "Sigma 2026Q1"},
            "25% roi": {"source": "Different 2026Q2"},
        }
        msg_path = tmp_path / "messages.json"
        ev_path = tmp_path / "evidence_refs.json"
        msg_path.write_text(json.dumps(messages), encoding="utf-8")
        ev_path.write_text(json.dumps(evidence), encoding="utf-8")

        result = subprocess.run(
            [sys.executable, "scripts/check_messages_claims.py",
             "--messages", str(msg_path), "--evidence", str(ev_path)],
            capture_output=True, text=True,
        )
        # The claim is cleared (both keys map to "25% roi"), but the
        # warning fires.
        assert "case-insensitive match" in result.stderr
        assert result.returncode == 0  # claim is still satisfied

    def test_detect_responses_handles_missing_copy_gracefully(self):
        """PR-16 fold-in (prospect-daily-QA-build16 BLOCKING):
        `workflows.detect_responses.build_classifier_opener` (or its
        equivalent) calls `get_message` inside a try/except that
        previously only caught `(ValueError, KeyError)`. PR-16
        changes get_message to raise MissingMessageError (extends
        Exception), so this site MUST also catch MissingMessageError
        — otherwise it propagates uncaught and breaks response
        classification.

        This test directly imports detect_responses' helper and
        exercises the missing-copy path; the fold-in adds
        MissingMessageError to the except tuple."""
        from unittest.mock import patch as _patch

        from workflows import detect_responses

        # Mock get_message to raise — simulate a missing copy.
        with _patch(
            "workflows.detect_responses.get_message",
            side_effect=MissingMessageError(
                persona="operations_leaders", language="en",
                dm_step="dm1", variant="default",
            ),
        ):
            result = detect_responses._reconstruct_opener(
                persona_value="operations_leaders",
                language_value="en",
                prospect_name="Alice",
                company="Acme",
            )
        # Fallback opener is returned cleanly; no exception escapes.
        assert isinstance(result, str)
        assert len(result) > 0
