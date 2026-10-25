"""The write services: one way in for every change, whoever makes it (dashboard, email
button, CLI, and later the API)."""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from jobradar.db.models import JobStatus
from jobradar.db.repo import Repo
from jobradar.pipeline.task_types import BUILD_RESUME, NOTION_STATUS, PROCESS_MESSAGE
from jobradar.queue.tasks import TaskQueue
from jobradar.services import intake, notify, resumes, statuses, tasks
from jobradar.services import profile as profile_svc
from jobradar.services.context import Ctx
from jobradar.services.errors import NotFound, Refused
from tests.conftest import FakeClock
from tests.test_digest import settings
from tests.test_web import PROFILE_YAML, add


def ctx(repo: Repo, clock: FakeClock, queue: TaskQueue | None = None, s: Any = None) -> Ctx:
    return Ctx(repo, s or settings(), clock(), queue=queue)


def queued(repo: Repo, type_: str) -> list[str]:
    with repo.engine.connect() as c:
        rows = c.execute(
            text("SELECT key FROM task WHERE type = :t ORDER BY id"), {"t": type_}
        ).all()
    return [r[0] for r in rows]


def test_quick_actions_only_move_forward(repo: Repo, clock: FakeClock, queue: TaskQueue) -> None:
    add(repo, "j")
    done = statuses.act(ctx(repo, clock, queue), "j", "applied")
    assert done.ok and (done.before, done.after) == ("new", "applied")
    assert repo.get_job("j").status == "applied"  # type: ignore[union-attr]
    assert queued(repo, NOTION_STATUS) == ["j"]  # Notion follows through the queue
    back = statuses.act(ctx(repo, clock, queue), "j", "shortlist")
    assert not back.ok and "Change status" in back.message
    with pytest.raises(NotFound):
        statuses.act(ctx(repo, clock, queue), "nope", "applied")


def test_change_status_can_fix_a_mistake(repo: Repo, clock: FakeClock, queue: TaskQueue) -> None:
    add(repo, "j", status=JobStatus.APPLIED, you=True)
    fixed = statuses.set_status(ctx(repo, clock, queue), "j", "new")
    assert fixed.ok and repo.get_job("j").status == "new"  # type: ignore[union-attr]
    assert not statuses.set_status(ctx(repo, clock, queue), "j", "discovered").ok  # not yours


def test_shortlisting_queues_a_resume_unless_told_not_to(
    repo: Repo, clock: FakeClock, queue: TaskQueue
) -> None:
    add(repo, "a")
    add(repo, "b")
    statuses.act(ctx(repo, clock, queue), "a", "shortlist")
    statuses.act(ctx(repo, clock, queue), "b", "shortlist", build_resumes=False)  # email button
    assert queued(repo, BUILD_RESUME) == ["a"]


def test_resume_requests(repo: Repo, clock: FakeClock, queue: TaskQueue) -> None:
    add(repo, "abcd1234", role="Backend Engineer")
    here = ctx(repo, clock, queue)
    assert resumes.request(here, "abcd1234") == "Building a resume for Backend Engineer…"
    assert resumes.progress(here, "abcd1234") == resumes.Progress(0, True)
    with pytest.raises(NotFound):
        resumes.request(here, "nope")
    base = settings()
    off = base.model_copy(
        update={"config": base.config.model_copy(update={"resume": base.config.resume.model_copy(
            update={"enabled": False})})}
    )  # fmt: skip
    with pytest.raises(Refused, match="Resumes are off"):
        resumes.request(ctx(repo, clock, queue, off), "abcd1234")


def test_find_a_job_by_the_start_of_its_id(repo: Repo, clock: FakeClock) -> None:
    add(repo, "abcd1111")
    add(repo, "abcd2222")
    here = ctx(repo, clock)
    assert resumes.find_job(here, "ABCD1").id == "abcd1111"  # case and length don't matter
    with pytest.raises(Refused, match="2 jobs start with"):
        resumes.find_job(here, "abcd")
    with pytest.raises(NotFound):
        resumes.find_job(here, "ffff")


def test_i_know_this(repo: Repo, clock: FakeClock, tmp_path: Path) -> None:
    path = tmp_path / "profile.yaml"
    path.write_text(PROFILE_YAML)
    base = settings()
    s = base.model_copy(update={"config": base.config.model_copy(update={
        "matching": base.config.matching.model_copy(update={"profile_path": path})})})  # fmt: skip
    here = ctx(repo, clock, s=s)
    backup = profile_svc.know_skill(here, "AWS")
    assert backup == "profile.yaml.bak-20261008-143000"  # the stamp is in your timezone
    with pytest.raises(Refused, match="already"):
        profile_svc.know_skill(here, "aws")


def test_add_a_link_and_retry_failed(repo: Repo, clock: FakeClock, queue: TaskQueue) -> None:
    here = ctx(repo, clock, queue)
    asyncio.run(intake.add_link(here, "https://boards.greenhouse.io/acme/jobs/1"))
    assert len(queued(repo, PROCESS_MESSAGE)) == 1
    task = queue.claim()
    assert task is not None
    queue.fail(task, "boom")
    assert tasks.retry_failed(here) == 1


def test_follow_up_states(repo: Repo, clock: FakeClock, queue: TaskQueue) -> None:
    add(repo, "j", status=JobStatus.APPLIED, you=True)
    assert [f.state for f in notify.followups(ctx(repo, clock, queue))] == ["waiting"]
    clock.advance(11 * 86400)  # past notify.followup_after_days (10)
    assert [f.state for f in notify.followups(ctx(repo, clock, queue))] == ["due"]


def test_emails_need_email_settings(repo: Repo, clock: FakeClock) -> None:
    base = settings()
    s = base.model_copy(update={"secrets": base.secrets.model_copy(
        update={"email_address": None, "email_app_password": None})})  # fmt: skip
    with pytest.raises(Refused, match=r"Add to \.env first"):
        notify.send_test_alert(ctx(repo, clock, s=s))
