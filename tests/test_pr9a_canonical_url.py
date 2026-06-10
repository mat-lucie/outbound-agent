"""PR-9a: Tests for canonical_linkedin_url and vanity_url_slug functions.

Covers:
  - URL normalization edge cases (trailing slash, www., mixed case, URL-encoding)
  - Vanity slug extraction from canonical form
  - Rejection of non-profile URLs (company pages, malformed)
  - Idempotency of _canonical_linkedin_url (applying twice = same result)
"""

from __future__ import annotations

from clients.attio import _canonical_linkedin_url, _vanity_url_slug

# ============================================================
# _canonical_linkedin_url
# ============================================================


class TestCanonicalLinkedinUrl:
    def test_trailing_slash_stripped(self) -> None:
        url = "https://linkedin.com/in/mateo-lt-12345/"
        assert _canonical_linkedin_url(url) == "https://linkedin.com/in/mateo-lt-12345"

    def test_www_stripped(self) -> None:
        url = "https://www.linkedin.com/in/mateo-lt-12345"
        assert _canonical_linkedin_url(url) == "https://linkedin.com/in/mateo-lt-12345"

    def test_www_and_trailing_slash_stripped(self) -> None:
        url = "https://www.linkedin.com/in/mateo-lt-12345/"
        assert _canonical_linkedin_url(url) == "https://linkedin.com/in/mateo-lt-12345"

    def test_mixed_case_lowercased(self) -> None:
        url = "https://LinkedIn.com/in/Mateo-LT-12345"
        assert _canonical_linkedin_url(url) == "https://linkedin.com/in/mateo-lt-12345"

    def test_url_encoded_decoded(self) -> None:
        url = "https://linkedin.com/in/i%C3%B1igo-marchal"
        result = _canonical_linkedin_url(url)
        assert result == "https://linkedin.com/in/iñigo-marchal"

    def test_http_scheme_preserved(self) -> None:
        url = "http://linkedin.com/in/test-slug"
        assert _canonical_linkedin_url(url) == "http://linkedin.com/in/test-slug"

    def test_no_scheme(self) -> None:
        url = "linkedin.com/in/test-slug"
        result = _canonical_linkedin_url(url)
        assert "test-slug" in result
        assert result == result.lower()

    def test_empty_string_returns_empty(self) -> None:
        assert _canonical_linkedin_url("") == ""

    def test_none_like_empty_string_edge(self) -> None:
        # The function accepts str, not None — but whitespace-only should be empty.
        assert _canonical_linkedin_url("   ") == ""

    def test_idempotent_single_apply(self) -> None:
        """Applying canonicalization once must equal applying it twice."""
        url = "https://www.linkedin.com/in/Mateo-LT-12345/"
        once = _canonical_linkedin_url(url)
        twice = _canonical_linkedin_url(once)
        assert once == twice

    def test_idempotent_url_encoded(self) -> None:
        url = "https://linkedin.com/in/i%C3%B1igo-marchal/"
        once = _canonical_linkedin_url(url)
        twice = _canonical_linkedin_url(once)
        assert once == twice

    def test_already_canonical_unchanged(self) -> None:
        canonical = "https://linkedin.com/in/mateo-lt-12345"
        assert _canonical_linkedin_url(canonical) == canonical

    def test_company_url_returned_lowercased(self) -> None:
        """Company URLs are normalized but not rejected — caller decides scope."""
        url = "https://www.linkedin.com/company/SomeCompany/"
        result = _canonical_linkedin_url(url)
        assert result == "https://linkedin.com/company/somecompany"

    def test_multiple_trailing_slashes(self) -> None:
        url = "https://linkedin.com/in/test-slug///"
        result = _canonical_linkedin_url(url)
        assert not result.endswith("/")

    def test_url_with_query_params_preserved(self) -> None:
        """Query params are uncommon but should not be stripped by the canonicalizer."""
        url = "https://www.linkedin.com/in/test-slug?miniProfileUrn=urn%3Ali"
        result = _canonical_linkedin_url(url)
        assert result.startswith("https://linkedin.com/in/test-slug")


# ============================================================
# _vanity_url_slug
# ============================================================


class TestVanityUrlSlug:
    def test_standard_profile_url(self) -> None:
        url = "https://linkedin.com/in/mateo-lt-12345"
        assert _vanity_url_slug(url) == "mateo-lt-12345"

    def test_www_url_stripped_correctly(self) -> None:
        url = "https://www.linkedin.com/in/mateo-lt-12345"
        assert _vanity_url_slug(url) == "mateo-lt-12345"

    def test_trailing_slash_ignored(self) -> None:
        url = "https://linkedin.com/in/mateo-lt-12345/"
        assert _vanity_url_slug(url) == "mateo-lt-12345"

    def test_mixed_case_slug_lowercased(self) -> None:
        url = "https://www.linkedin.com/in/Mateo-LT-12345/"
        assert _vanity_url_slug(url) == "mateo-lt-12345"

    def test_url_encoded_slug(self) -> None:
        url = "https://linkedin.com/in/i%C3%B1igo-marchal"
        assert _vanity_url_slug(url) == "iñigo-marchal"

    def test_no_scheme_url(self) -> None:
        url = "linkedin.com/in/mateo-lt-12345"
        assert _vanity_url_slug(url) == "mateo-lt-12345"

    def test_empty_string_returns_empty(self) -> None:
        assert _vanity_url_slug("") == ""

    def test_company_url_returns_empty(self) -> None:
        url = "https://linkedin.com/company/somecompany"
        assert _vanity_url_slug(url) == ""

    def test_non_linkedin_url_returns_empty(self) -> None:
        url = "https://example.com/in/mateo-lt-12345"
        assert _vanity_url_slug(url) == ""

    def test_slug_with_numbers_only(self) -> None:
        url = "https://linkedin.com/in/12345678"
        assert _vanity_url_slug(url) == "12345678"

    def test_slug_with_unicode(self) -> None:
        url = "https://linkedin.com/in/iñigo-marchal"
        assert _vanity_url_slug(url) == "iñigo-marchal"

    def test_http_scheme_works(self) -> None:
        url = "http://linkedin.com/in/test-slug"
        assert _vanity_url_slug(url) == "test-slug"

    def test_already_canonical_no_scheme(self) -> None:
        url = "linkedin.com/in/test-slug-123"
        assert _vanity_url_slug(url) == "test-slug-123"

    def test_url_with_query_params_slug_strips_params(self) -> None:
        """Query params are stripped from the slug by _vanity_url_slug.

        A URL like /in/test-slug?miniProfileUrn=... should return exactly
        'test-slug', with the query string removed. This prevents dirty slug
        writes when PB-sourced URLs carry tracking params.
        """
        url = "https://linkedin.com/in/test-slug?miniProfileUrn=abc"
        slug = _vanity_url_slug(url)
        assert slug == "test-slug"
