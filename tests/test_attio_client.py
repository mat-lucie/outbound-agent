"""Tests for AttioClient delete helpers used by the dedup script."""

from unittest.mock import patch

import httpx
import pytest

from clients.attio import AttioClient


def _http_error(status: int, url: str = "https://api.attio.com/v2/x") -> httpx.HTTPStatusError:
    request = httpx.Request("DELETE", url)
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"{status}", request=request, response=response)


@pytest.fixture
def attio() -> AttioClient:
    return AttioClient(api_key="test-key")


class TestDeletePerson:
    def test_returns_true_on_success(self, attio: AttioClient) -> None:
        with patch.object(attio, "_request", return_value={}) as mock_req:
            result = attio.delete_person("abc-123")

        assert result is True
        mock_req.assert_called_once_with("DELETE", "/objects/people/records/abc-123")

    def test_returns_false_on_404(self, attio: AttioClient) -> None:
        with patch.object(attio, "_request", side_effect=_http_error(404)):
            assert attio.delete_person("missing") is False

    def test_raises_on_other_errors(self, attio: AttioClient) -> None:
        with patch.object(attio, "_request", side_effect=_http_error(500)), pytest.raises(httpx.HTTPStatusError):
            attio.delete_person("abc-123")


class TestDeleteCompany:
    def test_returns_true_on_success(self, attio: AttioClient) -> None:
        with patch.object(attio, "_request", return_value={}) as mock_req:
            result = attio.delete_company("co-123")

        assert result is True
        mock_req.assert_called_once_with("DELETE", "/objects/companies/records/co-123")

    def test_returns_false_on_404(self, attio: AttioClient) -> None:
        with patch.object(attio, "_request", side_effect=_http_error(404)):
            assert attio.delete_company("missing") is False

    def test_raises_on_other_errors(self, attio: AttioClient) -> None:
        with patch.object(attio, "_request", side_effect=_http_error(403)), pytest.raises(httpx.HTTPStatusError):
            attio.delete_company("co-123")


class TestDeleteListEntry:
    def test_uses_explicit_list_id(self, attio: AttioClient) -> None:
        with patch.object(attio, "_request", return_value={}) as mock_req:
            result = attio.delete_list_entry("entry-9", list_id="list-xyz")

        assert result is True
        mock_req.assert_called_once_with("DELETE", "/lists/list-xyz/entries/entry-9")

    def test_falls_back_to_env_list_id(self, attio: AttioClient, monkeypatch) -> None:
        monkeypatch.setenv("ATTIO_LIST_ID", "env-list")
        with patch.object(attio, "_request", return_value={}) as mock_req:
            attio.delete_list_entry("entry-9")

        mock_req.assert_called_once_with("DELETE", "/lists/env-list/entries/entry-9")

    def test_returns_false_on_404(self, attio: AttioClient) -> None:
        with patch.object(attio, "_request", side_effect=_http_error(404)):
            assert attio.delete_list_entry("missing", list_id="list-xyz") is False


