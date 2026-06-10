"""PR-B.9 (Sales Nav migration) — recheck-cache continuity across backends.

The recheck cache (workflows.recheck_cache, keyed by `_normalize_linkedin_url`)
sits ABOVE the backend choice — a URL cached during a regular-backend run
should short-circuit the Sales Nav scrape too. Cache continuity is what
limits PB cost during the rollout window (regular → sales_nav cuts over
gradually; cached degree results survive the cutover).

Without continuity, every flag-flip would re-burn a PB launch for every
recently-seen profile, doubling cost during the migration and stretching
the daily-run wall-clock past the operator-visible threshold.
"""
from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from workflows import recheck_cache
from workflows.pre_invite_check import _pre_invite_degree_check

if TYPE_CHECKING:
    import pytest


def _batch_row(url: str, suffix: str) -> dict:
    """Build a to_send_data row with all PR-21-required keys."""
    return {
        "linkedInUrl": url,
        "entry_id": f"ent-{suffix}",
        "record_id": f"rec-{suffix}",
        "current_stage": "Prospect",
        "experiment_id": "exp-test",
        "experiment_id_frozen_at": "prospect",
        "message": f"hi {suffix}",
    }


@patch("workflows.daily_check.write_prospects_to_sheet", return_value="https://sheet.example/foo")
@patch("workflows.daily_check._pb_session_args", return_value={})
class TestPreInviteCacheContinuitySalesNav:
    """Seed the recheck cache with a degree result, then invoke the
    pre-invite check in `sales_nav` mode. The cached prospect MUST hit
    the cache (no PB launch for that URL); only stale prospects scrape."""

    def _enable_sales_nav(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Set env so `_resolve_degree_check_backend()` returns 'sales_nav'."""
        monkeypatch.setenv("PRE_INVITE_DEGREE_CHECK_BACKEND", "sales_nav")
        monkeypatch.setenv(
            "PB_SALES_NAV_PROFILE_SCRAPER_ID", "phantom-602790655114603"
        )
        monkeypatch.setenv(
            "PB_PROFILE_SCRAPER_ID", "phantom-LEGACY-different-id"
        )
        monkeypatch.setenv("PB_LI_SALES_NAV_SESSION_COOKIE", "fake-li-at")

    def test_cached_prospect_skips_pb_launch_in_sales_nav_mode(
        self,
        _pb_args,
        _sheet,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Seed the cache with a fresh 2nd-degree result for Alice (cached
        from a prior regular-backend run). Invoke in sales_nav mode with
        Alice + Bob in the batch. Alice MUST hit the cache and stay in
        still_to_invite without triggering a PB launch for her URL; only
        Bob should be scraped."""
        self._enable_sales_nav(monkeypatch)

        # Seed the cache for Alice. The autouse `_isolate_recheck_cache`
        # fixture in conftest.py redirects CACHE_FILE to a per-test temp
        # path, so this write is isolated to this test.
        alice_url = "https://www.linkedin.com/in/alice"
        bob_url = "https://www.linkedin.com/in/bob"
        recheck_cache.record_many({alice_url: "2nd"})

        # Verify the seed took: lookup should return Alice's entry.
        cached = recheck_cache.lookup(alice_url)
        assert cached is not None and cached.get("degree") == "2nd"

        attio = MagicMock()
        pb = MagicMock()
        # If a Sales Nav scrape DOES happen, it should only cover Bob.
        # The phantom-args path reads agent.get("argument") — stub it.
        pb.get_agent.return_value = {"argument": '{"identities": [{}]}'}
        # Bob comes back as 2nd-degree from the Sales Nav scrape.
        pb.download_result_csv.return_value = (
            "linkedinProfileUrl,connectionDegree,hasPendingInvitation,query,fullName\n"
            f"{bob_url},2nd,false,{bob_url},Bob Boberts\n"
        )
        launch = MagicMock()
        launch.container_id = "container-cache-test"
        pb.launch_agent.return_value = launch

        batch = [_batch_row(alice_url, "A"), _batch_row(bob_url, "B")]
        still, already = _pre_invite_degree_check(
            batch, pb, None, attio, "list-id",
            sales_nav_profile_scraper_id="phantom-602790655114603",
        )

        # Both prospects land in still_to_invite (Alice cache hit @ 2nd,
        # Bob scraped @ 2nd).
        assert len(still) == 2
        assert {row["entry_id"] for row in still} == {"ent-A", "ent-B"}
        assert already == []

        # Critical assertion: PB was launched EXACTLY ONCE (for the stale
        # prospect Bob — not for cached prospect Alice).
        assert pb.launch_agent.call_count == 1
        # The launch args must reference Bob's URL only (single-URL path
        # passes the URL directly via spreadsheetUrl).
        launch_args = pb.launch_agent.call_args.args[1]
        assert bob_url in str(launch_args)
        assert alice_url not in str(launch_args), (
            f"Alice should have been cache-hit; launch args leaked her URL: "
            f"{launch_args!r}"
        )

    def test_all_cached_prospects_skip_pb_entirely_in_sales_nav_mode(
        self,
        _pb_args,
        _sheet,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """When every prospect in the batch is cached, the cache_hit_flip
        codepath fires and PB is never called — even in sales_nav mode."""
        self._enable_sales_nav(monkeypatch)

        urls = [
            ("https://www.linkedin.com/in/alice", "2nd"),
            ("https://www.linkedin.com/in/bob", "3rd"),
            ("https://www.linkedin.com/in/carol", "1st"),
        ]
        recheck_cache.record_many({u: d for u, d in urls})

        attio = MagicMock()
        pb = MagicMock()

        batch = [
            _batch_row(urls[0][0], "A"),
            _batch_row(urls[1][0], "B"),
            _batch_row(urls[2][0], "C"),
        ]
        still, already = _pre_invite_degree_check(
            batch, pb, None, attio, "list-id",
            sales_nav_profile_scraper_id="phantom-602790655114603",
        )

        # Alice (2nd) + Bob (3rd) → invite queue. Carol (1st) → flipped.
        assert len(still) == 2
        assert {r["entry_id"] for r in still} == {"ent-A", "ent-B"}
        assert len(already) == 1
        assert already[0]["entry_id"] == "ent-C"

        # NO PB calls — pure cache_hit_flip path.
        pb.launch_agent.assert_not_called()
        pb.download_result_csv.assert_not_called()
        pb.get_agent.assert_not_called()

    def test_cached_first_degree_flips_via_attio_writer_no_pb(
        self,
        _pb_args,
        _sheet,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """The Pattern-A flip codepath is exempt from STRICT mode and from
        backend selection — a cached "1st" flips to ACCEPTED without
        touching PB, regardless of backend. Continuity guarantee: a
        prospect cached during a regular-backend run still flips correctly
        under sales_nav."""
        self._enable_sales_nav(monkeypatch)

        alice_url = "https://www.linkedin.com/in/alice"
        recheck_cache.record_many({alice_url: "1st"})

        attio = MagicMock()
        pb = MagicMock()

        batch = [_batch_row(alice_url, "A")]
        still, already = _pre_invite_degree_check(
            batch, pb, None, attio, "list-id",
            sales_nav_profile_scraper_id="phantom-602790655114603",
        )

        assert len(already) == 1
        assert already[0]["entry_id"] == "ent-A"
        assert still == []

        # No PB launch.
        pb.launch_agent.assert_not_called()
        # Pattern-A flip went through AttioWriter. Wave-2-B: dispatch
        # routes through ``attio.update_list_entry``.
        ent_a_calls = [
            c for c in attio.update_list_entry.call_args_list
            if c.kwargs.get("entry_id") == "ent-A"
        ]
        assert len(ent_a_calls) == 1
