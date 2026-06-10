"""Tests for pipeline starvation alarm + fresh-prospect quarantine.

Covers:
  - `models.pipeline.is_invite_eligible` — all parse + edge branches.
  - `workflows.weekly_prospect._build_prospect_entry_attrs` — quarantine
    attrs land on every fresh PROSPECT commit; env override honored.
  - `workflows.starvation.evaluate_pipeline_starvation` — three triggers
    fire correctly, false-alarm guard suppresses stale_weekly, idempotency
    on re-runs, pure-Attio (no LLM calls).
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from models.business_calendar import add_business_days
from models.pipeline import PipelineStage, is_invite_eligible
from tests.fakes import fake_daily_run
from workflows import starvation as st
from workflows.escalation_schemas import ESCALATION_SCHEMAS

# --------- Fixtures ---------

@pytest.fixture
def mock_attio():
    """A MagicMock standing in for AttioClient — escalate() POSTs queue
    rows; we record them so tests can assert idempotency keys.
    """
    client = MagicMock()
    posted: list[dict] = []

    def _request(method: str, path: str, json: dict | None = None, **_):
        if path.endswith("/records/query"):
            # Idempotency lookup: pretend no row exists.
            return {"data": []}
        if path.endswith("/records") and method == "POST":
            posted.append({
                "path": path,
                "body": json or {},
            })
            return {
                "data": {
                    "id": {"record_id": f"rec_{len(posted)}"},
                    "values": (json or {}).get("data", {}).get("values", {}),
                },
            }
        return {"data": {}}

    client._request.side_effect = _request
    client.posted = posted  # type: ignore[attr-defined]
    return client


def _prospect(*, score: int = 80, invite_eligible_after: str | None = None,
              prospect_committed_at: str | None = None,
              stage: str = PipelineStage.PROSPECT.value) -> dict:
    """Build a flat parsed-entry dict the way AttioClient.parse_entry would."""
    out: dict = {
        "stage": stage,
        "quality_score": score,
        "experiment_id_frozen_at": None,
    }
    if invite_eligible_after is not None:
        out["invite_eligible_after"] = invite_eligible_after
    if prospect_committed_at is not None:
        out["prospect_committed_at"] = prospect_committed_at
    return out


# ====================================================================
# is_invite_eligible
# ====================================================================

class TestIsInviteEligible:
    def test_missing_attr_passes(self):
        """Legacy rows with no invite_eligible_after are eligible — the
        backfill is best-effort; the daily filter shouldn't punish a
        legitimate prospect just because the attribute was never set."""
        assert is_invite_eligible({}, date(2026, 5, 21)) is True
        assert is_invite_eligible({"invite_eligible_after": None}, date(2026, 5, 21)) is True
        assert is_invite_eligible({"invite_eligible_after": ""}, date(2026, 5, 21)) is True

    def test_quarantine_window_active(self):
        today = date(2026, 5, 21)
        assert is_invite_eligible(
            {"invite_eligible_after": "2026-05-25"}, today,
        ) is False

    def test_quarantine_window_elapsed(self):
        today = date(2026, 5, 21)
        assert is_invite_eligible(
            {"invite_eligible_after": "2026-05-19"}, today,
        ) is True

    def test_quarantine_exact_today_eligible(self):
        """Boundary: invite_eligible_after == today must pass — the
        quarantine is a strict-greater-than, not >=. A prospect's first
        invite-eligible day shouldn't be off-by-one."""
        today = date(2026, 5, 21)
        assert is_invite_eligible(
            {"invite_eligible_after": "2026-05-21"}, today,
        ) is True

    def test_malformed_date_drops_safely(self):
        """Garbage dates fail-closed — better to skip the invite than to
        ship one we can't reason about."""
        assert is_invite_eligible(
            {"invite_eligible_after": "not-a-date"}, date(2026, 5, 21),
        ) is False

    def test_date_object_accepted(self):
        """Internal callers may pass a date directly; the helper accepts
        both shapes without forcing a string conversion."""
        today = date(2026, 5, 21)
        assert is_invite_eligible(
            {"invite_eligible_after": date(2026, 5, 19)}, today,
        ) is True
        assert is_invite_eligible(
            {"invite_eligible_after": date(2026, 5, 25)}, today,
        ) is False


