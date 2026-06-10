"""Shipped-placeholder send gate.

A fresh install ships NEUTRAL placeholder content in `content/` (messages,
emails) carrying a literal sentinel so a new operator cannot accidentally send
the engine's placeholder copy under their own LinkedIn / email identity. Every
LIVE send path runs `assert_content_replaced()` as a preflight; it raises
`PlaceholderContentError` if the active content still contains the sentinel.

The gate is intentionally NOT invoked on `--dry-run` — dry-run exists precisely
to let an operator inspect the (still-placeholder) copy before replacing it.

Operators resolve the gate by either replacing `content/messages.json` /
`content/emails.json` with their own copy, or pointing `OUTBOUND_CONTENT_DIR`
at a filled-in content directory (e.g. the bundled `examples/acme/content/`).
"""

from __future__ import annotations

from pathlib import Path

# The literal that marks an un-replaced shipped placeholder. Present in every
# cell of the neutral default `content/messages.json` and `content/emails.json`.
# Must stay in sync with the sentinel written into those shipped files.
PLACEHOLDER_SENTINEL = "REPLACE_THIS_TEMPLATE"

_GATE_HINT = (
    "Shipped placeholder content detected — the engine is still configured with "
    "its neutral default copy. Replace content/messages.json and "
    "content/emails.json with your own outreach copy, or set OUTBOUND_CONTENT_DIR "
    "to a filled-in content directory. See GETTING_STARTED.md."
)


class PlaceholderContentError(RuntimeError):
    """Raised by `assert_content_replaced()` when the active content still
    contains the shipped placeholder sentinel and a LIVE send was attempted.

    Carries the offending filenames so the operator sees exactly which files
    to replace.
    """

    def __init__(self, offending_files: list[str]) -> None:
        self.offending_files = offending_files
        files = ", ".join(offending_files) if offending_files else "(unknown)"
        super().__init__(f"{_GATE_HINT} Files still containing the placeholder: {files}")


def _content_dir() -> Path:
    """Resolve the active content directory at call time.

    Reads `models.campaign.CONTENT_DIR` so the gate honors both the
    `OUTBOUND_CONTENT_DIR` override and any test that monkeypatches the
    module constant.
    """
    from models import campaign

    return Path(campaign.CONTENT_DIR)


def _file_contains_sentinel(path: Path) -> bool:
    """Return True if `path` exists and its text contains the sentinel.

    A missing file is treated as NOT a placeholder hit — a deleted/renamed
    content file is a different failure class surfaced by the loaders, not by
    this gate.
    """
    if not path.exists():
        return False
    try:
        return PLACEHOLDER_SENTINEL in path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False


def content_has_placeholders(filenames: tuple[str, ...] = ("messages.json", "emails.json")) -> list[str]:
    """Return the subset of `filenames` (under the active content dir) that
    still contain the shipped placeholder sentinel.

    Pure inspection — never raises. Used both by the live-send gate and by
    `sales health-check` to surface the un-replaced state without blocking.
    """
    content_dir = _content_dir()
    return [name for name in filenames if _file_contains_sentinel(content_dir / name)]


def assert_content_replaced(
    *,
    dry_run: bool = False,
    filenames: tuple[str, ...] = ("messages.json", "emails.json"),
) -> None:
    """Block a LIVE send when the active content still ships placeholders.

    No-op under `dry_run=True` (the operator is inspecting, not sending).
    Raises `PlaceholderContentError` listing the offending files otherwise.
    """
    if dry_run:
        return
    offenders = content_has_placeholders(filenames)
    if offenders:
        raise PlaceholderContentError(offenders)
