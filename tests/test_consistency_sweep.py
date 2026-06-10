"""Consistency sweep tests (2026-06-09 desync-invariant design §2).

The tally is source of truth for what was actually sent; the sweep
converges divergent person entries toward it same-day instead of
waiting for the throttle to trip (the desync would otherwise sit for
days until the next send to that company)."""
from datetime import date
from unittest.mock import MagicMock

from workflows.consistency_sweep import (
    REPAIR_CIRCUIT_THRESHOLD,
    run_company_tally_consistency_sweep,
)


def _company(record_id, ts, person_id, step="DM1"):
    values = {
        "last_outreach_at": [{"value": ts}],
        "last_outreach_person_id": [
            {"target_object": "people", "target_record_id": person_id}
        ],
    }
    if step is not None:
        values["last_outreach_step"] = [{"option": {"title": step}}]
    return {"id": {"record_id": record_id}, "values": values}


def _entry(entry_id, *, dm_step, stage):
    return {
        "id": {"entry_id": entry_id},
        "entry_values": {
            "dm_step": [{"value": dm_step}],
            "stage": [{"status": {"title": stage}}],
        },
    }


def _raw_entry(person_record_id: str) -> dict:
    """Minimal raw Attio list-entry shape for the index builder.

    parse_entry() reads record_id from entry["parent_record_id"] so the
    entries_by_record index correctly maps person_record_id → raw entry.
    _filter_and_rank_entries_for_record is mocked, so the raw shape only
    needs to satisfy the index build step (parse_entry call), not the full
    ranking path.
    """
    return {
        "parent_record_id": person_record_id,
        "entry_values": {},
        "id": {"entry_id": f"raw-{person_record_id}"},
    }


def _attio(companies, person_entries, person_id: str = "rec_p"):
    attio = MagicMock()
    attio.search_companies.return_value = companies
    # Provide a realistic raw entry so the Fix-2 index builder can call
    # AttioClient.parse_entry() without a TypeError.  The mock for
    # _filter_and_rank_entries_for_record still controls what the sweep
    # sees as the person's ranked entries — the index just slices the
    # raw list to entries matching person_record_id before passing them on.
    attio.query_list_entries.return_value = [_raw_entry(person_id)]
    attio._filter_and_rank_entries_for_record.return_value = person_entries
    return attio


def _sweep(attio, *, dry_run=False, advance=None, esc=None, monkeypatch=None):
    advance = advance or MagicMock(return_value=True)
    audit = MagicMock()
    if esc is not None and monkeypatch is not None:
        monkeypatch.setattr("workflows.consistency_sweep.escalate", esc)
    summary = run_company_tally_consistency_sweep(
        attio=attio,
        list_id="lst",
        today=date(2026, 6, 9),
        dry_run=dry_run,
        advance_fn=advance,
        audit_logger=audit,
    )
    return summary, advance, audit


