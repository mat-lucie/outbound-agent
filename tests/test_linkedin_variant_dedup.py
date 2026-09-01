"""Slug-variant cadence-leak regression: LinkedIn identity-key dedup.

A weekly ingest re-prospected a DM3-complete person under the shortened slug
`dana-q-70481235` while their canonical entry already sat at DM3 Sent under
`dana-quiroga-ramos-mba-70481235`. Both slugs share the numeric profile-id
suffix `70481235` — LinkedIn slug variants keep it — but every dedup surface
compared exact URL strings, so a duplicate person + entry were created and a
duplicate DM1 shipped to a DM3-complete prospect.

(The names/ids here are synthetic stand-ins for the upstream incident pair;
only the slug SHAPE the fix keys on is reproduced.)

Covers the three fixes:
  1. `linkedin_profile_id` / `linkedin_identity_key` (clients.attio) wired
     into the weekly-ingest URL gate, the 14-day recent-outreach guard, and
     the daily send-path dedup (`_dedupe_by_linkedin_url`, stage-rank index).
  2. The Pattern-A quarantine escalation payload carries
     `prior_cadence_entries` — pipeline entries sharing the profile-id — so
     the operator sees the prior cadence before judging "real accept".
  3. `manual_reply_suppressed_self_echo` idempotency keys on the thread
     signature (entry|template|count), not the date, so a resolved row no
     longer re-opens every day.
"""

import csv
import io
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from clients.attio import (
    _canonical_linkedin_url,
    linkedin_identity_key,
    linkedin_profile_id,
)
from clients.crm.base import CRMProvider, Entry, Stage
from models.pipeline import PipelineStage
from tests.test_weekly_prospect import _crm
from workflows.daily_check_helpers import _dedupe_by_linkedin_url
from workflows.weekly_prospect import _process_prospects

_LONG = "https://linkedin.com/in/dana-quiroga-ramos-mba-70481235"
_SHORT = "https://www.linkedin.com/in/dana-q-70481235"


def _entry(entry_id: str, record_id: str, **attributes) -> Entry:
    return Entry(
        entry_id=entry_id,
        record_id=record_id,
        stage=Stage(name=str(attributes.get("stage") or "")),
        attributes=dict(attributes),
    )


# ---------------------------------------------------------------------------
# 1a. The identity-key helpers
# ---------------------------------------------------------------------------

class TestProfileIdHelpers:
    def test_incident_slugs_share_profile_id(self):
        assert linkedin_profile_id(_LONG) == "70481235"
        assert linkedin_profile_id(_SHORT) == "70481235"

    def test_identity_key_bridges_slug_variants(self):
        assert linkedin_identity_key(_LONG) == "li-id:70481235"
        assert linkedin_identity_key(_LONG) == linkedin_identity_key(_SHORT)

    def test_short_numeric_tail_is_not_a_profile_id(self):
        # A user-chosen tail like `-2` must not conflate distinct people.
        url = "https://linkedin.com/in/john-smith-2"
        assert linkedin_profile_id(url) == ""
        assert linkedin_identity_key(url) == _canonical_linkedin_url(url)

    def test_no_numeric_suffix_falls_back_to_canonical(self):
        url = "https://linkedin.com/in/dana-quiroga-ramos"
        assert linkedin_profile_id(url) == ""
        assert linkedin_identity_key(url) == _canonical_linkedin_url(url)

    def test_non_profile_url_falls_back_to_canonical(self):
        url = "https://linkedin.com/company/acme-70481235"
        assert linkedin_profile_id(url) == ""
        assert linkedin_identity_key(url) == _canonical_linkedin_url(url)

    def test_empty_url(self):
        assert linkedin_profile_id("") == ""
        assert linkedin_identity_key("") == ""

    def test_www_trailing_slash_and_query_variants_share_key(self):
        variants = [
            _LONG,
            _LONG + "/",
            "https://www.linkedin.com/in/dana-quiroga-ramos-mba-70481235/",
            _LONG + "?utm_source=x",
            # Suffixes that survive `_vanity_url_slug`: slash-before-query,
            # fragment, and /in/<slug>/sub-path forms.
            _LONG + "/?utm_source=x",
            _LONG + "#section",
            _LONG + "/recent-activity",
        ]
        assert {linkedin_identity_key(v) for v in variants} == {"li-id:70481235"}

    def test_percent_encoded_accent_variants_share_key(self):
        # `%C3%A9` vs a literal `é` in the name portion: the canonical form
        # already decodes, and the profile-id key is identical either way.
        encoded = "https://linkedin.com/in/ren%C3%A9-salas-88112233"
        literal = "https://linkedin.com/in/rené-salas-88112233"
        assert linkedin_identity_key(encoded) == linkedin_identity_key(literal)
        assert linkedin_identity_key(encoded) == "li-id:88112233"

    def test_distinct_ids_stay_distinct(self):
        a = "https://linkedin.com/in/ana-perez-11111111"
        b = "https://linkedin.com/in/ana-perez-22222222"
        assert linkedin_identity_key(a) != linkedin_identity_key(b)


