"""Tests for workflows/detect_responses.py (SN Inbox Scraper version)."""

import csv
import io
from unittest.mock import MagicMock, patch

import pytest

from models.pipeline import PipelineStage
from workflows.detect_responses import _normalize_name, detect_responses


@pytest.fixture(autouse=True)
def _detect_responses_test_env(monkeypatch):
    """Wave-2-B fix-up: detect_responses now routes through AttioWriter,
    which requires ATTIO_LIST_ID for list-entry writes. Also shrink the
    retry budget so transient-error tests don't burn ~30s on real sleep
    before AttioWriter exhausts retries.
    """
    monkeypatch.setenv("ATTIO_LIST_ID", "test-list-id")
    monkeypatch.setattr("clients.attio_writer.RETRY_BUDGET_SECONDS", 0.0001)

# ── Helpers ────────────────────────────────────────────────────────────

_STAGE_TO_DEFAULT_DM_STEP = {
    PipelineStage.DM1_SENT.value: 1,
    PipelineStage.DM2_SENT.value: 2,
    PipelineStage.DM3_SENT.value: 3,
}


def _make_entry(entry_id: str, record_id: str, stage: str, dm_step: int | None = None) -> dict:
    """Build a minimal Attio list entry dict as returned by parse_entry.

    `dm_step` defaults to the value consistent with `stage` (DM1_SENT → 1, etc.)
    so the Phase 0.5 auto-repair doesn't fire spurious corrections during tests.
    Override explicitly to simulate the drift cases.
    """
    if dm_step is None:
        dm_step = _STAGE_TO_DEFAULT_DM_STEP.get(stage)
    return {
        "entry_id": entry_id,
        "record_id": record_id,
        "stage": stage,
        "persona": "operations_leaders",
        "language": "en",
        "dm_step": dm_step,
        "quality_score": 75,
        "last_contact_date": "2026-04-01",
        "experiment_id": None,
    }


def _make_sn_csv(*rows: dict) -> str:
    """Build an SN Inbox Scraper CSV string."""
    if not rows:
        return ""
    fieldnames = [
        "participantProfileUrl", "participantFullName",
        "isLastMessageFromMe", "lastMessageBody", "lastMessageDate",
        "totalMessageCount",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in fieldnames})
    return buf.getvalue()


def _setup_mocks(attio, pb, entries, csv_content, name_map):
    """Common mock setup for detect_responses tests.

    name_map: dict of record_id → (name, company, linkedin_url)
    """
    attio.query_list_entries.return_value = entries
    pb.download_result_csv.return_value = csv_content

    mock_cache = MagicMock()
    mock_cache.get.side_effect = lambda rid: name_map.get(rid, ("", "", "", "", ""))

    return mock_cache


# ── Test: normalize_name ─────────────────────────────────────────────

class TestNormalizeName:
    def test_basic(self):
        assert _normalize_name("Carlos Lopez") == "carlos lopez"

    def test_extra_spaces(self):
        assert _normalize_name("  Carlos   Lopez  ") == "carlos lopez"

    def test_mixed_case(self):
        assert _normalize_name("VANESSA Solis") == "vanessa solis"


# ── Test: skip when no inbox_scraper_id ───────────────────────────────

class TestSkipWhenNoId:
    def test_returns_skipped_flag(self):
        result = detect_responses(MagicMock(), MagicMock(), inbox_scraper_id=None)
        assert result == {"skipped": True, "reason": "no_inbox_scraper_id"}

    def test_no_attio_calls_made(self):
        attio = MagicMock()
        pb = MagicMock()
        detect_responses(attio, pb, inbox_scraper_id=None)
        attio.query_list_entries.assert_not_called()
        pb.launch_agent.assert_not_called()

    def test_empty_string_id_also_skips(self):
        result = detect_responses(MagicMock(), MagicMock(), inbox_scraper_id="")
        assert result == {"skipped": True, "reason": "no_inbox_scraper_id"}


# ── Test: no prospects in DM stages ───────────────────────────────────

class TestNoDmProspects:
    def test_returns_zero_counts(self):
        attio = MagicMock()
        pb = MagicMock()

        prospect_entry = _make_entry("e1", "r1", PipelineStage.PROSPECT.value)
        attio.query_list_entries.return_value = [prospect_entry]

        with patch("workflows.detect_responses.AttioClient.parse_entry", side_effect=lambda e: e), \
             patch("workflows.detect_responses.RecordCache") as MockCache:
            mock_cache_instance = MagicMock()
            mock_cache_instance.get.return_value = ("Test User", "TestCo", "https://li.com/in/test", "", "")
            MockCache.return_value = mock_cache_instance

            result = detect_responses(attio, pb, inbox_scraper_id="scraper-123")

        pb.launch_agent.assert_not_called()
        assert result["detected"] == 0


