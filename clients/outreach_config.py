"""Typed loader for the operator's outreach operational knobs.

The engine's caps (daily invite / DM limits, invite batch size), DM cadence
intervals, post-DM3 nurture cadence, lane priority, weekend send-days, and the
per-company throttle window are OPERATOR-DEFINED policy — they describe HOW
MUCH / HOW OFTEN / WHICH-FIRST the operator wants to reach out. This module
externalizes those VALUES into ``config/outreach.yaml`` (falling back to the
shipped ``config/outreach.example.yaml`` neutral template) and exposes them as a
typed :class:`OutreachConfig`.

Lives in ``clients/`` (the foundation layer, alongside ``settings.py`` and
``pb_config.py``) rather than ``workflows/`` because BOTH ``models/`` (e.g.
``business_calendar``, ``resolution``) and ``workflows/`` consume it — putting
it in ``clients/`` keeps every dependency pointing downward and avoids a
``models -> workflows`` import inversion.

Load rule (per config/README.md convention):

    config/outreach.yaml         (operator's live config, gitignored)  — if present
    config/outreach.example.yaml (shipped neutral template)            — fallback

Fails loudly with :class:`ConfigError` when neither file exists or a required
section/key is missing/mistyped/out-of-range — never a silent default that
masks a misconfiguration.
"""

from dataclasses import dataclass
from typing import Any

from clients.settings import ConfigError, config_dir, load_yaml

# Day-name -> date.weekday() int (Mon=0 .. Sun=6). The schedule.send_days list
# uses these short names; the loader maps them to the ints is_send_day compares.
_DAY_NAME_TO_WEEKDAY: dict[str, int] = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}

# Exact key sets the nested maps MUST carry. The lane keys are the wire-stable
# ``scoring_lane`` strings minted in workflows.quality_gate; the nurture keys are
# the typed cadence lanes in workflows.cadence. Requiring the exact set (no
# missing, no extra) keeps a typo from silently dropping a lane.
_LANE_RANK_KEYS = frozenset({"enterprise_mode", "target_company_mode", "legacy"})
_NURTURE_KEYS = frozenset({"enterprise", "target", "legacy"})


@dataclass(frozen=True)
class OutreachConfig:
    """Typed, immutable snapshot of the operator's outreach operational knobs.

    Every field maps to a former hardcoded module constant. Frozen so a loaded
    config can't be reassigned mid-run.
    """

    # caps
    invites_per_day: int
    dms_per_day: int
    visits_per_day: int
    invite_batch_size: int

    # cadence — calendar days from the prior milestone until a step is eligible
    cadence_connect_to_dm1_days: int
    cadence_dm1_to_dm2_days: int
    cadence_dm2_to_dm3_days: int

    # cadence.nurture_business_days — post-DM3 re-engagement, business days, by lane
    nurture_business_days: dict[str, int]

    # lanes.rank — invite-queue priority (lower = sent first), by scoring_lane
    lane_rank: dict[str, int]

    # schedule.send_days — weekday ints on which outreach may fire
    send_days: frozenset[int]

    # throttle — no further outreach to a company for N days after a contact
    company_throttle_window_days: int

    # staleness — same-machine running row older than this many hours is
    # taken over (prior process assumed crashed). Cross-machine rows are
    # never auto-taken-over. (L4-4 audit fix.)
    stale_run_takeover_hours: int

    # sweep — end-of-run company-tally consistency sweep (2026-06-09 desync
    # design §2). window_days bounds how far back stamped companies are
    # verified; repair_circuit_threshold bounds consecutive repair-write
    # failures before the breaker opens. Both optional-with-default so
    # existing configs keep loading.
    sweep_window_days: int
    sweep_repair_circuit_threshold: int


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    """Return ``raw[name]`` as a mapping, or raise ConfigError."""
    value = raw.get(name)
    if not isinstance(value, dict):
        raise ConfigError(
            f"outreach config section {name!r} is missing or not a mapping "
            f"(got {type(value).__name__}). See config/outreach.example.yaml."
        )
    return value


