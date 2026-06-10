"""PR-36 tests: scripts/gtm_attio_inventory — lane metrics, phasing heuristic,
``icp_phasing`` escalation, CLI behavior."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from clients.attio import AttioClient as _RealAttioClient


def _patched_attio_class(attio_cm: MagicMock) -> MagicMock:
    """Build a MagicMock that mimics the AttioClient class: calling it
    returns ``attio_cm`` (the instance mock), but ``parse_entry`` /
    ``parse_deal`` static methods route to the REAL implementation so
    `compute_lane_metrics` etc. can still parse fixture entries."""
    cls = MagicMock()
    cls.return_value = attio_cm
    cls.parse_entry = _RealAttioClient.parse_entry
    cls.parse_deal = _RealAttioClient.parse_deal
    return cls

from scripts.gtm_attio_inventory import (
    DECISION_KEY,
    DM3_COMPLETED_MIN_FOR_RATE,
    ESCALATION_DEADLINE_DAYS,
    EXIT_ATTIO_FAILURE,
    EXIT_CONFIG_MISSING,
    EXIT_HEURISTIC_ABSTAINED,
    EXIT_OK,
    EXPECTED_MIN_LIST_ENTRIES_ENV,
    ICP1_LANES,
    ICP2_LANES,
    ICP2_LOW_RATE_THRESHOLD,
    LANE_BUCKETS,
    UNKNOWN_LANE_BUCKET,
    WEEK_ANCHOR_TZ,
    _aggregate,
    _idempotency_key_for,
    _is_dm3_completed,
    _is_positive,
    _lane_bucket,
    assess_data_quality,
    build_inventory,
    compute_lane_metrics,
    evaluate_phasing_recommendation,
    main,
    open_icp_phasing_escalation,
    response_distribution,
    stage_distribution,
)

# ────────────────────────────────────────────────────────────────────────
# Test fixtures: synthetic entries that mimic parse_entry output shape
# ────────────────────────────────────────────────────────────────────────


def _make_entry(
    *,
    scoring_lane: str | None = "legacy",
    dm3_sent_at: str | None = None,
    response_classification: str | None = None,
    stage: str | None = "Prospect",
) -> dict:
    """Build a raw Attio entry envelope that ``parse_entry`` can flatten."""

    def _v(value: object | None) -> list[dict]:
        return [{"value": value}] if value is not None else []

    return {
        "entry_id": "e-x",
        "parent_record_id": "p-x",
        "created_at": "2026-05-01T00:00:00Z",
        "entry_values": {
            "scoring_lane": _v(scoring_lane),
            "dm3_sent_at": _v(dm3_sent_at),
            "response_classification": _v(response_classification),
            "stage": (
                [{"status": {"title": stage}}] if stage is not None else []
            ),
        },
    }


# ────────────────────────────────────────────────────────────────────────
# Lane bucket + helpers
# ────────────────────────────────────────────────────────────────────────


class TestLaneBucket:
    @pytest.mark.parametrize(
        "lane,expected",
        [
            ("enterprise_mode", "icp1_enterprise"),
            # target_company_mode is ICP 2 per workflows.weekly_prospect.py:349
            # (the production ICP-2 detection rule). See TestLaneTaxonomyConsistency.
            ("target_company_mode", "icp2_target"),
            ("legacy", "icp2_legacy"),
            (None, UNKNOWN_LANE_BUCKET),
            ("garbage", UNKNOWN_LANE_BUCKET),
        ],
    )
    def test_known_and_unknown_lanes(self, lane, expected):
        assert _lane_bucket({"scoring_lane": lane}) == expected

    def test_buckets_constant_in_sync_with_lanes(self):
        """If a new scoring_lane is added in quality_gate, this constant
        needs an update — the test pins the contract."""
        assert set(LANE_BUCKETS) == {"enterprise_mode", "target_company_mode", "legacy"}

    def test_icp_lane_aggregates_pin_to_lane_buckets(self):
        """ICP1_LANES + ICP2_LANES partition LANE_BUCKETS.values() exactly —
        no lane silently belongs to neither (or both)."""
        all_lanes = set(LANE_BUCKETS.values())
        assert all_lanes == (ICP1_LANES | ICP2_LANES)
        assert frozenset() == (ICP1_LANES & ICP2_LANES)


class TestLaneTaxonomyConsistency:
    """Lane semantics must match `workflows.weekly_prospect.enforce_icp_lane_geo`
    — the production ICP-2 detection rule. If that rule changes, this test
    forces the update here so the inventory script doesn't drift silently."""

    def test_target_company_mode_is_icp2_per_weekly_prospect(self):
        # The contract: scoring_lane == "target_company_mode" is treated
        # as ICP 2 by enforce_icp_lane_geo. Pin both producers to that
        # semantic. If you change one, change the other.
        from workflows import weekly_prospect

        source = inspect_source(weekly_prospect.enforce_icp_lane_geo)
        assert 'scoring_lane == "target_company_mode"' in source, (
            "weekly_prospect.enforce_icp_lane_geo no longer pins "
            "target_company_mode as ICP 2 — update LANE_BUCKETS here."
        )
        assert LANE_BUCKETS["target_company_mode"] in ICP2_LANES, (
            "LANE_BUCKETS mapping drifted from weekly_prospect's ICP-2 rule."
        )


