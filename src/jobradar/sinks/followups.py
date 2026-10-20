"""Follow-up reminders: an email with a short follow-up draft when an application goes quiet.

A `followup_check` task runs every hour. Between 08:00 and 22:00 local time it looks at jobs
whose Status you set to Applied (and nothing since). One is due when `notify.
followup_after_days` (10) have passed since the latest of: the day you set Applied, the
Followed Up date you set in Notion, and the last reminder. At most `MAX_REMINDERS` (2) per
job; a later Status (Interview, Rejected…) ends them. Applications quiet for over
`QUIET_AFTER` days are left alone: by then a reply is unlikely and a nudge reads oddly.

The draft is written from your own data, never by an AI: the role, the company, the day you
applied, and up to three of the job's skills that are on the resume built for it (or, with no
resume, in your profile). So it can't claim anything you haven't said. JobRadar never sends
it: you send it to the recruiter (email or LinkedIn), then set Followed Up in Notion.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from jobradar.clock import Clock, from_iso, to_iso, utcnow
from jobradar.config import ConfigError, Settings
from jobradar.db.models import Job, JobStatus
from jobradar.db.repo import Repo, TaskRecord
from jobradar.pipeline.extract import own_post
from jobradar.pipeline.task_types import FOLLOWUP_CHECK
from jobradar.profile import load_profile
from jobradar.providers.skills_rule import compare_skills
from jobradar.queue.tasks import TaskQueue
from jobradar.sinks.email_actions import REPLIED, Actions, mailto, recruiter_email
from jobradar.sinks.email_notify import PREVIEW_DIR, notion_url, save_preview, smtp_send
from jobradar.sinks.email_template import (
    CARD,
    INK,
    LINE,
    MUTED,
    action_button,
    esc,
    job_card,
    layout,
    posting_of,
)
from jobradar.sinks.notion_sync import FOLLOWED_UP

log = logging.getLogger(__name__)

CHECK_SECONDS = 3600
WAKING_HOURS = range(8, 22)
MAX_REMINDERS = 2
QUIET_AFTER = timedelta(days=60)
SENT = "followup_reminder:"  # setting key prefix, per job: {"count": n, "last": iso}


@dataclass
class Due:
    job: Job
    applied: datetime  # local time
    quiet_days: int
    nth: int  # 1 or 2: which reminder this is


@dataclass
class Draft:
    subject: str
    body: str
    to: str | None = None  # the recruiter, when the job makes it clear


def _role(job: Job) -> str:
    return job.role or "the open role"


def _day(d: date) -> str:
    return f"{d.day} {d:%B}"


def draft(job: Job, applied: date, skills: list[str], basics: dict[str, Any], nth: int) -> Draft:
    """A short follow-up in plain words, from facts JobRadar has: nothing invented."""
    company = job.company or "your team"
    if nth == 1:
        fit = ""
        if skills:
            fit = f" My experience with {_join(skills[:3])} fits what the role asks for."
        lines = [
            "Hi,",
            "",
            f"I applied for the {_role(job)} role at {company} on {_day(applied)} and wanted to "
            f"follow up. I'm still very interested in the position.{fit}",
            "",
            "Is there any update on the next steps? I'm happy to share anything else you need.",
        ]
    else:
        lines = [
            "Hi,",
            "",
            f"I wanted to check in once more on my application for the {_role(job)} role at "
            f"{company}, sent on {_day(applied)}. I'm still keen on it and available to talk "
            "whenever suits you.",
        ]
    name = str(basics.get("name") or "").strip()
    contact = " | ".join(str(basics[k]).strip() for k in ("email", "phone") if basics.get(k))
    lines += ["", "Thanks,", *(x for x in (name, contact) if x)]
    subject = f"Following up: {_role(job)} application" + (f", {name}" if name else "")
    return Draft(subject, "\n".join(lines))


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


class FollowUpReminder:
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
        self.after = timedelta(days=self.notify.followup_after_days)
        self.repo = repo
        self.queue = queue
        self.clock = clock
        self.preview_dir = preview_dir

    def schedule(self, delay: float = 0, key: str = "a") -> None:
        self.queue.enqueue(FOLLOWUP_CHECK, key, delay=delay)

    async def tick(self, task: TaskRecord) -> None:
        try:
            await self.check_now()
        finally:
            self.schedule(CHECK_SECONDS, key="b" if task.key == "a" else "a")

    # ---- who's due ------------------------------------------------------------------------

    def _sent(self, job_id: str) -> tuple[int, datetime | None]:
        raw = self.repo.get_setting(SENT + job_id)
        if not raw:
            return 0, None
        data = json.loads(raw)
        return int(data.get("count", 0)), from_iso(data["last"]) if data.get("last") else None

    def status(self, job: Job) -> tuple[datetime, datetime, int]:
        """(applied, quiet since, reminders sent) for an Applied job."""
        applied = from_iso(job.user_status_at or job.updated_at)
        count, last = self._sent(job.id)
        since = [applied, *([last] if last else [])]
        if day := self.repo.get_setting(FOLLOWED_UP + job.id):
            local = datetime.fromisoformat(day).replace(tzinfo=self.notify.tz)
            since.append(local)
        return applied, max(since), count

    def due(self) -> list[Due]:
        now = self.clock()
        found = []
        for job in self.repo.jobs_applied():
            if self.repo.get_setting(REPLIED + job.id):
                continue  # the company wrote back: no nudging
            applied, since, count = self.status(job)
            if count >= MAX_REMINDERS or now - since < self.after or now - since > QUIET_AFTER:
                continue
            local = applied.astimezone(self.notify.tz)
            found.append(Due(job, local, (now - since).days, count + 1))
        return found

    # ---- sending --------------------------------------------------------------------------

    def _skills(self, job: Job, profile: dict[str, Any]) -> list[str]:
        """The job's skills that are on the resume built for it, else that you have."""
        if (artifact := self.repo.latest_artifact(job.id)) and artifact.content_json:
            ats = json.loads(artifact.content_json).get("ats") or {}
            if on_page := ats.get("skills_on_resume"):
                return [str(s) for s in on_page]
        posting = posting_of(job)
        if posting is None:
            return []
        have, _ = compare_skills(posting.skills_required + posting.skills_preferred, profile)
        return list(dict.fromkeys(have))

    def _draft(self, job: Job, applied: date, nth: int, profile: dict[str, Any]) -> Draft:
        dr = draft(job, applied, self._skills(job, profile), profile.get("basics") or {}, nth)
        posting = posting_of(job)
        dr.to = recruiter_email(job, posting.apply_url if posting else None,
                                own_post(self.repo, job.id))  # fmt: skip
        return dr

    def draft_for(self, job: Job) -> Draft | None:
        """The follow-up JobRadar would send you for an Applied job (the next one: first or
        second), whether or not it's due yet; None past the second or for other statuses."""
        if job.status != JobStatus.APPLIED.value or not job.user_status_at:
            return None
        applied, _, count = self.status(job)
        if count >= MAX_REMINDERS:
            return None
        local = applied.astimezone(self.notify.tz).date()
        return self._draft(job, local, count + 1, self._profile())

    def _profile(self) -> dict[str, Any]:
        try:
            return load_profile(self.settings.config.matching.profile_path)
        except ConfigError:
            return {}

    async def check_now(self) -> str:
        """Send (or preview) the reminders that are due; returns what happened."""
        local = self.clock().astimezone(self.notify.tz)
        if local.hour not in WAKING_HOURS:
            return "night: follow-up reminders wait for the morning"
        due = self.due()
        if not due:
            return "no application is due a follow-up"
        profile = self._profile()
        drafts = []
        for d in due:
            drafts.append(self._draft(d.job, d.applied.date(), d.nth, profile))
        secrets = self.settings.secrets
        sender = secrets.email_address or "jobradar@localhost"
        msg = build_followups(due, drafts, sender, secrets.notify_address or sender, self.after,
                              Actions.load(self.settings, self.repo))  # fmt: skip
        if self.notify.dry_run:
            path = save_preview(msg, self.preview_dir / f"followup-{local:%Y%m%d-%H%M}.html")
            log.info("dry run: follow-up reminder saved",
                     extra={"subject": msg["Subject"], "preview": str(path)})  # fmt: skip
            outcome = f"dry run: saved {path}"
        else:
            await asyncio.to_thread(smtp_send, self.settings, msg)
            log.info("follow-up reminder sent", extra={"jobs": len(due)})
            outcome = f"sent: {msg['Subject']}"
        now = to_iso(self.clock())
        for d in due:
            self.repo.set_setting(SENT + d.job.id, json.dumps({"count": d.nth, "last": now}))
        return outcome


