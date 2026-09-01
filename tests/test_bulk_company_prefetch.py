"""Bulk company prefetch for the pipeline preload.

``extract_record_info`` did one blocking company GET per first-seen company,
serially inside the preload prime loop — the dominant cost of a large
pipeline preload. These tests pin the remedy:

- `person_company_ref_id` parses the linked company id without an API call
- `bulk_prime_company_caches` fetches distinct companies through a bounded
  pool and primes the same three caches the lazy path writes, with the
  same fail-open per-company contract as bulk_fetch_persons_by_record_ids
- `CRMProvider.prefetch_companies_for_persons` is the vendor-neutral hook
  (default no-op; AttioProvider harvests + delegates)
- `preload_pipeline_persons` wires the prefetch between the person fetch
  and the prime loop, deduplicated, and fails open when the prefetch dies
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from clients.attio import AttioClient
from clients.crm.attio_provider import AttioProvider
from clients.crm.base import Record
from workflows.metrics import DailyRunMetrics
from workflows.record_cache import RecordCache, preload_pipeline_persons


@pytest.fixture
def attio() -> AttioClient:
    return AttioClient(api_key="test-key")


def _person(rid: str, cid: str | None) -> dict:
    values: dict = {"name": [{"first_name": "Ana", "last_name": "Diaz"}]}
    if cid is not None:
        values["company"] = [{"target_record_id": cid}]
    return {"id": {"record_id": rid}, "values": values}


def _company_response(name: str, industry: str | None = None) -> dict:
    values: dict = {"name": [{"value": name}]}
    if industry is not None:
        values["industry_vertical"] = [{"option": {"title": industry}}]
    return {"data": {"id": {"record_id": "x"}, "values": values}}


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.attio.com/v2/x")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"{status}", request=request, response=response)


class TestPersonCompanyRefId:
    def test_extracts_target_record_id(self):
        assert AttioClient.person_company_ref_id(_person("r1", "co-1")) == "co-1"

    def test_primary_company_fallback(self):
        record = {
            "values": {"primary_company": [{"target_record_id": "co-2"}]},
        }
        assert AttioClient.person_company_ref_id(record) == "co-2"

    def test_none_when_no_company(self):
        assert AttioClient.person_company_ref_id(_person("r1", None)) is None

    def test_none_for_plain_value_company(self):
        # Non-reference company values are rendered verbatim by
        # extract_record_info — the prefetch must skip them.
        record = {"values": {"company": [{"value": "Acme Sociedad Anonima"}]}}
        assert AttioClient.person_company_ref_id(record) is None

    def test_none_on_empty_record(self):
        assert AttioClient.person_company_ref_id({}) is None


class TestBulkPrimeCompanyCaches:
    def test_primes_all_three_caches(self, attio: AttioClient):
        with patch.object(
            attio, "_request",
            return_value=_company_response("Acme", "manufacturing"),
        ):
            primed = attio.bulk_prime_company_caches({"co-1"})
        assert primed == 1
        assert attio._company_cache["co-1"] == "Acme"
        assert attio._industry_cache["co-1"] == "manufacturing"
        assert attio._company_corruption_cache["co-1"] is False

    def test_extract_record_info_hits_primed_cache(self, attio: AttioClient):
        with patch.object(
            attio, "_request",
            return_value=_company_response("Acme", "manufacturing"),
        ):
            attio.bulk_prime_company_caches({"co-1"})
        # After priming, extract_record_info must not fetch the company.
        with patch.object(attio, "_request", side_effect=AssertionError(
            "company GET after prefetch — cache miss"
        )):
            _name, company, _url, industry, _title = attio.extract_record_info(
                _person("r1", "co-1")
            )
        assert company == "Acme"
        assert industry == "manufacturing"

    def test_corruption_fingerprint_detected(self, attio: AttioClient):
        corrupted = {
            "data": {
                "values": {
                    "name": [{"value": "Placeholder Employer"}],
                    "domains": [{"domain": "linkedin.com"}],
                }
            }
        }
        with patch.object(attio, "_request", return_value=corrupted):
            attio.bulk_prime_company_caches({"co-bad"})
        assert attio._company_corruption_cache["co-bad"] is True

    def test_skips_already_cached_ids(self, attio: AttioClient):
        attio._company_cache["co-1"] = "Cached Co"
        with patch.object(attio, "_request") as mock_req:
            assert attio.bulk_prime_company_caches({"co-1"}) == 0
        mock_req.assert_not_called()

    def test_empty_and_falsy_ids_are_noop(self, attio: AttioClient):
        with patch.object(attio, "_request") as mock_req:
            assert attio.bulk_prime_company_caches(set()) == 0
            assert attio.bulk_prime_company_caches({""}) == 0
        mock_req.assert_not_called()

    def test_404_primes_empty_sentinel_but_not_counted_returned(self, attio: AttioClient):
        # Matches the lazy path: HTTPStatusError there caches "" so the
        # prime loop never re-fetches a known-missing company. But the
        # metric mirrors the persons contract — a dangling company ref
        # (deleted/merged company) must stay visible as
        # requested != returned + failed, not read as healthy.
        m = DailyRunMetrics()
        with patch.object(attio, "_request", side_effect=_http_error(404)):
            primed = attio.bulk_prime_company_caches({"co-gone"}, metrics=m)
        assert primed == 1
        assert attio._company_cache["co-gone"] == ""
        assert attio._industry_cache["co-gone"] == ""
        assert m.bulk_fetch_companies_requested == 1
        assert m.bulk_fetch_companies_returned == 0
        assert m.bulk_fetch_companies_failed == 0

    def test_malformed_name_entry_degrades_to_sentinel_not_batch_abort(
        self, attio: AttioClient,
    ):
        # Attio has shipped non-dict `name` entries before
        # (is_linkedin_clearbit_corrupted guards the same shape). One
        # malformed company must cost at most that company — never the
        # rest of the batch.
        def fake_request(method, path, **kwargs):
            if "co-weird" in path:
                return {"data": {"values": {"name": ["just-a-string"]}}}
            return _company_response("Good Co")

        with patch.object(attio, "_request", side_effect=fake_request):
            primed = attio.bulk_prime_company_caches({"co-weird", "co-good"})
        assert primed == 2
        assert attio._company_cache["co-weird"] == ""
        assert attio._company_cache["co-good"] == "Good Co"

    def test_undecodable_json_body_is_fail_open_per_company(self, attio: AttioClient):
        # A truncated/HTML 200 body (proxy mid-outage) raises
        # JSONDecodeError — transport-class, not a refactor bug: one
        # company skipped, the rest still primed.
        import json as _json

        def fake_request(method, path, **kwargs):
            if "co-bad" in path:
                raise _json.JSONDecodeError("boom", "<html>", 0)
            return _company_response("Good Co")

        m = DailyRunMetrics()
        with patch.object(attio, "_request", side_effect=fake_request):
            primed = attio.bulk_prime_company_caches(
                {"co-good", "co-bad"}, metrics=m,
            )
        assert primed == 1
        assert "co-bad" not in attio._company_cache
        assert m.bulk_fetch_companies_failed == 1

    def test_transport_failure_is_fail_open_per_company(self, attio: AttioClient):
        def fake_request(method, path, **kwargs):
            if "co-bad" in path:
                raise httpx.ConnectError("boom")
            return _company_response("Good Co")

        m = DailyRunMetrics()
        with patch.object(attio, "_request", side_effect=fake_request):
            primed = attio.bulk_prime_company_caches(
                {"co-good", "co-bad"}, metrics=m,
            )
        assert primed == 1
        assert attio._company_cache["co-good"] == "Good Co"
        # The failed id stays unprimed → lazy serial fetch covers it.
        assert "co-bad" not in attio._company_cache
        assert m.bulk_fetch_companies_requested == 2
        assert m.bulk_fetch_companies_returned == 1
        assert m.bulk_fetch_companies_failed == 1
        assert any("co-bad" in w for w in m.runtime_warnings)

    def test_non_transport_exception_propagates(self, attio: AttioClient):
        # Refactor bugs must surface, not silently skip every company.
        with patch.object(attio, "_request", side_effect=ValueError("bug")), \
                pytest.raises(ValueError):
            attio.bulk_prime_company_caches({"co-1"})

    def test_uses_fail_fast_retry_contract(self, attio: AttioClient):
        with patch.object(attio, "get_company", return_value=None) as mock_get:
            attio.bulk_prime_company_caches({"co-1"})
        mock_get.assert_called_once_with("co-1", retry_500=False)


class TestProviderPrefetchHook:
    """The fork routes the prefetch through the CRMProvider contract; the
    Attio adapter harvests the ids, other adapters inherit the no-op."""

    def test_attio_provider_harvests_and_dedupes(self):
        inner = MagicMock(spec=AttioClient)
        inner.bulk_prime_company_caches.return_value = 2
        provider = AttioProvider(inner)
        records = [
            Record("r1", "people", {}, _person("r1", "co-1")),
            Record("r2", "people", {}, _person("r2", "co-1")),  # dedupe
            Record("r3", "people", {}, _person("r3", "co-2")),
            Record("r4", "people", {}, _person("r4", None)),    # excluded
        ]
        assert provider.prefetch_companies_for_persons(records) == 2
        (ids,), _kwargs = inner.bulk_prime_company_caches.call_args
        assert ids == {"co-1", "co-2"}

    def test_attio_provider_skips_call_when_no_company_refs(self):
        inner = MagicMock(spec=AttioClient)
        provider = AttioProvider(inner)
        records = [Record("r1", "people", {}, _person("r1", None))]
        assert provider.prefetch_companies_for_persons(records) == 0
        inner.bulk_prime_company_caches.assert_not_called()

    def test_contract_default_is_a_noop(self):
        # A provider that does not follow a company reference inherits the
        # base no-op — no engine behavior may depend on the prefetch.
        from clients.crm.base import CRMProvider

        assert CRMProvider.prefetch_companies_for_persons(
            MagicMock(), [Record("r1", "people", {}, _person("r1", "co-1"))],
        ) == 0


class TestPreloadWiring:
    def test_prefetch_receives_deduped_company_ids(self):
        attio = MagicMock(spec=AttioClient)
        attio.bulk_fetch_persons_by_record_ids.return_value = {
            "r1": _person("r1", "co-1"),
            "r2": _person("r2", "co-1"),  # same company — must dedupe
            "r3": _person("r3", "co-2"),
            "r4": _person("r4", None),   # no company — must be excluded
        }
        attio.extract_record_info.return_value = ("N", "C", "", None, "")
        cache = RecordCache(attio)  # type: ignore[arg-type]
        primed = preload_pipeline_persons(
            attio, cache, {"r1", "r2", "r3", "r4"},  # type: ignore[arg-type]
        )
        assert primed == 4
        attio.bulk_prime_company_caches.assert_called_once()
        (ids,), _kwargs = attio.bulk_prime_company_caches.call_args
        assert ids == {"co-1", "co-2"}

    def test_prefetch_failure_fails_open_to_serial(self):
        attio = MagicMock(spec=AttioClient)
        attio.bulk_fetch_persons_by_record_ids.return_value = {
            "r1": _person("r1", "co-1"),
        }
        attio.bulk_prime_company_caches.side_effect = RuntimeError("pool died")
        attio.extract_record_info.return_value = ("N", "C", "", None, "")
        m = DailyRunMetrics()
        cache = RecordCache(attio)  # type: ignore[arg-type]
        primed = preload_pipeline_persons(
            attio, cache, {"r1"}, metrics=m,  # type: ignore[arg-type]
        )
        # Persons still prime via the serial loop (lazy company fetch).
        assert primed == 1
        # The prefetch phase is still timed (finally-block).
        assert "preload_company_fetch_parallel" in m.phase_seconds
        assert "preload_company_resolve_serial" in m.phase_seconds
        # A total prefetch collapse regresses the run to the slow serial
        # path — it must reach the end-of-run summary, not just stdout,
        # and carry the exception type for debuggability.
        assert any(
            "bulk company prefetch failed" in w and "RuntimeError" in w
            for w in m.runtime_warnings
        )

    def test_malformed_person_record_costs_one_person_not_the_prefetch(self):
        # person_company_ref_id is total: a person with non-dict values
        # yields None in the id harvest instead of aborting the whole
        # prefetch with an AttributeError.
        assert AttioClient.person_company_ref_id(
            {"id": {"record_id": "r1"}, "values": "corrupt"}
        ) is None
        assert AttioClient.person_company_ref_id("not-a-dict") is None
