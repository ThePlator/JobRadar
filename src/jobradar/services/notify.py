"""Emails you can send by hand (a test alert, the digest, due follow-ups) and the follow-up
schedule. `jobradar run` sends all of these on its own; these are for "now"."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from jobradar.db.models import Job
from jobradar.queue.tasks import PermanentError, RetryableError
from jobradar.services.context import Ctx
from jobradar.services.errors import Refused


def _needs_email(ctx: Ctx, even_in_dry_run: bool = False) -> None:
    if (even_in_dry_run or not ctx.settings.config.notify.dry_run) and (
        missing := ctx.settings.missing("email")
    ):
        raise Refused("Add to .env first: " + ", ".join(missing))


def send_test_alert(ctx: Ctx) -> str:
    """Send yourself one test email, even in dry run. Returns where it went and the mode."""
    from jobradar.sinks.email_notify import smtp_send, test_email

    _needs_email(ctx, even_in_dry_run=True)
    msg = test_email(ctx.settings)
    try:
        smtp_send(ctx.settings, msg)
    except (PermanentError, RetryableError) as e:
        raise Refused(f"Not sent: {e}") from e
    mode = ("dry run is on: real alerts are saved to data/alerts/ instead"
            if ctx.settings.config.notify.dry_run else "alerts are on")  # fmt: skip
    return f"test email sent to {msg['To']} ({mode})"


async def send_digest_now(ctx: Ctx) -> str:
    """The digest for the current window, now. Returns what happened."""
    from jobradar.sinks.digest import Digest

    _needs_email(ctx)
    try:
        return await Digest(ctx.settings, ctx.repo, ctx.tasks).send_now()
    except (PermanentError, RetryableError) as e:
        raise Refused(f"Not sent: {e}") from e


@dataclass(frozen=True)
class FollowUp:
    """One Applied job and where its follow-up drafts stand."""

    job: Job
    applied: datetime
    since: datetime  # applied, or your last follow-up
    sent: int  # drafts sent so far
    next_at: datetime  # when the next draft is due
    state: str  # "done", "quiet", "due", "waiting"


def followups(ctx: Ctx) -> list[FollowUp]:
    """Every Applied job, with its follow-up state."""
    from jobradar.sinks.followups import MAX_REMINDERS, QUIET_AFTER, FollowUpReminder

    reminder = FollowUpReminder(ctx.settings, ctx.repo, ctx.tasks)
    out = []
    for job in ctx.repo.jobs_applied():
        applied, since, sent = reminder.status(job)
        next_at = since + reminder.after
        if sent >= MAX_REMINDERS:
            state = "done"
        elif ctx.now - since > QUIET_AFTER:
            state = "quiet"
        elif next_at <= ctx.now:
            state = "due"
        else:
            state = "waiting"
        out.append(FollowUp(job, applied, since, sent, next_at, state))
    return out


async def send_followups_now(ctx: Ctx) -> str:
    """Email the follow-up drafts that are due. Returns what happened."""
    from jobradar.sinks.followups import FollowUpReminder

    if not ctx.settings.config.notify.followups:
        raise Refused("notify.followups is false in config.yaml.")
    _needs_email(ctx)
    try:
        return await FollowUpReminder(ctx.settings, ctx.repo, ctx.tasks).check_now()
    except (PermanentError, RetryableError) as e:
        raise Refused(f"Not sent: {e}") from e