# ---------------------------------------------------------------------------
# 1b. Weekly-ingest URL gate
# ---------------------------------------------------------------------------

class TestWeeklyIngestVariantGate:
    def test_in_list_set_carries_profile_id_keys(self):
        from workflows import weekly_prospect as wp

        crm = MagicMock(spec=CRMProvider)
        urls = wp._load_in_list_canonical_urls(
            crm, [_entry("e1", "r1", canonical_linkedin_url=_LONG)]
        )
        assert _canonical_linkedin_url(_LONG) in urls
        assert "li-id:70481235" in urls

    def _summary(self):
        return {
            "exported": 0, "scored": 0, "qualified": 0, "duplicates": 0,
            "rejected": 0, "added": 0, "borderline_staged": 0,
            "reprospect_review": 0, "rejected_by_path": {},
        }

    def _candidate(self, url):
        return {
            "fullName": "Dana Quiroga Ramos",
            "title": "Director of Operations",
            "companyName": "Acme Foods",
            "location": "Mexico City, Mexico",
            "defaultProfileUrl": url,
        }

    def test_variant_slug_skipped_before_live_search(self):
        # The regression: the pipeline holds the LONG slug; the scraper
        # re-emits the person under the SHORT slug. Exact-URL dedup missed
        # this; the profile-id key must catch it before the live search.
        attio = MagicMock()
        summary = self._summary()
        in_list = {_canonical_linkedin_url(_LONG), linkedin_identity_key(_LONG)}

        _process_prospects(
            [self._candidate(_SHORT)], _crm(attio), list_id="l",
            today="2026-08-14", dry_run=False, summary=summary,
            seen_urls=set(), in_list_record_ids=set(),
            persona_config={"key": "operations_leaders", "enterprise_mode": True,
                            "search_size_credit": 30},
            borderline_stage=[], reprospect_review=[],
            in_list_canonical_urls=in_list,
        )

        attio.search_person_by_linkedin.assert_not_called()
        attio.upsert_person.assert_not_called()
        attio.add_list_entry.assert_not_called()
        assert summary["duplicates"] == 1
        assert summary["added"] == 0

    def test_within_run_variant_dedups_via_seen_urls(self):
        # Both slug variants arrive in the SAME scrape batch (e.g. two search
        # lanes): the second must dedup against the first via the identity
        # key, not commit a second person.
        attio = MagicMock()
        attio.search_person_by_linkedin.return_value = None
        attio.upsert_person.return_value = {"id": {"record_id": "new-1"}}
        attio.add_list_entry.return_value = None
        attio.search_company_by_domain.return_value = None
        attio.search_companies.return_value = []
        attio.create_company.return_value = {"id": {"record_id": "c1"}}
        summary = self._summary()

        _process_prospects(
            [self._candidate(_LONG), self._candidate(_SHORT)],
            _crm(attio), list_id="l", today="2026-08-14",
            dry_run=False, summary=summary, seen_urls=set(),
            in_list_record_ids=set(),
            persona_config={"key": "operations_leaders", "enterprise_mode": True,
                            "search_size_credit": 30},
            borderline_stage=[], reprospect_review=[],
            in_list_canonical_urls=set(),
        )

        assert summary["added"] == 1
        assert summary["duplicates"] == 1
        attio.upsert_person.assert_called_once()

    def test_different_profile_id_still_commits(self):
        attio = MagicMock()
        attio.search_person_by_linkedin.return_value = None
        attio.upsert_person.return_value = {"id": {"record_id": "new-1"}}
        attio.add_list_entry.return_value = None
        attio.search_company_by_domain.return_value = None
        attio.search_companies.return_value = []
        attio.create_company.return_value = {"id": {"record_id": "c1"}}
        summary = self._summary()
        in_list = {_canonical_linkedin_url(_LONG), linkedin_identity_key(_LONG)}

        _process_prospects(
            [self._candidate("https://linkedin.com/in/other-person-55555555")],
            _crm(attio), list_id="l", today="2026-08-14",
            dry_run=False, summary=summary, seen_urls=set(),
            in_list_record_ids=set(),
            persona_config={"key": "operations_leaders", "enterprise_mode": True,
                            "search_size_credit": 30},
            borderline_stage=[], reprospect_review=[],
            in_list_canonical_urls=in_list,
        )

        assert summary["duplicates"] == 0
        assert summary["added"] == 1