# ── Test: idempotency ────────────────────────────────────────────────

class TestIdempotency:
    def test_skips_already_responded_entry(self):
        attio = MagicMock()
        pb = MagicMock()

        # Entry is already RESPONDED — should not be in dm_prospects
        responded_entry = _make_entry("e1", "r1", PipelineStage.RESPONDED.value)
        attio.query_list_entries.return_value = [responded_entry]

        with patch("workflows.detect_responses.AttioClient.parse_entry", side_effect=lambda e: e), \
             patch("workflows.detect_responses.RecordCache") as MockCache:
            mock_cache_instance = MagicMock()
            mock_cache_instance.get.return_value = ("Carlos Lopez", "Acme", "https://li.com/in/carlos", "", "")
            MockCache.return_value = mock_cache_instance

            result = detect_responses(attio, pb, inbox_scraper_id="scraper-123")

        # Not in DM stages → no prospects to check → no PB launch
        attio.update_list_entry.assert_not_called()
        assert result["detected"] == 0

    def test_skips_not_interested_entry(self):
        attio = MagicMock()
        pb = MagicMock()

        ni_entry = _make_entry("e2", "r2", PipelineStage.NOT_INTERESTED.value)
        attio.query_list_entries.return_value = [ni_entry]

        with patch("workflows.detect_responses.AttioClient.parse_entry", side_effect=lambda e: e), \
             patch("workflows.detect_responses.RecordCache") as MockCache:
            mock_cache_instance = MagicMock()
            mock_cache_instance.get.return_value = ("Maria Gomez", "Corp", "https://li.com/in/maria", "", "")
            MockCache.return_value = mock_cache_instance

            result = detect_responses(attio, pb, inbox_scraper_id="scraper-123")

        attio.update_list_entry.assert_not_called()
        assert result["detected"] == 0


# ── Test: classification routing ──────────────────────────────────────

class TestClassificationRouting:
    def _run_with_response(self, message_body, prospect_name="Test User", stage=PipelineStage.DM1_SENT.value):
        """Run detect_responses with one SN Inbox Scraper CSV row containing a reply."""
        attio = MagicMock()
        pb = MagicMock()

        entry = _make_entry("e10", "r10", stage)
        attio.query_list_entries.return_value = [entry]

        csv_content = _make_sn_csv({
            "participantProfileUrl": "https://linkedin.com/sales/people/ACw123",
            "participantFullName": prospect_name,
            "isLastMessageFromMe": "false",
            "lastMessageBody": message_body,
            "lastMessageDate": "2026-04-09T12:00:00Z",
        })
        pb.download_result_csv.return_value = csv_content

        with patch("workflows.detect_responses.AttioClient.parse_entry", side_effect=lambda e: e), \
             patch("workflows.detect_responses._pb_session_args", return_value={}), \
             patch("workflows.detect_responses.RecordCache") as MockCache:
            mock_cache_instance = MagicMock()
            mock_cache_instance.get.return_value = (prospect_name, "TestCo", "https://li.com/in/test", "", "")
            MockCache.return_value = mock_cache_instance

            result = detect_responses(attio, pb, inbox_scraper_id="scraper-123")

        return result, attio

    def test_negative_response_moves_to_not_interested(self):
        result, attio = self._run_with_response("No thanks, not interested.")
        assert result["detected"] == 1
        assert result["negative"] == 1
        call_kwargs = attio.update_list_entry.call_args
        assert call_kwargs[1]["entry_attributes"]["stage"] == PipelineStage.NOT_INTERESTED.value

    def test_positive_response_moves_to_responded(self):
        result, attio = self._run_with_response("Interested! Let's schedule a demo.")
        assert result["detected"] == 1
        assert result["positive"] == 1
        call_kwargs = attio.update_list_entry.call_args
        assert call_kwargs[1]["entry_attributes"]["stage"] == PipelineStage.RESPONDED.value

    def test_question_response_moves_to_responded(self):
        result, attio = self._run_with_response("How much does it cost?")
        assert result["detected"] == 1
        assert result["question"] == 1
        call_kwargs = attio.update_list_entry.call_args
        assert call_kwargs[1]["entry_attributes"]["stage"] == PipelineStage.RESPONDED.value

    def test_neutral_response_moves_to_responded(self):
        result, attio = self._run_with_response("Thanks for reaching out.")
        assert result["detected"] == 1
        assert result["neutral"] == 1
        call_kwargs = attio.update_list_entry.call_args
        assert call_kwargs[1]["entry_attributes"]["stage"] == PipelineStage.RESPONDED.value

    def test_note_is_created_on_response(self):
        _, attio = self._run_with_response("Interested! Let's talk.")
        attio.create_note.assert_called_once()
        note_call = attio.create_note.call_args
        assert "positive" in note_call[1]["title"]
        assert "Interested" in note_call[1]["content"]


