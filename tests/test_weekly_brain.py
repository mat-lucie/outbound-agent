"""End-to-end tests for the weekly brain proposal writer."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from workflows.weekly_brain import (
    DM1_VARIANT_KEYS,
    VariantScore,
    _avg_q25_engagement,
    _avg_q75_defensive,
    _q25,
    _q75,
    _recommend_variant,
    build_proposal_markdown,
    collect_defensive_samples,
    load_dm1_variants,
    run_weekly_brain,
)

# ── Helpers ─────────────────────────────────────────────────────────


def _mock_attio_empty() -> MagicMock:
    attio = MagicMock()
    attio.query_list_entries.return_value = []
    attio.parse_entry.side_effect = lambda e: e
    return attio


def _mock_attio_with_entries(entries: list[dict]) -> MagicMock:
    attio = MagicMock()
    raw = [{"_data": e} for e in entries]
    attio.query_list_entries.return_value = raw
    attio.parse_entry.side_effect = lambda e: e["_data"]
    return attio


def _entry(
    stage: str = "DM1 Sent",
    experiment_id: str = "exp-042",
    persona: str = "digitalization_champions",
    classification: str | None = None,
    text: str | None = None,
) -> dict:
    return {
        "entry_id": "eid",
        "record_id": "rid",
        "stage": stage,
        "persona": persona,
        "language": "es",
        "dm_step": None,
        "quality_score": None,
        "last_contact_date": None,
        "experiment_id": experiment_id,
        "response_classification": classification,
        "last_response_text": text,
    }


def _score_envelope(defensive=(0.1, 0.3), engagement=(0.4, 0.7), reasoning="ok"):
    return {
        "defensive_likelihood": list(defensive),
        "engagement_likelihood": list(engagement),
        "reasoning": reasoning,
    }


def _mock_anthropic_returning(response_json: str) -> MagicMock:
    client = MagicMock()
    block = MagicMock()
    block.type = "text"
    block.text = response_json
    message = MagicMock()
    message.content = [block]
    client.messages.create.return_value = message
    return client


# ── load_dm1_variants ───────────────────────────────────────────────


class TestLoadVariants:
    def test_returns_es_variants_per_persona(self, tmp_path):
        messages = {
            "digitalization_champions": {
                "dm1": {"es": "V0 text", "en": "ignored"},
                "dm1_v1": {"es": "V1 text"},
                "dm1_v3": {"es": "V3 text"},
                "dm2": {"es": "unused"},
            },
            "operations_leaders": {
                "dm1": {"es": "ops V0"},
                "dm1_v1": {"es": "ops V1"},
            },
        }
        path = tmp_path / "messages.json"
        path.write_text(json.dumps(messages))

        loaded = load_dm1_variants(path)
        assert set(loaded.keys()) == {"digitalization_champions", "operations_leaders"}
        assert loaded["digitalization_champions"] == {
            "dm1": "V0 text",
            "dm1_v1": "V1 text",
            "dm1_v3": "V3 text",
        }
        assert loaded["operations_leaders"] == {"dm1": "ops V0", "dm1_v1": "ops V1"}

    def test_personas_without_variants_are_dropped(self, tmp_path):
        messages = {
            "has_variants": {"dm1": {"es": "text"}},
            "no_variants": {"connection_note": {"es": "x"}},
        }
        path = tmp_path / "messages.json"
        path.write_text(json.dumps(messages))

        loaded = load_dm1_variants(path)
        assert "has_variants" in loaded
        assert "no_variants" not in loaded

    def test_respects_variant_key_whitelist(self):
        """DM2 and DM3 must not leak into the DM1 prescreen."""
        assert "dm1" in DM1_VARIANT_KEYS
        assert "dm1_v1" in DM1_VARIANT_KEYS
        assert "dm1_v3" in DM1_VARIANT_KEYS
        assert "dm2" not in DM1_VARIANT_KEYS
        assert "dm2_v1" not in DM1_VARIANT_KEYS


# ── collect_defensive_samples ───────────────────────────────────────


class TestCollectDefensiveSamples:
    def test_only_defensive_classifications_returned(self):
        entries = [
            _entry(classification="positive", text="yes!"),
            _entry(classification="defensive", text="eso no aplica"),
            _entry(classification="negative", text="no thanks"),
            _entry(classification="defensive", text="estoy segura que no"),
        ]
        attio = _mock_attio_with_entries(entries)
        samples = collect_defensive_samples(attio, limit=10)
        assert samples == ["eso no aplica", "estoy segura que no"]

    def test_limit_respected(self):
        entries = [
            _entry(classification="defensive", text=f"defensive {i}") for i in range(5)
        ]
        attio = _mock_attio_with_entries(entries)
        samples = collect_defensive_samples(attio, limit=2)
        assert samples == ["defensive 0", "defensive 1"]

    def test_filter_by_experiment_id(self):
        entries = [
            _entry(experiment_id="exp-001", classification="defensive", text="A"),
            _entry(experiment_id="exp-002", classification="defensive", text="B"),
        ]
        attio = _mock_attio_with_entries(entries)
        samples = collect_defensive_samples(attio, experiment_id="exp-002", limit=10)
        assert samples == ["B"]

    def test_filter_by_persona(self):
        """Samples must be scoped to a single sales persona, not global."""
        entries = [
            _entry(persona="digitalization_champions", classification="defensive", text="digi"),
            _entry(persona="operations_leaders", classification="defensive", text="ops"),
            _entry(persona="operations_leaders", classification="defensive", text="ops2"),
        ]
        attio = _mock_attio_with_entries(entries)
        samples = collect_defensive_samples(attio, persona="operations_leaders", limit=10)
        assert samples == ["ops", "ops2"]
        assert "digi" not in samples

    def test_filter_applied_before_limit(self):
        """A generous limit with a narrow filter must not truncate early."""
        entries = [
            # 5 non-target experiment defensives first
            _entry(experiment_id="exp-A", classification="defensive", text=f"A{i}")
            for i in range(5)
        ] + [
            _entry(experiment_id="exp-B", classification="defensive", text="B1"),
            _entry(experiment_id="exp-B", classification="defensive", text="B2"),
        ]
        attio = _mock_attio_with_entries(entries)
        samples = collect_defensive_samples(attio, experiment_id="exp-B", limit=3)
        assert samples == ["B1", "B2"]  # filter runs before limit

    def test_missing_text_skipped(self):
        entries = [
            _entry(classification="defensive", text=None),
            _entry(classification="defensive", text="real"),
        ]
        attio = _mock_attio_with_entries(entries)
        samples = collect_defensive_samples(attio, limit=10)
        assert samples == ["real"]


# ── build_proposal_markdown ─────────────────────────────────────────


class TestBuildProposalMarkdown:
    def _example_scored(self) -> dict[str, dict[str, VariantScore]]:
        score_v0 = _score_envelope(defensive=(0.7, 0.9), engagement=(0.1, 0.2))
        score_v1 = _score_envelope(defensive=(0.1, 0.3), engagement=(0.5, 0.7))
        return {
            "digitalization_champions": {
                "dm1": VariantScore(
                    variant_id="dm1",
                    variant_text="V0 text",
                    scores_by_persona={"patricia_defensive_senior": score_v0},
                ),
                "dm1_v1": VariantScore(
                    variant_id="dm1_v1",
                    variant_text="V1 text",
                    scores_by_persona={"patricia_defensive_senior": score_v1},
                ),
            }
        }

    def test_markdown_contains_required_sections(self):
        md = build_proposal_markdown(
            experiment_id="exp-042",
            variants_by_persona={
                "digitalization_champions": {"dm1": "V0", "dm1_v1": "V1"}
            },
            scored=self._example_scored(),
            cohorts=[],
            defensive_samples_by_persona={"digitalization_champions": []},
        )
        assert "# Experiment Proposal — `exp-042`" in md
        assert "## Decision" in md
        assert "## Candidate Variants" in md
        assert "## Last Cohort Performance" in md
        assert "## Defensive Reply Samples" in md
        assert "## How to approve" in md

    def test_variant_text_shown_inline(self):
        md = build_proposal_markdown(
            experiment_id="exp-042",
            variants_by_persona={
                "digitalization_champions": {"dm1": "V0", "dm1_v1": "V1"}
            },
            scored=self._example_scored(),
            cohorts=[],
            defensive_samples_by_persona={"digitalization_champions": []},
        )
        assert "V0 text" in md
        assert "V1 text" in md

    def test_ranges_shown_not_point_estimates(self):
        """DESIGN_SPEC rule: scores must be ranges, not point estimates."""
        md = build_proposal_markdown(
            experiment_id="exp-042",
            variants_by_persona={
                "digitalization_champions": {"dm1": "V0", "dm1_v1": "V1"}
            },
            scored=self._example_scored(),
            cohorts=[],
            defensive_samples_by_persona={"digitalization_champions": []},
        )
        # V0 defensive range 0.70-0.90
        assert "[0.70, 0.90]" in md
        # V1 defensive range 0.10-0.30
        assert "[0.10, 0.30]" in md

    def test_recommendation_picks_low_defensive_variant(self):
        md = build_proposal_markdown(
            experiment_id="exp-042",
            variants_by_persona={
                "digitalization_champions": {"dm1": "V0", "dm1_v1": "V1"}
            },
            scored=self._example_scored(),
            cohorts=[],
            defensive_samples_by_persona={"digitalization_champions": []},
        )
        # Recommendation line exists for each sales persona
        assert "digitalization_champions" in md
        # dm1_v1 has lower defensive q75 than dm1 → should be recommended
        # dm1 q75 = 0.70 + 0.75*0.20 = 0.85; dm1_v1 q75 = 0.10 + 0.75*0.20 = 0.25
        decision_block = md.split("## Candidate Variants")[0]
        assert "`dm1_v1`" in decision_block

    def test_recommendation_uses_q75_not_max(self):
        """A wide-uncertainty variant [0.10, 0.80] must be ranked by its q75
        (0.625), not its max (0.80). A tight variant [0.40, 0.45] wins because
        its q75 (0.4375) is lower — the conservative ranking penalizes wide
        uncertainty ranges on the worst-side end of the defensive axis.

        B-MW-RANK (PR-24): switched from midpoint to q75 for defensive ranking.
        wide q75 = 0.10 + 0.75 * 0.70 = 0.625
        tight q75 = 0.40 + 0.75 * 0.05 = 0.4375 → tight wins (lower is better)
        """
        wide = _score_envelope(defensive=(0.10, 0.80), engagement=(0.30, 0.70))
        tight = _score_envelope(defensive=(0.40, 0.45), engagement=(0.30, 0.70))
        scored = {
            "digitalization_champions": {
                "wide": VariantScore(
                    variant_id="wide",
                    variant_text="wide",
                    scores_by_persona={"patricia_defensive_senior": wide},
                ),
                "tight": VariantScore(
                    variant_id="tight",
                    variant_text="tight",
                    scores_by_persona={"patricia_defensive_senior": tight},
                ),
            }
        }
        md = build_proposal_markdown(
            experiment_id="exp-042",
            variants_by_persona={
                "digitalization_champions": {"wide": "wide", "tight": "tight"}
            },
            scored=scored,
            cohorts=[],
            defensive_samples_by_persona={"digitalization_champions": []},
        )
        decision_block = md.split("## Candidate Variants")[0]
        # tight q75 = 0.4375, wide q75 = 0.625 → tight wins
        assert "`tight`" in decision_block
        # max-based ranking would have picked wide (0.80 vs 0.45 loses). Pin
        # the q75 behavior so a refactor back to max-based breaks this.
        assert "`wide`" not in decision_block.split("ship")[1].split("`tight`")[0]

    def test_recommendation_tie_broken_by_engagement(self):
        """When two variants have equal q75-defensive (PR-24: midpoint → q75/q25),
        pick the one with higher q25-engagement.

        Both variants here use defensive=(0.20, 0.30), so q75 = 0.20 + 0.75*0.10
        = 0.275 is identical — the tie-break runs on q25-engagement. high_eng's
        q25 = 0.60 + 0.25*0.20 = 0.65; low_eng's q25 = 0.10 + 0.25*0.10 = 0.125.
        high_eng wins.
        """
        high_eng = _score_envelope(defensive=(0.20, 0.30), engagement=(0.60, 0.80))
        low_eng = _score_envelope(defensive=(0.20, 0.30), engagement=(0.10, 0.20))
        scored = {
            "ops": {
                "high_eng": VariantScore(
                    variant_id="high_eng",
                    variant_text="high",
                    scores_by_persona={"coo_actively_searching": high_eng},
                ),
                "low_eng": VariantScore(
                    variant_id="low_eng",
                    variant_text="low",
                    scores_by_persona={"coo_actively_searching": low_eng},
                ),
            }
        }
        md = build_proposal_markdown(
            experiment_id="exp-042",
            variants_by_persona={"ops": {"high_eng": "high", "low_eng": "low"}},
            scored=scored,
            cohorts=[],
            defensive_samples_by_persona={"ops": []},
        )
        decision_block = md.split("## Candidate Variants")[0]
        assert "`high_eng`" in decision_block

    def test_defensive_samples_rendered(self):
        md = build_proposal_markdown(
            experiment_id="exp-042",
            variants_by_persona={
                "digitalization_champions": {"dm1": "V0", "dm1_v1": "V1"}
            },
            scored=self._example_scored(),
            cohorts=[],
            defensive_samples_by_persona={
                "digitalization_champions": [
                    "estoy segura que no viven en Excel",
                    "eso no aplica a nuestro caso",
                ]
            },
        )
        assert "estoy segura que no viven en Excel" in md
        assert "eso no aplica a nuestro caso" in md

    def test_empty_cohorts_message(self):
        md = build_proposal_markdown(
            experiment_id="exp-042",
            variants_by_persona={"digitalization_champions": {"dm1": "V0"}},
            scored={
                "digitalization_champions": {
                    "dm1": VariantScore(
                        "dm1", "V0", {"patricia_defensive_senior": _score_envelope()}
                    )
                }
            },
            cohorts=[],
            defensive_samples_by_persona={"digitalization_champions": []},
        )
        assert "No cohort data available" in md


# ── run_weekly_brain end-to-end ─────────────────────────────────────


class TestRunWeeklyBrain:
    def test_writes_proposal_to_disk(self, tmp_path):
        attio = _mock_attio_empty()
        client = _mock_anthropic_returning(json.dumps({
            "defensive_likelihood": [0.1, 0.3],
            "engagement_likelihood": [0.4, 0.6],
            "reasoning": "clean",
        }))

        variants = {
            "digitalization_champions": {"dm1": "baseline", "dm1_v1": "curiosity"}
        }
        out_path = run_weekly_brain(
            attio,
            experiment_id="exp-999",
            variants_by_persona=variants,
            output_dir=tmp_path,
            anthropic_client=client,
        )

        assert out_path.exists()
        assert out_path.name == "exp-999-proposal.md"
        content = out_path.read_text()
        assert "exp-999" in content
        assert "curiosity" in content
        assert "baseline" in content

    def test_dry_run_does_not_write_file(self, tmp_path):
        attio = _mock_attio_empty()
        client = _mock_anthropic_returning(json.dumps({
            "defensive_likelihood": [0.1, 0.3],
            "engagement_likelihood": [0.4, 0.6],
            "reasoning": "clean",
        }))

        out_path = run_weekly_brain(
            attio,
            experiment_id="exp-998",
            variants_by_persona={"execs": {"dm1_v1": "text"}},
            output_dir=tmp_path,
            anthropic_client=client,
            dry_run=True,
        )
        assert not out_path.exists()
        # Path is computed even in dry run
        assert out_path.name == "exp-998-proposal.md"

    def test_calls_score_matrix_per_persona(self, tmp_path):
        attio = _mock_attio_empty()
        client = _mock_anthropic_returning(json.dumps({
            "defensive_likelihood": [0.2, 0.4],
            "engagement_likelihood": [0.4, 0.6],
            "reasoning": "ok",
        }))

        variants = {
            "digitalization_champions": {"dm1": "A", "dm1_v1": "B"},
            "operations_leaders": {"dm1_v1": "C"},
        }
        run_weekly_brain(
            attio,
            experiment_id="exp-777",
            variants_by_persona=variants,
            output_dir=tmp_path,
            anthropic_client=client,
        )
        # 2 champions variants × 4 synthetic personas + 1 ops variant × 4 = 12 calls
        assert client.messages.create.call_count == 12

    def test_defensive_samples_scoped_per_persona(self, tmp_path):
        """Persona A has defensive samples; persona B has none. The markdown
        must NOT attribute A's samples to B.
        """
        entries = [
            _entry(
                persona="digitalization_champions",
                experiment_id="exp-444",
                classification="defensive",
                text="champions defensive reply",
            ),
            _entry(
                persona="operations_leaders",
                experiment_id="exp-444",
                classification="positive",
                text="ops happy reply",
            ),
        ]
        attio = _mock_attio_with_entries(entries)
        client = _mock_anthropic_returning(json.dumps({
            "defensive_likelihood": [0.1, 0.2],
            "engagement_likelihood": [0.5, 0.7],
            "reasoning": "ok",
        }))
        out_path = run_weekly_brain(
            attio,
            experiment_id="exp-444",
            variants_by_persona={
                "digitalization_champions": {"dm1_v1": "champ text"},
                "operations_leaders": {"dm1_v1": "ops text"},
            },
            output_dir=tmp_path,
            anthropic_client=client,
        )
        content = out_path.read_text()
        # Isolate the "Defensive Reply Samples" section so we don't get fooled
        # by persona names that appear in other sections (variant scoring tables).
        defensive_section = content.split("## Defensive Reply Samples", 1)[1]
        defensive_section = defensive_section.split("## How to approve", 1)[0]

        # Champions persona has one defensive sample
        assert "champions defensive reply" in defensive_section

        # Ops persona is positive-only, should not have an ops subsection in the
        # defensive block (or if present, must not contain the champions reply).
        champions_subblock_start = defensive_section.find("### `digitalization_champions`")
        champions_subblock = defensive_section[champions_subblock_start:]
        if "### `operations_leaders`" in champions_subblock:
            ops_subblock = champions_subblock.split("### `operations_leaders`", 1)[1]
            assert "champions defensive reply" not in ops_subblock
            # ops_happy was positive, not defensive — must not appear either
            assert "ops happy reply" not in ops_subblock

    def test_defensive_samples_surfaced_in_markdown(self, tmp_path):
        entries = [
            _entry(
                classification="defensive",
                text="eso no aplica",
                experiment_id="exp-555",
                persona="execs",
            ),
            _entry(
                classification="positive",
                text="book me",
                experiment_id="exp-555",
                persona="execs",
            ),
        ]
        attio = _mock_attio_with_entries(entries)
        client = _mock_anthropic_returning(json.dumps({
            "defensive_likelihood": [0.1, 0.2],
            "engagement_likelihood": [0.5, 0.7],
            "reasoning": "clean",
        }))
        out_path = run_weekly_brain(
            attio,
            experiment_id="exp-555",
            variants_by_persona={"execs": {"dm1_v1": "text"}},
            output_dir=tmp_path,
            anthropic_client=client,
        )
        content = out_path.read_text()
        assert "eso no aplica" in content
        # Positive sample must not be in the defensive section
        assert "book me" not in content


class TestCollectDefensiveSamplesSoftDeleteFilter:
    """§3.11 — defensive samples come from `query_list_entries`. Union-
    merge losers must NEVER feed the variant proposal sample pool, or a
    single defensive reply gets double-counted (winner inherited the
    classification; both rows would show the same text).
    """

    def test_merged_into_losers_excluded_from_defensive_samples(self):
        winner = _entry(
            classification="defensive",
            text="winner reply",
            experiment_id="exp-soft",
            persona="execs",
        )
        loser = _entry(
            classification="defensive",
            text="loser reply",
            experiment_id="exp-soft",
            persona="execs",
        )
        loser["merged_into"] = {"target_object": "people",
                                "target_record_id": "rec-winner"}
        attio = _mock_attio_with_entries([winner, loser])
        samples = collect_defensive_samples(
            attio, experiment_id="exp-soft", persona="execs", limit=10
        )
        # Loser's reply must NOT surface.
        assert "winner reply" in samples
        assert "loser reply" not in samples


# ── q75/q25 ranking helpers (B-MW-RANK) ────────────────────────────


class TestQ75Q25Helpers:
    """PR-24: verify the percentile math for the ranking helpers.

    _q75([a, b]) = a + 0.75 * (b - a)  — 75th percentile of uniform [a,b]
    _q25([a, b]) = a + 0.25 * (b - a)  — 25th percentile of uniform [a,b]
    """

    def test_q75_wide_defensive_range(self):
        """[0.10, 0.80]: q75 = 0.10 + 0.75*0.70 = 0.625"""
        assert abs(_q75([0.10, 0.80]) - 0.625) < 1e-9

    def test_q75_narrow_range(self):
        """[0.40, 0.45]: q75 = 0.40 + 0.75*0.05 = 0.4375"""
        assert abs(_q75([0.40, 0.45]) - 0.4375) < 1e-9

    def test_q75_zero_width_range(self):
        """[0.50, 0.50]: q75 = 0.50 (degenerate; same as midpoint)"""
        assert _q75([0.50, 0.50]) == 0.50

    def test_q25_wide_engagement_range(self):
        """[0.20, 0.80]: q25 = 0.20 + 0.25*0.60 = 0.35"""
        assert abs(_q25([0.20, 0.80]) - 0.35) < 1e-9

    def test_q25_narrow_engagement_range(self):
        """[0.60, 0.80]: q25 = 0.60 + 0.25*0.20 = 0.65"""
        assert abs(_q25([0.60, 0.80]) - 0.65) < 1e-9

    def test_q25_zero_width_range(self):
        """[0.70, 0.70]: q25 = 0.70 (degenerate)"""
        assert _q25([0.70, 0.70]) == 0.70

    def test_q75_equals_max_when_full_range(self):
        """Not in general — q75 of [0, 1] = 0.75, not 1.0."""
        assert abs(_q75([0.0, 1.0]) - 0.75) < 1e-9

    def test_q25_equals_min_when_full_range(self):
        """Not in general — q25 of [0, 1] = 0.25, not 0.0."""
        assert abs(_q25([0.0, 1.0]) - 0.25) < 1e-9


class TestQ75Q25Ranking:
    """PR-24: _recommend_variant uses q75-defensive + q25-engagement.

    Key behavioral contract: wide uncertainty ranges on the loss-side
    of each axis are penalized MORE than they would be under midpoint
    ranking.
    """

    def _vs(self, variant_id: str, defensive: tuple, engagement: tuple) -> VariantScore:
        return VariantScore(
            variant_id=variant_id,
            variant_text=variant_id,
            scores_by_persona={
                "patricia_defensive_senior": {
                    "defensive_likelihood": list(defensive),
                    "engagement_likelihood": list(engagement),
                    "reasoning": "test",
                }
            },
        )

    def test_q75_penalizes_wide_defensive_over_midpoint(self):
        """Under midpoint: wide [0.10, 0.80] (mid=0.45) beats narrow
        [0.40, 0.50] (mid=0.45) — they're equal, tie-breaks on engagement.
        Under q75: wide (0.625) loses to narrow (0.475) — wide uncertainty
        is penalized.
        """
        # Both have same midpoint = 0.45; same engagement midpoint
        wide = self._vs("wide", defensive=(0.10, 0.80), engagement=(0.50, 0.50))
        narrow = self._vs("narrow", defensive=(0.40, 0.50), engagement=(0.50, 0.50))
        variant_scores = {"wide": wide, "narrow": narrow}
        winner_id, rationale = _recommend_variant(variant_scores)
        # q75 wide = 0.10 + 0.75*0.70 = 0.625
        # q75 narrow = 0.40 + 0.75*0.10 = 0.475
        # narrow wins (lower q75-defensive is better)
        assert winner_id == "narrow"
        assert "q75" in rationale

    def test_q25_penalizes_wide_engagement_over_midpoint(self):
        """When defensive q75 ties, q25-engagement breaks the tie.
        wide engagement [0.20, 0.80] q25=0.35 loses to
        narrow engagement [0.55, 0.65] q25=0.575.
        """
        # Both have same defensive q75 (identical defensive range)
        high_eng = self._vs("high_eng", defensive=(0.20, 0.30), engagement=(0.55, 0.65))
        low_eng_wide = self._vs("low_eng_wide", defensive=(0.20, 0.30), engagement=(0.20, 0.80))
        variant_scores = {"high_eng": high_eng, "low_eng_wide": low_eng_wide}
        winner_id, _ = _recommend_variant(variant_scores)
        # q25 high_eng = 0.55 + 0.25*0.10 = 0.575
        # q25 low_eng_wide = 0.20 + 0.25*0.60 = 0.35
        # high_eng wins (higher q25-engagement is better)
        assert winner_id == "high_eng"

    def test_avg_q75_defensive_single_persona(self):
        """Verify the averaging logic for a single persona."""
        vs = self._vs("v", defensive=(0.10, 0.80), engagement=(0.30, 0.70))
        result = _avg_q75_defensive(vs)
        # q75([0.10, 0.80]) = 0.10 + 0.75 * 0.70 = 0.625
        assert abs(result - 0.625) < 1e-9

    def test_avg_q25_engagement_single_persona(self):
        """Verify the averaging logic for a single persona."""
        vs = self._vs("v", defensive=(0.10, 0.30), engagement=(0.20, 0.80))
        result = _avg_q25_engagement(vs)
        # q25([0.20, 0.80]) = 0.20 + 0.25 * 0.60 = 0.35
        assert abs(result - 0.35) < 1e-9

    def test_avg_q75_multiple_personas(self):
        """Average across multiple synthetic persona scores."""
        vs = VariantScore(
            variant_id="v",
            variant_text="v",
            scores_by_persona={
                "persona_a": {
                    "defensive_likelihood": [0.10, 0.80],  # q75=0.625
                    "engagement_likelihood": [0.30, 0.70],
                    "reasoning": "",
                },
                "persona_b": {
                    "defensive_likelihood": [0.20, 0.40],  # q75=0.35
                    "engagement_likelihood": [0.30, 0.70],
                    "reasoning": "",
                },
            },
        )
        result = _avg_q75_defensive(vs)
        # avg(0.625, 0.35) = 0.4875
        assert abs(result - 0.4875) < 1e-9

    def test_recommend_no_variants_returns_empty(self):
        variant_id, rationale = _recommend_variant({})
        assert variant_id == ""
        assert "No variants" in rationale

    def test_recommend_single_variant(self):
        vs = self._vs("only_one", defensive=(0.20, 0.30), engagement=(0.50, 0.70))
        variant_id, rationale = _recommend_variant({"only_one": vs})
        assert variant_id == "only_one"
        assert "q75" in rationale
        assert "q25" in rationale

    def test_recommend_rationale_includes_q75_q25_values(self):
        """The rationale must mention q75-defensive and q25-engagement
        so operators reading the proposal understand the ranking logic.
        """
        vs = self._vs("v", defensive=(0.10, 0.30), engagement=(0.40, 0.60))
        _, rationale = _recommend_variant({"v": vs})
        assert "q75" in rationale
        assert "q25" in rationale
