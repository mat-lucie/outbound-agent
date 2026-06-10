"""DM sequencer: timing logic for connection notes and DM1/DM2/DM3."""

from datetime import date, timedelta

from clients.outreach_config import load_outreach_config
from models.business_calendar import shift_off_weekend
from models.campaign import MessageStep
from models.pipeline import PipelineStage

# Calendar days after last contact before each DM's earliest send.
# Sourced from config/outreach.yaml (cadence section); models/resolution.py
# derives _NEXT_STEP_DELTA_DAYS from the SAME scalars, so the two dicts stay
# equivalent by construction (drift-guard test_pr12_weekend_shift.py).
# The computed due date is shifted off weekends (Sat->Fri, Sun->Mon).
# The "is today a send day?" question lives one layer up — at the CLI gate.
_OUTREACH = load_outreach_config()
DM_TIMING: dict[MessageStep, int] = {
    MessageStep.DM1: _OUTREACH.cadence_connect_to_dm1_days,  # after accept -> DM1
    MessageStep.DM2: _OUTREACH.cadence_dm1_to_dm2_days,      # after DM1 -> DM2
    MessageStep.DM3: _OUTREACH.cadence_dm2_to_dm3_days,      # after DM2 -> DM3
}

# Which pipeline stage a prospect must be in to receive each DM
STAGE_FOR_DM: dict[MessageStep, PipelineStage] = {
    MessageStep.DM1: PipelineStage.ACCEPTED,
    MessageStep.DM2: PipelineStage.DM1_SENT,
    MessageStep.DM3: PipelineStage.DM2_SENT,
}

# Which stage to advance to after sending each DM
NEXT_STAGE: dict[MessageStep, PipelineStage] = {
    MessageStep.DM1: PipelineStage.DM1_SENT,
    MessageStep.DM2: PipelineStage.DM2_SENT,
    MessageStep.DM3: PipelineStage.DM3_SENT,
}


def earliest_send_date(dm_step: MessageStep, last_contact_date: date) -> date:
    """Compute the weekday-adjusted earliest send date for a DM step."""
    target = last_contact_date + timedelta(days=DM_TIMING.get(dm_step, 0))
    return shift_off_weekend(target)


def is_dm_eligible(
    dm_step: MessageStep,
    last_contact_date: date | None,
    today: date | None = None,
) -> bool:
    """True if the cadence window for this DM has opened.

    Cadence-only — does not consider whether today is a weekday. The CLI
    gate decides whether to run sends at all on a given day.
    """
    if last_contact_date is None:
        return False
    today = today or date.today()
    return today >= earliest_send_date(dm_step, last_contact_date)


def get_pending_dms(
    stage: PipelineStage,
    last_contact_date: date | None,
    today: date | None = None,
    dm_step: int = 0,
) -> MessageStep | None:
    """Determine which DM (if any) should be sent to a prospect.

    Args:
        stage: Current pipeline stage.
        last_contact_date: Date of last contact.
        today: Override for testing.
        dm_step: Attio's dm_step counter. Belt-and-suspenders: if the counter
            is already at N, we've already sent DMn — refuse to re-queue
            DM1..DMn regardless of stage. Guards against stale stage values.

    Returns:
        The MessageStep to send, or None if no DM is due.
    """
    for step in (MessageStep.DM1, MessageStep.DM2, MessageStep.DM3):
        required_step = int(step.value.replace("dm", ""))
        if dm_step >= required_step:
            continue
        if STAGE_FOR_DM[step] == stage and is_dm_eligible(step, last_contact_date, today):
            return step
    return None