# ── Test: isLastMessageFromMe / manual-reply detection ────────────────

class TestFromMeFiltering:
    """When isLastMessageFromMe is 'true', `totalMessageCount` disambiguates.

    Two scenarios share `is_from_me=true`:
      - Just our automated DMs in the thread, no reply yet → totalMessageCount
        equals the count of DMs sent at this stage → skip.
      - Prospect replied AND we replied back manually → totalMessageCount
        strictly exceeds expected → flag as RESPONDED for human triage,
        since the SN Inbox Scraper only exposes the last message body
        (which is ours, not the prospect's reply).
    """

    def _run(self, *, stage: str, total_message_count: str, last_message_from_me: str = "true"):
        attio = MagicMock()
        pb = MagicMock()

        entry = _make_entry("e20", "r20", stage)
        attio.query_list_entries.return_value = [entry]

        csv_content = _make_sn_csv({
            "participantProfileUrl": "https://linkedin.com/sales/people/ACw123",
            "participantFullName": "Vanessa Solis",
            "isLastMessageFromMe": last_message_from_me,
            "lastMessageBody": "Gracias por tu respuesta, saludos!",
            "lastMessageDate": "2026-04-26T13:58:00Z",
            "totalMessageCount": total_message_count,
        })
        pb.download_result_csv.return_value = csv_content

        with patch("workflows.detect_responses.AttioClient.parse_entry", side_effect=lambda e: e), \
             patch("workflows.detect_responses._pb_session_args", return_value={}), \
             patch("workflows.detect_responses.RecordCache") as MockCache:
            mock_cache_instance = MagicMock()
            mock_cache_instance.get.return_value = ("Vanessa Solis", "Acme", "https://li.com/in/vanessa", "", "")
            MockCache.return_value = mock_cache_instance

            result = detect_responses(attio, pb, inbox_scraper_id="scraper-123")

        return result, attio

    def test_skip_when_only_our_dm1_in_thread(self):
        """DM1_SENT stage, total=1 → just our DM1, no reply yet."""
        result, attio = self._run(
            stage=PipelineStage.DM1_SENT.value,
            total_message_count="1",
        )
        attio.update_list_entry.assert_not_called()
        assert result["detected"] == 0

    def test_skip_when_only_our_dm1_dm2_in_thread(self):
        """DM2_SENT stage, total=2 → just our DM1+DM2, no reply yet."""
        result, attio = self._run(
            stage=PipelineStage.DM2_SENT.value,
            total_message_count="2",
        )
        attio.update_list_entry.assert_not_called()
        assert result["detected"] == 0

    def test_manual_reply_detected_at_dm1_stage(self):
        """The Sebastián case: DM1_SENT stage, total=3 (us, them, us-manual).

        Prospect replied and the operator replied back manually before the daily run
        could classify their reply. SN Scraper sees only the operator's last message.
        """
        result, attio = self._run(
            stage=PipelineStage.DM1_SENT.value,
            total_message_count="3",
        )
        attio.update_list_entry.assert_called_once()
        call_kwargs = attio.update_list_entry.call_args
        assert call_kwargs[1]["entry_attributes"]["stage"] == PipelineStage.RESPONDED.value
        attio.create_note.assert_called_once()
        note_call = attio.create_note.call_args
        assert "Manual reply" in note_call[1]["title"]
        assert result["detected"] == 1

    def test_manual_reply_detected_at_dm2_stage(self):
        """DM2_SENT stage, total=4 (us, them, us-DM2, them, us-manual)."""
        result, attio = self._run(
            stage=PipelineStage.DM2_SENT.value,
            total_message_count="4",
        )
        attio.update_list_entry.assert_called_once()
        call_kwargs = attio.update_list_entry.call_args
        assert call_kwargs[1]["entry_attributes"]["stage"] == PipelineStage.RESPONDED.value
        assert result["detected"] == 1

    def test_skip_when_total_message_count_missing(self):
        """Missing totalMessageCount → can't disambiguate → fall back to skip."""
        result, attio = self._run(
            stage=PipelineStage.DM1_SENT.value,
            total_message_count="",
        )
        attio.update_list_entry.assert_not_called()
        assert result["detected"] == 0

    def test_skip_when_total_message_count_garbage(self):
        """Non-numeric totalMessageCount → fall back to skip (treated as 0)."""
        result, attio = self._run(
            stage=PipelineStage.DM1_SENT.value,
            total_message_count="N/A",
        )
        attio.update_list_entry.assert_not_called()
        assert result["detected"] == 0


# ── Test: name matching ──────────────────────────────────────────────

