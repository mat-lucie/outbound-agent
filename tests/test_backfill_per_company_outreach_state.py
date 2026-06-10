"""Tests for scripts/backfill_per_company_outreach_state.py (PR-13)."""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import MagicMock, patch


def test_migration_run_writer_signature_matches():
    """Static check: the script's MigrationRunWriter() call uses kwargs
    that actually exist in MigrationRunWriter.__init__. Catches the
    bug class where mocked-writer tests pass but a real run TypeErrors.

    Per QA-build119: the prior version of the script called the writer
    with `script_version=` + `writer_module=` keyword args, but the
    real signature uses `script_name=` + `script_version=`. Mocks
    accepted anything, so the test suite stayed green while the
    production invocation would crash.
    """
    from workflows.migration_run_writer import MigrationRunWriter

    sig = inspect.signature(MigrationRunWriter.__init__)
    valid_params = set(sig.parameters.keys())
    assert "script_name" in valid_params, (
        "MigrationRunWriter.__init__ must accept `script_name`"
    )
    assert "writer_module" not in valid_params, (
        "MigrationRunWriter.__init__ does NOT accept `writer_module` — "
        "regression check: a previous script version mistakenly used this kwarg"
    )

    # Static-grep the script for the bad kwarg.
    script_text = (
        Path(__file__).resolve().parent.parent
        / "scripts" / "backfill_per_company_outreach_state.py"
    ).read_text(encoding="utf-8")
    assert "script_name=" in script_text, (
        "script must instantiate MigrationRunWriter with script_name="
    )
    assert "writer_module=" not in script_text, (
        "script must NOT pass writer_module= (not a valid kwarg)"
    )


def test_dry_run_no_writes():
    """Dry-run mode simulates without calling attio.update_company."""
    with patch("scripts.backfill_per_company_outreach_state.AttioClient") as mock_attio_class, \
         patch("scripts.backfill_per_company_outreach_state._get_all_entries_parsed") as mock_entries, \
         patch("scripts.backfill_per_company_outreach_state.MigrationRunWriter") as mock_writer_class:

        # Setup
        mock_attio = MagicMock()
        mock_attio_class.return_value = mock_attio

        mock_attio.search_companies.return_value = [
            {"id": {"record_id": "co-1"}, "values": {"last_outreach_at": None}}
        ]

        # parse_entry returns:
        #   - record_id: person record_id (parent of the list entry)
        #   - dm_step: int (0=CONNECTION_SENT … 3=DM3)
        #   - last_contact_date: YYYY-MM-DD (date-only string, not ISO timestamp)
        # Company linkage comes from the person record's `company` ref list,
        # surfaced via bulk_fetch_persons_by_record_ids.
        mock_entries.return_value = [
            {
                "record_id": "pe-1",
                "dm_step": 1,
                "last_contact_date": "2026-05-20",
                "experiment_id": "exp-1",
            }
        ]
        mock_attio.bulk_fetch_persons_by_record_ids.return_value = {
            "pe-1": {"values": {"company": [{"target_record_id": "co-1"}]}}
        }

        mock_writer = MagicMock()
        mock_writer_class.return_value.__enter__.return_value = mock_writer
        mock_writer.rows_examined = 1
        mock_writer.rows_modified = 0
        mock_writer.rows_skipped_idempotent = 0
        mock_writer.rows_failed = 0

        # Execute
        from scripts.backfill_per_company_outreach_state import main
        with patch("sys.argv", ["prog", "--dry-run"]):
            main()

        # Assert: update_company was never called
        mock_attio.update_company.assert_not_called()

        # Assert: mark_modified was called (dry-run still tracks the change)
        mock_writer.mark_modified.assert_called_once()


def test_skips_companies_with_last_outreach_at_set():
    """Companies that already have last_outreach_at set are skipped (idempotent)."""
    with patch("scripts.backfill_per_company_outreach_state.AttioClient") as mock_attio_class, \
         patch("scripts.backfill_per_company_outreach_state._get_all_entries_parsed") as mock_entries, \
         patch("scripts.backfill_per_company_outreach_state.MigrationRunWriter") as mock_writer_class:

        mock_attio = MagicMock()
        mock_attio_class.return_value = mock_attio

        # Company already has last_outreach_at
        mock_attio.search_companies.return_value = [
            {"id": {"record_id": "co-1"}, "values": {"last_outreach_at": "2026-05-01T00:00:00Z"}}
        ]

        mock_entries.return_value = []
        mock_attio.bulk_fetch_persons_by_record_ids.return_value = {}

        mock_writer = MagicMock()
        mock_writer_class.return_value.__enter__.return_value = mock_writer
        mock_writer.rows_examined = 1
        mock_writer.rows_modified = 0
        mock_writer.rows_skipped_idempotent = 1
        mock_writer.rows_failed = 0

        from scripts.backfill_per_company_outreach_state import main
        with patch("sys.argv", ["prog", "--apply"]):
            main()

        # Assert: skip_idempotent was called
        mock_writer.skip_idempotent.assert_called_once()

        # Assert: no update_company call
        mock_attio.update_company.assert_not_called()