class TestSweep:
    def test_divergent_company_repaired_with_tally_derived_attrs(
        self, monkeypatch
    ):
        """Divergence case: stamp DM1@2026-06-08, person Accepted/dm_step=0
        -> repair to DM1 Sent / dm_step=1 / last_contact 2026-06-08 /
        next_eligible recomputed from the STAMP date."""
        monkeypatch.setattr(
            "workflows.consistency_sweep.escalate", MagicMock()
        )
        attio = _attio(
            [_company("rec_co", "2026-06-08T12:00:00Z", "rec_p", "DM1")],
            [_entry("ent_1", dm_step=0, stage="Accepted")],
        )
        summary, advance, audit = _sweep(attio)
        assert summary["repaired"] == 1 and summary["escalated"] == 0
        kwargs = advance.call_args.kwargs
        assert kwargs["entry_id"] == "ent_1"
        attrs = kwargs["entry_attributes"]
        assert attrs["dm_step"] == 1
        assert attrs["stage"] == "DM1 Sent"
        assert attrs["last_contact_date"] == "2026-06-08"
        assert attrs["next_eligible_send_date"]  # recomputed, present for dm1

    def test_consistent_company_no_write(self, monkeypatch):
        monkeypatch.setattr(
            "workflows.consistency_sweep.escalate", MagicMock()
        )
        attio = _attio(
            [_company("rec_co", "2026-06-08T12:00:00Z", "rec_p", "DM1")],
            [_entry("ent_1", dm_step=1, stage="DM1 Sent")],
        )
        summary, advance, _ = _sweep(attio)
        assert summary["consistent"] == 1
        advance.assert_not_called()

    def test_person_ahead_of_stamp_no_write(self, monkeypatch):
        """dm_step=2 with a DM1 stamp is NOT divergence (stamp is a
        floor) — never regress."""
        monkeypatch.setattr(
            "workflows.consistency_sweep.escalate", MagicMock()
        )
        attio = _attio(
            [_company("rec_co", "2026-06-08T12:00:00Z", "rec_p", "DM1")],
            [_entry("ent_1", dm_step=2, stage="DM2 Sent")],
        )
        summary, advance, _ = _sweep(attio)
        assert summary["consistent"] == 1
        advance.assert_not_called()

    def test_stage_outside_safe_set_escalates_no_write(self, monkeypatch):
        """dm_step behind but stage=Responded: a write would regress the
        stage (monotonicity halt) — escalate for the operator instead."""
        esc = MagicMock()
        attio = _attio(
            [_company("rec_co", "2026-06-08T12:00:00Z", "rec_p", "DM1")],
            [_entry("ent_1", dm_step=0, stage="Responded")],
        )
        summary, advance, _ = _sweep(
            attio, esc=esc, monkeypatch=monkeypatch
        )
        assert summary["escalated"] == 1 and summary["repaired"] == 0
        advance.assert_not_called()
        assert esc.call_args.kwargs["type"] == "dm_person_advance_desync"

    def test_repair_failure_escalates_idempotently(self, monkeypatch):
        esc = MagicMock()
        attio = _attio(
            [_company("rec_co", "2026-06-08T12:00:00Z", "rec_p", "DM1")],
            [_entry("ent_1", dm_step=0, stage="Accepted")],
        )
        summary, advance, _ = _sweep(
            attio,
            advance=MagicMock(return_value=False),
            esc=esc,
            monkeypatch=monkeypatch,
        )
        assert summary["escalated"] == 1
        assert (
            esc.call_args.kwargs["idempotency_key"]
            == "dm-advance-desync|ent_1|2026-06-08"
        )

    def test_no_list_entry_escalates(self, monkeypatch):
        esc = MagicMock()
        attio = _attio(
            [_company("rec_co", "2026-06-08T12:00:00Z", "rec_p", "DM1")],
            [],
        )
        summary, advance, _ = _sweep(
            attio, esc=esc, monkeypatch=monkeypatch
        )
        assert summary["escalated"] == 1
        advance.assert_not_called()

    def test_invite_stamp_skipped(self, monkeypatch):
        """CONNECTION_SENT stamps have no dm_step floor (same waiver as
        PR #170's throttle exemption)."""
        monkeypatch.setattr(
            "workflows.consistency_sweep.escalate", MagicMock()
        )
        attio = _attio(
            [_company("rec_co", "2026-06-08T12:00:00Z", "rec_p",
                      "CONNECTION_SENT")],
            [],
        )
        summary, advance, _ = _sweep(attio)
        assert summary["skipped_no_floor"] == 1
        advance.assert_not_called()
        attio._filter_and_rank_entries_for_record.assert_not_called()

    def test_malformed_stamp_counted_not_crashed(self, monkeypatch):
        monkeypatch.setattr(
            "workflows.consistency_sweep.escalate", MagicMock()
        )
        attio = _attio(
            [_company("rec_co", "2026-06-08T12:00:00Z", None, "DM1")],
            [],
        )
        summary, advance, _ = _sweep(attio)
        assert summary["skipped_malformed_stamp"] == 1
        advance.assert_not_called()

    def test_dry_run_detects_but_never_writes_or_escalates(
        self, monkeypatch
    ):
        esc = MagicMock()
        attio = _attio(
            [_company("rec_co", "2026-06-08T12:00:00Z", "rec_p", "DM1")],
            [_entry("ent_1", dm_step=0, stage="Accepted")],
        )
        summary, advance, _ = _sweep(
            attio, dry_run=True, esc=esc, monkeypatch=monkeypatch
        )
        assert summary["dry_run_divergent"] == 1
        assert summary["repaired"] == 0
        advance.assert_not_called()
        esc.assert_not_called()

    def test_no_stamped_companies_skips_entry_fetch(self, monkeypatch):
        attio = _attio([], [])
        summary, advance, _ = _sweep(attio)
        assert summary["companies_checked"] == 0
        attio.query_list_entries.assert_not_called()

    def test_unparseable_timestamp_counts_malformed_not_crash(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            "workflows.consistency_sweep.escalate", MagicMock()
        )
        attio = _attio(
            [_company("rec_co", "not-a-date", "rec_p", "DM1")],
            [],
        )
        summary, advance, _ = _sweep(attio)
        assert summary["skipped_malformed_stamp"] == 1
        advance.assert_not_called()

    def test_one_bad_company_does_not_abort_the_rest(self, monkeypatch):
        """Per-company isolation: a RuntimeError on company 1's escalate()
        still lets company 2 be escalated. The final escalate is now wrapped
        best-effort (F-fix), so company 1's failure is counted in
        ``escalate_failed`` (not ``company_errors``) and never aborts the loop."""

        # Both companies have DM1 stamps that are divergent (person entry
        # is at dm_step=0 stage=Responded → unsafe → escalate path).
        # Make escalate() fail on first call but succeed on second.
        esc = MagicMock(side_effect=[RuntimeError("attio down"), None])
        co1 = _company("rec_co1", "2026-06-08T12:00:00Z", "rec_p1", "DM1")
        co2 = _company("rec_co2", "2026-06-08T12:00:00Z", "rec_p2", "DM1")
        attio = MagicMock()
        attio.search_companies.return_value = [co1, co2]
        attio.query_list_entries.return_value = [
            _raw_entry("rec_p1"), _raw_entry("rec_p2")
        ]
        # Both persons have stage=Responded → repair_safe=False → escalate
        entry_responded = _entry("ent_1", dm_step=0, stage="Responded")
        attio._filter_and_rank_entries_for_record.return_value = [
            entry_responded
        ]
        monkeypatch.setattr("workflows.consistency_sweep.escalate", esc)

        summary = run_company_tally_consistency_sweep(
            attio=attio,
            list_id="lst",
            today=date(2026, 6, 9),
            dry_run=False,
            advance_fn=MagicMock(return_value=True),
            audit_logger=MagicMock(),
        )
        # Company 1's escalate raised → swallowed best-effort → escalate_failed,
        # NOT company_errors (the per-company loop never sees the exception).
        assert summary["company_errors"] == 0
        assert summary["escalate_failed"] == 1
        # Company 2 still ran → escalated count should be 1 (company 2)
        assert summary["escalated"] == 1

    def test_invariant_violation_halts_sweep_and_escalates(
        self, monkeypatch
    ):
        from clients.attio_writer import AttioMonotonicityViolation

        esc = MagicMock()
        monkeypatch.setattr("workflows.consistency_sweep.escalate", esc)
        attio = _attio(
            [
                _company("rec_co1", "2026-06-08T12:00:00Z", "rec_p1", "DM1"),
                _company("rec_co2", "2026-06-08T12:00:00Z", "rec_p2", "DM1"),
            ],
            [_entry("ent_1", dm_step=0, stage="Accepted")],
        )
        # Two distinct person IDs across the two companies — supply a raw entry
        # for each so the Fix-2 index builder finds them.
        attio.query_list_entries.return_value = [
            _raw_entry("rec_p1"), _raw_entry("rec_p2")
        ]
        advance = MagicMock(
            side_effect=AttioMonotonicityViolation("regression")
        )
        summary = run_company_tally_consistency_sweep(
            attio=attio,
            list_id="lst",
            today=date(2026, 6, 9),
            dry_run=False,
            advance_fn=advance,
            audit_logger=MagicMock(),
        )
        assert summary["aborted_invariant_violation"] == (
            "AttioMonotonicityViolation"
        )
        # advance was called once (company 1), then sweep halted
        assert advance.call_count == 1
        # escalate was called for the invariant violation
        assert esc.called
        violation_call = next(
            (c for c in esc.call_args_list
             if c.kwargs.get("payload", {}).get("reason") == "invariant_violation"),
            None,
        )
        assert violation_call is not None, (
            "Expected an escalate() call with reason='invariant_violation'"
        )
        # The key carries the exception class so two distinct halt-class
        # violations on the same day open separate queue rows.
        assert violation_call.kwargs["idempotency_key"] == (
            "dm-advance-desync|invariant-violation|"
            "AttioMonotonicityViolation|2026-06-09"
        )

    def test_invariant_violation_keys_distinct_per_error_class(
        self, monkeypatch
    ):
        """Two different halt-class violations on the same date must not
        dedupe into one operator queue row."""
        from clients.attio_writer import (
            AttioMonotonicityViolation,
            AttioTerminalClassRegression,
        )

        keys = set()
        for exc_class in (
            AttioMonotonicityViolation,
            AttioTerminalClassRegression,
        ):
            esc = MagicMock()
            monkeypatch.setattr("workflows.consistency_sweep.escalate", esc)
            attio = _attio(
                [_company("rec_co", "2026-06-08T12:00:00Z", "rec_p", "DM1")],
                [_entry("ent_1", dm_step=0, stage="Accepted")],
            )
            run_company_tally_consistency_sweep(
                attio=attio,
                list_id="lst",
                today=date(2026, 6, 9),
                dry_run=False,
                advance_fn=MagicMock(side_effect=exc_class("boom")),
                audit_logger=MagicMock(),
            )
            keys.add(esc.call_args.kwargs["idempotency_key"])
        assert len(keys) == 2, (
            f"Expected distinct idempotency keys per error class, got {keys}"
        )

    def test_repair_never_pulls_next_eligible_earlier(self, monkeypatch):
        monkeypatch.setattr(
            "workflows.consistency_sweep.escalate", MagicMock()
        )
        entry = _entry("ent_1", dm_step=0, stage="Accepted")
        entry["entry_values"]["next_eligible_send_date"] = [
            {"value": "2027-01-01"}
        ]
        attio = _attio(
            [_company("rec_co", "2026-06-08T12:00:00Z", "rec_p", "DM1")],
            [entry],
        )
        summary, advance, _ = _sweep(attio)
        attrs = advance.call_args.kwargs["entry_attributes"]
        assert attrs["next_eligible_send_date"] == "2027-01-01"


