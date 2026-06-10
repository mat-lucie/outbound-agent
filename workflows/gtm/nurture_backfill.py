"""PR-39 — Backfill logic for ``nurture_re_eligible_at`` on existing
DM3-completed list entries.

Per ``clients/attio_writer_registry.py:221-222`` and
``docs/attio_schema_deltas.yaml`` the §3.15 write-owner registry
authorizes this module (``workflows.gtm.nurture_backfill``) as one of
the two writers for the ``nurture_re_eligible_at`` slug
(the other is ``workflows.daily_check.run_dm_sequencing``).

The companion CLI lives at ``scripts/backfill_nurture_re_eligible_at.py``
— it loads ``.env``, opens the AttioClient + AttioWriter, and invokes
``backfill_one`` per candidate. Splitting this way keeps the registry-
authorized writer in the ``workflows`` tree while the operator-facing
CLI sits where ``scripts/`` conventionally lives.

Belt-and-suspenders per Round-3 M9: ``backfill_one`` delegates the
suppression decision to ``workflows.cadence.compute_nurture_re_eligible_at``
— a None return ALWAYS skips the write so no §3.1 "no" prospect can be
re-armed by this path.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from clients.attio import AttioClient
from workflows.cadence import (
    _parse_date,
    classify_suppression,
    compute_nurture_re_eligible_at,
    derive_cadence_lane,
)

if TYPE_CHECKING:
    from clients.attio_writer import AttioWriter

WRITER_MODULE = "workflows.gtm.nurture_backfill"
LINKEDIN_OUTREACH_OBJECT = "linkedin_outreach"

# Stages that have already passed DM3 — these are the backfill candidates.
POST_DM3_STAGES: frozenset[str] = frozenset({
    "DM3 Sent",
    "Nurture",
    "Responded",
    "Defensive Hold",
    "Call Booked",
    "Qualified",
})


def select_candidates(entries: list[dict]) -> list[dict]:
    """Return entries that are post-DM3 and missing nurture_re_eligible_at."""
    out: list[dict] = []
    for entry in entries:
        attrs = AttioClient.parse_entry(entry)
        if attrs.get("stage") not in POST_DM3_STAGES:
            continue
        if attrs.get("nurture_re_eligible_at"):
            continue
        out.append(entry)
    return out


def backfill_one(
    entry: dict,
    *,
    writer: AttioWriter | None,
    list_id: str,
    dry_run: bool,
) -> str:
    """Compute and (when not dry-run) PATCH nurture_re_eligible_at on one entry.

    Returns a per-signal outcome tag so the backfill summary doubles as a
    per-signal health check (an OR-merge regression in one signal is
    detectable when the others still fire on the same volumes):

        ``patched``                       — wrote the new date
        ``would_patch``                   — dry-run would have written
        ``skipped_flag``                  — suppress_re_engagement=True
        ``skipped_classification``        — response_classification negative/defensive
        ``skipped_stage_not_interested``  — stage == NOT_INTERESTED
        ``skipped_stage_defensive_hold``  — stage == DEFENSIVE_HOLD
        ``skipped_no_dm3_anchor``         — dm3_sent_at missing
        ``skipped_corrupt_dm3_anchor``    — dm3_sent_at set but unparseable
                                            (loud signal: post-DM3 entry
                                            with garbage timestamp is a
                                            data-quality alarm)
    """
    import sys

    from clients.attio_writer import WriteIntent  # local: avoid import-time cycle

    attrs = AttioClient.parse_entry(entry)

    # Per-signal attribution comes BEFORE the cadence computation so the
    # summary counters can split a "skipped_suppressed" bucket into its
    # four constituent signals (silent-failure observability).
    suppression = classify_suppression(attrs)
    if suppression is not None:
        return f"skipped_{suppression}"

    # No suppression — check anchor validity.
    raw_anchor = attrs.get("dm3_sent_at")
    if not raw_anchor:
        return "skipped_no_dm3_anchor"
    if isinstance(raw_anchor, str) and _parse_date(raw_anchor) is None:
        # Loud signal: post-DM3 entry with a corrupted timestamp.
        sys.stderr.write(
            f"WARN: corrupt dm3_sent_at on entry "
            f"{attrs.get('entry_id', '?')!r}: {raw_anchor!r}\n"
        )
        return "skipped_corrupt_dm3_anchor"

    cadence_lane = derive_cadence_lane(attrs.get("scoring_lane"))
    target = compute_nurture_re_eligible_at(
        dm3_sent_at=raw_anchor,
        cadence_lane=cadence_lane,
        entry_attrs=attrs,
    )
    if target is None:
        # Shouldn't happen — we already ruled out both suppression and
        # missing/corrupt anchor. If it does, treat as corrupt anchor so
        # the loud signal still fires.
        return "skipped_corrupt_dm3_anchor"

    if dry_run or writer is None:
        return "would_patch"

    entry_id = attrs.get("entry_id", "")
    writer.apply(WriteIntent(
        object=LINKEDIN_OUTREACH_OBJECT,
        record_id=entry_id,
        updates={"nurture_re_eligible_at": target.isoformat()},
        prior_values={"nurture_re_eligible_at": attrs.get("nurture_re_eligible_at")},
        writer_module=WRITER_MODULE,
        is_list_entry=True,
        list_id=list_id,
    ))
    return "patched"
