"""Cross-channel re-engagement suppression — sole writer for ``suppress_re_engagement``.

Per plan §3.15 write-owner registry, this module is the sole authorized writer of
``linkedin_outreach.suppress_re_engagement`` (with ``scripts.attio_dedup`` as the
union-merge writer).  Per §3.1 (HARDEST RED LINE), a prospect who ever signalled
"no" through any channel must never be re-contacted through any other channel.

This module exposes two surfaces:

* **Read side** — ``is_suppressed`` + ``build_suppression_set`` consult three signals
  in OR-fashion (belt-and-suspenders §3.16 + §3.18):

    1. The persisted ``suppress_re_engagement`` flag (set by backfill or future
       write paths).
    2. ``response_classification`` ∈ {``negative``, ``defensive``} — the prospect
       already declined or pushed back on a prior channel.
    3. ``stage`` ∈ {``NOT_INTERESTED``, ``DEFENSIVE_HOLD``} — pipeline terminals
       that reflect the same decline via a different writer path.

  Runtime senders (``wave2_blast``, future ``association_outreach``) consult
  ``build_suppression_set`` BEFORE every send.  Even if the persisted flag missed
  a row, the response-classification and stage checks catch it.

* **Write side** — ``apply_suppression`` issues a single typed PATCH via
  ``AttioWriter.apply(WriteIntent(...))`` with ``writer_module`` set to this
  module's dotted path so the §3.15 registry authorizes the write.  Idempotent:
  no-op when the prior value is already True.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from models.pipeline import PipelineStage

if TYPE_CHECKING:
    from clients.attio import AttioClient
    from clients.attio_writer import AttioWriter

NEGATIVE_RESPONSE_CLASSIFICATIONS: frozenset[str] = frozenset({"negative", "defensive"})
SUPPRESSED_STAGES: frozenset[str] = frozenset({
    PipelineStage.NOT_INTERESTED.value,
    PipelineStage.DEFENSIVE_HOLD.value,
})

LINKEDIN_OUTREACH_LIST_ID_ENV = "ATTIO_LIST_ID"
WRITER_MODULE = "workflows.cross_channel_suppression"

# PR-40: Person-level outreach-channel attribute. The §3.1 hard red line
# extends across DM, email, and association-outreach channels; this attr
# is the persisted record of which channel(s) a Person has been contacted
# through so the next channel's gate can consult it. Multi-valued via
# repeated PATCHes (Attio select with multiple values).
OUTREACH_CHANNEL_LINKEDIN = "linkedin"
OUTREACH_CHANNEL_EMAIL = "email"
OUTREACH_CHANNEL_ASSOCIATION = "association_outreach"
OUTREACH_CHANNELS: frozenset[str] = frozenset({
    OUTREACH_CHANNEL_LINKEDIN,
    OUTREACH_CHANNEL_EMAIL,
    OUTREACH_CHANNEL_ASSOCIATION,
})


def is_suppressed(entry_attrs: dict) -> bool:
    """Return True if this list entry must be suppressed from re-engagement.

    ``entry_attrs`` is the dict returned by ``AttioClient.parse_entry``.  Three
    independent triggers — ANY truthy trigger suppresses.  Belt-and-suspenders
    runtime defense per §3.16 (manual-reply path may not have written
    ``response_classification``) and §3.18 (NURTURE re-engagement allowlist).
    """
    if entry_attrs.get("suppress_re_engagement"):
        return True
    if entry_attrs.get("response_classification") in NEGATIVE_RESPONSE_CLASSIFICATIONS:
        return True
    return entry_attrs.get("stage") in SUPPRESSED_STAGES


def build_suppression_set(attio: AttioClient) -> set[str]:
    """Return the set of Person ``record_id``s suppressed from re-engagement.

    Pulls every list entry once (single round-trip via auto-paginated
    ``query_list_entries``) and returns the union of every entry's parent
    ``record_id`` where ``is_suppressed`` evaluates True.

    Raises:
        RuntimeError: if ``ATTIO_LIST_ID`` is unset or empty (§0 #9 — no silent
            fallback to an empty set, which would silently disable suppression).
    """
    from clients.attio import AttioClient as _AttioClient  # local for type narrowing

    list_id = os.environ.get(LINKEDIN_OUTREACH_LIST_ID_ENV, "").strip()
    if not list_id:
        raise RuntimeError(
            f"LinkedIn Outreach list ID not set. "
            f"Export {LINKEDIN_OUTREACH_LIST_ID_ENV}=<uuid> and retry."
        )

    entries = attio.query_list_entries(
        list_id=list_id, limit=100_000, fail_if_truncated=True,
    )
    suppressed: set[str] = set()
    for entry in entries:
        attrs = _AttioClient.parse_entry(entry)
        if is_suppressed(attrs):
            record_id = attrs.get("record_id")
            if record_id:
                suppressed.add(record_id)
    return suppressed


def stamp_outreach_channel(
    writer: AttioWriter,
    attio: AttioClient,
    *,
    person_record_id: str,
    channel: str,
    writer_module: str = WRITER_MODULE,
) -> bool:
    """Append ``channel`` to a Person's ``outreach_channel`` select.

    PR-40 surface — the §3.15 registry-authorized writer for the Person-
    side outreach_channel slug. Callers (DM sender, email sender, the
    migration script) invoke this BEFORE sending so the cross-channel
    suppression record reflects the channels the prospect has been
    contacted on.

    The function fetches the current ``outreach_channel`` values from
    Attio internally and computes the union — Attio multi-select PATCH
    is REPLACEMENT semantics, so a caller that forgot to pass the prior
    array would wipe the other channels' values and break §3.1. Owning
    the read-then-merge inside the writer removes that footgun.

    Args:
        writer: AttioWriter the WriteIntent dispatches through.
        attio: AttioClient used to read the current outreach_channel values.
        person_record_id: target Person record_id.
        channel: one of OUTREACH_CHANNELS.
        writer_module: optional writer_module override for the §3.15
            registry. Defaults to ``workflows.cross_channel_suppression``;
            the migration script passes its own dotted path so the
            MigrationRunWriter audit trail reflects the actor.

    Returns True if a PATCH was issued, False when ``channel`` is
    already present (no-op idempotent).

    Raises:
        ValueError: ``channel`` not in ``OUTREACH_CHANNELS``.
    """
    from clients.attio_writer import WriteIntent  # local: avoid cycles

    if channel not in OUTREACH_CHANNELS:
        raise ValueError(
            f"channel={channel!r} not in OUTREACH_CHANNELS ({sorted(OUTREACH_CHANNELS)})"
        )

    # Read-then-merge: fetch current state from Attio and compute the
    # union here so caller code can't forget to pass the prior array
    # and accidentally wipe other channels.
    existing = _fetch_outreach_channels(attio, person_record_id)
    if channel in existing:
        return False

    new_channels = sorted(set(existing) | {channel})
    intent = WriteIntent(
        object="people",
        record_id=person_record_id,
        updates={"outreach_channel": new_channels},
        prior_values={"outreach_channel": existing},
        writer_module=writer_module,
        is_list_entry=False,
    )
    writer.apply(intent)
    return True


def _fetch_outreach_channels(attio: AttioClient, person_record_id: str) -> list[str]:
    """Read the current ``outreach_channel`` select titles for a Person."""

    record = attio.get_person(person_record_id) if hasattr(attio, "get_person") else None
    if record is None:
        # Fall back to a raw GET if no get_person helper exists; never
        # silent-fall to [] which would let the PATCH wipe values.
        record = attio._request("GET", f"/objects/people/records/{person_record_id}")
        record = record.get("data", record)

    values = (record or {}).get("values") or {}
    raw = values.get("outreach_channel", [])
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            opt = item.get("option") or {}
            title = opt.get("title")
            if title:
                out.append(title)
    return out


def apply_suppression(
    writer: AttioWriter,
    *,
    entry_id: str,
    list_id: str,
    prior_suppress: bool | None,
) -> bool:
    """Set ``suppress_re_engagement=True`` on a single list entry.

    Returns True if a PATCH was issued, False if skipped as a no-op (prior was
    already True).  The §3.15 registry authorizes this writer for this slug.

    Args:
        writer: an ``AttioWriter`` instance.
        entry_id: the list entry id (NOT the parent record_id).
        list_id: the LinkedIn Outreach list uuid.
        prior_suppress: the existing value of ``suppress_re_engagement`` (may be
            None for legacy entries).  When already True we skip the write.
    """
    from clients.attio_writer import WriteIntent  # local import: avoid cycles

    if prior_suppress is True:
        return False

    intent = WriteIntent(
        object="linkedin_outreach",
        record_id=entry_id,
        updates={"suppress_re_engagement": True},
        prior_values={"suppress_re_engagement": prior_suppress},
        writer_module=WRITER_MODULE,
        is_list_entry=True,
        list_id=list_id,
    )
    writer.apply(intent)
    return True
