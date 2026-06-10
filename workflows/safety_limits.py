"""Safety limits tracker — enforces daily LinkedIn action caps."""

import json
from datetime import date
from pathlib import Path

from clients.outreach_config import load_outreach_config

LIMITS_DIR = Path.home() / ".outbound-agent"
LIMITS_FILE = LIMITS_DIR / "daily_limits.json"

# Hard limits to protect LinkedIn account. Sourced from config/outreach.yaml
# (single source of truth, shared with workflows/daily_run.py).
_OUTREACH = load_outreach_config()
MAX_CONNECTIONS_PER_DAY = _OUTREACH.invites_per_day
MAX_MESSAGES_PER_DAY = _OUTREACH.dms_per_day
MAX_VISITS_PER_DAY = _OUTREACH.visits_per_day


def _load() -> dict:
    """Load current limits state, resetting if it's a new day."""
    if not LIMITS_FILE.exists():
        return _reset()
    with open(LIMITS_FILE) as f:
        data = json.load(f)
    if data.get("date") != date.today().isoformat():
        return _reset()
    return data


def _reset() -> dict:
    """Reset daily counters."""
    data = {
        "date": date.today().isoformat(),
        "connections": 0,
        "messages": 0,
        "visits": 0,
    }
    _save(data)
    return data


def _save(data: dict) -> None:
    """Persist limits state."""
    LIMITS_DIR.mkdir(parents=True, exist_ok=True)
    with open(LIMITS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def can_send_connections(count: int = 1) -> bool:
    """Check if we can send more connection requests today."""
    data = _load()
    return data["connections"] + count <= MAX_CONNECTIONS_PER_DAY


def can_send_messages(count: int = 1) -> bool:
    """Check if we can send more messages today."""
    data = _load()
    return data["messages"] + count <= MAX_MESSAGES_PER_DAY


def can_visit_profiles(count: int = 1) -> bool:
    """Check if we can visit more profiles today."""
    data = _load()
    return data["visits"] + count <= MAX_VISITS_PER_DAY


def record_connections(count: int = 1) -> None:
    """Record connection requests sent."""
    data = _load()
    data["connections"] += count
    _save(data)


def record_messages(count: int = 1) -> None:
    """Record messages sent."""
    data = _load()
    data["messages"] += count
    _save(data)


def record_visits(count: int = 1) -> None:
    """Record profile visits."""
    data = _load()
    data["visits"] += count
    _save(data)


def get_remaining() -> dict:
    """Get remaining capacity for today."""
    data = _load()
    return {
        "connections": MAX_CONNECTIONS_PER_DAY - data["connections"],
        "messages": MAX_MESSAGES_PER_DAY - data["messages"],
        "visits": MAX_VISITS_PER_DAY - data["visits"],
    }


def get_status() -> str:
    """Human-readable status of today's limits."""
    remaining = get_remaining()
    data = _load()
    return (
        f"Date: {data['date']}\n"
        f"Connections: {data['connections']}/{MAX_CONNECTIONS_PER_DAY} "
        f"(remaining: {remaining['connections']})\n"
        f"Messages: {data['messages']}/{MAX_MESSAGES_PER_DAY} "
        f"(remaining: {remaining['messages']})\n"
        f"Visits: {data['visits']}/{MAX_VISITS_PER_DAY} "
        f"(remaining: {remaining['visits']})"
    )
