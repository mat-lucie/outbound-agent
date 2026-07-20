"""PR-35 — LLM Budget Ledger (Attio-backed cost gate).

Replaces the F-PR-9 ``LLMBudgetLedger`` stub in ``workflows/llm_dispatch.py``
with an Attio-backed implementation that enforces per-(step, week_starting)
weekly caps per plan §3.7.

Contract (preserved from F-PR-9):
    LLMBudgetLedger.try_reserve(step, estimated_cost_usd, *, attio=None)
        -> None on success; raises CostCeilingExhausted on refusal.

Idempotency: one Attio ``llm_budget_ledger`` row per (step, week_starting).
The first reserve creates the row; subsequent reserves PATCH the same row
to bump ``consumed`` by the estimated cost. The cap-check compares the
current ``consumed + estimated_cost`` against the configured weekly cap
for the step.

PR-35 also seeds an EMPTY ``content/holdout/defensive_holdout_v1.jsonl``
placeholder. The holdout seed-required escalation is dispatched by
``check_holdout_seed`` when the file is empty or missing — operators
curate the 50 examples and re-run.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from clients.attio import AttioClient
    from clients.attio_writer import AttioWriter

WRITER_MODULE = "workflows.llm_budget.LLMBudgetLedger.try_reserve"
LLM_BUDGET_LEDGER_OBJECT = "llm_budget_ledger"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOLDOUT_SEED_PATH = PROJECT_ROOT / "content" / "holdout" / "defensive_holdout_v1.jsonl"

# Per-step weekly caps in USD. Defaults are deliberately conservative so a
# runaway loop trips the cap before burning the week's budget. Tunable per
# the §3.7 ``llm_cost_ceiling_decision`` queue (configuration_decision row);
# the operator updates this dict via a PR after reviewing the queue's
# recommendation. Unknown steps fall through to UNLIMITED_CAP (no cap) —
# rejecting unknown steps would force every new dispatch surface into a
# pre-commit cap-decision queue, which is too heavy for the v1.9 surface.
STEP_WEEKLY_CAP_USD: dict[str, Decimal] = {
    "quality_gate_haiku": Decimal("5.00"),
    "industry_classification": Decimal("2.00"),
    "borderline_verdict": Decimal("3.00"),
    "synthetic_prescreen": Decimal("8.00"),
    "weekly_brain_critique": Decimal("4.00"),
    "company_hq_classifier": Decimal("2.00"),
    "synth_holdout": Decimal("3.00"),
    "diagnostic_critique": Decimal("4.00"),
    "response_classification": Decimal("3.00"),
}

# Sentinel for "no configured cap" — never refuses.
_UNLIMITED_CAP = Decimal("Infinity")

# Attio currency attributes accept at most 4 decimal places; values with
# more precision are rejected with 400 (PR-216 incident: the
# quality_gate_haiku estimate $0.00065 has 5 dp, so every ledger create
# failed and weekly dispatches degraded to borderline_llm_error).
_USD_QUANT = Decimal("0.0001")


def _quantize_spend(value: Decimal) -> Decimal:
    """Quantize a spend amount (consumed / cost_usd_actual) to the Attio
    4-dp limit, rounding UP so the ledger never under-counts against the
    cap. A positive sub-unit estimate floors at $0.0001 rather than 0."""
    return value.quantize(_USD_QUANT, rounding=ROUND_UP)


def _quantize_remaining(value: Decimal) -> Decimal:
    """Quantize a remaining-budget amount to the Attio 4-dp limit,
    rounding DOWN so the ledger never overstates what's left."""
    return value.quantize(_USD_QUANT, rounding=ROUND_DOWN)


# ============================================================
# Exceptions
# ============================================================


