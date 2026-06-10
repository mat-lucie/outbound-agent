#!/usr/bin/env python3
"""Attio dedup — one-off cleanup for records created by the sales agent.

Two modes:
  scan   (default) — read-only. Detect duplicate people by LinkedIn URL and
                     duplicate companies by domain. Emit dedup_report.json
                     with auto_safe vs conflict groups.
  apply           — execute a triaged dedup_report.json. Merges non-empty
                     fields forward onto the winner, reparents the
                     most-advanced linkedin_outreach list entry, deletes
                     losers. Writes dedup_apply_log.jsonl for audit.

Workflow:
  1.  python scripts/attio_dedup.py scan > dedup_report.json
  2.  human review — open dedup_report.json; move any conflict group
      you want merged into "approved_conflict_groups", and any group you
      want to leave alone into "skipped_groups". Every conflict must
      appear in one bucket before apply will run.
  3.  python scripts/attio_dedup.py apply --report dedup_report.json

Safety:
  - Dry-run is the default; delete operations only happen under "apply".
  - The apply step refuses reports older than 24h.
  - Each group applies atomically per object: if any API call in a group
    errors, that group is aborted and logged; the run continues.
  - Before deleting losers, their full attribute snapshot is written to
    dedup_apply_log.jsonl so operators can reconstruct them if needed.
"""

from __future__ import annotations

import functools
import json
import os
import re
import sys
import unicodedata
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import click
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx  # noqa: E402

from clients.attio import AttioClient  # noqa: E402
from workflows.escalation import escalate  # noqa: E402

# Transient errors that can legitimately happen mid-run against Attio and
# should NOT halt the whole union-merge: HTTP failures, network blips, and
# socket-level connectivity errors. Anything else (TypeError / KeyError /
# AttributeError / ImportError / NameError / RuntimeError / etc.) is a code
# bug and must propagate so the run fails loudly per §3 hard constraint #9
# (no silent fallbacks). silent-failure-hunter CRIT-1/CRIT-3.
_TRANSIENT_ATTIO_ERRORS: tuple[type[BaseException], ...] = (
    httpx.HTTPStatusError,
    httpx.RequestError,
    ConnectionError,
    TimeoutError,
)

# Pipeline stage ranking for "most advanced wins" on duplicate list entries.
# Prospect and Partner Intro are parallel starts, so they share rank 1.
# Not Interested is terminal and outranks Qualified (a resolved prospect
# should not be demoted back into the funnel).
STAGE_RANK: dict[str, int] = {
    "Prospect": 1,
    "Partner Intro": 1,
    "Connection Sent": 2,
    "Accepted": 3,
    "DM1 Sent": 4,
    "DM2 Sent": 5,
    "DM3 Sent": 6,
    "Responded": 7,
    "Call Booked": 8,
    "Qualified": 9,
    "Not Interested": 10,
}

# Fields on people we consider for conflict detection + merge-forward.
# A conflict is "two records both have non-empty, non-equal values for this field".
PEOPLE_COMPARE_FIELDS = (
    "name",
    "linkedin",
    "job_title",
    "company",
    "email_addresses",
    "phone_numbers",
)

COMPANY_COMPARE_FIELDS = (
    "name",
    "domains",
    "description",
)

# Deal stage ranking — winner = highest rank wins when merging duplicates.
# Order reflects the Attio "deals" pipeline as seen in the workspace.
# Lost outranks Lead intentionally: a resolved deal should not be demoted
# back to the cold pool.
DEAL_STAGE_RANK: dict[str, int] = {
    "Lead": 1,
    "Qualified": 2,
    "Proposal": 3,
    "Negotiation": 4,
    "In Progress": 5,
    "Won": 6,
    "Lost": 6,
}

# Fields on deals to forward-merge onto the winner when the winner field is
# empty. Deliberately empty: deal-shaped values (country_code, currency_value,
# referenced_actor_id) are not handled by `comparable()` / `extract_writable_list()`,
# and forward-merging deal names would clobber the canonical winner name with
# a dated/country-suffixed loser variant. The only meaningful "merge" for
# deals is the associated_people union, which is computed separately and
# attached to the merge payload directly.
DEAL_MERGE_FIELDS: tuple[str, ...] = ()

# Deal-name prefix/suffix patterns to strip before name-based bucketing.
# Examples: "202507 l Bebidas Modelo l BR" → "bebidas modelo"
#           "202511 | Ypê (Química Amparo S.A.) | BR" → "ypê (química amparo s.a.)"
#           "MX - Farmacias del ahorro" → "farmacias del ahorro"
#
# The country-suffix/prefix regexes use a closed allowlist of LATAM ISO codes
# (plus US/CA/ES/IT/DE for the few international deals already in the workspace)
# rather than `[a-z]{2}`. The wildcard version was over-greedy — a name like
# "Foo - HP" or "Acme l GE" would collapse the brand suffix as if it were a
# country code, which is wrong. Allowlist keeps the strip conservative.
_LATAM_CODES = "(?:ar|bo|br|cl|co|cr|do|ec|es|gt|hn|it|mx|ni|pa|pe|pr|py|sv|us|uy|ve|ca|de)"
_DEAL_NAME_PREFIX_RE = re.compile(r"^\s*\d{4,6}\s*[\|l\-:]+\s*", re.IGNORECASE)
_DEAL_NAME_COUNTRY_SUFFIX_RE = re.compile(
    rf"\s+[\|l\-:]+\s*{_LATAM_CODES}\s*$", re.IGNORECASE
)
_DEAL_NAME_COUNTRY_PREFIX_RE = re.compile(
    rf"^{_LATAM_CODES}\s*[\|l\-:]+\s*", re.IGNORECASE
)
# Deal names in the original operator's CRM carried a "— <product>" suffix.
# The token is operator-specific; set OUTBOUND_DEAL_NAME_SUFFIX to enable
# stripping it (default: no product-suffix stripping).
def _product_suffix_re() -> re.Pattern[str] | None:
    token = os.environ.get("OUTBOUND_DEAL_NAME_SUFFIX", "").strip()
    if not token:
        return None
    return _compile_product_suffix_re(token)


@functools.lru_cache(maxsize=4)
def _compile_product_suffix_re(token: str) -> re.Pattern[str]:
    return re.compile(
        rf"\s+[\|l\-\u2014\u2013]+\s*{re.escape(token)}\s*$", re.IGNORECASE
    )

# List-entry attributes where divergence across a bucket should be flagged
# as a conflict even when the person records themselves look identical.
# Losing these to the "most-advanced stage wins" reducer would lose real
# signal the sales agent depends on.
LIST_ENTRY_CONFLICT_FIELDS = (
    "persona",
    "language",
    "response_classification",
    "experiment_id",
)


# ── Normalization helpers ─────────────────────────────────────


def normalize_linkedin_url(url: str) -> str:
    """Normalize a LinkedIn profile URL so encoded/accented variants match."""
    if not url:
        return ""
    s = url.strip().lower()
    s = urllib.parse.unquote(s)
    s = unicodedata.normalize("NFC", s)
    for prefix in ("https://", "http://", "//"):
        if s.startswith(prefix):
            s = s[len(prefix) :]
            break
    if s.startswith("www."):
        s = s[4:]
    for sep in ("?", "#"):
        if sep in s:
            s = s.split(sep)[0]
    return s.rstrip("/")


def normalize_domain(domain: str) -> str:
    """Normalize a domain string for bucketing."""
    if not domain:
        return ""
    s = domain.strip().lower()
    for prefix in ("https://", "http://", "//"):
        if s.startswith(prefix):
            s = s[len(prefix) :]
            break
    if s.startswith("www."):
        s = s[4:]
    return s.split("/")[0].rstrip(".")


def comparable(values_list: Any) -> str | None:
    """Return a comparable string for an Attio attribute value list.

    Attio stores every attribute as a list of dicts; shape varies by type.
    Returns None if the attribute is empty (no values).
    """
    if not values_list:
        return None
    if not isinstance(values_list, list):
        return str(values_list).strip().lower()
    first = values_list[0]
    if first is None:
        return None
    if not isinstance(first, dict):
        return str(first).strip().lower()
    # personal_name
    if "full_name" in first and first["full_name"]:
        return str(first["full_name"]).strip().lower()
    if "first_name" in first or "last_name" in first:
        joined = f"{first.get('first_name', '')} {first.get('last_name', '')}".strip().lower()
        return joined or None
    # status (list entry "stage")
    if isinstance(first.get("status"), dict):
        title = first["status"].get("title", "")
        return title.strip() if title else None
    # select
    if isinstance(first.get("option"), dict):
        title = first["option"].get("title", "")
        return title.strip() if title else None
    # record-reference
    if first.get("target_record_id"):
        return str(first["target_record_id"])
    # email / phone / domain
    if first.get("email_address"):
        return str(first["email_address"]).strip().lower()
    if first.get("phone_number"):
        return str(first["phone_number"]).strip()
    if first.get("domain"):
        return normalize_domain(str(first["domain"]))
    # text / number / date / linkedin
    if "value" in first and first["value"] is not None:
        v = first["value"]
        if isinstance(v, str):
            return v.strip().lower()
        return str(v)
    return None


def extract_writable_value(values_list: Any) -> Any:
    """Extract the writable value from an Attio attribute value list.

    Strips Attio's read-only metadata wrappers (`active_from`, `active_until`,
    `created_by_actor`, `attribute_type`) that the PATCH/PUT endpoints reject.
    Returns the first item's value in a shape Attio accepts on write:
      - select/status → title string
      - record-reference → {"target_object", "target_record_id"}
      - email/phone/domain → the raw address/number/domain string
      - text/number/date → the scalar `value`
      - name (personal_name) → {"first_name", "last_name", "full_name"}
    Returns None if empty.
    """
    if not values_list or not isinstance(values_list, list):
        return None
    first = values_list[0]
    if not isinstance(first, dict):
        return first
    # select
    if isinstance(first.get("option"), dict):
        return first["option"].get("title")
    # status (stage is handled separately via stage_name)
    if isinstance(first.get("status"), dict):
        return first["status"].get("title")
    # record-reference
    if first.get("target_record_id"):
        return {
            "target_object": first.get("target_object", "people"),
            "target_record_id": first["target_record_id"],
        }
    # personal_name
    if first.get("full_name") or first.get("first_name") or first.get("last_name"):
        return {
            "first_name": first.get("first_name", ""),
            "last_name": first.get("last_name", ""),
            "full_name": first.get("full_name", ""),
        }
    # email / phone / domain
    for key in ("email_address", "phone_number", "domain"):
        if first.get(key):
            return first[key]
    # text / number / date / linkedin / generic value
    if "value" in first and first["value"] is not None:
        return first["value"]
    return None


