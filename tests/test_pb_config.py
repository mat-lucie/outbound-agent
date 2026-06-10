"""Tests for clients/pb_config.py — PhantomBuster config consolidation (P3).

Proves the behavior-preserving resolution rule:

- Agent IDs + backend: config/phantombuster.yaml → env var → code default.
- A MISSING phantombuster.yaml falls back to env (current-deploy behavior).
- A PRESENT-but-malformed phantombuster.yaml raises ConfigError.
- Secrets (API key, cookies, user-agent) are ALWAYS read from env, never yaml.

Every test points OUTBOUND_CONFIG_DIR at a tmp dir so the repo's real config/
never leaks in, and controls the PB_* env vars via monkeypatch.
"""

from pathlib import Path

import pytest

from clients.pb_config import (
    DEFAULT_USER_AGENT,
    PBConfig,
    li_session_cookie,
    li_user_agent_or_default,
    li_user_agent_raw,
    li_user_agent_stripped,
    load_pb_config,
    require_api_key,
    sales_nav_session_cookie_stripped,
)
from clients.settings import ConfigError

# All PB_* env vars + the backend var the loader resolves. Cleared at the top
# of each test so no developer .env / prior test leaks in.
_PB_ENV_VARS = (
    "PB_SEARCH_EXPORT_ID",
    "PB_NETWORK_BOOSTER_ID",
    "PB_MESSAGE_SENDER_ID",
    "PB_PROFILE_SCRAPER_ID",
    "PB_SALES_NAV_PROFILE_SCRAPER_ID",
    "PB_SALES_NAV_URL_CONVERTER_ID",
    "PB_INBOX_SCRAPER_ID",
    "PRE_INVITE_DEGREE_CHECK_BACKEND",
    "PHANTOMBUSTER_API_KEY",
    "PB_LI_SESSION_COOKIE",
    "PB_LI_USER_AGENT",
    "PB_LI_SALES_NAV_SESSION_COOKIE",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Point config at an empty tmp dir and clear every PB env var.

    Yields the tmp config dir so a test can drop a phantombuster.yaml into it.
    """
    monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(tmp_path))
    for var in _PB_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _write_yaml(config_dir: Path, body: str) -> None:
    (config_dir / "phantombuster.yaml").write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# No yaml → env values + code defaults (current-deploy behavior).
# ---------------------------------------------------------------------------


class TestNoYaml:
    def test_unset_env_yields_empty_ids_and_default_backend(self, clean_env):
        """No yaml, no env: IDs are "" and backend defaults to "regular"."""
        cfg = load_pb_config()
        assert isinstance(cfg, PBConfig)
        assert cfg.search_export_id == ""
        assert cfg.network_booster_id == ""
        assert cfg.message_sender_id == ""
        assert cfg.profile_scraper_id == ""
        assert cfg.sales_nav_profile_scraper_id == ""
        assert cfg.sales_nav_url_converter_id == ""
        assert cfg.inbox_scraper_id == ""
        assert cfg.degree_check_backend_raw == "regular"

    def test_env_supplies_ids_when_no_yaml(self, clean_env, monkeypatch):
        """No yaml: each ID comes from its env var."""
        monkeypatch.setenv("PB_SEARCH_EXPORT_ID", "se-1")
        monkeypatch.setenv("PB_NETWORK_BOOSTER_ID", "nb-2")
        monkeypatch.setenv("PB_MESSAGE_SENDER_ID", "ms-3")
        monkeypatch.setenv("PB_PROFILE_SCRAPER_ID", "ps-4")
        monkeypatch.setenv("PB_SALES_NAV_PROFILE_SCRAPER_ID", "snps-5")
        monkeypatch.setenv("PB_INBOX_SCRAPER_ID", "ib-6")

        cfg = load_pb_config()
        assert cfg.search_export_id == "se-1"
        assert cfg.network_booster_id == "nb-2"
        assert cfg.message_sender_id == "ms-3"
        assert cfg.profile_scraper_id == "ps-4"
        assert cfg.sales_nav_profile_scraper_id == "snps-5"
        assert cfg.inbox_scraper_id == "ib-6"

    def test_backend_from_env_when_no_yaml(self, clean_env, monkeypatch):
        monkeypatch.setenv("PRE_INVITE_DEGREE_CHECK_BACKEND", "sales_nav")
        assert load_pb_config().degree_check_backend_raw == "sales_nav"

    def test_loader_is_live_not_cached(self, clean_env, monkeypatch):
        """Each call re-reads env — required for hot-reload + conftest delenv."""
        monkeypatch.setenv("PRE_INVITE_DEGREE_CHECK_BACKEND", "sales_nav")
        assert load_pb_config().degree_check_backend_raw == "sales_nav"
        monkeypatch.delenv("PRE_INVITE_DEGREE_CHECK_BACKEND", raising=False)
        assert load_pb_config().degree_check_backend_raw == "regular"


# ---------------------------------------------------------------------------
# Yaml present → operator config wins for IDs + backend.
# ---------------------------------------------------------------------------


class TestYamlWins:
    def test_yaml_supplies_ids(self, clean_env):
        _write_yaml(
            clean_env,
            "agents:\n"
            "  search_export: yaml-se\n"
            "  network_booster: yaml-nb\n"
            "pre_invite_degree_check_backend: regular\n",
        )
        cfg = load_pb_config()
        assert cfg.search_export_id == "yaml-se"
        assert cfg.network_booster_id == "yaml-nb"

    def test_yaml_wins_over_env_for_ids(self, clean_env, monkeypatch):
        """When both yaml and env supply an ID, yaml wins."""
        monkeypatch.setenv("PB_SEARCH_EXPORT_ID", "env-se")
        _write_yaml(clean_env, "agents:\n  search_export: yaml-se\n")
        assert load_pb_config().search_export_id == "yaml-se"

    def test_yaml_wins_over_env_for_backend(self, clean_env, monkeypatch):
        monkeypatch.setenv("PRE_INVITE_DEGREE_CHECK_BACKEND", "regular")
        _write_yaml(
            clean_env,
            "pre_invite_degree_check_backend: sales_nav\n",
        )
        assert load_pb_config().degree_check_backend_raw == "sales_nav"

    def test_blank_yaml_id_falls_through_to_env(self, clean_env, monkeypatch):
        """An empty/placeholder yaml ID falls back to the env var."""
        monkeypatch.setenv("PB_SEARCH_EXPORT_ID", "env-se")
        _write_yaml(clean_env, 'agents:\n  search_export: ""\n')
        assert load_pb_config().search_export_id == "env-se"

    def test_missing_yaml_key_falls_through_to_env(self, clean_env, monkeypatch):
        """A yaml that omits an agent key still reads that ID from env."""
        monkeypatch.setenv("PB_INBOX_SCRAPER_ID", "env-ib")
        _write_yaml(clean_env, "agents:\n  search_export: yaml-se\n")
        cfg = load_pb_config()
        assert cfg.search_export_id == "yaml-se"
        assert cfg.inbox_scraper_id == "env-ib"

    def test_yaml_with_no_agents_section_uses_env(self, clean_env, monkeypatch):
        """A yaml that only sets the backend leaves IDs on the env fallback."""
        monkeypatch.setenv("PB_SEARCH_EXPORT_ID", "env-se")
        _write_yaml(clean_env, "pre_invite_degree_check_backend: sales_nav\n")
        cfg = load_pb_config()
        assert cfg.search_export_id == "env-se"
        assert cfg.degree_check_backend_raw == "sales_nav"


# ---------------------------------------------------------------------------
# Malformed / present-but-broken yaml → ConfigError (fail loud).
# ---------------------------------------------------------------------------


class TestMalformedYaml:
    def test_malformed_yaml_raises(self, clean_env):
        _write_yaml(clean_env, "agents: [this is not: valid: yaml\n")
        with pytest.raises(ConfigError):
            load_pb_config()

    def test_non_mapping_top_level_raises(self, clean_env):
        _write_yaml(clean_env, "- just\n- a\n- list\n")
        with pytest.raises(ConfigError):
            load_pb_config()

    def test_agents_not_a_mapping_raises(self, clean_env):
        _write_yaml(clean_env, "agents: not-a-mapping\n")
        with pytest.raises(ConfigError):
            load_pb_config()

    def test_non_string_agent_id_raises(self, clean_env):
        _write_yaml(clean_env, "agents:\n  search_export: 12345\n")
        with pytest.raises(ConfigError):
            load_pb_config()

    def test_non_string_backend_raises(self, clean_env):
        _write_yaml(clean_env, "pre_invite_degree_check_backend: true\n")
        with pytest.raises(ConfigError):
            load_pb_config()


# ---------------------------------------------------------------------------
# Secrets — ALWAYS from env, yaml ignored for them.
# ---------------------------------------------------------------------------


class TestSecretsAreEnvOnly:
    def test_require_api_key_returns_env_value(self, clean_env, monkeypatch):
        monkeypatch.setenv("PHANTOMBUSTER_API_KEY", "secret-key")
        assert require_api_key() == "secret-key"

    def test_require_api_key_raises_keyerror_when_unset(self, clean_env):
        """Missing API key raises KeyError (preserves the prior
        os.environ['PHANTOMBUSTER_API_KEY'] contract, NOT ConfigError)."""
        with pytest.raises(KeyError):
            require_api_key()

    def test_yaml_credentials_block_does_not_supply_secret(
        self, clean_env, monkeypatch
    ):
        """Even with a credentials block in yaml, the API key still comes from
        env — a present yaml must not let a secret resolve from yaml."""
        monkeypatch.setenv("PHANTOMBUSTER_API_KEY", "env-secret")
        _write_yaml(
            clean_env,
            "agents:\n  search_export: yaml-se\n"
            "credentials:\n  api_key_env: PHANTOMBUSTER_API_KEY\n",
        )
        assert require_api_key() == "env-secret"

    def test_li_session_cookie_default_empty(self, clean_env):
        assert li_session_cookie() == ""

    def test_li_session_cookie_from_env(self, clean_env, monkeypatch):
        monkeypatch.setenv("PB_LI_SESSION_COOKIE", "li_at_value")
        assert li_session_cookie() == "li_at_value"

    def test_user_agent_default_is_shipped_ua(self, clean_env):
        assert li_user_agent_or_default() == DEFAULT_USER_AGENT

    def test_user_agent_or_default_from_env(self, clean_env, monkeypatch):
        monkeypatch.setenv("PB_LI_USER_AGENT", "Mozilla/Custom")
        assert li_user_agent_or_default() == "Mozilla/Custom"

    def test_user_agent_raw_default_empty(self, clean_env):
        """The weekly call site default is "" (it supplies its own fallback)."""
        assert li_user_agent_raw() == ""

    def test_user_agent_stripped_default_empty(self, clean_env):
        assert li_user_agent_stripped() == ""

    def test_user_agent_stripped_strips(self, clean_env, monkeypatch):
        monkeypatch.setenv("PB_LI_USER_AGENT", "  ua  ")
        assert li_user_agent_stripped() == "ua"

    def test_sales_nav_cookie_default_empty(self, clean_env):
        assert sales_nav_session_cookie_stripped() == ""

    def test_sales_nav_cookie_strips_whitespace_to_empty(
        self, clean_env, monkeypatch
    ):
        monkeypatch.setenv("PB_LI_SALES_NAV_SESSION_COOKIE", "   \n ")
        assert sales_nav_session_cookie_stripped() == ""

    def test_sales_nav_cookie_from_env(self, clean_env, monkeypatch):
        monkeypatch.setenv("PB_LI_SALES_NAV_SESSION_COOKIE", "  sn_li_at  ")
        assert sales_nav_session_cookie_stripped() == "sn_li_at"