class TestNameMatching:
    def test_unmatched_name_is_ignored(self):
        """CSV has a name that doesn't match any DM prospect."""
        attio = MagicMock()
        pb = MagicMock()

        entry = _make_entry("e40", "r40", PipelineStage.DM1_SENT.value)
        attio.query_list_entries.return_value = [entry]

        csv_content = _make_sn_csv({
            "participantProfileUrl": "https://linkedin.com/sales/people/ACw999",
            "participantFullName": "Unknown Person",
            "isLastMessageFromMe": "false",
            "lastMessageBody": "Interested!",
            "lastMessageDate": "2026-04-09T12:00:00Z",
        })
        pb.download_result_csv.return_value = csv_content

        with patch("workflows.detect_responses.AttioClient.parse_entry", side_effect=lambda e: e), \
             patch("workflows.detect_responses._pb_session_args", return_value={}), \
             patch("workflows.detect_responses.RecordCache") as MockCache:
            mock_cache_instance = MagicMock()
            mock_cache_instance.get.return_value = ("Known User", "KnownCo", "https://li.com/in/known", "", "")
            MockCache.return_value = mock_cache_instance

            result = detect_responses(attio, pb, inbox_scraper_id="scraper-123")

        attio.update_list_entry.assert_not_called()
        assert result["detected"] == 0

    def test_case_insensitive_name_match(self):
        """Name matching should be case-insensitive."""
        attio = MagicMock()
        pb = MagicMock()

        entry = _make_entry("e41", "r41", PipelineStage.DM1_SENT.value)
        attio.query_list_entries.return_value = [entry]

        csv_content = _make_sn_csv({
            "participantProfileUrl": "https://linkedin.com/sales/people/ACw456",
            "participantFullName": "CARLOS LOPEZ",  # uppercase in CSV
            "isLastMessageFromMe": "false",
            "lastMessageBody": "Interested! Let's talk.",
            "lastMessageDate": "2026-04-09T12:00:00Z",
        })
        pb.download_result_csv.return_value = csv_content

        with patch("workflows.detect_responses.AttioClient.parse_entry", side_effect=lambda e: e), \
             patch("workflows.detect_responses._pb_session_args", return_value={}), \
             patch("workflows.detect_responses.RecordCache") as MockCache:
            mock_cache_instance = MagicMock()
            # Attio stores as mixed case
            mock_cache_instance.get.return_value = ("Carlos Lopez", "Acme", "https://li.com/in/carlos", "", "")
            MockCache.return_value = mock_cache_instance

            result = detect_responses(attio, pb, inbox_scraper_id="scraper-123")

        assert result["detected"] == 1
        attio.update_list_entry.assert_called_once()


# ── Test: name collision (two distinct prospects share a normalized name) ──

