"""PR-40 CI guard — no production module may read
``content/association_outreach.json`` after migration to Attio.

The migration script renames the JSON to ``association_outreach.json.deprecated``
on a successful live run. Any code path still referencing the old
filename would silently fall through to "file not found" or, worse, read
a partially-deprecated state. This guard greps the repo to forbid the
filename in production code (the migration script itself is the sole
allowed reference, and this test as the canary).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
# Word-boundary match: the trailing negative-lookahead ensures we do
# NOT trip on `.json.deprecated`, `.json.bak`, or `.jsonl` — those are
# legitimate transition artifacts and an unrelated jsonl path.
FORBIDDEN_TOKEN = "association_outreach.json"
FORBIDDEN_PATTERN = re.compile(r"association_outreach\.json(?![a-zA-Z0-9._-])")

# Modules permitted to mention the JSON filename. The migration script
# owns the deprecation; the legacy workflows.association_outreach module
# may still reference it during the transitional window (it's emitting
# its own deprecation warning per the PR-40 plan).
ALLOWED_PATHS: frozenset[Path] = frozenset({
    REPO_ROOT / "scripts" / "migrate_association_outreach_to_attio.py",
    # `workflows/association_outreach.py` will be flipped to read from
    # Attio in a follow-up PR; until then it can still reference the
    # JSON path (it is the file being migrated AWAY from).
    REPO_ROOT / "workflows" / "association_outreach.py",
    # This guard test itself necessarily contains the forbidden string.
    Path(__file__).resolve(),
    # The PR-40 migration test exercises rename semantics on the
    # deprecated filename — necessarily mentions the token.
    REPO_ROOT / "tests" / "test_pr40_migrate_association_outreach.py",
})


def _iter_python_files() -> list[Path]:
    """Walk the repo for .py files outside vendored / cache directories."""
    skip_parts = {"__pycache__", ".git", "node_modules", ".venv", ".pytest_cache"}
    skip_top_level = {".claude", "docs", "exports", "directories", "research"}
    out: list[Path] = []
    for path in REPO_ROOT.rglob("*.py"):
        rel_parts = path.relative_to(REPO_ROOT).parts
        if any(p in skip_parts for p in rel_parts):
            continue
        if rel_parts and rel_parts[0] in skip_top_level:
            continue
        out.append(path)
    return out


def test_no_production_code_reads_deprecated_json():
    """Forbid `association_outreach.json` references outside the allowlist."""
    offenders: list[tuple[Path, int, str]] = []
    for path in _iter_python_files():
        if path in ALLOWED_PATHS:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not FORBIDDEN_PATTERN.search(text):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if FORBIDDEN_PATTERN.search(line):
                offenders.append((path.relative_to(REPO_ROOT), lineno, line.strip()))

    if offenders:
        formatted = "\n".join(
            f"  {p}:{n}  {line}" for p, n, line in offenders
        )
        pytest.fail(
            f"PR-40 CI guard: {len(offenders)} reference(s) to "
            f"{FORBIDDEN_TOKEN!r} outside the allowlist. The JSON is "
            "deprecated; read outreach_channel from Attio instead.\n"
            f"{formatted}"
        )


def test_allowlist_paths_actually_exist():
    """Sanity: the allowlist entries must point at real files (drift guard)."""
    missing = [p for p in ALLOWED_PATHS if not p.exists()]
    if missing:
        formatted = "\n".join(f"  {p.relative_to(REPO_ROOT)}" for p in missing)
        pytest.fail(
            f"Allowlist references {len(missing)} non-existent file(s); "
            f"prune ALLOWED_PATHS or fix the path:\n{formatted}"
        )
