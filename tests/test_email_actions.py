import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
import respx

from jobradar.db.models import Job, JobStatus
from jobradar.db.repo import Repo
from jobradar.pipeline.task_types import NOTION_STATUS
from jobradar.queue.tasks import TaskQueue
from jobradar.sinks import email_actions
from jobradar.sinks.email_actions import Actions, Incoming, ReplyReader, recruiter_email
from jobradar.sinks.email_notify import build_email
from jobradar.sinks.notion_sync import FOLLOWED_UP
from tests.conftest import FakeClock
from tests.test_digest import settings

ME = "me@example.com"
JOB = "a1b2c3d4e5f6" + "0" * 28


def actions() -> Actions:
    return Actions("k" * 32, ME)


def add(repo: Repo, job_id: str = JOB, status: JobStatus = JobStatus.NEW) -> None:
    repo.get_or_create_job(job_id, f"https://acme.com/{job_id}")
    repo.save_extraction(job_id, json.dumps({}), "Acme", "Software Engineer", None,
                         "rules-v1", JobStatus.EXTRACTED)  # fmt: skip
    repo.set_job_status(job_id, JobStatus.NEW)
    if status != JobStatus.NEW:
        repo.set_user_status(job_id, status)


def mail(subject: str, sender: str = ME, auth: str = "", n: int = 1) -> Incoming:
    sent = datetime(2026, 10, 19, 6, 0, tzinfo=UTC)
    return Incoming(str(n), f"<m{n}@x>", sender, subject, auth, sent)


def subject_of(link: str) -> str:
    return unquote(parse_qs(urlsplit(link).query)["subject"][0])


def reader(repo: Repo, queue: TaskQueue, clock: FakeClock) -> ReplyReader:
    return ReplyReader(settings(), repo, queue, actions(), clock=clock)


# ---- the buttons -------------------------------------------------------------------------


def test_buttons_are_mailto_links_to_yourself_with_a_signed_subject(repo: Repo) -> None:
    add(repo)
    job = repo.get_job(JOB)
    assert job is not None
    (applied, applied_link), (skip, _) = actions().job_links(job)
    assert (applied, skip) == ("I applied", "Not interested")
    assert applied_link.startswith(f"mailto:{ME}?subject=")
    subject = subject_of(applied_link)
    assert subject.startswith("JobRadar action: applied | Software Engineer @ Acme | a1b2c3d4e5f6.")
    assert actions().parse(subject) == ("applied", "a1b2c3d4e5f6")


def test_forged_or_altered_subjects_are_refused() -> None:
    real = f"JobRadar action: applied | x | a1b2c3d4e5f6.{actions().sign('applied', JOB)}"
    assert actions().parse(real) is not None
    assert actions().parse(real.replace("applied", "skip", 1)) is None  # other action, same sig
    assert actions().parse(real.replace("a1b2", "ffff")) is None  # other job
    assert Actions("other-key", ME).parse(real) is None  # someone else's JobRadar
    assert actions().parse("Re: your application") is None


def test_only_your_own_address_and_no_failed_checks() -> None:
    a = Actions("k", ME, also_from="Me@Phone.com")
    assert a.trusted(f"Me <{ME}>", "")
    assert a.trusted("me@phone.com", "mx.google.com; dkim=pass; spf=pass; dmarc=pass")
    assert not a.trusted("someone@else.com", "")
    assert not a.trusted(ME, "mx.google.com; spf=fail; dmarc=fail")


def test_no_buttons_once_you_applied(repo: Repo) -> None:
    add(repo, status=JobStatus.APPLIED)
    job = repo.get_job(JOB)
    assert job is not None and actions().job_links(job) == []


def test_alert_email_carries_the_buttons(repo: Repo) -> None:
    add(repo)
    job = repo.get_job(JOB)
    assert job is not None
    msg = build_email([job], ME, ME, actions=actions())
    html = str(msg.get_body(("html",)).get_content())  # type: ignore[union-attr]
    plain = str(msg.get_body(("plain",)).get_content())  # type: ignore[union-attr]
    assert "I applied</a>" in html and "Not interested</a>" in html and "mailto:" in html
    assert "I applied: mailto:" in plain


# ---- acting on your emails ----------------------------------------------------------------


