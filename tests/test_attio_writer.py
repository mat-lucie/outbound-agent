"""Tests for clients/attio_writer.py (F-PR-4)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from clients.attio_writer import (
    MAX_ATTEMPTS,
    AttioMonotonicityViolation,
    AttioPermanentError,
    AttioRateLimitExhausted,
    AttioTerminalClassRegression,
    AttioWriter,
    WriteIntent,
)
from clients.attio_writer_registry import UnauthorizedAttioWriteError


def _success_response(body: dict | None = None) -> httpx.Response:
    """Build an httpx.Response that the writer's resp.json()/raise_for_status
    consumers will accept as a successful PATCH."""
    body = body or {"data": {"id": {"record_id": "rec_x"}, "values": {}}}
    req = httpx.Request("PATCH", "https://api.attio.com/v2/x")
    return httpx.Response(200, request=req, json=body)


@pytest.fixture
def mock_attio():
    """AttioClient mock with a working `_client.request` that returns
    a 200 OK response. The writer bypasses `attio._request` (which has
    its own retry loop) and calls `attio._client.request` directly per
    engineer-QA-build4 #1."""
    client = MagicMock()
    client._client = MagicMock()
    client._client.request.return_value = _success_response()
    return client


@pytest.fixture
def dlq_tmp(_isolate_attio_dlq):
    """The per-test DLQ dir, already redirected away from the prod DLQ by the
    autouse ``_isolate_attio_dlq`` conftest fixture. Kept as a named alias so the
    tests below can assert on the written ``attio-*.jsonl`` files."""
    return _isolate_attio_dlq


def test_dlq_dir_isolated_from_real_home() -> None:
    """Regression guard: the autouse ``_isolate_attio_dlq`` conftest fixture must
    redirect DLQ_DIR away from the operator's real ``~/.outbound-agent/dlq`` so no
    test ever appends a forensic row to the prod DLQ (which would pollute it and
    risk masking a genuine prod failure)."""
    from clients import attio_writer

    assert Path.home() / ".outbound-agent" / "dlq" != attio_writer.DLQ_DIR
    assert Path.home() not in attio_writer.DLQ_DIR.parents


def _error_response(status: int, body: str = "") -> httpx.Response:
    """Build an httpx.Response with a non-2xx status. The writer's
    `resp.raise_for_status()` will turn this into an httpx.HTTPStatusError."""
    req = httpx.Request("PATCH", "https://api.attio.com/v2/x")
    return httpx.Response(status, request=req, content=body.encode())


def _make_intent(**overrides) -> WriteIntent:
    """Helper: build a fully-formed WriteIntent with sensible defaults
    for the §3.1-critical sole-writer attribute experiment_id."""
    defaults = dict(
        object="linkedin_outreach",
        record_id="rec_abc",
        updates={"experiment_id": "exp_test_2026"},
        prior_values={"experiment_id": None},
        writer_module="workflows.weekly_prospect._build_prospect_entry_attrs",
    )
    defaults.update(overrides)
    return WriteIntent(**defaults)


class TestWriteOwnerEnforcement:
    """§3.15 registry enforcement — unauthorized callers blocked at
    the AttioWriter boundary before any network I/O."""

    def test_authorized_writer_passes(self, mock_attio):
        writer = AttioWriter(attio=mock_attio)
        writer.apply(_make_intent())  # should not raise
        assert mock_attio._client.request.called

    def test_unauthorized_writer_blocked(self, mock_attio):
        writer = AttioWriter(attio=mock_attio)
        with pytest.raises(UnauthorizedAttioWriteError, match="not an authorized writer"):
            writer.apply(_make_intent(writer_module="workflows.malicious_module"))
        # Crucially: no network I/O happened.
        assert not mock_attio._client.request.called

    def test_missing_writer_module_blocked(self, mock_attio):
        writer = AttioWriter(attio=mock_attio)
        with pytest.raises(UnauthorizedAttioWriteError, match="missing writer_module"):
            writer.apply(_make_intent(writer_module=""))

    def test_special_alias_bypass(self, mock_attio):
        """§3.20 canary script writes ANY attribute via __bootstrap_canary__."""
        writer = AttioWriter(attio=mock_attio)
        writer.apply(WriteIntent(
            object="linkedin_outreach",
            record_id="rec_canary",
            updates={"some_random_attr": "test"},  # not in registry
            writer_module="__bootstrap_canary__",
        ))
        assert mock_attio._client.request.called


