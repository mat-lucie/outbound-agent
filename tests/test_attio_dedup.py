"""Tests for the Attio dedup scanner logic."""

from scripts.attio_dedup import (
    bucket_companies,
    bucket_people,
    comparable,
    compute_merge_fields,
    detect_conflicts,
    detect_list_entry_conflicts,
    extract_writable_value,
    normalize_domain,
    normalize_linkedin_url,
    pick_winner,
    plan_list_entry_actions,
    record_id,
)


class TestNormalizeLinkedinUrl:
    def test_strips_scheme_and_www(self):
        assert normalize_linkedin_url("https://www.linkedin.com/in/gerardo-chavez-2133b313a") == "linkedin.com/in/gerardo-chavez-2133b313a"

    def test_strips_trailing_slash(self):
        assert normalize_linkedin_url("linkedin.com/in/foo/") == "linkedin.com/in/foo"

    def test_decodes_percent_encoding(self):
        assert normalize_linkedin_url("https://www.linkedin.com/in/gerardo-ch%C3%A1vez-2133b313a") == normalize_linkedin_url("https://www.linkedin.com/in/gerardo-chávez-2133b313a")

    def test_strips_query_string(self):
        assert normalize_linkedin_url("https://linkedin.com/in/foo?trk=abc") == "linkedin.com/in/foo"

    def test_strips_fragment(self):
        assert normalize_linkedin_url("https://linkedin.com/in/foo#section") == "linkedin.com/in/foo"

    def test_lowercases(self):
        assert normalize_linkedin_url("https://LinkedIn.COM/in/FooBar") == "linkedin.com/in/foobar"

    def test_empty_is_empty(self):
        assert normalize_linkedin_url("") == ""
        assert normalize_linkedin_url(None or "") == ""


class TestNormalizeDomain:
    def test_strips_scheme(self):
        assert normalize_domain("https://acme-foods.com") == "acme-foods.com"

    def test_strips_www(self):
        assert normalize_domain("www.acme-foods.com") == "acme-foods.com"

    def test_strips_path(self):
        assert normalize_domain("acme-foods.com/about") == "acme-foods.com"


class TestComparable:
    def test_empty_list_returns_none(self):
        assert comparable([]) is None

    def test_none_returns_none(self):
        assert comparable(None) is None

    def test_text_value_extracted(self):
        assert comparable([{"value": "Gerente de Planta"}]) == "gerente de planta"

    def test_personal_name_full_name(self):
        assert comparable([{"full_name": "Gerardo Chávez"}]) == "gerardo chávez"

    def test_personal_name_first_last(self):
        assert comparable([{"first_name": "Gerardo", "last_name": "Chávez"}]) == "gerardo chávez"

    def test_record_reference(self):
        assert comparable([{"target_record_id": "abc-123"}]) == "abc-123"

    def test_select_option(self):
        assert comparable([{"option": {"title": "operations_leaders"}}]) == "operations_leaders"

    def test_status(self):
        assert comparable([{"status": {"title": "DM2 Sent"}}]) == "DM2 Sent"

    def test_email(self):
        assert comparable([{"email_address": "MAT@example.com"}]) == "mat@example.com"

    def test_domain(self):
        assert comparable([{"domain": "WWW.Foo.com"}]) == "foo.com"


def _person(rid: str, created: str, linkedin: str, job_title: str | None = None, company: str | None = None) -> dict:
    values: dict = {"linkedin": [{"value": linkedin}]}
    if job_title:
        values["job_title"] = [{"value": job_title}]
    if company:
        values["company"] = [{"target_object": "companies", "target_record_id": company}]
    return {"id": {"record_id": rid}, "created_at": created, "values": values}


class TestPickWinner:
    def test_oldest_wins(self):
        a = _person("a", "2026-04-13T10:00:00Z", "url")
        b = _person("b", "2026-04-07T05:22:28Z", "url")
        c = _person("c", "2026-04-10T07:21:13Z", "url")
        assert record_id(pick_winner([a, b, c])) == "b"

    def test_tiebreak_by_record_id(self):
        a = _person("b", "2026-04-07", "url")
        b = _person("a", "2026-04-07", "url")
        assert record_id(pick_winner([a, b])) == "a"


