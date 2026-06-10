"""Typed discriminator for live-vs-dry-run send paths.

Gating on `mode is RunMode.LIVE` (vs. a stray boolean kwarg) lets
mypy + grep catch missing gates at every send site.
"""

from __future__ import annotations

from enum import Enum


class RunMode(Enum):
    """Live vs. dry-run discriminator. LIVE produces real PB launches
    and real Attio stage mutations; DRY_RUN produces neither.

    A future tier (e.g. STAGING) should be added here + the grep
    guard extended, NOT introduced as a separate boolean flag — the
    whole point of the enum is to keep the gate vocabulary unified.
    """

    LIVE = "live"
    DRY_RUN = "dry_run"

    @classmethod
    def from_dry_run_flag(cls, dry_run: bool) -> RunMode:
        return cls.DRY_RUN if dry_run else cls.LIVE

    def is_live(self) -> bool:
        return self is RunMode.LIVE

    def is_dry_run(self) -> bool:
        return self is RunMode.DRY_RUN
