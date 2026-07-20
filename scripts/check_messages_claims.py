#!/usr/bin/env python3
"""PR-16 (B-PD-008): ROI / OTIF / quantitative-claim CI guard.

Scans `content/messages.json` for marketing-claim language (ROI %,
OTIF % improvements, savings $, etc.) and verifies every claim has a
corresponding evidence entry in `content/evidence_refs.json`.

**Intended** as a pre-commit hook + CI gate. v1 ships the script
itself but NOT the `.pre-commit-config.yaml` registration or a
GitHub Actions workflow — those land in a follow-up. Until then the
script is reachable via manual invocation and via the pytest fixture
`test_current_messages_json_passes_guard`, which serves as the only
automatic gate.

# Why this matters

Pre-PR-16, any operator could add `"DM1": "Acme improved OTIF from
85% to 97% using <product>"` to messages.json with no audit trail. If the
claim turns out to be inaccurate (or sourced from a different
customer than the prospect believes), the LATAM B2B sales motion's
trust depends on every quantitative claim being backed by a verifiable
case study or aggregated benchmark.

# Detection pattern

We use **conservative regex** scoped to marketing-claim shapes, NOT
casual mentions of the same acronyms:

  - `\\b\\d+(?:\\.\\d+)?\\s*%\\s*(?:improvement|ROI|return|reduction)`
  - `\\bROI\\s+(?:of|de|del)\\s+\\d+`
  - `\\bOTIF\\s+(?:from|de|desde)\\s+\\d+\\s*%`
  - `\\$\\s*\\d+(?:,\\d{3})+\\s*(?:saved|ahorr)`

The regexes intentionally require quantitative anchors. A casual
"we care about your ROI" or "OTIF is important" does NOT trigger
because it lacks a number. This keeps the guard tight enough to ship
operational copy without false positives.

# Evidence file shape

`content/evidence_refs.json` is a flat dict:

  {
    "OTIF 85% to 97%": {"source": "Acme Foods 2026Q1 case study",
                         "verified_by": "operator@example.com",
                         "verified_at": "2026-04-15"},
    ...
  }

The key is the exact substring that matched the claim regex (so the
guard can dedupe across repeated mentions). Missing keys = missing
evidence = exit code 1.

# Exit codes

  - 0: no claims found, OR every claim has an evidence entry.
  - 1: claims found that lack evidence entries (claim text + persona
       + language + step printed to stderr).
  - 2: content files missing or malformed (file-not-found, bad JSON).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MESSAGES = REPO_ROOT / "content" / "messages.json"
DEFAULT_EVIDENCE = REPO_ROOT / "content" / "evidence_refs.json"


# Claim shapes. Each pattern returns the matched substring as the
# canonical evidence-key. Patterns require a numeric anchor.
CLAIM_PATTERNS: list[re.Pattern[str]] = [
    # "X% ROI / improvement / reduction / return"
    re.compile(
        r"\b\d+(?:\.\d+)?\s*%\s*(?:improvement|ROI|return|reduction|increase|aumento|reducción|reduccion)\b",
        re.IGNORECASE,
    ),
    # "ROI of X%" / "ROI de X" / "ROI del X%"
    re.compile(
        r"\bROI\s+(?:of|de|del)\s+\d+(?:\.\d+)?\s*%?",
        re.IGNORECASE,
    ),
    # "OTIF from X% to Y%" / "OTIF de X% a Y%" / "OTIF desde X%"
    re.compile(
        r"\bOTIF\s+(?:from|de|desde)\s+\d+(?:\.\d+)?\s*%",
        re.IGNORECASE,
    ),
    # PR-16 fold-in (GTM-QA + prospect-daily-QA convergence): OTIF
    # point-range notation — the shipped `messages.json` uses
    # "OTIF +5-8 points / puntos / pontos" across all three languages
    # in dm2_v1 for ICP-1 personas. The original regex set required a
    # `%` anchor and missed this pattern entirely.
    re.compile(
        r"\bOTIF\s+(?:up|improved?|[+])\s*\d+(?:\s*[-–]\s*\d+)?\s+p(?:oint|unt|ont)s?",
        re.IGNORECASE,
    ),
    # "$X,XXX" with savings/profit context (saved/ahorró before OR after)
    re.compile(
        r"(?:saved|ahorr\w*)\s+\$\s*\d+(?:,\d{3})+",
        re.IGNORECASE,
    ),
    re.compile(
        r"\$\s*\d+(?:,\d{3})+\s+(?:saved|in savings|ahorr|en ahorr)",
        re.IGNORECASE,
    ),
]


def find_claims(text: str) -> list[str]:
    """Return every substring in `text` that matches one of CLAIM_PATTERNS.

    Each match is normalized to lowercase + collapsed whitespace so
    the same claim across two messages dedups in the evidence file.
    """
    seen: set[str] = set()
    out: list[str] = []
    for pattern in CLAIM_PATTERNS:
        for match in pattern.finditer(text):
            key = " ".join(match.group(0).lower().split())
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out


def scan_messages(messages: dict, evidence_keys: set[str]) -> list[tuple[str, str, str, str]]:
    """Walk the nested messages.json structure looking for unsubstantiated claims.

    Returns a list of (claim_key, persona, language, step) tuples for
    every claim that doesn't appear in `evidence_keys`. Empty list
    means clean.
    """
    findings: list[tuple[str, str, str, str]] = []
    for persona_key, persona_msgs in messages.items():
        if not isinstance(persona_msgs, dict):
            continue
        for step_key, step_msgs in persona_msgs.items():
            if not isinstance(step_msgs, dict):
                continue
            for lang_key, body in step_msgs.items():
                if not isinstance(body, str):
                    continue
                for claim in find_claims(body):
                    if claim not in evidence_keys:
                        findings.append((claim, persona_key, lang_key, step_key))
    return findings


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--messages", type=Path, default=DEFAULT_MESSAGES,
        help=f"Path to messages.json (default: {DEFAULT_MESSAGES})",
    )
    parser.add_argument(
        "--evidence", type=Path, default=DEFAULT_EVIDENCE,
        help=f"Path to evidence_refs.json (default: {DEFAULT_EVIDENCE})",
    )
    args = parser.parse_args(argv)

    try:
        messages = json.loads(args.messages.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"error: messages file not found: {args.messages}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"error: malformed messages JSON: {exc}", file=sys.stderr)
        return 2

    try:
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # An absent evidence file is treated as "no evidence yet";
        # the guard still runs and fails LOUD if claims exist.
        evidence = {}
    except json.JSONDecodeError as exc:
        print(f"error: malformed evidence JSON: {exc}", file=sys.stderr)
        return 2

    # PR-16 fold-in (silent-failure-hunter IMPORTANT #3): a malformed
    # evidence_refs.json (operator hand-edited it into a list, string,
    # or null) silently degraded to an empty set pre-fold-in. Fail
    # loud — exit 2 makes the schema violation observable.
    if not isinstance(evidence, dict):
        print(
            f"error: {args.evidence} must be a JSON object; "
            f"got {type(evidence).__name__}",
            file=sys.stderr,
        )
        return 2

    # PR-16 fold-in (silent-failure-hunter IMPORTANT #1): two
    # evidence keys that differ only in case ("OTIF from 85%" vs
    # "OTIF FROM 85%") silently collapsed pre-fold-in. The set
    # comprehension hid one of the two metadata blobs. Warn loudly
    # when collisions are detected so the operator sees the second
    # source citation isn't actually retained.
    lowered: dict[str, list[str]] = {}
    for key in evidence:
        lowered.setdefault(key.lower(), []).append(key)
    for lower_key, originals in lowered.items():
        if len(originals) > 1:
            print(
                f"warning: evidence keys collide under case-insensitive "
                f"match: {originals!r} all map to {lower_key!r}. "
                f"Only one metadata blob will be visible to the guard.",
                file=sys.stderr,
            )
    evidence_keys = set(lowered.keys())
    findings = scan_messages(messages, evidence_keys)

    if not findings:
        print("messages claims OK: scanned, no unsubstantiated claims found.")
        return 0

    print(
        f"{len(findings)} unsubstantiated claim(s) in content/messages.json:",
        file=sys.stderr,
    )
    for claim, persona, lang, step in findings:
        print(
            f"  - {persona}/{step}/{lang}: {claim!r} "
            f"(add to content/evidence_refs.json)",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