class TestComputeMergeFields:
    def test_loser_fills_empty_winner_field(self):
        winner = _person("w", "2026-04-07", "url", job_title="Jefe")  # no company
        loser = _person("l", "2026-04-08", "url", job_title="Jefe", company="co-1")
        merge = compute_merge_fields(winner, [loser], ("name", "linkedin", "job_title", "company"))
        assert "company" in merge
        assert merge["company"] == [{"target_object": "companies", "target_record_id": "co-1"}]

    def test_no_merge_when_winner_already_populated(self):
        winner = _person("w", "2026-04-07", "url", job_title="Jefe", company="co-1")
        loser = _person("l", "2026-04-08", "url", job_title="Jefe", company="co-2")
        merge = compute_merge_fields(winner, [loser], ("company",))
        assert merge == {}

    def test_earliest_loser_with_field_wins(self):
        winner = _person("w", "2026-04-07", "url")
        loser_a = _person("la", "2026-04-13", "url", company="co-A")
        loser_b = _person("lb", "2026-04-08", "url", company="co-B")  # earlier
        merge = compute_merge_fields(winner, [loser_a, loser_b], ("company",))
        assert merge["company"] == [{"target_object": "companies", "target_record_id": "co-B"}]


class TestDetectConflicts:
    def test_identical_values_are_safe(self):
        a = _person("a", "2026-04-07", "url", job_title="Jefe de Innovación")
        b = _person("b", "2026-04-08", "url", job_title="Jefe de Innovación")
        assert detect_conflicts([a, b], ("job_title",)) == []

    def test_different_non_empty_values_conflict(self):
        a = _person("a", "2026-04-07", "url", job_title="Jefe")
        b = _person("b", "2026-04-08", "url", job_title="Director")
        reasons = detect_conflicts([a, b], ("job_title",))
        assert len(reasons) == 1
        assert "job_title" in reasons[0]

    def test_empty_plus_populated_is_safe(self):
        a = _person("a", "2026-04-07", "url")  # no job title
        b = _person("b", "2026-04-08", "url", job_title="Jefe")
        assert detect_conflicts([a, b], ("job_title",)) == []


def _entry(entry_id: str, parent: str, stage: str, created: str = "2026-04-10T00:00:00Z") -> dict:
    return {
        "id": {"entry_id": entry_id},
        "parent_record_id": parent,
        "created_at": created,
        "entry_values": {"stage": [{"status": {"title": stage}}]},
    }


class TestPlanListEntryActions:
    def test_keeps_max_stage_on_winner(self):
        entries = {
            "winner": [_entry("e1", "winner", "DM2 Sent")],
            "loser": [_entry("e2", "loser", "Prospect")],
        }
        actions = plan_list_entry_actions("winner", {"winner", "loser"}, entries)
        types = [a["type"] for a in actions]
        assert types == ["keep", "delete"]
        assert actions[0]["entry_id"] == "e1"
        assert actions[1]["entry_id"] == "e2"

    def test_reparents_when_max_is_on_loser(self):
        entries = {
            "winner": [_entry("e1", "winner", "Prospect")],
            "loser": [_entry("e2", "loser", "DM2 Sent")],
        }
        actions = plan_list_entry_actions("winner", {"winner", "loser"}, entries)
        types = [a["type"] for a in actions]
        assert types == ["reparent", "delete"]
        assert actions[0]["entry_id"] == "e2"
        assert actions[0]["stage"] == "DM2 Sent"

    def test_no_entries_no_actions(self):
        actions = plan_list_entry_actions("winner", {"winner", "loser"}, {})
        assert actions == []

    def test_not_interested_outranks_qualified(self):
        entries = {
            "winner": [_entry("e1", "winner", "Qualified")],
            "loser": [_entry("e2", "loser", "Not Interested")],
        }
        actions = plan_list_entry_actions("winner", {"winner", "loser"}, entries)
        assert actions[0]["type"] == "reparent"
        assert actions[0]["stage"] == "Not Interested"


class TestBucketPeople:
    def test_gerardo_case_auto_safe_with_9_records(self):
        """All 9 records share LinkedIn URL, 5 have company, 4 don't, no conflicts."""
        url = "https://www.linkedin.com/in/gerardo-chávez-2133b313a"
        records = []
        dates = [
            "2026-04-07T05:22:28Z", "2026-04-07T05:28:36Z", "2026-04-07T05:34:20Z",
            "2026-04-07T16:54:44Z", "2026-04-07T16:57:29Z", "2026-04-10T07:21:13Z",
            "2026-04-13T13:53:09Z", "2026-04-13T14:10:03Z", "2026-04-13T15:13:29Z",
        ]
        for i, dt in enumerate(dates):
            has_company = i >= 3  # the 6 later ones have company; the first 3 don't
            records.append(_person(
                f"rec-{i}",
                dt,
                url,
                job_title="Jefe de Innovación y Empaque",
                company="co-1" if has_company else None,
            ))

        safe, conflicts = bucket_people(records, entries_by_person={})
        assert len(safe) == 1
        assert len(conflicts) == 0
        grp = safe[0]
        assert grp["winner_id"] == "rec-0"  # oldest created_at
        assert len(grp["loser_ids"]) == 8
        # winner (rec-0) has no company, but losers do → company should be merged forward
        assert "company" in grp["fields_to_merge"]

    def test_conflict_when_job_titles_differ(self):
        url = "https://linkedin.com/in/someone"
        a = _person("a", "2026-04-07", url, job_title="Gerente de Planta")
        b = _person("b", "2026-04-08", url, job_title="Director de Operaciones")
        safe, conflicts = bucket_people([a, b], entries_by_person={})
        assert len(safe) == 0
        assert len(conflicts) == 1
        assert any("job_title" in r for r in conflicts[0]["conflict_reasons"])

    def test_single_record_bucket_ignored(self):
        a = _person("a", "2026-04-07", "https://linkedin.com/in/foo")
        safe, conflicts = bucket_people([a], entries_by_person={})
        assert safe == []
        assert conflicts == []

    def test_record_without_linkedin_ignored(self):
        a = {"id": {"record_id": "a"}, "created_at": "2026-04-07", "values": {}}
        b = {"id": {"record_id": "b"}, "created_at": "2026-04-08", "values": {}}
        safe, conflicts = bucket_people([a, b], entries_by_person={})
        assert safe == []
        assert conflicts == []