class TestNameCollision:
    """In a LATAM list, common names (e.g. two 'Carlos Ejemplo' from different
    companies, or two duplicate Attio records for one person) all map to the
    same normalized name. A reply matched on that name must update *every*
    matched entry — the previous dict-overwrite implementation silently lost
    one of them and left them stuck in DM stage indefinitely.
    """

    def test_collision_updates_both_entries(self):
        attio = MagicMock()
        pb = MagicMock()

        # Two distinct entries with the same normalized name
        entry_a = _make_entry("e-collide-a", "r-collide-a", PipelineStage.DM1_SENT.value)
        entry_b = _make_entry("e-collide-b", "r-collide-b", PipelineStage.DM2_SENT.value)
        attio.query_list_entries.return_value = [entry_a, entry_b]

        csv_content = _make_sn_csv({
            "participantProfileUrl": "https://linkedin.com/sales/people/dup",
            "participantFullName": "Carlos Ejemplo",
            "isLastMessageFromMe": "false",
            "lastMessageBody": "No me interesa, gracias.",
            "lastMessageDate": "2026-04-09T12:00:00Z",
        })
        pb.download_result_csv.return_value = csv_content

        def cache_side_effect(rid):
            if rid == "r-collide-a":
                return ("Carlos Ejemplo", "Cementra", "https://li.com/in/carlos-ejemplo", "", "")
            return ("Carlos Ejemplo", "Bimbo", "https://li.com/in/carlos-bimbo", "", "")

        with patch("workflows.detect_responses.AttioClient.parse_entry", side_effect=lambda e: e), \
             patch("workflows.detect_responses._pb_session_args", return_value={}), \
             patch("workflows.detect_responses.RecordCache") as MockCache:
            mock_cache_instance = MagicMock()
            mock_cache_instance.get.side_effect = cache_side_effect
            MockCache.return_value = mock_cache_instance

            result = detect_responses(attio, pb, inbox_scraper_id="scraper-collide")

        # Detected once (one inbox row matched).
        assert result["detected"] == 1
        # But BOTH entries got updated — neither silently lost.
        assert attio.update_list_entry.call_count == 2
        updated_entry_ids = {
            call.kwargs.get("entry_id") for call in attio.update_list_entry.call_args_list
        }
        assert updated_entry_ids == {"e-collide-a", "e-collide-b"}

    def test_collision_with_one_terminal_entry_only_updates_actionable(self):
        """If one of the colliding entries is already in a terminal stage
        (RESPONDED/NOT_INTERESTED), it should be skipped — only actionable
        entries are touched.
        """
        attio = MagicMock()
        pb = MagicMock()

        entry_actionable = _make_entry("e-act", "r-act", PipelineStage.DM2_SENT.value)
        entry_terminal = _make_entry("e-term", "r-term", PipelineStage.RESPONDED.value)
        attio.query_list_entries.return_value = [entry_actionable, entry_terminal]

        csv_content = _make_sn_csv({
            "participantProfileUrl": "https://linkedin.com/sales/people/x",
            "participantFullName": "Maria Gonzalez",
            "isLastMessageFromMe": "false",
            "lastMessageBody": "Sounds good, let's talk.",
            "lastMessageDate": "2026-04-09T12:00:00Z",
        })
        pb.download_result_csv.return_value = csv_content

        def cache_side_effect(rid):
            return ("Maria Gonzalez", "AcmeCo", f"https://li.com/{rid}", "", "")

        with patch("workflows.detect_responses.AttioClient.parse_entry", side_effect=lambda e: e), \
             patch("workflows.detect_responses._pb_session_args", return_value={}), \
             patch("workflows.detect_responses.RecordCache") as MockCache:
            mock_cache_instance = MagicMock()
            mock_cache_instance.get.side_effect = cache_side_effect
            MockCache.return_value = mock_cache_instance

            detect_responses(attio, pb, inbox_scraper_id="scraper-mixed")

        # Only the actionable entry was updated; terminal one was skipped.
        attio.update_list_entry.assert_called_once()
        assert attio.update_list_entry.call_args.kwargs.get("entry_id") == "e-act"


# ── Cadence drift detector ─────────────────────────────────────────────

