"""Tests for the Follow-up Radar (models.followup + workflows.followup_radar).

Covers the pure staleness/ranking logic and the read-only detection engine:
lane assignment, per-stage thresholds, active-cadence + suppression exclusion,
urgency ordering (incl. deal value), digest rendering incl. the empty state, and
the observability signals (degraded runs, no-touch drops, schema guards) that
keep a partial Attio outage from masquerading as a clean "Radar limpio ✅".
"""

from __future__ import annotations

from datetime import date

import pytest

from clients.attio import AttioClient
from models.followup import (
    POLICY,
    FollowupReason,
    WarmLane,
    days_silent,
    is_positive_nurture,
    urgency_score,
)
from workflows import followup_radar
from workflows.followup_radar import (
    TOUCH_SOURCE_CONTACT_STAMP,
    TOUCH_SOURCE_CREATED_AT,
    TOUCH_SOURCE_INTERACTION,
    TOUCH_SOURCE_NONE,
    TOUCH_SOURCE_VERIFIED,
    FollowupCandidate,
    _deal_last_touch,
    _deal_recency,
    _deal_value_mult,
    _derive_channel_hint,
    _entry_last_touch,
    _entry_value_mult,
    _parse_attio_date,
    _person_last_interaction_date,
    _state_gate,
    detect_candidates,
    partition_lanes,
    render_digest,
    run_followup_radar,
    to_json,
)

TODAY = date(2026, 7, 1)  # a Wednesday


# ── models.followup ──────────────────────────────────────────────────────


def test_days_silent_business_vs_calendar():
    # 2026-06-25 (Thu) → 2026-07-01 (Wed): business days = Fri,Mon,Tue,Wed = 4
    start = date(2026, 6, 25)
    assert days_silent(start, TODAY, business=True) == 4
    # calendar days = 6
    assert days_silent(start, TODAY, business=False) == 6


def test_urgency_overdue_ratio_and_cap():
    assert urgency_score(heat=4, silent_days=3, threshold_days=3) == 4.0
    assert urgency_score(heat=4, silent_days=4, threshold_days=3) == pytest.approx(5.333, abs=0.01)
    assert urgency_score(heat=4, silent_days=1000, threshold_days=3) == pytest.approx(16.0)


def test_urgency_value_multiplier():
    base = urgency_score(heat=4, silent_days=3, threshold_days=3, value_mult=1.0)
    boosted = urgency_score(heat=4, silent_days=3, threshold_days=3, value_mult=2.0)
    assert boosted == pytest.approx(base * 2)


def test_is_positive_nurture():
    assert is_positive_nurture("positive") is True
    assert is_positive_nurture("question") is True
    assert is_positive_nurture("neutral") is False
    assert is_positive_nurture(None) is False


def test_policy_lanes():
    assert POLICY[FollowupReason.CALL_BOOKED_STALE].lane is WarmLane.OWED
    assert POLICY[FollowupReason.RESPONDED_NO_NEXT_STEP].lane is WarmLane.NUDGE


def test_deal_stage_reasons_use_enum():
    # Regression guard: the deal-stage map must stay coupled to DealStage.
    from models.followup import DEAL_STAGE_REASONS
    from models.pipeline import DealStage

    assert DealStage.LEAD.value in DEAL_STAGE_REASONS
    assert DealStage.IN_PROGRESS.value in DEAL_STAGE_REASONS
    assert DealStage.LOST.value not in DEAL_STAGE_REASONS


# ── workflow helpers ─────────────────────────────────────────────────────


def test_parse_attio_date_variants():
    assert _parse_attio_date("2026-06-01") == date(2026, 6, 1)
    assert _parse_attio_date("2026-06-01T09:30:00Z") == date(2026, 6, 1)
    assert _parse_attio_date(date(2026, 6, 1)) == date(2026, 6, 1)
    assert _parse_attio_date(None) is None
    assert _parse_attio_date("not-a-date") is None


def test_entry_last_touch_takes_max_not_synthetic():
    attrs = {
        "last_contact_date": "2026-06-01",
        "response_received_at": "2026-06-20T10:00:00Z",
        "entry_created_at": "2026-05-01",
    }
    lt, synthetic = _entry_last_touch(attrs)
    assert lt == date(2026, 6, 20)
    assert synthetic is False


def test_entry_last_touch_synthetic_fallback():
    lt, synthetic = _entry_last_touch({"entry_created_at": "2026-05-01"})
    assert lt == date(2026, 5, 1)
    assert synthetic is True


def test_entry_last_touch_none():
    lt, synthetic = _entry_last_touch({})
    assert lt is None
    assert synthetic is False


def test_deal_last_touch_reads_created_at():
    assert _deal_last_touch({"created_at": "2026-06-01T00:00:00Z"}) == date(2026, 6, 1)
    assert _deal_last_touch({}) is None


def test_deal_value_mult_scales_and_caps():
    assert _deal_value_mult(None) == 1.0
    assert _deal_value_mult(0) == 1.0
    assert _deal_value_mult(100_000) == pytest.approx(2.0)
    assert _deal_value_mult(10_000_000) == 3.0  # capped


def test_entry_value_mult_enterprise_bump():
    assert _entry_value_mult({"icp_lane_persisted": 1}) == pytest.approx(1.3)
    assert _entry_value_mult({"icp_lane_persisted": 2}) == 1.0
    assert _entry_value_mult({}) == 1.0


# ── detection ────────────────────────────────────────────────────────────


class FakeAttio:
    """Minimal AttioClient stand-in. Entries/deals are passed pre-parsed;
    parse_entry/parse_deal are patched to identity in the fixture."""

    def __init__(
        self, entries=None, deals=None, active_email_ids=(),
        persons=None, bulk_fetch_exc=None, bulk_fetch_failed=0,
        terminal_email_ids=(), terminal_fetch_exc=None,
    ):
        self._entries = entries or []
        self._deals = deals or []
        self._active = set(active_email_ids)
        # WAITING-lane terminal-email exclusion set; terminal_fetch_exc makes
        # ONLY the terminal-stage queries raise (fail-closed-for-the-lane test).
        self._terminal = set(terminal_email_ids)
        self._terminal_fetch_exc = terminal_fetch_exc
        # v2 person-interaction join: {record_id: raw person record}. The
        # engine's ONE bulk fetch resolves against this; bulk_fetch_exc makes
        # the whole fetch raise, bulk_fetch_failed simulates N per-record
        # failures (bumped onto the metrics shim like the real client does).
        self._persons = persons or {}
        self._bulk_fetch_exc = bulk_fetch_exc
        self._bulk_fetch_failed = bulk_fetch_failed
        self.bulk_fetch_calls: list[set] = []

    @property
    def inner_client(self):
        """Attio escape-hatch handle. run_followup_radar takes a
        CRMProvider and derives the raw client via _attio_inner_client,
        which reads .inner_client; the fake IS its own inner client."""
        return self

    def query_list_entries(self, list_id=None):
        return self._entries

    def search_deals(self, limit=500, *, fail_if_truncated=False):
        return self._deals

    def search_people(self, filter_=None, limit=0):
        stage = (filter_ or {}).get("email_campaign_stage")
        if stage in ("email_responded", "email_not_interested", "unsubscribed"):
            if self._terminal_fetch_exc is not None:
                raise self._terminal_fetch_exc
            return [{"id": {"record_id": rid}} for rid in self._terminal]
        return [{"id": {"record_id": rid}} for rid in self._active]

    # stage + all five followup_* state slugs → schema preflights pass.
    _FULL_SLUGS = [
        {"api_slug": s}
        for s in (
            "stage",
            "followup_draft_at",
            "followup_draft_id",
            "followup_snooze_until",
            "followup_muted",
            "followup_callback_date",
        )
    ]

    def get_object_attributes(self, slug):
        return self._FULL_SLUGS

    def get_list_attributes(self, list_id):
        return self._FULL_SLUGS

    def bulk_fetch_persons_by_record_ids(self, ids, max_workers=8, *, metrics=None):
        self.bulk_fetch_calls.append(set(ids))
        if self._bulk_fetch_exc is not None:
            raise self._bulk_fetch_exc
        if metrics is not None:
            metrics.bulk_fetch_records_requested += len(ids)
            metrics.bulk_fetch_records_failed += self._bulk_fetch_failed
        return {rid: self._persons[rid] for rid in ids if rid in self._persons}

    def get_company(self, cid):
        return None


@pytest.fixture
def identity_parsers(monkeypatch):
    """Make parse_entry/parse_deal identity so fakes can pass parsed dicts,
    and neutralize suppression unless a test provides its own set."""
    monkeypatch.setattr(AttioClient, "parse_entry", staticmethod(lambda e: e))
    monkeypatch.setattr(AttioClient, "parse_deal", staticmethod(lambda r: r))
    monkeypatch.setattr(followup_radar, "build_suppression_set", lambda attio: set())


def _entry(record_id, stage, *, last_contact="2026-06-20", **extra):
    base = {
        "record_id": record_id,
        "entry_id": f"ent-{record_id}",
        "stage": stage,
        "last_contact_date": last_contact,
        "response_received_at": None,
        "entry_created_at": last_contact,
        "response_classification": None,
        "merged_into": None,
        "icp_lane_persisted": None,
        # Channel signals (F1). Default None → channel_hint == "linkedin_only".
        # Pass email_address= or email_campaign_stage= to make a row email-channel.
        "email_address": None,
        "email_campaign_stage": None,
    }
    base.update(extra)
    return base


def _deal(record_id, stage, *, created="2026-06-01", value=None, company_id=None, name="Deal", **extra):
    base = {
        "record_id": record_id,
        "stage": stage,
        "value": value,
        "company_id": company_id,
        "name": name,
        "created_at": created,
    }
    base.update(extra)
    return base


def test_detect_surfaces_stale_responded(identity_parsers):
    attio = FakeAttio(entries=[_entry("r1", "Responded", last_contact="2026-06-25")])
    out = detect_candidates(attio, today=TODAY).candidates
    assert len(out) == 1
    assert out[0].reason is FollowupReason.RESPONDED_NO_NEXT_STEP
    assert out[0].lane is WarmLane.NUDGE
    assert out[0].silent_days == 4


def test_detect_skips_fresh(identity_parsers):
    attio = FakeAttio(entries=[_entry("r1", "Responded", last_contact="2026-06-30")])
    assert detect_candidates(attio, today=TODAY).candidates == []


def test_detect_excludes_suppressed(identity_parsers, monkeypatch):
    monkeypatch.setattr(followup_radar, "build_suppression_set", lambda attio: {"r1"})
    attio = FakeAttio(entries=[_entry("r1", "Responded", last_contact="2026-06-25")])
    assert detect_candidates(attio, today=TODAY).candidates == []


def test_detect_excludes_active_email_drip(identity_parsers):
    attio = FakeAttio(
        entries=[_entry("r1", "Responded", last_contact="2026-06-25")],
        active_email_ids={"r1"},
    )
    assert detect_candidates(attio, today=TODAY).candidates == []


def test_detect_excludes_merged_duplicates(identity_parsers):
    attio = FakeAttio(
        entries=[_entry("r1", "Responded", last_contact="2026-06-25", merged_into="r2")]
    )
    assert detect_candidates(attio, today=TODAY).candidates == []


def test_nurture_requires_positive_classification(identity_parsers):
    cold = FakeAttio(entries=[_entry("r1", "Nurture", last_contact="2026-05-01")])
    assert detect_candidates(cold, today=TODAY).candidates == []
    warm = FakeAttio(
        entries=[_entry("r1", "Nurture", last_contact="2026-05-01", response_classification="positive")]
    )
    out = detect_candidates(warm, today=TODAY).candidates
    assert len(out) == 1
    assert out[0].reason is FollowupReason.NURTURE_POSITIVE_STALE


