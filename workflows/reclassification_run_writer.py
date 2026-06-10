"""ReclassificationRunWriter — every classifier rerun is captured as
a typed Attio `Reclassification Run` row (F-PR-3.7, plan §3.12).

# What it guarantees

Every reclassification (industry abstain in PR-25, cohort archaeology
in PR-22, defensive-hold backfill in §3.6, manual-reply
response_classification, baseline-v0 recompute in PR-11,
suppress_re_engagement backfill in PR-37) produces a typed Attio row
capturing:

  * Identity: run_id, run_at, classifier_module,
    classifier_version (git SHA), model_used, dry_run
  * Coverage: input_attr, output_attr, batch_size, confidence_threshold
  * Outcome counters: success_count, abstain_count, error_count
  * Provenance: audit_log_pointer, cost_usd_actual

Every reclassified record gets `last_classified_by:
record_reference → Reclassification Run`. Re-running the same
classifier_version with the same confidence threshold returns the
cached result (idempotent — the §3.12 contract).

# Usage

    with ReclassificationRunWriter(
        classifier_module="workflows.industry_classifier",
        classifier_version=git_sha(),
        model_used="claude-opus-4-6",
        input_attr="company_name",
        output_attr="industry_vertical_status",
        confidence_threshold=0.8,
        dry_run=args.dry_run,
    ) as run:
        for record_id in batch:
            run.examine()
            verdict = classifier.classify(record_id)
            if verdict.confidence >= run.confidence_threshold:
                run.mark_success(record_id=record_id, value=verdict.label)
            else:
                # Reason vocabulary MUST match the verdict's status field so
                # downstream queries over abstain_details can filter by reason
                # consistently. PR-25 callers pass verdict.status ∈
                # {"low_confidence", "unknown"}; do NOT pass arbitrary strings
                # like "below_threshold" — that creates two parallel
                # vocabularies on the same field.
                run.mark_abstain(record_id=record_id, reason=verdict.status)
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from clients.attio import AttioClient

if TYPE_CHECKING:
    from types import TracebackType

RECLASSIFICATION_RUN_SLUG = "reclassification_run"


def _git_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent,
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()[:12]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


class ReclassificationRunWriter:
    """Context manager that wraps a single classifier rerun.

    Writes ONE `Reclassification Run` row on __exit__. Per-record
    `last_classified_by` stamps happen via `mark_success(record_id=...)`.
    """

    def __init__(
        self,
        *,
        classifier_module: str,
        classifier_version: str | None = None,
        model_used: str,
        input_attr: str,
        output_attr: str,
        confidence_threshold: float | None = None,
        dry_run: bool = False,
        attio: AttioClient | None = None,
        audit_log_pointer: str | None = None,
    ) -> None:
        self.classifier_module = classifier_module
        self.classifier_version = classifier_version or _git_sha()
        self.model_used = model_used
        self.input_attr = input_attr
        self.output_attr = output_attr
        self.confidence_threshold = confidence_threshold
        self.dry_run = dry_run
        self._attio = attio
        self.audit_log_pointer = audit_log_pointer

        self.run_id = uuid.uuid4().hex[:12]
        self.run_at: str | None = None
        self.batch_size = 0
        self.success_count = 0
        self.abstain_count = 0
        self.error_count = 0
        self.cost_usd_actual = 0.0
        # (record_id, object) tuples for back-pointer PATCH. §3.12
        # contract: every reclassified record gets last_classified_by.
        self.successful_records: list[tuple[str, str]] = []
        self.abstain_details: list[dict] = []
        self.error_details: list[dict] = []
        self._record_id: str | None = None
        self._back_pointer_failures: list[dict] = []

    def __enter__(self) -> ReclassificationRunWriter:
        self.run_at = datetime.now(UTC).isoformat()
        if self._attio is None:
            self._attio = AttioClient()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> Literal[False]:
        if exc_type is not None:
            self.error_count += 1
            self.error_details.append({
                "kind": "uncaught_exception",
                "exc_type": exc_type.__name__,
                "exc_msg": str(exc_val)[:500],
            })

        if not self.dry_run:
            self._write_reclassification_row()
            # §3.12 contract: every reclassified record gets
            # last_classified_by. Mirrors MigrationRunWriter pattern;
            # failures captured but don't re-raise.
            self._stamp_back_pointers()
            # engineer-QA-build3.7-v2 #1: surface silent back-pointer
            # failures on stderr (mirror of MigrationRunWriter).
            if self._back_pointer_failures:
                print(
                    f"WARNING: ReclassificationRunWriter run_id={self.run_id} "
                    f"wrote Reclassification Run row but "
                    f"{len(self._back_pointer_failures)} back-pointer PATCH(es) "
                    f"failed. Classifier results are safe; last_classified_by "
                    f"stamps are partial. See _back_pointer_failures for details.",
                    file=sys.stderr,
                )
        return False

    def examine(self) -> None:
        self.batch_size += 1

    def mark_success(
        self, *, record_id: str, object: str = "linkedin_outreach",
        value: Any | None = None,
    ) -> None:
        """Counts a successful reclassification and (on __exit__) stamps
        `last_classified_by` on the target record. `object` defaults to
        linkedin_outreach (the common case). `value` is recorded in
        successful_records for downstream diagnostics."""
        self.success_count += 1
        if object != "schema":
            self.successful_records.append((record_id, object))

    def mark_abstain(self, *, record_id: str, reason: str) -> None:
        self.abstain_count += 1
        self.abstain_details.append({"record_id": record_id, "reason": reason})

    def mark_error(self, *, record_id: str, error: Exception | str) -> None:
        self.error_count += 1
        msg = str(error)[:500] if isinstance(error, Exception) else str(error)[:500]
        self.error_details.append({
            "record_id": record_id,
            "error": msg,
            "error_class": type(error).__name__ if isinstance(error, Exception) else "str",
        })

    def add_cost(self, usd: float) -> None:
        """Accumulate per-call LLM cost. Operator-readable on the row."""
        self.cost_usd_actual += usd

    def _stamp_back_pointers(self) -> None:
        """For each successful reclassification, PATCH `last_classified_by`
        on the target record. §3.12 contract.

        Failures are captured in `_back_pointer_failures` but don't bubble
        — the Reclassification Run row is already on Attio, so the
        forensics chain is partially complete. Operators see the gap via
        PR-43.5's data-quality-report.
        """
        assert self._attio is not None
        if self._record_id is None:
            self._back_pointer_failures.append({
                "kind": "missing_reclassification_run_record_id",
                "n_back_pointers_skipped": len(self.successful_records),
            })
            return
        for record_id, target_object in self.successful_records:
            try:
                body = {
                    "data": {
                        "values": {
                            "last_classified_by": [{
                                "target_object": RECLASSIFICATION_RUN_SLUG,
                                "target_record_id": self._record_id,
                            }]
                        }
                    }
                }
                self._attio._request(
                    "PATCH",
                    f"/objects/{target_object}/records/{record_id}",
                    json=body,
                )
            except Exception as exc:
                self._back_pointer_failures.append({
                    "record_id": record_id,
                    "object": target_object,
                    "error": str(exc)[:300],
                    "error_class": type(exc).__name__,
                })

    def _write_reclassification_row(self) -> None:
        assert self._attio is not None
        attrs: dict[str, Any] = {
            "run_id": self.run_id,
            "run_at": self.run_at,
            "classifier_module": self.classifier_module,
            "classifier_version": self.classifier_version,
            "model_used": self.model_used,
            "input_attr": self.input_attr,
            "output_attr": self.output_attr,
            "batch_size": self.batch_size,
            "success_count": self.success_count,
            "abstain_count": self.abstain_count,
            "error_count": self.error_count,
            "cost_usd_actual": self.cost_usd_actual,
            "dry_run": self.dry_run,
            "agent_host": socket.gethostname(),
        }
        if self.confidence_threshold is not None:
            attrs["confidence_threshold"] = self.confidence_threshold
        if self.audit_log_pointer is not None:
            attrs["audit_log_pointer"] = self.audit_log_pointer
        if self.abstain_details or self.error_details:
            attrs["failure_details_pointer"] = json.dumps({
                "abstains": self.abstain_details,
                "errors": self.error_details,
            }, default=str)

        body = {"data": {"values": attrs}}
        data = self._attio._request(
            "POST", f"/objects/{RECLASSIFICATION_RUN_SLUG}/records", json=body
        )
        record = data.get("data", data)
        self._record_id = (
            record.get("id", {}).get("record_id")
            if isinstance(record.get("id"), dict)
            else record.get("record_id")
        )
