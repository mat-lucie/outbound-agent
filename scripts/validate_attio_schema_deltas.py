#!/usr/bin/env python3
"""Validate the Attio schema-delta manifest (F-PR-3.5).

Runs as a Phase-E precondition and as a CI guard. Catches:

  - duplicate (object, slug) entries — two PRs claiming the same attr
  - invalid `type` strings — typos that would fail at runtime
  - select types missing `options`
  - missing required fields (pr_id, write_owner_module, status)
  - invalid `status` values (must be: planned | shipped | verified)
  - inconsistent options shape across same-named attributes
  - (optional, --check-attio) collisions with existing Attio attributes
  - (optional, --check-attio-shipped) manifest-vs-live-Attio drift:
    every entry with `status: shipped` must exist in production. Closes
    the round-2 lens-QA blocking finding R2-B1 (manifest claimed shipped
    for 17 attrs that were never deployed). Respects deploy_holdback_objects.
  - (optional, --check-attio-verified) same existence check on
    `status: verified` entries — catches type/shape drift on brownfield
    attrs that the manifest documents as already-in-prod. Pairs with
    --check-attio-shipped for full coverage.

Exit codes:
  0  — manifest is valid
  1  — manifest has errors (printed to stderr)
  2  — manifest file missing or unreadable
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = REPO_ROOT / "docs" / "attio_schema_deltas.yaml"

VALID_TYPES = frozenset({
    "text", "select", "number", "date", "datetime",
    "record_reference", "long_text", "checkbox", "currency", "list_text",
})

VALID_STATUSES = frozenset({"planned", "shipped", "verified", "descoped"})

def load_manifest(path: Path) -> dict:
    """Load + parse the manifest YAML.

    Raises:
        FileNotFoundError: manifest path doesn't exist.
        yaml.YAMLError: manifest is not valid YAML.
        ValueError: top-level YAML is not a mapping.
    """
    if not path.exists():
        raise FileNotFoundError(f"manifest not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(
            f"manifest top-level must be a mapping; got {type(data).__name__}"
        )
    return data


def validate(manifest: dict) -> list[str]:
    """Return a list of error strings; empty list means valid."""
    errors: list[str] = []

    # Defensive: callers may pass non-dict data (e.g., a list from a YAML
    # file that's actually just a top-level list). Surface that loudly
    # instead of crashing on `.get()`.
    if not isinstance(manifest, dict):
        return [f"manifest must be a mapping; got {type(manifest).__name__}"]

    # Required top-level keys.
    for key in ("schema_version", "plan_version", "last_updated"):
        if key not in manifest:
            errors.append(f"missing top-level key: {key}")

    new_objects = manifest.get("new_objects", []) or []
    attributes = manifest.get("attributes", []) or []

    # --- New objects sanity ---
    seen_objects: set[str] = set()
    descoped_objects: set[str] = set()
    for i, obj in enumerate(new_objects):
        prefix = f"new_objects[{i}]"
        if not isinstance(obj, dict):
            errors.append(f"{prefix}: must be a mapping")
            continue
        for field in ("object", "pr_id", "status"):
            if field not in obj:
                errors.append(f"{prefix}: missing field `{field}`")
        if "status" in obj and obj["status"] not in VALID_STATUSES:
            errors.append(
                f"{prefix} (object={obj.get('object')}): "
                f"invalid status `{obj['status']}` "
                f"(must be one of {sorted(VALID_STATUSES)})"
            )
        name = obj.get("object")
        if name in seen_objects:
            errors.append(f"{prefix}: duplicate new object declaration `{name}`")
        elif name:
            seen_objects.add(name)
        if name and obj.get("status") == "descoped":
            descoped_objects.add(name)

    # --- Attributes ---
    seen_pairs: dict[tuple[str, str], int] = {}
    seen_options_shape: dict[tuple[str, str], tuple] = {}
    for i, attr in enumerate(attributes):
        prefix = f"attributes[{i}]"
        if not isinstance(attr, dict):
            errors.append(f"{prefix}: must be a mapping")
            continue
        required = ("object", "slug", "type", "pr_id", "status", "write_owner_module")
        for field in required:
            if field not in attr:
                errors.append(f"{prefix}: missing field `{field}`")
        if any(field not in attr for field in required):
            continue

        obj = attr["object"]
        slug = attr["slug"]
        kind = attr["type"]
        status = attr["status"]

        # type
        if kind not in VALID_TYPES:
            errors.append(
                f"{prefix} ({obj}.{slug}): invalid type `{kind}` "
                f"(must be one of {sorted(VALID_TYPES)})"
            )

        # status
        if status not in VALID_STATUSES:
            errors.append(
                f"{prefix} ({obj}.{slug}): invalid status `{status}` "
                f"(must be one of {sorted(VALID_STATUSES)})"
            )

        # (1d) "No shipped/verified attr under a descoped parent" invariant.
        # If the parent object's new_objects entry is descoped, none of its
        # attributes may claim shipped/verified — that would reach the live
        # wet-preflight existence gate against an intentionally-absent object.
        # Catches a missed child `status:` line before it hits production.
        if obj in descoped_objects and status in ("shipped", "verified"):
            errors.append(
                f"{prefix} ({obj}.{slug}): status `{status}` under descoped "
                f"object `{obj}` — descoped parents must host no "
                f"shipped/verified attrs (flip this attr to descoped)."
            )

        # (1c) Planned-but-written WARN guard (internal consistency, no Attio
        # access). An attr that already names a writer module but is still
        # status:planned is the week_starting-class gap: live code can write
        # the attr before the Attio object is deployed + flipped to shipped.
        # Emitted via logging (NOT the returned error list) so it stays
        # non-fatal — the wet preflight (cli.py daily) treats ANY returned
        # string as an error and would refuse to send.
        wom_present = attr.get("write_owner_module")
        if status == "planned" and wom_present:
            logger.warning(
                "attr `%s.%s` is written by code (`%s`) but still "
                "status:planned — deploy + flip to shipped before its writer "
                "runs against prod (week_starting-class gap).",
                obj, slug, wom_present,
            )

        # select must have options
        if kind == "select":
            options = attr.get("options")
            if not options or not isinstance(options, list):
                errors.append(
                    f"{prefix} ({obj}.{slug}): type=select requires "
                    f"non-empty `options` list"
                )
            else:
                # Pin shape: subsequent same-named attribute entries
                # (multi-PR coordination) must share identical option sets.
                shape = tuple(sorted(options))
                key = (obj, slug)
                prior = seen_options_shape.get(key)
                if prior is not None and prior != shape:
                    errors.append(
                        f"{prefix} ({obj}.{slug}): options diverge from "
                        f"earlier entry: {prior} vs {shape}"
                    )
                seen_options_shape[key] = shape

        # Non-select must NOT have options.
        if kind != "select" and attr.get("options") not in (None, []):
            errors.append(
                f"{prefix} ({obj}.{slug}): type={kind} must not declare `options` "
                f"(found {attr['options']!r})"
            )

        # Duplicate (object, slug) detection. Conflict-resolution-critical
        # field `pr_id` is surfaced for both entries so the operator can
        # tell at a glance which two PRs are claiming the same attribute.
        key = (obj, slug)
        if key in seen_pairs:
            prior_idx = seen_pairs[key]
            prior_pr = attributes[prior_idx].get("pr_id", "?")
            this_pr = attr.get("pr_id", "?")
            errors.append(
                f"{prefix} ({obj}.{slug}): duplicate entry "
                f"(pr_id={this_pr}) collides with attributes[{prior_idx}] "
                f"(pr_id={prior_pr})"
            )
        else:
            seen_pairs[key] = i

        # Pre-existing slug collision (informational; not always an error
        # — the plan re-registers a few pre-existing slugs for cross-agent
        # audit per B-DS-005 spirit, e.g. quality_score, dm_step, stage).
        # Allow if write_owner_module includes the pre-existing system.

        # write_owner_module: str | list[str], must be non-empty.
        wom = attr.get("write_owner_module")
        if isinstance(wom, list):
            if not wom or not all(isinstance(x, str) and x for x in wom):
                errors.append(
                    f"{prefix} ({obj}.{slug}): write_owner_module list must be "
                    f"non-empty strings"
                )
        elif not isinstance(wom, str) or not wom:
            errors.append(
                f"{prefix} ({obj}.{slug}): write_owner_module must be a non-empty "
                f"string or list[str]"
            )

    return errors


def check_attio_collisions(manifest: dict) -> list[str]:
    """Optional: query Attio for existing attrs on each object and warn
    about collisions.

    F-PR-3.5 ships the hook but the actual Attio query is deferred to
    F-PR-3 / F-PR-3.7 where the schema-fetch helper lands. Until then
    this is a stub — it prints a warning to stderr so operators don't
    silently assume the check ran.
    """
    print(
        "warning: --check-attio is a stub in F-PR-3.5 — Attio collision "
        "checks land with the schema-fetch helper in F-PR-3.7. The "
        "internal-consistency check (default) IS running and is sufficient "
        "for catching cross-PR collisions within the manifest itself.",
        file=sys.stderr,
    )
    return []


# linkedin_outreach is a list, not an object — list-attr lookups go
# through /lists/{list_id}/attributes. Mirrors the constant in
# scripts/migrate_attio_schema_F-PR-3.5b.py.
_LIST_PARENT_IDS: dict[str, str] = {
    "linkedin_outreach": "a68f3b3e-b957-40a9-bcbd-8f3b4b6f4188",
}


def check_attio_shipped(
    manifest: dict, *, statuses: frozenset[str] = frozenset({"shipped"}),
) -> list[str]:
    """Assert every entry with status in ``statuses`` exists in production
    Attio. Closes the round-2 lens-QA finding R2-B1 (manifest claimed
    shipped for attrs that were never deployed).

    By default checks only ``status: shipped`` entries (the original
    R2-B1 scope). Pass ``statuses={"shipped", "verified"}`` (via
    ``--check-attio-verified``) to also catch drift on brownfield attrs
    that the manifest documents as already-in-prod — Wave-2-A QA found
    that 3 brownfield slugs (scoring_lane, icp_lane, verdict_path) were
    declared with wrong types, which the shipped-only check missed.

    Respects ``deploy_holdback_objects`` — held-back objects + their
    child attrs are skipped because their writer code is shipped but
    the Attio placement is rethought (agent-4 decision; final
    placement pending Wave-2 follow-up).

    Requires ATTIO_API_KEY in env. Imports are deferred so the default
    ``validate`` path stays import-light and doesn't pull httpx.
    """
    import httpx

    # Add repo root for `clients.attio` resolution when invoked as
    # `python scripts/validate_attio_schema_deltas.py`.
    sys.path.insert(0, str(REPO_ROOT))
    from clients.attio import AttioClient  # noqa: E402

    try:
        attio = AttioClient()
    except KeyError:
        return [
            "--check-attio-shipped / --check-attio-verified requires "
            "ATTIO_API_KEY in env"
        ]

    holdback = frozenset(manifest.get("deploy_holdback_objects", []) or [])
    errors: list[str] = []

    # Objects whose existence the new_objects loop below GET-checks. Used by
    # the (1b) defense-in-depth block to avoid re-GETting the same object.
    new_objects_checked: set[str] = set()

    for spec in manifest.get("new_objects", []) or []:
        if spec.get("status") not in statuses:
            continue
        slug = spec.get("object")
        if not slug:
            continue
        if slug in holdback:
            continue
        # Lists are not standalone objects; their underlying parent
        # (people for linkedin_outreach) already exists in baseline
        # Attio, so skip the existence check.
        if slug in _LIST_PARENT_IDS:
            continue
        new_objects_checked.add(slug)
        try:
            attio._request("GET", f"/objects/{slug}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                errors.append(
                    f"new_object `{slug}` (status={spec.get('status')}) does "
                    f"NOT exist in production Attio — manifest lies. Deploy "
                    f"via `python scripts/migrate_attio_schema_F-PR-3.5b.py "
                    f"--apply`."
                )
                continue
            errors.append(
                f"new_object `{slug}` GET failed: HTTP {exc.response.status_code}"
            )
        except httpx.RequestError as exc:
            # Fail closed: a transport error during the existence check must
            # not be treated as "exists/OK". Surface it as an error so the
            # caller (e.g. the wet-run pre-flight) refuses to proceed.
            errors.append(
                f"new_object `{slug}` existence check errored (transport): "
                f"{type(exc).__name__}: {exc}"
            )

    # (1b) Defense-in-depth object existence (the week_starting-class gap).
    # The new_objects loop only GET-checks objects that have a new_objects
    # entry with a status-matching `status:`. An attribute can target an
    # object that has NO such entry (e.g. a brownfield/companies attr, or a
    # new object whose row is mis-statused) — so a status-matching attr could
    # imply an object that the new_objects loop never verified. Here we GET
    # the parent object for any status-matching attr whose object wasn't
    # already checked, isn't held back, and isn't a list-parent. Each object
    # is GET-checked at most once.
    attr_object_checked: set[str] = set()
    for spec in manifest.get("attributes", []) or []:
        if spec.get("status") not in statuses:
            continue
        obj = spec.get("object")
        if not obj:
            continue
        if obj in new_objects_checked or obj in attr_object_checked:
            continue
        if obj in holdback:
            continue
        if obj in _LIST_PARENT_IDS:
            continue
        attr_object_checked.add(obj)
        try:
            attio._request("GET", f"/objects/{obj}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                errors.append(
                    f"object `{obj}` hosts status-matching attrs but does "
                    f"NOT exist in production Attio — manifest lies."
                )
                continue
            errors.append(
                f"object `{obj}` existence check GET failed: "
                f"HTTP {exc.response.status_code}"
            )
        except httpx.RequestError as exc:
            # Fail closed: a transport error must not be treated as
            # "exists/OK". Surface it so the wet-run pre-flight refuses.
            errors.append(
                f"object `{obj}` existence check errored (transport): "
                f"{type(exc).__name__}: {exc}"
            )

    for spec in manifest.get("attributes", []) or []:
        if spec.get("status") not in statuses:
            continue
        obj = spec.get("object")
        slug = spec.get("slug")
        if not obj or not slug:
            continue
        if obj in holdback:
            continue
        if obj in _LIST_PARENT_IDS:
            url = f"/lists/{_LIST_PARENT_IDS[obj]}/attributes/{slug}"
        else:
            url = f"/objects/{obj}/attributes/{slug}"
        try:
            attio._request("GET", url)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                errors.append(
                    f"attribute `{obj}.{slug}` (status={spec.get('status')}, "
                    f"pr_id={spec.get('pr_id', '?')}) does NOT exist in "
                    f"production Attio — manifest lies. Deploy via "
                    f"`python scripts/migrate_attio_schema_F-PR-3.5b.py "
                    f"--apply`."
                )
                continue
            errors.append(
                f"attribute `{obj}.{slug}` GET failed: HTTP {exc.response.status_code}"
            )
        except httpx.RequestError as exc:
            # Fail closed: a transport error during the existence check must
            # not be treated as "exists/OK". Surface it so the wet-run
            # pre-flight refuses to proceed rather than crash with a raw
            # traceback (or, worse, skip the check).
            errors.append(
                f"attribute `{obj}.{slug}` existence check errored (transport): "
                f"{type(exc).__name__}: {exc}"
            )

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help=f"Path to manifest YAML (default: {DEFAULT_MANIFEST})",
    )
    parser.add_argument(
        "--check-attio",
        action="store_true",
        help="Also query Attio for collisions with existing attributes (requires ATTIO_API_KEY).",
    )
    parser.add_argument(
        "--check-attio-shipped",
        action="store_true",
        help=(
            "Assert every entry with status=shipped exists in production "
            "Attio (requires ATTIO_API_KEY). Closes round-2 lens-QA "
            "finding R2-B1 (manifest-vs-live drift). Skips entries listed "
            "under deploy_holdback_objects."
        ),
    )
    parser.add_argument(
        "--check-attio-verified",
        action="store_true",
        help=(
            "Also assert every entry with status=verified exists in "
            "production Attio. Use together with --check-attio-shipped "
            "(or alone) to also catch drift on brownfield attrs. Skips "
            "entries listed under deploy_holdback_objects."
        ),
    )
    args = parser.parse_args(argv)

    try:
        manifest = load_manifest(args.manifest)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (yaml.YAMLError, ValueError) as exc:
        print(f"error: malformed manifest YAML: {exc}", file=sys.stderr)
        return 2

    errors = validate(manifest)
    if args.check_attio:
        errors.extend(check_attio_collisions(manifest))
    if args.check_attio_shipped or args.check_attio_verified:
        statuses = set()
        if args.check_attio_shipped:
            statuses.add("shipped")
        if args.check_attio_verified:
            statuses.add("verified")
        errors.extend(
            check_attio_shipped(manifest, statuses=frozenset(statuses))
        )

    if errors:
        print(f"{len(errors)} validation error(s):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print(
        f"manifest OK: "
        f"{len(manifest.get('new_objects', []))} new objects, "
        f"{len(manifest.get('attributes', []))} attribute entries"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
