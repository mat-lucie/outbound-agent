"""PR-32 — sales-approve workflow.

Polls the Operator Review Queue for ``variant_proposal_pending`` rows
emitted by ``workflows.weekly_brain.run_weekly_brain``, and lets the
operator approve (``gh pr merge --squash``) or reject (close queue row
with rationale) each pending proposal.

Per plan §5 PR-32 + Round-4 D8: every git/gh side effect goes through
``subprocess.run`` rather than the ``mcp__github__*`` MCP, so this module
runs anywhere the gh CLI is configured (no MCP dependency at runtime).

All Attio writes go through ``AttioWriter.apply(WriteIntent)`` so the
§3.15 write-owner registry authorizes this module (PR-32 registers
``workflows.sales_approve`` as a co-writer of ``operator_review_queue.status``
and ``operator_review_queue.resolved_at``).
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from clients.attio_writer import AttioWriter, WriteIntent

if TYPE_CHECKING:
    from collections.abc import Callable

    from clients.attio import AttioClient
    from clients.crm.base import CRMProvider

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WRITER_MODULE = "workflows.sales_approve"
QUEUE_TYPE = "variant_proposal_pending"


def _run_subprocess(args: list[str]) -> subprocess.CompletedProcess:
    """Subprocess.run wrapper (PR-32). Always check=True so gh failures
    bubble up with stderr — §0 #9. Single seam to monkeypatch in tests."""
    return subprocess.run(
        args,
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def list_pending_proposals(crm: CRMProvider) -> list[dict]:
    """Return queue rows of type ``variant_proposal_pending`` whose
    ``status`` is not yet ``resolved``/``rejected``.

    Each row dict has at minimum ``experiment_id``, ``pr_url``,
    ``branch_name``, plus the Attio record_id under ``queue_record_id``
    so callers can resolve it later.

    Raises any httpx error from Attio — §0 #9, no silent empty-list fallback.
    """
    records = crm.query_object_records(
        "operator_review_queue",
        filters={
            "$and": [
                {"type": QUEUE_TYPE},
                {"status": {"$not": {"$in": ["resolved", "rejected"]}}},
            ],
        },
        limit=100,
    )
    proposals: list[dict] = []
    for record in records:
        # ``record.raw`` is the untouched Attio object-record payload
        # (``{"id": {...}, "values": {...}}``) — the same shape the raw
        # ``_request`` query returned per row. Read the array-wrapped
        # ``payload_json`` slug off it exactly as before; the flat
        # ``record.attributes`` map would have first-value-unwrapped it.
        row = record.raw
        values = row.get("values", {}) or {}
        # Attio api_slug is ``payload_json`` (long_text) per
        # ``scripts/migrate_attio_F1_operator_review_queue.py``.
        payload_arr = values.get("payload_json", [])
        payload: dict = {}
        if payload_arr and isinstance(payload_arr, list):
            first = payload_arr[0]
            if isinstance(first, dict):
                payload = first.get("value") or {}
                if isinstance(payload, str):
                    # Attio long_text payloads may arrive serialized.
                    import json
                    try:
                        payload = json.loads(payload)
                    except json.JSONDecodeError:
                        payload = {}
        proposals.append({
            "queue_record_id": row.get("id", {}).get("record_id", ""),
            "experiment_id": payload.get("experiment_id", ""),
            "pr_url": payload.get("pr_url", ""),
            "branch_name": payload.get("branch_name", ""),
            "proposal_markdown_path": payload.get("proposal_markdown_path", ""),
            "proposal_json_path": payload.get("proposal_json_path", ""),
        })
    return proposals


class ProposalNotFoundError(RuntimeError):
    """Raised when ``approve_proposal``/``reject_proposal`` cannot locate a
    pending queue row for the given experiment_id."""

    def __init__(self, experiment_id: str) -> None:
        self.experiment_id = experiment_id
        super().__init__(
            f"No pending variant_proposal_pending queue row for "
            f"experiment_id={experiment_id!r}. Has the brain run this week?"
        )


def _find_proposal_or_raise(crm: CRMProvider, experiment_id: str) -> dict:
    for prop in list_pending_proposals(crm):
        if prop["experiment_id"] == experiment_id:
            return prop
    raise ProposalNotFoundError(experiment_id)


def _resolve_queue_row(
    attio: AttioClient,
    queue_record_id: str,
    *,
    status: str,
    rationale: str,
    writer: AttioWriter | None = None,
) -> None:
    """Resolve the queue row via AttioWriter (registry-enforced per §3.15).

    Writes ``status`` + ``resolved_at`` + ``decision_json``. The
    ``workflows.sales_approve`` writer is registered as a co-writer of
    each of these slugs in ``clients/attio_writer_registry.py``."""
    if writer is None:
        writer = AttioWriter(attio=attio)

    now_iso = datetime.now(UTC).isoformat()
    writer.apply(WriteIntent(
        object="operator_review_queue",
        record_id=queue_record_id,
        updates={
            "status": status,
            "resolved_at": now_iso,
            "decision_json": {
                "rationale": rationale,
                "resolved_by": WRITER_MODULE,
            },
        },
        writer_module=WRITER_MODULE,
    ))


def approve_proposal(
    experiment_id: str,
    *,
    crm: CRMProvider,
    attio: AttioClient,
    run_cmd: Callable[..., subprocess.CompletedProcess] = _run_subprocess,
) -> dict:
    """Approve a pending proposal: merge the PR (squash + delete branch),
    then mark the queue row resolved.

    The pending-row lookup reads through the vendor-neutral ``crm``
    (``CRMProvider``); the queue-row resolution still writes through the
    raw ``attio`` client via ``AttioWriter`` (the write path is deferred to
    a later CRM-migration increment).

    Returns: dict with ``experiment_id``, ``pr_url``, ``merged`` (bool),
    ``queue_resolved`` (bool).

    Raises:
        ProposalNotFoundError: no matching pending row.
        subprocess.CalledProcessError: gh pr merge failure — §0 #9.

    Partial-completion note (PR-32 fold from silent-fail QA): if ``gh pr
    merge`` succeeds but the subsequent ``_resolve_queue_row`` fails (e.g.
    Attio 5xx), the PR is merged on GitHub but the queue row still shows
    ``status="open"``. Re-running ``sales-approve`` will hit ``gh pr merge``
    on an already-merged PR and abort, leaving the queue row stuck. The
    operator should manually resolve the queue row via the Attio UI in this
    rare case. The reverse ordering (resolve first, merge second) trades
    "queue stale" for "PR unmerged" — the former is preferable because the
    merge is the irreversible side effect.
    """
    proposal = _find_proposal_or_raise(crm, experiment_id)
    pr_url = proposal["pr_url"]
    if not pr_url:
        raise RuntimeError(
            f"queue row for {experiment_id!r} has no pr_url; cannot merge."
        )

    run_cmd([
        "gh", "pr", "merge", pr_url, "--squash", "--delete-branch",
    ])

    _resolve_queue_row(
        attio,
        proposal["queue_record_id"],
        status="resolved",
        rationale=f"Approved + merged via sales-approve ({pr_url})",
    )

    return {
        "experiment_id": experiment_id,
        "pr_url": pr_url,
        "merged": True,
        "queue_resolved": True,
    }


def reject_proposal(
    experiment_id: str,
    *,
    crm: CRMProvider,
    attio: AttioClient,
    rationale: str,
    run_cmd: Callable[..., subprocess.CompletedProcess] = _run_subprocess,
) -> dict:
    """Reject a pending proposal: close the PR (gh pr close) and mark the
    queue row rejected with the operator's rationale.

    Like ``approve_proposal``, the pending-row lookup reads through the
    vendor-neutral ``crm``; the queue-row resolution still writes through
    the raw ``attio`` client via ``AttioWriter``.

    Returns: dict with ``experiment_id``, ``pr_url``, ``pr_closed`` (bool),
    ``queue_rejected`` (bool).

    Raises:
        ProposalNotFoundError: no matching pending row.
        subprocess.CalledProcessError: gh pr close failure — §0 #9.
        ValueError: empty rationale (operator must provide one for audit).

    Partial-completion note: same as ``approve_proposal``. If the gh-close
    succeeds but the queue-resolve fails, the PR is closed on GitHub but
    the queue row still shows open. Re-run will hit gh-close on a
    closed PR and abort. Operator should resolve via Attio UI; the closed
    PR is the irreversible side effect.
    """
    if not rationale or not rationale.strip():
        raise ValueError(
            "reject_proposal requires a non-empty rationale for audit."
        )

    proposal = _find_proposal_or_raise(crm, experiment_id)
    pr_url = proposal["pr_url"]
    if pr_url:
        run_cmd([
            "gh", "pr", "close", pr_url,
            "--comment", f"Rejected via sales-approve: {rationale}",
            "--delete-branch",
        ])

    _resolve_queue_row(
        attio,
        proposal["queue_record_id"],
        status="rejected",
        rationale=rationale,
    )

    return {
        "experiment_id": experiment_id,
        "pr_url": pr_url,
        "pr_closed": bool(pr_url),
        "queue_rejected": True,
    }
