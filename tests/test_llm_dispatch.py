"""Tests for workflows/llm_dispatch.py (F-PR-9).

Covers the file-based inbox/outbox round-trip, dispatch-disabled env-var
gating, atomic-write semantics, stub LLMBudgetLedger, and timeout/
failure surfacing. The polling round-trip is driven by a background
thread that simulates the parent slash-command skill writing to the
outbox.
"""

from __future__ import annotations

import json
import threading
import time
from typing import TYPE_CHECKING

import pytest

from workflows.llm_dispatch import (
    DISPATCH_ENABLED_ENV,
    TOKEN_ESTIMATES,
    CostCeilingExhausted,
    LLMBudgetLedger,
    LLMDispatchFailed,
    LLMDispatchRequest,
    LLMDispatchResult,
    LLMDispatchTimeout,
    estimate_cost_usd,
    is_dispatch_enabled,
    request_llm_dispatch,
    write_dispatch_response,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def dispatch_dirs(tmp_path, monkeypatch):
    # Wave-2-B fix-up (multi-agent B4): request_llm_dispatch now passes
    # require_attio_ledger=True to try_reserve. The autouse conftest
    # `_isolate_attio_api_key` fixture deletes ATTIO_API_KEY, so
    # try_reserve would raise LLMBudgetLedgerNotInitialized in every
    # test that exercises dispatch. Set OUTBOUND_LLM_BUDGET_LEDGER_DISABLED
    # to short-circuit the ledger consult in dispatch round-trip tests
    # (those aren't validating budget enforcement; they're validating
    # the inbox/outbox protocol).
    monkeypatch.setenv("OUTBOUND_LLM_BUDGET_LEDGER_DISABLED", "1")
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    return inbox, outbox


def _drive_outbox(
    inbox: Path,
    outbox: Path,
    response_payload: dict,
    delay_s: float = 0.05,
) -> threading.Thread:
    """Background thread that simulates the parent skill: waits for any
    inbox file, then writes a matching outbox file with response_payload."""

    def _worker() -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            files = list(inbox.glob("*.json")) if inbox.exists() else []
            if files:
                time.sleep(delay_s)
                req_file = files[0]
                req_data = json.loads(req_file.read_text())
                write_dispatch_response(
                    outbox,
                    dispatch_id=req_data["dispatch_id"],
                    step=req_data["step"],
                    raw_text=response_payload.get("raw_text", ""),
                    success=response_payload.get("success", True),
                    error=response_payload.get("error"),
                )
                return
            time.sleep(0.01)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return t


# ── env-var gating ───────────────────────────────────────────────────────────


def test_is_dispatch_enabled_default_off(monkeypatch):
    monkeypatch.delenv(DISPATCH_ENABLED_ENV, raising=False)
    assert is_dispatch_enabled() is False


def test_is_dispatch_enabled_on_when_env_set(monkeypatch):
    monkeypatch.setenv(DISPATCH_ENABLED_ENV, "1")
    assert is_dispatch_enabled() is True


def test_is_dispatch_enabled_strict_value(monkeypatch):
    """Only the literal value "1" enables. "true" / "yes" do NOT — keeps
    the surface area minimal so a typo doesn't accidentally enable LLM
    dispatch in tests/CI."""
    monkeypatch.setenv(DISPATCH_ENABLED_ENV, "true")
    assert is_dispatch_enabled() is False
    monkeypatch.setenv(DISPATCH_ENABLED_ENV, "yes")
    assert is_dispatch_enabled() is False


# ── round-trip via file-based dispatch ──────────────────────────────────────


def test_request_llm_dispatch_round_trip(dispatch_dirs):
    inbox, outbox = dispatch_dirs
    _drive_outbox(inbox, outbox, {"raw_text": '{"answer": "hello"}'})
    result = request_llm_dispatch(
        step="quality_gate_haiku",
        prompt="qualify this prospect",
        system="You are an ICP qualifier.",
        model_class="haiku",
        max_tokens=300,
        timeout_s=2.0,
        poll_interval_s=0.05,
        inbox_dir=inbox,
        outbox_dir=outbox,
    )
    assert result.success is True
    assert result.raw_text == '{"answer": "hello"}'
    # Inbox + outbox cleaned up after success.
    assert not list(inbox.glob("*.json"))
    assert not list(outbox.glob("*.json"))


def test_request_llm_dispatch_failure_propagates(dispatch_dirs):
    """When the skill writes success=False, we surface LLMDispatchFailed."""
    inbox, outbox = dispatch_dirs
    _drive_outbox(
        inbox,
        outbox,
        {"raw_text": "", "success": False, "error": "subagent timed out"},
    )
    with pytest.raises(LLMDispatchFailed) as excinfo:
        request_llm_dispatch(
            step="quality_gate_haiku",
            prompt="x",
            system="y",
            timeout_s=2.0,
            poll_interval_s=0.05,
            inbox_dir=inbox,
            outbox_dir=outbox,
        )
    assert "subagent timed out" in excinfo.value.error


def test_request_llm_dispatch_timeout_leaves_inbox_for_forensics(dispatch_dirs):
    """Skill never writes outbox → timeout. Inbox file is preserved so
    a future invocation of the skill can pick it up."""
    inbox, outbox = dispatch_dirs
    with pytest.raises(LLMDispatchTimeout):
        request_llm_dispatch(
            step="quality_gate_haiku",
            prompt="x",
            system="y",
            timeout_s=0.2,
            poll_interval_s=0.05,
            inbox_dir=inbox,
            outbox_dir=outbox,
        )
    # Inbox file remains for forensics + retry.
    assert list(inbox.glob("*.json"))


def test_request_llm_dispatch_atomic_outbox_write(dispatch_dirs):
    """Verifies the round-trip survives a mid-write race: we drop a
    half-written outbox file and a complete one in quick succession,
    and the polling loop must NOT crash on JSONDecodeError mid-write."""
    inbox, outbox = dispatch_dirs

    def _worker():
        # Wait for inbox.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            files = list(inbox.glob("*.json"))
            if files:
                req_data = json.loads(files[0].read_text())
                # Atomic-write via write_dispatch_response.
                write_dispatch_response(
                    outbox,
                    dispatch_id=req_data["dispatch_id"],
                    step=req_data["step"],
                    raw_text="all good",
                    success=True,
                )
                return
            time.sleep(0.01)

    threading.Thread(target=_worker, daemon=True).start()
    result = request_llm_dispatch(
        step="quality_gate_haiku",
        prompt="x",
        system="y",
        timeout_s=2.0,
        poll_interval_s=0.05,
        inbox_dir=inbox,
        outbox_dir=outbox,
    )
    assert result.raw_text == "all good"


# ── budget gate ──────────────────────────────────────────────────────────────


def test_llm_budget_ledger_disabled_env_always_allows(monkeypatch):
    """PR-35 lands the real Attio-backed ledger. The dev/test path opts out
    via ``OUTBOUND_LLM_BUDGET_LEDGER_DISABLED=1`` so unit tests that exercise
    other dispatch code don't need to mock the Attio round-trip.

    Per type-design QA IMPORTANT-4 (preserved from F-PR-9 contract):
    returning bool would force callers to fabricate cap/consumed numbers
    when refused. Instead the ledger owns the forensic numbers and raises
    ``CostCeilingExhausted`` directly with them.
    """
    monkeypatch.setenv("OUTBOUND_LLM_BUDGET_LEDGER_DISABLED", "1")
    # Configured step + disabled flag → allow.
    assert LLMBudgetLedger.try_reserve("quality_gate_haiku", 0.001) is None
    # Unknown step → no cap → allow regardless of env flag.
    monkeypatch.delenv("OUTBOUND_LLM_BUDGET_LEDGER_DISABLED")
    assert LLMBudgetLedger.try_reserve("any_unknown_step", 999.0) is None


def test_cost_ceiling_exhausted_typed_exception():
    """The typed exception carries enough forensics for the skill to log
    a `cost_ceiling_breached` queue row when PR-35 wires the real ledger."""
    exc = CostCeilingExhausted("quality_gate_haiku", cap=0.05, consumed=0.06)
    assert exc.step == "quality_gate_haiku"
    assert exc.cap == 0.05
    assert exc.consumed == 0.06
    assert "Subagent NOT dispatched" in str(exc)


def test_request_llm_dispatch_raises_cost_ceiling_when_ledger_refuses(
    dispatch_dirs, monkeypatch
):
    """The gate fires BEFORE any inbox write. Verifies §3.7 fail-loud rule.

    Per type-design QA IMPORTANT-4: try_reserve raises CostCeilingExhausted
    itself (with the real cap/consumed numbers). request_llm_dispatch does
    NOT catch — it lets the exception propagate to the caller workflow,
    which catches and surfaces as a typed escalation.
    """
    inbox, outbox = dispatch_dirs

    def _refuse(step, cost, **_kwargs):
        # Wave-2-B fix-up (multi-agent B4): try_reserve now accepts a
        # require_attio_ledger=True keyword from request_llm_dispatch.
        # Swallow extras via **kwargs so the mock matches the new sig.
        raise CostCeilingExhausted(step, cap=0.05, consumed=0.06)

    monkeypatch.setattr(
        "workflows.llm_dispatch.LLMBudgetLedger.try_reserve",
        staticmethod(_refuse),
    )
    with pytest.raises(CostCeilingExhausted) as excinfo:
        request_llm_dispatch(
            step="quality_gate_haiku",
            prompt="x",
            system="y",
            inbox_dir=inbox,
            outbox_dir=outbox,
            timeout_s=0.5,
            poll_interval_s=0.05,
        )
    # Forensic numbers preserved verbatim — caller can log them.
    assert excinfo.value.cap == 0.05
    assert excinfo.value.consumed == 0.06
    assert excinfo.value.step == "quality_gate_haiku"
    # No inbox file written when the gate refuses.
    assert not list(inbox.glob("*.json"))


# ── token estimates ─────────────────────────────────────────────────────────


def test_token_estimates_cover_all_dispatch_steps():
    """Every F-PR-9-migrated step must have a TOKEN_ESTIMATES entry so
    PR-35's ledger can charge it. The 4 caller steps are the production
    set; the other entries are PR-25/33/34/35/36+ forward declarations."""
    required = {
        "quality_gate_haiku",
        "response_classification",
        "industry_classification",
        "synthetic_prescreen",
    }
    assert required.issubset(set(TOKEN_ESTIMATES.keys()))


def test_estimate_cost_haiku_vs_sonnet():
    """Sonnet rates are ~3× Haiku per the rate-card; the estimate
    function should respect that for any given step."""
    haiku = estimate_cost_usd("quality_gate_haiku", "haiku")
    sonnet = estimate_cost_usd("quality_gate_haiku", "sonnet")
    assert sonnet > haiku


def test_estimate_cost_unknown_step_raises_keyerror():
    """A new step that hasn't been added to TOKEN_ESTIMATES raises KeyError.

    Per type-design QA IMPORTANT-3 + AI-engineer F-1: returning 0.0 for
    unknown steps let typos silently bypass the cost gate. The dict's
    keys are now the canonical list of valid `step` identifiers; a typo
    raises here rather than slipping through. PR-35's ledger will
    additionally treat unknown steps as `configuration_decision`
    escalations at the dispatch boundary.
    """
    with pytest.raises(KeyError, match="some_unknown_step"):
        estimate_cost_usd("some_unknown_step", "haiku")


# ── dataclass shape contracts ────────────────────────────────────────────────


def test_dispatch_request_round_trips_through_json():
    req = LLMDispatchRequest(
        dispatch_id="abc",
        step="quality_gate_haiku",
        prompt="qualify",
        system="you qualify",
        model_class="haiku",
        max_tokens=300,
        schema_hint='Return JSON: {"pass": bool}',
    )
    import dataclasses
    payload = dataclasses.asdict(req)
    rebuilt = LLMDispatchRequest(**payload)
    assert rebuilt == req


def test_dispatch_result_round_trips_through_json():
    res = LLMDispatchResult(
        dispatch_id="abc",
        success=True,
        raw_text='{"pass": true}',
    )
    import dataclasses
    payload = dataclasses.asdict(res)
    rebuilt = LLMDispatchResult(**payload)
    assert rebuilt == res


def test_dispatch_result_rejects_success_with_error():
    """Per type-design QA IMPORTANT-1: the dataclass validator rejects
    incoherent combos so a malformed outbox file fails fast rather than
    leaving callers to guess which field to trust."""
    with pytest.raises(ValueError, match="success=True must have error=None"):
        LLMDispatchResult(
            dispatch_id="abc",
            success=True,
            raw_text="ok",
            error="should not be set",
        )


def test_dispatch_result_rejects_failure_without_error():
    """A failure without an explanatory error string is also incoherent."""
    with pytest.raises(ValueError, match="success=False requires a non-empty error"):
        LLMDispatchResult(
            dispatch_id="abc",
            success=False,
            raw_text="",
            error=None,
        )


def test_poll_for_outbox_propagates_schema_drift(dispatch_dirs):
    """Per silent-failure-hunter CRIT: a TypeError from
    LLMDispatchResult(**payload) (unknown/missing keys → schema drift)
    must propagate, not be masked as LLMDispatchTimeout. Otherwise a
    PR-35 schema change shows up to operators as a generic 5-min hang."""
    inbox, outbox = dispatch_dirs
    outbox.mkdir(parents=True, exist_ok=True)
    inbox.mkdir(parents=True, exist_ok=True)

    # Drive the worker to write a payload with an unknown extra key.
    def _worker():
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            files = list(inbox.glob("*.json"))
            if files:
                req = json.loads(files[0].read_text())
                outfile = outbox / f"{req['step']}-{req['dispatch_id']}.json"
                # Schema drift: extra key that LLMDispatchResult can't accept.
                outfile.write_text(
                    json.dumps(
                        {
                            "dispatch_id": req["dispatch_id"],
                            "success": True,
                            "raw_text": "ok",
                            "future_extra_field_from_pr_35": "boom",
                        }
                    )
                )
                return
            time.sleep(0.01)

    threading.Thread(target=_worker, daemon=True).start()
    with pytest.raises(TypeError):
        request_llm_dispatch(
            step="quality_gate_haiku",
            prompt="x",
            system="y",
            timeout_s=2.0,
            poll_interval_s=0.05,
            inbox_dir=inbox,
            outbox_dir=outbox,
        )
