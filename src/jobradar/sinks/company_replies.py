# ruff: noqa: E501  (inline-styled email HTML reads better one element per line)
"""Replies from companies you applied to: an interview invite, an online test or a
rejection in your inbox becomes a suggested status, emailed to you with a one-tap button.

It runs inside the inbox check that reads the email buttons (`sinks/email_actions.py`), on
the same IMAP login, every 2 minutes. Only new mail is looked at (the last UID seen is kept),
and only the headers, until one looks like it's from a company with a job you set to Applied
or Interview:

- the sender's own domain carries the company's name (rubrik.com for Rubrik), or
- it's from a hiring system or test platform (greenhouse.io, myworkday.com, hackerrank.com,
  ...) and the company's name is in the sender's name or the subject.

Only then is that one email read, and its words decide, no AI:
- a rejection ("decided to move forward with other candidates", "regret to inform");
- an online test (HackerRank, Mettl, "coding assessment", "aptitude test");
- an interview invite ("schedule your interview", "next round", "shortlisted for").
Sentences that only promise one ("if you are shortlisted, we will contact you for an
interview") don't count, and "thank you for applying" alone is nothing.

JobRadar never changes the status itself: it emails you "Rubrik replied: interview invite?"
with the email's subject and a "Yes, set Interview" button (the same signed buttons as "I
applied"). Any reply stops follow-up reminders for that job.
"""

from __future__ import annotations

import email
import imaplib
import json
import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from email.message import EmailMessage, Message
from email.policy import default as default_policy
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from jobradar.clock import Clock, utcnow
from jobradar.config import Settings
from jobradar.db.models import Job, JobStatus
from jobradar.db.repo import Repo
from jobradar.pipeline.dedupe import norm
from jobradar.sinks.email_actions import PREFIX, REPLIED, Action, Actions
from jobradar.sinks.email_notify import PREVIEW_DIR, notion_url, save_preview, smtp_send
from jobradar.sinks.email_template import INK, MUTED, action_button, esc, job_card, layout

log = logging.getLogger(__name__)

Kind = Literal["interview", "test", "rejected"]
UID_SETTING = "company_replies_uid"  # "uidvalidity:last uid seen"
FIRST_LOOK_BACK = timedelta(days=7)
MAX_PER_SCAN = 300
MAX_SIZE = 1_500_000  # bytes: bigger emails (attachments) aren't read

# Hiring systems and test platforms that write on a company's behalf.
HIRING_SENDERS = {
    "greenhouse.io", "greenhouse-mail.io", "lever.co", "ashbyhq.com", "myworkday.com",
    "myworkdayjobs.com", "workday.com", "smartrecruiters.com", "icims.com", "oraclecloud.com",
    "successfactors.com", "successfactors.eu", "sapsf.com", "rippling.com", "zohorecruit.com",
    "zohorecruit.in", "keka.com", "darwinbox.com", "darwinbox.in", "freshteam.com",
    "hackerrank.com", "hackerearth.com", "codility.com", "mettl.com", "aspiringminds.com",
    "cocubes.com", "testgorilla.com", "imocha.io", "codesignal.com", "hirepro.in", "talview.com",
    "superset.com", "joinsuperset.com", "instahyre.com", "wellfound.com",
}  # fmt: skip
_PERSONAL = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "rediffmail.com"}
_SUFFIXES = {"technologies", "technology", "systems", "solutions", "software", "services",
             "labs", "india", "group", "corporation", "company", "global", "international",
             "consulting", "digital", "tech", "enterprises"}  # fmt: skip

