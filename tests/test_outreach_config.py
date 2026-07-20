"""Tests for the outreach operational-knob loader (clients/outreach_config.py).

Covers: the typed happy-path under the conftest pin (the acme reference values),
the wiring of every consumer module to the loaded config, the derived-twin
cadence invariant, a shipped-neutral guard (the public config/outreach.example.yaml
the suite otherwise repoints away from must stay schema-valid + loadable), and
the fail-loud validation paths (missing file/section, wrong type, out-of-range,
bad send-day, wrong map keys).
"""

import textwrap

import pytest

from clients.outreach_config import OutreachConfig, load_outreach_config
from clients.settings import ConfigError

# A complete, valid outreach.yaml body. Tests mutate one part to exercise a
# specific validation path.
_VALID_YAML = textwrap.dedent(
    """\
    caps:
      invites_per_day: 25
      dms_per_day: 30
      visits_per_day: 50
      invite_batch_size: 20
    cadence:
      connect_to_dm1_days: 1
      dm1_to_dm2_days: 5
      dm2_to_dm3_days: 10
      nurture_business_days:
        enterprise: 90
        target: 60
        legacy: 45
    lanes:
      rank:
        enterprise_mode: 0
        target_company_mode: 1
        legacy: 2
    schedule:
      send_days: [mon, tue, wed, thu, fri]
    throttle:
      company_window_days: 14
    """
)


def _write_config(tmp_path, monkeypatch, body: str, *, name: str = "outreach.yaml"):
    """Write ``body`` to ``tmp_path/<name>`` and point OUTBOUND_CONFIG_DIR there."""
    (tmp_path / name).write_text(body, encoding="utf-8")
    monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(tmp_path))


# ── Happy path (under the conftest examples/acme/config pin) ─────────────────


def test_load_outreach_config_returns_example_defaults():
    """Under the conftest pin, the loader returns the acme reference values —
    the exact knobs the suite asserted before externalization."""
    cfg = load_outreach_config()
    assert isinstance(cfg, OutreachConfig)
    assert cfg.invites_per_day == 25
    assert cfg.dms_per_day == 30
    assert cfg.visits_per_day == 50
    assert cfg.invite_batch_size == 20
    assert cfg.cadence_connect_to_dm1_days == 1
    assert cfg.cadence_dm1_to_dm2_days == 5
    assert cfg.cadence_dm2_to_dm3_days == 10
    assert cfg.nurture_business_days == {"enterprise": 90, "target": 60, "legacy": 45}
    assert cfg.lane_rank == {"enterprise_mode": 0, "target_company_mode": 1, "legacy": 2}
    assert cfg.send_days == frozenset({0, 1, 2, 3, 4})
    assert cfg.company_throttle_window_days == 14
    assert cfg.weekly_scrape_batch_size == 300


def test_consumers_sourced_from_loaded_config():
    """Every wired module's module-level constant matches the loaded config."""
    import models.business_calendar as bc
    import models.resolution as resolution
    import workflows.cadence as cadence
    import workflows.daily_check as daily_check
    import workflows.daily_run as daily_run
    import workflows.dm_sequencer as dm_sequencer
    import workflows.safety_limits as safety_limits
    import workflows.throttle as throttle
    from models.campaign import MessageStep

    cfg = load_outreach_config()
    assert cfg.invites_per_day == safety_limits.MAX_CONNECTIONS_PER_DAY
    assert cfg.dms_per_day == safety_limits.MAX_MESSAGES_PER_DAY
    assert cfg.visits_per_day == safety_limits.MAX_VISITS_PER_DAY
    assert cfg.invites_per_day == daily_run.MAX_CONNECTIONS_PER_DAY
    assert cfg.dms_per_day == daily_run.MAX_MESSAGES_PER_DAY
    assert cfg.visits_per_day == daily_run.MAX_VISITS_PER_DAY
    assert dm_sequencer.DM_TIMING[MessageStep.DM1] == cfg.cadence_connect_to_dm1_days
    assert dm_sequencer.DM_TIMING[MessageStep.DM2] == cfg.cadence_dm1_to_dm2_days
    assert dm_sequencer.DM_TIMING[MessageStep.DM3] == cfg.cadence_dm2_to_dm3_days
    assert cfg.nurture_business_days == cadence.CADENCE_BY_LANE
    assert cfg.company_throttle_window_days == throttle.DEFAULT_THROTTLE_WINDOW_DAYS
    assert resolution._NEXT_STEP_DELTA_DAYS["dm1"] == cfg.cadence_dm1_to_dm2_days
    assert resolution._NEXT_STEP_DELTA_DAYS["dm2"] == cfg.cadence_dm2_to_dm3_days
    assert daily_check._OUTREACH.lane_rank == cfg.lane_rank
    # business_calendar.is_send_day reflects send_days (Mon-Fri True, Sat/Sun False)
    from datetime import date

    assert bc.is_send_day(date(2026, 6, 8))  # Monday
    assert not bc.is_send_day(date(2026, 6, 13))  # Saturday


