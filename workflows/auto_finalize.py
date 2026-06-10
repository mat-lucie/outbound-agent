"""Auto-finalize the borderline batch when the operator-decision window
expires.

Runs inside the operator-invoked sales-finalize-borderline skill. The
function picks an `operator_decision_run_id` (the explicit
caller-supplied value > looking it up from a resolved queue row >
the `default_expire` sentinel) and builds an idempotency key
`{batch_date}_{decision_run_id}` so re-runs collide on the queue's
uniqueness_key.

Two non-colliding key shapes per batch_date:
  * `{batch}_default_expire` — operator never resolved.
  * `{batch}_<operator-run-id>` — operator resolved before this ran.

Each lands at most once. The collision is enforced by escalate()'s
uniqueness_key mechanism on the Operator Review Queue. The
weekly_finalize_stale slug is the typed escalation that records the
finalize completion.

# Failure handling

`finalize_fn` is invoked AFTER the uniqueness_key check. If it
raises, no completion row is written and the operator can retry. If
it returns a dict carrying `exit_code != 0` (the subprocess wrapper
convention), this function raises `FinalizeRunFailed` BEFORE writing
the completion row — a poisoned idempotency key would block retry,
and silent success-on-failure violates §0 #9.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypedDict

from workflows.escalation import escalate

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date as date_type

    from clients.attio import AttioClient

OPERATOR_REVIEW_QUEUE_SLUG = "operator_review_queue"
DEFAULT_EXPIRE = "default_expire"

FinalizeAction = Literal["finalized", "skipped_idempotent"]


class AutoFinalizeResult(TypedDict):
    batch_date: str
    decision_run_id: str
    idempotency_key: str
    action: FinalizeAction
    finalize_result: dict[str, Any] | None


class FinalizeRunFailed(RuntimeError):
    """Raised when finalize_fn returns a dict whose exit_code is
    non-zero (or any other failure marker). The uniqueness_key is NOT
    written, so the operator can resolve the cause and re-run."""

    def __init__(self, batch_iso: str, decision_run_id: str, payload: dict) -> None:
        self.batch_iso = batch_iso
        self.decision_run_id = decision_run_id
        self.payload = payload
        super().__init__(
            f"finalize failed for batch={batch_iso} "
            f"decision_run_id={decision_run_id}: {payload!r}"
        )


def _find_operator_decision_run_id(
    attio: AttioClient, batch_date_iso: str,
) -> str | None:
    """Look up the latest operator-resolved queue row for the batch.

    The operator's decision_run_id is read from `decision_json` (the
    Attio long-text field carrying the resolution payload), NOT from
    `decision_source` — escalate() initializes decision_source to
    `'agent_opened'` and the resolve workflow that updates it is not
    yet built. When the resolve workflow lands it will write
    `{"decision_run_id": "<id>", ...}` to `decision_json`; this
    function parses that out. Until then this lookup returns None for
    every real row and the caller falls back to default_expire.

    Returns None when no resolved row exists OR when the row's
    decision_json doesn't carry a decision_run_id. Raises on any
    Attio read failure (no silent fallback to default_expire).

    A non-dict / non-empty value with a wrong shape raises ValueError
    so a buggy upstream resolver surfaces immediately rather than
    silently poisoning the idempotency key.
    """
    import json

    body = {
        "filter": {
            "type": {"$eq": "weekly_finalize_stale"},
            "status": {"$eq": "resolved"},
            "idempotency_key": {"$contains": batch_date_iso},
        },
        "limit": 1,
    }
    data = attio._request(
        "POST",
        f"/objects/{OPERATOR_REVIEW_QUEUE_SLUG}/records/query",
        json=body,
    )
    if "data" not in data:
        raise RuntimeError(
            f"Attio query response missing 'data' key: {data!r}"
        )
    rows = data.get("data") or []
    if not rows:
        return None
    values = rows[0].get("values", {}) or {}
    decision_json_items = values.get("decision_json") or []
    if not decision_json_items:
        return None
    item = decision_json_items[0]
    raw_value = item.get("value") if isinstance(item, dict) else item
    if not raw_value:
        return None
    try:
        payload = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"decision_json on row {rows[0].get('id')!r} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError(
            f"decision_json on row {rows[0].get('id')!r} must be an object; "
            f"got {type(payload).__name__}"
        )
    decision_run_id = payload.get("decision_run_id")
    if decision_run_id in (None, ""):
        return None
    return str(decision_run_id)


def auto_finalize_borderline_batch(
    attio: AttioClient,
    *,
    batch_date: date_type,
    finalize_fn: Callable[[str, str], dict[str, Any]],
    operator_decision_run_id: str | None = None,
) -> AutoFinalizeResult:
    """Finalize one borderline batch, idempotent within the day.

    `finalize_fn(batch_date_iso, decision_run_id) -> dict` performs
    the actual finalize. Injected for testability. Must return a dict;
    if the dict carries `exit_code != 0`, this function raises
    `FinalizeRunFailed` without writing the completion row so the
    operator can retry.

    `operator_decision_run_id` precedence: explicit caller arg >
    looked-up resolver row > DEFAULT_EXPIRE. Empty string is treated
    as "not supplied" so a stale empty-truthy caller doesn't bypass
    the lookup silently.
    """
    batch_iso = batch_date.isoformat()
    decision_run_id = (
        (operator_decision_run_id or None)
        or _find_operator_decision_run_id(attio, batch_iso)
        or DEFAULT_EXPIRE
    )
    key = f"{batch_iso}_{decision_run_id}"

    # Pre-check the escalate() uniqueness_key collision so we skip
    # finalize_fn entirely on a same-day re-run rather than doing the
    # work twice and relying on escalate() to dedup the row.
    existing = _existing_finalize_row(attio, key)
    if existing is not None:
        return AutoFinalizeResult(
            batch_date=batch_iso,
            decision_run_id=decision_run_id,
            idempotency_key=key,
            action="skipped_idempotent",
            finalize_result=None,
        )

    result = finalize_fn(batch_iso, decision_run_id)

    # The finalize_fn contract is "returns a dict". A non-dict result
    # would silently coerce in _summarize_result; raise loudly instead
    # so the operator sees the contract violation.
    if not isinstance(result, dict):
        raise TypeError(
            f"finalize_fn must return dict; got {type(result).__name__}"
        )

    # exit_code is the subprocess-wrapper convention. A non-zero
    # exit means weekly-finalize failed. Recording a completion row
    # in this state would poison the uniqueness_key and block retry.
    exit_code = result.get("exit_code", 0)
    if exit_code != 0:
        raise FinalizeRunFailed(batch_iso, decision_run_id, result)

    # `weekly_finalize_stale` is the typed escalation slug for the
    # finalize-completion record. The uniqueness_key collision on
    # `{type}|{idempotency_key}` is what makes re-runs no-ops.
    escalate(
        type="weekly_finalize_stale",
        idempotency_key=key,
        payload={
            "batch_date": batch_iso,
            "decision_run_id": decision_run_id,
            "result_summary": _summarize_result(result),
        },
        attio=attio,
    )

    return AutoFinalizeResult(
        batch_date=batch_iso,
        decision_run_id=decision_run_id,
        idempotency_key=key,
        action="finalized",
        finalize_result=result,
    )


def _existing_finalize_row(attio: AttioClient, key: str) -> dict | None:
    body = {
        "filter": {
            "type": {"$eq": "weekly_finalize_stale"},
            "uniqueness_key": {
                "$eq": f"weekly_finalize_stale|{key}",
            },
        },
        "limit": 1,
    }
    data = attio._request(
        "POST",
        f"/objects/{OPERATOR_REVIEW_QUEUE_SLUG}/records/query",
        json=body,
    )
    rows = data.get("data", []) or []
    return rows[0] if rows else None


def _summarize_result(result: dict) -> str:
    """Compress a finalize_fn result dict to a payload-safe one-liner.

    Capped at 200 chars so the operator can scan it in the Attio UI
    without scrolling. Recognizes the inner weekly-finalize counters
    (staged/passed/failed/missing/committed) AND the subprocess
    wrapper fields (exit_code/decision_run_id) so the operator sees
    structured fields rather than a raw dict repr in every run.
    """
    parts: list[str] = []
    for k in (
        "exit_code", "decision_run_id",
        "staged", "passed", "failed", "missing", "committed",
    ):
        if k in result:
            parts.append(f"{k}={result[k]}")
    if not parts:
        return str(result)[:200]
    return ", ".join(parts)[:200]
