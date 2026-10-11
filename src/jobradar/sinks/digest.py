"""The digest: one summary email at each `notify.digest_times` slot (09:00 and 19:00 by
default), covering everything since the previous slot.

Each slot is a `digest` task keyed by its local time ("2026-10-12 09:00"), queued to run at
that time; sending one queues the next. A window with nothing new, nothing closing and no
problems sends nothing. `notify.dry_run` saves a preview in data/alerts/ instead.
"""

from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from email.message import EmailMessage
from pathlib import Path

from jobradar.clock import Clock, to_iso, utcnow
from jobradar.config import Settings
from jobradar.db.models import Job, JobStatus, TaskStatus
from jobradar.db.repo import Repo, TaskRecord
from jobradar.pipeline.task_types import DIGEST
from jobradar.queue.tasks import TaskQueue
from jobradar.sinks.email_notify import PREVIEW_DIR, notion_url, save_preview, smtp_send

log = logging.getLogger(__name__)

TOP = 5
CLOSING_DAYS = 2


def slots(times: list[str]) -> list[time]:
    return sorted(time.fromisoformat(t) for t in times)


def next_slot(after: datetime, times: list[str]) -> datetime:
    """The first digest time strictly after `after` (an aware local datetime)."""
    for day in range(2):
        date = after.date() + timedelta(days=day)
        for t in slots(times):
            at = datetime.combine(date, t, tzinfo=after.tzinfo)
            if at > after:
                return at
    raise ValueError("no digest times configured")


def previous_slot(before: datetime, times: list[str]) -> datetime:
    """The last digest time strictly before `before`."""
    for day in range(2):
        date = before.date() - timedelta(days=day)
        for t in reversed(slots(times)):
            at = datetime.combine(date, t, tzinfo=before.tzinfo)
            if at < before:
                return at
    raise ValueError("no digest times configured")


def _hidden_kind(job: Job) -> str:
    why = (job.score_reason or "").lower()
    if why.startswith("not eligible"):
        return "not eligible"
    if why.startswith("weak match"):
        return "weak match"
    return "ads and other"


@dataclass
class DigestData:
    since: datetime
    until: datetime
    new: int = 0
    inbox: list[Job] = field(default_factory=list)  # new jobs still open, best first
    hidden: dict[str, int] = field(default_factory=dict)
    closing: list[Job] = field(default_factory=list)
    alerts: int = 0
    failed: int = 0
    spent_inr: float = 0.0
    budget_inr: float = 0.0

    @property
    def empty(self) -> bool:
        return not (self.new or self.closing or self.failed)


def gather(repo: Repo, settings: Settings, since: datetime, until: datetime) -> DigestData:
    jobs = repo.jobs_created_since(to_iso(since))
    data = DigestData(since=since, until=until, new=len(jobs))
    data.inbox = sorted(
        (j for j in jobs if j.status == JobStatus.NEW.value),
        key=lambda j: (j.score is None, -(j.score or 0)),
    )
    for job in jobs:
        if job.status in (JobStatus.HIDDEN.value, JobStatus.DISCARDED.value):
            kind = _hidden_kind(job)
            data.hidden[kind] = data.hidden.get(kind, 0) + 1
    today = until.date()
    data.closing = repo.jobs_closing(
        today.isoformat(), (today + timedelta(days=CLOSING_DAYS)).isoformat()
    )
    data.alerts = repo.alerts_sent_since(to_iso(since))
    data.failed = repo.task_counts()[TaskStatus.FAILED]
    midnight = until.replace(hour=0, minute=0, second=0, microsecond=0)
    data.spent_inr = repo.llm_spend_since(to_iso(midnight))
    data.budget_inr = settings.config.llm.daily_budget_inr
    return data


def _title(job: Job) -> str:
    role = job.role or "A job"
    return f"{role} @ {job.company}" if job.company else role


def _links(job: Job) -> str:
    links = []
    if job.canonical_url:
        links.append(f'<a href="{html.escape(job.canonical_url, quote=True)}">Apply</a>')
    if page := notion_url(job.notion_page_id):
        links.append(f'<a href="{html.escape(page, quote=True)}">Notion</a>')
    return " · ".join(links)


def _deadline(job: Job) -> str:
    try:
        day = datetime.fromisoformat(job.deadline or "")
    except ValueError:
        return ""
    return f"{day.day} {day:%b}"