class TestSweepDiagnostics:
    """F-fix: bind/echo per-company errors, guard the final escalate
    best-effort with an escalate_failed counter, and echo the first parse
    failure once per sweep."""

    def test_final_escalate_failure_counted_and_echoed(self, monkeypatch, capsys):
        """A raising final escalate() is swallowed best-effort: escalated stays
        0, escalate_failed increments, and a stderr WARNING names the class."""
        esc = MagicMock(side_effect=RuntimeError("attio 503"))
        attio = _attio(
            [_company("rec_co", "2026-06-08T12:00:00Z", "rec_p", "DM1")],
            [_entry("ent_1", dm_step=0, stage="Responded")],  # unsafe → escalate
        )
        summary, _advance, _ = _sweep(attio, esc=esc, monkeypatch=monkeypatch)
        assert summary["escalated"] == 0
        assert summary["escalate_failed"] == 1
        err = capsys.readouterr().err
        assert "escalate(dm_person_advance_desync) failed" in err
        assert "RuntimeError" in err

    def test_per_company_error_binds_type_and_echoes(self, monkeypatch, capsys):
        """A RAISING repair (advance_fn) lands in the per-company except: the
        bound exception's class name appears in the audit payload + a stderr
        WARNING naming the company."""
        monkeypatch.setattr("workflows.consistency_sweep.escalate", MagicMock())
        attio = _attio(
            [_company("rec_co", "2026-06-08T12:00:00Z", "rec_p", "DM1")],
            [_entry("ent_1", dm_step=0, stage="Accepted")],  # repair-safe
        )
        audit = MagicMock()
        summary = run_company_tally_consistency_sweep(
            attio=attio,
            list_id="lst",
            today=date(2026, 6, 9),
            dry_run=False,
            advance_fn=MagicMock(side_effect=KeyError("missing attr")),
            audit_logger=audit,
        )
        assert summary["company_errors"] == 1
        err = capsys.readouterr().err
        assert "consistency sweep errored on company" in err
        assert "KeyError" in err
        # Audit payload carries the bound error class.
        error_events = [
            c for c in audit.event.call_args_list
            if c.args and c.args[0] == "consistency_sweep_company_error"
        ]
        assert error_events
        assert error_events[0].kwargs.get("error_class") == "KeyError"

    def test_first_parse_failure_echoed_once(self, monkeypatch, capsys):
        """An unparseable raw entry increments entries_unparseable AND echoes the
        FIRST parse exception once — a systematic shape drift is debuggable."""
        monkeypatch.setattr("workflows.consistency_sweep.escalate", MagicMock())

        # Two unparseable raw entries (None trips parse_entry) — only the first
        # should produce a stderr echo.
        attio = MagicMock()
        attio.search_companies.return_value = [
            _company("rec_co", "2026-06-08T12:00:00Z", "rec_p", "DM1")
        ]
        attio.query_list_entries.return_value = [None, None]
        attio._filter_and_rank_entries_for_record.return_value = []

        summary = run_company_tally_consistency_sweep(
            attio=attio,
            list_id="lst",
            today=date(2026, 6, 9),
            dry_run=False,
            advance_fn=MagicMock(return_value=True),
            audit_logger=MagicMock(),
        )
        assert summary["entries_unparseable"] == 2
        err = capsys.readouterr().err
        assert err.count("could not parse a list entry") == 1


