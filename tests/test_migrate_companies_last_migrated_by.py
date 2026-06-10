"""Tests for scripts/migrate_attio_companies_last_migrated_by.py (#18).

The script creates the missing `companies.last_migrated_by`
(record_reference -> migration_run) so company-targeted migrations can
stamp their §3.13 back-pointer. Schema mutation itself is exercised in
tests/test_attio_migration_helpers.py; these tests pin the script-specific
contract: the right (object, slug, type, referenced_object) is deployed,
dry_run is forwarded, and the idempotent re-run skips.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import scripts.migrate_attio_companies_last_migrated_by as mod
from clients.attio_writer_registry import WRITE_OWNER_REGISTRY


def test_deploy_creates_companies_last_migrated_by_referencing_migration_run(monkeypatch):
    """Apply mode deploys last_migrated_by ON companies (NOT linkedin_outreach)
    as a record_reference -> migration_run, and logs it as a schema mutation."""
    spy = MagicMock(return_value="created")
    monkeypatch.setattr(mod, "ensure_attribute", spy)
    run = MagicMock()

    action = mod.deploy(object(), dry_run=False, run=run)

    assert action == "created"
    args, kwargs = spy.call_args
    # Positional: (attio, parent_kind, parent_id, slug, type_)
    assert args[1:] == ("object", "companies", "last_migrated_by", "record_reference")
    assert kwargs["referenced_object"] == "migration_run"
    assert kwargs["dry_run"] is False
    run.examine.assert_called_once()
    run.mark_modified.assert_called_once_with(
        record_id="companies.last_migrated_by", object="schema"
    )
    run.skip_idempotent.assert_not_called()


def test_deploy_dry_run_forwards_flag(monkeypatch):
    """Dry-run forwards dry_run=True to ensure_attribute (which short-circuits
    before any POST) and still counts the would-create as modified."""
    spy = MagicMock(return_value="would_create")
    monkeypatch.setattr(mod, "ensure_attribute", spy)
    run = MagicMock()

    action = mod.deploy(object(), dry_run=True, run=run)

    assert action == "would_create"
    assert spy.call_args.kwargs["dry_run"] is True
    run.mark_modified.assert_called_once()
    run.skip_idempotent.assert_not_called()


def test_deploy_idempotent_skip(monkeypatch):
    """A second run (attribute already exists) skips without marking modified."""
    spy = MagicMock(return_value="skipped")
    monkeypatch.setattr(mod, "ensure_attribute", spy)
    run = MagicMock()

    action = mod.deploy(object(), dry_run=False, run=run)

    assert action == "skipped"
    run.skip_idempotent.assert_called_once()
    run.mark_modified.assert_not_called()


def test_registry_declares_companies_last_migrated_by():
    """The §3.15 write-owner registry must declare companies.last_migrated_by
    so the back-pointer write has a registered owner (mirrors the
    linkedin_outreach entry)."""
    assert (
        WRITE_OWNER_REGISTRY[("companies", "last_migrated_by")]
        == "workflows.migration_run_writer.MigrationRunWriter"
    )
