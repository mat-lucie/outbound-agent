"""Tests for scripts/backfill_persona_reclassify.py — Part 2 of the
persona enterprise reclassification fix (plan 2026-05-31).

Test matrix:
  (a) Mislabeled digitalization_champions CEO at PROSPECT stage
      → eligible change; dry-run makes 0 writes; apply writes 1 row.
  (b) Mislabeled digitalization_champions title at DM2_SENT stage
      → changed BUT excluded; never written in either mode.
  (c) Correctly-labeled row (operations_leaders CEO already) at PROSPECT
      → no change; nothing written.
  (d) Genuine innovation/digital title at PROSPECT
      → stays digitalization_champions; no change needed.

All Attio I/O is mocked — no network.

## Mocking strategy
``patch('scripts.backfill_persona_reclassify.AttioClient')`` replaces the
*class* with a MagicMock, which also replaces ``AttioClient.parse_entry``
(a staticmethod) with a MagicMock that returns MagicMock objects. This
would silently break ``_build_diff_report``. To avoid that, all tests
additionally restore the real ``parse_entry`` implementation via
``patch.object(real_AttioClient, 'parse_entry', real_parse_entry)``. The
outer context-manager instance is wired via ``MockAttio.return_value``
instead of ``side_effect`` so that ``with AttioClient() as attio:`` returns
the pre-configured ``outer_instance``.
"""
from __future__ import annotations

import pathlib
from unittest.mock import MagicMock, patch

import pytest

# Real parse_entry — we need to restore it when AttioClient is mocked.
from clients.attio import AttioClient as _RealAttioClient

_real_parse_entry = _RealAttioClient.parse_entry


# ---------------------------------------------------------------------------
# Helpers: build fake Attio raw entries + person records
# ---------------------------------------------------------------------------

def _raw_entry(entry_id: str, record_id: str, stage: str, persona: str) -> dict:
    """Minimal raw Attio list-entry shape that AttioClient.parse_entry can decode."""
    return {
        "id": {"entry_id": entry_id, "record_id": record_id},
        "entry_id": entry_id,
        "parent_record_id": record_id,
        "created_at": "2026-01-01T00:00:00Z",
        "entry_values": {
            "stage": [{"status": {"title": stage}}],
            "persona": [{"attribute_type": "select", "option": {"title": persona}}],
        },
    }