def inspect_source(fn) -> str:
    import inspect as _inspect
    return _inspect.getsource(fn)


class TestPredicates:
    def test_dm3_completed_when_timestamp_set(self):
        assert _is_dm3_completed({"dm3_sent_at": "2026-05-10T00:00:00Z"})

    def test_dm3_not_completed_when_none(self):
        assert not _is_dm3_completed({"dm3_sent_at": None})

    def test_dm3_not_completed_when_empty_string(self):
        assert not _is_dm3_completed({"dm3_sent_at": ""})

    @pytest.mark.parametrize("classification,expected", [
        ("positive", True),
        ("interested", True),
        ("negative", False),
        ("defensive", False),
        (None, False),
    ])
    def test_positive_classification(self, classification, expected):
        assert _is_positive({"response_classification": classification}) is expected


# ────────────────────────────────────────────────────────────────────────
# Aggregation
# ────────────────────────────────────────────────────────────────────────


class TestComputeLaneMetrics:
    def test_empty_input(self):
        assert compute_lane_metrics([]) == {}

    def test_simple_split(self):
        entries = [
            _make_entry(scoring_lane="legacy"),
            _make_entry(scoring_lane="legacy", dm3_sent_at="x", response_classification="positive"),
            _make_entry(scoring_lane="enterprise_mode", dm3_sent_at="x"),
            _make_entry(scoring_lane="target_company_mode"),
        ]
        m = compute_lane_metrics(entries)
        assert m["icp2_legacy"]["total"] == 2
        assert m["icp2_legacy"]["dm3_completed"] == 1
        assert m["icp2_legacy"]["positive"] == 1
        assert m["icp1_enterprise"]["total"] == 1
        assert m["icp1_enterprise"]["dm3_completed"] == 1
        assert m["icp2_target"]["total"] == 1

    def test_positive_rate_none_below_min(self):
        """When dm3_completed < DM3_COMPLETED_MIN_FOR_RATE the rate must
        be reported as None — small-N noise must not drive a decision."""
        entries = [
            _make_entry(scoring_lane="legacy", dm3_sent_at="x", response_classification="positive"),
            _make_entry(scoring_lane="legacy", dm3_sent_at="x"),
        ]
        m = compute_lane_metrics(entries)
        assert m["icp2_legacy"]["positive_rate"] is None

    def test_positive_rate_when_above_min(self):
        n = DM3_COMPLETED_MIN_FOR_RATE
        # n DM3-completed, 5 positives.
        entries = [
            _make_entry(scoring_lane="legacy", dm3_sent_at="x", response_classification="positive")
            for _ in range(5)
        ] + [
            _make_entry(scoring_lane="legacy", dm3_sent_at="x") for _ in range(n - 5)
        ]
        m = compute_lane_metrics(entries)
        assert m["icp2_legacy"]["positive_rate"] == pytest.approx(5 / n)

    def test_unknown_lane_bucket_used_for_garbage(self):
        entries = [_make_entry(scoring_lane="weird")]
        m = compute_lane_metrics(entries)
        assert UNKNOWN_LANE_BUCKET in m
        assert m[UNKNOWN_LANE_BUCKET]["total"] == 1


