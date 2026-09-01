"""Residual exact-URL layers rekeyed on `linkedin_identity_key`.

Follow-up to the slug-variant cadence-leak fix (see
tests/test_linkedin_variant_dedup.py). That fix wired the profile-id
identity key into the weekly URL gate and the daily send-path dedup, but
three layers still compared exact URL strings and kept the variant gap:

  1. `workflows.learn._dedup_by_canonical_url` — a variant-duplicate pair
     counted as two people in experiment cohort math, inflating
     denominators.
  2. `scripts.attio_dedup.bucket_people` — the PR-9.5 physical dedup sweep
     could not group variant-slug duplicate person records for merge (the
     incident pair reported 0 groups).
  3. `clients.attio.search_person_by_linkedin` / `upsert_person` — ingest
     paths that don't run the weekly URL gate (intake, backfill scripts,
     reconciliation) could still create a duplicate person record under a
     variant slug.

All three now key on `linkedin_identity_key`; layer 3 adds a `$contains`
secondary lookup on the profile-id suffix, verified per candidate against
the identity key because `$contains` over-matches on id prefixes.
"""

from unittest.mock import patch

from clients.attio import (
    AttioClient,
    person_record_linkedin_url,
)
from scripts.attio_dedup import bucket_people, detect_conflicts
from workflows.learn import _dedup_by_canonical_url

# The incident pair (synthetic stand-ins): same profile, two slug variants
# sharing the numeric member-id suffix.
_LONG = "https://linkedin.com/in/dana-quiroga-ramos-mba-70481235"
_SHORT = "https://www.linkedin.com/in/dana-q-70481235"


# ---------------------------------------------------------------------------
# Layer 1 — workflows.learn._dedup_by_canonical_url (cohort measurement)
# ---------------------------------------------------------------------------


def _lo_entry(url: str | None, stage: str = "Prospect", rid: str = "") -> dict:
    return {
        "canonical_linkedin_url": url,
        "stage": stage,
        "record_id": rid or f"rec-{url or 'nourl'}-{stage}",
    }


class TestLearnDedupIdentityKey:
    def test_incident_pair_counts_as_one_person(self):
        """Slug variants of the same profile collapse to a single entry so
        cohort denominators aren't inflated by variant duplicates."""
        entries = [
            _lo_entry(_LONG, "DM3 Sent"),
            _lo_entry(_SHORT, "Prospect"),
        ]
        result = _dedup_by_canonical_url(entries)
        assert len(result) == 1

    def test_keeps_most_advanced_variant_entry(self):
        lower = _lo_entry(_SHORT, "Prospect")
        higher = _lo_entry(_LONG, "DM3 Sent")
        result = _dedup_by_canonical_url([lower, higher])
        assert len(result) == 1
        assert result[0]["stage"] == "DM3 Sent"

    def test_distinct_profile_ids_not_deduped(self):
        entries = [
            _lo_entry("https://linkedin.com/in/dana-q-70481235", "DM1 Sent"),
            _lo_entry("https://linkedin.com/in/dana-q-18273645", "DM2 Sent"),
        ]
        result = _dedup_by_canonical_url(entries)
        assert len(result) == 2

    def test_www_variant_without_profile_id_now_dedupes(self):
        """Custom-vanity slugs (no numeric suffix) fall back to the canonical
        URL, which strips www — the old strip/lower normalization kept
        www vs non-www as two people."""
        entries = [
            _lo_entry("https://www.linkedin.com/in/alice-slug", "Prospect"),
            _lo_entry("https://linkedin.com/in/alice-slug", "DM2 Sent"),
        ]
        result = _dedup_by_canonical_url(entries)
        assert len(result) == 1
        assert result[0]["stage"] == "DM2 Sent"

    def test_no_url_entries_still_preserved(self):
        entries = [_lo_entry(None), _lo_entry("")]
        assert len(_dedup_by_canonical_url(entries)) == 2


# ---------------------------------------------------------------------------
# Layer 2 — scripts.attio_dedup.bucket_people (PR-9.5 physical dedup sweep)
# ---------------------------------------------------------------------------


def _person(
    rid: str,
    created: str,
    linkedin: str,
    name: str | None = None,
    job_title: str | None = None,
) -> dict:
    values: dict = {"linkedin": [{"value": linkedin}]}
    if name:
        values["name"] = [{"full_name": name}]
    if job_title:
        values["job_title"] = [{"value": job_title}]
    return {"id": {"record_id": rid}, "created_at": created, "values": values}