def test_detect_deals_lead_and_in_progress(identity_parsers):
    attio = FakeAttio(
        deals=[
            _deal("d1", "Lead", created="2026-06-25"),
            _deal("d2", "In Progress", created="2026-06-01"),
            _deal("d3", "Lost", created="2026-01-01"),  # terminal — excluded
        ]
    )
    out = detect_candidates(attio, today=TODAY).candidates
    reasons = {c.record_id: c.reason for c in out}
    assert reasons == {
        "d1": FollowupReason.DEAL_LEAD_UNWORKED,
        "d2": FollowupReason.DEAL_IN_PROGRESS_STALE,
    }


def test_deal_in_progress_calendar_threshold(identity_parsers):
    # In Progress threshold is 10 CALENDAR days (weekends count).
    fresh = FakeAttio(deals=[_deal("d", "In Progress", created="2026-06-23")])  # 8 days
    assert detect_candidates(fresh, today=TODAY).candidates == []
    stale = FakeAttio(deals=[_deal("d", "In Progress", created="2026-06-18")])  # 13 days
    assert len(detect_candidates(stale, today=TODAY).candidates) == 1


def test_ranking_orders_by_urgency_high_first(identity_parsers):
    attio = FakeAttio(
        entries=[
            _entry("cold", "Responded", last_contact="2026-06-25"),   # heat 4
            _entry("hot", "Call Booked", last_contact="2026-06-25"),  # heat 6
        ]
    )
    out = detect_candidates(attio, today=TODAY).candidates
    assert [c.record_id for c in out] == ["hot", "cold"]
    assert out[0].urgency > out[1].urgency


def test_high_value_deal_outranks_fresh_reply(identity_parsers):
    attio = FakeAttio(
        entries=[_entry("reply", "Responded", last_contact="2026-06-25")],  # heat4
        deals=[_deal("bigdeal", "In Progress", created="2026-06-10", value=250_000)],
    )
    out = detect_candidates(attio, today=TODAY).candidates
    assert out[0].record_id == "bigdeal"


def test_synthetic_last_touch_flagged(identity_parsers):
    attio = FakeAttio(
        entries=[_entry("r1", "Call Booked", last_contact=None, entry_created_at="2026-05-01")]
    )
    out = detect_candidates(attio, today=TODAY).candidates
    assert len(out) == 1
    assert out[0].last_touch_synthetic is True


# ── observability: degraded runs must never read clean ───────────────────


def test_entry_none_touch_counted_not_silent(identity_parsers):
    # No datable touch at all → dropped, but counted.
    attio = FakeAttio(
        entries=[_entry("r1", "Responded", last_contact=None, entry_created_at=None)]
    )
    res = detect_candidates(attio, today=TODAY)
    assert res.candidates == []
    assert res.dropped_no_touch == 1


def test_deal_none_touch_counted(identity_parsers):
    attio = FakeAttio(deals=[_deal("d1", "Lead", created=None)])
    res = detect_candidates(attio, today=TODAY)
    assert res.candidates == []
    assert res.dropped_no_touch == 1


def test_deal_schema_missing_skips_deals_and_degrades(identity_parsers):
    class NoStageDeals(FakeAttio):
        def get_object_attributes(self, slug):
            return []  # deals.stage missing

    attio = NoStageDeals(
        entries=[_entry("r1", "Responded", last_contact="2026-06-25")],
        deals=[_deal("d1", "Lead", created="2026-06-25")],
    )
    res = detect_candidates(attio, today=TODAY)
    assert {c.record_id for c in res.candidates} == {"r1"}  # entry still surfaces
    assert any("deal follow-ups skipped" in d for d in res.degraded)


def test_entry_schema_missing_skips_entries_and_degrades(identity_parsers):
    class NoStageList(FakeAttio):
        def get_list_attributes(self, list_id):
            return []  # linkedin_outreach.stage missing

    attio = NoStageList(entries=[_entry("r1", "Responded", last_contact="2026-06-25")])
    res = detect_candidates(attio, today=TODAY)
    assert res.candidates == []
    assert any("entry follow-ups skipped" in d for d in res.degraded)


def test_active_email_partial_failure_degrades(identity_parsers):
    class FlakyEmail(FakeAttio):
        def search_people(self, filter_=None, limit=0):
            raise RuntimeError("attio 503")

    attio = FlakyEmail(entries=[_entry("r1", "Responded", last_contact="2026-06-25")])
    res = detect_candidates(attio, today=TODAY)
    # Candidate still surfaces (fail-open on the partial set) BUT the run is
    # flagged degraded so the operator knows the exclusion had holes.
    assert {c.record_id for c in res.candidates} == {"r1"}
    assert any("cold-email exclusion incomplete" in d for d in res.degraded)


def test_stage_drift_zero_warm_degrades(identity_parsers):
    # Non-empty list yielding zero warm reasons = stage-title drift signal.
    attio = FakeAttio(entries=[_entry("r1", "Some Unknown Stage")])
    res = detect_candidates(attio, today=TODAY)
    assert res.candidates == []
    assert any("stage-title drift" in d for d in res.degraded)


# ── digest rendering ─────────────────────────────────────────────────────


def test_render_empty_clean_state():
    md = render_digest([])
    assert "Radar limpio" in md


def test_render_empty_but_degraded_is_not_clean():
    md = render_digest([], degraded=["active cold-email exclusion incomplete"])
    assert "Radar limpio" not in md
    assert "degraded" in md.lower()


def test_render_dropped_no_touch_banner():
    md = render_digest([], dropped_no_touch=3)
    assert "Radar limpio" not in md
    assert "no datable touch" in md


def test_render_has_owed_and_nudge_sections(identity_parsers):
    # Email-channel entries land in the Owed/Nudge sections; a linkedin_only
    # entry would route to the LinkedIn-warm section instead (see F3 tests).
    attio = FakeAttio(
        entries=[
            _entry("owed1", "Call Booked", last_contact="2026-06-20", email_address="a@x.com"),
            _entry("nudge1", "Responded", last_contact="2026-06-20", email_address="b@x.com"),
        ]
    )
    out = detect_candidates(attio, today=TODAY).candidates
    md = render_digest(out)
    assert "Owed — do these" in md
    assert "Consider nudging" in md


def test_render_banner_shows_when_candidates_present(identity_parsers):
    attio = FakeAttio(entries=[_entry("r1", "Responded", last_contact="2026-06-20")])
    out = detect_candidates(attio, today=TODAY).candidates
    md = render_digest(out, degraded=["exclusion incomplete"], dropped_no_touch=2)
    assert "Detection degraded" in md
    assert "exclusion incomplete" in md
    assert "no datable touch" in md


def test_nudge_lane_collapses_by_default_and_expands_with_full(identity_parsers):
    # 5 nudge candidates → default preview shows 3 + "…+2 more"; full shows all.
    # Email-channel so they land in Nudge (not the LinkedIn-warm section).
    entries = [
        _entry(f"n{i}", "Responded", last_contact="2026-06-20", email_address=f"n{i}@x.com")
        for i in range(5)
    ]
    out = detect_candidates(FakeAttio(entries=entries), today=TODAY).candidates
    collapsed = render_digest(out)
    assert "…+2 more cooling" in collapsed
    assert "sales followup --full" in collapsed
    full = render_digest(out, full=True)
    assert "more cooling" not in full


def test_deal_candidate_marked_synthetic(identity_parsers):
    attio = FakeAttio(deals=[_deal("d1", "Lead", created="2026-06-25")])
    out = detect_candidates(attio, today=TODAY).candidates
    assert len(out) == 1
    assert out[0].last_touch_synthetic is True


def test_deal_in_progress_exact_threshold_surfaces(identity_parsers):
    # Exactly 10 calendar days (2026-06-21 → 2026-07-01) must surface (>=).
    attio = FakeAttio(deals=[_deal("d", "In Progress", created="2026-06-21")])
    assert len(detect_candidates(attio, today=TODAY).candidates) == 1


def test_owed_lane_stable_ordering_by_heat_then_silence(identity_parsers):
    # Call Booked (heat 6) must precede a deal Lead (heat 5) regardless of
    # silence; within equal heat, longer silence (older last_touch) first.
    # Both are email/unknown channel so they share the Owed section (a
    # linkedin_only Call Booked would route to LinkedIn-warm instead).
    attio = FakeAttio(
        entries=[
            _entry("call", "Call Booked", last_contact="2026-06-20", email_address="c@x.com"),  # heat6
        ],
        deals=[_deal("lead", "Lead", created="2026-01-01", name="lead")],  # heat5, very old
    )
    out = detect_candidates(attio, today=TODAY).candidates
    md = render_digest(out, full=True)
    # In the Owed section, the heat-6 call must be listed before the heat-5 lead.
    owed_section = md.split("### Owed")[1]
    assert owed_section.index("call") < owed_section.index("lead")


# ── Phase 2: state-aware detection (mute / snooze / callback / draft dedup) ──


def test_state_gate_muted_and_snoozed_exclude():
    assert _state_gate({"followup_muted": True}, TODAY, None)[0] == "exclude"
    assert _state_gate({"followup_snooze_until": "2026-07-15"}, TODAY, None)[0] == "exclude"
    # a past snooze does not exclude
    assert _state_gate({"followup_snooze_until": "2026-06-01"}, TODAY, None)[0] == "surface"


def test_state_gate_callback_future_excludes_due_surfaces():
    assert _state_gate({"followup_callback_date": "2026-08-01"}, TODAY, None)[0] == "exclude"
    assert _state_gate({"followup_callback_date": "2026-07-01"}, TODAY, None)[0] == "callback"
    assert _state_gate({"followup_callback_date": "2026-06-15"}, TODAY, None)[0] == "callback"


def test_state_gate_draft_cooldown_stale_and_advanced():
    # drafted 2 days ago, not advanced → suppress (cooldown)
    assert _state_gate({"followup_draft_at": "2026-06-29"}, TODAY, date(2026, 6, 20))[0] == "drafted_recent"
    # drafted 10 days ago, not advanced → re-surface with an escalation note
    verdict, note = _state_gate({"followup_draft_at": "2026-06-21"}, TODAY, date(2026, 6, 20))
    assert verdict == "surface" and note and "unsent" in note
    # drafted, but real activity advanced past the draft → not suppressed
    assert _state_gate({"followup_draft_at": "2026-06-21"}, TODAY, date(2026, 6, 25))[0] == "surface"


def test_muted_entry_excluded(identity_parsers):
    attio = FakeAttio(
        entries=[_entry("r1", "Responded", last_contact="2026-06-25", followup_muted=True)]
    )
    assert detect_candidates(attio, today=TODAY).candidates == []


def test_callback_due_surfaces_owed_even_when_fresh(identity_parsers):
    # last_contact is fresh (would not normally surface), but the callback is due.
    attio = FakeAttio(
        entries=[_entry("r1", "Responded", last_contact="2026-06-30", followup_callback_date="2026-07-01")]
    )
    out = detect_candidates(attio, today=TODAY).candidates
    assert len(out) == 1
    assert out[0].reason is FollowupReason.CALLBACK_DUE
    assert out[0].lane is WarmLane.OWED


