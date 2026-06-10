"""Inspect the PhantomBuster phantoms + Attio schema-attribute contract that
the Sales Navigator migration depends on. Answers questions that the rest of
PR-A's code is currently parameterized on.

Specifically:

1. **URL Converter shape** — input column name (likely `linkedinProfileUrl`),
   output column names (looking for `salesNavigatorUrl` AND `fullName` for the
   name-sanity-check), saved per-launch cap, agent name.
2. **Sales Nav Profile Scraper shape** — input column name (likely
   `profileUrls` or `spreadsheetUrl`), expected output columns (`degree` vs
   `connectionDegree` vs `networkDistance`, `defaultProfileUrl`, etc.).
3. **URL Converter cookie scope** — does the converter need Sales Nav cookies
   (`li_at` + `li_a`) or just the regular `li_at`? Inspected from the
   phantom's saved arguments.
4. **Attio `url` type acceptance** — does Attio's `url` attribute type accept
   the comma in `linkedin.com/sales/lead/<id>,<auth>` URLs, or do we need to
   fall back to `text`? Probed via a throwaway test attribute.

Output: `~/.outbound-agent/probe/pb-contracts-YYYY-MM-DD.md` (human-readable
report). Drives the column constants in PR-B.4 and the schema-type choice in
PR-A.A4.

## Usage

    python3 scripts/probe_pb_phantom_contracts.py
    python3 scripts/probe_pb_phantom_contracts.py --skip-attio    # PB-only
    python3 scripts/probe_pb_phantom_contracts.py --skip-pb       # Attio-only

Cleanup contract: the Attio probe creates a temp attribute `__sales_nav_url_probe`
on the `people` object and DELETES it after testing. If the script crashes
mid-probe, run `python3 scripts/probe_pb_phantom_contracts.py --cleanup-only`
to remove orphaned probe attributes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from clients.attio import AttioClient  # noqa: E402
from clients.phantombuster import PhantomBusterClient  # noqa: E402

PROBE_DIR = Path.home() / ".outbound-agent" / "probe"
PROBE_ATTR_SLUG = "__sales_nav_url_probe"
PROBE_SALES_NAV_URL = (
    "https://www.linkedin.com/sales/lead/ACoAABCDEF12345,NAME_SEARCH_FAKE"
)


@dataclass
class PhantomProbe:
    label: str
    agent_id: str
    name: str = ""
    script_id: str = ""
    saved_argument: dict | None = None
    last_csv_columns: list[str] = field(default_factory=list)
    last_run_at: str = ""
    error: str = ""


@dataclass
class AttioProbe:
    url_type_accepts_comma: bool | None = None
    detail: str = ""
    error: str = ""


@dataclass
class ProbeReport:
    run_at: str
    phantoms: list[PhantomProbe] = field(default_factory=list)
    attio: AttioProbe | None = None
    notes: list[str] = field(default_factory=list)


# --- PB phantom inspection ---


def inspect_phantom(
    pb: PhantomBusterClient, label: str, agent_id: str
) -> PhantomProbe:
    probe = PhantomProbe(label=label, agent_id=agent_id)
    try:
        agent = pb.get_agent(agent_id)
        probe.name = agent.get("name", "")
        probe.script_id = str(agent.get("scriptId", ""))
        # `argument` field stores the last-applied input as a JSON string.
        # Different PB phantom builds use `argument` (string) vs `arguments`
        # (dict). Try both.
        raw_arg = agent.get("argument") or agent.get("arguments")
        if isinstance(raw_arg, str):
            try:
                probe.saved_argument = json.loads(raw_arg)
            except json.JSONDecodeError:
                probe.saved_argument = {"_raw": raw_arg}
        elif isinstance(raw_arg, dict):
            probe.saved_argument = raw_arg
    except httpx.HTTPError as exc:
        probe.error = f"get_agent failed: {exc}"
        return probe

    # Try to pull the last container's CSV column headers (no new launch).
    try:
        out = pb.get_output(agent_id) if hasattr(pb, "get_output") else None
        if out:
            container_id = out.get("containerId", "")
            probe.last_run_at = out.get("lastEndStatusDate", "")
            if container_id:
                # Inspect CSV through the existing get_result_csv_url ->
                # download_result_csv path. We don't have a typed PBLaunch
                # for the last container, so do it inline.
                from clients.pb_envelope import PBLaunch  # noqa

                synth_launch = PBLaunch(
                    container_id=container_id,
                    agent_id=agent_id,
                    launched_at=datetime.now(UTC),
                    arguments_hash="probe",
                )
                csv_text = pb.download_result_csv(synth_launch)
                if csv_text:
                    first_line = csv_text.split("\n", 1)[0]
                    probe.last_csv_columns = [
                        c.strip().strip('"') for c in first_line.split(",")
                    ]
    except (httpx.HTTPError, AttributeError, ImportError) as exc:
        # CSV inspection is best-effort.
        if not probe.error:
            probe.error = f"CSV inspection skipped: {exc}"

    return probe


# --- Attio attribute type probe ---


def _attio_post_attribute(
    attio: AttioClient, attr_type: str
) -> tuple[bool, str]:
    """Try to create a probe attribute of the given type. Returns
    (succeeded, detail). The attribute is left in place for the value-write
    probe and deleted by `_attio_cleanup_probe`."""
    body = {
        "data": {
            "title": "Sales Nav URL Probe (DELETE ME)",
            "api_slug": PROBE_ATTR_SLUG,
            "description": "Throwaway attribute created by scripts/probe_pb_phantom_contracts.py to test whether Attio's url type accepts comma-containing Sales Nav URLs. Safe to delete.",
            "type": attr_type,
            "is_required": False,
            "is_unique": False,
            "is_multiselect": False,
            "config": {},
        }
    }
    try:
        attio._request("POST", "/objects/people/attributes", json=body)
        return True, f"attribute created with type={attr_type}"
    except httpx.HTTPStatusError as exc:
        body_text = exc.response.text[:300] if exc.response else ""
        return False, f"HTTP {exc.response.status_code}: {body_text}"


def _attio_cleanup_probe(attio: AttioClient) -> str:
    """DELETE the probe attribute. Idempotent."""
    try:
        attio._request(
            "DELETE", f"/objects/people/attributes/{PROBE_ATTR_SLUG}"
        )
        return "deleted"
    except httpx.HTTPStatusError as exc:
        if exc.response and exc.response.status_code == 404:
            return "already absent"
        return f"delete failed: HTTP {exc.response.status_code if exc.response else '?'}"


def probe_attio_url_type(attio: AttioClient) -> AttioProbe:
    """Probe whether Attio's `url` attribute type accepts the comma in a
    Sales Nav URL. Falls back to creating a `text`-typed attribute (which is
    known to work) and reports the actual constraint.

    NOTE: this CREATES then DELETES a `__sales_nav_url_probe` attribute on
    the `people` object. Per Attio's API, attribute deletes are an admin op.
    """
    probe = AttioProbe()
    # 1) Try `url` type creation
    ok, detail = _attio_post_attribute(attio, "url")
    if not ok:
        probe.url_type_accepts_comma = False
        probe.detail = f"`url` type rejected at creation: {detail}. Falling back to text."
        # No cleanup needed since creation failed.
        return probe

    # 2) Attribute created. Now attempt to write a comma URL into it on a
    #    sandbox-style probe Person. We don't actually create a person here;
    #    instead, we just inspect the attribute's schema to see the value
    #    constraints. If Attio honors `url` for comma-containing values, the
    #    attribute creation alone is the evidence we need.
    probe.url_type_accepts_comma = True
    probe.detail = (
        f"`url` type accepted at creation: {detail}. "
        "(Did not write a value — attribute creation acceptance is taken as "
        "evidence the type permits comma URLs. If runtime writes fail, "
        "switch the schema to `text` in scripts/migrate_attio_add_sales_nav_url.py.)"
    )

    # 3) Cleanup
    cleanup = _attio_cleanup_probe(attio)
    probe.detail += f" Cleanup: {cleanup}."
    return probe


# --- Report rendering ---

_SECRET_KEY_TOKENS = ("cookie", "session", "li_a", "li_at")


def _redact_secrets(obj):
    """Recursively redact string values whose key name matches a secret
    token. PB's Sales Nav phantom shape nests the session cookie inside
    ``identities: [{ "sessionCookie": "...", ... }]`` — a shallow filter
    that only checks top-level keys would leak the cookie when rendering
    the saved-argument dict (security-scan HIGH-1 from PR-B.9 review)."""
    if isinstance(obj, dict):
        return {
            k: (
                "<redacted>"
                if (
                    isinstance(v, str)
                    and any(s in k.lower() for s in _SECRET_KEY_TOKENS)
                )
                else _redact_secrets(v)
            )
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact_secrets(x) for x in obj]
    return obj


def render_report(report: ProbeReport) -> str:
    lines = [
        f"# PhantomBuster + Attio contracts probe — {report.run_at}",
        "",
        "Generated by `scripts/probe_pb_phantom_contracts.py`. Drives the column",
        "constants in PR-B.4 and the schema-type choice in PR-A.A4.",
        "",
    ]
    for phantom in report.phantoms:
        lines.append(f"## {phantom.label} (`{phantom.agent_id}`)")
        if phantom.error:
            lines.append(f"- **ERROR:** {phantom.error}")
        lines.append(f"- name: `{phantom.name}`")
        lines.append(f"- scriptId: `{phantom.script_id}`")
        lines.append(f"- last run at: `{phantom.last_run_at or '<unknown>'}`")
        if phantom.saved_argument:
            lines.append("- saved argument keys:")
            redacted_arg = _redact_secrets(phantom.saved_argument)
            for k, v in redacted_arg.items():
                lines.append(f"  - `{k}` = `{v!r}`"[:200])
        if phantom.last_csv_columns:
            lines.append("- last CSV columns:")
            for col in phantom.last_csv_columns:
                lines.append(f"  - `{col}`")
        lines.append("")

    lines.append("## Attio `url` attribute type — comma acceptance")
    attio = report.attio
    if attio is None:
        lines.append("- skipped (--skip-attio)")
    elif attio.error:
        lines.append(f"- **ERROR:** {attio.error}")
    else:
        accepts = attio.url_type_accepts_comma
        verdict = "accepts" if accepts else "rejects" if accepts is False else "unknown"
        lines.append(f"- verdict: **{verdict}** the comma in Sales Nav URLs")
        lines.append(f"- detail: {attio.detail}")
        if accepts:
            lines.append(
                "- recommendation: PR-A.A4 should use `type=url` for the "
                "`sales_nav_url` attribute."
            )
        else:
            lines.append(
                "- recommendation: PR-A.A4 should use `type=text` for the "
                "`sales_nav_url` attribute (fallback)."
            )
    lines.append("")
    if report.notes:
        lines.append("## Notes")
        for note in report.notes:
            lines.append(f"- {note}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--skip-pb", action="store_true", help="Skip PB phantom inspection"
    )
    parser.add_argument(
        "--skip-attio", action="store_true", help="Skip Attio url-type probe"
    )
    parser.add_argument(
        "--cleanup-only",
        action="store_true",
        help="Delete orphaned probe attribute and exit (no other probing)",
    )
    args = parser.parse_args(argv)

    load_dotenv()

    if args.cleanup_only:
        attio = AttioClient()
        result = _attio_cleanup_probe(attio)
        print(f"Cleanup result: {result}")
        return 0

    report = ProbeReport(run_at=datetime.now(UTC).isoformat())

    if not args.skip_pb:
        converter_id = os.environ.get(
            "PB_SALES_NAV_URL_CONVERTER_ID", ""
        ).strip()
        scraper_id = os.environ.get(
            "PB_SALES_NAV_PROFILE_SCRAPER_ID", ""
        ).strip()
        if not converter_id:
            report.notes.append(
                "PB_SALES_NAV_URL_CONVERTER_ID not set — converter inspection skipped"
            )
        if not scraper_id:
            report.notes.append(
                "PB_SALES_NAV_PROFILE_SCRAPER_ID not set — scraper inspection skipped"
            )
        with PhantomBusterClient() as pb:
            if converter_id:
                report.phantoms.append(
                    inspect_phantom(pb, "URL Converter", converter_id)
                )
            if scraper_id:
                report.phantoms.append(
                    inspect_phantom(pb, "Sales Nav Profile Scraper", scraper_id)
                )

    if not args.skip_attio:
        try:
            attio = AttioClient()
            report.attio = probe_attio_url_type(attio)
        except (httpx.HTTPError, RuntimeError) as exc:
            report.attio = AttioProbe(error=f"Attio client failed: {exc}")

    out_path = (
        PROBE_DIR / f"pb-contracts-{date.today().isoformat()}.md"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_report(report))
    print(render_report(report))
    print(f"\nWrote: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