class TestCadenceDriftDetector:
    """Unit tests for _detect_cadence_drift."""

    def test_terminal_stage_dm_step_low_flagged(self):
        """Responded prospect with dm_step=1 but thread shows 3 outbound from us → drift."""
        from workflows.detect_responses import _detect_cadence_drift
        threads = [{
            "participantFullName": "Alberto Cavia",
            "totalMessageCount": "4",
            "isLastMessageFromMe": "true",
        }]
        pipeline = {
            _normalize_name("Alberto Cavia"): [{
                "stage": PipelineStage.RESPONDED.value,
                "dm_step": 1,
                "company_name": "Conagra",
                "record_id": "r1",
            }],
        }
        drifts = _detect_cadence_drift(threads, pipeline)
        assert len(drifts) == 1
        assert drifts[0]["kind"] == "terminal_dm_step_low"
        assert drifts[0]["inferred_dm_step"] == 3

    def test_terminal_stage_with_correct_dm_step_no_drift(self):
        """Responded prospect with dm_step matching thread → no drift."""
        from workflows.detect_responses import _detect_cadence_drift
        threads = [{
            "participantFullName": "Bob Bob",
            "totalMessageCount": "2",
            "isLastMessageFromMe": "false",
        }]
        pipeline = {
            _normalize_name("Bob Bob"): [{
                "stage": PipelineStage.RESPONDED.value,
                "dm_step": 1,
                "company_name": "X",
                "record_id": "r2",
            }],
        }
        drifts = _detect_cadence_drift(threads, pipeline)
        assert drifts == []

    def test_terminal_stage_stage_never_flipped(self):
        """NOT_INTERESTED stays terminal even when thread shows full outbound cadence."""
        from workflows.detect_responses import _detect_cadence_drift
        threads = [{
            "participantFullName": "Patricia Ejemplo",
            "totalMessageCount": "3",
            "isLastMessageFromMe": "true",
        }]
        pipeline = {
            _normalize_name("Patricia Ejemplo"): [{
                "stage": PipelineStage.NOT_INTERESTED.value,
                "dm_step": 0,
                "company_name": "Novo Nordisk",
                "record_id": "r3",
            }],
        }
        drifts = _detect_cadence_drift(threads, pipeline)
        # dm_step drift flagged but stage NOT one of the non-terminal kinds.
        assert len(drifts) == 1
        assert drifts[0]["kind"] == "terminal_dm_step_low"
        assert drifts[0]["current_stage"] == PipelineStage.NOT_INTERESTED.value

    def test_unreachable_stage_not_unparked_by_drift(self):
        """Wave-2-A: UNREACHABLE is terminal — a matching thread must NOT
        un-park the row back toward an active stage. Because UNREACHABLE is
        in _TERMINAL_STAGES_FOR_DRIFT, it takes the terminal branch (kind
        `terminal_dm_step_low`, same as NOT_INTERESTED) whose inferred_stage
        stays UNREACHABLE — so an auto-repair is a stage no-op, never a flip
        to a DM/RESPONDED stage. (Regression guard: drop UNREACHABLE from
        that set and this row would infer an active stage and get un-parked.)"""
        from workflows.detect_responses import _detect_cadence_drift
        threads = [{
            "participantFullName": "Daniel Mayorga",
            "totalMessageCount": "3",
            "isLastMessageFromMe": "true",
        }]
        pipeline = {
            _normalize_name("Daniel Mayorga"): [{
                "stage": PipelineStage.UNREACHABLE.value,
                "dm_step": 0,
                "company_name": "Nissan",
                "record_id": "r-unreach",
            }],
        }
        drifts = _detect_cadence_drift(threads, pipeline)
        assert len(drifts) == 1
        assert drifts[0]["current_stage"] == PipelineStage.UNREACHABLE.value
        assert drifts[0]["kind"] == "terminal_dm_step_low"
        # The crux: the row is NOT inferred onto an active stage — it stays
        # UNREACHABLE, so no repair can un-park it.
        assert drifts[0]["inferred_stage"] == PipelineStage.UNREACHABLE.value

    def test_non_terminal_attio_underestimates(self):
        """Accepted prospect with dm_step=0 but thread shows 2 outbound from us → drift up."""
        from workflows.detect_responses import _detect_cadence_drift
        threads = [{
            "participantFullName": "Rafael Ejemplo",
            "totalMessageCount": "2",
            "isLastMessageFromMe": "true",
        }]
        pipeline = {
            _normalize_name("Rafael Ejemplo"): [{
                "stage": PipelineStage.ACCEPTED.value,
                "dm_step": 0,
                "company_name": "FarmaSur",
                "record_id": "r4",
            }],
        }
        drifts = _detect_cadence_drift(threads, pipeline)
        assert len(drifts) == 1
        assert drifts[0]["kind"] == "attio_dm_step_lower_than_thread"
        assert drifts[0]["inferred_dm_step"] == 2

    def test_attio_overstates_empty_thread(self):
        """DM1 Sent in Attio but no outbound messages exist → drift down."""
        from workflows.detect_responses import _detect_cadence_drift
        threads = [{
            "participantFullName": "Phantom Send",
            "totalMessageCount": "0",
            "isLastMessageFromMe": "false",
        }]
        pipeline = {
            _normalize_name("Phantom Send"): [{
                "stage": PipelineStage.DM1_SENT.value,
                "dm_step": 1,
                "company_name": "PhantomCo",
                "record_id": "r5",
            }],
        }
        drifts = _detect_cadence_drift(threads, pipeline)
        assert len(drifts) == 1
        assert drifts[0]["kind"] == "attio_overstates_thread_empty"

    def test_no_matching_pipeline_entry_no_drift(self):
        """Inbox thread for a non-pipeline contact → no false positive."""
        from workflows.detect_responses import _detect_cadence_drift
        threads = [{
            "participantFullName": "Random Person",
            "totalMessageCount": "5",
            "isLastMessageFromMe": "true",
        }]
        drifts = _detect_cadence_drift(threads, {})
        assert drifts == []

    def test_drift_records_carry_entry_id_and_inferred_stage(self):
        """Drift dicts must include entry_id and inferred_stage so the auto-
        repair can write to Attio without re-deriving them."""
        from workflows.detect_responses import _detect_cadence_drift
        threads = [{
            "participantFullName": "Arianne Sales",
            "totalMessageCount": "5",
            "isLastMessageFromMe": "true",
        }]
        pipeline = {
            _normalize_name("Arianne Sales"): [{
                "stage": PipelineStage.DM1_SENT.value,
                "dm_step": 1,
                "company_name": "Stellantis",
                "record_id": "r-arianne",
                "entry_id": "e-arianne",
            }],
        }
        drifts = _detect_cadence_drift(threads, pipeline)
        assert len(drifts) == 1
        d = drifts[0]
        assert d["entry_id"] == "e-arianne"
        assert d["inferred_stage"] == PipelineStage.DM3_SENT.value
        assert d["inferred_dm_step"] == 3


# ── Cadence auto-repair ────────────────────────────────────────────────

