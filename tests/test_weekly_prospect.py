"""Tests for workflows/weekly_prospect.py — borderline staging via agent_gate."""

import json
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from clients.crm.attio_provider import AttioProvider
from workflows.weekly_prospect import _commit_prospect, _process_prospects

if TYPE_CHECKING:
    from clients.crm.base import Entry


def _crm(attio):
    """Wrap a raw-AttioClient mock in the reference provider (P1c migration).

    The weekly call-tree now threads a `CRMProvider`; the provider delegates
    every typed read/write to the same inner mock and normalizes its raw-dict
    returns into the contract dataclasses (`Record`/`Entry`). All existing
    assertions on the raw client methods (`attio.create_company.call_args`,
    `attio.upsert_person.assert_not_called()`, …) keep working because the
    provider calls those exact methods on the wrapped mock, and the §7 raw
    escape hatch (`match_or_create_company`) reaches it via `inner_client`.
    """
    return AttioProvider(attio)


def _make_attio_mock():
    attio = MagicMock()
    attio.search_person_by_linkedin.return_value = None
    attio.upsert_person.return_value = {"id": {"record_id": "rec-123"}}
    return attio


ENTERPRISE_PERSONA = {
    "key": "operations_leaders",
    "enterprise_mode": True,
}

# A raw CSV row that yields a borderline prospect in enterprise mode:
# Plant Manager + 200 employees → score ~43 (borderline 40-75)
BORDERLINE_RAW = {
    "fullName": "Test User",
    "title": "Plant Manager",
    "companyName": "Subsidiary",
    "location": "Mexico City, Mexico",
    "companyEmployees": "200",
    "defaultProfileUrl": "https://www.linkedin.com/in/test-user",
}


class TestBorderlineStaging:
    """Borderline prospects with agent_gate=True should be staged, not written to Attio."""

    def test_borderline_staged_not_written_to_attio(self, tmp_path):
        """A borderline prospect must be staged to borderline_stage and NOT upserted."""
        attio = _make_attio_mock()
        summary = {
            "exported": 0,
            "scored": 0,
            "qualified": 0,
            "duplicates": 0,
            "rejected": 0,
            "added": 0,
            "borderline_staged": 0,
        }
        seen_urls: set[str] = set()
        borderline_stage: list[dict] = []

        _process_prospects(
            [BORDERLINE_RAW],
            _crm(attio),
            list_id="list-123",
            today="2026-04-19",
            dry_run=False,
            summary=summary,
            seen_urls=seen_urls,
            in_list_record_ids=set(),
            persona_config=ENTERPRISE_PERSONA,
            borderline_stage=borderline_stage,
        )

        # Must be staged, not added to Attio
        assert summary["borderline_staged"] == 1
        assert summary["added"] == 0
        attio.upsert_person.assert_not_called()
        attio.add_list_entry.assert_not_called()

    def test_borderline_jsonl_payload_has_qualification_prompt(self, tmp_path):
        """Staged entry must include qualification_prompt with system+user."""
        attio = _make_attio_mock()
        summary = {
            "exported": 0,
            "scored": 0,
            "qualified": 0,
            "duplicates": 0,
            "rejected": 0,
            "added": 0,
            "borderline_staged": 0,
        }
        seen_urls: set[str] = set()
        borderline_stage: list[dict] = []

        _process_prospects(
            [BORDERLINE_RAW],
            _crm(attio),
            list_id="list-123",
            today="2026-04-19",
            dry_run=False,
            summary=summary,
            seen_urls=seen_urls,
            in_list_record_ids=set(),
            persona_config=ENTERPRISE_PERSONA,
            borderline_stage=borderline_stage,
        )

        assert len(borderline_stage) == 1
        entry = borderline_stage[0]
        # URL is stored in canonical form (no www, no trailing slash, lowercase)
        # to match Attio's stored form and prevent dup-record bugs.
        assert entry["linkedin_url"] == "https://linkedin.com/in/test-user"
        assert "prospect_data" in entry
        assert "raw_csv_row" in entry
        assert "persona" in entry
        assert "language" in entry
        assert "score" in entry
        qp = entry["qualification_prompt"]
        assert "system" in qp
        assert "user" in qp
        assert len(qp["system"]) > 0
        assert len(qp["user"]) > 0

    def test_summary_reports_borderline_staged_count(self):
        """summary['borderline_staged'] must reflect the number of staged prospects."""
        attio = _make_attio_mock()
        summary = {
            "exported": 0,
            "scored": 0,
            "qualified": 0,
            "duplicates": 0,
            "rejected": 0,
            "added": 0,
            "borderline_staged": 0,
        }
        seen_urls: set[str] = set()
        borderline_stage: list[dict] = []

        # Two borderline prospects
        raw2 = dict(BORDERLINE_RAW)
        raw2 = {**BORDERLINE_RAW, "defaultProfileUrl": "https://www.linkedin.com/in/test-user-2"}

        _process_prospects(
            [BORDERLINE_RAW, raw2],
            _crm(attio),
            list_id="list-123",
            today="2026-04-19",
            dry_run=False,
            summary=summary,
            seen_urls=seen_urls,
            in_list_record_ids=set(),
            persona_config=ENTERPRISE_PERSONA,
            borderline_stage=borderline_stage,
        )

        assert summary["borderline_staged"] == 2
        assert len(borderline_stage) == 2


# ---------------------------------------------------------------------------
# Fixtures / helpers shared by TestCommitProspect tests
# ---------------------------------------------------------------------------

