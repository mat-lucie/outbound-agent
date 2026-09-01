"""Person-level language override.

Company HQ country is the wrong key for LATAM-based staff of non-LATAM
multinationals: a director who writes an entirely Portuguese profile at a
group whose true HQ is in Europe resolves to EN under every HQ-derived
inference. Backfilling company HQ country makes those rows look
CORROBORATED while being wrong.

`people.language` is the fix: an explicit per-person truth that outranks
company HQ (person override > company HQ > lane default), plus a source
label so the send-dms dry-run says WHY a language was chosen.

Tests drive the real `run_dm_sequencing` over faked externals, reusing the
harness from tests/test_language_hq_advisory.py.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import httpx
import pytest

from clients.attio import AttioClient
from models.enums import Language
from models.resolution import (
    BROKEN_OVERRIDE_SOURCES,
    LANGUAGE_OVERRIDE_READ_FAILED,
    UNVERIFIED_LANGUAGE_SOURCES,
    LanguageSource,
    MissingLanguageError,
    classify_language_source,
    coerce_language,
    has_person_override,
    resolve_language,
    resolve_language_with_source,
    should_report_language_source,
)
from tests.test_language_hq_advisory import _lane_entry, _run_sequencing

# ==================================================================
# coerce_language — the one place an unusable value degrades
# ==================================================================


class TestCoerceLanguage:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("es", Language.ES),
            ("en", Language.EN),
            ("pt", Language.PT),
            ("PT", Language.PT),      # select titles are not case-normalized
            ("  pt  ", Language.PT),  # nor whitespace-trimmed
            (Language.PT, Language.PT),
        ],
    )
    def test_accepts_usable_values(self, raw, expected):
        assert coerce_language(raw) is expected

    @pytest.mark.parametrize("raw", [None, "", "  ", 42, object(), ["pt"], {"pt": 1}])
    def test_rejects_unusable_values(self, raw):
        assert coerce_language(raw) is None

    def test_rejects_a_select_option_with_no_copy(self):
        """A workspace `people.language` select may offer codes the copy
        library has no templates for (`fr` is the canonical example). Such a
        value must degrade to "no override" rather than crash a send or ship
        untranslated copy."""
        assert coerce_language("fr") is None


# ==================================================================
# resolve_language — precedence
# ==================================================================


class TestResolveLanguagePrecedence:
    def test_override_outranks_stored_entry_language(self):
        """The whole point: the entry could say anything; a human said PT."""
        assert resolve_language({"language": "en"}, person_override="pt") is Language.PT

    def test_override_rescues_a_row_with_no_stored_language(self):
        """A human-checked value is better evidence than the absent one it
        replaces, so it resolves instead of raising."""
        assert resolve_language({"language": None}, person_override="pt") is Language.PT

    def test_unusable_override_falls_through_to_stored_value(self):
        assert resolve_language({"language": "es"}, person_override="fr") is Language.ES

    def test_unusable_override_does_not_rescue_a_missing_language(self):
        """Fail-closed contract survives: an ignored override leaves the row
        exactly as broken as it was."""
        with pytest.raises(MissingLanguageError):
            resolve_language({"language": None}, person_override="fr")

    def test_absent_override_is_the_pre_existing_behavior(self):
        assert resolve_language({"language": "es"}) is Language.ES
        with pytest.raises(MissingLanguageError):
            resolve_language({"language": None})


# ==================================================================
# classify_language_source — the "why"
# ==================================================================


class TestClassifyLanguageSource:
    def test_person_override_wins_over_every_other_signal(self):
        assert classify_language_source(
            Language.PT,
            person_override="pt",
            hq_expected=Language.EN,
            scoring_lane="enterprise_mode",
        ) is LanguageSource.PERSON_OVERRIDE

    def test_us_mode_is_english_by_construction(self):
        assert classify_language_source(
            Language.EN, hq_expected=Language.EN, scoring_lane="us_mode",
        ) is LanguageSource.US_MODE

    def test_no_hq_is_lane_default(self):
        assert classify_language_source(
            Language.ES, hq_expected=None,
        ) is LanguageSource.LANE_DEFAULT

    def test_hq_english_is_a_catchall_not_corroboration(self):
        """`detect_language_from_country` returns "en" for EVERY non-LATAM
        code, so an HQ-derived EN cannot distinguish a genuine English
        expectation from a Portuguese-speaking director at a European-HQ
        multinational."""
        assert classify_language_source(
            Language.PT, hq_expected=Language.EN,
        ) is LanguageSource.COMPANY_HQ_CATCHALL

    def test_hq_latam_agreeing_is_corroboration(self):
        assert classify_language_source(
            Language.ES, hq_expected=Language.ES,
        ) is LanguageSource.COMPANY_HQ

    def test_hq_latam_disagreeing_is_reported_honestly(self):
        """es↔pt: a Brazilian GM at a Mexico-HQ company. Never a blocker,
        but not corroboration either — so it must not be labelled
        `company-hq` as if HQ agreed."""
        assert classify_language_source(
            Language.PT, hq_expected=Language.ES,
        ) is LanguageSource.COMPANY_HQ_DISAGREES

    def test_tolerates_a_bare_code_string_for_language(self):
        """Several send-path tests patch `resolve_language` to return a plain
        code. A shape difference must not masquerade as an es↔pt divergence."""
        assert classify_language_source(
            "es", hq_expected=Language.ES,
        ) is LanguageSource.COMPANY_HQ

    def test_a_present_but_unusable_override_is_never_mistaken_for_absence(self):
        """A selectable option with no copy behind it. Without its own source
        the row would read as "nobody ever checked this" when a human just
        did — and the HQ branches could even label it `company-hq`, which
        prints nothing."""
        assert classify_language_source(
            Language.ES, person_override="fr", hq_expected=Language.ES,
        ) is LanguageSource.OVERRIDE_UNUSABLE

    def test_a_failed_read_is_never_mistaken_for_absence(self):
        """THE ASYMMETRY THAT MAKES THIS NECESSARY: absence can classify as
        COMPANY_HQ, which is silent. Collapsing a failed read into absence
        would make a prospect whose override was just lost look HEALTHIER
        than an ordinary row."""
        assert classify_language_source(
            Language.ES,
            person_override=LANGUAGE_OVERRIDE_READ_FAILED,
            hq_expected=Language.ES,
        ) is LanguageSource.OVERRIDE_READ_FAILED

    def test_broken_override_outranks_the_us_mode_short_circuit(self):
        """us_mode is EN by construction, but a broken override is a fact
        about the row that no lane convention makes safe."""
        assert classify_language_source(
            Language.EN,
            person_override=LANGUAGE_OVERRIDE_READ_FAILED,
            scoring_lane="us_mode",
        ) is LanguageSource.OVERRIDE_READ_FAILED

    def test_non_string_override_is_treated_as_absent(self):
        """The getter's contract is `str | None`; a non-str is out of
        contract (in practice a test double) and must not manufacture a
        warning."""
        assert classify_language_source(
            Language.ES, person_override=MagicMock(), hq_expected=Language.ES,
        ) is LanguageSource.COMPANY_HQ

    def test_only_verified_sources_stay_out_of_the_warn_set(self):
        assert {
            LanguageSource.COMPANY_HQ_CATCHALL,
            LanguageSource.COMPANY_HQ_DISAGREES,
            LanguageSource.LANE_DEFAULT,
            LanguageSource.OVERRIDE_UNUSABLE,
            LanguageSource.OVERRIDE_READ_FAILED,
        } == UNVERIFIED_LANGUAGE_SOURCES
        for verified in (
            LanguageSource.PERSON_OVERRIDE,
            LanguageSource.COMPANY_HQ,
            LanguageSource.US_MODE,
        ):
            assert verified not in UNVERIFIED_LANGUAGE_SOURCES

    def test_broken_sources_are_a_subset_of_unverified(self):
        """A broken override must never be reportable-but-not-warned."""
        assert BROKEN_OVERRIDE_SOURCES <= UNVERIFIED_LANGUAGE_SOURCES
        assert {
            LanguageSource.OVERRIDE_UNUSABLE,
            LanguageSource.OVERRIDE_READ_FAILED,
        } == BROKEN_OVERRIDE_SOURCES


class TestShouldReportLanguageSource:
    """The whole warn/stay-silent policy in one predicate."""

    @pytest.mark.parametrize("source", [
        LanguageSource.PERSON_OVERRIDE,
        LanguageSource.COMPANY_HQ,
        LanguageSource.US_MODE,
    ])
    @pytest.mark.parametrize("dry_run", [True, False])
    def test_verified_sources_never_report(self, source, dry_run):
        assert should_report_language_source(source, dry_run=dry_run) is False

    @pytest.mark.parametrize("source", [
        LanguageSource.LANE_DEFAULT,
        LanguageSource.COMPANY_HQ_CATCHALL,
        LanguageSource.COMPANY_HQ_DISAGREES,
    ])
    def test_unverified_inferences_report_on_dry_runs_only(self, source):
        assert should_report_language_source(source, dry_run=True) is True
        assert should_report_language_source(source, dry_run=False) is False

    @pytest.mark.parametrize("source", [
        LanguageSource.OVERRIDE_UNUSABLE,
        LanguageSource.OVERRIDE_READ_FAILED,
    ])
    def test_broken_overrides_report_on_the_wet_path_too(self, source):
        """Dry-run and wet are separate processes with separate caches, so
        the wet run re-reads every override — one present at preview can be
        LOST at send time. A dry-run-only warning cannot catch that."""
        assert should_report_language_source(source, dry_run=True) is True
        assert should_report_language_source(source, dry_run=False) is True


class TestHasPersonOverride:
    """The single definition of "this row was human-checked" — both guards
    call it directly so the suppression decision can't drift with
    `classify_language_source`'s branch ordering."""

    @pytest.mark.parametrize("raw", ["pt", "es", "en", Language.PT])
    def test_true_for_usable_values(self, raw):
        assert has_person_override(raw) is True

    @pytest.mark.parametrize(
        "raw", [None, "", "fr", LANGUAGE_OVERRIDE_READ_FAILED, 42],
    )
    def test_false_for_everything_the_send_path_cannot_use(self, raw):
        assert has_person_override(raw) is False


