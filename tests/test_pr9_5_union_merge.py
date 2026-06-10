"""PR-9.5 §3.11 union-merge dedup tests.

Coverage:
  - Triage doc parser produces 68 groups (45 auto + 23 review).
  - §3.11 union-merge rule per-attribute fixtures:
      * dm_step → MAX
      * last_contact_date → MAX (ISO string)
      * dmN_sent_at / response_received_at → MAX-non-null
      * stage → highest by canonical STAGE_RANK
      * response_classification → most-pessimistic-wins
      * suppress_re_engagement / had_connection_note → logical OR
      * experiment_id → winner if non-null; multiple distinct non-null
        escalate `dedup_experiment_id_conflict`.
  - Loser entries get `merged_into=winner_record_id`.
  - REVIEW-shape groups open `dedup_review` queue rows, do NOT merge.
  - Idempotency: re-run = no-op (losers already pointing at winner
    are skipped, no second writes).
  - MigrationRunWriter counters reflect examined/modified/skipped.
  - Writer registry agrees with manifest (scripts.attio_dedup is a
    declared writer for all union-merged attrs).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from scripts.attio_dedup import (
    _any_truthy,
    _experiment_id_conflict,
    _max_dm_step,
    _max_iso,
    _max_stage_by_rank,
    _pessimistic_response_classification,
    apply_union_merge_group,
    compute_union_merge_attrs,
    open_dedup_review_for_group,
    parse_triage_doc,
    run_union_merge,
)

if TYPE_CHECKING:
    from pathlib import Path

# ============================================================
# Triage doc parser
# ============================================================


class TestParseTriageDoc:
    def test_parses_68_groups(self):
        # The canonical triage doc lives next to the repo root; the parser
        # defaults to it. The doc is an anonymized snapshot (prospect slugs,
        # names, emails replaced) with structure and counts preserved —
        # PR-9.5 ships against this exact group/shape layout.
        groups = parse_triage_doc()
        assert len(groups) == 68, (
            f"Expected 68 conflict groups in triage doc, got {len(groups)}"
        )

    def test_split_is_45_auto_plus_23_review(self):
        groups = parse_triage_doc()
        auto = [g for g in groups if g["auto_mergeable"]]
        review = [g for g in groups if not g["auto_mergeable"]]
        assert len(auto) == 45
        assert len(review) == 23
        # Auto-mergeable shape is exclusively "entry.persona" per §3.11.
        assert all(g["conflict_shape"] == "entry.persona" for g in auto)

    def test_every_group_has_winner_and_losers(self):
        for g in parse_triage_doc():
            assert g["winner_id"], g
            assert g["loser_ids"], g
            for lid in g["loser_ids"]:
                assert lid and lid != g["winner_id"], (
                    f"loser_id {lid!r} equals winner_id or is empty for "
                    f"{g['canonical_linkedin_url']}"
                )

    def test_review_shapes_are_seven_distinct_kinds(self):
        review_shapes = {
            g["conflict_shape"]
            for g in parse_triage_doc()
            if not g["auto_mergeable"]
        }
        # Triage doc enumerates: company+entry.persona, company, email+name,
        # email, company+email, company+email+name, company+entry.persona+name.
        assert review_shapes == {
            "company+entry.persona",
            "company",
            "email+name",
            "email",
            "company+email",
            "company+email+name",
            "company+entry.persona+name",
        }

    def test_malformed_doc_raises(self, tmp_path: Path):
        bad_doc = tmp_path / "bad.md"
        bad_doc.write_text(
            "## Shape: `entry.persona` (1 groups)\n"
            "### https://www.linkedin.com/in/foo\n"
            "- **winner_id**: `abc`\n"
            # missing loser_ids
        )
        with pytest.raises(ValueError, match="malformed group"):
            parse_triage_doc(bad_doc)


# ============================================================
# §3.11 per-attribute union-merge primitives
# ============================================================


class TestMaxIso:
    def test_returns_max_of_iso_dates(self):
        assert _max_iso("2026-04-10", "2026-04-15", "2026-04-12") == "2026-04-15"

    def test_returns_max_of_iso_datetimes(self):
        assert _max_iso(
            "2026-04-15T10:00:00Z",
            "2026-04-15T12:30:00Z",
            "2026-04-15T09:00:00Z",
        ) == "2026-04-15T12:30:00Z"

    def test_none_values_ignored(self):
        assert _max_iso(None, "2026-04-15", None) == "2026-04-15"

    def test_all_none_returns_none(self):
        assert _max_iso(None, None, "") is None

    def test_no_args_returns_none(self):
        assert _max_iso() is None


class TestMaxDmStep:
    def test_max_of_dm0_dm1_dm2(self):
        assert _max_dm_step("dm0", "dm1", "dm2") == "dm2"

    def test_max_handles_dm3_correctly(self):
        # Lexicographic max would still produce "dm3" here, but the
        # implementation uses numeric extraction so this guards against
        # a future "dm10" regression.
        assert _max_dm_step("dm1", "dm3", "dm2") == "dm3"

    def test_dm10_beats_dm9_numerically(self):
        # Future-proofing: if the engine ever extends dm_step beyond dm3,
        # the script must NOT silently regress by picking dm9 over dm10.
        assert _max_dm_step("dm9", "dm10") == "dm10"

    def test_none_ignored(self):
        assert _max_dm_step(None, "dm1", None) == "dm1"

    def test_all_none(self):
        assert _max_dm_step(None, None) is None


class TestMaxStageByRank:
    def test_dm3_sent_beats_dm1_sent(self):
        assert _max_stage_by_rank("DM1 Sent", "DM3 Sent", "Accepted") == "DM3 Sent"

    def test_not_interested_beats_qualified(self):
        # NOT_INTERESTED is rank 200 (the hardest terminal — F-PR-1
        # rebumped from 100→200 specifically so backfills can't
        # monotonically override it). Qualified is rank 101.
        assert _max_stage_by_rank("Qualified", "Not Interested") == "Not Interested"

    def test_defensive_hold_beats_responded(self):
        # Defensive replies route to DEFENSIVE_HOLD (95) NOT RESPONDED
        # (90); the union-merge must respect that or it weakens §3.6.
        assert _max_stage_by_rank("Responded", "Defensive Hold") == "Defensive Hold"

    def test_unknown_stage_skipped(self):
        # Unknown stage strings rank -1; they should NEVER beat a known
        # stage. The merge falls back to the highest known.
        assert _max_stage_by_rank("Made Up Stage", "DM2 Sent") == "DM2 Sent"

    def test_none_ignored(self):
        assert _max_stage_by_rank(None, "DM1 Sent", "", None) == "DM1 Sent"


class TestPessimisticResponseClassification:
    def test_defensive_beats_negative(self):
        # §3.11: defensive > negative > positive > null. A row that
        # ever said "defensive" must stay defensive through any merge.
        assert _pessimistic_response_classification("negative", "defensive") == "defensive"

    def test_defensive_beats_positive(self):
        assert _pessimistic_response_classification("positive", "defensive") == "defensive"

    def test_negative_beats_positive(self):
        assert _pessimistic_response_classification("positive", "negative") == "negative"

    def test_null_loses_to_defensive(self):
        # Specific case from PR-9.5 gotchas: winner with null and a loser
        # with `defensive` → winner must inherit `defensive`.
        assert _pessimistic_response_classification(None, "defensive") == "defensive"

    def test_all_null_returns_none(self):
        assert _pessimistic_response_classification(None, None, "") is None

    def test_case_insensitive(self):
        assert _pessimistic_response_classification("DEFENSIVE", "negative") == "defensive"


class TestAnyTruthy:
    def test_any_true_wins(self):
        assert _any_truthy(False, True, False) is True

    def test_all_false(self):
        assert _any_truthy(False, False) is False

    def test_all_none_returns_none(self):
        # Distinguishes "no data" from "all explicitly False" — caller
        # uses this to decide whether to skip the write entirely.
        assert _any_truthy(None, None) is None

    def test_string_true_counts(self):
        assert _any_truthy("true", False) is True
        assert _any_truthy("yes", None) is True

    def test_none_then_true(self):
        assert _any_truthy(None, True) is True


class TestExperimentIdConflict:
    def test_zero_non_null(self):
        chosen, frozen, distinct = _experiment_id_conflict((None, None), ("", None))
        assert chosen is None and frozen is None and distinct == []

    def test_single_non_null(self):
        chosen, frozen, distinct = _experiment_id_conflict(
            ("exp-abc", "prospect"),
            (None, None),
        )
        assert chosen == "exp-abc"
        assert frozen == "prospect"
        assert distinct == ["exp-abc"]

    def test_same_id_across_duplicates_no_conflict(self):
        chosen, frozen, distinct = _experiment_id_conflict(
            ("exp-abc", "prospect"),
            ("exp-abc", "accepted"),
        )
        assert chosen == "exp-abc"
        # Highest-confidence frozen_at wins (prospect=0 < accepted=2 per
        # `_FROZEN_AT_CONFIDENCE`), NOT first-observed. With swapped order
        # the result is still `prospect` — see test below.
        assert frozen == "prospect"
        assert distinct == ["exp-abc"]

    def test_two_distinct_ids_signals_conflict(self):
        chosen, frozen, distinct = _experiment_id_conflict(
            ("exp-abc", "prospect"),
            ("exp-xyz", "prospect"),
        )
        assert chosen is None
        assert frozen is None
        assert set(distinct) == {"exp-abc", "exp-xyz"}

    def test_same_id_higher_confidence_frozen_at_wins(self):
        """§3.10 + §3.1 protection: when duplicates share an experiment_id
        but disagree on frozen_at, the highest-confidence label wins —
        a `prospect`-committed stamp must NEVER be weakened by a
        `legacy_pure_unknown` archaeology stamp during the union-merge.
        """
        chosen, frozen, distinct = _experiment_id_conflict(
            ("exp-abc", "legacy_pure_unknown"),  # winner — lower confidence
            ("exp-abc", "prospect"),             # loser — higher confidence
        )
        assert chosen == "exp-abc"
        assert frozen == "prospect"
        assert distinct == ["exp-abc"]

    def test_same_id_confidence_picks_archaeology_over_pure_unknown(self):
        # legacy_inferred_by_archaeology (≥0.8 confidence) wins over
        # legacy_pure_unknown (<0.8).
        chosen, frozen, distinct = _experiment_id_conflict(
            ("exp-abc", "legacy_pure_unknown"),
            ("exp-abc", "legacy_inferred_by_archaeology"),
        )
        assert frozen == "legacy_inferred_by_archaeology"


# ============================================================
# compute_union_merge_attrs — composite §3.11 rule
# ============================================================


def _attrs(**kwargs):
    """Helper to build a parse_entry-shaped dict with overrides."""
    base = {
        "entry_id": kwargs.get("entry_id", "ent-1"),
        "record_id": kwargs.get("record_id", "rec-1"),
        "stage": None,
        "dm_step": None,
        "last_contact_date": None,
        "dm1_sent_at": None,
        "dm2_sent_at": None,
        "dm3_sent_at": None,
        "response_received_at": None,
        "experiment_id": None,
        "experiment_id_frozen_at": None,
        "response_classification": None,
        "suppress_re_engagement": None,
        "had_connection_note": None,
        "merged_into": None,
    }
    base.update(kwargs)
    return base


class TestComputeUnionMergeAttrs:
    def test_winner_already_unionized_returns_empty_delta(self):
        winner = _attrs(
            stage="DM3 Sent",
            dm_step="dm3",
            last_contact_date="2026-04-20",
            experiment_id="exp-abc",
            experiment_id_frozen_at="prospect",
        )
        loser = _attrs(
            stage="DM1 Sent",
            dm_step="dm1",
            last_contact_date="2026-04-10",
            experiment_id="exp-abc",
            experiment_id_frozen_at="prospect",
        )
        delta = compute_union_merge_attrs(winner, [loser])
        # Winner already has the MAX in every attribute — re-applying the
        # merge is a no-op. This is the §9.4 idempotency contract.
        assert delta == {}

    def test_loser_carries_higher_dm_step_and_lcd(self):
        winner = _attrs(stage="DM1 Sent", dm_step="dm1", last_contact_date="2026-04-10")
        loser = _attrs(stage="DM3 Sent", dm_step="dm3", last_contact_date="2026-04-20")
        delta = compute_union_merge_attrs(winner, [loser])
        assert delta["stage"] == "DM3 Sent"
        assert delta["dm_step"] == "dm3"
        assert delta["last_contact_date"] == "2026-04-20"

    def test_dmN_sent_at_max_non_null(self):
        winner = _attrs(dm1_sent_at=None, dm2_sent_at="2026-04-10T10:00:00Z")
        loser_a = _attrs(dm1_sent_at="2026-04-05T10:00:00Z", dm2_sent_at="2026-04-08T10:00:00Z")
        loser_b = _attrs(dm1_sent_at="2026-04-09T10:00:00Z", dm2_sent_at=None)
        delta = compute_union_merge_attrs(winner, [loser_a, loser_b])
        # Winner had no dm1_sent_at; loser_b's 2026-04-09 wins.
        assert delta["dm1_sent_at"] == "2026-04-09T10:00:00Z"
        # Winner already had MAX dm2_sent_at; nothing to write.
        assert "dm2_sent_at" not in delta

    def test_response_classification_most_pessimistic_wins(self):
        winner = _attrs(response_classification=None)
        loser = _attrs(response_classification="defensive")
        delta = compute_union_merge_attrs(winner, [loser])
        assert delta["response_classification"] == "defensive"

    def test_suppress_re_engagement_or_merge(self):
        winner = _attrs(suppress_re_engagement=False)
        loser = _attrs(suppress_re_engagement=True)
        delta = compute_union_merge_attrs(winner, [loser])
        assert delta["suppress_re_engagement"] is True

    def test_suppress_re_engagement_not_weakened_by_false(self):
        # Winner True + loser False → winner stays True; no write.
        winner = _attrs(suppress_re_engagement=True)
        loser = _attrs(suppress_re_engagement=False)
        delta = compute_union_merge_attrs(winner, [loser])
        assert "suppress_re_engagement" not in delta

    def test_had_connection_note_or_merge(self):
        winner = _attrs(had_connection_note=False)
        loser = _attrs(had_connection_note=True)
        delta = compute_union_merge_attrs(winner, [loser])
        assert delta["had_connection_note"] is True

    def test_experiment_id_inherits_from_loser_when_winner_null(self):
        winner = _attrs(experiment_id=None, experiment_id_frozen_at=None)
        loser = _attrs(experiment_id="exp-abc", experiment_id_frozen_at="prospect")
        delta = compute_union_merge_attrs(winner, [loser])
        assert delta["experiment_id"] == "exp-abc"
        assert delta["experiment_id_frozen_at"] == "prospect"

    def test_experiment_id_conflict_signals_escalation(self):
        winner = _attrs(experiment_id="exp-abc")
        loser = _attrs(experiment_id="exp-xyz")
        delta = compute_union_merge_attrs(winner, [loser])
        assert "_experiment_id_conflict_ids" in delta
        assert set(delta["_experiment_id_conflict_ids"]) == {"exp-abc", "exp-xyz"}
        # No experiment_id should be in the delta — the group must escalate.
        assert "experiment_id" not in delta

    def test_no_conflict_when_winner_has_value_loser_null(self):
        winner = _attrs(experiment_id="exp-abc", experiment_id_frozen_at="prospect")
        loser = _attrs(experiment_id=None)
        delta = compute_union_merge_attrs(winner, [loser])
        # Winner already has the value; no write needed.
        assert "experiment_id" not in delta
        assert "_experiment_id_conflict_ids" not in delta


# ============================================================
# apply_union_merge_group — Attio-touching code with mocks
# ============================================================


def _entry(entry_id: str, record_id: str, **values):
    """Build an Attio list-entry stub for tests."""
    entry_values: dict = {}
    if "stage" in values:
        entry_values["stage"] = [{"status": {"title": values["stage"]}}]
    for key in (
        "dm_step",
        "last_contact_date",
        "dm1_sent_at",
        "dm2_sent_at",
        "dm3_sent_at",
        "response_received_at",
        "experiment_id",
        "experiment_id_frozen_at",
        "response_classification",
        "persona",
        "language",
    ):
        if key in values:
            entry_values[key] = [{"value": values[key]}]
    if "suppress_re_engagement" in values:
        entry_values["suppress_re_engagement"] = [{"value": values["suppress_re_engagement"]}]
    if "had_connection_note" in values:
        entry_values["had_connection_note"] = [{"value": values["had_connection_note"]}]
    if "merged_into" in values and values["merged_into"] is not None:
        entry_values["merged_into"] = [{
            "target_object": "people",
            "target_record_id": values["merged_into"],
        }]
    return {
        "id": {"entry_id": entry_id, "record_id": record_id},
        "entry_id": entry_id,
        "parent_record_id": record_id,
        "entry_values": entry_values,
    }


class TestApplyUnionMergeGroup:
    def _mk_writer(self):
        w = MagicMock()
        w.examine = MagicMock()
        w.skip_idempotent = MagicMock()
        w.mark_modified = MagicMock()
        w.mark_failed = MagicMock()
        return w

    def test_winner_with_no_entry_marks_losers_failed(self, tmp_path):
        attio = MagicMock()
        attio._find_list_entries_for_record.return_value = []
        writer = self._mk_writer()
        log_fh = (tmp_path / "log.jsonl").open("w")

        group = {
            "canonical_linkedin_url": "https://linkedin.com/in/foo",
            "winner_id": "win-1",
            "loser_ids": ["lose-1", "lose-2"],
            "conflict_shape": "entry.persona",
            "auto_mergeable": True,
        }
        result = apply_union_merge_group(attio, group, "list-1", writer, log_fh, pretend=False)
        log_fh.close()

        assert result["errors"], "winner-missing must surface as an error"
        # One mark_failed for the winner + one per loser (2) = 3 total.
        assert writer.mark_failed.call_count == 3
        attio.update_list_entry.assert_not_called()

    def test_auto_merge_writes_delta_and_merged_into(self, tmp_path):
        attio = MagicMock()
        winner_entry = _entry("win-ent", "win-1", stage="DM1 Sent", dm_step="dm1",
                              last_contact_date="2026-04-10")
        loser_entry = _entry("lose-ent", "lose-1", stage="DM3 Sent", dm_step="dm3",
                             last_contact_date="2026-04-20")

        def find_entries(rid, lid):
            if rid == "win-1":
                return [winner_entry]
            if rid == "lose-1":
                return [loser_entry]
            return []

        attio._find_list_entries_for_record.side_effect = find_entries
        writer = self._mk_writer()
        log_fh = (tmp_path / "log.jsonl").open("w")

        group = {
            "canonical_linkedin_url": "https://linkedin.com/in/foo",
            "winner_id": "win-1",
            "loser_ids": ["lose-1"],
            "conflict_shape": "entry.persona",
            "auto_mergeable": True,
        }
        result = apply_union_merge_group(attio, group, "list-1", writer, log_fh, pretend=False)
        log_fh.close()

        # Two PATCHes expected: winner delta + loser merged_into.
        assert attio.update_list_entry.call_count == 2
        winner_patch = attio.update_list_entry.call_args_list[0]
        loser_patch = attio.update_list_entry.call_args_list[1]

        # Winner patch carries MAX dm_step + stage + last_contact_date.
        winner_attrs = winner_patch.kwargs["entry_attributes"]
        assert winner_attrs.get("dm_step") == "dm3"
        assert winner_attrs.get("stage") == "DM3 Sent"
        assert winner_attrs.get("last_contact_date") == "2026-04-20"

        # Loser patch sets merged_into → winner_id record reference.
        loser_attrs = loser_patch.kwargs["entry_attributes"]
        assert loser_attrs == {
            "merged_into": {
                "target_object": "people",
                "target_record_id": "win-1",
            }
        }
        # No errors and writer counters captured one winner + one loser mod.
        assert result["errors"] == []
        assert writer.mark_modified.call_count == 2

    def test_idempotent_re_run_skips_loser_already_pointing_at_winner(self, tmp_path):
        attio = MagicMock()
        winner_entry = _entry("win-ent", "win-1", stage="DM3 Sent", dm_step="dm3",
                              last_contact_date="2026-04-20")
        # Loser already carries merged_into → winner; second run = no-op.
        loser_entry = _entry("lose-ent", "lose-1", stage="DM3 Sent", dm_step="dm3",
                             last_contact_date="2026-04-20", merged_into="win-1")

        def find_entries(rid, lid):
            return {"win-1": [winner_entry], "lose-1": [loser_entry]}.get(rid, [])

        attio._find_list_entries_for_record.side_effect = find_entries
        writer = self._mk_writer()
        log_fh = (tmp_path / "log.jsonl").open("w")

        group = {
            "canonical_linkedin_url": "https://linkedin.com/in/foo",
            "winner_id": "win-1",
            "loser_ids": ["lose-1"],
            "conflict_shape": "entry.persona",
            "auto_mergeable": True,
        }
        apply_union_merge_group(attio, group, "list-1", writer, log_fh, pretend=False)
        log_fh.close()

        # Winner already unionized + loser already pointing at winner →
        # zero update_list_entry calls (the §9.4 idempotency contract).
        attio.update_list_entry.assert_not_called()
        assert writer.skip_idempotent.called
        writer.mark_modified.assert_not_called()

    def test_experiment_id_conflict_escalates_and_skips_merge(self, tmp_path):
        attio = MagicMock()
        winner_entry = _entry("win-ent", "win-1", experiment_id="exp-abc",
                              experiment_id_frozen_at="prospect")
        loser_entry = _entry("lose-ent", "lose-1", experiment_id="exp-xyz",
                             experiment_id_frozen_at="prospect")

        def find_entries(rid, lid):
            return {"win-1": [winner_entry], "lose-1": [loser_entry]}.get(rid, [])

        attio._find_list_entries_for_record.side_effect = find_entries
        writer = self._mk_writer()
        log_fh = (tmp_path / "log.jsonl").open("w")

        group = {
            "canonical_linkedin_url": "https://linkedin.com/in/foo",
            "winner_id": "win-1",
            "loser_ids": ["lose-1"],
            "conflict_shape": "entry.persona",
            "auto_mergeable": True,
        }
        # Patch where `escalate` is *used*, not where it lives — PR-9.5
        # fold-in hoisted the import to the top of scripts.attio_dedup so
        # the bound name lives on that module.
        with patch("scripts.attio_dedup.escalate") as mock_escalate:
            mock_escalate.return_value = {"id": "queue-row"}
            result = apply_union_merge_group(
                attio, group, "list-1", writer, log_fh, pretend=False
            )
        log_fh.close()

        # Escalation fired with the conflicting experiment_ids.
        mock_escalate.assert_called_once()
        kwargs = mock_escalate.call_args.kwargs
        assert kwargs["type"] == "dedup_experiment_id_conflict"
        assert set(kwargs["payload"]["experiment_ids"]) == {"exp-abc", "exp-xyz"}
        # conflict_shape is now mirrored from DedupReviewPayload.
        assert kwargs["payload"]["conflict_shape"] == "entry.persona"

        # NO list-entry writes — the group is parked.
        attio.update_list_entry.assert_not_called()
        # Result records the escalation in actions.
        assert any(a["op"] == "escalate_experiment_id_conflict" for a in result["actions"])

    def test_loser_with_multiple_entries_all_get_merged_into(self, tmp_path):
        """A single person record can have multiple list entries (legacy
        duplicates within the list). All of them must get merged_into
        stamped so the cross-channel suppression chain stays intact —
        a half-stamped loser is a §3.1 risk because a consumer scanning
        the un-stamped entry would re-process the prospect."""
        attio = MagicMock()
        winner_entry = _entry("win-ent", "win-1", stage="DM3 Sent", dm_step="dm3",
                              last_contact_date="2026-04-20")
        # Two distinct list entries for the same loser record_id.
        loser_entry_a = _entry("lose-ent-a", "lose-1", stage="DM1 Sent",
                               dm_step="dm1", last_contact_date="2026-04-05")
        loser_entry_b = _entry("lose-ent-b", "lose-1", stage="DM2 Sent",
                               dm_step="dm2", last_contact_date="2026-04-10")

        def find_entries(rid, lid):
            return {
                "win-1": [winner_entry],
                "lose-1": [loser_entry_a, loser_entry_b],
            }.get(rid, [])

        attio._find_list_entries_for_record.side_effect = find_entries
        writer = self._mk_writer()
        log_fh = (tmp_path / "log.jsonl").open("w")

        group = {
            "canonical_linkedin_url": "https://linkedin.com/in/foo",
            "winner_id": "win-1",
            "loser_ids": ["lose-1"],
            "conflict_shape": "entry.persona",
            "auto_mergeable": True,
        }
        apply_union_merge_group(attio, group, "list-1", writer, log_fh, pretend=False)
        log_fh.close()

        # One PATCH for the winner delta (winner is already at MAX, but
        # last_contact_date may differ if loser_b is later — it's NOT
        # in this fixture; the winner is at MAX dm3/2026-04-20 already.
        # So 0 winner patches + 2 loser-entry merged_into patches = 2.
        # We assert at minimum the two loser entries are stamped.
        loser_patches = [
            c for c in attio.update_list_entry.call_args_list
            if c.kwargs.get("entry_attributes", {}).get("merged_into") is not None
        ]
        assert len(loser_patches) == 2, (
            f"Expected 2 merged_into PATCHes (one per loser entry), got "
            f"{len(loser_patches)}: {loser_patches}"
        )
        # Both PATCHes point at the winner.
        for c in loser_patches:
            mi = c.kwargs["entry_attributes"]["merged_into"]
            assert mi["target_record_id"] == "win-1"

    def test_pretend_mode_skips_attio_writes(self, tmp_path):
        attio = MagicMock()
        winner_entry = _entry("win-ent", "win-1", stage="DM1 Sent", dm_step="dm1",
                              last_contact_date="2026-04-10")
        loser_entry = _entry("lose-ent", "lose-1", stage="DM3 Sent", dm_step="dm3",
                             last_contact_date="2026-04-20")

        def find_entries(rid, lid):
            return {"win-1": [winner_entry], "lose-1": [loser_entry]}.get(rid, [])

        attio._find_list_entries_for_record.side_effect = find_entries
        writer = self._mk_writer()
        log_fh = (tmp_path / "log.jsonl").open("w")

        group = {
            "canonical_linkedin_url": "https://linkedin.com/in/foo",
            "winner_id": "win-1",
            "loser_ids": ["lose-1"],
            "conflict_shape": "entry.persona",
            "auto_mergeable": True,
        }
        result = apply_union_merge_group(attio, group, "list-1", writer, log_fh, pretend=True)
        log_fh.close()

        # No writes despite the delta being non-empty + loser un-merged.
        attio.update_list_entry.assert_not_called()
        # Actions still recorded so dry-run logs are meaningful.
        ops = {a["op"] for a in result["actions"]}
        assert "patch_winner_entry" in ops
        assert "stamp_merged_into" in ops


# ============================================================
# open_dedup_review_for_group — REVIEW path
# ============================================================


class TestOpenDedupReviewForGroup:
    def test_pretend_returns_stub(self):
        group = {
            "canonical_linkedin_url": "https://linkedin.com/in/foo",
            "winner_id": "win-1",
            "loser_ids": ["lose-1", "lose-2"],
            "conflict_shape": "company+entry.persona",
            "auto_mergeable": False,
        }
        stub = open_dedup_review_for_group(attio=None, group=group, pretend=True)
        assert stub["pretend"] is True
        assert stub["type"] == "dedup_review"
        assert stub["payload"]["auto_mergeable"] is False
        assert stub["payload"]["conflict_shape"] == "company+entry.persona"
        assert stub["payload"]["record_ids"] == ["win-1", "lose-1", "lose-2"]

    def test_live_calls_escalate_with_typed_payload(self):
        attio = MagicMock()
        group = {
            "canonical_linkedin_url": "https://linkedin.com/in/foo",
            "winner_id": "win-1",
            "loser_ids": ["lose-1"],
            "conflict_shape": "email",
            "auto_mergeable": False,
        }
        # Patch where `escalate` is bound (post-hoist).
        with patch("scripts.attio_dedup.escalate") as mock_escalate:
            mock_escalate.return_value = {"id": "queue-row"}
            open_dedup_review_for_group(attio, group, pretend=False)
        mock_escalate.assert_called_once()
        kwargs = mock_escalate.call_args.kwargs
        assert kwargs["type"] == "dedup_review"
        assert kwargs["idempotency_key"] == "dedup-review-https://linkedin.com/in/foo"
        assert kwargs["payload"]["canonical_linkedin_url"] == "https://linkedin.com/in/foo"
        assert kwargs["payload"]["auto_mergeable"] is False


# ============================================================
# Writer registry: scripts.attio_dedup is a declared writer
# ============================================================


class TestWriterRegistry:
    """§3.15: scripts.attio_dedup is the SOLE writer for merged_into and
    a registered MULTI-writer for every other §3.11 union-merge target.
    Without these registrations F-PR-4's AttioWriter would reject the
    PATCH at the §3.15 gate with UnauthorizedAttioWriteError.
    """

    def test_merged_into_sole_writer(self):
        from clients.attio_writer_registry import get_authorized_writers
        owners = get_authorized_writers("linkedin_outreach", "merged_into")
        assert owners == ["scripts.attio_dedup"]

    @pytest.mark.parametrize("attr", [
        "dm_step",
        "stage",
        "last_contact_date",
        "dm1_sent_at",
        "dm2_sent_at",
        "dm3_sent_at",
        "response_received_at",
        "experiment_id",
        "experiment_id_frozen_at",
        "response_classification",
        "suppress_re_engagement",
        "had_connection_note",
    ])
    def test_union_merge_target_registers_dedup_writer(self, attr):
        from clients.attio_writer_registry import is_authorized_writer
        assert is_authorized_writer(
            "linkedin_outreach", attr, "scripts.attio_dedup"
        ), (
            f"scripts.attio_dedup must be registered as a writer for "
            f"linkedin_outreach.{attr} per §3.11 union-merge."
        )


# ============================================================
# Escalation TypedDicts wired correctly
# ============================================================


# ============================================================
# run_union_merge — top-level orchestrator end-to-end
# ============================================================


class TestRunUnionMerge:
    """Exercise the full pipeline against the canonical 68-group triage
    doc in pretend mode. The MigrationRunWriter is patched to avoid an
    Attio dependency; the test asserts counter math, run_id propagation,
    and that the 23 review groups + 45 auto groups are walked.
    """

    def test_pretend_run_counts_45_auto_and_23_review(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock as _Mock

        log_path = tmp_path / "union_merge.jsonl"

        # Mock MigrationRunWriter to a stub so we don't try to talk
        # to Attio in pretend mode (the writer's __enter__ instantiates
        # an AttioClient otherwise).
        mock_writer_cls = _Mock()
        writer_instance = _Mock()
        writer_instance.run_id = "test-run-1234"
        writer_instance.rows_examined = 0
        writer_instance.rows_modified = 0
        writer_instance.rows_skipped_idempotent = 0
        writer_instance.rows_failed = 0
        mock_writer_cls.return_value.__enter__.return_value = writer_instance
        mock_writer_cls.return_value.__exit__.return_value = False
        monkeypatch.setattr(
            "workflows.migration_run_writer.MigrationRunWriter",
            mock_writer_cls,
        )

        # Mock the escalation path so dedup_review queue rows don't try
        # to hit Attio. Patch the bound name in scripts.attio_dedup (post-
        # fold-in hoist). Each REVIEW group should call escalate() once.
        mock_escalate = _Mock(return_value={"id": "queue-row"})
        monkeypatch.setattr("scripts.attio_dedup.escalate", mock_escalate)

        totals = run_union_merge(
            list_id="test-list-id",
            pretend=True,
            log_path=str(log_path),
        )

        # Totals reflect the canonical 68-group split.
        assert totals["groups_total"] == 68
        assert totals["auto_groups_total"] == 45
        assert totals["review_groups_total"] == 23
        # In pretend mode with no live Attio, auto groups are walked-then-
        # skipped (cannot fetch entries). The orchestrator counts them as
        # skipped so the operator can see what would have been touched.
        assert totals["auto_groups_skipped"] == 45
        # In pretend mode escalate is still called (the queue-row stub
        # path returns without touching Attio). 23 review groups + 0
        # auto-group escalations.
        assert totals["review_queue_rows_opened"] == 23
        assert totals["experiment_id_conflict_escalations"] == 0
        assert totals["errors"] == 0

        # MigrationRunWriter must have been entered once with the right
        # script_name + dry_run=True.
        mock_writer_cls.assert_called_once()
        kwargs = mock_writer_cls.call_args.kwargs
        assert kwargs["script_name"] == "scripts.attio_dedup.union_merge"
        assert kwargs["dry_run"] is True

        # Log file contains start + per-group + end records.
        log_lines = log_path.read_text().splitlines()
        assert any('"kind": "union_merge_run_start"' in line for line in log_lines)
        assert any('"kind": "union_merge_run_end"' in line for line in log_lines)
        # 23 dedup_review_opened lines.
        review_lines = [
            line for line in log_lines
            if '"kind": "dedup_review_opened"' in line
        ]
        assert len(review_lines) == 23


class TestEscalationSchemas:
    def test_dedup_review_typeddict_registered(self):
        from workflows.escalation_schemas import (
            ESCALATION_SCHEMAS,
            DedupReviewPayload,
        )
        assert ESCALATION_SCHEMAS["dedup_review"] is DedupReviewPayload

    def test_dedup_experiment_id_conflict_typeddict_registered(self):
        from workflows.escalation_schemas import (
            ESCALATION_SCHEMAS,
            DedupExperimentIdConflictPayload,
        )
        assert ESCALATION_SCHEMAS["dedup_experiment_id_conflict"] is DedupExperimentIdConflictPayload

    def test_both_slugs_in_escalation_types(self):
        from workflows.escalation_schemas import ESCALATION_TYPES_SET
        assert "dedup_review" in ESCALATION_TYPES_SET
        assert "dedup_experiment_id_conflict" in ESCALATION_TYPES_SET


# ============================================================
# Fold-in coverage: 7-agent QA convergence
# ============================================================


class TestUnknownResponseClassificationRank:
    """data-steward + code-reviewer convergence — unknown response_
    classification labels must rank BELOW null. Previously the `.get(s, 0)`
    default of 0 beat `best_rank=-1` (null winner), so an unknown loser
    string like `"neutral"` would silently overwrite a null winner. The
    fold-in moves unknown labels to `-2` so null wins.
    """

    def test_winner_null_with_unknown_loser_stays_null(self):
        # Pre-fix: unknown loser would overwrite null winner.
        # Post-fix: unknown ranks (-2) below null (-1) → winner stays null.
        result = _pessimistic_response_classification(None, "neutral")
        assert result is None, (
            "Unknown response_classification labels must NOT overwrite a "
            "null winner — they rank below null per the fold-in fix."
        )

    def test_unknown_label_does_not_beat_known(self):
        # A known label always wins over an unknown one.
        result = _pessimistic_response_classification("neutral", "defensive")
        assert result == "defensive"

    def test_only_unknowns_returns_none(self):
        # No known labels and no null observed → None.
        result = _pessimistic_response_classification("foo", "bar")
        assert result is None


class TestFrozenAtConfidenceMatchesYamlEnum:
    """The `_FROZEN_AT_CONFIDENCE` ordering in attio_dedup.py is the
    confidence ranking PR-9.5 uses to pick the highest-confidence
    `experiment_id_frozen_at` during union-merge. It MUST contain
    exactly the same enum members as `docs/attio_schema_deltas.yaml`
    declares for the `experiment_id_frozen_at` select — otherwise a
    legitimate frozen_at value would rank as "unknown" and a higher-
    confidence merge could lose to a legacy stamp.
    """

    def test_confidence_tuple_matches_yaml_enum(self):
        from pathlib import Path

        import yaml  # PyYAML is a runtime dep of validate_attio_schema_deltas

        from scripts.attio_dedup import _FROZEN_AT_CONFIDENCE

        yaml_path = (
            Path(__file__).resolve().parent.parent
            / "docs"
            / "attio_schema_deltas.yaml"
        )
        spec = yaml.safe_load(yaml_path.read_text())
        # Find the experiment_id_frozen_at entry.
        entries = spec.get("attributes", spec) if isinstance(spec, dict) else spec
        if isinstance(entries, dict):
            entries = entries.get("attributes", []) or []
        match = next(
            (
                e for e in entries
                if e.get("slug") == "experiment_id_frozen_at"
                and e.get("object") == "linkedin_outreach"
            ),
            None,
        )
        assert match is not None, (
            "experiment_id_frozen_at entry missing from attio_schema_deltas.yaml"
        )
        yaml_options = set(match.get("options", []))
        confidence_set = set(_FROZEN_AT_CONFIDENCE)
        assert yaml_options == confidence_set, (
            f"YAML enum {yaml_options!r} does not match "
            f"_FROZEN_AT_CONFIDENCE {confidence_set!r} — the confidence "
            f"ranking would treat real frozen_at values as unknown."
        )


class TestUnionMergeJsonlAuditTrail:
    """pr-test LOW gap — assert the per-group audit record JSONL contains
    `kind: union_merge_group` plus winner_id and losers, so an operator
    reading the log can reconstruct the merge without re-running.
    """

    def test_per_group_record_has_correct_kind_and_fields(self, tmp_path):
        import json as _json

        attio = MagicMock()
        winner_entry = _entry("win-ent", "win-1", stage="DM1 Sent",
                              dm_step="dm1", last_contact_date="2026-04-10")
        loser_entry = _entry("lose-ent", "lose-1", stage="DM2 Sent",
                             dm_step="dm2", last_contact_date="2026-04-12")

        def find_entries(rid, lid):
            return {"win-1": [winner_entry], "lose-1": [loser_entry]}.get(rid, [])

        attio._find_list_entries_for_record.side_effect = find_entries

        writer = MagicMock()
        log_path = tmp_path / "audit.jsonl"
        log_fh = log_path.open("w")

        group = {
            "canonical_linkedin_url": "https://linkedin.com/in/foo",
            "winner_id": "win-1",
            "loser_ids": ["lose-1"],
            "conflict_shape": "entry.persona",
            "auto_mergeable": True,
        }
        apply_union_merge_group(attio, group, "list-1", writer, log_fh,
                                pretend=False)
        log_fh.close()

        lines = log_path.read_text().splitlines()
        records = [_json.loads(line) for line in lines if line.strip()]
        # Exactly one union_merge_group record per group.
        group_records = [r for r in records if r.get("kind") == "union_merge_group"]
        assert len(group_records) == 1
        rec = group_records[0]
        assert rec["winner_id"] == "win-1"
        assert rec["loser_ids"] == ["lose-1"]
        assert rec["canonical_linkedin_url"] == "https://linkedin.com/in/foo"


class TestComputeUnionMergeAttrsWinnerPositiveLoserNullNoWrite:
    """pr-test MED gap — a winner with `response_classification="positive"`
    and a loser with `response_classification=None` must NOT include
    `response_classification` in the delta. The null loser cannot weaken
    or "overwrite" the winner; emitting a no-op write would muddle audit.
    """

    def test_winner_positive_loser_null_no_classification_write(self):
        winner = _attrs(response_classification="positive")
        loser = _attrs(response_classification=None)
        delta = compute_union_merge_attrs(winner, [loser])
        assert "response_classification" not in delta, (
            "Null loser must not produce a response_classification write "
            "when the winner already has a known value."
        )


class TestExperimentIdsMinimumLengthValidation:
    """type-design IMPORTANT-1 — `DedupExperimentIdConflictPayload` is a
    *conflict* payload. ≥2 distinct experiment_ids IS the conflict. The
    fold-in runtime validator must reject empty or single-element lists.
    """

    def test_empty_experiment_ids_raises(self):
        from workflows.escalation import _validate_payload_against_typeddict
        from workflows.escalation_schemas import EscalationSchemaError

        payload = {
            "canonical_linkedin_url": "https://linkedin.com/in/foo",
            "winner_id": "win-1",
            "loser_ids": ["lose-1"],
            "experiment_ids": [],
            "conflict_shape": "entry.persona",
        }
        with pytest.raises(EscalationSchemaError, match="experiment_ids"):
            _validate_payload_against_typeddict(
                "dedup_experiment_id_conflict", payload
            )

    def test_single_experiment_id_raises(self):
        from workflows.escalation import _validate_payload_against_typeddict
        from workflows.escalation_schemas import EscalationSchemaError

        payload = {
            "canonical_linkedin_url": "https://linkedin.com/in/foo",
            "winner_id": "win-1",
            "loser_ids": ["lose-1"],
            "experiment_ids": ["only-one"],
            "conflict_shape": "entry.persona",
        }
        with pytest.raises(EscalationSchemaError, match="≥2 distinct ids"):
            _validate_payload_against_typeddict(
                "dedup_experiment_id_conflict", payload
            )

    def test_two_ids_passes(self):
        from workflows.escalation import _validate_payload_against_typeddict
        payload = {
            "canonical_linkedin_url": "https://linkedin.com/in/foo",
            "winner_id": "win-1",
            "loser_ids": ["lose-1"],
            "experiment_ids": ["exp-a", "exp-b"],
            "conflict_shape": "entry.persona",
        }
        # No raise.
        _validate_payload_against_typeddict(
            "dedup_experiment_id_conflict", payload
        )


class TestPayloadDisjointness:
    """type-design NIT — `dedup_experiment_id_conflict` must reject a
    `winner_id` that also appears in `loser_ids` (a record cannot dedup
    against itself); `dedup_review` must reject duplicate record_ids.
    """

    def test_winner_in_losers_raises(self):
        from workflows.escalation import _validate_payload_against_typeddict
        from workflows.escalation_schemas import EscalationSchemaError

        payload = {
            "canonical_linkedin_url": "https://linkedin.com/in/foo",
            "winner_id": "shared",
            "loser_ids": ["shared"],
            "experiment_ids": ["exp-a", "exp-b"],
            "conflict_shape": "entry.persona",
        }
        with pytest.raises(EscalationSchemaError, match="winner_id"):
            _validate_payload_against_typeddict(
                "dedup_experiment_id_conflict", payload
            )

    def test_dedup_review_duplicate_record_ids_raises(self):
        from workflows.escalation import _validate_payload_against_typeddict
        from workflows.escalation_schemas import EscalationSchemaError

        payload = {
            "canonical_linkedin_url": "https://linkedin.com/in/foo",
            "record_ids": ["rec-1", "rec-2", "rec-1"],  # rec-1 twice
            "conflict_shape": "company",
            "auto_mergeable": False,
        }
        with pytest.raises(EscalationSchemaError, match="duplicates"):
            _validate_payload_against_typeddict("dedup_review", payload)
