"""Layer 2 of the Pattern-A re-prospecting fix: advance prospects PB reports as
already-invited (input-already-processed) to CONNECTION_SENT so they leave the
invite pool instead of re-queuing forever.

Covers ``_advance_already_processed_rows`` directly (the advance-gate branch glue
is thin: it calls this helper only when ``SendOutcome.already_processed`` is True
AND the batch is invites-only).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from clients.attio_writer import AttioError
from models.pipeline import PipelineStage
from workflows.daily_check import _advance_already_processed_rows


def _row(suffix: str, experiment_id: str | None = "exp-1",
         frozen_at: str | None = "prospect") -> dict:
    return {
        "linkedInUrl": f"https://www.linkedin.com/in/{suffix}",
        "entry_id": f"ent-{suffix}",
        "record_id": f"rec-{suffix}",
        "current_stage": "Prospect",
        "experiment_id": experiment_id,
        "experiment_id_frozen_at": frozen_at,
    }


def test_advances_each_row_to_connection_sent():
    attio = MagicMock()
    n = _advance_already_processed_rows(
        [_row("a"), _row("b")],
        attio=attio, list_id="list-id", today="2026-05-29",
    )
    assert n == 2
    calls = {
        c.kwargs["entry_id"]: c.kwargs["entry_attributes"]
        for c in attio.update_list_entry.call_args_list
    }
    assert calls["ent-a"]["stage"] == PipelineStage.CONNECTION_SENT.value
    assert calls["ent-a"]["experiment_id_frozen_at"] == "connection_sent"
    assert "last_contact_date" in calls["ent-a"]
    # C1: no invite is happening now — the prior experiment_id must be PRESERVED,
    # never (re)written, so we don't back-fill the currently-running cohort.
    assert "experiment_id" not in calls["ent-a"]
    assert calls["ent-b"]["stage"] == PipelineStage.CONNECTION_SENT.value


def test_no_experiment_id_omits_frozen_at_stamp():
    attio = MagicMock()
    _advance_already_processed_rows(
        [_row("a", experiment_id=None)],
        attio=attio, list_id="list-id", today="2026-05-29",
    )
    entry_attrs = attio.update_list_entry.call_args.kwargs["entry_attributes"]
    assert entry_attrs["stage"] == PipelineStage.CONNECTION_SENT.value
    assert "experiment_id_frozen_at" not in entry_attrs
    assert "experiment_id" not in entry_attrs


def test_terminal_frozen_at_is_not_restamped():
    """I1: a row whose prior frozen_at is terminal (accepted/legacy_*) must NOT
    be re-stamped to connection_sent (immutability violation). The stage still
    advances, but the immutable cohort tag is preserved."""
    attio = MagicMock()
    _advance_already_processed_rows(
        [_row("a", experiment_id="exp-1", frozen_at="accepted")],
        attio=attio, list_id="list-id", today="2026-05-29",
    )
    entry_attrs = attio.update_list_entry.call_args.kwargs["entry_attributes"]
    assert entry_attrs["stage"] == PipelineStage.CONNECTION_SENT.value
    assert "experiment_id_frozen_at" not in entry_attrs


def test_partial_failure_counts_only_successful_advances():
    """A failed write is not silent (AttioWriter opens the DLQ row) and must not
    be counted as advanced; the row stays at its prior stage to retry."""
    attio = MagicMock()
    # Fail the write for ent-a, succeed for ent-b.
    def _apply(intent):
        if intent.record_id == "ent-a":
            raise AttioError("simulated write failure")
        return None
    with patch("clients.attio_writer.AttioWriter.apply", side_effect=_apply):
        n = _advance_already_processed_rows(
            [_row("a"), _row("b")],
            attio=attio, list_id="list-id", today="2026-05-29",
        )
    assert n == 1  # only ent-b advanced


def test_empty_batch_advances_nothing():
    attio = MagicMock()
    n = _advance_already_processed_rows(
        [], attio=attio, list_id="list-id", today="2026-05-29",
    )
    assert n == 0
    assert not attio.update_list_entry.called
