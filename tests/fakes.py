"""Shared test fakes for required collaborator parameters.

``run_dm_sequencing`` (PR-17) and ``run_connection_requests`` (the #182 port)
both require a ``daily_run`` argument. Tests that don't assert on cap-charging
behaviour use this minimal stand-in.
"""
from unittest.mock import MagicMock

from workflows.daily_run import DailyRun


def fake_daily_run(remaining: int = 25) -> MagicMock:
    """A DailyRun stand-in for call sites that require the parameter.

    ``remaining()`` returns an int so ``min()`` arithmetic in the invite
    target-trim works; ``reserve_send`` returns a stable token;
    ``confirm_lease``/``release_lease`` are recorded no-ops queryable via
    MagicMock assertions.
    """
    mock = MagicMock(spec=DailyRun)
    mock.remaining.return_value = remaining
    mock.reserve_send.return_value = "fake-lease-token"
    return mock