class TestDistributions:
    def test_stage_distribution_counts(self):
        entries = [
            _make_entry(stage="Prospect"),
            _make_entry(stage="Prospect"),
            _make_entry(stage="DM1_SENT"),
            _make_entry(stage=None),
        ]
        d = stage_distribution(entries)
        assert d["Prospect"] == 2
        assert d["DM1_SENT"] == 1
        assert d["unknown"] == 1

    def test_response_distribution_groups_unclassified(self):
        entries = [
            _make_entry(response_classification="positive"),
            _make_entry(response_classification="negative"),
            _make_entry(response_classification=None),
            _make_entry(response_classification=None),
        ]
        d = response_distribution(entries)
        assert d["positive"] == 1
        assert d["negative"] == 1
        assert d["unclassified"] == 2


# ────────────────────────────────────────────────────────────────────────
# Heuristic
# ────────────────────────────────────────────────────────────────────────


class TestEvaluatePhasingRecommendation:
    def test_below_evidence_threshold_does_not_escalate(self):
        lane_metrics = {
            "icp2_legacy": {
                "total": 5, "dm3_completed": 5, "positive": 5, "positive_rate": None,
            },
        }
        out = evaluate_phasing_recommendation(lane_metrics)
        assert out["escalate"] is False
        assert out["recommended"] == "keep_both"
        assert out["triggers"] == []

    def test_low_icp2_rate_escalates(self):
        # Below 5% positive rate on >=30 N → escalate.
        n = DM3_COMPLETED_MIN_FOR_RATE
        lane_metrics = {
            "icp2_legacy": {
                "total": n, "dm3_completed": n, "positive": 0,
                "positive_rate": 0.0,
            },
        }
        out = evaluate_phasing_recommendation(lane_metrics)
        assert out["escalate"] is True
        assert out["recommended"] == "phase_out_icp2"
        assert any("ICP2_total.positive_rate" in t for t in out["triggers"])

    def test_high_icp1_vs_icp2_ratio_escalates(self):
        n = DM3_COMPLETED_MIN_FOR_RATE
        lane_metrics = {
            "icp1_enterprise": {
                "total": n, "dm3_completed": n, "positive": 10,
                "positive_rate": 10 / n,
            },
            "icp2_target": {
                "total": 0, "dm3_completed": 0, "positive": 0, "positive_rate": None,
            },
            "icp2_legacy": {
                "total": n, "dm3_completed": n, "positive": 2,
                "positive_rate": 2 / n,
            },
        }
        out = evaluate_phasing_recommendation(lane_metrics)
        # 10/30 ≈ 0.333; 2/30 ≈ 0.067; ratio ≈ 5x → trigger fires
        assert out["escalate"] is True
        assert out["recommended"] == "phase_out_icp2"
        assert any("icp1_rate/icp2_rate" in t for t in out["triggers"])

    def test_healthy_rates_no_escalation(self):
        n = DM3_COMPLETED_MIN_FOR_RATE
        lane_metrics = {
            "icp1_enterprise": {
                "total": n, "dm3_completed": n, "positive": 4,
                "positive_rate": 4 / n,
            },
            "icp2_legacy": {
                "total": n, "dm3_completed": n, "positive": 3,
                "positive_rate": 3 / n,
            },
        }
        out = evaluate_phasing_recommendation(lane_metrics)
        # Both above 5%, ratio ~1.3x → no escalation.
        assert out["escalate"] is False
        assert out["recommended"] == "keep_both"
        assert out["triggers"] == []

    def test_ratio_only_fires_when_icp2_rate_positive(self):
        """If ICP 2 rate is exactly 0, the ratio rule is skipped (zero-divide
        avoided) but the low-rate rule still fires below threshold."""
        n = DM3_COMPLETED_MIN_FOR_RATE
        lane_metrics = {
            "icp1_enterprise": {
                "total": n, "dm3_completed": n, "positive": 15,
                "positive_rate": 15 / n,
            },
            "icp2_legacy": {
                "total": n, "dm3_completed": n, "positive": 0,
                "positive_rate": 0.0,
            },
        }
        out = evaluate_phasing_recommendation(lane_metrics)
        assert out["escalate"] is True
        # Low-rate trigger present; ratio trigger NOT (would have been divide-by-zero)
        assert any("ICP2_total.positive_rate" in t for t in out["triggers"])
        assert not any("icp1_rate/icp2_rate" in t for t in out["triggers"])


