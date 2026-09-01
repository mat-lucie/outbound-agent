"""
check_no_secrets.py — Secrets / PII safety gate for outbound-agent.

Scans the repository tree for known secret-value patterns and exits non-zero
if any are found. Designed to run as a pre-commit check and as a pytest target.

Usage:
    python scripts/check_no_secrets.py            # exit 0 = clean, 1 = found secrets
    python scripts/check_no_secrets.py --help

Excluded trees (not scanned):
    - .venv/
    - .git/
    - scripts/check_no_secrets.py   (this file)
    - tests/test_no_secrets_gate.py (the companion test — may contain planted examples)
    - .env.example                  (empty placeholders)

NOTE on examples/:
    examples/ is NOT fully excluded. It is scanned for SECRET_PATTERNS
    just like the rest of the repo. The earlier full exemption was removed
    because pasted live cookies/keys are most likely to accumulate there.
    The directory currently contains only docs/markdown with zero secret hits
    (verified 2026-06-02). If operator real data with PII (but no secrets)
    ever lives there, add a PII-only exemption separately — do not re-add a
    blanket exclusion that would also suppress secret scanning.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------
# Each entry: (label, compiled regex)
# A pattern fires when its regex matches ANY non-empty capturing group
# in a line — meaning an actual secret value is present, not just the
# variable name or an empty assignment.
#
# Design goals:
#   PASS: KEY=           (empty .env.example placeholder)
#   PASS: KEY=<placeholder>   (angle-bracket placeholder)
#   PASS: # ... li_at cookie  (docstring naming a variable)
#   PASS: "Bearer {self.api_key}"   (format-string reference, no literal value)
#   FAIL: li_at=AQEDAuXY...  (real LinkedIn cookie value)
#   FAIL: sk-ant-api03-...   (real Anthropic key)
#   FAIL: AQEDAXYZ...        (raw AQE-prefixed base64 blob)

# Match a "real" value (non-empty, non-placeholder, non-variable-reference)
# — at least 20 chars, no spaces, not wrapped in {}, not <...>
_REAL_VALUE = r'(?P<val>[A-Za-z0-9+/=_\-]{20,})'

# Placeholder strings that look like values but are clearly docs/examples.
# Used in _is_placeholder_value() to suppress false positives.
# IMPORTANT: be precise — do NOT add short substrings that could appear in real tokens.
_PLACEHOLDER_SUBSTRINGS = (
    "your-",
    "your_",
    "yourkey",
    "your-secret",
    "example-key",
    "example_key",
    "placeholder",
    "changeme",
    "replace-me",
    "xxxxxxx",    # 7+ x's = obviously fake
    "test-key",
    "secret-here",
    "key-here",
)


def _is_placeholder_value(value: str) -> bool:
    """Return True if the matched value looks like a documentation placeholder."""
    lower = value.lower()
    return any(sub in lower for sub in _PLACEHOLDER_SUBSTRINGS)

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # ------------------------------------------------------------------ #
    # LinkedIn session cookies
    # ------------------------------------------------------------------ #
    # li_at=<value>  (URL-encoded assignment form from PhantomBuster config)
    (
        "LinkedIn li_at cookie (assignment)",
        re.compile(r'\bli_at=(?!<)' + _REAL_VALUE),
    ),
    # PB_LI_SESSION_COOKIE=<non-empty non-placeholder value>
    (
        "PB_LI_SESSION_COOKIE env var with value",
        re.compile(r'PB_LI_(?:SALES_NAV_)?SESSION_COOKIE\s*=\s*(?!<|\{)' + _REAL_VALUE),
    ),
    # Raw AQE... blob — LinkedIn session tokens always start with AQE
    # (must be ≥30 chars to avoid short false positives like "AQED" in prose)
    (
        "Raw AQE-prefixed LinkedIn session token",
        re.compile(r'(?<![A-Za-z0-9])AQE[A-Za-z0-9+/=_\-]{27,}'),
    ),

    # ------------------------------------------------------------------ #
    # Anthropic / Claude API keys
    # ------------------------------------------------------------------ #
    (
        "Anthropic API key (sk-ant-...)",
        re.compile(r'sk-ant-[a-zA-Z0-9\-_]{20,}'),
    ),

    # ------------------------------------------------------------------ #
    # PhantomBuster API keys
    # ------------------------------------------------------------------ #
    # PB keys are 64-char hex strings assigned to PHANTOMBUSTER_API_KEY
    (
        "PhantomBuster API key with value",
        re.compile(r'PHANTOMBUSTER_API_KEY\s*=\s*(?!<|\{)(?P<val>[0-9a-fA-F]{32,})'),
    ),
    # X-Phantombuster-Key header with an actual value (not a variable ref)
    (
        "X-Phantombuster-Key header with literal value",
        re.compile(r'X-Phantombuster-Key["\s]*:\s*["\']?(?!\{)' + _REAL_VALUE),
    ),

    # ------------------------------------------------------------------ #
    # Generic bearer tokens — literal Authorization header with a value
    # that is NOT a Python format-string placeholder
    # ------------------------------------------------------------------ #
    # Fires on:  Authorization: Bearer eyJhbGciO...
    # Does NOT fire on: f"Bearer {self.api_key}"  or  "Bearer " + key
    (
        "Literal Authorization Bearer token value",
        re.compile(r'[Aa]uthorization["\s]*:\s*["\']?Bearer\s+(?!\{)' + _REAL_VALUE),
    ),

    # ------------------------------------------------------------------ #
    # AWS access keys
    # ------------------------------------------------------------------ #
    (
        "AWS access key ID",
        re.compile(r'(?:AWS_ACCESS_KEY_ID|AKIA)[A-Z0-9]{16,}'),
    ),
    (
        "AWS secret access key with value",
        re.compile(
            r'AWS_SECRET_ACCESS_KEY\s*=\s*(?!<|\{)(?P<val>[A-Za-z0-9+/]{40,})'
        ),
    ),

    # ------------------------------------------------------------------ #
    # Attio / Resend API keys with values
    # ------------------------------------------------------------------ #
    (
        "ATTIO_API_KEY with value",
        re.compile(r'ATTIO_API_KEY\s*=\s*(?!<|\{)' + _REAL_VALUE),
    ),
    (
        "RESEND_API_KEY with value",
        re.compile(r'RESEND_API_KEY\s*=\s*(?!<|\{)' + _REAL_VALUE),
    ),

    # ------------------------------------------------------------------ #
    # Google OAuth / service-account credentials
    # ------------------------------------------------------------------ #
    # Google OAuth refresh token — starts with "1//0" followed by ≥30 url-safe chars.
    # These appear in credentials/google-authorized-user.json (and .bak).
    (
        "Google OAuth refresh token (1//0...)",
        re.compile(r'1//0[A-Za-z0-9_\-]{30,}'),
    ),
    # Google OAuth client secret — GOCSPX- prefix.
    # Appears in credentials/google-oauth.json (OAuth2 desktop app credentials).
    (
        "Google OAuth client secret (GOCSPX-...)",
        re.compile(r'GOCSPX-[A-Za-z0-9_\-]{20,}'),
    ),
    # Private key PEM block — service account JSON or any leaked private key file.
    (
        "PEM private key block",
        re.compile(r'-----BEGIN (?:RSA |EC )?PRIVATE KEY-----'),
    ),
    # "refresh_token": "<non-empty value>" in JSON — catches varied token shapes
    # that don't start with 1//0 (e.g. older Google tokens or other OAuth providers).
    # Fires when the JSON value is ≥20 chars and not a placeholder/variable ref.
    (
        'JSON "refresh_token" field with real value',
        re.compile(r'"refresh_token"\s*:\s*"(?!\{|<)' + _REAL_VALUE),
    ),

    # ------------------------------------------------------------------ #
    # LinkedIn li_a cookie (Sales Navigator)
    # ------------------------------------------------------------------ #
    # PB_LI_SALES_NAV_LI_A_COOKIE env var with a real value.
    # This is a first-class secret listed in .env.example but was not
    # previously covered by any pattern.
    (
        "PB_LI_SALES_NAV_LI_A_COOKIE env var with value",
        re.compile(r'PB_LI_(?:SALES_NAV_)?LI_A_COOKIE\s*=\s*(?!<|\{)' + _REAL_VALUE),
    ),
    # Bare li_a=<value> assignment form (mirrors the li_at= pattern above).
    # li_a values often start with AQJ... but the AQE blob pattern won't catch them.
    (
        "LinkedIn li_a cookie (assignment)",
        re.compile(r'\bli_a=(?!<)' + _REAL_VALUE),
    ),
]

# ---------------------------------------------------------------------------
# PII deny-list — known upstream incident identities
# ---------------------------------------------------------------------------
# This repo is PUBLIC and CRM-agnostic. A prior port carried real prospect,
# founder, and company identities from the private upstream into fixtures,
# docstrings, and comments. Those were scrubbed to synthetic acme-style names;
# this deny-list makes sure they can never silently reappear (a re-port, a
# copy-paste from private notes, or a merge from an unscrubbed branch).
#
# Keep this list append-only and specific: add a name only when it is a real
# person/company from an actual PII incident — never a generic English word.
# Matching is whole-word and case-insensitive (accents count as word chars).
#
# NOTE: the repo LICENSE legitimately names the copyright holder (a real
# person, "López-Therese") — it is exempted via EXCLUDED_FILES below rather
# than by dropping the name here, so the guard still fires everywhere else.
NAMES_DENYLIST = (
    "césar", "cesar",               # César de la Garza (Sigma exec)
    "garza",
    "mariano", "rozada",            # Mariano Rozada (Molinos exec)
    "molinos",
    "sigma",                        # Sigma Alimentos
    "therese",                      # López-Therese (founder surname)
    "pepe",                         # nickname carried in a dedup fixture
    "bachoco", "suzano", "canela",  # other real companies from private notes
    "bafar",                        # real company from the operator's never-contact set
)

for _name in NAMES_DENYLIST:
    SECRET_PATTERNS.append((
        f"PII: known upstream incident identity ({_name!r})",
        re.compile(r'\b' + re.escape(_name) + r'\b', re.IGNORECASE),
    ))

# ---------------------------------------------------------------------------
# File extensions to scan (source code, config, data)
# ---------------------------------------------------------------------------
SCAN_EXTENSIONS = {
    ".py", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini",
    ".env", ".txt", ".sh", ".md", ".html", ".js", ".ts",
    # FIX 3: additional surfaces — .jsonl (holdout/fixtures), .bak (OAuth token
    # backup written by clients/google_sheets.py), .csv (PB export files).
    ".jsonl", ".bak", ".csv",
}

# ---------------------------------------------------------------------------
# Paths excluded from scanning (relative to repo root)
# ---------------------------------------------------------------------------
EXCLUDED_DIRS = {
    ".venv",
    ".git",
    # NOTE: examples/ is intentionally NOT excluded here.
    # It IS scanned for SECRET_PATTERNS. See module docstring for rationale.
    "__pycache__",
}
EXCLUDED_FILES = {
    "scripts/check_no_secrets.py",          # this file
    "tests/test_no_secrets_gate.py",        # companion test (may plant examples)
    ".env.example",                          # empty placeholders
    "LICENSE",                               # legitimately names the copyright holder
}


def _is_excluded(path: Path, repo_root: Path) -> bool:
    rel = path.relative_to(repo_root)
    rel_str = str(rel)
    # Excluded files
    if rel_str in EXCLUDED_FILES:
        return True
    # Excluded directory trees — any path component (except the filename) in the set
    parts = rel.parts
    return any(part in EXCLUDED_DIRS for part in parts[:-1])


def scan_file(path: Path) -> list[tuple[int, str, str]]:
    """Return list of (lineno, label, matched_text) for any hits."""
    hits: list[tuple[int, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, PermissionError):
        return hits
    for lineno, line in enumerate(text.splitlines(), start=1):
        for label, pattern in SECRET_PATTERNS:
            m = pattern.search(line)
            if not m:
                continue
            # Suppress obvious documentation placeholders
            matched_text = m.group(0)
            if _is_placeholder_value(matched_text):
                continue
            hits.append((lineno, label, line.rstrip()))
    return hits


def scan_repo(repo_root: Path) -> list[tuple[Path, int, str, str]]:
    """Scan all eligible files; return list of (path, lineno, label, line)."""
    findings: list[tuple[Path, int, str, str]] = []
    for path in sorted(repo_root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix not in SCAN_EXTENSIONS:
            continue
        if _is_excluded(path, repo_root):
            continue
        for lineno, label, line in scan_file(path):
            findings.append((path, lineno, label, line))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan the repo for leaked secrets / PII and exit non-zero if found."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Repository root to scan (default: parent of this script's directory)",
    )
    args = parser.parse_args(argv)
    repo_root: Path = args.root.resolve()

    findings = scan_repo(repo_root)

    if not findings:
        print("check_no_secrets: PASSED — no secret patterns found.")
        return 0

    print("check_no_secrets: FAILED — potential secrets found:\n")
    for path, lineno, label, line in findings:
        print(f"  {path.relative_to(repo_root)}:{lineno}  [{label}]")
        # Truncate long lines for readability
        display = line[:120] + ("..." if len(line) > 120 else "")
        print(f"    {display}")
        print()
    print(
        f"Total: {len(findings)} finding(s). Remove or rotate the above before committing."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