def extract_writable_list(values_list: Any) -> list | Any | None:
    """Like `extract_writable_value` but for list-typed attributes (e.g.,
    `company` or `email_addresses`), Attio's PATCH endpoint accepts a list
    of writable dicts. Returns the original list shape with every item's
    metadata wrappers stripped, so multi-valued attributes round-trip cleanly.
    """
    if not values_list or not isinstance(values_list, list):
        return None
    cleaned = []
    for item in values_list:
        if not isinstance(item, dict):
            cleaned.append(item)
            continue
        # record-reference
        if item.get("target_record_id"):
            cleaned.append({
                "target_object": item.get("target_object", "people"),
                "target_record_id": item["target_record_id"],
            })
            continue
        # personal_name
        if item.get("full_name") or item.get("first_name") or item.get("last_name"):
            cleaned.append({
                "first_name": item.get("first_name", ""),
                "last_name": item.get("last_name", ""),
                "full_name": item.get("full_name", ""),
            })
            continue
        # email / phone / domain
        kept = False
        for key in ("email_address", "phone_number", "domain"):
            if item.get(key):
                cleaned.append({key: item[key]})
                kept = True
                break
        if kept:
            continue
        # select/status — write form is {"option": "<title>"} or similar;
        # simplest shape that Attio accepts is {"value": <title>}.
        if isinstance(item.get("option"), dict):
            cleaned.append({"value": item["option"].get("title")})
            continue
        if isinstance(item.get("status"), dict):
            cleaned.append({"value": item["status"].get("title")})
            continue
        # text/number/date — scalars must be wrapped as {"value": X} in
        # list form for Attio's PATCH to accept them.
        if "value" in item and item["value"] is not None:
            cleaned.append({"value": item["value"]})
    return cleaned if cleaned else None


def record_linkedin(record: dict) -> str:
    """Extract normalized LinkedIn URL from a person record."""
    raw = comparable(record.get("values", {}).get("linkedin", []))
    return raw or ""


def record_primary_domain(record: dict) -> str:
    """Extract normalized primary domain from a company record."""
    raw = comparable(record.get("values", {}).get("domains", []))
    return raw or ""


def record_created_at(record: dict) -> str:
    """Return the record's created_at timestamp, or empty string if missing."""
    values = record.get("values", {})
    created = values.get("created_at", [])
    if created and isinstance(created, list):
        ts = created[0].get("value") if isinstance(created[0], dict) else created[0]
        if ts:
            return str(ts)
    return record.get("created_at", "") or ""


def record_id(record: dict) -> str:
    """Return the record_id from an Attio record dict."""
    rid = record.get("id")
    if isinstance(rid, dict):
        return rid.get("record_id", "")
    return str(rid) if rid else ""


def entry_id(entry: dict) -> str:
    """Return the entry_id from an Attio list-entry dict."""
    eid = entry.get("id")
    if isinstance(eid, dict):
        return eid.get("entry_id", "")
    return str(eid) if eid else entry.get("entry_id", "")


def entry_parent(entry: dict) -> str:
    """Return the parent_record_id for a list entry."""
    pid = entry.get("parent_record_id")
    if pid:
        return pid
    pid2 = entry.get("id", {})
    if isinstance(pid2, dict):
        return pid2.get("record_id", "")
    return ""


def entry_stage(entry: dict) -> str:
    """Return the stage title for a list entry."""
    return comparable(entry.get("entry_values", {}).get("stage", [])) or ""


# ── Scan: detect duplicates ──────────────────────────────────


def pick_winner(records: list[dict]) -> dict:
    """Winner = oldest created_at. Ties broken by record_id for determinism."""
    return sorted(records, key=lambda r: (record_created_at(r), record_id(r)))[0]


def compute_merge_fields(winner: dict, losers: list[dict], fields: tuple[str, ...]) -> dict:
    """For each field empty on the winner but populated on a loser, extract
    the loser's writable value so it can be PATCHed onto the winner.
    Strips Attio's read-only metadata wrappers (`active_from`, `attribute_type`,
    etc.) — the PATCH endpoint rejects those. If multiple losers have the
    field, first loser by oldest created_at wins.
    """
    merge: dict = {}
    w_values = winner.get("values", {})
    for field in fields:
        if comparable(w_values.get(field, [])) is not None:
            continue  # winner already has a value
        # find the earliest loser with a non-empty value for this field
        candidates = [
            loser for loser in sorted(losers, key=lambda r: record_created_at(r))
            if comparable(loser.get("values", {}).get(field, [])) is not None
        ]
        if candidates:
            raw = candidates[0]["values"][field]
            # Use list-form for multi-valued attributes Attio expects as lists.
            writable = extract_writable_list(raw)
            if writable is not None:
                merge[field] = writable
    return merge


def detect_conflicts(
    group: list[dict],
    fields: tuple[str, ...],
) -> list[str]:
    """Return a list of conflict reasons (empty list = auto-safe)."""
    reasons: list[str] = []
    for field in fields:
        seen_values: set[str] = set()
        seen_on: list[str] = []
        for rec in group:
            val = comparable(rec.get("values", {}).get(field, []))
            if val is None:
                continue
            if val not in seen_values:
                seen_values.add(val)
                seen_on.append(f"{record_id(rec)[:8]}={val!r}")
        if len(seen_values) > 1:
            reasons.append(f"{field}: " + ", ".join(seen_on))
    return reasons


def detect_list_entry_conflicts(
    bucket_ids: set[str],
    entries_by_person: dict[str, list[dict]],
) -> list[str]:
    """Flag divergent list-entry attribute values across a bucket's entries.

    Since dedup keeps only the most-advanced entry, losing signal on fields
    like persona or response_classification is real data loss.
    """
    reasons: list[str] = []
    all_entries: list[dict] = []
    for pid in bucket_ids:
        all_entries.extend(entries_by_person.get(pid, []))
    if len(all_entries) < 2:
        return reasons

    for field in LIST_ENTRY_CONFLICT_FIELDS:
        seen_values: set[str] = set()
        seen_on: list[str] = []
        for e in all_entries:
            val = comparable(e.get("entry_values", {}).get(field, []))
            if val is None:
                continue
            if val not in seen_values:
                seen_values.add(val)
                seen_on.append(f"{entry_id(e)[:8]}={val!r}")
        if len(seen_values) > 1:
            reasons.append(f"entry.{field}: " + ", ".join(seen_on))
    return reasons


def plan_list_entry_actions(
    winner_id: str,
    bucket_ids: set[str],
    entries_by_person: dict[str, list[dict]],
) -> list[dict]:
    """Decide what to do with linkedin_outreach entries across the bucket.

    Strategy: find the most-advanced entry (highest stage rank, tiebreak by
    oldest created_at). Keep its stage+entry_values on the winner; delete
    everything else.

    Returns a list of action dicts:
      {"type": "keep",     "entry_id": ..., "stage": ...}
      {"type": "delete",   "entry_id": ..., "stage": ..., "parent_id": ...}
      {"type": "reparent", "entry_id": ..., "stage": ..., "entry_values": {...}, "from_parent": ...}
    """
    all_entries: list[dict] = []
    for pid in bucket_ids:
        all_entries.extend(entries_by_person.get(pid, []))
    if not all_entries:
        return []

    def sort_key(e: dict) -> tuple[int, str]:
        rank = STAGE_RANK.get(entry_stage(e), 0)
        # negate rank so highest-rank comes first; tiebreak by oldest created_at
        created = e.get("created_at") or ""
        return (-rank, created)

    sorted_entries = sorted(all_entries, key=sort_key)
    max_entry = sorted_entries[0]
    max_entry_id = entry_id(max_entry)
    max_entry_parent = entry_parent(max_entry)

    actions: list[dict] = []
    if max_entry_parent == winner_id:
        actions.append({
            "type": "keep",
            "entry_id": max_entry_id,
            "stage": entry_stage(max_entry),
        })
        for e in sorted_entries[1:]:
            actions.append({
                "type": "delete",
                "entry_id": entry_id(e),
                "stage": entry_stage(e),
                "parent_id": entry_parent(e),
            })
    else:
        # the best entry lives on a loser → reparent to winner
        actions.append({
            "type": "reparent",
            "entry_id": max_entry_id,
            "stage": entry_stage(max_entry),
            "entry_values": max_entry.get("entry_values", {}),
            "from_parent": max_entry_parent,
        })
        for e in sorted_entries[1:]:
            actions.append({
                "type": "delete",
                "entry_id": entry_id(e),
                "stage": entry_stage(e),
                "parent_id": entry_parent(e),
            })
    return actions


def bucket_people(
    records: list[dict],
    entries_by_person: dict[str, list[dict]],
) -> tuple[list[dict], list[dict]]:
    """Bucket people by normalized LinkedIn URL. Return (auto_safe_groups, conflict_groups)."""
    buckets: dict[str, list[dict]] = {}
    for rec in records:
        key = record_linkedin(rec)
        if not key:
            continue  # people without a linkedin URL can't be safely bucketed
        buckets.setdefault(key, []).append(rec)

    auto_safe: list[dict] = []
    conflicts: list[dict] = []
    for key, group in buckets.items():
        if len(group) < 2:
            continue
        winner = pick_winner(group)
        losers = [r for r in group if record_id(r) != record_id(winner)]
        winner_id = record_id(winner)
        bucket_ids = {record_id(r) for r in group}

        reasons = detect_conflicts(group, PEOPLE_COMPARE_FIELDS)
        reasons.extend(detect_list_entry_conflicts(bucket_ids, entries_by_person))
        entry_actions = plan_list_entry_actions(winner_id, bucket_ids, entries_by_person)

        group_entry = {
            "linkedin_url_normalized": key,
            "winner_id": winner_id,
            "loser_ids": [record_id(r) for r in losers],
            "fields_to_merge": compute_merge_fields(winner, losers, PEOPLE_COMPARE_FIELDS),
            "list_entry_actions": entry_actions,
        }
        if reasons:
            group_entry["conflict_reasons"] = reasons
            conflicts.append(group_entry)
        else:
            auto_safe.append(group_entry)

    return auto_safe, conflicts


