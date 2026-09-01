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
        # PR-207: the finalize path pre-fetches the list once for the re-stamp
        # guard; these are net-new commits, so the list reports no entries.
        mock_attio.query_list_entries.return_value = []

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
        # PR-207: the finalize path pre-fetches the list once for the re-stamp
        # guard; these are net-new commits, so the list reports no entries.
        mock_attio.query_list_entries.return_value = []

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


class TestWeeklyFinalizeNetNewAccounting:
    """Net-new vs already-listed split in the finalize summary (PR-248).

    `_commit_prospect` returns True for re-stamp-guard skips, so
    "Passed: N (committed)" said nothing about net-new supply — a run can
    print "309 committed" while the pipeline list grows by ~20. These tests
    pin the summary split, the per-lane breakdown, and the recycling warning.
    """

    BATCH = "2026-04-19"

    def _invoke_with_listed(
        self,
        tmp_path: Path,
        staged: list[dict],
        verdicts: list[dict],
        listed_record_ids: set[str] | None = None,
    ) -> tuple:
        """Like _invoke_finalize, but per-URL record ids and a configurable
        set of record ids the re-stamp guard reports as already in the list."""
        import shutil

        borderline_src = tmp_path / f"weekly_borderline_{self.BATCH}.jsonl"
        verdicts_src = tmp_path / f"weekly_verdicts_{self.BATCH}.jsonl"
        _write_jsonl(borderline_src, staged)
        _write_jsonl(verdicts_src, verdicts)

        listed = listed_record_ids or set()

        runner = CliRunner()
        mock_attio = MagicMock()
        mock_attio.__enter__ = MagicMock(return_value=mock_attio)
        mock_attio.__exit__ = MagicMock(return_value=False)
        # Record id derived from the linkedin slug so each staged entry is
        # distinguishable to the guard.
        mock_attio.upsert_person.side_effect = lambda matching_attribute, attributes: {
            "id": {"record_id": "rec-" + attributes["linkedin"].rsplit("/", 1)[-1]}
        }
        # The CLI pre-fetches this once and hands it to _commit_prospect as
        # `existing_entries`; the fork's guard matches on Entry.record_id, so
        # raw entries with a parent_record_id are all the normalizer needs.
        mock_attio.query_list_entries.return_value = [
            {
                "id": {"entry_id": f"ent-{rid}"},
                "parent_record_id": rid,
                "entry_values": {},
            }
            for rid in sorted(listed)
        ]

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

    def test_already_listed_counts_as_restamped_not_net_new(self, tmp_path):
        """One net-new + one already-listed → summary splits them; only the
        net-new prospect gets a list entry."""
        url_new = "https://www.linkedin.com/in/user-new"
        url_listed = "https://www.linkedin.com/in/user-listed"
        staged = [_make_staged_entry(url_new), _make_staged_entry(url_listed)]
        verdicts = [_pass_verdict(url_new), _pass_verdict(url_listed)]

        result, mock_attio = self._invoke_with_listed(
            tmp_path, staged, verdicts, listed_record_ids={"rec-user-listed"}
        )

        assert result.exit_code == 0
        assert "Passed:    2" in result.output
        assert "net-new pipeline entries:                 1" in result.output
        assert "already in pipeline (skipped, no write):  1" in result.output
        # The already-listed prospect must not be re-stamped.
        assert mock_attio.add_list_entry.call_count == 1

    def test_per_lane_net_new_split(self, tmp_path):
        """Lanes are tallied separately: one lane net-new, the other already
        listed."""
        url_a = "https://www.linkedin.com/in/user-a"
        url_b = "https://www.linkedin.com/in/user-b"
        entry_a = _make_staged_entry(url_a)
        entry_a["scoring_lane"] = "target_company_mode"
        entry_b = _make_staged_entry(url_b)
        entry_b["scoring_lane"] = "enterprise_mode"

        result, _ = self._invoke_with_listed(
            tmp_path,
            [entry_a, entry_b],
            [_pass_verdict(url_a), _pass_verdict(url_b)],
            listed_record_ids={"rec-user-b"},
        )

        assert result.exit_code == 0
        assert "target_company_mode: 1 net-new / 0 already-listed" in result.output
        assert "enterprise_mode: 0 net-new / 1 already-listed" in result.output

    def test_supply_warning_fires_when_majority_already_listed(self, tmp_path):
        """2 of 3 passes already listed → recycling warning printed."""
        urls = [f"https://www.linkedin.com/in/user-{i}" for i in range(3)]
        staged = [_make_staged_entry(u) for u in urls]
        verdicts = [_pass_verdict(u) for u in urls]

        result, _ = self._invoke_with_listed(
            tmp_path, staged, verdicts,
            listed_record_ids={"rec-user-1", "rec-user-2"},
        )

        assert result.exit_code == 0
        assert "SUPPLY WARNING" in result.output

    def test_no_supply_warning_when_mostly_net_new(self, tmp_path):
        """All passes net-new → no recycling warning."""
        urls = [f"https://www.linkedin.com/in/user-{i}" for i in range(3)]
        staged = [_make_staged_entry(u) for u in urls]
        verdicts = [_pass_verdict(u) for u in urls]

        result, _ = self._invoke_with_listed(tmp_path, staged, verdicts)

        assert result.exit_code == 0
        assert "SUPPLY WARNING" not in result.output
