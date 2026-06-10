"""Tests for the Attio schema-delta manifest + validator (F-PR-3.5)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import validate_attio_schema_deltas as v  # noqa: E402

MANIFEST_PATH = REPO_ROOT / "docs" / "attio_schema_deltas.yaml"


@pytest.fixture(scope="module")
def manifest() -> dict:
    return v.load_manifest(MANIFEST_PATH)


class TestShippedManifest:
    """The manifest committed in this PR must validate clean — the
    whole point of F-PR-3.5 is that it's the source of truth."""

    def test_manifest_validates(self, manifest):
        errors = v.validate(manifest)
        assert errors == [], "\n  - " + "\n  - ".join(errors)

    def test_manifest_has_required_top_level_keys(self, manifest):
        for key in ("schema_version", "plan_version", "last_updated"):
            assert key in manifest

    def test_manifest_declares_every_new_object(self, manifest):
        names = {o["object"] for o in manifest["new_objects"]}
        # Every Attio object the plan introduces must be declared. Listed
        # exhaustively here so a future "we'll add this later" PR omission
        # fails CI.
        required_objects = {
            "operator_review_queue",        # F-PR-3
            "reclassification_run",         # F-PR-3.7
            "migration_run",                # F-PR-3.7
            "experiment",                   # F-PR-6
            "daily_run",                    # F-PR-8
            "llm_budget_ledger",            # PR-35
            "data_quality_report",          # PR-43.5
        }
        missing = required_objects - names
        assert not missing, f"missing new objects: {sorted(missing)}"

    def test_manifest_declares_recheck_cache_attrs(self, manifest):
        """§3.5 — Attio-resident recheck cache replaces recheck.json.
        Without these two attrs the recheck-cache architecture decision
        is unenforced."""
        pairs = {(a["object"], a["slug"]) for a in manifest["attributes"]}
        assert ("linkedin_outreach", "last_observed_degree") in pairs
        assert ("linkedin_outreach", "last_observed_at") in pairs

    def test_no_resend_critical_attrs_have_correct_write_owners(self, manifest):
        """§3.1 hard red line: the §3.15 registry pins these attrs to a
        specific writer set. PR-9.5 promoted three of them to explicit
        multi-writer per §3.11 (scripts.attio_dedup is the §3.11
        union-merge writer; multiple-distinct-experiment_id groups
        ESCALATE instead of auto-merging, so dedup's write authority
        does not weaken the no-resend guarantee).

        merged_into and experiment_id_backfill_confidence remain
        sole-writer — write-once attributes by design.
        """
        attrs_by_pair = {(a["object"], a["slug"]): a for a in manifest["attributes"]}
        # Sole-writer-locked attrs (write-once or no §3.11 union-merge path).
        sole_writer_expected = {
            ("linkedin_outreach", "experiment_id_backfill_confidence"):
                "scripts.backfill_experiment_id_archaeology",
            ("linkedin_outreach", "merged_into"):
                "scripts.attio_dedup",
        }
        for pair, expected_writer in sole_writer_expected.items():
            assert pair in attrs_by_pair, f"missing §3.1-critical attribute: {pair}"
            wom = attrs_by_pair[pair]["write_owner_module"]
            assert wom == expected_writer, (
                f"{pair} write_owner_module is {wom!r}, expected {expected_writer!r} "
                f"per §3.15"
            )
        # Multi-writer expected per §3.11 union-merge — scripts.attio_dedup
        # MUST be present alongside the §3.15 primary writer.
        # PR-22 update: experiment_id has 6 writers; experiment_id_frozen_at has 7
        # (PR-22 adds scripts.backfill_experiment_id_archaeology as the
        # cohort-archaeology backfill writer for legacy rows).
        multi_writer_expected = {
            ("linkedin_outreach", "experiment_id"): {
                "workflows.weekly_prospect._build_prospect_entry_attrs",
                "workflows.daily_check.run_connection_requests",
                "workflows.daily_check.detect_accepted_connections",
                "scripts.migrate_experiments_tsv_to_attio",
                "scripts.attio_dedup",
                # PR-22: 6th writer — archaeology backfill for legacy NULL frozen_at rows
                "scripts.backfill_experiment_id_archaeology",
            },
            ("linkedin_outreach", "experiment_id_frozen_at"): {
                "workflows.weekly_prospect._build_prospect_entry_attrs",
                "workflows.daily_check.run_connection_requests",
                "workflows.pre_invite_check._pre_invite_degree_check",
                "workflows.daily_check.detect_accepted_connections",
                "scripts.migrate_experiments_tsv_to_attio",
                "scripts.attio_dedup",
                # PR-22: 7th writer — archaeology backfill for legacy NULL frozen_at rows
                "scripts.backfill_experiment_id_archaeology",
            },
            ("linkedin_outreach", "suppress_re_engagement"): {
                "workflows.cross_channel_suppression",
                "scripts.attio_dedup",
            },
        }
        for pair, expected_writers in multi_writer_expected.items():
            assert pair in attrs_by_pair, f"missing §3.1-critical attribute: {pair}"
            wom = attrs_by_pair[pair]["write_owner_module"]
            assert isinstance(wom, list), (
                f"{pair} write_owner_module is {wom!r}, expected list per §3.11"
            )
            assert set(wom) == expected_writers, (
                f"{pair} write_owner_module is {set(wom)!r}, expected "
                f"{expected_writers!r} per §3.11 + §3.15"
            )

    def test_experiment_id_frozen_at_enum_complete(self, manifest):
        """§3.1 full 6-value enum. Missing values would let archaeology
        rows back into send-eligibility."""
        attrs_by_pair = {(a["object"], a["slug"]): a for a in manifest["attributes"]}
        opts = set(attrs_by_pair[("linkedin_outreach", "experiment_id_frozen_at")]["options"])
        assert opts == {
            "prospect",
            "connection_sent",
            "accepted",
            "legacy_pre_tsv_era",
            "legacy_inferred_by_archaeology",
            "legacy_pure_unknown",
        }


