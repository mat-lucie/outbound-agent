#!/usr/bin/env python3
"""Record a verified per-person LinkedIn language on the CRM `people.language`.

SCOPE — LinkedIn DMs and connection notes only. The email campaign
(`workflows/email_campaign.py`) derives its language independently from
company domain + country and does NOT read this attribute, so setting it
here does not change what that person receives by email.

Why this exists: company HQ country seeds the entry `language`
(scripts/backfill_language.py) and is the only signal the send-time guard
can corroborate against. That key is WRONG for LATAM-based staff of
non-LATAM multinationals — a director who writes an entirely Portuguese
profile at a group whose true HQ is in Europe resolves to EN under every
HQ-derived inference.

`people.language` outranks every company-derived inference in
`models.resolution` (person override > company HQ > lane default), so a
value recorded here survives the company HQ-country backfill instead of
being contradicted by it.

NARROW EXCEPTION LIST: this attribute stays EMPTY for almost everyone. Set
it only where HQ or profile location gets the language wrong — one person
at a time, after a human checked that person. An empty override is not a
data gap; it means "the inferred language was never contradicted". Do not
build a bulk backfill on top of this script.

# Usage

    python3 scripts/set_person_language.py --dry-run \
        --person <record_id> --language pt --note "PT profile, EU-HQ parent"
    python3 scripts/set_person_language.py --apply \
        --person <record_id> --language pt --note "PT profile, EU-HQ parent"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from clients.attio import AttioClient  # noqa: E402
from models.resolution import coerce_language  # noqa: E402
from workflows.migration_run_writer import MigrationRunWriter  # noqa: E402

SCRIPT_VERSION = "person-language-v1"

def _apply_one(
    attio: AttioClient,
    mrun,
    person_id: str,
    language: str,
    label: str,
    *,
    dry_run: bool,
) -> None:
    """Set one person's language override, verifying the write landed.

    Idempotent by natural filter: a person who already carries an override
    is skipped, never overwritten — a human set that value deliberately and
    this script must not silently disagree with them. That guarantee needs
    a read that can FAIL loudly, not one that fails open; see below.
    """
    mrun.examine()

    # Read the record DIRECTLY rather than through person_language_override:
    # that getter cannot raise (fail-open by contract), so a transient error
    # would read as "no override" and this function would blind-overwrite a
    # value another human set deliberately — printing a success line for it.
    # get_person DOES raise, so a failed probe aborts the row instead.
    try:
        record = attio.get_person(person_id)
    except Exception as exc:  # noqa: BLE001 — report and skip, never guess
        print(f"  ✗ {label}: could not read current value ({type(exc).__name__}: {exc}).")
        mrun.mark_failed(record_id=person_id, error=exc)
        return
    if record is None:
        print(f"  ✗ {label}: person record not found.")
        mrun.mark_failed(record_id=person_id, error="person record not found")
        return

    current = AttioClient._extract_person_language(record.get("values", {}))
    if current:
        if current == language:
            print(f"  = {label}: already {current!r}.")
            mrun.skip_idempotent()
        else:
            # A DIFFERENT existing override is a real conflict, not a
            # no-op: two humans disagree about this person. Fail the row
            # loudly rather than picking a winner.
            print(
                f"  ✗ {label}: already carries {current!r}, refusing to "
                f"overwrite with {language!r}. Resolve by hand in the CRM."
            )
            mrun.mark_failed(
                record_id=person_id,
                error=f"conflicting override {current!r} != {language!r}",
            )
        return

    if dry_run:
        print(f"  DRY {label}: would set language = {language!r}.")
        mrun.mark_modified(record_id=person_id, object="people")
        return

    try:
        attio.update_person(person_id, {"language": language})
    except (httpx.HTTPStatusError, httpx.RequestError, httpx.TimeoutException) as exc:
        print(f"  ✗ {label}: write failed ({type(exc).__name__}: {exc}).")
        mrun.mark_failed(record_id=person_id, error=exc)
        return

    # Verify through the SAME getter the send path reads, so a write that
    # lands in an unexpected shape fails loud here instead of silently
    # doing nothing on the next run.
    attio.invalidate_person_language(person_id)
    readback = attio.person_language_override(person_id)
    if readback != language:
        print(
            f"  ✗ {label}: wrote {language!r} but read back {readback!r} — "
            f"write did not land in the shape the resolver reads."
        )
        mrun.mark_failed(
            record_id=person_id,
            error=f"verify readback {readback!r} != {language!r}",
        )
        return

    print(f"  ✓ {label}: language = {language}")
    mrun.mark_modified(record_id=person_id, object="people")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Preview only")
    mode.add_argument("--apply", action="store_true", help="Write to the CRM")
    parser.add_argument(
        "--person", required=True,
        help="Person record_id whose language override to set.",
    )
    parser.add_argument(
        "--language", required=True,
        help="Language code for --person (es/en/pt)",
    )
    parser.add_argument("--note", default="", help="Free-text label for the console line")
    args = parser.parse_args(argv)

    # Validate through the resolver's own coercion so this script can never
    # write a value the send path would silently ignore — a workspace select
    # may offer codes the copy library has no templates for.
    if coerce_language(args.language) is None:
        print(
            f"error: language {args.language!r} is not one of es/en/pt "
            f"(the resolver would ignore it)",
            file=sys.stderr,
        )
        return 2
    roster = [(args.person, args.language.strip().lower(), args.note or args.person)]

    try:
        attio = AttioClient()
    except KeyError:
        print("error: ATTIO_API_KEY env var not set", file=sys.stderr)
        return 2

    print(
        f"Person language override "
        f"({'dry-run' if args.dry_run else 'apply'}) — {len(roster)} person(s)."
    )

    mig_writer = MigrationRunWriter(
        script_name=Path(__file__).name,
        script_version=SCRIPT_VERSION,
        rollback_script_path=None,  # values are hand-verified; correct a bad
        # one by clearing the attribute on the person record in the CRM
        dry_run=args.dry_run,
        attio=attio,
    )

    with mig_writer as mrun:
        for person_id, language, label in roster:
            _apply_one(
                attio, mrun, person_id, language, label, dry_run=args.dry_run,
            )

    print(
        f"\nSet {mrun.rows_modified} · already-set "
        f"{mrun.rows_skipped_idempotent} · failed {mrun.rows_failed}"
    )
    return 1 if mrun.rows_failed else 0


if __name__ == "__main__":
    sys.exit(main())