def _make_commit_attio_mock():
    """Attio mock wired for a new-company + new-person creation path."""
    attio = MagicMock()
    attio.search_company_by_domain.return_value = None
    attio.search_companies.return_value = []
    attio.create_company.return_value = {"id": {"record_id": "comp-X"}}
    attio.upsert_person.return_value = {"id": {"record_id": "person-X"}}
    attio.add_list_entry.return_value = None
    attio.create_note.return_value = None
    # PR-207 default: the record is NOT yet in the pipeline list. The truth-based
    # already-listed guard in _commit_prospect consults query_list_entries on
    # paths that pass no in_list_record_ids snapshot (e.g. weekly_finalize_cmd);
    # an un-stubbed MagicMock would fail normalization / read truthy.
    attio.query_list_entries.return_value = []
    return attio


_PROSPECT_DATA = {
    "name": "Juan García",
    "title": "Gerente de Planta",
    "company": "Sigma Alimentos",
    "location": "Monterrey, Mexico",
    "linkedin_url": "https://www.linkedin.com/in/juan-garcia",
    "employee_count": "500",
}

_RAW_CSV_ROW = {
    "fullName": "Juan García",
    "title": "Gerente de Planta",
    "companyName": "Sigma Alimentos",
    "defaultProfileUrl": "https://www.linkedin.com/in/juan-garcia",
}

_SCORE_RESULT = {
    "pass": True,
    "score": 80,
    "persona": "operations_leaders",
    "language": "es",
    "reasons": [],
}


class TestReprospectReviewSkip:
    """An existing Attio person record without a current list entry must NOT
    be auto-added at PROSPECT stage. It almost always represents a record
    whose prior cadence list-entry was removed by dedup or terminal-state
    pruning — re-prospecting it erases history and can re-invite already
    connected / dismissed people.

    Caused the 2026-05-08 stale-accept incident: three prospects were
    re-added by the 2026-05-03 weekly run on top of person records whose
    entries were removed in the 2026-04-21 dedup.
    LinkedIn no-op'd the new "invites" (they were already connections), then
    Phase 0 mistook the no-op for fresh acceptance.
    """

    _PASSING_RAW = {
        "fullName": "Existing Person",
        "title": "Director of Operations",
        "companyName": "Acme Foods",
        "location": "Mexico City, Mexico",
        "companyEmployees": "5000",
        "defaultProfileUrl": "https://www.linkedin.com/in/existing-person",
    }

    def _summary(self):
        return {
            "exported": 0, "scored": 0, "qualified": 0, "duplicates": 0,
            "rejected": 0, "added": 0, "borderline_staged": 0,
            "reprospect_review": 0,
        }

    def test_existing_record_no_list_entry_skipped_for_review(self):
        """An existing person record without a list entry triggers the review
        path, NOT auto-add. Verifies the 2026-05-08 fix.
        """
        attio = MagicMock()
        attio.search_person_by_linkedin.return_value = {"id": {"record_id": "rec-existing"}}
        attio.upsert_person.return_value = {"id": {"record_id": "rec-existing"}}

        summary = self._summary()
        seen_urls: set[str] = set()
        borderline_stage: list[dict] = []
        reprospect_review: list[dict] = []

        _process_prospects(
            [self._PASSING_RAW],
            _crm(attio),
            list_id="list-123",
            today="2026-05-08",
            dry_run=False,
            summary=summary,
            seen_urls=seen_urls,
            in_list_record_ids=set(),  # rec-existing NOT in current pipeline
            persona_config={"key": "operations_leaders", "enterprise_mode": True},
            borderline_stage=borderline_stage,
            reprospect_review=reprospect_review,
        )

        # Bug used to add silently as PROSPECT — must NOT happen now.
        attio.add_list_entry.assert_not_called()
        assert summary["added"] == 0

        # Must surface in the manual-review queue.
        assert summary["reprospect_review"] == 1
        assert len(reprospect_review) == 1
        entry = reprospect_review[0]
        assert entry["record_id"] == "rec-existing"
        assert entry["linkedin_url"] == "https://linkedin.com/in/existing-person"
        assert entry["name"] == "Existing Person"

    def test_existing_record_already_in_list_still_skips_as_duplicate(self):
        """When the record IS already in the pipeline list, the existing
        duplicates path still wins — no review entry, no add.
        """
        attio = MagicMock()
        attio.search_person_by_linkedin.return_value = {"id": {"record_id": "rec-in-list"}}

        summary = self._summary()
        seen_urls: set[str] = set()
        borderline_stage: list[dict] = []
        reprospect_review: list[dict] = []

        _process_prospects(
            [self._PASSING_RAW],
            _crm(attio),
            list_id="list-123",
            today="2026-05-08",
            dry_run=False,
            summary=summary,
            seen_urls=seen_urls,
            in_list_record_ids={"rec-in-list"},
            persona_config={"key": "operations_leaders", "enterprise_mode": True},
            borderline_stage=borderline_stage,
            reprospect_review=reprospect_review,
        )

        attio.add_list_entry.assert_not_called()
        assert summary["duplicates"] == 1
        assert summary["reprospect_review"] == 0
        assert reprospect_review == []


