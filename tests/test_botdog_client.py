"""Tests for clients/botdog.py — the OPTIONAL Botdog delivery transport.

Follows the tests/test_phantombuster_typed.py pattern: public methods are
exercised with `patch.object(client, "_request")`; `_request` itself is
exercised at the httpx layer with real `httpx.Response` objects driven
through a patched `client._client.request`. Covers:

- add_leads_to_campaign: payload shape, >100 batch ValueError (never
  auto-split), missing-linkedinUrl ValueError, per-lead result extraction
  with the raw payload retained.
- message DTO + text validation (empty / whitespace / unpaired surrogate /
  UTF-16 length cap).
- pagination: the cursor shape is FOLLOWED, every other marker fails loud,
  and truncation is never silent.
- _request: 429-then-success retry honoring Retry-After, fallback schedule
  when the header is absent, BotdogRateLimited on schedule exhaustion,
  non-429 errors raising BotdogError (409 -> BotdogLeadConflict)
  immediately with status + scrubbed snippet, and typed-but-never-retried
  transport/decode failures.
- missing BOTDOG_API_KEY raising at construction.
- The operator-config-backed identity accessors: `BotdogConfig.campaign_id`
  and `clients.botdog.blacklist_name()`.

Config-backed assertions resolve the bundled synthetic reference operator
at examples/acme/config/ (pinned suite-wide by tests/conftest.py via
OUTBOUND_CONFIG_DIR).
"""

from __future__ import annotations

import textwrap
from unittest.mock import patch

import httpx
import pytest

from clients.botdog import (
    MAX_LEADS_PER_BATCH,
    MAX_PAGES,
    BotdogBatchResult,
    BotdogClient,
    BotdogError,
    BotdogInvalidMessage,
    BotdogLeadConflict,
    BotdogRateLimited,
    blacklist_name,
)
from clients.botdog_config import DEFAULT_BLACKLIST_NAME, load_botdog_config


@pytest.fixture(autouse=True)
def _stub_botdog_api_key(monkeypatch):
    monkeypatch.setenv("BOTDOG_API_KEY", "bd_test_key_unused")


@pytest.fixture
def client():
    return BotdogClient(api_key="bd_test_key")


def _response(
    status: int, body: dict | list | None = None, headers: dict | None = None
) -> httpx.Response:
    req = httpx.Request("POST", "https://api.botdog.co/v1/x")
    if body is None:
        return httpx.Response(status, request=req, headers=headers)
    return httpx.Response(status, request=req, json=body, headers=headers)


class TestConstruction:
    def test_missing_api_key_raises(self, monkeypatch) -> None:
        """No arg + no env var must fail loud at construction, not at
        first request."""
        monkeypatch.delenv("BOTDOG_API_KEY", raising=False)
        with pytest.raises(KeyError):
            BotdogClient()

    def test_env_fallback_and_auth_header(self, monkeypatch) -> None:
        monkeypatch.setenv("BOTDOG_API_KEY", "bd_test_from_env")
        c = BotdogClient()
        assert c.api_key == "bd_test_from_env"
        assert c._client.headers["x-api-key"] == "bd_test_from_env"