def bucket_companies(records: list[dict]) -> tuple[list[dict], list[dict]]:
    """Bucket companies by primary domain. Return (auto_safe_groups, conflict_groups)."""
    buckets: dict[str, list[dict]] = {}
    for rec in records:
        key = record_primary_domain(rec)
        if not key:
            continue
        buckets.setdefault(key, []).append(rec)

    auto_safe: list[dict] = []
    conflicts: list[dict] = []
    for key, group in buckets.items():
        if len(group) < 2:
            continue
        winner = pick_winner(group)
        losers = [r for r in group if record_id(r) != record_id(winner)]
        reasons = detect_conflicts(group, COMPANY_COMPARE_FIELDS)
        group_entry = {
            "domain_normalized": key,
            "winner_id": record_id(winner),
            "loser_ids": [record_id(r) for r in losers],
            "fields_to_merge": compute_merge_fields(winner, losers, COMPANY_COMPARE_FIELDS),
        }
        if reasons:
            group_entry["conflict_reasons"] = reasons
            conflicts.append(group_entry)
        else:
            auto_safe.append(group_entry)

    return auto_safe, conflicts


def normalize_deal_name(name: str) -> str:
    """Normalize a deal name so labeling variants (date prefixes, country
    suffixes, "— <product>" suffix per $OUTBOUND_DEAL_NAME_SUFFIX) collapse
    to the same bucket key.

    Examples:
      "202507 l Bebidas Modelo l BR"        → "bebidas modelo"
      "202511 | Quimêra (Química Eg) | BR"  → "quimêra (química eg)"
      "Ejemplosa — AcmeBot"                 → "ejemplosa"  (suffix="AcmeBot")
      "MX - Farmacias Modelo"               → "farmacias modelo"
    """
    if not name:
        return ""
    s = unicodedata.normalize("NFC", str(name)).strip()
    # iterate strip passes — order matters since prefix vs suffix
    prev = None
    while prev != s:
        prev = s
        s = _DEAL_NAME_PREFIX_RE.sub("", s)
        s = _DEAL_NAME_COUNTRY_PREFIX_RE.sub("", s)
        s = _DEAL_NAME_COUNTRY_SUFFIX_RE.sub("", s)
        suffix_re = _product_suffix_re()
        if suffix_re is not None:
            s = suffix_re.sub("", s)
        s = s.strip()
    return s.lower()


def deal_company_id(record: dict) -> str:
    """Extract associated_company.target_record_id from a deal record."""
    arr = record.get("values", {}).get("associated_company", [])
    if not arr or not isinstance(arr, list):
        return ""
    first = arr[0]
    if isinstance(first, dict):
        return first.get("target_record_id", "") or ""
    return ""


def deal_name_raw(record: dict) -> str:
    """Extract the raw name value from a deal record."""
    arr = record.get("values", {}).get("name", [])
    if not arr or not isinstance(arr, list):
        return ""
    first = arr[0]
    if isinstance(first, dict):
        return str(first.get("value", "") or "")
    return str(first or "")


def deal_stage_title(record: dict) -> str:
    """Extract the stage title from a deal record."""
    arr = record.get("values", {}).get("stage", [])
    if not arr or not isinstance(arr, list):
        return ""
    first = arr[0]
    if isinstance(first, dict):
        status = first.get("status")
        if isinstance(status, dict):
            return status.get("title", "") or ""
    return ""


def deal_associated_people(record: dict) -> list[str]:
    """Return the list of associated person record_ids on a deal."""
    arr = record.get("values", {}).get("associated_people", [])
    if not arr or not isinstance(arr, list):
        return []
    out: list[str] = []
    for item in arr:
        if isinstance(item, dict):
            pid = item.get("target_record_id")
            if pid:
                out.append(pid)
    return out


def pick_deal_winner(records: list[dict]) -> dict:
    """Pick the canonical deal from a duplicate group.

    Tiebreakers, in order:
      1. Highest DEAL_STAGE_RANK (In Progress > Lead).
      2. Most associated_people (preserves the most relationship graph).
      3. Oldest created_at (preserves history continuity).
      4. record_id (deterministic final tiebreak).
    """
    def sort_key(r: dict) -> tuple:
        rank = DEAL_STAGE_RANK.get(deal_stage_title(r), 0)
        contacts = len(deal_associated_people(r))
        created = record_created_at(r) or ""
        rid = record_id(r) or ""
        # negate rank + contacts so highest come first
        return (-rank, -contacts, created, rid)

    return sorted(records, key=sort_key)[0]


def compute_deal_merge_fields(winner: dict, losers: list[dict]) -> dict:
    """For each DEAL_MERGE_FIELDS field empty on the winner but populated on
    a loser, extract a writable value to PATCH onto the winner. First loser
    by oldest created_at wins per field.
    """
    merge: dict = {}
    w_values = winner.get("values", {})
    for field in DEAL_MERGE_FIELDS:
        if comparable(w_values.get(field, [])) is not None:
            continue
        candidates = [
            loser for loser in sorted(losers, key=lambda r: record_created_at(r))
            if comparable(loser.get("values", {}).get(field, [])) is not None
        ]
        if candidates:
            raw = candidates[0]["values"][field]
            writable = extract_writable_list(raw)
            if writable is not None:
                merge[field] = writable
    return merge


def compute_deal_associated_people_union(
    winner: dict, losers: list[dict]
) -> list[dict] | None:
    """Build the union of associated_people across winner + losers.

    Returns the Attio writable list shape (list of {target_object, target_record_id})
    if there are any new person_ids to add to the winner, else None.
    """
    winner_pids = set(deal_associated_people(winner))
    loser_pids: set[str] = set()
    for loser in losers:
        loser_pids.update(deal_associated_people(loser))
    new_pids = loser_pids - winner_pids
    if not new_pids:
        return None
    # Need the full union for the PATCH (Attio replaces multi-valued fields, not appends).
    full = list(winner_pids) + list(new_pids)
    return [{"target_object": "people", "target_record_id": pid} for pid in full]


def bucket_deals(records: list[dict]) -> tuple[list[dict], list[dict]]:
    """Bucket deals into (auto_safe_groups, conflict_groups).

    Detection rules:
      - Two deals sharing the same `associated_company.target_record_id` → auto-safe
        (they literally point at the same company; near-zero false-positive risk).
      - Two deals sharing the same normalized name but DIFFERENT company UUIDs → conflict
        (could be a real sibling-plant deal OR a duplicate company-record problem
        upstream; needs the operator to look at it).
    Deals missing a `name` AND a `company_uuid` are skipped — they have no bucket key.
    """
    # Pass 1 — bucket by company_uuid (auto-safe)
    by_company: dict[str, list[dict]] = {}
    for rec in records:
        cid = deal_company_id(rec)
        if not cid:
            continue
        by_company.setdefault(cid, []).append(rec)

    auto_safe: list[dict] = []
    auto_safe_record_ids: set[str] = set()
    for cid, group in by_company.items():
        if len(group) < 2:
            continue
        winner = pick_deal_winner(group)
        losers = [r for r in group if record_id(r) != record_id(winner)]
        winner_id = record_id(winner)
        loser_ids = [record_id(r) for r in losers]

        merge = compute_deal_merge_fields(winner, losers)
        people_union = compute_deal_associated_people_union(winner, losers)
        if people_union is not None:
            merge["associated_people"] = people_union

        auto_safe.append({
            "key": f"company_uuid:{cid}",
            "match_signal": "same_company_uuid",
            "winner_id": winner_id,
            "winner_name": deal_name_raw(winner),
            "winner_stage": deal_stage_title(winner),
            "winner_contact_count": len(deal_associated_people(winner)),
            "loser_ids": loser_ids,
            "loser_summary": [
                {
                    "record_id": record_id(r),
                    "name": deal_name_raw(r),
                    "stage": deal_stage_title(r),
                    "contact_count": len(deal_associated_people(r)),
                }
                for r in losers
            ],
            "fields_to_merge_keys": list(merge.keys()),
            "fields_to_merge": merge,
        })
        auto_safe_record_ids.update({winner_id, *loser_ids})

    # Pass 2 — among records NOT already in an auto-safe group, bucket by normalized name
    # and flag cross-company-UUID collisions as conflicts.
    by_name: dict[str, list[dict]] = {}
    for rec in records:
        rid = record_id(rec)
        if rid in auto_safe_record_ids:
            continue
        norm = normalize_deal_name(deal_name_raw(rec))
        if not norm:
            continue
        by_name.setdefault(norm, []).append(rec)

    conflicts: list[dict] = []
    for norm, group in by_name.items():
        if len(group) < 2:
            continue
        company_ids = {deal_company_id(r) for r in group}
        if len(company_ids) < 2:
            # Same company UUID would have been caught by pass 1; if missed,
            # treat as conflict so the operator can review whether company UUID is missing.
            pass
        winner = pick_deal_winner(group)
        losers = [r for r in group if record_id(r) != record_id(winner)]
        conflicts.append({
            "key": f"name_normalized:{norm}",
            "match_signal": "same_normalized_name_different_company_uuid"
                if len(company_ids) > 1 else "same_normalized_name_missing_company_uuid",
            "winner_id": record_id(winner),
            "winner_name": deal_name_raw(winner),
            "winner_stage": deal_stage_title(winner),
            "winner_contact_count": len(deal_associated_people(winner)),
            "loser_ids": [record_id(r) for r in losers],
            "loser_summary": [
                {
                    "record_id": record_id(r),
                    "name": deal_name_raw(r),
                    "stage": deal_stage_title(r),
                    "company_uuid": deal_company_id(r),
                    "contact_count": len(deal_associated_people(r)),
                }
                for r in losers
            ],
            "winner_company_uuid": deal_company_id(winner),
            "conflict_reasons": [
                f"normalized_name={norm!r} but distinct company_uuids: "
                f"{sorted(c for c in company_ids if c)}"
            ] if len(company_ids) > 1 else [
                f"normalized_name={norm!r} matches but at least one record "
                f"has no associated_company link"
            ],
        })

    return auto_safe, conflicts