class TestCadenceAutoRepair:
    """Unit tests for _apply_cadence_repairs (Phase 0.5 auto-repair)."""

    def _drift(self, **overrides) -> dict:
        base = {
            "kind": "attio_dm_step_lower_than_thread",
            "name": "Arianne Sales",
            "company": "Stellantis",
            "current_stage": PipelineStage.DM1_SENT.value,
            "current_dm_step": 1,
            "thread_total": 5,
            "thread_last_from_me": True,
            "inferred_stage": PipelineStage.DM3_SENT.value,
            "inferred_dm_step": 3,
            "record_id": "r-arianne",
            "entry_id": "e-arianne",
        }
        base.update(overrides)
        return base

    def test_applies_regressed_drift(self):
        from workflows.detect_responses import _apply_cadence_repairs
        attio = MagicMock()
        applied, failed = _apply_cadence_repairs(
            attio, list_id="L1", drifts=[self._drift()], today_iso="2026-05-20",
        )
        assert (applied, failed) == (1, 0)
        attio.update_list_entry.assert_called_once()
        call = attio.update_list_entry.call_args
        assert call.kwargs["entry_attributes"] == {
            "stage": PipelineStage.DM3_SENT.value,
            "dm_step": 3,
            "last_contact_date": "2026-05-20",
        }
        attio.create_note.assert_called_once()
        assert "Cadence auto-repair" in attio.create_note.call_args.kwargs["title"]

    def test_skips_advisory_kind_overstated(self):
        """attio_dm_step_higher_than_thread is never auto-repaired."""
        from workflows.detect_responses import _apply_cadence_repairs
        attio = MagicMock()
        applied, failed = _apply_cadence_repairs(
            attio, list_id="L1",
            drifts=[self._drift(kind="attio_dm_step_higher_than_thread")],
            today_iso="2026-05-20",
        )
        assert (applied, failed) == (0, 0)
        attio.update_list_entry.assert_not_called()

    def test_skips_advisory_kind_empty_thread(self):
        """attio_overstates_thread_empty is never auto-repaired."""
        from workflows.detect_responses import _apply_cadence_repairs
        attio = MagicMock()
        applied, failed = _apply_cadence_repairs(
            attio, list_id="L1",
            drifts=[self._drift(kind="attio_overstates_thread_empty")],
            today_iso="2026-05-20",
        )
        assert (applied, failed) == (0, 0)
        attio.update_list_entry.assert_not_called()

    def test_applies_terminal_dm_step_low(self):
        """terminal_dm_step_low IS auto-applied (dm_step monotonic-forward, stage stays)."""
        from workflows.detect_responses import _apply_cadence_repairs
        attio = MagicMock()
        d = self._drift(
            kind="terminal_dm_step_low",
            current_stage=PipelineStage.RESPONDED.value,
            inferred_stage=PipelineStage.RESPONDED.value,  # sticky
        )
        applied, failed = _apply_cadence_repairs(
            attio, list_id="L1", drifts=[d], today_iso="2026-05-20",
        )
        assert (applied, failed) == (1, 0)
        call = attio.update_list_entry.call_args
        assert call.kwargs["entry_attributes"]["stage"] == PipelineStage.RESPONDED.value

    def test_missing_entry_id_skipped(self):
        """Defensively skip drift records without entry_id (shouldn't happen in prod)."""
        from workflows.detect_responses import _apply_cadence_repairs
        attio = MagicMock()
        applied, failed = _apply_cadence_repairs(
            attio, list_id="L1",
            drifts=[self._drift(entry_id=None)],
            today_iso="2026-05-20",
        )
        assert (applied, failed) == (0, 0)
        attio.update_list_entry.assert_not_called()

    def test_failure_counted_does_not_crash(self):
        """One failed write doesn't abort the rest."""
        import httpx

        from workflows.detect_responses import _apply_cadence_repairs
        attio = MagicMock()
        # Only httpx-class exceptions are caught now (post-QA fold-in:
        # broad `except Exception` was a §3 #9 silent-failure risk).
        attio.update_list_entry.side_effect = [
            httpx.RequestError("attio down"),
            None,
        ]
        applied, failed = _apply_cadence_repairs(
            attio, list_id="L1",
            drifts=[
                self._drift(entry_id="e1", record_id="r1"),
                self._drift(entry_id="e2", record_id="r2"),
            ],
            today_iso="2026-05-20",
        )
        assert (applied, failed) == (1, 1)

    def test_update_succeeds_note_fails_counts_as_applied(self):
        """Partial-success path: state-write OK, audit-note fails.

        Pre-QA-fold-in the entry was counted as 'failed' even though
        Attio state was already modified — destroyed audit symmetry
        because the entry's stage/dm_step had advanced but the
        forensics note was missing. Post-fold-in: applied=1, failed=0;
        the audit-note gap is surfaced to stderr but does NOT flip
        the counter. Mirrors the silent-failure-hunter BLOCKER and
        the code-reviewer BLOCKER from QA round 1.
        """
        import httpx

        from workflows.detect_responses import _apply_cadence_repairs
        attio = MagicMock()
        attio.update_list_entry.return_value = None  # state-write OK
        attio.create_note.side_effect = httpx.RequestError("note quota")
        applied, failed = _apply_cadence_repairs(
            attio, list_id="L1", drifts=[self._drift()], today_iso="2026-05-20",
        )
        # State was correctly repaired; don't flip applied → failed.
        # A re-run next day would re-attempt the audit note but the
        # state-write would now be a no-op (idempotent by current_stage
        # already matching inferred_stage post-repair).
        assert (applied, failed) == (1, 0)
        attio.update_list_entry.assert_called_once()
        attio.create_note.assert_called_once()

    def test_non_httpx_exception_propagates(self):
        """Non-httpx exceptions are NOT silently counted as failed.

        Per QA-fold-in §3 #9 + silent-failure-hunter BLOCKER #2: the
        previous bare `except Exception` would swallow KeyError /
        AttributeError / future code-bug exceptions and mask them
        as 'failed repair'. Narrowed catch only includes httpx errors.
        """
        from workflows.detect_responses import _apply_cadence_repairs
        attio = MagicMock()
        attio.update_list_entry.side_effect = AttributeError("bug")
        try:
            _apply_cadence_repairs(
                attio, list_id="L1", drifts=[self._drift()],
                today_iso="2026-05-20",
            )
        except AttributeError as e:
            assert "bug" in str(e)
        else:
            raise AssertionError("AttributeError should have propagated")

    def test_malformed_drift_dict_counted_failed_not_raised(self):
        """Missing required keys → counted as failed with explicit message.

        Per QA-fold-in: drift dicts missing required keys (e.g. from
        test scaffolding or future _detect_cadence_drift refactor
        regression) used to raise KeyError inside the try block and
        be silently caught as a generic 'failed' repair. Post-fold-in:
        explicit guard fails fast with the missing-keys list logged.
        """
        from workflows.detect_responses import _apply_cadence_repairs
        attio = MagicMock()
        malformed = self._drift()
        del malformed["inferred_stage"]
        del malformed["thread_total"]
        applied, failed = _apply_cadence_repairs(
            attio, list_id="L1", drifts=[malformed], today_iso="2026-05-20",
        )
        assert (applied, failed) == (0, 1)
        # The state-write should NEVER have been attempted on a
        # malformed dict — that's the whole point of the guard.
        attio.update_list_entry.assert_not_called()


