"""Distinct types for the two LinkedIn URL forms used in this codebase.

Two URL shapes exist:

- `CanonicalLinkedInUrl` — `linkedin.com/in/<slug>` form. This is the canonical
  dedup key in Attio (see `_canonical_linkedin_url` in `clients/attio.py:67`).
  All caller-facing code, all Attio reads/writes, and the `recheck_cache` key
  use this form.

- `SalesNavUrl` — `linkedin.com/sales/lead/<id>,<authToken>` form. Required by
  the PhantomBuster Sales Navigator Profile Scraper. Stored on the Attio Person
  record as the `sales_nav_url` attribute (populated by the backfill +
  enrichment hook). Never used as a dedup key — there is a one-to-one map from
  canonical -> sales-nav, but the inverse is fragile because the authToken
  segment is viewer-session-scoped on some accounts.

These are `NewType` aliases — they have zero runtime cost. They exist so that
mypy/ruff catches the most likely class of bug in this migration: passing a
`SalesNavUrl` where a `CanonicalLinkedInUrl` is expected (or vice versa).
"""

from __future__ import annotations

from typing import NewType

CanonicalLinkedInUrl = NewType("CanonicalLinkedInUrl", str)
SalesNavUrl = NewType("SalesNavUrl", str)