def test_drafted_recent_skipped_and_counted(identity_parsers):
    attio = FakeAttio(
        entries=[_entry("r1", "Responded", last_contact="2026-06-25", followup_draft_at="2026-06-30")]
    )
    res = detect_candidates(attio, today=TODAY)
    assert res.candidates == []
    assert res.drafted_skipped == 1


def test_stale_unsent_draft_resurfaces_with_note(identity_parsers):
    # Draft (06-21) made AFTER the last contact (06-10) and never sent; today is
    # 07-01 → 10 days since draft, no activity advanced past it → re-surface.
    attio = FakeAttio(
        entries=[_entry("r1", "Responded", last_contact="2026-06-10", followup_draft_at="2026-06-21")]
    )
    out = detect_candidates(attio, today=TODAY).candidates
    assert len(out) == 1
    assert out[0].notes and "unsent" in out[0].notes[0]


def test_snoozed_deal_excluded(identity_parsers):
    attio = FakeAttio(
        deals=[_deal("d1", "Lead", created="2026-06-25", followup_snooze_until="2026-07-20")]
    )
    assert detect_candidates(attio, today=TODAY).candidates == []


def test_parked_pool_counted_by_reason(identity_parsers):
    entries = [
        _entry("m", "Responded", last_contact="2026-06-25", followup_muted=True),
        _entry("s", "Responded", last_contact="2026-06-25", followup_snooze_until="2026-07-20"),
        _entry("c", "Responded", last_contact="2026-06-25", followup_callback_date="2026-09-01"),
    ]
    res = detect_candidates(FakeAttio(entries=entries), today=TODAY)
    assert res.candidates == []
    assert res.parked == {"muted": 1, "snoozed": 1, "callback": 1}


def test_render_parked_not_reported_clean():
    md = render_digest([], parked={"muted": 5, "snoozed": 2, "callback": 1})
    assert "Radar limpio" not in md
    assert "8 parked" in md
    assert "5 muted" in md


def test_followup_state_schema_incomplete_degrades(identity_parsers):
    class NoStateSchema(FakeAttio):
        def get_list_attributes(self, list_id):
            return [{"api_slug": "stage"}]  # followup_* attrs absent

    res = detect_candidates(NoStateSchema(entries=[]), today=TODAY, list_id="L-1")
    assert any("state schema incomplete" in d for d in res.degraded)


def test_deal_last_touch_values_fallback():
    rec = {"values": {"created_at": [{"value": "2026-06-01T00:00:00Z"}]}}
    assert _deal_last_touch(rec) == date(2026, 6, 1)


def test_followup_cmd_errors_cleanly(monkeypatch):
    """Standalone command must print a clean error + exit 1, not raw-crash."""
    from click.testing import CliRunner

    from cli import cli

    class _DummyAttio:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("clients.attio.AttioClient", _DummyAttio)

    def _boom(*a, **k):
        raise RuntimeError("attio down")

    monkeypatch.setattr("workflows.followup_radar.run_followup_radar", _boom)

    result = CliRunner().invoke(cli, ["followup"])
    assert result.exit_code == 1
    assert "follow-up radar failed" in result.output


# ── F1: channel_hint derivation ──────────────────────────────────────────


def test_channel_hint_email_when_address_present():
    assert _derive_channel_hint(
        object="linkedin_outreach", email_address="a@x.com", email_campaign_stage=None
    ) == "email"


def test_channel_hint_email_when_campaign_stage_present():
    assert _derive_channel_hint(
        object="linkedin_outreach", email_address=None, email_campaign_stage="email1_sent"
    ) == "email"


def test_channel_hint_linkedin_only_when_neither():
    assert _derive_channel_hint(
        object="linkedin_outreach", email_address=None, email_campaign_stage=None
    ) == "linkedin_only"


def test_channel_hint_deals_email_vs_unknown():
    # A deal with any email evidence → "email"; with none → "unknown"
    # (the skill layer still tries an Attio email search for unknown deals).
    assert _derive_channel_hint(
        object="deals", email_address="buyer@co.com", email_campaign_stage=None
    ) == "email"
    assert _derive_channel_hint(
        object="deals", email_address=None, email_campaign_stage=None
    ) == "unknown"


def test_channel_hint_whitespace_is_not_evidence():
    # Whitespace-only strings must not upgrade a row to "email".
    assert _derive_channel_hint(
        object="linkedin_outreach", email_address="   ", email_campaign_stage=None
    ) == "linkedin_only"
    assert _derive_channel_hint(
        object="linkedin_outreach", email_address=None, email_campaign_stage=" "
    ) == "linkedin_only"
    assert _derive_channel_hint(
        object="deals", email_address="  ", email_campaign_stage=None
    ) == "unknown"


def test_channel_hint_non_str_is_not_evidence():
    # Truthy non-str shapes (a raw Attio list/dict leaking through, a number)
    # must not upgrade a row to "email" when the person-attr mirror lands.
    assert _derive_channel_hint(
        object="linkedin_outreach", email_address=["a@x.com"], email_campaign_stage=None
    ) == "linkedin_only"
    assert _derive_channel_hint(
        object="linkedin_outreach",
        email_address=None,
        email_campaign_stage={"option": {"title": "email1_sent"}},
    ) == "linkedin_only"
    assert _derive_channel_hint(
        object="deals", email_address=1, email_campaign_stage=None
    ) == "unknown"


def test_entry_channel_hint_on_candidate(identity_parsers):
    # linkedin_outreach entry with no email signal → linkedin_only on the candidate.
    ln = FakeAttio(entries=[_entry("r1", "Responded", last_contact="2026-06-25")])
    out = detect_candidates(ln, today=TODAY).candidates
    assert out[0].channel_hint == "linkedin_only"
    # …with an email address → email.
    em = FakeAttio(
        entries=[_entry("r2", "Responded", last_contact="2026-06-25", email_address="a@x.com")]
    )
    assert detect_candidates(em, today=TODAY).candidates[0].channel_hint == "email"


def test_deal_channel_hint_unknown(identity_parsers):
    out = detect_candidates(
        FakeAttio(deals=[_deal("d1", "Lead", created="2026-06-25")]), today=TODAY
    ).candidates
    assert out[0].channel_hint == "unknown"


# ── F2: Partner Intro lane ───────────────────────────────────────────────


def test_partner_intro_routes_to_partner_lane(identity_parsers):
    attio = FakeAttio(entries=[_entry("p1", "Partner Intro", last_contact="2026-06-20")])
    out = detect_candidates(attio, today=TODAY).candidates
    assert len(out) == 1
    assert out[0].reason is FollowupReason.PARTNER_INTRO_UNWORKED
    assert out[0].lane is WarmLane.PARTNER


def test_partner_section_renders_above_owed(identity_parsers):
    attio = FakeAttio(
        entries=[_entry("p1", "Partner Intro", last_contact="2026-06-20")],
        deals=[_deal("d1", "Lead", created="2026-06-25", name="d1")],  # heat5 Owed
    )
    out = detect_candidates(attio, today=TODAY).candidates
    md = render_digest(out, full=True)
    assert "Partner intros" in md
    assert "Owed — do these" in md
    assert md.index("Partner intros") < md.index("Owed — do these")


def test_partner_lane_preview_cap_and_full(identity_parsers):
    # 12 partner rows → default preview shows _PARTNER_PREVIEW (10) + overflow.
    entries = [
        _entry(f"p{i}", "Partner Intro", last_contact="2026-06-20") for i in range(12)
    ]
    out = detect_candidates(FakeAttio(entries=entries), today=TODAY).candidates
    collapsed = render_digest(out)
    assert "…+2 more partner intros" in collapsed
    full = render_digest(out, full=True)
    assert "more partner intros" not in full


def test_partner_only_run_is_not_radar_limpio(identity_parsers):
    attio = FakeAttio(entries=[_entry("p1", "Partner Intro", last_contact="2026-06-20")])
    out = detect_candidates(attio, today=TODAY).candidates
    md = render_digest(out)
    assert "Radar limpio" not in md
    assert "Partner intros" in md


def test_summary_dict_has_partner_count(identity_parsers):
    attio = FakeAttio(
        entries=[
            _entry("p1", "Partner Intro", last_contact="2026-06-20"),
            _entry("p2", "Partner Intro", last_contact="2026-06-20"),
        ]
    )
    summary = run_followup_radar(attio, today=TODAY)
    assert summary["partner"] == 2


def test_summary_counts_disjoint_and_match_digest_header(identity_parsers):
    """The summary dict and the digest header derive from the SAME partition
    (partition_lanes): counts are disjoint, sum to surfaced, and the header
    split line shows exactly the same numbers — the CLI footer (which formats
    the summary dict) can never disagree with the digest header."""
    # One of each bucket, plus a second LinkedIn-warm: partner, email owed,
    # linkedin_only owed-lane (→ LinkedIn-warm), email nudge, linkedin_only
    # nudge-lane (→ LinkedIn-warm).
    attio = FakeAttio(
        entries=[
            _entry("p1", "Partner Intro", last_contact="2026-06-20"),
            _entry("eo1", "Call Booked", last_contact="2026-06-20", email_address="a@x.com"),
            _entry("lo1", "Call Booked", last_contact="2026-06-20"),   # linkedin_only, owed lane
            _entry("en1", "Responded", last_contact="2026-06-20", email_address="b@x.com"),
            _entry("ln1", "Responded", last_contact="2026-06-20"),     # linkedin_only, nudge lane
        ]
    )
    summary = run_followup_radar(attio, today=TODAY)
    # Disjoint split: the two linkedin_only rows are NOT counted in owed/nudge.
    assert summary["partner"] == 1
    assert summary["owed"] == 1
    assert summary["linkedin_warm"] == 2
    assert summary["nudge"] == 1
    assert (
        summary["partner"] + summary["owed"] + summary["linkedin_warm"] + summary["nudge"]
        == summary["surfaced"]
        == 5
    )
    # The digest header split line shows the same numbers.
    digest = summary["digest"]
    assert "1 partner intro" in digest
    assert "1 owed" in digest
    assert "2 LinkedIn-warm" in digest
    assert "1 nudge" in digest
    # And the per-section counts agree too.
    assert "a partner's credibility is on the line (1)" in digest
    assert "Owed — do these (1)" in digest
    assert "LinkedIn warm — no email on file (DM likely) (2)" in digest
    assert "Consider nudging (1)" in digest


def _hand_candidate(record_id, lane, channel_hint="unknown"):
    """Hand-built candidate for partition-invariant tests (bypasses detection)."""
    return FollowupCandidate(
        object="linkedin_outreach",
        record_id=record_id,
        reason=FollowupReason.RESPONDED_NO_NEXT_STEP,
        lane=lane,
        last_touch=date(2026, 6, 20),
        silent_days=5,
        heat=4,
        value_mult=1.0,
        urgency=4.0,
        channel_hint=channel_hint,
    )


def test_partition_invariant_raises_on_unpartitionable_candidate():
    """A candidate no bucket claims (e.g. a future WarmLane member added
    without a partition rule) must raise loudly, naming the record_id —
    never vanish silently from the digest and counts."""
    good = _hand_candidate("good", WarmLane.NUDGE)
    # lane=None stands in for a future WarmLane member: not PARTNER, not
    # OWED/NUDGE, channel "unknown" → no bucket claims it.
    rogue = _hand_candidate("rogue-id", None)
    with pytest.raises(ValueError, match="rogue-id"):
        partition_lanes([good, rogue])
    # Sanity: without the rogue, the same set partitions cleanly.
    lanes = partition_lanes([good])
    assert [c.record_id for c in lanes["nudge"]] == ["good"]


# ── F3: LinkedIn-warm section ────────────────────────────────────────────