def build_digest(data: DigestData, sender: str, to: str, database_id: str | None) -> EmailMessage:
    part = "Morning" if data.until.hour < 12 else "Evening"
    strong = sum(1 for j in data.inbox if (j.score or 0) >= 75)
    bits = [f"{data.new} new"]
    if strong:
        bits.append(f"{strong} strong")
    if data.closing:
        bits.append(f"{len(data.closing)} closing soon")
    subject = f"[JobRadar] {part} digest · " + " · ".join(bits)

    text: list[str] = [subject.removeprefix("[JobRadar] "), ""]
    body: list[str] = [f"<h2 style='margin:0 0 4px;font-size:18px'>{html.escape(part)} digest</h2>"]
    since = f"{data.since.day} {data.since:%b %H:%M}"
    hidden = sum(data.hidden.values())
    summary = (
        f"Since {since}: {data.new} new jobs · {len(data.inbox)} kept in your Inbox · "
        f"{hidden} hidden · {data.alerts} alert{'s' if data.alerts != 1 else ''} sent"
    )
    text.append(summary)
    body.append(f"<p style='color:#5b6372;margin:0 0 16px'>{html.escape(summary)}</p>")

    if data.inbox:
        text += ["", "Top matches"]
        body.append("<h3 style='font-size:15px;margin:16px 0 8px'>Top matches</h3>")
        for job in data.inbox[:TOP]:
            score = "\u2013" if job.score is None else str(job.score)
            text.append(f"  {score}  {_title(job)}  {job.score_reason or ''}")
            body.append(
                "<div style='margin:0 0 12px'>"
                f"<b style='color:#2456c4'>{score}</b> <b>{html.escape(_title(job))}</b>"
                f"<div style='color:#5b6372'>{html.escape(job.score_reason or '')}</div>"
                f"<div>{_links(job)}</div></div>"
            )
        if len(data.inbox) > TOP:
            more = f"…and {len(data.inbox) - TOP} more in your Inbox."
            text.append("  " + more)
            body.append(f"<p style='color:#5b6372'>{html.escape(more)}</p>")

    if data.closing:
        text += ["", f"Closing in {CLOSING_DAYS} days"]
        body.append(
            f"<h3 style='font-size:15px;margin:16px 0 8px'>Closing in {CLOSING_DAYS} days</h3>"
        )
        for job in data.closing[:10]:
            text.append(f"  {_deadline(job)}  {_title(job)}")
            body.append(
                f"<div style='margin:0 0 8px'><b>{html.escape(_deadline(job))}</b> · "
                f"{html.escape(_title(job))} · {_links(job)}</div>"
            )

    if data.hidden:
        detail = ", ".join(f"{n} {kind}" for kind, n in sorted(data.hidden.items()))
        line = f"Hidden: {detail}. They're in Notion under Status = Hidden."
        text += ["", line]
        body.append(f"<p style='color:#5b6372'>{html.escape(line)}</p>")

    status = f"Gemini today: about ₹{data.spent_inr:.0f} of ₹{data.budget_inr:.0f} (list price)."
    if data.failed:
        status += f" {data.failed} task(s) failed: run `jobradar retry --failed`."
    text += ["", status]
    body.append(f"<p style='color:#5b6372;font-size:12px'>{html.escape(status)}</p>")
    if database_id:
        url = notion_url(database_id) or ""
        text.append(f"Notion: {url}")
        body.append(f"<p><a href='{html.escape(url, quote=True)}'>Open your Notion Inbox</a></p>")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.set_content("\n".join(text) + "\n\n— JobRadar\n")
    msg.add_alternative(
        "<div style='font-family:system-ui,sans-serif;font-size:14px;color:#16191f'>"
        + "".join(body)
        + "</div>",
        subtype="html",
    )
    return msg


class Digest:
    def __init__(
        self,
        settings: Settings,
        repo: Repo,
        queue: TaskQueue,
        clock: Clock = utcnow,
        preview_dir: Path = PREVIEW_DIR,
    ) -> None:
        self.settings = settings
        self.notify = settings.config.notify
        self.repo = repo
        self.queue = queue
        self.clock = clock
        self.preview_dir = preview_dir

    def _local_now(self) -> datetime:
        return self.clock().astimezone(self.notify.tz)

    def schedule_next(self) -> datetime:
        """Queue the next slot's digest (a no-op if it's already queued)."""
        now = self._local_now()
        at = next_slot(now, self.notify.digest_times)
        self.queue.enqueue(DIGEST, f"{at:%Y-%m-%d %H:%M}", delay=(at - now).total_seconds())
        return at

    async def send(self, task: TaskRecord) -> None:
        try:
            await self.send_now()
        finally:
            self.schedule_next()

    async def send_now(self) -> str:
        """Send (or preview) the digest for the window since the previous slot; returns
        what happened, for logs and the CLI."""
        now = self._local_now()
        since = previous_slot(now - timedelta(minutes=1), self.notify.digest_times)
        data = gather(self.repo, self.settings, since, now)
        if data.empty:
            log.info("digest skipped: nothing new", extra={"since": since.isoformat()})
            return "nothing new since the last digest: not sent"
        secrets = self.settings.secrets
        sender = secrets.email_address or "jobradar@localhost"
        msg = build_digest(
            data, sender, secrets.notify_address or sender, secrets.notion_database_id
        )
        if self.notify.dry_run:
            path = save_preview(msg, self.preview_dir / f"digest-{now:%Y%m%d-%H%M}.html")
            log.info(
                "dry run: digest saved", extra={"subject": msg["Subject"], "preview": str(path)}
            )
            return f"dry run: saved {path}"
        await asyncio.to_thread(smtp_send, self.settings, msg)
        log.info("digest sent", extra={"subject": msg["Subject"]})
        return f"sent: {msg['Subject']}"