def build_deal_report(attio: AttioClient) -> dict:
    """Run the deals scan and build a deal-only dedup report."""
    click.echo("Fetching deals…", err=True)
    deals = attio.search_deals(filter_=None, limit=10000)
    click.echo(f"  {len(deals)} deal records", err=True)

    click.echo("Bucketing duplicate deals…", err=True)
    auto_safe, conflicts = bucket_deals(deals)

    return {
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "deals": {
            "auto_safe_groups": auto_safe,
            "conflict_groups": conflicts,
            "approved_conflict_groups": [],
            "skipped_groups": [],
        },
        "summary": {
            "deals_groups_auto_safe": len(auto_safe),
            "deals_groups_conflict": len(conflicts),
            "deals_records_to_delete": sum(len(g["loser_ids"]) for g in auto_safe),
        },
    }


def apply_deal_group(
    attio: AttioClient,
    group: dict,
    log_fh,
    pretend: bool,
) -> dict:
    """Merge a single deal dedup group. Returns a result summary dict."""
    winner_id = group["winner_id"]
    result: dict = {"object": "deals", "winner_id": winner_id, "actions": [], "errors": []}

    try:
        # 1. Merge non-empty fields + union of associated_people onto winner
        merge = group.get("fields_to_merge", {})
        if merge:
            if pretend:
                result["actions"].append({
                    "op": "update_deal",
                    "winner_id": winner_id,
                    "fields": list(merge.keys()),
                    "pretend": True,
                })
            else:
                attio.update_deal(winner_id, merge)
                result["actions"].append({
                    "op": "update_deal",
                    "winner_id": winner_id,
                    "fields": list(merge.keys()),
                })

        # 2. Snapshot + delete each loser
        for lid in group["loser_ids"]:
            snap = None if pretend else attio.get_deal(lid)
            if snap is not None:
                log_fh.write(json.dumps({
                    "kind": "snapshot",
                    "object": "deals",
                    "record_id": lid,
                    "snapshot": snap,
                }) + "\n")
            if pretend:
                result["actions"].append({"op": "delete_deal", "record_id": lid, "pretend": True})
            else:
                attio.delete_deal(lid)
                result["actions"].append({"op": "delete_deal", "record_id": lid})
    except Exception as e:
        result["errors"].append(f"{type(e).__name__}: {e}")

    return result


def apply_deal_report(
    report: dict,
    pretend: bool = False,
    log_path: str = "deal_dedup_apply_log.jsonl",
) -> dict:
    """Execute a triaged deal dedup report. Mirrors apply_report() shape."""
    check_freshness(report)

    # check triage for the deals section specifically
    section = report.get("deals", {})
    conflicts = section.get("conflict_groups", [])
    approved = section.get("approved_conflict_groups", [])
    skipped = section.get("skipped_groups", [])
    approved_ids = {g["winner_id"] for g in approved}
    skipped_ids = {g["winner_id"] for g in skipped}
    untriaged = [
        g for g in conflicts
        if g["winner_id"] not in approved_ids and g["winner_id"] not in skipped_ids
    ]
    if untriaged:
        raise SystemExit(
            f"deals: {len(untriaged)} conflict groups need review "
            f"(move each into approved_conflict_groups or skipped_groups)"
        )

    banner = "DRY RUN (pretend mode)" if pretend else "LIVE RUN"
    click.echo(f"\n{banner} — applying deal dedup report\n", err=True)

    attio = AttioClient() if not pretend else None

    deal_groups = list(section.get("auto_safe_groups", [])) + list(approved)

    totals = {"deals_groups": 0, "errors": 0}

    with open(log_path, "w") as fh:
        fh.write(json.dumps({
            "kind": "apply_run_start",
            "generated_at": report.get("generated_at"),
            "pretend": pretend,
            "object": "deals",
        }) + "\n")
        for g in deal_groups:
            res = apply_deal_group(attio, g, fh, pretend=pretend)
            fh.write(json.dumps({"kind": "group_result", **res}) + "\n")
            totals["deals_groups"] += 1
            totals["errors"] += len(res.get("errors", []))
        fh.write(json.dumps({"kind": "apply_run_end", **totals}) + "\n")

    return totals


def build_report(attio: AttioClient) -> dict:
    """Run the scan and build the dedup report."""
    click.echo("Fetching people…", err=True)
    people = attio.search_people(filter_=None, limit=50000)
    click.echo(f"  {len(people)} people records", err=True)

    click.echo("Fetching companies…", err=True)
    companies = attio.search_companies(filter_=None, limit=50000)
    click.echo(f"  {len(companies)} company records", err=True)

    click.echo("Fetching linkedin_outreach list entries…", err=True)
    entries = attio.query_list_entries(filter_=None, limit=50000)
    click.echo(f"  {len(entries)} list entries", err=True)

    entries_by_person: dict[str, list[dict]] = {}
    for e in entries:
        pid = entry_parent(e)
        if pid:
            entries_by_person.setdefault(pid, []).append(e)

    click.echo("Bucketing duplicates…", err=True)
    people_safe, people_conflicts = bucket_people(people, entries_by_person)
    co_safe, co_conflicts = bucket_companies(companies)

    return {
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "people": {
            "auto_safe_groups": people_safe,
            "conflict_groups": people_conflicts,
            "approved_conflict_groups": [],
            "skipped_groups": [],
        },
        "companies": {
            "auto_safe_groups": co_safe,
            "conflict_groups": co_conflicts,
            "approved_conflict_groups": [],
            "skipped_groups": [],
        },
        "summary": {
            "people_groups_auto_safe": len(people_safe),
            "people_groups_conflict": len(people_conflicts),
            "people_records_to_delete": sum(len(g["loser_ids"]) for g in people_safe),
            "companies_groups_auto_safe": len(co_safe),
            "companies_groups_conflict": len(co_conflicts),
            "companies_records_to_delete": sum(len(g["loser_ids"]) for g in co_safe),
        },
    }


# ── Apply: execute a triaged report ──────────────────────────


def check_freshness(report: dict, max_age_hours: int = 24) -> None:
    """Refuse to apply a report older than max_age_hours."""
    raw = report.get("generated_at", "")
    generated = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    age = datetime.now(UTC) - generated
    if age > timedelta(hours=max_age_hours):
        raise SystemExit(
            f"Report is {age} old ({raw}); refuse to apply because records "
            f"may have changed. Re-run scan to regenerate."
        )


def check_conflicts_triaged(report: dict) -> None:
    """Refuse to apply if any conflict group is neither approved nor skipped."""
    errors: list[str] = []
    for obj in ("people", "companies"):
        section = report[obj]
        conflicts = section.get("conflict_groups", [])
        approved = section.get("approved_conflict_groups", [])
        skipped = section.get("skipped_groups", [])
        approved_ids = {g["winner_id"] for g in approved}
        skipped_ids = {g["winner_id"] for g in skipped}
        untriaged = [
            g for g in conflicts
            if g["winner_id"] not in approved_ids and g["winner_id"] not in skipped_ids
        ]
        if untriaged:
            errors.append(
                f"{obj}: {len(untriaged)} conflict groups need review "
                f"(move each into approved_conflict_groups or skipped_groups)"
            )
    if errors:
        raise SystemExit("Conflict triage required:\n  - " + "\n  - ".join(errors))


def snapshot_record(attio: AttioClient, obj: str, record_id: str) -> dict | None:
    """Fetch a full record snapshot before deletion, for audit."""
    if obj == "people":
        return attio.get_person(record_id)
    return attio.get_company(record_id)


def apply_person_group(
    attio: AttioClient,
    group: dict,
    log_fh,
    pretend: bool,
) -> dict:
    """Merge a single person dedup group. Returns a result summary dict."""
    winner_id = group["winner_id"]
    result: dict = {"object": "people", "winner_id": winner_id, "actions": [], "errors": []}

    try:
        # 1. Merge non-empty fields forward onto winner
        merge = group.get("fields_to_merge", {})
        if merge:
            if pretend:
                result["actions"].append({"op": "update_person", "winner_id": winner_id, "fields": list(merge.keys()), "pretend": True})
            else:
                attio.update_person(winner_id, merge)
                result["actions"].append({"op": "update_person", "winner_id": winner_id, "fields": list(merge.keys())})

        # 2. Process list entries
        for action in group.get("list_entry_actions", []):
            t = action["type"]
            if t == "keep":
                continue
            if t == "reparent":
                raw_values = action.get("entry_values") or {}
                stage = action["stage"]
                if pretend:
                    result["actions"].append({"op": "reparent_entry", "entry_id": action["entry_id"], "pretend": True})
                else:
                    # Re-emit the loser's entry on the winner with proper types.
                    # Use extract_writable_value so selects/references preserve shape.
                    # `stage` is passed separately via stage_name; skip system fields.
                    skip = {"stage", "created_at", "created_by", "entry_id"}
                    clean: dict = {}
                    for key, vlist in raw_values.items():
                        if key in skip:
                            continue
                        val = extract_writable_value(vlist)
                        if val is not None:
                            clean[key] = val
                    attio.add_list_entry(
                        record_id=winner_id,
                        stage_name=stage,
                        entry_attributes=clean,
                    )
                    attio.delete_list_entry(action["entry_id"])
                    result["actions"].append({"op": "reparent_entry", "entry_id": action["entry_id"]})
            elif t == "delete":
                if pretend:
                    result["actions"].append({"op": "delete_entry", "entry_id": action["entry_id"], "pretend": True})
                else:
                    attio.delete_list_entry(action["entry_id"])
                    result["actions"].append({"op": "delete_entry", "entry_id": action["entry_id"]})

        # 3. Snapshot + delete each loser
        for lid in group["loser_ids"]:
            snap = None if pretend else snapshot_record(attio, "people", lid)
            if snap is not None:
                log_fh.write(json.dumps({"kind": "snapshot", "object": "people", "record_id": lid, "snapshot": snap}) + "\n")
            if pretend:
                result["actions"].append({"op": "delete_person", "record_id": lid, "pretend": True})
            else:
                attio.delete_person(lid)
                result["actions"].append({"op": "delete_person", "record_id": lid})
    except Exception as e:
        result["errors"].append(f"{type(e).__name__}: {e}")

    return result