class CostCeilingExhausted(Exception):
    """Raised when ``LLMBudgetLedger.try_reserve`` refuses a step.

    Per §3.7 fail-loud rule: dispatch NOT emitted, no subagent invoked,
    calling workflow surfaces this as a typed error and the skill catches
    it to log a ``cost_ceiling_breached`` escalation. The ledger owns the
    forensic numbers; callers don't need to fabricate them.

    Mirrors ``workflows.llm_dispatch.CostCeilingExhausted`` (the stub
    raised the same shape). PR-35 imports this name from llm_dispatch so
    existing catch-blocks continue to work; this class is the canonical
    home and the dispatch-module name is the alias.
    """

    def __init__(self, step: str, cap: Decimal, consumed: Decimal) -> None:
        self.step = step
        self.cap = cap
        self.consumed = consumed
        super().__init__(
            f"Cost ceiling exhausted for {step!r}: cap=${cap:.4f}, "
            f"consumed=${consumed:.4f}. Subagent NOT dispatched."
        )


class LLMBudgetLedgerNotInitialized(Exception):
    """Raised when ``try_reserve`` cannot consult the Attio-backed ledger
    in a production-dispatch context.

    Wave-2 fix for adversarial-pass1-round2 IMPORTANT-2: pre-fix, an
    ATTIO_API_KEY-unset condition was silently treated as "allow this
    dispatch", turning the §3.7 cost cap into stderr-warn-and-proceed
    whenever the production cron's environment was misconfigured (cron
    typically routes stderr to /dev/null, so the warning never
    surfaced).

    Post-fix: when ``$OUTBOUND_USE_LLM_DISPATCH=1`` (production dispatch
    surface) AND ATTIO_API_KEY is unset, ``try_reserve`` raises this
    typed exception so the dispatcher refuses to fire instead of going
    uncapped. Tests + dev shells (where dispatch is disabled by the
    autouse conftest fixture) keep the fail-soft warning path so the
    autouse ``_isolate_attio_api_key`` fixture doesn't break every
    test that touches an LLM-dispatching code path.
    """

    def __init__(self, step: str, reason: str) -> None:
        self.step = step
        self.reason = reason
        super().__init__(
            f"LLMBudgetLedger cannot enforce cap for step={step!r}: "
            f"{reason}. Dispatch refused so §3.7 cost cap is not "
            f"silently bypassed. Fix the configuration before retrying."
        )


# ============================================================
# Week helpers
# ============================================================


def _monday_of(today: date | None = None) -> date:
    """Return the Monday of the week containing ``today``.

    The ``week_starting`` key on llm_budget_ledger rows is always the
    Monday so all dispatches within the same week land in the same row.
    """
    today = today or datetime.now(UTC).date()
    return today - timedelta(days=today.weekday())


# ============================================================
# Ledger
# ============================================================


