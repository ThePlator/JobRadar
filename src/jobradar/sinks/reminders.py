"""Closing-soon reminders: one email when a job you shortlisted closes today or tomorrow.

A `closing_check` task runs every hour. Between 08:00 and 22:00 local time it looks for jobs
with Status Shortlisted whose deadline is today or tomorrow and that haven't been reminded
about yet. Each one is first checked with its hiring site (`pipeline/liveness.py`): a posting
that has already closed is skipped, not reminded about. Each job is reminded about once.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

from jobradar.clock import Clock, to_iso, utcnow
from jobradar.config import Settings
from jobradar.db.models import Job, JobStatus
from jobradar.db.repo import Repo, TaskRecord
from jobradar.pipeline.liveness import Liveness, LivenessChecker
from jobradar.pipeline.task_types import CLOSING_CHECK
from jobradar.queue.tasks import TaskQueue
from jobradar.sinks.email_actions import Actions
from jobradar.sinks.email_notify import PREVIEW_DIR, notion_url, save_preview, smtp_send
from jobradar.sinks.email_template import job_card, layout, posting_of

log = logging.getLogger(__name__)

CHECK_SECONDS = 3600
WAKING_HOURS = range(8, 22)  # no reminders at night: the next check in the morning sends it
REMINDED = "closing_reminder:"  # setting key prefix, per job
_SHORTLISTED = (JobStatus.SHORTLISTED.value, JobStatus.RESUME_READY.value)


def _title(job: Job) -> str:
    role = job.role or "Role not stated"
    return f"{role} @ {job.company}" if job.company else role


def build_reminder(
    jobs: list[Job], today: str, sender: str, to: str, actions: Actions | None = None
) -> EmailMessage:
    def when(job: Job) -> str:
        return "today" if job.deadline == today else "tomorrow"

    if len(jobs) == 1:
        subject = f"[JobRadar] Closes {when(jobs[0])}: {_title(jobs[0])}"
        title = f"Closes {when(jobs[0])}"
    else:
        subject = f"[JobRadar] {len(jobs)} jobs you shortlisted close soon"
        title = f"{len(jobs)} shortlisted jobs close soon"
    subtitle = "You shortlisted these in Notion. Apply before they close."
    cards = [
        job_card(job, notion=notion_url(job.notion_page_id),
                 actions=actions.job_links(job) if actions else None)
        for job in jobs
    ]  # fmt: skip
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.set_content(f"{title}\n\n" + "\n\n".join(text for _, text in cards) + "\n\n— JobRadar\n")
    footer = (
        "Sent by JobRadar to you only, once per job. Turn these off with "
        "<code>notify.closing_reminders: false</code>."
    )
    msg.add_alternative(
        layout(
            "JobRadar reminder",
            title,
            subtitle,
            subject.removeprefix("[JobRadar] "),
            "".join(card for card, _ in cards),
            footer,
        ),
        subtype="html",
    )
    return msg


class ClosingReminder:
    def __init__(
        self,
        settings: Settings,
        repo: Repo,
        queue: TaskQueue,
        liveness: LivenessChecker | None = None,
        clock: Clock = utcnow,
        preview_dir: Path = PREVIEW_DIR,
    ) -> None:
        self.settings = settings
        self.notify = settings.config.notify
        self.repo = repo
        self.queue = queue
        self.liveness = liveness
        self.clock = clock
        self.preview_dir = preview_dir

    def schedule(self, delay: float = 0, key: str = "a") -> None:
        self.queue.enqueue(CLOSING_CHECK, key, delay=delay)

    async def tick(self, task: TaskRecord) -> None:
        try:
            await self.check_now()
        finally:
            # The other key: re-queueing the running task would run it again at once.
            self.schedule(CHECK_SECONDS, key="b" if task.key == "a" else "a")

    async def check_now(self) -> str:
        """Send (or preview) reminders that are due; returns what happened."""
        local: datetime = self.clock().astimezone(self.notify.tz)
        if local.hour not in WAKING_HOURS:
            return "night: reminders wait for the morning"
        today = local.date()
        due = [
            job
            for job in self.repo.jobs_closing(
                today.isoformat(), (today + timedelta(days=1)).isoformat()
            )
            if job.status in _SHORTLISTED and self.repo.get_setting(REMINDED + job.id) is None
        ]
        jobs = [job for job in due if not await self._closed(job)]
        if not jobs:
            return "nothing closing soon"
        secrets = self.settings.secrets
        sender = secrets.email_address or "jobradar@localhost"
        msg = build_reminder(jobs, today.isoformat(), sender, secrets.notify_address or sender,
                             Actions.load(self.settings, self.repo))  # fmt: skip
        if self.notify.dry_run:
            path = save_preview(msg, self.preview_dir / f"reminder-{local:%Y%m%d-%H%M}.html")
            log.info(
                "dry run: reminder saved", extra={"subject": msg["Subject"], "preview": str(path)}
            )
            outcome = f"dry run: saved {path}"
        else:
            await asyncio.to_thread(smtp_send, self.settings, msg)
            log.info("closing reminder sent", extra={"jobs": len(jobs)})
            outcome = f"sent: {msg['Subject']}"
        for job in jobs:
            self.repo.set_setting(REMINDED + job.id, to_iso(self.clock()))
        return outcome

    async def _closed(self, job: Job) -> bool:
        """Already closed on its hiring site: no point reminding you (marked as done)."""
        if self.liveness is None:
            return False
        posting = posting_of(job)
        urls = [u for u in ((posting.apply_url if posting else None), job.canonical_url) if u]
        for url in dict.fromkeys(urls):
            state = await self.liveness.check(url)
            if state == Liveness.CLOSED:
                log.info("shortlisted job already closed; no reminder",
                         extra={"job_id": job.id[:10]})  # fmt: skip
                self.repo.set_setting(REMINDED + job.id, "closed")
                return True
            if state == Liveness.OPEN:
                return False
        return False
