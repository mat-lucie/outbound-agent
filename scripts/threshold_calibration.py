#!/usr/bin/env python3
"""Score-threshold ROC calibration entry point.

Wraps `workflows.threshold_calibration.calibrate_score_threshold` so
the `/threshold-calibration` slash command can invoke a stable CLI
surface. Pure Attio read + arithmetic — zero LLM cost. NEVER changes
the live threshold in `quality_gate.score_prospect`; opens an
Operator Review Queue row with the ROC table and abstains explicitly
when the labeled cohort is too small or class-imbalanced.

Cost/value tuning: `OUTBOUND_COST_PER_SEND_USD` (default 0.50 — rough
LinkedIn invite cost) and `OUTBOUND_VALUE_PER_POSITIVE_USD` (default 50
— rough expected value of a booked discovery call). Override via
env or `--cost-per-send` / `--value-per-positive`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from clients.attio import AttioClient  # noqa: E402
from models.business_calendar import operator_today  # noqa: E402
from workflows.threshold_calibration import calibrate_score_threshold  # noqa: E402

if TYPE_CHECKING:
    from datetime import date as date_type

DEFAULT_COST_PER_SEND = 0.50
DEFAULT_VALUE_PER_POSITIVE = 50.0


class _DryRunAttio:
    """Wraps an AttioClient so read-side methods pass through but the
    `_request` POSTs (queue-row writes from escalate) get intercepted
    + logged. Lets --dry-run produce a real ROC table from this week's
    live cohort without persisting the recommendation row."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.intercepted_posts: list[dict] = []

    def _request(self, method: str, path: str, json=None, **kwargs):
        if method == "POST" and path.endswith("/records"):
            # Skip the write but pretend it landed so escalate()'s
            # idempotency check downstream still resolves cleanly.
            self.intercepted_posts.append({"path": path, "body": json or {}})
            return {"data": {"id": {"record_id": "dry-run-stub"}}}
        return self._inner._request(method, path, json=json, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _env_float(name: str, default: float) -> float:
    """Read a float env var. Fail loud on garbage — a typo'd override
    skewing the cost-weighted recommendation silently is exactly the
    silent-fallback failure mode the project rule prohibits.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        parsed = float(raw)
    except ValueError as exc:
        print(
            f"error: {name}={raw!r} not a number; cannot interpret "
            f"the override safely. Unset the env var or supply a "
            f"valid float.",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    import math
    if math.isnan(parsed) or math.isinf(parsed) or parsed < 0:
        print(
            f"error: {name}={parsed} must be a non-negative finite "
            f"number; got NaN/Inf/negative.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return parsed


def _render_summary(out: dict) -> str:
    if out["status"] == "abstain":
        return (
            f"ROC calibration ABSTAIN: reason={out['reason']}, "
            f"n_labeled={out['n_labeled']}, "
            f"n_dropped_invalid_score={out['n_dropped_invalid_score']}; "
            f"queue row {out['queue_row_key']} opened."
        )
    youden = out["max_youden"]
    cost = out["cost_weighted"]
    return (
        f"ROC calibration OK: n_labeled={out['n_labeled']}, "
        f"n_dropped_invalid_score={out['n_dropped_invalid_score']}\n"
        f"  max-Youden threshold: {youden['threshold']} "
        f"(TPR={youden['tpr']}, TNR={youden['tnr']}, Youden={youden['youden']})\n"
        f"  cost-weighted threshold: {cost['threshold']} "
        f"(TPR={cost['tpr']}, TNR={cost['tnr']})\n"
        f"  queue row: {out['queue_row_key']}\n"
        f"  NOTE: live threshold in quality_gate.score_prospect NOT changed."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compute + print the ROC sweep without opening a queue row.",
    )
    parser.add_argument(
        "--cost-per-send", type=float,
        default=_env_float("OUTBOUND_COST_PER_SEND_USD", DEFAULT_COST_PER_SEND),
        help="Estimated cost per send (USD). Used by cost-weighted variant.",
    )
    parser.add_argument(
        "--value-per-positive", type=float,
        default=_env_float(
            "OUTBOUND_VALUE_PER_POSITIVE_USD", DEFAULT_VALUE_PER_POSITIVE,
        ),
        help="Estimated value per positive outcome (USD).",
    )
    args = parser.parse_args(argv)

    try:
        attio = AttioClient()
    except KeyError:
        print("error: ATTIO_API_KEY env var not set", file=sys.stderr)
        return 2

    today: date_type = operator_today()
    with attio:
        # Under --dry-run, wrap the AttioClient so the queue-row POST
        # is intercepted (logged + skipped) while every read still
        # hits the real backend. Operator sees the actual ROC table
        # for THIS week's cohort but no queue row lands.
        client = _DryRunAttio(attio) if args.dry_run else attio
        out = calibrate_score_threshold(
            client,
            cost_per_send=args.cost_per_send,
            value_per_positive=args.value_per_positive,
            today=today,
        )

    print(_render_summary(out))
    if args.dry_run:
        print(
            "(--dry-run: queue row was NOT written to Attio)",
            file=sys.stderr,
        )
    print("\nsnapshot:", json.dumps(out, default=str), file=sys.stderr)

    # Exit 0 on both ok and abstain — the queue row IS the operator
    # signal. Non-zero exits are reserved for ATTIO_API_KEY / unexpected
    # failures so a CI chain can distinguish "ran cleanly" from "tooling
    # broken".
    return 0


if __name__ == "__main__":
    sys.exit(main())
