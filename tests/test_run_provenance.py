"""Tests for workflows/run_provenance.py — PR-228 / PR-229.

A wet run must know (and record) which code it executed, and refuse to run
from a checkout that is behind origin/main unless explicitly overridden.
"""

from unittest.mock import MagicMock, patch

import pytest

from workflows import run_provenance
from workflows.run_provenance import (
    StaleCheckoutError,
    assert_checkout_current,
    collect_run_provenance,
    format_provenance,
)


def _fake_git(responses: dict):
    """Build a _git stub keyed on the first git arg (or arg pair)."""
    def fake(args, **kwargs):
        key = args[0] if args[0] != "merge-base" else "merge-base"
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            key = "branch"
        elif args[:2] == ["rev-parse", "--verify"]:
            key = "verify"
        elif args[:1] == ["rev-parse"]:
            key = "sha"
        return responses.get(key)
    return fake


class TestCollectRunProvenance:
    def test_current_clean_checkout(self):
        with patch.object(run_provenance, "_git", _fake_git({
            "sha": "abcdef1234567890", "branch": "main",
            "status": "", "merge-base": "",  # exit 0 → ancestor → current
        })):
            prov = collect_run_provenance()
        assert prov == {
            "sha": "abcdef123456", "branch": "main",
            "dirty": False, "behind_origin_main": False,
        }

    def test_behind_origin_main(self):
        # merge-base fails (exit 1 → None) but origin/main resolves → behind.
        with patch.object(run_provenance, "_git", _fake_git({
            "sha": "abcdef1234567890", "branch": "feat/old",
            "status": " M workflows/x.py", "merge-base": None,
            "verify": "123abc",
        })):
            prov = collect_run_provenance()
        assert prov["behind_origin_main"] is True
        assert prov["dirty"] is True

    def test_unverifiable_when_origin_main_missing(self):
        with patch.object(run_provenance, "_git", _fake_git({
            "sha": "abcdef1234567890", "branch": "main",
            "status": "", "merge-base": None, "verify": None,
        })):
            prov = collect_run_provenance()
        assert prov["behind_origin_main"] is None

    def test_total_git_failure_degrades_to_unknown(self):
        with patch.object(run_provenance, "_git", lambda *a, **k: None):
            prov = collect_run_provenance()
        assert prov["sha"] == "unknown"
        assert prov["branch"] == "unknown"
        assert prov["dirty"] is None
        assert prov["behind_origin_main"] is None


class TestAssertCheckoutCurrent:
    def _patched(self, prov):
        return patch.object(
            run_provenance, "collect_run_provenance", return_value=prov
        )

    def test_wet_run_behind_refuses(self):
        prov = {"sha": "a" * 12, "branch": "stale", "dirty": False,
                "behind_origin_main": True}
        with self._patched(prov), pytest.raises(StaleCheckoutError, match="BEHIND"):
            assert_checkout_current(dry_run=False)

    def test_refusal_advice_is_branch_aware(self):
        # Feature-branch worktrees must NOT be told to pull/rebase (wrong
        # move mid-work); main checkouts must.
        feature = {"sha": "a" * 12, "branch": "feat/x", "dirty": False,
                   "behind_origin_main": True}
        with self._patched(feature), pytest.raises(
                StaleCheckoutError, match="checkout that is on current main"):
            assert_checkout_current(dry_run=False)
        on_main = {"sha": "a" * 12, "branch": "main", "dirty": False,
                   "behind_origin_main": True}
        with self._patched(on_main), pytest.raises(
                StaleCheckoutError, match="git pull origin main"):
            assert_checkout_current(dry_run=False)

    def test_wet_run_behind_with_allow_stale_warns_and_returns(self):
        prov = {"sha": "a" * 12, "branch": "stale", "dirty": False,
                "behind_origin_main": True}
        with self._patched(prov):
            out = assert_checkout_current(dry_run=False, allow_stale=True)
        assert out is prov

    def test_dry_run_behind_warns_only(self):
        prov = {"sha": "a" * 12, "branch": "stale", "dirty": True,
                "behind_origin_main": True}
        with self._patched(prov):
            out = assert_checkout_current(dry_run=True)
        assert out is prov

    def test_current_checkout_passes_silently(self):
        prov = {"sha": "a" * 12, "branch": "main", "dirty": False,
                "behind_origin_main": False}
        with self._patched(prov):
            assert assert_checkout_current(dry_run=False) is prov

    def test_unverifiable_warns_but_proceeds(self):
        prov = {"sha": "unknown", "branch": "unknown", "dirty": None,
                "behind_origin_main": None}
        with self._patched(prov):
            assert assert_checkout_current(dry_run=False) is prov