class TestCommitProspect:
    """_commit_prospect correctly integrates industry classification."""

    def test_commit_prospect_classifies_and_sets_industry(self):
        """When anthropic_client is provided, create_company receives industry_vertical."""
        attio = _make_commit_attio_mock()

        anthropic_client = MagicMock()
        content_block = MagicMock()
        content_block.type = "text"
        content_block.text = "Food & Beverage"
        mock_response = MagicMock()
        mock_response.content = [content_block]
        anthropic_client.messages.create.return_value = mock_response

        result = _commit_prospect(
            _crm(attio),
            _PROSPECT_DATA,
            _RAW_CSV_ROW,
            _SCORE_RESULT,
            "list-id",
            "2026-04-19",
            anthropic_client=anthropic_client,
        )

        assert result is True
        call_args = attio.create_company.call_args
        # match_or_create_company passes attributes as first positional arg
        attrs = call_args.args[0]
        assert attrs.get("industry_vertical") == "Food & Beverage"

    def test_commit_prospect_without_anthropic_skips_classification(self):
        """Without anthropic_client, create_company must NOT receive industry_vertical."""
        attio = _make_commit_attio_mock()

        result = _commit_prospect(
            _crm(attio),
            _PROSPECT_DATA,
            _RAW_CSV_ROW,
            _SCORE_RESULT,
            "list-id",
            "2026-04-19",
        )

        assert result is True
        attio.create_company.assert_called_once()
        call_args = attio.create_company.call_args
        attrs = call_args.args[0]
        assert "industry_vertical" not in attrs

    def test_commit_prospect_returns_false_on_400(self):
        """Per-prospect validation errors (HTTP 400) are swallowed — return False, keep run going."""
        import httpx

        attio = _make_commit_attio_mock()
        # upsert_person raises a 400 (treated as per-prospect validation issue)
        attio.upsert_person.side_effect = httpx.HTTPStatusError(
            "Bad Request",
            request=httpx.Request("POST", "https://api.attio.com/v2/objects/people/records"),
            response=httpx.Response(400, text='{"message":"linkedin: invalid url"}'),
        )

        result = _commit_prospect(
            _crm(attio),
            _PROSPECT_DATA,
            _RAW_CSV_ROW,
            _SCORE_RESULT,
            "list-id",
            "2026-04-19",
        )

        assert result is False

    def test_commit_prospect_propagates_401(self):
        """Auth errors (HTTP 401) must propagate — otherwise the run silently
        ships zero prospects and the operator never knows the API key broke."""
        import httpx
        import pytest

        attio = _make_commit_attio_mock()
        attio.upsert_person.side_effect = httpx.HTTPStatusError(
            "Unauthorized",
            request=httpx.Request("POST", "https://api.attio.com/v2/objects/people/records"),
            response=httpx.Response(401, text='{"message":"invalid token"}'),
        )

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            _commit_prospect(
                _crm(attio),
                _PROSPECT_DATA,
                _RAW_CSV_ROW,
                _SCORE_RESULT,
                "list-id",
                "2026-04-19",
            )
        assert exc_info.value.response.status_code == 401

    def test_commit_prospect_propagates_429(self):
        """Rate-limit (429) propagates — the retry policy lives in AttioClient,
        not in _commit_prospect's exception handler."""
        import httpx
        import pytest

        attio = _make_commit_attio_mock()
        attio.upsert_person.side_effect = httpx.HTTPStatusError(
            "Too Many Requests",
            request=httpx.Request("POST", "https://api.attio.com/v2/objects/people/records"),
            response=httpx.Response(429, text='{"message":"rate limit"}'),
        )

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            _commit_prospect(
                _crm(attio),
                _PROSPECT_DATA,
                _RAW_CSV_ROW,
                _SCORE_RESULT,
                "list-id",
                "2026-04-19",
            )
        assert exc_info.value.response.status_code == 429


# A raw list-entry JSON whose parent record matches the person upserted by
# `_make_commit_attio_mock` (record_id="person-X"); parse_entry reads the
# record_id off `id.record_id`.
_EXISTING_RAW_ENTRY = {"id": {"record_id": "person-X", "entry_id": "entry-existing"}}


class TestReStampTerminalGuard:
    """Weekly must never re-stamp a record that already owns a pipeline entry.

    PR-207 (re-stamp incident): `weekly_finalize_cmd` commits borderline
    passers via `_commit_prospect` with NO `in_list_record_ids` snapshot, so the
    snapshot-membership skip was a dead no-op on that path — an existing entry
    got PATCHed back to a fresh stage/dm_step=0, wiping cadence depth. The guard
    must be grounded in CRM truth, not in whether the caller built a snapshot.
    """

    def test_finalize_path_skips_restamp_of_already_listed_record(self):
        """No caches passed (the finalize path): if the record already owns a
        list entry, _commit_prospect must skip the add entirely — no re-stamp.
        """
        attio = _make_commit_attio_mock()
        # Record already owns an entry; the finalize path passes no snapshot, so
        # the guard queries the CRM directly and filters in-memory.
        attio.query_list_entries.return_value = [_EXISTING_RAW_ENTRY]
        summary: dict = {}

        ok = _commit_prospect(
            _crm(attio),
            _PROSPECT_DATA,
            _RAW_CSV_ROW,
            _SCORE_RESULT,
            "list-id",
            "2026-06-25",
            summary=summary,
            # No in_list_record_ids, no existing_entries — the finalize path.
        )

        assert ok is True
        # The existing entry must NOT be re-stamped.
        attio.add_list_entry.assert_not_called()
        # And it is churn, not net-new supply.
        assert summary.get("restamped_existing") == 1
        assert summary.get("net_new_created", 0) == 0

    def test_finalize_path_skips_via_existing_entries_cache(self):
        """When the finalize path supplies existing_entries (one list fetch, no
        in_list_record_ids), the guard resolves membership in-memory and skips.
        """
        attio = _make_commit_attio_mock()
        crm = _crm(attio)
        existing_entry = crm._to_entry(_EXISTING_RAW_ENTRY)

        ok = _commit_prospect(
            crm,
            _PROSPECT_DATA,
            _RAW_CSV_ROW,
            _SCORE_RESULT,
            "list-id",
            "2026-06-25",
            existing_entries=[existing_entry],
            # in_list_record_ids intentionally omitted (the finalize shape).
        )

        assert ok is True
        attio.add_list_entry.assert_not_called()

    def test_guard_fails_closed_when_truth_lookup_errors(self):
        """If the no-snapshot truth lookup errors, the guard must NOT assume
        net-new and re-stamp — it skips the commit (returns False) instead.
        """
        import httpx

        attio = _make_commit_attio_mock()
        attio.query_list_entries.side_effect = httpx.HTTPStatusError(
            "rate limited",
            request=httpx.Request("GET", "https://api.attio.com/v2/lists/x/entries"),
            response=httpx.Response(429),
        )

        ok = _commit_prospect(
            _crm(attio),
            _PROSPECT_DATA,
            _RAW_CSV_ROW,
            _SCORE_RESULT,
            "list-id",
            "2026-06-25",
            # No caches — forces the direct CRM lookup, which errors here.
        )

        assert ok is False
        attio.add_list_entry.assert_not_called()


