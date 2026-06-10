"""PR-32 tests: weekly_brain JSON sidecar + open_proposal_pr + sales_approve."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock

import pytest

from clients.crm.attio_provider import AttioProvider
from workflows.sales_approve import (
    ProposalNotFoundError,
    approve_proposal,
    list_pending_proposals,
    reject_proposal,
)
from workflows.weekly_brain import (
    VariantScore,
    build_proposal_json_sidecar,
    open_proposal_pr,
)

# ────────────────────────────────────────────────────────────────────────
# JSON sidecar emit
# ────────────────────────────────────────────────────────────────────────


def _variant_score(variant_id: str, defensive_q75: float = 0.3) -> VariantScore:
    """Build a VariantScore with controllable q75 to drive ranking."""
    return VariantScore(
        variant_id=variant_id,
        variant_text=f"text for {variant_id}",
        scores_by_persona={
            "synthetic_persona_a": {
                "defensive_likelihood": [defensive_q75 - 0.1, defensive_q75 + 0.1],
                "engagement_likelihood": [0.4, 0.6],
                "reasoning": "test",
            },
        },
    )


class TestBuildProposalJsonSidecar:
    def test_emits_recommendations_per_persona(self):
        scored = {
            "ops_leaders": {
                "dm1": _variant_score("dm1", defensive_q75=0.5),
                "dm1_v3": _variant_score("dm1_v3", defensive_q75=0.2),
            }
        }
        sidecar = build_proposal_json_sidecar(
            experiment_id="exp-001",
            variants_by_persona={"ops_leaders": {"dm1": "old", "dm1_v3": "new"}},
            scored=scored,
            cohorts=[],
            defensive_samples_by_persona={"ops_leaders": []},
        )

        assert sidecar["experiment_id"] == "exp-001"
        assert "generated_at" in sidecar
        assert "ops_leaders" in sidecar["recommendations"]
        rec = sidecar["recommendations"]["ops_leaders"]
        # The lower-defensive variant should rank first.
        assert rec["recommended_variant_id"] == "dm1_v3"
        ranking_ids = [r["variant_id"] for r in rec["ranking"]]
        assert ranking_ids == ["dm1_v3", "dm1"]

    def test_includes_cohort_evidence_and_samples(self):
        sidecar = build_proposal_json_sidecar(
            experiment_id="exp-X",
            variants_by_persona={},
            scored={},
            cohorts=[{"experiment_id": "exp-X", "cohort_size": 12}],
            defensive_samples_by_persona={"ops_leaders": ["sample 1"]},
        )
        assert sidecar["cohort_evidence"][0]["cohort_size"] == 12
        assert sidecar["defensive_samples"]["ops_leaders"] == ["sample 1"]

    def test_sidecar_is_json_serializable(self):
        sidecar = build_proposal_json_sidecar(
            experiment_id="exp-X",
            variants_by_persona={"p": {"v1": "x"}},
            scored={"p": {"v1": _variant_score("v1")}},
            cohorts=[],
            defensive_samples_by_persona={"p": []},
        )
        # Round-trip without TypeError.
        encoded = json.dumps(sidecar, default=str)
        decoded = json.loads(encoded)
        assert decoded["experiment_id"] == "exp-X"


# ────────────────────────────────────────────────────────────────────────
# open_proposal_pr — subprocess-based gh flow
# ────────────────────────────────────────────────────────────────────────


def _make_completed_proc(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["gh"], returncode=returncode, stdout=stdout, stderr="",
    )


class TestOpenProposalPr:
    def test_invokes_git_then_gh_in_correct_order(self, tmp_path):
        # Each subprocess.run call returns a no-op; the final gh pr create
        # returns the PR URL.
        calls: list[list[str]] = []

        def fake_run(args, **_):
            calls.append(list(args))
            if args[0] == "gh" and args[1] == "pr" and args[2] == "create":
                return _make_completed_proc(
                    stdout="https://github.com/example-org/outbound-agent/pull/999\n",
                )
            return _make_completed_proc()

        result = open_proposal_pr(
            experiment_id="exp-007",
            proposal_paths=[tmp_path / "p.md", tmp_path / "p.json"],
            run_cmd=fake_run,
        )

        assert result.pr_url == "https://github.com/example-org/outbound-agent/pull/999"
        assert result.branch_name == "experiment/exp-007-proposal"
        # Order: checkout, add, commit, push, pr create.
        assert [c[0:2] for c in calls] == [
            ["git", "checkout"],
            ["git", "add"],
            ["git", "commit"],
            ["git", "push"],
            ["gh", "pr"],
        ]
        # Branch name correctly propagates to checkout + push.
        assert calls[0][-2:] == ["experiment/exp-007-proposal", "main"]

    def test_raises_when_gh_returns_non_url(self):
        def fake_run(args, **_):
            if args[0] == "gh":
                return _make_completed_proc(stdout="oops not a url\n")
            return _make_completed_proc()

        with pytest.raises(RuntimeError, match="did not return a recognizable URL"):
            open_proposal_pr(
                experiment_id="exp-1",
                proposal_paths=[],
                run_cmd=fake_run,
            )

    def test_propagates_subprocess_failure(self):
        def fake_run(args, **_):
            if args[0] == "git" and args[1] == "push":
                raise subprocess.CalledProcessError(
                    returncode=1, cmd=args, output="", stderr="rejected"
                )
            return _make_completed_proc()

        with pytest.raises(subprocess.CalledProcessError):
            open_proposal_pr(
                experiment_id="exp-1",
                proposal_paths=[],
                run_cmd=fake_run,
            )


# ────────────────────────────────────────────────────────────────────────
# sales_approve
# ────────────────────────────────────────────────────────────────────────


def _queue_row(
    experiment_id: str,
    pr_url: str,
    record_id: str = "q-1",
    payload_serialized: bool = True,
) -> dict:
    """Shape of an operator_review_queue row as returned by the Attio query.

    The migration script declares ``payload_json`` as ``long_text``, so
    production payloads arrive serialized. ``payload_serialized=False``
    exercises the dict-passthrough branch in ``list_pending_proposals``.
    """
    payload = {
        "experiment_id": experiment_id,
        "pr_url": pr_url,
        "branch_name": f"experiment/{experiment_id}-proposal",
        "proposal_markdown_path": "docs/experiments/x.md",
        "proposal_json_path": "docs/experiments/x.json",
    }
    payload_value = json.dumps(payload) if payload_serialized else payload
    return {
        "id": {"record_id": record_id},
        "values": {
            "type": [{"value": "variant_proposal_pending"}],
            "status": [{"value": "open"}],
            "payload_json": [{"value": payload_value}],
        },
    }


def _provider_returning(rows: list[dict]) -> tuple[AttioProvider, MagicMock]:
    """Build a real ``AttioProvider`` over a mocked inner ``AttioClient``
    whose ``_request`` returns ``{"data": rows}``.

    The list functions now read through ``CRMProvider.query_object_records``;
    wrapping the mock in the real ``AttioProvider`` exercises the genuine
    query-body construction + ``Record`` normalization, and ``record.raw``
    reproduces each original row dict — so the parsed output is asserted
    through the same path production uses. Returns the provider plus the
    inner mock so callers can assert the query body.
    """
    attio = MagicMock()
    attio._request.return_value = {"data": rows}
    return AttioProvider(attio_client=attio), attio


class TestListPendingProposals:
    def test_returns_parsed_rows(self):
        crm, attio = _provider_returning([
            _queue_row("exp-1", "https://gh/pull/1", record_id="q-1"),
            _queue_row("exp-2", "https://gh/pull/2", record_id="q-2"),
        ])
        result = list_pending_proposals(crm)
        assert len(result) == 2
        assert result[0]["experiment_id"] == "exp-1"
        assert result[0]["queue_record_id"] == "q-1"
        assert result[1]["pr_url"] == "https://gh/pull/2"
        # The query targets the operator_review_queue object with the
        # Attio-native pending filter passed through unchanged.
        method, path = attio._request.call_args[0][:2]
        assert method == "POST"
        assert path == "/objects/operator_review_queue/records/query"
        body = attio._request.call_args.kwargs["json"]
        assert body["filter"] == {
            "$and": [
                {"type": "variant_proposal_pending"},
                {"status": {"$not": {"$in": ["resolved", "rejected"]}}},
            ],
        }
        assert body["limit"] == 100

    def test_returns_empty_when_no_pending(self):
        crm, _ = _provider_returning([])
        assert list_pending_proposals(crm) == []

    def test_reads_payload_json_attr_not_payload(self):
        """Regression: the Attio api_slug is ``payload_json`` (per the
        migration script's ATTRIBUTES spec), not ``payload``. A row that
        carries ``payload_json`` with a serialized JSON string — exactly
        what production returns — must parse cleanly. A row that only
        carries the legacy ``payload`` slug must NOT mask a real production
        row's empty fields.
        """
        crm, _ = _provider_returning([
            {
                "id": {"record_id": "q-prod"},
                "values": {
                    "type": [{"value": "variant_proposal_pending"}],
                    "status": [{"value": "open"}],
                    "payload_json": [{"value": json.dumps({
                        "experiment_id": "exp-prod",
                        "pr_url": "https://gh/pull/777",
                        "branch_name": "experiment/exp-prod-proposal",
                        "proposal_markdown_path": "docs/experiments/p.md",
                        "proposal_json_path": "docs/experiments/p.json",
                    })}],
                },
            },
        ])
        rows = list_pending_proposals(crm)
        assert len(rows) == 1
        assert rows[0]["experiment_id"] == "exp-prod"
        assert rows[0]["pr_url"] == "https://gh/pull/777"
        assert rows[0]["branch_name"] == "experiment/exp-prod-proposal"
        assert rows[0]["queue_record_id"] == "q-prod"

    def test_legacy_payload_attr_returns_empty_fields(self):
        """A row that only has the legacy ``payload`` slug (which the
        production Attio object never emits) must NOT silently populate
        downstream fields. This pins the contract so a future regression
        flipping back to the wrong slug is caught."""
        crm, _ = _provider_returning([
            {
                "id": {"record_id": "q-legacy"},
                "values": {
                    "type": [{"value": "variant_proposal_pending"}],
                    "status": [{"value": "open"}],
                    "payload": [{"value": {
                        "experiment_id": "exp-x",
                        "pr_url": "https://gh/pull/1",
                        "branch_name": "experiment/exp-x-proposal",
                        "proposal_markdown_path": "docs/x.md",
                        "proposal_json_path": "docs/x.json",
                    }}],
                },
            },
        ])
        rows = list_pending_proposals(crm)
        assert len(rows) == 1
        assert rows[0]["queue_record_id"] == "q-legacy"
        # The legacy slug is not what production emits — every downstream
        # field stays empty rather than being silently populated.
        assert rows[0]["experiment_id"] == ""
        assert rows[0]["pr_url"] == ""
        assert rows[0]["branch_name"] == ""

    def test_dict_payload_passthrough(self):
        """Payloads written in-memory (test fixtures, dict-not-serialized)
        still parse — preserves the dual-shape contract documented in
        ``_queue_row``."""
        crm, _ = _provider_returning([
            _queue_row("exp-d", "https://gh/pull/3", payload_serialized=False)
        ])
        rows = list_pending_proposals(crm)
        assert rows[0]["experiment_id"] == "exp-d"
        assert rows[0]["pr_url"] == "https://gh/pull/3"


class TestApproveProposal:
    def test_merges_pr_and_resolves_queue(self):
        # The find-pending read goes through ``crm`` (AttioProvider over the
        # same mock); the queue-resolve write stays on the raw ``attio``
        # client via the patched AttioWriter.
        crm, attio = _provider_returning([_queue_row("exp-1", "https://gh/pull/1")])
        writer = MagicMock()  # AttioWriter mock
        commands: list[list[str]] = []

        def fake_run(args, **_):
            commands.append(list(args))
            return _make_completed_proc()

        from unittest.mock import patch as _patch

        with _patch("workflows.sales_approve.AttioWriter", return_value=writer):
            result = approve_proposal(
                "exp-1", crm=crm, attio=attio, run_cmd=fake_run
            )

        assert result == {
            "experiment_id": "exp-1",
            "pr_url": "https://gh/pull/1",
            "merged": True,
            "queue_resolved": True,
        }
        # gh pr merge invoked with squash + delete-branch.
        assert commands == [
            ["gh", "pr", "merge", "https://gh/pull/1", "--squash", "--delete-branch"]
        ]
        # Queue row resolved via AttioWriter.
        writer.apply.assert_called_once()
        intent = writer.apply.call_args[0][0]
        assert intent.object == "operator_review_queue"
        assert intent.updates["status"] == "resolved"
        assert intent.writer_module == "workflows.sales_approve"

    def test_raises_when_no_pending_proposal(self):
        crm, attio = _provider_returning([])

        with pytest.raises(ProposalNotFoundError) as exc_info:
            approve_proposal(
                "nonexistent", crm=crm, attio=attio, run_cmd=MagicMock()
            )

        assert exc_info.value.experiment_id == "nonexistent"

    def test_propagates_gh_merge_failure(self):
        crm, attio = _provider_returning([_queue_row("exp-1", "https://gh/pull/1")])

        def fake_run(args, **_):
            raise subprocess.CalledProcessError(
                returncode=1, cmd=args, output="", stderr="merge conflict"
            )

        with pytest.raises(subprocess.CalledProcessError):
            approve_proposal("exp-1", crm=crm, attio=attio, run_cmd=fake_run)


class TestRejectProposal:
    def test_closes_pr_and_marks_queue_rejected(self):
        crm, attio = _provider_returning([_queue_row("exp-1", "https://gh/pull/1")])
        commands: list[list[str]] = []

        def fake_run(args, **_):
            commands.append(list(args))
            return _make_completed_proc()

        from unittest.mock import patch as _patch
        writer = MagicMock()
        with _patch("workflows.sales_approve.AttioWriter", return_value=writer):
            result = reject_proposal(
                "exp-1", crm=crm, attio=attio,
                rationale="reactance still too high",
                run_cmd=fake_run,
            )

        assert result["queue_rejected"] is True
        assert result["pr_closed"] is True
        # gh pr close called with --comment + --delete-branch.
        assert commands[0][0:4] == ["gh", "pr", "close", "https://gh/pull/1"]
        assert "--comment" in commands[0]
        # Queue row marked rejected.
        intent = writer.apply.call_args[0][0]
        assert intent.updates["status"] == "rejected"

    def test_empty_rationale_raises(self):
        crm, attio = _provider_returning([])
        with pytest.raises(ValueError, match="non-empty rationale"):
            reject_proposal(
                "exp-1", crm=crm, attio=attio, rationale="", run_cmd=MagicMock()
            )

        with pytest.raises(ValueError, match="non-empty rationale"):
            reject_proposal(
                "exp-1", crm=crm, attio=attio, rationale="   ", run_cmd=MagicMock()
            )


class TestWriterRegistryCompliance:
    EXPECTED_MODULE = "workflows.sales_approve"

    def test_status_writer_registered(self):
        from clients.attio_writer_registry import WRITE_OWNER_REGISTRY
        writers = WRITE_OWNER_REGISTRY[("operator_review_queue", "status")]
        assert isinstance(writers, list)
        assert self.EXPECTED_MODULE in writers

    def test_resolved_at_writer_registered(self):
        from clients.attio_writer_registry import WRITE_OWNER_REGISTRY
        writers = WRITE_OWNER_REGISTRY[("operator_review_queue", "resolved_at")]
        assert isinstance(writers, list)
        assert self.EXPECTED_MODULE in writers

    def test_decision_json_writer_registered(self):
        from clients.attio_writer_registry import WRITE_OWNER_REGISTRY
        writers = WRITE_OWNER_REGISTRY[("operator_review_queue", "decision_json")]
        assert isinstance(writers, list)
        assert self.EXPECTED_MODULE in writers