def _person_record(record_id: str, name: str, title: str) -> dict:
    """Minimal Attio person-record shape for get_person mocks."""
    first, *rest = name.split(" ", 1)
    last = rest[0] if rest else ""
    return {
        "id": {"record_id": record_id},
        "values": {
            "name": [{"first_name": first, "last_name": last}],
            "job_title": [{"value": title}],
        },
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_entries():
    """Four fake entries covering the test matrix."""
    return [
        # (a) Mislabeled CEO at PROSPECT — eligible change
        _raw_entry("e-ceo-prospect", "r-ceo", "Prospect", "digitalization_champions"),
        # (b) Mislabeled CEO at DM2_SENT — changed but excluded
        _raw_entry("e-ceo-dm2", "r-ceo-dm2", "DM2 Sent", "digitalization_champions"),
        # (c) Correctly labeled operations_leaders CEO at PROSPECT — no change
        _raw_entry("e-ops-correct", "r-ops", "Prospect", "operations_leaders"),
        # (d) Genuine digitalization title at PROSPECT — stays, no change
        _raw_entry("e-digital", "r-digital", "Prospect", "digitalization_champions"),
    ]


@pytest.fixture
def fake_persons():
    """Person records keyed by record_id."""
    return {
        "r-ceo":     _person_record("r-ceo",     "Ana Ramos",    "CEO"),
        "r-ceo-dm2": _person_record("r-ceo-dm2", "Luis Vega",    "Director General"),
        "r-ops":     _person_record("r-ops",      "Pedro Salas",  "CEO"),
        "r-digital": _person_record("r-digital",  "Carla Brito",  "Digital Transformation Manager"),
    }


# ---------------------------------------------------------------------------
# Context-manager helper
# ---------------------------------------------------------------------------

def _make_patch_ctx(fake_entries, fake_persons, run_writer_mock=None):
    """Return a pre-wired patch context for the backfill script.

    The strategy:
    - ``MockAttio.return_value`` is the outer context-manager instance; we
      configure ``__enter__.return_value = outer_instance`` so
      ``with AttioClient() as attio:`` binds the correct object.
    - The real ``parse_entry`` staticmethod is restored via
      ``patch.object`` so ``_build_diff_report`` can parse entries normally.
    - Worker instances created in the ThreadPoolExecutor call
      ``AttioClient()`` again; their ``__enter__`` returns themselves
      (MagicMock default), but only ``update_list_entry`` is exercised.
    - A no-op ``ReclassificationRunWriter`` mock prevents Attio network
      calls during the run's ``__exit__`` flush.
    """
    if run_writer_mock is None:
        run_writer_mock = MagicMock()
        run_writer_mock.__enter__ = MagicMock(return_value=run_writer_mock)
        run_writer_mock.__exit__ = MagicMock(return_value=False)
        run_writer_mock.run_id = "testrunid"

    outer_instance = MagicMock()
    outer_instance.__enter__.return_value = outer_instance
    outer_instance.__exit__.return_value = False
    outer_instance.query_list_entries.return_value = fake_entries
    outer_instance.get_person.side_effect = lambda rid: fake_persons.get(rid)
    outer_instance.update_list_entry.return_value = {}

    worker_instance = MagicMock()
    worker_instance.__enter__.return_value = worker_instance
    worker_instance.__exit__.return_value = False
    worker_instance.update_list_entry.return_value = {}

    return outer_instance, worker_instance, run_writer_mock


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDryRun:
    """--dry-run (default) must never call update_list_entry."""

    def test_zero_writes(self, fake_entries, fake_persons):
        outer_instance, worker_instance, rw_mock = _make_patch_ctx(
            fake_entries, fake_persons
        )

        with patch("scripts.backfill_persona_reclassify.AttioClient") as MockAttio, \
             patch("scripts.backfill_persona_reclassify.ReclassificationRunWriter",
                   return_value=rw_mock), \
             patch.dict("os.environ", {"ATTIO_LIST_ID": "test-list-uuid"}), \
             patch.object(pathlib.Path, "mkdir"), \
             patch.object(pathlib.Path, "write_text"), \
             patch("scripts.backfill_persona_reclassify._parse_entry",
                   side_effect=_real_parse_entry):
            # Wire outer context manager
            MockAttio.return_value = outer_instance
            # Worker calls reuse outer_instance (no write happens in dry-run)
            MockAttio.side_effect = None

            from click.testing import CliRunner

            from scripts.backfill_persona_reclassify import main
            runner = CliRunner()
            result = runner.invoke(main, [])  # no --apply → dry-run

        assert result.exit_code == 0, result.output
        outer_instance.update_list_entry.assert_not_called()
        worker_instance.update_list_entry.assert_not_called()


class TestApplyMode:
    """--apply must write ONLY the eligible changed row."""

    def test_writes_only_eligible(self, fake_entries, fake_persons):
        outer_instance, worker_instance, rw_mock = _make_patch_ctx(
            fake_entries, fake_persons
        )

        with patch("scripts.backfill_persona_reclassify.AttioClient") as MockAttio, \
             patch("scripts.backfill_persona_reclassify.ReclassificationRunWriter",
                   return_value=rw_mock), \
             patch.dict("os.environ", {"ATTIO_LIST_ID": "test-list-uuid"}), \
             patch.object(pathlib.Path, "mkdir"), \
             patch.object(pathlib.Path, "write_text"), \
             patch("scripts.backfill_persona_reclassify._parse_entry",
                   side_effect=_real_parse_entry):
            # First call (outer context) → outer_instance;
            # subsequent calls (workers) → worker_instance
            MockAttio.side_effect = [outer_instance, worker_instance]

            from click.testing import CliRunner

            from scripts.backfill_persona_reclassify import main
            runner = CliRunner()
            result = runner.invoke(main, ["--apply"])

        assert result.exit_code == 0, result.output

        # Exactly one write: entry (a) e-ceo-prospect
        assert worker_instance.update_list_entry.call_count == 1
        call_kwargs = worker_instance.update_list_entry.call_args
        called_entry_id = (
            call_kwargs.kwargs.get("entry_id")
            or (call_kwargs.args[0] if call_kwargs.args else None)
        )
        assert called_entry_id == "e-ceo-prospect", (
            f"Expected write for e-ceo-prospect, got {called_entry_id}"
        )
        # Payload must not include stage
        attrs = call_kwargs.kwargs.get("entry_attributes") or {}
        assert "stage" not in attrs, "PATCH payload must never include stage"
        assert attrs.get("persona") == "operations_leaders"

    def test_excluded_active_never_written(self, fake_entries, fake_persons):
        """DM2_SENT entry must never be written, even under --apply."""
        outer_instance, worker_instance, rw_mock = _make_patch_ctx(
            fake_entries, fake_persons
        )

        with patch("scripts.backfill_persona_reclassify.AttioClient") as MockAttio, \
             patch("scripts.backfill_persona_reclassify.ReclassificationRunWriter",
                   return_value=rw_mock), \
             patch.dict("os.environ", {"ATTIO_LIST_ID": "test-list-uuid"}), \
             patch.object(pathlib.Path, "mkdir"), \
             patch.object(pathlib.Path, "write_text"), \
             patch("scripts.backfill_persona_reclassify._parse_entry",
                   side_effect=_real_parse_entry):
            MockAttio.side_effect = [outer_instance, worker_instance]

            from click.testing import CliRunner

            from scripts.backfill_persona_reclassify import main
            runner = CliRunner()
            result = runner.invoke(main, ["--apply"])

        assert result.exit_code == 0, result.output

        for c in worker_instance.update_list_entry.call_args_list:
            entry_id_called = (
                c.kwargs.get("entry_id")
                or (c.args[0] if c.args else None)
            )
            assert entry_id_called != "e-ceo-dm2", (
                "DM2 Sent entry must never be written to Attio"
            )


class TestSummaryCounts:
    """Output summary line counts must be accurate."""

    def test_summary_counts(self, fake_entries, fake_persons):
        outer_instance, worker_instance, rw_mock = _make_patch_ctx(
            fake_entries, fake_persons
        )

        with patch("scripts.backfill_persona_reclassify.AttioClient") as MockAttio, \
             patch("scripts.backfill_persona_reclassify.ReclassificationRunWriter",
                   return_value=rw_mock), \
             patch.dict("os.environ", {"ATTIO_LIST_ID": "test-list-uuid"}), \
             patch.object(pathlib.Path, "mkdir"), \
             patch.object(pathlib.Path, "write_text"), \
             patch("scripts.backfill_persona_reclassify._parse_entry",
                   side_effect=_real_parse_entry):
            MockAttio.side_effect = [outer_instance, worker_instance]

            from click.testing import CliRunner

            from scripts.backfill_persona_reclassify import main
            runner = CliRunner()
            result = runner.invoke(main, ["--apply"])

        output = result.output
        assert "Examined     : 4" in output, output
        # 2 changed: (a) CEO@PROSPECT and (b) CEO@DM2_SENT
        assert "Changed      : 2" in output, output
        # 1 eligible write: (a) only
        assert "Written      : 1" in output, output
        # 1 excluded by stage: (b) DM2_SENT
        assert "Excl (stage) : 1" in output, output
        # 2 unchanged: (c) correctly labeled + (d) genuine digital title
        assert "Unchanged    : 2" in output, output
        assert "Errors       : 0" in output, output


class TestEnterpriseOnlyGuard:
    """Midmarket personas (target_company_mode) and null/empty-title entries
    must NEVER be reclassified — _classify_persona only emits enterprise
    personas, so recomputing a midmarket label would overwrite a correct value."""

    GUARD_ENTRIES = [
        # midmarket CEO at PROSPECT — _classify_persona would say operations_leaders,
        # but this must be LEFT ALONE (would corrupt a correct midmarket label).
        _raw_entry("e-mid-ceo", "r-mid-ceo", "Prospect", "mx_midmarket_manufacturing"),
        # enterprise entry with a blank title — no signal, must be skipped.
        _raw_entry("e-blank", "r-blank", "Prospect", "digitalization_champions"),
        # enterprise entry whose linked person record is missing (fetch drop) —
        # must be counted as no_person, NOT silently folded into "unchanged".
        _raw_entry("e-ghost", "r-ghost", "Prospect", "digitalization_champions"),
        # control: a genuinely mislabeled enterprise CEO that SHOULD change.
        _raw_entry("e-ent-ceo", "r-ent-ceo", "Prospect", "digitalization_champions"),
    ]
    GUARD_PERSONS = {
        "r-mid-ceo": _person_record("r-mid-ceo", "Sofia Luna", "CEO"),
        "r-blank":   _person_record("r-blank",   "No Title",   ""),
        # r-ghost intentionally absent → get_person returns None
        "r-ent-ceo": _person_record("r-ent-ceo", "Diego Mora", "CEO"),
    }

    def test_midmarket_and_blank_never_written_only_enterprise_changes(self):
        outer_instance, worker_instance, rw_mock = _make_patch_ctx(
            self.GUARD_ENTRIES, self.GUARD_PERSONS
        )

        with patch("scripts.backfill_persona_reclassify.AttioClient") as MockAttio, \
             patch("scripts.backfill_persona_reclassify.ReclassificationRunWriter",
                   return_value=rw_mock), \
             patch.dict("os.environ", {"ATTIO_LIST_ID": "test-list-uuid"}), \
             patch.object(pathlib.Path, "mkdir"), \
             patch.object(pathlib.Path, "write_text"), \
             patch("scripts.backfill_persona_reclassify._parse_entry",
                   side_effect=_real_parse_entry):
            MockAttio.side_effect = [outer_instance, worker_instance]

            from click.testing import CliRunner

            from scripts.backfill_persona_reclassify import main
            runner = CliRunner()
            result = runner.invoke(main, ["--apply"])

        assert result.exit_code == 0, result.output
        # Only the enterprise CEO is written — midmarket + blank + ghost skipped.
        assert worker_instance.update_list_entry.call_count == 1
        written_id = (
            worker_instance.update_list_entry.call_args.kwargs.get("entry_id")
            or worker_instance.update_list_entry.call_args.args[0]
        )
        assert written_id == "e-ent-ceo"
        assert "Changed      : 1" in result.output, result.output
        # Skip buckets must be surfaced distinctly (no silent drops).
        assert "Out of scope : 1" in result.output, result.output   # midmarket
        assert "Empty title  : 1" in result.output, result.output   # blank title
        assert "No person    : 1" in result.output, result.output   # fetch drop


class TestReClassifyPersonaLogic:
    """Unit-test _classify_persona for the specific titles in the matrix."""

    def test_ceo_is_operations_leader(self):
        from workflows.quality_gate import _classify_persona
        assert _classify_persona("CEO") == "operations_leaders"

    def test_gerente_general_is_operations_leader(self):
        from workflows.quality_gate import _classify_persona
        assert _classify_persona("Director General") == "operations_leaders"

    def test_digital_transformation_manager_is_digitalization(self):
        from workflows.quality_gate import _classify_persona
        assert _classify_persona("Digital Transformation Manager") == "digitalization_champions"

    def test_reclassify_is_idempotent(self):
        """Running classify twice on the same title gives the same result."""
        from workflows.quality_gate import _classify_persona
        title = "VP Operaciones"
        assert _classify_persona(title) == _classify_persona(title)


class TestStageFilter:
    """_is_eligible_stage and _is_active_cadence_stage must partition correctly."""

    def test_prospect_is_eligible(self):
        from scripts.backfill_persona_reclassify import _is_eligible_stage
        assert _is_eligible_stage("Prospect") is True

    def test_connection_sent_is_eligible(self):
        from scripts.backfill_persona_reclassify import _is_eligible_stage
        assert _is_eligible_stage("Connection Sent") is True

    def test_accepted_is_eligible(self):
        from scripts.backfill_persona_reclassify import _is_eligible_stage
        assert _is_eligible_stage("Accepted") is True

    def test_dm1_sent_is_not_eligible(self):
        from scripts.backfill_persona_reclassify import _is_eligible_stage
        assert _is_eligible_stage("DM1 Sent") is False

    def test_dm2_sent_is_not_eligible(self):
        from scripts.backfill_persona_reclassify import _is_eligible_stage
        assert _is_eligible_stage("DM2 Sent") is False

    def test_dm3_sent_is_not_eligible(self):
        from scripts.backfill_persona_reclassify import _is_eligible_stage
        assert _is_eligible_stage("DM3 Sent") is False

    def test_nurture_is_not_eligible(self):
        from scripts.backfill_persona_reclassify import _is_eligible_stage
        assert _is_eligible_stage("Nurture") is False

    def test_responded_is_not_eligible(self):
        from scripts.backfill_persona_reclassify import _is_eligible_stage
        assert _is_eligible_stage("Responded") is False

    def test_unknown_stage_is_not_eligible(self):
        """Unknown stage string → treated as active (do not write)."""
        from scripts.backfill_persona_reclassify import _is_eligible_stage
        assert _is_eligible_stage("some_unknown_stage") is False