def test_company_with_no_dms_skipped_excluded():
    """Companies with no LinkedIn Outreach entries are marked skip_excluded."""
    with patch("scripts.backfill_per_company_outreach_state.AttioClient") as mock_attio_class, \
         patch("scripts.backfill_per_company_outreach_state._get_all_entries_parsed") as mock_entries, \
         patch("scripts.backfill_per_company_outreach_state.MigrationRunWriter") as mock_writer_class:

        mock_attio = MagicMock()
        mock_attio_class.return_value = mock_attio

        mock_attio.search_companies.return_value = [
            {"id": {"record_id": "co-orphan"}, "values": {"last_outreach_at": None}}
        ]

        # No entries for this company
        mock_entries.return_value = []
        mock_attio.bulk_fetch_persons_by_record_ids.return_value = {}

        mock_writer = MagicMock()
        mock_writer_class.return_value.__enter__.return_value = mock_writer
        mock_writer.rows_examined = 1
        mock_writer.rows_modified = 0
        mock_writer.rows_skipped_idempotent = 0
        mock_writer.rows_failed = 0

        from scripts.backfill_per_company_outreach_state import main
        with patch("sys.argv", ["prog", "--apply"]):
            main()

        # Assert: skip_excluded was called
        mock_writer.skip_excluded.assert_called_once_with(reason="no_outreach_history")

        # Assert: no update_company call
        mock_attio.update_company.assert_not_called()


def test_picks_most_advanced_entry_by_dm_step():
    """Selects the entry with highest dm_step (ties broken by last_contact_date DESC)."""
    with patch("scripts.backfill_per_company_outreach_state.AttioClient") as mock_attio_class, \
         patch("scripts.backfill_per_company_outreach_state._get_all_entries_parsed") as mock_entries, \
         patch("scripts.backfill_per_company_outreach_state.MigrationRunWriter") as mock_writer_class:

        mock_attio = MagicMock()
        mock_attio_class.return_value = mock_attio

        mock_attio.search_companies.return_value = [
            {"id": {"record_id": "co-1"}, "values": {"last_outreach_at": None}}
        ]

        # Company has 3 entries at different dm_step values. parse_entry shape:
        # `record_id` = person record_id; `dm_step` is an INT; `last_contact_date`
        # is a date-only string. Company linkage resolved via
        # bulk_fetch_persons_by_record_ids below.
        mock_entries.return_value = [
            {
                "record_id": "pe-1",
                "dm_step": 1,
                "last_contact_date": "2026-05-18",
                "experiment_id": "exp-1",
            },
            {
                "record_id": "pe-2",
                "dm_step": 3,
                "last_contact_date": "2026-05-20",
                "experiment_id": "exp-2",
            },
            {
                "record_id": "pe-3",
                "dm_step": 2,
                "last_contact_date": "2026-05-19",
                "experiment_id": "exp-3",
            },
        ]
        mock_attio.bulk_fetch_persons_by_record_ids.return_value = {
            "pe-1": {"values": {"company": [{"target_record_id": "co-1"}]}},
            "pe-2": {"values": {"company": [{"target_record_id": "co-1"}]}},
            "pe-3": {"values": {"company": [{"target_record_id": "co-1"}]}},
        }

        mock_writer = MagicMock()
        mock_writer_class.return_value.__enter__.return_value = mock_writer
        mock_writer.rows_examined = 1
        mock_writer.rows_modified = 1
        mock_writer.rows_skipped_idempotent = 0
        mock_writer.rows_failed = 0

        from scripts.backfill_per_company_outreach_state import main
        with patch("sys.argv", ["prog", "--apply"]):
            main()

        # Assert: update_company was called with the DM3 entry data, AND
        # the payload uses the Attio schema shapes (not the raw parse_entry
        # shapes). dm_step int → select-option title; last_contact_date
        # date-only → ISO timestamp at midnight UTC.
        call_args = mock_attio.update_company.call_args
        assert call_args[0][0] == "co-1"  # company_id
        payload = call_args[0][1]
        assert payload["last_outreach_step"] == "DM3", (
            f"dm_step int=3 should map to select option 'DM3'; got {payload['last_outreach_step']!r}"
        )
        assert payload["last_outreach_at"] == "2026-05-20T00:00:00Z", (
            f"date-only should promote to ISO timestamp; got {payload['last_outreach_at']!r}"
        )
        assert payload["last_outreach_person_id"][0]["target_record_id"] == "pe-2"
        assert payload["last_outreach_experiment_id"] == "exp-2"


