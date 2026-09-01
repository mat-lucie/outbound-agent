"""Botdog account-limit sync against our daily cap config.

# Why

Two safety layers gate every Botdog send: our own daily lease/ledger
(``workflows/safety_limits.py``) AND Botdog's per-account server-side
limits. If Botdog's limits are looser than ours, Botdog can out-send our
policy on a day our own gate is bypassed or races. This module makes the
two agree — REPORT-ONLY by default; only an explicit apply writes to
Botdog. Limits are NEVER changed silently, and never from the daily run.

# Cap mapping

``desired_caps()`` maps our two caps to Botdog's account-limit keys.
Response shape: ``{"limits": {"invitations": {"limit": N, "usedToday":
..., "remaining": ...}, "messages": {...}, ...}}``. Comparison covers only
the keys we manage — a Botdog limit we don't manage is left untouched, and
a managed key MISSING from the response is reported as a mismatch (loud),
never assumed-correct.

Direction policy: a Botdog limit LOWER than ours is OK (stricter server
side can never out-send our policy — the two-layer principle only requires
Botdog <= ours). Only a Botdog limit ABOVE ours is a mismatch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from workflows.safety_limits import (
    MAX_CONNECTIONS_PER_DAY,
    MAX_MESSAGES_PER_DAY,
)

if TYPE_CHECKING:
    from clients.botdog import BotdogClient

# Botdog account-limit keys we manage → the source-of-truth cap constant.
# Keys are the entries under the response's `limits` object. Connections ==
# LinkedIn invites; messages == DMs.
CAP_KEY_INVITES = "invitations"
CAP_KEY_MESSAGES = "messages"


def desired_caps() -> dict[str, int]:
    """Our daily caps expressed as Botdog account limits."""
    return {
        CAP_KEY_INVITES: MAX_CONNECTIONS_PER_DAY,
        CAP_KEY_MESSAGES: MAX_MESSAGES_PER_DAY,
    }


def _actual_limit(actual: dict, key: str) -> int | None:
    """Extract one managed limit from the response shape.

    ``{"limits": {key: {"limit": N, ...}}}``; tolerates a flat ``{key: N}``
    fallback. None when absent/unparseable.
    """
    limits = actual.get("limits")
    node = limits.get(key) if isinstance(limits, dict) else actual.get(key)
    if isinstance(node, dict):
        node = node.get("limit")
    # `not isinstance(node, bool)`: bool is an int subclass, so a response
    # of `{"invitations": true}` would otherwise read as the limit 1 and
    # silently PASS the comparison — "assume the cap is fine" is the unsafe
    # direction. An unparseable limit is reported as missing (loud) instead.
    if isinstance(node, bool):
        return None
    return node if isinstance(node, int) else None


@dataclass(frozen=True)
class LimitsComparison:
    """Result of comparing our desired caps against Botdog's actual limits."""

    matches: bool
    # (key, desired, actual) for keys present but different.
    mismatches: list[tuple[str, int, object]] = field(default_factory=list)
    # keys we manage that Botdog's response did not include.
    missing: list[str] = field(default_factory=list)


def compare_limits(desired: dict[str, int], actual: dict) -> LimitsComparison:
    """Compare desired caps to Botdog's reported limits (managed keys only).

    Direction policy (docstring above): only Botdog ABOVE ours mismatches;
    equal-or-stricter passes.
    """
    mismatches: list[tuple[str, int, object]] = []
    missing: list[str] = []
    for key, want in desired.items():
        have = _actual_limit(actual, key)
        if have is None:
            missing.append(key)
            continue
        if have > want:
            mismatches.append((key, want, have))
    return LimitsComparison(
        matches=not mismatches and not missing,
        mismatches=mismatches,
        missing=missing,
    )


def format_comparison(cmp: LimitsComparison, desired: dict[str, int]) -> str:
    """Human-readable one-block summary for the CLI / run report."""
    if cmp.matches:
        return f"Botdog limits match our caps ({desired}). OK."
    lines = ["⚠ Botdog limits DO NOT match our caps:"]
    for key, want, have in cmp.mismatches:
        lines.append(f"  - {key}: Botdog={have!r}, ours={want!r}")
    for key in cmp.missing:
        lines.append(
            f"  - {key}: MISSING from Botdog's response, ours={desired[key]!r}"
        )
    return "\n".join(lines)


def resolve_account_id(botdog: BotdogClient, explicit: str | None = None) -> str:
    """Resolve the Botdog account to sync.

    ``explicit`` wins (the CLI passes ``config/botdog.yaml``'s
    ``account_id`` / ``BOTDOG_ACCOUNT_ID`` here). Otherwise, when exactly
    one account is connected, use it. Ambiguity (0 or >1 accounts, no
    explicit id) raises with an actionable message rather than guessing
    which seat to reconfigure.
    """
    if explicit:
        return explicit
    accounts = botdog.get_accounts()
    ids = [
        str(acc.get("id"))
        for acc in accounts
        if isinstance(acc, dict) and acc.get("id")
    ]
    if len(ids) == 1:
        return ids[0]
    raise ValueError(
        f"cannot resolve Botdog account id automatically (found {len(ids)}: "
        f"{ids}); set account_id in config/botdog.yaml or BOTDOG_ACCOUNT_ID."
    )


def sync_limits(
    botdog: BotdogClient, *, account_id: str, apply: bool
) -> dict:
    """Compare (and, when ``apply``, set) Botdog's limits to our caps.

    REPORT-ONLY when ``apply`` is False: reads limits, compares, returns
    the report — writes NOTHING. When ``apply`` is True and there is a
    mismatch, calls ``set_account_limits`` with the full desired cap set
    and re-reads to confirm. Returns a report dict for the caller to
    render / key an exit code off.
    """
    desired = desired_caps()
    actual = botdog.get_account_limits(account_id)
    cmp = compare_limits(desired, actual)
    report: dict = {
        "account_id": account_id,
        "desired": desired,
        "actual": actual,
        "matches": cmp.matches,
        "mismatches": cmp.mismatches,
        "missing": cmp.missing,
        "applied": False,
    }
    if cmp.matches or not apply:
        return report
    # Mismatch + apply: set the full desired set (never a partial patch —
    # the whole managed cap set moves together), then confirm.
    botdog.set_account_limits(account_id, desired)
    confirmed = botdog.get_account_limits(account_id)
    confirm_cmp = compare_limits(desired, confirmed)
    report["applied"] = True
    report["actual"] = confirmed
    report["matches"] = confirm_cmp.matches
    report["mismatches"] = confirm_cmp.mismatches
    report["missing"] = confirm_cmp.missing
    return report
