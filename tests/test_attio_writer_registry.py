"""Tests for clients/attio_writer_registry.py (F-PR-3.7, plan §3.15)."""

from __future__ import annotations

from pathlib import Path

import yaml

from clients.attio_writer_registry import (
    WRITE_OWNER_REGISTRY,
    get_authorized_writers,
    is_authorized_writer,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "docs" / "attio_schema_deltas.yaml"


class TestRegistryShape:
    def test_no_resend_critical_attrs_are_sole_writer(self):
        """§3.1 hard-red-line attrs that remain sole-writer-locked.

        PR-9.5 added scripts.attio_dedup as an explicit multi-writer for
        experiment_id / experiment_id_frozen_at / suppress_re_engagement
        per §3.11 union-merge. The escalation discipline (a group with
        multiple distinct experiment_ids escalates rather than auto-
        merging) preserves the §3.1 spirit even with a wider writer set.

        merged_into and experiment_id_backfill_confidence remain
        sole-writer — those are write-once attrs by design (the
        soft-delete pointer flips once at dedup; the archaeology
        confidence is set once at PR-22 stamping).
        """
        sole_writer_critical = [
            ("linkedin_outreach", "experiment_id_backfill_confidence"),
            ("linkedin_outreach", "merged_into"),
        ]
        for key in sole_writer_critical:
            entry = WRITE_OWNER_REGISTRY.get(key)
            assert entry is not None, f"missing registry entry: {key}"
            assert isinstance(entry, str), (
                f"{key} must be sole-writer (string), not multi-writer (list); "
                f"got {entry!r}"
            )

    def test_dedup_is_multi_writer_for_union_merge_targets(self):
        """§3.11 multi-writer additions. Each attribute below must list
        scripts.attio_dedup alongside the primary writer so F-PR-4's
        AttioWriter accepts the PATCH at the §3.15 gate.
        """
        union_merge_targets = [
            "experiment_id",
            "experiment_id_frozen_at",
            "suppress_re_engagement",
            "had_connection_note",
            "response_classification",
            "last_contact_date",
            "dm_step",
            "stage",
            "dm1_sent_at",
            "dm2_sent_at",
            "dm3_sent_at",
            "response_received_at",
        ]
        for slug in union_merge_targets:
            entry = WRITE_OWNER_REGISTRY.get(("linkedin_outreach", slug))
            assert isinstance(entry, list), (
                f"{slug} must be multi-writer per §3.11; got {entry!r}"
            )
            assert "scripts.attio_dedup" in entry, (
                f"scripts.attio_dedup missing from writers for {slug}: {entry!r}"
            )

    def test_provenance_pointers_have_writer_class_paths(self):
        """last_classified_by + last_migrated_by are written by the
        writer classes only. Registry entries point at the class, not
        a function."""
        assert WRITE_OWNER_REGISTRY[("linkedin_outreach", "last_classified_by")] == (
            "workflows.reclassification_run_writer.ReclassificationRunWriter"
        )
        assert WRITE_OWNER_REGISTRY[("linkedin_outreach", "last_migrated_by")] == (
            "workflows.migration_run_writer.MigrationRunWriter"
        )

    def test_dm_step_is_multi_writer(self):
        """dm_step is multi-writer per §3.15 — declared explicitly."""
        entry = WRITE_OWNER_REGISTRY[("linkedin_outreach", "dm_step")]
        assert isinstance(entry, list)
        assert "workflows.daily_check.run_dm_sequencing" in entry
        assert "scripts.attio_dedup" in entry


class TestHelpers:
    def test_get_authorized_writers_for_sole_writer(self):
        # merged_into remains sole-writer (write-once pointer). PR-9.5
        # promoted experiment_id to multi-writer per §3.11; using
        # merged_into here keeps the sole-writer code path covered
        # without re-asserting the §3.11 multi-writer fact.
        owners = get_authorized_writers("linkedin_outreach", "merged_into")
        assert owners == ["scripts.attio_dedup"]

    def test_get_authorized_writers_for_multi_writer(self):
        owners = get_authorized_writers("linkedin_outreach", "dm_step")
        assert owners is not None
        assert len(owners) >= 2

    def test_get_authorized_writers_for_unknown_returns_none(self):
        assert get_authorized_writers("linkedin_outreach", "nonexistent_slug") is None

    def test_is_authorized_writer_sole(self):
        # Use merged_into (sole-writer, PR-9.5) to cover the sole-writer
        # code path. experiment_id moved to multi-writer per §3.11.
        assert is_authorized_writer(
            "linkedin_outreach",
            "merged_into",
            "scripts.attio_dedup",
        )
        assert not is_authorized_writer(
            "linkedin_outreach", "merged_into", "workflows.malicious_module"
        )

    def test_is_authorized_writer_multi(self):
        assert is_authorized_writer(
            "linkedin_outreach", "dm_step",
            "workflows.daily_check.run_dm_sequencing",
        )
        assert is_authorized_writer(
            "linkedin_outreach", "dm_step", "scripts.attio_dedup"
        )
        assert not is_authorized_writer(
            "linkedin_outreach", "dm_step", "scripts.repair_orphan"
        )

    def test_is_authorized_writer_unknown_attribute_returns_false(self):
        assert not is_authorized_writer(
            "linkedin_outreach", "nonexistent_slug", "any_module"
        )


class TestManifestRegistryConsistency:
    """Every attribute in the manifest must have a corresponding registry
    entry (and vice versa, modulo new-object internal attrs that don't
    need cross-PR write-owner enforcement)."""

    def test_every_manifest_attribute_is_in_registry(self):
        manifest = yaml.safe_load(MANIFEST.read_text())
        missing: list[str] = []
        for attr in manifest.get("attributes", []):
            key = (attr["object"], attr["slug"])
            if key not in WRITE_OWNER_REGISTRY:
                missing.append(f"{attr['object']}.{attr['slug']}")
        # Operator Review Queue attrs were added to the manifest in
        # F-PR-3 build3 fold-in; they ARE in the registry now.
        # Anything missing here would be a §3.15 gap.
        assert not missing, (
            f"manifest entries without a WRITE_OWNER_REGISTRY mapping: "
            f"{missing}"
        )

    def test_registry_owners_match_manifest_write_owners(self):
        """Cross-check: the manifest's `write_owner_module` field must
        match the registry's authorized writers exactly. Diverging
        sources of truth defeat the purpose of having two."""
        manifest = yaml.safe_load(MANIFEST.read_text())
        mismatches: list[str] = []
        for attr in manifest.get("attributes", []):
            key = (attr["object"], attr["slug"])
            registry_owners = get_authorized_writers(*key)
            if registry_owners is None:
                continue  # caught by the test above
            manifest_owner = attr.get("write_owner_module")
            manifest_owners = (
                [manifest_owner] if isinstance(manifest_owner, str)
                else list(manifest_owner or [])
            )
            if set(manifest_owners) != set(registry_owners):
                mismatches.append(
                    f"{key}: manifest={sorted(manifest_owners)} vs "
                    f"registry={sorted(registry_owners)}"
                )
        assert not mismatches, "\n  ".join(["manifest/registry drift:"] + mismatches)
