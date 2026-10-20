# ruff: noqa: E501  (inline-styled email HTML reads better one element per line)
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
from jobradar.config import ConfigError, Settings
from jobradar.db.models import Job, JobStatus, TaskStatus
from jobradar.db.repo import Repo, TaskRecord
from jobradar.pipeline.decide import hidden_kind
from jobradar.pipeline.skill_gaps import Gap, gap_line, skill_gaps
from jobradar.pipeline.task_types import DIGEST
from jobradar.profile import load_profile
from jobradar.queue.tasks import TaskQueue
from jobradar.sinks.email_actions import Actions
from jobradar.sinks.email_notify import (
    PREVIEW_DIR,
    dashboard_link,
    notion_url,
    save_preview,
    smtp_send,
)
from jobradar.sinks.email_template import (
    ACCENT,
    CARD,
    FAIR,
    GOOD,
    INK,
    LINE,
    MUTED,
    button,
    day_month,
    esc,
    heading,
    job_card,
    layout,
    stats_row,
)

log = logging.getLogger(__name__)

TOP = 5
GAPS_SENT = "skill_gaps_week"  # setting: the ISO week whose digest showed the skill gaps
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
    pipeline: dict[str, int] = field(default_factory=dict)  # applied/interview/offer now
    moved: dict[str, int] = field(default_factory=dict)  # statuses you set in the window
    boards: list[tuple[Job, str]] = field(default_factory=list)  # (job, "Greenhouse · Stripe")
    skill_gaps: list[Gap] = field(default_factory=list)  # once a week: skills you keep missing
    gap_jobs: int = 0  # good matches the gaps were counted over

    @property
    def empty(self) -> bool:
        return not (self.new or self.closing or self.failed)


def gather(
    repo: Repo, settings: Settings, since: datetime, until: datetime, bounded: bool = False
) -> DigestData:
    """What happened between `since` and `until`. `bounded`: leave out anything after
    `until` (an earlier digest, seen again on the dashboard); the email digest runs at
    `until`, so nothing comes after it."""
    end = to_iso(until) if bounded else None
    jobs = repo.jobs_created_since(to_iso(since), end)
    data = DigestData(since=since, until=until, new=len(jobs))
    data.inbox = sorted(
        (j for j in jobs if j.status == JobStatus.NEW.value),
        key=lambda j: (j.score is None, -(j.score or 0)),
    )
    for job in jobs:
        if job.status in (JobStatus.HIDDEN.value, JobStatus.DISCARDED.value):
            kind = hidden_kind(job.score_reason)
            data.hidden[kind] = data.hidden.get(kind, 0) + 1
    today = until.date()
    data.closing = repo.jobs_closing(
        today.isoformat(), (today + timedelta(days=CLOSING_DAYS)).isoformat()
    )
    data.alerts = repo.alerts_sent_since(to_iso(since), end)
    data.failed = repo.task_counts()[TaskStatus.FAILED]
    midnight = until.replace(hour=0, minute=0, second=0, microsecond=0)
    data.spent_inr = repo.llm_spend_since(to_iso(midnight))
    data.budget_inr = settings.config.llm.daily_budget_inr
    counts = repo.status_counts()
    data.pipeline = {s: counts.get(s, 0) for s in _PIPELINE}
    data.moved = repo.status_changes_since(to_iso(since), end)
    for job in data.inbox:
        board = next((s for s in repo.job_sightings(job.id) if s.platform == "board"), None)
        if board is not None:
            data.boards.append((job, board.chat_title or board.chat_id))
    return data


_PIPELINE = ("shortlisted", "applied", "interview", "offer")


def pipeline_line(data: DigestData) -> str | None:
    """'Your applications: 15 applied · 3 interviews · 0 offers (+2 applied today)'."""
    if not any(data.pipeline.values()):
        return None
    words = {"shortlisted": "shortlisted", "applied": "applied",
             "interview": "interviews", "offer": "offers"}  # fmt: skip
    line = " · ".join(f"{data.pipeline[s]} {words[s]}" for s in _PIPELINE)
    moved = [f"+{n} {s}" for s, n in data.moved.items() if s in _PIPELINE and n]
    return f"Your applications: {line}" + (f" ({', '.join(moved)} since then)" if moved else "")


def _title(job: Job) -> str:
    role = job.role or "Role not stated"
    return f"{role} @ {job.company}" if job.company else role


def part_of_day(at: datetime) -> str:
    return "Morning" if at.hour < 12 else "Afternoon" if at.hour < 17 else "Evening"