class TestResolveLanguageWithSource:
    def test_returns_both_halves(self):
        assert resolve_language_with_source(
            {"language": "en"}, person_override="pt", hq_expected=Language.EN,
        ) == (Language.PT, LanguageSource.PERSON_OVERRIDE)

    def test_propagates_missing_language(self):
        with pytest.raises(MissingLanguageError):
            resolve_language_with_source({"language": None})


# ==================================================================
# AttioClient.person_language_override
# ==================================================================


def _client() -> AttioClient:
    with patch.dict("os.environ", {"ATTIO_API_KEY": "k"}):
        return AttioClient()


class TestPersonLanguageOverrideGetter:
    @pytest.mark.parametrize(
        "values,expected",
        [
            ({"language": [{"option": {"title": "pt"}}]}, "pt"),
            ({"language": [{"value": "pt"}]}, "pt"),
            ({"language": "pt"}, "pt"),
            ({"language": [{"option": {"title": "PT"}}]}, "pt"),  # lower-cased
            ({}, None),
            ({"language": []}, None),
            ({"language": None}, None),
        ],
    )
    def test_reads_every_person_record_shape(self, values, expected):
        client = _client()
        with patch.object(
            client, "get_person", return_value={"values": values},
        ):
            assert client.person_language_override("p1") == expected

    def test_returns_unsupported_codes_verbatim_for_the_resolver_to_reject(self):
        """The getter does NOT validate — validation lives in
        `coerce_language`, so an unsupported value degrades at exactly one
        place instead of being silently dropped here."""
        client = _client()
        with patch.object(
            client, "get_person", return_value={"values": {"language": "fr"}},
        ):
            assert client.person_language_override("p1") == "fr"
        assert coerce_language("fr") is None

    def test_empty_record_id_never_fetches(self):
        client = _client()
        with patch.object(client, "get_person") as get_person:
            assert client.person_language_override("") is None
        get_person.assert_not_called()

    def test_result_is_cached_so_a_second_read_is_free(self):
        client = _client()
        with patch.object(
            client, "get_person", return_value={"values": {"language": "pt"}},
        ) as get_person:
            assert client.person_language_override("p1") == "pt"
            assert client.person_language_override("p1") == "pt"
        assert get_person.call_count == 1

    def test_absence_is_cached_too(self):
        """None is the normal answer on a narrow exception list — caching it
        is what keeps the send path from re-fetching every prospect."""
        client = _client()
        with patch.object(
            client, "get_person", return_value={"values": {}},
        ) as get_person:
            assert client.person_language_override("p1") is None
            assert client.person_language_override("p1") is None
        assert get_person.call_count == 1

    def test_fetch_error_returns_the_sentinel_not_none(self, capsys):
        """Never raises into the send path, but must NOT collapse into the
        "no override" answer — that would make a lost override invisible."""
        client = _client()
        with patch.object(
            client, "get_person", side_effect=httpx.ConnectError("boom"),
        ):
            result = client.person_language_override("p1")
        assert result is LANGUAGE_OVERRIDE_READ_FAILED
        assert result is not None
        assert "person_language_override" in capsys.readouterr().err

    def test_a_failed_read_is_not_cached_so_a_blip_is_retried(self):
        """Caching the failure would freeze a one-off error in for the whole
        run, silently disabling the override for that prospect."""
        client = _client()
        with patch.object(
            client, "get_person",
            side_effect=[httpx.ConnectError("boom"), {"values": {"language": "pt"}}],
        ) as get_person:
            assert client.person_language_override("p1") is LANGUAGE_OVERRIDE_READ_FAILED
            assert client.person_language_override("p1") == "pt"
        assert get_person.call_count == 2

    def test_missing_record_is_no_override(self):
        client = _client()
        with patch.object(client, "get_person", return_value=None):
            assert client.person_language_override("p1") is None

    def test_extract_record_info_primes_the_cache_for_free(self):
        """The send path's zero-extra-API-call guarantee: the bulk person
        preload already calls extract_record_info, so the override rides
        along with it."""
        client = _client()
        record = {
            "id": {"record_id": "p1"},
            "values": {
                "name": [{"first_name": "Ana", "last_name": "Diaz"}],
                "language": [{"option": {"title": "pt"}}],
            },
        }
        client.extract_record_info(record)
        with patch.object(client, "get_person") as get_person:
            assert client.person_language_override("p1") == "pt"
        get_person.assert_not_called()

    def test_invalidate_forces_a_refetch(self):
        client = _client()
        with patch.object(
            client, "get_person", return_value={"values": {"language": "pt"}},
        ) as get_person:
            client.person_language_override("p1")
            client.invalidate_person_language("p1")
            client.person_language_override("p1")
        assert get_person.call_count == 2


