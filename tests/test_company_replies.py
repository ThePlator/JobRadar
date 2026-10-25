import json
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

from jobradar.db.models import JobStatus
from jobradar.db.repo import Repo
from jobradar.queue.tasks import TaskQueue
from jobradar.sinks import company_replies as replies_mod
from jobradar.sinks.company_replies import (
    UID_SETTING,
    CompanyReplies,
    classify,
    company_key,
    match_job,
    site,
)
from jobradar.sinks.email_actions import REPLIED, Actions, ReplyReader
from tests.conftest import FakeClock
from tests.test_digest import settings

ME = "me@example.com"


def add(repo: Repo, job_id: str, company: str, status: JobStatus = JobStatus.APPLIED) -> None:
    repo.get_or_create_job(job_id, f"https://acme.com/{job_id}")
    repo.save_extraction(job_id, "{}", company, "Software Engineer", None, "rules-v1",
                         JobStatus.EXTRACTED)  # fmt: skip
    repo.set_job_status(job_id, JobStatus.NEW)
    repo.set_user_status(job_id, status)


# ---- reading the words ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subject", "body", "kind"),
    [
        (
            "Interview invitation: Software Engineer",
            "Please pick a slot for your interview.",
            "interview",
        ),
        ("Next steps", "We'd like to schedule your interview on Monday at 11 am.", "interview"),
        ("Update", "Congratulations! You are shortlisted for the technical round.", "interview"),
        (
            "Rubrik assessment",
            "Please complete the HackerRank coding test within 48 hours.",
            "test",
        ),
        (
            "Your application",
            "We regret to inform you that we will not be moving forward.",
            "rejected",
        ),
        (
            "Update on your application",
            "After careful review of the interview process, we have "
            "decided to move forward with other candidates.",
            "rejected",
        ),
        (
            "Thank you for applying",
            "We have received your application. If you are shortlisted, "
            "we will contact you to schedule an interview.",
            None,
        ),
        ("Thank you for applying", "Our team will review your profile.", None),
        ("New jobs at Rubrik", "10 new roles match your search.", None),
    ],
)
def test_classify(subject: str, body: str, kind: str | None) -> None:
    assert classify(subject, body) == kind


def test_company_key_and_site() -> None:
    assert company_key("EdgeVerve Systems Limited") == "edgeverve"
    assert company_key("Thermo Fisher Scientific") == "thermofisherscientific"
    assert company_key("HP") == ""  # too short to match on safely
    assert site("mail.careers.rubrik.com") == "rubrik.com"
    assert site("hr.tcs.co.in") == "tcs.co.in"


def test_match_job(repo: Repo) -> None:
    add(repo, "r", "Rubrik")
    add(repo, "t", "Thermo Fisher Scientific")
    jobs = repo.jobs_you_moved_to([JobStatus.APPLIED])
    ids = lambda sender, subject="": j.id if (j := match_job(sender, subject, jobs)) else None  # noqa: E731
    assert ids("Rubrik Talent <talent@mail.rubrik.com>") == "r"
    assert ids("no-reply@thermofisher.com") == "t"  # the domain is the start of the name
    assert ids("Rubrik Recruiting <no-reply@greenhouse.io>") == "r"  # a hiring system
    assert ids("no-reply@hackerrank.com", "Rubrik invites you to a test") == "r"
    assert ids("no-reply@greenhouse.io", "Your application to Acme") is None
    assert ids("Rubrik HR <rubrik.hr@gmail.com>") is None  # personal mail never matches
    assert ids("news@linkedin.com", "Jobs at Rubrik") is None  # LinkedIn isn't a hiring system here


# ---- the inbox scan --------------------------------------------------------------------------


def mail(uid: int, sender: str, subject: str, body: str) -> bytes:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, ME, subject
    msg["Message-ID"] = f"<m{uid}@x>"
    msg["Date"] = "Mon, 19 Oct 2026 10:00:00 +0000"
    msg.set_content(body)
    return bytes(msg)


class FakeImap:
    def __init__(self, messages: dict[int, bytes]) -> None:
        self.messages = messages
        self.read_bodies: list[int] = []
        self.searches: list[str] = []

    def status(self, box: str, what: str) -> tuple[str, list[bytes]]:
        return "OK", [b'"INBOX" (UIDVALIDITY 7)']

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        if command == "SEARCH":
            self.searches.append(args[1])
            return "OK", [" ".join(str(u) for u in sorted(self.messages)).encode()]
        uid, what = int(args[0]), args[1]
        raw = self.messages[uid]
        if "HEADER.FIELDS" in what:
            head = raw.split(b"\n\n", 1)[0]
            return "OK", [(f"{uid} (RFC822.SIZE {len(raw)} BODY[HEADER]".encode(), head), b")"]
        self.read_bodies.append(uid)
        return "OK", [(f"{uid} (BODY[]".encode(), raw), b")"]


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[EmailMessage]:
    out: list[EmailMessage] = []
    monkeypatch.setattr(replies_mod, "smtp_send", lambda s, m: out.append(m))
    return out


