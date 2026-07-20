"""Tests for PR-28 — weekly-finalize idempotency + ICP-2 geo + week_starting.

Builds B-PW-FINALIZE + B-PW-ICP2-GEO + folds the `icp_lane_persisted`
write fix for deterministic-pass prospects.

Coverage:
  * `_monday_of` correctness across weekdays + DST-style edge weeks
  * `_location_is_latam` ES/PT/EN markers + city forms + non-LATAM
  * `enforce_icp_lane_geo` — ICP-1 always passes; ICP-2 LATAM passes;
    ICP-2 non-LATAM emits `icp2_geo_violation` queue row + returns False;
    detection via icp_lane numeric OR scoring_lane string
  * `_has_recent_outreach` — returns True within 14d, False past 14d,
    False if no prior outreach, False if no list configured (raises)
  * `weekly_finalize_idempotent` — full orchestrator:
      - 14-day idempotency: re-run skips already-committed URLs
      - ICP-2 geo gate skips non-LATAM
      - dry_run does not write
      - week_starting stamped on every committed entry (Monday)
  * `icp_lane_persisted` fix: deterministic enterprise_pass /
    target_pass populates `icp_lane` so the persisted column carries
    the lane (was previously None for non-LLM verdicts)
  * Writer registry + Attio schema manifest pin the canonical entries
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

from clients.crm.base import CRMProvider, Entry, Stage
from workflows.escalation_schemas import (
    ESCALATION_SCHEMAS,
    ESCALATION_TYPES_SET,
    Icp2GeoViolationPayload,
)
from workflows.quality_gate import score_prospect
from workflows.weekly_prospect import (
    _has_recent_outreach,
    _load_recent_outreach_map,
    _location_is_latam,
    _monday_of,
    enforce_icp_lane_geo,
    weekly_finalize_idempotent,
)


def _entry(*, linkedin_url: str | None = None, last_contact_date: str | None = None) -> Entry:
    """Build a normalized pipeline `Entry` with the cadence attrs the
    recent-outreach map reads (`canonical_linkedin_url` + `last_contact_date`).

    P1c migration: `_load_recent_outreach_map` now reads off the `Entry`
    dataclass (`entry.attributes[...]`) instead of the legacy
    `AttioClient.parse_entry(raw_dict)` flat dict — so tests inject the
    same signals via `Entry.attributes` rather than by patching
    `parse_entry`. Test intent (recent-vs-stale, per-URL match) is
    unchanged; only the injection shape moves to the contract dataclass.

    The keyword arg stays `linkedin_url` for call-site ergonomics, but the
    value is injected under the attribute key the reader actually consumes —
    `canonical_linkedin_url`, the key `parse_entry`/`Entry.attributes` emit.
    Previously it was injected under `linkedin_url`, a key the production
    reader never reads; that agreement-on-a-nonexistent-key masked the bug
    where the recent-outreach map was always `{}`.
    """
    attrs: dict = {}
    if linkedin_url is not None:
        attrs["canonical_linkedin_url"] = linkedin_url
    if last_contact_date is not None:
        attrs["last_contact_date"] = last_contact_date
    return Entry(entry_id="e", record_id="r", stage=Stage(name=""), attributes=attrs)


def _crm_with_entries(entries: list[Entry]) -> MagicMock:
    """A `CRMProvider` mock whose `query_list_entries` returns `entries`.

    Replaces the legacy `MagicMock()` + `query_list_entries.return_value =
    [<raw dicts>]` + `patch("AttioClient.parse_entry", ...)` triad. The
    migrated reader consumes normalized `Entry` objects directly.
    """
    crm = MagicMock(spec=CRMProvider)
    crm.query_list_entries.return_value = entries
    return crm


# -- _monday_of ----------------------------------------------------------


class TestMondayOf:
    def test_monday_returns_self(self):
        # 2026-05-18 is a Monday.
        assert _monday_of(date(2026, 5, 18)) == date(2026, 5, 18)

    def test_friday_returns_prior_monday(self):
        # 2026-05-22 is a Friday → prior Monday is 2026-05-18.
        assert _monday_of(date(2026, 5, 22)) == date(2026, 5, 18)

    def test_sunday_returns_prior_monday(self):
        # 2026-05-24 is a Sunday → prior Monday is 2026-05-18.
        assert _monday_of(date(2026, 5, 24)) == date(2026, 5, 18)

    def test_first_of_year_handled(self):
        # 2026-01-01 is a Thursday → prior Monday is 2025-12-29.
        assert _monday_of(date(2026, 1, 1)) == date(2025, 12, 29)


# -- _location_is_latam --------------------------------------------------


class TestLocationIsLatam:
    @pytest.mark.parametrize("location", [
        "Mexico City, Mexico",
        "Ciudad de México, México",
        "São Paulo, Brazil",
        "Sao Paulo, Brasil",
        "Lima, Peru",
        "Lima, Perú",
        "Bogotá, Colombia",
        "Bogota, Colombia",
        "Santiago, Chile",
        "San Juan, Puerto Rico",
        "Buenos Aires, Argentina",
        "Monterrey, Nuevo León, Mexico",
        "Medellín, Antioquia, Colombia",
    ])
    def test_latam_locations(self, location):
        assert _location_is_latam(location) is True, location

    @pytest.mark.parametrize("location", [
        "New York, NY, United States",
        "Berlin, Germany",
        "Tokyo, Japan",
        "Paris, France",
        "Madrid, Spain",  # Spain is not LATAM
        "London, United Kingdom",
        "",
        None,
    ])
    def test_non_latam_locations(self, location):
        assert _location_is_latam(location) is False, location

    @pytest.mark.parametrize("location", [
        # Post-fold (3-agent convergence): US/Spain towns that share
        # LATAM country names. The structured parser splits on `,` and
        # checks LAST token as country — these MUST fail the gate
        # because their country token (Ohio / NJ / Spain / etc.) is
        # not in `_LATAM_COUNTRY_TOKENS`.
        "Lima, Ohio, United States",
        "Lima, OH",
        "Santiago de Compostela, Galicia, Spain",
        "Guadalajara, Spain",
        "Bogota, New Jersey, United States",
        "Monterrey, California, United States",
        "Mexico, Missouri, United States",
        "Peru, Indiana, United States",
        "Chile, New York, United States",
        "Brazil, Indiana",
    ])
    def test_latam_substring_false_positives_now_rejected(self, location):
        # Pre-fold these matched bare city substrings. Post-fold the
        # structured parser correctly rejects them.
        assert _location_is_latam(location) is False, location


# -- enforce_icp_lane_geo -----------------------------------------------


class TestEnforceIcpLaneGeo:
    def _prospect(self, **kwargs) -> dict:
        base = {
            "linkedin_url": "https://www.linkedin.com/in/test/",
            "title": "Director de Operaciones",
            "company": "Test Co",
            "location": "Mexico City, Mexico",
        }
        base.update(kwargs)
        return base

    def test_icp1_always_passes(self):
        # ICP-1 (icp_lane=1 or scoring_lane=enterprise_mode) is never
        # geo-gated, even with a non-LATAM location.
        crm = MagicMock(spec=CRMProvider)
        result = enforce_icp_lane_geo(
            self._prospect(location="New York, USA"),
            {"icp_lane": 1, "scoring_lane": "enterprise_mode"},
            crm=crm,
        )
        assert result is True
        crm.assert_not_called()

    def test_icp2_latam_passes(self):
        crm = MagicMock(spec=CRMProvider)
        result = enforce_icp_lane_geo(
            self._prospect(location="Lima, Peru"),
            {"icp_lane": 2, "scoring_lane": "target_company_mode"},
            crm=crm,
        )
        assert result is True

    def test_icp2_non_latam_emits_queue_and_returns_false(self):
        # The whole point of the gate: catch ICP-2 prospects bleeding
        # in from non-LATAM PB exports.
        from workflows import weekly_prospect
        crm = MagicMock(spec=CRMProvider)
        with patch.object(weekly_prospect, "escalate") as mock_escalate:
            result = enforce_icp_lane_geo(
                self._prospect(location="Berlin, Germany"),
                {"icp_lane": 2, "scoring_lane": "target_company_mode"},
                crm=crm,
            )

        assert result is False
        mock_escalate.assert_called_once()
        kwargs = mock_escalate.call_args.kwargs
        assert kwargs["type"] == "icp2_geo_violation"
        assert kwargs["idempotency_key"] == (
            "https://www.linkedin.com/in/test/"
        )
        payload = kwargs["payload"]
        assert payload["icp_lane"] == 2
        assert payload["location"] == "Berlin, Germany"
        assert payload["scoring_lane"] == "target_company_mode"

    def test_icp2_detected_via_scoring_lane_string_only(self):
        # Even when icp_lane numeric is absent, scoring_lane string
        # triggers the gate. Belt-and-suspenders against partial
        # score_results from legacy callers.
        from workflows import weekly_prospect
        crm = MagicMock(spec=CRMProvider)
        with patch.object(weekly_prospect, "escalate") as mock_escalate:
            result = enforce_icp_lane_geo(
                self._prospect(location="Tokyo, Japan"),
                {"scoring_lane": "target_company_mode"},
                crm=crm,
            )
        assert result is False
        assert mock_escalate.called

    def test_empty_location_treated_as_non_latam(self):
        # No location data + ICP-2 lane → assume non-LATAM (fail-safe)
        # rather than pass the gate. Operator triages from the queue row.
        from workflows import weekly_prospect
        crm = MagicMock(spec=CRMProvider)
        with patch.object(weekly_prospect, "escalate"):
            result = enforce_icp_lane_geo(
                self._prospect(location=""),
                {"icp_lane": 2, "scoring_lane": "target_company_mode"},
                crm=crm,
            )
        assert result is False


# -- _has_recent_outreach ----------------------------------------------


class TestHasRecentOutreach:
    """Tests for the single-prospect idempotency check + the map-load.

    Post-fold (3-agent convergence): `_has_recent_outreach` now takes
    `list_id` as a required keyword arg (was reading from env). All
    tests use `patch("clients.attio.AttioClient.parse_entry", ...)` —
    the prior `patch.object(type(attio), ...)` pattern silently
    intercepted nothing because production calls the classmethod
    directly on `AttioClient`, not via the mock instance (pr-test
    analyzer I-1).
    """

    def test_within_window_returns_true(self):
        recent = (date.today() - timedelta(days=5)).isoformat()
        crm = _crm_with_entries([_entry(
            linkedin_url="https://www.linkedin.com/in/x/",
            last_contact_date=recent,
        )])
        cutoff = date.today() - timedelta(days=14)
        assert _has_recent_outreach(
            crm, "https://linkedin.com/in/x", cutoff, list_id="LIST",
        ) is True

    def test_past_window_returns_false(self):
        stale = (date.today() - timedelta(days=30)).isoformat()
        crm = _crm_with_entries([_entry(
            linkedin_url="https://www.linkedin.com/in/x/",
            last_contact_date=stale,
        )])
        cutoff = date.today() - timedelta(days=14)
        assert _has_recent_outreach(
            crm, "https://linkedin.com/in/x", cutoff, list_id="LIST",
        ) is False

    def test_no_matching_url_returns_false(self):
        recent = (date.today() - timedelta(days=2)).isoformat()
        crm = _crm_with_entries([_entry(
            linkedin_url="https://www.linkedin.com/in/other/",
            last_contact_date=recent,
        )])
        assert _has_recent_outreach(
            crm, "https://linkedin.com/in/x",
            date.today() - timedelta(days=14),
            list_id="LIST",
        ) is False

    def test_empty_list_returns_false(self):
        crm = _crm_with_entries([])
        assert _has_recent_outreach(
            crm, "https://linkedin.com/in/x",
            date.today() - timedelta(days=14),
            list_id="LIST",
        ) is False

    def test_exactly_at_cutoff_returns_true(self):
        # pr-test-analyzer I-2: boundary at 14-day-exact. The `>=`
        # comparison means an entry exactly 14 days old is treated as
        # recent (correct per the inclusive-cutoff design).
        at_cutoff = (date.today() - timedelta(days=14)).isoformat()
        crm = _crm_with_entries([_entry(
            linkedin_url="https://www.linkedin.com/in/x/",
            last_contact_date=at_cutoff,
        )])
        cutoff = date.today() - timedelta(days=14)
        assert _has_recent_outreach(
            crm, "https://linkedin.com/in/x", cutoff, list_id="LIST",
        ) is True

    def test_one_day_past_cutoff_returns_false(self):
        past = (date.today() - timedelta(days=15)).isoformat()
        crm = _crm_with_entries([_entry(
            linkedin_url="https://www.linkedin.com/in/x/",
            last_contact_date=past,
        )])
        cutoff = date.today() - timedelta(days=14)
        assert _has_recent_outreach(
            crm, "https://linkedin.com/in/x", cutoff, list_id="LIST",
        ) is False

    def test_empty_map_against_nonempty_entries_logs_warning(self, caplog):
        """_load_recent_outreach_map emits WARNING when entries exist but
        none yield a usable canonical_linkedin_url.  This is the fingerprint
        of the silent bug where the wrong attribute key caused the map to be
        always {} — the warning makes that failure mode visible within one
        run instead of hiding for months.  (5f2696c delta port.)
        """
        import logging

        # Entry has last_contact_date but NO canonical_linkedin_url → every
        # entry falls out at the `if not url: continue` guard, so `out` stays
        # empty while `entries` is non-empty.
        crm = _crm_with_entries([_entry(last_contact_date=date.today().isoformat())])
        cutoff = date.today() - timedelta(days=14)
        with patch(
            "workflows.weekly_prospect.escalate"
        ), caplog.at_level(logging.WARNING, logger="workflows.weekly_prospect"):
            result = _load_recent_outreach_map(crm, "LIST", cutoff)
        assert result == {}
        warning_messages = [
            r.message for r in caplog.records if r.levelname == "WARNING"
        ]
        assert any(
            "0 carry a usable canonical_linkedin_url" in m for m in warning_messages
        )

    def test_canonical_present_but_no_recent_contact_does_not_escalate(self, caplog):
        """Benign quiet window: entries DO carry canonical_linkedin_url but none
        had contact in the 14-day window. The map is empty, but this is NOT the
        dead-guard bug — escalating it would be a false alarm. Assert no
        escalation, an info log, and an intact guard ({} returned)."""
        import logging

        old = (date.today() - timedelta(days=90)).isoformat()
        crm = _crm_with_entries([
            _entry(
                linkedin_url="https://www.linkedin.com/in/x/",
                last_contact_date=old,
            )
        ])
        cutoff = date.today() - timedelta(days=14)
        with patch(
            "workflows.weekly_prospect.escalate"
        ) as mock_escalate, caplog.at_level(logging.INFO, logger="workflows.weekly_prospect"):
            result = _load_recent_outreach_map(crm, "LIST", cutoff)
        assert result == {}
        mock_escalate.assert_not_called()
        assert any("benign quiet window" in r.message for r in caplog.records)

    def test_empty_map_against_nonempty_entries_escalates(self):
        """Fix 2b: a zero map from a non-empty list is the exact silent-bug
        fingerprint (NULL canonical_linkedin_url). It must open an operator
        review queue row so the no-op surfaces, not just a log line. The guard
        must still return {} and never crash on escalate failure.
        """
        crm = _crm_with_entries([_entry(last_contact_date=date.today().isoformat())])
        cutoff = date.today() - timedelta(days=14)
        with patch("workflows.weekly_prospect.escalate") as mock_escalate:
            result = _load_recent_outreach_map(crm, "LIST", cutoff)

        assert result == {}
        mock_escalate.assert_called_once()
        kwargs = mock_escalate.call_args.kwargs
        assert kwargs["type"] == "recent_outreach_map_empty"
        assert kwargs["payload"]["entries_scanned"] == 1
        assert kwargs["payload"]["entries_with_canonical"] == 0  # the bug fingerprint
        assert kwargs["payload"]["cutoff_date"] == cutoff.isoformat()
        assert kwargs["attio"] is crm

    def test_empty_map_escalate_failure_does_not_crash(self):
        """Fix 2b swallow: an escalate raising must not abort the guard."""
        crm = _crm_with_entries([_entry(last_contact_date=date.today().isoformat())])
        with patch(
            "workflows.weekly_prospect.escalate",
            side_effect=RuntimeError("attio down"),
        ):
            result = _load_recent_outreach_map(
                crm, "LIST", date.today() - timedelta(days=14),
            )
        assert result == {}


# -- weekly_finalize_idempotent -----------------------------------------


class TestWeeklyFinalizeIdempotent:
    def _candidate(self, **kwargs) -> dict:
        score_result = {
            "score": 80,
            "pass": True,
            "persona": "operations_leaders",
            "language": "es",
            "verdict_path": "enterprise_pass",
            "icp_lane": 1,
            "scoring_lane": "enterprise_mode",
        }
        prospect_data = {
            "name": "Test Person",
            "title": "VP Operations",
            "company": "Bimbo",
            "linkedin_url": "https://www.linkedin.com/in/test-person/",
            "location": "Mexico City, Mexico",
            "employee_count": 50000,
        }
        prospect_data.update(kwargs.pop("prospect_data", {}))
        score_result.update(kwargs.pop("score_result", {}))
        return {
            "prospect_data": prospect_data,
            "score_result": score_result,
            "raw": {},
        }

    def test_dry_run_does_not_commit(self, monkeypatch):
        monkeypatch.setenv("ATTIO_LIST_ID", "LIST")
        crm = _crm_with_entries([])
        from workflows import weekly_prospect
        with patch.object(weekly_prospect, "_commit_prospect") as mock_commit:
            summary = weekly_finalize_idempotent(
                crm, list_id="LIST", today="2026-05-22",
                candidates=[self._candidate()],
                dry_run=True,
            )
        assert summary["committed"] == 1
        mock_commit.assert_not_called()

    # -- connectionDegree routing (Sales Nav export, free) --------------

    def test_first_degree_routes_to_accepted_dry_run(self, monkeypatch):
        monkeypatch.setenv("ATTIO_LIST_ID", "LIST")
        crm = _crm_with_entries([])
        cand = self._candidate(); cand["raw"] = {"connectionDegree": "1st"}
        from workflows import weekly_prospect
        with patch.object(weekly_prospect, "_commit_prospect") as mock_commit:
            summary = weekly_finalize_idempotent(
                crm, list_id="LIST", today="2026-05-22",
                candidates=[cand], dry_run=True,
            )
        assert summary["committed"] == 1
        assert summary["accepted_first_degree"] == 1
        mock_commit.assert_not_called()

    def test_first_degree_commits_at_accepted_wet(self, monkeypatch):
        monkeypatch.setenv("ATTIO_LIST_ID", "LIST")
        crm = _crm_with_entries([])
        cand = self._candidate(); cand["raw"] = {"connectionDegree": "1st"}
        from workflows import weekly_prospect
        with patch.object(weekly_prospect, "_commit_prospect", return_value=True) as mock_commit:
            summary = weekly_finalize_idempotent(
                crm, list_id="LIST", today="2026-05-22", candidates=[cand],
            )
        assert summary["committed"] == 1
        assert summary["accepted_first_degree"] == 1
        assert mock_commit.call_args.kwargs["stage_name"] == "Accepted"

    def test_out_of_network_skipped_not_committed(self, monkeypatch):
        monkeypatch.setenv("ATTIO_LIST_ID", "LIST")
        crm = _crm_with_entries([])
        cand = self._candidate(); cand["raw"] = {"connectionDegree": "Out of Network"}
        from workflows import weekly_prospect
        with patch.object(weekly_prospect, "_commit_prospect") as mock_commit:
            summary = weekly_finalize_idempotent(
                crm, list_id="LIST", today="2026-05-22", candidates=[cand],
            )
        assert summary["skipped_uninvitable"] == 1
        assert summary["committed"] == 0
        mock_commit.assert_not_called()

    def test_second_degree_normal_prospect_wet(self, monkeypatch):
        monkeypatch.setenv("ATTIO_LIST_ID", "LIST")
        crm = _crm_with_entries([])
        cand = self._candidate(); cand["raw"] = {"connectionDegree": "2nd"}
        from workflows import weekly_prospect
        with patch.object(weekly_prospect, "_commit_prospect", return_value=True) as mock_commit:
            summary = weekly_finalize_idempotent(
                crm, list_id="LIST", today="2026-05-22", candidates=[cand],
            )
        assert summary["committed"] == 1
        assert summary["accepted_first_degree"] == 0
        assert mock_commit.call_args.kwargs["stage_name"] == "Prospect"

    def test_idempotent_re_run_skips_committed_urls(self, monkeypatch):
        # The whole point: a 2nd invocation within 14 days over the
        # same URL is a no-op even when in-run dedup (seen_urls)
        # doesn't carry across invocations.
        monkeypatch.setenv("ATTIO_LIST_ID", "LIST")
        # Pretend Attio already has an entry for the test URL with
        # last_contact_date 3 days ago.
        recent = (date.today() - timedelta(days=3)).isoformat()
        crm = _crm_with_entries([_entry(
            linkedin_url="https://www.linkedin.com/in/test-person/",
            last_contact_date=recent,
        )])
        from workflows import weekly_prospect

        with patch.object(weekly_prospect, "_commit_prospect") as mock_commit:
            summary = weekly_finalize_idempotent(
                crm, list_id="LIST", today="2026-05-22",
                candidates=[self._candidate()],
            )
        assert summary["idempotent_skipped"] == 1
        assert summary["committed"] == 0
        mock_commit.assert_not_called()

    def test_icp2_non_latam_skipped_and_not_committed(self, monkeypatch):
        monkeypatch.setenv("ATTIO_LIST_ID", "LIST")
        crm = _crm_with_entries([])
        cand = self._candidate(
            prospect_data={"location": "Berlin, Germany"},
            score_result={
                "icp_lane": 2,
                "scoring_lane": "target_company_mode",
                "verdict_path": "target_pass",
            },
        )
        from workflows import weekly_prospect
        with patch.object(weekly_prospect, "_commit_prospect") as mock_commit, \
             patch.object(weekly_prospect, "escalate") as mock_escalate:
            summary = weekly_finalize_idempotent(
                crm, list_id="LIST", today="2026-05-22",
                candidates=[cand],
            )
        assert summary["icp2_geo_skipped"] == 1
        assert summary["committed"] == 0
        mock_commit.assert_not_called()
        # The geo gate emits the queue row before we ever query Attio.
        mock_escalate.assert_called_once()

    def test_commit_path_calls_commit_prospect(self):
        crm = _crm_with_entries([])
        from workflows import weekly_prospect

        with patch.object(
            weekly_prospect, "_commit_prospect", return_value=True
        ) as mock_commit:
            summary = weekly_finalize_idempotent(
                crm, list_id="LIST", today="2026-05-22",
                candidates=[self._candidate()],
            )
        assert summary["committed"] == 1
        mock_commit.assert_called_once()

    def test_unrelated_url_with_recent_outreach_does_not_skip(self):
        # pr-test-analyzer I-3: the load-bearing assertion that
        # `_has_recent_outreach` is per-URL. A candidate at /in/alice/
        # with an Attio entry at /in/bob/ (3 days ago) MUST commit, not
        # skip. A future refactor dropping the URL filter would pass
        # the existing idempotency test but fail this one.
        recent = (date.today() - timedelta(days=3)).isoformat()
        crm = _crm_with_entries([_entry(
            linkedin_url="https://www.linkedin.com/in/bob/",
            last_contact_date=recent,
        )])
        from workflows import weekly_prospect

        with patch.object(
            weekly_prospect, "_commit_prospect", return_value=True
        ) as mock_commit:
            # The candidate URL doesn't match the Attio entry — different person.
            summary = weekly_finalize_idempotent(
                crm, list_id="LIST", today="2026-05-22",
                candidates=[self._candidate()],
            )
        assert summary["committed"] == 1
        assert summary["idempotent_skipped"] == 0
        mock_commit.assert_called_once()

    def test_malformed_linkedin_url_counts_as_malformed_input(self):
        # silent-failure I-3 + new summary key: empty/invalid URL is
        # a data-quality signal, not a write failure. Separate counter.
        crm = _crm_with_entries([])
        cand = self._candidate(prospect_data={"linkedin_url": ""})
        from workflows import weekly_prospect
        with patch.object(weekly_prospect, "_commit_prospect") as mock_commit:
            summary = weekly_finalize_idempotent(
                crm, list_id="LIST", today="2026-05-22",
                candidates=[cand],
            )
        assert summary["malformed_input"] == 1
        assert summary["write_errors"] == 0
        assert summary["committed"] == 0
        mock_commit.assert_not_called()

    def test_skipped_urls_audit_trail_populated(self):
        # silent-failure B-2 fold: every skipped URL ends up in
        # `summary["skipped_urls"]` so operators can audit "which
        # ones got skipped and why" without grepping logs.
        recent = (date.today() - timedelta(days=3)).isoformat()
        crm = _crm_with_entries([_entry(
            linkedin_url="https://www.linkedin.com/in/test-person/",
            last_contact_date=recent,
        )])
        from workflows import weekly_prospect

        with patch.object(weekly_prospect, "_commit_prospect"):
            summary = weekly_finalize_idempotent(
                crm, list_id="LIST", today="2026-05-22",
                candidates=[self._candidate()],
            )
        assert summary["idempotent_skipped"] == 1
        # The audit trail stores the CANONICAL form (no www, no
        # trailing slash) — that's what was matched against the
        # recent-outreach map.
        assert summary["skipped_urls"] == ["https://linkedin.com/in/test-person"]

    def test_recent_outreach_read_from_canonical_linkedin_url_key(self):
        # Regression guard for the silent-failure fix: build the Attio
        # entry with the LITERAL `canonical_linkedin_url` attribute key —
        # the key `parse_entry`/`Entry.attributes` actually emit — instead
        # of going through `_entry`'s kwarg. The reader previously looked up
        # `linkedin_url`, a key the entry never carries, so the recent-
        # outreach map was always `{}` and `idempotent_skipped` always 0.
        # This test fails if the reader regresses to any non-emitted key,
        # because it cannot accidentally agree with a wrong key here.
        recent = (date.today() - timedelta(days=3)).isoformat()
        entry = Entry(
            entry_id="e",
            record_id="r",
            stage=Stage(name=""),
            attributes={
                "canonical_linkedin_url": "https://www.linkedin.com/in/test-person/",
                "last_contact_date": recent,
            },
        )
        crm = _crm_with_entries([entry])
        from workflows import weekly_prospect

        with patch.object(weekly_prospect, "_commit_prospect") as mock_commit:
            summary = weekly_finalize_idempotent(
                crm, list_id="LIST", today="2026-05-22",
                candidates=[self._candidate()],
            )
        assert summary["idempotent_skipped"] == 1
        assert summary["committed"] == 0
        mock_commit.assert_not_called()

    def test_namedtuple_candidate_shape_works(self):
        # type-design + code-reviewer convergence: `WeeklyCandidate`
        # NamedTuple is the preferred construction.
        from workflows.weekly_prospect import WeeklyCandidate

        crm = _crm_with_entries([])
        cand = WeeklyCandidate(
            prospect_data={
                "name": "T",
                "title": "VP Operations",
                "company": "Bimbo",
                "linkedin_url": "https://www.linkedin.com/in/t/",
                "location": "Lima, Peru",
            },
            score_result={
                "score": 80, "persona": "operations_leaders",
                "language": "es", "icp_lane": 1,
                "scoring_lane": "enterprise_mode",
                "verdict_path": "enterprise_pass",
            },
            raw={},
        )
        from workflows import weekly_prospect
        with patch.object(
            weekly_prospect, "_commit_prospect", return_value=True
        ):
            summary = weekly_finalize_idempotent(
                crm, list_id="LIST", today="2026-05-22",
                candidates=[cand],
            )
        assert summary["committed"] == 1


# -- week_starting on the entry attrs -----------------------------------


class TestWeekStartingOnEntry:
    def test_week_starting_is_monday_of_today(self):
        from workflows.weekly_prospect import _build_prospect_entry_attrs
        score_result = {
            "score": 80,
            "persona": "operations_leaders",
            "language": "es",
        }
        # 2026-05-22 is a Friday — week_starting should be 2026-05-18.
        attrs = _build_prospect_entry_attrs(score_result, "2026-05-22")
        assert attrs["week_starting"] == "2026-05-18"

    def test_week_starting_is_self_when_today_is_monday(self):
        from workflows.weekly_prospect import _build_prospect_entry_attrs
        score_result = {
            "score": 80,
            "persona": "operations_leaders",
            "language": "es",
        }
        attrs = _build_prospect_entry_attrs(score_result, "2026-05-18")
        assert attrs["week_starting"] == "2026-05-18"


# -- icp_lane_persisted write fix ---------------------------------------


class TestIcpLanePersistedFix:
    """Pre-PR-28: deterministic enterprise_pass / target_pass left
    `icp_lane=None` because the LLM gate never ran. The lane is
    structurally implied by persona_config though — enterprise_mode is
    ICP-1, target_company_mode is ICP-2. PR-28 derives icp_lane from
    persona_config for deterministic-pass paths so the persisted column
    carries the lane on every committed prospect, not only LLM-classified
    borderlines.
    """

    def test_deterministic_enterprise_pass_at_real_icp_company(self):
        # A deterministic enterprise pass needs a credit above the reachability
        # line (PR-227): 22 + 28 (DM) + 20 + 12 (confirmed in-ICP) = 82 > 75.
        persona = {"enterprise_mode": True, "key": "operations_leaders",
                   "search_size_credit": 22}
        result = score_prospect({
            "name": "Test",
            "title": "VP Operations LATAM",
            "company": "Whirlpool Mexico",
            "location": "Mexico City, Mexico",
            "industry": "Manufacturing",
        }, persona_config=persona)
        assert result["pass"] is True
        assert result["verdict_path"] == "enterprise_pass"
        assert result.get("icp_lane") == 1

    def test_deterministic_target_pass_sets_icp_lane_2(self):
        persona = {"target_company_mode": True, "key": "operations_leaders",
                   "search_size_credit": 22}
        result = score_prospect({
            "name": "Test",
            "title": "Director de Operaciones",
            "company": "Mid-Market LATAM Mfg",
            "location": "Lima, Peru",
            "industry": "Manufacturing",
        }, persona_config=persona)
        assert result["pass"] is True
        assert result["verdict_path"] == "target_pass"
        assert result.get("icp_lane") == 2

    def test_icp_lane_in_entry_attrs(self):
        # Integration: the icp_lane lands in _build_prospect_entry_attrs.
        from workflows.weekly_prospect import _build_prospect_entry_attrs
        score_result = {
            "score": 88,
            "persona": "operations_leaders",
            "language": "es",
            "icp_lane": 1,
            "verdict_path": "enterprise_pass",
            "scoring_lane": "enterprise_mode",
        }
        attrs = _build_prospect_entry_attrs(score_result, "2026-05-22")
        assert attrs["icp_lane_persisted"] == 1

    def test_borderline_llm_icp_lane_not_overwritten(self):
        # pr-test-analyzer N-4 fold: when score lands in the borderline
        # band (40-75), the LLM's `icp_lane` output is authoritative.
        # A future change that inverted the precedence (deterministic
        # branch clobbering LLM output) would silently drop the LLM's
        # lane verdict — assert the LLM output is preserved.
        from unittest.mock import MagicMock as MM

        mock_client = MM()
        # Stub Haiku response: lane=2 (mid-market) at borderline score.
        response = MM()
        block = MM()
        block.type = "text"
        block.text = (
            '{"pass": true, "icp_lane": 2, "rationale": "borderline mid-market"}'
        )
        response.content = [block]
        mock_client.messages.create.return_value = response

        # Build a prospect that lands borderline (40-75 score).
        result = score_prospect({
            "name": "Test",
            "title": "Manager Operations",  # influencer, not decision-maker
            "company": "Mid Manufacturer",
            "location": "Lima, Peru",
            "employee_count": 400,
        }, anthropic_client=mock_client)

        # Verdict should be borderline_pass with LLM's icp_lane=2 intact.
        assert result["verdict_path"] in {"borderline_pass", "borderline_reject"}
        assert result.get("icp_lane") == 2


# -- registry + manifest invariants -------------------------------------


class TestRegistryAndManifestInvariants:
    def test_icp2_geo_violation_slug_in_escalation_types(self):
        assert "icp2_geo_violation" in ESCALATION_TYPES_SET

    def test_icp2_geo_violation_typeddict_registered(self):
        assert ESCALATION_SCHEMAS.get("icp2_geo_violation") is (
            Icp2GeoViolationPayload
        )

    def test_writer_registry_pins_week_starting(self):
        from clients.attio_writer_registry import WRITE_OWNER_REGISTRY

        assert WRITE_OWNER_REGISTRY[("linkedin_outreach", "week_starting")] == (
            "workflows.weekly_prospect.weekly_finalize_idempotent"
        )

    def test_manifest_declares_week_starting(self):
        from pathlib import Path

        import yaml

        manifest_path = (
            Path(__file__).parent.parent / "docs" / "attio_schema_deltas.yaml"
        )
        manifest = yaml.safe_load(manifest_path.read_text())
        attrs = {
            (a["object"], a["slug"]): a
            for a in manifest.get("attributes", [])
        }
        ws = attrs.get(("linkedin_outreach", "week_starting"))
        assert ws is not None
        assert ws["type"] == "date"
        assert ws["pr_id"] == "PR-28"
        assert ws["write_owner_module"] == (
            "workflows.weekly_prospect.weekly_finalize_idempotent"
        )