class TestPersonSelectExtractor:
    """One implementation of person-record select-shape handling, on the
    client, so every reader of a person select parses it identically."""

    @pytest.mark.parametrize(
        "values,expected",
        [
            ({"f": [{"option": {"title": "queued"}}]}, "queued"),
            ({"f": [{"value": "queued"}]}, "queued"),
            ({"f": "  queued  "}, "queued"),
            ({"f": [{}]}, ""),
            ({"f": ["scalar-in-list"]}, ""),
            ({}, ""),
        ],
    )
    def test_reads_every_shape_without_raising(self, values, expected):
        assert AttioClient.extract_person_select_value(values, "f") == expected


# ==================================================================
# Send path — the regression this exists to prevent
# ==================================================================


def _run(entries, *, hq_country, person_language, capsys, get_message_mock=None):
    """`_run_sequencing` (shared harness) with the person override wired."""
    return _run_sequencing(
        entries, hq_country=hq_country, dry_run=True, capsys=capsys,
        person_language=person_language, get_message_mock=get_message_mock,
    )


class TestSendPathPersonOverride:
    def test_non_latam_hq_without_override_still_warns_after_hq_backfill(
        self, capsys,
    ):
        """THE REGRESSION GUARD. Backfilling a European-HQ company's country
        makes the HQ-derived expectation EN — under the old
        `expected_lang is None` advisory the row would have gone silent,
        hiding exactly the case the HQ key gets wrong. It must still warn,
        as a catch-all."""
        result, out, _ = _run(
            [_lane_entry(language="pt")], hq_country="GB",
            person_language=None, capsys=capsys,
        )
        assert result["language_unverified"] == 1
        assert "company-hq-catchall" in out
        assert "LATAM-based staff of multinationals" in out

    def test_person_override_is_reported_but_never_warns(self, capsys):
        """A human already checked this row; re-warning every run trains the
        operator to skim past the line."""
        result, out, escalate_mock = _run(
            [_lane_entry(language="pt")], hq_country="GB",
            person_language="pt", capsys=capsys,
        )
        assert result["language_unverified"] == 0
        assert "Verify before approving" not in out
        # Advisory only in either direction — the send is never gated here.
        assert result["dry_run"]["dm1"] == 1
        escalate_mock.assert_not_called()

    def test_override_changes_the_language_actually_rendered(self, capsys):
        """Whatever the entry says, PT is what goes out."""
        get_message = MagicMock(return_value="Olá [firstName]")
        _run(
            [_lane_entry(language="en")], hq_country="GB",
            person_language="pt", capsys=capsys, get_message_mock=get_message,
        )
        assert get_message.call_args.args[1] is Language.PT

    def test_override_suppresses_a_hq_derived_mismatch_skip(self, capsys):
        """HQ says ES (Mexico-HQ parent), the human says PT. Person-level
        truth outranks company HQ, so the row must not be skipped."""
        result, _out, escalate_mock = _run(
            [_lane_entry(language="en")], hq_country="MX",
            person_language="pt", capsys=capsys,
        )
        assert result["skipped_language_mismatch"] == 0
        escalate_mock.assert_not_called()

    def test_without_override_the_hq_mismatch_guard_still_fires(self, capsys):
        """Control for the test above: EN copy landing on a LATAM contact
        must still be caught."""
        result, _out, escalate_mock = _run(
            [_lane_entry(language="en")], hq_country="MX",
            person_language=None, capsys=capsys,
        )
        assert result["skipped_language_mismatch"] == 1
        assert escalate_mock.call_args.kwargs["type"] == "language_mismatch"

    def test_us_mode_lane_violation_still_flags_despite_an_override(self, capsys):
        """us_mode copy is English by construction. A non-EN override there
        would ship untranslated copy — a lane violation, not a person-level
        truth the guard should defer to."""
        result, _out, escalate_mock = _run(
            [_lane_entry(language="pt", scoring_lane="us_mode")],
            hq_country=None, person_language="pt", capsys=capsys,
        )
        assert result["skipped_language_mismatch"] == 1
        escalate_mock.assert_called()

    def test_unusable_override_is_visible_rather_than_silently_discarded(
        self, capsys,
    ):
        """A code with no copy set by hand in the CRM UI. The row must not
        read as `company-hq` (silent) just because the value can't render."""
        result, out, _ = _run(
            [_lane_entry(language="es")], hq_country="MX",
            person_language="fr", capsys=capsys,
        )
        assert result["language_override_broken"] == 1
        assert "override-unusable" in out

    def test_broken_override_counts_on_the_wet_path_too(self, capsys):
        """The dry-run-only rollup stays silent for a lane default on a wet
        run, but a BROKEN override is a data-integrity signal that must
        surface even there."""
        result, out, _ = _run_sequencing(
            [_lane_entry(language="es")], hq_country="MX",
            person_language=LANGUAGE_OVERRIDE_READ_FAILED,
            dry_run=False, capsys=capsys,
        )
        assert result["language_override_broken"] == 1
        assert result["language_unverified"] == 1
        assert "BROKEN" in out

    def test_every_unverified_source_has_an_operator_hint(self):
        """A new unverified source without a hint would print a bare label
        the operator can't act on — or KeyError mid-dry-run."""
        from workflows.daily_check import _LANGUAGE_SOURCE_HINTS

        for source in UNVERIFIED_LANGUAGE_SOURCES:
            assert source in _LANGUAGE_SOURCE_HINTS
            assert _LANGUAGE_SOURCE_HINTS[source].strip()