def test_pass_2_is_noop_after_pass_1():
    """Second run sees all companies as idempotent (rows_modified=0)."""
    with patch("scripts.backfill_per_company_outreach_state.AttioClient") as mock_attio_class, \
         patch("scripts.backfill_per_company_outreach_state._get_all_entries_parsed") as mock_entries, \
         patch("scripts.backfill_per_company_outreach_state.MigrationRunWriter") as mock_writer_class:

        mock_attio = MagicMock()
        mock_attio_class.return_value = mock_attio

        # All companies now have last_outreach_at set (from first run)
        mock_attio.search_companies.return_value = [
            {"id": {"record_id": "co-1"}, "values": {"last_outreach_at": "2026-05-20T10:00:00Z"}},
            {"id": {"record_id": "co-2"}, "values": {"last_outreach_at": "2026-05-19T10:00:00Z"}},
            {"id": {"record_id": "co-3"}, "values": {"last_outreach_at": "2026-05-18T10:00:00Z"}},
        ]

        mock_entries.return_value = []
        mock_attio.bulk_fetch_persons_by_record_ids.return_value = {}

        mock_writer = MagicMock()
        mock_writer_class.return_value.__enter__.return_value = mock_writer
        mock_writer.rows_examined = 3
        mock_writer.rows_modified = 0
        mock_writer.rows_skipped_idempotent = 3
        mock_writer.rows_failed = 0

        from scripts.backfill_per_company_outreach_state import main
        with patch("sys.argv", ["prog", "--apply"]):
            main()

        # Assert: skip_idempotent was called 3 times
        assert mock_writer.skip_idempotent.call_count == 3

        # Assert: no update_company calls
        mock_attio.update_company.assert_not_called()


def test_incomplete_state_skipped_excluded():
    """Companies whose best entry has no dm_step or no last_contact_date
    are skipped rather than written with half-state. Defense against
    stamping CONNECTION_SENT on Prospect-stage entries that never sent.
    """
    with patch("scripts.backfill_per_company_outreach_state.AttioClient") as mock_attio_class, \
         patch("scripts.backfill_per_company_outreach_state._get_all_entries_parsed") as mock_entries, \
         patch("scripts.backfill_per_company_outreach_state.MigrationRunWriter") as mock_writer_class:

        mock_attio = MagicMock()
        mock_attio_class.return_value = mock_attio

        mock_attio.search_companies.return_value = [
            {"id": {"record_id": "co-1"}, "values": {"last_outreach_at": None}},
            {"id": {"record_id": "co-2"}, "values": {"last_outreach_at": None}},
        ]

        # Two incomplete-state cases: missing dm_step, and missing last_contact_date.
        mock_entries.return_value = [
            {
                "record_id": "pe-1",
                "dm_step": None,
                "last_contact_date": "2026-05-18",
                "experiment_id": "exp-1",
            },
            {
                "record_id": "pe-2",
                "dm_step": 1,
                "last_contact_date": None,
                "experiment_id": "exp-2",
            },
        ]
        mock_attio.bulk_fetch_persons_by_record_ids.return_value = {
            "pe-1": {"values": {"company": [{"target_record_id": "co-1"}]}},
            "pe-2": {"values": {"company": [{"target_record_id": "co-2"}]}},
        }

        mock_writer = MagicMock()
        mock_writer_class.return_value.__enter__.return_value = mock_writer
        mock_writer.rows_examined = 2
        mock_writer.rows_modified = 0
        mock_writer.rows_skipped_idempotent = 0
        mock_writer.rows_failed = 0

        from scripts.backfill_per_company_outreach_state import main
        with patch("sys.argv", ["prog", "--apply"]):
            main()

        # Both companies should be skipped as incomplete state — no writes.
        assert mock_writer.skip_excluded.call_count == 2
        calls = [c.kwargs for c in mock_writer.skip_excluded.call_args_list]
        assert all(c.get("reason") == "incomplete_outreach_state" for c in calls), calls
        mock_attio.update_company.assert_not_called()