class TestStageMonotonicity:
    """Rank monotonicity enforcement via F-PR-1's STAGE_RANK."""

    def test_forward_transition_passes(self, mock_attio):
        writer = AttioWriter(attio=mock_attio)
        writer.apply(_make_intent(
            updates={"stage": "DM1 Sent"},
            prior_values={"stage": "Accepted"},
            writer_module="workflows.daily_check.run_dm_sequencing",
        ))

    def test_rank_regression_blocked(self, mock_attio):
        writer = AttioWriter(attio=mock_attio)
        with pytest.raises(AttioMonotonicityViolation, match="regresses rank"):
            writer.apply(_make_intent(
                updates={"stage": "Prospect"},
                prior_values={"stage": "DM2 Sent"},
                writer_module="workflows.daily_check.run_dm_sequencing",
            ))
        assert not mock_attio._client.request.called

    def test_nurture_reengagement_passes_when_eligible(self, mock_attio):
        """NURTURE → DM1_SENT is in NURTURE_REENGAGEMENT_ALLOWED.
        With is_send_eligible=True AND experiment_id_frozen_at is
        explicitly present in prior_values (not archaeology), AttioWriter
        allows the bypass."""
        writer = AttioWriter(attio=mock_attio)
        writer.apply(_make_intent(
            updates={"stage": "DM1 Sent"},
            prior_values={
                "stage": "Nurture",
                # advertiser-daily-QA-build4 #1: explicit presence required;
                # `None` is a valid value (legacy_pre_tsv_era equivalent).
                "experiment_id_frozen_at": None,
            },
            writer_module="workflows.daily_check.run_dm_sequencing",
        ))
        assert mock_attio._client.request.called

    def test_nurture_reengagement_requires_explicit_frozen_at(self, mock_attio):
        """advertiser-daily-QA-build4 #1: the NURTURE bypass MUST require
        `experiment_id_frozen_at` to be explicitly present in prior_values.
        A caller that omits the field would silently fail open
        (is_send_eligible reads .get() which returns None and passes).
        Guard forces the caller to acknowledge the field exists."""
        writer = AttioWriter(attio=mock_attio)
        with pytest.raises(AttioMonotonicityViolation, match="experiment_id_frozen_at"):
            writer.apply(_make_intent(
                updates={"stage": "DM1 Sent"},
                prior_values={"stage": "Nurture"},  # missing frozen_at
                writer_module="workflows.daily_check.run_dm_sequencing",
            ))

    def test_nurture_reengagement_blocked_for_archaeology_row(self, mock_attio):
        writer = AttioWriter(attio=mock_attio)
        with pytest.raises(AttioMonotonicityViolation, match="is_send_eligible"):
            writer.apply(_make_intent(
                updates={"stage": "DM1 Sent"},
                prior_values={
                    "stage": "Nurture",
                    "experiment_id_frozen_at": "legacy_inferred_by_archaeology",
                },
                writer_module="workflows.daily_check.run_dm_sequencing",
            ))

    def test_new_entry_creation_passes(self, mock_attio):
        """No prior stage means it's a fresh record; no monotonicity to enforce.
        Uses a writer authorized for `stage` (run_connection_requests on the
        Prospect→CONNECTION_SENT path)."""
        writer = AttioWriter(attio=mock_attio)
        writer.apply(_make_intent(
            updates={"stage": "Connection Sent"},
            prior_values={},  # fresh — no prior stage to compare against
            writer_module="workflows.daily_check.run_connection_requests",
        ))