def _divergent_fleet(n: int):
    """n companies, each stamped DM1 with its own person, plus the raw
    entries and a MagicMock attio wired so every person resolves to its
    own divergent repair-safe entry (dm_step=0, stage=Accepted).
    Per-company entry ids keep escalation idempotency keys distinct,
    matching production."""
    companies = [
        _company(f"rec_co{i}", "2026-06-08T12:00:00Z", f"rec_p{i}", "DM1")
        for i in range(n)
    ]
    attio = MagicMock()
    attio.search_companies.return_value = companies
    attio.query_list_entries.return_value = [
        _raw_entry(f"rec_p{i}") for i in range(n)
    ]
    attio._filter_and_rank_entries_for_record.side_effect = [
        [_entry(f"ent_{i}", dm_step=0, stage="Accepted")] for i in range(n)
    ]
    return attio


class TestRepairCircuitBreaker:
    """After REPAIR_CIRCUIT_THRESHOLD (3) consecutive repair_write_failed
    outcomes, the sweep must stop attempting repairs (each failure can
    burn AttioWriter's full ~300s retry budget) and escalate the rest
    with reason='repair_circuit_open' — a mid-outage 3-day window must
    not serialize into hours of retries."""

    def _run(self, attio, advance, monkeypatch, esc=None):
        esc = esc or MagicMock()
        monkeypatch.setattr("workflows.consistency_sweep.escalate", esc)
        audit = MagicMock()
        summary = run_company_tally_consistency_sweep(
            attio=attio,
            list_id="lst",
            today=date(2026, 6, 9),
            dry_run=False,
            advance_fn=advance,
            audit_logger=audit,
        )
        return summary, esc, audit

    def test_circuit_opens_after_three_consecutive_failures(
        self, monkeypatch
    ):
        """5 divergent repair-safe companies, every write fails: only 3
        write attempts; companies 4-5 escalate without a write."""
        attio = _divergent_fleet(5)
        advance = MagicMock(return_value=False)
        summary, esc, audit = self._run(attio, advance, monkeypatch)

        assert advance.call_count == REPAIR_CIRCUIT_THRESHOLD, (
            "circuit must open after REPAIR_CIRCUIT_THRESHOLD consecutive "
            f"failed writes — got {advance.call_count} attempts"
        )
        assert summary["repair_circuit_open"] is True
        assert summary["repaired"] == 0
        # Every divergent company still lands in the operator queue.
        assert summary["escalated"] == 5
        reasons = [
            c.kwargs["payload"]["reason"] for c in esc.call_args_list
        ]
        assert reasons == (
            ["repair_write_failed"] * REPAIR_CIRCUIT_THRESHOLD
            + ["repair_circuit_open"] * 2
        )
        # Every company keeps its own queue row — keys must not collide.
        keys = {c.kwargs["idempotency_key"] for c in esc.call_args_list}
        assert len(keys) == 5
        circuit_events = [
            c for c in audit.event.call_args_list
            if c.args and c.args[0] == "consistency_sweep_repair_circuit_open"
        ]
        assert len(circuit_events) == 1, "circuit-open event fires exactly once"
        assert (
            circuit_events[0].kwargs["consecutive_failures"]
            == REPAIR_CIRCUIT_THRESHOLD
        )

    def test_successful_repair_resets_the_streak(self, monkeypatch):
        """F F S F F F across 7 companies: the success at #3 resets the
        counter, so the circuit opens only after the 6th write (3
        consecutive failures at #4-#6) and #7 skips the write."""
        attio = _divergent_fleet(7)
        advance = MagicMock(
            side_effect=[False, False, True, False, False, False]
        )
        summary, esc, _ = self._run(attio, advance, monkeypatch)

        assert advance.call_count == 6
        assert summary["repaired"] == 1
        assert summary["repair_circuit_open"] is True
        assert (
            esc.call_args_list[-1].kwargs["payload"]["reason"]
            == "repair_circuit_open"
        )

    def test_non_write_outcomes_do_not_feed_or_reset_the_streak(
        self, monkeypatch
    ):
        """stage_unsafe escalations are not write attempts: they neither
        increment nor reset the consecutive-failure counter. Pattern
        F, unsafe, F, unsafe, F → circuit opens; a 6th repair-safe
        company escalates as repair_circuit_open with no write."""
        companies = [
            _company(f"rec_co{i}", "2026-06-08T12:00:00Z", f"rec_p{i}", "DM1")
            for i in range(6)
        ]
        attio = MagicMock()
        attio.search_companies.return_value = companies
        attio.query_list_entries.return_value = [
            _raw_entry(f"rec_p{i}") for i in range(6)
        ]
        safe = _entry("ent_safe", dm_step=0, stage="Accepted")
        unsafe = _entry("ent_unsafe", dm_step=0, stage="Responded")
        # Companies 0,2,4,5 resolve repair-safe; 1,3 resolve stage-unsafe.
        attio._filter_and_rank_entries_for_record.side_effect = [
            [safe], [unsafe], [safe], [unsafe], [safe], [safe]
        ]
        advance = MagicMock(return_value=False)
        summary, esc, _ = self._run(attio, advance, monkeypatch)

        # Writes attempted only for the first three repair-safe rows.
        assert advance.call_count == 3
        assert summary["repair_circuit_open"] is True
        reasons = [
            c.kwargs["payload"]["reason"] for c in esc.call_args_list
        ]
        assert reasons == [
            "repair_write_failed",
            "stage_unsafe",
            "repair_write_failed",
            "stage_unsafe",
            "repair_write_failed",
            "repair_circuit_open",
        ]

    def test_circuit_stays_closed_below_threshold(self, monkeypatch):
        """2 failures then end of window: no circuit, both escalate as
        repair_write_failed."""
        attio = _divergent_fleet(2)
        advance = MagicMock(return_value=False)
        summary, esc, audit = self._run(attio, advance, monkeypatch)

        assert advance.call_count == 2
        assert summary["repair_circuit_open"] is False
        reasons = [
            c.kwargs["payload"]["reason"] for c in esc.call_args_list
        ]
        assert reasons == ["repair_write_failed"] * 2
        circuit_events = [
            c for c in audit.event.call_args_list
            if c.args and c.args[0] == "consistency_sweep_repair_circuit_open"
        ]
        assert not circuit_events


