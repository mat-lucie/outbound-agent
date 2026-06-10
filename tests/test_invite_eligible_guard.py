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

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# (file relative to repo root, the §3.1 reason this file must gate)
GUARDED_INVITE_CALL_SITES: tuple[tuple[str, str], ...] = (
    (
        "workflows/daily_check.py",
        "run_connection_requests is the production invite slice — "
        "every PROSPECT entering the slice MUST clear is_invite_eligible.",
    ),
    (
        "workflows/pre_invite_check.py",
        "Defense-in-depth re-verification before degree scrape.",
    ),
)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_invite_eligible_imported_in_guarded_files() -> None:
    """Both daily_check.py and pre_invite_check.py must import `is_invite_eligible`
    from `models.pipeline` (or a relative re-export of the same symbol).

    Catches the silent-regression where a refactor moves the import to
    a different module but leaves the call site looking correct.
    """
    offenders: list[str] = []
    for rel, _reason in GUARDED_INVITE_CALL_SITES:
        path = REPO_ROOT / rel
        assert path.exists(), f"guarded file missing: {rel}"
        text = _read(path)
        if "is_invite_eligible" not in text:
            offenders.append(rel)
            continue
        # Crude but effective: must appear at least once on a `from
        # models.pipeline import ...` line.
        import_lines = [
            ln for ln in text.splitlines()
            if "from models.pipeline" in ln and "import" in ln
        ]
        if not any("is_invite_eligible" in ln for ln in import_lines):
            offenders.append(rel)
    assert not offenders, (
        f"§3.1 invite-eligibility guard: is_invite_eligible must be imported from "
        f"models.pipeline in: {offenders}. Defense-in-depth call site "
        f"on the invite path."
    )


def test_invite_eligible_called_in_guarded_files() -> None:
    """Both files must INVOKE `is_invite_eligible(...)`. An import without
    a call site is a regression (someone refactored away the gate but left
    the import to keep the test green).
    """
    offenders: list[tuple[str, str]] = []
    for rel, reason in GUARDED_INVITE_CALL_SITES:
        path = REPO_ROOT / rel
        text = _read(path)
        # Look for an actual call: `is_invite_eligible(` (paren).
        if "is_invite_eligible(" not in text:
            offenders.append((rel, reason))
    assert not offenders, (
        f"§3.1 invite-eligibility guard: is_invite_eligible(...) must be CALLED in: "
        f"{[(f, r) for f, r in offenders]}"
    )


def test_no_private_invite_eligible_shim() -> None:
    """No private re-implementation. The canonical definition lives in
    `models.pipeline.is_invite_eligible` — any local helper named
    `_is_invite_eligible` (or `is_invite_eligible_*`) would let a bug
    drift away from the central rule.

    Exempt: `models/pipeline.py` itself (the canonical definition).
    Test files are not scanned (only `clients/`, `models/`, `workflows/`).
    """
    import re

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
