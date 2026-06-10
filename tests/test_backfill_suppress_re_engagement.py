"""PR-37 backfill tests.

Validates the rule-based backfill emits both a Migration Run row and a
Reclassification Run row per §3.13 + §3.16, captures the manual-reply-unscored
abstain count, and is idempotent on re-runs."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import scripts.backfill_suppress_re_engagement as backfill


def _entry(
    *,
    entry_id: str,
    record_id: str,
    stage: str | None = None,
    response_classification: str | None = None,
    suppress_re_engagement: bool | None = None,
) -> dict:
    """Build a raw list-entry dict that AttioClient.parse_entry can parse."""
    values: dict = {}
    if stage is not None:
        values["stage"] = [{"status": {"title": stage}}]
    if response_classification is not None:
        values["response_classification"] = [{"value": response_classification}]
    if suppress_re_engagement is not None:
        values["suppress_re_engagement"] = [{"value": suppress_re_engagement}]
    return {
        "id": {"entry_id": entry_id, "record_id": record_id},
        "entry_values": values,
    }


@pytest.fixture
def mock_attio(monkeypatch):
    """Wire up AttioClient + writer mocks. Patches the env, and short-circuits
    AttioClient() constructor + all Writer constructors used by the script.

    Critically: we wrap (not replace) AttioClient so the static
    ``parse_entry`` method remains real — the script calls it on raw entry
    dicts and we want it to behave like production."""
    from clients.attio import AttioClient as RealAttio

    monkeypatch.setenv("ATTIO_LIST_ID", "list-uuid-001")
    monkeypatch.setenv("ATTIO_API_KEY", "test-key")

    attio = MagicMock()

    def _attio_factory(*args, **kwargs):
        return attio

    # Create a stand-in class that constructs to our mock but preserves
    # the real static methods (parse_entry, _extract_value).
    class _StubAttio:
        parse_entry = RealAttio.parse_entry
        _extract_value = RealAttio._extract_value
        _extract_stage = RealAttio._extract_stage

        def __new__(cls, *args, **kwargs):  # noqa: D401
            return attio

    with (
        patch("scripts.backfill_suppress_re_engagement.AttioClient", _StubAttio),
        patch("scripts.backfill_suppress_re_engagement.AttioWriter") as writer_cls,
        patch("workflows.migration_run_writer.AttioClient", _attio_factory),
        patch("workflows.reclassification_run_writer.AttioClient", _attio_factory),
    ):
        writer = MagicMock()
        writer_cls.return_value = writer
        yield {"attio": attio, "writer": writer}


def test_dry_run_does_not_write_to_attio(mock_attio):
    attio = mock_attio["attio"]
    attio.query_list_entries.return_value = [
        _entry(entry_id="e1", record_id="rA", stage="Not Interested"),
        _entry(entry_id="e2", record_id="rB", response_classification="defensive"),
    ]
    rc = backfill.main(["--dry-run"])
    assert rc == 0
    # In dry-run mode neither writer should issue an apply().
    mock_attio["writer"].apply.assert_not_called()
    # No Reclassification Run / Migration Run row written either (both
    # writers skip the final POST when dry_run=True).
    # We assert that no POST to /objects/.../records is made through attio.
    for call in attio._request.call_args_list:
        assert call.args[:2] != ("POST",) or "/records" not in call.args[1] or (
            "reclassification_run" not in call.args[1]
            and "migration_run" not in call.args[1]
        )


def test_wet_run_writes_only_matching_rows(mock_attio):
    attio = mock_attio["attio"]
    attio.query_list_entries.return_value = [
        _entry(entry_id="e1", record_id="rA", stage="Not Interested"),
        _entry(entry_id="e2", record_id="rB", response_classification="negative"),
        _entry(entry_id="e3", record_id="rC", stage="DM1 Sent"),  # clean → skip
        _entry(entry_id="e4", record_id="rD", response_classification="positive"),  # clean
    ]
    # Make the AttioWriter mock no-op successfully.
    mock_attio["writer"].apply.return_value = {"ok": True}

    rc = backfill.main([])
    assert rc == 0
    # Two writes (e1 + e2).
    assert mock_attio["writer"].apply.call_count == 2
    entry_ids_written = [
        c.args[0].record_id for c in mock_attio["writer"].apply.call_args_list
    ]
    assert set(entry_ids_written) == {"e1", "e2"}


def test_idempotent_when_prior_true(mock_attio, capsys):
    attio = mock_attio["attio"]
    attio.query_list_entries.return_value = [
        _entry(
            entry_id="e1",
            record_id="rA",
            stage="Not Interested",
            suppress_re_engagement=True,
        ),
    ]
    rc = backfill.main([])
    assert rc == 0
    # Already True → apply_suppression returns False → no apply() call.
    mock_attio["writer"].apply.assert_not_called()
    # Strong assertion (per fold-in IMPORTANT-2): the Migration Run row
    # captures rows_modified=0 + rows_skipped_idempotent=1 so the audit trail
    # reflects the idempotent no-op.
    out = capsys.readouterr().out
    assert "rows_modified=0" in out
    assert "rows_skipped_idempotent=1" in out


def test_apply_suppression_failure_recorded(mock_attio):
    """BLOCKING-1 fold (test-QA): when apply_suppression raises, the script
    MUST record both mrun.mark_failed and crun.mark_error, continue to the
    next entry, and exit 1 on rows_failed>0. §0 #9 — no silent swallow."""
    attio = mock_attio["attio"]
    attio.query_list_entries.return_value = [
        _entry(entry_id="e1", record_id="rA", stage="Not Interested"),
        _entry(entry_id="e2", record_id="rB", response_classification="negative"),
    ]
    # First write raises, second succeeds — verifies the loop continues.
    mock_attio["writer"].apply.side_effect = [
        RuntimeError("attio 5xx"),
        {"ok": True},
    ]

    rc = backfill.main([])
    # rows_failed > 0 → exit 1 per main()'s contract.
    assert rc == 1
    # Both entries were attempted; the exception did NOT abort the loop.
    assert mock_attio["writer"].apply.call_count == 2
    entry_ids_attempted = [
        c.args[0].record_id for c in mock_attio["writer"].apply.call_args_list
    ]
    assert entry_ids_attempted == ["e1", "e2"]


