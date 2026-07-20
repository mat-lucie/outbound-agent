"""Code-version provenance for wet runs (PR-228).

An internal incident motivated this module: a weekly run executed from a
stale checkout that was missing two merged fixes — and nothing in the run's
artifacts recorded which code ran, so the miss was only reconstructable by
forensic fingerprinting of the staged JSONL. This module gives every wet run
two defenses:

1. `collect_run_provenance()` — a small dict (sha, branch, dirty,
   behind_origin_main) stamped into the weekly summary and the staged
   borderline JSONL, so a stale-code run is detectable post-hoc.
2. `assert_checkout_current()` — a preflight that refuses a wet run from a
   checkout behind origin/main (escape hatch: --allow-stale), mirroring the
   experiment/schema preflights (first line of the run, click.ClickException).

Git failures degrade to "unknown" — provenance must never crash a run.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import click

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Short timeout: `git fetch` talks to the network; a hung remote must not
# stall the run. All other commands are local and effectively instant.
_FETCH_TIMEOUT_S = 15
_LOCAL_TIMEOUT_S = 5


def _git(args: list[str], *, timeout: int = _LOCAL_TIMEOUT_S) -> str | None:
    """Run a git command in the repo root; None on any failure."""
    try:
        out = subprocess.check_output(
            ["git", *args],
            cwd=_REPO_ROOT,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
        return out.decode().strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError, OSError):
        return None


def collect_run_provenance(*, fetch: bool = False) -> dict:
    """Return {sha, branch, dirty, behind_origin_main} for the running code.

    `behind_origin_main` is True when origin/main is NOT an ancestor of HEAD
    — i.e. merged work exists that this checkout lacks. With fetch=False the
    comparison uses the locally-known origin/main (cheap, may itself be
    stale); fetch=True refreshes it first (network, best-effort).

    Every field degrades to "unknown"/None on git failure — a provenance
    stamp is never worth failing a run over.
    """
    if fetch:
        _git(["fetch", "origin", "main", "--quiet"], timeout=_FETCH_TIMEOUT_S)

    sha = _git(["rev-parse", "HEAD"])
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    porcelain = _git(["status", "--porcelain"])

    behind: bool | None = None
    ancestor_check = _git(
        ["merge-base", "--is-ancestor", "origin/main", "HEAD"]
    )
    if ancestor_check is not None:
        behind = False  # exit 0 → origin/main is an ancestor → current
    else:
        # Exit 1 means "not an ancestor" (genuinely behind); any other
        # failure (no origin/main ref, detached edge cases) is unknowable.
        # Distinguish via rev-parse of the ref itself.
        if _git(["rev-parse", "--verify", "origin/main"]) is not None:
            behind = True

    return {
        "sha": (sha or "unknown")[:12],
        "branch": branch or "unknown",
        "dirty": bool(porcelain) if porcelain is not None else None,
        "behind_origin_main": behind,
    }


def format_provenance(prov: dict) -> str:
    """One-line human rendering for the run summary."""
    dirty = {True: " +dirty", False: "", None: " dirty?"}[prov.get("dirty")]
    behind = {
        True: " ⚠ BEHIND origin/main",
        False: "",
        None: " (origin/main unknown)",
    }[prov.get("behind_origin_main")]
    return f"{prov.get('branch')}@{prov.get('sha')}{dirty}{behind}"


class StaleCheckoutError(click.ClickException):
    """Wet run refused: the checkout is missing merged work (PR-228 preflight)."""


def assert_checkout_current(
    *, dry_run: bool, allow_stale: bool = False
) -> dict:
    """Preflight for wet runs: refuse when HEAD is behind origin/main.

    Returns the collected provenance either way so the caller can stamp it.
    - Wet run + behind + not allow_stale → StaleCheckoutError (clean abort
      before any lock/PB launch/CRM write).
    - Dry run or allow_stale → warn to stderr, continue.
    - Dirty tree → warn only (worktree-based development is routine here;
      dirtiness is signal, not a blocker).
    - Git/network failures → warn that staleness is unverifiable, continue
      (an offline operator must still be able to run).
    """
    prov = collect_run_provenance(fetch=not dry_run)

    if prov["dirty"]:
        click.echo(
            f"⚠  Working tree has uncommitted changes ({format_provenance(prov)}) "
            "— the run's code is not fully described by its commit.",
            err=True,
        )

    if prov["behind_origin_main"] is True:
        # Remediation is branch-aware (operator lens): on a feature-branch
        # worktree "pull/rebase" is the WRONG advice — the right move is
        # running from a current main checkout, or an explicit --allow-stale
        # if this branch's code is intentionally wanted.
        if prov["branch"] in ("main", "master"):
            remedy = (
                "Run `git pull origin main` in this checkout, or re-run "
                "with --allow-stale to proceed anyway."
            )
        else:
            remedy = (
                f"You are on branch {prov['branch']!r}, not main — run from "
                "a checkout that is on current main, or pass --allow-stale "
                "if you intentionally want THIS branch's code."
            )
        msg = (
            f"Checkout is BEHIND origin/main ({format_provenance(prov)}) — "
            "merged fixes exist that this run would not execute. This is how "
            "a stale-checkout run can silently miss already-merged work. "
            + remedy
        )
        if dry_run or allow_stale:
            click.echo(f"⚠  {msg}", err=True)
        else:
            raise StaleCheckoutError(msg)
    elif prov["behind_origin_main"] is None:
        click.echo(
            "⚠  Could not verify checkout freshness against origin/main "
            f"({format_provenance(prov)}) — proceeding, but treat results "
            "with the same caution as --allow-stale.",
            err=True,
        )

    return prov