class TestAddLeadsToCampaign:
    def test_posts_campaign_and_leads(self, client) -> None:
        leads = [
            {
                "linkedinUrl": "https://linkedin.com/in/acme-alice",
                "name": "Alice Acme",
                "customAttributes": {"inviteMessage": "hola"},
            },
        ]
        with patch.object(client, "_request") as mock_req:
            mock_req.return_value = {
                "results": [{"linkedinUrl": "a", "status": "created"}]
            }
            result = client.add_leads_to_campaign("cmp_1", leads)
            mock_req.assert_called_once_with(
                "POST",
                "/leads/add_to_campaign",
                json={"campaignId": "cmp_1", "leads": leads},
            )
        assert isinstance(result, BotdogBatchResult)
        assert result.lead_results == (
            {"linkedinUrl": "a", "status": "created"},
        )
        # Raw payload stays accessible for defensive callers.
        assert result.raw == {
            "results": [{"linkedinUrl": "a", "status": "created"}]
        }

    def test_oversized_batch_raises_without_calling_api(self, client) -> None:
        """>100 leads must ValueError — the caller splits; the client
        NEVER auto-splits into extra (unreviewed) requests."""
        leads = [
            {"linkedinUrl": f"https://linkedin.com/in/acme-p{i}"}
            for i in range(MAX_LEADS_PER_BATCH + 1)
        ]
        with patch.object(client, "_request") as mock_req:
            with pytest.raises(ValueError, match="exceeds the"):
                client.add_leads_to_campaign("cmp_1", leads)
            mock_req.assert_not_called()

    def test_batch_of_exactly_100_allowed(self, client) -> None:
        leads = [
            {"linkedinUrl": f"https://linkedin.com/in/acme-p{i}"}
            for i in range(MAX_LEADS_PER_BATCH)
        ]
        with patch.object(client, "_request") as mock_req:
            mock_req.return_value = {}
            client.add_leads_to_campaign("cmp_1", leads)
            mock_req.assert_called_once()

    def test_missing_linkedin_url_raises(self, client) -> None:
        """linkedinUrl is the LeadCreateDto's one required field —
        catch its absence before the wire, not as a per-lead API 4xx."""
        leads = [
            {"linkedinUrl": "https://linkedin.com/in/acme-ok"},
            {"name": "no url"},
        ]
        with patch.object(client, "_request") as mock_req:
            with pytest.raises(ValueError, match="linkedinUrl"):
                client.add_leads_to_campaign("cmp_1", leads)
            mock_req.assert_not_called()

    def test_unrecognized_envelope_yields_empty_results_raw_kept(
        self, client
    ) -> None:
        """Unknown response shape → lead_results empty, raw retained
        (never invent a schema; caller can inspect raw)."""
        with patch.object(client, "_request") as mock_req:
            mock_req.return_value = {"weird": {"nested": True}}
            result = client.add_leads_to_campaign(
                "cmp_1", [{"linkedinUrl": "https://linkedin.com/in/acme-a"}]
            )
        assert result.lead_results == ()
        assert result.raw == {"weird": {"nested": True}}


class TestLeadReads:
    def test_get_leads_passes_filters_as_params(self, client) -> None:
        with patch.object(client, "_request") as mock_req:
            mock_req.return_value = {"leads": [{"id": "l_1"}]}
            rows = client.get_leads(
                linkedinUrl="https://linkedin.com/in/acme-alice"
            )
            mock_req.assert_called_once_with(
                "GET",
                "/leads",
                params={
                    "linkedinUrl": "https://linkedin.com/in/acme-alice"
                },
            )
        assert rows == [{"id": "l_1"}]

    def test_get_leads_follows_nextcursor_shape(self, client) -> None:
        """The pinned paging shape: `{"data": [...], "nextCursor":
        "<token>"}`, next page fetched as `?cursor=<token>`."""
        with patch.object(client, "_request") as mock_req:
            mock_req.side_effect = [
                {"data": [{"id": "l_1"}], "nextCursor": "cur_2"},
                {"data": [{"id": "l_2"}], "nextCursor": None},
            ]
            rows = client.get_leads(campaignId="cmp_1")
        assert [r["id"] for r in rows] == ["l_1", "l_2"]
        assert [c.kwargs["params"] for c in mock_req.call_args_list] == [
            {"campaignId": "cmp_1"},
            {"campaignId": "cmp_1", "cursor": "cur_2"},
        ]

    def test_get_leads_accepts_bare_list_response(self, client) -> None:
        with patch.object(client, "_request") as mock_req:
            # _request wraps bare arrays as {"data": [...]}.
            mock_req.return_value = {"data": [{"id": "l_1"}, {"id": "l_2"}]}
            assert len(client.get_leads()) == 2

    def test_get_lead_hits_detail_path(self, client) -> None:
        with patch.object(client, "_request") as mock_req:
            mock_req.return_value = {"id": "l_1", "events": []}
            lead = client.get_lead("l_1")
            mock_req.assert_called_once_with("GET", "/leads/l_1")
        assert lead["id"] == "l_1"