class TestValidator:
    """Negative tests: malformed manifests should produce specific errors."""

    def test_duplicate_object_slug_pair_caught(self):
        bad = {
            "schema_version": 1,
            "plan_version": "test",
            "last_updated": "2026-05-21",
            "new_objects": [],
            "attributes": [
                {
                    "object": "linkedin_outreach", "slug": "foo",
                    "type": "text", "pr_id": "PR-1", "status": "planned",
                    "write_owner_module": "workflows.x",
                },
                {
                    "object": "linkedin_outreach", "slug": "foo",
                    "type": "text", "pr_id": "PR-2", "status": "planned",
                    "write_owner_module": "workflows.y",
                },
            ],
        }
        errors = v.validate(bad)
        assert any("duplicate entry" in e for e in errors), errors

    def test_invalid_type_caught(self):
        bad = {
            "schema_version": 1, "plan_version": "x", "last_updated": "x",
            "new_objects": [],
            "attributes": [{
                "object": "x", "slug": "y", "type": "definitely_not_a_type",
                "pr_id": "PR-1", "status": "planned",
                "write_owner_module": "workflows.x",
            }],
        }
        errors = v.validate(bad)
        assert any("invalid type" in e for e in errors), errors

    def test_select_without_options_caught(self):
        bad = {
            "schema_version": 1, "plan_version": "x", "last_updated": "x",
            "new_objects": [],
            "attributes": [{
                "object": "x", "slug": "y", "type": "select", "options": None,
                "pr_id": "PR-1", "status": "planned",
                "write_owner_module": "workflows.x",
            }],
        }
        errors = v.validate(bad)
        assert any("type=select requires" in e for e in errors), errors

    def test_non_select_with_options_caught(self):
        bad = {
            "schema_version": 1, "plan_version": "x", "last_updated": "x",
            "new_objects": [],
            "attributes": [{
                "object": "x", "slug": "y", "type": "text", "options": ["a", "b"],
                "pr_id": "PR-1", "status": "planned",
                "write_owner_module": "workflows.x",
            }],
        }
        errors = v.validate(bad)
        assert any("must not declare `options`" in e for e in errors), errors

    def test_missing_required_field_caught(self):
        bad = {
            "schema_version": 1, "plan_version": "x", "last_updated": "x",
            "new_objects": [],
            "attributes": [{
                "object": "x", "slug": "y", "type": "text", "status": "planned",
                # missing pr_id and write_owner_module
            }],
        }
        errors = v.validate(bad)
        assert any("missing field `pr_id`" in e for e in errors), errors
        assert any("missing field `write_owner_module`" in e for e in errors), errors

    def test_invalid_status_caught(self):
        bad = {
            "schema_version": 1, "plan_version": "x", "last_updated": "x",
            "new_objects": [],
            "attributes": [{
                "object": "x", "slug": "y", "type": "text",
                "pr_id": "PR-1", "status": "not_a_status",
                "write_owner_module": "workflows.x",
            }],
        }
        errors = v.validate(bad)
        assert any("invalid status" in e for e in errors), errors

    def test_empty_write_owner_caught(self):
        bad = {
            "schema_version": 1, "plan_version": "x", "last_updated": "x",
            "new_objects": [],
            "attributes": [{
                "object": "x", "slug": "y", "type": "text",
                "pr_id": "PR-1", "status": "planned",
                "write_owner_module": "",
            }],
        }
        errors = v.validate(bad)
        assert any("write_owner_module must be" in e for e in errors), errors

    def test_diverging_select_options_caught(self):
        """Two PRs claiming the same (object, slug) with different option
        sets is a genuine planning bug."""
        bad = {
            "schema_version": 1, "plan_version": "x", "last_updated": "x",
            "new_objects": [],
            "attributes": [
                {
                    "object": "linkedin_outreach", "slug": "lane",
                    "type": "select", "options": ["a", "b"],
                    "pr_id": "PR-1", "status": "planned",
                    "write_owner_module": "workflows.x",
                },
                {
                    "object": "linkedin_outreach", "slug": "lane",
                    "type": "select", "options": ["a", "c"],
                    "pr_id": "PR-2", "status": "planned",
                    "write_owner_module": "workflows.y",
                },
            ],
        }
        errors = v.validate(bad)
        # Two findings: duplicate entry + diverging options. Either one
        # is sufficient to catch the bug.
        assert any("duplicate" in e or "options diverge" in e for e in errors), errors

    def test_missing_top_level_keys_caught(self):
        bad = {"attributes": []}
        errors = v.validate(bad)
        assert any("schema_version" in e for e in errors), errors

    def test_duplicate_new_object_caught(self):
        bad = {
            "schema_version": 1, "plan_version": "x", "last_updated": "x",
            "new_objects": [
                {"object": "foo", "pr_id": "PR-1", "status": "planned"},
                {"object": "foo", "pr_id": "PR-2", "status": "planned"},
            ],
            "attributes": [],
        }
        errors = v.validate(bad)
        assert any("duplicate new object" in e for e in errors), errors

    def test_descoped_status_accepted(self):
        """Wave-2-A: `descoped` is a valid terminal status (writer code
        shipped, Attio object intentionally absent)."""
        ok = {
            "schema_version": 1, "plan_version": "x", "last_updated": "x",
            "new_objects": [
                {"object": "foo", "pr_id": "PR-1", "status": "descoped"},
            ],
            "attributes": [{
                "object": "foo", "slug": "bar", "type": "text",
                "pr_id": "PR-1", "status": "descoped",
                "write_owner_module": "workflows.x",
            }],
        }
        errors = v.validate(ok)
        assert errors == [], errors

    @pytest.mark.parametrize("attr_status", ["shipped", "verified"])
    def test_descoped_parent_rejects_shipped_attr(self, attr_status):
        """1(d) invariant: a descoped object must not host any
        shipped OR verified attr — either would reach the live gate."""
        bad = {
            "schema_version": 1, "plan_version": "x", "last_updated": "x",
            "new_objects": [
                {"object": "foo", "pr_id": "PR-1", "status": "descoped"},
            ],
            "attributes": [{
                "object": "foo", "slug": "bar", "type": "text",
                "pr_id": "PR-1", "status": attr_status,
                "write_owner_module": "workflows.x",
            }],
        }
        errors = v.validate(bad)
        assert any(
            "descoped" in e and "foo.bar" in e for e in errors
        ), errors

    def test_planned_written_attr_warns(self, caplog):
        """1(c) guard: a planned attr that already has a writer module is
        a week_starting-class gap — warn (via logging, non-fatal) but do
        NOT add to the returned error list (would trip the wet preflight)."""
        import logging
        manifest = {
            "schema_version": 1, "plan_version": "x", "last_updated": "x",
            "new_objects": [],
            "attributes": [{
                "object": "linkedin_outreach", "slug": "some_attr",
                "type": "text", "pr_id": "PR-X", "status": "planned",
                "write_owner_module": "workflows.daily_check.run_x",
            }],
        }
        with caplog.at_level(logging.WARNING):
            errors = v.validate(manifest)
        # Non-fatal: the warning must NOT appear in the returned list.
        assert errors == [], errors
        assert any(
            "linkedin_outreach.some_attr" in rec.message
            and "status:planned" in rec.message
            for rec in caplog.records
        ), [r.message for r in caplog.records]


