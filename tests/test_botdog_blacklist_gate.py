"""Tests for the Botdog blacklist presence gate.

`scripts/seed_botdog_blacklist.py --apply` pushes the operator's
never-contact set into Botdog, which inherits none of PhantomBuster's
internal dedup memory. As a documentation-only pre-send step it was
skippable — and the first Botdog run could then re-invite a prospect
already burned, or cold-contact a company on the operator's denylist. The
gate in `workflows.daily_check_helpers.assert_botdog_blacklist_seeded`
makes that step CODE-ENFORCED.

Every test here runs the REAL gate: the autouse fixture drops any ambient
BOTDOG_SKIP_BLACKLIST_CHECK (an operator override in the environment must
never mask a regression) and clears the per-process memo BEFORE and AFTER
each test, so gate state cannot leak between these tests or into the rest
of the suite. All clients are mocked; no live calls anywhere.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from clients.botdog import BotdogError, blacklist_name
from workflows.daily_check_helpers import (
    BOTDOG_SKIP_BLACKLIST_CHECK_ENV,
    assert_botdog_blacklist_seeded,
    reset_blacklist_gate,
)


@pytest.fixture(autouse=True)
def _gate_live(monkeypatch):
    """Run the REAL gate: drop any ambient bypass, clear the memo."""
    monkeypatch.delenv(BOTDOG_SKIP_BLACKLIST_CHECK_ENV, raising=False)
    reset_blacklist_gate()
    yield
    reset_blacklist_gate()


def _name() -> str:
    """The collection name the gate resolves (operator config)."""
    return blacklist_name()


def _client(blacklists) -> MagicMock:
    client = MagicMock()
    client.get_blacklists.return_value = blacklists
    return client


class TestGateBlocks:
    def test_missing_collection_blocks(self):
        client = _client([{"id": "bl_other", "name": "Something else"}])
        with pytest.raises(RuntimeError) as exc:
            assert_botdog_blacklist_seeded(client)
        assert "not seeded" in str(exc.value)
        assert "seed_botdog_blacklist.py --apply" in str(exc.value)
        assert _name() in str(exc.value)

    def test_no_collections_at_all_blocks(self):
        with pytest.raises(RuntimeError):
            assert_botdog_blacklist_seeded(_client([]))

    def test_empty_collection_blocks(self):
        """Present but 0 leads == unseeded. A seeded account always has
        the never-contact set in it."""
        client = _client([{"id": "bl_1", "name": _name(), "leads": []}])
        with pytest.raises(RuntimeError) as exc:
            assert_botdog_blacklist_seeded(client)
        assert "EMPTY" in str(exc.value)

    def test_zero_count_field_blocks(self):
        """The collection DTO may report a count instead of embedding
        leads — an explicit zero blocks either way."""
        client = _client([
            {"id": "bl_1", "name": _name(), "leadsCount": 0},
        ])
        with pytest.raises(RuntimeError):
            assert_botdog_blacklist_seeded(client)

    def test_a_blocked_gate_does_not_memoize(self):
        """A raise must re-raise on the next call — memoizing a FAILURE
        would let the second sender build of the run sail through."""
        client = _client([])
        for _ in range(2):
            with pytest.raises(RuntimeError):
                assert_botdog_blacklist_seeded(client)
        assert client.get_blacklists.call_count == 2


class TestGatePasses:
    def test_seeded_collection_passes(self):
        client = _client([
            {"id": "bl_1", "name": _name(),
             "leads": [{"linkedinUrl": "https://linkedin.com/in/acme-alice"}]},
        ])
        assert_botdog_blacklist_seeded(client)  # no raise

    def test_name_match_is_case_and_whitespace_insensitive(self):
        client = _client([
            {"id": "bl_1", "name": f"  {_name().upper()}  ",
             "leads": [{"linkedinUrl": "https://linkedin.com/in/acme-alice"}]},
        ])
        assert_botdog_blacklist_seeded(client)

    def test_unknown_lead_count_passes(self):
        """The collection DTO shape is unverified: when the payload
        reports no count at all, presence is enough. Blocking a real,
        populated collection because the API omits its leads would
        strand every send."""
        client = _client([{"id": "bl_1", "name": _name()}])
        assert_botdog_blacklist_seeded(client)

    def test_unknown_lead_count_warns_that_it_verified_existence_only(
        self, capsys
    ):
        """The pass is deliberate, but it is a WEAKER verdict than the
        operator thinks they got — `None` means "unknown", not "0". Saying
        nothing lets an unreadable count read as a clean gate."""
        client = _client([{"id": "bl_1", "name": _name()}])
        assert_botdog_blacklist_seeded(client)
        err = capsys.readouterr().err
        assert "NO readable lead count" in err
        assert "EXISTENCE ONLY" in err

    def test_known_lead_count_does_not_warn(self, capsys):
        client = _client([{"id": "bl_1", "name": _name(), "leadCount": 42}])
        assert_botdog_blacklist_seeded(client)
        assert "EXISTENCE ONLY" not in capsys.readouterr().err

    def test_empty_duplicate_does_not_shadow_the_populated_collection(self):
        """Duplicate same-named collections exist in the wild — one empty,
        one populated. The gate must pick the POPULATED one by lead count,
        not the first by list order, or an API that returns the empty
        duplicate first would read 0 leads and block every send."""
        populated = {"id": "full", "name": _name(), "leadCount": 1462}
        empty = {"id": "dup", "name": _name(), "leadCount": 0}
        # Both orders must pass — the empty duplicate may arrive first.
        assert_botdog_blacklist_seeded(_client([empty, populated]))
        reset_blacklist_gate()
        assert_botdog_blacklist_seeded(_client([populated, empty]))


class TestGateNonBlockingPaths:
    def test_override_env_warns_but_does_not_block(self, monkeypatch, capsys):
        monkeypatch.setenv(BOTDOG_SKIP_BLACKLIST_CHECK_ENV, "1")
        client = _client([])  # would otherwise block
        assert_botdog_blacklist_seeded(client)
        err = capsys.readouterr().err
        assert BOTDOG_SKIP_BLACKLIST_CHECK_ENV in err
        assert "BYPASSED" in err
        # The override short-circuits before the API call.
        client.get_blacklists.assert_not_called()

    def test_check_error_warns_but_does_not_block(self, capsys):
        """An API blip must not kill the run — the sends downstream carry
        their own error handling, and failing the whole run on a flaky
        read would be a bigger outage than the risk it guards."""
        client = MagicMock()
        client.get_blacklists.side_effect = BotdogError(
            "gateway timeout", status_code=504
        )
        assert_botdog_blacklist_seeded(client)  # no raise
        err = capsys.readouterr().err
        assert "could not run" in err
        assert "NOT blocking" in err


class TestGateCaching:
    def test_passing_gate_calls_the_api_once_per_process(self):
        client = _client([
            {"id": "bl_1", "name": _name(),
             "leads": [{"linkedinUrl": "https://linkedin.com/in/acme-alice"}]},
        ])
        for _ in range(5):
            assert_botdog_blacklist_seeded(client)
        assert client.get_blacklists.call_count == 1

    def test_warning_paths_warn_once_per_process(self, capsys):
        client = MagicMock()
        client.get_blacklists.side_effect = BotdogError("boom")
        for _ in range(3):
            assert_botdog_blacklist_seeded(client)
        assert client.get_blacklists.call_count == 1
        assert capsys.readouterr().err.count("could not run") == 1


class TestBuilderWiring:
    """`_build_botdog_sender` serves ONLY the read-only event drain. The
    blacklist gate is a PRE-SEND safety step; running it on the drain path
    would mean an emptied or renamed collection could kill the very event
    ingestion that confirms deliveries. The builder must NOT run it."""

    def test_build_botdog_sender_does_not_run_the_gate(self, monkeypatch):
        from clients.botdog_config import BotdogConfig
        from workflows import daily_check

        monkeypatch.setenv("BOTDOG_API_KEY", "bd_test")
        # The reference config ships `enabled: false` (the engine default
        # posture), and a disabled transport is now skipped outright — so
        # this test builds an ENABLED config in-process to reach the
        # construction path the gate assertion is about.
        monkeypatch.setattr(
            "clients.botdog_config.load_botdog_config",
            lambda: BotdogConfig(
                enabled=True, campaigns={"invite": "cmp-1"}
            ),
        )
        # An account with NO blacklist collections at all — a gate call
        # would raise "not seeded"; the drain builder must not care.
        fake_client = _client([])
        monkeypatch.setattr(
            "clients.botdog.BotdogClient", lambda *a, **k: fake_client
        )
        sender = daily_check._build_botdog_sender()
        assert sender is not None
        assert sender._client is fake_client
        assert fake_client.get_blacklists.call_count == 0


class TestBuilderHonorsEnabledAndPlaceholders:
    """`config/botdog.example.yaml` documents `enabled: false` as "every
    Botdog surface is inert". The builder must make that TRUE: reading a
    disabled config and constructing a sender anyway (polling whatever
    campaign ids the template happens to carry, placeholders included) is
    the flag lying about itself."""

    def _config(self, monkeypatch, **kwargs):
        from clients.botdog_config import BotdogConfig

        monkeypatch.setattr(
            "clients.botdog_config.load_botdog_config",
            lambda: BotdogConfig(**kwargs),
        )

    def test_disabled_config_skips_the_build_visibly(
        self, monkeypatch, capsys
    ):
        from workflows import daily_check

        monkeypatch.setenv("BOTDOG_API_KEY", "bd_test")
        self._config(
            monkeypatch, enabled=False, campaigns={"invite": "cmp-1"}
        )
        monkeypatch.setattr(
            "clients.botdog.BotdogClient",
            lambda *a, **k: pytest.fail("must not construct a client"),
        )

        assert daily_check._build_botdog_sender() is None
        assert "SKIPPED" in capsys.readouterr().out

    def test_disabled_config_does_not_demand_an_api_key(
        self, monkeypatch
    ):
        """A transport that will not act must not require credentials it
        will never use — the enabled check runs FIRST."""
        from workflows import daily_check

        monkeypatch.delenv("BOTDOG_API_KEY", raising=False)
        self._config(monkeypatch, enabled=False)

        assert daily_check._build_botdog_sender() is None

    def test_surviving_placeholder_campaign_id_fails_loud(self, monkeypatch):
        """Last stop before a template value would be used as a real
        campaign. The loader already refuses this pairing, so reaching
        here means that guard was bypassed — never poll it anyway."""
        from workflows import daily_check

        monkeypatch.setenv("BOTDOG_API_KEY", "bd_test")
        self._config(
            monkeypatch,
            enabled=True,
            campaigns={"invite": "REPLACE_WITH_BOTDOG_INVITE_CAMPAIGN_ID"},
        )
        monkeypatch.setattr(
            "clients.botdog.BotdogClient",
            lambda *a, **k: pytest.fail("must not construct a client"),
        )

        with pytest.raises(RuntimeError, match="REPLACE_WITH"):
            daily_check._build_botdog_sender()