class TestMessaging:
    # DTO contract: see BotdogClient.send_message's docstring. These tests
    # pin the EXACT body — the API rejects any property outside
    # `leadId` + `text`, and sending the copy under the wrong key 400s
    # every DM in the batch.
    def test_send_message_builds_exact_documented_body(self, client) -> None:
        with patch.object(client, "_request") as mock_req:
            mock_req.return_value = {"id": "m_1"}
            client.send_message(text="hola", lead_id="l_1")
            mock_req.assert_called_once_with(
                "POST", "/messages", json={"leadId": "l_1", "text": "hola"}
            )

    def test_send_message_empty_text_raises_without_calling_api(
        self, client
    ) -> None:
        with patch.object(client, "_request") as mock_req:
            with pytest.raises(BotdogInvalidMessage, match="empty"):
                client.send_message(text="", lead_id="l_1")
            mock_req.assert_not_called()

    def test_send_message_whitespace_only_raises_without_calling_api(
        self, client
    ) -> None:
        # Must match the batch guard's blank definition (.strip()) so the
        # transport really is a backstop for whitespace-only renders.
        with patch.object(client, "_request") as mock_req:
            with pytest.raises(BotdogInvalidMessage, match="empty"):
                client.send_message(text="  \n\t ", lead_id="l_1")
            mock_req.assert_not_called()

    def test_send_message_unpaired_surrogate_raises_invalid_message(
        self, client
    ) -> None:
        # A lone surrogate (reachable via JSON escapes: '"\\ud83d"' parses
        # under the stdlib json module) cannot encode to UTF-16. It must
        # surface as BotdogInvalidMessage — never a raw UnicodeEncodeError,
        # which would escape send_dm's never-raise contract and abort the
        # whole batch loop.
        with patch.object(client, "_request") as mock_req:
            with pytest.raises(BotdogInvalidMessage, match="surrogate"):
                client.send_message(text="hola \ud83d", lead_id="l_1")
            mock_req.assert_not_called()

    def test_send_message_oversized_text_raises_without_calling_api(
        self, client
    ) -> None:
        with patch.object(client, "_request") as mock_req:
            with pytest.raises(BotdogInvalidMessage, match="8000"):
                client.send_message(text="x" * 8001, lead_id="l_1")
            mock_req.assert_not_called()

    def test_send_message_at_8000_chars_sends_exact_body(self, client) -> None:
        text = "x" * 8000
        with patch.object(client, "_request") as mock_req:
            mock_req.return_value = {}
            client.send_message(text=text, lead_id="l_1")
            mock_req.assert_called_once_with(
                "POST", "/messages", json={"leadId": "l_1", "text": text}
            )

    def test_send_message_counts_utf16_units_like_the_api(self, client) -> None:
        # The API's MaxLength counts UTF-16 code units, so an astral-plane
        # char (emoji) counts as 2: 4000 emoji fit exactly, 4000 emoji + 1
        # ASCII char is 8001 units and must not go out even though len()
        # says 4001.
        emoji = "\U0001f600"
        with patch.object(client, "_request") as mock_req:
            mock_req.return_value = {}
            client.send_message(text=emoji * 4000, lead_id="l_1")
            mock_req.assert_called_once()
        with patch.object(client, "_request") as mock_req:
            with pytest.raises(BotdogInvalidMessage, match="8001"):
                client.send_message(text=emoji * 4000 + "x", lead_id="l_1")
            mock_req.assert_not_called()

    def test_reply_hits_conversation_path_with_text_body(self, client) -> None:
        with patch.object(client, "_request") as mock_req:
            mock_req.return_value = {}
            client.reply("cv_9", "gracias")
            mock_req.assert_called_once_with(
                "POST", "/conversations/cv_9/reply", json={"text": "gracias"}
            )

    def test_reply_empty_text_raises_without_calling_api(self, client) -> None:
        with patch.object(client, "_request") as mock_req:
            with pytest.raises(BotdogInvalidMessage, match="empty"):
                client.reply("cv_9", "")
            mock_req.assert_not_called()


