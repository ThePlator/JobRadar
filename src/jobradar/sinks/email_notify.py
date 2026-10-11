"""Email alerts over SMTP, sent only to NOTIFY_TO (or EMAIL_ADDRESS).

- One alert per job, ever (`alert` table).
- Alerts queued within `notify.alert_batch_minutes` go out as one email: the first alert
  schedules the send, later ones join it.
- At most `notify.max_alerts_per_day` per local day; the rest are marked `capped`.
- `notify.dry_run`: nothing is sent; the email is logged and saved to data/alerts/*.html.
"""

from __future__ import annotations

import asyncio
import html
import logging
import smtplib
import ssl
from collections.abc import Callable
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from jobradar.clock import Clock, to_iso, utcnow
from jobradar.config import Settings
from jobradar.db.models import Job
from jobradar.db.repo import Repo, TaskRecord
from jobradar.pipeline.schemas import JobPosting
from jobradar.pipeline.task_types import SEND_ALERTS
from jobradar.queue.tasks import PermanentError, RetryableError, TaskQueue

log = logging.getLogger(__name__)

PREVIEW_DIR = Path("data/alerts")
BATCH_KEY = "batch"

Send = Callable[[EmailMessage], None]


def notion_url(page_id: str | None) -> str | None:
    return f"https://www.notion.so/{page_id.replace('-', '')}" if page_id else None


def _posting(job: Job) -> JobPosting | None:
    try:
        return JobPosting.model_validate_json(job.data_json) if job.data_json else None
    except ValueError:
        return None


def _title(job: Job) -> str:
    role = job.role or "A job"
    return f"{role} @ {job.company}" if job.company else role


def _closes(job: Job) -> str:
    if not job.deadline:
        return ""
    try:
        day = datetime.fromisoformat(job.deadline)
        return f" · closes {day.day} {day:%b}"
    except ValueError:
        return ""


def subject(jobs: list[Job]) -> str:
    if len(jobs) == 1:
        job = jobs[0]
        return f"[JobRadar {job.score}] {_title(job)}{_closes(job)}"
    best = max(jobs, key=lambda j: j.score or 0)
    return f"[JobRadar] {len(jobs)} strong matches · best: {_title(best)} ({best.score})"


def build_email(jobs: list[Job], sender: str, to: str) -> EmailMessage:
    """Plain text and HTML versions of one alert email covering these jobs."""
    text_parts: list[str] = []
    html_parts: list[str] = []
    for job in jobs:
        posting = _posting(job)
        apply = (posting.apply_url if posting else None) or job.canonical_url
        page = notion_url(job.notion_page_id)
        lines = [f"{_title(job)} · {job.score}{_closes(job)}", job.score_reason or ""]
        if apply:
            lines.append(f"Apply: {apply}")
        if page:
            lines.append(f"Notion: {page}")
        text_parts.append("\n".join(line for line in lines if line))

        links = []
        if apply:
            links.append(f'<a href="{html.escape(apply, quote=True)}">Apply</a>')
        if page:
            links.append(f'<a href="{html.escape(page, quote=True)}">Open in Notion</a>')
        html_parts.append(
            "<div style='margin:0 0 20px'>"
            f"<div style='font-size:16px;font-weight:600'>{html.escape(_title(job))}"
            f" <span style='color:#2456c4'>{job.score}</span>"
            f"<span style='color:#5b6372;font-weight:400'>{html.escape(_closes(job))}</span></div>"
            f"<div style='color:#5b6372;margin:4px 0'>{html.escape(job.score_reason or '')}</div>"
            f"<div>{' · '.join(links)}</div></div>"
        )
    msg = EmailMessage()
    msg["Subject"] = subject(jobs)
    msg["From"] = sender
    msg["To"] = to
    msg.set_content("\n\n".join(text_parts) + "\n\n— JobRadar\n")
    msg.add_alternative(
        "<div style='font-family:system-ui,sans-serif;font-size:14px;color:#16191f'>"
        + "".join(html_parts)
        + "<div style='color:#5b6372;font-size:12px'>Sent by JobRadar to you only.</div></div>",
        subtype="html",
    )
    return msg