class TestTerminalClassRegression:
    """Cross-class terminal flip block per F-PR-1's terminal_class."""

    def test_positive_to_negative_blocked(self, mock_attio):
        writer = AttioWriter(attio=mock_attio)
        with pytest.raises(AttioTerminalClassRegression, match="terminal-class flip"):
            writer.apply(_make_intent(
                updates={"stage": "Not Interested"},
                prior_values={"stage": "Call Booked"},
                writer_module="workflows.daily_check.run_dm_sequencing",
            ))

    def test_positive_to_negative_allowed_with_opt_in(self, mock_attio):
        writer = AttioWriter(attio=mock_attio)
        writer.apply(WriteIntent(
            object="linkedin_outreach",
            record_id="rec_x",
            updates={"stage": "Not Interested"},
            prior_values={"stage": "Call Booked"},
            writer_module="workflows.daily_check.run_dm_sequencing",
            allow_terminal_class_change=True,
        ))
        assert mock_attio._client.request.called

    def test_responded_to_defensive_hold_passes(self, mock_attio):
        """§3.6 backfill: RESPONDED is None-class, DEFENSIVE_HOLD is
        'defensive'. The terminal-class check returns None for one side
        so the regression block doesn't fire; the rank check (90 → 95)
        is monotonic."""
        writer = AttioWriter(attio=mock_attio)
        writer.apply(WriteIntent(
            object="linkedin_outreach",
            record_id="rec_x",
            updates={"stage": "Defensive Hold"},
            prior_values={"stage": "Responded"},
            writer_module="workflows.daily_check.run_dm_sequencing",
        ))


class TestRetryAndDlq:
    """Retry policy + DLQ + escalate semantics."""

    def test_429_retried_then_succeeds(self, mock_attio, dlq_tmp):
        # 2 transient errors, then a success. Writer's resp.raise_for_status()
        # turns the 429 responses into httpx.HTTPStatusError, which the retry
        # loop catches.
        mock_attio._client.request.side_effect = [
            _error_response(429),
            _error_response(429),
            _success_response(),
        ]
        writer = AttioWriter(attio=mock_attio)
        result = writer.apply(_make_intent())
        # 3 attempts; final returned the success dict.
        assert mock_attio._client.request.call_count == 3
        assert "values" in result

    def test_permanent_4xx_no_retry_then_dlq(self, mock_attio, dlq_tmp, monkeypatch):
        mock_attio._client.request.return_value = _error_response(400, "bad request")
        # Stub escalate so the DLQ test doesn't trip on missing Attio.
        monkeypatch.setattr(
            "workflows.escalation.escalate",
            lambda **kw: {},
        )
        writer = AttioWriter(attio=mock_attio)
        with pytest.raises(AttioPermanentError):
            writer.apply(_make_intent())
        # Only one attempt — no retry on 400.
        assert mock_attio._client.request.call_count == 1
        # DLQ line written.
        dlq_files = list(dlq_tmp.glob("attio-*.jsonl"))
        assert len(dlq_files) == 1
        line = json.loads(dlq_files[0].read_text().strip())
        assert line["kind"] == "permanent_http_400"
        assert line["object"] == "linkedin_outreach"
        assert line["record_id"] == "rec_abc"

    def test_exhausted_retries_raises_and_dlqs(
        self, mock_attio, dlq_tmp, monkeypatch
    ):
        # Mock a transient error on every attempt.
        mock_attio._client.request.return_value = _error_response(503)
        # Skip the random sleep so the test isn't slow.
        monkeypatch.setattr(
            "clients.attio_writer.AttioWriter._sleep_with_jitter",
            lambda self, attempt: None,
        )
        monkeypatch.setattr(
            "workflows.escalation.escalate",
            lambda **kw: {},
        )
        writer = AttioWriter(attio=mock_attio)
        with pytest.raises(AttioRateLimitExhausted):
            writer.apply(_make_intent())
        assert mock_attio._client.request.call_count == MAX_ATTEMPTS
        # DLQ recorded.
        dlq_files = list(dlq_tmp.glob("attio-*.jsonl"))
        assert len(dlq_files) == 1
        line = json.loads(dlq_files[0].read_text().strip())
        assert line["kind"] == "max_attempts"

    def test_timeout_is_retryable(self, mock_attio, monkeypatch):
        # Timeout exceptions are RAISED by _client.request itself (not
        # via raise_for_status). Use side_effect for those.
        mock_attio._client.request.side_effect = [
            httpx.ReadTimeout("slow"),
            _success_response(),
        ]
        monkeypatch.setattr(
            "clients.attio_writer.AttioWriter._sleep_with_jitter",
            lambda self, attempt: None,
        )
        writer = AttioWriter(attio=mock_attio)
        result = writer.apply(_make_intent())
        assert mock_attio._client.request.call_count == 2
        assert "values" in result


