"""PR-35 tests: Attio-backed LLMBudgetLedger + holdout seed check."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import httpx
import pytest

from workflows.llm_budget import (
    HOLDOUT_SEED_PATH,
    LLM_BUDGET_LEDGER_OBJECT,
    STEP_WEEKLY_CAP_USD,
    WRITER_MODULE,
    CostCeilingExhausted,
    LLMBudgetLedger,
    _monday_of,
    check_holdout_seed,
    ensure_holdout_seed_placeholder,
)

# ────────────────────────────────────────────────────────────────────────
# week math
# ────────────────────────────────────────────────────────────────────────


class TestMondayOf:
    @pytest.mark.parametrize("input_date,expected", [
        (date(2026, 5, 18), date(2026, 5, 18)),  # Monday → same
        (date(2026, 5, 19), date(2026, 5, 18)),  # Tuesday → previous Monday
        (date(2026, 5, 24), date(2026, 5, 18)),  # Sunday → Monday of same week
        (date(2026, 5, 25), date(2026, 5, 25)),  # Next Monday → itself
    ])
    def test_returns_monday(self, input_date, expected):
        assert _monday_of(input_date) == expected


# ────────────────────────────────────────────────────────────────────────
# LLMBudgetLedger
# ────────────────────────────────────────────────────────────────────────


@pytest.fixture
def attio_no_existing_row():
    """Attio returns no existing ledger row + a CREATE returns a new id."""
    attio = MagicMock()
    attio._request.side_effect = [
        {"data": []},  # find query
        {"data": {"id": {"record_id": "ledger-1"}}},  # create
    ]
    writer = MagicMock()
    return attio, writer


@pytest.fixture
def attio_with_existing_row():
    """Builder for an Attio mock that returns a ledger row with the given
    consumed amount."""
    def _build(consumed_value: str) -> tuple[MagicMock, MagicMock]:
        attio = MagicMock()
        attio._request.return_value = {
            "data": [{
                "id": {"record_id": "ledger-1"},
                "values": {
                    "step": [{"value": "industry_classification"}],
                    "consumed": [{"currency_value": consumed_value}],
                },
            }]
        }
        writer = MagicMock()
        return attio, writer
    return _build


class TestTryReserveUnknownStep:
    def test_unknown_step_passes_through(self):
        """Unknown steps have no configured cap → no enforcement → return None.

        Matches the F-PR-9 stub behavior. New step surfaces are expected to
        add an entry to STEP_WEEKLY_CAP_USD via PR; the unknown-step branch
        is a permissive default, not a silent-bypass (configured-cap steps
        ARE enforced)."""
        # No Attio call — unknown step short-circuits.
        attio = MagicMock()
        result = LLMBudgetLedger.try_reserve(
            "any_unknown_step", 999.0, attio=attio,
        )
        assert result is None
        attio._request.assert_not_called()


class TestTryReserveFreshRow:
    def test_creates_row_when_under_cap(self, attio_no_existing_row):
        attio, writer = attio_no_existing_row
        # industry_classification cap is $2.00.
        result = LLMBudgetLedger.try_reserve(
            "industry_classification",
            0.50,
            attio=attio,
            writer=writer,
            today=date(2026, 5, 18),  # Monday
        )
        assert result is None
        # Query + create = 2 _request calls; no AttioWriter PATCH on fresh row.
        assert attio._request.call_count == 2
        create_body = attio._request.call_args_list[1].kwargs["json"]
        assert create_body["data"]["values"]["step"] == "industry_classification"
        assert create_body["data"]["values"]["week_starting"] == "2026-05-18"
        # Decimal str-formatting preserves the input precision — Decimal("0.50") → "0.50".
        assert Decimal(create_body["data"]["values"]["consumed"]) == Decimal("0.50")

    def test_refuses_when_first_call_exceeds_cap(self, attio_no_existing_row):
        attio, writer = attio_no_existing_row
        # industry_classification cap $2.00; reserving $3 should refuse.
        with pytest.raises(CostCeilingExhausted) as exc_info:
            LLMBudgetLedger.try_reserve(
                "industry_classification",
                3.00,
                attio=attio,
                writer=writer,
                today=date(2026, 5, 18),
            )
        assert exc_info.value.step == "industry_classification"
        assert exc_info.value.cap == Decimal("2.00")
        assert exc_info.value.consumed == Decimal("0")
        # Query happened but NO create after the cap check.
        assert attio._request.call_count == 1


class TestTryReserveExistingRow:
    def test_patches_when_under_cap(self, attio_with_existing_row):
        attio, writer = attio_with_existing_row("0.80")
        # industry_classification cap $2.00, consumed $0.80; reserve $0.40 ok.
        result = LLMBudgetLedger.try_reserve(
            "industry_classification",
            0.40,
            attio=attio,
            writer=writer,
            today=date(2026, 5, 18),
        )
        assert result is None
        writer.apply.assert_called_once()
        intent = writer.apply.call_args[0][0]
        assert intent.object == LLM_BUDGET_LEDGER_OBJECT
        assert intent.record_id == "ledger-1"
        # Decimal-equal comparison (str repr drops trailing zeros: "1.20" → "1.2").
        assert Decimal(intent.updates["consumed"]) == Decimal("1.20")
        assert Decimal(intent.updates["cap_remaining_this_week"]) == Decimal("0.80")
        assert Decimal(intent.updates["cost_usd_actual"]) == Decimal("0.40")
        assert intent.writer_module == WRITER_MODULE

    def test_refuses_when_increment_exceeds_cap(self, attio_with_existing_row):
        attio, writer = attio_with_existing_row("1.90")
        # cap $2.00, consumed $1.90; reserve $0.20 → would push to $2.10 → refuse.
        with pytest.raises(CostCeilingExhausted) as exc_info:
            LLMBudgetLedger.try_reserve(
                "industry_classification",
                0.20,
                attio=attio,
                writer=writer,
                today=date(2026, 5, 18),
            )
        assert exc_info.value.cap == Decimal("2.00")
        assert exc_info.value.consumed == Decimal("1.90")
        writer.apply.assert_not_called()


class TestCurrencyPrecisionFourDecimalPlaces:
    """PR-216 production regression: Attio currency attributes accept at
    most 4 decimal places. `quality_gate_haiku`'s estimated cost is $0.00065
    (5 dp), so every create/patch 400'd and the ledger never wrote a row —
    weekly dispatches degraded to `borderline_llm_error`.

    Contract: every currency value serialized to the CRM (`consumed`,
    `cap_remaining_this_week`, `cost_usd_actual`) has ≤4 decimal places and
    plain (non-scientific) notation. Rounding is pessimistic for the budget
    gate: spend rounds UP, remaining budget rounds DOWN.
    """

    CURRENCY_SLUGS = ("consumed", "cap_remaining_this_week", "cost_usd_actual")

    @staticmethod
    def _assert_attio_safe(raw: str) -> Decimal:
        value = Decimal(raw)
        exponent = value.as_tuple().exponent
        assert exponent >= -4, f"{raw!r} has more than 4 decimal places"
        assert "e" not in raw.lower(), f"{raw!r} uses scientific notation"
        return value

    def test_create_quantizes_production_cost(self, attio_no_existing_row):
        """Exact production repro: quality_gate_haiku estimate = $0.00065."""
        attio, writer = attio_no_existing_row
        LLMBudgetLedger.try_reserve(
            "quality_gate_haiku",
            0.00065,
            attio=attio,
            writer=writer,
            today=date(2026, 6, 29),  # Monday of the failing run week
        )
        create_values = attio._request.call_args_list[1].kwargs["json"]["data"]["values"]
        for slug in self.CURRENCY_SLUGS:
            self._assert_attio_safe(create_values[slug])
        # Spend rounds UP: 0.00065 → 0.0007 (never under-counts against cap).
        assert Decimal(create_values["consumed"]) == Decimal("0.0007")
        assert Decimal(create_values["cost_usd_actual"]) == Decimal("0.0007")
        # Remaining rounds DOWN: 5.00 - 0.0007 = 4.9993 (never overstates).
        assert Decimal(create_values["cap_remaining_this_week"]) == Decimal("4.9993")

    def test_patch_quantizes_production_cost(self, attio_with_existing_row):
        attio, writer = attio_with_existing_row("0.0007")
        LLMBudgetLedger.try_reserve(
            "quality_gate_haiku",
            0.00065,
            attio=attio,
            writer=writer,
            today=date(2026, 6, 29),
        )
        writer.apply.assert_called_once()
        updates = writer.apply.call_args[0][0].updates
        for slug in self.CURRENCY_SLUGS:
            self._assert_attio_safe(updates[slug])
        # Stored consumed (0.0007) + quantized cost (0.0007) = 0.0014.
        assert Decimal(updates["consumed"]) == Decimal("0.0014")
        assert Decimal(updates["cap_remaining_this_week"]) == Decimal("4.9986")
        assert Decimal(updates["cost_usd_actual"]) == Decimal("0.0007")

    def test_tiny_estimate_never_scientific_notation(self, attio_no_existing_row):
        """An adversarially tiny estimate must not serialize as '6.5E-7'."""
        attio, writer = attio_no_existing_row
        LLMBudgetLedger.try_reserve(
            "quality_gate_haiku",
            0.00000065,
            attio=attio,
            writer=writer,
            today=date(2026, 6, 29),
        )
        create_values = attio._request.call_args_list[1].kwargs["json"]["data"]["values"]
        for slug in self.CURRENCY_SLUGS:
            self._assert_attio_safe(create_values[slug])
        # ROUND_UP floors positive spend at the smallest representable unit.
        assert Decimal(create_values["consumed"]) == Decimal("0.0001")

    def test_cap_check_counts_quantized_cost(self, attio_with_existing_row):
        """The cap comparison must use the same quantized cost that gets
        persisted — otherwise consumed drifts from what the check saw."""
        attio, writer = attio_with_existing_row("4.9994")
        # 4.9994 + quantize_up(0.00065)=0.0007 → 5.0001 > 5.00 cap → refuse.
        with pytest.raises(CostCeilingExhausted) as exc_info:
            LLMBudgetLedger.try_reserve(
                "quality_gate_haiku",
                0.00065,
                attio=attio,
                writer=writer,
                today=date(2026, 6, 29),
            )
        assert exc_info.value.consumed == Decimal("4.9994")
        writer.apply.assert_not_called()


class TestLedgerUnavailable:
    """Ledger infra I/O failures must surface as the typed
    `LLMBudgetLedgerUnavailable` (a subclass of `LLMDispatchFailed`) so:
      - every dispatch caller that already degrades on LLMDispatchFailed
        degrades gracefully instead of crashing on a raw httpx error, and
      - the quality gate can distinguish "ledger down" (fail open to the
        agent staging path) from "cap exhausted" (fail closed).
    PR-216 incident: raw HTTPStatusError leaked into the generic
    LLM-failure bucket and borderlines were rejected with no artifact.
    """

    def _http_error(self) -> httpx.HTTPStatusError:
        req = httpx.Request("POST", "https://api.attio.com/v2/objects/llm_budget_ledger/records")
        return httpx.HTTPStatusError(
            "Client error '400 Bad Request'", request=req,
            response=httpx.Response(400, request=req),
        )

    def test_query_error_raises_typed_unavailable(self):
        from workflows.llm_dispatch import LLMBudgetLedgerUnavailable, LLMDispatchFailed
        attio = MagicMock()
        attio._request.side_effect = self._http_error()
        with pytest.raises(LLMBudgetLedgerUnavailable) as exc_info:
            LLMBudgetLedger.try_reserve(
                "quality_gate_haiku", 0.00065,
                attio=attio, writer=MagicMock(), today=date(2026, 6, 29),
            )
        assert isinstance(exc_info.value, LLMDispatchFailed)
        assert exc_info.value.step == "quality_gate_haiku"

    def test_create_error_raises_typed_unavailable(self):
        from workflows.llm_dispatch import LLMBudgetLedgerUnavailable
        attio = MagicMock()
        attio._request.side_effect = [
            {"data": []},        # find query succeeds, no row
            self._http_error(),  # create 400s
        ]
        with pytest.raises(LLMBudgetLedgerUnavailable):
            LLMBudgetLedger.try_reserve(
                "quality_gate_haiku", 0.00065,
                attio=attio, writer=MagicMock(), today=date(2026, 6, 29),
            )

    def test_patch_error_raises_typed_unavailable(self, attio_with_existing_row):
        from clients.attio_writer import AttioWriteFailed
        from workflows.llm_dispatch import LLMBudgetLedgerUnavailable
        attio, writer = attio_with_existing_row("0.0007")
        writer.apply.side_effect = AttioWriteFailed("max_attempts")
        with pytest.raises(LLMBudgetLedgerUnavailable):
            LLMBudgetLedger.try_reserve(
                "quality_gate_haiku", 0.00065,
                attio=attio, writer=writer, today=date(2026, 6, 29),
            )

    def test_cap_exhausted_is_not_wrapped(self, attio_with_existing_row):
        """CostCeilingExhausted must keep its type — it is a fail-closed
        budget verdict, not an infra failure."""
        attio, writer = attio_with_existing_row("4.9999")
        with pytest.raises(CostCeilingExhausted):
            LLMBudgetLedger.try_reserve(
                "quality_gate_haiku", 0.00065,
                attio=attio, writer=writer, today=date(2026, 6, 29),
            )


class TestTryReserveDisabledEnv:
    def test_disabled_short_circuits(self, monkeypatch):
        monkeypatch.setenv("OUTBOUND_LLM_BUDGET_LEDGER_DISABLED", "1")
        attio = MagicMock()
        result = LLMBudgetLedger.try_reserve(
            "industry_classification", 100.0, attio=attio,
        )
        assert result is None
        attio._request.assert_not_called()


class TestAttioMissingFailsSoft:
    def test_no_attio_api_key_logs_warning_and_allows_when_dispatch_disabled(
        self, monkeypatch, capsys,
    ):
        """Dev/test env (no OUTBOUND_USE_LLM_DISPATCH): ledger logs a stderr
        warning and falls back to allow so the autouse conftest fixture
        doesn't crash every test that touches an LLM-dispatching path."""
        monkeypatch.delenv("ATTIO_API_KEY", raising=False)
        monkeypatch.delenv("OUTBOUND_USE_LLM_DISPATCH", raising=False)
        result = LLMBudgetLedger.try_reserve(
            "industry_classification", 0.50,
        )
        assert result is None  # fail-soft allow
        captured = capsys.readouterr()
        assert "ATTIO_API_KEY unset" in captured.err
        assert "industry_classification" in captured.err

    def test_no_attio_api_key_raises_when_dispatch_enabled(self, monkeypatch):
        """Production dispatch env (OUTBOUND_USE_LLM_DISPATCH=1) without
        ATTIO_API_KEY MUST refuse to dispatch — silent fail-soft would
        bypass §3.7 cap enforcement and the warning's only surface is
        stderr (cron typically routes to /dev/null). Wave-2 fix for
        adversarial-pass1-round2 IMPORTANT-2."""
        from workflows.llm_budget import LLMBudgetLedgerNotInitialized
        monkeypatch.delenv("ATTIO_API_KEY", raising=False)
        monkeypatch.setenv("OUTBOUND_USE_LLM_DISPATCH", "1")
        with pytest.raises(LLMBudgetLedgerNotInitialized) as exc_info:
            LLMBudgetLedger.try_reserve(
                "industry_classification", 0.50,
            )
        assert exc_info.value.step == "industry_classification"
        assert "ATTIO_API_KEY" in exc_info.value.reason
        # Wave-2-B fix-up (multi-agent B4): error message moved from
        # naming the env var literally to naming the two gate signals
        # explicitly. Validate the new shape.
        assert "is_dispatch_enabled=True" in exc_info.value.reason


