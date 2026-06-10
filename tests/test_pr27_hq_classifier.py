"""Tests for PR-27 — `company_hq_country` Haiku classifier.

Covers `classify_company_hq` (the per-company classifier) and
`backfill_company_hq_for_missing` (the Attio scan + update sweep).
Builds B-PW-GLOBAL.

Coverage surfaces:
  * Haiku response parsing (well-formed JSON, malformed JSON, markdown
    fences, missing fields, non-numeric confidence, clamp out-of-range)
  * Explicit-unknown vs. parse-error distinction (the None-vs-result
    semantics mirror `classify_industry`)
  * LATAM detection — Spanish/Portuguese/English forms
  * Cost-ceiling exhausted path (CostCeilingExhausted treated as
    transient, returns None for retry-later)
  * Backfill idempotency — only touches records without populated HQ
  * Backfill respects `limit` + `dry_run`
  * Backfill summary surfaces latam_count + non_latam_count
  * Writer registry pins the canonical write-owner path
  * Schema manifest entries present for both attrs
"""

from __future__ import annotations

from unittest.mock import MagicMock

from workflows.weekly_prospect import (
    HQClassificationResult,
    _is_latam_country,
    _parse_hq_response,
    backfill_company_hq_for_missing,
    classify_company_hq,
)


def _client_returning(text: str) -> MagicMock:
    """Build a MagicMock Anthropic-shaped client that returns `text`."""
    client = MagicMock()
    response = MagicMock()
    block = MagicMock()
    block.type = "text"
    block.text = text
    response.content = [block]
    client.messages.create.return_value = response
    return client


def _company_record(name: str, *, hq_country: str | None = None) -> dict:
    """Build a minimal Attio company record shape for backfill tests."""
    values: dict = {"name": [{"value": name}]}
    if hq_country is not None:
        values["company_hq_country"] = [{"value": hq_country}]
    return {"id": {"record_id": f"rec_{name.lower().replace(' ', '_')}"}, "values": values}


# -- Parse response ------------------------------------------------------


class TestParseHQResponse:
    def test_well_formed_response(self):
        result = _parse_hq_response('{"country": "Mexico", "confidence": 0.9}', "Bimbo")
        assert result == HQClassificationResult(country="mexico", confidence=0.9)
        # `is_latam` is now a derived property — verify separately.
        assert result.is_latam is True

    def test_well_formed_non_latam(self):
        result = _parse_hq_response(
            '{"country": "Germany", "confidence": 0.95}', "Siemens"
        )
        assert result is not None
        assert result.country == "germany"
        assert result.confidence == 0.95
        assert result.is_latam is False

    def test_markdown_fenced_response_still_parses(self):
        raw = '```json\n{"country": "Brasil", "confidence": 0.8}\n```'
        result = _parse_hq_response(raw, "Marcopolo")
        assert result is not None
        assert result.country == "brasil"
        assert result.is_latam is True

    def test_explicit_unknown_returns_result_with_country_none(self):
        result = _parse_hq_response(
            '{"country": "unknown", "confidence": 0.0}', "Acme GenericCo"
        )
        assert result is not None
        assert result.country is None
        assert result.confidence == 0.0
        assert result.is_latam is False

    def test_malformed_json_returns_none(self):
        assert _parse_hq_response("this is not JSON", "Bimbo") is None

    def test_missing_country_field_returns_none(self):
        assert _parse_hq_response('{"confidence": 0.9}', "Bimbo") is None

    def test_non_string_country_returns_none(self):
        assert _parse_hq_response(
            '{"country": 42, "confidence": 0.9}', "Bimbo"
        ) is None

    def test_empty_country_returns_none(self):
        assert _parse_hq_response(
            '{"country": "", "confidence": 0.9}', "Bimbo"
        ) is None

    def test_non_numeric_confidence_clamps_to_zero(self):
        result = _parse_hq_response(
            '{"country": "Mexico", "confidence": "high"}', "Bimbo"
        )
        assert result is not None
        assert result.confidence == 0.0

    def test_out_of_range_confidence_clamped(self):
        assert _parse_hq_response(
            '{"country": "Mexico", "confidence": 1.5}', "Bimbo"
        ).confidence == 1.0
        assert _parse_hq_response(
            '{"country": "Mexico", "confidence": -0.3}', "Bimbo"
        ).confidence == 0.0

    def test_nan_confidence_demoted_to_zero(self):
        # Math-QA convergence: bare `NaN` in Haiku's JSON would clamp
        # to 1.0 under min/max comparison semantics. The fold rejects
        # non-finite values to 0.0 before the clamp.
        result = _parse_hq_response(
            '{"country": "Mexico", "confidence": NaN}', "Bimbo"
        )
        assert result is not None
        assert result.confidence == 0.0

    def test_infinity_confidence_demoted_to_zero(self):
        result = _parse_hq_response(
            '{"country": "Mexico", "confidence": Infinity}', "Bimbo"
        )
        assert result is not None
        assert result.confidence == 0.0


