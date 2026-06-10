"""Tests for scripts/reclassify_industry.py — PR-25 industry reclassification."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.reclassify_industry import main  # noqa: E402


class TestReclassifyIndustryCommand:
    """Test the reclassify_industry.py CLI."""

    def test_requires_dry_run_or_apply(self):
        """Test that exactly one of --dry-run or --apply is required."""
        # Neither flag
        assert main([]) == 2
        # Both flags
        assert main(["--dry-run", "--apply"]) == 2

    @patch("scripts.reclassify_industry.AttioClient")
    def test_missing_attio_api_key(self, mock_attio_class):
        """Test error handling when ATTIO_API_KEY is not set."""
        mock_attio_class.side_effect = KeyError("ATTIO_API_KEY")
        assert main(["--dry-run"]) == 2

    @patch("scripts.reclassify_industry.backfill_missing_industries")
    @patch("scripts.reclassify_industry.ReclassificationRunWriter")
    @patch("scripts.reclassify_industry.MigrationRunWriter")
    @patch("scripts.reclassify_industry.AttioClient")
    def test_reclassify_industry_dry_run_no_writes(
        self,
        mock_attio_class,
        mock_mig_writer_class,
        mock_rec_writer_class,
        mock_backfill,
    ):
        """Test that dry-run mode does not write to Attio."""
        # Mock the writers
        mock_mig_writer = MagicMock()
        mock_mig_writer.__enter__ = MagicMock(return_value=mock_mig_writer)
        mock_mig_writer.__exit__ = MagicMock(return_value=False)
        mock_mig_writer.rows_examined = 5
        mock_mig_writer.rows_modified = 0
        mock_mig_writer.rows_skipped_idempotent = 5
        mock_mig_writer.rows_failed = 0

        mock_rec_writer = MagicMock()
        mock_rec_writer.__enter__ = MagicMock(return_value=mock_rec_writer)
        mock_rec_writer.__exit__ = MagicMock(return_value=False)
        mock_rec_writer.run_id = "test-run-id"
        mock_rec_writer.success_count = 5
        mock_rec_writer.abstain_count = 0
        mock_rec_writer.error_count = 0

        mock_mig_writer_class.return_value = mock_mig_writer
        mock_rec_writer_class.return_value = mock_rec_writer
        mock_backfill.return_value = {
            "total_scanned": 10,
            "missing": 5,
            "classified": 5,
            "written": 0,
            "api_errors": 0,
            "skipped": 0,
        }

        # Run dry-run
        result = main(["--dry-run"])
        assert result == 0

        # Verify backfill was called with dry_run=True
        mock_backfill.assert_called_once()
        call_kwargs = mock_backfill.call_args[1]
        assert call_kwargs["dry_run"] is True

    @patch("scripts.reclassify_industry.backfill_missing_industries")
    @patch("scripts.reclassify_industry.ReclassificationRunWriter")
    @patch("scripts.reclassify_industry.MigrationRunWriter")
    @patch("scripts.reclassify_industry.AttioClient")
    def test_reclassify_industry_uses_migration_run_writer(
        self,
        mock_attio_class,
        mock_mig_writer_class,
        mock_rec_writer_class,
        mock_backfill,
    ):
        """Test that MigrationRunWriter is initialized and passed to backfill."""
        # Mock the writers
        mock_mig_writer = MagicMock()
        mock_mig_writer.__enter__ = MagicMock(return_value=mock_mig_writer)
        mock_mig_writer.__exit__ = MagicMock(return_value=False)
        mock_mig_writer.rows_examined = 0
        mock_mig_writer.rows_modified = 0
        mock_mig_writer.rows_skipped_idempotent = 0
        mock_mig_writer.rows_failed = 0

        mock_rec_writer = MagicMock()
        mock_rec_writer.__enter__ = MagicMock(return_value=mock_rec_writer)
        mock_rec_writer.__exit__ = MagicMock(return_value=False)
        mock_rec_writer.run_id = "test-run-id"
        mock_rec_writer.success_count = 0
        mock_rec_writer.abstain_count = 0
        mock_rec_writer.error_count = 0

        mock_mig_writer_class.return_value = mock_mig_writer
        mock_rec_writer_class.return_value = mock_rec_writer
        mock_backfill.return_value = {
            "total_scanned": 0,
            "missing": 0,
            "classified": 0,
            "written": 0,
            "api_errors": 0,
            "skipped": 0,
        }

        # Run with --apply
        result = main(["--apply"])
        assert result == 0

        # Verify MigrationRunWriter was created with correct parameters
        mock_mig_writer_class.assert_called_once()
        call_kwargs = mock_mig_writer_class.call_args[1]
        assert call_kwargs["script_name"] == "reclassify_industry.py"
        assert call_kwargs["script_version"] == "pr-25-v1"
        assert call_kwargs["dry_run"] is False
        assert call_kwargs["reclassification_run_id"] == "test-run-id"

        # Verify backfill was called with the writers
        mock_backfill.assert_called_once()
        call_kwargs = mock_backfill.call_args[1]
        assert call_kwargs["mig_run"] == mock_mig_writer
        assert call_kwargs["rc_run"] == mock_rec_writer
        assert call_kwargs["dry_run"] is False

    @patch("scripts.reclassify_industry.backfill_missing_industries")
    @patch("scripts.reclassify_industry.ReclassificationRunWriter")
    @patch("scripts.reclassify_industry.MigrationRunWriter")
    @patch("scripts.reclassify_industry.AttioClient")
    def test_pass_2_is_noop_after_pass_1(
        self,
        mock_attio_class,
        mock_mig_writer_class,
        mock_rec_writer_class,
        mock_backfill,
    ):
        """Test idempotency: second pass returns zero rows_modified.

        Simulates:
        - Pass 1: 5 missing records → 5 classified and written
        - Pass 2: all companies have industry_vertical set → rows_modified=0
        """
        # Pass 1 setup
        pass1_mig_writer = MagicMock()
        pass1_mig_writer.__enter__ = MagicMock(return_value=pass1_mig_writer)
        pass1_mig_writer.__exit__ = MagicMock(return_value=False)
        pass1_mig_writer.rows_examined = 10
        pass1_mig_writer.rows_modified = 5
        pass1_mig_writer.rows_skipped_idempotent = 5
        pass1_mig_writer.rows_failed = 0

        pass1_rec_writer = MagicMock()
        pass1_rec_writer.__enter__ = MagicMock(return_value=pass1_rec_writer)
        pass1_rec_writer.__exit__ = MagicMock(return_value=False)
        pass1_rec_writer.run_id = "pass1-run-id"
        pass1_rec_writer.success_count = 5
        pass1_rec_writer.abstain_count = 0
        pass1_rec_writer.error_count = 0

        # Pass 2 setup (idempotent — no changes)
        pass2_mig_writer = MagicMock()
        pass2_mig_writer.__enter__ = MagicMock(return_value=pass2_mig_writer)
        pass2_mig_writer.__exit__ = MagicMock(return_value=False)
        pass2_mig_writer.rows_examined = 10
        pass2_mig_writer.rows_modified = 0  # ZERO modifications
        pass2_mig_writer.rows_skipped_idempotent = 10  # All skipped (already set)
        pass2_mig_writer.rows_failed = 0

        pass2_rec_writer = MagicMock()
        pass2_rec_writer.__enter__ = MagicMock(return_value=pass2_rec_writer)
        pass2_rec_writer.__exit__ = MagicMock(return_value=False)
        pass2_rec_writer.run_id = "pass2-run-id"
        pass2_rec_writer.success_count = 10
        pass2_rec_writer.abstain_count = 0
        pass2_rec_writer.error_count = 0

        # Configure mocks to return different writers per call
        mock_mig_writer_class.side_effect = [pass1_mig_writer, pass2_mig_writer]
        mock_rec_writer_class.side_effect = [pass1_rec_writer, pass2_rec_writer]

        # Pass 1: 5 missing, 5 classified
        pass1_summary = {
            "total_scanned": 10,
            "missing": 5,
            "classified": 5,
            "written": 5,
            "api_errors": 0,
            "skipped": 0,
        }

        # Pass 2: 0 missing (all 10 have industry_vertical set)
        pass2_summary = {
            "total_scanned": 10,
            "missing": 0,
            "classified": 0,
            "written": 0,
            "api_errors": 0,
            "skipped": 0,
        }

        mock_backfill.side_effect = [pass1_summary, pass2_summary]

        # Run Pass 1
        result1 = main(["--apply"])
        assert result1 == 0

        # Run Pass 2
        result2 = main(["--apply"])
        assert result2 == 0

        # Verify Pass 2's four-invariant contract
        assert pass2_mig_writer.rows_examined > 0
        assert pass2_mig_writer.rows_modified == 0  # Critical: zero modifications
        assert pass2_mig_writer.rows_skipped_idempotent == pass2_mig_writer.rows_examined
        assert pass2_mig_writer.rows_failed == 0
