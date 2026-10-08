from datetime import UTC, datetime

from jobradar.db.models import Platform, TaskStatus
from jobradar.db.repo import Repo
from jobradar.pipeline.dedupe import job_id_for_url
from jobradar.pipeline.ingest import NOTION_UPSERT, PROCESS_MESSAGE, Ingest, make_emit
from jobradar.queue.tasks import TaskQueue
from jobradar.sources.base import IncomingMessage


def msg(chat: str, mid: str, text: str, urls: list[str] | None = None) -> IncomingMessage:
    return IncomingMessage(
        platform=Platform.TELEGRAM,
        chat_id=chat,
        chat_title=f"Group {chat}",
        message_id=mid,
        text=text,
        urls=urls or [],
        posted_at=datetime(2026, 10, 8, 8, 0, tzinfo=UTC),
    )


async def drain(ingest: Ingest, queue: TaskQueue) -> list[str]:
    """Run process_message tasks; return the keys of queued notion_upsert tasks."""
    while (task := queue.claim([PROCESS_MESSAGE])) is not None:
        await ingest.process_message(task)
        queue.complete(task)
    upserts = []
    while (task := queue.claim([NOTION_UPSERT])) is not None:
        upserts.append(task.key)
        queue.complete(task)
    return upserts


async def test_emit_stores_once_and_queues(repo: Repo, queue: TaskQueue) -> None:
    woken = []
    emit = make_emit(repo, queue, lambda: woken.append(1))
    await emit(msg("-1001", "5", "job https://acme.com/j/1"))
    await emit(msg("-1001", "5", "job https://acme.com/j/1"))  # replayed by catch-up
    assert repo.task_counts()[TaskStatus.PENDING] == 1
    assert woken == [1]


async def test_same_job_from_two_groups_is_one_job(repo: Repo, queue: TaskQueue) -> None:
    emit = make_emit(repo, queue)
    ingest = Ingest(repo, queue)
    await emit(msg("-1001", "1", "SDE at Acme https://acme.com/j/1?utm_source=tg"))
    await emit(msg("-1002", "9", "Acme hiring", urls=["https://www.acme.com/j/1/"]))
    await emit(msg("-1002", "10", "join us https://t.me/morejobs and https://youtu.be/x"))

    upserts = await drain(ingest, queue)

    job_id = job_id_for_url("https://acme.com/j/1")
    assert upserts == [job_id]  # merged; non-job links ignored
    sightings = repo.job_sightings(job_id)
    assert [(s.chat_id, s.message_id) for s in sightings] == [("-1001", "1"), ("-1002", "9")]
    assert all(repo.get_raw_message(i) is not None for i in (1, 2, 3))
    raw3 = repo.get_raw_message(3)
    assert raw3 is not None and raw3.processed


async def test_one_message_with_two_jobs(repo: Repo, queue: TaskQueue) -> None:
    emit = make_emit(repo, queue)
    await emit(msg("-1001", "1", "A https://a.com/j B https://b.com/j"))
    upserts = await drain(Ingest(repo, queue), queue)
    assert sorted(upserts) == sorted(
        job_id_for_url(u) for u in ("https://a.com/j", "https://b.com/j")
    )


async def test_reprocessing_does_not_requeue_notion(repo: Repo, queue: TaskQueue) -> None:
    emit = make_emit(repo, queue)
    ingest = Ingest(repo, queue)
    await emit(msg("-1001", "1", "https://acme.com/j/1"))
    assert len(await drain(ingest, queue)) == 1
    queue.enqueue(PROCESS_MESSAGE, "1")  # e.g. a retry after a crash
    assert await drain(ingest, queue) == []