def test_linkedin_warm_section_renders(identity_parsers):
    # A linkedin_only (no email) Call Booked entry → LinkedIn-warm section, not Owed.
    attio = FakeAttio(entries=[_entry("r1", "Call Booked", last_contact="2026-06-20")])
    out = detect_candidates(attio, today=TODAY).candidates
    md = render_digest(out, full=True)
    assert "LinkedIn warm — no email on file (DM likely)" in md
    # It is NOT double-listed under Owed.
    assert "Owed — do these" not in md


def test_linkedin_only_removed_from_owed_and_nudge(identity_parsers):
    # One email Owed + one linkedin_only Owed + one linkedin_only Nudge.
    # record_ids kept ≤8 chars so the _who fallback (record_id[:8]) shows them whole.
    attio = FakeAttio(
        entries=[
            _entry("emowed", "Call Booked", last_contact="2026-06-20", email_address="a@x.com"),
            _entry("lnowed", "Call Booked", last_contact="2026-06-20"),   # linkedin_only
            _entry("lnnudge", "Responded", last_contact="2026-06-20"),    # linkedin_only
        ]
    )
    out = detect_candidates(attio, today=TODAY).candidates
    md = render_digest(out, full=True)
    owed_section = md.split("### Owed — do these")[1].split("###")[0]
    assert "emowed" in owed_section
    assert "lnowed" not in owed_section
    # The two linkedin_only rows live in the LinkedIn-warm section.
    ln_section = md.split("### LinkedIn warm")[1]
    assert "lnowed" in ln_section
    assert "lnnudge" in ln_section
    # No Nudge section rendered (the only Responded row was linkedin_only).
    assert "Consider nudging" not in md


def test_linkedin_warm_preview_cap(identity_parsers):
    entries = [
        _entry(f"l{i}", "Responded", last_contact="2026-06-20") for i in range(5)
    ]
    out = detect_candidates(FakeAttio(entries=entries), today=TODAY).candidates
    collapsed = render_digest(out)
    assert "…+2 more LinkedIn-warm" in collapsed


def test_counts_line_shows_split(identity_parsers):
    attio = FakeAttio(
        entries=[
            _entry("emailowed", "Call Booked", last_contact="2026-06-20", email_address="a@x.com"),
            _entry("lnwarm", "Responded", last_contact="2026-06-20"),  # linkedin_only nudge
        ]
    )
    out = detect_candidates(attio, today=TODAY).candidates
    md = render_digest(out)
    # No "(email)" qualifier — matches the cli.py footer wording exactly.
    assert "1 owed" in md
    assert "1 owed (email)" not in md
    assert "1 LinkedIn-warm" in md


def test_linkedin_only_still_in_json_with_channel_hint(identity_parsers):
    attio = FakeAttio(entries=[_entry("r1", "Responded", last_contact="2026-06-20")])
    out = detect_candidates(attio, today=TODAY).candidates
    payload = to_json(out)
    assert len(payload) == 1
    assert payload[0]["channel_hint"] == "linkedin_only"
    assert payload[0]["lane"] == WarmLane.NUDGE.value


# ── F1/F6: JSON fields present for both entry and deal candidates ─────────


def test_json_has_new_fields_for_entry_and_deal(identity_parsers):
    attio = FakeAttio(
        entries=[_entry("r1", "Responded", last_contact="2026-06-20", email_address="a@x.com")],
        deals=[_deal("d1", "Lead", created="2026-06-25")],
    )
    out = detect_candidates(attio, today=TODAY).candidates
    payload = to_json(out)
    for row in payload:
        assert "object" in row
        assert "channel_hint" in row
        assert "email_campaign_stage" in row
        assert "email_address" in row
    by_id = {r["record_id"]: r for r in payload}
    assert by_id["r1"]["channel_hint"] == "email"
    assert by_id["r1"]["email_address"] == "a@x.com"
    assert by_id["d1"]["channel_hint"] == "unknown"


# ── F4: deal-side partner attribution (referred_by) ──────────────────────


def test_real_parse_deal_extracts_referred_by():
    """The REAL AttioClient.parse_deal (no identity monkeypatch) must carry
    referred_by through from a raw Attio deal payload — present value and
    absent→None. Guards the extraction the radar tests bypass via the
    identity_parsers fixture."""
    raw = {
        "id": {"record_id": "rec_referred"},
        "values": {
            "name": [{"value": "Referred Co"}],
            "stage": [{"status": {"title": "Lead"}}],
            "referred_by": [{"value": "xavi@partner.com"}],
        },
    }
    out = AttioClient.parse_deal(raw)
    assert out["referred_by"] == "xavi@partner.com"
    # Sanity: the rest of the parse is unaffected.
    assert out["record_id"] == "rec_referred"
    assert out["stage"] == "Lead"

    # Absent attribute (pre-migration deal) → None, key still present.
    plain = {
        "id": {"record_id": "rec_plain"},
        "values": {
            "name": [{"value": "Plain Co"}],
            "stage": [{"status": {"title": "Lead"}}],
        },
    }
    out_plain = AttioClient.parse_deal(plain)
    assert "referred_by" in out_plain
    assert out_plain["referred_by"] is None


def test_channel_hint_deal_referred_by_is_email_evidence():
    # A partner referral is email evidence by construction → "email".
    assert _derive_channel_hint(
        object="deals", email_address=None, email_campaign_stage=None,
        referred_by="xavi@partner.com",
    ) == "email"
    # Whitespace-only referral is NOT evidence.
    assert _derive_channel_hint(
        object="deals", email_address=None, email_campaign_stage=None,
        referred_by="   ",
    ) == "unknown"
    # referred_by never upgrades the entry-side derivation (deal-only signal).
    assert _derive_channel_hint(
        object="linkedin_outreach", email_address=None, email_campaign_stage=None,
        referred_by="xavi@partner.com",
    ) == "linkedin_only"


def test_deal_referred_by_routes_to_partner_lane(identity_parsers):
    attio = FakeAttio(
        deals=[_deal("d1", "Lead", created="2026-06-25", referred_by="xavi@partner.com")]
    )
    out = detect_candidates(attio, today=TODAY).candidates
    assert len(out) == 1
    # Lane overridden to PARTNER (Lead would otherwise be OWED); the
    # stage-derived reason/label is preserved.
    assert out[0].lane is WarmLane.PARTNER
    assert out[0].reason is FollowupReason.DEAL_LEAD_UNWORKED
    assert out[0].channel_hint == "email"
    assert out[0].referred_by == "xavi@partner.com"


def test_deal_empty_referred_by_unchanged(identity_parsers):
    # Empty/whitespace referred_by → unchanged behavior (owed lane, unknown hint).
    attio = FakeAttio(
        deals=[
            _deal("d1", "Lead", created="2026-06-25", referred_by=""),
            _deal("d2", "Lead", created="2026-06-25", referred_by="  "),
        ]
    )
    out = detect_candidates(attio, today=TODAY).candidates
    by_id = {c.record_id: c for c in out}
    for c in out:
        assert c.lane is WarmLane.OWED
        assert c.channel_hint == "unknown"
    # And a deal with no referred_by attr at all (pre-migration) is unchanged.
    plain = FakeAttio(deals=[_deal("d3", "Lead", created="2026-06-25")])
    p = detect_candidates(plain, today=TODAY).candidates[0]
    assert p.lane is WarmLane.OWED
    assert p.channel_hint == "unknown"
    assert p.referred_by is None


def test_deal_referred_by_appears_in_partner_section_above_owed(identity_parsers):
    attio = FakeAttio(
        deals=[
            _deal("dref", "Lead", created="2026-06-25", name="Referred Co",
                  referred_by="xavi@partner.com"),
            _deal("dplain", "Lead", created="2026-06-25", name="Plain Co",
                  email_address="buyer@plain.com"),  # email owed
        ]
    )
    out = detect_candidates(attio, today=TODAY).candidates
    md = render_digest(out, full=True)
    assert "Partner intros" in md
    assert "Owed — do these" in md
    assert md.index("Partner intros") < md.index("Owed — do these")
    # The referred deal is in the partner section, the plain one in Owed.
    partner_section = md.split("Partner intros")[1].split("###")[0]
    assert "Referred Co" in partner_section
    assert "Plain Co" not in partner_section


def test_deal_referred_by_renders_via_suffix(identity_parsers):
    attio = FakeAttio(
        deals=[_deal("dref", "Lead", created="2026-06-25", name="Referred Co",
                     referred_by="gustavo@partner.com")]
    )
    out = detect_candidates(attio, today=TODAY).candidates
    md = render_digest(out, full=True)
    assert "· via gustavo@partner.com" in md


def test_deal_referred_by_carried_in_json(identity_parsers):
    attio = FakeAttio(
        deals=[
            _deal("dref", "Lead", created="2026-06-25", referred_by="xavi@partner.com"),
            _deal("dplain", "Lead", created="2026-06-25"),
        ]
    )
    out = detect_candidates(attio, today=TODAY).candidates
    by_id = {r["record_id"]: r for r in to_json(out)}
    assert "referred_by" in by_id["dref"]
    assert by_id["dref"]["referred_by"] == "xavi@partner.com"
    assert by_id["dref"]["lane"] == WarmLane.PARTNER.value
    assert by_id["dplain"]["referred_by"] is None


def test_partition_invariant_holds_with_mixed_entry_and_deal_partner_rows(identity_parsers):
    # Entry Partner Intro + deal referred_by + a plain email owed deal +
    # a linkedin_only entry: all four buckets exercised, invariant must hold.
    attio = FakeAttio(
        entries=[
            _entry("pentry", "Partner Intro", last_contact="2026-06-20"),
            _entry("lnwarm", "Responded", last_contact="2026-06-20"),  # linkedin_only nudge
        ],
        deals=[
            _deal("pdeal", "Lead", created="2026-06-25", name="pdeal",
                  referred_by="harlyn@partner.com"),
            _deal("owed", "Lead", created="2026-06-25", name="owed",
                  email_address="buyer@owed.com"),
        ],
    )
    out = detect_candidates(attio, today=TODAY).candidates
    lanes = partition_lanes(out)  # must not raise
    assert {c.record_id for c in lanes["partner"]} == {"pentry", "pdeal"}
    assert {c.record_id for c in lanes["owed"]} == {"owed"}
    assert {c.record_id for c in lanes["linkedin_warm"]} == {"lnwarm"}
    assert lanes["nudge"] == []
    assigned = sum(len(v) for v in lanes.values())
    assert assigned == len(out)


# ── v2: person-interaction join + three-tier deal recency ────────────────


def _person(last_interaction=None):
    """Raw Attio person record with the live-verified last_interaction shape."""
    vals = {}
    if last_interaction is not None:
        vals["last_interaction"] = [
            {
                "interacted_at": last_interaction,
                "interaction_type": "email",
                "attribute_type": "interaction",
            }
        ]
    return {"values": vals}


def test_person_last_interaction_date_shapes():
    assert _person_last_interaction_date(
        _person("2026-03-20T18:47:39.000000000Z")
    ) == date(2026, 3, 20)
    # Never-interacted / absent / malformed shapes all resolve to None, never raise.
    assert _person_last_interaction_date(_person()) is None
    assert _person_last_interaction_date({}) is None
    assert _person_last_interaction_date({"values": None}) is None
    assert _person_last_interaction_date({"values": {"last_interaction": "junk"}}) is None
    assert _person_last_interaction_date({"values": {"last_interaction": ["junk"]}}) is None
    assert _person_last_interaction_date({"values": {"last_interaction": [{}]}}) is None
    assert _person_last_interaction_date(
        {"values": {"last_interaction": [{"interacted_at": "garbage"}]}}
    ) is None


