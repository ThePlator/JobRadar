import random

import pytest

from jobradar.db.models import TaskStatus
from jobradar.db.repo import Repo
from jobradar.queue.tasks import MAX_ATTEMPTS, TaskQueue, backoff_seconds
from tests.conftest import FakeClock


def test_backoff_schedule_and_cap() -> None:
    rng = random.Random(1)
    for attempt, base in [(1, 30), (2, 120), (3, 480), (4, 1920), (5, 3600), (9, 3600)]:
        d = backoff_seconds(attempt, rng)
        assert 0.8 * base <= d <= 1.2 * base


def test_enqueue_is_idempotent_and_claim_order(queue: TaskQueue, clock: FakeClock) -> None:
    queue.enqueue("fetch", "job-b")
    clock.advance(1)
    queue.enqueue("fetch", "job-a")
    queue.enqueue("fetch", "job-b", {"v": 2})  # merges into the pending row

    first = queue.claim()
    second = queue.claim()
    assert (first and first.key, second and second.key) == ("job-b", "job-a")
    assert first is not None and first.payload == {"v": 2} and first.attempts == 1
    assert queue.claim() is None


def test_run_after_and_type_filter(queue: TaskQueue, clock: FakeClock) -> None:
    queue.enqueue("digest", "2026-10-08/am", delay=60)
    queue.enqueue("fetch", "j1")
    assert queue.claim(["digest"]) is None  # not due yet
    task = queue.claim(["digest", "notion_upsert"])
    assert task is None
    assert (t := queue.claim(["fetch"])) is not None and t.key == "j1"
    clock.advance(61)
    assert (t := queue.claim(["digest"])) is not None and t.key == "2026-10-08/am"


def test_done_task_can_be_enqueued_again(queue: TaskQueue, repo: Repo) -> None:
    queue.enqueue("notion_upsert", "j1")
    t = queue.claim()
    assert t is not None
    queue.complete(t)
    assert repo.task_counts()[TaskStatus.DONE] == 1

    queue.enqueue("notion_upsert", "j1")
    t2 = queue.claim()
    assert t2 is not None and t2.id == t.id and t2.attempts == 1


def test_enqueue_while_running_runs_again(queue: TaskQueue) -> None:
    queue.enqueue("notion_upsert", "j1", {"v": 1})
    t = queue.claim()
    assert t is not None
    queue.enqueue("notion_upsert", "j1", {"v": 2})  # job changed mid-sync
    assert queue.claim() is None  # never two attempts of one task at once
    queue.complete(t)
    again = queue.claim()
    assert again is not None and again.payload == {"v": 2}


def test_retry_with_backoff_then_failed(queue: TaskQueue, repo: Repo, clock: FakeClock) -> None:
    queue.enqueue("fetch", "j1")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        t = queue.claim()
        assert t is not None and t.attempts == attempt
        retried = queue.retry(t, "timeout")
        assert retried is (attempt < MAX_ATTEMPTS)
        assert queue.claim() is None  # backoff: not immediately due
        clock.advance(3600 * 1.2 + 1)
    assert queue.claim() is None
    assert repo.task_counts()[TaskStatus.FAILED] == 1

    assert queue.requeue_failed() == 1
    t = queue.claim()
    assert t is not None and t.attempts == 1


def test_retry_after_is_honoured(queue: TaskQueue, clock: FakeClock) -> None:
    queue.enqueue("notion_upsert", "j1")
    t = queue.claim()
    assert t is not None
    queue.retry(t, "429", retry_after=5)
    clock.advance(4)
    assert queue.claim() is None
    clock.advance(2)
    assert queue.claim() is not None


def test_permanent_fail(queue: TaskQueue, repo: Repo) -> None:
    queue.enqueue("fetch", "j1")
    t = queue.claim()
    assert t is not None
    queue.fail(t, "404")
    assert repo.task_counts()[TaskStatus.FAILED] == 1


def test_defer_does_not_count_an_attempt(queue: TaskQueue, clock: FakeClock) -> None:
    queue.enqueue("telegram_catchup", "@jobs")
    t = queue.claim()
    assert t is not None and t.attempts == 1
    queue.defer(t, 30)
    assert queue.claim() is None
    clock.advance(31)
    t2 = queue.claim()
    assert t2 is not None and t2.attempts == 1


def test_reclaim_stuck_after_crash(queue: TaskQueue, clock: FakeClock) -> None:
    queue.enqueue("fetch", "j1")
    assert queue.claim() is not None  # then the process "crashes"
    clock.advance(5 * 60)
    assert queue.reclaim_stuck() == 0
    clock.advance(6 * 60)
    assert queue.reclaim_stuck() == 1
    t = queue.claim()
    assert t is not None and t.attempts == 2


REPORTS = {
    "complete": lambda q, t: q.complete(t),
    "retry": lambda q, t: q.retry(t, "late error"),
    "fail": lambda q, t: q.fail(t, "late error"),
    "defer": lambda q, t: q.defer(t, 1),
}


@pytest.mark.parametrize("report", REPORTS)
def test_second_report_for_an_attempt_is_ignored(queue: TaskQueue, report: str) -> None:
    queue.enqueue("fetch", "j1")
    t = queue.claim()
    assert t is not None
    queue.complete(t)
    REPORTS[report](queue, t)  # e.g. a handler that reports twice
    assert queue.claim() is None
