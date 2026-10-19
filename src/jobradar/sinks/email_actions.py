"""Answer JobRadar's emails instead of opening Notion: "I applied", "Not interested",
"I followed up".

Each job card in an alert, digest or reminder email carries small buttons. A button is a
`mailto:` link to your own address with a ready subject such as

    JobRadar action: applied | Software Engineer @ Rubrik | 1a2b3c4d5e6f.0c9d8e7f

You tap it and press Send. A `reply_check` task reads the INBOX over IMAP every 2 minutes
(`EMAIL_ADDRESS` and its app password, the same login that sends alerts) and acts on these
emails: the job's Status becomes Applied or Skipped, or its Followed Up date is set, in
JobRadar and in Notion. The email is then archived (Gmail) or marked read.

Only emails that pass every check count, so nobody else can change your jobs:
- the subject carries the job id and a signature made with a key only this JobRadar has
  (kept in its database), so it must come from a button JobRadar sent you;
- it is from `EMAIL_ADDRESS` or `NOTIFY_TO`;
- the server's own Authentication-Results, when present, show no failed SPF, DKIM or DMARC.

Statuses only move forward: "I applied" works on a job you haven't moved past Applied,
"Not interested" on one you haven't applied to, "I followed up" on an Applied job.
"""

from __future__ import annotations

import asyncio
import contextlib
import email
import hashlib
import hmac
import imaplib
import logging
import re
import secrets
import ssl
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.policy import default as default_policy
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any, Literal, Protocol
from urllib.parse import quote

from jobradar.clock import Clock, utcnow
from jobradar.config import Settings
from jobradar.db.models import Job, JobStatus
from jobradar.db.repo import Repo, TaskRecord
from jobradar.pipeline.dedupe import sha1
from jobradar.pipeline.task_types import NOTION_STATUS, REPLY_CHECK
from jobradar.queue.tasks import PermanentError, RetryableError, TaskQueue
from jobradar.sinks.notion_sync import FOLLOWED_UP

log = logging.getLogger(__name__)