# ====================================================================
# _build_prospect_entry_attrs
# ====================================================================

class TestBuildProspectEntryAttrs:
    def _score_result(self) -> dict:
        return {
            "persona": "operations_leaders",
            "language": "en",
            "score": 78,
            "score_breakdown": {"icp": 78},
        }

    def test_writes_committed_at_and_invite_eligible_after(self, monkeypatch):
        from workflows import weekly_prospect

        # Pin the quarantine window to 2 bdays so the test is deterministic.
        monkeypatch.delenv("OUTBOUND_PROSPECT_QUARANTINE_BDAYS", raising=False)
        today = "2026-05-21"  # Thursday
        attrs = weekly_prospect._build_prospect_entry_attrs(
            self._score_result(), today,
        )
        assert "prospect_committed_at" in attrs
        # ISO-8601 datetime string, UTC suffix.
        assert attrs["prospect_committed_at"].endswith("+00:00") or attrs[
            "prospect_committed_at"
        ].endswith("Z")
        # invite_eligible_after = today + 2 business days = Mon 2026-05-25.
        expected = add_business_days(date.fromisoformat(today), 2).isoformat()
        assert attrs["invite_eligible_after"] == expected

    def test_quarantine_env_override(self, monkeypatch):
        from workflows import weekly_prospect

        monkeypatch.setenv("OUTBOUND_PROSPECT_QUARANTINE_BDAYS", "5")
        today = "2026-05-21"
        attrs = weekly_prospect._build_prospect_entry_attrs(
            self._score_result(), today,
        )
        expected = add_business_days(date.fromisoformat(today), 5).isoformat()
        assert attrs["invite_eligible_after"] == expected

    def test_quarantine_env_garbage_falls_back_to_default(self, monkeypatch):
        from workflows import weekly_prospect

        monkeypatch.setenv("OUTBOUND_PROSPECT_QUARANTINE_BDAYS", "not-an-int")
        today = "2026-05-21"
        attrs = weekly_prospect._build_prospect_entry_attrs(
            self._score_result(), today,
        )
        expected = add_business_days(date.fromisoformat(today), 2).isoformat()
        assert attrs["invite_eligible_after"] == expected

    def test_quarantine_attrs_excluded_from_phase1_fallback(self):
        """The §3.1 quarantine attrs MUST NOT join the missing-schema
        fallback — a PROSPECT committed without invite_eligible_after
        would be immediately invite-eligible (is_invite_eligible's
        missing-attr → True). Better to fail the commit loudly on
        unmigrated schema than to silently bypass quarantine.
        """
        from workflows import weekly_prospect

        assert "prospect_committed_at" not in weekly_prospect._PHASE1_ENTRY_KEYS
        assert "invite_eligible_after" not in weekly_prospect._PHASE1_ENTRY_KEYS


# ====================================================================
# evaluate_pipeline_starvation
# ====================================================================

