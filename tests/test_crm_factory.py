"""Tests for the CRM provider factory (``clients.crm.factory``).

Pin the construction seam introduced in the P1c CRM increment:

* default-when-no-config builds an Attio adapter from the environment
  (backward-compatible with pre-config deployments),
* an explicit ``config/crm.yaml`` with ``vendor: attio`` builds the same,
* an unknown vendor fails loudly with :class:`ConfigError`,
* the returned :class:`CRMBundle` exposes the raw inner ``AttioClient`` the
  workflow-migration transition still threads, and closing the bundle closes
  that client (today's ``with AttioClient()`` lifecycle).

``AttioClient`` is mocked throughout (``patch("clients.attio.AttioClient")`` —
the factory's documented construction patch target), so no test touches the
network or needs ``ATTIO_API_KEY`` (the autouse conftest fixture deletes it).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from clients.crm import AttioProvider
from clients.crm.factory import CRMBundle, get_crm_provider
from clients.settings import ConfigError

if TYPE_CHECKING:
    from pathlib import Path


def _mock_attio_class() -> tuple[MagicMock, MagicMock]:
    """Return a (class_mock, instance_mock) pair for patching AttioClient."""
    instance = MagicMock(name="AttioClientInstance")
    cls = MagicMock(name="AttioClient", return_value=instance)
    return cls, instance


class TestDefaultNoConfig:
    def test_returns_attio_provider_bundle(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """No config/crm.yaml present → Attio-from-env default, no network."""
        # Point config dir at an empty temp dir so crm.yaml is genuinely absent.
        monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(tmp_path))
        cls, instance = _mock_attio_class()

        with patch("clients.attio.AttioClient", cls):
            bundle = get_crm_provider()

        assert isinstance(bundle, CRMBundle)
        assert isinstance(bundle.provider, AttioProvider)
        # The bundle exposes the raw inner client workflows still consume, and
        # it is the exact instance the factory constructed.
        assert bundle.attio is instance
        # Default path builds with no api_key arg (client reads env itself).
        # The field_mapping is threaded in from the identity default mapping —
        # the canonical Attio slugs, so the inner client's reads stay
        # byte-identical to the pre-seam `AttioClient()` call.
        cls.assert_called_once_with(
            field_mapping={
                "linkedin_url": "linkedin",
                "full_name": "name",
                "company_domain": "domains",
            }
        )

    def test_bundle_closes_inner_client_on_exit(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Entering/exiting the bundle closes the raw client (CM lifecycle)."""
        monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(tmp_path))
        cls, instance = _mock_attio_class()

        with patch("clients.attio.AttioClient", cls):
            with get_crm_provider() as bundle:
                assert bundle.attio is instance
                instance.close.assert_not_called()
            instance.close.assert_called_once_with()


class TestWithConfig:
    def test_vendor_attio_builds_provider(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An explicit crm.yaml with vendor: attio builds the Attio adapter."""
        monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("ATTIO_API_KEY", "test-key-123")
        (tmp_path / "crm.yaml").write_text(
            "vendor: attio\n"
            "credentials:\n"
            "  api_key_env: ATTIO_API_KEY\n"
        )
        cls, instance = _mock_attio_class()

        with patch("clients.attio.AttioClient", cls):
            bundle = get_crm_provider()

        assert isinstance(bundle.provider, AttioProvider)
        assert bundle.attio is instance
        # api_key_env present → resolved value passed through to the client,
        # alongside the identity-default field_mapping (canonical Attio slugs).
        cls.assert_called_once_with(
            api_key="test-key-123",
            field_mapping={
                "linkedin_url": "linkedin",
                "full_name": "name",
                "company_domain": "domains",
            },
        )

    def test_config_field_mapping_threads_into_inner_client(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A crm.yaml field_mapping override flows through to AttioClient so the
        inner client's own reads/filters honor the operator's renamed slugs."""
        monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("ATTIO_API_KEY", "test-key-123")
        (tmp_path / "crm.yaml").write_text(
            "vendor: attio\n"
            "credentials:\n"
            "  api_key_env: ATTIO_API_KEY\n"
            "field_mapping:\n"
            "  linkedin_url: linkedin_profile\n"
            "  full_name: full_name_attr\n"
            "  company_domain: website\n"
        )
        cls, _instance = _mock_attio_class()

        with patch("clients.attio.AttioClient", cls):
            get_crm_provider()

        cls.assert_called_once_with(
            api_key="test-key-123",
            field_mapping={
                "linkedin_url": "linkedin_profile",
                "full_name": "full_name_attr",
                "company_domain": "website",
            },
        )

    def test_unknown_vendor_raises_config_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An unrecognized vendor fails loudly, naming the vendor."""
        monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(tmp_path))
        (tmp_path / "crm.yaml").write_text("vendor: salesforce\n")

        with pytest.raises(ConfigError, match="salesforce"):
            get_crm_provider()

    def test_non_mapping_credentials_raises_config_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A malformed `credentials` block fails loudly (no silent skip)."""
        monkeypatch.setenv("OUTBOUND_CONFIG_DIR", str(tmp_path))
        (tmp_path / "crm.yaml").write_text("vendor: attio\ncredentials: not-a-map\n")

        with pytest.raises(ConfigError, match="credentials"):
            get_crm_provider()