Action = Literal["applied", "skip", "followed up", "interview", "rejected"]
PREFIX = "JobRadar action:"
KEY_SETTING = "email_action_key"
DONE = "email_action_done:"  # setting key prefix, per message: handled already
REPLIED = "company_replied:"  # setting key prefix, per job: the company wrote back (JSON)
CHECK_SECONDS = 120
LOOK_BACK = timedelta(days=7)
ID_CHARS = 12
_SUBJECT = re.compile(
    r"JobRadar action:\s*(applied|skip|followed up|interview|rejected)\b.*?"
    r"\b([0-9a-f]{12})\.([0-9a-f]{8})\b",
    re.IGNORECASE | re.DOTALL,
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_HR_WORDS = re.compile(r"hr|career|recruit|talent|hiring|jobs|people|placement", re.IGNORECASE)

# What each action may change: the job's current status must be one of these.
_FROM: dict[str, set[str]] = {
    "applied": {s.value for s in (JobStatus.DISCOVERED, JobStatus.EXTRACTED, JobStatus.NEW,
                                  JobStatus.SHORTLISTED, JobStatus.RESUME_READY,
                                  JobStatus.HIDDEN, JobStatus.SKIPPED)},
    "skip": {s.value for s in (JobStatus.DISCOVERED, JobStatus.EXTRACTED, JobStatus.NEW,
                               JobStatus.SHORTLISTED, JobStatus.RESUME_READY, JobStatus.HIDDEN)},
    "followed up": {JobStatus.APPLIED.value},
    "interview": {s.value for s in (JobStatus.NEW, JobStatus.SHORTLISTED,
                                    JobStatus.RESUME_READY, JobStatus.APPLIED)},
    "rejected": {s.value for s in (JobStatus.NEW, JobStatus.SHORTLISTED, JobStatus.RESUME_READY,
                                   JobStatus.APPLIED, JobStatus.INTERVIEW)},
}  # fmt: skip
NOTION_OPTION = {"applied": "Applied", "skip": "Skipped", "interview": "Interview",
                 "rejected": "Rejected"}  # fmt: skip
_STATUS = {"applied": JobStatus.APPLIED, "skip": JobStatus.SKIPPED,
           "interview": JobStatus.INTERVIEW, "rejected": JobStatus.REJECTED}  # fmt: skip


def _title(job: Job) -> str:
    role = (job.role or "Role not stated")[:50]
    return f"{role} @ {job.company[:40]}" if job.company else role


def mailto(to: str, subject: str, body: str) -> str:
    return f"mailto:{quote(to, safe='@')}?subject={quote(subject)}&body={quote(body)}"


class Actions:
    """Makes the signed buttons and checks the emails they produce."""

    def __init__(self, key: str, address: str, also_from: str | None = None) -> None:
        self.key = key.encode()
        self.address = address
        self.senders = {a.casefold() for a in (address, also_from) if a}

    @classmethod
    def load(cls, settings: Settings, repo: Repo) -> Actions | None:
        """None when the buttons are off or there's no mailbox to read them from."""
        address = settings.secrets.email_address
        if not settings.config.notify.email_actions or not address:
            return None
        key = repo.get_setting(KEY_SETTING)
        if not key:
            key = secrets.token_hex(16)
            repo.set_setting(KEY_SETTING, key)
        return cls(key, address, settings.secrets.notify_address)

    def sign(self, action: str, job_id: str) -> str:
        msg = f"{action.casefold()}:{job_id[:ID_CHARS]}".encode()
        return hmac.new(self.key, msg, hashlib.sha256).hexdigest()[:8]

    def subject(self, action: Action, job: Job) -> str:
        token = f"{job.id[:ID_CHARS]}.{self.sign(action, job.id)}"
        return f"{PREFIX} {action} | {_title(job)} | {token}"

    def link(self, action: Action, job: Job) -> str:
        what = {"applied": "Applied", "skip": "Skipped (not interested)",
                "followed up": "followed up today", "interview": "Interview",
                "rejected": "Rejected"}[action]  # fmt: skip
        body = (
            f"Send this email as it is. JobRadar marks {_title(job)} as {what} in Notion within "
            "a few minutes, then archives this email."
        )
        return mailto(self.address, self.subject(action, job), body)

    def job_links(self, job: Job) -> list[tuple[str, str]]:
        """Buttons for a job you haven't applied to yet."""
        if job.status not in _FROM["applied"]:
            return []
        return [
            ("I applied", self.link("applied", job)),
            ("Not interested", self.link("skip", job)),
        ]

    def parse(self, subject: str) -> tuple[Action, str] | None:
        """(action, job id prefix) for a genuine button email; None for anything else."""
        m = _SUBJECT.search(subject or "")
        if not m:
            return None
        action, prefix, sig = m.group(1).casefold(), m.group(2), m.group(3)
        if not hmac.compare_digest(self.sign(action, prefix), sig):
            return None
        return action, prefix  # type: ignore[return-value]

    def trusted(self, sender: str, auth_results: str) -> bool:
        addresses = {a.casefold() for _, a in getaddresses([sender]) if a}
        if not addresses or not addresses <= self.senders:
            return False
        return not re.search(r"\b(?:spf|dkim|dmarc)=(?:fail|softfail)\b", auth_results or "", re.I)


def recruiter_email(job: Job, apply_url: str | None, post: str | None) -> str | None:
    """Where a follow-up can go, when it's clear: a mailto apply link, or an address in the
    job's own post that looks like HR or careers, or is on the company's own domain."""
    if apply_url and apply_url.lower().startswith("mailto:"):
        found = _EMAIL.search(apply_url)
        return str(found.group(0)) if found else None
    company = re.sub(r"[^a-z0-9]", "", (job.company or "").casefold())
    for address in dict.fromkeys(str(a) for a in _EMAIL.findall(post or "")):
        local, _, domain = address.partition("@")
        if domain.casefold() in ("gmail.com", "yahoo.com", "outlook.com", "hotmail.com"):
            continue
        squashed = re.sub(r"[^a-z0-9]", "", domain.casefold())
        if _HR_WORDS.search(local) or (len(company) >= 3 and company in squashed):
            return address
    return None


# ---- reading the mailbox ------------------------------------------------------------------


class InboxScanner(Protocol):
    """Something else that reads the inbox while JobRadar is logged in (company replies)."""

    def scan(self, imap: imaplib.IMAP4_SSL) -> list[str]: ...


@dataclass
class Incoming:
    uid: str
    message_id: str
    sender: str
    subject: str
    auth_results: str
    sent: datetime | None


def fetch(settings: Settings, since: date) -> tuple[imaplib.IMAP4_SSL, list[Incoming]]:
    """Log in and read the headers of recent button emails (the connection stays open)."""
    address, password = settings.secrets.email_address, settings.secrets.email_app_password
    if not address or password is None:
        raise PermanentError("EMAIL_ADDRESS / EMAIL_APP_PASSWORD missing in .env")
    host = settings.config.email.imap_host
    try:
        imap = imaplib.IMAP4_SSL(host, 993, ssl_context=ssl.create_default_context(), timeout=30)
    except OSError as e:
        raise RetryableError(f"IMAP connect to {host} failed: {e}") from e
    try:
        imap.login(address, password.get_secret_value())
    except imaplib.IMAP4.abort:
        raise  # the connection dropped mid-login: a retry, not a wrong password
    except imaplib.IMAP4.error as e:
        imap.logout()
        raise PermanentError(
            f"IMAP login rejected ({e}). In Gmail, check Settings > Forwarding and POP/IMAP > "
            "IMAP is enabled; the app password is the one used for alerts."
        ) from e
    imap.select("INBOX")
    _, data = imap.uid("SEARCH", None, f'(SINCE {since:%d-%b-%Y} SUBJECT "{PREFIX}")')  # type: ignore[arg-type]
    found = []
    for uid in (data[0] or b"").decode().split()[-200:]:
        _, parts = imap.uid(
            "FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID DATE "
            "AUTHENTICATION-RESULTS)])",
        )  # fmt: skip
        raw = next((p[1] for p in parts if isinstance(p, tuple)), b"")
        msg = email.message_from_bytes(raw, policy=default_policy)
        try:
            sent = parsedate_to_datetime(str(msg["Date"])) if msg["Date"] else None
        except (TypeError, ValueError):
            sent = None
        found.append(
            Incoming(uid, str(msg["Message-ID"] or uid), str(msg["From"] or ""),
                     str(msg["Subject"] or ""), str(msg["Authentication-Results"] or ""), sent)
        )  # fmt: skip
    return imap, found


def archive(imap: imaplib.IMAP4_SSL, uids: list[str]) -> None:
    """Gmail: take them out of the Inbox (still in All Mail). Elsewhere: mark them read."""
    gmail = "X-GM-EXT-1" in {str(c).upper() for c in imap.capabilities}
    for uid in uids:
        imap.uid("STORE", uid, "+FLAGS", "(\\Seen)")
        if gmail:
            imap.uid("STORE", uid, "-X-GM-LABELS", "(\\Inbox)")


class ReplyReader:
    def __init__(
        self,
        settings: Settings,
        repo: Repo,
        queue: TaskQueue,
        actions: Actions,
        clock: Clock = utcnow,
        companies: InboxScanner | None = None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.queue = queue
        self.actions = actions
        self.clock = clock
        self.tz = settings.config.notify.tz
        self.companies = companies  # replies from companies you applied to (same login)

    def schedule(self, delay: float = 0, key: str = "a") -> None:
        self.queue.enqueue(REPLY_CHECK, key, delay=delay)

    async def tick(self, task: TaskRecord) -> None:
        try:
            await self.check_now()
        finally:
            self.schedule(CHECK_SECONDS, key="b" if task.key == "a" else "a")

    async def check_now(self) -> list[str]:
        """Read the inbox once; returns what was done, one line per email acted on."""
        return await asyncio.to_thread(self._check)

    def _check(self) -> list[str]:
        since = (self.clock() - LOOK_BACK).date()
        try:
            imap, incoming = fetch(self.settings, since)
        except (OSError, imaplib.IMAP4.abort) as e:  # Gmail timed out or dropped the line
            raise RetryableError(f"inbox not reachable right now: {e}") from e
        done: list[str] = []
        handled: list[str] = []
        try:
            for mail in incoming:
                key = DONE + sha1(mail.message_id)[:16]
                if self.repo.get_setting(key):
                    handled.append(mail.uid)
                    continue
                outcome = self.apply(mail)
                if outcome is None:
                    continue  # not a genuine button email: left alone in the inbox
                self.repo.set_setting(key, outcome)
                handled.append(mail.uid)
                done.append(outcome)
            if handled:
                archive(imap, handled)
            if self.companies is not None:
                try:
                    done += self.companies.scan(imap)
                except (OSError, imaplib.IMAP4.error) as e:  # the buttons still count
                    log.warning("company replies not read this time", extra={"error": str(e)})
        finally:
            with contextlib.suppress(imaplib.IMAP4.error, OSError):
                imap.logout()
        return done

    def apply(self, mail: Incoming) -> str | None:
        parsed = self.actions.parse(mail.subject)
        if parsed is None:
            return None
        if not self.actions.trusted(mail.sender, mail.auth_results):
            log.warning("button email not from you; ignored", extra={"from": mail.sender[:80]})
            return None
        action, prefix = parsed
        matches = self.repo.jobs_with_prefix(prefix)
        job = self.repo.get_job(matches[0]) if len(matches) == 1 else None
        if job is None:
            return f"{action}: job {prefix} not found"
        title = _title(job)
        if job.status not in _FROM[action]:
            about = {"job_id": job.id[:10], "action": action, "status": job.status}
            log.info("button email ignored: status has moved on", extra=about)
            return f"{action} ignored for {title}: it's {job.status} now"
        payload: dict[str, Any]
        if action == "followed up":
            day = (mail.sent or self.clock()).astimezone(self.tz).date().isoformat()
            self.repo.set_setting(FOLLOWED_UP + job.id, day)
            payload = {"followed_up": day}
        else:
            self.repo.set_user_status(job.id, _STATUS[action])
            payload = {"status": NOTION_OPTION[action]}
        self.queue.enqueue(NOTION_STATUS, job.id, payload)
        log.info("your email action", extra={"job_id": job.id[:10], "action": action})
        return f"{action}: {title}"