class TestProspectEntryAttrsPersistence:
    """The Attio list-entry payload preserves scorer signals end-to-end.

    Locks in the JSON encoding of score_breakdown and the bare-string
    encoding of select-type fields (scoring_lane, verdict_path). A typo in
    json.dumps or a guard-condition flip would silently corrupt every
    written prospect's entry — these tests catch it before merge.
    """

    _SCORE_RESULT_FULL = {
        "pass": True,
        "score": 82,
        "persona": "operations_leaders",
        "language": "es",
        "reasons": ["Mid-market sweet spot", "Decision-maker title", "In-ICP industry"],
        "score_breakdown": {
            "size": 28,
            "role": 28,
            "competitor": 20,
            "industry": 12,
            "total": 88,  # pre-clamp
            "reasons": ["Mid-market sweet spot", "Decision-maker title", "In-ICP industry"],
        },
        "scoring_lane": "target_company_mode",
        "verdict_path": "target_pass",
        "llm_rationale": None,
    }

    def test_build_prospect_entry_attrs_json_roundtrip(self):
        """score_breakdown must be a JSON string that decodes back to the original dict."""
        from workflows.weekly_prospect import _build_prospect_entry_attrs

        attrs = _build_prospect_entry_attrs(self._SCORE_RESULT_FULL, "2026-04-19")

        assert isinstance(attrs["score_breakdown"], str)
        decoded = json.loads(attrs["score_breakdown"])
        assert decoded == self._SCORE_RESULT_FULL["score_breakdown"]
        assert decoded["total"] == 88
        # Select-type fields must be bare strings, not JSON-wrapped
        assert attrs["scoring_lane"] == "target_company_mode"
        assert attrs["verdict_path"] == "target_pass"
        # Optional None fields should be skipped, not written as "null" or ""
        assert "llm_rationale" not in attrs

    def test_build_prospect_entry_attrs_skips_missing_optional_fields(self):
        """Score result without the new fields produces a clean entry attrs dict."""
        from workflows.weekly_prospect import _build_prospect_entry_attrs

        minimal = {
            "pass": True,
            "score": 70,
            "persona": "operations_leaders",
            "language": "es",
        }
        attrs = _build_prospect_entry_attrs(minimal, "2026-04-19")
        assert "score_breakdown" not in attrs
        assert "scoring_lane" not in attrs
        assert "verdict_path" not in attrs
        assert "llm_rationale" not in attrs
        # Core fields still present
        assert attrs["quality_score"] == 70
        assert attrs["persona"] == "operations_leaders"
        assert attrs["dm_step"] == 0

    def test_build_prospect_entry_attrs_omits_last_contact_date(self):
        """A freshly-committed PROSPECT has had zero contact, so the entry must
        carry NO last_contact_date. Stamping it at commit (the old behaviour)
        made every uncontacted prospect read in Attio as 'already contacted',
        polluting the operator's view and any pre-invite signal. The field is
        written for real only when an invite is sent
        (daily_check.run_connection_requests). prospect_committed_at remains the
        canonical PROSPECT-stage origin timestamp."""
        from workflows.weekly_prospect import _build_prospect_entry_attrs

        attrs = _build_prospect_entry_attrs(self._SCORE_RESULT_FULL, "2026-04-19")

        assert "last_contact_date" not in attrs
        assert "prospect_committed_at" in attrs
        assert attrs["dm_step"] == 0

    def test_commit_prospect_persists_score_breakdown_to_attio(self):
        """End-to-end: _commit_prospect → add_list_entry receives JSON-encoded breakdown."""
        attio = _make_commit_attio_mock()

        result = _commit_prospect(
            _crm(attio),
            _PROSPECT_DATA,
            _RAW_CSV_ROW,
            self._SCORE_RESULT_FULL,
            "list-id",
            "2026-04-19",
        )

        assert result is True
        attio.add_list_entry.assert_called_once()
        entry_attrs = attio.add_list_entry.call_args.kwargs["entry_attributes"]
        # The JSON survives the journey from scorer to Attio
        assert isinstance(entry_attrs["score_breakdown"], str)
        decoded = json.loads(entry_attrs["score_breakdown"])
        assert decoded["industry"] == 12
        assert decoded["total"] == 88
        assert entry_attrs["scoring_lane"] == "target_company_mode"
        assert entry_attrs["verdict_path"] == "target_pass"


# ── Test: in-run URL dedup uses the same canonical form Attio stores ────────

