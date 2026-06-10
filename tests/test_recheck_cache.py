"""Tests for workflows/recheck_cache.py — the per-URL TTL cache that lets
Phase 0, the pre-invite degree check, and Part A re-checks skip PhantomBuster
launches for profiles seen recently.

Also covers the wiring inside workflows/daily_check.py: cache hits must
short-circuit PB and feed the right downstream decisions (flip to ACCEPTED,
add to invite queue, or skip re-visit).
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from workflows import recheck_cache
from workflows.daily_check import _pre_invite_degree_check


def _yday(days: int = 1) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


def _today() -> str:
    return date.today().isoformat()


def _write_cache(payload: dict[str, dict]) -> None:
    recheck_cache.CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(recheck_cache.CACHE_FILE, "w") as f:
        json.dump(payload, f)


class TestRecheckCacheModule:
    def test_lookup_returns_none_for_missing_url(self):
        assert recheck_cache.lookup("https://linkedin.com/in/nobody") is None

    def test_lookup_returns_entry_when_fresh(self):
        _write_cache({"https://linkedin.com/in/alice": {"checked_at": _today(), "degree": "1st"}})
        entry = recheck_cache.lookup("https://www.linkedin.com/in/alice/")
        assert entry == {"checked_at": _today(), "degree": "1st"}

    def test_lookup_returns_none_when_stale(self):
        # 4 days ago > TTL of 3
        _write_cache({"https://linkedin.com/in/alice": {"checked_at": _yday(4), "degree": "1st"}})
        assert recheck_cache.lookup("https://linkedin.com/in/alice") is None

    def test_partition_splits_fresh_and_stale(self):
        _write_cache({
            "https://linkedin.com/in/alice": {"checked_at": _today(), "degree": "1st"},
            "https://linkedin.com/in/bob": {"checked_at": _yday(5), "degree": "2nd"},
        })
        urls = [
            "https://www.linkedin.com/in/alice",
            "https://www.linkedin.com/in/bob",
            "https://www.linkedin.com/in/carol",
        ]
        fresh, stale = recheck_cache.partition(urls)
        assert set(fresh) == {"https://www.linkedin.com/in/alice"}
        assert set(stale) == {"https://www.linkedin.com/in/bob", "https://www.linkedin.com/in/carol"}

    def test_record_many_writes_today_for_each_url(self):
        recheck_cache.record_many({
            "https://www.linkedin.com/in/alice": "1st",
            "https://www.linkedin.com/in/bob": None,  # visit-only, no degree
        })
        with open(recheck_cache.CACHE_FILE) as f:
            data = json.load(f)
        assert data["https://linkedin.com/in/alice"]["degree"] == "1st"
        assert data["https://linkedin.com/in/alice"]["checked_at"] == _today()
        assert data["https://linkedin.com/in/bob"]["checked_at"] == _today()
        assert "degree" not in data["https://linkedin.com/in/bob"]

    def test_record_does_not_overwrite_known_degree_with_none(self):
        # A subsequent visit-only check should not erase a previously known degree.
        recheck_cache.record_many({"https://www.linkedin.com/in/alice": "1st"})
        recheck_cache.record_many({"https://www.linkedin.com/in/alice": None})
        entry = recheck_cache.lookup("https://www.linkedin.com/in/alice")
        assert entry is not None
        assert entry.get("degree") == "1st"

    def test_load_prunes_entries_past_retention(self):
        # 20 days old > RETENTION_DAYS (14); should not survive a load.
        _write_cache({
            "https://linkedin.com/in/old": {"checked_at": _yday(20), "degree": "1st"},
            "https://linkedin.com/in/recent": {"checked_at": _today(), "degree": "2nd"},
        })
        recheck_cache.record_many({"https://www.linkedin.com/in/new": "3rd"})  # forces _load+_save
        with open(recheck_cache.CACHE_FILE) as f:
            data = json.load(f)
        assert "https://linkedin.com/in/old" not in data
        assert "https://linkedin.com/in/recent" in data

    def test_corrupt_cache_file_does_not_crash(self):
        recheck_cache.CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        recheck_cache.CACHE_FILE.write_text("{ this is not json ")
        assert recheck_cache.lookup("https://linkedin.com/in/alice") is None

    def test_corrupt_cache_file_warns_to_stderr(self, capsys):
        recheck_cache.CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        recheck_cache.CACHE_FILE.write_text("{ this is not json ")
        assert recheck_cache.lookup("https://linkedin.com/in/alice") is None
        err = capsys.readouterr().err
        assert "WARN" in err
        assert "unreadable" in err


@patch("workflows.daily_check.write_prospects_to_sheet", return_value="https://sheet.example/foo")
@patch("workflows.daily_check._pb_session_args", return_value={})
class TestPreInviteDegreeCheckCachePaths:
    """Pre-invite degree check should consult the cache before launching PB."""

    def _make_batch(self):
        # PR-15: rows need `record_id` + `current_stage` per the new
        # `_pre_invite_degree_check` contract.
        # PR-21: rows also need `experiment_id` + `experiment_id_frozen_at`
        # (direct key access raises KeyError on missing keys).
        return [
            {"linkedInUrl": "https://www.linkedin.com/in/alice", "message": "hi A",
             "entry_id": "ent-A", "record_id": "rec-A", "current_stage": "Prospect",
             "experiment_id": "exp-test", "experiment_id_frozen_at": "prospect"},
            {"linkedInUrl": "https://www.linkedin.com/in/bob", "message": "hi B",
             "entry_id": "ent-B", "record_id": "rec-B", "current_stage": "Prospect",
             "experiment_id": "exp-test", "experiment_id_frozen_at": "prospect"},
            {"linkedInUrl": "https://www.linkedin.com/in/carol", "message": "hi C",
             "entry_id": "ent-C", "record_id": "rec-C", "current_stage": "Prospect",
             "experiment_id": "exp-test", "experiment_id_frozen_at": "prospect"},
        ]

    def test_all_cached_skips_pb_entirely(self, _pb_args, _sheet):
        _write_cache({
            "https://linkedin.com/in/alice": {"checked_at": _today(), "degree": "1st"},
            "https://linkedin.com/in/bob": {"checked_at": _today(), "degree": "2nd"},
            "https://linkedin.com/in/carol": {"checked_at": _today(), "degree": "3rd"},
        })
        attio = MagicMock()
        pb = MagicMock()

        still, already = _pre_invite_degree_check(
            self._make_batch(), pb, "scraper-id", attio, "list-id"
        )

        # PB never launched — saves the scrape entirely.
        pb.launch_agent.assert_not_called()
        # Alice (1st) flipped, Bob + Carol still in invite queue.
        assert {r["entry_id"] for r in already} == {"ent-A"}
        assert {r["entry_id"] for r in still} == {"ent-B", "ent-C"}
        # PR-15: Pattern-A flip via AttioWriter. Wave-2-B: dispatch
        # routes through ``attio.update_list_entry`` so the assertion
        # targets that helper directly.
        ent_a_calls = [
            c for c in attio.update_list_entry.call_args_list
            if c.kwargs.get("entry_id") == "ent-A"
        ]
        assert len(ent_a_calls) == 1

    def test_partial_cache_scrapes_only_stale(self, _pb_args, _sheet):
        # Alice fresh, Bob stale, Carol missing — PB only sees Bob + Carol.
        _write_cache({
            "https://linkedin.com/in/alice": {"checked_at": _today(), "degree": "1st"},
            "https://linkedin.com/in/bob": {"checked_at": _yday(5), "degree": "2nd"},
        })
        attio = MagicMock()
        pb = MagicMock()
        # Bob + Carol come back as 2nd-degree from PB.
        pb.download_result_csv.return_value = (
            "linkedinProfileUrl,connectionDegree\n"
            "https://www.linkedin.com/in/bob,2nd\n"
            "https://www.linkedin.com/in/carol,2nd\n"
        )

        still, already = _pre_invite_degree_check(
            self._make_batch(), pb, "scraper-id", attio, "list-id"
        )

        pb.launch_agent.assert_called_once()
        # Alice cache-flipped; Bob + Carol scraped → invite queue.
        assert {r["entry_id"] for r in already} == {"ent-A"}
        assert {r["entry_id"] for r in still} == {"ent-B", "ent-C"}

    def test_fresh_scrape_writes_to_cache(self, _pb_args, _sheet):
        attio = MagicMock()
        pb = MagicMock()
        pb.download_result_csv.return_value = (
            "linkedinProfileUrl,connectionDegree\n"
            "https://www.linkedin.com/in/alice,1st\n"
            "https://www.linkedin.com/in/bob,2nd\n"
            "https://www.linkedin.com/in/carol,3rd\n"
        )

        _pre_invite_degree_check(self._make_batch(), pb, "scraper-id", attio, "list-id")

        # All three scraped values should now be cached.
        assert recheck_cache.lookup("https://www.linkedin.com/in/alice")["degree"] == "1st"
        assert recheck_cache.lookup("https://www.linkedin.com/in/bob")["degree"] == "2nd"
        assert recheck_cache.lookup("https://www.linkedin.com/in/carol")["degree"] == "3rd"

    def test_cached_visit_without_degree_still_triggers_scrape(self, _pb_args, _sheet):
        # NB-only visit cached with no degree → pre-invite must still scrape
        # to discover whether the profile is 1st/2nd/3rd.
        _write_cache({
            "https://linkedin.com/in/alice": {"checked_at": _today()},
            "https://linkedin.com/in/bob": {"checked_at": _today()},
            "https://linkedin.com/in/carol": {"checked_at": _today()},
        })
        attio = MagicMock()
        pb = MagicMock()
        pb.download_result_csv.return_value = (
            "linkedinProfileUrl,connectionDegree\n"
            "https://www.linkedin.com/in/alice,2nd\n"
            "https://www.linkedin.com/in/bob,2nd\n"
            "https://www.linkedin.com/in/carol,2nd\n"
        )

        _pre_invite_degree_check(self._make_batch(), pb, "scraper-id", attio, "list-id")

        pb.launch_agent.assert_called_once()


class TestClear:
    """One-time stuck-cohort drain (Pattern-A): clearing forces a re-scrape."""

    def test_clear_all_empties_cache(self):
        _write_cache({
            "https://linkedin.com/in/alice": {"checked_at": _today(), "degree": "2nd"},
            "https://linkedin.com/in/bob": {"checked_at": _today(), "degree": "3rd"},
        })
        removed = recheck_cache.clear()
        assert set(removed) == {
            "https://linkedin.com/in/alice",
            "https://linkedin.com/in/bob",
        }
        assert recheck_cache.lookup("https://linkedin.com/in/alice") is None
        assert recheck_cache.lookup("https://linkedin.com/in/bob") is None

    def test_clear_specific_url_only(self):
        _write_cache({
            "https://linkedin.com/in/alice": {"checked_at": _today(), "degree": "2nd"},
            "https://linkedin.com/in/bob": {"checked_at": _today(), "degree": "3rd"},
        })
        # URL passed in non-normalized form (www + trailing slash) must still match.
        removed = recheck_cache.clear(["https://www.linkedin.com/in/alice/"])
        assert removed == ["https://linkedin.com/in/alice"]
        assert recheck_cache.lookup("https://linkedin.com/in/alice") is None
        assert recheck_cache.lookup("https://linkedin.com/in/bob") is not None

    def test_clear_missing_url_is_noop(self):
        _write_cache({
            "https://linkedin.com/in/alice": {"checked_at": _today(), "degree": "2nd"},
        })
        removed = recheck_cache.clear(["https://linkedin.com/in/nobody"])
        assert removed == []
        assert recheck_cache.lookup("https://linkedin.com/in/alice") is not None
