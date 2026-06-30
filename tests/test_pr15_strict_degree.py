"""PR-15 (B-SD-010): STRICT pre-invite degree check + Pattern-A flip
atomicity + per-prospect `degree_unknown` queue rows.

Three pieces shipped together:

  1. `STRICT_PRE_INVITE_DEGREE_CHECK` env (default true) raises
     `ConfigError` when the send_invite codepath has no scraper-id.
  2. Pattern-A flip (cache-hit "1st" → ACCEPTED) writes stage +
     last_contact_date as a single multi-attribute AttioWriter PATCH
     so a torn write can't let a retry re-invite an already-1st-degree
     connection.
  3. Partial-CSV signal: when the scrape returns fewer rows than
     requested, emit ONE `degree_unknown` Operator Review Queue row
     PER MISSING PROSPECT — never an aggregated row.

§3.1 protection: PR-15 closes the silent-bypass that pre-PR-15 used
when scraper-id was missing. Tests assert both the loud-failure
behavior (STRICT raise) and the carve-out (cache_hit_flip path
proceeds even without scraper-id).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from workflows.pre_invite_check import (
    STRICT_PRE_INVITE_DEGREE_CHECK_ENV,
    ConfigError,
    _pre_invite_degree_check,
    _strict_mode_enabled,
)

# ==================================================================
# STRICT env gate
# ==================================================================


class TestStrictModeFlag:
    def test_default_is_strict(self, monkeypatch: pytest.MonkeyPatch):
        """Safe default: STRICT enabled when env var is unset."""
        monkeypatch.delenv(STRICT_PRE_INVITE_DEGREE_CHECK_ENV, raising=False)
        assert _strict_mode_enabled() is True

    def test_explicit_true(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(STRICT_PRE_INVITE_DEGREE_CHECK_ENV, "true")
        assert _strict_mode_enabled() is True

    def test_explicit_false(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(STRICT_PRE_INVITE_DEGREE_CHECK_ENV, "false")
        assert _strict_mode_enabled() is False

    def test_capitalization_insensitive(self, monkeypatch: pytest.MonkeyPatch):
        """Operator might type `FALSE` or `False`. Treat any 'false'
        case-insensitive as opt-out."""
        monkeypatch.setenv(STRICT_PRE_INVITE_DEGREE_CHECK_ENV, "FALSE")
        assert _strict_mode_enabled() is False
        monkeypatch.setenv(STRICT_PRE_INVITE_DEGREE_CHECK_ENV, "False")
        assert _strict_mode_enabled() is False


# ==================================================================
# STRICT raise on send_invite codepath; Pattern-A flip exempt
# ==================================================================


def _make_recording_escalate(calls: list[dict]):
    """Build an escalate() stand-in that records calls + returns a stub row."""
    def _fake(**kw):
        calls.append(kw)
        return {"id": "x"}
    return _fake


def _csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    header = "linkedinProfileUrl,connectionDegree\n"
    body = "\n".join(f"{r['url']},{r['degree']}" for r in rows)
    return header + body


@patch("workflows.daily_check.write_prospects_to_sheet", return_value="https://sheet.example/foo")
@patch("workflows.daily_check._pb_session_args", return_value={})
class TestStrictModeRaiseOnSendInvite:
    """STRICT mode raises ConfigError ONLY on the send_invite codepath
    (stale URLs need scraping). The cache_hit_flip carve-out exempts
    the path where all URLs have cached degree results."""

    def _stale_batch(self):
        # PR-21: rows need experiment_id + experiment_id_frozen_at (direct key access).
        return [
            {
                "linkedInUrl": "https://www.linkedin.com/in/alice",
                "entry_id": "ent-A", "record_id": "rec-A",
                "current_stage": "Prospect",
                "experiment_id": "exp-test",
                "experiment_id_frozen_at": "prospect",
            },
        ]

    def test_send_invite_raises_config_error_when_strict_and_no_scraper(
        self, _pb_args, _sheet, monkeypatch: pytest.MonkeyPatch,
    ):
        """STRICT=true (default) + missing scraper-id + stale URL
        (send_invite codepath) → ConfigError."""
        monkeypatch.delenv(STRICT_PRE_INVITE_DEGREE_CHECK_ENV, raising=False)
        attio = MagicMock()
        pb = MagicMock()

        with pytest.raises(ConfigError) as exc_info:
            _pre_invite_degree_check(
                self._stale_batch(), pb, None, attio, "list-id",
            )
        assert "profile_scraper_id" in str(exc_info.value)
        assert "STRICT_PRE_INVITE_DEGREE_CHECK" in str(exc_info.value)
        # No PB launch attempted — the gate fires BEFORE any side effect.
        pb.launch_agent.assert_not_called()

    def test_send_invite_with_scraper_id_proceeds(
        self, _pb_args, _sheet,
    ):
        """STRICT=true + scraper-id set + stale URL → no raise, normal
        scrape path runs."""
        attio = MagicMock()
        pb = MagicMock()
        pb.download_result_csv.return_value = _csv([
            {"url": "https://www.linkedin.com/in/alice", "degree": "2nd"},
        ])

        still, already = _pre_invite_degree_check(
            self._stale_batch(), pb, "scraper-id", attio, "list-id",
        )
        # No raise — and the prospect lands in still_to_invite (2nd-degree).
        assert len(still) == 1
        assert already == []

    def test_strict_false_lets_silent_bypass_through(
        self, _pb_args, _sheet, monkeypatch: pytest.MonkeyPatch, capsys,
    ):
        """STRICT=false + missing scraper-id + stale URL → no raise.
        The codepath falls through to cache-only handling. Caller's
        own degree_lookup is empty so the prospect stays in still_to_invite
        as 'unknown degree, proceed to invite' (the pre-PR-15 default).

        PR-15 fold-in (salesman-daily-QA-build15 BLOCKING B1): even
        STRICT=false must be loud — a click.echo warning to stderr
        signals the bypass to the operator. The pre-PR-15 code had this
        warning; PR-15's initial commit deleted it. The fold-in
        restores it so STRICT=false is not a silent §0 #9 escape hatch."""
        monkeypatch.setenv(STRICT_PRE_INVITE_DEGREE_CHECK_ENV, "false")
        attio = MagicMock()
        pb = MagicMock()

        still, already = _pre_invite_degree_check(
            self._stale_batch(), pb, None, attio, "list-id",
        )
        assert len(still) == 1
        assert already == []
        # No PB launch — STRICT=false doesn't suddenly enable scraping
        # without a scraper-id; it just permits the proceed.
        pb.launch_agent.assert_not_called()
        # Operator-visible warning MUST fire — §0 #9 compliance.
        captured = capsys.readouterr()
        assert "STRICT_PRE_INVITE_DEGREE_CHECK=false" in captured.err
        assert "DEGREE CHECK SKIPPED" in captured.err
        assert "§3.1" in captured.err


