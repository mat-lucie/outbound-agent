"""Schema preflight tests (2026-06-09 desync-invariant design §3).

week_starting class (PR #146): one undeployed attribute 400s every
entry write in a run while tallies keep landing -> N desyncs at once.
The preflight converts that into an abort before any send.
"""
from unittest.mock import MagicMock

import click
import pytest

from workflows.schema_preflight import (
    DM_COMPANY_WRITER_ATTRS,
    DM_ENTRY_WRITER_ATTRS,
    DmWriterSchemaPreflightError,
    assert_dm_writer_schema,
)


def _attio(entry_slugs, company_slugs):
    attio = MagicMock()
    attio.get_list_attributes.return_value = [
        {"api_slug": s} for s in entry_slugs
    ]
    attio.get_object_attributes.return_value = [
        {"api_slug": s} for s in company_slugs
    ]
    return attio


class TestAssertDmWriterSchema:
    def test_all_present_passes(self):
        attio = _attio(DM_ENTRY_WRITER_ATTRS, DM_COMPANY_WRITER_ATTRS)
        assert_dm_writer_schema(attio, "lst")  # no raise
        attio.get_object_attributes.assert_called_once_with("companies")

    def test_missing_entry_attr_aborts_and_audits(self):
        attio = _attio(
            DM_ENTRY_WRITER_ATTRS - {"next_eligible_send_date"},
            DM_COMPANY_WRITER_ATTRS,
        )
        audit = MagicMock()
        with pytest.raises(DmWriterSchemaPreflightError) as exc:
            assert_dm_writer_schema(attio, "lst", audit_logger=audit)
        assert "next_eligible_send_date" in str(exc.value)
        audit.event.assert_called_once_with(
            "dm_writer_schema_preflight_failed",
            missing_list_entry_attrs=["next_eligible_send_date"],
            missing_company_attrs=[],
        )

    def test_missing_company_attr_aborts(self):
        attio = _attio(
            DM_ENTRY_WRITER_ATTRS,
            DM_COMPANY_WRITER_ATTRS - {"last_outreach_experiment_id"},
        )
        with pytest.raises(DmWriterSchemaPreflightError):
            assert_dm_writer_schema(attio, "lst")

    def test_fetch_failure_propagates_fail_closed(self):
        attio = MagicMock()
        attio.get_list_attributes.side_effect = RuntimeError("boom")
        with pytest.raises(RuntimeError):
            assert_dm_writer_schema(attio, "lst")


def test_preflight_error_is_click_exception():
    """The CLI surfaces ClickException messages cleanly (nonzero exit,
    no traceback wall) — operators see WHAT attr is missing."""
    assert issubclass(DmWriterSchemaPreflightError, click.ClickException)


class TestEmptyListId:
    def test_empty_list_id_aborts_with_clear_message(self):
        with pytest.raises(DmWriterSchemaPreflightError) as exc:
            assert_dm_writer_schema(MagicMock(), "")
        assert "ATTIO_LIST_ID" in str(exc.value)


