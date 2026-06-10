"""Tests for PhantomBuster client, focused on launch_agent retry behavior."""

from unittest.mock import patch

import httpx
import pytest

from clients.phantombuster import PhantomBusterClient


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.phantombuster.com/api/v2/agents/launch")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"{status}", request=request, response=response)


@pytest.fixture
def pb() -> PhantomBusterClient:
    return PhantomBusterClient(api_key="test-key")


def test_launch_agent_returns_immediately_on_success(pb: PhantomBusterClient) -> None:
    with patch.object(pb, "_request", return_value={"containerId": "abc"}) as mock_req:
        launch = pb.launch_agent("123", {"foo": "bar"})

    # F-PR-5: launch_agent returns a typed PBLaunch envelope.
    assert launch.container_id == "abc"
    assert launch.agent_id == "123"
    assert mock_req.call_count == 1
    mock_req.assert_called_once_with(
        "POST", "/agents/launch", json={"id": "123", "arguments": {"foo": "bar"}}
    )


def test_launch_agent_retries_on_429_then_succeeds(pb: PhantomBusterClient) -> None:
    responses = [_http_error(429), _http_error(429), {"containerId": "abc"}]

    def fake_request(*args, **kwargs):
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    with patch.object(pb, "_request", side_effect=fake_request) as mock_req, \
         patch("clients.phantombuster.time.sleep") as mock_sleep:
        launch = pb.launch_agent("123")

    assert launch.container_id == "abc"
    assert mock_req.call_count == 3
    # First retry waits 30s, second waits 60s.
    assert [c.args[0] for c in mock_sleep.call_args_list] == [30, 60]


def test_launch_agent_raises_non_429_immediately(pb: PhantomBusterClient) -> None:
    with patch.object(pb, "_request", side_effect=_http_error(500)) as mock_req, \
         patch("clients.phantombuster.time.sleep") as mock_sleep, \
         pytest.raises(httpx.HTTPStatusError) as exc:
        pb.launch_agent("123")

    assert exc.value.response.status_code == 500
    assert mock_req.call_count == 1
    mock_sleep.assert_not_called()


def test_launch_agent_gives_up_after_full_schedule(pb: PhantomBusterClient) -> None:
    with patch.object(pb, "_request", side_effect=_http_error(429)) as mock_req, \
         patch("clients.phantombuster.time.sleep") as mock_sleep, \
         pytest.raises(httpx.HTTPStatusError) as exc:
        pb.launch_agent("123")

    assert exc.value.response.status_code == 429
    # 1 initial attempt + 5 retries in the backoff schedule = 6 total.
    assert mock_req.call_count == 1 + len(PhantomBusterClient._LAUNCH_BACKOFF_SCHEDULE)
    assert [c.args[0] for c in mock_sleep.call_args_list] == list(
        PhantomBusterClient._LAUNCH_BACKOFF_SCHEDULE
    )