def _company(rid: str, created: str, domain: str, name: str | None = None) -> dict:
    values: dict = {"domains": [{"domain": domain}]}
    if name:
        values["name"] = [{"value": name}]
    return {"id": {"record_id": rid}, "created_at": created, "values": values}


class TestExtractWritableValue:
    def test_select_preserves_case(self):
        assert extract_writable_value([{"option": {"title": "operations_leaders"}}]) == "operations_leaders"

    def test_record_reference_shape(self):
        result = extract_writable_value([{"target_record_id": "abc-123", "target_object": "companies"}])
        assert result == {"target_object": "companies", "target_record_id": "abc-123"}

    def test_text_value(self):
        assert extract_writable_value([{"value": "Gerente de Planta"}]) == "Gerente de Planta"

    def test_number_value(self):
        assert extract_writable_value([{"value": 85}]) == 85

    def test_date_value(self):
        assert extract_writable_value([{"value": "2026-04-10"}]) == "2026-04-10"

    def test_empty_returns_none(self):
        assert extract_writable_value([]) is None
        assert extract_writable_value(None) is None


class TestDetectListEntryConflicts:
    def test_divergent_personas_flagged(self):
        entries = {
            "r1": [{
                "id": {"entry_id": "e1"},
                "parent_record_id": "r1",
                "entry_values": {"persona": [{"option": {"title": "operations_leaders"}}]},
            }],
            "r2": [{
                "id": {"entry_id": "e2"},
                "parent_record_id": "r2",
                "entry_values": {"persona": [{"option": {"title": "executive_sponsors"}}]},
            }],
        }
        reasons = detect_list_entry_conflicts({"r1", "r2"}, entries)
        assert len(reasons) == 1
        assert "entry.persona" in reasons[0]

    def test_same_personas_no_conflict(self):
        entries = {
            "r1": [{
                "id": {"entry_id": "e1"},
                "parent_record_id": "r1",
                "entry_values": {"persona": [{"option": {"title": "operations_leaders"}}]},
            }],
            "r2": [{
                "id": {"entry_id": "e2"},
                "parent_record_id": "r2",
                "entry_values": {"persona": [{"option": {"title": "operations_leaders"}}]},
            }],
        }
        assert detect_list_entry_conflicts({"r1", "r2"}, entries) == []

    def test_divergent_response_classification_flagged(self):
        entries = {
            "r1": [{
                "id": {"entry_id": "e1"},
                "parent_record_id": "r1",
                "entry_values": {"response_classification": [{"status": {"title": "Interested"}}]},
            }],
            "r2": [{
                "id": {"entry_id": "e2"},
                "parent_record_id": "r2",
                "entry_values": {"response_classification": [{"status": {"title": "Not Interested"}}]},
            }],
        }
        reasons = detect_list_entry_conflicts({"r1", "r2"}, entries)
        assert any("response_classification" in r for r in reasons)


class TestBucketCompanies:
    def test_acme_case(self):
        """Two Acme Foods records with the same canonical domain get bucketed."""
        a = _company("sig-a", "2025-12-01", "acme-foods.com", name="Acme Foods")
        b = _company("sig-b", "2026-03-15", "www.acme-foods.com", name="Acme Grains")
        safe, conflicts = bucket_companies([a, b])
        # different names → conflict
        assert len(safe) == 0
        assert len(conflicts) == 1
        assert conflicts[0]["winner_id"] == "sig-a"

    def test_same_domain_same_name_auto_safe(self):
        a = _company("a", "2025-12-01", "foo.com", name="Foo Inc")
        b = _company("b", "2026-03-15", "foo.com", name="Foo Inc")
        safe, conflicts = bucket_companies([a, b])
        assert len(safe) == 1
        assert len(conflicts) == 0