def apply_company_group(
    attio: AttioClient,
    group: dict,
    log_fh,
    pretend: bool,
) -> dict:
    winner_id = group["winner_id"]
    result: dict = {"object": "companies", "winner_id": winner_id, "actions": [], "errors": []}

    try:
        merge = group.get("fields_to_merge", {})
        if merge:
            if pretend:
                result["actions"].append({"op": "update_company", "winner_id": winner_id, "fields": list(merge.keys()), "pretend": True})
            else:
                attio.update_company(winner_id, merge)
                result["actions"].append({"op": "update_company", "winner_id": winner_id, "fields": list(merge.keys())})

        for lid in group["loser_ids"]:
            snap = None if pretend else snapshot_record(attio, "companies", lid)
            if snap is not None:
                log_fh.write(json.dumps({"kind": "snapshot", "object": "companies", "record_id": lid, "snapshot": snap}) + "\n")
            if pretend:
                result["actions"].append({"op": "delete_company", "record_id": lid, "pretend": True})
            else:
                attio.delete_company(lid)
                result["actions"].append({"op": "delete_company", "record_id": lid})
    except Exception as e:
        result["errors"].append(f"{type(e).__name__}: {e}")

    return result


def apply_report(report: dict, pretend: bool = False, log_path: str = "dedup_apply_log.jsonl") -> dict:
    check_freshness(report)
    check_conflicts_triaged(report)

    banner = "DRY RUN (pretend mode)" if pretend else "LIVE RUN"
    click.echo(f"\n{banner} — applying dedup report\n", err=True)
    click.echo(
        "NOTE: groups apply sequentially but each group's steps (merge → "
        "entries → delete losers) are NOT wrapped in a transaction. If a "
        "step errors mid-group, later scan/apply cycles will re-detect the "
        "remaining losers; inspect dedup_apply_log.jsonl if in doubt.\n",
        err=True,
    )

    attio = AttioClient() if not pretend else None

    people_groups = list(report["people"].get("auto_safe_groups", [])) + list(report["people"].get("approved_conflict_groups", []))
    co_groups = list(report["companies"].get("auto_safe_groups", [])) + list(report["companies"].get("approved_conflict_groups", []))

    totals = {"people_groups": 0, "companies_groups": 0, "errors": 0}

    with open(log_path, "w") as fh:
        fh.write(json.dumps({"kind": "apply_run_start", "generated_at": report.get("generated_at"), "pretend": pretend}) + "\n")
        for g in people_groups:
            result = apply_person_group(attio, g, fh, pretend=pretend)
            fh.write(json.dumps({"kind": "group_result", **result}) + "\n")
            totals["people_groups"] += 1
            totals["errors"] += len(result.get("errors", []))
        for g in co_groups:
            result = apply_company_group(attio, g, fh, pretend=pretend)
            fh.write(json.dumps({"kind": "group_result", **result}) + "\n")
            totals["companies_groups"] += 1
            totals["errors"] += len(result.get("errors", []))
        fh.write(json.dumps({"kind": "apply_run_end", **totals}) + "\n")

    return totals


# ── PR-9.5 §3.11 Union-merge (triage-doc driven) ─────────────────────
#
# The `union-merge` subcommand reads the canonical 68-group triage
# (`tests/fixtures/attio-dedup-triage-2026-04-17.md`), applies the §3.11 union-merge
# rule on the 45 auto-mergeable groups, and opens `dedup_review`
# Operator Review Queue rows for the 23 REVIEW-shape groups.
#
# §3.11 union-merge rule (winner inherits, per attribute):
#   - dm_step                    → MAX
#   - last_contact_date          → MAX (ISO string)
#   - stage                      → highest by canonical STAGE_RANK
#   - dm1/2/3_sent_at, response_received_at → MAX-non-null
#   - experiment_id              → winner if non-null; else inherit single
#                                  non-null. MULTIPLE distinct non-nulls →
#                                  escalate dedup_experiment_id_conflict
#                                  and SKIP merge.
#   - response_classification    → most-pessimistic-wins
#                                  (defensive > negative > positive > null)
#   - suppress_re_engagement     → logical OR
#   - had_connection_note        → logical OR
#   - canonical_linkedin_url, vanity_url_slug → join key / impossible-conflict
#
# Losers get `merged_into=winner_record_id` (soft-delete; never physical).
#
# §9.4 idempotency contract: second run must be no-op. The script reads
# every loser's current `merged_into` value; if already non-null AND
# pointing to the expected winner, the loser is counted as
# `rows_skipped_idempotent` and not re-written.

_TRIAGE_DOC_PATH = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "attio-dedup-triage-2026-04-17.md"

# §3.11: "Auto-mergeable" = the `entry.persona`-only conflict shape (45
# of 68 groups). Every other shape (the seven `*REVIEW*`-tagged shapes
# in the triage doc, 23 groups total) opens a `dedup_review` queue row.
_AUTO_MERGEABLE_SHAPE: str = "entry.persona"

# Most-pessimistic-wins ranking for response_classification. Higher index
# wins. None ranks below all known values; unknown labels rank BELOW null
# (so an unrecognized string cannot silently overwrite a null winner).
# Keep the known-label ranks ≥ 1 so the `rank > best_rank=-1` (null) check
# only accepts known labels; unknowns get rank `-2` and never win.
_UNKNOWN_RESPONSE_CLASSIFICATION_RANK: int = -2
_RESPONSE_CLASSIFICATION_PESSIMISM: dict[str, int] = {
    "positive": 1,
    "negative": 2,
    "defensive": 3,
}


def parse_triage_doc(triage_path: Path = _TRIAGE_DOC_PATH) -> list[dict]:
    """Parse the 68-group triage doc into a structured list.

    Returns one dict per group:
      {
        "canonical_linkedin_url": "...",
        "winner_id": "...",
        "loser_ids": [...],
        "conflict_shape": "entry.persona" | "company+entry.persona" | ...,
        "auto_mergeable": True | False,
      }

    The triage doc is the SOURCE OF TRUTH for the 68 conflict groups
    (snapshot of state at 2026-04-17). The script does NOT re-scan
    Attio to discover groups — that path is the existing `scan` /
    `apply` subcommand and is unrelated to PR-9.5.
    """
    text = triage_path.read_text(encoding="utf-8")

    groups: list[dict] = []
    current_shape: str | None = None
    current_group: dict | None = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        # Shape header: "## Shape: `entry.persona` (45 groups)"
        m_shape = re.match(r"^##\s+Shape:\s+`([^`]+)`", line)
        if m_shape:
            current_shape = m_shape.group(1)
            continue
        # Group header: "### https://www.linkedin.com/in/..."
        m_url = re.match(r"^###\s+(\S+)$", line)
        if m_url:
            if current_group is not None:
                groups.append(current_group)
            current_group = {
                "canonical_linkedin_url": m_url.group(1),
                "winner_id": "",
                "loser_ids": [],
                "conflict_shape": current_shape or "unknown",
                "auto_mergeable": current_shape == _AUTO_MERGEABLE_SHAPE,
            }
            continue
        if current_group is None:
            continue
        # Field lines like "- **winner_id**: `...`"
        m_winner = re.match(r"^-\s+\*\*winner_id\*\*:\s+`([^`]+)`", line)
        if m_winner:
            current_group["winner_id"] = m_winner.group(1)
            continue
        m_losers = re.match(r"^-\s+\*\*loser_ids\*\*:\s+(.+)$", line)
        if m_losers:
            raw = m_losers.group(1)
            ids = [tok.strip(" `") for tok in raw.split(",")]
            current_group["loser_ids"] = [i for i in ids if i]
            continue

    if current_group is not None:
        groups.append(current_group)

    # Defensive consistency check — the triage doc is canonical, so any
    # group missing a winner_id is a parse bug that must be caught loud.
    for g in groups:
        if not g["winner_id"] or not g["loser_ids"]:
            raise ValueError(
                f"parse_triage_doc: malformed group for "
                f"{g['canonical_linkedin_url']}: winner_id={g['winner_id']!r}, "
                f"loser_ids={g['loser_ids']!r}"
            )
    return groups


def _max_iso(*values: Any) -> str | None:
    """Return the lexicographically-MAX (latest) of ISO-8601 timestamp
    or date strings. None / empty values are ignored. Returns None if
    no value is non-null.

    ISO-8601 strings sort lexicographically in the same order they sort
    chronologically (when consistent timezone suffix or no suffix), so
    string max == chronological max. Mixed-format inputs (date vs
    datetime) still produce a stable MAX because the date prefix is
    shared; this matches the §3.11 "MAX (most-recent)" rule for both
    `last_contact_date` (date) and `dmN_sent_at` (datetime).
    """
    candidates = [v for v in values if v is not None and v != ""]
    if not candidates:
        return None
    return max(str(c) for c in candidates)


def _pessimistic_response_classification(*values: Any) -> str | None:
    """§3.11 most-pessimistic-wins rule for response_classification.

    Pessimism order: defensive > negative > positive > null > unknown.
    Null/None ranks below known labels (`-1`); unknown labels rank below
    null (`_UNKNOWN_RESPONSE_CLASSIFICATION_RANK = -2`) so an
    unrecognized loser string CANNOT silently overwrite a null winner —
    if `winner=null` and `loser="neutral"`, the merge returns `None` and
    no write is emitted.

    The "defensive > negative" ordering is non-obvious: a defensive
    reply encodes "active objection" while a negative reply encodes
    "not interested for now" — defensive must never be weakened by a
    later negative because the §3.1 hard red line tracks defensive
    holds via DEFENSIVE_HOLD stage routing (§3.6).
    """
    best_rank = -1
    best_label: str | None = None
    for v in values:
        if v is None or v == "":
            continue
        s = str(v).strip().lower()
        rank = _RESPONSE_CLASSIFICATION_PESSIMISM.get(
            s, _UNKNOWN_RESPONSE_CLASSIFICATION_RANK
        )
        if rank > best_rank:
            best_rank = rank
            best_label = s
    return best_label


