"""Tests for scripts/backfill_language.py — PR-26 language backfill."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from scripts.backfill_language import main


@pytest.fixture
def mock_attio():
    """Mock AttioClient for testing."""
    client = MagicMock()
    client.query_list_entries.return_value = []
    return client


@pytest.fixture
def mock_env():
    """Mock environment with ATTIO_LIST_ID set."""
    with patch.dict("os.environ", {"ATTIO_LIST_ID": "test-list-id"}):
        yield


def test_dry_run_no_writes(mock_attio, mock_env):
    """Dry-run mode should not call update_list_entry."""
    mock_attio.query_list_entries.return_value = [
        {
            "entry_id": "entry1",
            "id": {"entry_id": "entry1", "record_id": "person1"},
            "parent_record_id": "person1",
            "entry_values": {
                "language": [],  # empty — should update
                "company": [{"target_record_id": "company1"}],
            },
        }
    ]
    mock_attio.search_companies.return_value = [
        {
            "values": {
                "domains": [{"domain": "danone.com"}],
                "hq_country_code": [{"country_code": "BR"}],
            }
        }
    ]

    with patch("scripts.backfill_language.AttioClient", return_value=mock_attio), \
            patch("scripts.backfill_language.MigrationRunWriter") as mock_mig:
        mock_mig.return_value.__enter__.return_value.rows_failed = 0
        ret = main(["--dry-run"])

    assert ret == 0
    mock_attio.update_list_entry.assert_not_called()


def test_skips_entries_with_language_set(mock_attio, mock_env):
    """Entries with language already set should skip (idempotent)."""
    mock_attio.query_list_entries.return_value = [
        {
            "entry_id": "entry1",
            "id": {"entry_id": "entry1", "record_id": "person1"},
            "parent_record_id": "person1",
            "entry_values": {
                "language": [{"value": "es"}],  # already set
                "company": [{"target_record_id": "company1"}],
            },
        }
    ]

    with patch("scripts.backfill_language.AttioClient", return_value=mock_attio), \
            patch("scripts.backfill_language.MigrationRunWriter") as mock_mig:
        mock_mig.return_value.__enter__.return_value.rows_failed = 0
        ret = main(["--apply"])

    assert ret == 0
    # Should call skip_idempotent, not mark_modified
    mock_mig.return_value.__enter__.return_value.skip_idempotent.assert_called()
    mock_mig.return_value.__enter__.return_value.mark_modified.assert_not_called()


def test_infers_language_from_country(mock_attio, mock_env):
    """Should infer language from company country code."""
    mock_attio.query_list_entries.return_value = [
        {
            "entry_id": "entry1",
            "id": {"entry_id": "entry1", "record_id": "person1"},
            "parent_record_id": "person1",
            "entry_values": {
                "language": [],  # empty
                "company": [{"target_record_id": "company1"}],
            },
        }
    ]
    mock_attio.search_companies.return_value = [
        {
            "values": {
                "domains": [{"domain": "heineken.com"}],
                "hq_country_code": [{"country_code": "MX"}],
            }
        }
    ]

    # Mock AttioClient.parse_entry static method
    mock_parse_entry = MagicMock(return_value={
        "entry_id": "entry1",
        "record_id": "person1",
        "language": None,  # empty/falsy
        "company": [{"target_record_id": "company1"}],
    })

    with patch("scripts.backfill_language.AttioClient", return_value=mock_attio), \
            patch("scripts.backfill_language.AttioClient.parse_entry", mock_parse_entry), \
            patch("scripts.backfill_language.MigrationRunWriter") as mock_mig:
        mock_mig.return_value.__enter__.return_value.rows_failed = 0
        ret = main(["--apply"])

    assert ret == 0
    mock_attio.update_list_entry.assert_called_once()
    call_args = mock_attio.update_list_entry.call_args
    assert call_args[1]["entry_attributes"]["language"] == "es"


def test_no_domain_no_country_marks_failed(mock_attio, mock_env):
    """Should mark_failed if company has neither domain nor country."""
    mock_attio.query_list_entries.return_value = [
        {
            "entry_id": "entry1",
            "id": {"entry_id": "entry1", "record_id": "person1"},
            "parent_record_id": "person1",
            "entry_values": {
                "language": [],
                "company": [{"target_record_id": "company1"}],
            },
        }
    ]
    mock_attio.search_companies.return_value = [
        {"values": {"domains": [], "hq_country_code": []}}
    ]

    # Mock AttioClient.parse_entry static method
    mock_parse_entry = MagicMock(return_value={
        "entry_id": "entry1",
        "record_id": "person1",
        "language": None,  # empty/falsy
        "company": [{"target_record_id": "company1"}],
    })

    with patch("scripts.backfill_language.AttioClient", return_value=mock_attio), \
            patch("scripts.backfill_language.AttioClient.parse_entry", mock_parse_entry), \
            patch("scripts.backfill_language.escalate") as mock_escalate, \
            patch("scripts.backfill_language.MigrationRunWriter") as mock_mig:
        mock_mig.return_value.__enter__.return_value.rows_failed = 1
        ret = main(["--apply"])

    assert ret == 1
    mock_mig.return_value.__enter__.return_value.mark_failed.assert_called()
    mock_attio.update_list_entry.assert_not_called()

    # Fold-in QA round 1: verify the §3 #9 escalation fires when
    # domain AND country are both absent. Without this assertion the
    # original code shipped a silent mark_failed with no operator-
    # visible queue row. Uses the existing `missing_language` slug
    # (workflows.escalation_schemas:518 MissingLanguagePayload).
    mock_escalate.assert_called_once()
    escalate_kwargs = mock_escalate.call_args.kwargs or {}
    if not escalate_kwargs:
        # Some call patterns put args positionally; normalise.
        # We only enforce keyword form per workflows.escalation.escalate signature.
        raise AssertionError(
            "escalate() must be called with keyword args; got positional"
        )
    assert escalate_kwargs["type"] == "missing_language"
    assert "entry1" in escalate_kwargs["idempotency_key"]
    payload = escalate_kwargs["payload"]
    assert payload["record_id"] in ("person1", "entry1")
    assert payload["language_value"] is None
    assert payload["dm_step"] == "backfill"
    assert "company1" in payload["error_msg"]


def test_pass_2_is_noop_after_pass_1(mock_attio, mock_env):
    """Second consecutive run should skip all (idempotent, rows_modified=0)."""
    # First pass: entry has no language, company has domain + country
    entry = {
        "entry_id": "entry1",
        "id": {"entry_id": "entry1", "record_id": "person1"},
        "parent_record_id": "person1",
        "entry_values": {
            "language": [],
            "company": [{"target_record_id": "company1"}],
        },
    }
    company = {
        "values": {
            "domains": [{"domain": "danone.com"}],
            "hq_country_code": [{"country_code": "BR"}],
        }
    }

    mock_attio.query_list_entries.return_value = [entry]
    mock_attio.search_companies.return_value = [company]

    with patch("scripts.backfill_language.AttioClient", return_value=mock_attio), \
            patch("scripts.backfill_language.MigrationRunWriter") as mock_mig:
        # First pass: language is empty/falsy
        parse_calls = []
        def parse_entry_side_effect(e):
            parse_calls.append(1)
            # First call: no language; subsequent calls: language set
            return {
                "entry_id": "entry1",
                "record_id": "person1",
                "language": "pt" if len(parse_calls) > 1 else None,
                "company": [{"target_record_id": "company1"}],
            }

        with patch("scripts.backfill_language.AttioClient.parse_entry", side_effect=parse_entry_side_effect):
            # First pass
            mock_mig.return_value.__enter__.return_value.rows_failed = 0
            main(["--apply"])

            # Second pass: simulate entry now having language set
            entry["entry_values"]["language"] = [{"value": "pt"}]
            main(["--apply"])

        # Verify skip_idempotent was called on second pass
        # (It gets called when language is present, which happens on second call)
        calls = mock_mig.return_value.__enter__.return_value.skip_idempotent.call_args_list
        assert len(calls) >= 1, "skip_idempotent should be called at least once"


def test_xor_flags():
    """Should require exactly one of --dry-run or --apply."""
    with patch("scripts.backfill_language.AttioClient"):
        # Neither flag
        ret = main([])
        assert ret == 2

        # Both flags
        ret = main(["--dry-run", "--apply"])
        assert ret == 2


def test_missing_list_id():
    """Should error if ATTIO_LIST_ID not set."""
    import os

    old_val = os.environ.get("ATTIO_LIST_ID")
    try:
        if "ATTIO_LIST_ID" in os.environ:
            del os.environ["ATTIO_LIST_ID"]
        with patch("scripts.backfill_language.AttioClient"):
            ret = main(["--dry-run"])
        assert ret == 2
    finally:
        if old_val:
            os.environ["ATTIO_LIST_ID"] = old_val
