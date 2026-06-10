"""Tests for workflows/pb_advance_gate.py — dry-skip + dead-end emit."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from clients.pb_envelope import PBLaunch, SendOutcome, hash_arguments
from workflows.audit import AuditLogger
from workflows.pb_advance_gate import emit_pb_inmail_dead_end, emit_pb_silent_no_op


def _launch() -> PBLaunch:
    return PBLaunch(
        container_id="c_DRY",
        agent_id="ag_msg",
        launched_at=datetime(2026, 5, 21, 12, 0, tzinfo=UTC),
        arguments_sha256=hash_arguments({"sheet": "u"}),
        request_id="req_x",
    )


def _failed_outcome() -> SendOutcome:
    return SendOutcome(
        container_id="c_DRY",
        csv_status="Empty",
        sent_count=0,
        requested_count=3,
        drift_skipped_reason="pb_returned_no_csv",
        next_day_drift_key="ag_msg:2026-05-21:hash",
        sent_urls=frozenset(),
        skipped_urls=frozenset(),
    )


class TestEmitPbSilentNoOp:
    def test_escalate_called_with_typed_payload(self) -> None:
        with patch("workflows.pb_advance_gate.escalate") as mock_esc:
            emit_pb_silent_no_op(
                _launch(),
                _failed_outcome(),
                attio=MagicMock(),
            )
            assert mock_esc.called
            call_kwargs = mock_esc.call_args.kwargs
            assert call_kwargs["type"] == "pb_silent_no_op"
            assert call_kwargs["idempotency_key"] == "c_DRY"
            payload = call_kwargs["payload"]
            assert payload["container_id"] == "c_DRY"
            assert payload["agent_id"] == "ag_msg"
            assert payload["sent_count"] == 0
            assert payload["requested_count"] == 3
            assert payload["csv_status"] == "Empty"
            assert payload["next_day_drift_key"] == "ag_msg:2026-05-21:hash"

    def test_idempotency_key_is_container_id(self) -> None:
        """Repeat runs of the same batch must NOT open duplicate queue rows."""
        with patch("workflows.pb_advance_gate.escalate") as mock_esc:
            launch = _launch()
            emit_pb_silent_no_op(launch, _failed_outcome(), attio=MagicMock())
            assert mock_esc.call_args.kwargs["idempotency_key"] == launch.container_id

    def test_audit_logger_records_drift_key(self, tmp_path) -> None:
        """When audit logger is provided, next_day_drift_key is logged so
        PR-19's drift detector can correlate."""
        with patch("workflows.pb_advance_gate.escalate") as _esc:
            log_path = tmp_path / "audit.jsonl"
            with patch(
                "workflows.audit._audit_path",
                return_value=log_path,
            ), AuditLogger(workflow="test", run_id="r_abc") as log:
                emit_pb_silent_no_op(
                    _launch(),
                    _failed_outcome(),
                    attio=MagicMock(),
                    audit_logger=log,
                )

            text = log_path.read_text()
            assert "pb_silent_no_op" in text
            assert "ag_msg:2026-05-21:hash" in text
            assert "c_DRY" in text

    def test_audit_logger_optional(self) -> None:
        """No audit logger → just emit queue row, no error."""
        with patch("workflows.pb_advance_gate.escalate") as mock_esc:
            emit_pb_silent_no_op(
                _launch(),
                _failed_outcome(),
                attio=MagicMock(),
                audit_logger=None,
            )
            assert mock_esc.called

    def test_experiment_id_propagates_to_payload(self) -> None:
        with patch("workflows.pb_advance_gate.escalate") as mock_esc:
            emit_pb_silent_no_op(
                _launch(),
                _failed_outcome(),
                attio=MagicMock(),
                experiment_id="exp_2026_05_21",
            )
            assert mock_esc.call_args.kwargs["payload"]["experiment_id"] == "exp_2026_05_21"

    def test_skipped_urls_propagates_as_sorted_list(self) -> None:
        """`skipped_urls` is converted from frozenset to sorted list at
        payload-emit time so Attio's JSON serializer can encode it."""
        outcome = SendOutcome(
            container_id="c_DRY",
            csv_status="Skipped",
            sent_count=0,
            requested_count=3,
            drift_skipped_reason="pb_csv_zero_sent_rows",
            next_day_drift_key="ag_msg:2026-05-21:hash",
            sent_urls=frozenset(),
            skipped_urls=frozenset({"https://linkedin.com/in/z", "https://linkedin.com/in/a"}),
        )
        with patch("workflows.pb_advance_gate.escalate") as mock_esc:
            emit_pb_silent_no_op(_launch(), outcome, attio=MagicMock())
            payload = mock_esc.call_args.kwargs["payload"]
            assert isinstance(payload["skipped_urls"], list)
            assert payload["skipped_urls"] == sorted(outcome.skipped_urls)


class TestEmitPbInmailDeadEnd:
    """Per-URL InMail/Invite dead-end emit."""

    def test_idempotency_key_is_url_pipe_step(self) -> None:
        with patch("workflows.pb_advance_gate.escalate") as mock_esc:
            emit_pb_inmail_dead_end(
                _launch(),
                linkedin_url="https://linkedin.com/in/alice",
                dm_step="dm2",
                pb_status="skipped_in_csv",
                attio=MagicMock(),
            )
            assert mock_esc.call_args.kwargs["idempotency_key"] == (
                "https://linkedin.com/in/alice|dm2"
            )

    def test_payload_shape(self) -> None:
        with patch("workflows.pb_advance_gate.escalate") as mock_esc:
            emit_pb_inmail_dead_end(
                _launch(),
                linkedin_url="https://linkedin.com/in/bob",
                dm_step="invite",
                pb_status="skipped_in_csv",
                attio=MagicMock(),
                experiment_id="exp_x",
            )
            payload = mock_esc.call_args.kwargs["payload"]
            assert payload["linkedin_url"] == "https://linkedin.com/in/bob"
            assert payload["dm_step"] == "invite"
            assert payload["container_id"] == "c_DRY"
            assert payload["pb_status"] == "skipped_in_csv"
            assert payload["experiment_id"] == "exp_x"
            assert mock_esc.call_args.kwargs["type"] == "pb_inmail_dead_end"

    def test_audit_logger_records_event(self, tmp_path) -> None:
        with patch("workflows.pb_advance_gate.escalate"):
            log_path = tmp_path / "audit.jsonl"
            with patch("workflows.audit._audit_path", return_value=log_path), \
                 AuditLogger(workflow="test", run_id="r_inmail") as log:
                emit_pb_inmail_dead_end(
                    _launch(),
                    linkedin_url="https://linkedin.com/in/carol",
                    dm_step="dm1",
                    pb_status="skipped_in_csv",
                    attio=MagicMock(),
                    audit_logger=log,
                )

            text = log_path.read_text()
            assert "pb_inmail_dead_end" in text
            assert "linkedin.com/in/carol" in text
            assert "dm1" in text

    def test_audit_logger_optional(self) -> None:
        with patch("workflows.pb_advance_gate.escalate") as mock_esc:
            emit_pb_inmail_dead_end(
                _launch(),
                linkedin_url="https://linkedin.com/in/dan",
                dm_step="dm3",
                pb_status="skipped_in_csv",
                attio=MagicMock(),
                audit_logger=None,
            )
            assert mock_esc.called
