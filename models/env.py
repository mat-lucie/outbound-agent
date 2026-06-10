"""Small env-var helpers for operator-tunable configuration.

Operator-tunable integer thresholds all need the same shape: read a
name, fall back to a default, warn-and-fall-back on garbage input,
optionally require positive. Centralizing avoids divergent fallback
messages and the small but real risk that one parser quietly accepts
something another rejects.

"""

from __future__ import annotations

import os
import sys


def env_int_positive(
    name: str,
    default: int,
    *,
    allow_zero: bool = False,
) -> int:
    """Parse ``os.environ[name]`` as int with ``default`` on miss / garbage.

    - Unset name → ``default``.
    - Non-int value → stderr warning, returns ``default``.
    - ``allow_zero=False`` (the default) treats ``value <= 0`` as garbage
      and warns + falls back. ``allow_zero=True`` accepts 0 but still
      rejects negatives.

    The stderr warning is the operator's only signal that an override
    didn't take effect, so it's not silent (§0 invariant #9).
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        print(
            f"WARNING: {name}={raw!r} is not an integer; "
            f"falling back to default {default}",
            file=sys.stderr,
        )
        return default
    min_value = 0 if allow_zero else 1
    if parsed < min_value:
        print(
            f"WARNING: {name}={parsed} below minimum {min_value}; "
            f"falling back to default {default}",
            file=sys.stderr,
        )
        return default
    return parsed
