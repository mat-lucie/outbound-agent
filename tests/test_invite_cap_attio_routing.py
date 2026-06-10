"""Invite/visit daily-cap charging via the daily_run row (port of upstream #182).

Split-brain fix: the invite path charged only the legacy local file
(~/.outbound-agent/daily_limits.json) while the DM path charged the daily_run
row — so every daily_run row reported connections_sent=0 and visits_sent=0
forever. These tests pin the ported behaviour on the fork's CRMProvider surface:
the gate reads BOTH sources, and charging goes through the PR-17 two-phase lease
(reserve before PB launch, confirm with the actual charge, release on failure),
exactly like the DM path.

Fork adaptation note: unlike upstream, ``run_connection_requests`` here has no
``compute_invite_outcome`` optimistic-advance step. The amount charged is
``sent_for_cap`` (0 on a gate-fail / silent-no-op batch, ``outcome.sent_count``
on a per-row advance-pass), computed inside the advance-gate branches — so the
connections lease confirms with ``sent_for_cap`` AFTER the branches run, in the
same guarded span as the local-file mirror.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import httpx
import pytest

from tests.fakes import fake_daily_run
from tests.test_integration import _make_attio_entry, _typed_pb_mock

_ENV = {
    "ATTIO_LIST_ID": "list-001",
    "ATTIO_API_KEY": "fake",
    "PHANTOMBUSTER_API_KEY": "fake",
    "GSHEET_AUTOCONNECT_ID": "fake-sheet-id",
    "PB_LI_SESSION_COOKIE": "fake-cookie",
    "PB_LI_USER_AGENT": "TestAgent/1.0",
    "STRICT_PRE_INVITE_DEGREE_CHECK": "false",
}


def _prospect(idx: int) -> dict:
    return _make_attio_entry(
        entry_id=f"entry-{idx:03d}",
        record_id=f"rec-{idx:03d}",
        stage="Prospect",
        quality_score=75,
    )


def _cs_entry(idx: int) -> dict:
    """A CONNECTION_SENT entry eligible for a re-check (profile visit)."""
    return _make_attio_entry(
        entry_id=f"entry-cs-{idx:03d}",
        record_id=f"rec-cs-{idx:03d}",
        stage="Connection Sent",
        quality_score=80,
    )


class TestAttioGate:
    @patch.dict(os.environ, _ENV)
    def test_attio_remaining_zero_blocks_run(self):
        """daily_run.remaining('connections') == 0 short-circuits exactly like
        the local can_send_connections gate — no PB launch, reason=daily_limit."""
        from workflows.daily_check import run_connection_requests

        attio = MagicMock()
        attio.is_person_company_corrupted.return_value = False
        attio.query_list_entries.return_value = [_prospect(1)]
        pb = MagicMock()
        dr = fake_daily_run(remaining=0)

        with patch("workflows.daily_check.RecordCache.get",
                   return_value=("Test Person", "Test Co",
                                 "https://linkedin.com/in/testperson", "", "")), \
             patch("workflows.daily_check.can_send_connections", return_value=True), \
             patch("workflows.daily_check.get_remaining",
                   return_value={"connections": 25, "messages": 30, "visits": 50}):
            result = run_connection_requests(
                attio=attio, pb=pb, network_booster_id="agent-nb-001",
                auto_confirm=True, daily_run=dr,
            )

        assert result["reason"] == "daily_limit"
        pb.launch_agent.assert_not_called()

    @patch.dict(os.environ, _ENV)
    def test_target_trims_to_attio_remaining(self, capsys):
        """target = min(local remaining, daily_run remaining, batch_size): with
        daily_run remaining=1 and 2 eligible prospects, only 1 invite queues,
        and the trim echo names daily_run as the binding ledger."""
        from workflows.daily_check import run_connection_requests

        attio = MagicMock()
        attio.is_person_company_corrupted.return_value = False
        attio.query_list_entries.return_value = [_prospect(0), _prospect(1)]
        pb = MagicMock()
        dr = fake_daily_run(remaining=1)

        def _cache(record_id):
            idx = int(record_id.split("-")[-1])
            return (f"Person {idx}", f"Company {idx}",
                    f"https://linkedin.com/in/person{idx}", "", "")

        with patch("workflows.daily_check.RecordCache.get", side_effect=_cache), \
             patch("workflows.daily_check.can_send_connections", return_value=True), \
             patch("workflows.daily_check.get_remaining",
                   return_value={"connections": 25, "messages": 30, "visits": 50}):
            result = run_connection_requests(
                attio=attio, pb=pb, network_booster_id="agent-nb-001",
                auto_confirm=True, dry_run=True, daily_run=dr,
            )

        assert result["dry_run"] == 1  # trimmed to daily_run remaining, not 2
        captured = capsys.readouterr()
        assert "daily_run remaining: 1" in captured.out, (
            f"Expected trim echo to name daily_run as the binding ledger, "
            f"got:\n{captured.out}"
        )


class TestVisitBudgetTrim:
    @patch.dict(os.environ, _ENV)
    def test_rechecks_trimmed_to_visit_budget(self, capsys):
        """With visit budget 1 (daily_run) and 2 stale CONNECTION_SENT re-check
        rows, only 1 re-check row queues into the combined PB batch."""
        from workflows.daily_check import run_connection_requests

        attio = MagicMock()
        attio.is_person_company_corrupted.return_value = False
        attio.query_list_entries.return_value = [_cs_entry(0), _cs_entry(1)]
        pb = MagicMock()

        dr = fake_daily_run()
        dr.remaining.side_effect = lambda kind: {"connections": 25, "visits": 1}[kind]

        def _cache(record_id):
            idx = int(record_id.split("-")[-1])
            return (f"Person {idx}", f"Co {idx}",
                    f"https://linkedin.com/in/person-cs-{idx}", "", "")

        with patch("workflows.daily_check.RecordCache.get", side_effect=_cache), \
             patch("workflows.daily_check.can_send_connections", return_value=True), \
             patch("workflows.daily_check.get_remaining",
                   return_value={"connections": 25, "messages": 30, "visits": 50}), \
             patch("workflows.daily_check.recheck_cache") as mock_rc:
            mock_rc.partition.side_effect = lambda urls: ({}, list(urls))
            mock_rc.RECHECK_TTL_DAYS = 3
            result = run_connection_requests(
                attio=attio, pb=pb, network_booster_id="agent-nb-001",
                auto_confirm=True, dry_run=True, daily_run=dr,
            )

        assert result["dry_run_rechecks"] == 1, (
            f"Expected dry_run_rechecks=1 (visit budget=1 trims 2 → 1), got: {result}"
        )
        captured = capsys.readouterr()
        assert "visit budget" in captured.out.lower(), (
            f"Expected trim echo naming the visit budget, got:\n{captured.out}"
        )

    @patch.dict(os.environ, _ENV)
    def test_local_file_binds_visit_trim(self, capsys):
        """When the local file (get_remaining visits=0) is lower than the
        daily_run visits remaining (=5), all re-checks trim and the echo names
        'local file' as the binding ledger."""
        from workflows.daily_check import run_connection_requests

        attio = MagicMock()
        attio.is_person_company_corrupted.return_value = False
        attio.query_list_entries.return_value = [_cs_entry(0), _cs_entry(1)]
        pb = MagicMock()

        dr = fake_daily_run()
        dr.remaining.side_effect = lambda kind: {"connections": 25, "visits": 5}[kind]

        def _cache(record_id):
            idx = int(record_id.split("-")[-1])
            return (f"Person {idx}", f"Co {idx}",
                    f"https://linkedin.com/in/person-cs-{idx}", "", "")

        with patch("workflows.daily_check.RecordCache.get", side_effect=_cache), \
             patch("workflows.daily_check.can_send_connections", return_value=True), \
             patch("workflows.daily_check.get_remaining",
                   return_value={"connections": 25, "messages": 30, "visits": 0}), \
             patch("workflows.daily_check.recheck_cache") as mock_rc:
            mock_rc.partition.side_effect = lambda urls: ({}, list(urls))
            mock_rc.RECHECK_TTL_DAYS = 3
            result = run_connection_requests(
                attio=attio, pb=pb, network_booster_id="agent-nb-001",
                auto_confirm=True, dry_run=True, daily_run=dr,
            )

        assert result.get("dry_run_rechecks", 0) == 0, (
            f"Expected dry_run_rechecks=0 when local file visits=0, got: {result}"
        )
        captured = capsys.readouterr()
        assert "local file" in captured.out.lower(), (
            f"Expected trim echo to name 'local file' as binding ledger, "
            f"got:\n{captured.out}"
        )


class TestInviteLease:
    """Two-phase reserve/confirm lease around the Network Booster launch,
    mirroring the DM-path PR-17 lease: reserve before PB is touched, confirm
    with the actual charge, release in finally on failure."""

    @staticmethod
    def _clean_pb() -> MagicMock:
        """Clean launch whose CSV marks the prospect URL 'Message sent', so the
        fork's advance gate passes and sent_for_cap == 1."""
        return _typed_pb_mock(
            csv_text=(
                "query,status\n"
                "https://linkedin.com/in/testperson,Message sent\n"
            ),
            container_id="c-lease",
        )

    @patch.dict(os.environ, _ENV)
    def test_clean_launch_reserves_and_confirms_sent_count(self):
        """Clean launch: reserve_send('connections', 1) BEFORE pb.launch_agent;
        confirm_lease with confirmed_count=1 (sent_for_cap); release never
        fires; legacy local mirror record_connections(1) still fires once."""
        from workflows.daily_check import run_connection_requests

        attio = MagicMock()
        attio.is_person_company_corrupted.return_value = False
        attio.query_list_entries.return_value = [_prospect(0)]
        pb = self._clean_pb()
        dr = fake_daily_run()

        with patch("workflows.daily_check.RecordCache.get",
                   return_value=("Test Person", "Test Co",
                                 "https://linkedin.com/in/testperson", "", "")), \
             patch("workflows.daily_check.can_send_connections", return_value=True), \
             patch("workflows.daily_check.get_remaining",
                   return_value={"connections": 25, "messages": 30, "visits": 50}), \
             patch("workflows.daily_check.write_prospects_to_sheet",
                   return_value="https://docs.google.com/spreadsheets/d/fake"), \
             patch("workflows.daily_check.record_connections") as mock_record, \
             patch("workflows.daily_check.record_visits"):
            run_connection_requests(
                attio=attio, pb=pb, network_booster_id="agent-nb-001",
                auto_confirm=True, daily_run=dr,
            )

        dr.reserve_send.assert_called_once_with("connections", 1)
        assert pb.launch_agent.called
        dr.confirm_lease.assert_called_once_with("fake-lease-token", confirmed_count=1)
        dr.release_lease.assert_not_called()
        mock_record.assert_called_once_with(1)

    @patch.dict(os.environ, _ENV)
    def test_optimistic_advance_charges_advanced_count(self):
        """Post-#164 the invite path advances OPTIMISTICALLY on a clean
        authenticated launch even when the Network Booster CSV confirms zero
        sends. The connections lease must then confirm with the optimistically-
        advanced count (= requested invites), NOT the parsed sent_count of 0 —
        otherwise the cap charge would drift below the rows actually advanced."""
        from workflows.daily_check import run_connection_requests

        attio = MagicMock()
        attio.is_person_company_corrupted.return_value = False
        attio.query_list_entries.return_value = [_prospect(30)]
        # Clean launch, but the CSV reports zero "Message sent" rows (the NB
        # CSV bug). compute_invite_outcome overrides → sent_for_cap = 1.
        pb = _typed_pb_mock(
            csv_text="query,error\nhttps://linkedin.com/in/optperson,\n",
            container_id="c-optimistic",
            log="🔄 Adding profiles...\n✅ CSV saved\nProcess finished successfully",
        )
        dr = fake_daily_run()

        with patch("workflows.daily_check.RecordCache.get",
                   return_value=("Opt Person", "Co",
                                 "https://linkedin.com/in/optperson", "", "")), \
             patch("workflows.daily_check.can_send_connections", return_value=True), \
             patch("workflows.daily_check.get_remaining",
                   return_value={"connections": 25, "messages": 30, "visits": 50}), \
             patch("workflows.daily_check.write_prospects_to_sheet",
                   return_value="https://docs.google.com/spreadsheets/d/fake"), \
             patch("workflows.daily_check.record_connections") as mock_record, \
             patch("workflows.daily_check.record_visits"):
            run_connection_requests(
                attio=attio, pb=pb, network_booster_id="agent-nb-001",
                auto_confirm=True, daily_run=dr,
            )

        dr.confirm_lease.assert_called_once_with("fake-lease-token", confirmed_count=1)
        dr.release_lease.assert_not_called()
        mock_record.assert_called_once_with(1)

    @patch.dict(os.environ, _ENV)
    def test_cap_blocked_launch_charges_zero(self):
        """A LinkedIn cap/restriction launch must NOT optimistically advance —
        invites were silently dropped — so the connections lease confirms with
        0 (capacity refunded), mirroring the auth-failure gate-fail path."""
        from workflows.daily_check import run_connection_requests

        attio = MagicMock()
        attio.is_person_company_corrupted.return_value = False
        attio.query_list_entries.return_value = [_prospect(31)]
        pb = _typed_pb_mock(
            csv_text="query,error\nhttps://linkedin.com/in/capperson,\n",
            container_id="c-capped",
            log="🔄 Adding profiles...\n⚠️ You've reached the weekly invitation limit.",
        )
        dr = fake_daily_run()

        with patch("workflows.daily_check.RecordCache.get",
                   return_value=("Cap Person", "Co",
                                 "https://linkedin.com/in/capperson", "", "")), \
             patch("workflows.daily_check.can_send_connections", return_value=True), \
             patch("workflows.daily_check.get_remaining",
                   return_value={"connections": 25, "messages": 30, "visits": 50}), \
             patch("workflows.daily_check.write_prospects_to_sheet",
                   return_value="https://docs.google.com/spreadsheets/d/fake"), \
             patch("workflows.daily_check.record_connections") as mock_record, \
             patch("workflows.daily_check.record_visits"), \
             patch("workflows.daily_check.emit_pb_silent_no_op"):
            run_connection_requests(
                attio=attio, pb=pb, network_booster_id="agent-nb-001",
                auto_confirm=True, daily_run=dr,
            )

        dr.confirm_lease.assert_called_once_with("fake-lease-token", confirmed_count=0)
        mock_record.assert_called_once_with(0)

    @patch.dict(os.environ, _ENV)
    def test_pb_failure_releases_lease_without_confirm(self):
        """pb.wait_for_completion raising PBRunFailed → lease released, confirm
        never called, exception propagates (the §3.1 quota-leak guard)."""
        from datetime import UTC, datetime

        from clients.pb_envelope import PBLaunch, PBRunFailed, hash_arguments
        from workflows.daily_check import run_connection_requests

        attio = MagicMock()
        attio.is_person_company_corrupted.return_value = False
        attio.query_list_entries.return_value = [_prospect(1)]

        launch = PBLaunch(
            container_id="c-pbfail",
            agent_id="agent-nb",
            launched_at=datetime(2026, 6, 9, 12, 0, tzinfo=UTC),
            arguments_sha256=hash_arguments(None),
        )
        pb = MagicMock()
        pb.launch_agent.return_value = launch
        pb.wait_for_completion.side_effect = PBRunFailed(
            container_id="c-pbfail",
            agent_id="agent-nb",
            log_tail="PB reported status=error",
        )
        dr = fake_daily_run()

        with patch("workflows.daily_check.RecordCache.get",
                   return_value=("PB Fail Person", "Co",
                                 "https://linkedin.com/in/pbfailperson", "", "")), \
             patch("workflows.daily_check.can_send_connections", return_value=True), \
             patch("workflows.daily_check.get_remaining",
                   return_value={"connections": 25, "messages": 30, "visits": 50}), \
             patch("workflows.daily_check.write_prospects_to_sheet",
                   return_value="https://docs.google.com/spreadsheets/d/fake"), \
             patch("workflows.daily_check.record_connections"), \
             patch("workflows.daily_check.record_visits"), \
             pytest.raises(PBRunFailed):
            run_connection_requests(
                attio=attio, pb=pb, network_booster_id="agent-nb-001",
                auto_confirm=True, daily_run=dr,
            )

        dr.release_lease.assert_called_once_with("fake-lease-token")
        dr.confirm_lease.assert_not_called()

    @patch.dict(os.environ, _ENV)
    def test_gate_fail_confirms_zero(self):
        """Auth-failure launch (advance gate FAILS → sent_for_cap=0): the
        connections lease confirms with confirmed_count=0 — capacity refunded,
        not consumed. release never fires (the lease was consumed).

        Post-#164 the invite path advances OPTIMISTICALLY on a clean launch, so
        the genuine gate-fail case is a dead-cookie auth failure (invites NOT
        sent) — that is what must charge 0."""
        from workflows.daily_check import run_connection_requests

        attio = MagicMock()
        attio.is_person_company_corrupted.return_value = False
        attio.query_list_entries.return_value = [_prospect(2)]
        # Auth failure → compute_invite_outcome leaves the outcome Skipped →
        # gate fails → sent_for_cap=0. (A clean launch would optimistically
        # advance and charge 1, defeating the purpose of this test.)
        pb = _typed_pb_mock(
            csv_text=None, container_id="c-gatefail",
            log="❌ No valid credentials found",
        )
        dr = fake_daily_run()

        with patch("workflows.daily_check.RecordCache.get",
                   return_value=("Gate Fail", "Co",
                                 "https://linkedin.com/in/gatefailperson", "", "")), \
             patch("workflows.daily_check.can_send_connections", return_value=True), \
             patch("workflows.daily_check.get_remaining",
                   return_value={"connections": 25, "messages": 30, "visits": 50}), \
             patch("workflows.daily_check.write_prospects_to_sheet",
                   return_value="https://docs.google.com/spreadsheets/d/fake"), \
             patch("workflows.daily_check.record_connections") as mock_record, \
             patch("workflows.daily_check.record_visits"), \
             patch("workflows.daily_check.emit_pb_silent_no_op"):
            run_connection_requests(
                attio=attio, pb=pb, network_booster_id="agent-nb-001",
                auto_confirm=True, daily_run=dr,
            )

        dr.confirm_lease.assert_called_once_with("fake-lease-token", confirmed_count=0)
        dr.release_lease.assert_not_called()
        mock_record.assert_called_once_with(0)

    @patch.dict(os.environ, _ENV)
    def test_rechecks_reserve_and_confirm_visits(self):
        """A batch with a re-check row reserves and confirms a 'visits' lease
        for len(recheck_data) alongside the connections lease. On a clean
        launch the visits confirm commits the full reservation
        (confirmed_count == len(recheck_data))."""
        from workflows.daily_check import run_connection_requests

        attio = MagicMock()
        attio.is_person_company_corrupted.return_value = False
        attio.query_list_entries.return_value = [_prospect(10), _cs_entry(0)]
        pb = _typed_pb_mock(
            csv_text=(
                "query,status\n"
                "https://linkedin.com/in/invperson,Message sent\n"
            ),
            container_id="c-rc-lease",
        )

        dr = fake_daily_run()
        tokens = {"connections": "conn-token", "visits": "visit-token"}
        dr.reserve_send.side_effect = lambda kind, count: tokens[kind]

        def _cache(record_id):
            if "cs" in record_id:
                return ("RC Person", "Acme", "https://linkedin.com/in/rcperson", "", "")
            return ("Inv Person", "Acme", "https://linkedin.com/in/invperson", "", "")

        with patch("workflows.daily_check.RecordCache.get", side_effect=_cache), \
             patch("workflows.daily_check.can_send_connections", return_value=True), \
             patch("workflows.daily_check.get_remaining",
                   return_value={"connections": 25, "messages": 30, "visits": 50}), \
             patch("workflows.daily_check.write_prospects_to_sheet",
                   return_value="https://docs.google.com/spreadsheets/d/fake"), \
             patch("workflows.daily_check.record_connections"), \
             patch("workflows.daily_check.record_visits"), \
             patch("workflows.daily_check.recheck_cache") as mock_rc:
            mock_rc.partition.side_effect = lambda urls: ({}, list(urls))
            mock_rc.RECHECK_TTL_DAYS = 3
            run_connection_requests(
                attio=attio, pb=pb, network_booster_id="agent-nb-001",
                auto_confirm=True, daily_run=dr,
            )

        dr.reserve_send.assert_any_call("visits", 1)
        # Clean launch (advance gate passed) → visits charged in full.
        dr.confirm_lease.assert_any_call("visit-token", confirmed_count=1)

    @patch.dict(os.environ, _ENV)
    def test_gate_fail_with_rechecks_confirms_visits_zero_and_skips_cache(self):
        """E-fix: on a gate-fail (auth-failure) launch the re-check profiles
        were NOT reliably visited, so the visits lease confirms 0 (capacity
        refunded) AND the recheck cache is NOT stamped — otherwise tomorrow's
        run would skip a re-check that never happened."""
        from workflows.daily_check import run_connection_requests

        attio = MagicMock()
        attio.is_person_company_corrupted.return_value = False
        attio.query_list_entries.return_value = [_prospect(40), _cs_entry(0)]
        # Auth failure → advance gate FAILS → sent_for_cap=0, gate-fail branch.
        pb = _typed_pb_mock(
            csv_text=None, container_id="c-gatefail-rc",
            log="❌ No valid credentials found",
        )

        dr = fake_daily_run()
        tokens = {"connections": "conn-token", "visits": "visit-token"}
        dr.reserve_send.side_effect = lambda kind, count: tokens[kind]

        def _cache(record_id):
            if "cs" in record_id:
                return ("RC Person", "Acme", "https://linkedin.com/in/rcperson", "", "")
            return ("Inv Person", "Acme", "https://linkedin.com/in/invperson", "", "")

        with patch("workflows.daily_check.RecordCache.get", side_effect=_cache), \
             patch("workflows.daily_check.can_send_connections", return_value=True), \
             patch("workflows.daily_check.get_remaining",
                   return_value={"connections": 25, "messages": 30, "visits": 50}), \
             patch("workflows.daily_check.write_prospects_to_sheet",
                   return_value="https://docs.google.com/spreadsheets/d/fake"), \
             patch("workflows.daily_check.record_connections"), \
             patch("workflows.daily_check.record_visits") as mock_visits, \
             patch("workflows.daily_check.emit_pb_silent_no_op"), \
             patch("workflows.daily_check.recheck_cache") as mock_rc:
            mock_rc.partition.side_effect = lambda urls: ({}, list(urls))
            mock_rc.RECHECK_TTL_DAYS = 3
            run_connection_requests(
                attio=attio, pb=pb, network_booster_id="agent-nb-001",
                auto_confirm=True, daily_run=dr,
            )

        # Visit lease confirmed with 0 (not the full reservation).
        dr.confirm_lease.assert_any_call("visit-token", confirmed_count=0)
        mock_visits.assert_called_once_with(0)
        # Cache NOT stamped on a gate-fail.
        mock_rc.record_many.assert_not_called()

    @patch.dict(os.environ, _ENV)
    def test_advance_loop_raise_echoes_sent_but_uncharged(self, capsys):
        """D-fix: a clean launch passes the advance gate, but a raise inside the
        per-row advance loop (e.g. the deliberately re-raised
        UnauthorizedAttioWriteError) aborts before confirm — refunding the
        leases though PB already sent the batch. A loud post-launch echo must
        fire naming the physically-sent-but-uncharged invites, and the
        exception must propagate."""
        from clients.attio_writer import UnauthorizedAttioWriteError
        from workflows.daily_check import run_connection_requests

        attio = MagicMock()
        attio.is_person_company_corrupted.return_value = False
        attio.query_list_entries.return_value = [_prospect(50)]
        pb = self._clean_pb()
        dr = fake_daily_run()

        with patch("workflows.daily_check.RecordCache.get",
                   return_value=("Advance Fail", "Co",
                                 "https://linkedin.com/in/testperson", "", "")), \
             patch("workflows.daily_check.can_send_connections", return_value=True), \
             patch("workflows.daily_check.get_remaining",
                   return_value={"connections": 25, "messages": 30, "visits": 50}), \
             patch("workflows.daily_check.write_prospects_to_sheet",
                   return_value="https://docs.google.com/spreadsheets/d/fake"), \
             patch("workflows.daily_check.record_connections"), \
             patch("workflows.daily_check.record_visits"), \
             patch("workflows.daily_check._attio_advance_with_escalation",
                   side_effect=UnauthorizedAttioWriteError("registry denied")), \
             pytest.raises(UnauthorizedAttioWriteError):
            run_connection_requests(
                attio=attio, pb=pb, network_booster_id="agent-nb-001",
                auto_confirm=True, daily_run=dr,
            )

        captured = capsys.readouterr()
        assert "launch completed" in captured.err.lower(), (
            f"Expected post-launch echo in stderr, got:\n{captured.err}"
        )
        assert "UNCHARGED" in captured.err, (
            f"Expected 'UNCHARGED' in stderr, got:\n{captured.err}"
        )
        # The connections lease must have been refunded (released, not confirmed).
        dr.release_lease.assert_any_call("fake-lease-token")

    @patch.dict(os.environ, _ENV)
    def test_confirm_transport_error_echoes_sent_but_uncharged(self, capsys):
        """When confirm_lease raises httpx.RequestError, the exception
        propagates AND a loud operator echo names the completed PB launch and
        the failed charge. record_connections must NOT fire — the local mirror
        stays in lockstep with the failed daily_run charge."""
        from workflows.daily_check import run_connection_requests

        attio = MagicMock()
        attio.is_person_company_corrupted.return_value = False
        attio.query_list_entries.return_value = [_prospect(20)]
        pb = self._clean_pb()

        dr = fake_daily_run()
        dr.confirm_lease.side_effect = httpx.RequestError("attio down")

        with patch("workflows.daily_check.RecordCache.get",
                   return_value=("Transport Person", "Co",
                                 "https://linkedin.com/in/testperson", "", "")), \
             patch("workflows.daily_check.can_send_connections", return_value=True), \
             patch("workflows.daily_check.get_remaining",
                   return_value={"connections": 25, "messages": 30, "visits": 50}), \
             patch("workflows.daily_check.write_prospects_to_sheet",
                   return_value="https://docs.google.com/spreadsheets/d/fake"), \
             patch("workflows.daily_check.record_connections") as mock_record, \
             patch("workflows.daily_check.record_visits"), \
             pytest.raises(httpx.RequestError):
            run_connection_requests(
                attio=attio, pb=pb, network_booster_id="agent-nb-001",
                auto_confirm=True, daily_run=dr,
            )

        captured = capsys.readouterr()
        assert "PB launch completed" in captured.err, (
            f"Expected 'PB launch completed' in stderr, got:\n{captured.err}"
        )
        assert "FAILED" in captured.err, (
            f"Expected 'FAILED' in stderr, got:\n{captured.err}"
        )
        mock_record.assert_not_called()

    @patch.dict(os.environ, _ENV)
    def test_record_connections_fires_after_confirm(self):
        """Order guard: record_connections fires inside the guarded span,
        immediately after confirm_lease, with the same value (sent_for_cap)."""
        from workflows.daily_check import run_connection_requests

        attio = MagicMock()
        attio.is_person_company_corrupted.return_value = False
        attio.query_list_entries.return_value = [_prospect(21)]
        pb = self._clean_pb()
        dr = fake_daily_run()

        call_order: list[str] = []
        dr.confirm_lease.side_effect = lambda *a, **k: call_order.append("confirm_lease")

        with patch("workflows.daily_check.RecordCache.get",
                   return_value=("Order Guard", "Co",
                                 "https://linkedin.com/in/testperson", "", "")), \
             patch("workflows.daily_check.can_send_connections", return_value=True), \
             patch("workflows.daily_check.get_remaining",
                   return_value={"connections": 25, "messages": 30, "visits": 50}), \
             patch("workflows.daily_check.write_prospects_to_sheet",
                   return_value="https://docs.google.com/spreadsheets/d/fake"), \
             patch("workflows.daily_check.record_connections",
                   side_effect=lambda n: call_order.append(f"record_connections({n})")), \
             patch("workflows.daily_check.record_visits"):
            run_connection_requests(
                attio=attio, pb=pb, network_booster_id="agent-nb-001",
                auto_confirm=True, daily_run=dr,
            )

        assert call_order[0] == "confirm_lease", (
            f"confirm_lease must fire before record_connections; got: {call_order}"
        )
        assert call_order[1] == "record_connections(1)", (
            f"record_connections(1) must fire right after confirm; got: {call_order}"
        )
