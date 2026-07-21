"""Silent-truncation guards on the Attio paginated search/query methods.

PR #218 review found `run_weekly_report` fetching deals with
`search_deals(limit=500)` while the deal count drifts toward 500 after the
2026-07-01 import — past the ceiling the weekly report silently drops deals.
These tests pin the fix: full-sweep callers fetch with the shared
`DEAL_FULL_SCAN_LIMIT` and `fail_if_truncated=True`, and the client raises
`AttioResultTruncated` instead of silently slicing when a result set hits
its limit.
"""

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from clients.attio import (
    DEAL_FULL_SCAN_LIMIT,
    AttioClient,
    AttioResultTruncated,
)


@pytest.fixture
def attio() -> AttioClient:
    return AttioClient(api_key="test-key")


def _pages(*counts: int):
    """Build _request side effects returning `counts` records per call."""
    return [{"data": [{"id": {"record_id": f"r{p}-{i}"}} for i in range(n)]}
            for p, n in enumerate(counts)]


class TestPaginationTruncationGuard:
    """The shared pagination loop under search_people / search_companies /
    search_deals / query_list_entries."""

    def test_search_deals_paginates_past_500(self, attio: AttioClient) -> None:
        # 6 full pages + 1 short page = 620 records, all returned.
        with patch.object(attio, "_request", side_effect=_pages(100, 100, 100, 100, 100, 100, 20)):
            records = attio.search_deals(limit=DEAL_FULL_SCAN_LIMIT)
        assert len(records) == 620

    def test_search_deals_exhausted_below_limit_does_not_raise(self, attio: AttioClient) -> None:
        with patch.object(attio, "_request", side_effect=_pages(100, 37)):
            records = attio.search_deals(limit=10_000, fail_if_truncated=True)
        assert len(records) == 137

    def test_search_deals_raises_when_records_remain_past_limit(self, attio: AttioClient) -> None:
        # 2 full pages fill limit=200 exactly; the probe page finds more
        # records → fail loudly.
        with patch.object(attio, "_request", side_effect=_pages(100, 100, 50)), \
                pytest.raises(AttioResultTruncated):
            attio.search_deals(limit=200, fail_if_truncated=True)

    def test_search_deals_exact_limit_complete_does_not_raise(self, attio: AttioClient) -> None:
        # Exactly limit records exist (probe page comes back empty): the
        # sweep is complete, so no false-positive truncation error.
        with patch.object(attio, "_request", side_effect=_pages(100, 100, 0)) as mock_req:
            records = attio.search_deals(limit=200, fail_if_truncated=True)
        assert len(records) == 200
        assert mock_req.call_count == 3  # 2 data pages + 1 boundary probe

    def test_search_deals_raises_when_slice_drops_records(self, attio: AttioClient) -> None:
        # limit=250 not page-aligned: 300 fetched, 50 would be dropped.
        with patch.object(attio, "_request", side_effect=_pages(100, 100, 100)), \
                pytest.raises(AttioResultTruncated):
            attio.search_deals(limit=250, fail_if_truncated=True)

    def test_search_deals_silent_slice_preserved_without_flag(self, attio: AttioClient) -> None:
        # Legacy default behavior unchanged: no flag → slice, no raise.
        with patch.object(attio, "_request", side_effect=_pages(100, 100, 100)):
            records = attio.search_deals(limit=250)
        assert len(records) == 250

    def test_query_list_entries_supports_fail_if_truncated(self, attio: AttioClient) -> None:
        with patch.object(attio, "_request", side_effect=_pages(100, 100, 10)), pytest.raises(AttioResultTruncated):
            attio.query_list_entries(list_id="L", limit=200, fail_if_truncated=True)

    def test_search_people_supports_fail_if_truncated(self, attio: AttioClient) -> None:
        with patch.object(attio, "_request", side_effect=_pages(100, 100, 10)), pytest.raises(AttioResultTruncated):
            attio.search_people(limit=200, fail_if_truncated=True)

    def test_search_companies_supports_fail_if_truncated(self, attio: AttioClient) -> None:
        with patch.object(attio, "_request", side_effect=_pages(100, 100, 10)), pytest.raises(AttioResultTruncated):
            attio.search_companies(limit=200, fail_if_truncated=True)

    def test_truncation_error_names_the_endpoint_and_limit(self, attio: AttioClient) -> None:
        with patch.object(attio, "_request", side_effect=_pages(100, 100, 10)), \
                pytest.raises(AttioResultTruncated, match=r"deals.*200"):
            attio.search_deals(limit=200, fail_if_truncated=True)


class TestFullSweepCallersFailLoudly:
    """Recurring engine paths must sweep with a loud truncation guard."""

    def test_weekly_report_deal_fetch_uses_full_scan_limit(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("ATTIO_LIST_ID", "LIST")
        monkeypatch.setenv("KPI_SNAPSHOT_DIR", str(tmp_path))
        from workflows.weekly_report import run_weekly_report

        attio = MagicMock()
        attio.query_list_entries.return_value = []
        attio.search_deals.return_value = []
        with patch.object(AttioClient, "parse_deal", side_effect=lambda d: d), \
             patch.object(AttioClient, "parse_entry",
                          side_effect=lambda e: {"persona": "ops", "stage": "PROSPECT"}):
            run_weekly_report(
                attio, resend=None, report_email="operator@example.com",
                dry_run=True, today=date(2026, 7, 2),
            )

        _, deal_kwargs = attio.search_deals.call_args
        assert deal_kwargs["limit"] == DEAL_FULL_SCAN_LIMIT
        assert deal_kwargs["fail_if_truncated"] is True
        _, entry_kwargs = attio.query_list_entries.call_args
        assert entry_kwargs["fail_if_truncated"] is True

    def test_followup_radar_deal_scan_limit_is_shared_constant(self) -> None:
        from workflows import followup_radar

        assert followup_radar._DEAL_SCAN_LIMIT == DEAL_FULL_SCAN_LIMIT

    def test_learn_measure_cohorts_fails_loudly(self) -> None:
        from workflows.learn import measure_cohorts

        attio = MagicMock()
        attio.query_list_entries.return_value = []
        measure_cohorts(attio)
        _, kwargs = attio.query_list_entries.call_args
        assert kwargs["fail_if_truncated"] is True

    def test_weekly_brain_sample_fails_loudly(self) -> None:
        from workflows.weekly_brain import collect_defensive_samples

        attio = MagicMock()
        attio.query_list_entries.return_value = []
        collect_defensive_samples(attio)
        _, kwargs = attio.query_list_entries.call_args
        assert kwargs["fail_if_truncated"] is True
