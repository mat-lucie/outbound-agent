"""Phase C — reconciliation sweep (workflows/pending_invite_reconciliation.py).

The pipeline-leakage red line: a PROSPECT row is flipped to CONNECTION_SENT
ONLY when a fresh Sales Nav scrape returns hasPendingInvitation="true". Any
other value leaves it at PROSPECT — never ghost-advance an un-invited prospect.

Fork note: ``--dry-run`` is side-effect free and does NOT scrape (mirrors the
fork's ``_pre_invite_degree_check`` dry-run), so it previews candidates only and
cannot project per-row flips. The wet path scrapes and flips.
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from workflows.daily_check_helpers import _normalize_linkedin_url

MOD = "workflows.pending_invite_reconciliation"


def _entry(suffix: str, *, stage: str = "Prospect",
           experiment_id: str | None = "exp-1",
           frozen_at: str | None = "prospect",
           record_id: str | None = None) -> dict:
    return {
        "entry_id": f"ent-{suffix}",
        "record_id": record_id or f"rec-{suffix}",
        "stage": stage,
        "experiment_id": experiment_id,
        "experiment_id_frozen_at": frozen_at,
    }


def _url(record_id: str) -> str:
    return f"https://www.linkedin.com/in/{record_id}"


@contextmanager
def _harness(entries: list[dict], extras_by_url: dict[str, str],
             *, advance_spy=None):
    """Patch the sweep's collaborators. `extras_by_url` maps a record_id's URL
    to its hasPendingInvitation value; `advance_spy` records flip calls."""
    spy = advance_spy or MagicMock(return_value=True)

    def _cache_get(record_id):
        return ("Name", "Co", _url(record_id), "", "")

    cache = MagicMock()
    cache.get.side_effect = _cache_get

    def _scrape(pb, scraper_id, urls, **kwargs):
        extras = {
            _normalize_linkedin_url(u): {"hasPendingInvitation": extras_by_url.get(u, "false")}
            for u in urls
        }
        degrees = {_normalize_linkedin_url(u): "2nd" for u in urls}
        return "container-1", degrees, extras

    with (
        patch(f"{MOD}._get_all_entries_parsed", return_value=entries),
        patch(f"{MOD}.RecordCache", return_value=cache),
        patch(f"{MOD}._launch_sales_nav_scrape", side_effect=_scrape),
        patch(f"{MOD}.recheck_cache.partition", side_effect=lambda urls: ({}, list(urls))),
        patch(f"{MOD}.recheck_cache.record_many"),
        patch("workflows.daily_check._attio_advance_with_escalation", spy),
    ):
        yield spy


def _run(**kw):
    from workflows.pending_invite_reconciliation import run_pending_invite_reconciliation
    base = dict(
        attio=MagicMock(), pb=MagicMock(),
        sales_nav_profile_scraper_id="sn-1", list_id="list-1",
    )
    base.update(kw)
    return run_pending_invite_reconciliation(**base)


def test_flips_only_pending_rows():
    entries = [_entry("A"), _entry("B")]
    extras = {_url("rec-A"): "true", _url("rec-B"): "false"}
    with _harness(entries, extras) as spy:
        summary = _run(dry_run=False)
    assert summary["flipped"] == 1
    assert summary["left_at_prospect"] == 1
    # Only the pending row (ent-A) was advanced.
    flipped_entry_ids = {c.kwargs["entry_id"] for c in spy.call_args_list}
    assert flipped_entry_ids == {"ent-A"}


def test_never_flips_non_pending_ghost_advance_guard():
    # blank / 2nd-degree / missing-from-scrape must NEVER flip.
    entries = [_entry("A"), _entry("B")]
    extras = {_url("rec-A"): "false"}  # rec-B absent from extras entirely
    with _harness(entries, extras) as spy:
        summary = _run(dry_run=False)
    assert summary["flipped"] == 0
    assert summary["left_at_prospect"] == 2
    spy.assert_not_called()


def test_flips_all_entry_ids_for_duplicate_prospect():
    # Two entries resolve to the SAME LinkedIn URL (duplicate records) → dedup
    # collapses them; the flip must hit BOTH entry_ids.
    entries = [
        _entry("dup1", record_id="rec-dup"),
        _entry("dup2", record_id="rec-dup"),
    ]
    extras = {_url("rec-dup"): "true"}
    with _harness(entries, extras) as spy:
        summary = _run(dry_run=False)
    assert summary["flipped"] == 1  # one prospect
    flipped_entry_ids = {c.kwargs["entry_id"] for c in spy.call_args_list}
    assert flipped_entry_ids == {"ent-dup1", "ent-dup2"}


def test_dry_run_previews_candidates_without_scraping_or_writing():
    # Fork convention: dry-run does NOT scrape, so it previews candidates and
    # writes nothing. flipped stays 0 (no live hasPendingInvitation signal).
    entries = [_entry("A")]
    extras = {_url("rec-A"): "true"}
    with _harness(entries, extras) as spy, \
            patch(f"{MOD}._launch_sales_nav_scrape") as scrape:
        summary = _run(dry_run=True)
    assert summary["dry_run"] is True
    assert summary["candidates"] == 1
    assert summary["flipped"] == 0
    assert summary["scraped"] == 0
    scrape.assert_not_called()
    spy.assert_not_called()


def test_experiment_id_preserved_and_frozen_at_restamped():
    entries = [_entry("A", experiment_id="exp-1", frozen_at="prospect")]
    extras = {_url("rec-A"): "true"}
    with _harness(entries, extras) as spy:
        _run(dry_run=False)
    attrs = spy.call_args.kwargs["entry_attributes"]
    assert attrs["stage"] == "Connection Sent"
    assert attrs["experiment_id_frozen_at"] == "connection_sent"
    # experiment_id is preserved (never re-written) — a prior-run event.
    assert "experiment_id" not in attrs


def test_terminal_frozen_at_not_restamped():
    # Immutability guard: a terminal frozen_at (accepted/legacy_*) is preserved.
    entries = [_entry("A", experiment_id="exp-1", frozen_at="accepted")]
    extras = {_url("rec-A"): "true"}
    with _harness(entries, extras) as spy:
        _run(dry_run=False)
    attrs = spy.call_args.kwargs["entry_attributes"]
    assert attrs["stage"] == "Connection Sent"
    assert "experiment_id_frozen_at" not in attrs


def test_no_experiment_omits_frozen_at_stamp():
    entries = [_entry("A", experiment_id=None, frozen_at=None)]
    extras = {_url("rec-A"): "true"}
    with _harness(entries, extras) as spy:
        _run(dry_run=False)
    attrs = spy.call_args.kwargs["entry_attributes"]
    assert "experiment_id_frozen_at" not in attrs


def test_non_prospect_rows_ignored():
    entries = [_entry("A", stage="Connection Sent"), _entry("B", stage="Accepted")]
    with _harness(entries, {}) as spy:
        summary = _run(dry_run=False)
    assert summary["candidates"] == 0
    spy.assert_not_called()


def test_writer_module_is_authorized_in_registry():
    # The flip's writer_module must be a registered writer for stage /
    # last_contact_date / experiment_id_frozen_at, or AttioWriter raises.
    from clients.attio_writer_registry import is_authorized_writer
    mod = "workflows.pending_invite_reconciliation.run_pending_invite_reconciliation"
    for slug in ("stage", "last_contact_date", "experiment_id_frozen_at"):
        assert is_authorized_writer("linkedin_outreach", slug, mod), slug


def test_failed_write_counts_as_failed_not_flipped():
    entries = [_entry("A")]
    extras = {_url("rec-A"): "true"}
    spy = MagicMock(return_value=False)  # advance helper reports failure
    with _harness(entries, extras, advance_spy=spy):
        summary = _run(dry_run=False)
    assert summary["flipped"] == 0
    assert summary["failed"] == 1


def test_scrape_config_error_propagates_no_write():
    """A wet scrape that raises a config error (e.g. a missing Sales Nav session
    cookie) must PROPAGATE so the CLI surfaces a clean message — never swallowed
    — and no flip can have happened (it aborts before any write)."""
    import pytest

    from workflows.daily_check_helpers import SalesNavConfigError

    entries = [_entry("A"), _entry("B")]
    cache = MagicMock()
    cache.get.side_effect = lambda rid: ("N", "C", _url(rid), "", "")
    spy = MagicMock(return_value=True)

    def _boom(pb, scraper_id, urls, **kwargs):
        raise SalesNavConfigError("PB_LI_SALES_NAV_SESSION_COOKIE is not set")

    with (
        patch(f"{MOD}._get_all_entries_parsed", return_value=entries),
        patch(f"{MOD}.RecordCache", return_value=cache),
        patch(f"{MOD}._launch_sales_nav_scrape", side_effect=_boom),
        patch(f"{MOD}.recheck_cache.partition", side_effect=lambda urls: ({}, list(urls))),
        patch("workflows.daily_check._attio_advance_with_escalation", spy),
        pytest.raises(SalesNavConfigError),
    ):
        _run(dry_run=False)
    spy.assert_not_called()