def subject(action: str, job_id: str = JOB) -> str:
    return f"JobRadar action: {action} | x | {job_id[:12]}.{actions().sign(action, job_id)}"


async def test_i_applied_and_not_interested(repo: Repo, queue: TaskQueue, clock: FakeClock) -> None:
    other = "b" * 40
    add(repo)
    add(repo, other)
    r = reader(repo, queue, clock)
    assert r.apply(mail(subject("applied"))) == "applied: Software Engineer @ Acme"
    assert r.apply(mail(subject("skip", other))) == "skip: Software Engineer @ Acme"
    job, skipped = repo.get_job(JOB), repo.get_job(other)
    assert job is not None and job.status == JobStatus.APPLIED.value and job.user_status_at
    assert skipped is not None and skipped.status == JobStatus.SKIPPED.value
    task = queue.claim([NOTION_STATUS])
    assert task is not None and task.key == JOB and task.payload == {"status": "Applied"}


async def test_statuses_only_move_forward(repo: Repo, queue: TaskQueue, clock: FakeClock) -> None:
    add(repo, status=JobStatus.INTERVIEW)
    r = reader(repo, queue, clock)
    assert "ignored" in (r.apply(mail(subject("applied"))) or "")
    assert "ignored" in (r.apply(mail(subject("skip"))) or "")
    job = repo.get_job(JOB)
    assert job is not None and job.status == JobStatus.INTERVIEW.value


async def test_i_followed_up_sets_the_date(repo: Repo, queue: TaskQueue, clock: FakeClock) -> None:
    add(repo, status=JobStatus.APPLIED)
    r = reader(repo, queue, clock)
    assert r.apply(mail(subject("followed up"))) == "followed up: Software Engineer @ Acme"
    assert repo.get_setting(FOLLOWED_UP + JOB) == "2026-10-19"  # the email's own date, IST
    task = queue.claim([NOTION_STATUS])
    assert task is not None and task.payload == {"followed_up": "2026-10-19"}


async def test_strangers_and_unknown_jobs(repo: Repo, queue: TaskQueue, clock: FakeClock) -> None:
    add(repo)
    r = reader(repo, queue, clock)
    assert r.apply(mail(subject("applied"), sender="x@evil.com")) is None
    assert r.apply(mail("JobRadar action: applied | x | a1b2c3d4e5f6.00000000")) is None
    assert r.apply(mail(subject("applied", "c" * 40))) == "applied: job cccccccccccc not found"
    job = repo.get_job(JOB)
    assert job is not None and job.status == JobStatus.NEW.value


