"""Observability baseline — typed counters surfaced per daily run.

In-process counters for a single sales-daily invocation. Send-
touching workflows receive a reference and bump the relevant counter;
end-of-run renders all non-zero counters to stderr so the operator
gets structured numbers without grepping the audit log.

Heavier metrics (latency histograms, per-error-class rates) belong in
the Attio Daily Run row where they're queryable by the dashboard;
this module is the stderr summary only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class DailyRunMetrics:
    pb_launches_attempted: int = 0
    pb_launches_skipped_dry_run: int = 0
    attio_writes_attempted: int = 0
    attio_writes_skipped_dry_run: int = 0
    bulk_fetch_records_requested: int = 0
    bulk_fetch_records_returned: int = 0
    bulk_fetch_records_failed: int = 0
    quarantine_skipped: int = 0
    starvation_triggers_fired: int = 0
    escalations_fired: int = 0
    escalations_by_type: dict[str, int] = field(default_factory=dict)
    runtime_warnings: list[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        self.runtime_warnings.append(message)

    def bump_escalation(self, escalation_type: str) -> None:
        """Record that one escalation of the given type was opened.

        Call this at each escalate() call site in daily_check.py so the
        run-end render can show ``escalations: 3 (2 degree_unknown,
        1 missing_linkedin_url)`` without wiring metrics through
        workflows.escalation (which would create a circular dependency).
        """
        self.escalations_fired += 1
        self.escalations_by_type[escalation_type] = (
            self.escalations_by_type.get(escalation_type, 0) + 1
        )

    def to_dict(self) -> dict[str, Any]:
        # Drift-proof via dataclasses.asdict — a new field added below
        # automatically shows up in the snapshot without a parallel
        # edit here.
        return asdict(self)

    def render(self) -> str:
        """One-line-per-non-zero stderr render."""
        lines: list[str] = ["=== Daily run metrics ==="]
        snapshot = self.to_dict()
        warnings = snapshot.pop("runtime_warnings")
        by_type = snapshot.pop("escalations_by_type")
        for key, value in snapshot.items():
            if value:
                lines.append(f"  {key}: {value}")
        # Surface escalation breakdown when any fired.
        if by_type:
            breakdown = ", ".join(
                f"{count} {etype}"
                for etype, count in sorted(by_type.items())
            )
            lines.append(f"  escalations: {self.escalations_fired} ({breakdown})")
        if warnings:
            lines.append("  warnings:")
            lines.extend(f"    - {w}" for w in warnings)
        if len(lines) == 1:
            lines.append("  (no notable activity)")
        return "\n".join(lines)