# ── End-to-end: auto-repair integration in detect_responses ───────────

class TestAutoRepairIntegration:
    """Integration tests: auto-repair triggers correctly from detect_responses."""

    def test_classifier_touched_prospects_skip_auto_repair(self):
        """When the response classifier just moved a prospect to RESPONDED,
        the cadence auto-repair must NOT overwrite that decision with the
        DM-cadence-inferred stage."""
        attio = MagicMock()
        pb = MagicMock()
        attio.query_list_entries.return_value = [
            _make_entry("e1", "r1", PipelineStage.DM1_SENT.value),
        ]
        # Manual-reply signature: stage=DM1, total=3, last from us → classifier
        # moves to RESPONDED. Drift detector would also see this as drift
        # (inferred_stage=DM3_SENT) but should defer to classifier.
        pb.download_result_csv.return_value = _make_sn_csv({
            "participantProfileUrl": "https://linkedin.com/sales/people/ACw1",
            "participantFullName": "Manual Reply Case",
            "isLastMessageFromMe": "true",
            "lastMessageBody": "Hola, gracias por escribir.",
            "lastMessageDate": "2026-04-09T12:00:00Z",
            "totalMessageCount": "3",
        })
        mock_cache = MagicMock()
        mock_cache.get.side_effect = (
            lambda rid: ("Manual Reply Case", "Co", "url", "", "Title")
            if rid == "r1" else ("", "", "", "", "")
        )
        # parse_entry pass-through: test fixture entries are already in
        # the parsed-attrs shape.
        with patch("workflows.detect_responses.AttioClient.parse_entry", side_effect=lambda e: e), \
             patch("workflows.detect_responses._pb_session_args", return_value={}):
            result = detect_responses(attio, pb, "inbox_id", cache=mock_cache)
        # Classifier should have moved to RESPONDED (one call).
        # Auto-repair must NOT have run a SECOND call with stage=DM3_SENT.
        assert attio.update_list_entry.call_count == 1
        call = attio.update_list_entry.call_args
        assert call.kwargs["entry_attributes"]["stage"] == PipelineStage.RESPONDED.value
        assert result["drift_auto_repaired"] == 0
        assert result["detected"] == 1