async def test_each_email_once_then_archived(
    repo: Repo, queue: TaskQueue, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    add(repo)
    archived: list[list[str]] = []

    class FakeImap:
        def logout(self) -> None:
            pass

    inbox = [mail(subject("applied"), n=1), mail("hello", n=2)]
    monkeypatch.setattr(email_actions, "fetch", lambda s, since: (FakeImap(), inbox))
    monkeypatch.setattr(email_actions, "archive", lambda imap, uids: archived.append(uids))
    r = reader(repo, queue, clock)
    assert await r.check_now() == ["applied: Software Engineer @ Acme"]
    assert archived == [["1"]]  # "hello" isn't a button email: left alone
    assert await r.check_now() == []  # already done; archived again in case it came back
    assert archived[-1] == ["1"]


# ---- the follow-up's recipient --------------------------------------------------------------


@pytest.mark.parametrize(
    ("apply_url", "post", "to"),
    [
        ("mailto:jobs@acme.com?subject=CV", None, "jobs@acme.com"),
        (None, "Send your CV to hr.team@acme.co.in today", "hr.team@acme.co.in"),
        (None, "Questions: priya@acmecorp.com", "priya@acmecorp.com"),  # company's own domain
        (None, "Join our channel admin: freshersjobs@gmail.com", None),
        (None, "Contact: someone@unrelated.io", None),
        (None, None, None),
    ],
)
def test_recruiter_email(apply_url: str | None, post: str | None, to: str | None) -> None:
    job = Job(id="x", company="Acme Corp", created_at="", updated_at="")
    assert recruiter_email(job, apply_url, post) == to


# ---- Notion ---------------------------------------------------------------------------------


async def test_notion_gets_your_status_and_followed_up_date(repo: Repo) -> None:
    from jobradar.sinks import notion
    from tests.test_notion import API, DS, http, schema_response
    from tests.test_notion import settings as notion_settings

    add(repo, status=JobStatus.APPLIED)
    repo.set_notion_page(JOB, "page-1")
    with respx.mock(base_url=API, assert_all_called=False) as api:
        api.get("/databases/db-1").respond(json={"data_sources": [{"id": DS, "name": "Jobs"}]})
        api.get(f"/data_sources/{DS}").respond(json=schema_response())
        patch = api.patch("/pages/page-1").respond(json={})
        sink = await notion.connect(notion_settings(), http=http())
        await sink.set_by_you(repo, JOB, "Applied", "2026-10-19")
        await sink.aclose()
    body: dict[str, Any] = json.loads(patch.calls[0].request.content)
    assert body == {"properties": {"Status": {"select": {"name": "Applied"}},
                                   "Followed Up": {"date": {"start": "2026-10-19"}}}}  # fmt: skip


def test_buttons_off_without_a_mailbox(repo: Repo) -> None:
    from jobradar.config import Settings

    s = settings()
    assert Actions.load(s, repo) is not None
    no_mailbox = Settings(
        config=s.config, secrets=s.secrets.model_copy(update={"email_address": None})
    )
    assert Actions.load(no_mailbox, repo) is None
    s2 = settings(email_actions=False)
    assert Actions.load(s2, repo) is None


async def test_a_gmail_timeout_is_a_quiet_retry(
    repo: Repo, queue: TaskQueue, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobradar.queue.tasks import RetryableError

    def slow(s: object, since: object) -> None:
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(email_actions, "fetch", slow)
    with pytest.raises(RetryableError, match="inbox not reachable"):
        await reader(repo, queue, clock).check_now()


class ArchiveImap:
    """Just enough of Gmail's IMAP to archive: LIST, STORE and MOVE, recorded."""

    def __init__(self, gmail: bool = True, move: str = "OK", folders: list[bytes] | None = None):
        self.capabilities = ("IMAP4REV1", "X-GM-EXT-1") if gmail else ("IMAP4REV1",)
        self.calls: list[tuple[str, ...]] = []
        self.move = move
        self.folders = (
            folders
            if folders is not None
            else [
                b'(\\HasNoChildren) "/" "INBOX"',
                b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"',
                b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"',
            ]
        )

    def uid(self, command: str, *args: str) -> tuple[str, list[bytes]]:
        self.calls.append((command, *args))
        return (self.move if command == "MOVE" else "OK"), [b""]

    # Last: in the class body, a method named list hides the builtin from annotations after it
    # (Python < 3.14 evaluates them at once).
    def list(self) -> tuple[str, "list[bytes]"]:
        return "OK", self.folders


def test_gmail_archive_moves_to_all_mail() -> None:
    imap = ArchiveImap()
    email_actions.archive(imap, ["7", "9"])  # type: ignore[arg-type]
    assert imap.calls == [
        ("STORE", "7", "+FLAGS", "(\\Seen)"),
        ("STORE", "9", "+FLAGS", "(\\Seen)"),
        ("MOVE", "7,9", '"[Gmail]/All Mail"'),  # not -X-GM-LABELS \Inbox: Gmail ignores that
    ]


def test_all_mail_in_another_language() -> None:
    imap = ArchiveImap(folders=[b'(\\HasNoChildren \\All) "/" "[Google Mail]/Alle Nachrichten"'])
    assert email_actions.all_mail(imap) == '"[Google Mail]/Alle Nachrichten"'  # type: ignore[arg-type]


def test_archive_elsewhere_or_failing_only_marks_read(caplog: pytest.LogCaptureFixture) -> None:
    other = ArchiveImap(gmail=False)
    email_actions.archive(other, ["1"])  # type: ignore[arg-type]
    assert [c[0] for c in other.calls] == ["STORE"]
    refused = ArchiveImap(move="NO")
    email_actions.archive(refused, ["1"])  # type: ignore[arg-type]
    assert "button emails not archived" in caplog.text
    no_folder = ArchiveImap(folders=[b'(\\HasNoChildren) "/" "INBOX"'])
    email_actions.archive(no_folder, ["1"])  # type: ignore[arg-type]
    assert [c[0] for c in no_folder.calls] == ["STORE"]