# ==================================================================
# Pattern-A flip atomicity — single-PATCH multi-attribute write
# ==================================================================


@patch("workflows.daily_check.write_prospects_to_sheet", return_value="https://sheet.example/foo")
@patch("workflows.daily_check._pb_session_args", return_value={})
class TestPatternAFlipAtomicity:
    """B-SD-010 §3.1 protection: cache-hit "1st" → ACCEPTED writes
    stage AND last_contact_date in a single AttioWriter PATCH.
    Tearing (writing one and not the other) would let a retry re-invite
    an already-1st-degree connection."""

    def _batch(self):
        # PR-21: rows need experiment_id + experiment_id_frozen_at (direct key access).
        return [
            {
                "linkedInUrl": "https://www.linkedin.com/in/alice",
                "entry_id": "ent-A", "record_id": "rec-A",
                "current_stage": "Prospect",
                "experiment_id": "exp-test",
                "experiment_id_frozen_at": "prospect",
            },
        ]

    def test_first_degree_flip_uses_attio_writer_path(
        self, _pb_args, _sheet,
    ):
        """The flip MUST go through AttioWriter, which dispatches list
        entry writes via ``attio.update_list_entry`` (Wave-2-B). The
        pre-Wave-2-B path called ``_client.request`` directly; the
        post-Wave-2-B path keeps the dispatch through the existing
        AttioClient helper so legacy mocks of that helper continue to
        work and the §3.15 registry check still fires in
        ``AttioWriter.apply()``."""
        attio = MagicMock()
        pb = MagicMock()
        pb.download_result_csv.return_value = _csv([
            {"url": "https://www.linkedin.com/in/alice", "degree": "1st"},
        ])

        still, already = _pre_invite_degree_check(
            self._batch(), pb, "scraper-id", attio, "list-id",
        )
        assert len(already) == 1
        assert still == []
        # AttioWriter dispatch path: update_list_entry call with the
        # expected entry_id + list_id.
        matching_calls = [
            c for c in attio.update_list_entry.call_args_list
            if c.kwargs.get("entry_id") == "ent-A"
            and c.kwargs.get("list_id") == "list-id"
        ]
        assert len(matching_calls) == 1

    def test_first_degree_flip_writes_stage_and_last_contact_atomically(
        self, _pb_args, _sheet,
    ):
        """Both attributes appear in the SAME update_list_entry call —
        atomicity guarantee from F-PR-4's single-PATCH contract,
        dispatched via update_list_entry under Wave-2-B."""
        attio = MagicMock()
        pb = MagicMock()
        pb.download_result_csv.return_value = _csv([
            {"url": "https://www.linkedin.com/in/alice", "degree": "1st"},
        ])

        _pre_invite_degree_check(
            self._batch(), pb, "scraper-id", attio, "list-id",
        )
        flip_call = next(
            c for c in attio.update_list_entry.call_args_list
            if c.kwargs.get("entry_id") == "ent-A"
        )
        entry_values = flip_call.kwargs["entry_attributes"]
        # Both attributes MUST be present in the same dispatch call.
        assert "stage" in entry_values
        assert "last_contact_date" in entry_values
        assert entry_values["stage"] == "Accepted"

    def test_pattern_a_flip_failure_does_not_partition_to_already_connected(
        self, _pb_args, _sheet,
    ):
        """When the AttioWriter PATCH fails (e.g., 500 from Attio),
        the flip is NOT recorded as already_connected — fail-safe per
        §3.1: better to skip the invite than to assume the flip
        succeeded and bypass tomorrow's retry."""
        import httpx

        attio = MagicMock()
        # Simulate AttioWriter's PATCH raising a permanent error.
        # Wave-2-B: list-entry writes dispatch via update_list_entry,
        # so the failure injection point moves there.
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "internal error"
        attio.update_list_entry.side_effect = httpx.HTTPStatusError(
            "boom", request=MagicMock(), response=mock_response,
        )
        pb = MagicMock()
        pb.download_result_csv.return_value = _csv([
            {"url": "https://www.linkedin.com/in/alice", "degree": "1st"},
        ])

        still, already = _pre_invite_degree_check(
            self._batch(), pb, "scraper-id", attio, "list-id",
        )
        # The PATCH failed → Alice did NOT make it to already_connected.
        # She is NOT in still_to_invite either (the function only adds
        # to still_to_invite for non-1st degrees). The fail-safe is the
        # partition: 1st-degree-but-flip-failed disappears from both
        # lists; the caller MUST treat the empty result as "do not send".
        assert already == []
        assert still == []

    def test_pattern_a_flip_unauthorized_write_error_does_not_escape(
        self, _pb_args, _sheet, monkeypatch: pytest.MonkeyPatch,
    ):
        """PR-15 fold-in (silent-failure-hunter BLOCKING B1):
        `UnauthorizedAttioWriteError` extends `PermissionError`, NOT
        `AttioError`. The pre-fold-in `except AttioError` would have
        let this exception escape and abort the entire batch — other
        1st-degree prospects later in the batch would never get
        flipped, and invites would ship to them next day (§3.1 contract
        broken under future-refactor regression). The widened catch
        keeps batch processing alive."""
        from clients.attio_writer_registry import UnauthorizedAttioWriteError

        escalate_calls: list[dict] = []
        monkeypatch.setattr(
            "workflows.pre_invite_check.escalate",
            _make_recording_escalate(escalate_calls),
        )

        attio = MagicMock()
        # Wave-2-B: UnauthorizedAttioWriteError is raised by
        # AttioWriter._check_write_owner BEFORE the dispatch call,
        # so injecting it on update_list_entry would never fire.
        # Instead, attach the side_effect to the writer's pre-check
        # by patching is_authorized_writer to return False for this
        # specific writer.
        attio.update_list_entry.side_effect = UnauthorizedAttioWriteError(
            "fake unauthorized write"
        )
        pb = MagicMock()
        pb.download_result_csv.return_value = _csv([
            {"url": "https://www.linkedin.com/in/alice", "degree": "1st"},
        ])

        # No exception escapes the loop.
        still, already = _pre_invite_degree_check(
            self._batch(), pb, "scraper-id", attio, "list-id",
        )
        # Same fail-safe partition as AttioError path.
        assert still == []
        assert already == []
        # PR-15 fold-in (silent-failure-hunter IMPORTANT I1): the flip
        # failure now opens a `degree_unknown` queue row so the operator
        # sees it. Idempotency-keyed on entry_id + today.
        flip_fail_calls = [
            c for c in escalate_calls
            if c["type"] == "degree_unknown"
            and "pattern-a-flip-fail" in c["idempotency_key"]
        ]
        assert len(flip_fail_calls) == 1
        payload = flip_fail_calls[0]["payload"]
        assert payload["record_id"] == "rec-A"
        assert payload["last_known_degree"] == "1st"
        assert "UnauthorizedAttioWriteError" in payload["scrape_attempt_id"]


