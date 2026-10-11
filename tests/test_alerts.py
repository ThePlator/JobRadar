import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Any, ClassVar

import pytest

from jobradar.config import AppConfig, Secrets, Settings
from jobradar.db.models import JobStatus
from jobradar.db.repo import Repo
from jobradar.pipeline.task_types import SEND_ALERTS
from jobradar.queue.tasks import PermanentError, RetryableError, TaskQueue
from jobradar.sinks.email_notify import AlertSender, build_email, subject
from tests.conftest import FakeClock


def settings(**notify: Any) -> Settings:
    return Settings(
        config=AppConfig.model_validate(
            {"sources": {"telegram": {"enabled": False}}, "notify": notify}
        ),
        secrets=Secrets.model_validate(
            {
                "email_address": "me@example.com",
                "email_app_password": "app-pass",
                "notify_to": "phone@example.com",
            }
        ),
    )


def add_job(repo: Repo, job_id: str, score: int, deadline: str | None = None) -> None:
    repo.get_or_create_job(job_id, f"https://acme.com/{job_id}")
    repo.save_extraction(job_id, '{"role": "SDE"}', "Acme", f"SDE {job_id}", deadline,
                         "rules-v1", JobStatus.EXTRACTED)  # fmt: skip
    repo.set_match_score(job_id, score, f"Good fit · {score} · eligible ✓")
    repo.set_notion_page(job_id, "1234-abcd")


class Outbox:
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    def __call__(self, msg: EmailMessage) -> None:
        self.sent.append(msg)


def sender(
    repo: Repo, queue: TaskQueue, clock: FakeClock, tmp_path: Path, **notify: Any
) -> tuple[AlertSender, Outbox]:
    outbox = Outbox()
    return AlertSender(settings(**notify), repo, queue, clock=clock, send=outbox,
                       preview_dir=tmp_path / "alerts"), outbox  # fmt: skip


async def run_due(queue: TaskQueue, alerts: AlertSender) -> int:
    ran = 0
    while (task := queue.claim([SEND_ALERTS])) is not None:
        await alerts.send(task)
        queue.complete(task)
        ran += 1
    return ran


async def test_alerts_are_batched_and_sent_once(
    repo: Repo, queue: TaskQueue, clock: FakeClock, tmp_path: Path
) -> None:
    alerts, outbox = sender(repo, queue, clock, tmp_path)
    add_job(repo, "a", 88, "2026-10-12")
    add_job(repo, "b", 80)
    alerts.queue_alert("a")
    clock.advance(120)
    alerts.queue_alert("b")  # joins the batch the first alert opened
    alerts.queue_alert("a")  # never twice
    assert await run_due(queue, alerts) == 0  # the 10-minute window is still open

    clock.advance(8 * 60)
    assert await run_due(queue, alerts) == 1
    assert len(outbox.sent) == 1
    msg = outbox.sent[0]
    assert msg["To"] == "phone@example.com" and msg["From"] == "me@example.com"
    assert msg["Subject"] == "[JobRadar] 2 strong matches · best: SDE a @ Acme (88)"
    text = msg.get_body(("plain",)).get_content()
    assert "Good fit · 88" in text and "https://www.notion.so/1234abcd" in text
    assert repo.pending_alerts() == []

    alerts.queue_alert("a")
    clock.advance(11 * 60)
    assert await run_due(queue, alerts) == 0  # nothing new: no task was even queued


async def test_dry_run_saves_a_preview(
    repo: Repo, queue: TaskQueue, clock: FakeClock, tmp_path: Path
) -> None:
    alerts, outbox = sender(repo, queue, clock, tmp_path, dry_run=True, alert_batch_minutes=0)
    add_job(repo, "a", 90)
    alerts.queue_alert("a")
    await run_due(queue, alerts)
    assert outbox.sent == []
    previews = list((tmp_path / "alerts").glob("*.html"))
    assert len(previews) == 1 and "[JobRadar 90] SDE a @ Acme" in previews[0].read_text()
    assert repo.alerts_sent_since("2026-10-08T00:00:00.000000Z") == 1


async def test_daily_limit(repo: Repo, queue: TaskQueue, clock: FakeClock, tmp_path: Path) -> None:
    alerts, outbox = sender(
        repo, queue, clock, tmp_path, max_alerts_per_day=1, alert_batch_minutes=0
    )
    for job_id in ("a", "b"):
        add_job(repo, job_id, 90)
        alerts.queue_alert(job_id)
    await run_due(queue, alerts)
    assert len(outbox.sent) == 1 and outbox.sent[0]["Subject"].startswith("[JobRadar 90] SDE a")
    assert repo.pending_alerts() == []  # b was marked capped, not left hanging


def test_subject_for_one_job() -> None:
    from jobradar.db.models import Job

    job = Job(id="x", role="SDE", company="Acme", score=82, deadline="2026-10-12",
              created_at="x", updated_at="x")  # fmt: skip
    assert subject([job]) == "[JobRadar 82] SDE @ Acme · closes 12 Oct"
    html = build_email([job], "a@x.com", "b@x.com").get_body(("html",)).get_content()
    assert "SDE @ Acme" in html


class FakeSMTP:
    error: Exception | None = None
    sent: ClassVar[list[EmailMessage]] = []

    def __init__(self, host: str, port: int, timeout: float) -> None:
        pass

    def __enter__(self) -> "FakeSMTP":
        return self

    def __exit__(self, *a: object) -> None:
        pass

    def starttls(self, context: Any) -> None:
        pass

    def login(self, user: str, password: str) -> None:
        if self.error:
            raise self.error

    def send_message(self, msg: EmailMessage) -> None:
        self.sent.append(msg)


def test_smtp_errors(monkeypatch: pytest.MonkeyPatch, repo: Repo, queue: TaskQueue) -> None:
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    alerts = AlertSender(settings(), repo, queue)
    msg = EmailMessage()
    msg["Subject"] = "test"
    alerts._smtp(msg)
    assert FakeSMTP.sent == [msg]
    FakeSMTP.error = smtplib.SMTPAuthenticationError(535, b"bad")
    with pytest.raises(PermanentError, match="EMAIL_APP_PASSWORD"):
        alerts._smtp(msg)  # no retries: a rejected password only risks a lockout
    FakeSMTP.error = OSError("network down")
    with pytest.raises(RetryableError):
        alerts._smtp(msg)
    FakeSMTP.error = None
