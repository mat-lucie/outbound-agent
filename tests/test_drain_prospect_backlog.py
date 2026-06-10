"""Tests for scripts/drain_prospect_backlog.py.

The drain degree-checks the WHOLE invite-eligible PROSPECT pool in one pass
(chunked) and lets the reused _pre_invite_degree_check reclassify
already-connected/pending/OON rows out of PROSPECT — WITHOUT sending any
invites. It unblocks the daily invite slice, which oldest-first ordering
otherwise keeps jammed with already-connected prospects.

Unlike the daily, the drain must NOT apply the §3.8 company throttle or
same-company dedup: those gate *contacting* a company, but a drain only reads
degree, so every eligible prospect must be checked.
"""

from __future__ import annotations

from datetime import date

from scripts.drain_prospect_backlog import (
    build_send_rows,
    drain,
    gather_eligible_prospects,
)

TODAY = date(2026, 6, 4)


def _attrs(**over) -> dict:
    """An invite-eligible PROSPECT entry (parse_entry shape), overridable."""
    base: dict = {
        "entry_id": "e1",
        "record_id": "r1",
        "stage": "Prospect",
        "quality_score": 70,
        "invite_eligible_after": "2026-06-02",  # past -> eligible
        "experiment_id_frozen_at": "prospect",  # not legacy_* -> send-eligible
        "scoring_lane": "enterprise_mode",
        "created_at": "2026-05-01",
    }
    base.update(over)
    return base


class _FakeCache:
    """Minimal stand-in for RecordCache.

    get(record_id) -> (name, company, linkedin_url, industry, title).
    """

    def __init__(self, url_by_record: dict[str, str]):
        self._urls = url_by_record

    def get(self, record_id: str) -> tuple:
        url = self._urls.get(record_id, "")
        return ("Synthetic Name", "Synthetic Co", url, "technology", "Engineer")


class TestGatherEligibleProspects:
    def test_includes_eligible_prospect(self) -> None:
        assert len(gather_eligible_prospects([_attrs()], TODAY)) == 1

    def test_excludes_non_prospect_stage(self) -> None:
        assert gather_eligible_prospects([_attrs(stage="Connection Sent")], TODAY) == []

    def test_excludes_low_quality_score(self) -> None:
        assert gather_eligible_prospects([_attrs(quality_score=59)], TODAY) == []

    def test_excludes_not_yet_invite_eligible(self) -> None:
        """A fresh prospect still inside its quarantine window is skipped —
        same §3.1 gate the daily invite slice uses."""
        future = gather_eligible_prospects(
            [_attrs(invite_eligible_after="2026-06-30")], TODAY
        )
        assert future == []

    def test_sorts_enterprise_lane_before_legacy(self) -> None:
        pool = gather_eligible_prospects(
            [
                _attrs(entry_id="legacy", scoring_lane="legacy"),
                _attrs(entry_id="ent", scoring_lane="enterprise_mode"),
            ],
            TODAY,
        )
        assert [a["entry_id"] for a in pool] == ["ent", "legacy"]

    def test_sorts_target_company_mode_between_enterprise_and_legacy(self) -> None:
        pool = gather_eligible_prospects(
            [
                _attrs(entry_id="leg", scoring_lane="legacy"),
                _attrs(entry_id="tc", scoring_lane="target_company_mode"),
                _attrs(entry_id="ent", scoring_lane="enterprise_mode"),
            ],
            TODAY,
        )
        assert [a["entry_id"] for a in pool] == ["ent", "tc", "leg"]


