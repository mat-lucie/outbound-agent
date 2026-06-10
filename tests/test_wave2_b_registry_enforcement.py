"""Wave-2-B regression tests: §3.15 bypass call sites route through AttioWriter.

Pre-Wave-2-B these sites called ``attio.update_list_entry`` /
``attio.update_company`` / ``attio._client.request`` / ``attio._request``
directly, bypassing ``AttioWriter._check_write_owner`` so the §3.15
registry-as-contract was documentation-only.

Each test below asserts that the production helper now invokes
``AttioWriter.apply`` (verified by spying on ``_check_write_owner``)
when called against the same fixture the bypass tests used.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from clients.attio_writer import AttioWriter, WriteIntent


@pytest.fixture
def writer_spy(monkeypatch):
    """Spy on AttioWriter._check_write_owner to count calls + arguments.

    Returns ``(spy, original)`` so tests can assert on calls while the
    real registry check still fires (registry violations still raise
    UnauthorizedAttioWriteError).
    """
    calls: list[WriteIntent] = []
    original = AttioWriter._check_write_owner

    def _spy(self, intent: WriteIntent) -> None:
        calls.append(intent)
        return original(self, intent)

    monkeypatch.setattr(AttioWriter, "_check_write_owner", _spy)
    return calls


class TestDailyCheckHelpersRouteThroughAttioWriter:
    """``_attio_advance_with_escalation`` + ``_write_company_throttle_tally``
    now thread ``writer_module`` to ``AttioWriter.apply``."""

    def test_attio_advance_with_escalation_invokes_registry_check(
        self, writer_spy, monkeypatch,
    ):
        from workflows.daily_check import _attio_advance_with_escalation

        attio = MagicMock()
        ok = _attio_advance_with_escalation(
            attio=attio,
            entry_id="entry_xyz",
            entry_attributes={
                "stage": "DM1 Sent",
                "dm_step": 1,
                "last_contact_date": "2026-05-25",
            },
            list_id="list-test",
            linkedin_url="https://li.com/in/x",
            today="2026-05-25",
            step_label="dm1",
            writer_module="workflows.daily_check.run_dm_sequencing",
            prior_stage="Accepted",
        )
        assert ok is True
        # AttioWriter._check_write_owner was invoked at least once with
        # the right writer_module attribution.
        matching = [
            c for c in writer_spy
            if c.writer_module == "workflows.daily_check.run_dm_sequencing"
            and c.object == "linkedin_outreach"
            and c.is_list_entry
        ]
        assert matching, (
            "PR-B regression: _attio_advance_with_escalation did NOT route "
            "through AttioWriter so §3.15 registry never fired"
        )

    def test_write_company_throttle_tally_invokes_registry_check(self, writer_spy):
        from workflows.daily_check import _write_company_throttle_tally

        attio = MagicMock()
        _write_company_throttle_tally(
            attio=attio,
            company_id="company_abc",
            person_record_id="person_def",
            step_label="dm1",
            experiment_id="exp_2026",
            today=date(2026, 5, 25),
        )
        matching = [
            c for c in writer_spy
            if c.writer_module == "workflows.daily_check.run_dm_sequencing"
            and c.object == "companies"
            and c.record_id == "company_abc"
        ]
        assert matching, (
            "PR-B regression: _write_company_throttle_tally did NOT route "
            "through AttioWriter so §3.15 registry never fired"
        )


# NOTE: ``_patch_resend_message_id`` + ``_upsert_kpi_snapshot`` no longer
# route through AttioWriter — Wave-2-A moved the weekly_kpi_snapshot
# sidecar to the filesystem (reports/weekly-kpi/<week>.json). The former
# TestWeeklyReportRoutesThroughAttioWriter class was removed with that
# migration; filesystem behavior is covered in test_pr30_weekly_report.py.


class TestWeeklyProspectHQAttrsRoutesThroughAttioWriter:
    """``_write_hq_attrs`` now writes via AttioWriter."""

    def test_write_hq_attrs_invokes_registry_check(self, writer_spy):
        from workflows.weekly_prospect import HQClassificationResult, _write_hq_attrs

        attio = MagicMock()
        summary = {"written": 0, "api_errors": 0}
        result = HQClassificationResult(country="mexico", confidence=0.9)

        _write_hq_attrs(attio, "company_xyz", "Test Co", result, summary)

        matching = [
            c for c in writer_spy
            if c.writer_module == "workflows.weekly_prospect.classify_company_hq"
            and c.object == "companies"
            and c.record_id == "company_xyz"
        ]
        assert matching, (
            "PR-B regression: _write_hq_attrs did NOT route through "
            "AttioWriter so §3.15 registry never fired"
        )
        assert summary["written"] == 1


class TestLLMBudgetLedgerNotInitialized:
    """``LLMBudgetLedger.try_reserve`` raises typed exception when
    production dispatch is enabled and ATTIO_API_KEY is unset."""

    def test_raises_when_dispatch_enabled_and_no_attio_key(self, monkeypatch):
        from workflows.llm_budget import (
            LLMBudgetLedger,
            LLMBudgetLedgerNotInitialized,
        )
        monkeypatch.delenv("ATTIO_API_KEY", raising=False)
        monkeypatch.setenv("OUTBOUND_USE_LLM_DISPATCH", "1")
        with pytest.raises(LLMBudgetLedgerNotInitialized) as exc_info:
            LLMBudgetLedger.try_reserve("industry_classification", 0.5)
        # Carries forensic context: step + reason.
        assert exc_info.value.step == "industry_classification"
        assert "ATTIO_API_KEY" in exc_info.value.reason
        # Wave-2-B fix-up (multi-agent B4): reason names the two gate
        # signals (require_attio_ledger + is_dispatch_enabled) so an
        # operator reading the error can immediately see which gate
        # fired. is_dispatch_enabled=True here because of the env var.
        assert "is_dispatch_enabled=True" in exc_info.value.reason

    def test_raises_when_require_attio_ledger_signal_set(self, monkeypatch):
        """Wave-2-B fix-up (multi-agent B4): the explicit
        ``require_attio_ledger=True`` signal MUST also trigger the
        fail-loud path so callers that enable dispatch via a
        ``use_llm_dispatch=True`` parameter (rather than the env var)
        still hit the §3.7 cap-enforcement gate."""
        from workflows.llm_budget import (
            LLMBudgetLedger,
            LLMBudgetLedgerNotInitialized,
        )
        monkeypatch.delenv("ATTIO_API_KEY", raising=False)
        monkeypatch.delenv("OUTBOUND_USE_LLM_DISPATCH", raising=False)
        with pytest.raises(LLMBudgetLedgerNotInitialized) as exc_info:
            LLMBudgetLedger.try_reserve(
                "industry_classification", 0.5,
                require_attio_ledger=True,
            )
        assert "require_attio_ledger=True" in exc_info.value.reason

    def test_fail_soft_when_dispatch_disabled(self, monkeypatch, capsys):
        # When dispatch is NOT enabled (default dev/test posture), the
        # ledger keeps the fail-soft warning path so the autouse
        # ``_isolate_attio_api_key`` conftest fixture doesn't crash
        # every test that touches LLM dispatch.
        from workflows.llm_budget import LLMBudgetLedger
        monkeypatch.delenv("ATTIO_API_KEY", raising=False)
        monkeypatch.delenv("OUTBOUND_USE_LLM_DISPATCH", raising=False)
        result = LLMBudgetLedger.try_reserve("industry_classification", 0.5)
        assert result is None
        captured = capsys.readouterr()
        assert "ATTIO_API_KEY unset" in captured.err


class TestAttioRateLimitExhaustedCauseChain:
    """Wave-2-B fix-up (multi-agent test gap 2): ``__cause__`` chaining
    is what enables the network-vs-HTTPStatusError discrimination in
    ``workflows/detect_responses.py`` classify_reply path. Pin the
    contract here so a future refactor that drops ``from last_exc``
    fails the test before it can silently invert the swallow semantics
    in production code."""

    def test_chains_httpstatuserror_when_5xx_exhausted(self, monkeypatch):
        import httpx

        from clients.attio_writer import (
            AttioRateLimitExhausted,
            AttioWriter,
        )
        # Tight retry budget so the test doesn't hang on real sleep.
        monkeypatch.setattr("clients.attio_writer.RETRY_BUDGET_SECONDS", 0.0001)
        mock_attio = MagicMock()
        response = MagicMock(spec=httpx.Response)
        response.status_code = 503
        response.text = "outage"
        # AttioWriter dispatches list-entry writes via update_list_entry
        # in Wave-2-B; inject failure there.
        mock_attio.update_list_entry.side_effect = httpx.HTTPStatusError(
            "503", request=MagicMock(), response=response,
        )
        writer = AttioWriter(attio=mock_attio)
        with pytest.raises(AttioRateLimitExhausted) as exc_info:
            writer.apply(WriteIntent(
                object="linkedin_outreach",
                record_id="ent_x",
                updates={"stage": "DM1 Sent"},
                prior_values={"stage": "Accepted"},
                writer_module="workflows.daily_check.run_dm_sequencing",
                is_list_entry=True,
                list_id="list_test",
            ))
        # Discrimination contract: __cause__ MUST be the underlying
        # httpx.HTTPStatusError so detect_responses can route 5xx
        # exhaustion to "propagate".
        assert isinstance(exc_info.value.__cause__, httpx.HTTPStatusError)
        assert exc_info.value.__cause__.response.status_code == 503

    def test_chains_connecterror_when_network_exhausted(self, monkeypatch):
        import httpx

        from clients.attio_writer import (
            AttioRateLimitExhausted,
            AttioWriter,
        )
        monkeypatch.setattr("clients.attio_writer.RETRY_BUDGET_SECONDS", 0.0001)
        mock_attio = MagicMock()
        # ConnectError → AttioWriter retries via the network-class
        # branch; needs to fire from the dispatch helper.
        mock_attio.update_list_entry.side_effect = httpx.ConnectError(
            "DNS fail", request=MagicMock(),
        )
        writer = AttioWriter(attio=mock_attio)
        with pytest.raises(AttioRateLimitExhausted) as exc_info:
            writer.apply(WriteIntent(
                object="linkedin_outreach",
                record_id="ent_y",
                updates={"stage": "DM1 Sent"},
                prior_values={"stage": "Accepted"},
                writer_module="workflows.daily_check.run_dm_sequencing",
                is_list_entry=True,
                list_id="list_test",
            ))
        # Network-class exhaustion → __cause__ is the ConnectError so
        # detect_responses routes to count+continue (FIX-C semantic).
        assert isinstance(exc_info.value.__cause__, httpx.ConnectError)


class TestWriterModuleStringsResolveToRealPython:
    """Wave-2-B fix-up (multi-agent test gap 3): every writer_module
    string in WRITE_OWNER_REGISTRY MUST point to a real Python module
    or method. Pre-fix-up the spy tests only asserted that the helper
    routes through AttioWriter — a typo in the writer_module string
    would silently trip UnauthorizedAttioWriteError at runtime.

    Known pre-existing phantom writers (gtm-MIN-7 + similar) are listed
    in ``_KNOWN_PHANTOM_WRITERS`` so the test surfaces NEW typos
    introduced by future PRs without failing on backlog items that
    pre-date this PR. Removing an entry from the set should make the
    corresponding test failure go away."""

    # Phantom writers tracked separately so this test catches NEW
    # registry-vs-code drift while documenting the existing backlog.
    # Each entry should reference a tracking item (gtm-MIN-X / PR-N).
    _KNOWN_PHANTOM_WRITERS: frozenset[str] = frozenset({
        # gtm-MIN-7 (done-qa-gtm-pass1-round2): deal-creation contract
        # is unwired top-to-bottom; script doesn't exist as a file.
        # Tracked for a future Wave-2 ABM build that ships schema +
        # caller + this backfill together.
        "scripts.backfill_operator_unknown_deals",
        # PR-39 forward-declaration. The function lands when nurture
        # re-engagement ships; the registry entry pre-stages the
        # write-owner so PR-39 doesn't have to retouch the registry.
        "workflows.daily_check.run_nurture_re_engagement",
        # Wave-1.6-era backfill that was registered against a planned
        # one-shot script but never landed. The DEFENSIVE_HOLD enum
        # value DID land (per PR-18 B-SD-003); the one-shot backfill
        # was deemed unnecessary once the runtime classifier started
        # writing DEFENSIVE_HOLD directly. Registry entry kept as a
        # historical placeholder.
        "scripts.backfill_DEFENSIVE_HOLD",
        # workflows.detect_responses.classify_reply is the documented-
        # writer alias for the classify_reply path. The actual function
        # at module scope is `classify_reply_llm` (the LLM-wrapped
        # variant). The registry uses the unprefixed name as a logical
        # writer identifier; both `detect_responses` (the loop) and
        # `classify_reply_llm` (the inner LLM call) participate.
        "workflows.detect_responses.classify_reply",
        # workflows.escalation_resolver was a planned module per
        # F-PR-3 operator-resolution flow; landed inline as part of
        # workflows.sales_approve. Registry entry stays as a forward-
        # decl for a future split.
        "workflows.escalation_resolver",
        # Wave-2-A (2026-06-01): apply_dm3_revisit_verdict wrote
        # verdict_v1/verdict_v2/dm3_window_settles_at to the Attio
        # `experiment` object, which was descoped (never deployed, 404
        # live). The function had zero production callers (dead code that
        # would crash on the 404) and was deleted. Its
        # ("experiment", verdict_v1/v2/settles_at) registry entries are
        # retained for schema symmetry per the Wave-2-A plan, so the
        # writer_module string is now a phantom-by-design.
        "workflows.weekly_brain.apply_dm3_revisit_verdict",
    })

    def test_every_registered_writer_module_resolves(self):
        import importlib

        from clients.attio_writer_registry import (
            SPECIAL_WRITER_ALIASES,
            WRITE_OWNER_REGISTRY,
        )

        phantom_findings: list[tuple[str, str]] = []
        seen: set[str] = set()
        for owners in WRITE_OWNER_REGISTRY.values():
            owners_list = [owners] if isinstance(owners, str) else owners
            for owner in owners_list:
                if owner in SPECIAL_WRITER_ALIASES or owner in seen:
                    continue
                seen.add(owner)
                # Module path is everything up to the last dotted
                # component; the last component is the attr name.
                # Method paths like "MigrationRunWriter.try_reserve"
                # need to resolve the class then check the method
                # exists on it; iteratively walk attributes.
                parts = owner.split(".")
                module = None
                module_depth = 0
                for i in range(len(parts), 0, -1):
                    try:
                        module = importlib.import_module(".".join(parts[:i]))
                        module_depth = i
                        break
                    except ModuleNotFoundError:
                        continue
                if module is None:
                    phantom_findings.append((
                        owner,
                        "no importable module prefix",
                    ))
                    continue
                # Walk remaining components as attributes.
                obj = module
                missing_attr = None
                for part in parts[module_depth:]:
                    if not hasattr(obj, part):
                        missing_attr = part
                        break
                    obj = getattr(obj, part)
                if missing_attr is not None:
                    phantom_findings.append((
                        owner,
                        f"attribute {missing_attr!r} not found",
                    ))

        new_phantoms = [
            (owner, reason) for owner, reason in phantom_findings
            if owner not in self._KNOWN_PHANTOM_WRITERS
        ]
        assert not new_phantoms, (
            "NEW phantom writer_module strings in WRITE_OWNER_REGISTRY "
            "(not in _KNOWN_PHANTOM_WRITERS): "
            f"{new_phantoms!r}. Either fix the registry or add the "
            f"entry to the _KNOWN_PHANTOM_WRITERS set with a tracking "
            f"reference (gtm-MIN-X / PR-N)."
        )