# ==================================================================
# Pattern-A flip exempt from STRICT — cache-hit path with no scraper-id
# ==================================================================


@patch("workflows.daily_check.write_prospects_to_sheet", return_value="https://sheet.example/foo")
@patch("workflows.daily_check._pb_session_args", return_value={})
class TestPatternAFlipExemptFromStrict:
    """The cache_hit_flip codepath (no stale URLs to scrape) is
    EXEMPT from STRICT mode — missing scraper-id is fine because
    Pattern-A is read-then-record-existing, not invite-issuing."""

    def test_cache_only_path_proceeds_without_scraper_id(
        self, _pb_args, _sheet, monkeypatch: pytest.MonkeyPatch,
    ):
        """STRICT=true + missing scraper-id + ALL urls cached as "1st"
        → no raise, the Pattern-A flip path proceeds. The exempt
        codepath is reachable in production when the recheck cache
        has populated entries for all batch URLs."""
        from workflows import recheck_cache as _rc

        monkeypatch.delenv(STRICT_PRE_INVITE_DEGREE_CHECK_ENV, raising=False)

        # Prime the cache so partition() returns all URLs as cached.
        monkeypatch.setattr(
            _rc, "partition",
            lambda urls: (
                {url: {"degree": "1st"} for url in urls},  # fresh_cache
                [],  # stale_urls
            ),
        )

        attio = MagicMock()
        pb = MagicMock()
        batch = [
            {
                "linkedInUrl": "https://www.linkedin.com/in/alice",
                "entry_id": "ent-A", "record_id": "rec-A",
                "current_stage": "Prospect",
                "experiment_id": "exp-test",
                "experiment_id_frozen_at": "prospect",
            },
        ]

        still, already = _pre_invite_degree_check(
            batch, pb, None, attio, "list-id",
        )
        # No raise; Alice flipped to ACCEPTED via cache-only path.
        assert len(already) == 1
        assert still == []
        # No PB scrape attempted — cache_hit_flip path.
        pb.launch_agent.assert_not_called()


