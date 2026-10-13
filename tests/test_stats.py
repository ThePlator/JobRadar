from datetime import UTC, datetime

from jobradar.db.models import JobStatus, Platform
from jobradar.db.repo import Repo
from jobradar.pipeline.ingest import Ingest, make_emit
from jobradar.queue.tasks import TaskQueue
from jobradar.sources.base import IncomingMessage
from jobradar.stats import build_report, render, title_key
from tests.conftest import FakeClock


def post(chat: str, mid: str, text: str) -> IncomingMessage:
    return IncomingMessage(
        platform=Platform.TELEGRAM,
        chat_id=chat,
        chat_title=f"Channel {chat}",
        message_id=mid,
        text=text,
        posted_at=datetime(2026, 10, 8, 8, 0, tzinfo=UTC),
    )


def test_title_key_ignores_filler_and_short_titles() -> None:
    assert (
        title_key("🔥 VIR Softech Hiring Software Engineer Freshers!")
        == "vir softech software engineer"
    )
    assert title_key("Apply now!") is None


async def test_report_counts_and_likely_duplicates(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    emit = make_emit(repo, queue)
    await emit(
        post("-1001", "1", "VIR Softech Hiring Software Engineer Freshers!\nhttps://a.com/j/1")
    )
    await emit(
        post("-1002", "7", "VIR Softech hiring Software Engineer - freshers\nhttps://b.com/x?id=9")
    )
    await emit(post("-1001", "2", "Acme Backend Intern\nhttps://acme.com/j/2"))
    await emit(
        post("-1002", "8", "Acme Backend Intern (repost)\nhttps://www.acme.com/j/2/?utm_source=tg")
    )
    await emit(post("-1001", "3", "Join our group https://t.me/morejobs"))
    await emit(
        post(
            "-1001",
            "4",
            "Multiple Hiring Drives Live at AccioJob!\nhttps://drives.example.com/1 https://drives.example.com/2",
        )
    )
    ingest = Ingest(repo, queue)
    while (task := queue.claim(["process_message"])) is not None:
        await ingest.process_message(task)
        queue.complete(task)

    report = build_report(repo, days=7, clock=clock)

    assert report.messages == 6
    assert report.no_link == 1
    assert report.jobs == 5  # a.com, b.com, acme (merged by URL), drives/1, drives/2
    assert report.merged == 1
    # Only the two VIR Softech posts; the two drives from one AccioJob post are siblings.
    assert [len(g) for g in report.duplicate_groups] == [2]
    assert report.extra_rows == 1
    assert report.duplicate_rate == 0.2
    assert dict((name, (p, j)) for name, p, j in report.per_chat) == {
        "Channel -1001": (4, 4),
        "Channel -1002": (2, 2),
    }
    text = render(report)
    assert "Likely duplicate rows      1   20.0% of jobs, over the 5% target" in text
    assert "https://b.com/x?id=9" in text


def test_empty_database(repo: Repo, clock: FakeClock) -> None:
    report = build_report(repo, clock=clock)
    assert report.jobs == 0 and report.duplicate_rate == 0
    assert "Queue: 0 waiting, 0 failed" in render(report)


def test_alert_threshold_suggestion() -> None:
    from jobradar.stats import suggest_alert_above

    assert "at least 5" in (suggest_alert_above({"applied": [90, 80]}, 75) or "")
    scores = {"applied": [92, 88, 81, 79, 77], "interview": [86], "skipped": [80, 40]}
    line = suggest_alert_above(scores, 85) or ""
    assert "Suggested scoring.alert_above: 75 (now 85)" in line
    assert "1 job(s) you skipped also score that high" in line


def test_hidden_jobs_are_counted_by_reason(repo: Repo, clock: FakeClock) -> None:
    from jobradar.stats import build_report, render

    for job_id, reason in (("e", "not eligible: batch 2027 only"), ("w", "weak match (29)"),
                           ("a", "promo link"), ("y", None)):  # fmt: skip
        repo.get_or_create_job(job_id, f"https://acme.com/{job_id}")
        if reason:
            repo.set_job_status(job_id, JobStatus.HIDDEN, reason)
    report = build_report(repo, 3, clock=clock)
    assert report.hidden == {"not eligible": 1, "weak match": 1, "ad or promo link": 1}
    assert [ad.job_id for ad in report.hidden_ads] == ["a"]  # only real ads are listed as ads
    text = render(report)
    assert "Jobs hidden                3" in text and "not eligible             1" in text