class TestYamlSelectOptionsMatchCapDict:
    """Fold-in BLOCKING-1 (schema-writer QA): the yaml select options for
    llm_budget_ledger.step MUST include every step in STEP_WEEKLY_CAP_USD,
    otherwise Attio's select validation rejects CREATE with unknown values
    (silent prod failure)."""

    def test_yaml_options_cover_all_configured_steps(self):
        from pathlib import Path

        import yaml as _yaml

        manifest_path = Path(__file__).resolve().parent.parent / "docs" / "attio_schema_deltas.yaml"
        manifest = _yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        step_entry = next(
            attr for attr in manifest["attributes"]
            if attr["object"] == "llm_budget_ledger" and attr["slug"] == "step"
        )
        yaml_options = set(step_entry["options"])
        cap_dict_steps = set(STEP_WEEKLY_CAP_USD.keys())
        missing = cap_dict_steps - yaml_options
        assert not missing, (
            f"yaml step options missing {missing!r} — Attio will reject these "
            "with select-validation error on CREATE."
        )


class TestStepWeeklyCapUsdConfig:
    def test_known_steps_have_caps(self):
        # Sample of LLM steps that ship in production. Each MUST have a
        # configured cap so cap enforcement actually fires for them.
        for step in (
            "industry_classification", "borderline_verdict",
            "synthetic_prescreen", "weekly_brain_critique",
            "company_hq_classifier", "diagnostic_critique",
        ):
            assert step in STEP_WEEKLY_CAP_USD, (
                f"{step!r} has no STEP_WEEKLY_CAP_USD entry — cost gate disabled"
            )

    def test_caps_are_decimal(self):
        for step, cap in STEP_WEEKLY_CAP_USD.items():
            assert isinstance(cap, Decimal), (
                f"{step!r} cap is {type(cap).__name__}, must be Decimal"
            )
            assert cap > 0


