"""Email-lane kill switch — the drip senders are disarmed unless armed on purpose.

The cold-email drip is the highest-blast-radius lane in the engine and the one
least likely to be running on any given install: contacts can sit at stage
``queued`` for months while ``RESEND_API_KEY`` stays configured, so one
mis-typed ``email-daily --yes`` mails every queued prospect up to
``EMAIL_DAILY_CAP`` for real.

This module makes arming explicit. ``email-daily`` and ``email-wave2`` refuse a
LIVE send unless ``OUTBOUND_EMAIL_ENABLED`` is set to a truthy value. Nothing
else is gated:

  * ``--dry-run`` stays usable — previewing the queue is how you decide whether
    to arm, and a preview sends nothing;
  * ``email-association`` (custom, hand-approved outreach) is untouched;
  * ``email-unsubscribe`` and ``email-import`` are untouched — one is a
    compliance path that must always work, the other only stages CRM state.

Shape mirrors ``workflows.email_compliance.assert_email_compliance_ready``:
a loud exception at the top of the workflow, exempt on dry runs.

To arm a single run::

    OUTBOUND_EMAIL_ENABLED=1 python3 cli.py email-daily --yes

To re-open the lane for good, set ``OUTBOUND_EMAIL_ENABLED=1`` in ``.env``.

Provenance: ported from upstream ``LUCIE_EMAIL_ENABLED`` (upstream ``7502ac0``);
the env var is renamed per the fork's ``OUTBOUND_*`` seam and the upstream
operator's queue statistics are dropped (they were point-in-time facts about one
install, not engine behavior).
"""

from __future__ import annotations

import os

ENV_FLAG = "OUTBOUND_EMAIL_ENABLED"

# Deliberately narrow: an operator who typed something else meant something
# else, and "almost armed" must read as disarmed.
_TRUTHY = frozenset({"1", "true", "yes", "on"})


class EmailLaneDisabledError(Exception):
    """A live drip send was attempted while the email lane is disarmed."""


def email_lane_enabled() -> bool:
    """Return True when ``OUTBOUND_EMAIL_ENABLED`` arms the drip senders."""
    return os.environ.get(ENV_FLAG, "").strip().lower() in _TRUTHY


def assert_email_lane_enabled(command: str, *, dry_run: bool = False) -> None:
    """Fail loud before a LIVE drip send when the lane is disarmed.

    ``command`` names the CLI entry point (``email-daily`` / ``email-wave2``)
    so the message tells the operator exactly what to re-run.
    """
    if dry_run or email_lane_enabled():
        return
    # No volatile facts (queue size, last-send date) in this message: it is read
    # months from now, and a stale number reads as authoritative.
    raise EmailLaneDisabledError(
        f"Email lane is DISARMED — no live send from {command!r}. "
        f"The drip is off by default; contacts still parked in the email queue "
        f"would be mailed for real, so live sending needs an explicit opt-in. "
        f"Preview with:  python3 cli.py {command} --dry-run "
        f"Send for real with:  {ENV_FLAG}=1 python3 cli.py {command} --yes "
        f"(or set {ENV_FLAG}=1 in .env to re-open the lane for good)."
    )