def test_deal_recency_precedence_matrix():
    record = {"created_at": "2026-01-01"}
    interactions = {"p1": date(2026, 6, 30)}
    # Tier 1: verified wins even over a NEWER synced interaction (C.2
    # re-verification refreshes the stamp; it can carry off-thread knowledge).
    deal = {"last_verified_touch": "2026-03-20", "associated_people": ["p1"]}
    assert _deal_recency(deal, record, interactions, TODAY) == (
        date(2026, 3, 20), False, TOUCH_SOURCE_VERIFIED,
    )
    # Tier 2: max over the join when no verified stamp.
    deal = {"associated_people": ["p1", "p2"]}
    assert _deal_recency(
        deal, record, {"p1": date(2026, 5, 1), "p2": date(2026, 6, 2)}, TODAY
    ) == (
        date(2026, 6, 2), False, TOUCH_SOURCE_INTERACTION,
    )
    # Tier 3: created_at fallback is the ONLY synthetic tier.
    assert _deal_recency({"associated_people": []}, record, {}, TODAY) == (
        date(2026, 1, 1), True, TOUCH_SOURCE_CREATED_AT,
    )
    # Associated people with no interaction data fall through to tier 3 too.
    assert _deal_recency({"associated_people": ["ghost"]}, record, {}, TODAY) == (
        date(2026, 1, 1), True, TOUCH_SOURCE_CREATED_AT,
    )
    # Tier 4: nothing datable → dropped (counted by the caller).
    assert _deal_recency({}, {}, {}, TODAY) == (None, False, TOUCH_SOURCE_NONE)
    # Absent/None attrs on the deal never raise.
    assert _deal_recency(
        {"last_verified_touch": None, "associated_people": None}, {}, {}, TODAY,
    ) == (None, False, TOUCH_SOURCE_NONE)


def test_resolve_deal_recency_matches_full_resolver_on_parsed_deal():
    """The parsed-deal wrapper (shared with weekly_report) resolves every
    tier identically to `_deal_recency(deal, record, ...)`, because
    `parse_deal` now carries the top-level `created_at`."""
    from workflows.followup_radar import resolve_deal_recency

    interactions = {"p1": date(2026, 6, 2)}
    cases = [
        {"last_verified_touch": "2026-03-20", "associated_people": ["p1"],
         "created_at": "2026-01-01"},                              # tier 1
        {"associated_people": ["p1"], "created_at": "2026-01-01"},  # tier 2
        {"associated_people": [], "created_at": "2026-01-01"},      # tier 3
        {},                                                          # tier 4
    ]
    for deal in cases:
        record = {"created_at": deal.get("created_at")}
        assert resolve_deal_recency(deal, interactions, TODAY) == (
            _deal_recency(deal, record, interactions, TODAY)
        )


def test_sanitize_future_touch_dates():
    """A future-dated touch must never hide a deal (silence would read 0).

    One day of skew is legitimate (UTC timestamps read 'tomorrow' during the
    operator's evening) and clamps to today; further future is invalid — the
    resolver falls to the next tier. Read-side twin of the write-side guard."""
    from datetime import timedelta

    record = {"created_at": "2026-01-01"}
    # Verified stamp 'tomorrow' (UTC skew) → clamps to today, stays tier 1.
    deal = {"last_verified_touch": (TODAY + timedelta(days=1)).isoformat()}
    assert _deal_recency(deal, record, {}, TODAY) == (
        TODAY, False, TOUCH_SOURCE_VERIFIED,
    )
    # Verified stamp far-future (hand-edited via Attio UI, bypassing the CLI
    # guard) → ignored, falls through to the join / created_at.
    deal = {"last_verified_touch": "2027-07-02", "associated_people": ["p1"]}
    assert _deal_recency(deal, record, {"p1": date(2026, 5, 1)}, TODAY) == (
        date(2026, 5, 1), False, TOUCH_SOURCE_INTERACTION,
    )
    # Same for a future-dated synced interaction (calendar-sync quirk).
    deal = {"associated_people": ["p1"]}
    assert _deal_recency(deal, record, {"p1": date(2027, 1, 1)}, TODAY) == (
        date(2026, 1, 1), True, TOUCH_SOURCE_CREATED_AT,
    )


def test_candidate_invariant_enforced_at_construction():
    """synthetic ⟺ source=='created_at' — a construction site that breaks the
    coupling must raise, not silently mislabel."""
    with pytest.raises(ValueError, match="contradicts"):
        FollowupCandidate(
            object="deals",
            record_id="bad",
            reason=FollowupReason.DEAL_IN_PROGRESS_STALE,
            lane=WarmLane.NUDGE,
            last_touch=date(2026, 6, 1),
            silent_days=30,
            heat=3,
            value_mult=1.0,
            urgency=3.0,
            last_touch_synthetic=True,  # says created_at…
            last_touch_source=TOUCH_SOURCE_INTERACTION,  # …but labeled real
        )


def test_detect_join_recency_unstales_an_old_looking_deal(identity_parsers):
    """The v1 fake-ranking killer: an OLD deal whose person interacted recently
    is NOT stale — deal age must no longer manufacture urgency."""
    attio = FakeAttio(
        deals=[_deal("d", "In Progress", created="2026-01-01",
                     associated_people=["p1"])],
        persons={"p1": _person("2026-06-30T12:00:00Z")},  # 1 day ago
    )
    assert detect_candidates(attio, today=TODAY).candidates == []


def test_detect_join_recency_stales_a_fresh_looking_deal(identity_parsers):
    """The Bafar replay in miniature: a recently-created deal whose real last
    interaction is months old surfaces with REAL silence, not deal age."""
    attio = FakeAttio(
        deals=[_deal("d", "In Progress", created="2026-06-25",  # 6d — under threshold
                     associated_people=["p1", "pnone"])],
        persons={"p1": _person("2026-03-20T18:47:39Z"), "pnone": _person()},
    )
    out = detect_candidates(attio, today=TODAY).candidates
    assert len(out) == 1
    c = out[0]
    assert c.last_touch == date(2026, 3, 20)
    assert c.last_touch_synthetic is False
    assert c.last_touch_source == TOUCH_SOURCE_INTERACTION
    assert c.silent_days == (TODAY - date(2026, 3, 20)).days


def test_detect_verified_touch_beats_join_for_ranking(identity_parsers):
    """Ranking precedence is strict: a verified stamp wins tier 1 even though
    the deal's people ARE fetched (the gate needs the join — see the
    drafted-advance test below)."""
    attio = FakeAttio(
        deals=[_deal("d", "In Progress", created="2026-01-01",
                     last_verified_touch="2026-06-28",
                     associated_people=["p1"])],
        persons={"p1": _person("2026-03-20T00:00:00Z")},
    )
    result = detect_candidates(attio, today=TODAY)
    assert result.candidates == []  # verified 3 days ago → not stale
    assert attio.bulk_fetch_calls == [{"p1"}]  # fetched for the gate


def test_detect_parked_deals_skip_the_join_fetch(identity_parsers):
    """Muted / future-snoozed deals are excluded by the gate before recency is
    ever consulted — their people are dropped from the bulk fetch (the join
    data would be computed and discarded). Drafted deals stay IN the fetch."""
    attio = FakeAttio(
        deals=[
            _deal("dmuted", "In Progress", created="2026-01-01",
                  followup_muted=True, associated_people=["pm"]),
            _deal("dsnoozed", "In Progress", created="2026-01-01",
                  followup_snooze_until="2026-08-01", associated_people=["ps"]),
            _deal("ddrafted", "In Progress", created="2026-01-01",
                  followup_draft_at="2026-06-30", associated_people=["pd"]),
        ],
        persons={},
    )
    detect_candidates(attio, today=TODAY)
    assert attio.bulk_fetch_calls == [{"pd"}]


def test_reply_during_cooldown_resurfaces_verified_drafted_deal(identity_parsers):
    """The H1/M1 review finding: a synced interaction NEWER than the draft
    stamp must count as 'activity advanced past the draft' even on a verified
    deal — otherwise a reply landing during the 5-day cooldown could never
    re-surface the deal (the verified stamp predates the draft by
    construction, and ranking precedence would pin last_touch to it)."""
    attio = FakeAttio(
        deals=[_deal("d", "In Progress", created="2026-01-01",
                     last_verified_touch="2026-05-01",   # stamped at verify time
                     followup_draft_at="2026-06-29",     # drafted 2 days ago
                     associated_people=["p1"])],
        persons={"p1": _person("2026-06-30T12:00:00Z")},  # reply AFTER draft
    )
    out = detect_candidates(attio, today=TODAY).candidates
    # advanced=True → not hidden as drafted_recent; ranking recency stays the
    # verified stamp (strict tier precedence), so it surfaces as stale.
    assert len(out) == 1
    assert out[0].last_touch == date(2026, 5, 1)
    assert out[0].last_touch_source == TOUCH_SOURCE_VERIFIED


def test_no_reply_during_cooldown_keeps_drafted_deal_hidden(identity_parsers):
    """Counterpart guard: without newer activity the cooldown still hides."""
    attio = FakeAttio(
        deals=[_deal("d", "In Progress", created="2026-01-01",
                     last_verified_touch="2026-05-01",
                     followup_draft_at="2026-06-29",
                     associated_people=["p1"])],
        persons={"p1": _person("2026-04-01T12:00:00Z")},  # older than draft
    )
    result = detect_candidates(attio, today=TODAY)
    assert result.candidates == []
    assert result.drafted_skipped == 1


def test_detect_stale_verified_touch_surfaces_with_source(identity_parsers):
    attio = FakeAttio(
        deals=[_deal("d", "In Progress", created="2026-06-30",
                     last_verified_touch="2026-06-01")],
    )
    out = detect_candidates(attio, today=TODAY).candidates
    assert len(out) == 1
    assert out[0].last_touch == date(2026, 6, 1)
    assert out[0].last_touch_synthetic is False
    assert out[0].last_touch_source == TOUCH_SOURCE_VERIFIED


def test_detect_one_bulk_fetch_across_many_deals(identity_parsers):
    """The join is ONE bulk fetch per run over the union of people — never
    per-candidate."""
    attio = FakeAttio(
        deals=[
            _deal("d1", "In Progress", created="2026-01-01", associated_people=["p1"]),
            _deal("d2", "Lead", created="2026-01-01", associated_people=["p2", "p3"]),
        ],
        persons={},
    )
    detect_candidates(attio, today=TODAY)
    assert attio.bulk_fetch_calls == [{"p1", "p2", "p3"}]


def test_bulk_fetch_exception_degrades_loudly_and_falls_back(identity_parsers):
    """A dead person-fetch path must never kill the radar OR read clean: deals
    fall back to creation-date recency AND the run is marked degraded."""
    attio = FakeAttio(
        deals=[_deal("d", "In Progress", created="2026-06-01",
                     associated_people=["p1"])],
        persons={"p1": _person("2026-06-30T00:00:00Z")},
        bulk_fetch_exc=RuntimeError("attio down"),
    )
    result = detect_candidates(attio, today=TODAY)
    assert len(result.candidates) == 1  # surfaced on the created_at fallback
    assert result.candidates[0].last_touch_synthetic is True
    assert result.candidates[0].last_touch_source == TOUCH_SOURCE_CREATED_AT
    assert any("person-interaction join unavailable" in d for d in result.degraded)