class TestUrlCanonicalization:
    """The in-run `seen_urls` dedup must use Attio's canonical LinkedIn URL form
    (lowercase, no www, no trailing slash, percent-decoded), so two PB rows
    that differ only in URL casing/wrapping collapse to one prospect — the
    same way Attio's upsert_person collapses them. Drift between these two
    normalizations was the root cause of the 2026-04-21 duplicate explosion.
    """

    def _summary(self):
        return {
            "exported": 0, "scored": 0, "qualified": 0, "duplicates": 0,
            "rejected": 0, "added": 0, "borderline_staged": 0,
        }

    def test_dedupes_url_pairs_that_differ_only_by_www_or_slash(self):
        attio = _make_attio_mock()
        attio.search_person_by_linkedin.return_value = None
        seen_urls: set[str] = set()
        summary = self._summary()
        borderline_stage: list[dict] = []

        # Same person submitted twice in different URL forms
        raw_a = {**BORDERLINE_RAW, "defaultProfileUrl": "https://www.linkedin.com/in/test-user/"}
        raw_b = {**BORDERLINE_RAW, "defaultProfileUrl": "https://linkedin.com/in/Test-User"}

        _process_prospects(
            [raw_a, raw_b],
            _crm(attio),
            list_id="list-123",
            today="2026-04-26",
            dry_run=False,
            summary=summary,
            seen_urls=seen_urls,
            in_list_record_ids=set(),
            persona_config=ENTERPRISE_PERSONA,
            borderline_stage=borderline_stage,
        )

        # The second row must collapse onto the first via canonicalization.
        assert summary["duplicates"] == 1
        # Only one canonical URL stored
        assert seen_urls == {"https://linkedin.com/in/test-user"}

    def test_percent_encoded_accent_dedupes_with_plain_form(self):
        """A URL with a percent-encoded accented character must dedup against
        the plain-form URL — the bug pattern from the 2026-04-21 incident.
        """
        attio = _make_attio_mock()
        attio.search_person_by_linkedin.return_value = None
        seen_urls: set[str] = set()
        summary = self._summary()
        borderline_stage: list[dict] = []

        raw_a = {**BORDERLINE_RAW, "defaultProfileUrl": "https://linkedin.com/in/jos%C3%A9-l%C3%B3pez"}
        raw_b = {**BORDERLINE_RAW, "defaultProfileUrl": "https://www.linkedin.com/in/josé-lópez"}

        _process_prospects(
            [raw_a, raw_b],
            _crm(attio),
            list_id="list-123",
            today="2026-04-26",
            dry_run=False,
            summary=summary,
            seen_urls=seen_urls,
            in_list_record_ids=set(),
            persona_config=ENTERPRISE_PERSONA,
            borderline_stage=borderline_stage,
        )

        assert summary["duplicates"] == 1
        assert len(seen_urls) == 1


# ── Test: snapshot caches stay fresh during a run ──────────────────────────

class TestCachesStayFresh:
    """The weekly run pre-loads `existing_entries` and `in_list_record_ids`
    once at the top, then passes them through the loop. If a brand-new prospect
    is created mid-run by `_commit_prospect`, both caches must be updated so a
    later iteration that re-encounters the same person (via a different URL
    variant resolved by Attio's variant search) finds the just-created entry
    and upserts instead of POSTing a duplicate list entry.

    Same root cause family as the 2026-04-21 duplicate-record incident.
    """

    def test_commit_prospect_updates_in_list_record_ids(self):
        """After a successful create+add, the new record_id must appear in the
        caller's `in_list_record_ids` set.
        """
        attio = _make_commit_attio_mock()
        attio.add_list_entry.return_value = {
            "id": {"entry_id": "entry-new", "list_id": "list-id"},
            "parent_record_id": "person-X",
            "entry_values": {"stage": [{"status": {"title": "Prospect"}}]},
        }
        in_list_record_ids: set[str] = set()
        # The cache now holds normalized `Entry` dataclasses (the provider's
        # add_list_entry return shape), not raw entry dicts.
        existing_entries: list[Entry] = []

        ok = _commit_prospect(
            _crm(attio),
            _PROSPECT_DATA,
            _RAW_CSV_ROW,
            _SCORE_RESULT,
            "list-id",
            "2026-04-19",
            in_list_record_ids=in_list_record_ids,
            existing_entries=existing_entries,
        )

        assert ok is True
        # Cache must reflect the just-created record so a re-encounter
        # of the same person early-exits via the in_list_record_ids check.
        assert "person-X" in in_list_record_ids
        # And the entries snapshot must include the new entry so the next
        # add_list_entry call sees it client-side. The appended item is the
        # normalized `Entry` the provider returned.
        assert any(e.entry_id == "entry-new" for e in existing_entries)

    def test_commit_prospect_empty_entry_body_not_cached(self):
        """An empty-body add_list_entry response must NOT enter the dedup cache.

        Regression guard for the F-2 fix: the provider normalizes an empty 2xx
        body to a degenerate Entry(record_id="") which — unlike the old raw {}
        dict — is always truthy. The append is gated on `new_entry.record_id`,
        so the junk entry stays out of `existing_entries` (where a record_id=""
        row would be spurious). The prospect itself is still committed (the
        person upsert succeeded), and the person record_id still enters
        `in_list_record_ids`.
        """
        attio = _make_commit_attio_mock()
        # Empty 2xx body → AttioProvider._to_entry({}) → Entry(record_id="").
        attio.add_list_entry.return_value = {}
        in_list_record_ids: set[str] = set()
        existing_entries: list = []

        ok = _commit_prospect(
            _crm(attio),
            _PROSPECT_DATA,
            _RAW_CSV_ROW,
            _SCORE_RESULT,
            "list-id",
            "2026-04-19",
            in_list_record_ids=in_list_record_ids,
            existing_entries=existing_entries,
        )

        assert ok is True
        # Person record_id is keyed off the upsert, independent of the entry body.
        assert "person-X" in in_list_record_ids
        # The degenerate empty entry must NOT have been appended.
        assert existing_entries == []

    def test_commit_prospect_handles_none_caches(self):
        """When no caches are passed, the function still works (no AttributeError)."""
        attio = _make_commit_attio_mock()
        attio.add_list_entry.return_value = {"id": {"entry_id": "entry-x"}}

        ok = _commit_prospect(
            _crm(attio),
            _PROSPECT_DATA,
            _RAW_CSV_ROW,
            _SCORE_RESULT,
            "list-id",
            "2026-04-19",
            # Caches omitted — function must not assume they exist.
        )

        assert ok is True


