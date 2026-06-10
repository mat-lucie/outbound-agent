"""Tests for scripts/unified_cluster_triage.py — cluster triage decision logic."""


from models.pipeline import PipelineStage
from scripts.unified_cluster_triage import decide_target_stage


class TestDecideTargetStage:
    """Test the decision logic for target DM stage selection."""

    def test_terminal_preserved_when_responded(self):
        """Terminal stage (RESPONDED) should never be downgraded."""
        target, reason = decide_target_stage(
            cluster_max_stage=PipelineStage.DM1_SENT.value,
            cluster_terminal_stage=PipelineStage.RESPONDED.value,
            pb_send_count=0,
            has_conflict=False,
            n_entries=2,
        )
        assert target == PipelineStage.RESPONDED.value
        assert reason == "terminal_preserved"

    def test_terminal_preserved_when_call_booked(self):
        """Terminal stage (CALL_BOOKED) should never be downgraded."""
        target, reason = decide_target_stage(
            cluster_max_stage=PipelineStage.DM2_SENT.value,
            cluster_terminal_stage=PipelineStage.CALL_BOOKED.value,
            pb_send_count=1,
            has_conflict=True,
            n_entries=3,
        )
        assert target == PipelineStage.CALL_BOOKED.value
        assert reason == "terminal_preserved"

    def test_terminal_preserved_when_qualified(self):
        """Terminal stage (QUALIFIED) should never be downgraded."""
        target, reason = decide_target_stage(
            cluster_max_stage=PipelineStage.PROSPECT.value,
            cluster_terminal_stage=PipelineStage.QUALIFIED.value,
            pb_send_count=0,
            has_conflict=False,
            n_entries=1,
        )
        assert target == PipelineStage.QUALIFIED.value
        assert reason == "terminal_preserved"

    def test_terminal_preserved_when_not_interested(self):
        """Terminal stage (NOT_INTERESTED) should never be downgraded."""
        target, reason = decide_target_stage(
            cluster_max_stage=PipelineStage.ACCEPTED.value,
            cluster_terminal_stage=PipelineStage.NOT_INTERESTED.value,
            pb_send_count=0,
            has_conflict=True,
            n_entries=2,
        )
        assert target == PipelineStage.NOT_INTERESTED.value
        assert reason == "terminal_preserved"

    def test_pb_catchup_from_prospect_when_any_send(self):
        """PB shows ≥1 send but Attio stuck at Prospect → advance to DM1_SENT."""
        target, reason = decide_target_stage(
            cluster_max_stage=PipelineStage.PROSPECT.value,
            cluster_terminal_stage=None,
            pb_send_count=1,
            has_conflict=False,
            n_entries=2,
        )
        assert target == PipelineStage.DM1_SENT.value
        assert reason == "pb_bug1_catchup"

    def test_pb_catchup_from_accepted_when_any_send(self):
        """Attio at Accepted + ≥1 PB send → advance to DM1_SENT (Bug 1 catchup)."""
        target, reason = decide_target_stage(
            cluster_max_stage=PipelineStage.ACCEPTED.value,
            cluster_terminal_stage=None,
            pb_send_count=1,
            has_conflict=False,
            n_entries=2,
        )
        assert target == PipelineStage.DM1_SENT.value
        assert reason == "pb_bug1_catchup"

    def test_pb_count_never_upgrades_past_attio_dm1(self):
        """3 PB sends cannot push a DM1_SENT record to DM3 — repeat sends
        inflate the count when the repeat-DM bug was active. Trust Attio."""
        target, reason = decide_target_stage(
            cluster_max_stage=PipelineStage.DM1_SENT.value,
            cluster_terminal_stage=None,
            pb_send_count=3,
            has_conflict=False,
            n_entries=1,
        )
        assert target == PipelineStage.DM1_SENT.value
        assert reason == "attio_max_stage"

    def test_pb_count_never_upgrades_past_attio_dm2(self):
        """3 PB sends on a DM2_SENT cluster stays at DM2_SENT — DM3 content
        hasn't been deployed; repeats inflate count."""
        target, reason = decide_target_stage(
            cluster_max_stage=PipelineStage.DM2_SENT.value,
            cluster_terminal_stage=None,
            pb_send_count=3,
            has_conflict=False,
            n_entries=1,
        )
        assert target == PipelineStage.DM2_SENT.value
        assert reason == "attio_max_stage"

    def test_attio_max_stage_when_no_pb_history(self):
        """Use Attio max stage when PB count is zero."""
        target, reason = decide_target_stage(
            cluster_max_stage=PipelineStage.ACCEPTED.value,
            cluster_terminal_stage=None,
            pb_send_count=0,
            has_conflict=False,
            n_entries=2,
        )
        assert target == PipelineStage.ACCEPTED.value
        assert reason == "attio_max_stage"

    def test_attio_max_stage_prospect(self):
        """Prospect stage from Attio should be preserved if no PB history."""
        target, reason = decide_target_stage(
            cluster_max_stage=PipelineStage.PROSPECT.value,
            cluster_terminal_stage=None,
            pb_send_count=0,
            has_conflict=False,
            n_entries=1,
        )
        assert target == PipelineStage.PROSPECT.value
        assert reason == "attio_max_stage"

    def test_attio_max_stage_connection_sent(self):
        """Connection Sent from Attio should be preserved if no PB history."""
        target, reason = decide_target_stage(
            cluster_max_stage=PipelineStage.CONNECTION_SENT.value,
            cluster_terminal_stage=None,
            pb_send_count=0,
            has_conflict=True,
            n_entries=2,
        )
        assert target == PipelineStage.CONNECTION_SENT.value
        assert reason == "attio_max_stage"

    def test_fallback_lost_large_cluster_with_conflict_prospect(self):
        """Large cluster (3+ entries) with conflict in Prospect → NOT_INTERESTED."""
        target, reason = decide_target_stage(
            cluster_max_stage=PipelineStage.PROSPECT.value,
            cluster_terminal_stage=None,
            pb_send_count=0,
            has_conflict=True,
            n_entries=3,
        )
        assert target == PipelineStage.NOT_INTERESTED.value
        assert reason == "fallback_lost"

    def test_fallback_not_triggered_small_cluster(self):
        """Small cluster (2 entries) with conflict should not trigger fallback."""
        target, reason = decide_target_stage(
            cluster_max_stage=PipelineStage.PROSPECT.value,
            cluster_terminal_stage=None,
            pb_send_count=0,
            has_conflict=True,
            n_entries=2,
        )
        # Should use attio_max_stage, not fallback_lost
        assert target == PipelineStage.PROSPECT.value
        assert reason == "attio_max_stage"

    def test_fallback_not_triggered_no_conflict(self):
        """Large cluster without conflict should not trigger fallback."""
        target, reason = decide_target_stage(
            cluster_max_stage=PipelineStage.PROSPECT.value,
            cluster_terminal_stage=None,
            pb_send_count=0,
            has_conflict=False,
            n_entries=4,
        )
        # Should use attio_max_stage, not fallback_lost
        assert target == PipelineStage.PROSPECT.value
        assert reason == "attio_max_stage"

    def test_fallback_not_triggered_has_pb_history(self):
        """Large cluster with conflict but with PB history should not trigger fallback."""
        target, reason = decide_target_stage(
            cluster_max_stage=PipelineStage.PROSPECT.value,
            cluster_terminal_stage=None,
            pb_send_count=1,
            has_conflict=True,
            n_entries=3,
        )
        # Should use pb_history, not fallback_lost
        assert target == PipelineStage.DM1_SENT.value
        assert reason == "pb_bug1_catchup"

    def test_fallback_not_triggered_not_prospect_stage(self):
        """Large cluster with conflict not in Prospect stage should not trigger fallback."""
        target, reason = decide_target_stage(
            cluster_max_stage=PipelineStage.ACCEPTED.value,
            cluster_terminal_stage=None,
            pb_send_count=0,
            has_conflict=True,
            n_entries=3,
        )
        # Should use attio_max_stage, not fallback_lost
        assert target == PipelineStage.ACCEPTED.value
        assert reason == "attio_max_stage"

    def test_fallback_lost_exact_boundary_3_entries(self):
        """Fallback should trigger at exactly 3 entries (n > 2)."""
        target, reason = decide_target_stage(
            cluster_max_stage=PipelineStage.PROSPECT.value,
            cluster_terminal_stage=None,
            pb_send_count=0,
            has_conflict=True,
            n_entries=3,
        )
        assert target == PipelineStage.NOT_INTERESTED.value
        assert reason == "fallback_lost"

    def test_fallback_not_triggered_exactly_2_entries(self):
        """Fallback should NOT trigger at exactly 2 entries (needs n > 2)."""
        target, reason = decide_target_stage(
            cluster_max_stage=PipelineStage.PROSPECT.value,
            cluster_terminal_stage=None,
            pb_send_count=0,
            has_conflict=True,
            n_entries=2,
        )
        assert target == PipelineStage.PROSPECT.value
        assert reason == "attio_max_stage"

    def test_decision_hierarchy_terminal_beats_all(self):
        """Terminal stage should beat PB history, Attio, and fallback."""
        target, reason = decide_target_stage(
            cluster_max_stage=PipelineStage.PROSPECT.value,
            cluster_terminal_stage=PipelineStage.RESPONDED.value,
            pb_send_count=0,
            has_conflict=True,
            n_entries=5,
        )
        assert target == PipelineStage.RESPONDED.value
        assert reason == "terminal_preserved"

    def test_decision_hierarchy_pb_beats_attio_and_fallback(self):
        """PB history should beat Attio max and fallback."""
        target, reason = decide_target_stage(
            cluster_max_stage=PipelineStage.PROSPECT.value,
            cluster_terminal_stage=None,
            pb_send_count=1,
            has_conflict=True,
            n_entries=5,
        )
        assert target == PipelineStage.DM1_SENT.value
        assert reason == "pb_bug1_catchup"

    def test_partner_intro_stage_preserved(self):
        """PARTNER_INTRO in terminal or max should be handled correctly."""
        # PARTNER_INTRO is in TERMINAL_STAGES
        target, reason = decide_target_stage(
            cluster_max_stage=PipelineStage.DM1_SENT.value,
            cluster_terminal_stage=PipelineStage.PARTNER_INTRO.value,
            pb_send_count=0,
            has_conflict=False,
            n_entries=2,
        )
        assert target == PipelineStage.PARTNER_INTRO.value
        assert reason == "terminal_preserved"