class TestPersonaUpgradeAcrossVariantSlugs:
    """The midmarket persona upgrade must find the staged borderline entry
    even when the second search returns the person under a slug VARIANT —
    the identity-key dedup collapses them, so the upgrade lookup must too."""

    def _raw(self, url):
        # Borderline in enterprise mode: Plant Manager → staged, not written.
        return {
            "fullName": "Dana Quiroga Ramos",
            "title": "Plant Manager",
            "companyName": "Subsidiary",
            "location": "Mexico City, Mexico",
            "defaultProfileUrl": url,
        }

    def test_variant_match_still_upgrades_borderline_entry(self):
        from tests.test_weekly_prospect import (
            ENTERPRISE_PERSONA,
            MIDMARKET_PERSONA,
            _make_attio_mock,
        )

        attio = _make_attio_mock()
        crm = _crm(attio)
        summary = {
            "exported": 0, "scored": 0, "qualified": 0, "duplicates": 0,
            "rejected": 0, "added": 0, "borderline_staged": 0,
        }
        seen_urls: set[str] = set()
        seen_urls_midmarket: set[str] = set()
        borderline_stage: list[dict] = []

        _process_prospects(
            [self._raw(_LONG)], crm, list_id="l", today="2026-08-14",
            dry_run=False, summary=summary, seen_urls=seen_urls,
            in_list_record_ids=set(), persona_config=ENTERPRISE_PERSONA,
            borderline_stage=borderline_stage,
            seen_urls_midmarket=seen_urls_midmarket,
        )
        assert summary["borderline_staged"] == 1

        # Midmarket search returns the SAME person under the SHORT variant.
        _process_prospects(
            [self._raw(_SHORT)], crm, list_id="l", today="2026-08-14",
            dry_run=False, summary=summary, seen_urls=seen_urls,
            in_list_record_ids=set(), persona_config=MIDMARKET_PERSONA,
            borderline_stage=borderline_stage,
            seen_urls_midmarket=seen_urls_midmarket,
        )

        assert len(borderline_stage) == 1
        assert borderline_stage[0]["persona"] == "mx_midmarket_manufacturing"
        assert summary.get("persona_upgraded_to_midmarket") == 1
        assert summary["duplicates"] == 1


# ---------------------------------------------------------------------------
# 1c. 14-day recent-outreach guard
# ---------------------------------------------------------------------------

class TestRecentOutreachMapVariant:
    def test_map_carries_profile_id_keys(self):
        from workflows import weekly_prospect as wp

        crm = MagicMock(spec=CRMProvider)
        crm.query_list_entries.return_value = [
            _entry("e1", "r1", canonical_linkedin_url=_LONG,
                   last_contact_date="2026-08-10"),
        ]

        out = wp._load_recent_outreach_map(crm, "list-1", date(2026, 8, 1))

        assert out[_canonical_linkedin_url(_LONG)] == date(2026, 8, 10)
        # The identity key rides along, so a slug VARIANT of the same person
        # is caught by the guard too.
        assert out["li-id:70481235"] == date(2026, 8, 10)
        assert linkedin_identity_key(_SHORT) in out


# ---------------------------------------------------------------------------
# 1d. Daily send-path batch dedup
# ---------------------------------------------------------------------------