# -- LATAM detection -----------------------------------------------------


class TestLatamDetection:
    def test_spanish_country_names(self):
        for name in ("mexico", "méxico", "colombia", "perú", "argentina",
                     "chile", "ecuador", "panamá", "puerto rico"):
            assert _is_latam_country(name) is True, name

    def test_portuguese_country_names(self):
        assert _is_latam_country("brasil") is True
        assert _is_latam_country("brazil") is True

    def test_english_country_names(self):
        assert _is_latam_country("Mexico") is True
        assert _is_latam_country("Dominican Republic") is True

    def test_non_latam_countries(self):
        for name in ("united states", "germany", "france", "japan",
                     "switzerland", "spain", "china"):
            assert _is_latam_country(name) is False, name

    def test_none_and_empty(self):
        assert _is_latam_country(None) is False
        assert _is_latam_country("") is False
        assert _is_latam_country("   ") is False

    def test_case_insensitive(self):
        assert _is_latam_country("MEXICO") is True
        assert _is_latam_country("  Brazil  ") is True


# -- classify_company_hq (test-injection path) ---------------------------


class TestClassifyCompanyHQ:
    def test_classifies_known_latam_company(self):
        client = _client_returning('{"country": "Mexico", "confidence": 0.95}')
        result = classify_company_hq("Grupo Bimbo", "bimbo.com", anthropic_client=client)
        assert result is not None
        assert result.country == "mexico"
        assert result.is_latam is True
        assert result.confidence == 0.95

    def test_classifies_non_latam_multinational(self):
        client = _client_returning('{"country": "Germany", "confidence": 0.9}')
        result = classify_company_hq("Siemens AG", anthropic_client=client)
        assert result is not None
        assert result.country == "germany"
        assert result.is_latam is False

    def test_empty_company_name_returns_none(self):
        assert classify_company_hq("", anthropic_client=MagicMock()) is None
        assert classify_company_hq(None, anthropic_client=MagicMock()) is None

    def test_dispatch_off_and_no_client_returns_none(self, monkeypatch):
        # Default: use_llm_dispatch=False, anthropic_client=None,
        # OUTBOUND_USE_LLM_DISPATCH unset → safe-for-tests no-op.
        # Defensively unset the env var so a CI environment that
        # exports it doesn't silently flip this test to a real
        # dispatch attempt (code-reviewer I-3 convergence).
        monkeypatch.delenv("OUTBOUND_USE_LLM_DISPATCH", raising=False)
        assert classify_company_hq("Grupo Bimbo", "bimbo.com") is None

    def test_client_exception_returns_none(self):
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("network down")
        assert classify_company_hq("Bimbo", anthropic_client=client) is None

    def test_hqclassificationresult_rejects_nonfinite_confidence(self):
        # Type-design QA convergence: __post_init__ validator catches
        # NaN / inf at construction. The parser guards this too, but
        # the dataclass enforces the invariant at every construction
        # site (hand-built, test fixtures, future ingest pipeline).
        import math

        import pytest

        with pytest.raises(ValueError, match="finite"):
            HQClassificationResult(country="mexico", confidence=float("nan"))
        with pytest.raises(ValueError, match="finite"):
            HQClassificationResult(country="mexico", confidence=math.inf)

    def test_hqclassificationresult_rejects_out_of_range_confidence(self):
        import pytest

        with pytest.raises(ValueError, match=r"\[0\.0, 1\.0\]"):
            HQClassificationResult(country="mexico", confidence=1.5)
        with pytest.raises(ValueError, match=r"\[0\.0, 1\.0\]"):
            HQClassificationResult(country="mexico", confidence=-0.3)

    def test_is_latam_is_derived_property(self):
        # Cannot construct an inconsistent state — `is_latam` is now
        # a read-only @property derived from `country`.
        mexico = HQClassificationResult(country="mexico", confidence=0.9)
        germany = HQClassificationResult(country="germany", confidence=0.9)
        unknown = HQClassificationResult(country=None, confidence=0.0)
        assert mexico.is_latam is True
        assert germany.is_latam is False
        assert unknown.is_latam is False