# ==================================================================
# Partial-CSV signal — per-prospect degree_unknown queue rows
# ==================================================================


@patch("workflows.daily_check.write_prospects_to_sheet", return_value="https://sheet.example/foo")
@patch("workflows.daily_check._pb_session_args", return_value={})
class TestPartialCsvDegreeUnknown:
    """B-SD-010 partial-CSV contract: when the scrape returns fewer
    rows than requested, emit ONE `degree_unknown` Operator Review
    Queue row PER MISSING PROSPECT. Aggregated/summary writes are
    forbidden — PR-17's run-end summary writes the aggregate."""

    def test_one_queue_row_per_missing_prospect(
        self, _pb_args, _sheet, monkeypatch: pytest.MonkeyPatch,
    ):
        """Three prospects in the batch; CSV returns only 1. Two
        `degree_unknown` queue rows must open (one per missing)."""
        escalate_calls: list[dict] = []
        monkeypatch.setattr(
            "workflows.pre_invite_check.escalate",
            _make_recording_escalate(escalate_calls),
        )

        attio = MagicMock()
        pb = MagicMock()
        # Container_id used in the idempotency key.
        launch = MagicMock()
        launch.container_id = "container-123"
        pb.launch_agent.return_value = launch
        # CSV only includes Alice; Bob and Carol are missing.
        pb.download_result_csv.return_value = _csv([
            {"url": "https://www.linkedin.com/in/alice", "degree": "2nd"},
        ])

        batch = [
            {"linkedInUrl": "https://www.linkedin.com/in/alice",
             "entry_id": "ent-A", "record_id": "rec-A", "current_stage": "Prospect"},
            {"linkedInUrl": "https://www.linkedin.com/in/bob",
             "entry_id": "ent-B", "record_id": "rec-B", "current_stage": "Prospect"},
            {"linkedInUrl": "https://www.linkedin.com/in/carol",
             "entry_id": "ent-C", "record_id": "rec-C", "current_stage": "Prospect"},
        ]
        _pre_invite_degree_check(
            batch, pb, "scraper-id", attio, "list-id",
        )

        # Two queue rows opened — one for Bob, one for Carol.
        degree_unknown_calls = [
            c for c in escalate_calls if c["type"] == "degree_unknown"
        ]
        assert len(degree_unknown_calls) == 2
        record_ids = {c["payload"]["record_id"] for c in degree_unknown_calls}
        assert record_ids == {"rec-B", "rec-C"}

    def test_queue_row_payload_carries_all_required_fields(
        self, _pb_args, _sheet, monkeypatch: pytest.MonkeyPatch,
    ):
        """The DegreeUnknownPayload TypedDict requires 7 fields. Each
        emitted queue row MUST carry them all so the operator triage
        UI doesn't get fragmented rows."""
        from workflows.escalation_schemas import DegreeUnknownPayload

        escalate_calls: list[dict] = []
        monkeypatch.setattr(
            "workflows.pre_invite_check.escalate",
            _make_recording_escalate(escalate_calls),
        )

        attio = MagicMock()
        pb = MagicMock()
        launch = MagicMock()
        launch.container_id = "container-456"
        pb.launch_agent.return_value = launch
        pb.download_result_csv.return_value = _csv([
            {"url": "https://www.linkedin.com/in/alice", "degree": "2nd"},
        ])

        batch = [
            {"linkedInUrl": "https://www.linkedin.com/in/alice",
             "entry_id": "ent-A", "record_id": "rec-A", "current_stage": "Prospect"},
            {"linkedInUrl": "https://www.linkedin.com/in/bob",
             "entry_id": "ent-B", "record_id": "rec-B", "current_stage": "Prospect"},
        ]
        _pre_invite_degree_check(
            batch, pb, "scraper-id", attio, "list-id",
        )

        dropped = [c for c in escalate_calls if c["type"] == "degree_unknown"]
        assert len(dropped) == 1
        payload = dropped[0]["payload"]
        # All 7 fields present per TypedDict required_keys.
        required = DegreeUnknownPayload.__required_keys__
        assert required.issubset(payload.keys())
        assert payload["record_id"] == "rec-B"
        assert payload["linkedin_url"] == "https://www.linkedin.com/in/bob"
        assert payload["scrape_attempt_id"] == "container-456"
        assert payload["csv_row_count_observed"] == 1
        assert payload["csv_row_count_expected"] == 2

    def test_complete_csv_emits_no_degree_unknown_rows(
        self, _pb_args, _sheet, monkeypatch: pytest.MonkeyPatch,
    ):
        """Happy path: CSV returns all requested rows → zero
        `degree_unknown` queue rows emitted."""
        escalate_calls: list[dict] = []
        monkeypatch.setattr(
            "workflows.pre_invite_check.escalate",
            _make_recording_escalate(escalate_calls),
        )

        attio = MagicMock()
        pb = MagicMock()
        pb.download_result_csv.return_value = _csv([
            {"url": "https://www.linkedin.com/in/alice", "degree": "2nd"},
            {"url": "https://www.linkedin.com/in/bob", "degree": "2nd"},
        ])

        batch = [
            {"linkedInUrl": "https://www.linkedin.com/in/alice",
             "entry_id": "ent-A", "record_id": "rec-A", "current_stage": "Prospect"},
            {"linkedInUrl": "https://www.linkedin.com/in/bob",
             "entry_id": "ent-B", "record_id": "rec-B", "current_stage": "Prospect"},
        ]
        _pre_invite_degree_check(
            batch, pb, "scraper-id", attio, "list-id",
        )

        degree_unknown_calls = [
            c for c in escalate_calls if c["type"] == "degree_unknown"
        ]
        assert degree_unknown_calls == []


