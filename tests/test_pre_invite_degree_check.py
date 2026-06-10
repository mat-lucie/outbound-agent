"""Tests for the pre-invite degree check in workflows/daily_check.py.

Locks in the partition behavior that catches Pattern A (re-prospected records
re-added after dedup) and Pattern B (the operator connected externally) before any
phantom invite ships. Without this guard, LinkedIn no-ops the invite, Phase 0
detects degree=1st within 14 days, flips to ACCEPTED, and a "thanks for
accepting" DM goes out to a long-time connection.

PR-B.9 (Sales Nav migration): the `TestPreInviteDegreeCheck` class below
exercises the LEGACY regular Profile Scraper path explicitly (the rollback
default). The cross-backend behavior that holds identically — empty batch
short-circuit, 1st-degree Pattern-A flip, URL normalization for matched
rows — lives in `TestPreInviteDegreeCheckCrossBackend` so a regression
trips on BOTH backends.

The §3.1-hardened sales_nav default arm (escalate-and-drop on
blank/missing/unknown/pending) is covered by
`tests/test_pre_invite_sales_nav_path.py`.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from workflows.daily_check import _pre_invite_degree_check


def _csv(rows: list[dict]) -> str:
    """Build a Profile Scraper-style CSV with linkedinProfileUrl + connectionDegree."""
    if not rows:
        return ""
    header = "linkedinProfileUrl,connectionDegree\n"
    body = "\n".join(f"{r['url']},{r['degree']}" for r in rows)
    return header + body


def _sales_nav_csv(rows: list[dict]) -> str:
    """Build a Sales Nav-style CSV. Includes hasPendingInvitation + query."""
    if not rows:
        return ""
    header = (
        "linkedinProfileUrl,connectionDegree,hasPendingInvitation,query,fullName\n"
    )
    body = "\n".join(
        f"{r['url']},{r['degree']},{r.get('pending', 'false')},{r['url']},"
        for r in rows
    )
    return header + body


# Sales Nav backend env helpers, used in the cross-backend parametrize.
_SALES_NAV_SCRAPER_ID = "phantom-602790655114603"


def _enable_sales_nav_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRE_INVITE_DEGREE_CHECK_BACKEND", "sales_nav")
    monkeypatch.setenv("PB_SALES_NAV_PROFILE_SCRAPER_ID", _SALES_NAV_SCRAPER_ID)
    monkeypatch.setenv("PB_PROFILE_SCRAPER_ID", "phantom-LEGACY-different-id")
    monkeypatch.setenv("PB_LI_SALES_NAV_SESSION_COOKIE", "fake-li-at")


def _enable_regular_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRE_INVITE_DEGREE_CHECK_BACKEND", raising=False)
    monkeypatch.delenv("PB_SALES_NAV_PROFILE_SCRAPER_ID", raising=False)
    monkeypatch.delenv("PB_LI_SALES_NAV_SESSION_COOKIE", raising=False)


def _stub_pb_for_sales_nav(pb: MagicMock) -> None:
    """Set up the launch helper's get_agent + launch_agent dependencies.

    The Sales Nav path reads `agent.get("argument")` then mutates an
    identities slot, then launches. Without the get_agent stub, the launch
    helper crashes on .get(). The launch_agent stub gives the returned
    launch a string container_id so downstream escalation payloads pass
    DegreeUnknownPayload's `scrape_attempt_id: str` type-check.
    """
    pb.get_agent.return_value = {"argument": '{"identities": [{}]}'}
    launch = MagicMock()
    launch.container_id = "container-stub"
    pb.launch_agent.return_value = launch


@patch("workflows.daily_check.write_prospects_to_sheet", return_value="https://sheet.example/foo")
@patch("workflows.daily_check._pb_session_args", return_value={})
class TestPreInviteDegreeCheck:
    """Each test sets up a 3-URL invite batch and varies what PB returns."""

    def _make_batch(self):
        # PR-15: every batch row needs `record_id` (for the partial-CSV
        # `degree_unknown` emit's idempotency key) + `current_stage`
        # (for the Pattern-A flip's AttioWriter prior_values check).
        # PR-21: every row also needs `experiment_id` + `experiment_id_frozen_at`
        # (direct key access in pre_invite_check raises KeyError on missing keys).
        return [
            {"linkedInUrl": "https://www.linkedin.com/in/alice", "message": "hi A",
             "entry_id": "ent-A", "record_id": "rec-A", "current_stage": "Prospect",
             "experiment_id": "exp-test", "experiment_id_frozen_at": "prospect"},
            {"linkedInUrl": "https://www.linkedin.com/in/bob", "message": "hi B",
             "entry_id": "ent-B", "record_id": "rec-B", "current_stage": "Prospect",
             "experiment_id": "exp-test", "experiment_id_frozen_at": "prospect"},
            {"linkedInUrl": "https://www.linkedin.com/in/carol", "message": "hi C",
             "entry_id": "ent-C", "record_id": "rec-C", "current_stage": "Prospect",
             "experiment_id": "exp-test", "experiment_id_frozen_at": "prospect"},
        ]

    def test_one_first_degree_partitioned_out_and_flipped_to_accepted(self, _pb_args, _sheet):
        attio = MagicMock()
        pb = MagicMock()
        pb.download_result_csv.return_value = _csv([
            {"url": "https://www.linkedin.com/in/alice", "degree": "1st"},
            {"url": "https://www.linkedin.com/in/bob", "degree": "2nd"},
            {"url": "https://www.linkedin.com/in/carol", "degree": "3rd"},
        ])

        still, already = _pre_invite_degree_check(
            self._make_batch(), pb, "scraper-id", attio, "list-id"
        )

        # Alice partitioned out and flipped in Attio. PR-15 routes the
        # flip through AttioWriter (single-PATCH multi-attribute write).
        # Wave-2-B: AttioWriter dispatches list-entry writes via
        # ``attio.update_list_entry`` so the assertion targets that
        # helper directly.
        assert len(already) == 1
        assert already[0]["entry_id"] == "ent-A"

        ent_a_calls = [
            c for c in attio.update_list_entry.call_args_list
            if c.kwargs.get("entry_id") == "ent-A"
        ]
        assert len(ent_a_calls) == 1, (
            f"expected exactly one update for ent-A; got {ent_a_calls!r}"
        )
        # Body must contain BOTH stage AND last_contact_date atomically
        # (B-SD-010 §3.1 protection — torn write would allow re-invite).
        entry_values = ent_a_calls[0].kwargs["entry_attributes"]
        assert entry_values["stage"] == "Accepted"
        assert "last_contact_date" in entry_values

        # Bob and Carol stay in the invite queue.
        assert len(still) == 2
        assert {row["entry_id"] for row in still} == {"ent-B", "ent-C"}

    def test_all_second_degree_no_partition_no_attio_writes(self, _pb_args, _sheet):
        attio = MagicMock()
        pb = MagicMock()
        pb.download_result_csv.return_value = _csv([
            {"url": "https://www.linkedin.com/in/alice", "degree": "2nd"},
            {"url": "https://www.linkedin.com/in/bob", "degree": "2nd"},
            {"url": "https://www.linkedin.com/in/carol", "degree": "2nd"},
        ])

        still, already = _pre_invite_degree_check(
            self._make_batch(), pb, "scraper-id", attio, "list-id"
        )

        assert len(still) == 3
        assert already == []
        # PR-15: no Pattern-A flips → no list-entry updates issued.
        # Wave-2-B: AttioWriter dispatches list updates via
        # ``update_list_entry`` so the absence-of-flip assertion now
        # checks that helper for zero calls instead of inspecting
        # raw ``_client.request`` PATCH bodies.
        assert attio.update_list_entry.call_count == 0

    def test_pb_returns_no_csv_drops_entire_batch(self, _pb_args, _sheet):
        """Fail-safe: when PB returns nothing, drop the invite batch entirely
        rather than risk re-inviting already-connected prospects.

        This is intentional. The cost of one missed day of invites is tiny
        compared to the relationship damage of re-inviting people the operator is
        already connected with — exactly the bug pattern this guard exists for.
        """
        attio = MagicMock()
        pb = MagicMock()
        pb.download_result_csv.return_value = None

        still, already = _pre_invite_degree_check(
            self._make_batch(), pb, "scraper-id", attio, "list-id"
        )

        assert still == []
        assert already == []
        attio.update_list_entry.assert_not_called()

    def test_unknown_degree_keeps_in_invite_queue(self, _pb_args, _sheet):
        """If PB returns a profile but the degree is missing/blank, default to
        keeping it in the invite queue (better than dropping a legitimate
        prospect over a Phantom data quirk)."""
        attio = MagicMock()
        pb = MagicMock()
        pb.download_result_csv.return_value = _csv([
            {"url": "https://www.linkedin.com/in/alice", "degree": ""},
            {"url": "https://www.linkedin.com/in/bob", "degree": "2nd"},
            # Carol entirely missing from CSV
        ])

        still, already = _pre_invite_degree_check(
            self._make_batch(), pb, "scraper-id", attio, "list-id"
        )

        # All 3 stay in invite queue — only confirmed 1st-degree gets partitioned out.
        assert len(still) == 3
        assert already == []
        attio.update_list_entry.assert_not_called()

    def test_empty_batch_short_circuits(self, _pb_args, _sheet):
        attio = MagicMock()
        pb = MagicMock()

        still, already = _pre_invite_degree_check(
            [], pb, "scraper-id", attio, "list-id"
        )

        assert still == []
        assert already == []
        pb.launch_agent.assert_not_called()
        attio.update_list_entry.assert_not_called()

    def test_url_normalization_matches_www_variant(self, _pb_args, _sheet):
        """PB sometimes returns linkedinProfileUrl without the www. prefix; the
        partition must still match the queue's www. URL via _normalize_linkedin_url."""
        attio = MagicMock()
        pb = MagicMock()
        pb.download_result_csv.return_value = _csv([
            {"url": "https://linkedin.com/in/alice", "degree": "1st"},  # no www.
            {"url": "https://www.linkedin.com/in/bob", "degree": "2nd"},
            {"url": "https://www.linkedin.com/in/carol", "degree": "2nd"},
        ])

        still, already = _pre_invite_degree_check(
            self._make_batch(), pb, "scraper-id", attio, "list-id"
        )

        assert len(already) == 1
        assert already[0]["entry_id"] == "ent-A"
        assert len(still) == 2