def test_sweep_escalation_payloads_pass_real_schema_validation():
    """Every test above mocks escalate(); this pins both payload shapes
    the sweep emits against the real schema validator so a TypedDict
    drift can't silently kill the queue row again.

    Validates:
    - The normal divergence payload (via _divergence_payload)
    - The invariant-violation payload (via _invariant_violation_payload)
    """
    from workflows.consistency_sweep import (
        _divergence_payload,
        _invariant_violation_payload,
    )
    from workflows.escalation import _validate_payload_against_typeddict

    # Minimal divergence dict matching what _check_one_company builds.
    divergence = {
        "company_id": "rec_co",
        "person_record_id": "rec_p",
        "stamped_step": "DM1",
        "stamp_date": "2026-06-08",
        "entry_id": "ent_1",
        "person_dm_step": 0,
        "person_stage": "Accepted",
        "intended_attrs": {"dm_step": 1, "stage": "DM1 Sent"},
    }

    # All reason values used in the divergence path.
    for reason in (
        "no_list_entry",
        "stage_unsafe",
        "repair_write_failed",
        "repair_circuit_open",
    ):
        payload = _divergence_payload(divergence, reason)
        # Must not raise EscalationSchemaError.
        _validate_payload_against_typeddict("dm_person_advance_desync", payload)

    # Minimal company dict matching what _company_record_id expects.
    company = {"id": {"record_id": "rec_co"}}
    inv_payload = _invariant_violation_payload(
        company=company,
        error_class="AttioMonotonicityViolation",
        today_iso="2026-06-09",
    )
    # Must not raise EscalationSchemaError.
    _validate_payload_against_typeddict("dm_person_advance_desync", inv_payload)
    # Verify the sentinel-filled required fields are present and correct types.
    assert inv_payload["reason"] == "invariant_violation"
    assert inv_payload["error_class"] == "AttioMonotonicityViolation"
    assert inv_payload["company_id"] == "rec_co"
    assert inv_payload["person_record_id"] == ""
    assert inv_payload["stamped_step"] == ""
    assert inv_payload["stamp_date"] == "2026-06-09"
    assert inv_payload["entry_id"] == ""
    assert inv_payload["person_dm_step"] == 0
    assert inv_payload["person_stage"] == ""
    assert inv_payload["intended_attrs"] == {}


