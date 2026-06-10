"""Tests for workflows/industry_classifier.py — classify_industry() and backfill_missing_industries()."""

from unittest.mock import MagicMock, patch

import httpx

from models.campaign import INDUSTRY_LABELS
from workflows.industry_classifier import backfill_missing_industries, classify_industry


def _mock_text_response(text: str):
    """Build a mock Anthropic response shaped like the real SDK response."""
    content_block = MagicMock()
    content_block.type = "text"
    content_block.text = text
    mock_response = MagicMock()
    mock_response.content = [content_block]
    return mock_response


class TestClassifyIndustry:
    def _client(self, text: str):
        """Return a mock anthropic_client whose messages.create returns text."""
        client = MagicMock()
        client.messages.create.return_value = _mock_text_response(text)
        return client

    def test_returns_valid_label(self):
        client = self._client("Automotive")
        assert classify_industry("Toyota", anthropic_client=client) == "Automotive"

    def test_case_insensitive_rescue(self):
        client = self._client("automotive")
        assert classify_industry("Toyota", anthropic_client=client) == "Automotive"

    def test_case_insensitive_rescue_ampersand(self):
        client = self._client("food & beverage")
        assert classify_industry("Bimbo", anthropic_client=client) == "Food & Beverage"

    def test_strips_surrounding_whitespace_quotes_backticks(self):
        client = self._client('  "Packaging."  ')
        assert classify_industry("Empaques Norte", anthropic_client=client) == "Packaging"

    def test_invalid_label_returns_none(self):
        """Invalid LLM output is a classifier error, not a confirmed Other —
        return None so the scorer treats it as data-missing rather than
        applying the off-ICP penalty."""
        client = self._client("Robotics")
        assert classify_industry("FANUC", anthropic_client=client) is None

    def test_api_error_returns_none(self):
        """Network/API failures must not silently flag a real manufacturer
        as off-ICP. Return None so the scorer treats it as unknown."""
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("api down")
        result = classify_industry("Bimbo", anthropic_client=client)
        assert result is None

    def test_empty_company_name_returns_none(self):
        """Empty input is data-missing, not a classifier verdict."""
        client = MagicMock()
        result = classify_industry("", anthropic_client=client)
        assert result is None
        client.messages.create.assert_not_called()

    def test_none_company_name_returns_none(self):
        """None input is data-missing, not a classifier verdict."""
        client = MagicMock()
        result = classify_industry(None, anthropic_client=client)
        assert result is None
        client.messages.create.assert_not_called()

    def test_no_api_key_returns_none(self, monkeypatch):
        """No API key means classification can't run — return None, not Other."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        result = classify_industry("Bimbo")
        assert result is None

    def test_explicit_other_verdict_preserved(self):
        """When the LLM CONFIRMS 'Other' (non-manufacturer), preserve that
        verdict — don't conflate it with classifier failure."""
        client = self._client("Other")
        assert classify_industry("Goldman Sachs", anthropic_client=client) == "Other"

    def test_omits_domain_line_when_none(self):
        client = self._client("Pharma")
        classify_industry("Pfizer", domain=None, anthropic_client=client)
        call_args = client.messages.create.call_args
        messages = call_args.kwargs["messages"]
        user_message = messages[0]["content"]
        assert "Domain:" not in user_message

    def test_includes_domain_line_when_provided(self):
        client = self._client("Food & Beverage")
        classify_industry("Bimbo", domain="bimbo.com", anthropic_client=client)
        call_args = client.messages.create.call_args
        messages = call_args.kwargs["messages"]
        user_message = messages[0]["content"]
        assert "Domain: bimbo.com" in user_message

    def test_uses_haiku_model(self):
        client = self._client("Chemicals")
        classify_industry("BASF", anthropic_client=client)
        call_args = client.messages.create.call_args
        model = call_args.kwargs["model"]
        assert model == "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# Helpers for backfill tests
# ---------------------------------------------------------------------------

