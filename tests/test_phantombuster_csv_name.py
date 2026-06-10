"""S3-fallback CSV resolution must honor a non-default result file name.

Per-launch csvName busts the phantom's dedup database (PB keys processing
state on the result file name). The happy path — the "CSV saved at <url>"
container log line — is filename-agnostic, but the agent-scoped S3 fallback
hardcoded `result.csv` and would fetch the WRONG (stale) file for launches
that used a custom csvName.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from clients.pb_envelope import PBLaunch
from clients.phantombuster import PhantomBusterClient


def _client_with_output(output_log: str) -> PhantomBusterClient:
    client = PhantomBusterClient(api_key="test-key")
    client.get_container_output = MagicMock(return_value={"output": output_log})
    client.get_agent = MagicMock(
        return_value={"s3Folder": "agent-folder", "orgS3Folder": "org-folder"}
    )
    return client


def _launch() -> PBLaunch:
    from datetime import UTC, datetime

    return PBLaunch(
        container_id="ct-1",
        agent_id="ag-1",
        launched_at=datetime.now(UTC),
        arguments_sha256="x",
        request_id=None,
    )


def test_fallback_url_uses_custom_csv_name():
    client = _client_with_output("no csv line here")
    url = client.get_result_csv_url(_launch(), csv_name="deg-20260610-101500-123456")
    assert url == (
        "https://phantombuster.s3.amazonaws.com/"
        "org-folder/agent-folder/deg-20260610-101500-123456.csv"
    )


def test_fallback_url_defaults_to_result_csv():
    client = _client_with_output("no csv line here")
    url = client.get_result_csv_url(_launch())
    assert url.endswith("/result.csv")


def test_happy_path_log_line_wins_over_csv_name():
    client = _client_with_output(
        "CSV saved at https://phantombuster.s3.amazonaws.com/x/y/whatever.csv"
    )
    url = client.get_result_csv_url(_launch(), csv_name="ignored-name")
    assert url == "https://phantombuster.s3.amazonaws.com/x/y/whatever.csv"


def test_download_result_csv_threads_csv_name():
    client = _client_with_output("no csv line here")
    with patch("clients.phantombuster.httpx.get") as mock_get:
        mock_get.return_value = MagicMock(text="a,b\n1,2\n", raise_for_status=MagicMock())
        client.download_result_csv(_launch(), csv_name="deg-x")
    assert mock_get.call_args[0][0].endswith("/deg-x.csv")


def test_download_returns_none_on_fallback_404():
    """An HTTP error on the fallback S3 URL degrades to None, not an exception."""
    import httpx as _httpx

    client = _client_with_output("no csv line here")
    with patch("clients.phantombuster.httpx.get") as mock_get:
        mock_get.side_effect = _httpx.HTTPStatusError(
            "404", request=MagicMock(), response=MagicMock()
        )
        result = client.download_result_csv(_launch(), csv_name="deg-missing")
    assert result is None
