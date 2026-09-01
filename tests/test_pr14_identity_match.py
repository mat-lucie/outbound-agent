"""PR-14 (B-SD-004 + B-PD-004 + B-PD-006): identity match + vanity-URL
backfill + literal-"Unknown" guard.

Covers four pieces:

  1. `workflows.identity_match.normalize_for_match` — diacritic-stable
     name normalization (NFKD + ASCII strip).
  2. `workflows.identity_match.match_thread_to_entries` — ranked
     LinkedIn-thread-to-entry matching with vanity-slug preference.
  3. `workflows.record_cache.RecordCache.get` — returns `None` for
     missing fields instead of the literal string "Unknown" (B-PD-006).
  4. `scripts.backfill_vanity_url_slug` — populates `vanity_url_slug`
     from `canonical_linkedin_url` (B-PD-004 / PR-9a-dependent).

§3.1 protection: PR-14 closes the silent "Unknown" string fallback
that let downstream consumers ship messages with the literal token
instead of the prospect's name. The diacritic-normalized matcher
adds reliability to thread attribution — accents from LATAM names
no longer cause spurious mismatches.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from workflows.identity_match import (
    MatchCandidate,
    match_thread_to_entries,
    normalize_for_match,
)
from workflows.record_cache import RecordCache

# ==================================================================
# normalize_for_match — diacritic + casing stability
# ==================================================================


class TestNormalizeForMatch:
    def test_lowercases_and_collapses_whitespace(self):
        assert normalize_for_match("Carlos Lopez") == "carlos lopez"
        assert normalize_for_match("  Carlos   Lopez  ") == "carlos lopez"

    def test_strips_diacritics(self):
        """The B-SD-004 root case: 'Carlos López' must match 'Carlos Lopez'."""
        assert normalize_for_match("Carlos López") == "carlos lopez"
        assert normalize_for_match("José María") == "jose maria"
        assert normalize_for_match("Iñigo Marchal") == "inigo marchal"
        assert normalize_for_match("Andrés Pérez") == "andres perez"

    def test_strips_portuguese_accents(self):
        assert normalize_for_match("João Pereira") == "joao pereira"
        assert normalize_for_match("Antônio") == "antonio"
        assert normalize_for_match("Conceição") == "conceicao"

    def test_empty_string_returns_empty(self):
        assert normalize_for_match("") == ""

    def test_unicode_normalization_handles_combining_forms(self):
        """A name composed with combining marks must normalize the same
        as one composed with precomposed characters."""
        combining = "Café"  # 'e' + combining acute
        precomposed = "Café"
        assert normalize_for_match(combining) == normalize_for_match(precomposed)


# ==================================================================
# match_thread_to_entries — ranked matching
# ==================================================================


class TestMatchThreadToEntries:
    def test_returns_empty_for_no_entries(self):
        result = match_thread_to_entries("Carlos López", "https://linkedin.com/in/carlos", [])
        assert result == []

    def test_vanity_slug_exact_match_scores_1_0(self):
        entries = [
            {"entry_id": "e1", "record_id": "r1",
             "vanity_url_slug": "carlos-lopez", "name": "Different Name",
             "linkedin_url": "https://linkedin.com/in/carlos-lopez"},
        ]
        result = match_thread_to_entries(
            "Different Name", "https://linkedin.com/in/carlos-lopez", entries,
        )
        assert len(result) == 1
        assert result[0].score == 1.0
        assert result[0].reason == "vanity_slug_exact"
        assert result[0].entry_id == "e1"

    def test_diacritic_normalized_name_match_scores_0_85(self):
        """B-SD-004 contract: 'Carlos López' (thread) matches
        'Carlos Lopez' (entry name without accent)."""
        entries = [
            {"entry_id": "e1", "record_id": "r1",
             "vanity_url_slug": "", "name": "Carlos Lopez",
             "linkedin_url": ""},
        ]
        result = match_thread_to_entries("Carlos López", None, entries)
        assert len(result) == 1
        assert result[0].score == 0.85
        assert result[0].reason == "name_diacritic_exact"

    def test_first_last_match_scores_0_70(self):
        """Middle name on one side but not the other still matches
        at the lower-confidence band."""
        entries = [
            {"entry_id": "e1", "record_id": "r1",
             "vanity_url_slug": "", "name": "Carlos Andres Lopez",
             "linkedin_url": ""},
        ]
        result = match_thread_to_entries("Carlos Lopez", None, entries)
        assert len(result) == 1
        assert result[0].score == 0.70
        assert result[0].reason == "name_first_last"

    def test_vanity_slug_outranks_name_match(self):
        """When two entries match by different signals, the
        vanity-slug match (1.0) outranks the name match (0.85)."""
        entries = [
            {"entry_id": "e_name", "record_id": "r1",
             "vanity_url_slug": "different-slug", "name": "Carlos López",
             "linkedin_url": "https://linkedin.com/in/different-slug"},
            {"entry_id": "e_slug", "record_id": "r2",
             "vanity_url_slug": "carlos-lopez", "name": "Different Person",
             "linkedin_url": "https://linkedin.com/in/carlos-lopez"},
        ]
        result = match_thread_to_entries(
            "Carlos López", "https://linkedin.com/in/carlos-lopez", entries,
        )
        # Both match — vanity-slug must rank first.
        assert len(result) == 2
        assert result[0].score == 1.0
        assert result[0].entry_id == "e_slug"
        assert result[1].score == 0.85

    def test_below_threshold_returns_no_match(self):
        """Single-name match (only first or only last) is sub-threshold."""
        entries = [
            {"entry_id": "e1", "record_id": "r1",
             "vanity_url_slug": "", "name": "Carlos",
             "linkedin_url": ""},
        ]
        # Thread says "Different Person" — no token in common, no slug.
        result = match_thread_to_entries("Different Person", None, entries)
        assert result == []

    def test_vanity_slug_recomputed_from_linkedin_url(self):
        """An entry with an empty vanity_url_slug but a valid
        linkedin_url still matches via on-the-fly slug computation
        (pre-PR-9a rows that haven't been backfilled yet)."""
        entries = [
            {"entry_id": "e1", "record_id": "r1",
             "vanity_url_slug": "",
             "name": "Different",
             "linkedin_url": "https://linkedin.com/in/carlos-lopez"},
        ]
        result = match_thread_to_entries(
            "Other", "https://linkedin.com/in/carlos-lopez", entries,
        )
        assert len(result) == 1
        assert result[0].score == 1.0

    def test_returns_ranked_list_for_caller_disambiguation(self):
        """The function returns a ranked list, not a single match,
        per the docstring contract — the caller chooses whether to
        accept the top match or escalate ambiguity."""
        entries = [
            {"entry_id": "e1", "record_id": "r1",
             "vanity_url_slug": "", "name": "Carlos Lopez",
             "linkedin_url": ""},
            {"entry_id": "e2", "record_id": "r2",
             "vanity_url_slug": "", "name": "Carlos Andres Lopez",
             "linkedin_url": ""},
        ]
        result = match_thread_to_entries("Carlos López", None, entries)
        assert len(result) == 2
        assert result[0].score == 0.85
        assert result[1].score == 0.70


# ==================================================================
# RecordCache returns None instead of "Unknown" (B-PD-006)
# ==================================================================


class TestRecordCacheUnknownGuard:
    def test_missing_record_returns_none_fields(self):
        """B-PD-006 root case: when the Attio record isn't found,
        RecordCache.get returns (None, None, "", None, "") — NOT
        the pre-PR-14 ("Unknown", "Unknown", "", "", "") literal.
        Downstream consumers MUST use `is None`, not `== "Unknown"`.
        """
        attio = MagicMock()
        attio.get_person.return_value = None
        cache = RecordCache(attio)

        name, company, linkedin, industry, title = cache.get("missing-id")
        assert name is None
        assert company is None
        assert linkedin == ""  # backward-compat: `if linkedin_url:` checks
        assert industry is None
        assert title == ""

    def test_extract_record_info_returns_none_for_missing_fields(self):
        """B-PD-006: extract_record_info ALSO returns None for missing
        name/company/industry (not "Unknown")."""
        from clients.attio import AttioClient

        # Minimal record with no name/company/industry/title.
        record = {"id": {"record_id": "rid_1"}, "values": {}}
        attio = MagicMock(spec=AttioClient)
        # spec'd mocks carry the class's METHODS but none of its instance
        # state, so the side-effect caches extract_record_info writes to
        # have to be supplied by hand.
        attio._person_language_cache = {}
        # Use the real extract_record_info logic via the actual class
        # (MagicMock can't bind unbound methods automatically).
        result = AttioClient.extract_record_info(attio, record)
        name, company, linkedin, industry, title = result
        assert name is None
        assert company is None
        assert industry is None

    def test_cache_hit_returns_cached_tuple(self):
        """A second call returns the cached value without re-fetching."""
        attio = MagicMock()
        attio.get_person.return_value = None
        cache = RecordCache(attio)

        cache.get("rid")
        cache.get("rid")  # second call: no new fetch
        assert attio.get_person.call_count == 1

    def test_prime_accepts_none_fields(self):
        """The new tuple type allows None — prime() must accept it."""
        attio = MagicMock()
        cache = RecordCache(attio)

        cache.prime("rid", (None, None, "", None, ""))
        result = cache.get("rid")
        assert result == (None, None, "", None, "")
        # No fetch happened — cache was primed.
        attio.get_person.assert_not_called()


# ==================================================================
# RecordCache over the CRMProvider shim (AttioProvider) — proves the
# dataclass RecordInfo → 5-tuple boundary preserves every value and the
# None / "" semantics, going through the real AttioProvider.
# ==================================================================


class TestRecordCacheCRMProviderShim:
    def test_get_over_attio_provider_preserves_five_tuple(self):
        """RecordCache(AttioProvider(inner)) returns the exact 5-tuple the
        inner client's extract_record_info produces — proving the
        contract's RecordInfo dataclass is unpacked back to the cache's
        positional 5-tuple with all five values intact (incl. None for
        missing name/company/industry and ""-not-None for blank
        linkedin_url/title)."""
        from clients.attio import AttioClient
        from clients.crm.attio_provider import AttioProvider

        inner = MagicMock(spec=AttioClient)
        inner.get_person.return_value = {"id": {"record_id": "rid-1"}, "values": {}}
        # name=None, company=None, industry=None (missing); linkedin_url
        # and title are ""-when-blank per the contract.
        inner.extract_record_info.return_value = (
            "Alice", None, "https://linkedin.com/in/alice", None, "",
        )

        cache = RecordCache(AttioProvider(inner))
        result = cache.get("rid-1")

        assert result == ("Alice", None, "https://linkedin.com/in/alice", None, "")
        # The provider threaded the untouched raw payload into the inner
        # extract_record_info (the company-resolve side-effect path).
        inner.extract_record_info.assert_called_once_with(
            {"id": {"record_id": "rid-1"}, "values": {}}
        )

    def test_get_over_attio_provider_missing_record_returns_none_tuple(self):
        """A missing record (inner get_person → None) returns
        (None, None, "", None, "") through the provider shim, identical to
        the pre-migration contract."""
        from clients.attio import AttioClient
        from clients.crm.attio_provider import AttioProvider

        inner = MagicMock(spec=AttioClient)
        inner.get_person.return_value = None

        cache = RecordCache(AttioProvider(inner))
        result = cache.get("missing")

        assert result == (None, None, "", None, "")
        # No record → extract_record_info is never consulted.
        inner.extract_record_info.assert_not_called()


# ==================================================================
# Backfill script smoke test — no Attio calls, helper functions only
# ==================================================================


class TestBackfillVanitySlug:
    def test_read_existing_text_attr_handles_empty_entry(self):
        from scripts.backfill_vanity_url_slug import _read_existing_text_attr

        # Entry with no entry_values.
        assert _read_existing_text_attr({}, "vanity_url_slug") == ""
        # Entry with empty list.
        assert _read_existing_text_attr({"entry_values": {"vanity_url_slug": []}}, "vanity_url_slug") == ""
        # Entry with the canonical Attio shape.
        assert _read_existing_text_attr(
            {"entry_values": {"vanity_url_slug": [{"value": "carlos-lopez"}]}},
            "vanity_url_slug",
        ) == "carlos-lopez"

    def test_read_existing_text_attr_handles_null_value(self):
        from scripts.backfill_vanity_url_slug import _read_existing_text_attr

        assert _read_existing_text_attr(
            {"entry_values": {"vanity_url_slug": [{"value": None}]}},
            "vanity_url_slug",
        ) == ""

    def test_vanity_slug_helper_extracts_from_canonical(self):
        """The backfill computes vanity_url_slug from canonical_linkedin_url
        via clients.attio._vanity_url_slug — verify the helper itself
        produces the expected output for the canonical PR-9a form."""
        from clients.attio import _vanity_url_slug

        assert _vanity_url_slug("https://linkedin.com/in/carlos-lopez") == "carlos-lopez"
        assert _vanity_url_slug("https://linkedin.com/in/iñigo-marchal") == "iñigo-marchal"
        # Non-profile URL returns "".
        assert _vanity_url_slug("https://linkedin.com/company/acme") == ""
        # Trailing slash handled.
        assert _vanity_url_slug("https://linkedin.com/in/carlos-lopez/") == "carlos-lopez"


# ==================================================================
# MatchCandidate dataclass shape
# ==================================================================


# ==================================================================
# PR-14 fold-in: integration regressions on None-company consumers
# (silent-failure-hunter CRITICAL #1 + #2 + code-reviewer Critical #2)
# ==================================================================


class TestNoneCompanyConsumerRegressions:
    """Pre-PR-14 the literal `"Unknown"` returned by RecordCache acted
    as an accidental safety mechanism — consumers that did
    `template.replace("[Company]", company)` or `company.lower()`
    silently shipped ugly output instead of crashing.

    PR-14 removed the safety. These tests assert that the fold-in
    guards (`company or ""`) prevent the TypeError/AttributeError
    crashes that 2 QA agents (silent-failure-hunter + code-reviewer)
    flagged convergently."""

    def test_personalize_does_not_crash_on_empty_company(self):
        """`models.campaign.personalize` calls `template.replace`
        which requires str args. PR-14 fold-in coerces company to ""
        at the daily_check call sites — verify personalize itself
        handles "" cleanly (no TypeError).

        Note: passing "" still substitutes [Company] with empty string,
        producing degraded output ("at ?" instead of "at Acme?"). A
        follow-up PR should add an explicit `missing_company` skip +
        escalate at the daily_check call sites — the current fold-in
        prevents the crash but doesn't yet match the §3.1 ideal of
        "skip + escalate instead of ship-degraded". Documented in
        the PR-14 commit body as deferred."""
        from models.campaign import personalize
        from models.enums import Language

        template = "Hi [Name], thoughts on production at [Company]?"
        # The call MUST NOT raise TypeError.
        result = personalize(
            template, "Alice", "",
            industry="manufacturing", language=Language.EN,
        )
        # Name was substituted. Company was substituted with "" (the
        # known degraded-output trade-off).
        assert "Alice" in result
        assert "[Company]" not in result  # was replaced with ""

    def test_build_linkedin_collision_set_handles_none_company(
        self, monkeypatch,
    ):
        """`_build_linkedin_collision_set` calls `company.lower()`
        when no email domain is available. With company=None from
        the new RecordCache contract, the pre-fold-in code raised
        AttributeError. The fold-in `(company or "").lower()` makes
        the collision entry use an empty-string domain, which still
        builds a (first, last, "") tuple — operationally a no-op
        against real prospects (their entries carry real domains),
        which is the correct fail-safe."""
        from unittest.mock import MagicMock

        from workflows.email_campaign import _build_linkedin_collision_set

        attio = MagicMock()
        attio.query_list_entries.return_value = [
            {"id": {"entry_id": "e1", "record_id": "rec_p"}, "entry_values": {}},
        ]
        # Stub parse_entry to return an active LinkedIn stage.
        from clients.attio import AttioClient
        monkeypatch.setattr(
            AttioClient, "parse_entry", staticmethod(
                lambda _e: {"stage": "Connection Sent", "record_id": "rec_p", "entry_id": "e1"}
            ),
        )
        # Attio.get_person returns the person record.
        attio.get_person.return_value = {"id": {"record_id": "rec_p"}, "values": {}}
        # extract_record_info returns name=Alice but company=None — the
        # exact failure mode silent-failure-hunter flagged.
        attio.extract_record_info.return_value = (
            "Alice", None, "https://linkedin.com/in/alice", None, "",
        )

        # Pre-fold-in: AttributeError. Post-fold-in: clean collision set.
        result = _build_linkedin_collision_set(attio)
        # No crash → the fold-in works. The set may contain a
        # (first, last, "") entry but that's the right fail-safe.
        assert isinstance(result, set)


class TestMatchCandidateShape:
    def test_is_frozen(self):
        """MatchCandidate is immutable to prevent caller mutation."""
        from dataclasses import FrozenInstanceError

        c = MatchCandidate(entry_id="e1", record_id="r1", score=1.0, reason="vanity_slug_exact")
        with pytest.raises(FrozenInstanceError):
            c.score = 0.5  # type: ignore[misc]

    def test_fields(self):
        c = MatchCandidate(entry_id="e1", record_id="r1", score=0.85, reason="name_diacritic_exact")
        assert c.entry_id == "e1"
        assert c.record_id == "r1"
        assert c.score == 0.85
        assert c.reason == "name_diacritic_exact"