class TestEvaluatePipelineStarvation:
    """Each trigger fires when the relevant floor is crossed, abstains
    when not. False-alarm guard on stale_weekly. Re-runs are idempotent
    (each daily key fires at most one queue row).
    """

    def _entries(self, n_invite_eligible: int, n_quarantined: int,
                 most_recent_commit: date | None) -> list[dict]:
        out: list[dict] = []
        committed_iso = (
            most_recent_commit.isoformat() if most_recent_commit else None
        )
        for _ in range(n_invite_eligible):
            out.append(_prospect(
                score=80,
                invite_eligible_after="2026-04-01",  # well past today
                prospect_committed_at=committed_iso,
            ))
        # Future invite_eligible_after = still in quarantine
        for _ in range(n_quarantined):
            out.append(_prospect(
                score=80,
                invite_eligible_after="2099-12-31",
                prospect_committed_at=committed_iso,
            ))
        return out

    def test_low_prospects_fires(self, mock_attio, monkeypatch):
        monkeypatch.setenv("OUTBOUND_STARVATION_LOW_PROSPECTS_FLOOR", "10")
        monkeypatch.setenv("OUTBOUND_STARVATION_DAILY_INVITE_RATE", "15")
        entries = self._entries(
            n_invite_eligible=3,  # below floor
            n_quarantined=0,
            most_recent_commit=date(2026, 5, 19),
        )
        today = date(2026, 5, 21)
        out = st.evaluate_pipeline_starvation(
            mock_attio, today, attio_query=lambda _a: entries,
        )
        assert "low_prospects" in out["triggers_fired"]
        keys = out["queue_rows_opened"]
        assert any(k.startswith("low_prospects|2026-05-21") for k in keys)

    def test_low_prospects_abstains_when_pool_healthy(self, mock_attio, monkeypatch):
        monkeypatch.setenv("OUTBOUND_STARVATION_LOW_PROSPECTS_FLOOR", "10")
        monkeypatch.setenv("OUTBOUND_STARVATION_DAILY_INVITE_RATE", "15")
        monkeypatch.setenv("OUTBOUND_STARVATION_SHORT_RUNWAY_BDAYS", "3")
        entries = self._entries(
            n_invite_eligible=100,
            n_quarantined=0,
            most_recent_commit=date(2026, 5, 19),
        )
        today = date(2026, 5, 21)
        out = st.evaluate_pipeline_starvation(
            mock_attio, today, attio_query=lambda _a: entries,
        )
        assert out["triggers_fired"] == []
        assert out["queue_rows_opened"] == []

    def test_short_runway_fires(self, mock_attio, monkeypatch):
        monkeypatch.setenv("OUTBOUND_STARVATION_LOW_PROSPECTS_FLOOR", "5")
        monkeypatch.setenv("OUTBOUND_STARVATION_DAILY_INVITE_RATE", "15")
        monkeypatch.setenv("OUTBOUND_STARVATION_SHORT_RUNWAY_BDAYS", "3")
        # 20 invite-eligible / 15 rate = 1.33 bdays runway → below floor 3.
        entries = self._entries(
            n_invite_eligible=20,
            n_quarantined=0,
            most_recent_commit=date(2026, 5, 19),
        )
        today = date(2026, 5, 21)
        out = st.evaluate_pipeline_starvation(
            mock_attio, today, attio_query=lambda _a: entries,
        )
        assert "short_runway" in out["triggers_fired"]

    def test_short_runway_skipped_when_pool_empty(self, mock_attio, monkeypatch):
        """When invite_eligible_pool == 0, short_runway must NOT fire —
        low_prospects already covers that case (with more context). Two
        rows for the same forensic event would just spam the queue.
        """
        monkeypatch.setenv("OUTBOUND_STARVATION_LOW_PROSPECTS_FLOOR", "5")
        monkeypatch.setenv("OUTBOUND_STARVATION_DAILY_INVITE_RATE", "15")
        monkeypatch.setenv("OUTBOUND_STARVATION_SHORT_RUNWAY_BDAYS", "3")
        entries = self._entries(
            n_invite_eligible=0,
            n_quarantined=0,
            most_recent_commit=date(2026, 5, 19),
        )
        today = date(2026, 5, 21)
        out = st.evaluate_pipeline_starvation(
            mock_attio, today, attio_query=lambda _a: entries,
        )
        assert "short_runway" not in out["triggers_fired"]
        assert "low_prospects" in out["triggers_fired"]

    def test_stale_weekly_fires_after_min_bdays(self, mock_attio, monkeypatch):
        monkeypatch.setenv("OUTBOUND_STARVATION_LOW_PROSPECTS_FLOOR", "5")
        monkeypatch.setenv("OUTBOUND_STARVATION_DAILY_INVITE_RATE", "15")
        monkeypatch.setenv("OUTBOUND_STARVATION_SHORT_RUNWAY_BDAYS", "1")
        monkeypatch.setenv("OUTBOUND_STARVATION_STALE_WEEKLY_BDAYS", "7")
        monkeypatch.setenv("OUTBOUND_STARVATION_MIN_BDAYS_SINCE_COMMIT", "5")
        # most_recent_commit = 10 bdays ago.
        today = date(2026, 5, 21)  # Thursday
        old_commit = date(2026, 5, 7)  # 10 business days back (Thurs)
        entries = self._entries(
            n_invite_eligible=100, n_quarantined=0,
            most_recent_commit=old_commit,
        )
        out = st.evaluate_pipeline_starvation(
            mock_attio, today, attio_query=lambda _a: entries,
        )
        assert "stale_weekly" in out["triggers_fired"]

    def test_stale_weekly_false_alarm_guard(self, mock_attio, monkeypatch):
        """If bdays_since_commit < min_bdays_for_alarm, stale_weekly
        MUST NOT fire — even if stale_floor is crossed. This is the
        false-alarm guard that prevents the operator from being trained
        to dismiss starvation alerts.
        """
        monkeypatch.setenv("OUTBOUND_STARVATION_STALE_WEEKLY_BDAYS", "2")
        monkeypatch.setenv("OUTBOUND_STARVATION_MIN_BDAYS_SINCE_COMMIT", "5")
        monkeypatch.setenv("OUTBOUND_STARVATION_LOW_PROSPECTS_FLOOR", "5")
        monkeypatch.setenv("OUTBOUND_STARVATION_SHORT_RUNWAY_BDAYS", "1")
        today = date(2026, 5, 21)
        # 3 bdays ago — past stale_floor=2 but below min_bdays_for_alarm=5.
        entries = self._entries(
            n_invite_eligible=100, n_quarantined=0,
            most_recent_commit=today - timedelta(days=3),
        )
        out = st.evaluate_pipeline_starvation(
            mock_attio, today, attio_query=lambda _a: entries,
        )
        assert "stale_weekly" not in out["triggers_fired"]

    def test_stale_weekly_no_commit_in_pipeline(self, mock_attio, monkeypatch):
        """When no entry carries prospect_committed_at (legacy pipeline
        + backfill not yet run), stale_weekly cannot evaluate. It must
        NOT fire on the basis of None — that would alarm on every fresh
        deploy. The backfill script fixes the upstream signal.
        """
        monkeypatch.setenv("OUTBOUND_STARVATION_STALE_WEEKLY_BDAYS", "1")
        monkeypatch.setenv("OUTBOUND_STARVATION_MIN_BDAYS_SINCE_COMMIT", "1")
        monkeypatch.setenv("OUTBOUND_STARVATION_LOW_PROSPECTS_FLOOR", "5")
        today = date(2026, 5, 21)
        entries = self._entries(
            n_invite_eligible=100, n_quarantined=0,
            most_recent_commit=None,
        )
        out = st.evaluate_pipeline_starvation(
            mock_attio, today, attio_query=lambda _a: entries,
        )
        assert out["bdays_since_commit"] is None
        assert "stale_weekly" not in out["triggers_fired"]

    def test_quarantined_pool_not_in_invite_eligible(self, mock_attio, monkeypatch):
        """Sanity: a row whose invite_eligible_after is still in the
        future MUST be counted in `quarantined_pool`, NOT
        `invite_eligible_pool`. Otherwise low_prospects would falsely
        pass through quarantined rows.
        """
        monkeypatch.setenv("OUTBOUND_STARVATION_LOW_PROSPECTS_FLOOR", "10")
        monkeypatch.setenv("OUTBOUND_STARVATION_DAILY_INVITE_RATE", "15")
        monkeypatch.setenv("OUTBOUND_STARVATION_SHORT_RUNWAY_BDAYS", "1")
        entries = self._entries(
            n_invite_eligible=2,    # below floor
            n_quarantined=15,        # would push above the floor if leaked
            most_recent_commit=date(2026, 5, 19),
        )
        today = date(2026, 5, 21)
        out = st.evaluate_pipeline_starvation(
            mock_attio, today, attio_query=lambda _a: entries,
        )
        assert out["invite_eligible_pool"] == 2
        assert out["quarantined_pool"] == 15
        # Still fires low_prospects because invite_eligible_pool < floor.
        assert "low_prospects" in out["triggers_fired"]

    def test_low_score_entries_excluded(self, mock_attio, monkeypatch):
        """Entries with quality_score < 60 don't count toward the
        invite-eligible pool — they're not the operator's invite material."""
        monkeypatch.setenv("OUTBOUND_STARVATION_LOW_PROSPECTS_FLOOR", "5")
        entries = [
            _prospect(score=30, invite_eligible_after="2026-01-01") for _ in range(20)
        ]
        today = date(2026, 5, 21)
        out = st.evaluate_pipeline_starvation(
            mock_attio, today, attio_query=lambda _a: entries,
        )
        assert out["invite_eligible_pool"] == 0


