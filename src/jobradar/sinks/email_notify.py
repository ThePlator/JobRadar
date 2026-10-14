"""Email alerts over SMTP, sent only to NOTIFY_TO (or EMAIL_ADDRESS).

- One alert per job, ever (`alert` table).
- The first alert goes out at once. Alerts that follow within `notify.alert_batch_minutes`
  of an email wait and go out together when that window ends, so a burst of posts gives
  you one follow-up rather than one email each.
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
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

from jobradar.clock import Clock, from_iso, to_iso, utcnow
from jobradar.config import Settings
from jobradar.db.models import Job
from jobradar.db.repo import Repo, TaskRecord
from jobradar.pipeline.task_types import FRESH, SEND_ALERTS
from jobradar.queue.tasks import PermanentError, RetryableError, TaskQueue
from jobradar.sinks.email_template import day_month, job_card, layout

log = logging.getLogger(__name__)

PREVIEW_DIR = Path("data/alerts")
BATCH_KEY = "batch"

Send = Callable[[EmailMessage], None]


def notion_url(page_id: str | None) -> str | None:
    return f"https://www.notion.so/{page_id.replace('-', '')}" if page_id else None


def _title(job: Job) -> str:
    role = job.role or "Role not stated"
    return f"{role} @ {job.company}" if job.company else role


def _closes(job: Job) -> str:
    closes = day_month(job.deadline)
    return f" · closes {closes}" if closes else ""


def subject(jobs: list[Job]) -> str:
    if len(jobs) == 1:
        job = jobs[0]
        return f"[JobRadar {job.score}] {_title(job)}{_closes(job)}"
    best = max(jobs, key=lambda j: j.score or 0)
    return f"[JobRadar] {len(jobs)} strong matches · best: {_title(best)} ({best.score})"


def where_posted(chat: str | None, platform: str = "telegram") -> str:
    """'in Freshershunt', or 'on Stripe's Greenhouse board' for a company board."""
    if not chat:
        return ""
    if platform == "board" and " · " in chat:
        ats, company = chat.split(" · ", 1)
        return f"on {company}'s {ats} board"
    return f"in {chat}"


def posted_ago(posted_at: str, chat: str | None, now: datetime, platform: str = "telegram") -> str:
    """'posted 3 min ago in Freshershunt' / '… on Stripe's Greenhouse board'."""
    minutes = max(int((now - from_iso(posted_at)).total_seconds() // 60), 0)
    if minutes < 1:
        age = "just now"
    elif minutes < 60:
        age = f"{minutes} min ago"
    elif minutes < 48 * 60:
        age = f"{minutes // 60} h ago"
    else:
        age = f"{minutes // (24 * 60)} days ago"
    where = where_posted(chat, platform)
    return f"posted {age}" + (f" {where}" if where else "")


def build_email(
    jobs: list[Job], sender: str, to: str, seen: dict[str, str] | None = None
) -> EmailMessage:
    """One alert email covering these jobs, as HTML and plain text. `seen` maps a job id to
    where and when it was posted."""
    seen = seen or {}
    cards = [job_card(job, seen.get(job.id, ""), notion_url(job.notion_page_id)) for job in jobs]
    if len(jobs) == 1:
        title = "New strong match"
        subtitle = seen.get(jobs[0].id, "") or "Apply early: fresh posts get read first."
        if subtitle.startswith("posted"):
            subtitle = f"Apply early: it was {subtitle}."
    else:
        title = f"{len(jobs)} new strong matches"
        subtitle = "Best first. Apply early: fresh posts get read first."
    ordered = sorted(zip(jobs, cards, strict=True), key=lambda pair: -(pair[0].score or 0))
    footer = (
        "Sent by JobRadar to you only. Each job alerts once; you get alerts for new jobs "
        "scoring at or above <code>scoring.alert_above</code>."
    )
    msg = EmailMessage()
    msg["Subject"] = subject(jobs)
    msg["From"] = sender
    msg["To"] = to
    msg.set_content(
        f"{title}\n\n" + "\n\n".join(text for _, (_, text) in ordered) + "\n\n— JobRadar\n"
    )
    msg.add_alternative(
        layout(
            "JobRadar alert",
            title,
            subtitle,
            subject(jobs).removeprefix("[JobRadar] "),
            "".join(card for _, (card, _) in ordered),
            footer,
        ),
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
        if not self.repo.queue_alert(job_id):
            return
        # Send now, unless an email went out within the window: then wait for its end, and
        # every alert arriving meanwhile joins that one follow-up (a pending send keeps its
        # earlier time).
        wait = 0.0
        if last := self.repo.last_alert_sent_at():
            window_ends = from_iso(last) + timedelta(minutes=self.notify.alert_batch_minutes)
            wait = max((window_ends - self.clock()).total_seconds(), 0.0)
        self.queue.enqueue(SEND_ALERTS, BATCH_KEY, delay=wait, priority=FRESH)

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
        msg = build_email(jobs, sender, to, self._seen(jobs))
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

    def _seen(self, jobs: list[Job]) -> dict[str, str]:
        seen: dict[str, str] = {}
        now = self.clock()
        for job in jobs:
            if sightings := self.repo.job_sightings(job.id):
                first = sightings[0]
                seen[job.id] = posted_ago(first.posted_at, first.chat_title, now, first.platform)
        return seen

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