def test_company_missing_record_id_skipped_excluded():
    """A company whose id object has no record_id sub-key is skipped with a
    distinct reason — not silently bucketed as 'no_outreach_history'.
    """
    with patch("scripts.backfill_per_company_outreach_state.AttioClient") as mock_attio_class, \
         patch("scripts.backfill_per_company_outreach_state._get_all_entries_parsed") as mock_entries, \
         patch("scripts.backfill_per_company_outreach_state.MigrationRunWriter") as mock_writer_class:

        mock_attio = MagicMock()
        mock_attio_class.return_value = mock_attio
        # Malformed id object — record_id absent.
        mock_attio.search_companies.return_value = [
            {"id": {"workspace_id": "ws-1"}, "values": {"last_outreach_at": None}}
        ]
        mock_entries.return_value = []
        mock_attio.bulk_fetch_persons_by_record_ids.return_value = {}

        mock_writer = MagicMock()
        mock_writer_class.return_value.__enter__.return_value = mock_writer

        from scripts.backfill_per_company_outreach_state import main
        with patch("sys.argv", ["prog", "--apply"]):
            main()

        # Distinct skip reason, not no_outreach_history.
        mock_writer.skip_excluded.assert_called_once_with(reason="missing_record_id")
        mock_attio.update_company.assert_not_called()


def test_dm_step_out_of_range_marked_failed():
    """A dm_step int outside {0,1,2,3} is marked_failed with an explicit error,
    NOT bucketed into the incomplete_outreach_state skip. Defends against a
    future DM4 (or corrupt -1) silently disappearing.
    """
    with patch("scripts.backfill_per_company_outreach_state.AttioClient") as mock_attio_class, \
         patch("scripts.backfill_per_company_outreach_state._get_all_entries_parsed") as mock_entries, \
         patch("scripts.backfill_per_company_outreach_state.MigrationRunWriter") as mock_writer_class:

        mock_attio = MagicMock()
        mock_attio_class.return_value = mock_attio
        mock_attio.search_companies.return_value = [
            {"id": {"record_id": "co-1"}, "values": {"last_outreach_at": None}}
        ]
        # dm_step=4 is a valid int but not in _STEP_LABEL.
        mock_entries.return_value = [
            {
                "record_id": "pe-1",
                "dm_step": 4,
                "last_contact_date": "2026-05-20",
                "experiment_id": "exp-1",
            }
        ]
        mock_attio.bulk_fetch_persons_by_record_ids.return_value = {
            "pe-1": {"values": {"company": [{"target_record_id": "co-1"}]}}
        }

        mock_writer = MagicMock()
        mock_writer_class.return_value.__enter__.return_value = mock_writer

        from scripts.backfill_per_company_outreach_state import main
        with patch("sys.argv", ["prog", "--apply"]):
            main()

        # Out-of-range is treated as a failure, not a skip.
        mock_writer.skip_excluded.assert_not_called()
        mock_writer.mark_failed.assert_called_once()
        call_kwargs = mock_writer.mark_failed.call_args.kwargs
        assert call_kwargs["record_id"] == "co-1"
        assert "4" in call_kwargs["error"] and "out of range" in call_kwargs["error"]
        mock_attio.update_company.assert_not_called()


# ── #12 coverage gaps: tie-break, multi-company, fan-out, mode parity ─────────


def _run_backfill(mode, *, companies, entries, persons):
    """Run the backfill main() under the 3 standard patches.

    `mode` is "--dry-run" or "--apply". Returns (mock_attio, mock_writer) so
    callers can assert on update_company / the MigrationRunWriter counters.
    """
    with (
        patch("scripts.backfill_per_company_outreach_state.AttioClient") as mock_attio_class,
        patch("scripts.backfill_per_company_outreach_state._get_all_entries_parsed") as mock_entries,
        patch("scripts.backfill_per_company_outreach_state.MigrationRunWriter") as mock_writer_class,
    ):
        mock_attio = MagicMock()
        mock_attio_class.return_value = mock_attio
        mock_attio.search_companies.return_value = companies
        mock_entries.return_value = entries
        mock_attio.bulk_fetch_persons_by_record_ids.return_value = persons

        mock_writer = MagicMock()
        mock_writer_class.return_value.__enter__.return_value = mock_writer

        from scripts.backfill_per_company_outreach_state import main
        with patch("sys.argv", ["prog", mode]):
            main()
    return mock_attio, mock_writer


