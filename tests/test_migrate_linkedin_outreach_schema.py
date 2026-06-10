"""Orchestration tests for scripts/migrate_attio_linkedin_outreach_schema.py.

The underlying ensure_attribute / reconcile_select_options helpers have their own
idempotency tests; here we verify the script's deploy() wiring: it creates the
select attr THEN reconciles its options (the single-`--apply` correctness fix),
records actions incrementally, and marks the Migration Run row skip-vs-modified
correctly.
"""
from __future__ import annotations

import importlib
from unittest.mock import MagicMock, patch

mod = importlib.import_module("scripts.migrate_attio_linkedin_outreach_schema")


def test_fresh_create_reconciles_options_then_marks_modified():
    run = MagicMock()
    actions: dict = {}
    with patch.object(mod, "ensure_attribute", return_value="created") as ea, \
         patch.object(mod, "reconcile_select_options", return_value="added_options:1") as rso:
        mod.deploy(MagicMock(), list_id="L", dry_run=False, run=run, actions=actions)

    # experiment_id_frozen_at created as a select (slug is the 4th positional arg).
    assert ea.call_args.args[3] == "experiment_id_frozen_at"
    assert ea.call_args.args[4] == "select"
    # After a fresh create, options are reconciled explicitly (single --apply fix).
    assert actions["experiment_id_frozen_at"] == "created"
    assert actions["experiment_id_frozen_at_options"] == "added_options:1"
    # response_classification option also reconciled.
    assert actions["response_classification"] == "added_options:1"
    # Not all skipped -> mark_modified, not skip_idempotent.
    run.mark_modified.assert_called_once()
    run.skip_idempotent.assert_not_called()
    # reconcile called twice: frozen_at options backfill + response_classification.
    assert rso.call_count == 2


def test_idempotent_reapply_skips():
    run = MagicMock()
    actions: dict = {}
    with patch.object(mod, "ensure_attribute", return_value="skipped"), \
         patch.object(mod, "reconcile_select_options", return_value="skipped"):
        mod.deploy(MagicMock(), list_id="L", dry_run=False, run=run, actions=actions)

    # Attr already exists -> no extra frozen_at options backfill key.
    assert actions == {
        "experiment_id_frozen_at": "skipped",
        "response_classification": "skipped",
    }
    run.skip_idempotent.assert_called_once()
    run.mark_modified.assert_not_called()


def test_dry_run_would_create_does_not_reconcile_missing_attr():
    """Dry-run: a not-yet-existing attr returns would_create; the script must
    NOT then call reconcile_select_options on it (that GET would 404)."""
    run = MagicMock()
    actions: dict = {}
    with patch.object(mod, "ensure_attribute", return_value="would_create"), \
         patch.object(mod, "reconcile_select_options", return_value="would_add_options:1") as rso:
        mod.deploy(MagicMock(), list_id="L", dry_run=True, run=run, actions=actions)

    assert actions["experiment_id_frozen_at"] == "would_create"
    assert "experiment_id_frozen_at_options" not in actions
    # reconcile only called once (for response_classification), never for the
    # not-yet-created frozen_at attr.
    assert rso.call_count == 1