# ===========================================================================
# PR-B.9 — cross-backend invariants
# ===========================================================================
#
# Behavior that holds identically across `regular` and `sales_nav` backends.
# A regression here must trip on BOTH paths. The §3.1-divergent behavior
# (sales_nav's hardened default arm vs regular's permissive default arm) is
# NOT exercised here — see tests/test_pre_invite_sales_nav_path.py for
# sales_nav-specific assertions.


def _kwargs_for_backend(backend: str) -> dict:
    """Build the per-backend kwargs to thread into _pre_invite_degree_check."""
    if backend == "sales_nav":
        return {"sales_nav_profile_scraper_id": _SALES_NAV_SCRAPER_ID}
    return {}


def _csv_for_backend(backend: str, rows: list[dict]) -> str:
    """Build the CSV body that matches the backend's expected schema."""
    if backend == "sales_nav":
        return _sales_nav_csv(rows)
    return _csv(rows)


def _make_batch_3() -> list[dict]:
    return [
        {"linkedInUrl": "https://www.linkedin.com/in/alice", "message": "hi A",
         "entry_id": "ent-A", "record_id": "rec-A", "current_stage": "Prospect",
         "experiment_id": "exp-test", "experiment_id_frozen_at": "prospect"},
        {"linkedInUrl": "https://www.linkedin.com/in/bob", "message": "hi B",
         "entry_id": "ent-B", "record_id": "rec-B", "current_stage": "Prospect",
         "experiment_id": "exp-test", "experiment_id_frozen_at": "prospect"},
        {"linkedInUrl": "https://www.linkedin.com/in/carol", "message": "hi C",
         "entry_id": "ent-C", "record_id": "rec-C", "current_stage": "Prospect",
         "experiment_id": "exp-test", "experiment_id_frozen_at": "prospect"},
    ]


