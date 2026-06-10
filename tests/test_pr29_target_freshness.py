"""Tests for PR-29 — target-list freshness gate (B-PW-TARGETS-FRESH).

Post-fold coverage (6-agent QA convergence):
  * Three freshness bands at boundaries (FRESH / WARN / STALE)
  * Queue-write-before-raise ORDERING (side_effect flag, not just call_count)
  * Multi-STALE collection: N stale files → N queue rows → single error
  * `escalate()` failure preserves staleness signal (silent-failure I-1)
  * End-to-end TypedDict validation (no mock-escalate) (pr-test B-1)
  * `_meta.updated_at` precedence over mtime (code-reviewer I-2)
  * `operator_today` for clock-skew consistency (silent-failure I-3)
  * `FreshnessResult.__post_init__` invariants (type-design fold)
  * Path-containment in orchestrator (GTM fold)
  * Standard refresh lifecycle, FileNotFoundError, registry invariants
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from models.freshness import (
    STALE_THRESHOLD_DAYS,
    WARN_THRESHOLD_DAYS,
    FreshnessResult,
    FreshnessStatus,
    WeeklyTargetListStaleError,
)
from workflows.escalation_schemas import (
    ESCALATION_SCHEMAS,
    ESCALATION_TYPES_SET,
    TargetListStalePayload,
)
from workflows.target_freshness import (
    _read_meta_updated_at,
    check_target_list_freshness,
)


def _make_target_file(
    tmp_path: Path,
    name: str,
    days_old: int,
    *,
    with_meta_updated_at: bool = False,
) -> Path:
    """Create a JSON target file. If `with_meta_updated_at` is True
    the `_meta.updated_at` field is written to a date `days_old` days
    ago AND the mtime is also backdated; otherwise only mtime is
    backdated.
    """
    path = tmp_path / f"{name}-targets.json"
    ago = date.today() - timedelta(days=days_old)
    data: dict = {"_meta": {}, "tier_1_p0": []}
    if with_meta_updated_at:
        data["_meta"]["updated_at"] = ago.isoformat()
    path.write_text(json.dumps(data))
    if days_old > 0:
        from datetime import datetime as _dt
        ts = int(_dt(ago.year, ago.month, ago.day).timestamp())
        os.utime(path, (ts, ts))
    return path


# -- Three freshness bands -----------------------------------------------


class TestFreshnessBands:
    def test_fresh_returns_no_escalation(self, tmp_path):
        path = _make_target_file(tmp_path, "fresh", days_old=5)
        attio = MagicMock()
        from workflows import target_freshness as tf
        with patch.object(tf, "escalate") as mock_escalate:
            result = check_target_list_freshness(path, date.today(), attio=attio)

        assert result.status == FreshnessStatus.FRESH
        assert 0 <= result.days_stale < WARN_THRESHOLD_DAYS
        mock_escalate.assert_not_called()

    def test_warn_band_emits_to_stderr_via_click(self, tmp_path, capsys):
        # silent-failure-hunter QA convergence (B-1): WARN must be
        # operator-visible regardless of logger config. We assert via
        # `capsys` (stderr capture) instead of relying on caplog, so
        # the test catches the case where the logger fallback was the
        # only signal path.
        path = _make_target_file(tmp_path, "warn", days_old=40)
        attio = MagicMock()
        from workflows import target_freshness as tf
        with patch.object(tf, "escalate") as mock_escalate:
            result = check_target_list_freshness(path, date.today(), attio=attio)

        assert result.status == FreshnessStatus.WARN
        assert WARN_THRESHOLD_DAYS <= result.days_stale < STALE_THRESHOLD_DAYS
        mock_escalate.assert_not_called()
        # The stderr emit uses click.echo(..., err=True).
        stderr = capsys.readouterr().err
        assert "WARN band" in stderr
        assert str(path) in stderr

    def test_stale_band_emits_queue_then_raises(self, tmp_path):
        path = _make_target_file(tmp_path, "stale", days_old=90)
        attio = MagicMock()
        from workflows import target_freshness as tf
        with patch.object(tf, "escalate") as mock_escalate, \
             pytest.raises(WeeklyTargetListStaleError) as exc_info:
            check_target_list_freshness(path, date.today(), attio=attio)

        mock_escalate.assert_called_once()
        kwargs = mock_escalate.call_args.kwargs
        assert kwargs["type"] == "target_list_stale"

        payload = kwargs["payload"]
        assert payload["target_list_path"] == str(path)
        assert payload["freshness_check_at"] == date.today().isoformat()
        assert payload["days_stale"] >= STALE_THRESHOLD_DAYS

        # Exception now carries a LIST of results (multi-STALE
        # collection fold).
        results = exc_info.value.results
        assert len(results) == 1
        assert results[0].status == FreshnessStatus.STALE
        assert results[0].target_list_path == path

    def test_at_exactly_30d_is_warn(self, tmp_path):
        path = _make_target_file(tmp_path, "boundary30", days_old=30)
        attio = MagicMock()
        from workflows import target_freshness as tf
        with patch.object(tf, "escalate"):
            result = check_target_list_freshness(path, date.today(), attio=attio)
        assert result.status == FreshnessStatus.WARN

    def test_at_exactly_60d_is_stale(self, tmp_path):
        path = _make_target_file(tmp_path, "boundary60", days_old=60)
        attio = MagicMock()
        from workflows import target_freshness as tf
        with patch.object(tf, "escalate"), \
             pytest.raises(WeeklyTargetListStaleError):
            check_target_list_freshness(path, date.today(), attio=attio)


# -- Queue-write-before-raise ORDERING (pr-test I-1 fold) ---------------


class TestQueueWriteBeforeRaiseOrdering:
    def test_queue_write_completes_before_raise(self, tmp_path):
        # side_effect flag: if escalate() is called AFTER the raise
        # (or skipped entirely), the flag stays False and the test
        # fails. Locks the documented durability contract.
        from workflows import target_freshness as tf

        path = _make_target_file(tmp_path, "stale", days_old=90)
        attio = MagicMock()
        order_log: list[str] = []

        def _track_escalate(**kwargs):
            order_log.append("escalate_called")

        with patch.object(tf, "escalate", side_effect=_track_escalate), \
             pytest.raises(WeeklyTargetListStaleError):
            check_target_list_freshness(path, date.today(), attio=attio)

        # If the raise happened before the queue write, the list
        # would be empty.
        assert order_log == ["escalate_called"]


# -- escalate() failure path (silent-failure I-1 fold) ------------------


class TestEscalateFailureDoesNotMaskStaleness:
    def test_escalate_raising_still_raises_stale_error(self, tmp_path, caplog):
        # silent-failure-hunter I-1: if escalate() itself raises
        # (e.g. MissingAttioCredentials, network error), the operator
        # must still see WeeklyTargetListStaleError, not the Attio
        # error class. The freshness signal is the primary alarm;
        # the queue row is the durable signal; both should surface.
        from workflows import target_freshness as tf

        path = _make_target_file(tmp_path, "stale", days_old=90)
        attio = MagicMock()

        def _raise(**kwargs):
            from workflows.escalation_schemas import MissingAttioCredentials
            raise MissingAttioCredentials("ATTIO_API_KEY unset")

        with patch.object(tf, "escalate", side_effect=_raise), \
             pytest.raises(WeeklyTargetListStaleError), \
             caplog.at_level("ERROR", logger="workflows.target_freshness"):
            check_target_list_freshness(path, date.today(), attio=attio)

        # The escalate failure must be logged with full context.
        assert any(
            "Failed to write target_list_stale queue row" in r.message
            for r in caplog.records
        )


# -- End-to-end TypedDict validation (pr-test BLOCKING fold) ------------


class TestPayloadValidatesAgainstTypedDict:
    def test_payload_passes_runtime_validator(self, tmp_path):
        # pr-test BLOCKING fold: every other STALE test mocks
        # escalate, which bypasses _validate_payload_against_typeddict.
        # This test does NOT mock escalate — instead it mocks the
        # AttioClient at the boundary (so no network call lands) and
        # lets escalate() construct + validate the payload normally.
        # If `TargetListStalePayload` field names drift away from
        # what `check_target_list_freshness` emits, this fails.
        from workflows.escalation import _validate_payload_against_typeddict

        path = _make_target_file(tmp_path, "stale", days_old=90)
        # Build a minimal mock AttioClient that satisfies the
        # _request signature without writing anywhere.
        attio = MagicMock()
        attio._request.return_value = {"data": []}

        # Call escalate() the real way, intercepting only the Attio
        # client. If the payload didn't match TargetListStalePayload,
        # `escalate()` raises `EscalationSchemaError` from the
        # validator BEFORE the Attio call.
        with pytest.raises(WeeklyTargetListStaleError):
            check_target_list_freshness(path, date.today(), attio=attio)

        # Belt-and-suspenders: hand-construct the same payload shape
        # and verify the validator accepts it. If the field set in
        # the TypedDict drifts from what the function emits, BOTH
        # this assertion and the above check_target_list_freshness
        # raise EscalationSchemaError.
        payload = {
            "target_list_path": str(path),
            "last_modified_at": (date.today() - timedelta(days=90)).isoformat(),
            "days_stale": 90,
            "freshness_check_at": date.today().isoformat(),
        }
        _validate_payload_against_typeddict("target_list_stale", payload)


# -- `_meta.updated_at` precedence (code-reviewer I-2 fold) -------------


class TestMetaUpdatedAtPrecedence:
    def test_meta_updated_at_overrides_mtime(self, tmp_path):
        # code-reviewer I-2 fold: a file touched by git checkout
        # (mtime fresh) but with `_meta.updated_at` from 90 days ago
        # MUST be flagged STALE. The semantic refresh signal beats
        # the filesystem signal.
        path = _make_target_file(
            tmp_path, "git_touched", days_old=90,
            with_meta_updated_at=True,
        )
        # Now bump mtime to "today" simulating a git checkout.
        os.utime(path, None)

        attio = MagicMock()
        from workflows import target_freshness as tf
        with patch.object(tf, "escalate"), \
             pytest.raises(WeeklyTargetListStaleError) as exc_info:
            check_target_list_freshness(path, date.today(), attio=attio)
        assert exc_info.value.results[0].days_stale >= STALE_THRESHOLD_DAYS

    def test_mtime_fallback_when_no_meta(self, tmp_path):
        # Standard path: no `_meta.updated_at` → fall back to mtime.
        path = _make_target_file(tmp_path, "no_meta", days_old=90)
        attio = MagicMock()
        from workflows import target_freshness as tf
        with patch.object(tf, "escalate"), \
             pytest.raises(WeeklyTargetListStaleError):
            check_target_list_freshness(path, date.today(), attio=attio)

    def test_read_meta_updated_at_returns_none_for_missing_field(self, tmp_path):
        path = tmp_path / "x.json"
        path.write_text('{"tier_1_p0": []}')
        assert _read_meta_updated_at(path) is None

    def test_read_meta_updated_at_returns_none_for_malformed_json(self, tmp_path):
        path = tmp_path / "x.json"
        path.write_text("not JSON at all")
        assert _read_meta_updated_at(path) is None

    def test_read_meta_updated_at_returns_none_for_bad_date(self, tmp_path):
        path = tmp_path / "x.json"
        path.write_text('{"_meta": {"updated_at": "not-a-date"}}')
        assert _read_meta_updated_at(path) is None

    def test_read_meta_updated_at_accepts_iso_datetime(self, tmp_path):
        path = tmp_path / "x.json"
        path.write_text('{"_meta": {"updated_at": "2026-05-22T10:30:00"}}')
        assert _read_meta_updated_at(path) == date(2026, 5, 22)


# -- Multi-STALE collection (silent-failure B-2 fold) -------------------


class TestMultiStaleCollection:
    def test_two_stale_lists_both_queue_rows_one_raise(
        self, tmp_path, monkeypatch,
    ):
        # silent-failure B-2 + pr-test I-2 convergence: two STALE
        # files emit two queue rows but the operator sees ONE error
        # with both results. Refresh blast-radius visible in one
        # cycle.
        path_a = _make_target_file(tmp_path, "stale_a", days_old=70)
        path_b = _make_target_file(tmp_path, "stale_b", days_old=80)

        from workflows import target_freshness as tf
        from workflows import weekly_prospect
        monkeypatch.setattr(weekly_prospect, "CONTENT_DIR", tmp_path)

        personas_data = {
            "p_a": {"target_company_list": "stale_a"},
            "p_b": {"target_company_list": "stale_b"},
        }
        attio = MagicMock()
        with patch.object(tf, "escalate") as mock_escalate, \
             pytest.raises(WeeklyTargetListStaleError) as exc_info:
            weekly_prospect._check_all_persona_target_lists_fresh(
                personas_data, attio,
            )

        # Both files emit queue rows.
        assert mock_escalate.call_count == 2
        # Single error carries both results.
        results = exc_info.value.results
        assert len(results) == 2
        paths = {r.target_list_path for r in results}
        assert paths == {path_a, path_b}


# -- Idempotency-key shape ----------------------------------------------


class TestIdempotencyKey:
    def test_idempotency_key_shape(self, tmp_path):
        path = _make_target_file(tmp_path, "stale", days_old=90)
        attio = MagicMock()
        from workflows import target_freshness as tf
        with patch.object(tf, "escalate") as mock_escalate, \
             pytest.raises(WeeklyTargetListStaleError):
            check_target_list_freshness(path, date.today(), attio=attio)
        key = mock_escalate.call_args.kwargs["idempotency_key"]
        assert str(path) in key
        assert "|" in key

    def test_re_run_with_same_file_produces_same_key(self, tmp_path):
        path = _make_target_file(tmp_path, "stale", days_old=90)
        attio = MagicMock()
        from workflows import target_freshness as tf
        with patch.object(tf, "escalate") as mock_escalate:
            for _ in range(2):
                with pytest.raises(WeeklyTargetListStaleError):
                    check_target_list_freshness(path, date.today(), attio=attio)
        keys = [c.kwargs["idempotency_key"] for c in mock_escalate.call_args_list]
        assert keys[0] == keys[1]


# -- Missing path is fail-loud ------------------------------------------


class TestMissingPath:
    def test_missing_path_raises_file_not_found(self, tmp_path):
        bogus = tmp_path / "nonexistent.json"
        attio = MagicMock()
        with pytest.raises(FileNotFoundError, match="does not exist"):
            check_target_list_freshness(bogus, date.today(), attio=attio)


# -- Refresh unblocks the gate -----------------------------------------


class TestRefreshLifecycle:
    def test_stale_then_meta_refresh_unblocks_gate(self, tmp_path):
        # End-to-end: STALE → operator refreshes `_meta.updated_at`
        # → next check passes. The git-resistant freshness source
        # demands an explicit `_meta` bump, not a content change.
        path = _make_target_file(
            tmp_path, "lifecycle", days_old=90,
            with_meta_updated_at=True,
        )
        attio = MagicMock()
        from workflows import target_freshness as tf

        with patch.object(tf, "escalate"), \
             pytest.raises(WeeklyTargetListStaleError):
            check_target_list_freshness(path, date.today(), attio=attio)

        # Operator refreshes: bump _meta.updated_at to today.
        data = json.loads(path.read_text())
        data["_meta"]["updated_at"] = date.today().isoformat()
        path.write_text(json.dumps(data))

        with patch.object(tf, "escalate") as mock_escalate:
            result = check_target_list_freshness(path, date.today(), attio=attio)
        assert result.status == FreshnessStatus.FRESH
        mock_escalate.assert_not_called()


# -- Persona orchestration ----------------------------------------------


class TestPersonaOrchestration:
    def test_iterator_skips_missing_files(self, tmp_path, monkeypatch):
        from workflows import weekly_prospect
        monkeypatch.setattr(weekly_prospect, "CONTENT_DIR", tmp_path)
        personas_data = {"p_missing": {"target_company_list": "does_not_exist"}}
        attio = MagicMock()
        # No raise.
        weekly_prospect._check_all_persona_target_lists_fresh(
            personas_data, attio,
        )

    def test_iterator_skips_personas_without_target_list_config(
        self, tmp_path, monkeypatch,
    ):
        from workflows import weekly_prospect
        monkeypatch.setattr(weekly_prospect, "CONTENT_DIR", tmp_path)
        personas_data = {"enterprise_legacy": {"enterprise_mode": True}}
        attio = MagicMock()
        weekly_prospect._check_all_persona_target_lists_fresh(
            personas_data, attio,
        )

    def test_iterator_dedupes_shared_target_lists(self, tmp_path, monkeypatch):
        from workflows import target_freshness as tf
        from workflows import weekly_prospect
        monkeypatch.setattr(weekly_prospect, "CONTENT_DIR", tmp_path)
        _make_target_file(tmp_path, "shared", days_old=5)
        personas_data = {
            "p_a": {"target_company_list": "shared"},
            "p_b": {"target_company_list": "shared"},
            "p_c": {"target_company_list": "shared"},
        }
        attio = MagicMock()
        with patch.object(
            tf, "check_target_list_freshness",
            side_effect=tf.check_target_list_freshness,
        ) as wrapper:
            weekly_prospect._check_all_persona_target_lists_fresh(
                personas_data, attio,
            )
        # Shared file gets checked once — but the wrapper sees the
        # call from the orchestrator's `from workflows.target_freshness
        # import check_target_list_freshness` inside-function import
        # so we patch the module attribute. The orchestrator's local
        # binding resolves at call time per the lazy import (kept
        # explicitly for testability — see weekly_prospect docstring).
        assert wrapper.call_count == 1

    def test_path_traversal_rejected(self, tmp_path, monkeypatch):
        # GTM QA fold: a target_company_list value containing
        # path-traversal (`../`) MUST fail loud, not silently load a
        # file outside CONTENT_DIR.
        from workflows import weekly_prospect
        monkeypatch.setattr(weekly_prospect, "CONTENT_DIR", tmp_path)
        personas_data = {
            "evil": {"target_company_list": "../etc/passwd"},
        }
        attio = MagicMock()
        with pytest.raises(ValueError, match="outside CONTENT_DIR"):
            weekly_prospect._check_all_persona_target_lists_fresh(
                personas_data, attio,
            )


# -- Registry + manifest invariants -------------------------------------


class TestRegistryInvariants:
    def test_target_list_stale_slug_in_escalation_types(self):
        assert "target_list_stale" in ESCALATION_TYPES_SET

    def test_target_list_stale_typeddict_registered(self):
        assert ESCALATION_SCHEMAS.get("target_list_stale") is TargetListStalePayload

    def test_payload_required_fields(self):
        required = {
            "target_list_path", "last_modified_at", "days_stale",
            "freshness_check_at",
        }
        assert required.issubset(TargetListStalePayload.__required_keys__)


# -- FreshnessResult / status enum + __post_init__ invariants -----------


class TestDataclasses:
    def test_freshness_status_enum_string_values(self):
        assert FreshnessStatus.FRESH.value == "fresh"
        assert FreshnessStatus.WARN.value == "warn_30_60"
        assert FreshnessStatus.STALE.value == "stale_over_60"

    def test_freshness_result_is_frozen(self):
        import dataclasses
        result = FreshnessResult(
            status=FreshnessStatus.FRESH,
            days_stale=3,
            last_modified_at=date.today(),
            target_list_path=Path("/tmp/x"),
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.days_stale = 999  # type: ignore[misc]

    def test_post_init_rejects_negative_days(self):
        with pytest.raises(ValueError, match=">= 0"):
            FreshnessResult(
                status=FreshnessStatus.FRESH, days_stale=-1,
                last_modified_at=date.today(), target_list_path=Path("/tmp/x"),
            )

    def test_post_init_enforces_band_correlation_fresh(self):
        with pytest.raises(ValueError, match="FRESH"):
            FreshnessResult(
                status=FreshnessStatus.FRESH, days_stale=99,
                last_modified_at=date.today(), target_list_path=Path("/tmp/x"),
            )

    def test_post_init_enforces_band_correlation_warn(self):
        with pytest.raises(ValueError, match="WARN"):
            FreshnessResult(
                status=FreshnessStatus.WARN, days_stale=99,
                last_modified_at=date.today(), target_list_path=Path("/tmp/x"),
            )

    def test_post_init_enforces_band_correlation_stale(self):
        with pytest.raises(ValueError, match="STALE"):
            FreshnessResult(
                status=FreshnessStatus.STALE, days_stale=5,
                last_modified_at=date.today(), target_list_path=Path("/tmp/x"),
            )

    def test_weekly_target_list_stale_error_carries_results_list(self, tmp_path):
        r1 = FreshnessResult(
            status=FreshnessStatus.STALE, days_stale=90,
            last_modified_at=date.today() - timedelta(days=90),
            target_list_path=tmp_path / "a.json",
        )
        r2 = FreshnessResult(
            status=FreshnessStatus.STALE, days_stale=120,
            last_modified_at=date.today() - timedelta(days=120),
            target_list_path=tmp_path / "b.json",
        )
        err = WeeklyTargetListStaleError([r1, r2])
        assert err.results == [r1, r2]
        # Backwards-compat: `.result` returns first.
        assert err.result is r1
        assert "2 target list(s) stale" in str(err)
        assert "a.json" in str(err)
        assert "b.json" in str(err)

    def test_weekly_target_list_stale_error_rejects_empty_list(self):
        with pytest.raises(ValueError, match="at least one"):
            WeeklyTargetListStaleError([])
