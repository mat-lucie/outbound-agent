"""Tests for compute_outreach_volume — weekly invite/DM/visit volume read
from the CRM daily_run ledger.

Covers:
  * window sums across days AND across machines within a day
  * bare run_date filter per day (query_todays_row pattern, all machines)
  * lenient counter parse: malformed reads counted + surfaced, never raise
  * snapshot wiring: run_weekly_report carries outreach_volume
  * build_report_html renders the volume section + caveat note

Adaptation notes vs upstream:
  * Routes through CRMProvider.query_object_records (not attio._client.request)
    consistent with the fork's CRM seam (see workflows/daily_run.py
    query_todays_row for the established idiom).
  * Record.attributes values are already scalar (the provider normalization
    unwraps the vendor list shape); _counter_value reads them directly.
  * No pre-fix-era date cutoff: the fork has no split-brain era artifact.
    A missing row is simply "no coverage" — the coverage_warning surfaces it.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

from clients.crm.base import Record
from workflows.weekly_report import (
    build_report_html,
    compute_outreach_volume,
)

# Reference window start: any recent date works (no pre-fix cutoff in fork).
_D0 = date(2026, 6, 10)


def _daily_run_record(
    run_date: str = "2026-06-10",
    connections: int | None = 0,
    messages: int | None = 0,
    visits: int | None = 0,
    status: str | None = None,
) -> Record:
    """Build a normalized daily_run Record as the CRM provider returns it.

    ``None`` for a counter omits the attribute, producing the malformed shape.
    The provider already flattened ``[{"value": N}]`` to the plain scalar N,
    so ``attributes`` carries plain ints (or is absent for None counters).
    ``status`` is the SELECT attr already unwrapped to its title string.
    """
    attrs: dict = {"run_date": run_date}
    for key, val in (
        ("connections_sent", connections),
        ("messages_sent", messages),
        ("visits_sent", visits),
    ):
        if val is not None:
            attrs[key] = val
    if status is not None:
        attrs["status"] = status
    return Record(
        record_id="rec_x",
        object="daily_run",
        attributes=attrs,
        raw={},
    )


def _crm_with_days(rows_by_date: dict[str, list[Record]]) -> MagicMock:
    """CRM mock whose daily_run query answers per run_date filter."""
    crm = MagicMock()

    def _query(object_type, *, filters=None, sorts=None, limit=None):
        assert object_type == "daily_run"
        run_date = (filters or {}).get("run_date", "")
        return rows_by_date.get(run_date, [])

    crm.query_object_records.side_effect = _query
    return crm


class TestVolumeSums:
    def test_sums_across_days(self):
        crm = _crm_with_days({
            _D0.isoformat(): [_daily_run_record(_D0.isoformat(), 10, 5, 20)],
            (_D0 + timedelta(days=1)).isoformat(): [
                _daily_run_record((_D0 + timedelta(days=1)).isoformat(), 7, 3, 0)
            ],
        })
        vol = compute_outreach_volume(crm, _D0, _D0 + timedelta(days=6))
        assert vol["totals"] == {
            "connections_sent": 17,
            "messages_sent": 8,
            "visits_sent": 20,
        }

    def test_sums_across_machines_same_day(self):
        crm = _crm_with_days({
            _D0.isoformat(): [
                _daily_run_record(_D0.isoformat(), 10, 5, 20),
                _daily_run_record(_D0.isoformat(), 4, 2, 1),
            ],
        })
        vol = compute_outreach_volume(crm, _D0, _D0)
        assert vol["totals"]["connections_sent"] == 14
        assert vol["per_day"][_D0.isoformat()]["messages_sent"] == 7

    def test_days_without_rows_absent_from_per_day(self):
        crm = _crm_with_days({_D0.isoformat(): [_daily_run_record(_D0.isoformat(), 1, 1, 1)]})
        vol = compute_outreach_volume(crm, _D0, _D0 + timedelta(days=2))
        assert list(vol["per_day"]) == [_D0.isoformat()]

    def test_queries_bare_run_date_filter_per_day(self):
        """Bare run_date filter (no machine_id, no uniqueness_key) — closed
        rows are re-keyed on close, and all machines must be included."""
        crm = _crm_with_days({})
        compute_outreach_volume(crm, _D0, _D0 + timedelta(days=1))
        calls = crm.query_object_records.call_args_list
        filters_used = [c.kwargs["filters"] for c in calls]
        assert filters_used == [
            {"run_date": _D0.isoformat()},
            {"run_date": (_D0 + timedelta(days=1)).isoformat()},
        ]


class TestMalformedCounters:
    def test_missing_counter_counted_not_raised(self):
        crm = _crm_with_days({
            _D0.isoformat(): [
                _daily_run_record(_D0.isoformat(), connections=None, messages=5, visits=2)
            ],
        })
        vol = compute_outreach_volume(crm, _D0, _D0)
        assert vol["malformed_reads"] == 1
        assert vol["totals"]["messages_sent"] == 5  # good counters still summed

    def test_non_numeric_counter_counted_not_raised(self):
        # Build a record with a string value for messages_sent
        row = _daily_run_record(_D0.isoformat(), 0, 0, 0)
        # Override the attribute to be non-numeric (simulate provider returning garbage)
        attrs = dict(row.attributes)
        attrs["messages_sent"] = "not-a-number"
        bad_row = Record(record_id=row.record_id, object=row.object,
                         attributes=attrs, raw=row.raw)
        crm = _crm_with_days({_D0.isoformat(): [bad_row]})
        vol = compute_outreach_volume(crm, _D0, _D0)
        assert vol["malformed_reads"] == 1
        assert vol["totals"]["messages_sent"] == 0

    def test_well_formed_zero_is_not_malformed(self):
        crm = _crm_with_days({_D0.isoformat(): [_daily_run_record(_D0.isoformat(), 0, 0, 0)]})
        vol = compute_outreach_volume(crm, _D0, _D0)
        assert vol["malformed_reads"] == 0

    def test_negative_counter_is_malformed_not_subtracted(self):
        """A negative send counter is malformed by domain definition —
        passing it through would silently SUBTRACT from the weekly total."""
        crm = _crm_with_days({
            _D0.isoformat(): [_daily_run_record(_D0.isoformat(), 10, 5, 20)],
            (_D0 + timedelta(days=1)).isoformat(): [
                _daily_run_record((_D0 + timedelta(days=1)).isoformat(), -5, 0, 0)
            ],
        })
        vol = compute_outreach_volume(crm, _D0, _D0 + timedelta(days=1))
        assert vol["malformed_reads"] == 1
        assert vol["totals"]["connections_sent"] == 10  # not 5


class TestAbandonedRows:
    def test_abandoned_row_skipped_only_completed_counted(self):
        """A stale-run takeover leaves an 'abandoned' row with partial counters
        alongside the replacement row. Summing both would double-count, so the
        abandoned row is skipped and the skip surfaced."""
        crm = _crm_with_days({
            _D0.isoformat(): [
                # abandoned partial-work row from the crashed process
                _daily_run_record(_D0.isoformat(), 3, 2, 1, status="abandoned"),
                # the completed replacement row redid the work
                _daily_run_record(_D0.isoformat(), 10, 5, 20, status="completed"),
            ],
        })
        vol = compute_outreach_volume(crm, _D0, _D0 + timedelta(days=6))
        assert vol["totals"] == {
            "connections_sent": 10,
            "messages_sent": 5,
            "visits_sent": 20,
        }
        assert vol["abandoned_rows_skipped"] == 1
        assert vol["per_day"][_D0.isoformat()]["abandoned_rows_skipped"] == 1

    def test_missing_status_keeps_row_counted(self):
        """A row without a status (or a malformed one) is never silently
        dropped — its counters still contribute."""
        crm = _crm_with_days({
            _D0.isoformat(): [_daily_run_record(_D0.isoformat(), 4, 4, 4)],
        })
        vol = compute_outreach_volume(crm, _D0, _D0 + timedelta(days=6))
        assert vol["totals"]["connections_sent"] == 4
        assert vol["abandoned_rows_skipped"] == 0


class TestCoverage:
    def test_days_with_rows_and_window_days(self):
        crm = _crm_with_days({_D0.isoformat(): [_daily_run_record(_D0.isoformat(), 1, 1, 1)]})
        vol = compute_outreach_volume(crm, _D0, _D0 + timedelta(days=6))
        assert vol["window_days"] == 7
        assert vol["days_with_rows"] == 1

    def test_empty_window_sets_coverage_warning(self):
        """Silent-zero guard: an all-empty query over window days must not
        render as a healthy zero-activity week."""
        crm = _crm_with_days({})
        vol = compute_outreach_volume(crm, _D0, _D0 + timedelta(days=6))
        assert "ledger/query gap" in vol["coverage_warning"]

    def test_no_coverage_warning_when_rows_exist(self):
        crm = _crm_with_days({_D0.isoformat(): [_daily_run_record(_D0.isoformat(), 0, 0, 0)]})
        vol = compute_outreach_volume(crm, _D0, _D0 + timedelta(days=6))
        assert vol["coverage_warning"] == ""

    def test_source_field_is_present(self):
        crm = _crm_with_days({})
        vol = compute_outreach_volume(crm, _D0, _D0)
        assert vol["source"] == "crm_daily_run_ledger"


class TestHtmlRendering:
    KPIS = {
        "total_prospects": 0,
        "connection_acceptance_rate": 0.0,
        "response_rate": 0.0,
        "calls_booked": 0,
        "qualified": 0,
        "stage_counts": {},
    }
    ACTIVE = {
        "in_progress": [],
        "in_progress_count": 0,
        "in_progress_value": 0.0,
        "lead_count": 0,
        "lost_count": 0,
        "blank_value_count": 0,
    }

    def _volume(
        self,
        note: str = "",
        coverage_warning: str = "",
        malformed_reads: int = 0,
    ) -> dict:
        return {
            "source": "crm_daily_run_ledger",
            "totals": {"connections_sent": 17, "messages_sent": 8, "visits_sent": 20},
            "per_day": {},
            "window_days": 7,
            "days_with_rows": 5,
            "malformed_reads": malformed_reads,
            "note": note,
            "coverage_warning": coverage_warning,
        }

    def test_volume_section_rendered(self):
        html = build_report_html(
            self.KPIS, self.ACTIVE, date(2026, 6, 14),
            outreach_volume=self._volume(),
        )
        assert "Outreach Volume (7d)" in html
        assert "Invites Sent" in html
        assert "17" in html
        assert "Ledger rows on 5 of 7 window days" in html

    def test_note_rendered_when_present(self):
        html = build_report_html(
            self.KPIS, self.ACTIVE, date(2026, 6, 14),
            outreach_volume=self._volume(note="some operator caveat"),
        )
        assert "some operator caveat" in html

    def test_coverage_warning_and_malformed_rendered(self):
        """Every caveat the CLI prints must land in the email too — on a wet
        run the email is the artifact the operator actually reads."""
        html = build_report_html(
            self.KPIS, self.ACTIVE, date(2026, 6, 14),
            outreach_volume=self._volume(
                coverage_warning="more likely a ledger/query gap",
                malformed_reads=2,
            ),
        )
        assert "ledger/query gap" in html
        assert "2 daily_run counter reads" in html

    def test_legacy_caller_without_volume_still_renders(self):
        html = build_report_html(self.KPIS, self.ACTIVE, date(2026, 6, 14))
        assert "Outreach Volume" not in html
        assert "Pipeline Breakdown" in html


class TestSnapshotWiring:
    def test_run_weekly_report_with_crm_carries_outreach_volume(self, monkeypatch, tmp_path):
        """run_weekly_report passes crm to compute_outreach_volume; result
        lands in the returned KPISnapshot."""
        from unittest.mock import patch

        from clients.attio import AttioClient
        from workflows.weekly_report import run_weekly_report

        monkeypatch.setattr(
            "workflows.weekly_report.REPORTS_DIR", tmp_path / "weekly-kpi"
        )
        monkeypatch.setenv("ATTIO_LIST_ID", "LIST")

        # 2026-06-16 → window 06-10..06-16 (7 days).
        crm = _crm_with_days({
            "2026-06-12": [_daily_run_record("2026-06-12", 3, 2, 1)],
        })
        attio = MagicMock()
        attio.query_list_entries.return_value = []
        attio.search_deals.return_value = []
        with patch.object(AttioClient, "parse_deal", side_effect=lambda d: d), \
             patch.object(AttioClient, "parse_entry",
                          side_effect=lambda e: {"persona": "ops",
                                                 "stage": "PROSPECT",
                                                 "merged_into": None}):
            result = run_weekly_report(
                attio, resend=None, report_email="operator@example.com",
                dry_run=True, today=date(2026, 6, 16),
                crm=crm,
            )
        assert result["outreach_volume"]["totals"] == {
            "connections_sent": 3,
            "messages_sent": 2,
            "visits_sent": 1,
        }
        assert result["outreach_volume"]["note"] == ""

    def test_run_weekly_report_without_crm_has_coverage_warning(self, monkeypatch, tmp_path):
        """When crm=None (backward-compat), outreach_volume has a coverage warning
        and zero totals — not a silent zero."""
        from unittest.mock import patch

        from clients.attio import AttioClient
        from workflows.weekly_report import run_weekly_report

        monkeypatch.setattr(
            "workflows.weekly_report.REPORTS_DIR", tmp_path / "weekly-kpi"
        )
        monkeypatch.setenv("ATTIO_LIST_ID", "LIST")
        attio = MagicMock()
        attio.query_list_entries.return_value = []
        attio.search_deals.return_value = []
        with patch.object(AttioClient, "parse_deal", side_effect=lambda d: d), \
             patch.object(AttioClient, "parse_entry",
                          side_effect=lambda e: {"persona": "ops",
                                                 "stage": "PROSPECT",
                                                 "merged_into": None}):
            result = run_weekly_report(
                attio, resend=None, dry_run=True,
                today=date(2026, 6, 16),
                crm=None,
            )
        assert result["outreach_volume"]["totals"]["connections_sent"] == 0
        assert result["outreach_volume"]["coverage_warning"] != ""
