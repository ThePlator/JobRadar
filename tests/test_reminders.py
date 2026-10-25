import json
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
import respx

from jobradar.db.models import JobStatus
from jobradar.db.repo import Repo
from jobradar.pipeline.liveness import LivenessChecker
from jobradar.pipeline.task_types import CLOSING_CHECK
from jobradar.queue.tasks import TaskQueue
from jobradar.sinks import reminders as reminders_mod
from jobradar.sinks.reminders import ClosingReminder
from tests.conftest import FakeClock
from tests.test_digest import settings

# The fake clock starts at 8 Oct 09:00 UTC = 14:30 IST.
GH = "https://job-boards.greenhouse.io/stripe/jobs/{}"
GH_API = "https://boards-api.greenhouse.io/v1/boards/stripe/jobs/{}"


def add_job(repo: Repo, job_id: str, status: JobStatus, deadline: str, gh: int = 0) -> None:
    url = GH.format(gh) if gh else f"https://acme.com/{job_id}"
    repo.get_or_create_job(job_id, url)
    data = {"apply_url": url} if gh else {}
    repo.save_extraction(job_id, json.dumps(data), "Acme", f"Role {job_id}", deadline,
                         "rules-v1", JobStatus.EXTRACTED)  # fmt: skip
    repo.set_job_status(job_id, JobStatus.NEW)
    if status != JobStatus.NEW:
        repo.set_user_status(job_id, status)


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[EmailMessage]:
    out: list[EmailMessage] = []
    monkeypatch.setattr(reminders_mod, "smtp_send", lambda s, m: out.append(m))
    return out


def make(
    repo: Repo, queue: TaskQueue, clock: FakeClock, tmp_path: Path, **kw: Any
) -> ClosingReminder:
    return ClosingReminder(settings(), repo, queue, clock=clock, preview_dir=tmp_path, **kw)


async def test_reminds_once_about_shortlisted_jobs_closing_soon(
    repo: Repo, queue: TaskQueue, clock: FakeClock, tmp_path: Path, sent: list[EmailMessage]
) -> None:
    add_job(repo, "today", JobStatus.SHORTLISTED, "2026-10-08")
    add_job(repo, "tomorrow", JobStatus.SHORTLISTED, "2026-10-09")
    add_job(repo, "later", JobStatus.SHORTLISTED, "2026-10-12")
    add_job(repo, "inbox", JobStatus.NEW, "2026-10-08")  # not shortlisted: the digest covers it
    add_job(repo, "applied", JobStatus.APPLIED, "2026-10-08")
    r = make(repo, queue, clock, tmp_path)

    assert (await r.check_now()).startswith("sent:")
    assert sent[0]["Subject"] == "[JobRadar] 2 jobs you shortlisted close soon"
    text = sent[0].get_body(("plain",)).get_content()
    assert "Role today" in text and "Role tomorrow" in text and "Role later" not in text
    assert await r.check_now() == "nothing closing soon"  # once per job
    assert len(sent) == 1


async def test_quiet_at_night(
    repo: Repo, queue: TaskQueue, clock: FakeClock, tmp_path: Path, sent: list[EmailMessage]
) -> None:
    add_job(repo, "a", JobStatus.SHORTLISTED, "2026-10-09")
    clock.advance(9 * 3600)  # 23:30 IST
    r = make(repo, queue, clock, tmp_path)
    assert "night" in await r.check_now()
    clock.advance(9 * 3600)  # 08:30 IST, the deadline is now today
    await r.check_now()
    assert sent[0]["Subject"] == "[JobRadar] Closes today: Role a @ Acme"


@respx.mock
async def test_closed_postings_are_not_reminded(
    repo: Repo, queue: TaskQueue, clock: FakeClock, tmp_path: Path, sent: list[EmailMessage]
) -> None:
    respx.get(GH_API.format(1)).respond(404)
    respx.get(GH_API.format(2)).respond(200, json={})
    add_job(repo, "gone", JobStatus.SHORTLISTED, "2026-10-08", gh=1)
    add_job(repo, "open", JobStatus.SHORTLISTED, "2026-10-08", gh=2)
    liveness = LivenessChecker()
    await make(repo, queue, clock, tmp_path, liveness=liveness).check_now()
    await liveness.aclose()
    assert sent[0]["Subject"] == "[JobRadar] Closes today: Role open @ Acme"
    assert repo.get_user_setting("closing_reminder:gone") == "closed"


async def test_hourly_and_dry_run(
    repo: Repo, queue: TaskQueue, clock: FakeClock, tmp_path: Path
) -> None:
    from tests.test_digest import settings as make_settings

    add_job(repo, "a", JobStatus.SHORTLISTED, "2026-10-08")
    r = ClosingReminder(make_settings(dry_run=True), repo, queue, clock=clock, preview_dir=tmp_path)
    r.schedule()
    task = queue.claim([CLOSING_CHECK])
    assert task is not None
    await r.tick(task)
    queue.complete(task)
    assert len(list(tmp_path.glob("reminder-*.html"))) == 1  # noqa: ASYNC240 (test files only)
    assert queue.claim([CLOSING_CHECK]) is None  # the next check is an hour away
    clock.advance(3600)
    nxt = queue.claim([CLOSING_CHECK])
    assert nxt is not None and nxt.key == "b"