# ====================================================================
# Daily-check + pre-invite-check filter behavior
# ====================================================================

class TestDailyCheckQuarantineFilter:
    def test_quarantined_prospects_excluded_from_invite_slice(self, monkeypatch):
        """Behavioral integration: drive run_connection_requests through
        a parsed-entries fixture where one PROSPECT is in quarantine,
        another is past quarantine, a third is below the score floor.
        Assert only the past-quarantine row enters the invite slice.
        """
        from clients.phantombuster import PhantomBusterClient
        from workflows import daily_check

        # PR-12 (B-PD-001): `language` must be explicitly set on every
        # PROSPECT row. weekly_prospect's PROSPECT-commit path is the
        # canonical writer; test fixtures need to mirror that contract,
        # otherwise resolve_language opens a missing_language queue row
        # and skips the prospect before PR-43's quarantine logic runs.
        eligible = {
            "stage": "Prospect", "quality_score": 80, "entry_id": "e-elig",
            "record_id": "r-elig",
            "invite_eligible_after": "2026-01-01",  # past
            "prospect_committed_at": None,
            "language": "en",
        }
        quarantined = {
            "stage": "Prospect", "quality_score": 80, "entry_id": "e-quar",
            "record_id": "r-quar",
            "invite_eligible_after": "2099-12-31",  # future
            "prospect_committed_at": None,
            "language": "en",
        }
        low_score = {
            "stage": "Prospect", "quality_score": 30, "entry_id": "e-low",
            "record_id": "r-low",
            "invite_eligible_after": "2026-01-01",
            "prospect_committed_at": None,
            "language": "en",
        }
        parsed = [eligible, quarantined, low_score]

        monkeypatch.setattr(
            daily_check, "_get_all_entries_parsed", lambda _attio: parsed,
        )
        # Cache.get returns (name, company, url, industry_raw, title) for
        # the record_id. Quarantined + low_score never reach here.
        cache = MagicMock()
        cache.get.return_value = ("Test Person", "Acme", "https://linkedin.com/in/test", "manufacturing", "Director")
        monkeypatch.setattr(
            daily_check, "_assert_no_unresolved_placeholders", lambda *_a, **_k: None,
        )
        monkeypatch.setattr(
            daily_check, "can_send_connections", lambda _n: True,
        )
        monkeypatch.setattr(
            daily_check, "get_remaining",
            lambda: {"connections": 25, "messages": 30, "visits": 50},
        )
        monkeypatch.setattr(
            daily_check, "get_status", lambda: "",
        )
        # PR-15 (B-SD-010): STRICT mode would raise ConfigError when
        # profile_scraper_id is missing on the send_invite codepath.
        # This test exercises quarantine filtering rather than degree
        # verification — opt into NON-STRICT mode so the silent-bypass
        # path the test was written against still works.
        monkeypatch.setenv("STRICT_PRE_INVITE_DEGREE_CHECK", "false")

        attio = MagicMock()
        pb = MagicMock(spec=PhantomBusterClient)

        result = daily_check.run_connection_requests(
            attio, pb, "agent-id", batch_size=10,
            dry_run=True,
            auto_confirm=True,
            cache=cache,
            today=date(2026, 5, 21),
            daily_run=fake_daily_run(),
        )

        # Dry-run summary should show exactly one prospect prepared (the
        # eligible one). Quarantined and low_score must NOT appear.
        assert result.get("dry_run") == 1