def _max_dm_step(*values: Any) -> str | None:
    """MAX across dm_step values. dm_step is a select-type slug
    ("dm0" | "dm1" | "dm2" | "dm3"). Strip whitespace, ignore None/empty,
    return None if no value is present.

    Sorting by the integer suffix is the right semantic — "dm10" would
    sort after "dm9" numerically — but no current dm_step exceeds dm3
    so the lexicographic `max("dm0", "dm1", ...)` agrees with numeric
    `max(0, 1, 2, 3)`. Keep numeric extraction for explicit safety as
    a §3.1 defense: if a future schema adds dm10, lexicographic max
    would silently regress and a loser's dm3 could win over a winner's
    dm10, re-opening a closed cadence.
    """
    candidates: list[tuple[int, str]] = []
    for v in values:
        if v is None or v == "":
            continue
        s = str(v).strip()
        m = re.match(r"^dm(\d+)$", s, re.IGNORECASE)
        if m:
            candidates.append((int(m.group(1)), s.lower()))
        else:
            # Unknown shape — sort to the back; preserve original for forensics.
            candidates.append((-1, s))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0], reverse=True)
    return candidates[0][1]


def _max_stage_by_rank(*values: Any) -> str | None:
    """Return the stage label with the highest canonical STAGE_RANK.

    `values` are stage labels in their Attio surface form
    ("Prospect" | "Connection Sent" | ...). Uses
    `models.pipeline.stage_rank` so the rank table stays the F-PR-1
    canonical source. Unknown stage strings rank as -1 (sort to back).
    """
    # Lazy import to keep the module import-fast for tools that only
    # use the existing scan/apply path.
    from models.pipeline import PipelineStage, stage_rank

    best_rank = -2
    best_label: str | None = None
    for v in values:
        if v is None or v == "":
            continue
        label = str(v).strip()
        try:
            r = stage_rank(label)
        except (ValueError, KeyError):
            # Try matching the enum value form (e.g., the script may
            # see legacy strings); fall through to -1 on failure.
            r = -1
            for stg in PipelineStage:
                if stg.value.lower() == label.lower():
                    r = stage_rank(stg)
                    break
        if r > best_rank:
            best_rank = r
            best_label = label
    return best_label


def _any_truthy(*values: Any) -> bool | None:
    """Logical-OR across nullable-boolean values. Returns True if any
    value is truthy; False if all observed values are explicitly False;
    None if no value is non-null.

    §3.11: `suppress_re_engagement` / `had_connection_note` are OR-merged
    because suppression must never weaken. The None return distinguishes
    "no row carries any value" from "every row says False" so the dedup
    can skip an OR-merge write when there's nothing to merge.
    """
    saw_value = False
    for v in values:
        if v is None:
            continue
        saw_value = True
        if v is True or (isinstance(v, str) and v.lower() in ("true", "1", "yes")):
            return True
    if saw_value:
        return False
    return None


# §3.1 frozen_at confidence ordering — lower index = higher confidence.
# When the union-merge inherits a single experiment_id from multiple
# duplicates with different frozen_at values, the highest-confidence
# label wins so a legacy_pure_unknown stamp can't override a prospect-
# committed one.
_FROZEN_AT_CONFIDENCE: tuple[str, ...] = (
    "prospect",
    "connection_sent",
    "accepted",
    "legacy_pre_tsv_era",
    "legacy_inferred_by_archaeology",
    "legacy_pure_unknown",
)


def _experiment_id_conflict(*pairs: tuple[Any, Any]) -> tuple[str | None, str | None, list[str]]:
    """Inspect (experiment_id, frozen_at) pairs across duplicates.

    Returns (chosen_id, chosen_frozen_at, distinct_non_null_ids).

    - If 0 non-null ids → (None, None, []).
    - If 1 distinct non-null id (multiple rows may share it) →
      (id, highest-confidence frozen_at observed, [id]).
    - If 2+ distinct non-null ids → caller must escalate
      `dedup_experiment_id_conflict` and skip merge. Returns
      (None, None, distinct_ids) to signal the conflict.

    The frozen_at returned matches the chosen experiment_id (immutable
    pair per §3.1's enum). If the single non-null id appears with
    multiple frozen_at values (e.g., winner has `legacy_pure_unknown`
    and a loser has `prospect`), the highest-confidence frozen_at per
    `_FROZEN_AT_CONFIDENCE` wins — a `prospect`-committed stamp must
    never be weakened by a `legacy_*` archaeology stamp during union-
    merge. Cross-§3.1 + §3.10 protection.
    """
    seen_pairs: list[tuple[str, str | None]] = []
    for eid, frozen_at in pairs:
        if eid is None or eid == "":
            continue
        seen_pairs.append((str(eid), str(frozen_at) if frozen_at is not None else None))

    if not seen_pairs:
        return (None, None, [])

    distinct_ids: list[str] = []
    for eid, _frozen in seen_pairs:
        if eid not in distinct_ids:
            distinct_ids.append(eid)

    if len(distinct_ids) > 1:
        return (None, None, distinct_ids)

    chosen_id = seen_pairs[0][0]
    # Pick the highest-confidence (lowest index) frozen_at across the
    # duplicates sharing this single experiment_id. None ranks last.
    #
    # Edge case (partial-stamp): if every duplicate sharing this
    # `chosen_id` has `frozen_at=None`, the loop below leaves
    # `chosen_frozen` at None even though `chosen_id` is non-null. That
    # is intentional — the §3.1 enum requires (id, frozen_at) to be set
    # together; the caller (compute_union_merge_attrs) emits both fields
    # in the same delta only if both are non-null, preserving the
    # immutable-pair invariant.
    best_idx = len(_FROZEN_AT_CONFIDENCE)
    chosen_frozen: str | None = None
    for _eid, frozen in seen_pairs:
        if frozen is None:
            continue
        try:
            idx = _FROZEN_AT_CONFIDENCE.index(frozen)
        except ValueError:
            idx = len(_FROZEN_AT_CONFIDENCE)  # unknown labels rank last
        if idx < best_idx:
            best_idx = idx
            chosen_frozen = frozen
    return (chosen_id, chosen_frozen, distinct_ids)


def compute_union_merge_attrs(winner_attrs: dict, loser_attrs_list: list[dict]) -> dict:
    """Compute the union-merge delta to apply to the winner per §3.11.

    Returns a dict of attribute → new value. Only attributes whose
    union-merge result DIFFERS from the winner's current value are
    included (idempotency: re-applying the same merge twice is a no-op).

    `winner_attrs` and each item in `loser_attrs_list` are
    `AttioClient.parse_entry(entry)` outputs (flat dict).

    If `experiment_id` carries multiple distinct non-null values,
    `_experiment_id_conflict` returns an empty `chosen_id` AND a
    populated `distinct_ids`. The caller is responsible for inspecting
    the returned `_experiment_id_conflict_ids` flag (added to the
    returned delta as a sentinel) before applying; if non-empty, the
    group must escalate instead of merging.
    """
    delta: dict[str, Any] = {}

    all_attrs = [winner_attrs] + list(loser_attrs_list)

    # --- MAX-style attrs ---
    new_dm_step = _max_dm_step(*[a.get("dm_step") for a in all_attrs])
    if new_dm_step is not None and new_dm_step != (winner_attrs.get("dm_step") or None):
        delta["dm_step"] = new_dm_step

    new_lcd = _max_iso(*[a.get("last_contact_date") for a in all_attrs])
    if new_lcd is not None and new_lcd != (winner_attrs.get("last_contact_date") or None):
        delta["last_contact_date"] = new_lcd

    for attr in ("dm1_sent_at", "dm2_sent_at", "dm3_sent_at", "response_received_at"):
        new_val = _max_iso(*[a.get(attr) for a in all_attrs])
        if new_val is not None and new_val != (winner_attrs.get(attr) or None):
            delta[attr] = new_val

    new_stage = _max_stage_by_rank(*[a.get("stage") for a in all_attrs])
    if new_stage is not None and new_stage != (winner_attrs.get("stage") or None):
        delta["stage"] = new_stage

    # --- experiment_id (with conflict escalation flag) ---
    pairs = [(a.get("experiment_id"), a.get("experiment_id_frozen_at")) for a in all_attrs]
    chosen_eid, chosen_frozen, distinct_ids = _experiment_id_conflict(*pairs)
    if len(distinct_ids) > 1:
        # Caller must escalate; sentinel flag in returned delta.
        delta["_experiment_id_conflict_ids"] = distinct_ids
    else:
        if chosen_eid is not None and chosen_eid != (winner_attrs.get("experiment_id") or None):
            delta["experiment_id"] = chosen_eid
            if chosen_frozen is not None and chosen_frozen != (winner_attrs.get("experiment_id_frozen_at") or None):
                delta["experiment_id_frozen_at"] = chosen_frozen

    # --- response_classification: most-pessimistic-wins ---
    new_rc = _pessimistic_response_classification(*[a.get("response_classification") for a in all_attrs])
    if new_rc is not None and new_rc != (winner_attrs.get("response_classification") or None):
        delta["response_classification"] = new_rc

    # --- logical-OR attrs (suppress_re_engagement, had_connection_note) ---
    new_suppress = _any_truthy(*[a.get("suppress_re_engagement") for a in all_attrs])
    if new_suppress is True and not winner_attrs.get("suppress_re_engagement"):
        delta["suppress_re_engagement"] = True

    new_hcn = _any_truthy(*[a.get("had_connection_note") for a in all_attrs])
    if new_hcn is True and not winner_attrs.get("had_connection_note"):
        delta["had_connection_note"] = True

    return delta