def build_digest(
    data: DigestData,
    sender: str,
    to: str,
    database_id: str | None,
    actions: Actions | None = None,
    dashboard: str | None = None,
) -> EmailMessage:
    part = part_of_day(data.until)
    strong = sum(1 for j in data.inbox if (j.score or 0) >= 75)
    hidden = sum(data.hidden.values())
    bits = [f"{data.new} new"]
    if strong:
        bits.append(f"{strong} strong")
    if data.closing:
        bits.append(f"{len(data.closing)} closing soon")
    subject = f"[JobRadar] {part} digest · " + " · ".join(bits)
    since = f"{data.since.day} {data.since:%b, %H:%M}"
    title = f"{part} digest"
    subtitle = f"{data.until:%A} {data.until.day} {data.until:%B} · since {since}"

    rows = [
        stats_row(
            [
                (str(data.new), "new jobs"),
                (str(len(data.inbox)), "in your Inbox"),
                (str(hidden), "hidden"),
                (str(data.alerts), "alerts"),
            ]
        )
    ]
    text = [f"{title} · since {since}", "",
            f"{data.new} new · {len(data.inbox)} in your Inbox · {hidden} hidden · "
            f"{data.alerts} alerts"]  # fmt: skip
    if line := pipeline_line(data):
        rows.append(
            f"<tr><td style='padding:12px 0 0;font-size:14px;color:{INK}'>"
            f"<b style='color:{GOOD[1]}'>{esc(line.split(':', 1)[0])}:</b>{esc(line.split(':', 1)[1])}</td></tr>"
        )
        text += ["", line]

    if data.inbox:
        rows.append(heading("Top matches"))
        text += ["", "TOP MATCHES"]
        for job in data.inbox[:TOP]:
            card, card_text = job_card(
                job, notion=notion_url(job.notion_page_id), compact=True,
                actions=actions.job_links(job) if actions else None, dashboard=dashboard,
            )  # fmt: skip
            rows.append(card)
            text.append(card_text)
        if len(data.inbox) > TOP:
            more = f"{len(data.inbox) - TOP} more in your Notion Inbox."
            rows.append(
                f"<tr><td style='font-size:13px;color:{MUTED};padding:0 0 4px'>{esc(more)}</td></tr>"
            )
            text.append(more)

    if data.boards:
        rows.append(heading("From company boards"))
        text += ["", "FROM COMPANY BOARDS"]
        items = "".join(
            f"<tr><td width='52' style='padding:10px 0 10px 14px;font-size:13px;font-weight:700;color:{GOOD[1] if (job.score or 0) >= 75 else MUTED}'>{esc(str(job.score) if job.score is not None else chr(8211))}</td>"
            f"<td style='padding:10px 14px 10px 8px;font-size:14px;color:{INK}'>{esc(_title(job))}"
            f"<div style='font-size:12px;color:{MUTED};margin-top:2px'>{esc(board)}</div></td>"
            f"<td align='right' style='padding:10px 14px 10px 0;font-size:13px;white-space:nowrap'>"
            + (
                f"<a href='{esc(job.canonical_url)}' style='color:{ACCENT};font-weight:600;text-decoration:none'>Apply</a>"
                if job.canonical_url
                else ""
            )
            + "</td></tr>"
            for job, board in data.boards[:10]
        )
        rows.append(
            f"<tr><td><table role='presentation' width='100%' cellpadding='0' cellspacing='0' "
            f"style='background:{CARD};border:1px solid {LINE};border-radius:12px'>{items}</table></td></tr>"
        )
        text += [
            f"  {job.score if job.score is not None else '-':>3}  {_title(job)} ({board})"
            for job, board in data.boards[:10]
        ]

    if data.closing:
        rows.append(heading(f"Closing in the next {CLOSING_DAYS} days"))
        text += ["", "CLOSING SOON"]
        items = "".join(
            f"<tr><td width='64' style='padding:10px 0 10px 14px;font-size:13px;font-weight:700;color:{FAIR[1]}'>{esc(day_month(job.deadline))}</td>"
            f"<td style='padding:10px 14px 10px 8px;font-size:14px;color:{INK}'>{esc(_title(job))}</td>"
            f"<td align='right' style='padding:10px 14px 10px 0;font-size:13px;white-space:nowrap'>"
            + (
                f"<a href='{esc(job.canonical_url)}' style='color:{ACCENT};font-weight:600;text-decoration:none'>Apply</a>"
                if job.canonical_url
                else ""
            )
            + "</td></tr>"
            for job in data.closing[:10]
        )
        rows.append(
            f"<tr><td><table role='presentation' width='100%' cellpadding='0' cellspacing='0' "
            f"style='background:{CARD};border:1px solid {LINE};border-radius:12px'>{items}</table></td></tr>"
        )
        text += [f"  {day_month(j.deadline)}  {_title(j)}" for j in data.closing[:10]]

    if data.skill_gaps:
        rows.append(heading("Skills you keep missing"))
        lead = (f"This week's {data.gap_jobs} good matches asked for these, and your profile "
                "doesn't list them. Worth learning, or adding if you already know them.")  # fmt: skip
        top = data.skill_gaps[0].jobs
        bars = "".join(
            f"<tr><td style='padding:5px 0 5px 14px;font-size:14px;color:{INK};white-space:nowrap'>{esc(g.name)}</td>"
            f"<td width='60%' style='padding:5px 10px'><div style='height:8px;border-radius:99px;background:#e8eefc'>"
            f"<div style='height:8px;border-radius:99px;background:{ACCENT};width:{round(100 * g.jobs / top)}%'></div></div></td>"
            f"<td align='right' style='padding:5px 14px 5px 0;font-size:13px;color:{MUTED};white-space:nowrap'>{g.jobs} jobs</td></tr>"
            for g in data.skill_gaps
        )
        rows.append(
            f"<tr><td style='font-size:13px;color:{MUTED};padding:0 0 8px'>{esc(lead)}</td></tr>"
        )
        rows.append(
            f"<tr><td><table role='presentation' width='100%' cellpadding='0' cellspacing='0' "
            f"style='background:{CARD};border:1px solid {LINE};border-radius:12px;padding:6px 0'>{bars}</table></td></tr>"
        )
        text += ["", "SKILLS YOU KEEP MISSING", lead, f"  {gap_line(data.skill_gaps)}"]

    if data.hidden:
        detail = ", ".join(f"{n} {kind}" for kind, n in sorted(data.hidden.items()))
        line = f"Hidden: {detail}. Find them in Notion under Status = Hidden."
        rows.append(
            f"<tr><td style='padding:18px 0 0;font-size:13px;color:{MUTED}'>{esc(line)}</td></tr>"
        )
        text += ["", line]

    if database_id and (url := notion_url(database_id)):
        rows.append(
            f"<tr><td style='padding:20px 0 0'>{button('Open your Notion Inbox', url)}</td></tr>"
        )
        text += ["", f"Notion: {url}"]

    status = f"Gemini today: about ₹{data.spent_inr:.0f} of your ₹{data.budget_inr:.0f} daily budget (list price)."
    if data.failed:
        status += f" {data.failed} task(s) failed: run <code>jobradar retry --failed</code>."
    footer = status + "<br>Sent by JobRadar to you only."
    text += ["", html.unescape(status.replace("<code>", "").replace("</code>", ""))]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.set_content("\n".join(text) + "\n\n— JobRadar\n")
    msg.add_alternative(
        layout(
            "JobRadar digest",
            title,
            subtitle,
            subject.removeprefix("[JobRadar] "),
            "".join(rows),
            footer,
        ),
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

    def _gaps(self, now: datetime) -> tuple[list[Gap], int]:
        try:
            profile = load_profile(self.settings.config.matching.profile_path)
        except ConfigError:
            return [], 0
        alert_above = self.settings.config.scoring.alert_above
        return skill_gaps(self.repo, profile, now - timedelta(days=7), alert_above)

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
        week = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
        if self.notify.skill_gaps and self.repo.get_setting(GAPS_SENT) != week:
            data.skill_gaps, data.gap_jobs = self._gaps(now)
        secrets = self.settings.secrets
        sender = secrets.email_address or "jobradar@localhost"
        msg = build_digest(
            data, sender, secrets.notify_address or sender, secrets.notion_database_id,
            Actions.load(self.settings, self.repo), dashboard_link(self.settings),
        )  # fmt: skip
        if self.notify.dry_run:
            path = save_preview(msg, self.preview_dir / f"digest-{now:%Y%m%d-%H%M}.html")
            log.info(
                "dry run: digest saved", extra={"subject": msg["Subject"], "preview": str(path)}
            )
            return f"dry run: saved {path}"
        await asyncio.to_thread(smtp_send, self.settings, msg)
        log.info("digest sent", extra={"subject": msg["Subject"]})
        if data.skill_gaps:
            self.repo.set_setting(GAPS_SENT, week)  # the first digest each week carries them
        return f"sent: {msg['Subject']}"