def test_derived_twin_cadence_invariant():
    """The two parallel cadence dicts derive from one config section, so the
    drift-guard invariant (resolution mirrors dm_sequencer) holds."""
    import models.resolution as resolution
    import workflows.dm_sequencer as dm_sequencer
    from models.campaign import MessageStep

    assert (
        resolution._NEXT_STEP_DELTA_DAYS["dm1"]
        == dm_sequencer.DM_TIMING[MessageStep.DM2]
    )
    assert (
        resolution._NEXT_STEP_DELTA_DAYS["dm2"]
        == dm_sequencer.DM_TIMING[MessageStep.DM3]
    )


# ── Shipped-neutral guard ────────────────────────────────────────────────────


def test_shipped_neutral_outreach_example_is_loadable(tmp_path, monkeypatch):
    """The SHIPPED repo-root config/outreach.example.yaml stays schema-valid.

    The suite resolves the acme example config via the conftest pin, so the public
    default is never otherwise exercised — without this guard a malformed
    shipped template would ship undetected. Copy the real example into an
    isolated dir (as outreach.example.yaml, so a developer's local outreach.yaml
    can't shadow it) and assert it loads with a complete schema.
    """
    import shutil
    from pathlib import Path

    import clients.outreach_config as oc_mod

    repo_root = Path(oc_mod.__file__).resolve().parent.parent
    shipped = repo_root / "config" / "outreach.example.yaml"
    shutil.copy(shipped, tmp_path / "outreach.example.yaml")
    monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(tmp_path))

    cfg = load_outreach_config()
    assert isinstance(cfg, OutreachConfig)
    assert cfg.invites_per_day >= 1
    assert cfg.invite_batch_size <= cfg.invites_per_day
    assert cfg.send_days  # non-empty
    assert set(cfg.lane_rank) == {"enterprise_mode", "target_company_mode", "legacy"}
    assert set(cfg.nurture_business_days) == {"enterprise", "target", "legacy"}


# ── Fail-loud validation ─────────────────────────────────────────────────────


def test_missing_config_raises(tmp_path, monkeypatch):
    """Neither outreach.yaml nor outreach.example.yaml present → ConfigError."""
    monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(tmp_path))
    with pytest.raises(ConfigError, match="No outreach config found"):
        load_outreach_config()


def test_missing_section_raises(tmp_path, monkeypatch):
    body = _VALID_YAML.replace("throttle:\n  company_window_days: 14\n", "")
    _write_config(tmp_path, monkeypatch, body)
    with pytest.raises(ConfigError, match="throttle"):
        load_outreach_config()


def test_wrong_type_raises(tmp_path, monkeypatch):
    body = _VALID_YAML.replace("invites_per_day: 25", 'invites_per_day: "lots"')
    _write_config(tmp_path, monkeypatch, body)
    with pytest.raises(ConfigError, match="invites_per_day"):
        load_outreach_config()


def test_bool_rejected_as_int(tmp_path, monkeypatch):
    body = _VALID_YAML.replace("invites_per_day: 25", "invites_per_day: true")
    _write_config(tmp_path, monkeypatch, body)
    with pytest.raises(ConfigError, match="invites_per_day"):
        load_outreach_config()


def test_missing_visits_per_day_defaults_to_50(tmp_path, monkeypatch):
    """visits_per_day is optional: configs written before the knob existed
    keep the historical hard cap of 50."""
    body = _VALID_YAML.replace("  visits_per_day: 50\n", "")
    _write_config(tmp_path, monkeypatch, body)
    assert load_outreach_config().visits_per_day == 50


def test_invalid_visits_per_day_raises(tmp_path, monkeypatch):
    """When the knob IS present it is validated like the other caps."""
    body = _VALID_YAML.replace("  visits_per_day: 50\n", "  visits_per_day: 0\n")
    _write_config(tmp_path, monkeypatch, body)
    with pytest.raises(ConfigError, match="visits_per_day"):
        load_outreach_config()


def test_non_positive_int_raises(tmp_path, monkeypatch):
    body = _VALID_YAML.replace("dms_per_day: 30", "dms_per_day: 0")
    _write_config(tmp_path, monkeypatch, body)
    with pytest.raises(ConfigError, match="dms_per_day"):
        load_outreach_config()