@pytest.mark.parametrize("backend", ["regular", "sales_nav"])
@patch("workflows.daily_check.write_prospects_to_sheet", return_value="https://sheet.example/foo")
@patch("workflows.daily_check._pb_session_args", return_value={})
class TestPreInviteDegreeCheckCrossBackend:
    """Identical-across-backends invariants. A regression here breaks BOTH
    paths simultaneously — the safety net no rollback can repair."""

    def test_empty_batch_short_circuits_no_pb_call(
        self, _pb_args, _sheet, backend: str, monkeypatch: pytest.MonkeyPatch,
    ):
        if backend == "sales_nav":
            _enable_sales_nav_env(monkeypatch)
        else:
            _enable_regular_env(monkeypatch)
        attio = MagicMock()
        pb = MagicMock()

        still, already = _pre_invite_degree_check(
            [], pb, "scraper-id", attio, "list-id",
            **_kwargs_for_backend(backend),
        )

        assert still == []
        assert already == []
        pb.launch_agent.assert_not_called()
        # No Attio writes either — empty input is a complete no-op.
        attio.update_list_entry.assert_not_called()

    def test_first_degree_partitions_out_and_flips_to_accepted(
        self, _pb_args, _sheet, backend: str, monkeypatch: pytest.MonkeyPatch,
    ):
        """1st-degree → Pattern-A flip. The atomic single-PATCH multi-attribute
        write contract holds identically on both backends — same writer.apply
        call site."""
        if backend == "sales_nav":
            _enable_sales_nav_env(monkeypatch)
        else:
            _enable_regular_env(monkeypatch)

        attio = MagicMock()
        pb = MagicMock()
        _stub_pb_for_sales_nav(pb)  # no-op for regular path
        pb.download_result_csv.return_value = _csv_for_backend(backend, [
            {"url": "https://www.linkedin.com/in/alice", "degree": "1st"},
            {"url": "https://www.linkedin.com/in/bob", "degree": "2nd"},
            {"url": "https://www.linkedin.com/in/carol", "degree": "3rd"},
        ])

        still, already = _pre_invite_degree_check(
            _make_batch_3(), pb, "scraper-id", attio, "list-id",
            **_kwargs_for_backend(backend),
        )

        # Alice flipped via AttioWriter. Wave-2-B: dispatch routes
        # through ``attio.update_list_entry`` so the assertion targets
        # that helper directly.
        assert len(already) == 1
        assert already[0]["entry_id"] == "ent-A"
        ent_a_calls = [
            c for c in attio.update_list_entry.call_args_list
            if c.kwargs.get("entry_id") == "ent-A"
        ]
        assert len(ent_a_calls) == 1
        entry_values = ent_a_calls[0].kwargs["entry_attributes"]
        # Atomicity: stage + last_contact_date in the SAME dispatch call.
        assert entry_values["stage"] == "Accepted"
        assert "last_contact_date" in entry_values

        # Bob (2nd) + Carol (3rd) → invite queue on BOTH backends.
        assert {row["entry_id"] for row in still} == {"ent-B", "ent-C"}

    def test_all_second_or_third_degree_no_partition_no_attio_writes(
        self, _pb_args, _sheet, backend: str, monkeypatch: pytest.MonkeyPatch,
    ):
        """Pure 2nd/3rd-degree batch → all in still_to_invite, zero Attio writes.
        This is the common happy path; an Attio write here would indicate a
        cross-backend regression in the partition logic."""
        if backend == "sales_nav":
            _enable_sales_nav_env(monkeypatch)
        else:
            _enable_regular_env(monkeypatch)

        attio = MagicMock()
        pb = MagicMock()
        _stub_pb_for_sales_nav(pb)
        pb.download_result_csv.return_value = _csv_for_backend(backend, [
            {"url": "https://www.linkedin.com/in/alice", "degree": "2nd"},
            {"url": "https://www.linkedin.com/in/bob", "degree": "2nd"},
            {"url": "https://www.linkedin.com/in/carol", "degree": "3rd"},
        ])

        still, already = _pre_invite_degree_check(
            _make_batch_3(), pb, "scraper-id", attio, "list-id",
            **_kwargs_for_backend(backend),
        )

        assert len(still) == 3
        assert already == []
        # Wave-2-B: AttioWriter dispatches list updates via
        # ``update_list_entry``; absence of writes means zero calls.
        assert attio.update_list_entry.call_count == 0

    def test_url_normalization_matches_www_variant_on_first_degree(
        self, _pb_args, _sheet, backend: str, monkeypatch: pytest.MonkeyPatch,
    ):
        """The phantom may echo URLs without `www.`; the partition must
        still match the queue's `www.` URL via `_normalize_linkedin_url`.
        Holds identically on both backends — same matcher."""
        if backend == "sales_nav":
            _enable_sales_nav_env(monkeypatch)
        else:
            _enable_regular_env(monkeypatch)

        attio = MagicMock()
        pb = MagicMock()
        _stub_pb_for_sales_nav(pb)
        pb.download_result_csv.return_value = _csv_for_backend(backend, [
            {"url": "https://linkedin.com/in/alice", "degree": "1st"},  # no www.
            {"url": "https://www.linkedin.com/in/bob", "degree": "2nd"},
            {"url": "https://www.linkedin.com/in/carol", "degree": "2nd"},
        ])

        still, already = _pre_invite_degree_check(
            _make_batch_3(), pb, "scraper-id", attio, "list-id",
            **_kwargs_for_backend(backend),
        )

        # Alice's no-www CSV row matched the queue's www-prefixed URL.
        assert len(already) == 1
        assert already[0]["entry_id"] == "ent-A"
        assert len(still) == 2
        assert {row["entry_id"] for row in still} == {"ent-B", "ent-C"}

    def test_pb_returns_empty_csv_drops_entire_batch(
        self, _pb_args, _sheet, backend: str, monkeypatch: pytest.MonkeyPatch,
    ):
        """Empty CSV → drop batch on BOTH backends. The §3.1 fail-safe:
        better to lose a day's invites than risk re-inviting connected
        people.

        On regular backend: the early-return at line 629-635 fires.
        On sales_nav backend: the helper returns ({}, {}) but with
        container_id set; the partition then routes every prospect to
        the §3.1-hardened default arm (escalate-and-drop). Net effect on
        both backends: still == []."""
        if backend == "sales_nav":
            _enable_sales_nav_env(monkeypatch)
        else:
            _enable_regular_env(monkeypatch)

        attio = MagicMock()
        pb = MagicMock()
        _stub_pb_for_sales_nav(pb)
        pb.download_result_csv.return_value = None

        still, already = _pre_invite_degree_check(
            _make_batch_3(), pb, "scraper-id", attio, "list-id",
            **_kwargs_for_backend(backend),
        )

        assert still == []
        assert already == []
        attio.update_list_entry.assert_not_called()
