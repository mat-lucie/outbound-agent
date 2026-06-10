"""Typed loader for PhantomBuster configuration (P3 consolidation).

Before P3, PhantomBuster config was read via scattered ``os.environ`` calls
across ``clients/phantombuster.py``, ``workflows/daily_check_helpers.py``,
``workflows/weekly_prospect.py``, and ``cli.py`` (via Click ``envvar=``). This
module centralizes the NON-SECRET config — the PhantomBuster agent (phantom)
IDs and the pre-invite degree-check backend — into a typed :class:`PBConfig`,
and documents the secret env vars (API key, cookies, user-agent) without moving
them into YAML.

Resolution rule (behavior-preserving):

* **Agent IDs + backend** — read from ``config/phantombuster.yaml`` if present,
  ELSE from the env var, ELSE the current code default. With NO yaml (the test
  suite + the current deploy), behavior is byte-identical to today's pure-env
  reads. With a yaml, operator config wins over the env var.
* **Secrets** (``PHANTOMBUSTER_API_KEY``, ``PB_LI_SESSION_COOKIE``,
  ``PB_LI_SALES_NAV_SESSION_COOKIE``, ``PB_LI_USER_AGENT``) — keep reading from
  ENV with the EXACT current defaults at each call site. Secrets are NEVER put
  in YAML (see config/README.md). This keeps ``tests/conftest.py``'s ``delenv``
  isolation and every ``monkeypatch.setenv`` test working unchanged.

Critical invariant: the agent IDs + backend are read LIVE on every
:func:`load_pb_config` call (no import-time caching), so a fresh value in the
environment (or a per-test ``monkeypatch`` / the conftest ``delenv``) is always
honored. ``PRE_INVITE_DEGREE_CHECK_BACKEND`` stays hot-reloadable exactly as
before.

A MISSING ``config/phantombuster.yaml`` is fine — the loader falls back to env.
Only a PRESENT-but-malformed yaml raises :class:`ConfigError`.
"""

import os
from dataclasses import dataclass
from typing import Any

from clients.settings import ConfigError, config_dir, load_yaml

# The phantom-ID env var names, paired with their YAML keys under `agents:`.
# Order/membership mirrors the env vars consolidated in P3 (see .env.example
# lines 10-32). `inbox_scraper` is included because cli.py reads it via the
# same Click `envvar=` mechanism as the others, even though the original P3
# brief enumerated only the 6 outbound-launch IDs.
_AGENT_ENV_BY_KEY: dict[str, str] = {
    "search_export": "PB_SEARCH_EXPORT_ID",
    "network_booster": "PB_NETWORK_BOOSTER_ID",
    "message_sender": "PB_MESSAGE_SENDER_ID",
    "profile_scraper": "PB_PROFILE_SCRAPER_ID",
    "sales_nav_profile_scraper": "PB_SALES_NAV_PROFILE_SCRAPER_ID",
    "sales_nav_url_converter": "PB_SALES_NAV_URL_CONVERTER_ID",
    "inbox_scraper": "PB_INBOX_SCRAPER_ID",
}

# Pre-invite degree-check backend: env var + YAML key + the current code
# default. The default string MUST stay "regular" — every caller that reads
# PRE_INVITE_DEGREE_CHECK_BACKEND today defaults to it when the var is unset.
_BACKEND_ENV = "PRE_INVITE_DEGREE_CHECK_BACKEND"
_BACKEND_YAML_KEY = "pre_invite_degree_check_backend"
_BACKEND_DEFAULT = "regular"