# ────────────────────────────────────────────────────────────────────────
# CostCeilingExhausted
# ────────────────────────────────────────────────────────────────────────


class TestCostCeilingExhausted:
    def test_carries_forensics(self):
        exc = CostCeilingExhausted("foo_step", Decimal("5.00"), Decimal("4.90"))
        assert exc.step == "foo_step"
        assert exc.cap == Decimal("5.00")
        assert exc.consumed == Decimal("4.90")
        assert "foo_step" in str(exc)

    def test_importable_from_llm_dispatch(self):
        """F-PR-9 contract: existing callers `from workflows.llm_dispatch
        import CostCeilingExhausted` must keep working post-PR-35."""
        from workflows.llm_dispatch import CostCeilingExhausted as Alias
        assert Alias is CostCeilingExhausted


# ────────────────────────────────────────────────────────────────────────
# Holdout seed
# ────────────────────────────────────────────────────────────────────────


class TestCheckHoldoutSeed:
    def test_escalates_on_empty_file(self, tmp_path):
        # An empty .jsonl exists — should escalate.
        empty_path = tmp_path / "holdout.jsonl"
        empty_path.write_text("", encoding="utf-8")
        attio = MagicMock()
        with patch("workflows.escalation.escalate") as mock_escalate:
            result = check_holdout_seed(attio=attio, holdout_path=empty_path)
        assert result["exists"] is True
        assert result["line_count"] == 0
        assert result["needs_seed"] is True
        assert result["escalated"] is True
        mock_escalate.assert_called_once()
        kwargs = mock_escalate.call_args.kwargs
        assert kwargs["type"] == "holdout_seed_required"
        assert kwargs["idempotency_key"] == "defensive_holdout_v1"

    def test_escalates_on_missing_file(self, tmp_path):
        missing = tmp_path / "missing.jsonl"
        attio = MagicMock()
        with patch("workflows.escalation.escalate") as mock_escalate:
            result = check_holdout_seed(attio=attio, holdout_path=missing)
        assert result["exists"] is False
        assert result["escalated"] is True
        mock_escalate.assert_called_once()

    def test_does_not_escalate_when_populated(self, tmp_path):
        populated = tmp_path / "holdout.jsonl"
        populated.write_text(
            '{"example": "x"}\n{"example": "y"}\n', encoding="utf-8",
        )
        attio = MagicMock()
        with patch("workflows.escalation.escalate") as mock_escalate:
            result = check_holdout_seed(attio=attio, holdout_path=populated)
        assert result["line_count"] == 2
        assert result["needs_seed"] is False
        assert result["escalated"] is False
        mock_escalate.assert_not_called()

    def test_no_escalation_without_attio(self, tmp_path):
        """Calling without an attio client returns the gap diagnosis but
        doesn't escalate (caller decides). Useful for diagnostic-only paths."""
        empty = tmp_path / "holdout.jsonl"
        empty.write_text("", encoding="utf-8")
        with patch("workflows.escalation.escalate") as mock_escalate:
            result = check_holdout_seed(attio=None, holdout_path=empty)
        assert result["needs_seed"] is True
        assert result["escalated"] is False
        mock_escalate.assert_not_called()