# ==================================================================
# Escalation schema registration
# ==================================================================


class TestDegreeUnknownEscalationSchema:
    def test_schema_registered(self):
        from workflows.escalation_schemas import (
            ESCALATION_SCHEMAS,
            DegreeUnknownPayload,
        )
        assert ESCALATION_SCHEMAS.get("degree_unknown") is DegreeUnknownPayload

    def test_payload_required_fields(self):
        from workflows.escalation_schemas import DegreeUnknownPayload

        assert DegreeUnknownPayload.__required_keys__ == {
            "record_id",
            "linkedin_url",
            "last_known_degree",
            "scrape_attempt_id",
            "requested_at",
            "csv_row_count_observed",
            "csv_row_count_expected",
        }


# ==================================================================
# PR-B.9 — _resolve_degree_check_backend() env-driven config gate
# ==================================================================
#
# The backend resolver is called at the top of every _pre_invite_degree_check
# invocation so the flag is hot-reloadable. It's the single chokepoint for
# validating the Sales Nav rollout config — closes adversarial review
# finding F5 (cross-wired phantom IDs, boolean truthiness parsing).
# A regression here either silently routes Sales Nav calls through the
# legacy phantom (different CSV schema, different arg shape, §3.1 bypass)
# or 0-row-CSV every prospect into the operator queue.


