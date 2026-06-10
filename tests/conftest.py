"""Shared test fixtures."""

import os
import sys
from pathlib import Path

import pytest

# Ensure project root is on path
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# Point the engine at the bundled synthetic reference operator content
# (examples/acme/content/) for the whole suite. The repo-root `content/` ships
# NEUTRAL placeholder defaults with a REPLACE_THIS_TEMPLATE sentinel that the
# live-send gate refuses to send; tests assert against the Acme reference content
# (schema-complete, non-sentinel) and never trip the sentinel. Set at module
# import time — BEFORE any production module is imported during collection —
# because the four `CONTENT_DIR` constants resolve `OUTBOUND_CONTENT_DIR` once at
# import. A test that wants the shipped placeholders can override this env var and
# reimport, or monkeypatch the module constant directly.
#
# Set UNCONDITIONALLY (not setdefault): if a developer has OUTBOUND_CONTENT_DIR
# exported in their shell, setdefault would let that ambient value silently
# hijack the whole suite (tests would assert against an arbitrary directory and
# could mask a regression). Pinning it guarantees the suite always runs against
# the bundled reference content; per-test monkeypatch.setenv still overrides for
# the duration of a test and is restored afterward.
os.environ["OUTBOUND_CONTENT_DIR"] = str(
    _REPO_ROOT / "examples" / "acme" / "content"
)

# Point the engine at the bundled synthetic reference operator CONFIG
# (examples/acme/config/icp.yaml) for the whole suite — the exact parallel to the
# OUTBOUND_CONTENT_DIR pin above. The repo-root config/ ships the same NEUTRAL
# config/icp.example.yaml default; the Acme config mirrors it. The golden +
# scoring tests assert Acme's exact scores/bands and the byte-identical rendered
# qualifier prompt, so the suite must resolve the Acme ICP. Set at module import
# time — BEFORE workflows.quality_gate is imported during collection — because
# that module bakes `_ICP = load_icp_config()` and renders QUALIFIER_SYSTEM_PROMPT
# once at import from config_dir().
#
# NOTE (baseline semantics): the goldens are regenerated against this synthetic
# operator. They are self-consistent regression guards on the scoring/render
# LOGIC — not the original post==pre-refactor equivalence proof, which was retired
# with the original operator's reference data. See examples/acme/README.md.
#
# config_dir()/OUTBOUND_CONFIG_DIR is ALL-OR-NOTHING across crm/icp/phantombuster,
# but only the ICP loader and the qualifier-template render consult it eagerly:
#   * icp.yaml             -> examples/acme/config/icp.yaml (synthetic ICP)
#   * prompts/qualifier.md.j2 -> examples/acme/config/prompts/ (byte-identical copy
#     of the operator-neutral shipped template; byte-identical render)
#   * crm.yaml / phantombuster.yaml are ABSENT here, and their loaders read only
#     the live *.yaml (never *.example.yaml), so they fall back to the exact same
#     attio/identity/env defaults they use at repo-root — no ConfigError.
#
# Set UNCONDITIONALLY (not setdefault) for the same reason as the content pin: a
# developer's ambient OUTBOUND_CONFIG_DIR must not silently hijack the suite.
# Tests that need the repo-root neutral default (e.g. the shipped-config guard)
# or a toy config (the operator-driven prompt tests) delenv/override this var in
# their own scope, which monkeypatch restores afterward.
os.environ["OUTBOUND_CONFIG_DIR"] = str(_REPO_ROOT / "examples" / "acme" / "config")