def _int_at_least(
    section: dict[str, Any], key: str, *, section_name: str, minimum: int
) -> int:
    """Return ``section[key]`` as an int >= ``minimum``, or raise ConfigError.

    ``bool`` is rejected explicitly (it is an ``int`` subclass) so a stray
    ``true`` doesn't silently become 1.
    """
    value = section.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(
            f"outreach config key {section_name}.{key!r} must be an integer "
            f"(got {type(value).__name__}). See config/outreach.example.yaml."
        )
    if value < minimum:
        raise ConfigError(
            f"outreach config key {section_name}.{key!r} must be >= {minimum} "
            f"(got {value}). See config/outreach.example.yaml."
        )
    return value


def _int_map_exact_keys(
    section: dict[str, Any],
    key: str,
    *,
    section_name: str,
    required_keys: frozenset[str],
    minimum: int,
) -> dict[str, int]:
    """Return ``section[key]`` as a ``dict[str, int]`` whose key set is EXACTLY
    ``required_keys`` and whose values are ints >= ``minimum``. Raise ConfigError
    on a missing key, an unknown key, or a bad value type/range."""
    value = section.get(key)
    if not isinstance(value, dict):
        raise ConfigError(
            f"outreach config key {section_name}.{key!r} must be a mapping "
            f"(got {type(value).__name__}). See config/outreach.example.yaml."
        )
    got_keys = frozenset(value)
    if got_keys != required_keys:
        missing = required_keys - got_keys
        extra = got_keys - required_keys
        raise ConfigError(
            f"outreach config key {section_name}.{key!r} must have exactly the "
            f"keys {sorted(required_keys)} (missing={sorted(missing)}, "
            f"unknown={sorted(extra)}). See config/outreach.example.yaml."
        )
    out: dict[str, int] = {}
    for k, v in value.items():
        if not isinstance(v, int) or isinstance(v, bool):
            raise ConfigError(
                f"outreach config key {section_name}.{key}.{k!r} must be an "
                f"integer (got {type(v).__name__}). "
                f"See config/outreach.example.yaml."
            )
        if v < minimum:
            raise ConfigError(
                f"outreach config key {section_name}.{key}.{k!r} must be "
                f">= {minimum} (got {v}). See config/outreach.example.yaml."
            )
        out[k] = v
    return out


def _send_days(section: dict[str, Any]) -> frozenset[int]:
    """Return ``schedule.send_days`` as a non-empty frozenset of weekday ints.

    Each element must be a known short day name (mon..sun). Raises ConfigError
    on an empty list, a non-list, or an unknown name — a blank or typo'd
    send-day list would silently gate every day off (or on)."""
    value = section.get("send_days")
    if not isinstance(value, list) or not value:
        raise ConfigError(
            "outreach config key 'schedule.send_days' must be a non-empty list "
            "of day names (mon..sun). See config/outreach.example.yaml."
        )
    days: set[int] = set()
    for elem in value:
        if not isinstance(elem, str) or elem.lower() not in _DAY_NAME_TO_WEEKDAY:
            raise ConfigError(
                f"outreach config key 'schedule.send_days' has an invalid day "
                f"{elem!r}; expected one of {sorted(_DAY_NAME_TO_WEEKDAY)}. "
                f"See config/outreach.example.yaml."
            )
        days.add(_DAY_NAME_TO_WEEKDAY[elem.lower()])
    return frozenset(days)


def _config_name() -> str:
    """Pick which YAML to load: live ``outreach.yaml`` if present, else the
    shipped neutral ``outreach.example.yaml`` template."""
    base = config_dir()
    if (base / "outreach.yaml").is_file():
        return "outreach"
    if (base / "outreach.example.yaml").is_file():
        return "outreach.example"
    raise ConfigError(
        f"No outreach config found in {base}: expected outreach.yaml or "
        f"outreach.example.yaml. Copy outreach.example.yaml to outreach.yaml "
        f"and edit it, or restore the template."
    )