class TestSweepBucketsByIdentityKey:
    def test_incident_pair_reports_one_group(self):
        """Before the rekey, exact-URL bucketing put the incident pair in two
        singleton buckets → 0 groups → the sweep could never merge them."""
        canonical = _person("rec-old", "2026-07-01T00:00:00Z", _LONG, name="Dana Q")
        duplicate = _person("rec-new", "2026-08-14T00:00:00Z", _SHORT, name="Dana Q")
        safe, conflicts = bucket_people([canonical, duplicate], entries_by_person={})
        assert len(safe) + len(conflicts) == 1

    def test_variant_slug_alone_is_not_a_conflict(self):
        """Within an identity-key bucket the records are same-profile by
        construction — URL variance on the bucketing field itself must not
        force human triage."""
        canonical = _person("rec-old", "2026-07-01T00:00:00Z", _LONG, name="Dana Q")
        duplicate = _person("rec-new", "2026-08-14T00:00:00Z", _SHORT, name="Dana Q")
        safe, conflicts = bucket_people([canonical, duplicate], entries_by_person={})
        assert len(safe) == 1
        assert conflicts == []

    def test_group_winner_is_oldest_and_report_lists_both_urls(self):
        canonical = _person("rec-old", "2026-07-01T00:00:00Z", _LONG, name="Dana Q")
        duplicate = _person("rec-new", "2026-08-14T00:00:00Z", _SHORT, name="Dana Q")
        safe, _ = bucket_people([canonical, duplicate], entries_by_person={})
        grp = safe[0]
        assert grp["winner_id"] == "rec-old"
        assert grp["loser_ids"] == ["rec-new"]
        assert grp["linkedin_url_normalized"] == "li-id:70481235"
        # Raw URLs surface for operator forensics.
        assert len(grp["linkedin_urls"]) == 2

    def test_real_field_conflict_still_flags(self):
        a = _person("a", "2026-07-01", _LONG, job_title="Plant Manager")
        b = _person("b", "2026-08-14", _SHORT, job_title="Director of Operations")
        safe, conflicts = bucket_people([a, b], entries_by_person={})
        assert safe == []
        assert len(conflicts) == 1
        assert any("job_title" in r for r in conflicts[0]["conflict_reasons"])

    def test_distinct_profile_ids_report_zero_groups(self):
        a = _person("a", "2026-07-01", "https://linkedin.com/in/dana-q-70481235")
        b = _person("b", "2026-08-14", "https://linkedin.com/in/dana-q-18273645")
        safe, conflicts = bucket_people([a, b], entries_by_person={})
        assert safe == []
        assert conflicts == []

    def test_detect_conflicts_normalizer_scopes_to_named_field(self):
        """The normalizer applies only to its field — other fields keep raw
        comparison semantics."""
        a = _person("a", "2026-07-01", _LONG, job_title="Same Title")
        b = _person("b", "2026-08-14", _SHORT, job_title="Same Title")
        from clients.attio import linkedin_identity_key
        reasons = detect_conflicts(
            [a, b], ("linkedin", "job_title"),
            normalizers={"linkedin": linkedin_identity_key},
        )
        assert reasons == []
        reasons_raw = detect_conflicts([a, b], ("linkedin",))
        assert len(reasons_raw) == 1


# ---------------------------------------------------------------------------
# Layer 3 — clients.attio search/upsert identity-key fallback (ingest paths)
# ---------------------------------------------------------------------------


def _attio() -> AttioClient:
    return AttioClient(api_key="test-key")


def _stored_person(url: str, rid: str = "rec-existing") -> dict:
    return {"id": {"record_id": rid}, "values": {"linkedin": [{"value": url}]}}


