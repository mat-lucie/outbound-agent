"""CI guard: every invite-issuing call site must call `is_invite_eligible`.

Two complementary checks:

1. `is_invite_eligible` must be imported AND called from both of the
   guarded files. Both files are §3.1 defense layers — if a refactor
   removes the call, that's exactly the silent regression this guard
   exists to catch.
2. No other module under workflows/ or models/ may define a private
   `_is_invite_eligible` shim — the canonical definition lives in
   `models.pipeline` and callers must import it from there.

The guard is intentionally textual: `grep`-style detection is cheap and
catches the failure mode (someone removed the call) better than an
import-graph analysis would. Parallels the F-PR-1 `is_send_eligible`
coverage in `tests/test_pipeline.py`, but introduces the grep-guard
pattern rather than mirroring it (no equivalent file existed).
"""

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# (file relative to repo root, gate symbol the file must import from
#  models.pipeline and call, the §3.1 reason this file must gate)
#
# daily_check's gate is `invite_slice_reason` (PR-217) — the consolidated
# invite predicate chain, which itself calls `is_invite_eligible` (asserted
# by `test_invite_slice_reason_calls_quarantine_gate` below), so the §3.1
# quarantine coverage is transitive.
GUARDED_INVITE_CALL_SITES: tuple[tuple[str, str, str], ...] = (
    (
        "workflows/daily_check.py",
        "invite_slice_reason",
        "run_connection_requests is the production invite slice — "
        "every PROSPECT entering the slice MUST clear the consolidated "
        "invite predicate chain (which includes is_invite_eligible).",
    ),
    (
        "workflows/pre_invite_check.py",
        "is_invite_eligible",
        "Defense-in-depth re-verification before degree scrape.",
    ),
)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_invite_eligible_imported_in_guarded_files() -> None:
    """Each guarded file must import its gate symbol from `models.pipeline`
    (or a relative re-export of the same symbol). The import check tolerates
    multi-line parenthesized imports.

    Catches the silent-regression where a refactor moves the import to
    a different module but leaves the call site looking correct.
    """
    offenders: list[str] = []
    for rel, symbol, _reason in GUARDED_INVITE_CALL_SITES:
        path = REPO_ROOT / rel
        assert path.exists(), f"guarded file missing: {rel}"
        text = _read(path)
        # Crude but effective: the symbol must appear inside a
        # `from models.pipeline import ...` statement (single-line or
        # parenthesized multi-line).
        import_blocks = re.findall(
            r"from models\.pipeline import (?:\([^)]*\)|[^\n]*)", text
        )
        if not any(symbol in block for block in import_blocks):
            offenders.append(rel)
    assert not offenders, (
        f"§3.1 invite-eligibility guard: the gate symbol must be imported from "
        f"models.pipeline in: {offenders}. Defense-in-depth call site "
        f"on the invite path."
    )


def test_invite_eligible_called_in_guarded_files() -> None:
    """Each guarded file must INVOKE its gate symbol. An import without
    a call site is a regression (someone refactored away the gate but left
    the import to keep the test green).
    """
    offenders: list[tuple[str, str]] = []
    for rel, symbol, reason in GUARDED_INVITE_CALL_SITES:
        path = REPO_ROOT / rel
        text = _read(path)
        # Look for an actual call: `symbol(` (paren).
        if f"{symbol}(" not in text:
            offenders.append((rel, reason))
    assert not offenders, (
        f"§3.1 invite-eligibility guard: the gate symbol must be CALLED in: "
        f"{[(f, r) for f, r in offenders]}"
    )


def test_invite_slice_reason_calls_quarantine_gate() -> None:
    """`invite_slice_reason` (the consolidated invite chain daily_check now
    routes through) must itself CALL `is_invite_eligible` — this is what
    makes daily_check's transitive §3.1 quarantine coverage real.

    AST-based (unlike the textual checks above) because the substring
    `is_invite_eligible(` exists elsewhere in models/pipeline.py (its own
    `def` line, potential future callers): the call must be found inside
    the body of `invite_slice_reason` specifically, or a deleted
    quarantine gate would leave a substring check green.
    """
    tree = ast.parse(_read(REPO_ROOT / "models/pipeline.py"))
    fn = next(
        (
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "invite_slice_reason"
        ),
        None,
    )
    assert fn is not None, (
        "§3.1 invite-eligibility guard: models/pipeline.py no longer "
        "defines invite_slice_reason — daily_check's invite gate is gone."
    )
    calls = [
        node.func.id
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert "is_invite_eligible" in calls, (
        "§3.1 invite-eligibility guard: invite_slice_reason must call "
        "is_invite_eligible(...) — the consolidated chain lost its "
        "quarantine gate."
    )


def test_no_private_invite_eligible_shim() -> None:
    """No private re-implementation. The canonical definition lives in
    `models.pipeline.is_invite_eligible` — any local helper named
    `_is_invite_eligible` (or `is_invite_eligible_*`) would let a bug
    drift away from the central rule.

    Exempt: `models/pipeline.py` itself (the canonical definition).
    Test files are not scanned (only `clients/`, `models/`, `workflows/`).
    """
    # Match local def of a name STARTING with is_invite_eligible (catches
    # `def is_invite_eligible_for_dm(...)` shim attempts).
    pattern = re.compile(r"^\s*def\s+_?is_invite_eligible\w*\s*\(", re.MULTILINE)
    offenders: list[str] = []
    for d in ("clients", "models", "workflows"):
        root = REPO_ROOT / d
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel == "models/pipeline.py":
                continue
            if pattern.search(_read(path)):
                offenders.append(rel)
    assert not offenders, (
        f"§3.1 invite-eligibility guard: private is_invite_eligible shim defined in: "
        f"{offenders}. Import the canonical helper from models.pipeline."
    )
