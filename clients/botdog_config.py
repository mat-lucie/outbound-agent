"""Typed loader for the OPTIONAL Botdog transport's operator config.

Botdog is an alternative delivery transport for LinkedIn invites/DMs. It is
**off by default** — PhantomBuster owns sending (see ``clients/sender.py``).
This module holds the per-seat identity the Botdog surface needs when an
operator does turn it on: the campaign ids leads are injected into, the
connected LinkedIn account id, and the never-contact blacklist collection.

Those values are per-seat identity (like phantom IDs), never engine
constants, so they live in ``config/botdog.yaml`` rather than in code. A
worked synthetic reference lives at ``examples/acme/config/botdog.yaml``.

Load rule (per config/README.md convention):

    config/botdog.yaml         (operator's live config, gitignored)  — if present
    config/botdog.example.yaml (shipped neutral template)            — fallback
    neither present                                                  — DISABLED

A MISSING botdog config is fine and means "the transport is not configured"
— every Botdog surface is opt-in. What is NOT fine is a config that claims
``enabled: true`` while carrying no campaign ids or a shipped
``REPLACE_WITH_...`` placeholder: that would submit real prospects into a
campaign that does not exist. :func:`load_botdog_config` raises
:class:`ConfigError` on that combination — fail loud, never a silent
half-configured transport.

Secrets (``BOTDOG_API_KEY``) are NEVER put in YAML; they stay in the
environment, same as every other credential in this engine.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from clients.settings import ConfigError, config_dir, load_yaml

# Env var holding the Botdog API key. Vendor-scoped, so the name is kept
# verbatim (it is not an engine-level OUTBOUND_* knob).
BOTDOG_API_KEY_ENV = "BOTDOG_API_KEY"

# Env var that gates the Botdog event-ingest drain. It does NOT enable
# sending — no send path routes to Botdog (see clients/sender.py and
# workflows/daily_check.py).
BOTDOG_SEND_ENABLED_ENV = "BOTDOG_SEND_ENABLED"

# Env fallback for the connected LinkedIn account id (limits sync).
BOTDOG_ACCOUNT_ID_ENV = "BOTDOG_ACCOUNT_ID"

# Default name of the blacklist collection the never-contact set is seeded
# into. Operator-overridable; the name is the idempotency key, so it must
# stay stable across runs for a given operator.
DEFAULT_BLACKLIST_NAME = "Never-contact (CRM-seeded)"

# Any value still carrying this marker came straight from the shipped
# template and was never filled in.
_PLACEHOLDER_MARKER = "REPLACE_WITH"


@dataclass(frozen=True)
class BotdogConfig:
    """Typed, immutable snapshot of the operator's Botdog identity."""

    # False (the default) means: no Botdog surface may act. Nothing in the
    # engine flips this on by itself.
    enabled: bool

    # Campaign role slug -> Botdog campaign id. Roles are resolved
    # most-specific-first by clients.sender.invite_campaign_roles
    # (``invite_<language>`` then the ``invite`` catch-all).
    campaigns: dict[str, str] = field(default_factory=dict)

    # Connected LinkedIn account id, for the limits-sync command only.
    account_id: str = ""

    # Blacklist collection the never-contact set is seeded into.
    blacklist_name: str = DEFAULT_BLACKLIST_NAME

    # Lowercased company/person name tokens that must NEVER be contacted on
    # any channel, whatever the CRM says. Seeded into the blacklist at any
    # pipeline stage (see scripts/seed_botdog_blacklist.py). Empty by
    # default — the operator's CRM-derived never-contact set is the primary
    # source; this list is the explicit override for names the CRM cannot
    # express.
    denylist_company_tokens: tuple[str, ...] = ()

    def campaign_id(self, role: str) -> str | None:
        """Campaign id for a role slug, or None when the role is unmapped."""
        value = self.campaigns.get(role)
        return value or None

    @property
    def campaign_ids(self) -> tuple[str, ...]:
        """Every configured campaign id, deduped, in config order."""
        return tuple(dict.fromkeys(v for v in self.campaigns.values() if v))


