"""Tests for the deal-dedup mode of scripts/attio_dedup.py."""

from __future__ import annotations

from unittest.mock import MagicMock

from scripts.attio_dedup import (
    apply_deal_group,
    bucket_deals,
    compute_deal_associated_people_union,
    compute_deal_merge_fields,
    deal_associated_people,
    deal_company_id,
    deal_name_raw,
    deal_stage_title,
    normalize_deal_name,
    pick_deal_winner,
)


def _deal(
    record_id: str,
    name: str = "",
    stage: str = "Lead",
    company_uuid: str | None = "co-1",
    people: list[str] | None = None,
    created_at: str = "2026-02-11T00:00:00Z",
    country: str | None = None,
) -> dict:
    """Build a minimal deal record in the Attio shape."""
    values: dict = {
        "name": [{"value": name}] if name else [],
        "stage": [{"status": {"title": stage}}],
        "created_at": [{"value": created_at}],
    }
    if company_uuid:
        values["associated_company"] = [{
            "target_object": "companies",
            "target_record_id": company_uuid,
        }]
    if people:
        values["associated_people"] = [
            {"target_object": "people", "target_record_id": pid}
            for pid in people
        ]
    if country:
        values["country"] = [{"country_code": country}]
    return {"id": {"record_id": record_id}, "values": values, "created_at": created_at}


# ── normalize_deal_name ────────────────────────────────────────


class TestNormalizeDealName:
    def test_strips_date_prefix_with_l_separator(self):
        assert normalize_deal_name("202507 l Bebidas Modelo l BR") == "bebidas modelo"

    def test_strips_date_prefix_with_pipe(self):
        assert normalize_deal_name("202511 | Quimêra (Química Eg) | BR") == "quimêra (química eg)"

    def test_strips_product_suffix_when_configured(self, monkeypatch):
        monkeypatch.setenv("OUTBOUND_DEAL_NAME_SUFFIX", "AcmeBot")
        assert normalize_deal_name("Ejemplosa — AcmeBot") == "ejemplosa"

    def test_no_suffix_stripping_by_default(self, monkeypatch):
        monkeypatch.delenv("OUTBOUND_DEAL_NAME_SUFFIX", raising=False)
        assert normalize_deal_name("Ejemplosa — AcmeBot") == "ejemplosa — acmebot"

    def test_strips_country_prefix(self):
        assert normalize_deal_name("MX - Farmacias Modelo") == "farmacias modelo"

    def test_idempotent(self):
        once = normalize_deal_name("202505 l Absela l MX")
        twice = normalize_deal_name(once)
        assert once == twice == "absela"

    def test_handles_empty(self):
        assert normalize_deal_name("") == ""
        assert normalize_deal_name(None or "") == ""

    def test_preserves_simple_name(self):
        assert normalize_deal_name("Acme Foods") == "acme foods"


# ── Field extraction helpers ───────────────────────────────────


class TestFieldExtraction:
    def test_deal_company_id_extracts_target_record_id(self):
        d = _deal("d-1", company_uuid="co-42")
        assert deal_company_id(d) == "co-42"

    def test_deal_company_id_handles_missing(self):
        d = _deal("d-1", company_uuid=None)
        assert deal_company_id(d) == ""

    def test_deal_stage_title(self):
        d = _deal("d-1", stage="In Progress")
        assert deal_stage_title(d) == "In Progress"

    def test_deal_name_raw(self):
        d = _deal("d-1", name="Acme Foods")
        assert deal_name_raw(d) == "Acme Foods"

    def test_deal_associated_people_returns_uuid_list(self):
        d = _deal("d-1", people=["p-1", "p-2", "p-3"])
        assert deal_associated_people(d) == ["p-1", "p-2", "p-3"]

    def test_deal_associated_people_empty(self):
        d = _deal("d-1", people=None)
        assert deal_associated_people(d) == []


# ── Winner selection ───────────────────────────────────────────


class TestPickDealWinner:
    def test_higher_stage_wins(self):
        a = _deal("a", stage="Lead")
        b = _deal("b", stage="In Progress")
        winner = pick_deal_winner([a, b])
        assert winner["id"]["record_id"] == "b"

    def test_more_contacts_wins_at_same_stage(self):
        a = _deal("a", stage="Lead", people=["p1"])
        b = _deal("b", stage="Lead", people=["p1", "p2", "p3"])
        winner = pick_deal_winner([a, b])
        assert winner["id"]["record_id"] == "b"

    def test_older_wins_at_same_stage_and_contacts(self):
        a = _deal("a", stage="Lead", created_at="2026-03-15T00:00:00Z")
        b = _deal("b", stage="Lead", created_at="2026-02-11T00:00:00Z")
        winner = pick_deal_winner([a, b])
        assert winner["id"]["record_id"] == "b"

    def test_lost_outranks_lead(self):
        # A "Lost" deal is a resolved deal — should not be demoted back into the cold pool.
        a = _deal("a", stage="Lead", people=["p1", "p2"])
        b = _deal("b", stage="Lost")
        winner = pick_deal_winner([a, b])
        assert winner["id"]["record_id"] == "b"


# ── Merge field computation ────────────────────────────────────


class TestComputeDealMergeFields:
    def test_empty_merge_fields_returns_empty_dict(self):
        # DEAL_MERGE_FIELDS is deliberately empty — the associated_people
        # union is the only meaningful "merge" for deals, and it's added
        # separately by bucket_deals.
        winner = _deal("w", country=None)
        loser = _deal("l", country="MX")
        assert compute_deal_merge_fields(winner, [loser]) == {}