def test_bulk_fetch_partial_failure_degrades(identity_parsers):
    attio = FakeAttio(
        deals=[_deal("d", "In Progress", created="2026-06-01",
                     associated_people=["p1", "p2"])],
        persons={"p1": _person("2026-06-30T00:00:00Z")},
        bulk_fetch_failed=1,
    )
    result = detect_candidates(attio, today=TODAY)
    assert any("person-interaction join incomplete" in d for d in result.degraded)
    # The resolved person still contributes — partial data is used, not dropped.
    assert result.candidates == []  # p1 interacted yesterday → fresh


def test_all_person_reads_absent_is_structural_degradation(identity_parsers):
    """ZERO people returned from a non-empty request is treated as structural
    breakage (associated people ARE person records — all-absent means the join
    is broken, not that the data is empty), per the silent-failure review. The
    deals still surface on the created_at fallback."""
    attio = FakeAttio(
        deals=[_deal("d", "In Progress", created="2026-06-01",
                     associated_people=["ghost"])],
        persons={},
    )
    result = detect_candidates(attio, today=TODAY)
    assert any("returned 0 of 1" in d for d in result.degraded)
    assert len(result.candidates) == 1
    assert result.candidates[0].last_touch_source == TOUCH_SOURCE_CREATED_AT


def test_single_deleted_person_among_many_is_not_degraded(identity_parsers):
    """One 404 among successful reads is a legitimately deleted person — the
    affected deal falls to the next tier without crying wolf."""
    attio = FakeAttio(
        deals=[
            _deal("d1", "In Progress", created="2026-06-01",
                  associated_people=["ghost"]),
            _deal("d2", "In Progress", created="2026-06-01",
                  associated_people=["p1"]),
        ],
        persons={"p1": _person("2026-06-30T00:00:00Z")},
    )
    result = detect_candidates(attio, today=TODAY)
    assert result.degraded == []


def test_state_gate_uses_real_recency_for_advanced_check(identity_parsers):
    """Real interaction AFTER a draft stamp = the account was acted on → the
    draft cooldown no longer hides it (v1 could never see this for deals)."""
    attio = FakeAttio(
        deals=[_deal("d", "In Progress", created="2026-01-01",
                     followup_draft_at="2026-06-01",
                     associated_people=["p1"])],
        persons={"p1": _person("2026-06-10T00:00:00Z")},  # advanced past draft
    )
    out = detect_candidates(attio, today=TODAY).candidates
    assert len(out) == 1  # silent since Jun 10 (21d > 10d threshold), not hidden
    assert out[0].last_touch == date(2026, 6, 10)


def test_entry_sources_labeled(identity_parsers):
    attio = FakeAttio(
        entries=[
            _entry("real", "Responded", last_contact="2026-06-20"),
            _entry("synth", "Call Booked", last_contact=None,
                   entry_created_at="2026-05-01"),
        ]
    )
    by_id = {c.record_id: c for c in detect_candidates(attio, today=TODAY).candidates}
    assert by_id["real"].last_touch_source == TOUCH_SOURCE_CONTACT_STAMP
    assert by_id["synth"].last_touch_source == TOUCH_SOURCE_CREATED_AT


def test_to_json_carries_last_touch_source(identity_parsers):
    attio = FakeAttio(
        deals=[_deal("d", "In Progress", created="2026-06-25",
                     associated_people=["p1"])],
        persons={"p1": _person("2026-03-20T00:00:00Z")},
    )
    rows = to_json(detect_candidates(attio, today=TODAY).candidates)
    assert rows[0]["last_touch_source"] == TOUCH_SOURCE_INTERACTION
    assert rows[0]["last_touch_synthetic"] is False


def test_digest_deal_approx_flag_only_on_synthetic(identity_parsers):
    attio = FakeAttio(
        deals=[
            _deal("dreal", "In Progress", created="2026-06-25", name="RealCo",
                  associated_people=["p1"]),
            _deal("dsynth", "In Progress", created="2026-06-01", name="SynthCo"),
        ],
        persons={"p1": _person("2026-03-20T00:00:00Z")},
    )
    result = detect_candidates(attio, today=TODAY)
    digest = render_digest(result.candidates, degraded=result.degraded)
    real_line = next(line for line in digest.splitlines() if "RealCo" in line)
    synth_line = next(line for line in digest.splitlines() if "SynthCo" in line)
    assert "approx" not in real_line
    assert "(approx — deal age)" in synth_line
    # Footer note appears only because a synthetic deal row exists.
    assert "silence is deal age" in digest


def test_digest_no_deal_age_footer_when_all_deals_real(identity_parsers):
    attio = FakeAttio(
        deals=[_deal("d", "In Progress", created="2026-06-25", name="RealCo",
                     associated_people=["p1"])],
        persons={"p1": _person("2026-03-20T00:00:00Z")},
    )
    result = detect_candidates(attio, today=TODAY)
    digest = render_digest(result.candidates, degraded=result.degraded)
    assert "silence is deal age" not in digest


def test_parse_deal_extracts_v2_fields():
    """Raw-shape guard for the two v2 extractions (live-verified shapes)."""
    record = {
        "id": {"record_id": "d1"},
        "created_at": "2026-02-11T18:56:15.015000000Z",
        "values": {
            "name": [{"value": "GRUPO BAFAR"}],
            "stage": [{"status": {"title": "In Progress"}}],
            "associated_people": [
                {"target_object": "people", "target_record_id": "p-1",
                 "attribute_type": "record-reference"},
                {"target_object": "people", "target_record_id": "p-2",
                 "attribute_type": "record-reference"},
                "junk-not-a-dict",
                {"no_target": True},
            ],
            "last_verified_touch": [{"value": "2026-03-20"}],
        },
    }
    deal = AttioClient.parse_deal(record)
    assert deal["associated_people"] == ["p-1", "p-2"]
    assert deal["last_verified_touch"] == "2026-03-20"
    # Absent attrs read as safe empties, never KeyError.
    bare = AttioClient.parse_deal({"id": {"record_id": "d2"}, "values": {}})
    assert bare["associated_people"] == []
    assert bare["last_verified_touch"] is None


# ── WAITING lane (awaiting_reply_* state) ────────────────────────────────


def _waiting_entry(record_id, *, since, stage="DM3 Sent", nudges=None, **extra):
    """An entry with an awaiting-reply stamp. Default stage is NON-warm on
    purpose — the WAITING pre-pass is state-derived and must fire even when
    the stage path would early-out."""
    return _entry(
        record_id, stage,
        awaiting_reply_since=since,
        awaiting_reply_nudge_count=nudges,
        awaiting_reply_thread_id="thr-1",
        awaiting_reply_note_id="note-1",
        **extra,
    )


def test_waiting_surfaces_past_threshold(identity_parsers):
    # Stamped 2026-06-20 → 11 calendar days by TODAY (7d threshold).
    attio = FakeAttio(entries=[_waiting_entry("w1", since="2026-06-20")])
    out = detect_candidates(attio, today=TODAY).candidates
    assert len(out) == 1
    c = out[0]
    assert c.reason is FollowupReason.AWAITING_REPLY
    assert c.lane is WarmLane.WAITING
    assert c.silent_days == 11
    assert c.last_touch_source == "awaiting_send"
    assert c.last_touch_synthetic is False
    assert c.channel_hint == "email"  # stamp implies a verified Gmail thread
    assert c.awaiting_reply_thread_id == "thr-1"
    assert c.awaiting_reply_note_id == "note-1"


def test_waiting_fresh_send_suppresses_even_stage_path(identity_parsers):
    # Stamp 3 days old (< 7d threshold) on a WARM stage whose contact stamp is
    # ancient: the fresh send means the account was just touched — surfacing
    # "gone quiet" off the older DM stamp would nag a just-emailed prospect.
    attio = FakeAttio(entries=[
        _waiting_entry("w1", since="2026-06-28", stage="Responded", last_contact="2026-05-01"),
    ])
    assert detect_candidates(attio, today=TODAY).candidates == []


def test_waiting_ttl_expired_counts_and_falls_to_stage_path(identity_parsers):
    # Stamp 90 days old (> 60d TTL): not a WAITING row, counted as expired;
    # the warm stage path still applies (real staleness is real).
    attio = FakeAttio(entries=[
        _waiting_entry("w1", since="2026-04-02", stage="Responded", last_contact="2026-06-20"),
    ])
    res = detect_candidates(attio, today=TODAY)
    assert res.waiting_expired == 1
    assert len(res.candidates) == 1
    assert res.candidates[0].reason is FollowupReason.RESPONDED_NO_NEXT_STEP


def test_waiting_exhausted_counts_and_carries_nudge_count(identity_parsers):
    # At the 2-nudge ceiling: no WAITING row, counted as exhausted. The stage
    # path may still surface the account — but the candidate must carry the
    # nudge count so the skill layer never auto-nudges it again.
    attio = FakeAttio(entries=[
        _waiting_entry("w1", since="2026-06-15", nudges=2, stage="Responded",
                       last_contact="2026-06-20"),
    ])
    res = detect_candidates(attio, today=TODAY)
    assert res.waiting_exhausted == 1
    assert len(res.candidates) == 1
    c = res.candidates[0]
    assert c.lane is not WarmLane.WAITING
    assert c.awaiting_reply_nudge_count == 2


def test_waiting_dedup_one_candidate_per_record(identity_parsers):
    # Warm stage + eligible stamp → exactly ONE candidate, as WAITING.
    attio = FakeAttio(entries=[
        _waiting_entry("w1", since="2026-06-20", stage="Responded", last_contact="2026-06-01"),
    ])
    out = detect_candidates(attio, today=TODAY).candidates
    assert len(out) == 1
    assert out[0].lane is WarmLane.WAITING


def test_partner_stage_beats_waiting_for_entries(identity_parsers):
    attio = FakeAttio(entries=[
        _waiting_entry("w1", since="2026-06-20", stage="Partner Intro", last_contact="2026-06-01"),
    ])
    out = detect_candidates(attio, today=TODAY).candidates
    assert len(out) == 1
    assert out[0].lane is WarmLane.PARTNER


def test_partner_referred_deal_beats_waiting(identity_parsers):
    attio = FakeAttio(deals=[
        _deal("d1", "In Progress", created="2026-05-01",
              awaiting_reply_since="2026-06-20", referred_by="gus@partner.com"),
    ])
    out = detect_candidates(attio, today=TODAY).candidates
    assert len(out) == 1
    assert out[0].lane is WarmLane.PARTNER


def test_waiting_deal_surfaces(identity_parsers):
    attio = FakeAttio(deals=[
        _deal("d1", "In Progress", created="2026-05-01", value=50_000,
              awaiting_reply_since="2026-06-20"),
    ])
    out = detect_candidates(attio, today=TODAY).candidates
    assert len(out) == 1
    c = out[0]
    assert c.lane is WarmLane.WAITING
    assert c.object == "deals"
    assert c.value_mult > 1.0  # deal value still weights urgency


def test_waiting_excluded_for_terminal_email_person(identity_parsers):
    # The entry record_id is a person id in a terminal email stage: no
    # WAITING row (never nudge a decline) — and no stage row here either
    # (non-warm stage), so the account surfaces nowhere.
    attio = FakeAttio(
        entries=[_waiting_entry("w1", since="2026-06-20")],
        terminal_email_ids={"w1"},
    )
    assert detect_candidates(attio, today=TODAY).candidates == []