def load_outreach_config() -> OutreachConfig:
    """Load the operator's outreach knobs into a typed :class:`OutreachConfig`.

    Reads ``config/outreach.yaml`` when present, otherwise the shipped
    ``config/outreach.example.yaml`` neutral template. Raises
    :class:`ConfigError` on a missing file, malformed YAML, or a
    missing/mistyped/out-of-range section or key.
    """
    raw = load_yaml(_config_name())

    caps = _section(raw, "caps")
    cadence = _section(raw, "cadence")
    lanes = _section(raw, "lanes")
    schedule = _section(raw, "schedule")
    throttle = _section(raw, "throttle")
    staleness = raw.get("staleness") or {}
    sweep = raw.get("sweep") or {}

    # Optional-with-default knobs: validate via _int_at_least (type check,
    # bool-reject, range floor) when the key is present; otherwise default to 3.
    if "stale_run_takeover_hours" in staleness:
        stale_run_takeover_hours = _int_at_least(
            staleness, "stale_run_takeover_hours", section_name="staleness", minimum=1
        )
    else:
        stale_run_takeover_hours = 3
    if "window_days" in sweep:
        sweep_window_days = _int_at_least(
            sweep, "window_days", section_name="sweep", minimum=1
        )
    else:
        sweep_window_days = 3
    if "repair_circuit_threshold" in sweep:
        sweep_repair_circuit_threshold = _int_at_least(
            sweep, "repair_circuit_threshold", section_name="sweep", minimum=1
        )
    else:
        sweep_repair_circuit_threshold = 3

    invites_per_day = _int_at_least(caps, "invites_per_day", section_name="caps", minimum=1)
    dms_per_day = _int_at_least(caps, "dms_per_day", section_name="caps", minimum=1)
    # Optional with a default: visits were hard-capped at 50 before this
    # became an operator knob, so configs written before it keep working.
    if "visits_per_day" in caps:
        visits_per_day = _int_at_least(
            caps, "visits_per_day", section_name="caps", minimum=1
        )
    else:
        visits_per_day = 50
    invite_batch_size = _int_at_least(
        caps, "invite_batch_size", section_name="caps", minimum=1
    )
    if invite_batch_size > invites_per_day:
        raise ConfigError(
            f"outreach config 'caps.invite_batch_size' ({invite_batch_size}) must "
            f"be <= 'caps.invites_per_day' ({invites_per_day}); a batch larger than "
            f"the daily cap would be silently clamped. See config/outreach.example.yaml."
        )

    company_throttle_window_days = _int_at_least(
        throttle, "company_window_days", section_name="throttle", minimum=1
    )
    if sweep_window_days > company_throttle_window_days:
        raise ConfigError(
            f"outreach config 'sweep.window_days' ({sweep_window_days}) must be <= "
            f"'throttle.company_window_days' ({company_throttle_window_days}); a sweep "
            f"window wider than the throttle window means a company's stamp could be "
            f"overwritten by fresh outreach before the sweep verifies it. "
            f"See config/outreach.example.yaml."
        )

    return OutreachConfig(
        invites_per_day=invites_per_day,
        dms_per_day=dms_per_day,
        visits_per_day=visits_per_day,
        invite_batch_size=invite_batch_size,
        cadence_connect_to_dm1_days=_int_at_least(
            cadence, "connect_to_dm1_days", section_name="cadence", minimum=1
        ),
        cadence_dm1_to_dm2_days=_int_at_least(
            cadence, "dm1_to_dm2_days", section_name="cadence", minimum=1
        ),
        cadence_dm2_to_dm3_days=_int_at_least(
            cadence, "dm2_to_dm3_days", section_name="cadence", minimum=1
        ),
        nurture_business_days=_int_map_exact_keys(
            cadence,
            "nurture_business_days",
            section_name="cadence",
            required_keys=_NURTURE_KEYS,
            minimum=1,
        ),
        lane_rank=_int_map_exact_keys(
            lanes,
            "rank",
            section_name="lanes",
            required_keys=_LANE_RANK_KEYS,
            minimum=0,  # rank 0 is the highest priority (valid)
        ),
        send_days=_send_days(schedule),
        company_throttle_window_days=company_throttle_window_days,
        stale_run_takeover_hours=stale_run_takeover_hours,
        sweep_window_days=sweep_window_days,
        sweep_repair_circuit_threshold=sweep_repair_circuit_threshold,
    )