def _make_company_record(record_id: str, name: str, industry_title: str | None = None) -> dict:
    """Build a minimal Attio company record dict."""
    iv = []
    if industry_title is not None:
        iv = [{"option": {"title": industry_title}}]
    return {
        "id": {"record_id": record_id},
        "values": {
            "name": [{"value": name}],
            "industry_vertical": iv,
            "domains": [],
        },
    }


def _mock_classify_client(labels: list[str]):
    """Return a mock anthropic_client that returns labels in sequence."""
    client = MagicMock()
    responses = []
    for label in labels:
        block = MagicMock()
        block.type = "text"
        block.text = label
        resp = MagicMock()
        resp.content = [block]
        responses.append(resp)
    client.messages.create.side_effect = responses
    return client


class TestBackfillMissingIndustries:
    """backfill_missing_industries() scans, classifies, and writes correctly."""

    def test_only_targets_missing_industry_vertical(self):
        """Only records without industry_vertical are classified and written."""
        records = [
            _make_company_record("r1", "Toyota", industry_title="Automotive"),
            _make_company_record("r2", "BakeCo"),
            _make_company_record("r3", "BoxCorp"),
        ]
        attio = MagicMock()
        attio.search_companies.return_value = records
        anthropic_client = _mock_classify_client(["Packaging", "Food & Beverage"])

        with patch("workflows.industry_classifier.time"):
            summary = backfill_missing_industries(attio, anthropic_client=anthropic_client)

        assert summary["total_scanned"] == 3
        assert summary["missing"] == 2
        assert summary["classified"] == 2
        assert summary["written"] == 2
        assert summary["api_errors"] == 0
        assert attio.update_company.call_count == 2

        calls = attio.update_company.call_args_list
        written_ids = {c[0][0] for c in calls}
        assert written_ids == {"r2", "r3"}

        valid_labels = set(INDUSTRY_LABELS.keys())
        written_labels = [c.args[1]["industry_vertical"] for c in calls]
        for lbl in written_labels:
            assert lbl in valid_labels, f"Written label {lbl!r} is not a valid INDUSTRY_LABELS key"
        assert written_labels[0] != written_labels[1], (
            "Both records got the same label — classify may be returning a constant"
        )

    def test_dry_run_does_not_write(self):
        """dry_run=True counts classifications but never calls update_company."""
        records = [
            _make_company_record("r1", "BakeCo"),
            _make_company_record("r2", "BoxCorp"),
        ]
        attio = MagicMock()
        attio.search_companies.return_value = records
        anthropic_client = _mock_classify_client(["Food & Beverage", "Packaging"])

        with patch("workflows.industry_classifier.time"):
            summary = backfill_missing_industries(
                attio, anthropic_client=anthropic_client, dry_run=True
            )

        assert summary["written"] == 0
        attio.update_company.assert_not_called()
        assert summary["classified"] == 2

    def test_limit_caps_classification(self):
        """limit kwarg stops classification after N records; total_scanned is still full."""
        records = [_make_company_record(f"r{i}", f"Company {i}") for i in range(5)]
        attio = MagicMock()
        attio.search_companies.return_value = records
        anthropic_client = _mock_classify_client(["Manufacturing", "Chemicals"])

        with patch("workflows.industry_classifier.time"):
            summary = backfill_missing_industries(
                attio, anthropic_client=anthropic_client, limit=2
            )

        assert summary["total_scanned"] == 5
        assert summary["classified"] == 2
        assert summary["written"] == 2
        assert attio.update_company.call_count == 2

    def test_attio_update_failure_counted(self):
        """HTTPStatusError from update_company increments api_errors and does not abort."""
        records = [_make_company_record("r1", "FailCo")]
        attio = MagicMock()
        attio.search_companies.return_value = records

        mock_response = MagicMock()
        mock_response.status_code = 500
        attio.update_company.side_effect = httpx.HTTPStatusError(
            message="Server Error", request=MagicMock(), response=mock_response
        )

        anthropic_client = _mock_classify_client(["Manufacturing"])

        with patch("workflows.industry_classifier.time"):
            summary = backfill_missing_industries(attio, anthropic_client=anthropic_client)

        assert summary["api_errors"] == 1
        assert summary["classified"] == 1
        assert summary["written"] == 0