class TestListEntryWrites:
    """List entry writes dispatch via ``attio.update_list_entry`` which
    targets ``/lists/{list_id}/entries/{entry_id}`` internally.

    Wave-2-B note: AttioWriter routes list entries through the existing
    ``AttioClient.update_list_entry`` helper so legacy call sites (and
    their unit tests that mocked the helper directly) keep working
    after every Attio write is funneled through AttioWriter for §3.15
    registry enforcement.
    """

    def test_list_entry_dispatches_via_update_list_entry(self, mock_attio):
        writer = AttioWriter(attio=mock_attio)
        writer.apply(WriteIntent(
            object="linkedin_outreach",
            record_id="entry_abc",
            updates={"stage": "DM1 Sent"},
            prior_values={"stage": "Accepted"},
            writer_module="workflows.daily_check.run_dm_sequencing",
            is_list_entry=True,
            list_id="list_xyz",
        ))
        assert mock_attio.update_list_entry.call_count == 1
        kwargs = mock_attio.update_list_entry.call_args.kwargs
        assert kwargs["entry_id"] == "entry_abc"
        assert kwargs["list_id"] == "list_xyz"
        assert kwargs["entry_attributes"] == {"stage": "DM1 Sent"}

    def test_list_entry_without_list_id_raises(self, mock_attio):
        from clients.attio_writer import AttioWriteFailed
        writer = AttioWriter(attio=mock_attio)
        with pytest.raises(AttioWriteFailed, match="is_list_entry"):
            writer.apply(WriteIntent(
                object="linkedin_outreach",
                record_id="entry_abc",
                updates={"stage": "DM1 Sent"},
                prior_values={"stage": "Accepted"},
                writer_module="workflows.daily_check.run_dm_sequencing",
                is_list_entry=True,
                list_id=None,
            ))


class TestAtomicity:
    """Single-PATCH multi-attribute writes commit atomically server-side.
    The whole WriteIntent.updates dict travels in ONE Attio call."""

    def test_multi_attribute_writes_in_single_patch(self, mock_attio):
        writer = AttioWriter(attio=mock_attio)
        writer.apply(_make_intent(
            updates={
                "experiment_id": "exp_test",
                "experiment_id_frozen_at": "prospect",
            },
            prior_values={
                "experiment_id": None,
                "experiment_id_frozen_at": None,
            },
            writer_module="workflows.weekly_prospect._build_prospect_entry_attrs",
        ))
        # ONE PATCH call carrying BOTH attributes.
        assert mock_attio._client.request.call_count == 1
        body = mock_attio._client.request.call_args[1]["json"]["data"]["values"]
        assert body["experiment_id"] == "exp_test"
        assert body["experiment_id_frozen_at"] == "prospect"


class TestBypassesInnerRetry:
    """engineer-QA-build4 #1 — for object types without a dedicated
    AttioClient update helper (linkedin_outreach record-level,
    daily_run, weekly_kpi_snapshot, etc.), AttioWriter must call
    _client.request DIRECTLY, not _request (which has its own
    3-attempt retry that would compound to 15 HTTP calls under a 429
    storm).

    Wave-2-B carve-out: for ``companies``, ``people``, and list-entry
    writes, AttioWriter intentionally dispatches through the matching
    AttioClient helper (which DOES use _request internally) so legacy
    mocks of those helpers keep working — the retry-stacking trade-off
    is documented at ``AttioWriter._single_patch``.
    """

    def test_writer_calls_raw_httpx_not_attio_internal_request(self, mock_attio):
        writer = AttioWriter(attio=mock_attio)
        writer.apply(_make_intent())
        # The default `_make_intent()` targets
        # `object='linkedin_outreach'` record-level (not list-entry),
        # so it falls through to the raw-httpx path.
        assert not mock_attio._request.called
        assert mock_attio._client.request.called


class TestRetryStopsAtDeadline:
    """The 300s overall deadline kicks in even if attempts < MAX_ATTEMPTS."""

    def test_deadline_short_circuits_retry(self, mock_attio, dlq_tmp, monkeypatch):
        # Force the deadline to have already passed.
        import clients.attio_writer
        monkeypatch.setattr(
            clients.attio_writer, "RETRY_BUDGET_SECONDS", -1,
        )
        monkeypatch.setattr(
            "workflows.escalation.escalate",
            lambda **kw: {},
        )
        # First call raises retryable — but deadline is already past.
        mock_attio._client.request.return_value = _error_response(503)
        writer = AttioWriter(attio=mock_attio)
        with pytest.raises(AttioRateLimitExhausted, match="deadline"):
            writer.apply(_make_intent())