class TestBlacklist:
    def test_add_to_blacklist_hits_collection_leads_path(self, client) -> None:
        leads = [{"linkedinUrl": "https://linkedin.com/in/acme-never"}]
        with patch.object(client, "_request") as mock_req:
            mock_req.return_value = {}
            client.add_to_blacklist("bl_1", leads)
            mock_req.assert_called_once_with(
                "POST", "/blacklist/bl_1/leads", json={"leads": leads}
            )

    def test_get_blacklists_and_create(self, client) -> None:
        with patch.object(client, "_request") as mock_req:
            # Collection list envelope: `{"data": [...], "nextCursor": ...}`,
            # paginated like /leads.
            mock_req.return_value = {
                "data": [{"id": "bl_1", "leadCount": 3}],
                "nextCursor": None,
            }
            assert client.get_blacklists() == [{"id": "bl_1", "leadCount": 3}]
            mock_req.assert_called_with("GET", "/blacklist", params={})
            mock_req.return_value = {"id": "bl_2"}
            client.create_blacklist("never-touch")
            mock_req.assert_called_with(
                "POST", "/blacklist", json={"name": "never-touch"}
            )

    def test_get_blacklists_follows_cursor(self, client) -> None:
        """The collection list is cursor-paginated — every page is read,
        never a silent page 1."""
        with patch.object(client, "_request") as mock_req:
            mock_req.side_effect = [
                {"data": [{"id": "bl_1"}], "nextCursor": "c2"},
                {"data": [{"id": "bl_2"}], "nextCursor": None},
            ]
            assert client.get_blacklists() == [{"id": "bl_1"}, {"id": "bl_2"}]
            assert mock_req.call_count == 2

    def test_get_blacklist_leads_paginates_the_dedicated_endpoint(
        self, client
    ) -> None:
        """GET /v1/blacklist/{id}/leads reads every page and returns the
        raw entries (`linkedinProfile`-keyed) — `GET /v1/blacklist` embeds
        no entries at all."""
        with patch.object(client, "_request") as mock_req:
            mock_req.side_effect = [
                {
                    "data": [{"id": "l_1", "linkedinProfile": "u/a"}],
                    "nextCursor": "c2",
                },
                {
                    "data": [{"id": "l_2", "linkedinProfile": "u/b"}],
                    "nextCursor": None,
                },
            ]
            rows = client.get_blacklist_leads("bl_1")
            assert [r["id"] for r in rows] == ["l_1", "l_2"]
            first = mock_req.call_args_list[0]
            assert first.args == ("GET", "/blacklist/bl_1/leads")
            # Reads at the max page size — a real never-contact set
            # (thousands of rows) overflows the default 25/page x 40 = 1000
            # ceiling.
            assert first.kwargs["params"] == {"limit": 100}

    def test_get_blacklist_leads_reads_the_whole_large_set(
        self, client
    ) -> None:
        """A ~1500-row set is 15 pages at limit=100 — comfortably inside
        the lifted BLACKLIST_MAX_PAGES cap, so the read completes instead
        of raising the truncation guard."""
        from clients.botdog import BLACKLIST_PAGE_SIZE

        pages = []
        total = 1462
        idx = 0
        while idx < total:
            n = min(BLACKLIST_PAGE_SIZE, total - idx)
            nxt = f"c{idx + n}" if idx + n < total else None
            pages.append({
                "data": [{"id": f"l_{idx + j}"} for j in range(n)],
                "nextCursor": nxt,
            })
            idx += n
        with patch.object(client, "_request") as mock_req:
            mock_req.side_effect = pages
            rows = client.get_blacklist_leads("bl_1")
            assert len(rows) == total


class TestBlacklistSelection:
    """`select_blacklist` / `collection_lead_count` — the duplicate-safe
    collection picker shared by the seed script and the pre-send gate."""

    def test_selects_populated_over_empty_duplicate(self) -> None:
        from clients.botdog import select_blacklist

        cols = [
            {"id": "empty", "name": "L", "leadCount": 0},
            {"id": "full", "name": "L", "leadCount": 1462},
        ]
        assert select_blacklist(cols, "L")["id"] == "full"
        # Order must not matter — the empty duplicate can arrive first.
        assert select_blacklist(list(reversed(cols)), "L")["id"] == "full"

    def test_name_match_is_case_and_whitespace_insensitive(self) -> None:
        from clients.botdog import select_blacklist

        cols = [{"id": "x", "name": "  MY List  ", "leadCount": 5}]
        assert select_blacklist(cols, "my list")["id"] == "x"

    def test_returns_none_when_no_name_matches(self) -> None:
        from clients.botdog import select_blacklist

        assert select_blacklist([{"id": "x", "name": "other"}], "L") is None

    def test_lone_unknown_count_is_still_selected(self) -> None:
        from clients.botdog import collection_lead_count, select_blacklist

        cols = [{"id": "x", "name": "L"}]  # no count field at all
        assert select_blacklist(cols, "L")["id"] == "x"
        assert collection_lead_count(cols[0]) is None

    def test_lead_count_reads_leadcount_then_embedded_leads(self) -> None:
        from clients.botdog import collection_lead_count

        assert collection_lead_count({"leadCount": 7}) == 7
        assert collection_lead_count({"leads": [{}, {}]}) == 2
        assert collection_lead_count({"leadCount": True}) is None  # not a bool