class TestEnsureHoldoutSeedPlaceholder:
    def test_creates_empty_file_on_import(self):
        """Module-level call should have created the placeholder by now."""
        assert HOLDOUT_SEED_PATH.exists()

    def test_idempotent(self):
        # Call twice — second call must not raise or truncate.
        ensure_holdout_seed_placeholder()
        contents_before = HOLDOUT_SEED_PATH.read_text(encoding="utf-8")
        ensure_holdout_seed_placeholder()
        assert HOLDOUT_SEED_PATH.read_text(encoding="utf-8") == contents_before


# ────────────────────────────────────────────────────────────────────────
# Registry compliance
# ────────────────────────────────────────────────────────────────────────


class TestRegistryCompliance:
    EXPECTED = "workflows.llm_budget.LLMBudgetLedger.try_reserve"

    def test_constant_matches(self):
        assert WRITER_MODULE == self.EXPECTED

    @pytest.mark.parametrize("slug", [
        "step",
        "week_starting",
        "cap_remaining_this_week",
        "cost_usd_actual",
        "consumed",
    ])
    def test_writer_registered(self, slug):
        from clients.attio_writer_registry import WRITE_OWNER_REGISTRY
        assert WRITE_OWNER_REGISTRY[("llm_budget_ledger", slug)] == self.EXPECTED