_REJECT = re.compile(
    r"not (?:been )?(?:selected|shortlisted)|not (?:be )?(?:moving|progressing) (?:forward|ahead)"
    r"|decided (?:not )?to (?:move|proceed|go) (?:forward|ahead) with (?:other|another)"
    r"|(?:move|moving|proceed) forward with (?:other|another) candidate"
    r"|pursue other candidates|regret to inform|unable to (?:move forward|offer you|proceed)"
    r"|will not be (?:moving|proceeding|progressing|taking (?:your|this))"
    r"|position has (?:now )?been filled|no longer (?:under )?consider",
    re.IGNORECASE,
)
_TEST = re.compile(
    r"\b(?:hackerrank|hackerearth|codility|mettl|amcat|cocubes|testgorilla|imocha|codesignal"
    r"|hirepro|talview)\b|online (?:assessment|test)|coding (?:test|challenge|assessment|round)"
    r"|aptitude (?:test|round)|assessment link|(?:take|complete|attempt) (?:the|this|your) "
    r"(?:test|assessment)",
    re.IGNORECASE,
)
_INTERVIEW = re.compile(
    r"(?:invite|invitation|schedul\w*|slot|availability|calendly|book a time|confirm)\b"
    r"[^.\n]{0,60}\binterview|\binterview\b[^.\n]{0,60}\b(?:invite|invitation|scheduled|slot|"
    r"link|on (?:mon|tue|wed|thu|fri|sat|sun)\w*|at \d)|next round|technical round"
    r"|shortlisted for (?:the )?(?:next|interview|round|technical)"
    r"|would like to (?:invite you|schedule|speak with you|talk with you|meet you)",
    re.IGNORECASE,
)
_ONLY_IF = re.compile(r"\b(?:if|should you|in case|once)\b", re.IGNORECASE)
_SENTENCE = re.compile(r"(?<=[.!?\n])\s+")
_TAG = re.compile(r"<[^>]+>")


def classify(subject: str, body: str) -> Kind | None:
    """What a company's email means for your application, or None."""
    text = f"{subject}\n{body}"
    if _REJECT.search(text):
        return "rejected"
    # "If you are shortlisted, we'll invite you to an interview" promises nothing yet.
    firm = " ".join(s for s in _SENTENCE.split(text) if not _ONLY_IF.search(s))
    if _TEST.search(firm):
        return "test"
    if _INTERVIEW.search(firm):
        return "interview"
    return None


def company_key(name: str | None) -> str:
    """'EdgeVerve Systems Limited' -> 'edgeverve'; '' when too short to match on."""
    words = [w for w in norm(name).split() if w not in _SUFFIXES]
    key = "".join(words)
    return key if len(key) >= 4 else ""


def site(domain: str) -> str:
    """'mail.careers.rubrik.com' -> 'rubrik.com'; 'tcs.co.in' -> 'tcs.co.in'."""
    labels = domain.casefold().strip(".").split(".")
    if (
        len(labels) >= 3
        and labels[-2] in ("co", "com", "org", "net", "ac", "gov")
        and len(labels[-1]) == 2
    ):
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def match_job(sender: str, subject: str, jobs: list[Job]) -> Job | None:
    """The job (most recently moved first) a company's email is about, or None."""
    pairs = getaddresses([sender])
    if not pairs or "@" not in pairs[0][1]:
        return None
    display, address = pairs[0]
    domain = site(address.rpartition("@")[2])
    if domain in _PERSONAL:
        return None
    label = re.sub(r"[^a-z0-9]", "", domain.split(".")[0])
    said = re.sub(r"[^a-z0-9]", "", f"{display} {subject}".casefold())
    for job in jobs:
        key = company_key(job.company)
        if not key:
            continue
        if domain in HIRING_SENDERS:
            if key in said:
                return job
        elif key in label or (len(label) >= 5 and label in key):
            return job
    return None