class TestAccountsAndCampaigns:
    def test_account_limits_roundtrip(self, client) -> None:
        with patch.object(client, "_request") as mock_req:
            mock_req.return_value = {"dailyInvites": 20}
            assert client.get_account_limits("acc_1") == {"dailyInvites": 20}
            mock_req.assert_called_with("GET", "/accounts/acc_1/limits")
            client.set_account_limits("acc_1", {"dailyInvites": 15})
            mock_req.assert_called_with(
                "PATCH", "/accounts/acc_1/limits", json={"dailyInvites": 15}
            )

    def test_get_accounts_campaigns_and_campaign_leads(self, client) -> None:
        with patch.object(client, "_request") as mock_req:
            mock_req.return_value = {"accounts": [{"id": "acc_1"}]}
            assert client.get_accounts() == [{"id": "acc_1"}]
            mock_req.return_value = {"campaigns": [{"id": "cmp_1"}]}
            assert client.get_campaigns() == [{"id": "cmp_1"}]
            mock_req.return_value = {"leads": [{"id": "l_1"}]}
            assert client.get_campaign_leads("cmp_1") == [{"id": "l_1"}]
            # Paginated path: params ride along (empty on page 1).
            mock_req.assert_called_with(
                "GET", "/campaigns/cmp_1/leads", params={}
            )

    def test_health(self, client) -> None:
        with patch.object(client, "_request") as mock_req:
            mock_req.return_value = {"status": "ok"}
            assert client.health() == {"status": "ok"}
            mock_req.assert_called_once_with("GET", "/health")


class TestRequestRetryAndErrors:
    """Lower-level: drive the real `_request` through a patched
    `client._client.request` with real httpx.Response objects."""

    def test_429_then_success_honors_retry_after(self, client) -> None:
        with patch.object(client._client, "request") as mock_http, \
             patch("clients.botdog.time.sleep") as mock_sleep:
            mock_http.side_effect = [
                _response(429, {"error": "rate limited"},
                          headers={"Retry-After": "7"}),
                _response(200, {"ok": True}),
            ]
            data = client._request("GET", "/leads")
        assert data == {"ok": True}
        assert mock_http.call_count == 2
        # The Retry-After value (7s), NOT the fallback schedule's 5s.
        mock_sleep.assert_called_once_with(7.0)

    def test_429_without_retry_after_uses_fallback_schedule(
        self, client
    ) -> None:
        with patch.object(client._client, "request") as mock_http, \
             patch("clients.botdog.time.sleep") as mock_sleep:
            mock_http.side_effect = [
                _response(429, {"error": "rate limited"}),
                _response(200, {"ok": True}),
            ]
            client._request("GET", "/leads")
        mock_sleep.assert_called_once_with(
            float(BotdogClient._RATE_LIMIT_BACKOFF_SCHEDULE[0])
        )

    def test_persistent_429_raises_rate_limited_after_schedule(
        self, client
    ) -> None:
        """Backoff is BOUNDED: exhausting the schedule raises the typed
        BotdogRateLimited, never an infinite retry loop."""
        schedule_len = len(BotdogClient._RATE_LIMIT_BACKOFF_SCHEDULE)
        with patch.object(client._client, "request") as mock_http, \
             patch("clients.botdog.time.sleep"):
            mock_http.return_value = _response(
                429, {"error": "rate limited"}, headers={"Retry-After": "3"}
            )
            with pytest.raises(BotdogRateLimited) as exc:
                client._request("POST", "/leads/add_to_campaign")
        assert mock_http.call_count == schedule_len + 1
        assert exc.value.status_code == 429
        assert exc.value.retry_after == 3.0

    def test_retry_after_capped(self, client) -> None:
        """A pathological Retry-After must not stall the run — waits
        are capped at _RETRY_AFTER_CAP_SECONDS."""
        with patch.object(client._client, "request") as mock_http, \
             patch("clients.botdog.time.sleep") as mock_sleep:
            mock_http.side_effect = [
                _response(429, {}, headers={"Retry-After": "99999"}),
                _response(200, {"ok": True}),
            ]
            client._request("GET", "/leads")
        mock_sleep.assert_called_once_with(
            BotdogClient._RETRY_AFTER_CAP_SECONDS
        )

    def test_non_429_error_raises_immediately_with_status(self, client) -> None:
        """Non-429 HTTP errors never retry — one call, typed error
        carrying the status + body snippet."""
        with patch.object(client._client, "request") as mock_http:
            mock_http.return_value = _response(500, {"error": "boom"})
            with pytest.raises(BotdogError) as exc:
                client._request("GET", "/health")
        assert mock_http.call_count == 1
        assert exc.value.status_code == 500
        assert "boom" in exc.value.body_snippet

    def test_401_raises_botdog_error(self, client) -> None:
        with patch.object(client._client, "request") as mock_http:
            mock_http.return_value = _response(401, {"error": "bad key"})
            with pytest.raises(BotdogError) as exc:
                client._request("GET", "/accounts")
        assert exc.value.status_code == 401

    def test_409_maps_to_lead_conflict(self, client) -> None:
        """409 = lead already exists → the typed conflict subclass, so
        callers can branch idempotency-skip vs hard failure."""
        with patch.object(client._client, "request") as mock_http:
            mock_http.return_value = _response(
                409, {"error": "lead already exists"}
            )
            with pytest.raises(BotdogLeadConflict) as exc:
                client._request("POST", "/leads/add_to_campaign")
        assert exc.value.status_code == 409
        # Conflict IS a BotdogError — the unified except surface holds.
        assert isinstance(exc.value, BotdogError)

    def test_error_snippet_scrubs_profile_urls(self, client) -> None:
        """Error bodies can echo lead URLs; the exception message /
        snippet must be audit-log safe (same rule as PBRunFailed)."""
        with patch.object(client._client, "request") as mock_http:
            mock_http.return_value = _response(
                422,
                {
                    "error": "invalid lead "
                             "https://www.linkedin.com/in/acme-alice"
                },
            )
            with pytest.raises(BotdogError) as exc:
                client._request("POST", "/leads/add_to_campaign")
        assert "linkedin.com/in/acme-alice" not in str(exc.value)
        assert "<profile-url>" in exc.value.body_snippet

    def test_empty_body_returns_empty_dict(self, client) -> None:
        with patch.object(client._client, "request") as mock_http:
            mock_http.return_value = _response(204)
            assert client._request("POST", "/conversations/cv_1/read") == {}

    def test_bare_list_body_wrapped_as_data(self, client) -> None:
        """A bare JSON array keeps the dict return contract."""
        with patch.object(client._client, "request") as mock_http:
            mock_http.return_value = _response(200, [{"id": "l_1"}])
            assert client._request("GET", "/leads") == {
                "data": [{"id": "l_1"}]
            }