class AlertSender:
    def __init__(
        self,
        settings: Settings,
        repo: Repo,
        queue: TaskQueue,
        clock: Clock = utcnow,
        send: Send | None = None,
        preview_dir: Path = PREVIEW_DIR,
    ) -> None:
        self.settings = settings
        self.notify = settings.config.notify
        self.secrets = settings.secrets
        self.repo = repo
        self.queue = queue
        self.clock = clock
        self._send = send or self._smtp
        self.preview_dir = preview_dir

    def queue_alert(self, job_id: str) -> None:
        """Called by the decide step. A job is alerted at most once."""
        if self.repo.queue_alert(job_id):
            # A pending send keeps its earlier time, so later alerts join the same email.
            self.queue.enqueue(SEND_ALERTS, BATCH_KEY, delay=self.notify.alert_batch_minutes * 60)

    def _since_midnight(self) -> str:
        local = self.clock().astimezone(self.notify.tz)
        return to_iso(local.replace(hour=0, minute=0, second=0, microsecond=0))

    async def send(self, task: TaskRecord) -> None:
        pending = self.repo.pending_alerts()
        room = max(
            self.notify.max_alerts_per_day - self.repo.alerts_sent_since(self._since_midnight()), 0
        )
        batch, capped = pending[:room], pending[room:]
        if capped:
            log.info("daily alert limit reached", extra={"skipped": len(capped)})
            self.repo.finish_alerts(capped, "capped")
        jobs = [job for job_id in batch if (job := self.repo.get_job(job_id)) is not None]
        if not jobs:
            return
        sender = self.secrets.email_address or "jobradar@localhost"
        to = self.secrets.notify_address or sender
        msg = build_email(jobs, sender, to)
        if self.notify.dry_run:
            path = save_preview(msg, self.preview_dir / f"{self.clock():%Y%m%d-%H%M%S}.html")
            log.info(
                "dry run: would have emailed",
                extra={"subject": msg["Subject"], "preview": str(path)},
            )
            self.repo.finish_alerts([j.id for j in jobs], "dry_run")
            return
        await asyncio.to_thread(self._send, msg)
        log.info("alert email sent", extra={"jobs": len(jobs)})
        self.repo.finish_alerts([j.id for j in jobs], "sent")

    def _smtp(self, msg: EmailMessage) -> None:
        smtp_send(self.settings, msg)


def save_preview(msg: EmailMessage, path: Path) -> Path:
    """Dry run: the email as an HTML file you can open in a browser."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = msg.get_body(("html",))
    path.write_text(
        f"<h3>{html.escape(msg['Subject'])}</h3>" + (body.get_content() if body else "")
    )
    return path


def smtp_send(settings: Settings, msg: EmailMessage) -> None:
    """Send over SMTP with STARTTLS as EMAIL_ADDRESS."""
    secrets, email = settings.secrets, settings.config.email
    address, password = secrets.email_address, secrets.email_app_password
    if not address or password is None:
        raise PermanentError("EMAIL_ADDRESS / EMAIL_APP_PASSWORD missing in .env")
    try:
        with smtplib.SMTP(email.smtp_host, email.smtp_port, timeout=30) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(address, password.get_secret_value())
            smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        # Retrying a rejected password only risks a lockout.
        raise PermanentError("email login rejected: check EMAIL_APP_PASSWORD") from e
    except (smtplib.SMTPException, OSError) as e:
        raise RetryableError(f"could not send email: {e}") from e


def test_email(settings: Settings) -> EmailMessage:
    """What an alert looks like, with a made-up job, for `jobradar alerts --test`."""
    example = Job(
        id="example", role="Backend Engineer (example)", company="JobRadar test", score=88,
        score_reason="Good fit · 88 · eligible ✓ · Python, SQL ✓ · missing Kafka · Remote ✓",
        canonical_url="https://github.com/ThePlator/JobRadar", created_at="", updated_at="",
    )  # fmt: skip
    sender = settings.secrets.email_address or ""
    msg = build_email([example], sender, settings.secrets.notify_address or sender)
    msg.replace_header("Subject", "[JobRadar] Test email: alerts reach you")
    return msg