class TestDedupeByLinkedinUrlVariant:
    def test_slug_variants_collapse_to_one_send(self):
        rows = [
            {"linkedInUrl": _LONG, "entry_id": "e-canonical"},
            {"linkedInUrl": _SHORT, "entry_id": "e-duplicate"},
        ]
        deduped, dropped = _dedupe_by_linkedin_url(rows)
        assert len(deduped) == 1
        # Both entry_ids ride on the kept row so the stage-advance loop flips
        # the duplicate in lock-step (no re-queue tomorrow).
        assert deduped[0]["entry_ids"] == ["e-canonical", "e-duplicate"]
        assert dropped == [_SHORT]

    def test_distinct_people_not_collapsed(self):
        rows = [
            {"linkedInUrl": "https://linkedin.com/in/ana-11111111", "entry_id": "a"},
            {"linkedInUrl": "https://linkedin.com/in/ben-22222222", "entry_id": "b"},
        ]
        deduped, dropped = _dedupe_by_linkedin_url(rows)
        assert len(deduped) == 2
        assert dropped == []


# ---------------------------------------------------------------------------
# 2. Pattern-A quarantine payload: prior-cadence siblings
# ---------------------------------------------------------------------------

class TestPriorCadenceLookup:
    def _attio_with_entries(self, monkeypatch, parsed_entries):
        from workflows import pre_invite_check as pic

        raws = [{"i": i} for i in range(len(parsed_entries))]
        monkeypatch.setattr(
            pic.AttioClient, "parse_entry",
            staticmethod(lambda raw: parsed_entries[raw["i"]]),
        )
        attio = MagicMock()
        attio.query_list_entries.return_value = raws
        return attio

    def _sibling(self):
        return {
            "entry_id": "e-old", "record_id": "r-old",
            "canonical_linkedin_url": _LONG,
            "stage": "DM3 Sent", "dm_step": 3,
            "dm1_sent_at": "2026-05-20", "dm2_sent_at": "2026-05-26",
            "dm3_sent_at": "2026-06-01", "last_contact_date": "2026-06-01",
        }

    def test_returns_profile_id_siblings_excluding_own_entry(self, monkeypatch):
        from workflows.pre_invite_check import _prior_cadence_entries_for_url

        attio = self._attio_with_entries(monkeypatch, [
            self._sibling(),
            {"entry_id": "e-other", "record_id": "r-other",
             "canonical_linkedin_url": "https://linkedin.com/in/otra-persona-77777777",
             "stage": "Prospect", "dm_step": None, "dm1_sent_at": None,
             "dm2_sent_at": None, "dm3_sent_at": None, "last_contact_date": None},
        ])
        cache: dict = {}
        matches, err = _prior_cadence_entries_for_url(
            attio, "list-1", _SHORT, "e-dup", cache,
        )
        assert err == ""
        assert [m["entry_id"] for m in matches] == ["e-old"]
        assert matches[0]["stage"] == "DM3 Sent"
        assert matches[0]["dm3_sent_at"] == "2026-06-01"

    def test_own_entry_is_excluded(self, monkeypatch):
        from workflows.pre_invite_check import _prior_cadence_entries_for_url

        attio = self._attio_with_entries(monkeypatch, [self._sibling()])
        matches, err = _prior_cadence_entries_for_url(
            attio, "list-1", _SHORT, "e-old", {},
        )
        assert err == ""
        assert matches == []

    def test_merged_away_entries_excluded(self, monkeypatch):
        # §3.11 soft-deleted losers must not appear as siblings — their
        # winner inherited the cadence.
        from workflows.pre_invite_check import _prior_cadence_entries_for_url

        loser = dict(self._sibling(), merged_into="r-winner")
        attio = self._attio_with_entries(monkeypatch, [loser])
        matches, err = _prior_cadence_entries_for_url(
            attio, "list-1", _SHORT, "e-dup", {},
        )
        assert err == ""
        assert matches == []

    def test_missing_canonical_falls_back_to_person_record(self, monkeypatch):
        # PROSPECT-era entries often lack canonical_linkedin_url; the sibling
        # must still surface via the parent person record's LinkedIn URL.
        from workflows.pre_invite_check import _prior_cadence_entries_for_url

        sibling = dict(self._sibling(), canonical_linkedin_url=None)
        attio = self._attio_with_entries(monkeypatch, [sibling])
        attio.bulk_fetch_persons_by_record_ids.return_value = {
            "r-old": {"values": {"linkedin": [{"value": _LONG}]}},
        }
        matches, err = _prior_cadence_entries_for_url(
            attio, "list-1", _SHORT, "e-dup", {},
        )
        assert err == ""
        assert [m["entry_id"] for m in matches] == ["e-old"]
        assert matches[0]["linkedin_url"] == _LONG
        attio.bulk_fetch_persons_by_record_ids.assert_called_once_with({"r-old"})

    def test_fetch_happens_once_per_cache(self, monkeypatch):
        from workflows.pre_invite_check import _prior_cadence_entries_for_url

        attio = self._attio_with_entries(monkeypatch, [self._sibling()])
        cache: dict = {}
        _prior_cadence_entries_for_url(attio, "list-1", _SHORT, "e-dup", cache)
        _prior_cadence_entries_for_url(attio, "list-1", _LONG, "e-dup", cache)
        assert attio.query_list_entries.call_count == 1

    def test_fetch_failure_degrades_to_empty_with_error(self):
        from workflows.pre_invite_check import _prior_cadence_entries_for_url

        attio = MagicMock()
        attio.query_list_entries.side_effect = RuntimeError("crm down")
        cache: dict = {}
        matches, err = _prior_cadence_entries_for_url(
            attio, "list-1", _SHORT, "e-dup", cache,
        )
        assert matches == []
        assert "crm down" in err
        # The failure is memoized too — no retry storm within a run.
        _prior_cadence_entries_for_url(attio, "list-1", _SHORT, "e-dup", cache)
        assert attio.query_list_entries.call_count == 1


