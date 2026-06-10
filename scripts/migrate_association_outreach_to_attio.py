"""PR-40 — Migrate ``content/association_outreach.json`` into Attio.

Promotes the historic association-outreach contact list (file-based) into
the Person-side ``outreach_channel`` Attio attribute so the §3.1
cross-channel suppression rule consults the same source of truth across
DM, email, and association-outreach paths.

Wraps every PATCH in a ``MigrationRunWriter`` row per plan §3.13. On a
successful run (no failures) the JSON file is renamed to
``association_outreach.json.deprecated`` so a subsequent run is a no-op
and the file's continued existence does not silently shadow the Attio
record of truth.

Usage:
    python3 scripts/migrate_association_outreach_to_attio.py            # live
    python3 scripts/migrate_association_outreach_to_attio.py --dry-run  # no PATCH, no rename
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import click
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent))

from clients.attio import AttioClient  # noqa: E402
from clients.attio_writer import AttioWriter  # noqa: E402
from workflows.cross_channel_suppression import (  # noqa: E402
    OUTREACH_CHANNEL_ASSOCIATION,
    stamp_outreach_channel,
)
from workflows.migration_run_writer import MigrationRunWriter  # noqa: E402

CONTACTS_FILE = Path(__file__).parent.parent / "content" / "association_outreach.json"
DEPRECATED_SUFFIX = ".deprecated"
SCRIPT_NAME = "scripts/migrate_association_outreach_to_attio.py"
ROLLBACK_SCRIPT_PATH = "scripts/rollback_association_outreach_to_attio.py"  # to ship if rolling back is ever needed


def _git_sha(override: str | None = None) -> str:
    """Return short HEAD SHA. Raises when no SHA is resolvable.

    Per §0 #9 fail-loud — provenance on the Migration Run row matters,
    and a silent ``"unknown"`` stamp would lose the audit trail on every
    CI / out-of-checkout run. The ``override`` kwarg covers the legitimate
    "running outside a git checkout" path (CI containers, ops one-offs).
    """
    if override is not None:
        return override
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent,
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()[:12]
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        raise RuntimeError(
            "Cannot determine git HEAD — run from a git checkout or pass "
            "--script-version=<sha> explicitly. Migration Run rows MUST "
            "carry a real provenance stamp."
        ) from exc


def _load_contacts(path: Path = CONTACTS_FILE) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    return data.get("contacts", []) or []


def _resolve_person(attio: AttioClient, contact: dict) -> dict | None:
    """Find the Attio Person record for an association contact.

    Looks up by ``email`` first (most reliable), then ``linkedin``.
    Returns the raw record dict or None when no Person matches —
    association contacts are sometimes seeded ahead of LinkedIn
    enrichment, so a None resolution is an expected case, not a failure.
    """
    email = contact.get("email")
    if email:
        people = attio.search_people(filter_={"email_addresses": email}, limit=1)
        if people:
            return people[0]

    linkedin = contact.get("linkedin")
    if linkedin:
        result = attio.search_person_by_linkedin(linkedin)
        if result:
            return result
    return None


def _person_outreach_channels(record: dict) -> list[str]:
    """Extract the existing outreach_channel select values from a Person."""
    values = record.get("values") or {}
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


def migrate_one(
    contact: dict,
    *,
    attio: AttioClient,
    writer: AttioWriter | None,
    run: MigrationRunWriter,
    dry_run: bool,
) -> str:
    """Migrate one contact. Returns an outcome tag for the operator summary:

      ``patched``           — outreach_channel updated
      ``skipped_idempotent``— Person already has association_outreach in
                              outreach_channel
      ``skipped_no_match``  — no Person record found for this contact
      ``would_patch``       — dry-run would have written
      ``failed``            — Attio write raised; mark_failed recorded;
                              caller decides whether to halt or continue
    """
    run.examine()
    record = _resolve_person(attio, contact)
    if record is None:
        run.skip_idempotent()  # treat un-resolvable contacts as skip
        return "skipped_no_match"

    existing = _person_outreach_channels(record)
    if OUTREACH_CHANNEL_ASSOCIATION in existing:
        run.skip_idempotent()
        return "skipped_idempotent"

    record_id = (record.get("id") or {}).get("record_id", "")
    if dry_run or writer is None:
        return "would_patch"

    try:
        stamp_outreach_channel(
            writer,
            attio,
            person_record_id=record_id,
            channel=OUTREACH_CHANNEL_ASSOCIATION,
            writer_module=SCRIPT_NAME.replace("/", ".").removesuffix(".py"),
        )
        run.mark_modified(record_id=record_id)
        return "patched"
    except Exception as exc:
        # Return the "failed" tag instead of re-raising so the outer
        # rename-gate can observe real failure counts. The
        # MigrationRunWriter row still carries mark_failed for forensic
        # tracing.
        run.mark_failed(record_id=record_id, error=exc)
        return "failed"


UNMATCHED_RESIDUAL_FILE = CONTACTS_FILE.with_name("association_outreach.unmatched.json")


@click.command()
@click.option("--dry-run", is_flag=True, default=False, help="No PATCH, no JSON rename.")
@click.option(
    "--script-version",
    default=None,
    help="Override the git SHA stamped on the Migration Run row (for CI / out-of-checkout runs).",
)
def main(dry_run: bool, script_version: str | None) -> int:
    contacts = _load_contacts()
    click.echo(f"Loaded {len(contacts)} contact(s) from {CONTACTS_FILE}")

    if not contacts:
        click.echo("No contacts to migrate; renaming JSON to .deprecated.")
        if not dry_run:
            _deprecate_file(CONTACTS_FILE)
        sys.exit(0)

    outcomes: dict[str, int] = {}
    unmatched: list[dict] = []
    with AttioClient() as attio:
        writer = AttioWriter(attio=attio) if not dry_run else None
        with MigrationRunWriter(
            script_name=SCRIPT_NAME,
            script_version=_git_sha(script_version),
            rollback_script_path=ROLLBACK_SCRIPT_PATH,
            dry_run=dry_run,
            attio=attio,
        ) as run:
            for contact in contacts:
                tag = migrate_one(
                    contact, attio=attio, writer=writer, run=run, dry_run=dry_run,
                )
                outcomes[tag] = outcomes.get(tag, 0) + 1
                if tag == "skipped_no_match":
                    unmatched.append({
                        "email": contact.get("email"),
                        "linkedin": contact.get("linkedin"),
                        "name": contact.get("name"),
                    })

        click.echo("Migration outcomes:")
        for tag, n in sorted(outcomes.items()):
            click.echo(f"  {tag}: {n}")

        failed = outcomes.get("failed", 0)
        no_match = outcomes.get("skipped_no_match", 0)

        # Persist unmatched contacts BEFORE renaming the source — these
        # need to be seeded in Attio manually (or a follow-up sweep) so
        # the §3.1 cross-channel red line stays intact for prospects
        # added to Attio after migration day.
        if unmatched and not dry_run:
            UNMATCHED_RESIDUAL_FILE.write_text(
                json.dumps({"_meta": {
                    "purpose": "Contacts in association_outreach.json that could not be resolved to an Attio Person at migration time. Seed these in Attio + re-run stamp_outreach_channel for each before the residual file is removed.",
                    "source_file": str(CONTACTS_FILE),
                }, "contacts": unmatched}, indent=2),
                encoding="utf-8",
            )
            click.echo(
                f"WROTE residual file: {UNMATCHED_RESIDUAL_FILE.name} "
                f"({len(unmatched)} unmatched contact(s)). Seed these in "
                f"Attio + run stamp_outreach_channel before removing.",
                err=True,
            )

        # Rename gate: only when there were ZERO failures AND zero
        # unmatched (otherwise the unmatched contacts would silently
        # leak into other channels). dry-run never renames.
        if not dry_run and failed == 0 and no_match == 0:
            _deprecate_file(CONTACTS_FILE)
            click.echo(f"Renamed {CONTACTS_FILE.name} → {CONTACTS_FILE.name}{DEPRECATED_SUFFIX}")
        elif not dry_run and (failed > 0 or no_match > 0):
            click.echo(
                f"NOT renaming source JSON: failed={failed}, no_match={no_match}. "
                f"Resolve and re-run.",
                err=True,
            )

    sys.exit(1 if failed > 0 else 0)


def _deprecate_file(path: Path) -> Path:
    """Rename the file to ``<name>.deprecated``. Idempotent if already renamed."""
    target = path.with_suffix(path.suffix + DEPRECATED_SUFFIX)
    if target.exists() and not path.exists():
        return target  # already renamed
    path.rename(target)
    return target


if __name__ == "__main__":
    main()