class TestBuildSendRows:
    def test_resolves_url_and_blanks_message(self) -> None:
        rows = build_send_rows(
            [_attrs(record_id="r1")],
            _FakeCache({"r1": "https://www.linkedin.com/in/synthetic-user"}),
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["linkedInUrl"] == "https://www.linkedin.com/in/synthetic-user"
        assert row["message"] == ""  # a drain never sends an invite
        assert row["entry_id"] == "e1"
        assert row["current_stage"] == "Prospect"

    def test_skips_rows_with_no_linkedin_url(self) -> None:
        rows = build_send_rows([_attrs(record_id="r1")], _FakeCache({}))
        assert rows == []

    def test_carries_experiment_fields(self) -> None:
        """experiment_id and experiment_id_frozen_at must be forwarded so
        _pre_invite_degree_check's PR-21 immutability guard has the data it
        needs (KeyError is a caller bug per the module contract)."""
        rows = build_send_rows(
            [_attrs(record_id="r1", experiment_id="exp-abc", experiment_id_frozen_at="prospect")],
            _FakeCache({"r1": "https://www.linkedin.com/in/synthetic-user"}),
        )
        assert rows[0]["experiment_id"] == "exp-abc"
        assert rows[0]["experiment_id_frozen_at"] == "prospect"


class TestDrain:
    def test_tallies_fresh_vs_drained_across_chunks(self) -> None:
        # 4 rows, chunk_size 2 -> 2 chunks. Fake degree-check keeps the first
        # row of each chunk as "still to invite" (fresh) and flips the rest.
        rows = [{"entry_id": f"e{i}"} for i in range(4)]

        def fake_degree_check(chunk: list[dict]) -> tuple[list[dict], list[dict]]:
            still = chunk[:1]      # 1 fresh per chunk
            connected = chunk[1:]  # rest reclassified
            return still, connected

        report = drain(rows, degree_check=fake_degree_check, chunk_size=2)
        assert report["examined"] == 4
        assert report["fresh"] == 2      # 1 per chunk
        assert report["drained"] == 2    # examined - fresh

    def test_limit_caps_rows_examined(self) -> None:
        rows = [{"entry_id": f"e{i}"} for i in range(100)]

        def fake_degree_check(chunk: list[dict]) -> tuple[list[dict], list[dict]]:
            return [], chunk  # flip everything

        report = drain(rows, degree_check=fake_degree_check, chunk_size=25, limit=25)
        assert report["examined"] == 25

    def test_empty_pool_is_noop(self) -> None:
        called: list = []
        report = drain([], degree_check=lambda c: called.append(c) or ([], []), chunk_size=25)
        assert report["examined"] == 0
        assert called == []

    def test_dropped_chunk_is_failed_not_drained(self) -> None:
        """_pre_invite_degree_check returns ([], []) when it DROPS a chunk (PB
        failure / dead cookie). A non-empty chunk yielding zero fresh AND zero
        connected did not resolve — it must count as FAILED, never as drained,
        so a failed sweep can't masquerade as 'backlog cleared'."""
        rows = [{"entry_id": f"e{i}"} for i in range(3)]
        report = drain(rows, degree_check=lambda c: ([], []), chunk_size=3)
        assert report["failed_chunks"] == 1
        assert report["failed_rows"] == 3
        assert report["drained"] == 0
        assert report["examined"] == 0  # nothing was actually processed

    def test_exception_in_chunk_counted_as_failed(self) -> None:
        """A raising degree_check (ConfigError, PB timeout, Attio write error)
        must not abort the whole sweep nor be counted as drained."""
        def boom(chunk: list[dict]) -> tuple[list[dict], list[dict]]:
            raise RuntimeError("PB timeout")

        received: list[dict] = []

        def on_chunk(idx: int, total: int, info: dict) -> None:
            received.append(info)

        rows = [{"entry_id": f"e{i}"} for i in range(5)]
        report = drain(rows, degree_check=boom, chunk_size=5, on_chunk=on_chunk)
        assert report["failed_chunks"] == 1
        assert report["failed_rows"] == 5
        assert report["drained"] == 0
        # The per-chunk failure info carries the exception class name for triage.
        assert received[0]["error"].startswith("RuntimeError: ")

    def test_mixed_success_and_failure_tally(self) -> None:
        """A failed chunk among successful ones is isolated and reported."""
        rows = [{"entry_id": f"e{i}"} for i in range(4)]
        calls: dict[str, int] = {"n": 0}

        def flaky(chunk: list[dict]) -> tuple[list[dict], list[dict]]:
            calls["n"] += 1
            # Second chunk drops (simulates a PB batch failure).
            return ([], []) if calls["n"] == 2 else (chunk[:1], chunk[1:])

        report = drain(rows, degree_check=flaky, chunk_size=2)
        assert report["failed_chunks"] == 1
        assert report["failed_rows"] == 2
        assert report["examined"] == 2   # only the successful chunk
        assert report["fresh"] == 1
        assert report["drained"] == 1

    def test_on_chunk_callback_receives_progress_info(self) -> None:
        """on_chunk is called once per chunk with row/fresh/drained counts."""
        rows = [{"entry_id": f"e{i}"} for i in range(4)]
        received: list[dict] = []

        def fake_degree_check(chunk: list[dict]) -> tuple[list[dict], list[dict]]:
            return chunk, []  # all fresh

        def on_chunk(idx: int, total: int, info: dict) -> None:
            received.append({"idx": idx, "total": total, **info})

        drain(rows, degree_check=fake_degree_check, chunk_size=2, on_chunk=on_chunk)
        assert len(received) == 2
        assert received[0]["idx"] == 1
        assert received[0]["total"] == 2
        assert received[0]["fresh"] == 2
        assert received[0]["drained"] == 0

    def test_failed_chunk_callback_has_failed_flag(self) -> None:
        """on_chunk receives failed=True for dropped/raising chunks."""
        rows = [{"entry_id": "e0"}]
        received: list[dict] = []

        def on_chunk(idx: int, total: int, info: dict) -> None:
            received.append(info)

        drain(rows, degree_check=lambda c: ([], []), chunk_size=1, on_chunk=on_chunk)
        assert received[0]["failed"] is True

    def test_connected_1st_tally(self) -> None:
        """connected_1st counts rows returned in the already_connected list."""
        rows = [{"entry_id": f"e{i}"} for i in range(6)]

        def fake_degree_check(chunk: list[dict]) -> tuple[list[dict], list[dict]]:
            # 2 still-invitable, 4 connected per chunk (chunk_size=6 -> 1 chunk)
            return chunk[:2], chunk[2:]

        report = drain(rows, degree_check=fake_degree_check, chunk_size=6)
        assert report["connected_1st"] == 4
        assert report["fresh"] == 2
        assert report["drained"] == 4