@patch("workflows.daily_check.write_prospects_to_sheet",
       return_value="https://sheet.example/foo")
@patch("workflows.daily_check._pb_session_args", return_value={})
class TestQuarantinePayloadCarriesPriorCadence:
    """End-to-end through `_pre_invite_degree_check`: the escalation payload
    for a quarantined URL-variant duplicate must show the prior cadence."""

    def _row(self):
        return {
            "linkedInUrl": _SHORT,
            "message": "hi",
            "entry_id": "e-dup",
            "record_id": "r-dup",
            "current_stage": "Prospect",
            "experiment_id": "exp-x",
            "experiment_id_frozen_at": "prospect",
            "name": "Dana Quiroga Ramos",
            "company": "Acme Foods",
            "prospect_committed_at": (
                date(2026, 8, 18) - timedelta(days=4)
            ).isoformat(),
        }

    def test_escalation_payload_includes_dm3_sibling(self, _pb_args, _sheet,
                                                     monkeypatch):
        from workflows import pre_invite_check as pic
        from workflows.pre_invite_check import _pre_invite_degree_check

        sibling = {
            "entry_id": "e-old", "record_id": "r-old",
            "canonical_linkedin_url": _LONG,
            "stage": "DM3 Sent", "dm_step": 3,
            "dm1_sent_at": "2026-05-20", "dm2_sent_at": "2026-05-26",
            "dm3_sent_at": "2026-06-01", "last_contact_date": "2026-06-01",
        }
        monkeypatch.setattr(
            pic.AttioClient, "parse_entry", staticmethod(lambda raw: sibling),
        )
        attio = MagicMock()
        attio.query_list_entries.return_value = [{"raw": 1}]
        pb = MagicMock()
        pb.download_result_csv.return_value = (
            "linkedinProfileUrl,connectionDegree\n" + _SHORT + ",1st"
        )
        with patch("workflows.pre_invite_check.escalate") as mock_escalate:
            still, already = _pre_invite_degree_check(
                [self._row()], pb, "scraper-id", attio, "list-id",
                today=date(2026, 8, 18),
            )

        assert still == [] and already == []
        assert mock_escalate.called
        kwargs = mock_escalate.call_args.kwargs
        assert kwargs["type"] == "pattern_a_suspected_duplicate"
        prior = kwargs["payload"]["prior_cadence_entries"]
        assert [p["entry_id"] for p in prior] == ["e-old"]
        assert prior[0]["stage"] == "DM3 Sent"
        assert prior[0]["dm3_sent_at"] == "2026-06-01"
        assert "prior_cadence_lookup_error" not in kwargs["payload"]

    def test_lookup_failure_still_escalates_with_error_marker(
            self, _pb_args, _sheet):
        from workflows.pre_invite_check import _pre_invite_degree_check

        attio = MagicMock()
        attio.query_list_entries.side_effect = RuntimeError("crm down")
        pb = MagicMock()
        pb.download_result_csv.return_value = (
            "linkedinProfileUrl,connectionDegree\n" + _SHORT + ",1st"
        )
        with patch("workflows.pre_invite_check.escalate") as mock_escalate:
            _pre_invite_degree_check(
                [self._row()], pb, "scraper-id", attio, "list-id",
                today=date(2026, 8, 18),
            )

        kwargs = mock_escalate.call_args.kwargs
        assert kwargs["payload"]["prior_cadence_entries"] == []
        assert "crm down" in kwargs["payload"]["prior_cadence_lookup_error"]