def _assigned_dict_keys(func, var_name: str) -> set[str]:
    """All string keys assigned into *var_name* within *func*'s source,
    extracted structurally via ast (no char-window/regex fragility):

    - dict-literal keys from ``var_name = {...}`` and
      ``var_name: dict = {...}`` (Assign and AnnAssign)
    - conditional subscript assignments ``var_name["key"] = ...``

    Keys of OTHER dict literals (e.g. a ``"var_name": var_name`` item
    inside a payload dict) are ignored — only assignments whose target
    is *var_name* count.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    keys: set[str] = set()
    found_literal = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
            for target in targets:
                # var_name = {...} / var_name: dict = {...}
                if (
                    isinstance(target, ast.Name)
                    and target.id == var_name
                    and isinstance(node.value, ast.Dict)
                ):
                    found_literal = True
                    for k in node.value.keys:
                        if isinstance(k, ast.Constant) and isinstance(k.value, str):
                            keys.add(k.value)
                # var_name["key"] = ...
                elif (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == var_name
                    and isinstance(target.slice, ast.Constant)
                    and isinstance(target.slice.value, str)
                ):
                    keys.add(target.slice.value)
    assert found_literal, (
        f"No dict-literal assignment to {var_name!r} found in "
        f"{func.__qualname__} — the write site moved; update this guard."
    )
    return keys


class TestWriterAttrDrift:
    """The frozensets are the preflight's contract with the real write
    sites. If a writer gains an attr the preflight doesn't know, the
    week_starting class returns. These tests fail on drift."""

    def test_entry_attrs_match_dm_advance_write_site(self):
        import workflows.daily_check as dc

        # The attrs_to_update literal in run_dm_sequencing is the
        # single construction site for DM-advance entry writes.
        written = _assigned_dict_keys(dc.run_dm_sequencing, "attrs_to_update")
        assert written == DM_ENTRY_WRITER_ATTRS, (
            f"DM_ENTRY_WRITER_ATTRS is out of sync with run_dm_sequencing. "
            f"In source: {written}, in frozenset: {DM_ENTRY_WRITER_ATTRS}"
        )

    def test_company_attrs_match_tally_write_site(self):
        import inspect
        import re

        import workflows.daily_check as dc

        src = inspect.getsource(dc._write_company_throttle_tally)
        assigned = set(re.findall(r'"(last_outreach_[a-z_]+)"', src))
        assert assigned == DM_COMPANY_WRITER_ATTRS, (
            f"DM_COMPANY_WRITER_ATTRS is out of sync with "
            f"_write_company_throttle_tally. "
            f"In source: {assigned}, in frozenset: {DM_COMPANY_WRITER_ATTRS}"
        )

    def test_sweep_repair_attrs_covered_by_preflight(self):
        """The consistency sweep's repair write is the third entry-write
        site validated by the same preflight; an attr added to the
        repair alone would resurrect the week_starting class there."""
        import workflows.consistency_sweep as cs

        # Scope to _check_one_company — that's the only function that
        # assembles intended_attrs for a live repair write. The ast
        # extraction only counts assignments TO intended_attrs, so the
        # `"intended_attrs": intended_attrs` item in the divergence
        # payload dict (and the module-level sentinel in
        # _invariant_violation_payload) cannot pollute the result.
        assigned = _assigned_dict_keys(cs._check_one_company, "intended_attrs")
        assert assigned <= DM_ENTRY_WRITER_ATTRS, (
            f"consistency_sweep writes attrs not in DM_ENTRY_WRITER_ATTRS: "
            f"{assigned - DM_ENTRY_WRITER_ATTRS}"
        )
        # Verify all four expected repair keys are captured.
        expected = {"dm_step", "stage", "last_contact_date", "next_eligible_send_date"}
        assert expected <= assigned, (
            f"ast extraction missed expected repair keys: "
            f"{expected - assigned}. The write site changed shape; "
            f"update _assigned_dict_keys."
        )


def test_run_dm_sequencing_aborts_before_any_pb_launch(monkeypatch):
    """Preflight is the FIRST thing run_dm_sequencing does — a broken
    schema must abort before queue selection or any PB call."""
    from unittest.mock import MagicMock

    import workflows.daily_check as dc

    attio = MagicMock()
    attio.get_list_attributes.return_value = []   # everything missing
    attio.get_object_attributes.return_value = []
    pb = MagicMock()
    daily_run = MagicMock()
    daily_run.get_reply_detection_status.return_value = "ok"
    monkeypatch.setenv("ATTIO_LIST_ID", "lst")

    with pytest.raises(click.ClickException):
        dc.run_dm_sequencing(
            attio, pb, "ms", daily_run, dry_run=False, auto_confirm=True
        )
    pb.launch_agent.assert_not_called()
    attio.query_list_entries.assert_not_called()
