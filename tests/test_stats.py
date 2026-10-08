from datetime import UTC, datetime

from jobradar.db.models import Platform
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
            "Multiple Hiring Drives Live at AccioJob!\nhttps://x.com/1 https://x.com/2",
        )
    )
    ingest = Ingest(repo, queue)
    while (task := queue.claim(["process_message"])) is not None:
        await ingest.process_message(task)
        queue.complete(task)

    report = build_report(repo, days=7, clock=clock)

    assert report.messages == 6
    assert report.no_link == 1
    assert report.jobs == 5  # a.com, b.com, acme (merged by URL), x.com/1, x.com/2
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