class TestQuarantinePayloadSchema:
    def test_payload_validates_against_typeddict(self):
        from workflows.escalation import _validate_payload_against_typeddict

        _validate_payload_against_typeddict("pattern_a_suspected_duplicate", {
            "record_id": "r", "entry_id": "e", "linkedin_url": _SHORT,
            "name": "n", "company": "c", "prospect_committed_at": "2026-08-14",
            "degree": "1st", "prior_cadence_entries": [],
        })

    def test_missing_prior_cadence_entries_rejected(self):
        import pytest

        from workflows.escalation import _validate_payload_against_typeddict
        from workflows.escalation_schemas import EscalationSchemaError

        with pytest.raises(EscalationSchemaError):
            _validate_payload_against_typeddict("pattern_a_suspected_duplicate", {
                "record_id": "r", "entry_id": "e", "linkedin_url": _SHORT,
                "name": "n", "company": "c",
                "prospect_committed_at": "2026-08-14", "degree": "1st",
            })


# ---------------------------------------------------------------------------
# 3. Self-echo idempotency: signature-keyed, not date-keyed
# ---------------------------------------------------------------------------

class TestSelfEchoSignatureIdempotency:
    """A resolved `manual_reply_suppressed_self_echo` row must not re-open on
    subsequent days: the idempotency key is the thread signature
    (entry|template|count), which only changes when the thread changes."""

    def _make_entry(self):
        return {
            "entry_id": "e-dana", "record_id": "r-dana",
            "stage": PipelineStage.DM1_SENT.value,
            "persona": "operations_leaders", "language": "es",
            "dm_step": 1, "quality_score": 75,
            "last_contact_date": "2026-06-20", "experiment_id": None,
        }

    def _make_sn_csv(self, **row) -> str:
        fieldnames = [
            "participantProfileUrl", "participantFullName",
            "isLastMessageFromMe", "lastMessageBody", "lastMessageDate",
            "totalMessageCount",
        ]
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=fieldnames)
        w.writeheader()
        w.writerow({k: row.get(k, "") for k in fieldnames})
        return buf.getvalue()

    def test_idempotency_key_is_thread_signature(self, monkeypatch):
        from models.campaign import load_messages
        from workflows.detect_responses import (
            _looks_like_self_echo,
            detect_responses,
        )

        monkeypatch.setenv("ATTIO_LIST_ID", "test-list-id")
        template = load_messages()["operations_leaders"]["dm1"]["es"]
        echoed = template.replace("[Name]", "Dana").replace(
            "[Company]", "Acme Foods"
        )
        expected_template_id = _looks_like_self_echo(echoed)
        assert expected_template_id is not None

        attio = MagicMock()
        pb = MagicMock()
        attio.query_list_entries.return_value = [self._make_entry()]
        pb.download_result_csv.return_value = self._make_sn_csv(
            participantProfileUrl="https://linkedin.com/sales/people/ACw",
            participantFullName="Dana Quiroga Ramos",
            isLastMessageFromMe="true",
            lastMessageBody=echoed,
            lastMessageDate="2026-08-18T12:00:00Z",
            totalMessageCount="3",
        )
        with patch("workflows.detect_responses.AttioClient.parse_entry",
                   side_effect=lambda e: e), \
             patch("workflows.detect_responses._pb_session_args",
                   return_value={}), \
             patch("workflows.detect_responses.RecordCache") as MockCache, \
             patch("workflows.detect_responses.escalate") as mock_escalate:
            mock_cache = MagicMock()
            mock_cache.get.return_value = (
                "Dana Quiroga Ramos", "Acme Foods", _LONG, "", "",
            )
            MockCache.return_value = mock_cache
            detect_responses(attio, pb, inbox_scraper_id="scraper-123")

        echo_calls = [
            c for c in mock_escalate.call_args_list
            if c.kwargs.get("type") == "manual_reply_suppressed_self_echo"
        ]
        assert len(echo_calls) == 1
        key = echo_calls[0].kwargs["idempotency_key"]
        # Signature-keyed: same thread state → same key on every future run
        # (escalate() then no-ops against the existing row, even resolved).
        assert key == f"e-dana|{expected_template_id}|3"
        # No date component — the old daily re-open vector.
        assert date.today().isoformat() not in key