# -- Cost-ceiling exhausted path -----------------------------------------


class TestCostCeilingPath:
    def test_cost_ceiling_exhausted_returns_none(self, monkeypatch):
        # Verify the dispatch-path branch catches CostCeilingExhausted
        # alongside other dispatch errors and returns None (transient
        # semantics). Operators surface cost-ceiling separately via
        # dispatch log breadcrumbs.
        from workflows import llm_dispatch as ld

        def _raise_ceiling(**kwargs):
            raise ld.CostCeilingExhausted(
                step="company_hq_classifier",
                cap=0.50,
                consumed=0.51,
            )

        monkeypatch.setattr(ld, "request_llm_dispatch", _raise_ceiling)
        monkeypatch.setattr(ld, "is_dispatch_enabled", lambda: True)

        assert classify_company_hq("Bimbo", "bimbo.com") is None

    def test_dispatch_timeout_returns_none(self, monkeypatch):
        from workflows import llm_dispatch as ld

        def _raise_timeout(**kwargs):
            raise ld.LLMDispatchTimeout("company_hq_classifier", "abc", 300.0)

        monkeypatch.setattr(ld, "request_llm_dispatch", _raise_timeout)
        monkeypatch.setattr(ld, "is_dispatch_enabled", lambda: True)
        assert classify_company_hq("Bimbo") is None

    def test_dispatch_failed_returns_none(self, monkeypatch):
        from workflows import llm_dispatch as ld

        def _raise_failed(**kwargs):
            raise ld.LLMDispatchFailed("company_hq_classifier", "abc", "boom")

        monkeypatch.setattr(ld, "request_llm_dispatch", _raise_failed)
        monkeypatch.setattr(ld, "is_dispatch_enabled", lambda: True)
        assert classify_company_hq("Bimbo") is None


# -- Backfill sweep ------------------------------------------------------