@dataclass(frozen=True)
class PBConfig:
    """Typed, immutable snapshot of the NON-SECRET PhantomBuster config.

    Carries the resolved phantom (agent) IDs and the pre-invite degree-check
    backend. Each agent ID is the raw resolved value — ``""`` when neither yaml
    nor env supplies it (matching the prior ``os.environ.get(..., "")`` /
    Click-``envvar`` default of an unset option). Callers preserve their own
    downstream semantics (e.g. ``.strip()``, the "skip if empty" gate).

    Secrets are deliberately NOT fields here — they are resolved from ENV at
    each call site via the accessor functions below so per-call-site defaults
    (e.g. the user-agent default) stay byte-identical to the pre-P3 reads.
    """

    # Phantom (agent) IDs — "" when unset (yaml absent AND env unset).
    search_export_id: str
    network_booster_id: str
    message_sender_id: str
    profile_scraper_id: str
    sales_nav_profile_scraper_id: str
    sales_nav_url_converter_id: str
    inbox_scraper_id: str

    # Pre-invite degree-check backend, RAW (not validated). The "regular" /
    # "sales_nav" validation stays in workflows.daily_check_helpers so the
    # exact SalesNavConfigError contract + cross-wire/cookie checks are
    # preserved unchanged.
    degree_check_backend_raw: str


def _yaml_present() -> bool:
    """True iff a live ``config/phantombuster.yaml`` exists.

    The example template (``phantombuster.example.yaml``) is intentionally NOT
    consulted: unlike ICP, PhantomBuster's example holds placeholder phantom
    IDs, not a usable default. Absent a live yaml, resolution falls through to
    env (the current-deploy + test behavior).
    """
    return (config_dir() / "phantombuster.yaml").is_file()


def _load_yaml_or_raise() -> dict[str, Any]:
    """Load ``config/phantombuster.yaml`` (caller has confirmed it exists).

    Raises :class:`ConfigError` on malformed YAML or a non-mapping top level
    (via :func:`load_yaml`), or when the ``agents`` section — if present — is
    not a mapping.
    """
    raw = load_yaml("phantombuster")
    agents = raw.get("agents")
    if agents is not None and not isinstance(agents, dict):
        raise ConfigError(
            "phantombuster.yaml 'agents' section must be a mapping "
            f"(got {type(agents).__name__}). See config/phantombuster.example.yaml."
        )
    return raw


def _resolve_agent_id(
    agents: dict[str, Any], yaml_key: str, env_var: str
) -> str:
    """Resolve one agent ID: yaml value if present, else env, else ``""``.

    A yaml value that is present but blank/whitespace falls through to env
    (an operator who left a placeholder empty hasn't actually configured it).
    Non-string yaml values raise — a typo'd numeric/bool ID is a misconfig,
    not a silent fallback.
    """
    if yaml_key in agents:
        val = agents[yaml_key]
        if val is not None and not isinstance(val, str):
            raise ConfigError(
                f"phantombuster.yaml agents.{yaml_key!r} must be a string "
                f"(got {type(val).__name__}). See config/phantombuster.example.yaml."
            )
        if isinstance(val, str) and val.strip():
            return val
    return os.environ.get(env_var, "")


def _resolve_backend(raw: dict[str, Any]) -> str:
    """Resolve the degree-check backend: yaml → env → ``"regular"``.

    Returns the RAW value (no validation). A blank yaml value falls through to
    env, then to the default — preserving the unset→"regular" semantics every
    current caller relies on. A non-string yaml value raises.
    """
    if _BACKEND_YAML_KEY in raw:
        val = raw[_BACKEND_YAML_KEY]
        if val is not None and not isinstance(val, str):
            raise ConfigError(
                f"phantombuster.yaml {_BACKEND_YAML_KEY!r} must be a string "
                f"(got {type(val).__name__}). See config/phantombuster.example.yaml."
            )
        if isinstance(val, str) and val.strip():
            return val
    return os.environ.get(_BACKEND_ENV, _BACKEND_DEFAULT)