def _title(job: Job) -> str:
    role = job.role or "Role not stated"
    return f"{role} @ {job.company}" if job.company else role


def _draft_html(note: str, dr: Draft) -> str:
    small = f"font-size:13px;color:{MUTED}"
    box = (
        f"white-space:pre-wrap;font:14px/1.5 Georgia,serif;color:{INK};background:{CARD};"
        f"border:1px dashed {LINE};border-radius:8px;padding:12px 14px;margin:0"
    )
    return (
        f"<div style='{small};margin:12px 0 6px'>{esc(note)}</div>"
        f"<div style='{small};margin:0 0 4px'>Subject: "
        f"<b style='color:{INK}'>{esc(dr.subject)}</b></div>"
        f"<pre style='{box}'>{esc(dr.body)}</pre>\n"
    )


def build_followups(
    due: list[Due],
    drafts: list[Draft],
    sender: str,
    to: str,
    after: timedelta,
    actions: Actions | None = None,
) -> EmailMessage:
    if len(due) == 1:
        subject = f"[JobRadar] Follow up: {_title(due[0].job)}"
        title = f"No reply in {due[0].quiet_days} days"
    else:
        subject = f"[JobRadar] {len(due)} applications to follow up"
        title = f"{len(due)} applications have gone quiet"
    done = "tap I followed up" if actions else "set Followed Up in Notion to today"
    subtitle = (
        "A short follow-up often gets a reply. Here's a draft for each: send it to the recruiter "
        f"by email or LinkedIn, then {done}."
    )
    rows, texts = [], [title, subtitle]
    for d, dr in zip(due, drafts, strict=True):
        send = "Send to recruiter" if dr.to else "Open in your mail app"
        buttons = [(send, mailto(dr.to or "", dr.subject, dr.body))]
        if actions:
            buttons.append(("I followed up", actions.link("followed up", d.job)))
        card, text = job_card(d.job, notion=notion_url(d.job.notion_page_id), compact=True)
        after_draft = "".join(action_button(label, url) for label, url in buttons)
        which = "first follow-up" if d.nth == 1 else "last follow-up (the second)"
        note = f"Applied {_day(d.applied.date())}, {d.quiet_days} days quiet: {which}"
        rows.append(
            card.replace(
                "</td></tr></table>\n</td></tr>",
                _draft_html(note, dr)
                + f"<div style='margin:10px 0 0'>{after_draft}</div>"
                + "</td></tr></table>\n</td></tr>",
                1,
            )
        )
        texts += ["", text, f"    {note}", f"    Subject: {dr.subject}", "",
                  *("    " + line for line in dr.body.splitlines()), "",
                  *(f"    {label}: {url}" for label, url in buttons)]  # fmt: skip
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.set_content("\n".join(texts) + "\n\n— JobRadar\n")
    footer = (
        f"Sent by JobRadar to you only, {after.days} days after Applied with no reply, at most "
        "twice per job. A new Status (Interview, Rejected) stops them. Turn these off with "
        "<code>notify.followups: false</code>."
    )
    msg.add_alternative(
        layout("JobRadar follow-up", title, subtitle, subject.removeprefix("[JobRadar] "),
               "".join(rows), footer),
        subtype="html",
    )  # fmt: skip
    return msg