# ==================================================================
# Connection-note (invite) path — a wet LinkedIn send of its own
# ==================================================================


def _invite_attrs(record_id: str, language: str | None = "es") -> dict:
    return {
        "record_id": record_id,
        "entry_id": f"entry-{record_id}",
        "stage": "Prospect",
        "persona": "operations_leaders",
        "language": language,
        "scoring_lane": "enterprise_mode",
        "quality_score": 70,
        "invite_eligible_after": None,
        "experiment_id": None,
        "experiment_id_frozen_at": None,
    }


def _invite_attio(*, hq_country: str | None, person_language: str | None):
    attio = MagicMock()
    attio._person_to_company = {"r1": "c1"}
    attio.get_company.return_value = {"values": {}}  # un-throttled
    attio.company_hq_country_code.return_value = hq_country
    attio.person_language_override.return_value = person_language
    return attio


def _invite_cache():
    cache = MagicMock()
    cache.get.side_effect = lambda rid: (
        f"Name {rid}", f"Company {rid}", f"https://linkedin.com/in/{rid}",
        None, "Director",
    )
    return cache


class TestConnectionNotePathPersonOverride:
    """The invite path got the same three edits as the DM path but in a
    different code shape, which is exactly where mirrored logic breaks. An
    invite is a wet LinkedIn send, so it needs its own coverage."""

    def _run_invite(self, *, hq_country, person_language, entry_language="es"):
        """Returns (to_send, counts, escalate_mock, personalize_mock).

        The invite row carries no `language` key in its send data — the
        language is baked into the rendered note — so the rendered language
        is read off the `personalize(language=...)` kwarg.
        """
        from workflows.daily_check import _build_invite_send_data

        personalize_mock = MagicMock(return_value="NOTE")
        with (
            patch("workflows.daily_check.get_message", return_value="TEMPLATE"),
            patch("workflows.daily_check.personalize", personalize_mock),
            patch("workflows.daily_check.get_industry_label",
                  return_value="manufactura"),
            patch("workflows.daily_check.escalate") as escalate_mock,
        ):
            to_send, counts = _build_invite_send_data(
                [_invite_attrs("r1", language=entry_language)],
                target=1,
                attio=_invite_attio(
                    hq_country=hq_country, person_language=person_language,
                ),
                cache=_invite_cache(),
                today=date(2026, 8, 13),
                audit_logger=None,
                dry_run=True,
            )
        return to_send, counts, escalate_mock, personalize_mock

    def test_override_changes_the_invite_language(self):
        to_send, _, _, personalize_mock = self._run_invite(
            hq_country="GB", person_language="pt", entry_language="en",
        )
        assert len(to_send) == 1
        assert personalize_mock.call_args.kwargs["language"] is Language.PT

    def test_override_suppresses_the_hq_derived_mismatch_skip(self):
        """HQ says ES, the human says PT — person-level truth wins, so the
        invite must go out rather than be skipped."""
        to_send, counts, escalate_mock, _ = self._run_invite(
            hq_country="MX", person_language="pt", entry_language="en",
        )
        assert counts["language_mismatch"] == 0
        assert len(to_send) == 1
        escalate_mock.assert_not_called()

    def test_without_override_the_invite_mismatch_guard_still_fires(self):
        """Control: an EN invite bound for a LATAM contact is still caught."""
        to_send, counts, escalate_mock, _ = self._run_invite(
            hq_country="MX", person_language=None, entry_language="en",
        )
        assert counts["language_mismatch"] == 1
        assert to_send == []
        assert escalate_mock.call_args.kwargs["type"] == "language_mismatch"

    def test_override_rescues_an_invite_with_no_stored_language(self):
        to_send, counts, _, personalize_mock = self._run_invite(
            hq_country="GB", person_language="pt", entry_language=None,
        )
        assert counts["missing_language"] == 0
        assert personalize_mock.call_args.kwargs["language"] is Language.PT
