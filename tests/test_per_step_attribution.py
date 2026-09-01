"""Per-step response attribution regression tests (2026-06-10 fix).

Root cause being guarded against: dm{N}_sent_at was NEVER stamped by the
live send path (run_dm_sequencing) — only by the one-shot PR-9b backfill,
PR-9.5 dedup merges, and pb_send_recovery. Every DM sent after the backfill
carried NULL, so learn.py's PR-9b NULL gate excluded entire cohorts from
per-step denominators: step_n_observed read {'dm1': 0, 'dm2': 0, 'dm3': 0}
for a cohort with DM'd=99 / resp=13, posteriors collapsed to priors, and a
wet `learn` run would have struck a TERMINAL prior-only REJECTED_NULL via
apply_verdict.

Four guard layers, each tested here:
1. `_per_step_rates` attribution math on a synthetic cohort with KNOWN
   per-step responses (and the starvation mode it must surface, not hide).
2. `_confirmed_dm_advance_attrs` — the live send path now stamps
   dm{N}_sent_at on every PB-confirmed send.
3. The consistency sweep NULL-fills the converged step's sent_at from the
   tally date (never overwriting an existing stamp).
4. `evaluate_experiments` emits the NON-terminal "insufficient_data"
   verdict when the routed step's n_observed < SMALL_N_THRESHOLD, so a
   data-starved cohort can never receive a terminal verdict
   (to_experiment_verdict maps it to None and the wet path skips
   apply_verdict).

Phase-0 blind-window tolerance: on 2026-06-10 a repair flipped 22
long-pending acceptances (invited weeks earlier, DM1 sent 2026-06-10).
Attribution must rely only on dm_step + dm{N}_sent_at — never on invite /
entry-creation timelines — so those members measure normally. Covered by
TestPerStepRatesSyntheticCohort::test_blind_window_repaired_member_counts.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

from models.bayesian import SMALL_N_THRESHOLD, STEP_PRIORS
from models.campaign import MessageStep
from models.experiment import Experiment, ExperimentStatus, append_experiment
from models.pipeline import PipelineStage
from workflows.consistency_sweep import run_company_tally_consistency_sweep
from workflows.daily_check import _confirmed_dm_advance_attrs
from workflows.dm_sequencer import NEXT_STAGE
from workflows.learn import (
    _compute_posterior,
    _per_step_rates,
    evaluate_experiments,
    to_experiment_verdict,
)

# ── Fixtures ─────────────────────────────────────────────────────────────────


def _member(
    stage: str,
    dm_step: str | int | None,
    *,
    dm1_sent_at: str | None = None,
    dm2_sent_at: str | None = None,
    dm3_sent_at: str | None = None,
    record_id: str = "rid",
    entry_created_at: str | None = None,
) -> dict:
    """Minimal parsed LinkedIn Outreach entry for attribution tests."""
    return {
        "entry_id": f"eid-{record_id}",
        "record_id": record_id,
        "entry_created_at": entry_created_at,
        "stage": stage,
        "dm_step": dm_step,
        "experiment_id": "exp-synth",
        "experiment_id_frozen_at": "accepted",
        "dm1_sent_at": dm1_sent_at,
        "dm2_sent_at": dm2_sent_at,
        "dm3_sent_at": dm3_sent_at,
        "response_classification": None,
    }


def _synthetic_cohort() -> list[dict]:
    """12-member cohort with KNOWN per-step outcomes.

    - 6 members stopped at DM1 (2 responded at DM1, 4 silent)
    - 4 members reached DM2 (1 responded at DM2, 3 silent)
    - 2 members reached DM3 (1 responded at DM3, 1 silent)

    Expected attribution:
      dm1: n_observed=12, n_successes=2
      dm2: n_observed=6,  n_successes=1
      dm3: n_observed=2,  n_successes=1
    """
    responded = PipelineStage.RESPONDED.value
    members = []
    for i in range(6):
        stage = responded if i < 2 else PipelineStage.DM1_SENT.value
        members.append(
            _member(stage, "dm1", dm1_sent_at="2026-05-20", record_id=f"a{i}")
        )
    for i in range(4):
        stage = responded if i < 1 else PipelineStage.DM2_SENT.value
        members.append(
            _member(
                stage, "dm2",
                dm1_sent_at="2026-05-20", dm2_sent_at="2026-05-27",
                record_id=f"b{i}",
            )
        )
    for i in range(2):
        stage = responded if i < 1 else PipelineStage.DM3_SENT.value
        members.append(
            _member(
                stage, "dm3",
                dm1_sent_at="2026-05-20", dm2_sent_at="2026-05-27",
                dm3_sent_at="2026-06-08",
                record_id=f"c{i}",
            )
        )
    return members


def _make_experiment(
    exp_id: str,
    status: str,
    started: str,
    *,
    variable: str = "",
    dm_response_rate: float = 0.08,
    dm1_response_rate: float | None = None,
) -> Experiment:
    return Experiment(
        experiment_id=exp_id,
        started=started,
        completed="",
        cohort_size=50,
        dm_response_rate=dm_response_rate,
        baseline_rate=0.08,
        status=ExperimentStatus(status),
        variable=variable,
        description="test",
        dm1_response_rate=dm1_response_rate,
    )


# ── 1. Attribution math on the synthetic cohort ─────────────────────────────


class TestPerStepRatesSyntheticCohort:
    def test_known_per_step_counts(self):
        rates = _per_step_rates(_synthetic_cohort())

        assert rates["dm1_received"] == 12
        assert rates["dm1_replies"] == 2
        assert rates["dm2_received"] == 6
        assert rates["dm2_replies"] == 1
        assert rates["dm3_received"] == 2
        assert rates["dm3_replies"] == 1

        for n in (1, 2, 3):
            post = rates[f"dm{n}_posterior"]
            assert post["n_observed"] == rates[f"dm{n}_received"]
            assert post["n_successes"] == rates[f"dm{n}_replies"]

    def test_response_attributed_to_exactly_one_step(self):
        """Each responder is a success at its dm_step ONLY — the DM2
        responder must not leak into the DM1 numerator (it stays in the
        DM1 denominator: it did receive DM1 and did not reply to it)."""
        rates = _per_step_rates(_synthetic_cohort())
        total_successes = sum(rates[f"dm{n}_replies"] for n in (1, 2, 3))
        assert total_successes == 4  # 2 + 1 + 1, no double counting

    def test_starved_cohort_reads_zero_not_fabricated(self):
        """The 2026-06-10 symptom: same cohort, sent_at never stamped →
        every step denominator is 0 and the posterior equals the prior.
        This is the failure mode the send-path stamp + insufficient_data
        verdict guard exist for; the math layer must surface the zeros
        honestly (not fabricate counts from dm_step alone)."""
        starved = [
            {**m, "dm1_sent_at": None, "dm2_sent_at": None, "dm3_sent_at": None}
            for m in _synthetic_cohort()
        ]
        rates = _per_step_rates(starved)
        for n in (1, 2, 3):
            assert rates[f"dm{n}_received"] == 0
            assert rates[f"dm{n}_replies"] == 0
            prior_alpha, prior_beta = STEP_PRIORS[n]
            assert rates[f"dm{n}_posterior"]["mean"] == (
                prior_alpha / (prior_alpha + prior_beta)
            )

    def test_blind_window_repaired_member_counts(self):
        """Phase-0 blind-window timeline: invited mid-April, acceptance
        only flipped 2026-06-10, DM1 sent 2026-06-10. Attribution uses
        dm_step + dm1_sent_at only, so the member measures normally."""
        member = _member(
            PipelineStage.RESPONDED.value,
            "dm1",
            dm1_sent_at="2026-06-10",
            entry_created_at="2026-04-18T09:00:00Z",
            record_id="blind",
        )
        rates = _per_step_rates([member])
        assert rates["dm1_received"] == 1
        assert rates["dm1_replies"] == 1


# ── 2. Live send path stamps dm{N}_sent_at ──────────────────────────────────


class TestConfirmedDmAdvanceAttrs:
    TODAY = date(2026, 6, 10)
    TODAY_STR = "2026-06-10"

    def _attrs(self, step: MessageStep) -> dict:
        return _confirmed_dm_advance_attrs(
            step=step,
            next_stage=NEXT_STAGE[step],
            today=self.TODAY,
            today_str=self.TODAY_STR,
        )

    def test_every_dm_step_stamps_its_sent_at(self):
        for step in (MessageStep.DM1, MessageStep.DM2, MessageStep.DM3):
            attrs = self._attrs(step)
            assert attrs[f"{step.value}_sent_at"] == self.TODAY_STR, (
                f"{step.value}: confirmed send must stamp {step.value}_sent_at "
                "or the row is excluded from learn.py per-step denominators "
                "(PR-9b NULL gate) — the 2026-06-10 n_observed=0 bug"
            )

    def test_existing_advance_attrs_preserved(self):
        attrs = self._attrs(MessageStep.DM1)
        assert attrs["dm_step"] == 1
        assert attrs["stage"] == PipelineStage.DM1_SENT.value
        assert attrs["last_contact_date"] == self.TODAY_STR
        assert "next_eligible_send_date" in attrs

    def test_dm3_has_no_next_eligible(self):
        attrs = self._attrs(MessageStep.DM3)
        assert "next_eligible_send_date" not in attrs
        assert attrs["dm3_sent_at"] == self.TODAY_STR


# ── 3. Consistency sweep NULL-fills the converged step's sent_at ────────────


def _sweep_company(record_id, ts, person_id, step="DM1"):
    return {
        "id": {"record_id": record_id},
        "values": {
            "last_outreach_at": [{"value": ts}],
            "last_outreach_person_id": [
                {"target_object": "people", "target_record_id": person_id}
            ],
            "last_outreach_step": [{"option": {"title": step}}],
        },
    }


def _sweep_entry(entry_id, *, dm_step, stage, dm1_sent_at=None):
    entry_values = {
        "dm_step": [{"value": dm_step}],
        "stage": [{"status": {"title": stage}}],
    }
    if dm1_sent_at is not None:
        entry_values["dm1_sent_at"] = [{"value": dm1_sent_at}]
    return {"id": {"entry_id": entry_id}, "entry_values": entry_values}


def _sweep_attio(companies, person_entries, person_id="rec_p"):
    attio = MagicMock()
    attio.search_companies.return_value = companies
    attio.query_list_entries.return_value = [
        {
            "parent_record_id": person_id,
            "entry_values": {},
            "id": {"entry_id": f"raw-{person_id}"},
        }
    ]
    attio._filter_and_rank_entries_for_record.return_value = person_entries
    return attio


class TestSweepStampsSentAt:
    def _run(self, person_entries, monkeypatch):
        monkeypatch.setattr("workflows.consistency_sweep.escalate", MagicMock())
        attio = _sweep_attio(
            [_sweep_company("rec_co", "2026-06-08T12:00:00Z", "rec_p", "DM1")],
            person_entries,
        )
        advance = MagicMock(return_value=True)
        run_company_tally_consistency_sweep(
            attio=attio,
            list_id="lst",
            today=date(2026, 6, 9),
            dry_run=False,
            advance_fn=advance,
            audit_logger=MagicMock(),
        )
        return advance

    def test_null_sent_at_filled_from_tally_date(self, monkeypatch):
        advance = self._run(
            [_sweep_entry("ent_1", dm_step=0, stage="Accepted")], monkeypatch
        )
        attrs = advance.call_args.kwargs["entry_attributes"]
        assert attrs["dm1_sent_at"] == "2026-06-08"

    def test_existing_sent_at_never_overwritten(self, monkeypatch):
        advance = self._run(
            [
                _sweep_entry(
                    "ent_1", dm_step=0, stage="Accepted",
                    dm1_sent_at="2026-06-01",
                )
            ],
            monkeypatch,
        )
        attrs = advance.call_args.kwargs["entry_attributes"]
        assert "dm1_sent_at" not in attrs


# ── 3b. Slug agreement across all send-recording paths ──────────────────────


class TestSentAtSlugCrossCheck:
    """Three independent paths record confirmed DM sends (live sequencer,
    crash recovery, consistency sweep), each constructing the dm{N}_sent_at
    slug its own way. This pins them to one another so a rename or typo in
    any single path fails here instead of silently starving measurement —
    chosen over centralizing the construction, which would force enum
    conversions between the paths' different keyings (MessageStep vs
    PipelineStage)."""

    def test_three_send_recording_paths_agree(self):
        from workflows.pb_send_recovery import _DM_STAGE_META

        for step in (MessageStep.DM1, MessageStep.DM2, MessageStep.DM3):
            n = int(step.value.replace("dm", ""))
            stage = NEXT_STAGE[step]
            slug = f"{step.value}_sent_at"
            # Crash-recovery path (keyed by PipelineStage)
            assert _DM_STAGE_META[stage] == (n, slug)
            # Live send path (keyed by MessageStep) — and the sweep builds
            # the identical f-string from the same MessageStep.value.
            attrs = _confirmed_dm_advance_attrs(
                step=step,
                next_stage=stage,
                today=date(2026, 6, 10),
                today_str="2026-06-10",
            )
            assert slug in attrs


# ── 4. insufficient_data verdict guard ──────────────────────────────────────


class TestInsufficientDataVerdict:
    def _tsv(self, tmp_path):
        tsv = tmp_path / "experiments.tsv"
        append_experiment(
            _make_experiment(
                "baseline-v0", "won", "2026-01-01",
                dm_response_rate=0.08, dm1_response_rate=0.0426,
            ),
            tsv,
        )
        append_experiment(
            _make_experiment(
                "exp-dm1", "running", "2026-05-11",
                variable="messages.json:dm1:all",
            ),
            tsv,
        )
        return tsv

    def _cohort(self, *, dm1_observed: int, dm1_successes: int = 0) -> dict:
        """Mature dm1-routed cohort; per-step posteriors built for real."""
        posteriors = {
            f"dm{n}_posterior": _compute_posterior(
                dm1_observed,
                dm1_successes if n == 1 else 0,
                n,
            )
            for n in (1, 2, 3)
        }
        cohort = {
            "experiment_id": "exp-dm1",
            "cohort_size": 120,
            "dmed": 99,
            "responded": 13,
            "dm_response_rate": 13 / 99,
            "is_mature": True,
            "days_running": 30,
            "defensive_rate": 0.0,
            **posteriors,
        }
        for n in (1, 2, 3):
            cohort[f"dm{n}_response_rate"] = posteriors[f"dm{n}_posterior"]["mean"]
            cohort[f"dm{n}_received"] = posteriors[f"dm{n}_posterior"]["n_observed"]
            cohort[f"dm{n}_replies"] = posteriors[f"dm{n}_posterior"]["n_successes"]
        return cohort

    def test_starved_routed_step_yields_insufficient_data(self, tmp_path):
        """The exact 2026-06-10 scenario: DM'd=99, resp=13 at cohort level
        but n_observed=0 per step. The verdict must be insufficient_data —
        NOT inconclusive, which apply_verdict would persist terminally as
        REJECTED_NULL on prior-only numbers."""
        results = evaluate_experiments(
            [self._cohort(dm1_observed=0)], experiments_path=self._tsv(tmp_path)
        )
        assert len(results) == 1
        assert results[0]["verdict"] == "insufficient_data"
        assert results[0]["prior_dominated"] is True

    def test_insufficient_data_is_non_terminal(self):
        assert to_experiment_verdict("insufficient_data") is None

    def test_threshold_boundary(self, tmp_path):
        tsv = self._tsv(tmp_path)
        below = evaluate_experiments(
            [self._cohort(dm1_observed=SMALL_N_THRESHOLD - 1)],
            experiments_path=tsv,
        )
        assert below[0]["verdict"] == "insufficient_data"

        at = evaluate_experiments(
            [self._cohort(dm1_observed=SMALL_N_THRESHOLD)],
            experiments_path=tsv,
        )
        assert at[0]["verdict"] != "insufficient_data"
        assert at[0]["verdict"] in {"won", "lost", "inconclusive"}

    def test_well_observed_cohort_gets_ci_verdict(self, tmp_path):
        results = evaluate_experiments(
            [self._cohort(dm1_observed=60, dm1_successes=9)],
            experiments_path=self._tsv(tmp_path),
        )
        assert results[0]["verdict"] in {"won", "lost", "inconclusive"}

    def test_defensive_veto_beats_insufficient_data(self, tmp_path):
        """A reactance-triggering variant is killed even when the per-step
        denominators are starved — defensive_rate comes from cohort-level
        classifications, not from sent_at-gated step denominators."""
        cohort = self._cohort(dm1_observed=0)
        cohort["defensive_rate"] = 0.25  # > 2× BASELINE_DEFENSIVE_RATE
        results = evaluate_experiments(
            [cohort], experiments_path=self._tsv(tmp_path)
        )
        assert results[0]["verdict"] == "rejected_defensive"

    def test_unknown_n_observed_treated_as_insufficient(self, tmp_path):
        """A step-routed cohort carrying NEITHER a posterior NOR a
        dm{N}_received count has unknown n — that must never license a
        terminal verdict (previously it fell through to the mean-based
        rule and could strike one)."""
        cohort = self._cohort(dm1_observed=0)
        for n in (1, 2, 3):
            cohort[f"dm{n}_posterior"] = None
            del cohort[f"dm{n}_received"]
            cohort[f"dm{n}_response_rate"] = 0.20  # would read "won" on means
        results = evaluate_experiments(
            [cohort], experiments_path=self._tsv(tmp_path)
        )
        assert results[0]["verdict"] == "insufficient_data"

    def test_flat_scalar_fallback_path_also_guarded(self, tmp_path):
        """Pre-PR-10 cohort shape (no posteriors): the routed step's
        dm{N}_received drives the same guard."""
        cohort = self._cohort(dm1_observed=0)
        for n in (1, 2, 3):
            cohort[f"dm{n}_posterior"] = None
            cohort[f"dm{n}_received"] = 2
            cohort[f"dm{n}_replies"] = 0
            cohort[f"dm{n}_response_rate"] = 0.05
        results = evaluate_experiments(
            [cohort], experiments_path=self._tsv(tmp_path)
        )
        assert results[0]["verdict"] == "insufficient_data"