class LLMBudgetLedger:
    """Attio-backed per-(step, week_starting) weekly cost ceiling.

    The class is intentionally a holder of static methods (no instance
    state) so the F-PR-9 contract — ``LLMBudgetLedger.try_reserve(step,
    cost)`` — keeps working for every existing caller.

    Tests inject an attio mock via the optional ``attio`` kwarg.

    PR-35 swap-in note: workflows/llm_dispatch.py re-exports this class as
    ``LLMBudgetLedger`` so ``from workflows.llm_dispatch import
    LLMBudgetLedger`` keeps working. Don't break that import path.
    """

    @staticmethod
    def try_reserve(
        step: str,
        estimated_cost_usd: float | Decimal,
        *,
        attio: AttioClient | None = None,
        writer: AttioWriter | None = None,
        today: date | None = None,
        require_attio_ledger: bool = False,
    ) -> None:
        """Reserve ``estimated_cost_usd`` for ``step`` in the current week.

        Algorithm:
            1. Look up the configured cap for ``step``. If none configured
               (unknown step), return None (no enforcement).
            2. Look up (or create) the llm_budget_ledger row for
               ``(step, week_starting=Monday(today))``.
            3. If ``current_consumed + estimated_cost > cap``: raise
               ``CostCeilingExhausted(step, cap, current_consumed)``.
            4. Otherwise: PATCH ``consumed = current_consumed +
               estimated_cost`` and ``cap_remaining_this_week``, return
               None.

        Args:
            step: One of the keys in STEP_WEEKLY_CAP_USD (or any string;
                unknown → no enforcement).
            estimated_cost_usd: float or Decimal. Converted to Decimal
                internally for money-safe arithmetic.
            attio: optional AttioClient for testing. Lazy-constructed when
                None.
            writer: optional AttioWriter for testing. Lazy-constructed
                when None.
            today: optional date for testing (controls which Monday).
            require_attio_ledger: Wave-2-B fix-up (code-reviewer B4):
                forces the fail-loud `LLMBudgetLedgerNotInitialized` path
                even when ``$OUTBOUND_USE_LLM_DISPATCH`` is unset. Pass
                ``True`` from callers that have decided to dispatch via
                an explicit ``use_llm_dispatch=True`` parameter rather
                than the env var (e.g. ``synthetic_prescreen.py``). When
                ``False`` (the default), the gate falls back to checking
                ``is_dispatch_enabled()`` for backward compatibility.

        Raises:
            CostCeilingExhausted: cap exceeded for this (step, week).
            LLMBudgetLedgerNotInitialized: production dispatch active +
                ATTIO_API_KEY unset → the cap cannot be enforced; refuse
                to dispatch rather than silently bypass §3.7.
        """
        cap = STEP_WEEKLY_CAP_USD.get(step, _UNLIMITED_CAP)
        if cap == _UNLIMITED_CAP:
            return None

        # Quantize once at entry so the cap check, the created row, and the
        # PATCH increments all use the same 4-dp value Attio will store.
        cost = _quantize_spend(Decimal(str(estimated_cost_usd)))
        week = _monday_of(today)

        # Allow tests to disable the Attio round-trip entirely via env var.
        # When disabled, the ledger is fully in-memory per call — no state
        # persists. This matches the F-PR-9 stub for the legacy test path.
        if (
            os.environ.get("OUTBOUND_LLM_BUDGET_LEDGER_DISABLED", "").strip() == "1"
        ):
            return None

        if attio is None:
            from clients.attio import AttioClient as _AttioClient
            try:
                attio = _AttioClient()
            except KeyError as err:
                # Wave-2 §3.7 leak fix: when production dispatch is
                # active and ATTIO_API_KEY is missing, the cap CANNOT
                # be enforced — proceeding would silently bypass §3.7.
                # Raise the typed LLMBudgetLedgerNotInitialized so the
                # dispatcher (and any direct caller) refuses to fire
                # instead of going uncapped.
                #
                # "Production dispatch active" means EITHER:
                #   (a) the caller explicitly passed
                #       ``require_attio_ledger=True`` (Wave-2-B fix-up
                #       for code-reviewer B4 — closes the gap where
                #       callers like synthetic_prescreen.py enable
                #       dispatch via a ``use_llm_dispatch=True``
                #       parameter without setting the env var), OR
                #   (b) ``$OUTBOUND_USE_LLM_DISPATCH=1`` is set
                #       (slash-command harness path).
                #
                # The autouse conftest `_isolate_attio_api_key` fixture
                # deletes ATTIO_API_KEY in every test, but the same
                # fixture also keeps both signals unset by default —
                # so tests that don't opt in keep the fail-soft stderr
                # warning + return None path.
                from workflows.llm_dispatch import is_dispatch_enabled
                if require_attio_ledger or is_dispatch_enabled():
                    raise LLMBudgetLedgerNotInitialized(
                        step=step,
                        reason=(
                            "ATTIO_API_KEY is unset but production "
                            "dispatch is active (require_attio_ledger="
                            f"{require_attio_ledger}, "
                            f"is_dispatch_enabled={is_dispatch_enabled()}); "
                            "the Attio-backed ledger cannot be consulted "
                            "so cap enforcement would be silently disabled"
                        ),
                    ) from err
                import sys
                print(
                    "WARN: LLMBudgetLedger.try_reserve: ATTIO_API_KEY unset; "
                    f"falling back to allow for step={step!r} cost=${cost:.4f}. "
                    "Cost enforcement DISABLED until ATTIO_API_KEY is set "
                    "(dispatch is also disabled so production cap is not "
                    "bypassed — this branch is dev/test only).",
                    file=sys.stderr,
                )
                return None
        if writer is None:
            from clients.attio_writer import AttioWriter as _AttioWriter
            writer = _AttioWriter(attio=attio)

        # Ledger I/O failures are wrapped in the typed
        # LLMBudgetLedgerUnavailable (PR-216 incident: a raw
        # HTTPStatusError from the 400ing create leaked into the generic
        # LLM-failure bucket and borderlines were silently rejected).
        # CostCeilingExhausted is raised inside this block and must keep
        # its type — it is a budget verdict, not an infra failure.
        import httpx

        from clients.attio_writer import AttioError
        from workflows.llm_dispatch import LLMBudgetLedgerUnavailable

        try:
            existing = _find_ledger_row(attio, step=step, week_starting=week)

            if existing is None:
                current_consumed = Decimal("0")
                new_consumed = cost
                if new_consumed > cap:
                    raise CostCeilingExhausted(step, cap, current_consumed)
                _create_ledger_row(
                    attio,
                    step=step,
                    week_starting=week,
                    cap=cap,
                    consumed=new_consumed,
                )
                return None

            current_consumed = _parse_decimal(existing.get("values"), "consumed")
            new_consumed = current_consumed + cost
            if new_consumed > cap:
                raise CostCeilingExhausted(step, cap, current_consumed)

            record_id = existing.get("id", {}).get("record_id", "")
            cap_remaining = cap - new_consumed
            _patch_ledger_row(
                writer,
                record_id=record_id,
                consumed=new_consumed,
                cap_remaining=cap_remaining,
                cost_usd_actual=cost,
            )
            return None
        except (
            httpx.HTTPStatusError,
            httpx.RequestError,
            httpx.TimeoutException,
            AttioError,
        ) as err:
            raise LLMBudgetLedgerUnavailable(step, err) from err


