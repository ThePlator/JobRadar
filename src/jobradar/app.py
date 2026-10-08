"""Wires sources, workers and the scheduler into one long-running process."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from jobradar.config import Settings
from jobradar.db.repo import DEFAULT_DB_PATH, Repo, init_db, make_engine
from jobradar.pipeline.ingest import NOTION_UPSERT, PROCESS_MESSAGE, Ingest, make_emit
from jobradar.pipeline.links import ShortLinkExpander
from jobradar.queue.tasks import TaskQueue
from jobradar.queue.worker import Worker
from jobradar.sinks import notion
from jobradar.sources.telegram import TelegramSource

log = logging.getLogger(__name__)


def open_store() -> tuple[Repo, TaskQueue]:
    engine = make_engine(DEFAULT_DB_PATH)
    init_db(engine)
    repo = Repo(engine)
    return repo, TaskQueue(repo)


def make_ingest(
    settings: Settings, repo: Repo, queue: TaskQueue
) -> tuple[Ingest, ShortLinkExpander]:
    links = settings.config.links
    expander = ShortLinkExpander(extra_shorteners=links.extra_shorteners)
    return Ingest(repo, queue, expander, deny=links.deny), expander


def build_worker(ingest: Ingest, sink: notion.NotionSink, repo: Repo, queue: TaskQueue) -> Worker:
    return Worker(
        queue,
        {PROCESS_MESSAGE: ingest.process_message, NOTION_UPSERT: sink.handler(repo)},
        concurrency=4,
    )


async def run(settings: Settings) -> None:
    """`jobradar run`: Telegram in, Notion out, until Ctrl+C / SIGTERM."""
    settings.require("telegram", "notion")
    repo, queue = open_store()
    if reclaimed := queue.reclaim_stuck():
        log.info("re-queued tasks interrupted by the last shutdown", extra={"count": reclaimed})

    sink = await notion.connect(settings)
    ingest, expander = make_ingest(settings, repo, queue)
    if hidden := ingest.sweep_promos():
        log.info("hid promo links found in earlier posts", extra={"count": hidden})
    worker = build_worker(ingest, sink, repo, queue)
    source = TelegramSource(settings, repo)

    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows without WSL
            loop.add_signal_handler(sig, stopping.set)

    worker_task = asyncio.create_task(worker.run(), name="worker")
    source_task = asyncio.create_task(
        source.start(make_emit(repo, queue, worker.wake)), name="telegram"
    )
    stop_wait = asyncio.create_task(stopping.wait())
    try:
        done, _ = await asyncio.wait(
            {worker_task, source_task, stop_wait}, return_when=asyncio.FIRST_COMPLETED
        )
        for finished in done - {stop_wait}:
            finished.result()  # surface a crash in the source or worker
    finally:
        log.info("shutting down")
        await source.stop()
        worker.stop()
        stop_wait.cancel()
        await asyncio.gather(source_task, worker_task, return_exceptions=True)
        await expander.aclose()
        await sink.aclose()


async def drain(settings: Settings, max_seconds: float = 120) -> None:
    """Run queued work (e.g. after `jobradar add`) until nothing is ready, then return."""
    repo, queue = open_store()
    sink = await notion.connect(settings)
    ingest, expander = make_ingest(settings, repo, queue)
    worker = build_worker(ingest, sink, repo, queue)
    task = asyncio.create_task(worker.run())
    try:
        async with asyncio.timeout(max_seconds):
            # The queue lives in SQLite, so polling it is the simplest correct wait.
            while repo.has_ready_tasks([PROCESS_MESSAGE, NOTION_UPSERT]):  # noqa: ASYNC110
                await asyncio.sleep(0.2)
    finally:
        worker.stop()
        await task
        await expander.aclose()
        await sink.aclose()
