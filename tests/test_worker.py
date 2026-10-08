import asyncio

from jobradar.db.models import TaskStatus
from jobradar.db.repo import Repo, TaskRecord
from jobradar.queue.tasks import Defer, PermanentError, TaskQueue
from jobradar.queue.worker import Worker


async def run_until_idle(worker: Worker, repo: Repo, timeout: float = 5) -> None:
    runner = asyncio.create_task(worker.run())
    try:
        async with asyncio.timeout(timeout):
            while repo.task_counts()[TaskStatus.PENDING] or repo.task_counts()[TaskStatus.RUNNING]:
                await asyncio.sleep(0.01)
    finally:
        worker.stop()
        await runner


async def test_outcomes_are_recorded(queue: TaskQueue, repo: Repo) -> None:
    seen: list[str] = []

    async def handler(task: TaskRecord) -> None:
        seen.append(task.key)
        if task.key == "boom":
            raise RuntimeError("temporary")
        if task.key == "bad":
            raise PermanentError("404")

    for key in ("ok", "boom", "bad"):
        queue.enqueue("fetch", key)
    worker = Worker(queue, {"fetch": handler}, poll_interval=0.01)

    runner = asyncio.create_task(worker.run())
    async with asyncio.timeout(5):
        while repo.task_counts()[TaskStatus.DONE] + repo.task_counts()[TaskStatus.FAILED] < 2:
            await asyncio.sleep(0.01)
    worker.stop()
    await runner

    counts = repo.task_counts()
    assert sorted(seen) == ["bad", "boom", "ok"]
    assert counts[TaskStatus.DONE] == 1
    assert counts[TaskStatus.FAILED] == 1
    assert counts[TaskStatus.PENDING] == 1  # "boom" is waiting out its backoff


async def test_defer_and_concurrency_limit(queue: TaskQueue, repo: Repo) -> None:
    active = 0
    peak = 0
    deferred = False

    async def handler(task: TaskRecord) -> None:
        nonlocal active, peak, deferred
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        if task.key == "k0" and not deferred:
            deferred = True
            raise Defer(0)

    for i in range(6):
        queue.enqueue("fetch", f"k{i}")
    await run_until_idle(Worker(queue, {"fetch": handler}, concurrency=2, poll_interval=0.01), repo)

    assert peak == 2
    assert repo.task_counts()[TaskStatus.DONE] == 6


async def test_stop_waits_for_in_flight(queue: TaskQueue, repo: Repo) -> None:
    started = asyncio.Event()

    async def slow(task: TaskRecord) -> None:
        started.set()
        await asyncio.sleep(0.05)

    queue.enqueue("fetch", "k")
    worker = Worker(queue, {"fetch": slow}, poll_interval=0.01)
    runner = asyncio.create_task(worker.run())
    await started.wait()
    worker.stop()
    await runner
    assert repo.task_counts()[TaskStatus.DONE] == 1
