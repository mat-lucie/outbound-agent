"""
test_no_secrets_gate.py — Pytest wrapper for the secrets/PII safety gate.

Two classes of tests:
1. Integration test: the gate passes on the real repo tree (currently clean).
2. Unit tests: patterns fire on known-bad strings and pass on known-good strings.
3. File-extension tests: gate fires when secrets are in .jsonl / .bak / .csv.
4. examples/ scanning: gate scans that tree (no full exclusion).

NOTE: This file is in EXCLUDED_FILES inside check_no_secrets.py, so planting
example bad strings here does NOT trip the gate on the repo tree scan.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Import the gate module — scripts/ is not a package, so add it to sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_no_secrets import (  # noqa: E402
    SCAN_EXTENSIONS,
    SECRET_PATTERNS,
    _is_excluded,
    _is_placeholder_value,
    scan_file,
    scan_repo,
)

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _matches_any(line: str) -> list[str]:
    """Return labels of all patterns that fire on `line` (applies placeholder filter)."""
    hits = []
    for label, pattern in SECRET_PATTERNS:
        m = pattern.search(line)
        if m and not _is_placeholder_value(m.group(0)):
            hits.append(label)
    return hits


def _matches_none(line: str) -> bool:
    return len(_matches_any(line)) == 0


# ---------------------------------------------------------------------------
# 1. Integration test: current repo tree must be clean
# ---------------------------------------------------------------------------


class TestRepoTreeIsClean:
    def test_no_secrets_in_repo(self) -> None:
        """The full repo scan must return 0 findings on a clean tree."""
        repo_root = Path(__file__).resolve().parent.parent
        findings = scan_repo(repo_root)
        if findings:
            lines = []
            for path, lineno, label, line in findings:
                lines.append(
                    f"  {path.relative_to(repo_root)}:{lineno} [{label}]\n    {line[:100]}"
                )
            pytest.fail(
                "Secrets gate found findings in the repo tree:\n"
                + "\n".join(lines)
            )


# ---------------------------------------------------------------------------
# 2. Unit tests: patterns fire on bad strings
# ---------------------------------------------------------------------------


class TestPatternsFire:
    """Each of these lines should trip at least one pattern."""

    # Real-looking LinkedIn li_at cookie
    def test_li_at_cookie_assignment(self) -> None:
        line = "li_at=AQEDAuXYz1234567890abcdefghijklmnopqrstuvwxyz"
        assert _matches_any(line), f"Expected a hit on: {line!r}"

    def test_pb_li_session_cookie_env(self) -> None:
        line = "PB_LI_SESSION_COOKIE=AQEDAuXYz1234567890abcdefghijklmnopqrstuvwxyz"
        assert _matches_any(line), f"Expected a hit on: {line!r}"

    def test_pb_sales_nav_session_cookie_env(self) -> None:
        line = "PB_LI_SALES_NAV_SESSION_COOKIE=AQEDAuXYz1234567890abcdefghijklmnopqrstuvwxyz"
        assert _matches_any(line), f"Expected a hit on: {line!r}"

    def test_raw_aqe_blob(self) -> None:
        # A raw AQE... string in a JSON config value
        line = '"sessionCookie": "AQEDAuXYz1234567890abcdefghijklmnop"'
        assert _matches_any(line), f"Expected a hit on: {line!r}"

    def test_anthropic_key(self) -> None:
        # Use a plausible fake that does NOT contain placeholder substrings
        line = "ANTHROPIC_API_KEY=sk-ant-api03-aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789abcdef"
        assert _matches_any(line), f"Expected a hit on: {line!r}"

    def test_anthropic_key_inline(self) -> None:
        line = 'client = Anthropic(api_key="sk-ant-api03-aBcDeFgHiJkLmNoPqRsTuVwXyZ0123")'
        assert _matches_any(line), f"Expected a hit on: {line!r}"

    def test_phantombuster_api_key(self) -> None:
        line = "PHANTOMBUSTER_API_KEY=0123456789abcdef0123456789abcdef"
        assert _matches_any(line), f"Expected a hit on: {line!r}"

    def test_x_phantombuster_header(self) -> None:
        line = '"X-Phantombuster-Key": "0123456789abcdef0123456789abcdef"'
        assert _matches_any(line), f"Expected a hit on: {line!r}"

    def test_literal_bearer_token(self) -> None:
        line = '"Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9abcdefg"'
        assert _matches_any(line), f"Expected a hit on: {line!r}"

    def test_aws_access_key(self) -> None:
        line = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
        assert _matches_any(line), f"Expected a hit on: {line!r}"

    def test_aws_secret_key(self) -> None:
        line = "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        assert _matches_any(line), f"Expected a hit on: {line!r}"

    def test_attio_api_key(self) -> None:
        line = "ATTIO_API_KEY=abcdefghijklmnopqrstuvwxyz0123456789abcdef"
        assert _matches_any(line), f"Expected a hit on: {line!r}"

    def test_resend_api_key(self) -> None:
        line = "RESEND_API_KEY=re_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
        assert _matches_any(line), f"Expected a hit on: {line!r}"


# ---------------------------------------------------------------------------
# 3. Unit tests: safe patterns do NOT fire (false-positive guard)
# ---------------------------------------------------------------------------


class TestPatternsDontFire:
    """Lines that look adjacent to secrets but contain no actual values."""

    # .env.example style empty assignments
    def test_empty_li_session_cookie(self) -> None:
        assert _matches_none("PB_LI_SESSION_COOKIE=")

    def test_empty_phantombuster_key(self) -> None:
        assert _matches_none("PHANTOMBUSTER_API_KEY=")

    def test_empty_attio_key(self) -> None:
        assert _matches_none("ATTIO_API_KEY=")

    def test_empty_resend_key(self) -> None:
        assert _matches_none("RESEND_API_KEY=")

    # Format-string references (not literal values)
    def test_bearer_format_string(self) -> None:
        assert _matches_none('"Authorization": f"Bearer {self.api_key}"')

    def test_bearer_format_string_concat(self) -> None:
        assert _matches_none('"Authorization": "Bearer " + api_key')

    # Docstring / comment naming the variable without a value
    def test_li_at_in_docstring(self) -> None:
        assert _matches_none("    Returns ``{sessionCookie: <li_at>, userAgent: <ua>}``.")

    def test_li_at_description(self) -> None:
        assert _matches_none(
            "# li_at cookie used by Search Export / Network Booster / Message Sender"
        )

    # Monkeypatch test value — short/obvious fake, not real token
    def test_li_at_value_placeholder_in_test(self) -> None:
        # "li_at_value" is 8 chars — under the 20-char minimum for a real value
        assert _matches_none('monkeypatch.setenv("PB_LI_SESSION_COOKIE", "li_at_value")')

    # env var name mentioned without value
    def test_phantom_key_name_in_comment(self) -> None:
        assert _matches_none("# PHANTOMBUSTER_API_KEY — Settings → API")

    # Placeholder angle-bracket form
    def test_angle_bracket_placeholder(self) -> None:
        assert _matches_none("ATTIO_API_KEY=<your_api_key_here>")

    # The probe_pb_phantom_contracts.py tuple — names a var, no value
    def test_secret_key_tokens_tuple(self) -> None:
        assert _matches_none(
            '_SECRET_KEY_TOKENS = ("cookie", "session", "li_a", "li_at")'
        )

    # AWS AKIA prefix — only fires when ≥16 trailing uppercase chars
    # Here we have a short example that should not match
    def test_akia_short(self) -> None:
        assert _matches_none("AKIA_short")

    # li_at= with a short/empty value
    def test_li_at_empty_assignment(self) -> None:
        assert _matches_none("li_at=")

    # .env example comment line about PB_LI_SALES_NAV
    def test_env_comment_sales_nav(self) -> None:
        assert _matches_none(
            "# PB_LI_SALES_NAV_SESSION_COOKIE = li_at value (matches PB_LI_SESSION_COOKIE"
        )

    # li_a= empty assignment — should not fire
    def test_li_a_empty_assignment(self) -> None:
        assert _matches_none("li_a=")

    # li_a= placeholder in env.example comment — should not fire
    def test_li_a_env_comment(self) -> None:
        assert _matches_none(
            "# PB_LI_SALES_NAV_LI_A_COOKIE = li_a value (Sales Nav-specific)"
        )

    # PB_LI_SALES_NAV_LI_A_COOKIE empty — should not fire
    def test_pb_li_a_cookie_empty(self) -> None:
        assert _matches_none("PB_LI_SALES_NAV_LI_A_COOKIE=")

    # Google refresh token placeholder — empty value, no fire
    def test_google_refresh_token_empty(self) -> None:
        assert _matches_none('"refresh_token": ""')

    # Google refresh token with format-string ref — no fire
    def test_google_refresh_token_format_string(self) -> None:
        assert _matches_none('"refresh_token": "{token}"')

    # GOCSPX- short stub that does not reach 20 chars — no fire
    def test_gocspx_too_short(self) -> None:
        assert _matches_none("GOCSPX-short")

    # Private key label in prose (not a real block start) — no fire
    def test_private_key_in_prose(self) -> None:
        assert _matches_none("# stores a PRIVATE KEY in credentials/")


# ---------------------------------------------------------------------------
# 4. NEW: Google OAuth patterns fire on real-looking values
# ---------------------------------------------------------------------------


class TestGoogleOAuthPatternsFire:
    """FIX 1 — Google OAuth patterns catch real credential values."""

    def test_google_refresh_token_1_slash_slash_0(self) -> None:
        # Classic format from google-authorized-user.json
        line = '"refresh_token": "1//0gABcDeFgHiJkLmNoPqRsTuVwXyZ0123456789abcdef"'
        assert _matches_any(line), f"Expected a hit on: {line!r}"

    def test_google_refresh_token_bare_assignment(self) -> None:
        # Bare assignment form (e.g. shell export)
        line = "GOOGLE_REFRESH_TOKEN=1//0gABcDeFgHiJkLmNoPqRsTuVwXyZ0123456789abcdef"
        # This fires via the "1//0..." pattern (no key-name required)
        assert _matches_any(line), f"Expected a hit on: {line!r}"

    def test_google_client_secret_gocspx(self) -> None:
        line = '"client_secret": "GOCSPX-ABcDeFgHiJkLmNoPqRsTuVwXyZ012"'
        assert _matches_any(line), f"Expected a hit on: {line!r}"

    def test_google_client_secret_env_var(self) -> None:
        line = "GOOGLE_CLIENT_SECRET=GOCSPX-ABcDeFgHiJkLmNoPqRsTuVwXyZ012"
        assert _matches_any(line), f"Expected a hit on: {line!r}"

    def test_pem_rsa_private_key_block(self) -> None:
        line = "-----BEGIN RSA PRIVATE KEY-----"
        assert _matches_any(line), f"Expected a hit on: {line!r}"

    def test_pem_private_key_block(self) -> None:
        line = "-----BEGIN PRIVATE KEY-----"
        assert _matches_any(line), f"Expected a hit on: {line!r}"

    def test_pem_ec_private_key_block(self) -> None:
        line = "-----BEGIN EC PRIVATE KEY-----"
        assert _matches_any(line), f"Expected a hit on: {line!r}"

    def test_json_refresh_token_field_real_value(self) -> None:
        # JSON shape with a non-1//0 token (catches other OAuth providers too)
        line = '"refresh_token": "AMf-vBwABcDeFgHiJkLmNoPqRsTuVwXyZ012345678"'
        assert _matches_any(line), f"Expected a hit on: {line!r}"


# ---------------------------------------------------------------------------
# 5. NEW: li_a cookie patterns fire on real-looking values
# ---------------------------------------------------------------------------


class TestLiACookiePatternsFire:
    """FIX 2 — li_a cookie (Sales Navigator) is caught."""

    def test_li_a_assignment_aqj(self) -> None:
        # li_a values often start with AQJ (not AQE — AQE blob won't catch this)
        line = "li_a=AQJXRw_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789abcdef"
        assert _matches_any(line), f"Expected a hit on: {line!r}"

    def test_li_a_assignment_generic(self) -> None:
        line = "li_a=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghij"
        assert _matches_any(line), f"Expected a hit on: {line!r}"

    def test_pb_li_sales_nav_li_a_cookie_env(self) -> None:
        line = "PB_LI_SALES_NAV_LI_A_COOKIE=AQJXRw_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"
        assert _matches_any(line), f"Expected a hit on: {line!r}"

    def test_pb_li_li_a_cookie_env(self) -> None:
        # Shorter variant without SALES_NAV_ infix
        line = "PB_LI_LI_A_COOKIE=AQJXRw_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"
        assert _matches_any(line), f"Expected a hit on: {line!r}"


# ---------------------------------------------------------------------------
# 6. NEW: Extension gate — .jsonl, .bak, .csv files are scanned
# ---------------------------------------------------------------------------


class TestExtensionScanning:
    """FIX 3 + FIX 4 — secrets inside .jsonl, .bak, .csv files are caught."""

    def test_jsonl_in_scan_extensions(self) -> None:
        assert ".jsonl" in SCAN_EXTENSIONS

    def test_bak_in_scan_extensions(self) -> None:
        assert ".bak" in SCAN_EXTENSIONS

    def test_csv_in_scan_extensions(self) -> None:
        assert ".csv" in SCAN_EXTENSIONS

    def test_secret_in_jsonl_file_fires(self, tmp_path: Path) -> None:
        """A planted Anthropic key in a .jsonl file is caught."""
        f = tmp_path / "fixtures.jsonl"
        f.write_text(
            '{"key": "sk-ant-api03-aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789abcdef"}\n'
        )
        hits = scan_file(f)
        assert hits, "Expected a hit in .jsonl file, got none"

    def test_clean_jsonl_file_passes(self, tmp_path: Path) -> None:
        """A .jsonl file with no secrets passes clean."""
        f = tmp_path / "fixtures.jsonl"
        f.write_text('{"name": "Acme Corp", "industry": "manufacturing"}\n')
        hits = scan_file(f)
        assert not hits, f"Unexpected hit in clean .jsonl: {hits}"

    def test_google_refresh_token_in_bak_file_fires(self, tmp_path: Path) -> None:
        """A Google refresh token in a .bak file (OAuth token backup) is caught."""
        f = tmp_path / "google-authorized-user.json.bak"
        f.write_text(
            '{"refresh_token": "1//0gABcDeFgHiJkLmNoPqRsTuVwXyZ0123456789abcdef",'
            ' "token_uri": "https://oauth2.googleapis.com/token"}\n'
        )
        hits = scan_file(f)
        assert hits, "Expected a hit in .bak file, got none"

    def test_clean_bak_file_passes(self, tmp_path: Path) -> None:
        """A .bak file with no secrets passes clean."""
        f = tmp_path / "config.bak"
        f.write_text("SETTING=value\nOTHER_SETTING=other\n")
        hits = scan_file(f)
        assert not hits, f"Unexpected hit in clean .bak: {hits}"

    def test_pb_cookie_in_csv_file_fires(self, tmp_path: Path) -> None:
        """A LinkedIn session cookie embedded in a .csv PB export is caught."""
        f = tmp_path / "result.csv"
        f.write_text(
            "profileUrl,li_at\n"
            "https://linkedin.com/in/someone,AQEDAuXYz1234567890abcdefghijklmnopqrstuvwxyz\n"
        )
        hits = scan_file(f)
        assert hits, "Expected a hit in .csv file, got none"

    def test_clean_csv_file_passes(self, tmp_path: Path) -> None:
        """A .csv file with no secrets passes clean."""
        f = tmp_path / "prospects.csv"
        f.write_text("profileUrl,name\nhttps://linkedin.com/in/someone,John Doe\n")
        hits = scan_file(f)
        assert not hits, f"Unexpected hit in clean .csv: {hits}"


# ---------------------------------------------------------------------------
# 7. examples/ is scanned for secrets
# ---------------------------------------------------------------------------


class TestExamplesScanningEnabled:
    """examples/ is not fully excluded; secrets are caught there."""

    def test_examples_acme_not_in_excluded_dirs(self) -> None:
        from check_no_secrets import EXCLUDED_DIRS

        assert "examples/acme" not in EXCLUDED_DIRS, (
            "examples/acme must NOT be in EXCLUDED_DIRS — it should be scanned for secrets"
        )

    def test_examples_file_not_excluded(self, tmp_path: Path) -> None:
        """A file inside examples/ is not excluded from scanning."""
        repo_root = tmp_path
        ex_dir = tmp_path / "examples" / "acme"
        ex_dir.mkdir(parents=True)
        f = ex_dir / "test.md"
        f.write_text("nothing sensitive\n")
        assert not _is_excluded(f, repo_root), (
            "examples/acme/test.md should NOT be excluded"
        )

    def test_secret_in_examples_fires(self, tmp_path: Path) -> None:
        """A planted secret inside examples/ triggers the gate."""
        repo_root = tmp_path
        ex_dir = tmp_path / "examples" / "acme"
        ex_dir.mkdir(parents=True)
        f = ex_dir / "leaked.md"
        f.write_text(
            "# Notes\n\nli_at=AQEDAuXYz1234567890abcdefghijklmnopqrstuvwxyz\n"
        )
        findings = scan_repo(repo_root)
        assert findings, "Expected gate to fire on secret planted in examples/"

    def test_clean_examples_passes(self, tmp_path: Path) -> None:
        """The real examples/acme/ tree has zero secret hits today."""
        repo_root = Path(__file__).resolve().parent.parent
        ex_dir = repo_root / "examples" / "acme"
        if not ex_dir.exists():
            pytest.skip("examples/acme/ not present")
        # Scan only that subtree (scan_file / SCAN_EXTENSIONS already imported above)
        hits: list[tuple] = []
        for path in sorted(ex_dir.rglob("*")):
            if path.is_file() and path.suffix in SCAN_EXTENSIONS:
                for h in scan_file(path):
                    hits.append((path, *h))
        assert not hits, (
            "examples/acme/ contains secret hits — review before committing:\n"
            + "\n".join(f"  {p}: {label}" for p, _, label, _ in hits)
        )