def test_waiting_deal_excluded_when_any_associated_person_terminal(identity_parsers):
    attio = FakeAttio(
        deals=[_deal("d1", "In Progress", created="2026-05-01",
                     awaiting_reply_since="2026-06-20",
                     associated_people=["p-ok", "p-no"])],
        terminal_email_ids={"p-no"},
    )
    out = detect_candidates(attio, today=TODAY).candidates
    # Not WAITING; the deal still surfaces via its stage path (In Progress,
    # created 2026-05-01 → stale) — the terminal check only guards the lane.
    assert all(c.lane is not WarmLane.WAITING for c in out)


def test_waiting_fails_closed_when_terminal_fetch_breaks(identity_parsers):
    attio = FakeAttio(
        entries=[_waiting_entry("w1", since="2026-06-20")],
        terminal_fetch_exc=RuntimeError("attio down"),
    )
    res = detect_candidates(attio, today=TODAY)
    assert all(c.lane is not WarmLane.WAITING for c in res.candidates)
    assert any("WAITING lane skipped" in d for d in res.degraded)


def test_waiting_respects_mute_snooze_and_cooldown(identity_parsers):
    muted = _waiting_entry("w1", since="2026-06-20", followup_muted=True)
    snoozed = _waiting_entry("w2", since="2026-06-20", followup_snooze_until="2026-07-10")
    drafted = _waiting_entry("w3", since="2026-06-20", followup_draft_at="2026-06-29")
    res = detect_candidates(FakeAttio(entries=[muted, snoozed, drafted]), today=TODAY)
    assert res.candidates == []
    assert res.parked["muted"] == 1
    assert res.parked["snoozed"] == 1
    assert res.drafted_skipped == 1


def test_waiting_outbound_advance_does_not_redraft(identity_parsers):
    # Regression (advance-gate fix): a WAITING deal drafted 2 days ago whose
    # person-interaction join shows NEWER activity (the operator's own outbound sync)
    # must STAY in cooldown — the union interaction attr counts outbound, so
    # "activity advanced past the draft" would churn a re-draft every run.
    attio = FakeAttio(
        deals=[_deal("d1", "In Progress", created="2026-05-01",
                     awaiting_reply_since="2026-06-20",
                     followup_draft_at="2026-06-29",
                     associated_people=["p-1"])],
        persons={"p-1": {"values": {"last_interaction": [
            {"interacted_at": "2026-06-30T10:00:00Z"}]}}},
    )
    res = detect_candidates(attio, today=TODAY)
    assert res.candidates == []
    assert res.drafted_skipped == 1


def test_waiting_future_stamp_fails_closed(identity_parsers):
    # Beyond 1-day skew → invalid stamp → no WAITING row (and the non-warm
    # stage keeps it off the stage path too).
    attio = FakeAttio(entries=[_waiting_entry("w1", since="2026-07-15")])
    assert detect_candidates(attio, today=TODAY).candidates == []


def test_partition_and_render_with_waiting(identity_parsers):
    attio = FakeAttio(entries=[
        _waiting_entry("w1", since="2026-06-20"),
        _entry("owed1", "Call Booked", last_contact="2026-06-20", email_address="a@x.com"),
        _entry("li1", "Responded", last_contact="2026-06-20"),  # linkedin_only
    ])
    out = detect_candidates(attio, today=TODAY).candidates
    lanes = partition_lanes(out)
    assert [c.record_id for c in lanes["waiting"]] == ["w1"]
    assert [c.record_id for c in lanes["owed"]] == ["owed1"]
    assert [c.record_id for c in lanes["linkedin_warm"]] == ["li1"]
    # WAITING is email-semantic: never pulled into linkedin_warm.
    assert all(c.lane is not WarmLane.WAITING for c in lanes["linkedin_warm"])
    md = render_digest(out)
    assert "Waiting on them — you sent, no reply" in md
    assert "waiting" in md.split("\n")[2]  # split-count line names the lane


def test_waiting_preview_collapses(identity_parsers):
    entries = [
        _waiting_entry(f"w{i}", since="2026-06-20") for i in range(7)
    ]
    out = detect_candidates(FakeAttio(entries=entries), today=TODAY).candidates
    collapsed = render_digest(out)
    assert "…+2 more waiting" in collapsed
    full = render_digest(out, full=True)
    assert "more waiting" not in full


def test_waiting_counters_render_in_footer_and_empty_state(identity_parsers):
    md = render_digest([], waiting_exhausted=2, waiting_expired=1)
    assert "nada urgente hoy" in md
    assert "nudge ceiling" in md
    assert "Radar limpio" not in md  # exhausted/expired pool ≠ clean
    with_rows = render_digest(
        detect_candidates(
            FakeAttio(entries=[_waiting_entry("w1", since="2026-06-20")]), today=TODAY
        ).candidates,
        waiting_exhausted=1,
    )
    assert "handed to" in with_rows


def test_trim_caps_waiting_slots(identity_parsers):
    # 4 waiting (ancient stamps → huge urgency) + 3 nudge-stage rows, limit 5:
    # the trim admits at most 2 WAITING and backfills with the nudge rows.
    entries = [
        _waiting_entry(f"w{i}", since="2026-05-10") for i in range(4)
    ] + [
        _entry(f"n{i}", "Responded", last_contact="2026-06-20", email_address=f"n{i}@x.com")
        for i in range(3)
    ]
    summary = run_followup_radar(FakeAttio(entries=entries), today=TODAY, limit=5)
    assert summary["waiting"] == 2
    assert summary["surfaced"] == 5
    assert summary["nudge"] == 3


def test_waiting_fields_in_json(identity_parsers):
    out = detect_candidates(
        FakeAttio(entries=[_waiting_entry("w1", since="2026-06-20", nudges=1)]),
        today=TODAY,
    ).candidates
    row = to_json(out)[0]
    assert row["awaiting_reply_since"] == "2026-06-20"
    assert row["awaiting_reply_nudge_count"] == 1
    assert row["awaiting_reply_thread_id"] == "thr-1"
    assert row["awaiting_reply_note_id"] == "note-1"
    assert row["last_touch_source"] == "awaiting_send"


def test_waiting_due_callback_hard_surfaces_as_owed(identity_parsers):
    # A due callback on a stamped account outranks WAITING (heat 7 vs 4) —
    # and fires even from a non-warm stage, because the WAITING pre-pass
    # routes through the same state gate.
    attio = FakeAttio(entries=[
        _waiting_entry("w1", since="2026-06-20", followup_callback_date="2026-06-30"),
    ])
    out = detect_candidates(attio, today=TODAY).candidates
    assert len(out) == 1
    assert out[0].reason is FollowupReason.CALLBACK_DUE
    assert out[0].lane is WarmLane.OWED
    assert out[0].last_touch_source == "awaiting_send"


def test_callback_candidates_carry_awaiting_state(identity_parsers):
    # Review regression: the "carried on EVERY candidate" invariant must hold
    # on the callback path too — a due callback mid-waiting-cycle must show
    # the skill layer the real nudge count and note id, or the ceiling and
    # canonical-note dedup break exactly there.
    entry = _waiting_entry(
        "w1", since="2026-06-20", nudges=1, followup_callback_date="2026-06-30",
    )
    deal = _deal(
        "d1", "In Progress", created="2026-05-01",
        awaiting_reply_since="2026-06-20", awaiting_reply_nudge_count=1,
        awaiting_reply_note_id="note-d", awaiting_reply_thread_id="thr-d",
        followup_callback_date="2026-06-30",
    )
    out = detect_candidates(FakeAttio(entries=[entry], deals=[deal]), today=TODAY).candidates
    assert len(out) == 2
    for c in out:
        assert c.reason is FollowupReason.CALLBACK_DUE
        assert c.awaiting_reply_nudge_count == 1
        assert c.awaiting_reply_since is not None
        assert c.awaiting_reply_note_id is not None


def test_exhausted_stage_path_callback_still_carries_count(identity_parsers):
    # Exhausted stamp (ceiling hit) + warm stage + due callback: the pre-pass
    # declines (exhausted), the STAGE path emits the callback — and the count
    # must survive that hand-off.
    entry = _waiting_entry(
        "w1", since="2026-06-15", nudges=2, stage="Responded",
        last_contact="2026-06-01", followup_callback_date="2026-06-30",
    )
    res = detect_candidates(FakeAttio(entries=[entry]), today=TODAY)
    assert res.waiting_exhausted == 1
    assert len(res.candidates) == 1
    c = res.candidates[0]
    assert c.reason is FollowupReason.CALLBACK_DUE
    assert c.awaiting_reply_nudge_count == 2


def test_malformed_nudge_count_fails_closed_as_exhausted(identity_parsers):
    # Review regression: corruption must never reset the anti-nag ceiling —
    # an unparseable count reads as AT the ceiling, not as 0.
    attio = FakeAttio(entries=[
        _waiting_entry("w1", since="2026-06-20", nudges="garbage"),
    ])
    res = detect_candidates(attio, today=TODAY)
    assert res.candidates == []  # not eligible — no fresh nudges
    assert res.waiting_exhausted == 1


def test_trim_reports_capped_waiting(identity_parsers):
    entries = [
        _waiting_entry(f"w{i}", since="2026-05-10") for i in range(4)
    ] + [
        _entry(f"n{i}", "Responded", last_contact="2026-06-20", email_address=f"n{i}@x.com")
        for i in range(3)
    ]
    summary = run_followup_radar(FakeAttio(entries=entries), today=TODAY, limit=5)
    assert summary["waiting_capped"] == 2
    assert "displaced by the 2-slot draft cap" in summary["digest"]


def test_degraded_empty_state_still_surfaces_waiting_counters(identity_parsers):
    # Review regression: a degraded run with zero candidates must not swallow
    # the exhausted/expired counts — they are operator work regardless.
    md = render_digest(
        [], degraded=["exclusion incomplete"], waiting_exhausted=3, waiting_expired=1,
    )
    assert "detection was degraded" in md
    assert "3 waiting at the nudge ceiling" in md
    assert "1 waiting stamps expired" in md


def test_drift_alarm_suppressed_when_waiting_consumed_warm_entries(identity_parsers):
    # All warm-stage entries eaten by the WAITING pre-pass → warm titles
    # provably still parse → no false "stage-title drift" banner.
    attio = FakeAttio(entries=[
        _waiting_entry("w1", since="2026-06-20", stage="Responded", last_contact="2026-06-01"),
    ])
    res = detect_candidates(attio, today=TODAY)
    assert len(res.candidates) == 1
    assert not any("stage-title drift" in d for d in res.degraded)
    # …but a genuinely drifted list (no warm reason anywhere) still fires.
    drifted = FakeAttio(entries=[_entry("x1", "Some Renamed Stage")])
    res2 = detect_candidates(drifted, today=TODAY)
    assert any("stage-title drift" in d for d in res2.degraded)


def test_terminal_fetch_is_lazy(identity_parsers):
    # No awaiting stamps anywhere → the 3 terminal-stage queries never fire.
    calls = []

    class CountingAttio(FakeAttio):
        def search_people(self, filter_=None, limit=0):
            stage = (filter_ or {}).get("email_campaign_stage")
            if stage in ("email_responded", "email_not_interested", "unsubscribed"):
                calls.append(stage)
            return super().search_people(filter_=filter_, limit=limit)

    attio = CountingAttio(entries=[_entry("r1", "Responded", last_contact="2026-06-25")])
    detect_candidates(attio, today=TODAY)
    assert calls == []
    # With a stamp present, the fetch fires exactly once (memoized).
    attio2 = CountingAttio(entries=[
        _waiting_entry("w1", since="2026-06-20"),
        _waiting_entry("w2", since="2026-06-20"),
    ])
    detect_candidates(attio2, today=TODAY)
    assert len(calls) == 3  # one query per terminal stage, once per run