class TestBackfillCompanyHQ:
    @staticmethod
    def _no_sleep(monkeypatch):
        # pr-test-analyzer convergence: keep the suite fast — the
        # backfill loop's per-record `time.sleep(0.2)` is real-time
        # latency under test. The dry-run path already skips the
        # sleep, but exercising the write path requires this.
        import time as time_module
        monkeypatch.setattr(time_module, "sleep", lambda _: None)

    def _attio_with(self, records: list[dict]) -> MagicMock:
        attio = MagicMock()
        attio.search_companies.return_value = records
        attio.update_company.return_value = None
        return attio

    def test_idempotent_skips_already_populated(self, monkeypatch):
        self._no_sleep(monkeypatch)
        attio = self._attio_with([
            _company_record("Bimbo", hq_country="mexico"),
            _company_record("Siemens"),
        ])
        client = _client_returning('{"country": "Germany", "confidence": 0.9}')

        summary = backfill_company_hq_for_missing(
            attio, anthropic_client=client,
        )

        assert summary["total_scanned"] == 2
        assert summary["missing"] == 1
        assert summary["classified"] == 1
        assert summary["written"] == 1
        attio.update_company.assert_called_once()
        attio2 = self._attio_with([
            _company_record("Bimbo", hq_country="mexico"),
            _company_record("Siemens", hq_country="germany"),
        ])
        summary2 = backfill_company_hq_for_missing(attio2, anthropic_client=client)
        assert summary2["missing"] == 0
        assert summary2["classified"] == 0
        assert summary2["written"] == 0

    def test_dry_run_does_not_write(self):
        # Dry-run path skips sleep internally; no monkeypatch needed.
        attio = self._attio_with([_company_record("Bimbo")])
        client = _client_returning('{"country": "Mexico", "confidence": 0.9}')

        summary = backfill_company_hq_for_missing(
            attio, anthropic_client=client, dry_run=True,
        )

        assert summary["classified"] == 1
        assert summary["written"] == 0
        attio.update_company.assert_not_called()

    def test_respects_limit(self, monkeypatch):
        self._no_sleep(monkeypatch)
        records = [_company_record(f"Co{i}") for i in range(5)]
        attio = self._attio_with(records)
        client = _client_returning('{"country": "Mexico", "confidence": 0.9}')

        summary = backfill_company_hq_for_missing(
            attio, anthropic_client=client, limit=2,
        )

        assert summary["missing"] == 5
        assert summary["classified"] == 2
        assert summary["written"] == 2

    def test_summary_surfaces_latam_breakdown(self, monkeypatch):
        self._no_sleep(monkeypatch)
        attio = self._attio_with([
            _company_record("Bimbo"),
            _company_record("Siemens"),
            _company_record("Petrobras"),
        ])
        responses = iter([
            '{"country": "Mexico", "confidence": 0.9}',
            '{"country": "Germany", "confidence": 0.9}',
            '{"country": "Brasil", "confidence": 0.85}',
        ])
        client = MagicMock()
        client.messages.create.side_effect = lambda **_: _wrap_text(next(responses))

        summary = backfill_company_hq_for_missing(attio, anthropic_client=client)

        assert summary["classified"] == 3
        assert summary["latam_count"] == 2
        assert summary["non_latam_count"] == 1
        assert summary["confirmed_unknown_count"] == 0

    def test_classifier_failure_counts_as_api_error_and_skips_write(self):
        attio = self._attio_with([_company_record("Bimbo")])
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("transient")

        summary = backfill_company_hq_for_missing(
            attio, anthropic_client=client,
        )

        assert summary["classified"] == 0
        assert summary["api_errors"] == 1
        attio.update_company.assert_not_called()

    def test_skips_records_without_name(self, monkeypatch):
        self._no_sleep(monkeypatch)
        records = [
            {"id": {"record_id": "rec_x"}, "values": {}},
            _company_record("Bimbo"),
        ]
        attio = self._attio_with(records)
        client = _client_returning('{"country": "Mexico", "confidence": 0.9}')

        summary = backfill_company_hq_for_missing(attio, anthropic_client=client)
        assert summary["skipped"] == 1
        assert summary["classified"] == 1

    # ── Post-fold additions (cross-QA convergence) ────────────────────

    def test_write_payload_shape_for_latam_company(self, monkeypatch):
        # pr-test-analyzer I-1: pin the Attio write payload shape so a
        # future key-typo or dropped-field regression surfaces here.
        self._no_sleep(monkeypatch)
        attio = self._attio_with([_company_record("Bimbo")])
        client = _client_returning('{"country": "Mexico", "confidence": 0.95}')

        backfill_company_hq_for_missing(attio, anthropic_client=client)

        attrs = attio.update_company.call_args.args[1]
        assert attrs == {
            "company_hq_country": "mexico",
            "company_hq_confidence": 0.95,
        }

    def test_explicit_unknown_skips_attio_write(self, monkeypatch):
        # Cross-QA convergence (type-design B-2 + GTM I-1 + code-reviewer
        # B-2 + prospect-weekly I-1): writing a sentinel "unknown" string
        # to Attio breaks future re-classification via the populated-field
        # skip. The fold treats explicit-unknown like a transient failure
        # for the write path — count separately, leave the field null,
        # let the next sweep re-try with a (possibly improved) prompt.
        self._no_sleep(monkeypatch)
        attio = self._attio_with([_company_record("Acme")])
        client = _client_returning('{"country": "unknown", "confidence": 0.0}')

        summary = backfill_company_hq_for_missing(attio, anthropic_client=client)

        assert summary["classified"] == 1
        assert summary["confirmed_unknown_count"] == 1
        assert summary["latam_count"] == 0
        assert summary["non_latam_count"] == 0
        assert summary["written"] == 0
        attio.update_company.assert_not_called()

    def test_attio_http_status_error_counted_as_api_error(self, monkeypatch):
        # pr-test-analyzer I-2: HTTP error path on update_company.
        self._no_sleep(monkeypatch)
        import httpx
        attio = self._attio_with([_company_record("Bimbo")])
        response = MagicMock()
        response.status_code = 422
        attio.update_company.side_effect = httpx.HTTPStatusError(
            "422 Unprocessable", request=MagicMock(), response=response,
        )
        client = _client_returning('{"country": "Mexico", "confidence": 0.9}')

        summary = backfill_company_hq_for_missing(attio, anthropic_client=client)

        assert summary["classified"] == 1
        assert summary["written"] == 0
        assert summary["api_errors"] == 1

    def test_attio_network_error_does_not_kill_sweep(self, monkeypatch):
        # silent-failure-hunter convergence: a transient httpx network
        # error (ReadTimeout / ConnectError / RequestError) mid-sweep
        # must be caught + logged, not crash the whole batch.
        #
        # Wave-2-B: _write_hq_attrs now routes through AttioWriter
        # which retries timeouts up to MAX_ATTEMPTS=5 before raising
        # AttioRateLimitExhausted. The side_effect is a callable so
        # the first record's retries all fail uniformly (mirrors a
        # genuine Attio outage) while the second record succeeds.
        self._no_sleep(monkeypatch)
        import httpx
        attio = self._attio_with([
            _company_record("Bimbo"),
            _company_record("Siemens"),
        ])
        # First record's writes always time out (forces
        # AttioRateLimitExhausted after 5 retries); second succeeds.
        call_counter = {"n": 0}
        def _flaky_update(record_id: str, attrs: dict):
            call_counter["n"] += 1
            if record_id == "rec_bimbo":
                raise httpx.ReadTimeout(
                    "attio is slow", request=MagicMock(),
                )
            return None
        attio.update_company.side_effect = _flaky_update
        responses = iter([
            '{"country": "Mexico", "confidence": 0.9}',
            '{"country": "Germany", "confidence": 0.9}',
        ])
        client = MagicMock()
        client.messages.create.side_effect = lambda **_: _wrap_text(next(responses))

        summary = backfill_company_hq_for_missing(attio, anthropic_client=client)

        # Both classifications happened; one write failed, one succeeded.
        assert summary["classified"] == 2
        assert summary["written"] == 1
        assert summary["api_errors"] == 1

    def test_invariant_classified_equals_sum_of_buckets(self, monkeypatch):
        # math-QA + GTM convergence: every classification lands in
        # exactly one of latam_count, non_latam_count, confirmed_unknown_count.
        self._no_sleep(monkeypatch)
        attio = self._attio_with([
            _company_record("Bimbo"),
            _company_record("Siemens"),
            _company_record("Acme"),
        ])
        responses = iter([
            '{"country": "Mexico", "confidence": 0.9}',
            '{"country": "Germany", "confidence": 0.9}',
            '{"country": "unknown", "confidence": 0.0}',
        ])
        client = MagicMock()
        client.messages.create.side_effect = lambda **_: _wrap_text(next(responses))

        summary = backfill_company_hq_for_missing(attio, anthropic_client=client)

        assert summary["classified"] == (
            summary["latam_count"]
            + summary["non_latam_count"]
            + summary["confirmed_unknown_count"]
        )

    def test_domain_appended_to_prompt_when_set(self):
        # pr-test-analyzer I-3: domain must reach the prompt body.
        client = _client_returning('{"country": "Mexico", "confidence": 0.9}')
        classify_company_hq("Bimbo", "bimbo.com", anthropic_client=client)
        msg = client.messages.create.call_args.kwargs["messages"][0]["content"]
        assert "Company: Bimbo" in msg
        assert "Domain: bimbo.com" in msg

    def test_domain_omitted_when_none(self):
        client = _client_returning('{"country": "Mexico", "confidence": 0.9}')
        classify_company_hq("Bimbo", None, anthropic_client=client)
        msg = client.messages.create.call_args.kwargs["messages"][0]["content"]
        assert "Domain:" not in msg


