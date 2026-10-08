"""Wires sources, workers and the scheduler into one long-running process."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from jobradar.config import Settings
from jobradar.db.repo import DEFAULT_DB_PATH, Repo, init_db, make_engine
from jobradar.llm import LLM
from jobradar.pipeline.extract import Extractor
from jobradar.pipeline.fetch import Fetcher
from jobradar.pipeline.ingest import Ingest, make_emit
from jobradar.pipeline.links import ShortLinkExpander
from jobradar.pipeline.task_types import (
    EXTRACT,
    FETCH,
    NOTION_BODY,
    NOTION_UPSERT,
    PROCESS_MESSAGE,
)
from jobradar.queue.tasks import TaskQueue
from jobradar.queue.worker import Handler, Worker
from jobradar.sinks import notion
from jobradar.sources.telegram import TelegramSource

log = logging.getLogger(__name__)


def open_store() -> tuple[Repo, TaskQueue]:
    engine = make_engine(DEFAULT_DB_PATH)
    init_db(engine)
    repo = Repo(engine)
    return repo, TaskQueue(repo)


def extraction_enabled(settings: Settings) -> bool:
    if missing := settings.missing("llm"):
        log.warning(
            "LLM key missing: jobs are listed but not read. Add to .env: " + ", ".join(missing)
        )
        return False
    return True


def make_ingest(
    settings: Settings, repo: Repo, queue: TaskQueue
) -> tuple[Ingest, ShortLinkExpander]:
    links = settings.config.links
    expander = ShortLinkExpander(extra_shorteners=links.extra_shorteners)
    ingest = Ingest(repo, queue, expander, deny=links.deny, extract=extraction_enabled(settings))
    return ingest, expander


class Pipeline:
    """Everything the worker runs, plus what needs closing on shutdown."""

    def __init__(self, settings: Settings, repo: Repo, queue: TaskQueue, sink: notion.NotionSink):
        self.ingest, self.expander = make_ingest(settings, repo, queue)
        self.fetcher = Fetcher()
        handlers: dict[str, Handler] = {
            PROCESS_MESSAGE: self.ingest.process_message,
            NOTION_UPSERT: sink.handler(repo),
            NOTION_BODY: sink.body_handler(repo),
        }
        if self.ingest.extract:
            llm = LLM(settings, repo)
            extractor = Extractor(repo, queue, self.fetcher, llm, settings.config.llm.extract_model)
            handlers |= {FETCH: extractor.fetch, EXTRACT: extractor.extract}
        self.worker = Worker(queue, handlers, concurrency=4)

    async def aclose(self) -> None:
        await self.expander.aclose()
        await self.fetcher.aclose()


async def run(settings: Settings) -> None:
    """`jobradar run`: Telegram in, Notion out, until Ctrl+C / SIGTERM."""
    settings.require("telegram", "notion")
    repo, queue = open_store()
    if reclaimed := queue.reclaim_stuck():
        log.info("re-queued tasks interrupted by the last shutdown", extra={"count": reclaimed})

    sink = await notion.connect(settings)
    pipeline = Pipeline(settings, repo, queue, sink)
    worker = pipeline.worker
    if hidden := pipeline.ingest.sweep_promos():
        log.info("hid promo links found in earlier posts", extra={"count": hidden})
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
        await pipeline.aclose()
        await sink.aclose()


async def drain(settings: Settings, max_seconds: float = 120) -> None:
    """Run queued work (e.g. after `jobradar add`) until nothing is ready, then return."""
    repo, queue = open_store()
    sink = await notion.connect(settings)
    pipeline = Pipeline(settings, repo, queue, sink)
    task = asyncio.create_task(pipeline.worker.run())
    try:
        async with asyncio.timeout(max_seconds):
            # The queue lives in SQLite, so polling it is the simplest correct wait.
            while repo.has_ready_tasks(list(pipeline.worker.handlers)):  # noqa: ASYNC110
                await asyncio.sleep(0.2)
    finally:
        pipeline.worker.stop()
        await task
        await pipeline.aclose()
        await sink.aclose()
