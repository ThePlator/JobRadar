from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from jobradar.config import AppConfig, NotionConfig, Secrets, Settings
from jobradar.db.models import JobStatus
from jobradar.db.repo import Repo
from jobradar.pipeline.task_types import DIGEST
from jobradar.queue.tasks import TaskQueue
from jobradar.sinks import digest as digest_mod
from jobradar.sinks.digest import Digest, next_slot, previous_slot
from tests.conftest import FakeClock

IST = ZoneInfo("Asia/Kolkata")
TIMES = ["09:00", "19:00"]


def settings(**notify: Any) -> Settings:
    notify.setdefault("dry_run", False)
    return Settings(
        config=AppConfig.model_validate(
            {"sources": {"telegram": {"enabled": False}}, "notify": notify}
        ),
        secrets=Secrets.model_validate(
            {
                "email_address": "me@example.com",
                "email_app_password": "app-pass",
                "notion_database_id": "db-1234",
            }
        ),
    )


@pytest.mark.parametrize(
    ("now", "nxt", "prev"),
    [
        ("2026-10-11 08:59", "2026-10-11 09:00", "2026-10-10 19:00"),
        ("2026-10-11 09:00", "2026-10-11 19:00", "2026-10-10 19:00"),
        ("2026-10-11 18:30", "2026-10-11 19:00", "2026-10-11 09:00"),
        ("2026-10-11 23:10", "2026-10-12 09:00", "2026-10-11 19:00"),
    ],
)
def test_slots(now: str, nxt: str, prev: str) -> None:
    at = datetime.strptime(now, "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    assert f"{next_slot(at, TIMES):%Y-%m-%d %H:%M}" == nxt
    assert f"{previous_slot(at, TIMES):%Y-%m-%d %H:%M}" == prev


def add_job(repo: Repo, job_id: str, status: JobStatus, score: int | None = None,
            reason: str = "", deadline: str | None = None) -> None:  # fmt: skip
    repo.get_or_create_job(job_id, f"https://acme.com/{job_id}")
    repo.save_extraction(job_id, "{}", "Acme", f"Role {job_id}", deadline, "rules-v1",
                         JobStatus.EXTRACTED)  # fmt: skip
    if score is not None:
        repo.set_match_score(job_id, score, reason or f"Good fit · {score}")
    if status != JobStatus.NEW:
        repo.set_job_status(job_id, status, reason or None)


def make(repo: Repo, queue: TaskQueue, clock: FakeClock, tmp_path: Path, **notify: Any) -> Digest:
    return Digest(settings(**notify), repo, queue, clock=clock, preview_dir=tmp_path)


async def test_digest_email(
    repo: Repo, queue: TaskQueue, clock: FakeClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fake clock starts at 8 Oct 09:00 UTC = 14:30 IST; jobs are created "now".
    sent: list[EmailMessage] = []
    monkeypatch.setattr(digest_mod, "smtp_send", lambda s, m: sent.append(m))
    add_job(repo, "a", JobStatus.NEW, 92, "Strong fit · 92 · eligible ✓")
    add_job(repo, "b", JobStatus.NEW, 64)
    add_job(repo, "c", JobStatus.HIDDEN, 30, "weak match (30)")
    add_job(repo, "d", JobStatus.HIDDEN, None, "not eligible: batch 2027 only")
    add_job(repo, "e", JobStatus.NEW, 80, deadline="2026-10-09")
    clock.advance(4 * 3600 + 30 * 60)  # 19:00 IST, the evening slot

    outcome = await make(repo, queue, clock, tmp_path).send_now()

    assert outcome.startswith("sent:")
    msg = sent[0]
    assert msg["Subject"] == "[JobRadar] Evening digest · 5 new · 2 strong · 1 closing soon"
    text = msg.get_body(("plain",)).get_content()
    assert "5 new · 3 in your Inbox · 2 hidden" in text
    html_body = msg.get_body(("html",)).get_content()
    assert "Top matches" in html_body and "Open your Notion Inbox" in html_body
    assert text.index("Role a") < text.index("Role e") < text.index("Role b")  # best first
    assert "9 Oct  Role e @ Acme" in text
    assert "Hidden: 1 not eligible, 1 weak match" in text
    assert "https://www.notion.so/db1234" in text
    assert "FROM COMPANY BOARDS" not in text  # all from Telegram


async def test_digest_lists_jobs_from_company_boards(
    repo: Repo, queue: TaskQueue, clock: FakeClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobradar.db.models import Platform

    sent: list[EmailMessage] = []
    monkeypatch.setattr(digest_mod, "smtp_send", lambda s, m: sent.append(m))
    add_job(repo, "a", JobStatus.NEW, 81)
    add_job(repo, "b", JobStatus.NEW, 70)
    src = repo.upsert_source(Platform.BOARD, "greenhouse:stripe", "Greenhouse · Stripe")
    assert src.id is not None
    raw_id, _ = repo.save_raw_message(src.id, "123", "2026-10-08T08:00:00.000000Z", "post")
    repo.add_job_source("b", raw_id)
    clock.advance(4 * 3600 + 30 * 60)

    await make(repo, queue, clock, tmp_path).send_now()

    text = sent[0].get_body(("plain",)).get_content()
    assert "FROM COMPANY BOARDS\n   70  Role b @ Acme (Greenhouse · Stripe)" in text
    assert "From company boards" in sent[0].get_body(("html",)).get_content()


async def test_quiet_window_sends_nothing(
    repo: Repo, queue: TaskQueue, clock: FakeClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(digest_mod, "smtp_send", lambda s, m: pytest.fail("sent"))
    assert "nothing new" in await make(repo, queue, clock, tmp_path).send_now()


async def test_dry_run_preview_and_schedule(
    repo: Repo, queue: TaskQueue, clock: FakeClock, tmp_path: Path
) -> None:
    add_job(repo, "a", JobStatus.NEW, 88)
    d = make(repo, queue, clock, tmp_path, dry_run=True)
    task = queue.claim([DIGEST])
    assert task is None
    at = d.schedule_next()  # 14:30 IST now -> 19:00 IST today
    assert f"{at:%Y-%m-%d %H:%M}" == "2026-10-08 19:00"
    assert queue.claim([DIGEST]) is None  # not due yet
    clock.advance(4 * 3600 + 30 * 60)
    task = queue.claim([DIGEST])
    assert task is not None and task.key == "2026-10-08 19:00"

    await d.send(task)
    queue.complete(task)
    previews = list(tmp_path.glob("digest-*.html"))  # noqa: ASYNC240 (test files only)
    assert len(previews) == 1 and "Evening digest" in previews[0].read_text()
    # Sending queued the next slot: tomorrow morning.
    clock.advance(14 * 3600)
    nxt = queue.claim([DIGEST])
    assert nxt is not None and nxt.key == "2026-10-09 09:00"


def test_digest_command(monkeypatch: pytest.MonkeyPatch, repo: Repo, queue: TaskQueue) -> None:
    from typer.testing import CliRunner

    from jobradar import __main__ as cli
    from jobradar import app as application

    monkeypatch.setattr(cli, "_settings", lambda: settings(dry_run=True))
    monkeypatch.setattr(application, "open_store", lambda: (repo, queue))
    result = CliRunner().invoke(cli.app, ["digest", "--now"])
    assert result.exit_code == 0, result.output
    assert "nothing new since the last digest" in result.output


def test_pipeline_line() -> None:
    from jobradar.sinks.digest import DigestData, pipeline_line

    at = datetime(2026, 10, 13, 19, 0, tzinfo=IST)
    data = DigestData(since=at, until=at)
    assert pipeline_line(data) is None
    data.pipeline = {"shortlisted": 2, "applied": 15, "interview": 3, "offer": 0}
    data.moved = {"applied": 2, "new": 1}
    assert pipeline_line(data) == (
        "Your applications: 2 shortlisted · 15 applied · 3 interviews · 0 offers "
        "(+2 applied since then)"
    )


async def test_digest_without_notion_points_at_the_dashboard(
    repo: Repo, queue: TaskQueue, clock: FakeClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[EmailMessage] = []
    monkeypatch.setattr(digest_mod, "smtp_send", lambda s, m: sent.append(m))
    for n in range(7):
        add_job(repo, f"j{n}", JobStatus.NEW, 90 - n)
    add_job(repo, "h", JobStatus.HIDDEN, 30, "weak match (30)")
    with repo.engine.begin() as c:  # pages made while Notion was on
        c.exec_driver_sql("UPDATE user_jobs SET notion_page_id = 'page-1'")
    clock.advance(4 * 3600 + 30 * 60)
    base = settings()
    off = base.model_copy(
        update={"config": base.config.model_copy(update={"notion": NotionConfig(enabled=False)})}
    )
    await Digest(off, repo, queue, clock=clock, preview_dir=tmp_path).send_now()
    text = sent[0].get_body(("plain",)).get_content()
    html_body = sent[0].get_body(("html",)).get_content()
    assert "notion.so" not in text + html_body and "Notion" not in html_body
    assert "more in Today on the dashboard." in text
    assert "Find them on the dashboard: All jobs, Show hidden." in text