class TestPreInviteCheckQuarantineGuard:
    def test_quarantined_row_dropped_at_pre_invite_layer(self, monkeypatch):
        """Even if a future refactor of run_connection_requests drops
        the upstream quarantine filter, _pre_invite_degree_check must
        catch the leak. Drive it with one quarantined + one eligible
        row directly and assert the quarantined row never reaches the
        degree scrape.
        """
        from workflows import pre_invite_check

        scrape_called: dict = {"called": False, "urls": []}
        def _fake_pb_launch(*_a, **_k):
            scrape_called["called"] = True
            return MagicMock()
        pb = MagicMock()
        pb.launch_agent.side_effect = _fake_pb_launch
        # Empty CSV → fail-safe drop; we only care that scrape isn't
        # called on the quarantined row in the first place.
        pb.download_result_csv.return_value = ""

        attio = MagicMock()

        eligible = {
            "linkedInUrl": "https://linkedin.com/in/ok",
            "message": "hi", "entry_id": "e-ok",
            "name": "Ok", "company": "Acme", "title": "Director",
            "invite_eligible_after": "2026-01-01",
        }
        quarantined = {
            "linkedInUrl": "https://linkedin.com/in/quar",
            "message": "hi", "entry_id": "e-quar",
            "name": "Quar", "company": "Acme", "title": "Director",
            "invite_eligible_after": "2099-12-31",
        }
        # Stub the recheck-cache out so the test doesn't need the cache file.
        monkeypatch.setattr(
            pre_invite_check.recheck_cache, "partition",
            lambda urls: ({}, list(urls)),
        )
        monkeypatch.setattr(
            pre_invite_check.recheck_cache, "RECHECK_TTL_DAYS", 30,
        )
        monkeypatch.setattr(
            pre_invite_check.recheck_cache, "record_many", lambda _d: None,
        )

        # Stub the in-function namespace import path. The real function
        # imports workflows.daily_check inside the body; we replace
        # write_prospects_to_sheet + _pb_session_args via the module.
        import workflows.daily_check as _dc
        monkeypatch.setattr(
            _dc, "write_prospects_to_sheet",
            lambda *_a, **_k: "https://docs.google.com/sheet",
        )
        monkeypatch.setattr(_dc, "_pb_session_args", lambda: {})

        still, already = pre_invite_check._pre_invite_degree_check(
            [eligible, quarantined], pb, "scraper-id", attio, "list-id",
            today=date(2026, 5, 21),
        )

        # No invites returned (empty CSV makes the whole batch drop —
        # fail-safe behavior of the existing function). What matters is
        # the quarantined URL never reached the scrape:
        scraped_urls = []
        for call in pb.launch_agent.call_args_list:
            scraped_urls.append(call)
        # The quarantined URL must NOT appear in any launch arg.
        for call in pb.launch_agent.call_args_list:
            args = str(call)
            assert "linkedin.com/in/quar" not in args, (
                "quarantined row should be filtered BEFORE the degree scrape"
            )