# ============================================================
# Holdout seed check
# ============================================================


def check_holdout_seed(
    *, attio: AttioClient | None = None, holdout_path: Path | None = None,
) -> dict:
    """Verify ``content/holdout/defensive_holdout_v1.jsonl`` exists and is
    non-empty. When empty or missing, opens a ``holdout_seed_required``
    operator queue row.

    Returns: dict with ``path``, ``exists``, ``line_count``, ``escalated``.

    Per plan §5 PR-35: the operator curates 50 examples manually; this check
    surfaces the gap so it can't silently bypass the synthetic-prescreen
    holdout invariant.
    """
    path = holdout_path or HOLDOUT_SEED_PATH
    exists = path.exists()
    line_count = 0
    if exists:
        with path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    line_count += 1

    needs_seed = (not exists) or line_count == 0
    escalated = False
    if needs_seed and attio is not None:
        from workflows.escalation import escalate

        escalate(
            type="holdout_seed_required",
            idempotency_key="defensive_holdout_v1",
            payload={
                "path": str(path.relative_to(PROJECT_ROOT)
                            if path.is_relative_to(PROJECT_ROOT) else path),
                "exists": exists,
                "line_count": line_count,
                "opened_by": "workflows.llm_budget.check_holdout_seed",
            },
            attio=attio,
        )
        escalated = True

    return {
        "path": str(path),
        "exists": exists,
        "line_count": line_count,
        "escalated": escalated,
        "needs_seed": needs_seed,
    }


# ============================================================
# Internals
# ============================================================


def _parse_decimal(values: dict | None, key: str) -> Decimal:
    """Parse a currency-typed Attio value to Decimal. Returns 0 on missing."""
    if not values or not isinstance(values, dict):
        return Decimal("0")
    arr = values.get(key, [])
    if not arr or not isinstance(arr, list):
        return Decimal("0")
    first = arr[0]
    if not isinstance(first, dict):
        return Decimal("0")
    raw = first.get("currency_value") or first.get("value") or 0
    try:
        return Decimal(str(raw))
    except (ValueError, ArithmeticError):
        return Decimal("0")


def _find_ledger_row(
    attio: AttioClient,
    *,
    step: str,
    week_starting: date,
) -> dict | None:
    """Look up the llm_budget_ledger row for (step, week_starting). Returns
    None when no row exists yet."""
    data = attio._request(
        "POST",
        f"/objects/{LLM_BUDGET_LEDGER_OBJECT}/records/query",
        json={
            "filter": {
                "$and": [
                    {"step": step},
                    {"week_starting": week_starting.isoformat()},
                ],
            },
            "limit": 1,
        },
    )
    rows = data.get("data", [])
    if not rows:
        return None
    return rows[0]


