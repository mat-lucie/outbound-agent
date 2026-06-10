"""Central config-loading module for the outbound engine.

Loads YAML config files from the config/ directory and resolves secrets
from environment variables. Does NOT call load_dotenv() — .env is loaded
at process entry in cli.py. This module only reads os.environ.

Usage pattern:
    cfg = load_yaml("crm")                  # -> dict from config/crm.yaml
    api_key = resolve_env_ref(cfg, "api_key_env")   # reads the env var named by cfg["api_key_env"]
    token = require_env("ATTIO_API_KEY")    # direct env var access

Override the config directory at runtime:
    OUTBOUND_CONFIG_DIR=/path/to/my/config sales ...
"""

import os
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    """Raised on any misconfiguration.

    Always fails loudly — never returns a silent default that masks a
    missing or empty required value.
    """


def config_dir() -> Path:
    """Return the config directory path.

    Uses the ``OUTBOUND_CONFIG_DIR`` environment variable when set and
    non-empty; otherwise falls back to ``<repo_root>/config`` where
    ``repo_root`` is the parent of the ``clients/`` package directory.
    """
    override = os.environ.get("OUTBOUND_CONFIG_DIR", "")
    if override:
        return Path(override)
    # clients/ package dir is Path(__file__).parent; repo root is one level up.
    return Path(__file__).resolve().parent.parent / "config"


def load_yaml(name: str) -> dict[str, Any]:
    """Load ``config_dir()/<name>.yaml`` and return it as a dict.

    Args:
        name: Config file name without the ``.yaml`` extension (e.g. ``"crm"``).

    Returns:
        Parsed YAML content as a dict.

    Raises:
        ConfigError: If the file is missing, fails to parse, or does not
            parse to a mapping.
    """
    path = config_dir() / f"{name}.yaml"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(
            f"Config file not found: {path}. "
            f"Copy {name}.example.yaml to {name}.yaml and fill it in."
        ) from None

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Failed to parse {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(
            f"Config file {path} must contain a YAML mapping at the top level, "
            f"got {type(data).__name__}."
        )

    return data


def require_env(var_name: str) -> str:
    """Return the value of environment variable ``var_name``.

    Args:
        var_name: Name of the environment variable to read.

    Returns:
        The variable's value as a string.

    Raises:
        ConfigError: If the variable is unset, empty, or whitespace-only.
            Empty/blank is treated as missing (no silent zero default).
    """
    value = os.environ.get(var_name)
    if not value or not value.strip():
        raise ConfigError(
            f"Required environment variable {var_name!r} is not set or is empty. "
            f"Add it to your .env file."
        )
    return value


def resolve_env_ref(
    config: dict[str, Any],
    key: str,
    *,
    required: bool = True,
) -> str | None:
    """Resolve an env-var *name* stored under ``key`` in ``config``.

    Config files store the *name* of an env var (never the secret value
    itself), e.g.::

        api_key_env: ATTIO_API_KEY

    This function reads that name from ``config[key]`` and returns the
    actual env var value via :func:`require_env`.

    Args:
        config: Parsed config dict (typically from :func:`load_yaml`).
        key: The key in ``config`` that holds the env-var name.
        required: When ``True`` (the default), a missing ``key`` raises
            ``ConfigError``. When ``False``, a missing ``key`` returns
            ``None``. If the key *is* present it always resolves —
            ``required=False`` only relaxes the absent-key case.

    Returns:
        The resolved env var value, or ``None`` if ``required=False`` and
        the key is absent from ``config``.

    Raises:
        ConfigError: If ``required=True`` and the key is absent; if the
            value under ``key`` is not a string (env-var name); or if the
            referenced env var is unset, empty, or whitespace-only.
    """
    if key not in config:
        if not required:
            return None
        raise ConfigError(
            f"Required config key {key!r} is missing. "
            f"Add it to the relevant config/*.yaml file."
        )

    env_var_name = config[key]
    if not isinstance(env_var_name, str):
        raise ConfigError(
            f"Config key {key!r} must be a string naming an environment "
            f"variable, got {type(env_var_name).__name__} in the relevant "
            f"config/*.yaml file."
        )
    return require_env(env_var_name)