# ────────────────────────────────────────────────────────────────────────
# Idempotency key
# ────────────────────────────────────────────────────────────────────────


class TestIdempotencyKey:
    @pytest.mark.parametrize("today,expected_monday", [
        (date(2026, 5, 18), "2026-05-18"),  # Monday → itself
        (date(2026, 5, 21), "2026-05-18"),  # Thursday → previous Monday
        (date(2026, 5, 24), "2026-05-18"),  # Sunday → same-week Monday
        (date(2026, 5, 25), "2026-05-25"),  # Next Monday
    ])
    def test_pinned_to_iso_monday(self, today, expected_monday):
        assert _idempotency_key_for(today) == f"icp_phasing|{expected_monday}"


# ────────────────────────────────────────────────────────────────────────
# Escalation invocation
# ────────────────────────────────────────────────────────────────────────


class TestOpenIcpPhasingEscalation:
    def test_calls_escalate_with_configuration_decision_type(self):
        recommendation = {
            "escalate": True,
            "recommended": "phase_out_icp2",
            "rationale": "test rationale",
            "triggers": ["t1"],
        }
        lane_metrics = {"icp2_legacy": {"total": 30, "dm3_completed": 30, "positive": 0, "positive_rate": 0.0}}
        attio = MagicMock()
        today = date(2026, 5, 18)  # Monday
        with patch("scripts.gtm_attio_inventory.escalate") as m_escalate:
            m_escalate.return_value = {"id": {"record_id": "row-1"}}
            row = open_icp_phasing_escalation(
                recommendation, lane_metrics, today=today, attio=attio,
            )
        assert row["id"]["record_id"] == "row-1"

        m_escalate.assert_called_once()
        call = m_escalate.call_args
        assert call.kwargs["type"] == "configuration_decision"
        assert call.kwargs["decision_key"] == DECISION_KEY
        assert call.kwargs["idempotency_key"] == f"icp_phasing|{today.isoformat()}"
        # Deadline is exactly 7 days out (ESCALATION_DEADLINE_DAYS).
        expected_deadline = today.fromordinal(today.toordinal() + ESCALATION_DEADLINE_DAYS)
        assert call.kwargs["deadline"] == expected_deadline

        payload = call.kwargs["payload"]
        assert payload["recommended_option"] == "phase_out_icp2"
        assert payload["default_on_expiry"] == "keep_both"
        assert {opt["key"] for opt in payload["options"]} == {
            "keep_both", "phase_out_icp2", "double_down_icp1",
        }
        assert payload["lane_metrics"] is lane_metrics
        assert payload["triggers"] == ["t1"]
        assert payload["opened_by"].endswith("gtm_attio_inventory.main")


# ────────────────────────────────────────────────────────────────────────
# build_inventory (mocked Attio)
# ────────────────────────────────────────────────────────────────────────


class TestBuildInventory:
    def test_assembles_full_snapshot(self):
        entries = [
            _make_entry(scoring_lane="enterprise_mode", stage="DM1_SENT"),
            _make_entry(scoring_lane="legacy", stage="Prospect"),
        ]
        attio = MagicMock()
        attio.query_list_entries.return_value = entries
        attio.search_companies.return_value = [{"id": {"record_id": "c-1"}}]
        attio.search_deals.return_value = []

        inv = build_inventory(attio=attio, list_id="list-1")
        assert inv["list_id"] == "list-1"
        assert inv["totals"] == {"list_entries": 2, "companies": 1, "deals": 0}
        assert "icp1_enterprise" in inv["by_lane"]
        assert "icp2_legacy" in inv["by_lane"]
        assert inv["by_stage"]["DM1_SENT"] == 1
        assert inv["by_stage"]["Prospect"] == 1
        assert inv["phasing_recommendation"]["escalate"] is False  # too few entries


# ────────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────────


