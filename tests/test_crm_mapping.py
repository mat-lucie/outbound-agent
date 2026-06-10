"""Tests for the CRM field/stage mapping (``clients.crm.mapping``) and its
wiring through the Attio adapter.

Four concerns are pinned:

* **GOLDEN / identity** — under :func:`default_attio_mapping`, the adapter's
  stage normalization is byte-identical to the pre-mapping behavior for EVERY
  ``PipelineStage`` value plus the unknown-label and empty-string cases. This is
  the behavior-preservation proof.
* **RELABEL** — a non-identity mapping translates vendor label → canonical on
  read (with the canonical rank) and canonical → vendor label on write.
* **LOADER fail-loud** — every malformed config section raises ``ConfigError``.
* **LOADER defaults** — absent file → identity; partial ``stage_mapping`` →
  named stages remapped, the rest identity + ``STAGE_RANK``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from clients.attio import AttioClient
from clients.crm import AttioProvider
from clients.crm.base import Stage
from clients.crm.mapping import (
    CRMMapping,
    default_attio_mapping,
    load_crm_mapping,
)
from clients.settings import ConfigError
from models.pipeline import STAGE_RANK, PipelineStage

if TYPE_CHECKING:
    from pathlib import Path


def _provider(mapping: CRMMapping | None = None) -> tuple[AttioProvider, MagicMock]:
    inner = MagicMock(spec=AttioClient)
    return AttioProvider(attio_client=inner, mapping=mapping), inner


def _entry_raw(stage_label: str, entry_id: str = "e-1") -> dict:
    """A minimal raw Attio list-entry carrying ``stage_label`` as its stage."""
    return {
        "id": {"entry_id": entry_id},
        "parent_record_id": "rec-1",
        "entry_values": {"stage": [{"status": {"title": stage_label}}]},
    }


# ── GOLDEN: default mapping is byte-identical to pre-mapping behavior ─────────


def _legacy_stage_from_name(stage_name: str) -> Stage:
    """The EXACT pre-mapping ``_stage_from_name`` logic, reproduced here as the
    golden oracle the default-mapping path must match byte-for-byte."""
    try:
        rank: int | None = STAGE_RANK[PipelineStage(stage_name)]
    except (KeyError, ValueError):
        rank = None
    return Stage(name=stage_name, rank=rank)


class TestDefaultMappingByteIdentity:
    @pytest.mark.parametrize("stage", list(PipelineStage), ids=lambda s: s.name)
    def test_every_known_stage_matches_legacy(self, stage: PipelineStage) -> None:
        provider, _ = _provider()  # default identity mapping
        produced = provider._stage_from_name(stage.value)
        assert produced == _legacy_stage_from_name(stage.value)
        # Spell it out: name is the canonical value, rank is the STAGE_RANK.
        assert produced.name == stage.value
        assert produced.rank == STAGE_RANK[stage]

    def test_unknown_label_matches_legacy(self) -> None:
        provider, _ = _provider()
        produced = provider._stage_from_name("Totally Made Up")
        assert produced == _legacy_stage_from_name("Totally Made Up")
        assert produced == Stage(name="Totally Made Up", rank=None)

    def test_empty_string_matches_legacy(self) -> None:
        provider, _ = _provider()
        produced = provider._stage_from_name("")
        assert produced == _legacy_stage_from_name("")
        assert produced == Stage(name="", rank=None)

    def test_to_entry_default_matches_legacy_for_every_stage(self) -> None:
        provider, _ = _provider()
        for stage in PipelineStage:
            entry = provider._to_entry(_entry_raw(stage.value))
            assert entry.stage == _legacy_stage_from_name(stage.value)

    def test_write_passthrough_under_default(self) -> None:
        """add_list_entry / update_list_entry pass the canonical stage straight
        through under the identity default (no translation, byte-identical)."""
        provider, inner = _provider()
        inner.add_list_entry.return_value = _entry_raw("DM1 Sent")
        provider.add_list_entry(record_id="rec-1", stage_name="DM1 Sent")
        assert inner.add_list_entry.call_args.kwargs["stage_name"] == "DM1 Sent"

        inner.update_list_entry.return_value = _entry_raw("DM2 Sent")
        provider.update_list_entry(
            entry_id="e-1", entry_attributes={"stage": "DM2 Sent", "dm_step": 2}
        )
        passed = inner.update_list_entry.call_args.kwargs["entry_attributes"]
        assert passed == {"stage": "DM2 Sent", "dm_step": 2}


# ── RELABEL: a non-identity mapping round-trips ──────────────────────────────


def _relabel_mapping() -> CRMMapping:
    """A mapping where a few stages carry custom vendor labels; all others stay
    identity. Built by hand to mirror what :func:`load_crm_mapping` would yield
    for a partial ``stage_mapping``."""
    canonical_to_vendor = {s.value: s.value for s in PipelineStage}
    stage_rank = {s.value: STAGE_RANK[s] for s in PipelineStage}
    overrides = {
        PipelineStage.CONNECTION_SENT: ("Invite sent", 1),
        PipelineStage.DM1_SENT: ("First DM", 2),
    }
    for stage, (label, rank) in overrides.items():
        canonical_to_vendor[stage.value] = label
        stage_rank[stage.value] = rank
    vendor_to_canonical = {v: k for k, v in canonical_to_vendor.items()}
    return CRMMapping(
        field_mapping={"linkedin_url": "linkedin"},
        stage_field="stage",
        canonical_to_vendor_stage=canonical_to_vendor,
        vendor_to_canonical_stage=vendor_to_canonical,
        stage_rank=stage_rank,
    )


class TestRelabelRoundTrip:
    def test_read_vendor_label_normalizes_to_canonical(self) -> None:
        provider, inner = _provider(_relabel_mapping())
        inner.query_list_entries.return_value = [_entry_raw("Invite sent")]
        entry = provider.query_list_entries()[0]
        # Vendor "Invite sent" → engine-canonical "Connection Sent", rank 1.
        assert entry.stage == Stage(name="Connection Sent", rank=1)

    def test_read_identity_stage_still_passes_through(self) -> None:
        provider, inner = _provider(_relabel_mapping())
        inner.query_list_entries.return_value = [_entry_raw("Accepted")]
        entry = provider.query_list_entries()[0]
        assert entry.stage == Stage(name="Accepted", rank=STAGE_RANK[PipelineStage.ACCEPTED])

    def test_read_unknown_label_passes_through_rank_none(self) -> None:
        provider, inner = _provider(_relabel_mapping())
        inner.query_list_entries.return_value = [_entry_raw("Mystery")]
        entry = provider.query_list_entries()[0]
        assert entry.stage == Stage(name="Mystery", rank=None)

    def test_add_list_entry_translates_canonical_to_vendor(self) -> None:
        provider, inner = _provider(_relabel_mapping())
        inner.add_list_entry.return_value = _entry_raw("Invite sent")
        provider.add_list_entry(record_id="rec-1", stage_name="Connection Sent")
        # The engine's canonical "Connection Sent" reaches the inner client as
        # the vendor label "Invite sent".
        assert inner.add_list_entry.call_args.kwargs["stage_name"] == "Invite sent"

    def test_update_list_entry_translates_stage_value(self) -> None:
        provider, inner = _provider(_relabel_mapping())
        inner.update_list_entry.return_value = _entry_raw("First DM")
        caller_attrs = {"stage": "DM1 Sent", "dm_step": 1}
        provider.update_list_entry(entry_id="e-1", entry_attributes=caller_attrs)
        passed = inner.update_list_entry.call_args.kwargs["entry_attributes"]
        assert passed == {"stage": "First DM", "dm_step": 1}
        # The caller's dict is not mutated (defensive copy).
        assert caller_attrs == {"stage": "DM1 Sent", "dm_step": 1}

    def test_update_list_entry_without_stage_key_untouched(self) -> None:
        provider, inner = _provider(_relabel_mapping())
        inner.update_list_entry.return_value = _entry_raw("Accepted")
        provider.update_list_entry(entry_id="e-1", entry_attributes={"dm_step": 2})
        passed = inner.update_list_entry.call_args.kwargs["entry_attributes"]
        assert passed == {"dm_step": 2}


# ── LOADER: fail-loud on malformed config ────────────────────────────────────


def _write_crm_yaml(tmp_path: Path, body: str) -> None:
    (tmp_path / "crm.yaml").write_text(body)


class TestLoaderFailLoud:
    def test_bad_pipeline_stage_name(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(tmp_path))
        _write_crm_yaml(
            tmp_path,
            "stage_mapping:\n  NOT_A_STAGE:\n    name: X\n    rank: 1\n",
        )
        with pytest.raises(ConfigError, match="NOT_A_STAGE"):
            load_crm_mapping()

    def test_missing_rank(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(tmp_path))
        _write_crm_yaml(
            tmp_path, "stage_mapping:\n  CONNECTION_SENT:\n    name: Invite sent\n"
        )
        with pytest.raises(ConfigError, match="rank"):
            load_crm_mapping()

    def test_non_int_rank(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(tmp_path))
        _write_crm_yaml(
            tmp_path,
            "stage_mapping:\n  CONNECTION_SENT:\n    name: Invite sent\n"
            '    rank: "one"\n',
        )
        with pytest.raises(ConfigError, match="rank"):
            load_crm_mapping()

    def test_negative_rank(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(tmp_path))
        _write_crm_yaml(
            tmp_path,
            "stage_mapping:\n  CONNECTION_SENT:\n    name: Invite sent\n"
            "    rank: -5\n",
        )
        with pytest.raises(ConfigError, match="rank"):
            load_crm_mapping()

    def test_missing_name(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(tmp_path))
        _write_crm_yaml(
            tmp_path, "stage_mapping:\n  CONNECTION_SENT:\n    rank: 1\n"
        )
        with pytest.raises(ConfigError, match="name"):
            load_crm_mapping()

    def test_duplicate_vendor_label(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(tmp_path))
        # CONNECTION_SENT and ACCEPTED both map to "Same Label" → ambiguous
        # reverse lookup.
        _write_crm_yaml(
            tmp_path,
            "stage_mapping:\n"
            "  CONNECTION_SENT:\n    name: Same Label\n    rank: 1\n"
            "  ACCEPTED:\n    name: Same Label\n    rank: 2\n",
        )
        with pytest.raises(ConfigError, match="same vendor label"):
            load_crm_mapping()

    def test_field_mapping_non_str_value(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(tmp_path))
        _write_crm_yaml(tmp_path, "field_mapping:\n  linkedin_url: 42\n")
        with pytest.raises(ConfigError, match="field_mapping"):
            load_crm_mapping()

    def test_empty_stage_field(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(tmp_path))
        _write_crm_yaml(tmp_path, 'stage_field: ""\n')
        with pytest.raises(ConfigError, match="stage_field"):
            load_crm_mapping()

    def test_stage_mapping_not_a_mapping(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(tmp_path))
        _write_crm_yaml(tmp_path, "stage_mapping: just-a-string\n")
        with pytest.raises(ConfigError, match="stage_mapping"):
            load_crm_mapping()


# ── LOADER: default + partial-merge paths ────────────────────────────────────


class TestLoaderDefaults:
    def test_absent_file_yields_identity_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(tmp_path))  # empty dir
        mapping = load_crm_mapping()
        assert mapping == default_attio_mapping()
        # Every stage is identity with its canonical rank.
        for stage in PipelineStage:
            assert mapping.to_vendor_stage(stage.value) == stage.value
            assert mapping.to_canonical_stage(stage.value) == stage.value
            assert mapping.rank_for_canonical(stage.value) == STAGE_RANK[stage]

    def test_partial_stage_mapping_merges_with_identity(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(tmp_path))
        _write_crm_yaml(
            tmp_path,
            "stage_mapping:\n"
            "  CONNECTION_SENT:\n    name: Invite sent\n    rank: 11\n"
            "  DM1_SENT:\n    name: First DM\n    rank: 12\n",
        )
        mapping = load_crm_mapping()

        # The two named stages are remapped (label + rank).
        assert mapping.to_vendor_stage("Connection Sent") == "Invite sent"
        assert mapping.to_canonical_stage("Invite sent") == "Connection Sent"
        assert mapping.rank_for_canonical("Connection Sent") == 11
        assert mapping.to_vendor_stage("DM1 Sent") == "First DM"
        assert mapping.rank_for_canonical("DM1 Sent") == 12

        # Every OTHER stage stays identity + canonical STAGE_RANK.
        for stage in PipelineStage:
            if stage in (PipelineStage.CONNECTION_SENT, PipelineStage.DM1_SENT):
                continue
            assert mapping.to_vendor_stage(stage.value) == stage.value
            assert mapping.rank_for_canonical(stage.value) == STAGE_RANK[stage]

        # field_mapping / stage_field absent → their defaults.
        assert mapping.field_mapping == default_attio_mapping().field_mapping
        assert mapping.stage_field == "stage"

    def test_field_mapping_only_keeps_stage_identity(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(tmp_path))
        _write_crm_yaml(
            tmp_path, "field_mapping:\n  custom_field: vendor_slug\n"
        )
        mapping = load_crm_mapping()
        assert mapping.field_slug("custom_field") == "vendor_slug"
        # unmapped field passes through
        assert mapping.field_slug("unknown") == "unknown"
        # A PARTIAL field_mapping MERGES over the canonical default: the three
        # documented fields keep their canonical Attio slugs even though the
        # operator only named `custom_field`. Regression guard — dropping these
        # would make _field_slug("full_name") return "full_name" (not a valid
        # vendor slug) → silently blank person names + dead company lookups.
        assert mapping.field_slug("linkedin_url") == "linkedin"
        assert mapping.field_slug("full_name") == "name"
        assert mapping.field_slug("company_domain") == "domains"
        # stage side untouched → identity
        assert mapping.canonical_to_vendor_stage == default_attio_mapping().canonical_to_vendor_stage

    def test_partial_field_mapping_merges_documented_defaults(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Renaming ONLY linkedin_url must leave full_name/company_domain on their
        # canonical slugs (merge, not replace) — see Finding 1.
        monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(tmp_path))
        _write_crm_yaml(
            tmp_path, "field_mapping:\n  linkedin_url: linkedin_profile\n"
        )
        mapping = load_crm_mapping()
        assert mapping.field_slug("linkedin_url") == "linkedin_profile"
        assert mapping.field_slug("full_name") == "name"
        assert mapping.field_slug("company_domain") == "domains"
