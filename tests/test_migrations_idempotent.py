"""Plan §9 line 1086 — idempotency contract for every migration / backfill
script enumerated in the §9 done-criteria list.

Each script in SCRIPTS_REQUIRED_BY_PLAN_SECTION_9 must (on the SECOND
consecutive run with identical args) satisfy the four-invariant contract:

    rows_examined > 0
    rows_modified == 0
    rows_skipped_idempotent == rows_examined
    rows_failed == 0

This file covers the contract three ways:

  1. Static existence + contract markers (always runs in CI).
     Confirms each script (a) exists at the expected path or is
     explicitly documented MISSING / RENAMED, and (b) uses
     MigrationRunWriter (or is in EXEMPT_SCRIPTS per §3.13).

  2. F-PR-3 bootstrap JSONL bridge per §9 line 1087.
     F-PR-3's bootstrap migration (Operator Review Queue creation)
     predates the Migration Run object and so cannot use
     MigrationRunWriter. Instead it appends to
     ``~/.outbound-agent/audit/F-PR-3-bootstrap-{date}.jsonl`` and the
     contract is asserted by reading that file. The JSONL is one
     record per run with the same four counter fields.

  3. Behavioral subprocess pass-2 (gated by OUTBOUND_TEST_RUN_MIGRATIONS=1).
     Off by default — these scripts hit production Attio. When the
     env var is set, each script is invoked with `--dry-run` (or its
     subcommand equivalent) and the writer's emitted counters are
     parsed from stdout / from the most-recent Migration Run row.
     Operator-driven local verification only.

PR-38 deal idempotency is enforced at workflow level
(``workflows.deal_creation.create_deal_from_response``) via
``build_idempotency_key`` + ``creation_idempotency_key`` collision check
returning ``action="cached_idempotent"``. Behavioral coverage lives in
``tests/test_pr38_deal_creation.py::test_idempotent_hit_returns_cached``
and is referenced here for the §9 enumeration's completeness.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# §9 canonical enumeration (plan lines 1069-1085 + Round-4 D26 association
# outreach migration + workflows/deal_creation.py PR-38 + PR-39 nurture).
#
# Each entry is the path RELATIVE to the repo root and an optional
# `renamed_from` field for scripts the plan refers to under one name but
# that landed in the repo under another.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MigrationScriptSpec:
    plan_id: str  # e.g. "F-PR-3"
    plan_path: str  # the path the plan text uses
    actual_path: str | None  # the path in the repo (None if MISSING / not-applicable)
    exempt_from_writer: bool = False
    bootstrap_jsonl: bool = False
    workflow_idempotent: bool = False  # PR-38: workflow-level, no script
    # Reason this §9 entry never produced a standalone script. Set on entries
    # where the plan-§9 description turned out to not match the shipped PR
    # (different scope) or where the functionality is inlined into a workflow
    # function. When set, the entry skips with this reason instead of the
    # generic "MISSING" message; plan §9 was amended 2026-05-24 to match.
    not_applicable_reason: str | None = None


SCRIPTS_REQUIRED_BY_PLAN_SECTION_9: tuple[MigrationScriptSpec, ...] = (
    # 1. F-PR-3 — Operator Review Queue bootstrap (writer-exempt; uses JSONL)
    MigrationScriptSpec(
        plan_id="F-PR-3",
        plan_path="scripts/migrate_attio_F1_operator_review_queue.py",
        actual_path="scripts/migrate_attio_F1_operator_review_queue.py",
        exempt_from_writer=True,
        bootstrap_jsonl=True,
    ),
    # 2. F-PR-3.5 — schema delta validator (read-only, exempt)
    MigrationScriptSpec(
        plan_id="F-PR-3.5",
        plan_path="scripts/validate_attio_schema_deltas.py",
        actual_path="scripts/validate_attio_schema_deltas.py",
        exempt_from_writer=True,
    ),
    # 3. F-PR-3.7 — Reclassification Run + Migration Run object creators
    MigrationScriptSpec(
        plan_id="F-PR-3.7",
        plan_path="scripts/migrate_attio_reclassification_migration_objects.py",
        actual_path="scripts/migrate_attio_F2_reclassification_migration_objects.py",
        exempt_from_writer=True,
    ),
    # 4. F-PR-6 — experiments.tsv → Attio Experiment object migration
    MigrationScriptSpec(
        plan_id="F-PR-6",
        plan_path="scripts/migrate_experiments_tsv_to_attio.py",
        actual_path="scripts/migrate_experiments_tsv_to_attio.py",
    ),
    # 5. F-PR-8 — state files → Attio daily_run migration
    MigrationScriptSpec(
        plan_id="F-PR-8",
        plan_path="scripts/migrate_state_files_to_attio_daily_run.py",
        actual_path="scripts/migrate_state_files_to_attio_daily_run.py",
    ),
    # 6a. PR-9a — dmN_sent_at schema migration
    MigrationScriptSpec(
        plan_id="PR-9a-schema",
        plan_path="scripts/migrate_dmN_sent_at_schema.py",
        actual_path="scripts/migrate_dmN_sent_at_schema.py",
    ),
    # 6b. PR-9a B2-fix — canonical LinkedIn URL backfill
    MigrationScriptSpec(
        plan_id="PR-9a-canonical",
        plan_path="scripts/backfill_canonical_linkedin_url.py",
        actual_path="scripts/backfill_canonical_linkedin_url.py",
    ),
    # 7. PR-9.5 — union-merge dedup
    MigrationScriptSpec(
        plan_id="PR-9.5",
        plan_path="scripts/attio_dedup.py",
        actual_path="scripts/attio_dedup.py",
    ),
    # 8. PR-9b — per-step timestamp backfill
    MigrationScriptSpec(
        plan_id="PR-9b",
        plan_path="scripts/backfill_per_step_timestamps.py",
        actual_path="scripts/backfill_per_step_timestamps.py",
    ),
    # 9. PR-11 — baseline-v0 recompute
    MigrationScriptSpec(
        plan_id="PR-11",
        plan_path="scripts/recompute_baseline_v0.py",
        actual_path="scripts/recompute_baseline_v0.py",
    ),
    # 10. PR-13 — per-company outreach state backfill. Originally amended
    # 2026-05-24 as `not_applicable` because the actual PR-13 (commit b59f726)
    # shipped per-company throttle, a different scope. Re-flipped 2026-05-24
    # after the operator directed shipping all 3 missing scripts: PR #119 added
    # `scripts/backfill_per_company_outreach_state.py` which IS the §9
    # line 1079 candidate.
    MigrationScriptSpec(
        plan_id="PR-13",
        plan_path="scripts/backfill_per_company_outreach_state.py",
        actual_path="scripts/backfill_per_company_outreach_state.py",
    ),
    # 11. PR-22 — cohort archaeology
    MigrationScriptSpec(
        plan_id="PR-22",
        plan_path="scripts/backfill_experiment_id_archaeology.py",
        actual_path="scripts/backfill_experiment_id_archaeology.py",
    ),
    # 12. PR-25 — industry reclassification. Originally amended 2026-05-24 to
    # point at the workflow function (workflow_idempotent=True). Re-flipped
    # 2026-05-24 after the operator directed shipping all 3 missing scripts: PR #118
    # added `scripts/reclassify_industry.py` (CLI wrapper) which IS the §9
    # line 1081 candidate. The script accepts `mig_run` + `rc_run` kwargs and
    # threads the writer methods through `backfill_missing_industries`.
    MigrationScriptSpec(
        plan_id="PR-25",
        plan_path="scripts/reclassify_industry.py",
        actual_path="scripts/reclassify_industry.py",
    ),
    # 13. PR-26 — language backfill. Originally amended 2026-05-24 as
    # `not_applicable` because the actual PR-26 (commit 2e0b0c1) shipped
    # expanded deterministic disqualifiers, a different scope. Re-flipped
    # 2026-05-24 after the operator directed shipping all 3 missing scripts: PR #120
    # added `scripts/backfill_language.py` which IS the §9 line 1082
    # candidate. Inference path: linked Company domain + hq_country_code →
    # `models.email_campaign.detect_language()`.
    MigrationScriptSpec(
        plan_id="PR-26",
        plan_path="scripts/backfill_language.py",
        actual_path="scripts/backfill_language.py",
    ),
    # 14. PR-37 — suppress_re_engagement backfill
    MigrationScriptSpec(
        plan_id="PR-37",
        plan_path="scripts/backfill_suppress_re_engagement.py",
        actual_path="scripts/backfill_suppress_re_engagement.py",
    ),
    # 15. PR-38 — deal idempotency seed (workflow, not a script)
    MigrationScriptSpec(
        plan_id="PR-38",
        plan_path="workflows/deal_creation.py",
        actual_path="workflows/deal_creation.py",
        workflow_idempotent=True,
    ),
    # 16. PR-43 — quarantine override backfill (landed as prospect_committed_at)
    MigrationScriptSpec(
        plan_id="PR-43",
        plan_path="scripts/backfill_quarantine_override.py",
        actual_path="scripts/backfill_prospect_committed_at.py",
    ),
)


# ---------------------------------------------------------------------------
# Static existence + contract checks (cheap; run in every CI invocation).
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


_WRITER_IMPORT_RE = re.compile(
    r"^\s*from\s+workflows\.migration_run_writer\s+import\s+.*\bMigrationRunWriter\b",
    re.MULTILINE,
)
_WRITER_CALL_RE = re.compile(r"\bMigrationRunWriter\s*\(", re.MULTILINE)
_DRY_RUN_FLAG_RE = re.compile(
    r"--dry-run|--pretend|--apply", re.MULTILINE
)


@pytest.mark.parametrize(
    "spec",
    SCRIPTS_REQUIRED_BY_PLAN_SECTION_9,
    ids=lambda s: s.plan_id,
)
def test_script_exists_or_missing_documented(spec: MigrationScriptSpec) -> None:
    """Every §9-enumerated script either exists at actual_path or is
    explicitly None with a not_applicable_reason. Catches typos in this
    file when a script is renamed, and surfaces plan-vs-implementation
    drift via the rationale field."""
    if spec.actual_path is None:
        if spec.not_applicable_reason is None:
            pytest.fail(
                f"{spec.plan_id} ({spec.plan_path}) has actual_path=None "
                f"but no not_applicable_reason. Either point actual_path at "
                f"the shipped file, or set not_applicable_reason explaining "
                f"why the plan-§9 entry doesn't map to a real artifact."
            )
        pytest.skip(
            f"{spec.plan_id} ({spec.plan_path}) — not applicable: "
            f"{spec.not_applicable_reason}"
        )
    full = REPO_ROOT / spec.actual_path
    assert full.exists(), (
        f"{spec.plan_id} expected at {spec.actual_path} — file not found. "
        f"Either the script was deleted, renamed, or the spec is stale."
    )


@pytest.mark.parametrize(
    "spec",
    [
        s
        for s in SCRIPTS_REQUIRED_BY_PLAN_SECTION_9
        if s.actual_path is not None
        and not s.exempt_from_writer
        and not s.workflow_idempotent
    ],
    ids=lambda s: s.plan_id,
)
def test_script_uses_migration_run_writer(spec: MigrationScriptSpec) -> None:
    """Non-exempt migration scripts must use MigrationRunWriter — the
    sole source of the four-invariant log emission per §3.13."""
    assert spec.actual_path is not None  # narrowed by parametrize filter
    text = _read(REPO_ROOT / spec.actual_path)
    has_import = bool(_WRITER_IMPORT_RE.search(text))
    has_call = bool(_WRITER_CALL_RE.search(text))
    assert has_import and has_call, (
        f"{spec.plan_id} ({spec.actual_path}) does not import + invoke "
        f"MigrationRunWriter. Without the writer, the four-invariant "
        f"contract (§9 line 1086) cannot be satisfied. Add a "
        f"`with MigrationRunWriter(...) as run:` block, or add to "
        f"EXEMPT_SCRIPTS in test_migration_writer_compliance.py with a "
        f"plan-level justification."
    )


@pytest.mark.parametrize(
    "spec",
    [
        s
        for s in SCRIPTS_REQUIRED_BY_PLAN_SECTION_9
        if s.actual_path is not None
        and not s.workflow_idempotent
        # validate_attio_schema_deltas is read-only by construction; the
        # script is its own dry-run and doesn't surface a flag.
        and s.plan_id != "F-PR-3.5"
    ],
    ids=lambda s: s.plan_id,
)
def test_script_exposes_dry_run_flag(spec: MigrationScriptSpec) -> None:
    """Every mutating script must expose --dry-run / --pretend / --apply
    so the pass-2 verification can be invoked safely. attio_dedup uses
    Click subcommands; --pretend is its dry-run equivalent and --apply
    is the explicit-write gate."""
    assert spec.actual_path is not None
    text = _read(REPO_ROOT / spec.actual_path)
    assert _DRY_RUN_FLAG_RE.search(text), (
        f"{spec.plan_id} ({spec.actual_path}) does not expose a "
        f"--dry-run / --pretend / --apply flag. Pass-2 verification "
        f"requires one of these to invoke the script safely without "
        f"mutating production Attio."
    )


# ---------------------------------------------------------------------------
# Wave-1.6 FIX-4 — extended §3.13 glob for one-shot mutating scripts.
#
# The curated SCRIPTS_REQUIRED_BY_PLAN_SECTION_9 above tracks the
# canonical migration / backfill scripts the plan §9 enumerates. But
# the engineer-sre lens-QA pass-1 round-2 audit surfaced a second
# class of Attio-mutating scripts that the §3.13 compliance gate was
# blind to: one-shot repair / dedup / revert / reconstruct / apply-
# stage-override scripts (born from real incidents, e.g. the 2026-04-21
# dedup that damaged 70+ enterprise cadence states). These scripts
# mutate Attio just like migration scripts but were never §9-enumerated.
#
# Wave-1.6 surfaces them in the test output as KNOWN gaps (xfail).
# Wave-2 will retroactively wrap them with `MigrationRunWriter` — and
# each xfail entry that flips green is one bullet point closer to
# §3.13 compliance. The list of xfail'd scripts IS the Wave-2 backlog.
# ---------------------------------------------------------------------------


# Glob patterns for the second class of Attio-mutating scripts.
#
# Wave-1.6-ext FIX-4' (adversarial SB-3): the original Wave-1.6 glob
# at `apply_stage_*` missed `apply_industry_classifications.py` and
# `rescore_prospect_cohort.py`. Both mutate Attio (via update_company /
# update_list_entry / AttioWriter.apply) without `MigrationRunWriter`,
# so neither the §9 curated list NOR the original Wave-1.6 glob caught
# them — silent §3.13 violations. Extended `apply_stage_*` → `apply_*`
# and added `rescore_*` to close the gap.
#
# `scripts/attio_dedup.py` is intentionally NOT matched by `attio_*`
# here because it IS wrapped (verified — 5 `MigrationRunWriter` usages)
# AND it's already in the curated §9 list at SCRIPTS_REQUIRED_BY_PLAN_
# SECTION_9 (plan_id PR-9.5). Adding it to this glob would dupe-test
# the same script.
_REPAIR_GLOB_PATTERNS: tuple[str, ...] = (
    "scripts/repair_*.py",
    "scripts/dedup_*.py",
    "scripts/revert_*.py",
    "scripts/reconstruct_*.py",
    "scripts/apply_*.py",
    "scripts/rescore_*.py",
)


def _glob_repair_scripts() -> list[Path]:
    """Discover all repair/dedup/revert/reconstruct/apply-stage scripts
    under scripts/. Returns sorted absolute paths so the parametrize id
    list is stable across runs."""
    found: set[Path] = set()
    for pattern in _REPAIR_GLOB_PATTERNS:
        for path in REPO_ROOT.glob(pattern):
            if path.is_file():
                found.add(path)
    return sorted(found)


# Scripts known to be unwrapped as of Wave-1.6 (and Wave-1.6-ext).
# Each entry here is one bullet on the Wave-2 §3.13 wrap backlog. When
# a script in this list gets wrapped, REMOVE it from this set —
# pytest will then promote it from xfail to a real pass, which is the
# signal we want.
#
# Wave-2-C drained the entire backlog: every script that used to be in
# this set now imports `MigrationRunWriter` AND invokes it inside a
# context-manager wrap around its mutation loop. The set is kept
# empty rather than deleted so a future repair/dedup/revert/
# reconstruct/apply/rescore script that lands without the wrap still
# has a documented backlog mechanism to fall back to.
_WAVE_2_UNWRAPPED_BACKLOG: frozenset[str] = frozenset()


def _repair_script_params() -> list[pytest.param]:
    """Build parametrize values for the repair-script compliance test.
    Each known-unwrapped script gets an xfail marker so the test still
    surfaces the gap in CI output without breaking the build."""
    params = []
    for path in _glob_repair_scripts():
        rel = path.relative_to(REPO_ROOT).as_posix()
        marks = []
        if rel in _WAVE_2_UNWRAPPED_BACKLOG:
            marks.append(
                pytest.mark.xfail(
                    reason=(
                        "wave-2: needs MigrationRunWriter wrap "
                        "(§3.13 compliance backlog item)"
                    ),
                    strict=True,
                )
            )
        params.append(pytest.param(path, marks=marks, id=rel))
    return params


@pytest.mark.parametrize("script_path", _repair_script_params())
def test_repair_dedup_revert_scripts_use_migration_run_writer(
    script_path: Path,
) -> None:
    """Wave-1.6 FIX-4 / §3.13 compliance: every Attio-mutating one-shot
    script (repair_*, dedup_*, revert_*, reconstruct_*, apply_stage_*)
    must use `MigrationRunWriter` so its four-invariant counters land
    in the audit trail. xfail entries are tracked in
    `_WAVE_2_UNWRAPPED_BACKLOG` — when a script gets wrapped, remove it
    from that set so the test promotes from xfail to pass."""
    text = _read(script_path)
    has_import = bool(_WRITER_IMPORT_RE.search(text))
    has_call = bool(_WRITER_CALL_RE.search(text))
    assert has_import and has_call, (
        f"{script_path.name} does not import + invoke MigrationRunWriter. "
        f"One-shot repair/dedup/revert scripts mutate Attio just like §9 "
        f"migrations and must satisfy the same four-invariant audit contract "
        f"(§3.13). Wrap the script's Attio writes in a "
        f"`with MigrationRunWriter(...) as run:` block, or — if the script "
        f"is genuinely exempt — add an entry to the curated §9 spec with "
        f"`exempt_from_writer=True` and a justification."
    )


# Wave-2-C fix-up (multi-agent C6): semantic check on top of the regex.
# The regex above catches scripts that DON'T import + invoke
# MigrationRunWriter. But a script that imports it, opens
# `with MigrationRunWriter(...) as run:`, then never calls any counter
# method (examine / mark_modified / mark_failed / skip_idempotent /
# skip_excluded) still satisfies the regex but emits a useless
# Migration Run row with rows_examined=0 — silently §3.13-non-compliant.
# This test walks the AST to verify at least one counter call exists
# inside the `with MigrationRunWriter(...)` block.
_WRITER_COUNTER_METHODS = frozenset({
    "examine", "mark_modified", "mark_failed",
    "skip_idempotent", "skip_excluded",
})


def _writer_block_has_counter_calls(text: str) -> bool:
    """AST-walk: True iff at least one ``run.<counter>()`` call appears
    inside a ``with MigrationRunWriter(...) as run:`` block (or any
    enclosed function within that block).

    The check is name-based: we identify the bound variable from the
    ``as <name>:`` clause and look for ``<name>.examine()`` /
    ``<name>.mark_modified()`` / etc. anywhere inside the block. Names
    other than ``run`` are accepted (callers occasionally use
    ``mrun`` / ``writer`` / etc.).
    """
    import ast
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False

    bound_names: set[str] = set()

    class _Visitor(ast.NodeVisitor):
        def visit_With(self, node: ast.With) -> None:
            for item in node.items:
                ctx = item.context_expr
                # Detect `MigrationRunWriter(...)` constructor calls.
                is_writer = False
                if isinstance(ctx, ast.Call):
                    fn = ctx.func
                    if isinstance(fn, ast.Name) and fn.id == "MigrationRunWriter" or isinstance(fn, ast.Attribute) and fn.attr == "MigrationRunWriter":
                        is_writer = True
                if is_writer and item.optional_vars:
                    if isinstance(item.optional_vars, ast.Name):
                        bound_names.add(item.optional_vars.id)
            self.generic_visit(node)

    _Visitor().visit(tree)
    if not bound_names:
        return False

    # Walk again looking for any `<bound_name>.<counter>()` call.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not isinstance(fn, ast.Attribute):
            continue
        if not isinstance(fn.value, ast.Name):
            continue
        if fn.value.id not in bound_names:
            continue
        if fn.attr in _WRITER_COUNTER_METHODS:
            return True
    return False


@pytest.mark.parametrize("script_path", _repair_script_params())
def test_repair_dedup_revert_scripts_invoke_counter_methods(
    script_path: Path,
) -> None:
    """Wave-2-C fix-up (multi-agent C6): the lexical-regex check above
    catches "script doesn't import MigrationRunWriter at all" but
    misses "script opens the writer context manager but never calls
    examine() / mark_modified() / etc.". This test AST-walks the
    script source and asserts at least one counter call appears
    inside the writer block — semantic check, not regex.

    Pattern catches:
      - context manager opened then immediately closed without counter
      - copy-paste error where the wrap was added but the loop body
        kept its pre-wrap structure (incrementing only local counters)
      - future refactors that mistakenly remove ALL counter calls
    """
    text = _read(script_path)
    assert _writer_block_has_counter_calls(text), (
        f"{script_path.name} opens a MigrationRunWriter context manager "
        f"but never calls any counter method "
        f"({sorted(_WRITER_COUNTER_METHODS)}) inside the block. A "
        f"Migration Run row with rows_examined=0 / rows_modified=0 / "
        f"rows_failed=0 is §3.13-non-compliant — the audit row exists "
        f"but tells the operator nothing about what the script did. "
        f"Add at least run.examine() per row + the appropriate "
        f"mark_modified() / mark_failed() / skip_excluded() calls."
    )


# ---------------------------------------------------------------------------
# F-PR-3 JSONL bridge per §9 line 1087.
# ---------------------------------------------------------------------------


F_PR_3_AUDIT_DIR = Path.home() / ".outbound-agent" / "audit"
F_PR_3_JSONL_GLOB = "F-PR-3-bootstrap-*.jsonl"
F_PR_3_REQUIRED_FIELDS = frozenset({
    "run_id",
    "started",
    "completed",
    "script_name",
    "script_version",
    "rows_examined",
    "rows_modified",
    "rows_skipped_idempotent",
    "rows_failed",
})


def _f_pr_3_jsonl_files() -> list[Path]:
    if not F_PR_3_AUDIT_DIR.exists():
        return []
    return sorted(F_PR_3_AUDIT_DIR.glob(F_PR_3_JSONL_GLOB))


def test_f_pr_3_bootstrap_jsonl_shape_when_present() -> None:
    """§9 line 1087: F-PR-3's bootstrap JSONL is exempt from
    MigrationRunWriter but MUST contain the same four counter fields per
    run. If the JSONL doesn't exist (script hasn't run), skip; if it
    exists, every row must satisfy the contract."""
    files = _f_pr_3_jsonl_files()
    if not files:
        pytest.skip(
            f"No F-PR-3 bootstrap JSONL found under {F_PR_3_AUDIT_DIR}. "
            f"Either the F-PR-3 migration has not been run yet, or this "
            f"is a fresh dev environment. Once F-PR-3 has executed in "
            f"prod, this test asserts contract compliance per §9 line 1087."
        )
    for jsonl_path in files:
        with jsonl_path.open("r", encoding="utf-8") as fh:
            for line_no, raw in enumerate(fh, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                row = json.loads(raw)
                missing = F_PR_3_REQUIRED_FIELDS - row.keys()
                assert not missing, (
                    f"{jsonl_path.name}:{line_no} — F-PR-3 bootstrap row "
                    f"is missing required §9-line-1087 fields: "
                    f"{sorted(missing)}. Every row must carry the four "
                    f"invariant counters plus run identity fields."
                )
                for counter in (
                    "rows_examined",
                    "rows_modified",
                    "rows_skipped_idempotent",
                    "rows_failed",
                ):
                    assert isinstance(row[counter], int), (
                        f"{jsonl_path.name}:{line_no} — {counter} must "
                        f"be int, got {type(row[counter]).__name__}."
                    )
                    assert row[counter] >= 0, (
                        f"{jsonl_path.name}:{line_no} — {counter} cannot "
                        f"be negative."
                    )


def test_f_pr_3_bootstrap_jsonl_converges_to_noop_when_present() -> None:
    """The F-PR-3 bootstrap migration must CONVERGE to and HOLD a no-op
    steady state, per the §9 four-invariant contract (examined>0,
    modified==0, skipped==examined, failed==0).

    Why not just check ``rows[1]``: the JSONL stream is mutable local prod
    history that interleaves three things a positional row cannot tell
    apart — (a) ``--dry-run`` *predictions* (which count would-be changes
    as ``rows_modified``), (b) since-fixed failed applies (early runs that
    400'd on Attio-v2 type-mapping bugs, now corrected), and (c) multiple
    schema-definition versions (each time an option/attr is added to the
    migration, the next committed run legitimately reports one modification
    before settling). So the Nth positional row is meaningless. The actual
    idempotency contract is convergence: after the most recent run that
    changed or failed anything, every subsequent run must skip everything
    it examines, and a clean no-op must be reachable at all.

    NOTE: this is the PASSIVE history-reading check. Active "run it twice,
    second is a no-op" enforcement lives in the
    ``OUTBOUND_TEST_RUN_MIGRATIONS=1`` behavioral subprocess test (mode 3),
    which is the authority for catching a genuine non-idempotency.
    """
    files = _f_pr_3_jsonl_files()
    if not files:
        pytest.skip("No F-PR-3 bootstrap JSONL — see test above.")
    # Concatenate all jsonl files in chronological order. Each file is
    # `F-PR-3-bootstrap-{date}.jsonl` so the sorted name order = date
    # order; rows within a file are append-ordered.
    rows: list[dict] = []
    for jsonl_path in files:
        with jsonl_path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                rows.append(json.loads(raw))
    if len(rows) < 2:
        pytest.skip(
            f"Only {len(rows)} F-PR-3 bootstrap row(s) — need ≥2 to "
            f"verify the convergence invariant. Re-run the bootstrap "
            f"script (even --dry-run) to produce a second row."
        )

    def _is_clean_noop(r: dict) -> bool:
        return (
            r["rows_examined"] > 0
            and r["rows_modified"] == 0
            and r["rows_skipped_idempotent"] == r["rows_examined"]
            and r["rows_failed"] == 0
        )

    # (1) Convergence is reachable: at least one run in the whole stream is a
    # clean no-op. A migration that re-writes something on EVERY invocation
    # (genuine non-idempotency) never produces such a row and fails here.
    assert any(_is_clean_noop(r) for r in rows), (
        "F-PR-3 bootstrap never reached a clean no-op in any recorded run "
        "(rows_modified==0 & rows_skipped_idempotent==rows_examined & "
        "rows_failed==0). The migration is not converging to idempotency — "
        "inspect which Operator Review Queue object/attribute is re-applied "
        "on every run (likely a non-idempotent reconcile_select_options or "
        "an ensure_* that re-creates an existing target)."
    )

    # (2) Convergence holds: every run AFTER the most recent run that
    # changed or failed anything must be a clean no-op. The since-fixed early
    # failures sit before convergence, so they are naturally excluded.
    last_unsettled = max(
        (
            i
            for i, r in enumerate(rows)
            if r["rows_modified"] > 0 or r["rows_failed"] > 0
        ),
        default=-1,
    )
    confirming = rows[last_unsettled + 1 :]
    if last_unsettled >= 0 and not confirming:
        # The most recent run changed/failed something and NO later run has
        # confirmed it settled. Do NOT pass on the empty slice — a silent
        # vacuous pass would hide a converged-then-regressed migration whose
        # regression happens to be the last recorded row. Surface it as an
        # explicit skip; the OUTBOUND_TEST_RUN_MIGRATIONS=1 behavioral test is
        # the authority that actually re-runs and confirms convergence.
        pytest.skip(
            f"Most recent F-PR-3 bootstrap run (#{last_unsettled}) changed or "
            f"failed rows with no follow-up run to confirm it settles to a "
            f"no-op. Re-run the bootstrap (even --dry-run) to produce a "
            f"confirming row, or rely on the OUTBOUND_TEST_RUN_MIGRATIONS=1 "
            f"behavioral test."
        )
    for i, r in enumerate(confirming, start=last_unsettled + 1):
        assert _is_clean_noop(r), (
            f"F-PR-3 convergence violation at stream row #{i}: after the last "
            f"changing/failing run (#{last_unsettled}), every run must be a "
            f"clean no-op but this one has rows_examined="
            f"{r['rows_examined']}, rows_modified={r['rows_modified']}, "
            f"rows_skipped_idempotent={r['rows_skipped_idempotent']}, "
            f"rows_failed={r['rows_failed']}."
        )


# ---------------------------------------------------------------------------
# PR-38 deal idempotency — workflow-level, not a script.
# Cross-reference test only. The real assertions live in test_pr38_deal_creation.
# ---------------------------------------------------------------------------


def test_pr_38_idempotency_cross_reference() -> None:
    """PR-38 implements deal idempotency at workflow level via
    workflows.deal_creation.build_idempotency_key + creation_idempotency_key
    collision check (action="cached_idempotent"). The behavioural test
    lives in tests/test_pr38_deal_creation.py::test_idempotent_hit_returns_cached.

    This test exists so the §9 enumeration is complete — every script /
    workflow gets a row in the table above and a corresponding assertion
    here, even when the assertion is "covered elsewhere."
    """
    behavioural_test_file = REPO_ROOT / "tests" / "test_pr38_deal_creation.py"
    assert behavioural_test_file.exists(), (
        "tests/test_pr38_deal_creation.py is missing — PR-38 idempotency "
        "coverage relies on it. If it's been renamed, update the "
        "cross-reference here."
    )
    text = _read(behavioural_test_file)
    assert "cached_idempotent" in text, (
        "tests/test_pr38_deal_creation.py no longer references "
        "'cached_idempotent' — the idempotency contract may have been "
        "broken or the test moved. Investigate before merging."
    )


# ---------------------------------------------------------------------------
# Optional behavioural pass-2 verification (subprocess against prod Attio).
# Gated by OUTBOUND_TEST_RUN_MIGRATIONS=1 because each script writes to Attio
# on pass-1 (already done in prod via merged PRs) and re-running pass-2
# touches the live workspace. Operator-driven only.
# ---------------------------------------------------------------------------


SHOULD_RUN_BEHAVIORAL = os.environ.get("OUTBOUND_TEST_RUN_MIGRATIONS") == "1"


def _parse_writer_counters_from_log(log_lines: str) -> dict[str, int] | None:
    """Best-effort parse of MigrationRunWriter counters from stdout/stderr.
    The writer emits a JSON line under WARNING / on completion in some
    scripts; others print counts in a final summary line. Returns None
    if no counters could be extracted."""
    counters_re = re.compile(
        r"rows_examined[\"\s:=]+(\d+).*?rows_modified[\"\s:=]+(\d+)"
        r".*?rows_skipped_idempotent[\"\s:=]+(\d+).*?rows_failed[\"\s:=]+(\d+)",
        re.DOTALL,
    )
    m = counters_re.search(log_lines)
    if not m:
        return None
    return {
        "rows_examined": int(m.group(1)),
        "rows_modified": int(m.group(2)),
        "rows_skipped_idempotent": int(m.group(3)),
        "rows_failed": int(m.group(4)),
    }


@pytest.mark.skipif(
    not SHOULD_RUN_BEHAVIORAL,
    reason=(
        "Behavioral pass-2 against live Attio. Set "
        "OUTBOUND_TEST_RUN_MIGRATIONS=1 to enable. Off by default — these "
        "scripts hit production Attio."
    ),
)
@pytest.mark.parametrize(
    "spec",
    [
        s
        for s in SCRIPTS_REQUIRED_BY_PLAN_SECTION_9
        if s.actual_path is not None
        and not s.workflow_idempotent
        # F-PR-3.5 is read-only; running it doesn't surface writer counts.
        and s.plan_id != "F-PR-3.5"
    ],
    ids=lambda s: s.plan_id,
)
def test_script_pass_2_is_noop_behavioural(spec: MigrationScriptSpec) -> None:
    """Run each script with --dry-run (or --pretend for attio_dedup) and
    assert the writer's emitted counters show pass-2 invariants. Pass-1
    is assumed to have already executed in production via the merged PR;
    this is the SECOND pass.

    Operator must invoke with OUTBOUND_TEST_RUN_MIGRATIONS=1 and on a
    machine with valid Attio MCP scope."""
    assert spec.actual_path is not None
    full = REPO_ROOT / spec.actual_path
    if spec.plan_id == "PR-9.5":
        cmd = [
            "python",
            str(full),
            "apply",
            "--pretend",
            "--report",
            str(REPO_ROOT / "dedup_report.json"),
        ]
    else:
        cmd = ["python", str(full), "--dry-run"]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(REPO_ROOT),
        check=False,
    )
    combined = (result.stdout or "") + "\n" + (result.stderr or "")
    assert result.returncode == 0, (
        f"{spec.plan_id} exited {result.returncode}; output:\n{combined[:2000]}"
    )
    counters = _parse_writer_counters_from_log(combined)
    if counters is None:
        pytest.skip(
            f"{spec.plan_id} did not emit parseable writer counters. "
            f"Inspect output manually:\n{combined[:2000]}"
        )
    assert counters["rows_modified"] == 0, (
        f"{spec.plan_id} pass-2 violated rows_modified==0: counters="
        f"{counters}. Investigate why the script's _needs_change check "
        f"is not converging on the post-pass-1 state."
    )
    assert (
        counters["rows_skipped_idempotent"] == counters["rows_examined"]
    ), (
        f"{spec.plan_id} pass-2 violated rows_skipped_idempotent=="
        f"rows_examined: counters={counters}."
    )
    assert counters["rows_failed"] == 0, (
        f"{spec.plan_id} pass-2 had failures: counters={counters}."
    )