class TestFormatProvenance:
    def test_renders_all_flags(self):
        s = format_provenance({"sha": "abc123", "branch": "main",
                               "dirty": True, "behind_origin_main": True})
        assert "main@abc123" in s
        assert "+dirty" in s
        assert "BEHIND origin/main" in s


class TestWeeklyPreflightOrdering:
    """The staleness preflight must abort BEFORE the run lock and any PB/CRM
    client construction (mirrors the DM-sequencing preflight ordering test)."""

    def test_stale_wet_weekly_aborts_before_lock_and_clients(self, monkeypatch):
        from click.testing import CliRunner

        import cli as cli_module

        monkeypatch.setenv("PB_SEARCH_EXPORT_ID", "agent-1")
        prov = {"sha": "a" * 12, "branch": "stale", "dirty": False,
                "behind_origin_main": True}
        lock = MagicMock()
        with patch.object(run_provenance, "collect_run_provenance", return_value=prov), \
             patch("workflows.run_lock.acquire_run_lock", lock), \
             patch("clients.crm.factory.get_crm_provider") as crm_factory, \
             patch("clients.phantombuster.PhantomBusterClient") as pb_cls:
            result = CliRunner().invoke(cli_module.cli, ["weekly"])
        assert result.exit_code != 0
        assert "BEHIND" in result.output
        lock.assert_not_called()
        crm_factory.assert_not_called()
        pb_cls.assert_not_called()

    def test_allow_stale_flag_reaches_the_run(self, monkeypatch):
        from click.testing import CliRunner

        import cli as cli_module

        monkeypatch.setenv("PB_SEARCH_EXPORT_ID", "agent-1")
        prov = {"sha": "a" * 12, "branch": "stale", "dirty": False,
                "behind_origin_main": True}
        with patch.object(run_provenance, "collect_run_provenance", return_value=prov), \
             patch("workflows.run_lock.acquire_run_lock", MagicMock()), \
             patch("clients.crm.factory.get_crm_provider"), \
             patch("clients.phantombuster.PhantomBusterClient"), \
             patch("workflows.weekly_prospect.run_weekly_prospecting") as run_mock:
            result = CliRunner().invoke(cli_module.cli, ["weekly", "--allow-stale"])
        assert result.exit_code == 0, result.output
        assert run_mock.call_args.kwargs["code_provenance"] is prov


class TestStagedJsonlStamping:
    """Every staged borderline row carries the code_version that scored it."""

    def test_borderline_entries_carry_code_version(self, tmp_path, monkeypatch):
        import json
        from unittest.mock import patch as _patch

        from workflows import weekly_prospect as wp

        monkeypatch.setenv("ATTIO_LIST_ID", "list-1")
        monkeypatch.setattr(wp, "EXPORTS_DIR", tmp_path)

        personas = {
            "operations_leaders": {
                "enterprise_mode": True,
                "search_size_credit": 15,
                "search_queries": {"sn_search_urls": {"mx": "https://sn/s"}},
            }
        }
        csv_text = (
            "fullName,title,companyName,location,defaultProfileUrl\n"
            "Test User,Plant Manager,Subsidiary,\"Mexico City, Mexico\","
            "https://www.linkedin.com/in/test-user\n"
        )
        prov = {"sha": "a" * 12, "branch": "main", "dirty": False,
                "behind_origin_main": False}

        crm = MagicMock()
        crm.query_list_entries.return_value = []
        crm.search_person_by_linkedin.return_value = None
        pb = MagicMock()

        with _patch.object(wp, "build_anthropic_client", return_value=None), \
             _patch.object(wp, "load_personas", return_value=personas), \
             _patch.object(wp, "_check_all_persona_target_lists_fresh"), \
             _patch.object(wp, "_load_in_list_canonical_urls", return_value=set()), \
             _patch.object(wp, "_launch_and_download", return_value=csv_text):
            # Wet run (dry_run skips the PB download entirely, so nothing
            # would stage); all clients are mocks — no real writes.
            wp.run_weekly_prospecting(
                crm, pb, search_export_id="agent-1", dry_run=False,
                code_provenance=prov,
            )

        staged = list(tmp_path.glob("weekly_borderline_*.jsonl"))
        assert len(staged) == 1
        rows = [json.loads(line) for line in staged[0].read_text().splitlines()]
        assert rows, "expected at least one staged borderline row"
        assert all(r["code_version"] == prov for r in rows)