class TestLinkedInUrlCanonicalization:
    """URL-encoded, www, and trailing-slash variants all collapse to one form.

    Attio's text-field filter is exact-string. Without canonicalization the
    same LinkedIn profile creates a fresh duplicate each time PB returns a
    subtly different URL encoding — the root cause of the recurring
    duplicate-Attio-records bug.
    """

    def test_canonical_strips_www_slash_and_encoding(self) -> None:
        from clients.attio import _canonical_linkedin_url

        canonical = "https://linkedin.com/in/iñigo-marchal-b8901186"
        variants = [
            "https://www.linkedin.com/in/iñigo-marchal-b8901186",
            "https://linkedin.com/in/iñigo-marchal-b8901186",
            "https://www.linkedin.com/in/iñigo-marchal-b8901186/",
            "https://www.linkedin.com/in/i%C3%B1igo-marchal-b8901186",
            "HTTPS://www.linkedin.com/in/iñigo-marchal-b8901186",
        ]
        for v in variants:
            assert _canonical_linkedin_url(v) == canonical, v

    def test_variants_list_includes_www_and_slash_forms(self) -> None:
        from clients.attio import _linkedin_url_variants

        variants = _linkedin_url_variants("https://www.linkedin.com/in/foo")
        assert "https://linkedin.com/in/foo" in variants
        assert "https://www.linkedin.com/in/foo" in variants
        assert "https://www.linkedin.com/in/foo/" in variants
        # De-duplicated
        assert len(variants) == len(set(variants))

    def test_search_tries_canonical_first_then_variants(self, attio: AttioClient) -> None:
        """When Attio stored the www-prefixed form, a no-www search must still find it."""
        stored = {"id": {"record_id": "rec-1"}, "values": {"linkedin": [{"value": "https://www.linkedin.com/in/foo"}]}}
        calls: list[dict] = []

        def fake_request(method, path, json=None, **_):
            calls.append({"method": method, "path": path, "filter": json.get("filter") if json else None})
            f = (json or {}).get("filter", {})
            if f.get("linkedin") == "https://www.linkedin.com/in/foo":
                return {"data": [stored]}
            return {"data": []}

        with patch.object(attio, "_request", side_effect=fake_request):
            # Input is URL-encoded + no-www — neither matches Attio directly.
            result = attio.search_person_by_linkedin("https://linkedin.com/in/foo")
        assert result is stored
        # First variant tried is canonical (no-www), then with-www
        filters = [c["filter"]["linkedin"] for c in calls if c["filter"]]
        assert filters[0] == "https://linkedin.com/in/foo"
        assert "https://www.linkedin.com/in/foo" in filters