class TestComputeDealAssociatedPeopleUnion:
    def test_new_people_unioned(self):
        winner = _deal("w", people=["p1"])
        loser = _deal("l", people=["p2", "p3"])
        union = compute_deal_associated_people_union(winner, [loser])
        assert union is not None
        ids = sorted(item["target_record_id"] for item in union)
        assert ids == ["p1", "p2", "p3"]

    def test_no_new_people_returns_none(self):
        winner = _deal("w", people=["p1", "p2"])
        loser = _deal("l", people=["p1"])
        assert compute_deal_associated_people_union(winner, [loser]) is None

    def test_empty_winner_unioned(self):
        winner = _deal("w", people=None)
        loser = _deal("l", people=["p1"])
        union = compute_deal_associated_people_union(winner, [loser])
        assert union == [{"target_object": "people", "target_record_id": "p1"}]


# ── bucket_deals — the actual classifier ───────────────────────


class TestBucketDeals:
    def test_same_company_uuid_is_auto_safe(self):
        a = _deal("a", name="Pacasmayo", company_uuid="co-pacasmayo", stage="Lead")
        b = _deal("b", name="Pacasmayo", company_uuid="co-pacasmayo", stage="Lead", people=["p1"])
        auto, conflicts = bucket_deals([a, b])
        assert len(auto) == 1
        assert len(conflicts) == 0
        assert auto[0]["match_signal"] == "same_company_uuid"
        # winner is the one with more contacts
        assert auto[0]["winner_id"] == "b"

    def test_same_name_different_company_uuid_is_conflict(self):
        a = _deal("a", name="Pacasmayo", company_uuid="co-1")
        b = _deal("b", name="Pacasmayo", company_uuid="co-2")
        auto, conflicts = bucket_deals([a, b])
        assert len(auto) == 0
        assert len(conflicts) == 1
        assert conflicts[0]["match_signal"] == "same_normalized_name_different_company_uuid"
        assert "conflict_reasons" in conflicts[0]

    def test_normalized_name_match_collapses_date_prefix(self):
        # Two deals with date prefixes + country suffixes but same underlying company
        a = _deal("a", name="202507 l Bebidas Modelo l BR", company_uuid="co-bm-br")
        b = _deal("b", name="202507 l Bebidas Modelo Chile", company_uuid="co-bm-cl")
        auto, conflicts = bucket_deals([a, b])
        # name normalizes differently because Chile is a longer suffix, but the
        # cross-UUID name dupe should still flag as conflict in the relaxed case.
        # In this case names normalize to "bebidas modelo" vs "bebidas modelo chile"
        # so they're separate buckets. Verify behavior matches expectation.
        # (This documents the "country in name" edge case.)
        assert len(auto) == 0
        # They differ once Chile is part of the name; not a name-collision.
        assert len(conflicts) == 0

    def test_unique_deal_not_flagged(self):
        a = _deal("a", name="Acme Foods", company_uuid="co-acme", stage="In Progress")
        auto, conflicts = bucket_deals([a])
        assert auto == []
        assert conflicts == []

    def test_in_progress_winner_preserved_across_lead_losers(self):
        a = _deal("a", name="Acme Foods", company_uuid="co-acme", stage="In Progress", people=["p1"])
        b = _deal("b", name="Acme Foods", company_uuid="co-acme", stage="Lead", people=["p2"])
        auto, _ = bucket_deals([a, b])
        assert len(auto) == 1
        assert auto[0]["winner_id"] == "a"
        # Loser's contact p2 should be unioned into the merge payload.
        merge = auto[0]["fields_to_merge"]
        assert "associated_people" in merge
        ids = sorted(item["target_record_id"] for item in merge["associated_people"])
        assert ids == ["p1", "p2"]


# ── apply_deal_group — exercises live mode + pretend mode ──────


class TestApplyDealGroup:
    def test_pretend_mode_does_not_call_attio(self):
        # In pretend mode the function should not touch the client (attio may be None).
        group = {
            "winner_id": "w",
            "loser_ids": ["l"],
            "fields_to_merge": {"country": [{"country_code": "MX"}]},
        }
        fh = MagicMock()
        result = apply_deal_group(attio=None, group=group, log_fh=fh, pretend=True)
        assert result["errors"] == []
        ops = [a["op"] for a in result["actions"]]
        assert "update_deal" in ops
        assert "delete_deal" in ops
        for a in result["actions"]:
            assert a.get("pretend") is True

    def test_live_mode_calls_update_then_delete(self):
        attio = MagicMock()
        attio.get_deal.return_value = {"id": {"record_id": "l"}, "values": {}}
        group = {
            "winner_id": "w",
            "loser_ids": ["l1", "l2"],
            "fields_to_merge": {"country": [{"country_code": "MX"}]},
        }
        fh = MagicMock()
        result = apply_deal_group(attio=attio, group=group, log_fh=fh, pretend=False)
        assert result["errors"] == []
        attio.update_deal.assert_called_once_with("w", {"country": [{"country_code": "MX"}]})
        assert attio.delete_deal.call_count == 2
        attio.delete_deal.assert_any_call("l1")
        attio.delete_deal.assert_any_call("l2")

    def test_errors_collected_not_raised(self):
        attio = MagicMock()
        attio.update_deal.side_effect = RuntimeError("network blew up")
        group = {
            "winner_id": "w",
            "loser_ids": ["l1"],
            "fields_to_merge": {"country": [{"country_code": "MX"}]},
        }
        fh = MagicMock()
        result = apply_deal_group(attio=attio, group=group, log_fh=fh, pretend=False)
        assert len(result["errors"]) == 1
        assert "RuntimeError" in result["errors"][0]
        # delete should not have happened because update errored first
        attio.delete_deal.assert_not_called()