def test_entries_param_skips_query_list_entries(monkeypatch):
    """Fix 1: passing entries=[...] to run_company_tally_consistency_sweep
    must skip the attio.query_list_entries fetch entirely."""
    monkeypatch.setattr("workflows.consistency_sweep.escalate", MagicMock())
    # Company stamped DM1; person entry is consistent (dm_step=1).
    company = _company("rec_co", "2026-06-08T12:00:00Z", "rec_p", "DM1")
    raw_entry = _raw_entry("rec_p")
    attio = MagicMock()
    attio.search_companies.return_value = [company]
    attio._filter_and_rank_entries_for_record.return_value = [
        _entry("ent_1", dm_step=1, stage="DM1 Sent")
    ]

    run_company_tally_consistency_sweep(
        attio=attio,
        list_id="lst",
        today=date(2026, 6, 9),
        dry_run=False,
        advance_fn=MagicMock(return_value=True),
        audit_logger=MagicMock(),
        entries=[raw_entry],
    )

    # entries was provided → the fetch must have been bypassed.
    attio.query_list_entries.assert_not_called()


def test_index_routes_entries_to_correct_person():
    """Fix 2: the entries_by_record index must route each raw entry to the
    right person — using the REAL _filter_and_rank_entries_for_record (no mock).

    Build 3 raw entries: 2 for person_a (different stages) and 1 for person_b.
    Assert that the sweep finds the right entry for each person and that
    person_b's entry is not confused with person_a's.
    """
    from clients.attio import AttioClient

    def _raw(person_id: str, entry_id: str, stage: str) -> dict:
        return {
            "parent_record_id": person_id,
            "id": {"entry_id": entry_id},
            "created_at": "2026-06-01T00:00:00Z",
            "entry_values": {
                "stage": [{"status": {"title": stage}}],
                "dm_step": [{"value": 1}],
            },
        }

    raw_a1 = _raw("person_a", "ent_a1", "DM1 Sent")
    raw_a2 = _raw("person_a", "ent_a2", "Accepted")
    raw_b1 = _raw("person_b", "ent_b1", "DM2 Sent")
    all_entries = [raw_a1, raw_a2, raw_b1]

    # Build the index the same way the sweep does.
    entries_by_record: dict[str, list] = {}
    for e in all_entries:
        rid = AttioClient.parse_entry(e).get("record_id") or ""
        if rid:
            entries_by_record.setdefault(rid, []).append(e)

    # Simulate a real AttioClient (instantiation not needed — call the method directly).
    attio = AttioClient.__new__(AttioClient)

    # person_a: 2 entries; highest stage = DM1 Sent (ranked first by STAGE_RANK)
    results_a = attio._filter_and_rank_entries_for_record(
        entries_by_record.get("person_a", []), "person_a"
    )
    assert len(results_a) == 2
    top_a_id = results_a[0].get("id", {}).get("entry_id")
    assert top_a_id == "ent_a1", (
        f"Expected highest-ranked person_a entry to be ent_a1 (DM1 Sent), got {top_a_id}"
    )

    # person_b: 1 entry
    results_b = attio._filter_and_rank_entries_for_record(
        entries_by_record.get("person_b", []), "person_b"
    )
    assert len(results_b) == 1
    assert results_b[0].get("id", {}).get("entry_id") == "ent_b1"

    # person_c not in index → empty slice
    results_c = attio._filter_and_rank_entries_for_record(
        entries_by_record.get("person_c", []), "person_c"
    )
    assert results_c == []
