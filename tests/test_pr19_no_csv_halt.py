"""PR-19 B-SD-005: no-CSV halt + Part-B short-circuit.

Locks in:
- ``detect_responses`` raises ``NoCSVHalt`` when the inbox-scraper
  returns no CSV (was a silent ``return {..., "error": "no_csv"}``)
- Opens ``pb_csv_empty`` queue row + writes
  ``daily_run.reply_detection_status='failed'`` BEFORE raising
- cli.py catches ``NoCSVHalt`` and exits 2 (distinct from EX_TEMPFAIL=75)
- ``run_dm_sequencing`` short-circuits when
  ``daily_run.reply_detection_status='failed'``, opening
  ``dm_sequencing_blocked_on_reply_failure`` and returning early
- ``DailyRun.set_reply_detection_status`` writes the attribute via PATCH
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from tests.test_integration import _attio_with_full_schema
from workflows.daily_run import DailyRun
from workflows.detect_responses import NoCSVHalt


def _attio_with_dm_prospect():
    """Build an AttioClient mock returning one DM1_SENT prospect — enough
    to bypass the early ``no prospects in DM stages`` return so the
    inbox scrape (and the no-CSV halt path) actually runs."""
    entry = {
        "entry_id": "ent-1",
        "parent_record_id": "rec-1",
        "entry_values": {
            "stage": [{"status": {"title": "DM1 Sent"}}],
            "quality_score": [{"value": 80}],
            "persona": [{"option": {"title": "operations_leaders"}}],
            "language": [{"option": {"title": "es"}}],
            "last_contact_date": [{"value": "2026-05-19"}],
            "dm_step": [{"value": 1}],
        },
    }
    attio = MagicMock()
    attio.query_list_entries.return_value = [entry]
    return attio, entry


# ── DailyRun.set_reply_detection_status / get_reply_detection_status ────


def _fake_attio_response() -> httpx.Response:
    req = httpx.Request("PATCH", "https://api.attio.com/v2/x")
    return httpx.Response(200, request=req, json={"data": {}})


def test_set_reply_detection_status_writes_via_provider():
    """set_reply_detection_status writes through the provider's
    update_object_record (the adapter's retrying layer) so transient 429s don't
    leave reply_detection_status unwritten."""
    crm = MagicMock()

    run = DailyRun(
        crm=crm, record_id="rec_dr",
        run_date="2026-05-22", machine_id="host",
        run_id="run-1",
        initial_counters={"connections": 0, "messages": 0, "visits": 0},
    )
    run.set_reply_detection_status("failed")

    assert run.get_reply_detection_status() == "failed"
    obj, rid, values = crm.update_object_record.call_args[0]
    assert obj == "daily_run"
    assert rid == "rec_dr"
    assert values == {"reply_detection_status": "failed"}


def test_get_reply_detection_status_returns_none_before_set():
    crm = MagicMock()
    run = DailyRun(
        crm=crm, record_id="rec_dr",
        run_date="2026-05-22", machine_id="host",
        run_id="run-1",
        initial_counters={"connections": 0, "messages": 0, "visits": 0},
    )
    assert run.get_reply_detection_status() is None


# ── detect_responses no-CSV halt ────────────────────────────────────────


def test_detect_responses_raises_NoCSVHalt_on_empty_csv():
    """When ``pb.download_result_csv`` returns empty, detect_responses
    must raise ``NoCSVHalt`` — not a silent ``return {..., "error":
    "no_csv"}``. The §3.1 protection depends on Part-B short-circuiting
    on this signal."""
    from workflows.detect_responses import detect_responses

    attio, _ = _attio_with_dm_prospect()
    attio._request.return_value = {"data": []}  # for the escalate query
    pb = MagicMock()
    pb.launch_agent.return_value = MagicMock(container_id="c_test")
    pb.wait_for_completion.return_value = MagicMock(status="finished")
    pb.download_result_csv.return_value = ""  # empty CSV → halt

    with patch("workflows.detect_responses.RecordCache.get") as cache_get, \
         patch.dict("os.environ", {"ATTIO_LIST_ID": "list-1"}, clear=False):
        cache_get.return_value = ("Test Prospect", "ACME", "https://x", "", "")
        with pytest.raises(NoCSVHalt) as exc_info:
            detect_responses(attio, pb, "agent-inbox")

    assert exc_info.value.container_id == "c_test"
    assert "c_test" in exc_info.value.scrape_attempt_id


def test_detect_responses_opens_pb_csv_empty_queue_row_before_halting():
    """The queue row write must land BEFORE the raise — operator
    visibility is the durability guarantee."""
    from workflows.detect_responses import detect_responses

    create_calls: list[dict] = []

    def _attio_request(method, path, **kwargs):
        body = kwargs.get("json") or {}
        if "/query" in path:
            return {"data": []}
        # Capture create body.
        create_calls.append(body.get("data", {}).get("values", {}))
        return {"data": {"id": {"record_id": "rec_q"}, "values": {}}}

    attio, _ = _attio_with_dm_prospect()
    attio._request.side_effect = _attio_request

    pb = MagicMock()
    pb.launch_agent.return_value = MagicMock(container_id="c_xyz")
    pb.wait_for_completion.return_value = MagicMock(status="finished")
    pb.download_result_csv.return_value = ""

    with patch("workflows.detect_responses.RecordCache.get") as cache_get, \
         patch.dict("os.environ", {"ATTIO_LIST_ID": "list-1"}, clear=False):
        cache_get.return_value = ("Test Prospect", "ACME", "https://x", "", "")
        with pytest.raises(NoCSVHalt):
            detect_responses(attio, pb, "agent-inbox")

    # At least one create with type='pb_csv_empty'.
    pb_csv_empty_rows = [v for v in create_calls if v.get("type") == "pb_csv_empty"]
    assert pb_csv_empty_rows, (
        f"no pb_csv_empty queue row created before halt; captured: {create_calls}"
    )
    payload = pb_csv_empty_rows[0]
    # payload_json string carries the structured fields.
    import json
    payload_json = json.loads(payload["payload_json"])
    assert payload_json["container_id"] == "c_xyz"
    assert payload_json["observed_rows"] == 0
    assert payload_json["expected_min_rows"] == 1


def test_detect_responses_writes_reply_detection_status_failed_before_raising():
    """The daily_run row must carry reply_detection_status='failed' so
    Part-B can short-circuit on the next call. Write lands BEFORE the
    raise so a process crash mid-halt still records the failed state."""
    from workflows.detect_responses import detect_responses

    attio, _ = _attio_with_dm_prospect()
    attio._request.return_value = {"data": []}

    pb = MagicMock()
    pb.launch_agent.return_value = MagicMock(container_id="c_test")
    pb.wait_for_completion.return_value = MagicMock(status="finished")
    pb.download_result_csv.return_value = ""

    daily_run = MagicMock(spec=DailyRun)

    with patch("workflows.detect_responses.RecordCache.get") as cache_get, \
         patch.dict("os.environ", {"ATTIO_LIST_ID": "list-1"}, clear=False):
        cache_get.return_value = ("Test Prospect", "ACME", "https://x", "", "")
        with pytest.raises(NoCSVHalt):
            detect_responses(attio, pb, "agent-inbox", daily_run=daily_run)

    daily_run.set_reply_detection_status.assert_called_with("failed")


def test_detect_responses_success_path_writes_reply_detection_status_ok():
    """On successful reply detection (CSV present, processing
    completes), the daily_run row gets reply_detection_status='ok' so
    Part-B knows it's safe to proceed."""
    from workflows.detect_responses import detect_responses

    attio, _ = _attio_with_dm_prospect()
    attio._request.return_value = {"data": []}

    pb = MagicMock()
    pb.launch_agent.return_value = MagicMock(container_id="c_test")
    pb.wait_for_completion.return_value = MagicMock(status="finished")
    # Non-empty CSV with no useful rows; processing completes without
    # finding matches but does not halt.
    pb.download_result_csv.return_value = "participantFullName,lastMessageBody\n"

    daily_run = MagicMock(spec=DailyRun)

    with patch("workflows.detect_responses.RecordCache.get") as cache_get, \
         patch.dict("os.environ", {"ATTIO_LIST_ID": "list-1"}, clear=False):
        cache_get.return_value = ("Test Prospect", "ACME", "https://x", "", "")
        detect_responses(attio, pb, "agent-inbox", daily_run=daily_run)

    daily_run.set_reply_detection_status.assert_called_with("ok")


def test_detect_responses_no_dm_prospects_writes_reply_detection_status_ok():
    """Campaign-start / all-freshly-ACCEPTED day: no prospects sit in any DM
    stage, so response detection takes the clean early return. That is a CLEAN
    completion (detection ran, found nothing to check), NOT a no-op.

    The daily_run row MUST get reply_detection_status='ok'. send-dms's
    cross-process fail-closed guard (!= 'ok') would otherwise see None and
    REFUSE to send — silently blocking the very first DM1s of a campaign. No PB
    launch should happen on this path.
    """
    from workflows.detect_responses import detect_responses

    # Single entry in a NON-DM stage (ACCEPTED) → dm_prospects is empty.
    entry = {
        "entry_id": "ent-acc",
        "parent_record_id": "rec-acc",
        "entry_values": {
            "stage": [{"status": {"title": "Accepted"}}],
            "quality_score": [{"value": 80}],
            "persona": [{"option": {"title": "operations_leaders"}}],
            "language": [{"option": {"title": "es"}}],
            "last_contact_date": [{"value": "2026-05-19"}],
            "dm_step": [{"value": 0}],
        },
    }
    attio = MagicMock()
    attio.query_list_entries.return_value = [entry]

    pb = MagicMock()
    daily_run = MagicMock(spec=DailyRun)

    with patch("workflows.detect_responses.RecordCache.get") as cache_get, \
         patch.dict("os.environ", {"ATTIO_LIST_ID": "list-1"}, clear=False):
        cache_get.return_value = ("Test Prospect", "ACME", "https://x", "", "")
        result = detect_responses(attio, pb, "agent-inbox", daily_run=daily_run)

    # Clean completion → status marked 'ok' so send-dms is unblocked.
    daily_run.set_reply_detection_status.assert_called_once_with("ok")
    # Empty counts returned; no PB launch on the no-prospects path.
    assert result["detected"] == 0
    assert not pb.launch_agent.called


# ── Part-B short-circuit (run_dm_sequencing) ────────────────────────────


def test_run_dm_sequencing_short_circuits_when_reply_detection_failed():
    """When ``daily_run.get_reply_detection_status()`` returns
    'failed', ``run_dm_sequencing`` MUST return early without
    selecting due rows. §3.1 protection: a DM3 to a prospect whose
    reply might be in the unread inbox is a re-send risk."""
    from workflows.daily_check import run_dm_sequencing

    attio = MagicMock()
    pb = MagicMock()
    daily_run = MagicMock(spec=DailyRun)
    daily_run.get_reply_detection_status.return_value = "failed"
    daily_run.run_date = "2026-05-22"

    result = run_dm_sequencing(
        attio, pb, "msg-sender-id",
        daily_run=daily_run,
        dry_run=False, auto_confirm=True, cache=MagicMock(),
    )

    assert result.get("reason") == "reply_detection_failed"
    assert result.get("dm1") == 0
    assert result.get("dm2") == 0
    assert result.get("dm3") == 0
    # PB must not have been touched.
    assert not pb.launch_agent.called
    # Capacity ledger MUST not have been touched either.
    assert not daily_run.reserve_send.called


def test_run_dm_sequencing_opens_short_circuit_queue_row():
    """The short-circuit fires a ``dm_sequencing_blocked_on_reply_failure``
    queue row so operators see the downstream consequence of the
    upstream pb_csv_empty signal."""
    from workflows.daily_check import run_dm_sequencing

    create_calls: list[dict] = []

    def _attio_request(method, path, **kwargs):
        body = kwargs.get("json") or {}
        if "/query" in path:
            return {"data": []}
        create_calls.append(body.get("data", {}).get("values", {}))
        return {"data": {"id": {"record_id": "rec_q"}, "values": {}}}

    attio = MagicMock()
    attio._request.side_effect = _attio_request

    pb = MagicMock()
    daily_run = MagicMock(spec=DailyRun)
    daily_run.get_reply_detection_status.return_value = "failed"
    daily_run.run_date = "2026-05-22"

    run_dm_sequencing(
        attio, pb, "msg-sender-id",
        daily_run=daily_run,
        dry_run=False, auto_confirm=True, cache=MagicMock(),
    )

    blocked_rows = [
        v for v in create_calls
        if v.get("type") == "dm_sequencing_blocked_on_reply_failure"
    ]
    assert blocked_rows, (
        f"no dm_sequencing_blocked_on_reply_failure queue row created; "
        f"captured: {create_calls}"
    )
    import json
    payload = json.loads(blocked_rows[0]["payload_json"])
    assert payload["upstream_signal"] == "pb_csv_empty"
    assert payload["reason"] == "reply_detection_status=failed"


def test_run_dm_sequencing_proceeds_when_reply_detection_ok():
    """When reply_detection_status='ok', the short-circuit must NOT
    fire; normal queue selection continues. Sanity check that the
    short-circuit isn't over-eager."""
    from workflows.daily_check import run_dm_sequencing

    attio = _attio_with_full_schema()
    attio.query_list_entries.return_value = []
    pb = MagicMock()
    daily_run = MagicMock(spec=DailyRun)
    daily_run.get_reply_detection_status.return_value = "ok"
    daily_run.remaining.return_value = 30

    # With no entries, run_dm_sequencing returns with all-zero counts
    # via the "no DMs due" path — but it MUST get past the short-circuit
    # guard, which we verify by ensuring no blocked-queue row fired.
    # NOTE: patch _get_all_entries_with_raw (returns (raw, parsed) tuple);
    # the old _get_all_entries_parsed was renamed and is now a silent no-op.
    with patch("workflows.daily_check._get_all_entries_with_raw", return_value=([], [])), \
         patch.dict("os.environ", {"ATTIO_LIST_ID": "list-1"}, clear=False):
        result = run_dm_sequencing(
            attio, pb, "msg-sender-id",
            daily_run=daily_run,
            dry_run=True, auto_confirm=True, cache=MagicMock(),
        )

    assert result.get("reason") != "reply_detection_failed"


def test_run_dm_sequencing_proceeds_when_reply_detection_status_unset():
    """When reply_detection_status is None (e.g., dry-run never opened
    a real daily_run row, or Phase 0.5 was skipped), short-circuit
    MUST NOT fire — the guard is `== 'failed'`, not `!= 'ok'`."""
    from workflows.daily_check import run_dm_sequencing

    attio = _attio_with_full_schema()
    attio.query_list_entries.return_value = []
    pb = MagicMock()
    daily_run = MagicMock(spec=DailyRun)
    daily_run.get_reply_detection_status.return_value = None
    daily_run.remaining.return_value = 30

    # NOTE: patch _get_all_entries_with_raw (returns (raw, parsed) tuple);
    # the old _get_all_entries_parsed was renamed and is now a silent no-op.
    with patch("workflows.daily_check._get_all_entries_with_raw", return_value=([], [])), \
         patch.dict("os.environ", {"ATTIO_LIST_ID": "list-1"}, clear=False):
        result = run_dm_sequencing(
            attio, pb, "msg-sender-id",
            daily_run=daily_run,
            dry_run=True, auto_confirm=True, cache=MagicMock(),
        )

    assert result.get("reason") != "reply_detection_failed"


# ── CLI exit code 2 on NoCSVHalt ────────────────────────────────────────


# ── Fold-in tests for QA convergence ────────────────────────────────────


def test_DryRunDailyRun_supports_reply_detection_status_methods():
    """PR-19 fold-in (salesman-daily-QA BLOCKING): ``_DryRunDailyRun``
    must implement ``get_reply_detection_status`` and
    ``set_reply_detection_status`` or ``run_dm_sequencing`` crashes
    with AttributeError in dry-run mode (the function calls
    ``get_reply_detection_status`` as its very first line).
    """
    from cli import _DryRunDailyRun

    stub = _DryRunDailyRun()
    # get returns None — short-circuit guard ``== "failed"`` is False,
    # so run_dm_sequencing proceeds (correct dry-run behaviour).
    assert stub.get_reply_detection_status() is None
    # set is a no-op — no Attio write in dry-run.
    stub.set_reply_detection_status("failed")
    # After "set", get still returns None (the stub doesn't cache
    # state; dry-run never persists status).
    assert stub.get_reply_detection_status() is None


def test_detect_responses_writes_queue_row_BEFORE_status_patch():
    """PR-19 fold-in (pr-test-analyzer IMPORTANT joint-ordering):
    the queue row MUST land before the daily_run status PATCH.
    A future refactor that flips the order would let a status-PATCH
    failure (silenced before fold-in) leave operators with no signal.
    """
    from workflows.detect_responses import detect_responses

    call_order: list[str] = []

    attio, _ = _attio_with_dm_prospect()

    def _capture_request(method, path, **kwargs):
        if "/query" in path:
            return {"data": []}
        if "/operator_review_queue/records" in path:
            call_order.append("queue_row")
        return {"data": {"id": {"record_id": "rec_q"}, "values": {}}}

    attio._request.side_effect = _capture_request

    daily_run = MagicMock(spec=DailyRun)
    daily_run.set_reply_detection_status.side_effect = (
        lambda *_, **__: call_order.append("status_patch")
    )

    pb = MagicMock()
    pb.launch_agent.return_value = MagicMock(container_id="c_xyz")
    pb.wait_for_completion.return_value = MagicMock(status="finished")
    pb.download_result_csv.return_value = ""

    with patch("workflows.detect_responses.RecordCache.get") as cache_get, \
         patch.dict("os.environ", {"ATTIO_LIST_ID": "list-1"}, clear=False):
        cache_get.return_value = ("Test", "ACME", "https://x", "", "")
        with pytest.raises(NoCSVHalt):
            detect_responses(attio, pb, "agent-inbox", daily_run=daily_run)

    # Queue row write must appear strictly before status patch.
    assert "queue_row" in call_order
    assert "status_patch" in call_order
    qr_idx = call_order.index("queue_row")
    st_idx = call_order.index("status_patch")
    assert qr_idx < st_idx, (
        f"queue_row must land before status_patch (durable signal first); "
        f"got order: {call_order}"
    )


def test_detect_responses_halts_AND_logs_when_status_patch_fails(capsys):
    """PR-19 fold-in (silent-failure-hunter + engineer-QA 3-agent
    convergence): when ``set_reply_detection_status`` raises an
    httpx error, the halt must still fire AND the failure must be
    operator-visible via stderr CRITICAL — not silently swallowed.

    The cross-run §3.1 protection depends on Attio carrying the
    'failed' status. If the PATCH fails, the next run's Part-B
    short-circuit won't fire, and DMs could proceed on a prospect
    whose reply may be sitting unread. Operators MUST see the loud
    signal so they can manually halt Part-B.
    """
    from workflows.detect_responses import detect_responses

    attio, _ = _attio_with_dm_prospect()
    attio._request.return_value = {"data": []}

    pb = MagicMock()
    pb.launch_agent.return_value = MagicMock(container_id="c_test")
    pb.wait_for_completion.return_value = MagicMock(status="finished")
    pb.download_result_csv.return_value = ""

    daily_run = MagicMock(spec=DailyRun)
    req = httpx.Request("PATCH", "https://api.attio.com/v2/x")
    daily_run.set_reply_detection_status.side_effect = httpx.HTTPStatusError(
        "503", request=req, response=httpx.Response(503, request=req),
    )

    with patch("workflows.detect_responses.RecordCache.get") as cache_get, \
         patch.dict("os.environ", {"ATTIO_LIST_ID": "list-1"}, clear=False):
        cache_get.return_value = ("Test", "ACME", "https://x", "", "")
        # Halt must STILL fire — durability signal is the queue row.
        with pytest.raises(NoCSVHalt):
            detect_responses(attio, pb, "agent-inbox", daily_run=daily_run)

    captured = capsys.readouterr()
    # Stderr must carry the CRITICAL marker so operators know the
    # cross-run protection signal didn't persist.
    assert "CRITICAL" in captured.err
    assert "reply_detection_status='failed'" in captured.err
    assert "PATCH failed" in captured.err


def test_pb_csv_empty_idempotency_key_uses_container_id_only():
    """PR-19 fold-in (code-reviewer IMPORTANT #1 + salesman-daily
    IMPORTANT): two consecutive halts on the same PB container must
    refresh the same queue row (idempotent on container_id) — not
    open multiple rows via a time-based key.
    """
    from workflows.detect_responses import detect_responses

    captured_keys: list[str] = []

    attio, _ = _attio_with_dm_prospect()

    def _request_cap(method, path, **kwargs):
        body = kwargs.get("json") or {}
        if "/query" in path:
            return {"data": []}
        values = body.get("data", {}).get("values", {})
        if values.get("type") == "pb_csv_empty":
            captured_keys.append(values.get("idempotency_key", ""))
        return {"data": {"id": {"record_id": "rec_q"}, "values": {}}}

    attio._request.side_effect = _request_cap

    pb = MagicMock()
    pb.launch_agent.return_value = MagicMock(container_id="c_stable")
    pb.wait_for_completion.return_value = MagicMock(status="finished")
    pb.download_result_csv.return_value = ""

    with patch("workflows.detect_responses.RecordCache.get") as cache_get, \
         patch.dict("os.environ", {"ATTIO_LIST_ID": "list-1"}, clear=False):
        cache_get.return_value = ("Test", "ACME", "https://x", "", "")
        with pytest.raises(NoCSVHalt):
            detect_responses(attio, pb, "agent-inbox")
        with pytest.raises(NoCSVHalt):
            detect_responses(attio, pb, "agent-inbox")

    assert len(captured_keys) == 2
    # Both halts on the same container_id must produce the same
    # idempotency key so escalate() refreshes (not duplicates).
    assert captured_keys[0] == captured_keys[1] == "c_stable", (
        f"idempotency keys should match for same container; got {captured_keys}"
    )


@patch.dict("os.environ", {
    "ATTIO_API_KEY": "fake", "PHANTOMBUSTER_API_KEY": "fake",
    "ATTIO_LIST_ID": "list-1",
    "PB_INBOX_SCRAPER_ID": "agent-inbox",
})
def test_cli_daily_exits_2_on_NoCSVHalt():
    """The CLI must exit with code 2 (NOT 75 EX_TEMPFAIL) when
    detect_responses raises NoCSVHalt. The distinction matters: 75
    means "retry later" (lock contention), 2 means "operator
    intervention required" (the queue row is the call to action)."""
    from click.testing import CliRunner

    runner = CliRunner()

    with patch("workflows.run_lock.acquire_run_lock") as mock_lock, \
         patch("workflows.audit.AuditLogger") as mock_audit, \
         patch("clients.attio.AttioClient") as mock_attio_cls, \
         patch("clients.phantombuster.PhantomBusterClient") as mock_pb_cls, \
         patch("workflows.daily_run.open_daily_run") as mock_open_dr, \
         patch("workflows.detect_responses.detect_responses") as mock_detect, \
         patch("workflows.record_cache.preload_pipeline_persons", return_value=0), \
         patch("workflows.record_cache.RecordCache"), \
         patch("workflows.daily_check.detect_accepted_connections"):
        mock_lock.return_value.__enter__ = lambda self: None
        mock_lock.return_value.__exit__ = lambda self, *a: None
        mock_audit.return_value.__enter__ = lambda self: MagicMock()
        mock_audit.return_value.__exit__ = lambda self, *a: None
        attio_instance = MagicMock()
        attio_instance.query_list_entries.return_value = []
        mock_attio_cls.return_value.__enter__ = lambda self: attio_instance
        mock_attio_cls.return_value.__exit__ = lambda self, *a: None
        mock_pb_cls.return_value.__enter__ = lambda self: MagicMock()
        mock_pb_cls.return_value.__exit__ = lambda self, *a: None

        # daily_run context manager yields a real-ish DailyRun.
        fake_dr = MagicMock(spec=DailyRun)
        cm = MagicMock()
        cm.__enter__.return_value = fake_dr
        cm.__exit__.return_value = False
        mock_open_dr.return_value = cm

        mock_detect.side_effect = NoCSVHalt(
            container_id="c_test", scrape_attempt_id="c_test|123",
        )

        from cli import cli
        result = runner.invoke(cli, ["daily", "--yes"], catch_exceptions=False)

    assert result.exit_code == 2, (
        f"expected exit 2 (operator intervention required), got "
        f"{result.exit_code}; output={result.output!r}"
    )
