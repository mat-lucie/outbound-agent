"""CI guard: every NEW scripts/migrate_*.py and scripts/backfill_*.py
must use `MigrationRunWriter` (F-PR-3.7, plan §3.13).

The guard scans `scripts/` for the two filename patterns and asserts
each script either:

  1. Uses `workflows.migration_run_writer.MigrationRunWriter` (the
     happy path — every new migration goes through this writer); or
  2. Is in `EXEMPT_SCRIPTS` (the 3 documented exemptions per the §3.13
     bootstrap carve-out); or
  3. Is in `LEGACY_SCRIPTS` (pre-plan scripts grandfathered until the
     wave-1 PR that touches them migrates each to MigrationRunWriter).

Any new script that fits neither bucket fails the guard, forcing the
author to either use the writer or explicitly join one of the two
escape lists with a justification.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Exempt per plan §3.13 — read-only or bootstrap.
EXEMPT_SCRIPTS: set[str] = {
    # F-PR-3 bootstrap (created the Operator Review Queue object before
    # Migration Run existed). Retroactively backfilled by
    # scripts/backfill_F1_bootstrap_to_migration_run.py.
    "scripts/migrate_attio_F1_operator_review_queue.py",
    # F-PR-3.5 validator: read-only, no Attio mutations.
    "scripts/validate_attio_schema_deltas.py",
    # PR-43.5: read-only with default --report-only flag. If
    # --auto-remediate is added in the future, the script must opt
    # back into MigrationRunWriter.
    "scripts/data_quality_report.py",
    # F-PR-3.7 itself: creates Migration Run object. Uses
    # MigrationRunWriter in phase 2 but phase 1 (object create) is
    # pre-writer. The compliance test inspects code text — this script
    # imports MigrationRunWriter, so the grep-based check below passes.
    # Kept in EXEMPT_SCRIPTS so the intent is documented.
    "scripts/migrate_attio_F2_reclassification_migration_objects.py",
    # F-PR-3.7's retroactive backfill: writes Migration Run rows
    # DIRECTLY from F-PR-3's bootstrap JSONL (one-shot). Wrapping it in
    # MigrationRunWriter would create a recursive logging situation —
    # the backfill that creates Migration Run rows would itself need a
    # Migration Run row that logs the backfill. Exempt by design.
    "scripts/backfill_F1_bootstrap_to_migration_run.py",
    # 2026-05-31 persona reclassify backfill: uses ReclassificationRunWriter
    # (the typed subclass for classifier reruns, per F-PR-3.7 plan §3.12)
    # instead of MigrationRunWriter. Provenance is captured via the
    # Reclassification Run Attio object, not Migration Run.
    "scripts/backfill_persona_reclassify.py",
}

# Legacy scripts grandfathered. Each must be removed from this list by
# the wave-1 PR that migrates it to MigrationRunWriter. New scripts may
# NOT be added here — the guard explicitly does not protect them.
LEGACY_SCRIPTS: set[str] = {
    # Pre-plan: predates the writer contract.
    "scripts/backfill_baseline_per_step.py",
    "scripts/migrate_attio_schema.py",
}


# Line-anchored regexes for the two patterns that REALLY indicate
# MigrationRunWriter usage (vs a comment mentioning the class). One of:
#   - `from workflows.migration_run_writer import MigrationRunWriter`
#   - `import workflows.migration_run_writer` followed by ...
#   - `MigrationRunWriter(` somewhere in code (call or class use)
_IMPORT_RE = re.compile(
    r"^\s*from\s+workflows\.migration_run_writer\s+import\s+.*\bMigrationRunWriter\b",
    re.MULTILINE,
)
_CALL_RE = re.compile(r"\bMigrationRunWriter\s*\(", re.MULTILINE)


def _scan_for_migration_writer(path: Path) -> bool:
    """Return True iff the file genuinely uses MigrationRunWriter — an
    import statement OR a call site. A comment mentioning the class no
    longer counts (engineer-QA-build3.7 #2)."""
    text = path.read_text(encoding="utf-8")
    return bool(_IMPORT_RE.search(text) or _CALL_RE.search(text))


def _migration_script_paths() -> list[Path]:
    scripts_dir = REPO_ROOT / "scripts"
    if not scripts_dir.exists():
        return []
    return sorted(
        list(scripts_dir.glob("migrate_*.py"))
        + list(scripts_dir.glob("backfill_*.py"))
    )


@pytest.mark.parametrize("path", _migration_script_paths(), ids=lambda p: p.name)
def test_migration_script_uses_writer_or_is_exempted(path: Path) -> None:
    rel = path.relative_to(REPO_ROOT).as_posix()
    if rel in EXEMPT_SCRIPTS:
        return  # documented exemption
    if rel in LEGACY_SCRIPTS:
        return  # grandfathered; remove from LEGACY_SCRIPTS when migrated
    assert _scan_for_migration_writer(path), (
        f"{rel} is a migrate_*.py / backfill_*.py script that does NOT "
        f"use MigrationRunWriter and is NOT in EXEMPT_SCRIPTS or "
        f"LEGACY_SCRIPTS. Either:\n"
        f"  1. Use `with MigrationRunWriter(...)` for the mutations.\n"
        f"  2. Add to LEGACY_SCRIPTS in tests/test_migration_writer_compliance.py\n"
        f"     with a comment naming the PR that will migrate it.\n"
        f"  3. Add to EXEMPT_SCRIPTS if read-only or a bootstrap."
    )


def test_legacy_list_stays_bounded():
    """LEGACY_SCRIPTS exists to phase out scripts that predate the writer
    contract. New entries must not be added — each downstream PR that
    touches a legacy script should REMOVE it from LEGACY_SCRIPTS in the
    same diff. This test pins the current set so a `+=` slip is caught."""
    expected = {
        "scripts/backfill_baseline_per_step.py",
        "scripts/migrate_attio_schema.py",
    }
    assert expected == LEGACY_SCRIPTS, (
        f"LEGACY_SCRIPTS changed unexpectedly. Diff: "
        f"added={LEGACY_SCRIPTS - expected}, removed={expected - LEGACY_SCRIPTS}"
    )


def test_exempt_list_stays_bounded():
    """EXEMPT_SCRIPTS must be small and explicit per plan §3.13. Any
    change requires a plan-level justification, so the test pins the
    current set."""
    expected = {
        "scripts/migrate_attio_F1_operator_review_queue.py",
        "scripts/validate_attio_schema_deltas.py",
        "scripts/data_quality_report.py",
        "scripts/migrate_attio_F2_reclassification_migration_objects.py",
        "scripts/backfill_F1_bootstrap_to_migration_run.py",
        # 2026-05-31 persona reclassify: uses ReclassificationRunWriter
        # (typed classifier-rerun writer) per F-PR-3.7 plan §3.12.
        "scripts/backfill_persona_reclassify.py",
    }
    assert expected == EXEMPT_SCRIPTS