class TestUpsertPersonLinkedInDedup:
    """upsert_person must NOT create a duplicate when URL differs only in encoding."""

    def test_upsert_finds_existing_despite_www_mismatch(self, attio: AttioClient) -> None:
        stored = {"id": {"record_id": "rec-existing"}, "values": {"linkedin": [{"value": "https://www.linkedin.com/in/bar"}]}}

        def fake_request(method, path, json=None, params=None, **_):
            if method == "POST" and path == "/objects/people/records/query":
                f = (json or {}).get("filter", {})
                target = f.get("linkedin", "")
                return {"data": [stored] if target in ("https://www.linkedin.com/in/bar", "https://linkedin.com/in/bar") else []}
            if method == "PATCH":
                return {"data": stored}
            raise AssertionError(f"Unexpected {method} {path}")

        with patch.object(attio, "_request", side_effect=fake_request):
            result = attio.upsert_person(
                matching_attribute="linkedin",
                attributes={"linkedin": "https://www.linkedin.com/in/bar/", "name": [{"full_name": "Bar"}]},
            )
        assert result is stored

    def test_add_list_entry_upserts_when_record_already_has_entry(self, attio: AttioClient) -> None:
        """If the record already has a list entry, add_list_entry PATCHes
        instead of POSTing a fresh duplicate. Prevents the 243-record
        list-entry-accumulation bug we hit on 2026-04-21."""
        existing_entry = {
            "id": {"entry_id": "entry-existing", "list_id": "list-X"},
            "parent_record_id": "rec-1",
            "entry_values": {"stage": [{"status": {"title": "Prospect"}}]},
            "created_at": "2026-04-01T00:00:00Z",
        }

        def fake_request(method, path, json=None, **_):
            if method == "POST" and path.endswith("/entries/query"):
                return {"data": [existing_entry]}
            if method == "PATCH" and path == "/lists/list-X/entries/entry-existing":
                return {"data": {"id": {"entry_id": "entry-existing"}, "values": json}}
            raise AssertionError(f"Unexpected call: {method} {path}")

        with patch.object(attio, "_request", side_effect=fake_request):
            result = attio.add_list_entry(
                record_id="rec-1",
                stage_name="Connection Sent",
                entry_attributes={"persona": "operations_leaders"},
                list_id="list-X",
            )
        assert result["id"]["entry_id"] == "entry-existing"

    def test_add_list_entry_creates_new_when_record_has_no_entry(self, attio: AttioClient) -> None:
        """No existing entry for this record → POST a fresh entry."""
        def fake_request(method, path, json=None, **_):
            if method == "POST" and path.endswith("/entries/query"):
                return {"data": []}  # record has no entry in this list yet
            if method == "POST" and path == "/lists/list-X/entries":
                return {"data": {"id": {"entry_id": "entry-new"}}}
            raise AssertionError(f"Unexpected call: {method} {path}")

        with patch.object(attio, "_request", side_effect=fake_request):
            result = attio.add_list_entry(
                record_id="rec-brand-new",
                stage_name="Prospect",
                list_id="list-X",
            )
        assert result["id"]["entry_id"] == "entry-new"

    def test_add_list_entry_picks_most_advanced_when_multiple_exist(self, attio: AttioClient) -> None:
        """Legacy records with multiple list entries: upsert patches the
        most-advanced one (highest stage rank)."""
        prospect = {
            "id": {"entry_id": "entry-prospect", "list_id": "list-X"},
            "parent_record_id": "rec-1",
            "entry_values": {"stage": [{"status": {"title": "Prospect"}}]},
            "created_at": "2026-04-01T00:00:00Z",
        }
        dm1 = {
            "id": {"entry_id": "entry-dm1", "list_id": "list-X"},
            "parent_record_id": "rec-1",
            "entry_values": {"stage": [{"status": {"title": "DM1 Sent"}}]},
            "created_at": "2026-04-05T00:00:00Z",
        }
        patched: dict = {}

        def fake_request(method, path, json=None, **_):
            if method == "POST" and path.endswith("/entries/query"):
                return {"data": [prospect, dm1]}
            if method == "PATCH":
                patched["path"] = path
                return {"data": {"id": {"entry_id": path.rsplit("/", 1)[-1]}}}
            raise AssertionError(f"Unexpected call: {method} {path}")

        with patch.object(attio, "_request", side_effect=fake_request):
            result = attio.add_list_entry(
                record_id="rec-1",
                stage_name="DM2 Sent",
                list_id="list-X",
            )
        assert patched["path"].endswith("/entries/entry-dm1")
        assert result["id"]["entry_id"] == "entry-dm1"

    def test_add_list_entry_skips_query_when_existing_entries_supplied(self, attio: AttioClient) -> None:
        """When the caller passes pre-loaded `existing_entries`, add_list_entry
        must NOT trigger a fresh `query_list_entries(limit=50000)` scan. Without
        this kwarg, batch flows like weekly_prospect.py issue one full-list
        scan per qualified prospect — guaranteed Attio 429 throttling at scale.
        """
        existing_entry = {
            "id": {"entry_id": "entry-pre", "list_id": "list-X"},
            "parent_record_id": "rec-1",
            "entry_values": {"stage": [{"status": {"title": "Prospect"}}]},
            "created_at": "2026-04-01T00:00:00Z",
        }

        def fake_request(method, path, json=None, **_):
            # The /entries/query endpoint must NOT be hit — that's the whole point.
            if method == "POST" and path.endswith("/entries/query"):
                raise AssertionError("query_list_entries was called despite existing_entries kwarg")
            if method == "PATCH" and path == "/lists/list-X/entries/entry-pre":
                return {"data": {"id": {"entry_id": "entry-pre"}, "values": json}}
            raise AssertionError(f"Unexpected call: {method} {path}")

        with patch.object(attio, "_request", side_effect=fake_request):
            result = attio.add_list_entry(
                record_id="rec-1",
                stage_name="Connection Sent",
                list_id="list-X",
                existing_entries=[existing_entry],
            )
        assert result["id"]["entry_id"] == "entry-pre"

    def test_add_list_entry_existing_entries_filters_by_record_id(self, attio: AttioClient) -> None:
        """Pre-loaded entries can include OTHER records' entries — add_list_entry
        must filter to the target record_id and POST a fresh entry if none match.
        """
        unrelated = {
            "id": {"entry_id": "entry-other", "list_id": "list-X"},
            "parent_record_id": "rec-OTHER",
            "entry_values": {"stage": [{"status": {"title": "Prospect"}}]},
            "created_at": "2026-04-01T00:00:00Z",
        }

        def fake_request(method, path, json=None, **_):
            if method == "POST" and path.endswith("/entries/query"):
                raise AssertionError("query_list_entries should not be called")
            if method == "POST" and path == "/lists/list-X/entries":
                return {"data": {"id": {"entry_id": "entry-fresh"}}}
            raise AssertionError(f"Unexpected call: {method} {path}")

        with patch.object(attio, "_request", side_effect=fake_request):
            result = attio.add_list_entry(
                record_id="rec-1",
                stage_name="Prospect",
                list_id="list-X",
                existing_entries=[unrelated],
            )
        assert result["id"]["entry_id"] == "entry-fresh"

    # ── Fix 3: defense-in-depth stage-regression backstop ──────────────────
    # The weekly re-stamp class PATCHed an existing entry with a lower stage
    # (Accepted/Prospect over DM3 Sent), wiping cadence depth. add_list_entry
    # now drops a regressing `stage` (and `dm_step`) key on the existing-entry
    # PATCH while letting the other attrs through, and logs loudly.

    @staticmethod
    def _existing_entry(stage_title: str) -> dict:
        return {
            "id": {"entry_id": "entry-existing", "list_id": "list-X"},
            "parent_record_id": "rec-1",
            "entry_values": {"stage": [{"status": {"title": stage_title}}]},
            "created_at": "2026-04-01T00:00:00Z",
        }

    def test_add_list_entry_drops_regressing_stage_keeps_other_attrs(
        self, attio: AttioClient, caplog
    ) -> None:
        existing = self._existing_entry("DM3 Sent")
        patched: dict = {}

        def fake_request(method, path, json=None, **_):
            if method == "POST" and path.endswith("/entries/query"):
                return {"data": [existing]}
            if method == "PATCH" and path == "/lists/list-X/entries/entry-existing":
                patched["values"] = json["data"]["entry_values"]
                return {"data": {"id": {"entry_id": "entry-existing"}}}
            raise AssertionError(f"Unexpected call: {method} {path}")

        with patch.object(attio, "_request", side_effect=fake_request), \
                caplog.at_level("WARNING"):
            attio.add_list_entry(
                record_id="rec-1",
                stage_name="Accepted",  # rank 2 < DM3 Sent rank 5 → regression
                entry_attributes={"dm_step": 0, "persona": "operations_leaders"},
                list_id="list-X",
            )

        # stage AND dm_step stripped together (the cadence-depth pair) so the
        # backstop can't manufacture the stage=DM3/dm_step=0 corruption this
        # guard fixes; non-cadence attrs preserved.
        assert "stage" not in patched["values"]
        assert "dm_step" not in patched["values"]
        assert patched["values"]["persona"] == "operations_leaders"
        # The neutralized regression is observable in aggregate, not just logs.
        assert attio.stage_regressions_blocked == 1
        # Loud, structured warning with the diagnostic fields.
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "entry-existing" in joined
        assert "DM3 Sent" in joined
        assert "Accepted" in joined

    def test_add_list_entry_allows_forward_stage_transition(
        self, attio: AttioClient
    ) -> None:
        existing = self._existing_entry("Accepted")  # rank 2
        patched: dict = {}

        def fake_request(method, path, json=None, **_):
            if method == "POST" and path.endswith("/entries/query"):
                return {"data": [existing]}
            if method == "PATCH" and path == "/lists/list-X/entries/entry-existing":
                patched["values"] = json["data"]["entry_values"]
                return {"data": {"id": {"entry_id": "entry-existing"}}}
            raise AssertionError(f"Unexpected call: {method} {path}")

        with patch.object(attio, "_request", side_effect=fake_request):
            attio.add_list_entry(
                record_id="rec-1",
                stage_name="DM1 Sent",  # rank 3 > Accepted rank 2 → forward
                list_id="list-X",
            )
        assert patched["values"]["stage"] == "DM1 Sent"

    def test_add_list_entry_keeps_equal_rank_stage(
        self, attio: AttioClient
    ) -> None:
        existing = self._existing_entry("DM1 Sent")
        patched: dict = {}

        def fake_request(method, path, json=None, **_):
            if method == "POST" and path.endswith("/entries/query"):
                return {"data": [existing]}
            if method == "PATCH" and path == "/lists/list-X/entries/entry-existing":
                patched["values"] = json["data"]["entry_values"]
                return {"data": {"id": {"entry_id": "entry-existing"}}}
            raise AssertionError(f"Unexpected call: {method} {path}")

        with patch.object(attio, "_request", side_effect=fake_request):
            attio.add_list_entry(
                record_id="rec-1",
                stage_name="DM1 Sent",  # equal rank → not a regression, kept
                list_id="list-X",
            )
        assert patched["values"]["stage"] == "DM1 Sent"

    def test_add_list_entry_no_stage_attr_unaffected(
        self, attio: AttioClient
    ) -> None:
        """A PATCH with no stage key (attrs-only) is untouched by the guard."""
        existing = self._existing_entry("DM3 Sent")
        patched: dict = {}

        def fake_request(method, path, json=None, **_):
            if method == "POST" and path.endswith("/entries/query"):
                return {"data": [existing]}
            if method == "PATCH" and path == "/lists/list-X/entries/entry-existing":
                patched["values"] = json["data"]["entry_values"]
                return {"data": {"id": {"entry_id": "entry-existing"}}}
            raise AssertionError(f"Unexpected call: {method} {path}")

        with patch.object(attio, "_request", side_effect=fake_request):
            attio.add_list_entry(
                record_id="rec-1",
                stage_name="",  # no stage written into attrs
                entry_attributes={"last_contact_date": "2026-06-24"},
                list_id="list-X",
            )
        assert patched["values"]["last_contact_date"] == "2026-06-24"
        assert "stage" not in patched["values"]

    def test_upsert_canonicalizes_linkedin_before_storing(self, attio: AttioClient) -> None:
        """New records get the canonical URL written to Attio, not the raw input."""
        captured: dict = {}

        def fake_request(method, path, json=None, **_):
            if method == "POST" and path == "/objects/people/records/query":
                return {"data": []}  # no existing record
            if method == "POST" and path == "/objects/people/records":
                captured["body"] = json
                return {"data": {"id": {"record_id": "rec-new"}}}
            raise AssertionError(f"Unexpected {method} {path}")

        with patch.object(attio, "_request", side_effect=fake_request):
            attio.upsert_person(
                matching_attribute="linkedin",
                attributes={"linkedin": "https://www.linkedin.com/in/baz/"},
            )
        stored_url = captured["body"]["data"]["values"]["linkedin"]
        assert stored_url == "https://linkedin.com/in/baz"