@pytest.fixture(autouse=True)
def _isolate_anthropic_api_key(monkeypatch):
    """Guarantee no test ever hits the real Anthropic API.

    Tests that exercise the LLM path set ANTHROPIC_API_KEY explicitly via
    monkeypatch inside their own scope — this autouse fixture removes any
    ambient key first so the isolation is reliable regardless of CI env.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _isolate_llm_dispatch_env(monkeypatch):
    """Guarantee no test ever picks up an ambient ``OUTBOUND_USE_LLM_DISPATCH=1``.

    Per silent-failure-hunter HIGH (F-PR-9): a developer-shell-local
    export of this var would silently flip every no-client caller into
    dispatch mode and either hang on the 300s default timeout or hit a
    real Claude Code session. Tests that exercise dispatch set the env
    var explicitly inside their own scope (see
    ``test_response_classifier_env_var_enables_dispatch``).
    """
    monkeypatch.delenv("OUTBOUND_USE_LLM_DISPATCH", raising=False)


@pytest.fixture(autouse=True)
def _isolate_attio_api_key(monkeypatch):
    """Guarantee no test ever hits the real Attio API.

    When ATTIO_API_KEY is present (developer .env, exported shell var),
    LLM-dispatch tests that don't pass an explicit AttioClient mock leak
    through ``LLMBudgetLedger.try_reserve`` into live ``api.attio.com``
    requests against the ``llm_budget_ledger`` object — failing with 404
    (object not provisioned) or httpx.LocalProtocolError (empty bearer)
    instead of exercising the dispatch round-trip they intend to test.
    Deleting the env var here routes those paths through the documented
    KeyError fail-soft branch in ``LLMBudgetLedger.try_reserve``. Tests
    that exercise live-Attio code paths (PR-35 ledger, PR-9b backfill,
    suppress-re-engagement backfill) set ATTIO_API_KEY explicitly via
    their own monkeypatch fixture — those stack on top of this delenv.
    """
    monkeypatch.delenv("ATTIO_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _isolate_degree_check_backend_env(monkeypatch):
    """Guarantee no test inherits an ambient Sales Nav backend selection.

    ``PRE_INVITE_DEGREE_CHECK_BACKEND`` (+ the SN scraper-id / cookie vars
    it gates on) is read at call time by both the pre-invite degree check
    and Phase 0 ``detect_accepted_connections``. Once an operator sets
    ``PRE_INVITE_DEGREE_CHECK_BACKEND=sales_nav`` in their developer
    ``.env`` (or a prior test leaves it set), every degree-check test that
    expects the default ``regular`` path silently routes through the Sales
    Nav branch and fails — order-dependently, since collection order
    decides whether the polluting test runs first. Deleting these here
    before each test makes the suite order-independent for this flag.
    Tests that exercise the sales_nav branch set the vars explicitly via
    their own monkeypatch (which stacks on top of this delenv).
    """
    for var in (
        "PRE_INVITE_DEGREE_CHECK_BACKEND",
        "PB_SALES_NAV_PROFILE_SCRAPER_ID",
        "PB_LI_SALES_NAV_SESSION_COOKIE",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _isolate_recheck_cache(monkeypatch, tmp_path):
    """Redirect the recheck cache file to a per-test temp path.

    Without this, tests that exercise daily_check would read/write the real
    ~/.outbound-agent/recheck_cache.json and contaminate one another (and the
    user's live cache).
    """
    from workflows import recheck_cache

    monkeypatch.setattr(recheck_cache, "CACHE_FILE", tmp_path / "recheck_cache.json")


@pytest.fixture(autouse=True)
def _email_compliance_baseline_env(monkeypatch):
    """Give the suite a compliant email baseline so the CAN-SPAM send-gate
    (workflows.email_compliance.assert_email_compliance_ready) doesn't block the
    many existing live-send tests, which legitimately exercise the send path.

    Sets a test physical address (the one hard gate). Tests that verify the
    gate's fail-loud behavior delenv this in their own scope (monkeypatch
    restores it afterward). Other compliance config (sender org, unsubscribe
    mailto) is intentionally left unset so tests opt into it explicitly.
    """
    monkeypatch.setenv("EMAIL_PHYSICAL_ADDRESS", "123 Test St, Test City, TC 00000")


@pytest.fixture(autouse=True)
def _isolate_email_sent_ledger(monkeypatch, tmp_path):
    """Redirect the email idempotency sent-ledger to a per-test temp path.

    Without this, any test that drives run_email_daily into a live send would
    append to the operator's real ~/.outbound-agent/email_sent.json and leak state
    across tests (a contact "already sent" in one test would be skipped in
    another). Per-test isolation keeps the ledger deterministic.
    """
    from workflows import email_compliance

    monkeypatch.setattr(
        email_compliance, "LEDGER_FILE", tmp_path / "email_sent.json"
    )


@pytest.fixture(autouse=True)
def _isolate_attio_dlq(monkeypatch, tmp_path):
    """Redirect the Attio dead-letter-queue dir to a per-test temp path.

    Without this, any test that drives AttioWriter into ``_dlq_and_escalate`` —
    directly, or transitively via daily_check / detect_responses — appends a
    forensic row to the operator's real ``~/.outbound-agent/dlq/attio-<date>.jsonl``.
    That both pollutes the prod DLQ and risks a genuine prod failure being lost
    among test fixtures. test_attio_writer.py's ``dlq_tmp`` only covered tests
    that opt in; this autouse fixture covers the whole suite. Returns the dir so
    opt-in fixtures (``dlq_tmp``) can delegate to it by requesting the fixture by
    name — leading underscore kept for convention parity with the siblings above.
    """
    from clients import attio_writer

    dlq_dir = tmp_path / "dlq"
    monkeypatch.setattr(attio_writer, "DLQ_DIR", dlq_dir)
    return dlq_dir


@pytest.fixture(autouse=True)
def _isolate_weekly_kpi_reports(monkeypatch, tmp_path):
    """Redirect the weekly KPI sidecar dir to a per-test temp path.

    Without this, any test that reaches ``run_weekly_report`` without the
    opt-in ``kpi_dir`` fixture (e.g. the merged_into filter test, which
    drives the dry-run integration path) writes a fixture-data snapshot to
    the real ``reports/weekly-kpi/`` — and because the supersede-on-rerun
    audit logic renames the prior file aside, every suite run grows a new
    ``<week>_superseded_<n>.json``. Returns the dir so opt-in fixtures can
    delegate; ``kpi_dir`` in test_pr30_weekly_report stacks on top via its
    own setattr.
    """
    from workflows import weekly_report

    kpi_dir = tmp_path / "weekly-kpi"
    monkeypatch.setattr(weekly_report, "REPORTS_DIR", kpi_dir)
    return kpi_dir