def load_pb_config() -> PBConfig:
    """Load the NON-SECRET PhantomBuster config into a typed :class:`PBConfig`.

    Reads ``config/phantombuster.yaml`` when present (operator config wins for
    agent IDs + backend), otherwise resolves every value from the environment
    with the current code defaults. A MISSING yaml is fine — the loader falls
    back to env. A PRESENT-but-malformed yaml raises :class:`ConfigError`.

    Called LIVE wherever PB config is needed (no module-level caching), so a
    fresh env value — or a per-test ``monkeypatch`` / the conftest ``delenv`` —
    is always honored. The degree-check backend stays hot-reloadable.
    """
    raw: dict[str, Any] = _load_yaml_or_raise() if _yaml_present() else {}
    agents = raw.get("agents") or {}

    return PBConfig(
        search_export_id=_resolve_agent_id(
            agents, "search_export", _AGENT_ENV_BY_KEY["search_export"]
        ),
        network_booster_id=_resolve_agent_id(
            agents, "network_booster", _AGENT_ENV_BY_KEY["network_booster"]
        ),
        message_sender_id=_resolve_agent_id(
            agents, "message_sender", _AGENT_ENV_BY_KEY["message_sender"]
        ),
        profile_scraper_id=_resolve_agent_id(
            agents, "profile_scraper", _AGENT_ENV_BY_KEY["profile_scraper"]
        ),
        sales_nav_profile_scraper_id=_resolve_agent_id(
            agents,
            "sales_nav_profile_scraper",
            _AGENT_ENV_BY_KEY["sales_nav_profile_scraper"],
        ),
        sales_nav_url_converter_id=_resolve_agent_id(
            agents,
            "sales_nav_url_converter",
            _AGENT_ENV_BY_KEY["sales_nav_url_converter"],
        ),
        inbox_scraper_id=_resolve_agent_id(
            agents, "inbox_scraper", _AGENT_ENV_BY_KEY["inbox_scraper"]
        ),
        degree_check_backend_raw=_resolve_backend(raw),
    )


# ---------------------------------------------------------------------------
# Secret accessors — ENV-only, byte-identical to the pre-P3 call-site reads.
# ---------------------------------------------------------------------------
#
# Secrets never live in YAML. These thin accessors centralize the NAMES of the
# secret env vars while preserving each historical call site's exact default
# and (non-)strip behavior. The user-agent default in particular differs by
# call site, so there is intentionally no single "user_agent" value.

# The shipped fallback User-Agent — the canonical source of the default UA used
# by get_phantombuster_credentials() when PB_LI_USER_AGENT is unset.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)


def require_api_key() -> str:
    """Return ``PHANTOMBUSTER_API_KEY`` from env, raising ``KeyError`` if unset.

    Byte-identical to the historical ``os.environ["PHANTOMBUSTER_API_KEY"]``
    in ``PhantomBusterClient.__init__`` (a missing key raises ``KeyError``,
    NOT ``ConfigError`` — the existing tests set it explicitly).
    """
    return os.environ["PHANTOMBUSTER_API_KEY"]


def li_session_cookie() -> str:
    """``PB_LI_SESSION_COOKIE`` from env, default ``""`` (no strip)."""
    return os.environ.get("PB_LI_SESSION_COOKIE", "")


def li_user_agent_or_default() -> str:
    """``PB_LI_USER_AGENT`` from env, default :data:`DEFAULT_USER_AGENT`.

    Matches ``get_phantombuster_credentials`` — the call site that wants the
    shipped UA when the env var is unset.
    """
    return os.environ.get("PB_LI_USER_AGENT", DEFAULT_USER_AGENT)


def li_user_agent_raw() -> str:
    """``PB_LI_USER_AGENT`` from env, default ``""`` (no strip).

    Matches ``workflows.weekly_prospect._launch_and_download``, which supplies
    its own inline fallback UA when this is blank.
    """
    return os.environ.get("PB_LI_USER_AGENT", "")


def sales_nav_session_cookie_stripped() -> str:
    """``PB_LI_SALES_NAV_SESSION_COOKIE`` from env, default ``""``, stripped.

    Matches ``_pb_sales_nav_session_args`` — which treats a whitespace-only
    cookie as missing.
    """
    return os.environ.get("PB_LI_SALES_NAV_SESSION_COOKIE", "").strip()


def li_user_agent_stripped() -> str:
    """``PB_LI_USER_AGENT`` from env, default ``""``, stripped.

    Matches ``_pb_sales_nav_session_args`` — which omits the UA arg when blank.
    """
    return os.environ.get("PB_LI_USER_AGENT", "").strip()
