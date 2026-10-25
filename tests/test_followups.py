import json
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

from jobradar.config import AppConfig, Settings
from jobradar.db.models import JobStatus
from jobradar.db.repo import Repo
from jobradar.queue.tasks import TaskQueue
from jobradar.sinks import followups as followups_mod
from jobradar.sinks.followups import FollowUpReminder, draft
from jobradar.sinks.notion_sync import FOLLOWED_UP, page_followed_up
from tests.conftest import FakeClock
from tests.test_digest import settings as digest_settings

DAY = 86400
# The fake clock starts at 8 Oct 09:00 UTC = 14:30 IST.

PROFILE = """
basics: {name: Asha Rao, email: asha@example.com, phone: "+91-90000 00000"}
skills: {languages: [Python, SQL], infrastructure: [Docker]}
"""


def settings(tmp_path: Path, **notify: Any) -> Settings:
    (tmp_path / "profile.yaml").write_text(PROFILE)
    s = digest_settings(**notify)
    config = s.config.model_dump()
    config["matching"]["profile_path"] = str(tmp_path / "profile.yaml")
    return Settings(config=AppConfig.model_validate(config), secrets=s.secrets)


def applied(repo: Repo, job_id: str, skills: list[str] | None = None) -> None:
    repo.get_or_create_job(job_id, f"https://acme.com/{job_id}")
    data = {"skills_required": skills or []}
    repo.save_extraction(job_id, json.dumps(data), "Acme", f"Role {job_id}", None,
                         "rules-v1", JobStatus.EXTRACTED)  # fmt: skip
    repo.set_job_status(job_id, JobStatus.NEW)
    repo.set_user_status(job_id, JobStatus.APPLIED)


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[EmailMessage]:
    out: list[EmailMessage] = []
    monkeypatch.setattr(followups_mod, "smtp_send", lambda s, m: out.append(m))
    return out


def make(repo: Repo, queue: TaskQueue, clock: FakeClock, tmp_path: Path) -> FollowUpReminder:
    return FollowUpReminder(settings(tmp_path), repo, queue, clock=clock, preview_dir=tmp_path)


def text(msg: EmailMessage) -> str:
    return str(msg.get_body(("plain",)).get_content())  # type: ignore[union-attr]


async def test_due_ten_days_after_applied_and_at_most_twice(
    repo: Repo, queue: TaskQueue, clock: FakeClock, tmp_path: Path, sent: list[EmailMessage]
) -> None:
    applied(repo, "a", ["Python", "Docker", "Rust"])
    r = make(repo, queue, clock, tmp_path)
    clock.advance(9 * DAY)
    assert await r.check_now() == "no application is due a follow-up"
    clock.advance(DAY)
    assert (await r.check_now()).startswith("sent:")
    assert sent[0]["Subject"] == "[JobRadar] Follow up: Role a @ Acme"
    body = text(sent[0])
    assert "I applied for the Role a role at Acme on 8 October" in body
    assert "My experience with Python and Docker fits" in body  # Rust: you don't have it
    assert "Asha Rao" in body and "asha@example.com | +91-90000 00000" in body
    assert await r.check_now() == "no application is due a follow-up"  # once per window

    clock.advance(10 * DAY)
    assert (await r.check_now()).startswith("sent:")
    assert "check in once more" in text(sent[1])
    clock.advance(30 * DAY)
    assert await r.check_now() == "no application is due a follow-up"  # two at most
    assert len(sent) == 2


async def test_your_followed_up_date_restarts_the_wait(
    repo: Repo, queue: TaskQueue, clock: FakeClock, tmp_path: Path, sent: list[EmailMessage]
) -> None:
    applied(repo, "a")
    r = make(repo, queue, clock, tmp_path)
    clock.advance(10 * DAY)
    repo.set_user_setting(FOLLOWED_UP + "a", "2026-10-15")  # you followed up yourself on day 7
    assert await r.check_now() == "no application is due a follow-up"
    clock.advance(7 * DAY)  # 25 Oct: ten days after the 15th
    assert (await r.check_now()).startswith("sent:")


async def test_a_later_status_stops_them(
    repo: Repo, queue: TaskQueue, clock: FakeClock, tmp_path: Path, sent: list[EmailMessage]
) -> None:
    applied(repo, "a")
    applied(repo, "b")
    repo.set_user_status("b", JobStatus.INTERVIEW)
    r = make(repo, queue, clock, tmp_path)
    clock.advance(11 * DAY)
    await r.check_now()
    assert sent[0]["Subject"] == "[JobRadar] Follow up: Role a @ Acme"


async def test_old_quiet_applications_and_nights_are_left_alone(
    repo: Repo, queue: TaskQueue, clock: FakeClock, tmp_path: Path, sent: list[EmailMessage]
) -> None:
    applied(repo, "a")
    r = make(repo, queue, clock, tmp_path)
    clock.advance(61 * DAY)
    assert await r.check_now() == "no application is due a follow-up"
    applied(repo, "b")
    clock.advance(10 * DAY + 9 * 3600)  # 23:30 IST
    assert (await r.check_now()).startswith("night")
    assert sent == []


async def test_several_due_come_in_one_email(
    repo: Repo, queue: TaskQueue, clock: FakeClock, tmp_path: Path, sent: list[EmailMessage]
) -> None:
    applied(repo, "a")
    applied(repo, "b")
    r = make(repo, queue, clock, tmp_path)
    clock.advance(10 * DAY)
    await r.check_now()
    assert sent[0]["Subject"] == "[JobRadar] 2 applications to follow up"
    html = str(sent[0].get_body(("html",)).get_content())  # type: ignore[union-attr]
    assert html.count("Following up: ") >= 2 and "tap I followed up" in html
    assert "Send to recruiter" not in html and "Open in your mail app" in html  # no HR address


async def test_skills_come_from_the_resume_built_for_the_job(
    repo: Repo, queue: TaskQueue, clock: FakeClock, tmp_path: Path, sent: list[EmailMessage]
) -> None:
    applied(repo, "a", ["Python", "Docker", "SQL"])
    record = {"ats": {"skills_on_resume": ["SQL"], "skills_not_on_resume": ["Python"]}}
    repo.save_artifact("a", "resume", 1, str(tmp_path / "r.pdf"), json.dumps(record))
    r = make(repo, queue, clock, tmp_path)
    clock.advance(10 * DAY)
    await r.check_now()
    assert "My experience with SQL fits" in text(sent[0])


def test_draft_without_skills_or_profile_claims_nothing() -> None:
    from datetime import date

    from jobradar.db.models import Job

    job = Job(id="x", canonical_url="https://a.com", created_at="", updated_at="")
    d = draft(job, date(2026, 10, 6), [], {}, 1)
    assert d.subject == "Following up: the open role application"
    assert "experience" not in d.body and "6 October" in d.body
    assert d.body.endswith("Thanks,")


def test_followed_up_date_from_a_notion_page() -> None:
    page = {"properties": {"Followed Up": {"type": "date", "date": {"start": "2026-10-19"}}}}
    assert page_followed_up(page) == "2026-10-19"
    assert page_followed_up({"properties": {"Followed Up": {"type": "date", "date": None}}}) is None
    assert page_followed_up({"properties": {}}) is None