class TestCli:
    """Validator CLI exit codes."""

    def test_exit_0_on_valid_manifest(self, manifest, tmp_path, monkeypatch):
        path = tmp_path / "good.yaml"
        path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
        rc = v.main(["--manifest", str(path)])
        assert rc == 0

    def test_exit_1_on_invalid_manifest(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text(
            yaml.safe_dump({
                "schema_version": 1, "plan_version": "x", "last_updated": "x",
                "new_objects": [], "attributes": [{"object": "x"}],
            }),
            encoding="utf-8",
        )
        rc = v.main(["--manifest", str(path)])
        assert rc == 1

    def test_exit_2_on_missing_manifest(self, tmp_path):
        rc = v.main(["--manifest", str(tmp_path / "nope.yaml")])
        assert rc == 2

    def test_exit_2_on_malformed_yaml(self, tmp_path):
        """Engineer-QA #1: a YAML parse error must produce exit code 2
        with a clean stderr message, not a raw Python traceback."""
        path = tmp_path / "bad.yaml"
        path.write_text("{ this is not [ valid yaml :: at all\n", encoding="utf-8")
        rc = v.main(["--manifest", str(path)])
        assert rc == 2

    def test_exit_2_on_non_mapping_top_level(self, tmp_path):
        """Top-level YAML list (not mapping) is a manifest-shape error."""
        path = tmp_path / "list.yaml"
        path.write_text("- not_a_mapping\n", encoding="utf-8")
        rc = v.main(["--manifest", str(path)])
        assert rc == 2

    def test_check_attio_flag_emits_stub_warning(self, manifest, tmp_path, capsys):
        """Engineer-QA #3 + data-steward-QA #3: --check-attio is a stub
        in F-PR-3.5. Must warn on stderr so operators don't assume the
        check actually ran."""
        path = tmp_path / "good.yaml"
        path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
        rc = v.main(["--manifest", str(path), "--check-attio"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "stub" in captured.err.lower() or "deferred" in captured.err.lower()


class TestValidateRobustness:
    """Defensive top-level handling — bad inputs must surface loudly."""

    def test_validate_returns_error_for_non_dict(self):
        # validate([]) used to crash with AttributeError. Now it surfaces
        # the type mismatch as a structured error.
        errors = v.validate([])  # type: ignore[arg-type]
        assert errors
        assert any("must be a mapping" in e for e in errors), errors

    def test_validate_returns_error_for_none(self):
        errors = v.validate(None)  # type: ignore[arg-type]
        assert errors
        assert any("must be a mapping" in e for e in errors), errors

    def test_duplicate_error_message_includes_pr_ids(self):
        """Engineer-QA #4: the duplicate-entry error must name BOTH
        offending pr_ids so the operator can identify which two PRs are
        colliding."""
        bad = {
            "schema_version": 1, "plan_version": "x", "last_updated": "x",
            "new_objects": [],
            "attributes": [
                {
                    "object": "linkedin_outreach", "slug": "lane",
                    "type": "text", "pr_id": "PR-12",
                    "status": "planned", "write_owner_module": "workflows.x",
                },
                {
                    "object": "linkedin_outreach", "slug": "lane",
                    "type": "text", "pr_id": "PR-39",
                    "status": "planned", "write_owner_module": "workflows.y",
                },
            ],
        }
        errors = v.validate(bad)
        dup_error = next((e for e in errors if "duplicate" in e), None)
        assert dup_error is not None, errors
        assert "PR-12" in dup_error
        assert "PR-39" in dup_error


class TestCheckAttioShipped:
    """The wet-run schema-drift pre-flight (cli.py daily) relies on
    check_attio_shipped to flag manifest entries that claim shipped/verified
    but are missing from production Attio — the 2026-05-28 halt class, where
    experiment_id_frozen_at was written by live code while undeployed."""

    @staticmethod
    def _manifest(status: str) -> dict:
        return {
            "schema_version": 1, "plan_version": "x", "last_updated": "x",
            "new_objects": [],
            "attributes": [{
                "object": "companies", "slug": "made_up_attr_xyz",
                "type": "text", "pr_id": "PR-X",
                "status": status, "write_owner_module": "workflows.x",
            }],
        }

    @staticmethod
    def _attio_404():
        from unittest.mock import MagicMock

        import httpx
        req = httpx.Request("GET", "https://api.attio.com/v2/x")
        attio = MagicMock()
        attio._request.side_effect = httpx.HTTPStatusError(
            "404", request=req, response=httpx.Response(404, request=req),
        )
        return attio

    @pytest.mark.parametrize("status", ["shipped", "verified"])
    def test_flags_missing_attr_for_shipped_and_verified(self, status):
        from unittest.mock import patch
        with patch("clients.attio.AttioClient", return_value=self._attio_404()):
            errors = v.check_attio_shipped(
                self._manifest(status),
                statuses=frozenset({"shipped", "verified"}),
            )
        assert any(
            "made_up_attr_xyz" in e and "does NOT exist" in e for e in errors
        ), errors

    def test_passes_when_attr_exists(self):
        from unittest.mock import MagicMock, patch
        attio = MagicMock()
        attio._request.return_value = {"data": {"type": "text"}}
        with patch("clients.attio.AttioClient", return_value=attio):
            errors = v.check_attio_shipped(
                self._manifest("shipped"),
                statuses=frozenset({"shipped", "verified"}),
            )
        assert errors == [], errors

    def test_planned_attr_is_not_checked(self):
        """A planned attr legitimately doesn't exist in prod yet, so the
        shipped/verified existence check must skip it (no false positive)."""
        from unittest.mock import patch
        with patch("clients.attio.AttioClient", return_value=self._attio_404()):
            errors = v.check_attio_shipped(
                self._manifest("planned"),
                statuses=frozenset({"shipped", "verified"}),
            )
        assert errors == [], errors

    @staticmethod
    def _attio_raising(exc):
        from unittest.mock import MagicMock
        attio = MagicMock()
        attio._request.side_effect = exc
        return attio

    def test_non_404_http_error_is_surfaced_not_swallowed(self):
        """A 5xx/401 during the existence check must produce an error (fail
        closed), not be treated as 'exists/OK'."""
        from unittest.mock import patch

        import httpx
        req = httpx.Request("GET", "https://api.attio.com/v2/x")
        exc = httpx.HTTPStatusError(
            "500", request=req, response=httpx.Response(500, request=req),
        )
        with patch("clients.attio.AttioClient", return_value=self._attio_raising(exc)):
            errors = v.check_attio_shipped(
                self._manifest("shipped"),
                statuses=frozenset({"shipped", "verified"}),
            )
        assert any("made_up_attr_xyz" in e and "GET failed" in e for e in errors), errors

    def test_transport_error_fails_closed(self):
        """A transport error (ConnectError/timeout) during the existence check
        must fail closed — surfaced as an error so the wet-run pre-flight
        refuses to proceed rather than treating the attr as present."""
        from unittest.mock import patch

        import httpx
        exc = httpx.ConnectError("connection refused")
        with patch("clients.attio.AttioClient", return_value=self._attio_raising(exc)):
            errors = v.check_attio_shipped(
                self._manifest("shipped"),
                statuses=frozenset({"shipped", "verified"}),
            )
        assert any(
            "made_up_attr_xyz" in e and "transport" in e for e in errors
        ), errors

    def test_descoped_attr_is_not_checked(self):
        """A descoped attr legitimately has no Attio object, so the
        shipped/verified existence check must skip it (mirror of
        test_planned_attr_is_not_checked)."""
        from unittest.mock import patch
        with patch("clients.attio.AttioClient", return_value=self._attio_404()):
            errors = v.check_attio_shipped(
                self._manifest("descoped"),
                statuses=frozenset({"shipped", "verified"}),
            )
        assert errors == [], errors

    @staticmethod
    def _new_object_manifest(status: str, holdback=None) -> dict:
        return {
            "schema_version": 1, "plan_version": "x", "last_updated": "x",
            "deploy_holdback_objects": list(holdback or []),
            "new_objects": [{
                "object": "made_up_object_xyz",
                "pr_id": "PR-X", "status": status,
            }],
            "attributes": [],
        }

    def test_flags_missing_new_object(self):
        """A shipped new_object whose GET 404s → 'manifest lies' error."""
        from unittest.mock import patch
        with patch("clients.attio.AttioClient", return_value=self._attio_404()):
            errors = v.check_attio_shipped(
                self._new_object_manifest("shipped"),
                statuses=frozenset({"shipped", "verified"}),
            )
        assert any(
            "made_up_object_xyz" in e and "does NOT exist" in e for e in errors
        ), errors

    def test_holdback_object_skipped(self):
        """A shipped new_object in deploy_holdback_objects is skipped even
        when GET would 404."""
        from unittest.mock import patch
        with patch("clients.attio.AttioClient", return_value=self._attio_404()):
            errors = v.check_attio_shipped(
                self._new_object_manifest(
                    "shipped", holdback=["made_up_object_xyz"]
                ),
                statuses=frozenset({"shipped", "verified"}),
            )
        assert errors == [], errors

    def test_descoped_object_skipped(self):
        """A descoped new_object is skipped (status not in {shipped,
        verified}) even when GET would 404."""
        from unittest.mock import patch
        with patch("clients.attio.AttioClient", return_value=self._attio_404()):
            errors = v.check_attio_shipped(
                self._new_object_manifest("descoped"),
                statuses=frozenset({"shipped", "verified"}),
            )
        assert errors == [], errors

    def test_defense_in_depth_flags_missing_attr_parent_object(self):
        """1(b) week_starting-class gap: a shipped attr whose parent object
        is NOT covered by the new_objects loop (no new_objects entry), not
        in holdback, not a list-parent → the attr loop GETs the attribute
        (404), AND the defense-in-depth block GETs the parent object (404).
        Either is sufficient to halt the wet preflight; assert the object
        existence is checked."""
        from unittest.mock import patch
        manifest = {
            "schema_version": 1, "plan_version": "x", "last_updated": "x",
            "deploy_holdback_objects": [],
            "new_objects": [],
            "attributes": [{
                "object": "phantom_object", "slug": "some_attr",
                "type": "text", "pr_id": "PR-X", "status": "shipped",
                "write_owner_module": "workflows.x",
            }],
        }
        with patch("clients.attio.AttioClient", return_value=self._attio_404()):
            errors = v.check_attio_shipped(
                manifest, statuses=frozenset({"shipped", "verified"}),
            )
        assert any(
            "phantom_object" in e and "hosts status-matching attrs" in e
            for e in errors
        ), errors
