"""Tests for clients/settings.py — config-loading foundation.

Covers:
- config_dir() default and OUTBOUND_CONFIG_DIR override
- load_yaml() success, missing-file error, non-mapping parse error
- require_env() success, unset error, empty-string error
- resolve_env_ref() success, required=False returns None, loud error on
  present key whose env var is missing
"""

from pathlib import Path

import pytest

from clients.settings import ConfigError, config_dir, load_yaml, require_env, resolve_env_ref

# ---------------------------------------------------------------------------
# config_dir()
# ---------------------------------------------------------------------------


class TestConfigDir:
    """config_dir() resolves the config directory."""

    def test_default_is_repo_root_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without OUTBOUND_CONFIG_DIR set, returns <repo_root>/config."""
        monkeypatch.delenv("OUTBOUND_CONFIG_DIR", raising=False)
        result = config_dir()
        # repo root = parent of clients/ package dir
        import clients.settings as _mod

        expected = Path(_mod.__file__).resolve().parent.parent / "config"
        assert result == expected

    def test_override_via_env_var(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When OUTBOUND_CONFIG_DIR is set to a non-empty path, use it."""
        monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(tmp_path))
        assert config_dir() == tmp_path

    def test_empty_env_var_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty OUTBOUND_CONFIG_DIR is treated as unset (fallback to default)."""
        monkeypatch.setenv("OUTBOUND_CONFIG_DIR", "")
        import clients.settings as _mod

        expected = Path(_mod.__file__).resolve().parent.parent / "config"
        assert config_dir() == expected


# ---------------------------------------------------------------------------
# load_yaml()
# ---------------------------------------------------------------------------


class TestLoadYaml:
    """load_yaml() loads a YAML mapping from config_dir()/<name>.yaml."""

    def test_success_returns_dict(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A valid mapping YAML file is loaded and returned as a dict."""
        monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(tmp_path))
        (tmp_path / "crm.yaml").write_text("vendor: attio\napi_key_env: ATTIO_API_KEY\n")
        result = load_yaml("crm")
        assert result == {"vendor": "attio", "api_key_env": "ATTIO_API_KEY"}

    def test_missing_file_raises_config_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A ConfigError is raised (not FileNotFoundError) when the file is absent."""
        monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(tmp_path))
        with pytest.raises(ConfigError, match="crm.yaml"):
            load_yaml("crm")

    def test_non_mapping_raises_config_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A YAML file that parses to a non-mapping (list) raises ConfigError."""
        monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(tmp_path))
        (tmp_path / "bad.yaml").write_text("- item1\n- item2\n")
        with pytest.raises(ConfigError, match="bad.yaml"):
            load_yaml("bad")

    def test_invalid_yaml_raises_config_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A YAML parse error is wrapped in ConfigError with the filename."""
        monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(tmp_path))
        (tmp_path / "broken.yaml").write_text("key: [unclosed\n")
        with pytest.raises(ConfigError, match="broken.yaml"):
            load_yaml("broken")


# ---------------------------------------------------------------------------
# require_env()
# ---------------------------------------------------------------------------


class TestRequireEnv:
    """require_env() returns the env var value or raises ConfigError."""

    def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returns the value when the variable is set and non-empty."""
        monkeypatch.setenv("MY_TEST_VAR", "hello")
        assert require_env("MY_TEST_VAR") == "hello"

    def test_unset_raises_config_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Raises ConfigError naming the variable when it is unset."""
        monkeypatch.delenv("MY_TEST_VAR", raising=False)
        with pytest.raises(ConfigError, match="MY_TEST_VAR"):
            require_env("MY_TEST_VAR")

    def test_empty_string_raises_config_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty string is treated as missing — raises ConfigError."""
        monkeypatch.setenv("MY_TEST_VAR", "")
        with pytest.raises(ConfigError, match="MY_TEST_VAR"):
            require_env("MY_TEST_VAR")

    def test_whitespace_only_raises_config_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Whitespace-only value is treated as missing — raises ConfigError."""
        monkeypatch.setenv("MY_TEST_VAR", "   ")
        with pytest.raises(ConfigError, match="MY_TEST_VAR"):
            require_env("MY_TEST_VAR")


# ---------------------------------------------------------------------------
# resolve_env_ref()
# ---------------------------------------------------------------------------


class TestResolveEnvRef:
    """resolve_env_ref() looks up an env-var *name* stored under a config key."""

    def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Resolves the env-var name from config and returns the env value."""
        monkeypatch.setenv("ATTIO_API_KEY", "secret-value")
        cfg = {"api_key_env": "ATTIO_API_KEY"}
        assert resolve_env_ref(cfg, "api_key_env") == "secret-value"

    def test_required_false_key_absent_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When required=False and the key is absent from config, return None."""
        cfg: dict = {}
        result = resolve_env_ref(cfg, "optional_key_env", required=False)
        assert result is None

    def test_required_true_key_absent_raises_config_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When required=True (default) and the key is absent, raise ConfigError."""
        cfg: dict = {}
        with pytest.raises(ConfigError, match="missing_key_env"):
            resolve_env_ref(cfg, "missing_key_env")

    def test_key_present_but_env_var_missing_raises_config_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Key in config → must resolve; missing env var raises ConfigError."""
        monkeypatch.delenv("ATTIO_API_KEY", raising=False)
        cfg = {"api_key_env": "ATTIO_API_KEY"}
        with pytest.raises(ConfigError, match="ATTIO_API_KEY"):
            resolve_env_ref(cfg, "api_key_env")

    def test_key_present_but_env_var_empty_raises_config_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Key in config → must resolve; empty env var raises ConfigError."""
        monkeypatch.setenv("ATTIO_API_KEY", "")
        cfg = {"api_key_env": "ATTIO_API_KEY"}
        with pytest.raises(ConfigError, match="ATTIO_API_KEY"):
            resolve_env_ref(cfg, "api_key_env")

    def test_required_false_key_present_resolves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """required=False does NOT skip resolution when key is present."""
        monkeypatch.setenv("SOME_TOKEN", "tok-xyz")
        cfg = {"token_env": "SOME_TOKEN"}
        assert resolve_env_ref(cfg, "token_env", required=False) == "tok-xyz"

    def test_non_string_value_raises_config_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-string config value (e.g. an int) raises ConfigError, not TypeError."""
        cfg = {"api_key_env": 123}
        with pytest.raises(ConfigError, match="api_key_env"):
            resolve_env_ref(cfg, "api_key_env")
