"""Diacritic-normalized identity matching between LinkedIn threads
and Attio LinkedIn Outreach list entries (PR-14, B-SD-004 + B-PD-004).

# Why this exists

PhantomBuster inbox-scrape returns thread metadata keyed by the
participant's display name + LinkedIn URL. Matching that thread back
to the entry that issued the outbound DM relies on:

  1. **Vanity URL slug** — `linkedin.com/in/<slug>` is the canonical
     identity. The PR-9a schema added `canonical_linkedin_url` +
     `vanity_url_slug` to LinkedIn Outreach entries; this module
     prefers the vanity-slug join when both sides resolve.

  2. **Diacritic-normalized name** — when vanity-slug matching fails
     (LinkedIn URL changed, scrape returned a different display URL,
     etc.), fall back to name comparison. "Carlos López" and "Carlos
     Lopez" must compare equal — diacritics are display-only and
     do not disambiguate identity.

The pre-PR-14 `detect_responses._normalize_name` only did
lowercase + whitespace collapse. Accented names from LATAM
prospects ("Iñigo", "Andrés", "José María") matched the
ASCII-stripped scrape form ONLY when LinkedIn happened to emit the
same form on both sides — flaky. PR-14 makes the normalization
locale-stable: `Iñigo` and `Inigo` always compare equal.

# Return shape

`match_thread_to_entries` returns a **ranked list** of
`MatchCandidate(entry, score, reason)` tuples, not a single match.
Callers must decide whether to:
  - take the top match (high-confidence vanity-slug join), or
  - escalate ambiguity (multiple name-only matches at the same
    confidence band) via an `ambiguous_reply_match` queue row.

Confidence scores:
  - 1.0 — vanity slug exact match.
  - 0.85 — diacritic-normalized full name exact match.
  - 0.70 — diacritic-normalized first+last name match (middle names
    optional on either side).
  - <0.7 — not returned (sub-threshold).
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from clients.attio import _vanity_url_slug


@dataclass(frozen=True)
class MatchCandidate:
    """One entry that could be the thread's counterparty, with confidence."""
    entry_id: str
    record_id: str
    score: float
    reason: str  # e.g. "vanity_slug_exact" | "name_diacritic_exact" | "name_first_last"


def normalize_for_match(name: str) -> str:
    """Locale-stable name normalization for fuzzy thread-to-entry match.

    Pipeline:
      1. NFKD-decompose so accented chars split into base + combining marks.
      2. Strip non-ASCII (drops the combining marks; "Iñigo" -> "Inigo").
      3. Lowercase + collapse internal whitespace.

    The stdlib `unicodedata` + `encode('ascii', 'ignore')` pattern is
    the canonical Python idiom — no third-party dependency needed.
    """
    if not name:
        return ""
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_form = decomposed.encode("ascii", "ignore").decode("ascii")
    return " ".join(ascii_form.lower().strip().split())


def match_thread_to_entries(
    thread_participant_name: str,
    thread_participant_linkedin_url: str | None,
    entries: list[dict],
) -> list[MatchCandidate]:
    """Match a LinkedIn thread to LinkedIn Outreach entries.

    Args:
        thread_participant_name: the display name PB returned for the
            counterparty (e.g. "Carlos López"). May contain accents
            and casing variation.
        thread_participant_linkedin_url: the URL PB returned, if any.
            Pass None when the scrape didn't include a URL (older PB
            agents); the match then relies on name only.
        entries: list of LinkedIn Outreach entry dicts. Each entry MUST
            carry these keys (any may be empty/missing per the schema):
              - entry_id: Attio list-entry ID
              - record_id: Attio person record ID
              - vanity_url_slug: from the schema (PR-9a-shipped)
              - name: prospect's display name
              - linkedin_url: prospect's profile URL (canonical form
                preferred but the helper recomputes the vanity slug)

    Returns:
        Ranked list of MatchCandidate, highest score first. Empty list
        when no entry meets the 0.70 confidence floor.

    Caller policy:
        - len(returned) == 1 and score >= 0.85: safe to attribute the
          thread to that entry without operator review.
        - len(returned) > 1 with scores within 0.10 of each other:
          ambiguous — caller should open an `ambiguous_reply_match`
          queue row instead of guessing.
        - score < 0.85 even with len == 1: caller should treat as
          tentative and verify via a secondary signal before any
          state mutation.
    """
    if not entries:
        return []

    thread_vanity = (
        _vanity_url_slug(thread_participant_linkedin_url)
        if thread_participant_linkedin_url
        else ""
    )
    thread_name_norm = normalize_for_match(thread_participant_name)

    candidates: list[MatchCandidate] = []
    for entry in entries:
        entry_vanity = (entry.get("vanity_url_slug") or "").strip().lower()
        # Recompute from linkedin_url if the stored slug is empty —
        # pre-PR-14 rows may not have it set even after PR-9a backfilled
        # canonical_linkedin_url.
        if not entry_vanity:
            entry_vanity = _vanity_url_slug(entry.get("linkedin_url") or "")

        # 1.0: vanity slug exact match — strongest signal.
        if thread_vanity and entry_vanity and thread_vanity == entry_vanity:
            candidates.append(MatchCandidate(
                entry_id=str(entry.get("entry_id", "")),
                record_id=str(entry.get("record_id", "")),
                score=1.0,
                reason="vanity_slug_exact",
            ))
            continue

        entry_name_norm = normalize_for_match(entry.get("name") or "")
        if not thread_name_norm or not entry_name_norm:
            continue

        # 0.85: diacritic-normalized full name exact match.
        if thread_name_norm == entry_name_norm:
            candidates.append(MatchCandidate(
                entry_id=str(entry.get("entry_id", "")),
                record_id=str(entry.get("record_id", "")),
                score=0.85,
                reason="name_diacritic_exact",
            ))
            continue

        # 0.70: first+last tokens match (allows extra/missing middles).
        thread_tokens = thread_name_norm.split()
        entry_tokens = entry_name_norm.split()
        if (
            len(thread_tokens) >= 2
            and len(entry_tokens) >= 2
            and thread_tokens[0] == entry_tokens[0]
            and thread_tokens[-1] == entry_tokens[-1]
        ):
            candidates.append(MatchCandidate(
                entry_id=str(entry.get("entry_id", "")),
                record_id=str(entry.get("record_id", "")),
                score=0.70,
                reason="name_first_last",
                ))

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates
