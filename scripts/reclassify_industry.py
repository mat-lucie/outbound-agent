#!/usr/bin/env python3
"""PR-25 industry vertical reclassification: scan Attio companies, classify
missing industry_vertical using Haiku, and PATCH the company record with
industry_vertical_status and industry_vertical_confidence.

Per plan §9 line 1081 (amended 2026-05-24), wraps
workflows.industry_classifier.backfill_missing_industries with both
MigrationRunWriter and ReclassificationRunWriter to satisfy the §9.4
four-invariant idempotency contract:

  - rows_examined > 0
  - rows_modified == 0 (on second consecutive pass — all companies
    already have industry_vertical set)
  - rows_skipped_idempotent == rows_examined
  - rows_failed == 0

# Idempotency contract (§9.4)

Second consecutive run: all companies returned from the search are filtered
out before classification (no missing industry_vertical). MigrationRunWriter
logs: `rows_modified=0, rows_skipped_idempotent=N, rows_failed=0`.

# Usage

    python3 scripts/reclassify_industry.py --dry-run
    python3 scripts/reclassify_industry.py --apply [--limit 50] [--confidence-threshold 0.7]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clients.attio import AttioClient  # noqa: E402
from workflows.industry_classifier import backfill_missing_industries  # noqa: E402
from workflows.migration_run_writer import MigrationRunWriter  # noqa: E402
from workflows.reclassification_run_writer import ReclassificationRunWriter  # noqa: E402

SCRIPT_VERSION = "pr-25-v1"
WRITER_MODULE = "scripts.reclassify_industry"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Dry-run mode: count classifications without writing to Attio",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply mode: write classifications to Attio",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after classifying this many records",
    )
    parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=0.7,
        help="Confidence threshold for ReclassificationRunWriter (default 0.7)",
    )
    args = parser.parse_args(argv)

    # Enforce XOR: one of --dry-run or --apply required
    if (args.dry_run and args.apply) or (not args.dry_run and not args.apply):
        print(
            "error: exactly one of --dry-run or --apply must be specified",
            file=sys.stderr,
        )
        return 2

    try:
        attio = AttioClient()
    except KeyError:
        print("error: ATTIO_API_KEY env var not set", file=sys.stderr)
        return 2

    print(
        f"PR-25 industry_vertical reclassification "
        f"({'dry-run' if args.dry_run else 'apply'}) — "
        f"initializing writers..."
    )

    # Construct both writers before entering so we can thread the
    # reclassification run_id into the migration row for forensics join.
    rec_writer = ReclassificationRunWriter(
        classifier_module=WRITER_MODULE,
        classifier_version=SCRIPT_VERSION,
        model_used="haiku_classifier",
        input_attr="company_name+domain",
        output_attr="industry_vertical",
        dry_run=args.dry_run,
        attio=attio,
        confidence_threshold=args.confidence_threshold,
    )
    mig_writer = MigrationRunWriter(
        script_name=Path(__file__).name,
        script_version=SCRIPT_VERSION,
        rollback_script_path=None,  # classification is effectively irreversible
        dry_run=args.dry_run,
        attio=attio,
        reclassification_run_id=rec_writer.run_id,
    )

    with rec_writer as crun, mig_writer as mrun:
        summary = backfill_missing_industries(
            attio,
            limit=args.limit,
            dry_run=args.dry_run,
            mig_run=mrun,
            rc_run=crun,
        )

    print(
        f"\nPR-25 industry_vertical reclassification complete "
        f"({'dry-run' if args.dry_run else 'apply'}):\n"
        f"  summary.total_scanned={summary['total_scanned']}\n"
        f"  summary.missing={summary['missing']}\n"
        f"  summary.classified={summary['classified']}\n"
        f"  summary.written={summary['written']}\n"
        f"  summary.api_errors={summary['api_errors']}\n"
        f"  summary.skipped={summary['skipped']}\n"
        f"  migration.rows_examined={mrun.rows_examined}\n"
        f"  migration.rows_modified={mrun.rows_modified}\n"
        f"  migration.rows_skipped_idempotent={mrun.rows_skipped_idempotent}\n"
        f"  migration.rows_failed={mrun.rows_failed}\n"
        f"  reclassification.success_count={crun.success_count}\n"
        f"  reclassification.abstain_count={crun.abstain_count}\n"
        f"  reclassification.error_count={crun.error_count}"
    )

    if mrun.rows_failed > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