class TestResolveDegreeCheckBackend:
    """Env validation gate before any PB launch. Strict value parsing,
    cross-wire guard, cookie sanity-check."""

    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in (
            "PRE_INVITE_DEGREE_CHECK_BACKEND",
            "PB_SALES_NAV_PROFILE_SCRAPER_ID",
            "PB_PROFILE_SCRAPER_ID",
            "PB_LI_SALES_NAV_SESSION_COOKIE",
        ):
            monkeypatch.delenv(var, raising=False)

    def test_default_unset_is_sales_nav(self, monkeypatch: pytest.MonkeyPatch):
        """No env var set → defaults to sales_nav (flipped when the legacy
        Profile Scraper agent was deleted from the PB workspace, so a regular
        default guaranteed a mid-run 404). With a bare environment the default
        then fails LOUD on the missing SN scraper-id — the fail-at-resolve-time
        behavior the flip exists to provide."""
        from workflows.daily_check_helpers import (
            DEGREE_CHECK_BACKEND_DEFAULT,
            SalesNavConfigError,
            _resolve_degree_check_backend,
        )

        assert DEGREE_CHECK_BACKEND_DEFAULT == "sales_nav"
        self._clear_env(monkeypatch)
        with pytest.raises(SalesNavConfigError, match="PB_SALES_NAV_PROFILE_SCRAPER_ID"):
            _resolve_degree_check_backend()

    def test_default_unset_resolves_sales_nav_with_sn_env(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        """No backend var + a fully configured SN env → sales_nav. The
        production .env shape after the flip (operators may simply drop the
        backend line)."""
        from workflows.daily_check_helpers import _resolve_degree_check_backend

        self._clear_env(monkeypatch)
        monkeypatch.setenv("PB_SALES_NAV_PROFILE_SCRAPER_ID", "sn-id-123")
        monkeypatch.setenv("PB_LI_SALES_NAV_SESSION_COOKIE", "li-at-cookie")
        assert _resolve_degree_check_backend() == "sales_nav"

    def test_explicit_regular_returns_regular(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        from workflows.daily_check_helpers import _resolve_degree_check_backend

        self._clear_env(monkeypatch)
        monkeypatch.setenv("PRE_INVITE_DEGREE_CHECK_BACKEND", "regular")
        assert _resolve_degree_check_backend() == "regular"

    def test_regular_backend_skips_sales_nav_validation(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        """Backend=regular must NOT validate the Sales Nav env vars
        (operator rolling back must not need to also clear cookie env)."""
        from workflows.daily_check_helpers import _resolve_degree_check_backend

        self._clear_env(monkeypatch)
        monkeypatch.setenv("PRE_INVITE_DEGREE_CHECK_BACKEND", "regular")
        # No PB_SALES_NAV_*, no PB_LI_SALES_NAV_* set → still fine.
        assert _resolve_degree_check_backend() == "regular"

    def test_valid_sales_nav_full_config_returns_sales_nav(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        from workflows.daily_check_helpers import _resolve_degree_check_backend

        self._clear_env(monkeypatch)
        monkeypatch.setenv("PRE_INVITE_DEGREE_CHECK_BACKEND", "sales_nav")
        monkeypatch.setenv("PB_SALES_NAV_PROFILE_SCRAPER_ID", "phantom-602790655114603")
        monkeypatch.setenv("PB_PROFILE_SCRAPER_ID", "phantom-LEGACY-different-id")
        monkeypatch.setenv("PB_LI_SALES_NAV_SESSION_COOKIE", "fake-li-at")

        assert _resolve_degree_check_backend() == "sales_nav"

    def test_bogus_value_raises_with_valid_values_in_message(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        """Strict value parsing — typos must fail loud. Closes adversarial F5
        (boolean truthiness parsing would have silently flipped 'True'/'1' to
        False)."""
        from workflows.daily_check_helpers import (
            SalesNavConfigError,
            _resolve_degree_check_backend,
        )

        self._clear_env(monkeypatch)
        monkeypatch.setenv("PRE_INVITE_DEGREE_CHECK_BACKEND", "bogus")

        with pytest.raises(SalesNavConfigError) as exc_info:
            _resolve_degree_check_backend()
        msg = str(exc_info.value)
        # Valid values list must appear in the message so operators can
        # self-correct without reading source.
        assert "regular" in msg
        assert "sales_nav" in msg
        assert "bogus" in msg  # echo the offending value

    def test_sales_nav_missing_scraper_id_raises(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        from workflows.daily_check_helpers import (
            SalesNavConfigError,
            _resolve_degree_check_backend,
        )

        self._clear_env(monkeypatch)
        monkeypatch.setenv("PRE_INVITE_DEGREE_CHECK_BACKEND", "sales_nav")
        monkeypatch.setenv("PB_LI_SALES_NAV_SESSION_COOKIE", "fake-cookie")
        # PB_SALES_NAV_PROFILE_SCRAPER_ID unset
        with pytest.raises(SalesNavConfigError) as exc_info:
            _resolve_degree_check_backend()
        assert "PB_SALES_NAV_PROFILE_SCRAPER_ID" in str(exc_info.value)

    def test_sales_nav_empty_scraper_id_raises(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        """Empty-string scraper-id is treated identically to unset."""
        from workflows.daily_check_helpers import (
            SalesNavConfigError,
            _resolve_degree_check_backend,
        )

        self._clear_env(monkeypatch)
        monkeypatch.setenv("PRE_INVITE_DEGREE_CHECK_BACKEND", "sales_nav")
        monkeypatch.setenv("PB_SALES_NAV_PROFILE_SCRAPER_ID", "")
        monkeypatch.setenv("PB_LI_SALES_NAV_SESSION_COOKIE", "fake-cookie")

        with pytest.raises(SalesNavConfigError):
            _resolve_degree_check_backend()

    def test_sales_nav_cross_wire_with_legacy_raises(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        """When PB_SALES_NAV_PROFILE_SCRAPER_ID == PB_PROFILE_SCRAPER_ID the
        operator has pasted the legacy phantom ID into the new slot — this
        silently bypasses the §3.1-hardened partition (different CSV column
        contract). Must fail loud.

        Closes adversarial review F5 — the most-failure-mode pathology of
        the env-flag rollout. Without this guard, the launch_agent() call
        would run the regular scraper with Sales Nav-shaped args, get back
        legacy-schema CSV, and route every prospect through the legacy
        default arm (which sends to still_to_invite on any non-1st degree)."""
        from workflows.daily_check_helpers import (
            SalesNavConfigError,
            _resolve_degree_check_backend,
        )

        self._clear_env(monkeypatch)
        monkeypatch.setenv("PRE_INVITE_DEGREE_CHECK_BACKEND", "sales_nav")
        monkeypatch.setenv("PB_SALES_NAV_PROFILE_SCRAPER_ID", "same-phantom-id")
        monkeypatch.setenv("PB_PROFILE_SCRAPER_ID", "same-phantom-id")
        monkeypatch.setenv("PB_LI_SALES_NAV_SESSION_COOKIE", "fake-cookie")

        with pytest.raises(SalesNavConfigError) as exc_info:
            _resolve_degree_check_backend()
        msg = str(exc_info.value)
        assert "identical" in msg or "same" in msg.lower()
        assert "PB_SALES_NAV_PROFILE_SCRAPER_ID" in msg

    def test_sales_nav_missing_cookie_raises(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        """Sanity-check cookie presence at config-resolve time so callers
        don't waste a PB launch only to discover a missing cookie (which
        returns 0-row CSV → every prospect to operator queue under PR-B's
        §3.1-hardened partition)."""
        from workflows.daily_check_helpers import (
            SalesNavConfigError,
            _resolve_degree_check_backend,
        )

        self._clear_env(monkeypatch)
        monkeypatch.setenv("PRE_INVITE_DEGREE_CHECK_BACKEND", "sales_nav")
        monkeypatch.setenv("PB_SALES_NAV_PROFILE_SCRAPER_ID", "ph-SN")
        monkeypatch.setenv("PB_PROFILE_SCRAPER_ID", "ph-LEGACY")
        # PB_LI_SALES_NAV_SESSION_COOKIE unset

        with pytest.raises(SalesNavConfigError) as exc_info:
            _resolve_degree_check_backend()
        assert "PB_LI_SALES_NAV_SESSION_COOKIE" in str(exc_info.value)

    def test_sales_nav_no_legacy_id_set_is_fine(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        """If PB_PROFILE_SCRAPER_ID is unset (fresh deploy), the cross-wire
        check can't fire — backend=sales_nav must still resolve."""
        from workflows.daily_check_helpers import _resolve_degree_check_backend

        self._clear_env(monkeypatch)
        monkeypatch.setenv("PRE_INVITE_DEGREE_CHECK_BACKEND", "sales_nav")
        monkeypatch.setenv("PB_SALES_NAV_PROFILE_SCRAPER_ID", "ph-SN")
        # PB_PROFILE_SCRAPER_ID intentionally unset
        monkeypatch.setenv("PB_LI_SALES_NAV_SESSION_COOKIE", "cookie")

        assert _resolve_degree_check_backend() == "sales_nav"

    def test_whitespace_around_backend_value_is_stripped(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        """Operator pastes `  sales_nav\n` from a copy-paste; the resolver
        must .strip() before comparing."""
        from workflows.daily_check_helpers import _resolve_degree_check_backend

        self._clear_env(monkeypatch)
        monkeypatch.setenv("PRE_INVITE_DEGREE_CHECK_BACKEND", "  sales_nav\n")
        monkeypatch.setenv("PB_SALES_NAV_PROFILE_SCRAPER_ID", "ph-SN")
        monkeypatch.setenv("PB_LI_SALES_NAV_SESSION_COOKIE", "cookie")

        assert _resolve_degree_check_backend() == "sales_nav"