# ── CRM-agnostic escape-hatch + disabled-by-default path ────────────────────
# The command trunk (run_followup_radar) takes a CRMProvider and derives the
# raw AttioClient via _attio_inner_client. These tests pin the fork adaptation:
# a schemaless install stays green (fails-closed-clean), and a provider with no
# Attio inner handle raises a clear error rather than silently mis-routing.


def test_run_followup_radar_schemaless_is_clean_noop(identity_parsers):
    """Disabled-by-default proof: with the radar attributes absent, the
    command trunk returns a clean, degraded summary — never raises — so a
    fresh (unprovisioned) install's daily Phase C is inert, not broken."""
    class SchemalessAttio(FakeAttio):
        # Only the base pipeline `stage` slug exists — no followup_* attrs.
        def get_object_attributes(self, slug):
            return [{"api_slug": "stage"}]

        def get_list_attributes(self, list_id):
            return [{"api_slug": "stage"}]

    attio = SchemalessAttio(entries=[_entry("r1", "Responded", last_contact="2026-06-25")])
    summary = run_followup_radar(attio, today=TODAY, list_id="L-1")
    # No crash; the missing-state-schema degradation is SURFACED, never a
    # false "Radar limpio".
    assert isinstance(summary, dict)
    assert summary["degraded"], "schemaless run must surface a degraded reason"
    assert any("state schema" in d for d in summary["degraded"])


def test_run_followup_radar_rejects_provider_without_inner_client(identity_parsers):
    """A non-Attio CRMProvider exposes no `inner_client`; the escape hatch must
    raise a clear TypeError rather than silently mis-routing the Attio-only
    radar internals."""
    class NoInnerProvider:
        pass  # no `inner_client` attribute

    with pytest.raises(TypeError, match="inner_client"):
        run_followup_radar(NoInnerProvider(), today=TODAY)


# ── Gmail conversation-ledger sweep (PR-214, opt-in) ────────────────────────
# The optional email side: OFF by default, degrades to a clean skip without a
# Gmail token, and NEVER drops a candidate (annotation only). Uses branch-10's
# clients.gmail.GmailClient via an injected factory so no real inbox is touched.

from datetime import date as _date  # noqa: E402


class _FakeGmail:
    """Minimal GmailClient stand-in: returns a canned inbound hit for the
    addresses in `hits`, empty otherwise. Records the (email, after) queries."""

    def __init__(self, hits=(), raise_for=None):
        self._hits = set(hits)
        self._raise_for = set(raise_for or ())
        self.queries: list[tuple[str, _date]] = []

    def search_inbound(self, from_address, after):
        self.queries.append((from_address, after))
        if from_address in self._raise_for:
            raise RuntimeError("gmail 500")
        return [{"message_id": "m1", "thread_id": "t1"}] if from_address in self._hits else []


def _waiting_email_candidate(rid="w1", email="prospect@acme.test"):
    from workflows.followup_radar import FollowupCandidate

    return FollowupCandidate(
        object="linkedin_outreach",
        record_id=rid,
        entry_id=f"ent-{rid}",
        reason=FollowupReason.AWAITING_REPLY,
        lane=WarmLane.WAITING,
        last_touch=date(2026, 6, 10),
        silent_days=21,
        heat=4,
        value_mult=1.0,
        urgency=4.0,
        email_address=email,
        channel_hint="email",
    )


def test_gmail_sweep_marks_email_reply_seen():
    from workflows.followup_radar import sweep_gmail_conversations

    cand = _waiting_email_candidate(email="hot@acme.test")
    gmail = _FakeGmail(hits={"hot@acme.test"})
    degraded = sweep_gmail_conversations(
        [cand], today=TODAY, lookback_days=90, client_factory=lambda: gmail,
    )
    assert degraded == []
    assert cand.email_reply_seen is True
    assert any("email reply seen" in n for n in cand.notes)
    # Never searches before the CRM last_touch (a reply older than last_touch
    # isn't news) — the `after` bound is >= last_touch.
    assert gmail.queries[0][1] >= cand.last_touch


def test_gmail_sweep_no_hit_leaves_candidate_untouched():
    from workflows.followup_radar import sweep_gmail_conversations

    cand = _waiting_email_candidate()
    degraded = sweep_gmail_conversations(
        [cand], today=TODAY, lookback_days=90, client_factory=lambda: _FakeGmail(),
    )
    assert degraded == []
    assert cand.email_reply_seen is False


def test_gmail_sweep_degrades_to_skip_without_credentials():
    """The disabled/degraded email path: no Gmail token → a surfaced degraded
    reason and ZERO candidate mutation. The radar still runs on CRM signals."""
    from clients.gmail import GmailCredentialsMissing
    from workflows.followup_radar import sweep_gmail_conversations

    cand = _waiting_email_candidate(email="hot@acme.test")

    def _missing():
        raise GmailCredentialsMissing("no token")

    degraded = sweep_gmail_conversations(
        [cand], today=TODAY, lookback_days=90, client_factory=_missing,
    )
    assert len(degraded) == 1
    assert "no Gmail credentials" in degraded[0]
    assert cand.email_reply_seen is False  # untouched


def test_gmail_sweep_collects_per_candidate_errors_without_raising():
    from workflows.followup_radar import sweep_gmail_conversations

    good = _waiting_email_candidate("g1", email="ok@acme.test")
    bad = _waiting_email_candidate("b1", email="boom@acme.test")
    gmail = _FakeGmail(hits={"ok@acme.test"}, raise_for={"boom@acme.test"})
    degraded = sweep_gmail_conversations(
        [good, bad], today=TODAY, lookback_days=90, client_factory=lambda: gmail,
    )
    assert good.email_reply_seen is True
    assert bad.email_reply_seen is False
    assert len(degraded) == 1 and "b1" in degraded[0]


def test_gmail_sweep_skips_candidates_without_email():
    from workflows.followup_radar import sweep_gmail_conversations

    cand = _waiting_email_candidate(email="")
    gmail = _FakeGmail(hits={"x"})
    degraded = sweep_gmail_conversations(
        [cand], today=TODAY, lookback_days=90, client_factory=lambda: gmail,
    )
    assert degraded == []
    assert gmail.queries == []  # no lookup for an emailless candidate


def test_run_followup_radar_gmail_sweep_off_by_default(identity_parsers):
    """Feature OFF by default: run_followup_radar never invokes the Gmail
    factory unless explicitly enabled — a schemaless/credential-less install
    stays green."""
    attio = FakeAttio(entries=[
        _entry("r1", "Responded", last_contact="2026-06-25", email_address="a@acme.test"),
    ])
    called = {"n": 0}

    def _factory():
        called["n"] += 1
        return _FakeGmail()

    summary = run_followup_radar(attio, today=TODAY, gmail_client_factory=_factory)
    assert called["n"] == 0  # sweep did NOT run (default off)
    assert all(not c.get("email_reply_seen") for c in summary["candidates"])


def test_run_followup_radar_gmail_sweep_on_annotates_and_surfaces_degraded(identity_parsers):
    from clients.gmail import GmailCredentialsMissing

    attio = FakeAttio(entries=[
        _entry("r1", "Responded", last_contact="2026-06-25", email_address="a@acme.test"),
    ])

    def _missing():
        raise GmailCredentialsMissing("no token")

    summary = run_followup_radar(
        attio, today=TODAY, gmail_sweep=True, gmail_client_factory=_missing,
    )
    # Enabled-but-credential-less: the run still succeeds and the degradation
    # is surfaced (never a false clean).
    assert any("no Gmail credentials" in d for d in summary["degraded"])


@pytest.mark.parametrize("exc", [RuntimeError("build failed"), ValueError("malformed token")])
def test_gmail_sweep_construction_failure_degrades_not_blackout(exc):
    """IMPORTANT-1: from_credentials can raise more than GmailCredentialsMissing
    (ValueError on a malformed token, RefreshError, transport/build errors). ANY
    such failure must degrade to a surfaced reason — never propagate out and let
    the Phase C firewall discard the whole CRM digest."""
    from workflows.followup_radar import sweep_gmail_conversations

    cand = _waiting_email_candidate(email="hot@acme.test")

    def _boom():
        raise exc

    degraded = sweep_gmail_conversations(
        [cand], today=TODAY, lookback_days=90, client_factory=_boom,
    )
    assert len(degraded) == 1
    assert "client init failed" in degraded[0]
    assert type(exc).__name__ in degraded[0]
    # NEVER a credentials-missing wording for a non-credentials error — the two
    # degraded reasons stay distinct so the operator can tell them apart.
    assert "no Gmail credentials" not in degraded[0]
    assert cand.email_reply_seen is False  # untouched


def test_run_followup_radar_sweep_init_failure_keeps_crm_digest(identity_parsers):
    """IMPORTANT-1 end-to-end: a sweep factory that raises a NON-credentials
    error degrades the run AND the already-computed CRM digest still renders —
    the exact 'radar runs on CRM signals alone' contract."""
    attio = FakeAttio(entries=[
        _entry("r1", "Responded", last_contact="2026-06-25", email_address="a@acme.test"),
    ])

    def _boom():
        raise RuntimeError("gmail build exploded")

    summary = run_followup_radar(
        attio, today=TODAY, gmail_sweep=True, gmail_client_factory=_boom,
    )
    # Degraded reason surfaced …
    assert any("client init failed" in d for d in summary["degraded"])
    # … AND the CRM digest is NOT lost: the surfaced candidate still renders.
    assert summary["surfaced"] == 1
    assert len(summary["candidates"]) == 1
    assert "**r1**" in summary["digest"]


def test_reply_seen_marker_renders_over_state_gate_note():
    """IMPORTANT-2: email_reply_seen renders as its OWN marker in the digest row,
    independent of notes[0] ordering. A WAITING/drafted-stale candidate whose
    notes[0] is a state-gate line must STILL show the reply-seen reconciliation
    flag — otherwise it reads 'you went quiet, N days silent' with no counter."""
    cand = _waiting_email_candidate(email="hot@acme.test")
    # notes[0] is the WAITING/state-gate note that already owns the ⚠ slot.
    cand.notes.append("nudge 1/2 sent 5d ago")
    cand.email_reply_seen = True
    md = render_digest([cand], full=True)
    # The dedicated reply-seen marker renders …
    assert "↩ reply seen" in md
    # … AND does NOT displace the pre-existing state-gate note (both present).
    assert "nudge 1/2 sent 5d ago" in md


def test_reply_seen_marker_absent_when_not_seen():
    """The marker is only rendered when email_reply_seen is True — a normal warm
    candidate never gets a spurious ↩ flag."""
    cand = _waiting_email_candidate(email="hot@acme.test")
    assert cand.email_reply_seen is False
    md = render_digest([cand], full=True)
    assert "↩ reply seen" not in md


def test_run_followup_radar_enrich_failure_degrades_and_renders(identity_parsers):
    """MINOR-1: a transient Attio error during NAME resolution must not sink a
    fully-successful detection. The run degrades (names fall back to record_id
    via _who) and the digest still renders."""
    attio = FakeAttio(
        entries=[_entry("r1", "Responded", last_contact="2026-06-25", email_address="a@acme.test")],
        bulk_fetch_exc=RuntimeError("attio 503 during enrichment"),
    )
    summary = run_followup_radar(attio, today=TODAY)
    assert any("Name enrichment failed" in d for d in summary["degraded"])
    # Detection succeeded and the digest still renders the row (fallback name).
    assert summary["surfaced"] == 1
    assert "**r1**" in summary["digest"]