class TestBulkFetchPersonsByRecordIds:
    """Bulk preload calls get_person in parallel for the exact record set we
    need, vs. scanning the full workspace via search_people (which scales with
    total people in Attio and routinely takes minutes once a workspace exceeds
    ~10k records). Each call still flows through `_request` so 429 retry +
    backoff applies per-record."""

    def test_returns_only_requested_record_ids(self, attio: AttioClient) -> None:
        people_by_id = {
            "rec-1": {"id": {"record_id": "rec-1"}, "values": {"name": [{"full_name": "A"}]}},
            "rec-2": {"id": {"record_id": "rec-2"}, "values": {"name": [{"full_name": "B"}]}},
            "rec-3": {"id": {"record_id": "rec-3"}, "values": {"name": [{"full_name": "C"}]}},
        }
        with patch.object(attio, "get_person", side_effect=lambda rid: people_by_id.get(rid)) as mock_get:
            result = attio.bulk_fetch_persons_by_record_ids({"rec-1", "rec-3"})

        # Each requested id triggers one get_person call.
        assert sorted(c.args[0] for c in mock_get.call_args_list) == ["rec-1", "rec-3"]
        assert set(result.keys()) == {"rec-1", "rec-3"}
        assert result["rec-1"]["values"]["name"][0]["full_name"] == "A"
        assert result["rec-3"]["values"]["name"][0]["full_name"] == "C"

    def test_empty_record_ids_returns_empty_without_calling_attio(self, attio: AttioClient) -> None:
        """No records to fetch → no API call."""
        with patch.object(attio, "get_person") as mock_get:
            result = attio.bulk_fetch_persons_by_record_ids(set())

        assert result == {}
        mock_get.assert_not_called()

    def test_missing_record_in_attio_is_silently_dropped(self, attio: AttioClient) -> None:
        """If get_person returns None for a record (404 / deleted), the dict
        simply omits it. Phase code falls back to per-record GETs for cache
        misses, so a missing record self-heals."""
        def fake_get(rid: str):
            return {"id": {"record_id": "rec-1"}, "values": {}} if rid == "rec-1" else None

        with patch.object(attio, "get_person", side_effect=fake_get):
            result = attio.bulk_fetch_persons_by_record_ids({"rec-1", "rec-missing"})

        assert "rec-1" in result
        assert "rec-missing" not in result
