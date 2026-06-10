# workflows/email_sequencer.py
"""Email sequencer: business-day timing logic for the 3-step email drip."""

from datetime import date

from models.business_calendar import (
    add_business_days,
    business_days_between,
    is_weekday,
)
from models.email_campaign import (
    EMAIL_TIMING,
    STAGE_FOR_EMAIL,
    EmailStage,
    EmailStep,
)

__all__ = [
    "EMAIL_TIMING",
    "STAGE_FOR_EMAIL",
    "EmailStage",
    "EmailStep",
    "add_business_days",
    "business_days_between",
    "get_pending_email",
    "is_email_eligible",
    "is_weekday",
]


def is_email_eligible(
    email_step: EmailStep,
    last_sent_date: date | None,
    today: date | None = None,
) -> bool:
    """Check if enough business days have passed to send this email step.

    For EMAIL1, always eligible (delay is 0).
    For EMAIL2, 3 business days must have passed since last_sent_date.
    For EMAIL3, 4 business days must have passed since last_sent_date
    (7 total from Email 1, but 4 from Email 2).
    """
    if last_sent_date is None:
        return False

    today = today or date.today()

    if email_step == EmailStep.EMAIL1:
        return True

    # For EMAIL2: 3 bdays from Email 1 send date
    # For EMAIL3: 7 bdays from Email 1, but we track from last_sent (Email 2)
    #   so it's 7 - 3 = 4 bdays from Email 2 send date
    if email_step == EmailStep.EMAIL3:
        required = EMAIL_TIMING[EmailStep.EMAIL3] - EMAIL_TIMING[EmailStep.EMAIL2]  # 4
    else:
        required = EMAIL_TIMING[email_step]

    elapsed = business_days_between(last_sent_date, today)
    return elapsed >= required


def get_pending_email(
    stage: EmailStage,
    last_sent_date: date | None,
    today: date | None = None,
) -> EmailStep | None:
    """Determine which email (if any) should be sent to a contact.

    Args:
        stage: Current email campaign stage.
        last_sent_date: Date the last email was sent.
        today: Override for testing.

    Returns:
        The EmailStep to send, or None if no email is due.
    """
    for step in (EmailStep.EMAIL1, EmailStep.EMAIL2, EmailStep.EMAIL3):
        if STAGE_FOR_EMAIL[step] == stage and is_email_eligible(step, last_sent_date, today):
            return step
    return None