MIDMARKET_PERSONA = {
    "key": "mx_midmarket_manufacturing",
    "target_company_mode": True,
    # no target_company_list — filter is bypassed when fragments are empty,
    # so any prospect company is accepted (sufficient for this unit test).
}


class TestPersonaUpgradeOnCrossSearchMatch:
    """When a prospect already staged with an enterprise persona is then
    matched by a midmarket (target_company_mode) search, the borderline
    entry's persona/lane/breakdown must upgrade to the midmarket result.
    Locks in the cross-search dedup behavior that fixes the digitalization-
    sticky bug from the 2026-05-19 weekly run.
    """

    def _empty_summary(self) -> dict:
        return {
            "exported": 0,
            "scored": 0,
            "qualified": 0,
            "duplicates": 0,
            "rejected": 0,
            "added": 0,
            "borderline_staged": 0,
        }

    def test_midmarket_match_upgrades_existing_enterprise_borderline(self):
        attio = _make_attio_mock()
        summary = self._empty_summary()
        seen_urls: set[str] = set()
        seen_urls_midmarket: set[str] = set()
        borderline_stage: list[dict] = []

        # Pass 1: enterprise search stages the prospect with digitalization persona.
        _process_prospects(
            [BORDERLINE_RAW],
            _crm(attio),
            list_id="list-123",
            today="2026-04-19",
            dry_run=False,
            summary=summary,
            seen_urls=seen_urls,
            in_list_record_ids=set(),
            persona_config=ENTERPRISE_PERSONA,
            borderline_stage=borderline_stage,
            seen_urls_midmarket=seen_urls_midmarket,
        )
        assert summary["borderline_staged"] == 1
        assert borderline_stage[0]["scoring_lane"] == "enterprise_mode"
        enterprise_persona_tag = borderline_stage[0]["persona"]

        # Pass 2: midmarket search matches the same URL → must upgrade.
        _process_prospects(
            [BORDERLINE_RAW],
            _crm(attio),
            list_id="list-123",
            today="2026-04-19",
            dry_run=False,
            summary=summary,
            seen_urls=seen_urls,
            in_list_record_ids=set(),
            persona_config=MIDMARKET_PERSONA,
            borderline_stage=borderline_stage,
            seen_urls_midmarket=seen_urls_midmarket,
        )

        # Still one borderline entry — upgraded in place, not duplicated.
        assert len(borderline_stage) == 1
        entry = borderline_stage[0]
        assert entry["persona"] == "mx_midmarket_manufacturing", \
            f"persona should be upgraded from {enterprise_persona_tag!r} to midmarket"
        assert entry["scoring_lane"] == "target_company_mode", \
            f"scoring_lane should be upgraded, got {entry['scoring_lane']!r}"
        # Counter is incremented for visibility in the run summary.
        assert summary.get("persona_upgraded_to_midmarket") == 1
        # Dedup still counts the second encounter as a duplicate.
        assert summary["duplicates"] == 1

    def test_enterprise_match_does_not_downgrade_midmarket_borderline(self):
        """If midmarket runs first and tags the prospect, a later enterprise
        search must NOT downgrade the persona back to enterprise.
        """
        attio = _make_attio_mock()
        summary = self._empty_summary()
        seen_urls: set[str] = set()
        seen_urls_midmarket: set[str] = set()
        borderline_stage: list[dict] = []

        # Pass 1: midmarket first.
        _process_prospects(
            [BORDERLINE_RAW],
            _crm(attio),
            list_id="list-123",
            today="2026-04-19",
            dry_run=False,
            summary=summary,
            seen_urls=seen_urls,
            in_list_record_ids=set(),
            persona_config=MIDMARKET_PERSONA,
            borderline_stage=borderline_stage,
            seen_urls_midmarket=seen_urls_midmarket,
        )
        assert summary["borderline_staged"] == 1
        first_persona = borderline_stage[0]["persona"]
        assert first_persona == "mx_midmarket_manufacturing"

        # Pass 2: enterprise search tries to match.
        _process_prospects(
            [BORDERLINE_RAW],
            _crm(attio),
            list_id="list-123",
            today="2026-04-19",
            dry_run=False,
            summary=summary,
            seen_urls=seen_urls,
            in_list_record_ids=set(),
            persona_config=ENTERPRISE_PERSONA,
            borderline_stage=borderline_stage,
            seen_urls_midmarket=seen_urls_midmarket,
        )

        assert len(borderline_stage) == 1
        assert borderline_stage[0]["persona"] == first_persona, \
            "midmarket tag must survive a later enterprise match"
        assert summary.get("persona_upgraded_to_midmarket", 0) == 0
        assert summary["duplicates"] == 1


# ---------------------------------------------------------------------------
# Supply-visibility (2026-06-16 root cause): distinguish net-new prospects
# from re-stamps of records already in the pipeline, bust PB dedup on the
# weekly scrape, and skip already-listed records via an authoritative
# canonical-URL set BEFORE the eventual-consistent live search.
#
# Ported from upstream lucie-sales-agent #202 (d95d7b4) and adapted to the
# fork's CRMProvider seam: _commit_prospect / _process_prospects take a
# provider (wrapped via _crm), and the in-list URL set is resolved through
# the provider contract (bulk_fetch_persons + extract_person_info).
# ---------------------------------------------------------------------------