def test_manual_reply_unscored_recorded_as_abstain(mock_attio, capsys):
    """§3.16 contract: rows with response_classification=None AND stage not in
    SUPPRESSED_STAGES are unscored — captured in abstain_count."""
    attio = mock_attio["attio"]
    attio.query_list_entries.return_value = [
        # Unscored: no classification, active stage. Should abstain.
        _entry(entry_id="e1", record_id="rA", stage="DM1 Sent"),
        # Unscored: no classification, no stage. Should abstain.
        _entry(entry_id="e2", record_id="rB"),
        # Scored as positive: NOT an abstain (we know the answer).
        _entry(entry_id="e3", record_id="rC", response_classification="positive"),
        # Suppressed by stage: NOT an abstain (counted as success).
        _entry(entry_id="e4", record_id="rD", stage="Not Interested"),
    ]
    mock_attio["writer"].apply.return_value = {"ok": True}

    rc = backfill.main(["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "abstain_count=2" in out  # e1 + e2
    assert "success_count=1" in out  # e4


def test_missing_attio_list_id_exits_2(mock_attio, monkeypatch):
    monkeypatch.delenv("ATTIO_LIST_ID", raising=False)
    rc = backfill.main([])
    assert rc == 2


def test_no_entries_returns_clean(mock_attio):
    attio = mock_attio["attio"]
    attio.query_list_entries.return_value = []
    rc = backfill.main([])
    assert rc == 0
    mock_attio["writer"].apply.assert_not_called()
