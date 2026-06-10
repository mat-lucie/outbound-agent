"""DM-writer schema preflight (2026-06-09 desync-invariant design §3).

A single undeployed attribute 400s EVERY list-entry write in a run
while the company tallies keep landing (the week_starting class,
PR #146) — N person/company desyncs in one run. Verify the DM path's
writer attributes against the live Attio schema BEFORE any send and
abort the run when one is missing.

Fail-closed: a failure fetching the schema itself propagates and
aborts the run — an unverifiable schema is treated like a missing
attribute (the operator reruns once Attio is reachable).
"""

from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from clients.attio import AttioClient
    from workflows.audit import AuditLogger

# Attrs written by the DM path. Entry set must mirror
# the attrs_to_update literal in run_dm_sequencing; company set must
# mirror _write_company_throttle_tally (§3.15 registry, PR-13).
DM_ENTRY_WRITER_ATTRS = frozenset(
    {"dm_step", "stage", "last_contact_date", "next_eligible_send_date"}
)
DM_COMPANY_WRITER_ATTRS = frozenset(
    {
        "last_outreach_at",
        "last_outreach_person_id",
        "last_outreach_step",
        "last_outreach_experiment_id",
    }
)


class DmWriterSchemaPreflightError(click.ClickException):
    """A DM-path writer attribute is missing from the live schema."""


def assert_dm_writer_schema(
    attio: "AttioClient",
    list_id: str,
    *,
    audit_logger: "AuditLogger | None" = None,
) -> None:
    """Raise DmWriterSchemaPreflightError if any DM-path writer
    attribute is undeployed. Read-only; safe in dry-run."""
    if not list_id:
        raise DmWriterSchemaPreflightError(
            "ATTIO_LIST_ID is not set — cannot verify the DM writer "
            "schema. Set it and re-run."
        )
    live_entry = {
        a.get("api_slug") for a in attio.get_list_attributes(list_id)
    }
    live_company = {
        a.get("api_slug") for a in attio.get_object_attributes("companies")
    }
    missing_entry = sorted(DM_ENTRY_WRITER_ATTRS - live_entry)
    missing_company = sorted(DM_COMPANY_WRITER_ATTRS - live_company)
    if not missing_entry and not missing_company:
        return
    if audit_logger is not None:
        audit_logger.event(
            "dm_writer_schema_preflight_failed",
            missing_list_entry_attrs=missing_entry,
            missing_company_attrs=missing_company,
        )
    raise DmWriterSchemaPreflightError(
        "DM writer schema preflight failed — undeployed attribute(s): "
        f"list entry {missing_entry or 'none'}, "
        f"companies {missing_company or 'none'}. Aborting before any "
        "send (week_starting class: one missing attr desyncs every row "
        "in the run). Deploy the attribute(s) in Attio, then re-run."
    )