class TestCli:
    def test_fails_loud_without_list_id(self, monkeypatch, tmp_path):
        # .env may have populated ATTIO_LIST_ID at module load; empty-string
        # both unset and blank values via the same env-presence check.
        monkeypatch.setenv("ATTIO_LIST_ID", "")
        runner = CliRunner()
        result = runner.invoke(main, ["--out", str(tmp_path / "out.json")])
        assert result.exit_code == 2
        assert "ATTIO_LIST_ID" in result.output

    def test_dry_run_does_not_escalate(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ATTIO_LIST_ID", "list-1")
        # Empty population would trip the abstain gate before dry-run prints
        # — stub the floor down so we exercise the dry-run branch.
        monkeypatch.setenv(EXPECTED_MIN_LIST_ENTRIES_ENV, "0")
        attio_cm = MagicMock()
        attio_cm.__enter__.return_value = attio_cm
        attio_cm.__exit__.return_value = False
        # Empty inventory → recommendation.escalate=False, but dry-run also
        # short-circuits AFTER printing — confirm escalate() is never called.
        attio_cm.query_list_entries.return_value = []
        attio_cm.search_companies.return_value = []
        attio_cm.search_deals.return_value = []

        with patch("scripts.gtm_attio_inventory.AttioClient", _patched_attio_class(attio_cm)), \
             patch("scripts.gtm_attio_inventory.escalate") as m_escalate:
            runner = CliRunner()
            result = runner.invoke(
                main, ["--out", str(tmp_path / "out.json"), "--dry-run"],
            )
            assert result.exit_code == 0, result.output
            m_escalate.assert_not_called()

    def test_no_emit_empty_skips_write(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ATTIO_LIST_ID", "list-1")
        # Stub out the population floor so empty input doesn't trip abstain.
        monkeypatch.setenv(EXPECTED_MIN_LIST_ENTRIES_ENV, "0")
        attio_cm = MagicMock()
        attio_cm.__enter__.return_value = attio_cm
        attio_cm.__exit__.return_value = False
        attio_cm.query_list_entries.return_value = []
        attio_cm.search_companies.return_value = []
        attio_cm.search_deals.return_value = []

        out_path = tmp_path / "out.json"
        with patch("scripts.gtm_attio_inventory.AttioClient", _patched_attio_class(attio_cm)):
            runner = CliRunner()
            result = runner.invoke(
                main, ["--out", str(out_path), "--no-emit-empty", "--dry-run"],
            )
            assert result.exit_code == 0, result.output
            assert not out_path.exists()


# ────────────────────────────────────────────────────────────────────────
# Aggregation across ICP lanes (post-fold-in)
# ────────────────────────────────────────────────────────────────────────


class TestAggregate:
    def test_icp2_aggregates_target_and_legacy(self):
        # target_company_mode + legacy → ICP 2 aggregate per
        # workflows.weekly_prospect.enforce_icp_lane_geo.
        lane_metrics = {
            "icp2_target": {"dm3_completed": 20, "positive": 1},
            "icp2_legacy": {"dm3_completed": 20, "positive": 1},
        }
        agg = _aggregate(lane_metrics, ICP2_LANES)
        assert agg["dm3_completed"] == 40
        assert agg["positive"] == 2
        assert agg["positive_rate"] == pytest.approx(2 / 40)

    def test_below_min_returns_none_rate(self):
        lane_metrics = {
            "icp1_enterprise": {"dm3_completed": 5, "positive": 1},
        }
        agg = _aggregate(lane_metrics, ICP1_LANES)
        assert agg["positive_rate"] is None


class TestEvaluatePhasingWithSplitIcp2:
    def test_target_legacy_contribute_to_icp2_aggregate(self):
        """A phase-out trigger fires only when target+legacy COMBINED have
        ≥30 DM3-completed and the combined positive rate is low — not when
        each lane alone has 15."""
        lane_metrics = {
            # 15 + 15 = 30 ICP-2 dm3_completed, both with 0 positives → rate=0 → trigger A
            "icp2_target": {"total": 15, "dm3_completed": 15, "positive": 0, "positive_rate": None},
            "icp2_legacy": {"total": 15, "dm3_completed": 15, "positive": 0, "positive_rate": None},
        }
        out = evaluate_phasing_recommendation(lane_metrics)
        assert out["escalate"] is True
        assert out["icp2_aggregate"]["dm3_completed"] == 30
        assert out["icp2_aggregate"]["positive_rate"] == 0.0


# ────────────────────────────────────────────────────────────────────────
# Data-quality abstain gate
# ────────────────────────────────────────────────────────────────────────


class TestAssessDataQuality:
    def test_no_reasons_when_clean(self):
        inv = {
            "totals": {"list_entries": 1000},
            "by_lane": {"icp2_legacy": {"dm3_completed": 100}},
        }
        q = assess_data_quality(inv, expected_min=500)
        assert q["abstain"] is False
        assert q["reasons"] == []

    def test_population_below_floor(self):
        inv = {"totals": {"list_entries": 50}, "by_lane": {}}
        q = assess_data_quality(inv, expected_min=500)
        assert q["abstain"] is True
        assert any("population_below_floor" in r for r in q["reasons"])

    def test_unknown_lane_fraction_high(self):
        inv = {
            "totals": {"list_entries": 1000},
            "by_lane": {
                "icp2_legacy": {"dm3_completed": 80},
                UNKNOWN_LANE_BUCKET: {"dm3_completed": 20},  # 20% > 5%
            },
        }
        q = assess_data_quality(inv, expected_min=500)
        assert q["abstain"] is True
        assert any("unknown_lane_fraction_high" in r for r in q["reasons"])

    def test_unknown_fraction_zero_when_no_dm3_total(self):
        # All entries have no DM3 yet → cannot compute a fraction; do not
        # abstain on the lane-cleanliness rule (population floor still applies).
        inv = {
            "totals": {"list_entries": 1000},
            "by_lane": {UNKNOWN_LANE_BUCKET: {"dm3_completed": 0}},
        }
        q = assess_data_quality(inv, expected_min=500)
        assert q["abstain"] is False


# ────────────────────────────────────────────────────────────────────────
# Heuristic boundary tests (mutation-survival)
# ────────────────────────────────────────────────────────────────────────


class TestHeuristicBoundaries:
    def test_n_one_below_min_does_not_escalate(self):
        n = DM3_COMPLETED_MIN_FOR_RATE - 1
        lane_metrics = {
            "icp2_legacy": {
                "total": n, "dm3_completed": n, "positive": 0,
                "positive_rate": None,
            },
        }
        out = evaluate_phasing_recommendation(lane_metrics)
        assert out["escalate"] is False

    def test_rate_exactly_threshold_does_not_fire_strict_lt(self):
        n = 100  # large N so rate is computed
        # positive count s.t. rate == ICP2_LOW_RATE_THRESHOLD exactly (5/100=0.05)
        lane_metrics = {
            "icp2_legacy": {
                "total": n, "dm3_completed": n, "positive": 5,
                "positive_rate": ICP2_LOW_RATE_THRESHOLD,
            },
        }
        out = evaluate_phasing_recommendation(lane_metrics)
        # Strict < — exactly 0.05 does NOT trip the low-rate trigger
        assert out["escalate"] is False

    def test_ratio_exactly_threshold_fires_inclusive_ge(self):
        n = 100
        # icp1_rate=0.10, icp2_rate=0.05 → ratio=2.0 exactly
        lane_metrics = {
            "icp1_enterprise": {
                "total": n, "dm3_completed": n, "positive": 10, "positive_rate": 0.10,
            },
            "icp2_legacy": {
                "total": n, "dm3_completed": n, "positive": 5, "positive_rate": 0.05,
            },
        }
        out = evaluate_phasing_recommendation(lane_metrics)
        # Inclusive >= — exactly 2.0 DOES fire (low-rate is strict < so doesn't fire here)
        assert out["escalate"] is True
        assert any("icp1_rate/icp2_rate" in t for t in out["triggers"])


# ────────────────────────────────────────────────────────────────────────
# Week anchor — operator-local TZ
# ────────────────────────────────────────────────────────────────────────


class TestOperatorLocalMonday:
    def test_tz_constant_is_america_lima(self):
        # If this ever changes, the cron's idempotency semantics change —
        # forcibly review.
        assert WEEK_ANCHOR_TZ == "America/Lima"

    @pytest.mark.parametrize("anchor,expected_monday", [
        (date(2026, 5, 18), "2026-05-18"),  # Monday
        (date(2026, 5, 24), "2026-05-18"),  # Sunday
        (date(2026, 5, 25), "2026-05-25"),  # Next Monday
    ])
    def test_idempotency_key_uses_passed_in_date(self, anchor, expected_monday):
        # Anchors supplied directly bypass the live-TZ lookup so the test
        # is deterministic.
        assert _idempotency_key_for(anchor) == f"icp_phasing|{expected_monday}"


# ────────────────────────────────────────────────────────────────────────
# CLI exit codes + escalation success path (CLI lens fold-in)
# ────────────────────────────────────────────────────────────────────────


class TestCliExitCodes:
    def test_missing_list_id_returns_config_missing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ATTIO_LIST_ID", "")
        runner = CliRunner()
        result = runner.invoke(main, ["--out", str(tmp_path / "out.json")])
        assert result.exit_code == EXIT_CONFIG_MISSING

    def test_population_below_floor_returns_abstained(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ATTIO_LIST_ID", "list-1")
        monkeypatch.setenv(EXPECTED_MIN_LIST_ENTRIES_ENV, "1000")  # set high floor
        attio_cm = MagicMock()
        attio_cm.__enter__.return_value = attio_cm
        attio_cm.__exit__.return_value = False
        attio_cm.query_list_entries.return_value = [_make_entry(scoring_lane="legacy")]
        attio_cm.search_companies.return_value = []
        attio_cm.search_deals.return_value = []

        with patch("scripts.gtm_attio_inventory.AttioClient", _patched_attio_class(attio_cm)), \
             patch("scripts.gtm_attio_inventory.escalate") as m_escalate:
            runner = CliRunner()
            result = runner.invoke(main, ["--out", str(tmp_path / "out.json")])
            assert result.exit_code == EXIT_HEURISTIC_ABSTAINED, result.output
            m_escalate.assert_not_called()  # NEVER escalate on dirty data

    def test_bad_week_anchor_returns_config_missing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ATTIO_LIST_ID", "list-1")
        runner = CliRunner()
        result = runner.invoke(main, [
            "--out", str(tmp_path / "out.json"),
            "--week-anchor", "not-a-date",
            "--dry-run",
        ])
        assert result.exit_code == EXIT_CONFIG_MISSING

    def test_httpx_error_returns_attio_failure(self, monkeypatch, tmp_path):
        import httpx
        monkeypatch.setenv("ATTIO_LIST_ID", "list-1")
        attio_cm = MagicMock()
        attio_cm.__enter__.return_value = attio_cm
        attio_cm.__exit__.return_value = False
        attio_cm.query_list_entries.side_effect = httpx.ConnectError("boom")

        with patch("scripts.gtm_attio_inventory.AttioClient", _patched_attio_class(attio_cm)):
            runner = CliRunner()
            result = runner.invoke(main, ["--out", str(tmp_path / "out.json")])
            assert result.exit_code == EXIT_ATTIO_FAILURE


class TestCliEscalationSuccessPath:
    def test_full_round_trip_opens_escalation(self, monkeypatch, tmp_path):
        """The CLI happy path — heuristic fires, escalate called, exit 0."""
        monkeypatch.setenv("ATTIO_LIST_ID", "list-1")
        monkeypatch.setenv(EXPECTED_MIN_LIST_ENTRIES_ENV, "0")

        # 30 ICP-2 entries all with DM3-sent + 0 positives → low-rate trigger
        n = DM3_COMPLETED_MIN_FOR_RATE
        entries = [
            _make_entry(scoring_lane="legacy", dm3_sent_at="x", response_classification="negative")
            for _ in range(n)
        ]
        attio_cm = MagicMock()
        attio_cm.__enter__.return_value = attio_cm
        attio_cm.__exit__.return_value = False
        attio_cm.query_list_entries.return_value = entries
        attio_cm.search_companies.return_value = []
        attio_cm.search_deals.return_value = []

        with patch("scripts.gtm_attio_inventory.AttioClient", _patched_attio_class(attio_cm)), \
             patch("scripts.gtm_attio_inventory.escalate") as m_escalate:
            m_escalate.return_value = {"id": {"record_id": "row-success"}}
            runner = CliRunner()
            result = runner.invoke(main, [
                "--out", str(tmp_path / "out.json"),
                "--week-anchor", "2026-05-18",  # deterministic Monday
            ])
            assert result.exit_code == EXIT_OK, result.output
            m_escalate.assert_called_once()
            assert "Opened icp_phasing escalation" in result.output
            assert "row-success" in result.output
