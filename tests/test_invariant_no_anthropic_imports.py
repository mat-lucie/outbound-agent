"""Static guard: no production module imports the ``anthropic`` SDK.

Per plan §0 invariant #11 (F-PR-9): the engine does NOT hold
``ANTHROPIC_API_KEY`` and does NOT import the ``anthropic`` SDK. LLM
work surfaces to the parent Claude Code slash-command session via
``workflows.llm_dispatch.request_llm_dispatch`` (file-based handoff).

This test AST-scans the production source tree for any
``import anthropic`` or ``from anthropic ...`` statement. The pr-test-
analyzer QA flagged the absence of this guard as HIGH-1 — without it,
a future PR can silently reintroduce an ``import anthropic`` and the
test suite stays green.

The scan covers ``workflows/``, ``clients/``, ``models/``, plus
``cli.py``. Test files are exempt (they may use ``MagicMock`` shaped
like the SDK, and a few advanced tests can opt into a real-SDK path
if the operator ever needs it locally).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

SCAN_TARGETS = [
    REPO_ROOT / "workflows",
    REPO_ROOT / "clients",
    REPO_ROOT / "models",
    REPO_ROOT / "cli.py",
]


def _iter_python_files() -> list[Path]:
    files: list[Path] = []
    for target in SCAN_TARGETS:
        if not target.exists():
            continue
        if target.is_file():
            files.append(target)
        else:
            files.extend(p for p in target.rglob("*.py") if "__pycache__" not in p.parts)
    return files


def _imports_anthropic(tree: ast.AST) -> list[tuple[int, str]]:
    """Return [(lineno, statement)] for any anthropic import in the AST."""
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # Match `import anthropic` and `import anthropic.foo`.
                if alias.name == "anthropic" or alias.name.startswith("anthropic."):
                    hits.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "anthropic" or mod.startswith("anthropic."):
                names = ", ".join(a.name for a in node.names)
                hits.append((node.lineno, f"from {mod} import {names}"))
    return hits


@pytest.mark.parametrize("py_file", _iter_python_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_anthropic_imports_in_production(py_file: Path) -> None:
    """Per §0 invariant #11: no production file imports anthropic."""
    source = py_file.read_text()
    tree = ast.parse(source, filename=str(py_file))
    hits = _imports_anthropic(tree)
    assert not hits, (
        f"§0 invariant #11 violation in {py_file.relative_to(REPO_ROOT)}: "
        f"production code MUST NOT import the anthropic SDK. Hits: {hits}. "
        f"LLM work surfaces via workflows.llm_dispatch.request_llm_dispatch."
    )


def test_guard_finds_at_least_one_python_file() -> None:
    """Sanity: the scan list resolves to real files. Without this, a path
    typo in SCAN_TARGETS would make the parametrize generate zero cases
    and the guard would silently pass."""
    files = _iter_python_files()
    assert files, f"No Python files found in scan targets: {SCAN_TARGETS}"
    # Each scan target that exists has at least one file (cli.py + workflows/).
    assert any(p.name == "cli.py" for p in files)
    assert any("workflows" in p.parts for p in files)