def body_text(msg: Message) -> str:
    """The email's words: its plain-text part, or its HTML without tags."""
    part = msg.get_body(("plain", "html")) if isinstance(msg, EmailMessage) else None
    if part is None:
        return ""
    try:
        content = str(part.get_content())
    except (LookupError, ValueError):
        return ""
    if part.get_content_subtype() == "html":
        content = _TAG.sub(" ", re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", content))
    return " ".join(content.split())[:20_000]


@dataclass
class Reply:
    job: Job
    kind: Kind
    subject: str
    sender: str
    day: str
    message_id: str


class CompanyReplies:
    def __init__(
        self,
        settings: Settings,
        repo: Repo,
        actions: Actions,
        clock: Clock = utcnow,
        preview_dir: Path = PREVIEW_DIR,
    ) -> None:
        self.settings = settings
        self.notify = settings.config.notify
        self.repo = repo
        self.actions = actions
        self.clock = clock
        self.preview_dir = preview_dir
        self.own = {a.casefold() for a in (settings.secrets.email_address,
                                          settings.secrets.notify_address) if a}  # fmt: skip
        self.gmail = settings.config.email.imap_host.endswith("gmail.com")

    # ---- reading ----------------------------------------------------------------------

    def _new_uids(self, imap: imaplib.IMAP4_SSL) -> tuple[str, list[str]]:
        _, info = imap.status("INBOX", "(UIDVALIDITY)")
        found = re.search(rb"UIDVALIDITY (\d+)", info[0] or b"")
        validity = found.group(1).decode() if found else "0"
        saved = (self.repo.get_setting(UID_SETTING) or "").partition(":")
        if saved[0] == validity and saved[2].isdigit():
            last = int(saved[2])
            _, data = imap.uid("SEARCH", None, f"UID {last + 1}:*")  # type: ignore[arg-type]
            uids = [u for u in (data[0] or b"").decode().split() if int(u) > last]
        else:  # first run, or the mailbox was rebuilt: look back a week
            since = (self.clock() - FIRST_LOOK_BACK).date()
            _, data = imap.uid("SEARCH", None, f"SINCE {since:%d-%b-%Y}")  # type: ignore[arg-type]
            uids = (data[0] or b"").decode().split()
        return validity, uids[-MAX_PER_SCAN:]

    def scan(self, imap: imaplib.IMAP4_SSL) -> list[str]:
        """Look at mail that arrived since the last scan; returns one line per suggestion."""
        jobs = self.repo.jobs_you_moved_to([JobStatus.APPLIED, JobStatus.INTERVIEW])
        validity, uids = self._new_uids(imap)
        replies: list[Reply] = []
        for uid in uids if jobs else []:
            if (reply := self._read(imap, uid, jobs)) is not None:
                replies.append(reply)
        if uids:
            self.repo.set_setting(UID_SETTING, f"{validity}:{uids[-1]}")
        if not replies:
            return []
        for r in replies:
            self.repo.set_user_setting(REPLIED + r.job.id, json.dumps(
                {"kind": r.kind, "subject": r.subject[:200], "from": r.sender[:200],
                 "day": r.day, "message_id": r.message_id}))  # fmt: skip
            log.info("company replied", extra={"job_id": r.job.id[:10], "kind": r.kind})
        self._send(replies)
        return [f"{r.job.company} replied: {r.kind}" for r in replies]

    def _read(self, imap: imaplib.IMAP4_SSL, uid: str, jobs: list[Job]) -> Reply | None:
        _, parts = imap.uid(
            "FETCH", uid, "(RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])"
        )
        raw = next((p for p in parts if isinstance(p, tuple)), None)
        if raw is None:
            return None
        head = email.message_from_bytes(raw[1], policy=default_policy)
        sender, subject = str(head["From"] or ""), str(head["Subject"] or "")
        address = (getaddresses([sender]) or [("", "")])[0][1].casefold()
        if address in self.own or subject.startswith(("[JobRadar]", PREFIX)):
            return None
        job = match_job(sender, subject, jobs)
        if job is None:
            return None
        size = re.search(rb"RFC822\.SIZE (\d+)", raw[0])
        if size and int(size.group(1)) > MAX_SIZE:
            return None
        message_id = str(head["Message-ID"] or uid)
        if self._seen(job, message_id):
            return None
        _, full = imap.uid("FETCH", uid, "(BODY.PEEK[])")
        body = next((p[1] for p in full if isinstance(p, tuple)), b"")
        kind = classify(subject, body_text(email.message_from_bytes(body, policy=default_policy)))
        if kind is None or (job.status == JobStatus.INTERVIEW.value and kind != "rejected"):
            return None  # nothing new: a confirmation, or another round you already know of
        try:
            sent = parsedate_to_datetime(str(head["Date"])).astimezone(self.notify.tz)
            day = f"{sent.day} {sent:%b}"
        except (TypeError, ValueError):
            day = ""
        return Reply(job, kind, subject, sender, day, message_id)

    def _seen(self, job: Job, message_id: str) -> bool:
        raw = self.repo.get_user_setting(REPLIED + job.id)
        return raw is not None and json.loads(raw).get("message_id") == message_id

    # ---- telling you --------------------------------------------------------------------

    def _send(self, replies: list[Reply]) -> None:
        secrets = self.settings.secrets
        sender = secrets.email_address or "jobradar@localhost"
        msg = build_suggestions(replies, sender, secrets.notify_address or sender, self.actions,
                                self.gmail, self.settings.config.notion.enabled)  # fmt: skip
        if self.notify.dry_run:
            path = save_preview(
                msg, self.preview_dir / f"replies-{self.clock():%Y%m%d-%H%M%S}.html"
            )
            log.info("dry run: company reply saved", extra={"preview": str(path)})
            return
        smtp_send(self.settings, msg)
        log.info("company reply email sent", extra={"replies": len(replies)})


_SUGGEST: dict[Kind, tuple[str, Action]] = {
    "interview": ("interview invite", "interview"),
    "test": ("online test", "interview"),
    "rejected": ("rejection", "rejected"),
}


def build_suggestions(
    replies: list[Reply], sender: str, to: str, actions: Actions, gmail: bool, notion: bool = True
) -> EmailMessage:
    if len(replies) == 1:
        r = replies[0]
        subject = f"[JobRadar] {r.job.company} replied: {_SUGGEST[r.kind][0]}?"
        title = f"{r.job.company} replied"
    else:
        subject = f"[JobRadar] {len(replies)} companies replied"
        title = f"{len(replies)} companies replied"
    subtitle = ("It looks like news about an application. Check their email, then tap the button "
                f"to update {'Notion' if notion else 'the job'}. JobRadar never changes a status "
                "on its own.")  # fmt: skip
    rows, texts = [], [title, subtitle]
    for r in replies:
        label, action = _SUGGEST[r.kind]
        status = "Interview" if action == "interview" else "Rejected"
        buttons = [(f"Yes, set {status}", actions.link(action, r.job))]
        if gmail:
            mid = quote(r.message_id.strip().strip("<>"))
            buttons.append(
                ("Open their email", f"https://mail.google.com/mail/#search/rfc822msgid%3A{mid}")
            )
        card, text = job_card(r.job, notion=notion_url(r.job.notion_page_id, notion),
                              compact=True)  # fmt: skip
        quoted = (
            f"<div style='font-size:13px;color:{MUTED};margin:12px 0 4px'>Looks like: "
            f"<b style='color:{INK}'>{esc(label)}</b>{esc(', ' + r.day if r.day else '')}</div>"
            f"<div style='font-size:14px;color:{INK};margin:0 0 4px'>“{esc(r.subject)}”</div>"
            f"<div style='font-size:12px;color:{MUTED}'>{esc(r.sender)}</div>"
            f"<div style='margin:10px 0 0'>{''.join(action_button(lb, u) for lb, u in buttons)}</div>"
        )
        rows.append(card.replace("</td></tr></table>\n</td></tr>",
                                 quoted + "</td></tr></table>\n</td></tr>", 1))  # fmt: skip
        texts += ["", text, f"    Looks like: {label} ({r.day})", f"    “{r.subject}” from {r.sender}",
                  *(f"    {lb}: {u}" for lb, u in buttons)]  # fmt: skip
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.set_content("\n".join(texts) + "\n\n— JobRadar\n")
    footer = ("JobRadar read only this email, because it came from a company you applied to. "
              "Turn this off with <code>notify.company_replies: false</code>.")  # fmt: skip
    msg.add_alternative(
        layout("JobRadar", title, subtitle, subject.removeprefix("[JobRadar] "), "".join(rows),
               footer),
        subtype="html",
    )  # fmt: skip
    return msg