def scanner(repo: Repo, clock: FakeClock, tmp_path: Path) -> CompanyReplies:
    return CompanyReplies(
        settings(), repo, Actions("k" * 32, ME), clock=clock, preview_dir=tmp_path
    )


def test_suggests_a_status_from_a_company_email(
    repo: Repo, clock: FakeClock, tmp_path: Path, sent: list[EmailMessage]
) -> None:
    add(repo, "a" * 40, "Rubrik")
    imap = FakeImap({
        1: mail(1, "Rubrik Talent <talent@rubrik.com>", "Interview invitation",
                "Please choose a slot for your interview."),
        2: mail(2, "news@somestore.com", "Big sale", "Interview season deals"),
        3: mail(3, ME, "JobRadar action: applied | x | aaaaaaaaaaaa.00000000", "Send this"),
    })  # fmt: skip
    s = scanner(repo, clock, tmp_path)
    assert s.scan(imap) == ["Rubrik replied: interview"]  # type: ignore[arg-type]
    assert imap.read_bodies == [1]  # only the company's email was opened
    assert repo.get_setting(UID_SETTING) == "7:3"
    assert json.loads(repo.get_user_setting(REPLIED + "a" * 40) or "{}")["kind"] == "interview"
    assert sent[0]["Subject"] == "[JobRadar] Rubrik replied: interview invite?"
    plain = str(sent[0].get_body(("plain",)).get_content())  # type: ignore[union-attr]
    assert "Yes, set Interview: mailto:" in plain and "“Interview invitation”" in plain
    job = repo.get_job("a" * 40)
    assert job is not None and job.status == JobStatus.APPLIED.value  # only suggested

    assert s.scan(imap) == []  # nothing new since UID 3 (IMAP returns the last one anyway)
    assert imap.searches[-1] == "UID 4:*" and len(sent) == 1


def test_after_an_interview_only_a_rejection_is_news(
    repo: Repo, clock: FakeClock, tmp_path: Path, sent: list[EmailMessage]
) -> None:
    add(repo, "b" * 40, "Rubrik", JobStatus.INTERVIEW)
    imap = FakeImap({
        1: mail(1, "talent@rubrik.com", "Round 2", "We'd like to schedule your interview."),
        2: mail(2, "talent@rubrik.com", "Update", "We regret to inform you that we will not be "
                "moving forward."),
    })  # fmt: skip
    assert scanner(repo, clock, tmp_path).scan(imap) == ["Rubrik replied: rejected"]  # type: ignore[arg-type]
    assert sent[0]["Subject"] == "[JobRadar] Rubrik replied: rejection?"


def test_nothing_applied_reads_nothing(repo: Repo, clock: FakeClock, tmp_path: Path) -> None:
    imap = FakeImap({1: mail(1, "talent@rubrik.com", "Interview", "Schedule your interview.")})
    assert scanner(repo, clock, tmp_path).scan(imap) == []  # type: ignore[arg-type]
    assert imap.read_bodies == [] and repo.get_setting(UID_SETTING) == "7:1"


# ---- the buttons and follow-ups ----------------------------------------------------------------


async def test_yes_set_interview_button(repo: Repo, queue: TaskQueue, clock: FakeClock) -> None:
    from jobradar.sinks.email_actions import Incoming

    add(repo, "c" * 40, "Rubrik")
    a = Actions("k" * 32, ME)
    job = repo.get_job("c" * 40)
    assert job is not None
    from urllib.parse import parse_qs, unquote, urlsplit

    subject = unquote(parse_qs(urlsplit(a.link("interview", job)).query)["subject"][0])
    reader = ReplyReader(settings(), repo, queue, a, clock=clock)
    assert reader.apply(Incoming("1", "<x>", ME, subject, "", None)) == (
        "interview: Software Engineer @ Rubrik"
    )
    job = repo.get_job("c" * 40)
    assert job is not None and job.status == JobStatus.INTERVIEW.value


async def test_a_reply_stops_follow_ups(repo: Repo, queue: TaskQueue, clock: FakeClock,
                                        tmp_path: Path) -> None:  # fmt: skip
    from tests.test_followups import make

    add(repo, "d" * 40, "Rubrik")
    clock.advance(11 * 86400)
    f = make(repo, queue, clock, tmp_path)
    assert [d.job.id for d in f.due()] == ["d" * 40]
    repo.set_user_setting(REPLIED + "d" * 40, json.dumps({"kind": "test"}))
    assert f.due() == []