def test_tie_break_same_dm_step_picks_latest_contact_date():
    """When two entries share the SAME dm_step, the sort tie-breaks on
    last_contact_date DESC (the existing most-advanced test varied dm_step,
    so the tie-break path itself was uncovered)."""
    mock_attio, _ = _run_backfill(
        "--apply",
        companies=[{"id": {"record_id": "co-1"}, "values": {"last_outreach_at": None}}],
        entries=[
            {"record_id": "pe-early", "dm_step": 2, "last_contact_date": "2026-05-15", "experiment_id": "exp-early"},
            {"record_id": "pe-late", "dm_step": 2, "last_contact_date": "2026-05-22", "experiment_id": "exp-late"},
        ],
        persons={
            "pe-early": {"values": {"company": [{"target_record_id": "co-1"}]}},
            "pe-late": {"values": {"company": [{"target_record_id": "co-1"}]}},
        },
    )
    payload = mock_attio.update_company.call_args[0][1]
    assert payload["last_outreach_at"] == "2026-05-22T00:00:00Z"
    assert payload["last_outreach_person_id"][0]["target_record_id"] == "pe-late"
    assert payload["last_outreach_experiment_id"] == "exp-late"


def test_multi_company_run_writes_each_company():
    """A single run with multiple companies stamps each one from its own
    most-advanced entry (the prior multi-company tests were all-idempotent)."""
    mock_attio, _ = _run_backfill(
        "--apply",
        companies=[
            {"id": {"record_id": "co-1"}, "values": {"last_outreach_at": None}},
            {"id": {"record_id": "co-2"}, "values": {"last_outreach_at": None}},
        ],
        entries=[
            {"record_id": "pe-1", "dm_step": 1, "last_contact_date": "2026-05-10", "experiment_id": "exp-1"},
            {"record_id": "pe-2", "dm_step": 3, "last_contact_date": "2026-05-12", "experiment_id": "exp-2"},
        ],
        persons={
            "pe-1": {"values": {"company": [{"target_record_id": "co-1"}]}},
            "pe-2": {"values": {"company": [{"target_record_id": "co-2"}]}},
        },
    )
    by_company = {c.args[0]: c.args[1] for c in mock_attio.update_company.call_args_list}
    assert set(by_company) == {"co-1", "co-2"}
    assert by_company["co-1"]["last_outreach_step"] == "DM1"
    assert by_company["co-2"]["last_outreach_step"] == "DM3"
    assert by_company["co-2"]["last_outreach_person_id"][0]["target_record_id"] == "pe-2"


def test_person_linked_to_two_companies_stamps_both():
    """A person whose `company` ref list points to TWO companies contributes
    its entry to BOTH buckets (the person→company fan-out at lines 137-149)."""
    mock_attio, _ = _run_backfill(
        "--apply",
        companies=[
            {"id": {"record_id": "co-A"}, "values": {"last_outreach_at": None}},
            {"id": {"record_id": "co-B"}, "values": {"last_outreach_at": None}},
        ],
        entries=[
            {"record_id": "pe-shared", "dm_step": 2, "last_contact_date": "2026-05-20", "experiment_id": "exp-x"},
        ],
        persons={
            "pe-shared": {"values": {"company": [
                {"target_record_id": "co-A"},
                {"target_record_id": "co-B"},
            ]}},
        },
    )
    by_company = {c.args[0]: c.args[1] for c in mock_attio.update_company.call_args_list}
    assert set(by_company) == {"co-A", "co-B"}
    for payload in by_company.values():
        assert payload["last_outreach_step"] == "DM2"
        assert payload["last_outreach_person_id"][0]["target_record_id"] == "pe-shared"


def test_dry_run_and_apply_make_the_same_write_decision():
    """Dry-run and apply must agree on WHICH companies get modified (the
    payload is built once, before the mode branch). Apply writes co-1; dry-run
    records the same modification without calling update_company."""
    fixture = dict(
        companies=[{"id": {"record_id": "co-1"}, "values": {"last_outreach_at": None}}],
        entries=[
            {"record_id": "pe-1", "dm_step": 3, "last_contact_date": "2026-05-20", "experiment_id": "exp-1"},
        ],
        persons={"pe-1": {"values": {"company": [{"target_record_id": "co-1"}]}}},
    )

    apply_attio, apply_writer = _run_backfill("--apply", **fixture)
    apply_attio.update_company.assert_called_once()
    apply_writer.mark_modified.assert_called_once_with(record_id="co-1", object="companies")

    dry_attio, dry_writer = _run_backfill("--dry-run", **fixture)
    dry_attio.update_company.assert_not_called()
    dry_writer.mark_modified.assert_called_once_with(record_id="co-1", object="companies")
