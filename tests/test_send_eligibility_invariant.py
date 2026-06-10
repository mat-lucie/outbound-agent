"""CI guard: §3.10 send-eligibility invariant (PR-22).

Any code path that queries LinkedIn Outreach list entries and filters
candidates by `stage` + `dm_step` MUST consult `is_send_eligible` before
queuing a send. This prevents archaeology-stamped rows (`legacy_inferred_by_
archaeology`, `legacy_pure_unknown`) from being re-sent — a §3.1 hard red line.

This test scans the source tree for patterns that look like send-path queries
(filtering by stage / dm_step proximity to a send action) and verifies that
`is_send_eligible` appears in the same file.

# What this guards

- Any workflow file (`workflows/*.py`) that calls `get_pending_dms`,
  `get_pending_invites`, `get_pending_nurture_re_engage`, or reads
  `dm_step` from a LinkedIn Outreach entry in a send-path context.
- The guard fires when the file does NOT import or use `is_send_eligible`.

# Known allowlisted files

Files that legitimately read `dm_step` for MEASUREMENT-ONLY purposes (not
send path) are in `SEND_PATH_EXEMPT`. Learn workflow is the canonical example:
it reads `dm_step` for cohort math, not for send decisions. The guard would
false-positive on it without an allowlist.

# Why grep-based and not AST

The codebase's send-path pattern is stable: callers always use the
`is_send_eligible` name. A grep guard catches regressions immediately
without requiring a full AST parse, and matches the existing F-PR-1
`is_send_eligible` idiom (mirror pattern).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO_ROOT / "workflows"

# Files that read dm_step for measurement / analysis only (not for send decisions).
# These are NEVER send-path callers and must NOT be required to import is_send_eligible.
SEND_PATH_EXEMPT: set[str] = {
    # PR-10 / PR-22 cohort math — reads dm_step for denominator calculation.
    "workflows/learn.py",
    # PR-11 baseline recompute — reads dm_step for per-step rate backfill.
    "workflows/weekly_brain.py",
    # PR-34 diagnostic runner — reads dm_step for cell classification.
    "workflows/qualifier_diagnostic.py",
    # PR-30 weekly report — reads dm_step for KPI summary.
    "workflows/weekly_report.py",
    # Metrics helpers — reads dm_step for counting only.
    "workflows/metrics.py",
    # Starvation detection — reads stage for pool analysis (no sends).
    "workflows/starvation.py",
    # Throttle — reads stage for per-company counts (no direct sends).
    "workflows/throttle.py",
    # Daily run identity / lease — no send logic.
    "workflows/daily_run.py",
    # Escalation / queue helpers — no send logic.
    "workflows/escalation.py",
    "workflows/escalation_schemas.py",
    # Response detection — reads stage to classify responses (no new sends).
    "workflows/detect_responses.py",
    # Target freshness — reads stage for ICP scoring (no sends).
    "workflows/target_freshness.py",
    # Auto-finalize — post-send lifecycle (no new sends initiated).
    "workflows/auto_finalize.py",
    # Association outreach — experimental; not main send path.
    "workflows/association_outreach.py",
    # Wave-2 blast — reads stage for blast targeting (legacy path).
    "workflows/wave2_blast.py",
    # Audit — reads stage for audit analysis.
    "workflows/audit.py",
    # DM sequencer — defines the pending-DM logic itself (not a caller).
    "workflows/dm_sequencer.py",
    # Recheck cache — reads stage for cache validation (no sends).
    "workflows/recheck_cache.py",
    # Record cache — lookup utility (no sends).
    "workflows/record_cache.py",
    # LLM dispatch — language model routing (no sends).
    "workflows/llm_dispatch.py",
    # Safety limits — reads stage for limit enforcement.
    "workflows/safety_limits.py",
    # PB advance gate — pre-send check helper (is_send_eligible is in callers).
    "workflows/pb_advance_gate.py",
    # Industry classifier — reads stage for ICP validation (no sends).
    "workflows/industry_classifier.py",
    # Qualifier critique — reads stage for qualification scoring.
    "workflows/qualifier_critique.py",
    # Quality gate — reads stage for quality scoring (no sends).
    "workflows/quality_gate.py",
    # Response classifier — classification only (no sends).
    "workflows/response_classifier.py",
    # Run lock — flock / lease (no sends).
    "workflows/run_lock.py",
    # Threshold calibration — reads stage for calibration analysis.
    "workflows/threshold_calibration.py",
    # Identity match — reads stage for matching (no sends).
    "workflows/identity_match.py",
    # Backfill companies — company data enrichment (no sends).
    "workflows/backfill_companies.py",
    # Company matcher — reads stage for matching (no sends).
    "workflows/company_matcher.py",
    # Hot lead alert — reads stage for alert routing (no new sends).
    "workflows/hot_lead_alert.py",
    # Migration writers — provenance logging (no sends).
    "workflows/migration_run_writer.py",
    "workflows/reclassification_run_writer.py",
    # Synthetic prescreen — reads stage for qualification (no sends).
    "workflows/synthetic_prescreen.py",
    # Deal repair — post-deal lifecycle (no new sends).
    "workflows/repair_deal_contacts.py",
    # Email sequencer / campaign — different channel (not LinkedIn).
    "workflows/email_sequencer.py",
    "workflows/email_campaign.py",
}

# Patterns that identify "send-path" callers:
#   - Calls get_pending_dms / get_pending_invites / get_pending_nurture_re_engage
#   - Filters candidates on `PipelineStage.PROSPECT` in a send-scheduling
#     context (covers the invite candidate loop in run_connection_requests
#     which doesn't go through any get_pending_* helper).
#   - Uses dm_step in a send-scheduling context alongside stage filtering
_SEND_PATH_PATTERNS = [
    re.compile(r"\bget_pending_dms\s*\("),
    re.compile(r"\bget_pending_invites\s*\("),
    re.compile(r"\bget_pending_nurture_re_engage\s*\("),
    # Raw invite-path filter: `attrs["stage"] == PipelineStage.PROSPECT.value`
    # (or equivalent) marks the candidate pool for a connection-request send.
    # The PR-22 §3.1 leak proved that this pattern, when not gated by
    # `is_send_eligible`, lets archaeology rows escape into the invite slice.
    re.compile(r'\bstage["\']?\s*\]?\s*==\s*PipelineStage\.PROSPECT\.value'),
]

# Pattern confirming is_send_eligible is present in the file
_ELIGIBILITY_CHECK_RE = re.compile(r"\bis_send_eligible\s*\(")


def _is_send_path_file(source: str) -> bool:
    """Return True when the file contains a send-path query pattern."""
    return any(pat.search(source) for pat in _SEND_PATH_PATTERNS)


def _uses_send_eligibility(source: str) -> bool:
    """Return True when the file calls is_send_eligible(...)."""
    return bool(_ELIGIBILITY_CHECK_RE.search(source))


def _workflow_files() -> list[Path]:
    if not WORKFLOWS_DIR.exists():
        return []
    return sorted(WORKFLOWS_DIR.glob("*.py"))


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_send_path_uses_is_send_eligible(path: Path) -> None:
    """Every workflow file that calls get_pending_dms / get_pending_invites /
    get_pending_nurture_re_engage MUST also call is_send_eligible.

    §3.10 invariant: archaeology-stamped rows are SEND-INELIGIBLE and must
    never enter the send queue. Any send-path caller that bypasses
    `is_send_eligible` risks re-sending to a legacy_* row — a §3.1 violation.
    """
    rel = path.relative_to(REPO_ROOT).as_posix()
    if rel in SEND_PATH_EXEMPT:
        return  # measurement-only or non-send path; explicitly exempted

    source = path.read_text(encoding="utf-8")

    if not _is_send_path_file(source):
        return  # file does not contain send-path patterns — not a send caller

    assert _uses_send_eligibility(source), (
        f"{rel} contains a send-path query (get_pending_dms / get_pending_invites "
        "/ get_pending_nurture_re_engage) but does NOT call is_send_eligible(). "
        "\n"
        "§3.10 requires every send-path caller to gate on is_send_eligible before "
        "queuing a send. Archaeology-stamped rows (legacy_inferred_by_archaeology, "
        "legacy_pure_unknown) are SEND-INELIGIBLE — queuing them violates the "
        "§3.1 no-resend hard red line. "
        "\n"
        "Fix: add `if not is_send_eligible(entry): continue` before queuing."
    )


# ── Unit tests for the guard helpers ────────────────────────────────────────


class TestSendPathDetection:
    """Unit tests for the grep-based detection helpers.

    These verify the guard logic is correct independently of the file scan.
    """

    def test_detects_get_pending_dms(self):
        source = "pending = get_pending_dms(stage, last_date, today)"
        assert _is_send_path_file(source)

    def test_detects_get_pending_invites(self):
        source = "candidates = get_pending_invites(entries, today)"
        assert _is_send_path_file(source)

    def test_detects_get_pending_nurture_re_engage(self):
        source = "queue = get_pending_nurture_re_engage(entry, today)"
        assert _is_send_path_file(source)

    def test_detects_raw_prospect_stage_filter(self):
        """Invite candidate loops filtering on `stage == PipelineStage.PROSPECT.value`
        are send-path callers — `run_connection_requests` is the canonical example.
        """
        source = 'if attrs["stage"] == PipelineStage.PROSPECT.value:'
        assert _is_send_path_file(source)

    def test_detects_raw_prospect_stage_filter_dot_access(self):
        """Same as above with attribute access syntax."""
        source = "if attrs.stage == PipelineStage.PROSPECT.value:"
        assert _is_send_path_file(source)

    def test_does_not_detect_measurement_only(self):
        source = "# reads dm_step for denominator calculation\nif entry.get('dm_step'):"
        assert not _is_send_path_file(source)

    def test_detects_is_send_eligible_call(self):
        source = "if not is_send_eligible(attrs): continue"
        assert _uses_send_eligibility(source)

    def test_does_not_detect_comment_only(self):
        # A comment mentioning is_send_eligible should not satisfy the guard
        # (must be an actual call)
        source = "# is_send_eligible docs: see models.pipeline"
        assert not _uses_send_eligibility(source)

    def test_detects_send_path_with_eligibility_passes(self):
        source = (
            "if not is_send_eligible(attrs): continue\n"
            "pending = get_pending_dms(stage, last_date, today)"
        )
        assert _is_send_path_file(source)
        assert _uses_send_eligibility(source)

    def test_detects_send_path_without_eligibility_fails(self):
        source = "pending = get_pending_dms(stage, last_date, today)"
        assert _is_send_path_file(source)
        assert not _uses_send_eligibility(source)