# ====================================================================
# parse_entry wiring — the new attrs must round-trip through the parser
# (catches the silent no-op where the writer side ships an attr the
# reader side never extracts).
# ====================================================================

class TestParseEntryRoundTrip:
    def test_invite_eligible_after_extracted_by_parse_entry(self):
        from clients.attio import AttioClient

        raw = {
            "id": {"entry_id": "e1", "record_id": "r1"},
            "entry_values": {
                "stage": [{"status": {"title": "Prospect"}}],
                "quality_score": [{"value": 75}],
                "invite_eligible_after": [{"value": "2026-05-25"}],
                "prospect_committed_at": [{"value": "2026-05-21T12:00:00Z"}],
            },
        }
        parsed = AttioClient.parse_entry(raw)
        assert parsed["invite_eligible_after"] == "2026-05-25"
        assert parsed["prospect_committed_at"] == "2026-05-21T12:00:00Z"

    def test_parse_entry_returns_none_when_attr_missing(self):
        """Legacy entries with no quarantine attrs must parse to None
        (not raise) so is_invite_eligible's missing-attr → True path
        fires for the legacy cohort."""
        from clients.attio import AttioClient

        raw = {
            "id": {"entry_id": "e1", "record_id": "r1"},
            "entry_values": {
                "stage": [{"status": {"title": "Prospect"}}],
            },
        }
        parsed = AttioClient.parse_entry(raw)
        assert parsed["invite_eligible_after"] is None
        assert parsed["prospect_committed_at"] is None