def _wrap_text(text: str):
    """Build a single-message Anthropic-shaped response wrapping `text`."""
    response = MagicMock()
    block = MagicMock()
    block.type = "text"
    block.text = text
    response.content = [block]
    return response


# -- Schema manifest + writer registry invariants ------------------------


class TestRegistryAndManifestInvariants:
    def test_writer_registry_pins_canonical_owner(self):
        from clients.attio_writer_registry import WRITE_OWNER_REGISTRY

        assert WRITE_OWNER_REGISTRY[("companies", "company_hq_country")] == (
            "workflows.weekly_prospect.classify_company_hq"
        )
        assert WRITE_OWNER_REGISTRY[("companies", "company_hq_confidence")] == (
            "workflows.weekly_prospect.classify_company_hq"
        )

    def test_manifest_declares_both_attrs(self):
        # Schema manifest pre-populated by F-PR-3.5 with status=planned.
        # PR-27 ships the writer; the validator's collision pre-flight
        # is unchanged. This test pins the manifest entries so a future
        # accidental edit will surface here.
        from pathlib import Path

        import yaml

        manifest_path = Path(__file__).parent.parent / "docs" / "attio_schema_deltas.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())

        attrs = {
            (a["object"], a["slug"]): a
            for a in manifest.get("attributes", [])
        }

        country = attrs.get(("companies", "company_hq_country"))
        assert country is not None
        assert country["type"] == "text"
        assert country["pr_id"] == "PR-27"
        assert country["write_owner_module"] == (
            "workflows.weekly_prospect.classify_company_hq"
        )

        conf = attrs.get(("companies", "company_hq_confidence"))
        assert conf is not None
        assert conf["type"] == "number"
        assert conf["pr_id"] == "PR-27"
        assert conf["write_owner_module"] == (
            "workflows.weekly_prospect.classify_company_hq"
        )