class TestBotdogConfigIdentity:
    """`BotdogConfig.campaign_id` — the per-seat campaign accessor, with
    the same set/unset semantics as the PhantomBuster phantom-id lookup.

    The engine resolves campaign roles most-specific-first
    (``invite_<language>`` then the ``invite`` catch-all), so a role that
    is not mapped MUST answer None rather than falling through to some
    other campaign — injecting leads into the wrong campaign is a
    prospect-visible failure.
    """

    def _write_config(self, monkeypatch, tmp_path, content: str):
        (tmp_path / "botdog.yaml").write_text(
            textwrap.dedent(content), encoding="utf-8"
        )
        monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(tmp_path))
        return load_botdog_config()

    def test_set_returns_campaign_id(self, monkeypatch, tmp_path) -> None:
        config = self._write_config(monkeypatch, tmp_path, """\
            enabled: false
            campaigns:
              invite_es: "cmp_es_123"
              invite_en: "cmp_en_456"
            """)
        assert config.campaign_id("invite_es") == "cmp_es_123"
        assert config.campaign_id("invite_en") == "cmp_en_456"

    def test_unset_role_returns_none(self, monkeypatch, tmp_path) -> None:
        config = self._write_config(monkeypatch, tmp_path, """\
            enabled: false
            campaigns:
              invite_es: "cmp_es_123"
            """)
        assert config.campaign_id("invite_pt") is None

    def test_no_botdog_config_returns_none(self, monkeypatch, tmp_path) -> None:
        """An operator with no botdog YAML at all (the shipped default —
        the transport is opt-in) must still load and answer None for every
        role, never raise."""
        monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(tmp_path))
        config = load_botdog_config()
        assert config.enabled is False
        assert config.campaign_id("invite_es") is None
        assert config.campaign_ids == ()

    def test_campaign_ids_dedupes_in_config_order(
        self, monkeypatch, tmp_path
    ) -> None:
        """Several roles may point at ONE campaign; the id set a caller
        polls/reconciles against must list it once."""
        config = self._write_config(monkeypatch, tmp_path, """\
            enabled: false
            campaigns:
              invite: "cmp_shared"
              invite_es: "cmp_shared"
              invite_en: "cmp_en_456"
            """)
        assert config.campaign_ids == ("cmp_shared", "cmp_en_456")

    def test_reference_config_resolves_every_shipped_role(self) -> None:
        """The bundled synthetic reference operator (examples/acme/config,
        pinned by conftest) maps the generic `invite` catch-all PLUS the
        per-language roles — a missing catch-all is what makes an
        unmapped-language lane hard-abort mid-run. An unset role still
        answers None."""
        config = load_botdog_config()
        for role in ("invite", "invite_es", "invite_en"):
            assert config.campaign_id(role), role
        assert config.campaign_id("invite_zz") is None