# ====================================================================
# Idempotency re-run — escalate() returns existing row on 2nd call
# ====================================================================

class TestStarvationIdempotency:
    def test_second_run_does_not_open_duplicate_row(self, monkeypatch):
        """The starvation function delegates idempotency to escalate(),
        which looks up the uniqueness_key before POSTing. Simulate the
        first call creating a row, then verify the second call's lookup
        returns the row and no second POST fires.
        """
        first_call: list[dict] = []

        attio = MagicMock()
        # Flip-flop fixture: first lookup returns empty, then POST
        # captures the row; subsequent lookups return that captured row.
        captured_row = {"id": {"record_id": "rec-1"}, "values": {}}
        post_count = {"n": 0}

        def _request(method, path, json=None, **_):
            if path.endswith("/records/query"):
                if first_call:
                    return {"data": [captured_row]}
                return {"data": []}
            if path.endswith("/records") and method == "POST":
                post_count["n"] += 1
                first_call.append({"json": json})
                return {"data": captured_row}
            return {"data": {}}

        attio._request.side_effect = _request

        # One trigger: low_prospects.
        monkeypatch.setenv("OUTBOUND_STARVATION_LOW_PROSPECTS_FLOOR", "10")
        monkeypatch.setenv("OUTBOUND_STARVATION_DAILY_INVITE_RATE", "15")
        entries = [_prospect(
            score=80,
            invite_eligible_after="2026-04-01",
            prospect_committed_at="2026-05-19",
        ) for _ in range(3)]
        today = date(2026, 5, 21)

        # 1st run: opens row
        out1 = st.evaluate_pipeline_starvation(
            attio, today, attio_query=lambda _a: entries,
        )
        assert "low_prospects" in out1["triggers_fired"]
        assert post_count["n"] == 1

        # 2nd run with identical state: escalate() finds the row and
        # returns it; no new POST fires.
        out2 = st.evaluate_pipeline_starvation(
            attio, today, attio_query=lambda _a: entries,
        )
        # The trigger still "fires" (we still wanted to escalate), but
        # the queue side stays single-row.
        assert "low_prospects" in out2["triggers_fired"]
        assert post_count["n"] == 1, (
            f"escalate() should reuse existing row on 2nd call; "
            f"got {post_count['n']} POSTs"
        )


# ====================================================================
# Escalation schema registration
# ====================================================================

class TestEscalationSchemaRegistration:
    def test_pipeline_starvation_in_registry(self):
        assert "pipeline_starvation" in ESCALATION_SCHEMAS

    def test_required_field_validation(self):
        """A pipeline_starvation payload missing `trigger` or `today`
        must raise EscalationSchemaError so a starvation-detector bug
        is loud, not silent.
        """
        from workflows import escalation
        from workflows.escalation_schemas import EscalationSchemaError

        with pytest.raises(EscalationSchemaError):
            escalation._validate_payload_against_typeddict(
                "pipeline_starvation",
                {"trigger": "low_prospects"},  # missing `today` + `invite_eligible_pool`
            )
