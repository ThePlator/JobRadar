from typing import Any

from jobradar.db.models import JobStatus
from jobradar.db.repo import Repo
from jobradar.pipeline.task_types import NOTION_POLL
from jobradar.queue.tasks import TaskQueue
from jobradar.sinks.notion_sync import CURSOR_KEY, FIRST_SINCE, StatusSync
from tests.conftest import FakeClock


def page(job_id: str | None, status: str | None) -> dict[str, Any]:
    props: dict[str, Any] = {
        "Status": {"type": "select", "select": {"name": status} if status else None}
    }
    if job_id is not None:
        props["Job ID"] = {"type": "rich_text", "rich_text": [{"plain_text": job_id}]}
    return {"properties": props}


class FakeClient:
    def __init__(self, *batches: list[dict[str, Any]]) -> None:
        self.batches = list(batches)
        self.since: list[str] = []

    async def changed_pages(self, data_source_id: str, since: str) -> list[dict[str, Any]]:
        self.since.append(since)
        return self.batches.pop(0) if self.batches else []


def add(repo: Repo, job_id: str, status: JobStatus = JobStatus.NEW) -> None:
    repo.get_or_create_job(job_id, f"https://acme.com/{job_id}")
    repo.set_job_status(job_id, status)


def sync(repo: Repo, queue: TaskQueue, clock: FakeClock, client: FakeClient) -> StatusSync:
    return StatusSync(repo, queue, client, "ds-1", clock=clock)  # type: ignore[arg-type]


async def test_your_statuses_are_copied_and_ours_ignored(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    for job_id in ("applied", "untouched", "unhidden"):
        add(repo, job_id)
    repo.set_job_status("unhidden", JobStatus.HIDDEN, "weak match (30)")
    client = FakeClient([
        page("applied", "Applied"),
        page("untouched", "New"),  # JobRadar's own value: not a decision of yours
        page("unhidden", "New"),  # you moved a hidden job back to New
        page(None, "Applied"),  # a page you made yourself
        page("nobody", "Applied"),  # not a job JobRadar knows
        page("untouched", "Maybe later"),  # an option JobRadar doesn't know
    ])  # fmt: skip
    s = sync(repo, queue, clock, client)

    assert await s.poll_now() == 2
    assert client.since == [FIRST_SINCE]  # the first poll reads everything once
    applied, untouched, unhidden = (repo.get_job(j) for j in ("applied", "untouched", "unhidden"))
    assert applied is not None and applied.status == JobStatus.APPLIED
    assert applied.user_status_at is not None
    assert untouched is not None and untouched.user_status_at is None
    assert unhidden is not None and unhidden.status == JobStatus.NEW and unhidden.user_status_at
    assert repo.status_changes_since("2000-01-01T00:00:00.000000Z") == {"applied": 1, "new": 1}


async def test_cursor_overlaps_and_repeats_are_ignored(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    add(repo, "a")
    client = FakeClient([page("a", "Interview")], [page("a", "Interview")])
    s = sync(repo, queue, clock, client)
    await s.poll_now()
    assert repo.get_setting(CURSOR_KEY) == "2026-10-08T08:59:00.000000Z"  # a minute of overlap
    clock.advance(120)
    assert await s.poll_now() == 0  # the same page again: nothing new
    assert client.since[1] == "2026-10-08T08:59:00.000000Z"
    assert repo.status_changes_since("2000-01-01T00:00:00.000000Z") == {"interview": 1}


async def test_poll_task_reschedules_itself(repo: Repo, queue: TaskQueue, clock: FakeClock) -> None:
    s = sync(repo, queue, clock, FakeClient())
    s.schedule()
    task = queue.claim([NOTION_POLL])
    assert task is not None
    await s.poll(task)
    queue.complete(task)
    assert queue.claim([NOTION_POLL]) is None  # next one is 2 minutes away, not a tight loop
    clock.advance(121)
    second = queue.claim([NOTION_POLL])
    assert second is not None and second.key != task.key
    await s.poll(second)
    queue.complete(second)
    clock.advance(121)
    third = queue.claim([NOTION_POLL])
    assert third is not None and third.key == task.key  # the two keys take turns
    s.schedule()  # a restart while a poll is queued doesn't start a second chain
    queue.complete(third)
