"""Tests for _finalize_confirmed_dm_send (2026-06-09 desync-invariant design §1).

Invariant: the company tally records the confirmed send and is written
regardless of the person-advance outcome; a failed advance emits
dm_person_advance_desync for the same-day sweep to converge.
"""
from datetime import date
from unittest.mock import MagicMock

from models.campaign import MessageStep
from workflows.daily_check import _finalize_confirmed_dm_send


def _row() -> dict:
    return {
        "entry_ids": ["entry_1"],
        "record_id": "rec_person",
        "linkedInUrl": "https://linkedin.com/in/x",
        "current_stage": "Accepted",
    }


def _attrs() -> dict:
    return {
        "dm_step": 1,
        "last_contact_date": "2026-06-09",
        "stage": "DM1 Sent",
        "next_eligible_send_date": "2026-06-12",
    }


def _call(monkeypatch, *, advance_ok: bool):
    advance = MagicMock(return_value=advance_ok)
    tally = MagicMock()
    monkeypatch.setattr(
        "workflows.daily_check._attio_advance_with_escalation", advance
    )
    monkeypatch.setattr(
        "workflows.daily_check._write_company_throttle_tally", tally
    )
    monkeypatch.setattr(
        "workflows.daily_check._company_id_for_prospect",
        lambda attio, rid: "rec_co",
    )
    audit = MagicMock()
    updated = _finalize_confirmed_dm_send(
        attio=MagicMock(),
        row=_row(),
        step=MessageStep.DM1,
        attrs_to_update=_attrs(),
        list_id="lst",
        today=date(2026, 6, 9),
        today_str="2026-06-09",
        experiment_id="exp_1",
        audit_logger=audit,
        escalate_failures=[],
    )
    return updated, advance, tally, audit


class TestFinalizeConfirmedDmSend:
    def test_advance_fails_tally_still_written_with_desync_event(
        self, monkeypatch
    ):
        """Design test 1: failed advance -> tally STILL written (send
        happened; sibling ABM block must hold) + desync audit event
        carrying the intended attrs so the sweep/operator can replay."""
        updated, advance, tally, audit = _call(monkeypatch, advance_ok=False)
        assert updated == 0
        tally.assert_called_once()
        assert tally.call_args.kwargs["person_advance_ok"] is False
        assert tally.call_args.kwargs["company_id"] == "rec_co"
        audit.event.assert_any_call(
            "dm_person_advance_desync",
            person_record_id="rec_person",
            company_id="rec_co",
            step_label="dm1",
            failed_entry_ids=["entry_1"],
            intended_attrs=_attrs(),
        )

    def test_advance_succeeds_no_desync_event(self, monkeypatch):
        updated, advance, tally, audit = _call(monkeypatch, advance_ok=True)
        assert updated == 1
        tally.assert_called_once()
        assert tally.call_args.kwargs["person_advance_ok"] is True
        desync_calls = [
            c for c in audit.event.call_args_list
            if c.args and c.args[0] == "dm_person_advance_desync"
        ]
        assert desync_calls == []

    def test_partial_failure_counts_and_flags(self, monkeypatch):
        """Multi-entry row, one advance ok + one failed: updated counts
        the success, person_advance_ok is False, and the desync event
        names only the failed entry."""
        advance = MagicMock(side_effect=[True, False])
        tally = MagicMock()
        monkeypatch.setattr(
            "workflows.daily_check._attio_advance_with_escalation", advance
        )
        monkeypatch.setattr(
            "workflows.daily_check._write_company_throttle_tally", tally
        )
        monkeypatch.setattr(
            "workflows.daily_check._company_id_for_prospect",
            lambda attio, rid: "rec_co",
        )
        audit = MagicMock()
        row = _row()
        row["entry_ids"] = ["entry_1", "entry_2"]
        updated = _finalize_confirmed_dm_send(
            attio=MagicMock(),
            row=row,
            step=MessageStep.DM1,
            attrs_to_update=_attrs(),
            list_id="lst",
            today=date(2026, 6, 9),
            today_str="2026-06-09",
            experiment_id="exp_1",
            audit_logger=audit,
            escalate_failures=[],
        )
        assert updated == 1
        assert tally.call_args.kwargs["person_advance_ok"] is False
        desync = [
            c for c in audit.event.call_args_list
            if c.args and c.args[0] == "dm_person_advance_desync"
        ]
        assert len(desync) == 1
        assert desync[0].kwargs["failed_entry_ids"] == ["entry_2"]

    def test_no_record_id_skips_tally(self, monkeypatch):
        advance = MagicMock(return_value=True)
        tally = MagicMock()
        monkeypatch.setattr(
            "workflows.daily_check._attio_advance_with_escalation", advance
        )
        monkeypatch.setattr(
            "workflows.daily_check._write_company_throttle_tally", tally
        )
        updated = _finalize_confirmed_dm_send(
            attio=MagicMock(),
            row={"entry_ids": ["entry_1"], "record_id": None,
                 "linkedInUrl": "", "current_stage": "Accepted"},
            step=MessageStep.DM1,
            attrs_to_update=_attrs(),
            list_id="lst",
            today=date(2026, 6, 9),
            today_str="2026-06-09",
            experiment_id=None,
            audit_logger=MagicMock(),
            escalate_failures=[],
        )
        assert updated == 1
        tally.assert_not_called()