def _entry_for_record(
    attio: AttioClient, record_id: str, list_id: str
) -> dict | None:
    """Fetch the single most-advanced list entry for `record_id`.

    Wraps `_find_list_entries_for_record` (which orders by STAGE_RANK
    desc, oldest-created-at tiebreak). Returns None if no entry exists
    for the record in this list — the record was either never on the
    list or was already physically deleted by a prior cleanup. The
    union-merge skips losers with no entry (nothing to soft-delete).
    """
    entries = attio._find_list_entries_for_record(record_id, list_id)
    return entries[0] if entries else None


def apply_union_merge_group(
    attio: AttioClient,
    group: dict,
    list_id: str,
    writer,  # MigrationRunWriter
    log_fh,
    pretend: bool,
) -> dict:
    """Apply §3.11 union-merge for a single auto-mergeable group.

    Returns a result dict capturing winner/loser actions + any
    escalation. Does NOT raise on per-group failure — captures the
    error in result["errors"] and writes a `mark_failed` counter so the
    Migration Run row reflects partial failures honestly.

    Idempotency: for each loser, if `merged_into` is already non-null
    AND pointing at the expected winner, the loser is counted as
    `rows_skipped_idempotent` and NOT re-written. The winner's
    union-merged delta is only PATCH'd when at least one attribute
    actually differs (already enforced by `compute_union_merge_attrs`).
    """
    winner_id = group["winner_id"]
    loser_ids = group["loser_ids"]
    canonical_url = group["canonical_linkedin_url"]
    result: dict[str, Any] = {
        "kind": "union_merge_group",
        "canonical_linkedin_url": canonical_url,
        "winner_id": winner_id,
        "loser_ids": loser_ids,
        "actions": [],
        "errors": [],
    }

    try:
        # Count the winner as examined too — the Migration Run row's
        # rows_examined reflects EVERY record touched by the script
        # (winners + losers), per §3.13's "examined" semantics.
        writer.examine()
        winner_entry = _entry_for_record(attio, winner_id, list_id)
        if winner_entry is None:
            # Winner has no list entry — cannot merge. Operator escalation.
            result["errors"].append(
                f"winner_id={winner_id} has no entry in list={list_id}; cannot merge"
            )
            writer.mark_failed(record_id=winner_id, error="winner_entry_missing")
            for lid in loser_ids:
                writer.examine()
                writer.mark_failed(record_id=lid, error="winner_entry_missing")
            return result

        winner_attrs = AttioClient.parse_entry(winner_entry)
        winner_entry_id = winner_attrs.get("entry_id", "")

        loser_attrs_list: list[dict] = []
        # Map loser_id → list[entry_id, parsed_attrs] for the per-loser
        # write step. A single person record can have multiple list
        # entries (legacy duplicates within the list); ALL of them get
        # merged_into stamped to keep the cross-channel suppression
        # chain intact.
        loser_entries_by_record: dict[str, list[dict]] = {}
        for lid in loser_ids:
            writer.examine()
            entries = attio._find_list_entries_for_record(lid, list_id)
            loser_entries_by_record[lid] = entries
            if not entries:
                # No entry to soft-delete on this list — count as skipped
                # rather than failed; the record may live elsewhere.
                writer.skip_idempotent()
                result["actions"].append({
                    "op": "skip_loser_no_entry",
                    "loser_id": lid,
                })
                continue
            for ent in entries:
                loser_attrs_list.append(AttioClient.parse_entry(ent))

        delta = compute_union_merge_attrs(winner_attrs, loser_attrs_list)

        # --- experiment_id conflict: escalate, do NOT auto-merge ---
        conflict_ids = delta.pop("_experiment_id_conflict_ids", None)
        if conflict_ids:
            if not pretend:
                escalate(
                    type="dedup_experiment_id_conflict",
                    idempotency_key=f"dedup-eidconflict-{canonical_url}",
                    payload={
                        "canonical_linkedin_url": canonical_url,
                        "winner_id": winner_id,
                        "loser_ids": loser_ids,
                        "experiment_ids": list(conflict_ids),
                        "conflict_shape": group.get("conflict_shape", "unknown"),
                    },
                    attio=attio,
                )
            result["actions"].append({
                "op": "escalate_experiment_id_conflict",
                "experiment_ids": list(conflict_ids),
                "pretend": pretend,
            })
            # Counters: don't mark anything modified — the group is parked.
            # Losers stay un-merged. Re-running after operator resolution
            # picks the group up again.
            return result

        # --- Apply winner delta (only if non-empty) ---
        if delta:
            if pretend:
                result["actions"].append({
                    "op": "patch_winner_entry",
                    "entry_id": winner_entry_id,
                    "delta": delta,
                    "pretend": True,
                })
            else:
                attio.update_list_entry(
                    entry_id=winner_entry_id,
                    entry_attributes=delta,
                    list_id=list_id,
                )
                writer.mark_modified(record_id=winner_id)
                result["actions"].append({
                    "op": "patch_winner_entry",
                    "entry_id": winner_entry_id,
                    "delta": delta,
                })
        else:
            # Winner already carries the union — counts as examined,
            # not modified. Don't double-count `examine()` for the
            # winner; winner.examine() folds into the loser loop's
            # accounting below.
            result["actions"].append({"op": "winner_already_unionized"})

        # --- Mark each loser's entry with merged_into=winner_id ---
        winner_ref = {
            "target_object": "people",
            "target_record_id": winner_id,
        }
        for lid in loser_ids:
            entries = loser_entries_by_record.get(lid, [])
            if not entries:
                continue  # already accounted for above
            for ent in entries:
                ent_attrs = AttioClient.parse_entry(ent)
                ent_id = ent_attrs.get("entry_id", "")
                existing_mi = ent_attrs.get("merged_into")
                # Idempotency: skip writes when already pointing at winner.
                if existing_mi == winner_id or (
                    isinstance(existing_mi, dict)
                    and existing_mi.get("target_record_id") == winner_id
                ):
                    writer.skip_idempotent()
                    result["actions"].append({
                        "op": "skip_loser_already_merged",
                        "loser_id": lid,
                        "entry_id": ent_id,
                    })
                    continue
                if pretend:
                    result["actions"].append({
                        "op": "stamp_merged_into",
                        "loser_id": lid,
                        "entry_id": ent_id,
                        "merged_into": winner_id,
                        "pretend": True,
                    })
                else:
                    attio.update_list_entry(
                        entry_id=ent_id,
                        entry_attributes={"merged_into": winner_ref},
                        list_id=list_id,
                    )
                    writer.mark_modified(record_id=lid)
                    result["actions"].append({
                        "op": "stamp_merged_into",
                        "loser_id": lid,
                        "entry_id": ent_id,
                        "merged_into": winner_id,
                    })

    except _TRANSIENT_ATTIO_ERRORS as exc:
        # Narrow catch: HTTP / network errors are transient and per-group;
        # surface, count, and continue with the next group. Code bugs
        # (TypeError / KeyError / ImportError / AttributeError / ...) are
        # NOT caught here — they propagate and halt the run loudly so the
        # operator sees the real failure mode (silent-failure CRIT-1).
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        writer.mark_failed(record_id=winner_id, error=exc)

    # silent-failure HIGH-4: the per-group JSONL record is the audit trail
    # for what got merged. If the write fails (disk full, permissions,
    # closed handle) we must surface that — a missing audit line is a §3.1
    # red line because operators cannot reconstruct the merge after-the-fact.
    try:
        log_fh.write(json.dumps(result, default=str) + "\n")
    except OSError as exc:
        result["errors"].append(f"log_write_failed: {type(exc).__name__}: {exc}")
        sys.stderr.write(
            f"[attio_dedup] WARNING: failed to write union_merge_group "
            f"audit line for winner_id={winner_id}: {exc}\n"
        )
    return result


def open_dedup_review_for_group(
    attio: AttioClient | None, group: dict, pretend: bool
) -> dict:
    """Open a `dedup_review` Operator Review Queue row for a REVIEW-shape
    conflict group. Returns the queue row dict (or a pretend-stub).

    `attio` is Optional only to support `pretend=True` (where no live
    client is needed). In the live path (`pretend=False`) `attio` MUST be
    non-None — the assertion below converts the implicit contract into a
    loud failure rather than a downstream AttributeError.

    Idempotency: `escalate(type='dedup_review', idempotency_key=...)`
    is the single source of dedup-review uniqueness. Re-running the
    union-merge replays the same idempotency keys → no duplicate rows.
    """
    canonical_url = group["canonical_linkedin_url"]
    record_ids = [group["winner_id"]] + list(group["loser_ids"])
    payload = {
        "canonical_linkedin_url": canonical_url,
        "record_ids": record_ids,
        "conflict_shape": group["conflict_shape"],
        "auto_mergeable": False,
    }
    if pretend:
        return {"pretend": True, "type": "dedup_review", "payload": payload}
    assert attio is not None, (
        "open_dedup_review_for_group(pretend=False) requires a live AttioClient"
    )
    return escalate(
        type="dedup_review",
        idempotency_key=f"dedup-review-{canonical_url}",
        payload=payload,
        attio=attio,
    )