class TestBlacklistName:
    """`clients.botdog.blacklist_name()` — the ONE resolver both the
    seeding script and the pre-send presence gate call.

    They must agree: if they resolved different names, the gate would pass
    on a collection the seeder never filled and a never-contact company
    could be cold-contacted.
    """

    def test_reads_the_operator_config_collection_name(self) -> None:
        assert blacklist_name() == "Acme never-contact (CRM-seeded)"

    def test_falls_back_to_default_without_a_botdog_config(
        self, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(tmp_path))
        assert blacklist_name() == DEFAULT_BLACKLIST_NAME

    def test_blank_collection_name_falls_back_to_default(
        self, monkeypatch, tmp_path
    ) -> None:
        """The name is the idempotency key — an omitted/blank one must
        resolve to the stable default, never to an empty string that would
        match no collection."""
        (tmp_path / "botdog.yaml").write_text(
            "enabled: false\nblacklist: {}\n", encoding="utf-8"
        )
        monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(tmp_path))
        assert blacklist_name() == DEFAULT_BLACKLIST_NAME


class TestTransportAndDecodeErrors:
    """Transport + decode failures are typed, and NOT retried."""

    def test_timeout_raises_botdog_error_naming_the_exception(
        self, client
    ) -> None:
        with patch.object(
            client._client, "request",
            side_effect=httpx.ReadTimeout("timed out"),
        ) as req, pytest.raises(BotdogError) as exc:
            client._request("GET", "/leads")
        assert "ReadTimeout" in str(exc.value)
        assert "transport failure" in str(exc.value)
        # NOT retried — a POST that timed out may have landed server-side.
        assert req.call_count == 1

    def test_connect_error_raises_botdog_error(self, client) -> None:
        with patch.object(
            client._client, "request",
            side_effect=httpx.ConnectError("no route"),
        ), pytest.raises(BotdogError) as exc:
            client._request("POST", "/leads/add_to_campaign", json={})
        assert "ConnectError" in str(exc.value)

    def test_transport_error_surfaces_through_public_method(
        self, client
    ) -> None:
        """Callers `except BotdogError:` — a raw httpx error must never
        escape the client."""
        with patch.object(
            client._client, "request",
            side_effect=httpx.ReadTimeout("timed out"),
        ), pytest.raises(BotdogError):
            client.get_leads()

    def test_malformed_json_on_2xx_raises_botdog_error(self, client) -> None:
        req = httpx.Request("GET", "https://api.botdog.co/v1/leads")
        bad = httpx.Response(200, request=req, content=b"<html>oops</html>")
        with (
            patch.object(client._client, "request", return_value=bad) as call,
            pytest.raises(BotdogError) as exc,
        ):
            client._request("GET", "/leads")
        assert "undecodable JSON" in str(exc.value)
        assert exc.value.status_code == 200
        assert "oops" in exc.value.body_snippet
        assert call.call_count == 1  # not retried

    def test_empty_body_still_returns_empty_dict(self, client) -> None:
        """A 204 / empty body is "no data", NOT a decode failure."""
        req = httpx.Request("DELETE", "https://api.botdog.co/v1/x")
        with patch.object(
            client._client, "request",
            return_value=httpx.Response(204, request=req),
        ):
            assert client._request("DELETE", "/x") == {}


# ── pagination: cursor shape FOLLOWED, everything else fails LOUD ─────
#
# The cursor contract is pinned — `{"data": [...25], "nextCursor":
# "<token>"}` + `?cursor=<token>` — so `get_leads` / `get_campaign_leads`
# FOLLOW it. The loud check survives for every marker whose paging
# contract is still unverified, and for single-shot callers that do not
# page at all.


