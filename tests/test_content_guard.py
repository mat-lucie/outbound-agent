"""Tests for the shipped-placeholder send gate (workflows/content_guard.py).

The gate refuses to send LIVE when the active content directory still contains
the REPLACE_THIS_TEMPLATE sentinel that ships in the neutral default
content/messages.json and content/emails.json. It must:
  - BLOCK a live send when the sentinel is present
  - ALLOW a dry-run regardless (operators inspect placeholders before replacing)
  - ALLOW a live send once content is real (sentinel absent)
"""

from __future__ import annotations

import json

import pytest

from workflows import content_guard
from workflows.content_guard import (
    PLACEHOLDER_SENTINEL,
    PlaceholderContentError,
    assert_content_replaced,
    content_has_placeholders,
)


def _point_at(monkeypatch, content_dir):
    """Repoint the gate's resolved content dir at a tmp directory."""
    from models import campaign

    monkeypatch.setattr(campaign, "CONTENT_DIR", content_dir)


def _write(content_dir, name, contains_sentinel: bool):
    body = {"x": f"{PLACEHOLDER_SENTINEL} placeholder"} if contains_sentinel else {"x": "real copy"}
    (content_dir / name).write_text(json.dumps(body), encoding="utf-8")


class TestAssertContentReplaced:
    def test_blocks_live_send_when_sentinel_present(self, tmp_path, monkeypatch):
        _point_at(monkeypatch, tmp_path)
        _write(tmp_path, "messages.json", contains_sentinel=True)
        _write(tmp_path, "emails.json", contains_sentinel=False)

        with pytest.raises(PlaceholderContentError) as exc:
            assert_content_replaced(dry_run=False)

        # Only the actually-offending file is listed in offending_files; the
        # generic hint text references both filenames as guidance, so we assert
        # on the structured field rather than the prose.
        assert "messages.json" in str(exc.value)
        assert exc.value.offending_files == ["messages.json"]

    def test_dry_run_is_allowed_even_with_sentinel(self, tmp_path, monkeypatch):
        _point_at(monkeypatch, tmp_path)
        _write(tmp_path, "messages.json", contains_sentinel=True)
        _write(tmp_path, "emails.json", contains_sentinel=True)

        # No raise — dry-run is the inspect-before-replace path.
        assert assert_content_replaced(dry_run=True) is None

    def test_allows_live_send_when_content_is_real(self, tmp_path, monkeypatch):
        _point_at(monkeypatch, tmp_path)
        _write(tmp_path, "messages.json", contains_sentinel=False)
        _write(tmp_path, "emails.json", contains_sentinel=False)

        # No raise — real content, live send permitted.
        assert assert_content_replaced(dry_run=False) is None

    def test_missing_file_is_not_a_placeholder_hit(self, tmp_path, monkeypatch):
        # A deleted/renamed content file is a different failure class (the
        # loaders surface it); the gate must not block on absence.
        _point_at(monkeypatch, tmp_path)
        # Neither file exists.
        assert assert_content_replaced(dry_run=False) is None
        assert content_has_placeholders() == []

    def test_content_has_placeholders_lists_all_offenders(self, tmp_path, monkeypatch):
        _point_at(monkeypatch, tmp_path)
        _write(tmp_path, "messages.json", contains_sentinel=True)
        _write(tmp_path, "emails.json", contains_sentinel=True)

        offenders = content_has_placeholders()
        assert set(offenders) == {"messages.json", "emails.json"}


class TestShippedDefaultsTripTheGate:
    """The neutral defaults actually shipped in repo-root content/ MUST trip
    the gate — that is the whole point. (The test suite itself runs against
    examples/acme/content via conftest, so this points back at the real
    repo-root content/ to assert the shipped placeholders carry the sentinel.)
    """

    def test_shipped_repo_content_contains_sentinel(self, tmp_path, monkeypatch):
        from pathlib import Path

        repo_content = Path(content_guard.__file__).resolve().parent.parent / "content"
        from models import campaign

        monkeypatch.setattr(campaign, "CONTENT_DIR", repo_content)
        offenders = content_has_placeholders()
        # Both shipped neutral defaults carry the sentinel.
        assert "messages.json" in offenders
        assert "emails.json" in offenders

    def test_shipped_repo_content_is_structurally_loadable(self, monkeypatch):
        """Regression guard: the SHIPPED neutral content/ (which the suite
        otherwise repoints away from, via conftest) must stay schema-valid and
        loadable — all six Persona keys + the full message grid + the email
        templates. Without this, a malformed shipped placeholder would go
        unnoticed because every other test reads examples/acme/content.
        """
        from pathlib import Path

        repo_content = Path(content_guard.__file__).resolve().parent.parent / "content"
        from models import campaign, email_campaign
        from models.campaign import Persona

        monkeypatch.setattr(campaign, "CONTENT_DIR", repo_content)
        monkeypatch.setattr(email_campaign, "CONTENT_DIR", repo_content)

        personas = campaign.load_personas()
        messages = campaign.load_messages()
        emails = email_campaign.load_email_templates()

        # All six enum keys present in both personas and the message grid.
        for p in Persona:
            assert p.value in personas, f"persona {p.value} missing from shipped personas.json"
            assert p.value in messages, f"persona {p.value} missing from shipped messages.json"
        # Email templates present.
        for key in ("email1", "email2", "email3", "wave2"):
            assert key in emails, f"email template {key} missing from shipped emails.json"
