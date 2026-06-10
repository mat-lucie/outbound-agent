"""PR-22: Cohort archaeology + measure-side dedup + is_send_eligible extension.

Tests lock in:

1. Confidence-scoring heuristic: high/medium/low/zero confidence rows
2. Threshold split: exactly 0.8 → INCLUDED; 0.79 → EXCLUDED
3. _classify_row: frozen_at output + experiment_id handling
4. Idempotency: second --apply run = rows_modified=0
5. Immutability: rows at frozen_at != None are skipped
6. Soft-delete skip: merged_into rows are skipped (not modified)
7. "prospect" skip: frozen_at="prospect" rows are NOT overwritten
8. is_send_eligible: legacy_* → False
9. Measure-side dedup: 2 entries with same canonical_linkedin_url → 1 in output
10. COHORT_MEASUREMENT_START_DATE constant is present
11. Dry-run honesty: identical compute path, no writes
12. ReclassificationRunWriter + MigrationRunWriter both opened per run
13. run() returns correct exit codes
14. No-experiment edge case: inferred_experiment_id=None → legacy_pure_unknown
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from models.pipeline import is_send_eligible
from scripts.backfill_experiment_id_archaeology import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    _classify_row,
    _confidence_distribution,
    _should_skip_row,
    compute_archaeology_confidence,
    run,
)
from tests.fakes import fake_daily_run
from workflows.learn import COHORT_MEASUREMENT_START_DATE
from workflows.learn import _dedup_by_canonical_url as learn_dedup

# ============================================================
# Fixtures
# ============================================================


def _make_entry(**overrides) -> dict:
    """Minimal LinkedIn Outreach entry dict."""
    base: dict = {
        "record_id": "rec-001",
        "entry_id": "entry-001",
        "merged_into": None,
        "experiment_id": None,
        "experiment_id_frozen_at": None,
        "experiment_id_backfill_confidence": None,
        "stage": "Prospect",
        "dm_step": None,
        "dm1_sent_at": None,
        "dm2_sent_at": None,
        "dm3_sent_at": None,
        "last_contact_date": None,
        "response_classification": None,
        "canonical_linkedin_url": None,
    }
    base.update(overrides)
    return base


# ============================================================
# 1. Confidence-scoring heuristic
# ============================================================


class TestComputeArchaeologyConfidence:
    def test_zero_confidence_empty_row(self):
        """Row with no signals scores 0.0."""
        entry = _make_entry()
        assert compute_archaeology_confidence(entry) == 0.0

    def test_low_confidence_last_contact_only(self):
        """Only last_contact_date set → 0.10."""
        entry = _make_entry(last_contact_date="2026-01-15")
        assert compute_archaeology_confidence(entry) == pytest.approx(0.10, abs=1e-9)

    def test_medium_confidence_dm_step_and_last_contact(self):
        """dm_step + last_contact_date → 0.40 (0.30 + 0.10)."""
        entry = _make_entry(dm_step="dm1", last_contact_date="2026-01-15")
        score = compute_archaeology_confidence(entry)
        assert score == pytest.approx(0.40, abs=1e-9)

    def test_high_confidence_full_dm3_row(self):
        """dm_step=dm3 + dm1_sent_at + stage=DM3 Sent + last_contact → 0.85."""
        entry = _make_entry(
            dm_step="dm3",
            dm1_sent_at="2026-01-01T12:00:00Z",
            stage="DM3 Sent",
            last_contact_date="2026-02-01",
        )
        score = compute_archaeology_confidence(entry)
        # dm_step (0.30) + dm3 bonus (0.05) + dm1_sent_at (0.20) + stage (0.20) + lcd (0.10) = 0.85
        assert score == pytest.approx(0.85, abs=1e-9)

    def test_max_confidence_positive_response_row(self):
        """All signals → clamped at 1.0."""
        entry = _make_entry(
            dm_step="dm3",
            dm1_sent_at="2026-01-01T12:00:00Z",
            stage="Responded",
            last_contact_date="2026-03-01",
            response_classification="positive",
        )
        score = compute_archaeology_confidence(entry)
        # dm3 (0.35) + sent_at (0.20) + classification (0.10) + positive (0.05) +
        # stage (0.20) + lcd (0.10) = 1.00
        assert score == pytest.approx(1.0, abs=1e-9)

    def test_dm1_step_no_sent_at(self):
        """dm_step=dm1 alone → 0.30."""
        entry = _make_entry(dm_step="dm1")
        assert compute_archaeology_confidence(entry) == pytest.approx(0.30, abs=1e-9)

    def test_dm3_step_no_bonus_twice(self):
        """dm3 bonus is only counted once even if step is 'DM3'."""
        entry = _make_entry(dm_step="dm3")
        # dm_step (0.30) + dm3 bonus (0.05) = 0.35
        assert compute_archaeology_confidence(entry) == pytest.approx(0.35, abs=1e-9)

    def test_score_clamped_to_one(self):
        """Score never exceeds 1.0."""
        entry = _make_entry(
            dm_step="dm3",
            dm1_sent_at="t",
            dm2_sent_at="t",
            dm3_sent_at="t",
            stage="Responded",
            last_contact_date="2026-01-01",
            response_classification="positive",
        )
        score = compute_archaeology_confidence(entry)
        assert score <= 1.0

    def test_responded_stage_counts_as_funnel(self):
        """Responded stage is in _FUNNEL_STAGES."""
        entry = _make_entry(stage="Responded")
        score = compute_archaeology_confidence(entry)
        # stage alone → 0.20
        assert score == pytest.approx(0.20, abs=1e-9)

    def test_connection_sent_stage_not_funnel(self):
        """Connection Sent stage is NOT in _FUNNEL_STAGES — no stage signal."""
        entry = _make_entry(stage="Connection Sent")
        score = compute_archaeology_confidence(entry)
        assert score == pytest.approx(0.0, abs=1e-9)


# ============================================================
# 2. Threshold split
# ============================================================


class TestThresholdSplit:
    def test_at_threshold_is_included(self):
        """Confidence exactly at threshold → legacy_inferred_by_archaeology."""
        frozen_at, _ = _classify_row(
            _make_entry(),
            confidence=0.80,
            confidence_threshold=0.80,
            inferred_experiment_id="exp-001",
        )
        assert frozen_at == "legacy_inferred_by_archaeology"

    def test_below_threshold_is_excluded(self):
        """Confidence 0.01 below threshold → legacy_pure_unknown."""
        frozen_at, _ = _classify_row(
            _make_entry(),
            confidence=0.79,
            confidence_threshold=0.80,
            inferred_experiment_id="exp-001",
        )
        assert frozen_at == "legacy_pure_unknown"

    def test_above_threshold_is_included(self):
        """Confidence above threshold → legacy_inferred_by_archaeology."""
        frozen_at, _ = _classify_row(
            _make_entry(),
            confidence=0.95,
            confidence_threshold=0.80,
            inferred_experiment_id="exp-001",
        )
        assert frozen_at == "legacy_inferred_by_archaeology"

    def test_zero_confidence_is_excluded(self):
        """Zero confidence → legacy_pure_unknown."""
        frozen_at, _ = _classify_row(
            _make_entry(),
            confidence=0.0,
            confidence_threshold=0.80,
            inferred_experiment_id="exp-001",
        )
        assert frozen_at == "legacy_pure_unknown"

    def test_no_experiment_id_forces_excluded(self):
        """No inferable experiment_id → legacy_pure_unknown regardless of confidence."""
        frozen_at, exp_id = _classify_row(
            _make_entry(),
            confidence=0.99,
            confidence_threshold=0.80,
            inferred_experiment_id=None,
        )
        assert frozen_at == "legacy_pure_unknown"
        assert exp_id is None

    def test_existing_experiment_id_preserved_for_included(self):
        """Row already has experiment_id → kept as-is for INCLUDED rows."""
        entry = _make_entry(experiment_id="existing-exp")
        frozen_at, exp_id = _classify_row(
            entry,
            confidence=0.90,
            confidence_threshold=0.80,
            inferred_experiment_id="inferred-exp",
        )
        assert frozen_at == "legacy_inferred_by_archaeology"
        assert exp_id == "existing-exp"

    def test_null_experiment_id_gets_inferred_for_included(self):
        """Row has no experiment_id → gets the inferred one for INCLUDED rows."""
        entry = _make_entry(experiment_id=None)
        frozen_at, exp_id = _classify_row(
            entry,
            confidence=0.90,
            confidence_threshold=0.80,
            inferred_experiment_id="inferred-exp",
        )
        assert frozen_at == "legacy_inferred_by_archaeology"
        assert exp_id == "inferred-exp"

    def test_classify_max_confidence_included(self):
        """confidence=1.0 → INCLUDED (data-steward NIT explicit fixture)."""
        frozen_at, _ = _classify_row(
            _make_entry(),
            confidence=1.0,
            confidence_threshold=0.80,
            inferred_experiment_id="exp-001",
        )
        assert frozen_at == "legacy_inferred_by_archaeology"


# ============================================================
# 3. Skip logic (immutability + soft-delete + idempotency)
# ============================================================


class TestShouldSkipRow:
    def test_soft_delete_is_skipped(self):
        """merged_into set → skip reason 'soft_deleted'."""
        entry = _make_entry(merged_into="winner-record-id")
        reason = _should_skip_row(entry, "rec-001")
        assert reason == "soft_deleted"

    def test_already_stamped_accepted_is_skipped(self):
        """frozen_at='accepted' → idempotency skip."""
        entry = _make_entry(experiment_id_frozen_at="accepted")
        reason = _should_skip_row(entry, "rec-001")
        assert reason is not None
        assert "already_stamped" in reason

    def test_already_stamped_connection_sent_is_skipped(self):
        """frozen_at='connection_sent' → idempotency skip."""
        entry = _make_entry(experiment_id_frozen_at="connection_sent")
        reason = _should_skip_row(entry, "rec-001")
        assert reason is not None

    def test_already_stamped_prospect_is_skipped(self):
        """frozen_at='prospect' (fresh PROSPECT-commit from PR-21) → NOT overwritten."""
        entry = _make_entry(experiment_id_frozen_at="prospect")
        reason = _should_skip_row(entry, "rec-001")
        assert reason is not None, (
            "Rows at frozen_at='prospect' are fresh PR-21 PROSPECT-commits and "
            "must NOT be overwritten by archaeology."
        )

    def test_already_stamped_legacy_inferred_is_idempotent(self):
        """frozen_at='legacy_inferred_by_archaeology' (already done) → skip."""
        entry = _make_entry(experiment_id_frozen_at="legacy_inferred_by_archaeology")
        reason = _should_skip_row(entry, "rec-001")
        assert reason is not None

    def test_already_stamped_legacy_pure_unknown_is_idempotent(self):
        """frozen_at='legacy_pure_unknown' (already done) → skip."""
        entry = _make_entry(experiment_id_frozen_at="legacy_pure_unknown")
        reason = _should_skip_row(entry, "rec-001")
        assert reason is not None

    def test_null_frozen_at_is_eligible(self):
        """frozen_at=None → eligible for processing."""
        entry = _make_entry(experiment_id_frozen_at=None)
        reason = _should_skip_row(entry, "rec-001")
        assert reason is None

    def test_legacy_pre_tsv_era_is_skipped(self):
        """frozen_at='legacy_pre_tsv_era' → skip (intentional design choice).

        Pre-TSV-era rows predate the TSV experiment catalog. There is no
        legitimate experiment_id to attribute even with strong funnel signals,
        so archaeology MUST leave them alone (would falsely claim a cohort
        identity we cannot reconstruct). Caught by the idempotency check —
        `legacy_pre_tsv_era` is a non-None frozen_at value.
        """
        entry = _make_entry(experiment_id_frozen_at="legacy_pre_tsv_era")
        reason = _should_skip_row(entry, "rec-001")
        assert reason is not None


# ============================================================
# 4. is_send_eligible: legacy_* → False
# ============================================================


class TestIsSendEligibleLegacy:
    def test_legacy_inferred_by_archaeology_not_send_eligible(self):
        """§3.10: archaeology INCLUDED rows are send-ineligible."""
        entry = _make_entry(
            stage="DM1 Sent",
            experiment_id_frozen_at="legacy_inferred_by_archaeology",
        )
        assert is_send_eligible(entry) is False

    def test_legacy_pure_unknown_not_send_eligible(self):
        """§3.10: archaeology EXCLUDED rows are send-ineligible."""
        entry = _make_entry(
            stage="DM1 Sent",
            experiment_id_frozen_at="legacy_pure_unknown",
        )
        assert is_send_eligible(entry) is False

    def test_prospect_frozen_at_is_send_eligible(self):
        """fresh PROSPECT-commit (frozen_at='prospect') → ELIGIBLE for sends."""
        entry = _make_entry(
            stage="Prospect",
            experiment_id_frozen_at="prospect",
        )
        assert is_send_eligible(entry) is True

    def test_none_frozen_at_is_send_eligible(self):
        """NULL frozen_at (pre-PR-21 row not yet archaeology-stamped) → ELIGIBLE."""
        entry = _make_entry(
            stage="Accepted",
            experiment_id_frozen_at=None,
        )
        assert is_send_eligible(entry) is True

    def test_merged_into_overrides_legacy_check(self):
        """merged_into takes priority — soft-deleted is always ineligible."""
        entry = _make_entry(
            stage="DM1 Sent",
            merged_into="winner-id",
            experiment_id_frozen_at=None,
        )
        assert is_send_eligible(entry) is False


# ============================================================
# 5. Measure-side dedup in learn.py
# ============================================================


class TestDedupByCanonicalUrl:
    """Tests for the _dedup_by_canonical_url helper in workflows.learn (PR-22
    measure-side dedup). The dedup logic lives in learn.py; archaeology script
    relies on Attio-side canonical URLs already resolved by PR-9.5."""

    def _make_lo_entry(self, url: str | None, stage: str = "Prospect") -> dict:
        return {
            "canonical_linkedin_url": url,
            "stage": stage,
            "record_id": f"rec-{url or 'nourl'}-{stage}",
        }

    def test_two_same_url_entries_deduped_to_one(self):
        """Two entries with same canonical_url → keep one."""
        entries = [
            self._make_lo_entry("https://www.linkedin.com/in/alice", "DM1 Sent"),
            self._make_lo_entry("https://www.linkedin.com/in/alice", "Prospect"),
        ]
        result = learn_dedup(entries)
        # URLs are canonicalized (lowercase + strip trailing slash)
        assert len(result) == 1

    def test_keeps_higher_stage_rank_when_deduping(self):
        """When two entries share a URL, keep the one with higher stage_rank."""
        lower = self._make_lo_entry("https://www.linkedin.com/in/bob", "Prospect")
        higher = self._make_lo_entry("https://www.linkedin.com/in/bob", "DM3 Sent")
        result = learn_dedup([lower, higher])
        assert len(result) == 1
        assert result[0]["stage"] == "DM3 Sent"

    def test_distinct_urls_not_deduped(self):
        """Two entries with different URLs are both kept."""
        entries = [
            self._make_lo_entry("https://www.linkedin.com/in/alice", "DM1 Sent"),
            self._make_lo_entry("https://www.linkedin.com/in/bob", "DM2 Sent"),
        ]
        result = learn_dedup(entries)
        assert len(result) == 2

    def test_no_url_entries_preserved(self):
        """Entries with no canonical_linkedin_url are kept as-is."""
        entries = [
            self._make_lo_entry(None, "Prospect"),
            self._make_lo_entry("", "DM1 Sent"),
        ]
        result = learn_dedup(entries)
        assert len(result) == 2

    def test_url_normalization_trailing_slash(self):
        """URLs differing only in trailing slash are deduplicated."""
        entries = [
            self._make_lo_entry("https://www.linkedin.com/in/alice/", "Prospect"),
            self._make_lo_entry("https://www.linkedin.com/in/alice", "DM2 Sent"),
        ]
        result = learn_dedup(entries)
        assert len(result) == 1
        assert result[0]["stage"] == "DM2 Sent"

    def test_url_normalization_case(self):
        """URLs differing only in case are deduplicated."""
        entries = [
            self._make_lo_entry("https://www.linkedin.com/in/Alice", "Prospect"),
            self._make_lo_entry("https://www.linkedin.com/in/alice", "Responded"),
        ]
        result = learn_dedup(entries)
        assert len(result) == 1
        assert result[0]["stage"] == "Responded"

    def test_empty_input_returns_empty(self):
        """Empty input → empty output."""
        assert learn_dedup([]) == []

    def test_single_entry_unchanged(self):
        """Single entry → single entry unchanged."""
        entries = [self._make_lo_entry("https://www.linkedin.com/in/alice", "DM1 Sent")]
        result = learn_dedup(entries)
        assert result == entries


# ============================================================
# 6. COHORT_MEASUREMENT_START_DATE constant
# ============================================================


class TestCohortMeasurementStartDate:
    def test_constant_present_and_correct_format(self):
        """COHORT_MEASUREMENT_START_DATE is a valid ISO date string."""
        from datetime import date
        d = date.fromisoformat(COHORT_MEASUREMENT_START_DATE)
        assert d.year == 2026
        assert d.month == 5
        assert d.day == 22


# ============================================================
# 7. _confidence_distribution helper
# ============================================================


class TestConfidenceDistribution:
    def test_empty_scores(self):
        dist = _confidence_distribution([], 0.8)
        assert dist["count"] == 0
        assert dist["included"] == 0

    def test_all_included(self):
        dist = _confidence_distribution([0.9, 0.95, 1.0], 0.8)
        assert dist["count"] == 3
        assert dist["included"] == 3
        assert dist["excluded"] == 0

    def test_mixed_split(self):
        dist = _confidence_distribution([0.5, 0.8, 0.9], 0.8)
        assert dist["included"] == 2
        assert dist["excluded"] == 1

    def test_boundary_exact_threshold(self):
        """Score exactly at threshold is included."""
        dist = _confidence_distribution([0.8], 0.8)
        assert dist["included"] == 1

    def test_below_threshold(self):
        dist = _confidence_distribution([0.79], 0.8)
        assert dist["included"] == 0
        assert dist["excluded"] == 1


# ============================================================
# 8. run() integration — dry-run vs. apply
# ============================================================


class TestRunIntegration:
    """Integration tests for run() using heavily mocked Attio client."""

    @pytest.fixture(autouse=True)
    def _patch_threshold_decision_opener(self):
        """Stub the operator-review-queue opener — it would call into
        `workflows.escalation.escalate` which hits an Attio query path the
        per-test MagicMocks can't satisfy. The opener is exercised by a
        dedicated test (`TestThresholdDecisionRow`) using a richer mock.
        """
        with patch(
            "scripts.backfill_experiment_id_archaeology._open_threshold_decision_row"
        ):
            yield

    def _make_attio_mock(self, entries: list[dict]) -> MagicMock:
        """Return a mock AttioClient that yields the given entries."""
        attio = MagicMock()
        # query_list_entries returns raw entries; parse_entry returns them as-is
        attio.query_list_entries.return_value = entries
        attio.parse_entry.side_effect = lambda e: e
        attio._request.return_value = {"data": {"id": {"record_id": "run-record-001"}}}
        return attio

    def test_dry_run_no_attio_writes(self):
        """--dry-run: no PATCH calls issued."""
        eligible_entry = _make_entry(
            record_id="rec-001",
            experiment_id_frozen_at=None,
            stage="DM2 Sent",
            dm_step="dm2",
            last_contact_date="2026-01-01",
            dm1_sent_at="2026-01-01T12:00:00Z",
        )
        attio = self._make_attio_mock([eligible_entry])

        with patch(
            "scripts.backfill_experiment_id_archaeology._infer_experiment_id",
            return_value="exp-001",
        ):
            exit_code = run(
                dry_run=True,
                confidence_threshold=DEFAULT_CONFIDENCE_THRESHOLD,
                attio=attio,
            )

        assert exit_code == 0
        # No PATCH (write) calls should have been made
        patch_calls = [
            c for c in attio._request.call_args_list
            if c.args and "PATCH" in str(c.args)
        ]
        assert len(patch_calls) == 0, "dry_run must not issue PATCH calls"

    def test_apply_writes_to_eligible_row(self):
        """--apply: PATCH call issued for eligible row."""
        eligible_entry = _make_entry(
            record_id="rec-999",
            experiment_id_frozen_at=None,
            stage="DM3 Sent",
            dm_step="dm3",
            last_contact_date="2026-01-15",
            dm1_sent_at="2026-01-01T12:00:00Z",
        )
        attio = self._make_attio_mock([eligible_entry])

        with patch(
            "scripts.backfill_experiment_id_archaeology._infer_experiment_id",
            return_value="exp-001",
        ):
            exit_code = run(
                dry_run=False,
                confidence_threshold=DEFAULT_CONFIDENCE_THRESHOLD,
                attio=attio,
            )

        assert exit_code == 0
        # At least one PATCH call for the eligible row
        patch_calls = [
            c for c in attio._request.call_args_list
            if c.args and "PATCH" in str(c.args[0])
        ]
        assert len(patch_calls) >= 1

    def test_second_run_is_noop_after_apply(self):
        """§9.4 idempotency: running --apply twice in sequence must be a no-op
        on the second run. After the first run stamps the row, the second
        run's `query_list_entries` returns the post-write state — every row
        now has frozen_at set, and `_should_skip_row` reports
        already_stamped for all of them. rows_modified must be 0 on run 2.
        """
        from copy import deepcopy

        # Two eligible rows — both will be stamped on run 1.
        eligible_a = _make_entry(
            record_id="rec-AAA",
            experiment_id_frozen_at=None,
            stage="DM3 Sent",
            dm_step="dm3",
            last_contact_date="2026-01-15",
            dm1_sent_at="2026-01-01T12:00:00Z",
        )
        eligible_b = _make_entry(
            record_id="rec-BBB",
            experiment_id_frozen_at=None,
            stage="DM1 Sent",
            dm_step="dm1",
            last_contact_date="2026-02-01",
        )
        initial_entries = [eligible_a, eligible_b]
        # Stateful mock: query_list_entries returns the current entries; each
        # successful PATCH mutates the underlying entry so the next read sees
        # frozen_at populated.
        state: list[dict] = [deepcopy(e) for e in initial_entries]
        attio = MagicMock()
        attio.query_list_entries.side_effect = lambda **_: state
        attio.parse_entry.side_effect = lambda e: e

        def stateful_request(method, path, **kwargs):
            if method == "PATCH" and "linkedin_outreach/records" in str(path):
                # Extract record_id from path: /objects/linkedin_outreach/records/<id>
                rec_id = str(path).rsplit("/", 1)[-1]
                values = kwargs.get("json", {}).get("data", {}).get("values", {})
                for row in state:
                    if row["record_id"] == rec_id:
                        row.update(values)
                        break
            return {"data": {"id": {"record_id": "run-record-001"}}}

        attio._request.side_effect = stateful_request

        with patch(
            "scripts.backfill_experiment_id_archaeology._infer_experiment_id",
            return_value="exp-001",
        ):
            # Run 1: writes apply; rows_modified > 0
            exit_1 = run(
                dry_run=False,
                confidence_threshold=DEFAULT_CONFIDENCE_THRESHOLD,
                attio=attio,
            )
            assert exit_1 == 0
            # Verify state actually changed
            assert all(row.get("experiment_id_frozen_at") is not None for row in state), (
                "Run 1 must have stamped frozen_at on every eligible row"
            )

            patches_after_run1 = sum(
                1 for c in attio._request.call_args_list
                if c.args and c.args[0] == "PATCH"
                and "linkedin_outreach/records" in str(c.args)
            )
            assert patches_after_run1 >= 2, (
                f"Run 1 must have PATCHed both eligible rows; saw {patches_after_run1}"
            )

            # Run 2: every row now has frozen_at set; must be no-op
            exit_2 = run(
                dry_run=False,
                confidence_threshold=DEFAULT_CONFIDENCE_THRESHOLD,
                attio=attio,
            )
            assert exit_2 == 0

        patches_total = sum(
            1 for c in attio._request.call_args_list
            if c.args and c.args[0] == "PATCH"
            and "linkedin_outreach/records" in str(c.args)
        )
        # No additional PATCHes between run 1 and run 2 — second run is a no-op.
        assert patches_total == patches_after_run1, (
            f"Run 2 must not issue any linkedin_outreach PATCH; "
            f"saw {patches_total - patches_after_run1} new PATCH(es)."
        )

    def test_idempotency_already_stamped_row(self):
        """Row already stamped with legacy_inferred_by_archaeology → skipped."""
        already_done = _make_entry(
            record_id="rec-already",
            experiment_id_frozen_at="legacy_inferred_by_archaeology",
            experiment_id="exp-001",
        )
        attio = self._make_attio_mock([already_done])

        with patch(
            "scripts.backfill_experiment_id_archaeology._infer_experiment_id",
            return_value="exp-001",
        ):
            exit_code = run(
                dry_run=False,
                confidence_threshold=DEFAULT_CONFIDENCE_THRESHOLD,
                attio=attio,
            )

        assert exit_code == 0
        patch_calls = [
            c for c in attio._request.call_args_list
            if c.args and "PATCH" in str(c.args[0])
            and "linkedin_outreach/records" in str(c.args)
        ]
        assert len(patch_calls) == 0, "already-stamped row must not be PATCHed"

    def test_soft_deleted_row_skipped(self):
        """merged_into set → row is not modified."""
        loser = _make_entry(
            record_id="rec-loser",
            experiment_id_frozen_at=None,
            merged_into="winner-id",
            stage="DM1 Sent",
            dm_step="dm1",
        )
        attio = self._make_attio_mock([loser])

        with patch(
            "scripts.backfill_experiment_id_archaeology._infer_experiment_id",
            return_value="exp-001",
        ):
            exit_code = run(
                dry_run=False,
                confidence_threshold=DEFAULT_CONFIDENCE_THRESHOLD,
                attio=attio,
            )

        assert exit_code == 0
        # No PATCH for the loser record
        patch_calls = [
            c for c in attio._request.call_args_list
            if c.args and "PATCH" in str(c.args[0])
            and "rec-loser" in str(c)
        ]
        assert len(patch_calls) == 0

    def test_prospect_frozen_at_row_skipped(self):
        """frozen_at='prospect' (fresh PR-21 commit) → NOT overwritten."""
        fresh_row = _make_entry(
            record_id="rec-fresh",
            experiment_id="exp-001",
            experiment_id_frozen_at="prospect",
            stage="Prospect",
        )
        attio = self._make_attio_mock([fresh_row])

        with patch(
            "scripts.backfill_experiment_id_archaeology._infer_experiment_id",
            return_value="exp-001",
        ):
            exit_code = run(
                dry_run=False,
                confidence_threshold=DEFAULT_CONFIDENCE_THRESHOLD,
                attio=attio,
            )

        assert exit_code == 0
        patch_calls = [
            c for c in attio._request.call_args_list
            if c.args and "PATCH" in str(c.args[0])
            and "rec-fresh" in str(c)
        ]
        assert len(patch_calls) == 0

    def test_no_experiment_id_forces_pure_unknown(self):
        """When no experiment_id can be inferred, all rows get legacy_pure_unknown."""
        eligible_entry = _make_entry(
            record_id="rec-noexp",
            experiment_id_frozen_at=None,
            stage="DM2 Sent",
            dm_step="dm2",
            last_contact_date="2026-01-01",
            dm1_sent_at="2026-01-01T12:00:00Z",
        )
        attio = self._make_attio_mock([eligible_entry])
        attio._request.side_effect = [
            {"data": {"id": {"record_id": "run-rec-1"}}},  # ReclassificationRun write
            {"data": {"id": {"record_id": "run-rec-2"}}},  # MigrationRun write
        ]

        # Capture the actual PATCH body to verify frozen_at value
        patched_bodies = []

        def capture_request(method, path, **kwargs):
            if method == "PATCH" and "linkedin_outreach/records" in str(path):
                patched_bodies.append(kwargs.get("json", {}))
            return {"data": {"id": {"record_id": "run-rec-x"}}}

        attio._request.side_effect = capture_request
        attio.query_list_entries.return_value = [eligible_entry]

        with patch(
            "scripts.backfill_experiment_id_archaeology._infer_experiment_id",
            return_value=None,
        ):
            exit_code = run(
                dry_run=False,
                confidence_threshold=DEFAULT_CONFIDENCE_THRESHOLD,
                attio=attio,
            )

        assert exit_code == 0
        # Row MUST have been written with legacy_pure_unknown — the
        # capture-list-empty branch would silently pass the test if PATCH
        # never fired.
        assert patched_bodies, (
            "expected at least one PATCH body when inferred_experiment_id=None"
        )
        frozen_at_written = (
            patched_bodies[0]
            .get("data", {})
            .get("values", {})
            .get("experiment_id_frozen_at")
        )
        assert frozen_at_written == "legacy_pure_unknown"

    def test_confidence_threshold_above_one_raises_value_error(self):
        """run(confidence_threshold=1.5) raises ValueError before any Attio calls."""
        attio = self._make_attio_mock([])
        with pytest.raises(ValueError, match="confidence_threshold must be"):
            run(dry_run=True, confidence_threshold=1.5, attio=attio)
        # No Attio calls made before validation
        attio.query_list_entries.assert_not_called()

    def test_confidence_threshold_below_zero_raises_value_error(self):
        """run(confidence_threshold=-0.1) raises ValueError."""
        attio = self._make_attio_mock([])
        with pytest.raises(ValueError, match="confidence_threshold must be"):
            run(dry_run=True, confidence_threshold=-0.1, attio=attio)

    def test_unknown_record_id_skipped_not_patched(self):
        """A row with no resolvable record_id ("<unknown>" sentinel) is skipped
        via skip_excluded — must NEVER reach the PATCH path. Prevents the
        sentinel from leaking into an Attio URL.
        """
        broken = _make_entry(record_id=None)
        # Strip record_id to force the fallback path
        del broken["record_id"]
        broken["id"] = {}  # neither nested record_id either

        attio = self._make_attio_mock([broken])

        with patch(
            "scripts.backfill_experiment_id_archaeology._infer_experiment_id",
            return_value="exp-001",
        ):
            exit_code = run(
                dry_run=False,
                confidence_threshold=DEFAULT_CONFIDENCE_THRESHOLD,
                attio=attio,
            )
        assert exit_code == 0
        unknown_patches = [
            c for c in attio._request.call_args_list
            if c.args and c.args[0] == "PATCH"
            and "<unknown>" in str(c.args)
        ]
        assert unknown_patches == [], (
            "must never PATCH a URL containing the '<unknown>' record_id sentinel"
        )

    def test_attio_tempfail_returns_75(self):
        """Attio scope failure (cannot list entries) → EX_TEMPFAIL=75."""
        import httpx

        attio = MagicMock()
        attio.query_list_entries.side_effect = httpx.RequestError("scope error")

        exit_code = run(
            dry_run=True,
            confidence_threshold=DEFAULT_CONFIDENCE_THRESHOLD,
            attio=attio,
        )
        assert exit_code == 75

    def test_partial_failure_returns_exit_code_1(self):
        """Rows that fail PATCH → EX_PARTIAL=1."""
        import httpx

        eligible_entry = _make_entry(
            record_id="rec-fail",
            experiment_id_frozen_at=None,
            stage="DM1 Sent",
            dm_step="dm1",
        )
        attio = self._make_attio_mock([eligible_entry])

        def raise_on_linkedin_patch(method, path, **kwargs):
            if "linkedin_outreach/records/rec-fail" in str(path):
                raise httpx.HTTPStatusError(
                    "500", request=MagicMock(), response=MagicMock()
                )
            return {"data": {"id": {"record_id": "run-001"}}}

        attio._request.side_effect = raise_on_linkedin_patch

        with patch(
            "scripts.backfill_experiment_id_archaeology._infer_experiment_id",
            return_value="exp-001",
        ):
            exit_code = run(
                dry_run=False,
                confidence_threshold=DEFAULT_CONFIDENCE_THRESHOLD,
                attio=attio,
            )

        assert exit_code == 1


# ============================================================
# 9. ReclassificationRunWriter + MigrationRunWriter correlation
# ============================================================


class TestRunRowCorrelation:
    """Verify that ReclassificationRunWriter and MigrationRunWriter are both
    opened and that the rec_run.run_id is passed to MigrationRunWriter."""

    @pytest.fixture(autouse=True)
    def _patch_threshold_decision_opener(self):
        """See `TestRunIntegration._patch_threshold_decision_opener`."""
        with patch(
            "scripts.backfill_experiment_id_archaeology._open_threshold_decision_row"
        ):
            yield

    def test_both_writers_opened(self):
        """run() opens both ReclassificationRunWriter and MigrationRunWriter."""
        attio = MagicMock()
        attio.query_list_entries.return_value = []
        attio.parse_entry.side_effect = lambda e: e
        attio._request.return_value = {"data": {"id": {"record_id": "run-001"}}}

        with (
            patch(
                "scripts.backfill_experiment_id_archaeology.ReclassificationRunWriter"
            ) as mock_rec,
            patch(
                "scripts.backfill_experiment_id_archaeology.MigrationRunWriter"
            ) as mock_mig,
        ):
            # Set up context manager protocol for both mocks
            rec_instance = MagicMock()
            rec_instance.run_id = "rec-run-id-001"
            rec_instance.__enter__ = MagicMock(return_value=rec_instance)
            rec_instance.__exit__ = MagicMock(return_value=False)
            mock_rec.return_value = rec_instance

            mig_instance = MagicMock()
            mig_instance.rows_failed = 0
            mig_instance.rows_examined = 0
            mig_instance.rows_modified = 0
            mig_instance.rows_skipped_idempotent = 0
            mig_instance.rows_skipped_excluded = 0
            mig_instance.__enter__ = MagicMock(return_value=mig_instance)
            mig_instance.__exit__ = MagicMock(return_value=False)
            mock_mig.return_value = mig_instance

            with patch(
                "scripts.backfill_experiment_id_archaeology._infer_experiment_id",
                return_value="exp-001",
            ):
                run(dry_run=True, confidence_threshold=0.8, attio=attio)

        mock_rec.assert_called_once()
        mock_mig.assert_called_once()

    def test_migration_run_receives_reclassification_run_id(self):
        """MigrationRunWriter is constructed with reclassification_run_id from
        the ReclassificationRunWriter."""
        attio = MagicMock()
        attio.query_list_entries.return_value = []
        attio.parse_entry.side_effect = lambda e: e
        attio._request.return_value = {"data": {"id": {"record_id": "run-001"}}}

        rec_run_id = "rec-run-id-correlated"

        with (
            patch(
                "scripts.backfill_experiment_id_archaeology.ReclassificationRunWriter"
            ) as mock_rec,
            patch(
                "scripts.backfill_experiment_id_archaeology.MigrationRunWriter"
            ) as mock_mig,
        ):
            rec_instance = MagicMock()
            rec_instance.run_id = rec_run_id
            rec_instance.__enter__ = MagicMock(return_value=rec_instance)
            rec_instance.__exit__ = MagicMock(return_value=False)
            mock_rec.return_value = rec_instance

            mig_instance = MagicMock()
            mig_instance.rows_failed = 0
            mig_instance.rows_examined = 0
            mig_instance.rows_modified = 0
            mig_instance.rows_skipped_idempotent = 0
            mig_instance.rows_skipped_excluded = 0
            mig_instance.__enter__ = MagicMock(return_value=mig_instance)
            mig_instance.__exit__ = MagicMock(return_value=False)
            mock_mig.return_value = mig_instance

            with patch(
                "scripts.backfill_experiment_id_archaeology._infer_experiment_id",
                return_value="exp-001",
            ):
                run(dry_run=True, confidence_threshold=0.8, attio=attio)

        # Verify MigrationRunWriter was called with reclassification_run_id
        mig_call_kwargs = mock_mig.call_args.kwargs
        assert mig_call_kwargs.get("reclassification_run_id") == rec_run_id, (
            "MigrationRunWriter must be constructed with the ReclassificationRunWriter's "
            "run_id so the two rows can be correlated for forensics."
        )


# ============================================================
# 10. cohort_archaeology_threshold operator decision queue row
# ============================================================


class TestThresholdDecisionRow:
    """Audit-spec B-DS-003 requires the script to open a 7-day decision queue
    row per run so operators can revise the 0.8 confidence threshold."""

    def test_open_threshold_decision_row_calls_escalate(self):
        """`_open_threshold_decision_row` calls escalate() with the canonical
        cohort_archaeology_threshold configuration_decision payload."""
        from scripts.backfill_experiment_id_archaeology import (
            _open_threshold_decision_row,
        )

        attio = MagicMock()

        with patch(
            "scripts.backfill_experiment_id_archaeology.escalate"
        ) as mock_escalate:
            _open_threshold_decision_row(threshold=0.8, attio=attio)

        mock_escalate.assert_called_once()
        kwargs = mock_escalate.call_args.kwargs
        assert kwargs["type"] == "configuration_decision"
        assert kwargs["decision_key"] == "cohort_archaeology_threshold"
        assert kwargs["payload"]["default_on_expiry"] == "0.8", (
            "B-DS-003 spec: default_on_expiry must be 0.8 (the canonical "
            "MEASUREMENT-INCLUDED/EXCLUDED split)."
        )
        # Confirm 7-day deadline horizon
        from datetime import date
        deadline = kwargs["deadline"]
        delta = (deadline - date.today()).days
        assert delta == 7, f"Audit spec: deadline must be 7 days out; got {delta}"

    def test_run_opens_threshold_decision_row(self):
        """Every run() call (dry-run or apply) opens the decision queue row."""
        attio = MagicMock()
        attio.query_list_entries.return_value = []
        attio.parse_entry.side_effect = lambda e: e
        attio._request.return_value = {"data": {"id": {"record_id": "run-001"}}}

        with (
            patch(
                "scripts.backfill_experiment_id_archaeology._open_threshold_decision_row"
            ) as mock_open,
            patch(
                "scripts.backfill_experiment_id_archaeology._infer_experiment_id",
                return_value="exp-001",
            ),
            patch(
                "scripts.backfill_experiment_id_archaeology.ReclassificationRunWriter"
            ) as mock_rec,
            patch(
                "scripts.backfill_experiment_id_archaeology.MigrationRunWriter"
            ) as mock_mig,
        ):
            for mock_writer in (mock_rec, mock_mig):
                inst = MagicMock()
                inst.run_id = "id"
                inst.rows_failed = 0
                inst.rows_examined = 0
                inst.rows_modified = 0
                inst.rows_skipped_idempotent = 0
                inst.rows_skipped_excluded = 0
                inst.__enter__ = MagicMock(return_value=inst)
                inst.__exit__ = MagicMock(return_value=False)
                mock_writer.return_value = inst

            run(dry_run=True, confidence_threshold=0.8, attio=attio)

        mock_open.assert_called_once()
        assert mock_open.call_args.kwargs["threshold"] == 0.8

    def test_threshold_open_failure_does_not_kill_run(self):
        """A transport failure on the queue-row open is logged as WARNING but
        the rest of the archaeology run still proceeds (the row is an audit
        observability concern, not a critical path)."""
        import httpx

        attio = MagicMock()
        attio.query_list_entries.return_value = []
        attio.parse_entry.side_effect = lambda e: e
        attio._request.return_value = {"data": {"id": {"record_id": "run-001"}}}

        with (
            patch(
                "scripts.backfill_experiment_id_archaeology._open_threshold_decision_row",
                side_effect=httpx.RequestError("escalate transport down"),
            ),
            patch(
                "scripts.backfill_experiment_id_archaeology._infer_experiment_id",
                return_value="exp-001",
            ),
        ):
            exit_code = run(
                dry_run=True,
                confidence_threshold=DEFAULT_CONFIDENCE_THRESHOLD,
                attio=attio,
            )
        assert exit_code == 0


# ============================================================
# 11. Boundary tests + cross-agent convergences
# ============================================================


class TestThresholdSplitBoundaries:
    """Additional boundary tests called out by pr-test-analyzer MED-6."""

    def test_classify_at_zero_threshold_always_included(self):
        """threshold=0.0 → every confidence >= threshold ⇒ INCLUDED."""
        from scripts.backfill_experiment_id_archaeology import _classify_row

        frozen_at, _ = _classify_row(
            _make_entry(),
            confidence=0.0,
            confidence_threshold=0.0,
            inferred_experiment_id="exp-001",
        )
        assert frozen_at == "legacy_inferred_by_archaeology"

    def test_classify_at_one_threshold_only_max_included(self):
        """threshold=1.0 → only max-confidence rows ⇒ INCLUDED; 0.99 → EXCLUDED."""
        from scripts.backfill_experiment_id_archaeology import _classify_row

        frozen_at_high, _ = _classify_row(
            _make_entry(),
            confidence=1.0,
            confidence_threshold=1.0,
            inferred_experiment_id="exp-001",
        )
        frozen_at_just_below, _ = _classify_row(
            _make_entry(),
            confidence=0.99,
            confidence_threshold=1.0,
            inferred_experiment_id="exp-001",
        )
        assert frozen_at_high == "legacy_inferred_by_archaeology"
        assert frozen_at_just_below == "legacy_pure_unknown"


class TestDedupTieBreak:
    """Tie-break determinism (advertiser-weekly + silent-failure + pr-test
    convergence — equal stage_rank must resolve deterministically)."""

    def _entry(self, *, url: str, stage: str, lcd: str, rid: str) -> dict:
        return {
            "canonical_linkedin_url": url,
            "stage": stage,
            "last_contact_date": lcd,
            "record_id": rid,
        }

    def test_equal_rank_prefers_later_last_contact_date(self):
        """Two entries with same stage_rank → keep the LATER last_contact_date."""
        from workflows.learn import _dedup_by_canonical_url

        url = "https://www.linkedin.com/in/eq"
        early = self._entry(url=url, stage="DM2 Sent", lcd="2026-01-01", rid="rec-AAA")
        late = self._entry(url=url, stage="DM2 Sent", lcd="2026-05-01", rid="rec-BBB")
        result = _dedup_by_canonical_url([early, late])
        assert len(result) == 1
        assert result[0]["last_contact_date"] == "2026-05-01"

    def test_equal_rank_and_lcd_breaks_on_record_id(self):
        """Identical stage_rank + last_contact_date → smaller record_id wins
        for full determinism."""
        from workflows.learn import _dedup_by_canonical_url

        url = "https://www.linkedin.com/in/eqid"
        a = self._entry(url=url, stage="DM1 Sent", lcd="2026-03-01", rid="rec-AAA")
        b = self._entry(url=url, stage="DM1 Sent", lcd="2026-03-01", rid="rec-BBB")
        result_ab = _dedup_by_canonical_url([a, b])
        result_ba = _dedup_by_canonical_url([b, a])
        assert len(result_ab) == 1
        assert len(result_ba) == 1
        # Same winner regardless of iteration order
        assert result_ab[0]["record_id"] == result_ba[0]["record_id"] == "rec-AAA"


class TestArchaeologyMeasurementSemantics:
    """Label-vs-behavior: `legacy_inferred_by_archaeology` row with NULL
    `dmN_sent_at` is INCLUDED-by-frozen_at-label but EXCLUDED-by-sent_at-gate
    (math + pr-test convergence)."""

    def test_legacy_inferred_with_null_sent_at_excluded_from_denominator(self):
        from workflows.learn import _is_denominator_excluded

        # frozen_at says INCLUDED; but dm1_sent_at is NULL — must still exclude.
        entry = {
            "experiment_id_frozen_at": "legacy_inferred_by_archaeology",
            "dm1_sent_at": None,
            "dm_step": 1,
        }
        assert _is_denominator_excluded(entry, 1) is True

    def test_legacy_inferred_with_populated_sent_at_included_in_denominator(self):
        from workflows.learn import _is_denominator_excluded

        entry = {
            "experiment_id_frozen_at": "legacy_inferred_by_archaeology",
            "dm1_sent_at": "2026-01-01T12:00:00Z",
            "dm_step": 1,
        }
        assert _is_denominator_excluded(entry, 1) is False


# ============================================================
# 12. weekly_brain archaeology disclosure
# ============================================================


class TestWeeklyBrainArchaeologyDisclosure:
    """Operators need to see archaeology-inferred counts alongside DM'd counts
    (advertiser-weekly + pr-test convergence)."""

    def test_format_cohort_summary_includes_archaeology_when_positive(self):
        from workflows.weekly_brain import _format_cohort_summary

        cohort = {
            "experiment_id": "exp-001",
            "dmed": 50,
            "responded": 5,
            "dm_response_rate": 0.10,
            "defensive_rate": 0.02,
            "is_mature": True,
            "days_running": 30,
            "archaeology_inferred_count": 40,
            "dm1_posterior": {"mean": 0.10, "n_observed": 50},
            "dm2_posterior": {"mean": 0.05, "n_observed": 30},
            "dm3_posterior": {"mean": 0.02, "n_observed": 10},
        }
        out = _format_cohort_summary(cohort)
        assert "Archaeology-inferred: 40" in out

    def test_format_cohort_summary_omits_archaeology_when_zero(self):
        from workflows.weekly_brain import _format_cohort_summary

        cohort = {
            "experiment_id": "exp-002",
            "dmed": 20,
            "responded": 2,
            "dm_response_rate": 0.10,
            "defensive_rate": 0.0,
            "is_mature": False,
            "days_running": 5,
            "archaeology_inferred_count": 0,
            "dm1_posterior": {"mean": 0.10, "n_observed": 20},
            "dm2_posterior": {"mean": 0.0, "n_observed": 0},
            "dm3_posterior": {"mean": 0.0, "n_observed": 0},
        }
        out = _format_cohort_summary(cohort)
        assert "Archaeology-inferred" not in out

    def test_measure_cohorts_emits_archaeology_inferred_count(self):
        """measure_cohorts() returns archaeology_inferred_count in every row."""
        from workflows.learn import measure_cohorts

        # Two rows: one archaeology, one live PROSPECT-commit
        entries = [
            {
                "record_id": "rec-arch",
                "experiment_id": "exp-001",
                "experiment_id_frozen_at": "legacy_inferred_by_archaeology",
                "stage": "DM1 Sent",
                "dm_step": 1,
                "dm1_sent_at": "2026-01-01T12:00:00Z",
                "merged_into": None,
            },
            {
                "record_id": "rec-live",
                "experiment_id": "exp-001",
                "experiment_id_frozen_at": "prospect",
                "stage": "DM1 Sent",
                "dm_step": 1,
                "dm1_sent_at": "2026-02-01T12:00:00Z",
                "merged_into": None,
            },
        ]
        attio = MagicMock()
        attio.query_list_entries.return_value = entries
        attio.parse_entry.side_effect = lambda e: e
        cohorts = measure_cohorts(attio)
        target = next(c for c in cohorts if c["experiment_id"] == "exp-001")
        assert target["archaeology_inferred_count"] == 1


# ============================================================
# 13. §3.1 invite-path leak guard
# ============================================================


class TestInferExperimentIdTiers:
    """Cover the 4 tiers of `_infer_experiment_id` (pr-test MED-9):
      1. exactly one running experiment → use it
      2. zero running + exactly one total → use it
      3. zero running + zero total → None
      4. multiple running OR (zero running + multiple total) → None
    """

    def _exp(self, eid: str):
        from types import SimpleNamespace
        return SimpleNamespace(experiment_id=eid)

    def test_single_running_experiment(self):
        from scripts.backfill_experiment_id_archaeology import _infer_experiment_id

        attio = MagicMock()
        with patch(
            "scripts.backfill_experiment_id_archaeology.get_active_experiments",
            return_value=[self._exp("exp-single-running")],
        ):
            result = _infer_experiment_id(attio)
        assert result == "exp-single-running"

    def test_zero_running_single_total_fallback(self):
        from scripts.backfill_experiment_id_archaeology import _infer_experiment_id

        attio = MagicMock()
        with (
            patch(
                "scripts.backfill_experiment_id_archaeology.get_active_experiments",
                return_value=[],
            ),
            patch(
                "scripts.backfill_experiment_id_archaeology.load_experiments",
                return_value=[self._exp("exp-only-historical")],
            ),
        ):
            result = _infer_experiment_id(attio)
        assert result == "exp-only-historical"

    def test_zero_running_zero_total_returns_none(self):
        from scripts.backfill_experiment_id_archaeology import _infer_experiment_id

        attio = MagicMock()
        with (
            patch(
                "scripts.backfill_experiment_id_archaeology.get_active_experiments",
                return_value=[],
            ),
            patch(
                "scripts.backfill_experiment_id_archaeology.load_experiments",
                return_value=[],
            ),
        ):
            result = _infer_experiment_id(attio)
        assert result is None

    def test_multiple_running_returns_none(self):
        from scripts.backfill_experiment_id_archaeology import _infer_experiment_id

        attio = MagicMock()
        with patch(
            "scripts.backfill_experiment_id_archaeology.get_active_experiments",
            return_value=[self._exp("exp-a"), self._exp("exp-b")],
        ):
            result = _infer_experiment_id(attio)
        assert result is None

    def test_zero_running_multiple_total_returns_none(self):
        from scripts.backfill_experiment_id_archaeology import _infer_experiment_id

        attio = MagicMock()
        with (
            patch(
                "scripts.backfill_experiment_id_archaeology.get_active_experiments",
                return_value=[],
            ),
            patch(
                "scripts.backfill_experiment_id_archaeology.load_experiments",
                return_value=[self._exp("a"), self._exp("b")],
            ),
        ):
            result = _infer_experiment_id(attio)
        assert result is None


class TestInvitePathArchaeologyLeak:
    """advertiser-daily BLOCKING fold-in: `run_connection_requests` must
    consult `is_send_eligible` so archaeology rows can't escape into the
    invite slice via a bare `stage == PROSPECT` filter."""

    def test_invite_path_skips_archaeology_pure_unknown(self):
        """A PROSPECT-stage row stamped legacy_pure_unknown must NOT appear in
        the invite candidate pool. Pre-fold-in the row would have leaked into
        `to_send_data` because `is_invite_eligible` only checks quarantine.
        """
        from datetime import date as _date
        from unittest.mock import patch as _patch

        from models.pipeline import is_send_eligible

        # Construct the archaeology row the way `run_connection_requests` would
        # see it after `_get_all_entries_parsed`.
        leak_row = {
            "record_id": "rec-leak",
            "stage": "Prospect",
            "quality_score": 80,
            "experiment_id_frozen_at": "legacy_pure_unknown",
            "invite_eligible_after": None,
            "scoring_lane": "target_company_mode",
            "created_at": "2026-01-01",
            "language": "es",
            "persona": "operations_leaders",
        }

        # Direct invariant test: is_send_eligible MUST reject the row so any
        # caller using it as a gate (the post-fold-in invite loop) drops the row.
        assert is_send_eligible(leak_row) is False

        # End-to-end: run_connection_requests should drop the row entirely.
        from workflows.daily_check import run_connection_requests

        attio = MagicMock()
        pb = MagicMock()

        with (
            _patch(
                "workflows.daily_check._get_all_entries_parsed",
                return_value=[leak_row],
            ),
            _patch(
                "workflows.daily_check.ensure_throttle_policy_decision_opened"
            ),
            _patch(
                "workflows.daily_check.can_send_connections", return_value=True
            ),
            _patch(
                "workflows.daily_check.RecordCache"
            ) as mock_cache_cls,
        ):
            cache = MagicMock()
            cache.get.return_value = (
                "Alice", "AcmeCo",
                "https://linkedin.com/in/alice", "Manufacturing", "VP Ops"
            )
            mock_cache_cls.return_value = cache

            result = run_connection_requests(
                attio=attio,
                pb=pb,
                network_booster_id="pb-id",
                batch_size=15,
                dry_run=True,
                auto_confirm=True,
                today=_date(2026, 5, 22),
                daily_run=fake_daily_run(),
            )

        # The archaeology row must not have been sent.
        assert result["sent"] == 0, (
            "archaeology-stamped PROSPECT leaked into the invite slice — "
            "§3.1 hard red line. is_send_eligible gate must run before "
            "appending to `prospects`."
        )
