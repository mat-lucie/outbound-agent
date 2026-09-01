"""Tests for the `--feature provenance` set in scripts/setup_attio_schema.py.

The feature creates the missing `last_migrated_by`
(record_reference -> migration_run) on the record objects
`MigrationRunWriter._stamp_back_pointers` PATCHes, so migrations can stamp
their §3.13 back-pointer instead of printing a back-pointer WARNING on every
otherwise-successful run.

Schema mutation itself is exercised in tests/test_attio_migration_helpers.py;
these tests pin the feature-specific contract: the right (object, slug, type,
referenced_object) tuples are deployed, dry_run is forwarded, an idempotent
re-run is silent, and a failure on one object is surfaced rather than
swallowed.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import click
import pytest

import scripts.setup_attio_schema as mod
from clients.attio_writer_registry import WRITE_OWNER_REGISTRY


def _deployed(spy: MagicMock) -> list[tuple]:
    """(object_slug, attr_slug, type_) per ensure_attribute call."""
    return [(c.args[2], c.args[3], c.args[4]) for c in spy.call_args_list]


def test_provisions_last_migrated_by_on_people_and_companies(monkeypatch):
    """Apply mode deploys last_migrated_by as a record_reference ->
    migration_run on BOTH object-level back-pointer targets. `people` is the
    one that was missing; `companies` is included because a fresh install has
    never provisioned it either and ensure_attribute makes the re-run free."""
    spy = MagicMock(return_value="created")
    monkeypatch.setattr(mod, "ensure_attribute", spy)

    mod._provision_provenance(MagicMock(), dry_run=False)

    assert _deployed(spy) == [
        ("people", "last_migrated_by", "record_reference"),
        ("companies", "last_migrated_by", "record_reference"),
    ]
    for call in spy.call_args_list:
        assert call.args[1] == "object"  # parent_kind — never a list
        assert call.kwargs["referenced_object"] == "migration_run"
        assert call.kwargs["dry_run"] is False


def test_dry_run_forwards_flag(monkeypatch):
    """Dry-run forwards dry_run=True to ensure_attribute, which short-circuits
    before any POST."""
    spy = MagicMock(return_value="would_create")
    monkeypatch.setattr(mod, "ensure_attribute", spy)

    mod._provision_provenance(MagicMock(), dry_run=True)

    assert spy.call_count == len(mod._PROVENANCE_OBJECTS)
    for call in spy.call_args_list:
        assert call.kwargs["dry_run"] is True


def test_idempotent_rerun_is_silent(monkeypatch):
    """A second run (attributes already exist) skips without raising — the
    whole point of routing this through setup_attio_schema instead of a dated
    one-off migrate script."""
    spy = MagicMock(return_value="skipped")
    monkeypatch.setattr(mod, "ensure_attribute", spy)

    mod._provision_provenance(MagicMock(), dry_run=False)  # must not raise

    assert spy.call_count == len(mod._PROVENANCE_OBJECTS)


def test_failure_on_one_object_is_surfaced_not_swallowed(monkeypatch):
    """A failure must raise a ClickException (non-zero exit) — a silently
    half-provisioned schema is exactly the alarm-fatigue failure mode this
    attribute exists to remove. The sibling object is still attempted."""
    spy = MagicMock(side_effect=[RuntimeError("attio exploded"), "created"])
    monkeypatch.setattr(mod, "ensure_attribute", spy)

    with pytest.raises(click.ClickException, match="provenance item"):
        mod._provision_provenance(MagicMock(), dry_run=False)

    assert spy.call_count == 2


def test_provenance_objects_match_back_pointer_targets():
    """`_PROVENANCE_OBJECTS` must stay in sync with the write-owner registry:
    every object we provision has a registered MigrationRunWriter owner, so
    the back-pointer PATCH passes the §3.15 gate."""
    for object_slug in mod._PROVENANCE_OBJECTS:
        assert WRITE_OWNER_REGISTRY[(object_slug, "last_migrated_by")] == (
            "workflows.migration_run_writer.MigrationRunWriter"
        )


def test_registry_declares_people_last_migrated_by():
    """The §3.15 write-owner registry must declare people.last_migrated_by so
    the back-pointer write has a registered owner (mirrors the
    linkedin_outreach and companies entries)."""
    assert (
        WRITE_OWNER_REGISTRY[("people", "last_migrated_by")]
        == "workflows.migration_run_writer.MigrationRunWriter"
    )