def run_union_merge(
    triage_path: Path = _TRIAGE_DOC_PATH,
    *,
    list_id: str | None = None,
    pretend: bool = False,
    log_path: str = "dedup_union_merge_log.jsonl",
    attio: AttioClient | None = None,
) -> dict:
    """Top-level orchestrator for the §3.11 union-merge.

    Steps:
      1. Parse the canonical 68-group triage doc.
      2. Open a MigrationRunWriter context (writes one Migration Run row).
      3. For each REVIEW-shape group: open one `dedup_review` queue row.
      4. For each AUTO-shape group: compute union-merge delta and apply.
      5. Counters: rows_examined / rows_modified / rows_skipped_idempotent
         / rows_failed feed the Migration Run row.

    `pretend=True` exercises every code path without calling Attio
    write APIs. Used for the verification dry-run.
    """
    # Lazy imports — keeps the module's existing import surface unchanged
    # for tests/tools that don't use the union-merge path.
    from workflows.migration_run_writer import MigrationRunWriter

    groups = parse_triage_doc(triage_path)
    if not groups:
        raise ValueError(f"parse_triage_doc returned no groups from {triage_path}")

    lid = list_id or os.environ.get("ATTIO_LIST_ID", "")
    if not lid and not pretend:
        raise RuntimeError(
            "ATTIO_LIST_ID env var (or --list-id) is required for live apply. "
            "Use --pretend to exercise without Attio."
        )

    if attio is None and not pretend:
        attio = AttioClient()

    auto_groups = [g for g in groups if g["auto_mergeable"]]
    review_groups = [g for g in groups if not g["auto_mergeable"]]

    totals: dict[str, Any] = {
        "groups_total": len(groups),
        "auto_groups_total": len(auto_groups),
        "review_groups_total": len(review_groups),
        "auto_groups_applied": 0,
        "auto_groups_skipped": 0,
        "review_queue_rows_opened": 0,
        "experiment_id_conflict_escalations": 0,
        "errors": 0,
    }

    with open(log_path, "w", encoding="utf-8") as log_fh:
        log_fh.write(json.dumps({
            "kind": "union_merge_run_start",
            "started_at": datetime.now(UTC).isoformat(),
            "pretend": pretend,
            "groups_total": len(groups),
            "auto_groups_total": len(auto_groups),
            "review_groups_total": len(review_groups),
        }) + "\n")

        with MigrationRunWriter(
            script_name="scripts.attio_dedup.union_merge",
            rollback_script_path=None,  # union-merge is irreversible;
            # losers carry merged_into and snapshots are in the JSONL log.
            dry_run=pretend,
            attio=attio,
            audit_log_path=log_path,
        ) as writer:
            # 1. REVIEW groups → queue rows
            for g in review_groups:
                try:
                    open_dedup_review_for_group(attio, g, pretend=pretend)
                    totals["review_queue_rows_opened"] += 1
                    log_fh.write(json.dumps({
                        "kind": "dedup_review_opened",
                        "canonical_linkedin_url": g["canonical_linkedin_url"],
                        "conflict_shape": g["conflict_shape"],
                        "record_ids": [g["winner_id"]] + g["loser_ids"],
                    }, default=str) + "\n")
                except _TRANSIENT_ATTIO_ERRORS as exc:
                    # silent-failure CRIT-3: narrow to transient errors only.
                    # Code bugs (KeyError on group dict, ImportError on
                    # escalate, etc.) must propagate so the operator sees
                    # them — not get silently logged as a per-row failure.
                    totals["errors"] += 1
                    log_fh.write(json.dumps({
                        "kind": "dedup_review_failed",
                        "canonical_linkedin_url": g["canonical_linkedin_url"],
                        "error": f"{type(exc).__name__}: {exc}",
                    }, default=str) + "\n")

            # silent-failure CRIT-3 (part b): the operator MUST be notified
            # when any REVIEW group failed to open its queue row. A silently-
            # missed escalation row is a §3.1 red line — the conflict group
            # never gets human attention.
            if totals["review_queue_rows_opened"] < len(review_groups):
                missed = len(review_groups) - totals["review_queue_rows_opened"]
                sys.stderr.write(
                    f"[attio_dedup] FATAL: {missed} of "
                    f"{len(review_groups)} REVIEW groups failed to open a "
                    f"dedup_review queue row. See {log_path} for per-group "
                    f"`dedup_review_failed` records.\n"
                )
                sys.exit(1)

            # 2. AUTO groups → §3.11 union-merge
            for g in auto_groups:
                if attio is None and pretend:
                    # In pretend with no live Attio, we can still walk the
                    # triage logic + escalation paths but cannot fetch
                    # entries. Skip the Attio-touch step and record.
                    log_fh.write(json.dumps({
                        "kind": "auto_group_skipped_pretend_no_attio",
                        "canonical_linkedin_url": g["canonical_linkedin_url"],
                    }) + "\n")
                    totals["auto_groups_skipped"] += 1
                    continue
                result = apply_union_merge_group(
                    attio,
                    g,
                    list_id=lid,
                    writer=writer,
                    log_fh=log_fh,
                    pretend=pretend,
                )
                if result["errors"]:
                    totals["errors"] += len(result["errors"])
                if any(a.get("op") == "escalate_experiment_id_conflict"
                       for a in result["actions"]):
                    totals["experiment_id_conflict_escalations"] += 1
                    totals["auto_groups_skipped"] += 1
                else:
                    totals["auto_groups_applied"] += 1

            # Bubble the writer counters into the JSONL summary so
            # operators reading the log see them without opening Attio.
            log_fh.write(json.dumps({
                "kind": "migration_run_counters",
                "run_id": writer.run_id,
                "rows_examined": writer.rows_examined,
                "rows_modified": writer.rows_modified,
                "rows_skipped_idempotent": writer.rows_skipped_idempotent,
                "rows_failed": writer.rows_failed,
            }) + "\n")
            totals["migration_run_id"] = writer.run_id
            totals["rows_examined"] = writer.rows_examined
            totals["rows_modified"] = writer.rows_modified
            totals["rows_skipped_idempotent"] = writer.rows_skipped_idempotent
            totals["rows_failed"] = writer.rows_failed

            # silent-failure HIGH-5: write the end sentinel INSIDE the
            # MigrationRunWriter `with` block so a failure in __exit__
            # cannot strand the JSONL without a closing record. The audit
            # trail's "did the run finish?" gate is this sentinel — a
            # missing sentinel means partial run, no matter how it ended.
            try:
                log_fh.write(json.dumps({
                    "kind": "union_merge_run_end",
                    "ended_at": datetime.now(UTC).isoformat(),
                    **{k: v for k, v in totals.items() if k != "migration_run_id"},
                }, default=str) + "\n")
                log_fh.flush()
            except OSError as exc:
                # Don't swallow — operator needs to know the audit trail
                # is incomplete. Set a flag in totals so callers can see it.
                totals["errors"] += 1
                totals["run_end_sentinel_failed"] = (
                    f"{type(exc).__name__}: {exc}"
                )
                sys.stderr.write(
                    f"[attio_dedup] WARNING: failed to write "
                    f"union_merge_run_end sentinel: {exc}\n"
                )

    return totals


# ── CLI ──────────────────────────────────────────────────────


@click.group()
def cli() -> None:
    """Attio dedup tooling."""


@cli.command()
def scan() -> None:
    """Scan Attio and emit dedup_report.json to stdout."""
    attio = AttioClient()
    report = build_report(attio)
    click.echo(json.dumps(report, indent=2, default=str))
    s = report["summary"]
    click.echo(
        f"\nSummary: people={s['people_groups_auto_safe']} auto-safe + "
        f"{s['people_groups_conflict']} conflict → {s['people_records_to_delete']} to delete; "
        f"companies={s['companies_groups_auto_safe']} auto-safe + "
        f"{s['companies_groups_conflict']} conflict → {s['companies_records_to_delete']} to delete",
        err=True,
    )


@cli.command()
@click.option("--report", "report_path", required=True, type=click.Path(exists=True, dir_okay=False), help="Path to dedup_report.json")
@click.option("--pretend", is_flag=True, help="Exercise code paths without calling Attio write APIs")
@click.option("--log", "log_path", default="dedup_apply_log.jsonl", help="Path for the apply log (JSONL)")
def apply(report_path: str, pretend: bool, log_path: str) -> None:
    """Apply a triaged dedup_report.json. Writes an audit log."""
    report = json.loads(Path(report_path).read_text())
    totals = apply_report(report, pretend=pretend, log_path=log_path)
    click.echo(
        f"Applied: {totals['people_groups']} people groups, "
        f"{totals['companies_groups']} company groups, "
        f"{totals['errors']} errors → log: {log_path}",
        err=True,
    )
    if totals["errors"]:
        sys.exit(1)


@cli.command("union-merge")
@click.option(
    "--triage-doc",
    "triage_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=str(_TRIAGE_DOC_PATH),
    show_default=True,
    help="Canonical 68-group triage doc (PR-9.5 source of truth).",
)
@click.option(
    "--list-id",
    "list_id",
    default=None,
    help="Attio list_id for linkedin_outreach (defaults to $ATTIO_LIST_ID).",
)
@click.option(
    "--pretend",
    is_flag=True,
    help="Walk the union-merge logic without calling Attio write APIs.",
)
@click.option(
    "--log",
    "log_path",
    default="dedup_union_merge_log.jsonl",
    show_default=True,
    help="Per-group audit log (JSONL).",
)
def union_merge(triage_path: Path, list_id: str | None, pretend: bool, log_path: str) -> None:
    """PR-9.5 §3.11 union-merge of the 68-group dedup triage.

    Reads `tests/fixtures/attio-dedup-triage-2026-04-17.md` and applies the
    §3.11 union-merge rule on the 45 auto-mergeable groups; opens 23
    `dedup_review` Operator Review Queue rows for the REVIEW-shape
    groups; escalates `dedup_experiment_id_conflict` on any group with
    multiple distinct non-null experiment_id values.

    Idempotent: re-running is a no-op (losers with `merged_into`
    pointing at the winner are skipped, `dedup_review` rows dedupe on
    idempotency key).
    """
    totals = run_union_merge(
        triage_path=triage_path,
        list_id=list_id,
        pretend=pretend,
        log_path=log_path,
    )
    click.echo(
        f"Union-merge: {totals['groups_total']} groups "
        f"({totals['auto_groups_total']} auto, {totals['review_groups_total']} review). "
        f"applied={totals['auto_groups_applied']} "
        f"experiment_id_conflicts={totals['experiment_id_conflict_escalations']} "
        f"review_queue_opened={totals['review_queue_rows_opened']} "
        f"errors={totals['errors']} "
        f"rows_examined={totals.get('rows_examined', 0)} "
        f"rows_modified={totals.get('rows_modified', 0)} "
        f"rows_skipped_idempotent={totals.get('rows_skipped_idempotent', 0)} "
        f"rows_failed={totals.get('rows_failed', 0)} "
        f"log={log_path}",
        err=True,
    )
    if totals["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    cli()
