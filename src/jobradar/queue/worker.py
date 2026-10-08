"""Async worker loop: claims tasks, runs their handlers, and applies retry/backoff."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping

from jobradar.db.repo import TaskRecord
from jobradar.queue.tasks import Defer, PermanentError, RetryableError, TaskQueue

log = logging.getLogger(__name__)

Handler = Callable[[TaskRecord], Awaitable[None]]


class Worker:
    """Runs handlers for a set of task types with bounded concurrency.

    A handler signals the outcome by returning (done) or raising:
    `Defer` (postpone, attempt not counted), `PermanentError` (fail now),
    `RetryableError` or any other exception (retry with backoff, failed after max attempts).
    """

    def __init__(
        self,
        queue: TaskQueue,
        handlers: Mapping[str, Handler],
        concurrency: int = 4,
        poll_interval: float = 1.0,
        name: str = "worker",
    ) -> None:
        if not handlers:
            raise ValueError("a worker needs at least one handler")
        self.queue = queue
        self.handlers = dict(handlers)
        self.concurrency = concurrency
        self.poll_interval = poll_interval
        self.name = name
        self._stopping = asyncio.Event()
        self._wake = asyncio.Event()
        self._in_flight: set[asyncio.Task[None]] = set()

    def wake(self) -> None:
        """Skip the poll wait, e.g. right after enqueueing work for this worker."""
        self._wake.set()

    def stop(self) -> None:
        self._stopping.set()
        self._wake.set()

    async def run(self) -> None:
        """Process tasks until stop(); then wait for in-flight tasks to finish."""
        slots = asyncio.Semaphore(self.concurrency)
        types = list(self.handlers)
        while not self._stopping.is_set():
            await slots.acquire()
            task = None if self._stopping.is_set() else self.queue.claim(types)
            if task is None:
                slots.release()
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval)
                continue
            job = asyncio.create_task(self._run_one(task), name=f"{self.name}:{task.type}")
            self._in_flight.add(job)
            job.add_done_callback(self._in_flight.discard)
            job.add_done_callback(lambda _: slots.release())
        if self._in_flight:
            await asyncio.gather(*self._in_flight, return_exceptions=True)

    async def _run_one(self, task: TaskRecord) -> None:
        ctx = {"task": task.type, "key": task.key, "attempt": task.attempts}
        try:
            await self.handlers[task.type](task)
        except Defer as d:
            log.info("task deferred", extra=ctx | {"seconds": d.seconds})
            self.queue.defer(task, d.seconds)
        except PermanentError as e:
            log.warning("task failed permanently", extra=ctx | {"error": str(e)})
            self.queue.fail(task, f"{type(e).__name__}: {e}")
        except asyncio.CancelledError:
            self.queue.defer(task, 0)  # shutting down: hand it back untouched
            raise
        except Exception as e:  # retry everything else, including RetryableError
            retry_after = e.retry_after if isinstance(e, RetryableError) else None
            retried = self.queue.retry(task, f"{type(e).__name__}: {e}", retry_after)
            log.log(
                logging.WARNING if retried else logging.ERROR,
                "task error, will retry" if retried else "task failed after max attempts",
                extra=ctx | {"error": str(e)},
                exc_info=not isinstance(e, RetryableError),
            )
        else:
            self.queue.complete(task)