class TestSearchPersonIdentityFallback:
    def test_variant_slug_found_via_profile_id_contains(self):
        """Exact-variant probes miss the stored long slug; the `$contains`
        fallback on the shared profile-id finds it."""
        attio = _attio()
        stored = _stored_person(_LONG)
        filters: list = []

        def fake_request(method, path, json=None, **_):
            f = (json or {}).get("filter", {})
            filters.append(f.get("linkedin"))
            if isinstance(f.get("linkedin"), dict):
                assert f["linkedin"] == {"$contains": "-70481235"}
                return {"data": [stored]}
            return {"data": []}

        with patch.object(attio, "_request", side_effect=fake_request):
            result = attio.search_person_by_linkedin(_SHORT)
        assert result is stored
        # Exact variants were tried before falling back to $contains.
        assert any(isinstance(f, str) for f in filters)
        assert isinstance(filters[-1], dict)

    def test_contains_overmatch_on_longer_id_rejected(self):
        """`-70481235` is a substring of `-704812350`; a candidate with the
        longer id must be rejected by identity-key verification."""
        attio = _attio()
        other = _stored_person("https://linkedin.com/in/someone-else-704812350")

        def fake_request(method, path, json=None, **_):
            f = (json or {}).get("filter", {})
            if isinstance(f.get("linkedin"), dict):
                return {"data": [other]}
            return {"data": []}

        with patch.object(attio, "_request", side_effect=fake_request):
            assert attio.search_person_by_linkedin(_SHORT) is None

    def test_no_profile_id_skips_contains_lookup(self):
        """Custom vanity slugs have no id to key on — no `$contains` query
        is issued and the miss stays a miss."""
        attio = _attio()
        filters: list = []

        def fake_request(method, path, json=None, **_):
            f = (json or {}).get("filter", {})
            filters.append(f.get("linkedin"))
            return {"data": []}

        with patch.object(attio, "_request", side_effect=fake_request):
            assert attio.search_person_by_linkedin(
                "https://linkedin.com/in/dana-quiroga-ramos"
            ) is None
        assert all(isinstance(f, str) for f in filters if f is not None)

    def test_renamed_slug_workspace_uses_mapped_field(self):
        """Fork seam: an operator-renamed LinkedIn attribute must be used for
        BOTH the `$contains` filter and the candidate verification read."""
        attio = AttioClient(
            api_key="test-key",
            field_mapping={"linkedin_url": "linkedin_profile"},
        )
        stored = {
            "id": {"record_id": "rec-existing"},
            "values": {"linkedin_profile": [{"value": _LONG}]},
        }

        def fake_request(method, path, json=None, **_):
            f = (json or {}).get("filter", {})
            target = f.get("linkedin_profile")
            if isinstance(target, dict):
                assert target == {"$contains": "-70481235"}
                return {"data": [stored]}
            return {"data": []}

        with patch.object(attio, "_request", side_effect=fake_request):
            assert attio.search_person_by_linkedin(_SHORT) is stored

    def test_person_record_linkedin_url_shapes(self):
        assert person_record_linkedin_url(_stored_person(_LONG)) == _LONG
        assert person_record_linkedin_url({"values": {}}) == ""
        assert person_record_linkedin_url({}) == ""
        assert person_record_linkedin_url({"values": {"linkedin": [_LONG]}}) == _LONG


class TestUpsertPersonIdentityFallback:
    def test_variant_slug_updates_existing_without_clobbering_url(self):
        """Ingest under a NEW slug variant must PATCH the existing record —
        and must NOT rewrite its stored URL, which is the exact-string join
        key for list entries and historical sends."""
        attio = _attio()
        stored = _stored_person(_LONG)
        patches: list[dict] = []

        def fake_request(method, path, json=None, **_):
            if method == "POST" and path == "/objects/people/records/query":
                f = (json or {}).get("filter", {})
                if isinstance(f.get("linkedin"), dict):
                    return {"data": [stored]}
                return {"data": []}
            if method == "PATCH":
                patches.append(json["data"]["values"])
                return {"data": stored}
            raise AssertionError(f"Unexpected {method} {path}")

        with patch.object(attio, "_request", side_effect=fake_request):
            result = attio.upsert_person(
                matching_attribute="linkedin",
                attributes={"linkedin": _SHORT, "job_title": [{"value": "Jefe"}]},
            )
        assert result is stored
        assert len(patches) == 1
        assert "linkedin" not in patches[0]
        assert patches[0]["job_title"] == [{"value": "Jefe"}]

    def test_same_profile_encoding_variant_still_canonicalizes_url(self):
        """Pre-existing behavior guard: when the match is a www/encoding
        variant (same canonical form), the PATCH still writes the canonical
        URL — that cleanup is intended and must survive the rekey."""
        attio = _attio()
        stored = _stored_person("https://www.linkedin.com/in/bar")
        patches: list[dict] = []

        def fake_request(method, path, json=None, **_):
            if method == "POST" and path == "/objects/people/records/query":
                f = (json or {}).get("filter", {})
                target = f.get("linkedin", "")
                if target in (
                    "https://www.linkedin.com/in/bar",
                    "https://linkedin.com/in/bar",
                ):
                    return {"data": [stored]}
                return {"data": []}
            if method == "PATCH":
                patches.append(json["data"]["values"])
                return {"data": stored}
            raise AssertionError(f"Unexpected {method} {path}")

        with patch.object(attio, "_request", side_effect=fake_request):
            attio.upsert_person(
                matching_attribute="linkedin",
                attributes={"linkedin": "https://www.linkedin.com/in/bar/"},
            )
        assert patches == [{"linkedin": "https://linkedin.com/in/bar"}]

    def test_genuinely_new_person_still_created(self):
        attio = _attio()
        creates: list[dict] = []

        def fake_request(method, path, json=None, **_):
            if method == "POST" and path == "/objects/people/records/query":
                return {"data": []}
            if method == "POST" and path == "/objects/people/records":
                creates.append(json["data"]["values"])
                return {"data": {"id": {"record_id": "rec-created"}}}
            raise AssertionError(f"Unexpected {method} {path}")

        with patch.object(attio, "_request", side_effect=fake_request):
            attio.upsert_person(
                matching_attribute="linkedin",
                attributes={"linkedin": _SHORT},
            )
        assert len(creates) == 1
        # Stored canonicalized, per existing write-time normalization.
        assert creates[0]["linkedin"] == _SHORT.replace(
            "https://www.", "https://"
        ).rstrip("/")
