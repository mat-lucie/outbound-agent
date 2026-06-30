"""Tests for daily_check.compute_dm1_sent_cohort_by_date."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

from models.pipeline import PipelineStage
from workflows.daily_check import compute_dm1_sent_cohort_by_date

TODAY = date(2026, 6, 15)  # Monday — see window arithmetic below.


def _e(stage: str, *, dm1_sent_at=None, last_contact_date=None, rid="r") -> dict:
    return {
        "stage": stage,
        "dm1_sent_at": dm1_sent_at,
        "last_contact_date": last_contact_date,
        "record_id": rid,
        "entry_id": f"ent-{rid}",
    }


def _run(entries: list[dict], **kw) -> list[tuple[str, int]]:
    with patch(
        "workflows.daily_check._get_all_entries_parsed", return_value=entries
    ):
        return compute_dm1_sent_cohort_by_date(MagicMock(), today=TODAY, **kw)


def test_groups_dm1_sent_by_send_date_within_window():
    entries = [
        _e(PipelineStage.DM1_SENT.value, dm1_sent_at="2026-06-10", rid="a"),
        _e(PipelineStage.DM1_SENT.value, dm1_sent_at="2026-06-10", rid="b"),
        _e(PipelineStage.DM1_SENT.value, dm1_sent_at="2026-06-12", rid="c"),
    ]
    assert _run(entries) == [("2026-06-10", 2), ("2026-06-12", 1)]


def test_excludes_dates_older_than_window():
    entries = [
        _e(PipelineStage.DM1_SENT.value, dm1_sent_at="2026-06-12", rid="in"),
        _e(PipelineStage.DM1_SENT.value, dm1_sent_at="2026-06-08", rid="out5"),
        _e(PipelineStage.DM1_SENT.value, dm1_sent_at="2026-06-03", rid="out8"),
    ]
    assert _run(entries) == [("2026-06-12", 1)]


def test_only_dm1_sent_stage_is_counted():
    entries = [
        _e(PipelineStage.DM1_SENT.value, dm1_sent_at="2026-06-12", rid="dm1"),
        _e(PipelineStage.DM2_SENT.value, dm1_sent_at="2026-06-12", rid="dm2"),
        _e(PipelineStage.CONNECTION_SENT.value, last_contact_date="2026-06-12", rid="cs"),
    ]
    assert _run(entries) == [("2026-06-12", 1)]


def test_falls_back_to_last_contact_date_when_dm1_sent_at_missing():
    """Legacy DM1_SENT rows predating the PR-9a per-step timestamp still
    bucket correctly via last_contact_date (= the DM1 send date)."""
    entries = [
        _e(PipelineStage.DM1_SENT.value, last_contact_date="2026-06-12", rid="legacy"),
    ]
    assert _run(entries) == [("2026-06-12", 1)]


def test_prefers_dm1_sent_at_over_last_contact_date():
    entries = [
        _e(
            PipelineStage.DM1_SENT.value,
            dm1_sent_at="2026-06-12",
            last_contact_date="2026-06-10",
            rid="both",
        ),
    ]
    assert _run(entries) == [("2026-06-12", 1)]


def test_skips_rows_with_no_or_unparseable_send_date(capsys):
    entries = [
        _e(PipelineStage.DM1_SENT.value, rid="none"),
        _e(PipelineStage.DM1_SENT.value, dm1_sent_at="not-a-date", rid="garbage"),
        _e(PipelineStage.DM1_SENT.value, dm1_sent_at="2026-06-12", rid="ok"),
    ]
    assert _run(entries) == [("2026-06-12", 1)]
    # The unparseable row is surfaced, not silently dropped (no warning for
    # the no-date row — that's an expected, non-anomalous skip).
    err = capsys.readouterr().err
    assert "1 DM1_SENT row(s) had an unparseable send-date" in err


def test_empty_when_no_dm1_sent_rows():
    assert _run([_e(PipelineStage.PROSPECT.value, rid="p")]) == []