class TestNetNewSupplyAccounting:
    """_commit_prospect must classify each commit as a genuine net-new entry
    vs a re-stamp of a record already in the pipeline list, so the weekly
    summary can surface true supply (a re-stamp adds zero new prospects).
    """

    def test_commit_of_unlisted_record_counts_as_net_new(self):
        attio = _make_commit_attio_mock()
        attio.upsert_person.return_value = {"id": {"record_id": "person-NEW"}}
        summary: dict = {}
        in_list: set[str] = set()  # person-NEW is NOT already in the list

        result = _commit_prospect(
            _crm(attio), _PROSPECT_DATA, _RAW_CSV_ROW, _SCORE_RESULT,
            "list-id", "2026-06-16",
            in_list_record_ids=in_list, summary=summary,
        )

        assert result is True
        assert summary.get("net_new_created") == 1
        assert summary.get("restamped_existing", 0) == 0

    def test_net_new_commit_stamps_canonical_linkedin_url(self):
        """Fix 2a: a net-new commit must stamp `canonical_linkedin_url` on the
        list-entry attrs. This field is the key the 14-day re-prospect guard
        (`_load_recent_outreach_map`) reads — it was NULL on 100% of entries,
        which made the guard a silent no-op (weekly re-stamp root cause).
        """
        from clients.attio import _canonical_linkedin_url

        attio = _make_commit_attio_mock()
        attio.upsert_person.return_value = {"id": {"record_id": "person-NEW"}}
        in_list: set[str] = set()

        result = _commit_prospect(
            _crm(attio), _PROSPECT_DATA, _RAW_CSV_ROW, _SCORE_RESULT,
            "list-id", "2026-06-16",
            in_list_record_ids=in_list, summary={},
        )

        assert result is True
        attrs = attio.add_list_entry.call_args.kwargs["entry_attributes"]
        assert attrs["canonical_linkedin_url"] == _canonical_linkedin_url(
            _PROSPECT_DATA["linkedin_url"]
        )

    def test_commit_of_already_listed_record_counts_as_restamp(self):
        """An already-listed record is counted as a re-stamp for observability
        but its existing entry must NOT be rewritten — the daily cadence engine
        owns it (weekly re-stamp cadence wipe). add_list_entry must NOT be called.
        """
        attio = _make_commit_attio_mock()
        attio.upsert_person.return_value = {"id": {"record_id": "person-OLD"}}
        summary: dict = {}
        in_list = {"person-OLD"}  # already in the pipeline → this is a re-stamp

        result = _commit_prospect(
            _crm(attio), _PROSPECT_DATA, _RAW_CSV_ROW, _SCORE_RESULT,
            "list-id", "2026-06-16",
            in_list_record_ids=in_list, summary=summary,
        )

        assert result is True
        assert summary.get("restamped_existing") == 1
        assert summary.get("net_new_created", 0) == 0
        # The record stays in the list (no double-add) and its entry is left
        # untouched — weekly must never re-stamp a record the daily cadence owns.
        assert attio.add_list_entry.call_count == 0
        assert "person-OLD" in in_list

    def test_process_prospects_classifies_guard_miss_restamp(self):
        """When the dedup guard (search_person_by_linkedin) misses an
        already-listed record, the candidate falls through to _commit_prospect.
        That commit must be accounted as a re-stamp, NOT net-new supply.
        """
        attio = MagicMock()
        # Guard misses (returns None) even though the record IS in the list.
        attio.search_person_by_linkedin.return_value = None
        attio.upsert_person.return_value = {"id": {"record_id": "rec-listed"}}
        attio.add_list_entry.return_value = None
        attio.search_company_by_domain.return_value = None
        attio.search_companies.return_value = []
        attio.create_company.return_value = {"id": {"record_id": "comp-1"}}

        summary = {
            "exported": 0, "scored": 0, "qualified": 0, "duplicates": 0,
            "rejected": 0, "added": 0, "borderline_staged": 0,
            "reprospect_review": 0,
        }
        _process_prospects(
            [{
                "fullName": "Already Listed",
                "title": "Director of Operations",
                "companyName": "Acme Foods",
                "location": "Mexico City, Mexico",
                "companyEmployees": "5000",
                "defaultProfileUrl": "https://www.linkedin.com/in/already-listed",
            }],
            _crm(attio),
            list_id="list-123",
            today="2026-06-16",
            dry_run=False,
            summary=summary,
            seen_urls=set(),
            in_list_record_ids={"rec-listed"},
            persona_config={"key": "operations_leaders", "enterprise_mode": True},
            borderline_stage=[],
            reprospect_review=[],
        )

        assert summary["added"] == 1            # commit returned True
        assert summary.get("restamped_existing") == 1
        assert summary.get("net_new_created", 0) == 0
        # Fix 1: the existing entry must NOT be re-stamped — the record is
        # already in the list, so add_list_entry is skipped entirely.
        assert attio.add_list_entry.call_count == 0


class TestWeeklyScrapeCsvNameBust:
    """The weekly scrape must set a unique per-launch csvName (PR #179 pattern)
    so PhantomBuster's filename-keyed dedup is busted and the same saved search
    can re-scrape fresh rows instead of returning a frozen cached set.
    """

    def test_launch_sets_fresh_csv_name_and_downloads_with_it(self, monkeypatch):
        from workflows.weekly_prospect import _launch_and_download

        monkeypatch.setenv("PB_LI_SESSION_COOKIE", "cookie-abc")

        pb = MagicMock()
        launch = MagicMock()
        pb.launch_agent.return_value = launch
        pb.download_result_csv.return_value = "firstName,lastName\nA,B\n"

        out = _launch_and_download(pb, "agent-1", "https://linkedin.com/sales/search/x", 100)

        assert out == "firstName,lastName\nA,B\n"
        launch_args = pb.launch_agent.call_args.args[1]
        csv_name = launch_args.get("csvName")
        assert csv_name and csv_name.startswith("wk-")
        # Same name MUST be passed to the download so the per-launch CSV is fetched.
        assert pb.download_result_csv.call_args.kwargs.get("csv_name") == csv_name