def _create_ledger_row(
    attio: AttioClient,
    *,
    step: str,
    week_starting: date,
    cap: Decimal,
    consumed: Decimal,
) -> str:
    """Create a fresh llm_budget_ledger row. Returns the new record_id.

    Direct POST (not via AttioWriter) — the new record has no id to PATCH
    against. Mirrors the diagnostic_run create path (PR-34 precedent).
    """
    # Attio currency attrs reject values with >4 decimal places (400) —
    # quantize defensively at the serialization boundary even though
    # try_reserve already quantized `consumed`. Plain-notation str() is
    # guaranteed at exponent -4 (Decimal only uses scientific notation
    # for much smaller exponents).
    body = {
        "data": {
            "values": {
                "step": step,
                "week_starting": week_starting.isoformat(),
                "consumed": str(_quantize_spend(consumed)),
                "cap_remaining_this_week": str(_quantize_remaining(cap - consumed)),
                "cost_usd_actual": str(_quantize_spend(consumed)),
            }
        }
    }
    resp = attio._request(
        "POST",
        f"/objects/{LLM_BUDGET_LEDGER_OBJECT}/records",
        json=body,
    )
    record = resp.get("data", resp)
    return (
        record.get("id", {}).get("record_id")
        if isinstance(record.get("id"), dict)
        else record.get("record_id", "")
    ) or ""


def _patch_ledger_row(
    writer: AttioWriter,
    *,
    record_id: str,
    consumed: Decimal,
    cap_remaining: Decimal,
    cost_usd_actual: Decimal,
) -> None:
    """Update an existing llm_budget_ledger row via AttioWriter — the §3.15
    registry authorizes ``workflows.llm_budget.LLMBudgetLedger.try_reserve``
    for the three currency fields."""
    from clients.attio_writer import WriteIntent

    writer.apply(WriteIntent(
        object=LLM_BUDGET_LEDGER_OBJECT,
        record_id=record_id,
        updates={
            # 4-dp quantization mirrors _create_ledger_row — Attio currency
            # attrs 400 on more precision.
            "consumed": str(_quantize_spend(consumed)),
            "cap_remaining_this_week": str(_quantize_remaining(cap_remaining)),
            # cost_usd_actual is per-call; we record THIS call's cost here
            # so the row's aggregate semantics differ from `consumed`. In
            # practice operators look at `consumed` for the running total
            # and `cost_usd_actual` for spot-checks.
            "cost_usd_actual": str(_quantize_spend(cost_usd_actual)),
        },
        writer_module=WRITER_MODULE,
    ))


# ============================================================
# Holdout seed initialization
# ============================================================


def ensure_holdout_seed_placeholder() -> Path:
    """Create an EMPTY ``content/holdout/defensive_holdout_v1.jsonl`` if it
    doesn't exist. Idempotent — safe to call from tests or bootstrap.

    Returns the path. The file is empty (zero lines) until operator curation;
    callers should run ``check_holdout_seed`` to escalate the gap.
    """
    HOLDOUT_SEED_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not HOLDOUT_SEED_PATH.exists():
        HOLDOUT_SEED_PATH.write_text("", encoding="utf-8")
    return HOLDOUT_SEED_PATH


# Auto-create the placeholder on module import. Idempotent and safe in
# tests (the bare file is empty; tests can override the path via the
# holdout_path kwarg on check_holdout_seed).
ensure_holdout_seed_placeholder()


# Re-export so workflows.llm_dispatch can `from workflows.llm_budget import
# LLMBudgetLedger, CostCeilingExhausted` without duplicating the class.
__all__ = [
    "STEP_WEEKLY_CAP_USD",
    "CostCeilingExhausted",
    "HOLDOUT_SEED_PATH",
    "LLMBudgetLedger",
    "LLMBudgetLedgerNotInitialized",
    "WRITER_MODULE",
    "check_holdout_seed",
    "ensure_holdout_seed_placeholder",
]


# json import is exported so callers (and the auto-created seed) can rely
# on a stable shape. Suppress F401 since the import is intentional.
_ = json  # noqa: F841 — preserve for future jsonl writers
