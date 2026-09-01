"""Three-arm experiment-registry pre-flight (2026-08-23 silent-failure review).

Covers ``cli._experiment_registry_preflight``, the helper shared by the
``daily``, ``send-dms``, and ``weekly`` commands:

- arm (a): MultipleRunningExperimentsError → SystemExit(1), ABORT on stderr
  (unchanged behaviour, previously duplicated inline per command).
- arm (b): any OTHER registry failure (malformed/unreadable experiments.tsv,
  dead registry store) → SystemExit(1) with an actionable ABORT. Previously
  these were swallowed with a stderr note and the run proceeded — but the
  registry is re-read on every prospect commit
  (workflows.weekly_prospect._build_prospect_entry_attrs), so a persistent
  failure recurred MID-BATCH, after PhantomBuster spend and partial commits.
- arm (c): clean return (an experiment_id or None, i.e. registry absent or
  no experiment running) → proceed silently.
"""

from unittest.mock import patch

import pytest

from models.experiment import MultipleRunningExperimentsError

# ============================================================
# 1. Helper unit tests — the three arms
# ============================================================


class TestPreflightHelperArms:
    def test_arm_a_multiple_running_aborts(self, capsys):
        """Arm (a): multi-running ambiguity → exit 1, ids in the message."""
        from cli import _experiment_registry_preflight

        with patch(
            "models.experiment.get_current_experiment_id",
            side_effect=MultipleRunningExperimentsError(("exp-1", "exp-2")),
        ), pytest.raises(SystemExit) as excinfo:
            _experiment_registry_preflight("daily")

        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "ABORT" in err
        assert "exp-1" in err and "exp-2" in err
        assert "Close one experiment" in err

    @pytest.mark.parametrize(
        "exc",
        [
            OSError("Permission denied: experiments.tsv"),
            ValueError("could not convert string to float: 'garbage'"),
            KeyError("experiment_id"),
        ],
        ids=["unreadable-file", "malformed-row", "missing-column"],
    )
    def test_arm_b_registry_failure_aborts(self, capsys, exc):
        """Arm (b): unreadable/malformed registry → exit 1, actionable message.

        Regression guard: these used to be swallowed ("experiment check
        skipped") and the run proceeded into a mid-batch failure.
        """
        from cli import _experiment_registry_preflight

        with patch(
            "models.experiment.get_current_experiment_id",
            side_effect=exc,
        ), pytest.raises(SystemExit) as excinfo:
            _experiment_registry_preflight("weekly")

        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "ABORT" in err
        assert "unreadable" in err
        assert type(exc).__name__ in err  # operator can triage the cause
        assert "skipped" not in err  # the old swallow-and-proceed wording

    @pytest.mark.parametrize("exp_id", [None, "exp-active"])
    def test_arm_c_clean_return_proceeds(self, capsys, exp_id):
        """Arm (c): absent registry (None) or one running id → no exit, no noise."""
        from cli import _experiment_registry_preflight

        with patch(
            "models.experiment.get_current_experiment_id",
            return_value=exp_id,
        ):
            _experiment_registry_preflight("send-dms")  # must not raise

        captured = capsys.readouterr()
        assert "ABORT" not in captured.err
        assert captured.out == ""

    def test_command_label_appears_in_message(self, capsys):
        """The abort names the command so cron logs are attributable."""
        from cli import _experiment_registry_preflight

        for command in ("daily", "send-dms", "weekly"):
            with patch(
                "models.experiment.get_current_experiment_id",
                side_effect=OSError("boom"),
            ), pytest.raises(SystemExit):
                _experiment_registry_preflight(command)
            assert f"cannot run {command}" in capsys.readouterr().err


# ============================================================
# 2. CLI wiring — each command aborts on arm (b) before doing work
# ============================================================


@pytest.mark.parametrize(
    "argv",
    [
        ["daily"],
        ["send-dms"],
        ["weekly", "--search-export-id", "fake-id"],
    ],
    ids=["daily", "send-dms", "weekly"],
)
def test_cli_commands_abort_on_registry_failure(argv):
    """A persistent registry failure aborts each command pre-lock, on stderr."""
    from click.testing import CliRunner

    from cli import cli

    runner = CliRunner()
    with patch(
        "models.experiment.get_current_experiment_id",
        side_effect=OSError("Permission denied: experiments.tsv"),
    ):
        result = runner.invoke(cli, argv, catch_exceptions=False)

    assert result.exit_code == 1
    assert b"ABORT" not in result.stdout_bytes  # not stdout
    assert "ABORT" in result.stderr             # stderr
    assert "unreadable" in result.stderr
