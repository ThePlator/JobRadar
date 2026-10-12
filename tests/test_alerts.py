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
    notify.setdefault("dry_run", False)  # these tests check real sending
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


async def test_first_alert_is_instant_followups_are_grouped(
    repo: Repo, queue: TaskQueue, clock: FakeClock, tmp_path: Path
) -> None:
    alerts, outbox = sender(repo, queue, clock, tmp_path)
    for job_id, score in (("a", 88), ("b", 80), ("c", 92)):
        add_job(repo, job_id, score, "2026-10-12" if job_id == "a" else None)

    alerts.queue_alert("a")
    assert await run_due(queue, alerts) == 1  # no email for a while: sent at once
    assert outbox.sent[0]["Subject"] == "[JobRadar 88] SDE a @ Acme · closes 12 Oct"

    clock.advance(120)
    alerts.queue_alert("b")
    clock.advance(60)
    alerts.queue_alert("c")
    alerts.queue_alert("a")  # never twice
    assert await run_due(queue, alerts) == 0  # an email went out 3 min ago: wait

    clock.advance(7 * 60)  # 10 minutes after the first email
    assert await run_due(queue, alerts) == 1
    assert len(outbox.sent) == 2
    msg = outbox.sent[1]
    assert msg["To"] == "phone@example.com" and msg["From"] == "me@example.com"
    assert msg["Subject"] == "[JobRadar] 2 strong matches · best: SDE c @ Acme (92)"
    text = msg.get_body(("plain",)).get_content()
    assert "Good fit · 80" in text and "https://www.notion.so/1234abcd" in text
    assert repo.pending_alerts() == []


async def test_alert_says_when_and_where_it_was_posted(
    repo: Repo, queue: TaskQueue, clock: FakeClock, tmp_path: Path
) -> None:
    from jobradar.db.models import Platform

    alerts, outbox = sender(repo, queue, clock, tmp_path)
    src = repo.upsert_source(Platform.TELEGRAM, "-1001", "Freshershunt")
    assert src.id is not None
    raw_id, _ = repo.save_raw_message(src.id, "1", "2026-10-08T08:57:00.000000Z", "post")
    add_job(repo, "a", 90)
    repo.add_job_source("a", raw_id)
    alerts.queue_alert("a")  # the fake clock says 09:00: three minutes after the post
    await run_due(queue, alerts)
    assert "posted 3 min ago in Freshershunt" in outbox.sent[0].get_body(("plain",)).get_content()


def test_posted_ago() -> None:
    from datetime import UTC, datetime

    from jobradar.sinks.email_notify import posted_ago

    now = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
    assert posted_ago("2026-10-08T08:59:50.000000Z", None, now) == "posted just now"
    assert posted_ago("2026-10-08T06:00:00.000000Z", "Jobs", now) == "posted 3 h ago in Jobs"
    assert posted_ago("2026-10-05T09:00:00.000000Z", None, now) == "posted 3 days ago"


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
    alerts = AlertSender(settings(), repo, queue)  # _smtp is smtp_send with these settings
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


def test_dry_run_is_the_default() -> None:
    from jobradar.config import NotifyConfig

    assert NotifyConfig().dry_run is True


def test_test_email_command(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from jobradar import __main__ as cli

    sent: list[EmailMessage] = []
    monkeypatch.setattr(cli, "_settings", lambda: settings(dry_run=True))
    monkeypatch.setattr("jobradar.sinks.email_notify.smtp_send", lambda s, m: sent.append(m))
    result = CliRunner().invoke(cli.app, ["alerts", "--test"])
    assert result.exit_code == 0, result.output
    assert "test email sent to phone@example.com" in result.output
    assert sent[0]["Subject"] == "[JobRadar] Test email: alerts reach you"
    assert "Backend Engineer (example)" in sent[0].get_body(("plain",)).get_content()