def _config_name() -> str | None:
    """Pick which YAML to load, or None when Botdog is not configured."""
    base = config_dir()
    if (base / "botdog.yaml").is_file():
        return "botdog"
    if (base / "botdog.example.yaml").is_file():
        return "botdog.example"
    return None


def _str_map(raw: Any, *, section: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(
            f"botdog config section {section!r} must be a mapping of role -> "
            f"campaign id (got {type(raw).__name__}). See "
            f"config/botdog.example.yaml."
        )
    out: dict[str, str] = {}
    for key, value in raw.items():
        if value in (None, ""):
            continue
        if not isinstance(value, str):
            raise ConfigError(
                f"botdog config {section}.{key!r} must be a string campaign "
                f"id (got {type(value).__name__})."
            )
        out[str(key)] = value.strip()
    return out


def load_botdog_config() -> BotdogConfig:
    """Load the operator's Botdog identity into a typed :class:`BotdogConfig`.

    Returns a disabled config when no botdog YAML exists at all — the
    transport is opt-in and its absence is the normal case.

    Raises:
        ConfigError: the config is present but self-contradictory —
            ``enabled: true`` with no campaign ids, or with a shipped
            ``REPLACE_WITH_...`` placeholder still in place. Half-configured
            must never resolve to "send anyway".
    """
    name = _config_name()
    if name is None:
        return BotdogConfig(enabled=False)
    raw = load_yaml(name)

    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError(
            f"botdog config key 'enabled' must be a boolean (got "
            f"{type(enabled).__name__}). See config/botdog.example.yaml."
        )

    campaigns = _str_map(raw.get("campaigns"), section="campaigns")

    blacklist = raw.get("blacklist") or {}
    if not isinstance(blacklist, dict):
        raise ConfigError(
            f"botdog config section 'blacklist' must be a mapping (got "
            f"{type(blacklist).__name__}). See config/botdog.example.yaml."
        )
    blacklist_name = str(
        blacklist.get("collection_name") or DEFAULT_BLACKLIST_NAME
    ).strip()
    if not blacklist_name:
        raise ConfigError(
            "botdog config blacklist.collection_name must be a non-empty "
            "string — it is the idempotency key the seeder and the pre-send "
            "presence gate BOTH resolve by name."
        )

    raw_tokens = blacklist.get("denylist_companies") or []
    if not isinstance(raw_tokens, list):
        raise ConfigError(
            f"botdog config blacklist.denylist_companies must be a list of "
            f"name tokens (got {type(raw_tokens).__name__})."
        )
    tokens = tuple(
        str(t).strip().lower() for t in raw_tokens if str(t).strip()
    )

    account_id = str(
        raw.get("account_id") or os.environ.get(BOTDOG_ACCOUNT_ID_ENV, "")
    ).strip()

    if enabled:
        # Fail-loud on a config that claims to be on but cannot route a
        # single lead: submitting into a non-existent campaign is a silent
        # prospect-facing failure, and a shipped placeholder id is not a
        # campaign at all.
        if not campaigns:
            raise ConfigError(
                "botdog config sets enabled: true but declares no campaigns. "
                "Add at least an `invite` role under `campaigns:` (see "
                "config/botdog.example.yaml), or set enabled: false."
            )
        placeholders = sorted(
            role for role, value in campaigns.items()
            if _PLACEHOLDER_MARKER in value
        )
        if placeholders:
            raise ConfigError(
                f"botdog config sets enabled: true but campaign id(s) "
                f"{placeholders} still carry the shipped "
                f"{_PLACEHOLDER_MARKER}... placeholder. Fill in the real "
                f"campaign ids from the Botdog dashboard, or set "
                f"enabled: false."
            )
        if _PLACEHOLDER_MARKER in account_id:
            raise ConfigError(
                "botdog config sets enabled: true but account_id still "
                f"carries the shipped {_PLACEHOLDER_MARKER}... placeholder."
            )

    return BotdogConfig(
        enabled=enabled,
        campaigns=campaigns,
        account_id=account_id,
        blacklist_name=blacklist_name,
        denylist_company_tokens=tokens,
    )