class TestSupplyStarvationAlarm:
    """The keystone silent-failure fix: a run that qualifies people but sources
    zero net-new prospects must be detectable (it fires a loud operator alarm).
    """

    def test_fires_when_qualified_but_zero_net_new(self):
        from workflows.weekly_prospect import _is_supply_starved
        summary = {"qualified": 417, "net_new_created": 0, "restamped_existing": 200}
        assert _is_supply_starved(summary, dry_run=False) is True

    def test_silent_when_some_net_new(self):
        from workflows.weekly_prospect import _is_supply_starved
        summary = {"qualified": 417, "net_new_created": 5, "restamped_existing": 200}
        assert _is_supply_starved(summary, dry_run=False) is False

    def test_silent_when_nothing_qualified(self):
        from workflows.weekly_prospect import _is_supply_starved
        summary = {"qualified": 0, "net_new_created": 0}
        assert _is_supply_starved(summary, dry_run=False) is False

    def test_never_fires_on_dry_run(self):
        from workflows.weekly_prospect import _is_supply_starved
        summary = {"qualified": 417, "net_new_created": 0}
        assert _is_supply_starved(summary, dry_run=True) is False


# ---------------------------------------------------------------------------
# Recycle fix (B): an authoritative canonical-URL dedup so a search miss can no
# longer let an already-listed record fall through and get re-stamped.
# ---------------------------------------------------------------------------


class TestInListCanonicalUrlSet:
    def test_builds_from_entry_field_and_bulk_fetch_gap(self):
        """The set is built from the entry-level canonical_linkedin_url where
        present, and from the parent person record (via the provider's bulk
        fetch + extract_person_info) for the legacy entries that lack it."""
        from clients.crm.base import Entry, Record, RecordInfo
        from models.pipeline import PipelineStage
        from workflows.weekly_prospect import _load_in_list_canonical_urls

        entries = [
            Entry(
                entry_id="e1", record_id="r1", stage=PipelineStage.PROSPECT,
                attributes={"canonical_linkedin_url": "https://www.linkedin.com/in/Alice/"},
            ),
            Entry(
                entry_id="e2", record_id="r2", stage=PipelineStage.PROSPECT,
                attributes={"canonical_linkedin_url": None},
            ),
        ]

        bob = Record(record_id="r2", object="people", attributes={})
        crm = MagicMock()
        crm.bulk_fetch_persons.return_value = {"r2": bob}
        crm.extract_person_info.return_value = RecordInfo(
            name="Bob", company="Co",
            linkedin_url="https://linkedin.com/in/bob", industry=None, title="",
        )

        urls = _load_in_list_canonical_urls(crm, entries)

        assert urls == {"https://linkedin.com/in/alice", "https://linkedin.com/in/bob"}
        # Only the entry lacking a usable URL triggers a person lookup.
        crm.bulk_fetch_persons.assert_called_once_with({"r2"})


class TestCanonicalUrlDedupPrecheck:
    _PROSPECT = {
        "fullName": "Already Here",
        "title": "Director of Operations",
        "companyName": "Acme Foods",
        "location": "Mexico City, Mexico",
        "companyEmployees": "5000",
        "defaultProfileUrl": "https://www.linkedin.com/in/already-here",
    }

    def _summary(self):
        return {
            "exported": 0, "scored": 0, "qualified": 0, "duplicates": 0,
            "rejected": 0, "added": 0, "borderline_staged": 0,
            "reprospect_review": 0, "rejected_by_path": {},
        }

    def test_match_skips_before_live_search(self):
        from workflows.weekly_prospect import _canonical_linkedin_url
        attio = MagicMock()
        canonical = _canonical_linkedin_url(self._PROSPECT["defaultProfileUrl"])

        summary = self._summary()
        _process_prospects(
            [self._PROSPECT], _crm(attio), list_id="l", today="2026-06-17",
            dry_run=False, summary=summary, seen_urls=set(),
            in_list_record_ids=set(),
            persona_config={"key": "operations_leaders", "enterprise_mode": True},
            borderline_stage=[], reprospect_review=[],
            in_list_canonical_urls={canonical},
        )

        # The whole point: the flaky live search is never reached for an
        # already-listed URL, so it cannot miss and recycle the record.
        attio.search_person_by_linkedin.assert_not_called()
        attio.add_list_entry.assert_not_called()
        assert summary["duplicates"] == 1
        assert summary["added"] == 0

    def test_non_match_proceeds_to_commit(self):
        attio = MagicMock()
        attio.search_person_by_linkedin.return_value = None
        attio.upsert_person.return_value = {"id": {"record_id": "new-1"}}
        attio.add_list_entry.return_value = None
        attio.search_company_by_domain.return_value = None
        attio.search_companies.return_value = []
        attio.create_company.return_value = {"id": {"record_id": "c1"}}

        summary = self._summary()
        _process_prospects(
            [self._PROSPECT], _crm(attio), list_id="l", today="2026-06-17",
            dry_run=False, summary=summary, seen_urls=set(),
            in_list_record_ids=set(),
            persona_config={"key": "operations_leaders", "enterprise_mode": True},
            borderline_stage=[], reprospect_review=[],
            in_list_canonical_urls={"https://linkedin.com/in/someone-else"},
        )

        attio.search_person_by_linkedin.assert_called_once()
        assert summary["added"] == 1
        assert summary["duplicates"] == 0
