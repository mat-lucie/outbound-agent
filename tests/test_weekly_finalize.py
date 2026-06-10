"""Tests for cli.py weekly-finalize command."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from cli import cli


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _make_staged_entry(linkedin_url: str) -> dict:
    return {
        "linkedin_url": linkedin_url,
        "prospect_data": {
            "name": "Test User",
            "title": "Plant Manager",
            "company": "Big Mfg",
            "location": "Mexico City, Mexico",
            "linkedin_url": linkedin_url,
        },
        "raw_csv_row": {
            "fullName": "Test User",
            "companyName": "Big Mfg",
            "defaultProfileUrl": linkedin_url,
        },
        "persona": "operations_leaders",
        "language": "es",
        "score": 55,
        "qualification_prompt": {"system": "sys", "user": "usr"},
    }


def _pass_verdict(linkedin_url: str) -> dict:
    return {"linkedin_url": linkedin_url, "pass": True, "icp_lane": 1, "rationale": "Enterprise LATAM plant director"}


def _fail_verdict(linkedin_url: str) -> dict:
    return {"linkedin_url": linkedin_url, "pass": False, "icp_lane": 2, "rationale": "Consultant role"}


class TestWeeklyFinalize:
    BATCH = "2026-04-19"

    def _run(self, tmp_path: Path, staged: list[dict], verdicts: list[dict], extra_args: list | None = None) -> object:
        borderline = tmp_path / "exports" / f"weekly_borderline_{self.BATCH}.jsonl"
        verdicts_path = tmp_path / "exports" / f"weekly_verdicts_{self.BATCH}.jsonl"
        _write_jsonl(borderline, staged)
        _write_jsonl(verdicts_path, verdicts)

        runner = CliRunner()
        args = ["weekly-finalize", "--batch", self.BATCH] + (extra_args or [])

        mock_attio = MagicMock()
        mock_attio.__enter__ = MagicMock(return_value=mock_attio)
        mock_attio.__exit__ = MagicMock(return_value=False)
        mock_attio.upsert_person.return_value = {"id": {"record_id": "rec-abc"}}

        with (
            patch("cli.os.environ.get", side_effect=lambda k, d="": "" if k == "ATTIO_LIST_ID" else d),
            patch("workflows.weekly_prospect.AttioClient", return_value=mock_attio),
            patch("workflows.weekly_prospect.match_or_create_company", return_value="co-123"),
            patch("workflows.weekly_prospect.extract_real_domain", return_value="bigmfg.com"),
        ):
            # Run from tmp_path so file resolution works
            result = runner.invoke(cli, args, catch_exceptions=False, env={"ATTIO_LIST_ID": "list-123"})

        return result, mock_attio

    def _invoke_finalize(self, tmp_path: Path, staged: list[dict], verdicts: list[dict]) -> tuple:
        """Helper: write JSONL files and invoke weekly-finalize in an isolated filesystem."""
        import shutil

        borderline_src = tmp_path / f"weekly_borderline_{self.BATCH}.jsonl"
        verdicts_src = tmp_path / f"weekly_verdicts_{self.BATCH}.jsonl"
        _write_jsonl(borderline_src, staged)
        _write_jsonl(verdicts_src, verdicts)

        runner = CliRunner()
        mock_attio = MagicMock()
        mock_attio.__enter__ = MagicMock(return_value=mock_attio)
        mock_attio.__exit__ = MagicMock(return_value=False)
        mock_attio.upsert_person.return_value = {"id": {"record_id": "rec-abc"}}

        with (
            patch("clients.attio.AttioClient", return_value=mock_attio),
            patch("workflows.weekly_prospect.match_or_create_company", return_value="co-123"),
            patch("workflows.weekly_prospect.extract_real_domain", return_value="bigmfg.com"),
            runner.isolated_filesystem(),
        ):
            Path("exports").mkdir(exist_ok=True)
            shutil.copy(str(borderline_src), f"exports/weekly_borderline_{self.BATCH}.jsonl")
            shutil.copy(str(verdicts_src), f"exports/weekly_verdicts_{self.BATCH}.jsonl")
            result = runner.invoke(
                cli,
                ["weekly-finalize", "--batch", self.BATCH],
                catch_exceptions=False,
                env={"ATTIO_LIST_ID": "list-123"},
            )

        return result, mock_attio

    def test_commits_pass_verdicts(self, tmp_path):
        """Two staged, two pass verdicts → both upserted and list-entry added."""
        url1 = "https://www.linkedin.com/in/user-1"
        url2 = "https://www.linkedin.com/in/user-2"
        staged = [_make_staged_entry(url1), _make_staged_entry(url2)]
        verdicts = [_pass_verdict(url1), _pass_verdict(url2)]

        result, mock_attio = self._invoke_finalize(tmp_path, staged, verdicts)

        assert result.exit_code == 0
        assert mock_attio.upsert_person.call_count == 2
        assert mock_attio.add_list_entry.call_count == 2
        assert "Passed:    2" in result.output

    def test_skips_fail_verdicts(self, tmp_path):
        """Fail verdict → no Attio call, counted under Failed."""
        url1 = "https://www.linkedin.com/in/user-fail"
        staged = [_make_staged_entry(url1)]
        verdicts = [_fail_verdict(url1)]

        result, mock_attio = self._invoke_finalize(tmp_path, staged, verdicts)

        assert result.exit_code == 0
        mock_attio.upsert_person.assert_not_called()
        mock_attio.add_list_entry.assert_not_called()
        assert "Failed:    1" in result.output
        assert "Passed:    0" in result.output

    def test_missing_verdict_is_skipped(self, tmp_path):
        """Staged prospect with no matching verdict → counted in Missing."""
        url_staged = "https://www.linkedin.com/in/user-staged"
        url_verdict = "https://www.linkedin.com/in/user-other"
        staged = [_make_staged_entry(url_staged)]
        verdicts = [_pass_verdict(url_verdict)]  # different URL — no match

        result, mock_attio = self._invoke_finalize(tmp_path, staged, verdicts)

        assert result.exit_code == 0
        assert "Missing:   1" in result.output
        assert "Passed:    0" in result.output
