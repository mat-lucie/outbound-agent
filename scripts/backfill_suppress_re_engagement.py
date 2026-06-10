#!/usr/bin/env python3
"""PR-37 value backfill: populate suppress_re_engagement=True for prospects
who have ever signalled "no" through any channel.

Per plan §3.16 (and §3.1 hardest red line), every list entry where ANY of:

  * ``response_classification`` ∈ {negative, defensive}
  * ``stage`` ∈ {NOT_INTERESTED, DEFENSIVE_HOLD}

must carry ``suppress_re_engagement=True``.  The runtime suppression gate in
``workflows.wave2_blast`` (and future PR-40 ``association_outreach``) consults
``workflows.cross_channel_suppression.is_suppressed`` which already checks all
three signals — but persisting the flag is what makes the §3.11 dedup
union-merge effective (a winner who never said no inherits the loser's
suppression flag via logical OR).

# Run-provenance contract

Wraps in BOTH writers, per the two §3.13 and §3.16 mandates working in
concert:

* ``MigrationRunWriter`` — §3.13: every ``scripts/backfill_*.py`` MUST use it.
  Tracks rows_examined / rows_modified / rows_skipped_idempotent / rows_failed
  on the Migration Run object so the backfill is replayable and auditable.

* ``ReclassificationRunWriter`` — §3.16: this PR-37 backfill MUST emit a
  Reclassification Run row whose ``abstain_count`` captures the
  manual-reply-unscored gap.  An entry with ``response_classification is None``
  AND ``stage`` not in SUPPRESSED_STAGES means we have no signal to act on —
  these are recorded as abstains so PR-43.5's data-quality report can alarm on
  the unscored-manual-reply pool.

# Idempotency contract (§9.4)

Second consecutive run: entries already at True read ``prior_suppress=True``
and ``apply_suppression`` returns False (skip).  Resulting Migration Run row:
``rows_modified=0, rows_skipped_idempotent=N, rows_failed=0``.

# Usage

    python3 scripts/backfill_suppress_re_engagement.py [--dry-run] [--verbose]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clients.attio import AttioClient  # noqa: E402
from clients.attio_writer import AttioWriter  # noqa: E402
from workflows.cross_channel_suppression import (  # noqa: E402
    LINKEDIN_OUTREACH_LIST_ID_ENV,
    NEGATIVE_RESPONSE_CLASSIFICATIONS,
    SUPPRESSED_STAGES,
    apply_suppression,
    is_suppressed,
)
from workflows.migration_run_writer import MigrationRunWriter  # noqa: E402
from workflows.reclassification_run_writer import ReclassificationRunWriter  # noqa: E402

SCRIPT_VERSION = "pr-37-v1"
WRITER_MODULE = "scripts.backfill_suppress_re_engagement"


def _get_list_id() -> str:
    list_id = os.environ.get(LINKEDIN_OUTREACH_LIST_ID_ENV, "").strip()
    if list_id:
        return list_id
    raise RuntimeError(
        f"LinkedIn Outreach list ID not set. "
        f"Export {LINKEDIN_OUTREACH_LIST_ID_ENV}=<uuid> and retry."
    )


def _is_manual_reply_unscored(attrs: dict) -> bool:
    """§3.16 abstain criterion: response_classification is unset AND stage isn't
    in SUPPRESSED_STAGES. These are rows we cannot suppress without further
    classification — captured as abstains so the data-quality report can alarm.
    """
    if attrs.get("response_classification") is not None:
        return False
    return attrs.get("stage") not in SUPPRESSED_STAGES


def _suppression_reason(attrs: dict) -> str:
    """Human-readable reason string for log lines and abstain_details."""
    reasons: list[str] = []
    if attrs.get("response_classification") in NEGATIVE_RESPONSE_CLASSIFICATIONS:
        reasons.append(f"response_classification={attrs['response_classification']!r}")
    if attrs.get("stage") in SUPPRESSED_STAGES:
        reasons.append(f"stage={attrs['stage']!r}")
    if attrs.get("suppress_re_engagement"):
        reasons.append("suppress_re_engagement=True(prior)")
    return ",".join(reasons) or "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    try:
        attio = AttioClient()
    except KeyError:
        print("error: ATTIO_API_KEY env var not set", file=sys.stderr)
        return 2

    try:
        list_id = _get_list_id()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        f"PR-37 suppress_re_engagement backfill "
        f"({'dry-run' if args.dry_run else 'live'}) — fetching all entries..."
    )

    all_entries = attio.query_list_entries(list_id=list_id, limit=100_000)
    print(f"  Fetched {len(all_entries)} linkedin_outreach entries.")

    if not all_entries:
        print("  No entries found — nothing to backfill.")
        return 0

    writer = AttioWriter(attio=attio)
    already_set_count = 0

    # Construct both writers before entering so we can thread the
    # reclassification run_id into the migration row for forensics join.
    rec_writer = ReclassificationRunWriter(
        classifier_module=WRITER_MODULE,
        classifier_version=SCRIPT_VERSION,
        model_used="rule_based",
        input_attr="response_classification+stage",
        output_attr="suppress_re_engagement",
        dry_run=args.dry_run,
        attio=attio,
    )
    mig_writer = MigrationRunWriter(
        script_name=Path(__file__).name,
        script_version=SCRIPT_VERSION,
        rollback_script_path=None,  # boolean flip is effectively irreversible
        dry_run=args.dry_run,
        attio=attio,
        reclassification_run_id=rec_writer.run_id,
    )

    with rec_writer as crun, mig_writer as mrun:
        for entry in all_entries:
            mrun.examine()
            crun.examine()

            attrs = AttioClient.parse_entry(entry)
            entry_id = attrs.get("entry_id", "")
            record_id = attrs.get("record_id", "")

            if not entry_id:
                mrun.mark_failed(
                    record_id=record_id or "unknown",
                    error="missing entry_id",
                )
                if args.verbose:
                    print("  WARN: entry missing entry_id, skipping.", file=sys.stderr)
                continue

            if not is_suppressed(attrs):
                if _is_manual_reply_unscored(attrs):
                    crun.mark_abstain(
                        record_id=record_id or entry_id,
                        reason="manual_reply_unscored",
                    )
                mrun.skip_idempotent()
                continue

            prior_suppress = attrs.get("suppress_re_engagement")
            if prior_suppress is True:
                already_set_count += 1
                mrun.skip_idempotent()
                crun.mark_success(
                    record_id=record_id or entry_id,
                    object="schema",
                    value=True,
                )
                if args.verbose:
                    print(
                        f"  SKIP entry_id={entry_id}: already True "
                        f"({_suppression_reason(attrs)})"
                    )
                continue

            if args.dry_run:
                mrun.mark_modified(record_id=entry_id, object="schema")
                crun.mark_success(
                    record_id=record_id or entry_id,
                    object="schema",
                    value=True,
                )
                if args.verbose or mrun.rows_modified <= 10:
                    print(
                        f"  [dry-run] WOULD SET entry_id={entry_id}: "
                        f"suppress_re_engagement=True "
                        f"({_suppression_reason(attrs)})"
                    )
                continue

            try:
                wrote = apply_suppression(
                    writer,
                    entry_id=entry_id,
                    list_id=list_id,
                    prior_suppress=prior_suppress,
                )
                if wrote:
                    mrun.mark_modified(record_id=entry_id, object="schema")
                    crun.mark_success(
                        record_id=record_id or entry_id,
                        object="schema",
                        value=True,
                    )
                    if args.verbose:
                        print(
                            f"  SET entry_id={entry_id}: "
                            f"suppress_re_engagement=True "
                            f"({_suppression_reason(attrs)})"
                        )
                else:
                    already_set_count += 1
                    mrun.skip_idempotent()
            except Exception as exc:
                mrun.mark_failed(record_id=entry_id, error=exc)
                crun.mark_error(record_id=record_id or entry_id, error=exc)
                print(f"  FAIL entry_id={entry_id}: {exc}", file=sys.stderr)

    print(
        f"\nPR-37 suppress_re_engagement backfill complete "
        f"({'dry-run' if args.dry_run else 'live'}):\n"
        f"  rows_examined={mrun.rows_examined}\n"
        f"  rows_modified={mrun.rows_modified}\n"
        f"  rows_skipped_idempotent={mrun.rows_skipped_idempotent} "
        f"(of which {already_set_count} were already True)\n"
        f"  rows_failed={mrun.rows_failed}\n"
        f"  reclassification.success_count={crun.success_count}\n"
        f"  reclassification.abstain_count={crun.abstain_count} (manual_reply_unscored)\n"
        f"  reclassification.error_count={crun.error_count}"
    )
    if mrun.rows_failed > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