class TestPaginationFollowsCursor:
    def test_follows_multiple_pages_and_accumulates(self, client) -> None:
        with patch.object(client, "_request") as mock_req:
            mock_req.side_effect = [
                {"data": [{"id": "l_1"}], "nextCursor": "c2"},
                {"data": [{"id": "l_2"}], "nextCursor": "c3"},
                {"data": [{"id": "l_3"}]},  # cursor absent → last page
            ]
            rows = client.get_leads()
        assert [r["id"] for r in rows] == ["l_1", "l_2", "l_3"]
        assert mock_req.call_count == 3

    def test_campaign_leads_paginate_too(self, client) -> None:
        """The invite idempotency pre-check reads this endpoint — a
        truncated page 1 here would re-invite existing leads."""
        with patch.object(client, "_request") as mock_req:
            mock_req.side_effect = [
                {"data": [{"id": "l_1"}], "nextCursor": "c2"},
                {"data": [{"id": "l_2"}], "nextCursor": None},
            ]
            rows = client.get_campaign_leads("cmp_1")
        assert [r["id"] for r in rows] == ["l_1", "l_2"]
        assert mock_req.call_args_list[1].kwargs["params"] == {"cursor": "c2"}

    def test_single_page_dict_extracts_cleanly(self, client) -> None:
        body = {"data": [{"id": "l_1"}], "nextCursor": None}
        with patch.object(client, "_request", return_value=body):
            assert client.get_leads() == [{"id": "l_1"}]

    def test_page_cap_raises_rather_than_truncating(self, client) -> None:
        """Hitting MAX_PAGES with a cursor still live must FAIL, never
        hand back a partial lead set that reads as complete."""
        with patch.object(client, "_request") as mock_req:
            mock_req.side_effect = [
                {"data": [{"id": f"l_{i}"}], "nextCursor": f"c{i}"}
                for i in range(MAX_PAGES + 5)
            ]
            with pytest.raises(BotdogError) as exc:
                client.get_leads()
        assert mock_req.call_count == MAX_PAGES
        assert "TRUNCATED" in str(exc.value)

    def test_repeated_cursor_raises_instead_of_looping(self, client) -> None:
        """A server that never advances the cursor would otherwise burn
        the whole rate-limit budget and return duplicated rows."""
        with patch.object(
            client,
            "_request",
            return_value={"data": [{"id": "l_1"}], "nextCursor": "same"},
        ) as mock_req, pytest.raises(BotdogError, match="repeated the same"):
            client.get_leads()
        assert mock_req.call_count == 2

    def test_link_style_cursor_raises(self, client) -> None:
        """A full-URL `next` is a link contract we do not implement —
        posting it back as `?cursor=` would silently re-read page 1."""
        body = {
            "data": [{"id": "l_1"}],
            "next": "https://api.botdog.co/v1/leads?page=2",
        }
        with patch.object(client, "_request", return_value=body), pytest.raises(
            BotdogError, match="link-style"
        ):
            client.get_leads()


class TestPaginationLoudFail:
    @pytest.mark.parametrize(
        "marker",
        [{"page": 1}, {"totalPages": 3}, {"hasMore": True}, {"total": 250}],
    )
    def test_unknown_marker_alongside_list_raises(self, client, marker) -> None:
        """Markers whose paging contract is NOT pinned still fail loud —
        following a guessed contract is how page 1 becomes "everything"."""
        body = {"leads": [{"id": "l_1"}], **marker}
        with patch.object(
            client, "_request", return_value=body
        ), pytest.raises(BotdogError) as exc:
            client.get_leads()
        assert "pagination unsupported" in str(exc.value)

    def test_cursor_on_a_non_paging_caller_still_raises(self, client) -> None:
        """`get_accounts` / `get_campaigns` do NOT page — a live cursor
        there means they are reading page 1 only."""
        body = {"accounts": [{"id": "acc_1"}], "nextCursor": "c2"}
        with patch.object(
            client, "_request", return_value=body
        ), pytest.raises(BotdogError) as exc:
            client.get_accounts()
        assert "pagination unsupported" in str(exc.value)

    def test_no_marker_extracts_normally(self, client) -> None:
        with patch.object(
            client, "_request", return_value={"leads": [{"id": "l_1"}]}
        ):
            assert client.get_leads() == [{"id": "l_1"}]

    def test_bare_list_never_raises(self, client) -> None:
        with patch.object(
            client, "_request", return_value={"data": [{"id": "l_1"}]}
        ):
            assert client.get_leads() == [{"id": "l_1"}]

    def test_exhausted_marker_is_not_a_signal(self, client) -> None:
        """`next: null` / `hasMore: false` is the API saying "last page"
        — that is complete data, not a truncation."""
        body = {"leads": [{"id": "l_1"}], "next": None, "hasMore": False}
        with patch.object(client, "_request", return_value=body):
            assert client.get_leads() == [{"id": "l_1"}]

    def test_total_matching_row_count_is_not_a_signal(self, client) -> None:
        body = {"leads": [{"id": "l_1"}, {"id": "l_2"}], "total": 2}
        with patch.object(client, "_request", return_value=body):
            assert len(client.get_leads()) == 2

    def test_campaign_leads_unknown_marker_raises(self, client) -> None:
        body = {"leads": [{"id": "l_1"}], "hasMore": True}
        with patch.object(
            client, "_request", return_value=body
        ), pytest.raises(BotdogError):
            client.get_campaign_leads("cmp_1")
