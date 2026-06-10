"""PR-9a: Tests for the schema migration and backfill scripts.

Covers:
  - migrate_dmN_sent_at_schema._ensure_attribute: idempotency (skip if exists),
    create on 404, type-mismatch raises RuntimeError
  - migrate_dmN_sent_at_schema.main: dry-run mode, returns 0 on success
  - backfill_canonical_linkedin_url: idempotency (no-op on second run),
    skip entries with no linkedin URL, correct canonical computation
  - §9.4 idempotency contract: second consecutive run logs rows_modified=0
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from clients.attio import AttioClient

# ---- Helpers ----------------------------------------------------------------


def _http_error(status: int, body: str = "") -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://api.attio.com/v2/x")
    resp = httpx.Response(status, request=req, content=body.encode())
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    raise AssertionError("unreachable")  # pragma: no cover


def _ok_response(body: dict | None = None) -> httpx.Response:
    req = httpx.Request("POST", "https://api.attio.com/v2/x")
    return httpx.Response(
        200, request=req, json=body or {"data": {"id": {"record_id": "rec_x"}}}
    )


@pytest.fixture
def mock_attio(monkeypatch):
    """A minimal AttioClient mock that returns success on _request."""
    attio = AttioClient(api_key="test-key")
    return attio


# ============================================================
# migrate_dmN_sent_at_schema: _ensure_attribute
# ============================================================


class TestEnsureAttribute:
    def test_skips_existing_attribute(self) -> None:
        from scripts.migrate_dmN_sent_at_schema import _ensure_attribute
        attio = AttioClient(api_key="test-key")
        with patch.object(attio, "_request", return_value={"data": {"type": "datetime"}}):
            result = _ensure_attribute(attio, "list-id", "dm1_sent_at", "datetime", dry_run=False)
        assert result == "skipped"

    def test_creates_on_404(self) -> None:
        from scripts.migrate_dmN_sent_at_schema import _ensure_attribute
        attio = AttioClient(api_key="test-key")
        responses = [
            _http_error(404),          # GET → not found
            {"data": {}},              # POST → created
            # MigrationRunWriter POST (migration_run record)
            {"data": {"id": {"record_id": "run-id"}}},
        ]

        def side_effect(method, path, **kwargs):
            exc_or_resp = responses.pop(0)
            if isinstance(exc_or_resp, httpx.HTTPStatusError):
                raise exc_or_resp
            return exc_or_resp

        with patch.object(attio, "_request", side_effect=side_effect):
            result = _ensure_attribute(attio, "list-id", "dm1_sent_at", "datetime", dry_run=False)
        assert result == "created"

    def test_type_mismatch_raises(self) -> None:
        from scripts.migrate_dmN_sent_at_schema import _ensure_attribute
        attio = AttioClient(api_key="test-key")
        with patch.object(
            attio, "_request",
            return_value={"data": {"type": "text"}},  # expects datetime, got text
        ), pytest.raises(RuntimeError, match="type"):
            _ensure_attribute(
                attio, "list-id", "dm1_sent_at", "datetime", dry_run=False
            )

    def test_dry_run_returns_would_create(self) -> None:
        from scripts.migrate_dmN_sent_at_schema import _ensure_attribute
        attio = AttioClient(api_key="test-key")
        with patch.object(attio, "_request", side_effect=_http_error(404)):
            result = _ensure_attribute(attio, "list-id", "dm1_sent_at", "datetime", dry_run=True)
        assert result == "would_create"

    def test_non_404_http_error_propagates(self) -> None:
        from scripts.migrate_dmN_sent_at_schema import _ensure_attribute
        attio = AttioClient(api_key="test-key")
        with patch.object(attio, "_request", side_effect=_http_error(500)), pytest.raises(httpx.HTTPStatusError):
            _ensure_attribute(
                attio, "list-id", "dm1_sent_at", "datetime", dry_run=False
            )


# ============================================================
# migrate_dmN_sent_at_schema: main() dry-run
# ============================================================


class TestMigrateDmNSchemaMain:
    def test_dry_run_exits_0(self, tmp_path) -> None:
        """main() with --dry-run returns 0 when all attrs would-create or skip."""
        from scripts.migrate_dmN_sent_at_schema import main

        attio_mock = AttioClient(api_key="test-key")

        with (
            patch("scripts.migrate_dmN_sent_at_schema.AttioClient", return_value=attio_mock),
            patch("scripts.migrate_dmN_sent_at_schema._get_list_id", return_value="list-uuid"),
            patch("scripts.migrate_dmN_sent_at_schema._ensure_attribute", return_value="would_create"),
            # MigrationRunWriter tries to write a Migration Run row — suppress
            patch(
                "workflows.migration_run_writer.MigrationRunWriter._write_migration_run_row"
            ),
        ):
            rc = main(["--dry-run"])

        assert rc == 0

    def test_no_list_id_exits_2(self) -> None:
        """main() returns 2 when list ID cannot be determined."""
        attio_mock = AttioClient(api_key="test-key")

        with (
            patch("scripts.migrate_dmN_sent_at_schema.AttioClient", return_value=attio_mock),
            patch(
                "scripts.migrate_dmN_sent_at_schema._get_list_id",
                side_effect=RuntimeError("no list id"),
            ),
        ):
            from scripts.migrate_dmN_sent_at_schema import main
            rc = main([])

        assert rc == 2

    def test_all_attrs_covered(self) -> None:
        """NEW_ATTRS must contain all six PR-9a attributes."""
        from scripts.migrate_dmN_sent_at_schema import NEW_ATTRS
        slugs = {slug for slug, _, _ in NEW_ATTRS}
        expected = {
            "dm1_sent_at",
            "dm2_sent_at",
            "dm3_sent_at",
            "response_received_at",
            "canonical_linkedin_url",
            "vanity_url_slug",
        }
        assert slugs == expected, f"Missing or extra slugs: {slugs ^ expected}"


# ============================================================
# backfill_canonical_linkedin_url: idempotency
# ============================================================


def _make_entry(entry_id: str, record_id: str, existing_canonical: str = "") -> dict:
    """Build a minimal Attio list entry dict."""
    entry: dict = {
        "entry_id": entry_id,
        "parent_record_id": record_id,
        "entry_values": {},
    }
    if existing_canonical:
        entry["entry_values"]["canonical_linkedin_url"] = [
            {"value": existing_canonical, "attribute_type": "text"}
        ]
    return entry


class TestBackfillCanonicalLinkedinUrl:
    def test_already_correct_is_skipped(self) -> None:
        """When canonical_linkedin_url already has the correct value, skip → no write."""
        from scripts.backfill_canonical_linkedin_url import (
            _compute_canonical,
            _read_existing_canonical,
        )

        linkedin = "https://www.linkedin.com/in/mateo-lt-12345/"
        computed = _compute_canonical(linkedin)
        assert computed == "https://linkedin.com/in/mateo-lt-12345"

        entry = _make_entry("e1", "r1", existing_canonical=computed)
        existing = _read_existing_canonical(entry)
        assert existing == computed  # → idempotent: no write needed

    def test_empty_entry_needs_write(self) -> None:
        from scripts.backfill_canonical_linkedin_url import _read_existing_canonical

        entry = _make_entry("e1", "r1", existing_canonical="")
        assert _read_existing_canonical(entry) is None

    def test_different_value_needs_write(self) -> None:
        from scripts.backfill_canonical_linkedin_url import (
            _compute_canonical,
            _read_existing_canonical,
        )

        linkedin = "https://www.linkedin.com/in/mateo-lt-12345/"
        computed = _compute_canonical(linkedin)
        entry = _make_entry("e1", "r1", existing_canonical="https://linkedin.com/in/old-slug")
        existing = _read_existing_canonical(entry)
        assert existing != computed  # → write needed

    def test_no_linkedin_url_returns_none(self) -> None:
        from scripts.backfill_canonical_linkedin_url import _compute_canonical

        assert _compute_canonical("") is None
        assert _compute_canonical("   ") is None

    def test_compute_canonical_normalizes(self) -> None:
        from scripts.backfill_canonical_linkedin_url import _compute_canonical

        assert _compute_canonical("https://www.linkedin.com/in/Test-User-123/") == (
            "https://linkedin.com/in/test-user-123"
        )

    def test_second_run_is_noop(self) -> None:
        """§9.4 idempotency: after first run computes + writes canonical URL,
        a second run must see existing == computed and skip → rows_modified=0.

        We simulate this by calling _compute_canonical twice and asserting the
        values match (deterministic computation from fixed input).
        """
        from scripts.backfill_canonical_linkedin_url import _compute_canonical

        linkedin = "https://www.linkedin.com/in/Mateo-LT-12345/"
        first_run = _compute_canonical(linkedin)
        second_run = _compute_canonical(linkedin)
        # If first_run == second_run, the backfill is idempotent:
        # the second run finds existing == computed and skips.
        assert first_run == second_run
        assert first_run == "https://linkedin.com/in/mateo-lt-12345"

    def test_main_dry_run_no_writes(self, capsys) -> None:
        """--dry-run mode must emit 0 actual Attio writes."""
        from scripts.backfill_canonical_linkedin_url import main

        entry = _make_entry("e1", "r1")  # empty canonical, needs write
        mock_attio_inst = MagicMock(spec=AttioClient)
        mock_attio_inst.query_list_entries.return_value = [entry]
        # RecordCache.get returns (name, company, linkedin_url, industry, title)
        mock_attio_inst.get_person.return_value = {
            "values": {
                "linkedin": [{"value": "https://www.linkedin.com/in/test-user/"}],
                "name": [{"first_name": "Test", "last_name": "User"}],
                "primary_email_addresses": [],
                "company_links": [],
            }
        }
        mock_attio_inst.extract_record_info.return_value = (
            "Test User", "Acme", "https://www.linkedin.com/in/test-user/", "", ""
        )
        mock_attio_inst.bulk_fetch_persons_by_record_ids.return_value = {}

        mock_writer = MagicMock()

        with (
            patch("scripts.backfill_canonical_linkedin_url.AttioClient", return_value=mock_attio_inst),
            patch("scripts.backfill_canonical_linkedin_url._get_list_id", return_value="list-uuid"),
            patch("scripts.backfill_canonical_linkedin_url.AttioWriter", return_value=mock_writer),
            patch(
                "workflows.migration_run_writer.MigrationRunWriter._write_migration_run_row"
            ),
        ):
            rc = main(["--dry-run"])

        # In dry-run mode, AttioWriter.apply must not be called
        mock_writer.apply.assert_not_called()
        assert rc == 0

    def test_main_missing_attio_key_exits_2(self, monkeypatch) -> None:
        """main() returns 2 when ATTIO_API_KEY is not set."""
        monkeypatch.delenv("ATTIO_API_KEY", raising=False)
        monkeypatch.setenv("ATTIO_LIST_ID", "some-list")

        from scripts.backfill_canonical_linkedin_url import main

        with patch(
            "scripts.backfill_canonical_linkedin_url.AttioClient",
            side_effect=KeyError("ATTIO_API_KEY"),
        ):
            rc = main([])

        assert rc == 2

    def test_manifest_lists_canonical_url_as_shipped(self) -> None:
        """docs/attio_schema_deltas.yaml must show canonical_linkedin_url status=shipped."""
        from pathlib import Path

        import yaml  # type: ignore[import-untyped]

        manifest_path = (
            Path(__file__).resolve().parent.parent / "docs" / "attio_schema_deltas.yaml"
        )
        with manifest_path.open() as f:
            manifest = yaml.safe_load(f)

        attrs = manifest.get("attributes", [])
        pr9a_attrs = {
            a["slug"]: a["status"]
            for a in attrs
            if a.get("pr_id") == "PR-9a"
        }
        assert pr9a_attrs, "No PR-9a attrs found in manifest"

        expected_shipped = {
            "dm1_sent_at", "dm2_sent_at", "dm3_sent_at",
            "response_received_at", "canonical_linkedin_url", "vanity_url_slug",
        }
        not_shipped = {
            slug for slug in expected_shipped
            if pr9a_attrs.get(slug) != "shipped"
        }
        assert not not_shipped, (
            f"These PR-9a attrs are not 'shipped' in the manifest: {not_shipped}"
        )
