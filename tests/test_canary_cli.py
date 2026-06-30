"""Tests for the `canary` subcommand — the §3.20 Attio scope canary.

The canary does a create-note → delete-note round-trip through the REST
client (the credential the daily run mutates with) against a pinned, inert
canary record. Exit 0 = read+write+delete confirmed; non-zero + a typed
`mcp_scope_insufficient` line on stderr = halt the skill.

These tests stub the AttioClient ctx-manager, then vary the create/delete
legs to cover each failure mode the Step-0 preflight must catch.

Click 8.2+ keeps stdout and stderr on separate channels (``result.output``
is stdout only; ``result.stderr`` is stderr). The typed `mcp_scope_*` lines
are emitted with ``err=True``, so the helper below joins both channels for
the content assertions, and a dedicated test guards that the typed line
lands on stderr specifically.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from cli import cli

# A test stand-in for the pinned canary record id (the real fork default is
# empty — operators pin their own inert Person). Patched onto the registry
# constant for the tests that exercise the configured path.
CANARY_RID = "test-canary-rid-0001"


def _all_output(result) -> str:
    """stdout + stderr joined — the typed mcp_scope lines use err=True."""
    return result.output + (result.stderr or "")


def _pin_record(monkeypatch) -> None:
    monkeypatch.setattr(
        "clients.attio_writer_registry.CANARY_PERSON_RECORD_ID", CANARY_RID
    )


def _stub_client(monkeypatch, *, create, delete, pin_record: bool = True):
    """Stub AttioClient as a no-op ctx-manager whose create_note/delete_note
    are driven by the `create`/`delete` callables passed in.

    conftest's autouse `_isolate_attio_api_key` deletes ATTIO_API_KEY, which
    the command's credential pre-check would otherwise trip on before reaching
    the stub — so set a dummy key here."""
    if pin_record:
        _pin_record(monkeypatch)
    monkeypatch.setenv("ATTIO_API_KEY", "canary-test-key")
    monkeypatch.setattr("clients.attio.AttioClient.__init__", lambda self: None)
    monkeypatch.setattr("clients.attio.AttioClient.__enter__", lambda self: self)
    monkeypatch.setattr("clients.attio.AttioClient.__exit__", lambda self, *a: False)
    monkeypatch.setattr(
        "clients.attio.AttioClient.create_note",
        lambda self, *a, **k: create(*a, **k),
    )
    monkeypatch.setattr(
        "clients.attio.AttioClient.delete_note",
        lambda self, note_id: delete(note_id),
    )


def test_canary_subcommand_registered():
    """The skill Step-0 invokes `canary`; the entry point must exist."""
    assert "canary" in cli.commands


def test_canary_help_runs():
    result = CliRunner().invoke(cli, ["canary", "--help"])
    assert result.exit_code == 0


def test_canary_success(monkeypatch):
    """Happy path: create returns a note_id, delete succeeds → exit 0."""
    calls = {}

    def _create(record_id, *_a, **_k):
        calls["record"] = record_id
        return {"id": {"note_id": "note-1", "workspace_id": "ws"}}

    def _delete(note_id):
        calls["deleted"] = note_id
        return True

    _stub_client(monkeypatch, create=_create, delete=_delete)
    result = CliRunner().invoke(cli, ["canary"])

    assert result.exit_code == 0, _all_output(result)
    assert "OK" in result.output
    assert "note-1" in result.output
    assert calls["deleted"] == "note-1"  # delete leg actually ran
    # Safety premise: the round-trip must target the pinned canary record,
    # never a real prospect.
    assert calls["record"] == CANARY_RID


def test_canary_unconfigured_record(monkeypatch):
    """Empty CANARY_PERSON_RECORD_ID → fail closed, never touch the API."""
    monkeypatch.setattr(
        "clients.attio_writer_registry.CANARY_PERSON_RECORD_ID", ""
    )
    # If the API is hit despite the empty id, these blow up the test.
    _stub_client(
        monkeypatch,
        create=lambda *a, **k: pytest.fail("create_note must not be called"),
        delete=lambda note_id: pytest.fail("delete_note must not be called"),
        pin_record=False,
    )
    result = CliRunner().invoke(cli, ["canary"])

    assert result.exit_code == 1
    out = _all_output(result)
    assert "mcp_scope_insufficient" in out
    assert "canary_record_unconfigured" in out


def test_canary_write_leg_raises(monkeypatch):
    """create_note raising = write scope not live → exit 1 + typed line."""

    def _create(*_a, **_k):
        raise RuntimeError("403 forbidden")

    _stub_client(
        monkeypatch,
        create=_create,
        delete=lambda note_id: pytest.fail("delete must not run after write failure"),
    )
    result = CliRunner().invoke(cli, ["canary"])

    assert result.exit_code == 1
    out = _all_output(result)
    assert "mcp_scope_insufficient" in out
    assert "write leg failed" in out


def test_canary_write_returns_no_note_id(monkeypatch):
    """create_note returns a malformed payload (no note_id) → exit 1."""
    _stub_client(
        monkeypatch,
        create=lambda *a, **k: {"id": {}},
        delete=lambda note_id: pytest.fail("delete must not run without a note_id"),
    )
    result = CliRunner().invoke(cli, ["canary"])

    assert result.exit_code == 1
    out = _all_output(result)
    assert "mcp_scope_insufficient" in out
    assert "no note_id" in out


def test_canary_delete_leg_raises_reports_orphan(monkeypatch):
    """delete_note raising = delete scope not live; the orphaned note_id
    MUST be surfaced so the operator can clean it up manually."""

    def _delete(note_id):
        raise RuntimeError("500 server error")

    _stub_client(
        monkeypatch,
        create=lambda *a, **k: {"id": {"note_id": "orphan-9"}},
        delete=_delete,
    )
    result = CliRunner().invoke(cli, ["canary"])

    assert result.exit_code == 1
    out = _all_output(result)
    assert "mcp_scope_insufficient" in out
    assert "delete leg failed" in out
    assert "orphan-9" in out  # leaked note id surfaced for cleanup


def test_canary_delete_returns_false(monkeypatch):
    """delete_note returns False (404) = created note vanished → exit 1."""
    _stub_client(
        monkeypatch,
        create=lambda *a, **k: {"id": {"note_id": "note-404"}},
        delete=lambda note_id: False,
    )
    result = CliRunner().invoke(cli, ["canary"])

    assert result.exit_code == 1
    out = _all_output(result)
    assert "mcp_scope_insufficient" in out
    assert "404" in out


def test_canary_missing_api_key(monkeypatch):
    """ATTIO_API_KEY unset is itself a scope failure: emit the typed line,
    don't let AttioClient() raise a bare KeyError the skill can't parse."""
    _pin_record(monkeypatch)
    monkeypatch.delenv("ATTIO_API_KEY", raising=False)
    # If the client were constructed despite the missing key, this trips.
    monkeypatch.setattr(
        "clients.attio.AttioClient.__init__",
        lambda self: pytest.fail("AttioClient must not be constructed without a key"),
    )
    result = CliRunner().invoke(cli, ["canary"])

    assert result.exit_code == 1
    out = _all_output(result)
    assert "mcp_scope_insufficient" in out
    assert "attio_api_key_unset" in out


def test_canary_typed_line_lands_on_stderr(monkeypatch):
    """The skill contract is a typed line on STDERR specifically. Guard it
    on its own channel — a regression that printed to stdout would pass the
    other (channel-agnostic) tests."""
    _stub_client(
        monkeypatch,
        create=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        delete=lambda note_id: pytest.fail("delete must not run"),
    )
    result = CliRunner().invoke(cli, ["canary"])

    assert result.exit_code == 1
    assert "mcp_scope_insufficient" in result.stderr


def test_canary_write_returns_non_dict(monkeypatch):
    """create_note returning a non-dict (e.g. None) must fail closed: the
    `isinstance(note, dict)` guard sends it to the no-note_id branch rather
    than letting `note.get(...)` escape as an untyped AttributeError."""
    _stub_client(
        monkeypatch,
        create=lambda *a, **k: None,
        delete=lambda note_id: pytest.fail("delete must not run without a note_id"),
    )
    result = CliRunner().invoke(cli, ["canary"])

    assert result.exit_code == 1
    out = _all_output(result)
    assert "mcp_scope_insufficient" in out
    assert "no note_id" in out
