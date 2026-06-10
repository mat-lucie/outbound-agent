"""F-PR-1 CI guard: the three duplicate STAGE_RANK dicts must not return.

`STAGE_RANK` lives in `models.pipeline` (canonical, single source of truth).
F-PR-1 deleted three module-local copies:

- `_LIST_ENTRY_STAGE_RANK` in clients/attio.py
- `_STAGE_RANK` in workflows/daily_check.py
- `_STAGE_RANK_FOR_DRIFT` in workflows/detect_responses.py

This test fails if any of those names reappears, or if a new module-local
`STAGE_RANK`-style dict is introduced in models/ or workflows/. Scripts/
are exempt: PR-9.5 (dedup) and later PRs will consolidate those.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Names whose absence is a hard contract from F-PR-1.
FORBIDDEN_DELETED_NAMES = (
    "_LIST_ENTRY_STAGE_RANK",
    "_STAGE_RANK_FOR_DRIFT",
)

# Directories where rank tables must NOT be redefined locally; they must
# import from models.pipeline. scripts/ is intentionally excluded — those
# repair scripts predate canonicalization and are out of F-PR-1 scope.
GUARDED_DIRS = ("clients", "models", "workflows")


def _python_files_in(*dir_names: str) -> list[Path]:
    paths: list[Path] = []
    for d in dir_names:
        root = REPO_ROOT / d
        if not root.exists():
            continue
        paths.extend(p for p in root.rglob("*.py") if p.is_file())
    return paths


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", FORBIDDEN_DELETED_NAMES)
def test_forbidden_deleted_name_does_not_return(name: str) -> None:
    """Each of the three duplicate dicts F-PR-1 deleted must stay deleted.

    A grep over the repo (excluding the test file itself, plan/ docs, and
    git metadata) must turn up zero matches. If a future change reintroduces
    one of these names — even as a private helper — this test fails and
    forces the author to import `STAGE_RANK` from models.pipeline instead.
    """
    offenders: list[str] = []
    for path in REPO_ROOT.rglob("*.py"):
        # Skip this test file itself + venvs + worktree caches.
        if path.resolve() == Path(__file__).resolve():
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        # `.claude/` is local developer scratch — Claude Code worktree caches
        # and skill artifacts. None of it ships to main. Scanning it picks up
        # stale snapshots of pre-F-PR-1 code that have no bearing on the
        # current repo contract; skip the prefix the same way we skip venvs.
        if rel.startswith((".venv/", "venv/", ".git/", "node_modules/", ".claude/")):
            continue
        if name in _read(path):
            offenders.append(rel)
    assert not offenders, (
        f"F-PR-1 deleted {name}. It reappeared in: {offenders}. "
        f"Import STAGE_RANK from models.pipeline instead."
    )


def test_no_local_stage_rank_dict_in_guarded_dirs() -> None:
    """No module under clients/, models/ (except pipeline.py), or workflows/
    may define its own `STAGE_RANK`-shaped dict. They must import the
    canonical one from `models.pipeline`.

    Pattern matches a python dict-literal assignment whose left-hand side
    ends in `STAGE_RANK` or `_STAGE_RANK`. The canonical definition in
    `models/pipeline.py` is exempt by path.
    """
    import re

    # Match: any name ending in STAGE_RANK, followed by optional type hint,
    # then `= {` on the same line. Catches both `STAGE_RANK = {` and
    # `_STAGE_RANK: dict[...] = {`.
    pattern = re.compile(r"^\s*\w*STAGE_RANK\b[^=\n]*=\s*\{", re.MULTILINE)

    offenders: list[str] = []
    for path in _python_files_in(*GUARDED_DIRS):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel == "models/pipeline.py":
            continue
        if pattern.search(_read(path)):
            offenders.append(rel)
    assert not offenders, (
        f"Local STAGE_RANK-shaped dict defined in: {offenders}. "
        f"Import STAGE_RANK from models.pipeline instead. "
        f"(scripts/ is exempt — out of F-PR-1 scope.)"
    )