def test_batch_size_exceeding_cap_raises(tmp_path, monkeypatch):
    body = _VALID_YAML.replace("invite_batch_size: 20", "invite_batch_size: 99")
    _write_config(tmp_path, monkeypatch, body)
    with pytest.raises(ConfigError, match="invite_batch_size"):
        load_outreach_config()


def test_bad_send_day_name_raises(tmp_path, monkeypatch):
    body = _VALID_YAML.replace(
        "send_days: [mon, tue, wed, thu, fri]", "send_days: [mon, funday]"
    )
    _write_config(tmp_path, monkeypatch, body)
    with pytest.raises(ConfigError, match="send_days"):
        load_outreach_config()


def test_empty_send_days_raises(tmp_path, monkeypatch):
    body = _VALID_YAML.replace("send_days: [mon, tue, wed, thu, fri]", "send_days: []")
    _write_config(tmp_path, monkeypatch, body)
    with pytest.raises(ConfigError, match="send_days"):
        load_outreach_config()


def test_wrong_lane_keys_raises(tmp_path, monkeypatch):
    body = _VALID_YAML.replace(
        "    enterprise_mode: 0\n", "    enterprise_mode: 0\n    surprise_lane: 9\n"
    )
    _write_config(tmp_path, monkeypatch, body)
    with pytest.raises(ConfigError, match="rank"):
        load_outreach_config()


def test_missing_nurture_lane_key_raises(tmp_path, monkeypatch):
    body = _VALID_YAML.replace("    legacy: 45\n", "")
    _write_config(tmp_path, monkeypatch, body)
    with pytest.raises(ConfigError, match="nurture_business_days"):
        load_outreach_config()


def test_negative_lane_rank_raises(tmp_path, monkeypatch):
    body = _VALID_YAML.replace("legacy: 2", "legacy: -1")
    _write_config(tmp_path, monkeypatch, body)
    with pytest.raises(ConfigError, match="rank"):
        load_outreach_config()


# ── Optional-with-default knobs: staleness + sweep ──────────────────────────


def test_optional_knobs_absent_default(tmp_path, monkeypatch):
    """With no staleness/sweep section the loader uses the documented defaults."""
    _write_config(tmp_path, monkeypatch, _VALID_YAML)
    cfg = load_outreach_config()
    assert cfg.stale_run_takeover_hours == 3
    assert cfg.sweep_window_days == 3
    assert cfg.sweep_repair_circuit_threshold == 3
    assert cfg.weekly_scrape_batch_size == 300  # PR-252 default deep window


def test_scrape_batch_size_override(tmp_path, monkeypatch):
    """A scrape.weekly_batch_size override is loaded (PR-252)."""
    body = _VALID_YAML + "scrape:\n  weekly_batch_size: 150\n"
    _write_config(tmp_path, monkeypatch, body)
    assert load_outreach_config().weekly_scrape_batch_size == 150


@pytest.mark.parametrize(
    "section_block, match",
    [
        ("staleness:\n  stale_run_takeover_hours: 0\n", "stale_run_takeover_hours"),
        ("staleness:\n  stale_run_takeover_hours: true\n", "stale_run_takeover_hours"),
        ('staleness:\n  stale_run_takeover_hours: "lots"\n', "stale_run_takeover_hours"),
        ("sweep:\n  window_days: 0\n", "window_days"),
        ("sweep:\n  window_days: true\n", "window_days"),
        ('sweep:\n  window_days: "lots"\n', "window_days"),
        ("sweep:\n  repair_circuit_threshold: 0\n", "repair_circuit_threshold"),
        ("sweep:\n  repair_circuit_threshold: true\n", "repair_circuit_threshold"),
        ('sweep:\n  repair_circuit_threshold: "lots"\n', "repair_circuit_threshold"),
        ("scrape:\n  weekly_batch_size: 0\n", "weekly_batch_size"),
        ("scrape:\n  weekly_batch_size: true\n", "weekly_batch_size"),
        ('scrape:\n  weekly_batch_size: "lots"\n', "weekly_batch_size"),
    ],
)
def test_optional_knob_invalid_value_raises(tmp_path, monkeypatch, section_block, match):
    """zero / bool / string values for each optional knob raise ConfigError."""
    body = _VALID_YAML + section_block
    _write_config(tmp_path, monkeypatch, body)
    with pytest.raises(ConfigError, match=match):
        load_outreach_config()


def test_sweep_window_exceeding_throttle_window_raises(tmp_path, monkeypatch):
    """sweep.window_days > throttle.company_window_days → ConfigError (a stamp
    could be overwritten before the sweep verifies it)."""
    body = _VALID_YAML + "sweep:\n  window_days: 15\n"  # throttle window is 14
    _write_config(tmp_path, monkeypatch, body)
    with pytest.raises(ConfigError, match="window_days"):
        load_outreach_config()
