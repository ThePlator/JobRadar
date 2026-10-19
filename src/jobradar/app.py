"""Wires sources, workers and the scheduler into one long-running process."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from datetime import UTC, datetime

from jobradar.config import ConfigError, Settings
from jobradar.db.repo import DEFAULT_DB_PATH, Repo, init_db, make_engine
from jobradar.llm import LLM, load_prompt
from jobradar.pipeline.extract import Extractor, backfill_published, backfill_role_keys
from jobradar.pipeline.fetch import Fetcher
from jobradar.pipeline.ingest import Ingest, make_emit
from jobradar.pipeline.links import ShortLinkExpander
from jobradar.pipeline.liveness import LivenessChecker
from jobradar.pipeline.match import Matcher
from jobradar.pipeline.task_types import (
    BOARD_CHECK,
    BOARDS_TICK,
    BUILD_RESUME,
    CLOSING_CHECK,
    DIGEST,
    EXTRACT,
    FETCH,
    FOLLOWUP_CHECK,
    FREEHIRE_CHECK,
    MATCH,
    NOTION_BODY,
    NOTION_POLL,
    NOTION_STATUS,
    NOTION_UPSERT,
    PAGE_CHECK,
    PAGES_TICK,
    PROCESS_MESSAGE,
    REPLY_CHECK,
    SEND_ALERTS,
)
from jobradar.profile import compact_profile, load_profile, profile_version, skill_warnings
from jobradar.providers.base import MatchProvider
from jobradar.providers.llm import LLMProvider
from jobradar.providers.skills_rule import SkillsRule
from jobradar.queue.tasks import TaskQueue
from jobradar.queue.worker import Handler, Worker
from jobradar.resume.build import ResumeBuilder
from jobradar.sinks import notion
from jobradar.sinks.digest import Digest
from jobradar.sinks.email_actions import Actions, ReplyReader
from jobradar.sinks.email_notify import AlertSender
from jobradar.sinks.followups import FollowUpReminder
from jobradar.sinks.notion_sync import StatusSync
from jobradar.sinks.reminders import ClosingReminder
from jobradar.sources.boards.freehire import FreehireWatcher
from jobradar.sources.boards.watcher import BoardWatcher
from jobradar.sources.pages.watcher import PageWatcher
from jobradar.sources.telegram import TelegramSource

log = logging.getLogger(__name__)


def open_store() -> tuple[Repo, TaskQueue]:
    engine = make_engine(DEFAULT_DB_PATH)
    init_db(engine)
    repo = Repo(engine)
    return repo, TaskQueue(repo)


def llm_enabled(settings: Settings) -> bool:
    if missing := settings.missing("llm"):
        log.warning(
            "LLM key missing: jobs are read from page data and post labels only. "
            "Add to .env: " + ", ".join(missing)
        )
        return False
    return True


def make_ingest(
    settings: Settings, repo: Repo, queue: TaskQueue
) -> tuple[Ingest, ShortLinkExpander]:
    links = settings.config.links
    expander = ShortLinkExpander(extra_shorteners=links.extra_shorteners)
    ingest = Ingest(repo, queue, expander, deny=links.deny, extract=True)
    return ingest, expander


def make_alerts(settings: Settings, repo: Repo, queue: TaskQueue) -> AlertSender | None:
    """Email alerts, or None (with a warning) when they are off or email isn't set up."""
    notify = settings.config.notify
    if not notify.alerts:
        return None
    if not notify.dry_run and (missing := settings.missing("email")):
        log.warning("email alerts are off: add to .env " + ", ".join(missing))
        return None
    return AlertSender(settings, repo, queue)


def make_digest(settings: Settings, repo: Repo, queue: TaskQueue) -> Digest | None:
    """The digest, or None (with a warning) when it is off or email isn't set up."""
    notify = settings.config.notify
    if not notify.digest or not notify.digest_times:
        return None
    if not notify.dry_run and (missing := settings.missing("email")):
        log.warning("digest is off: add to .env " + ", ".join(missing))
        return None
    return Digest(settings, repo, queue)


def make_reminders(
    settings: Settings, repo: Repo, queue: TaskQueue, liveness: LivenessChecker
) -> ClosingReminder | None:
    """Closing-soon reminders, or None when they are off or email isn't set up."""
    notify = settings.config.notify
    if not notify.closing_reminders:
        return None
    if not notify.dry_run and (missing := settings.missing("email")):
        log.warning("closing reminders are off: add to .env " + ", ".join(missing))
        return None
    return ClosingReminder(settings, repo, queue, liveness)


def make_followups(settings: Settings, repo: Repo, queue: TaskQueue) -> FollowUpReminder | None:
    """Follow-up reminders, or None when they are off or email isn't set up."""
    notify = settings.config.notify
    if not notify.followups:
        return None
    if not notify.dry_run and (missing := settings.missing("email")):
        log.warning("follow-up reminders are off: add to .env " + ", ".join(missing))
        return None
    return FollowUpReminder(settings, repo, queue)


def make_replies(settings: Settings, repo: Repo, queue: TaskQueue) -> ReplyReader | None:
    """The reader for "I applied" / "Not interested" button emails, or None when it's off."""
    if (actions := Actions.load(settings, repo)) is None:
        return None
    if missing := settings.missing("email"):
        log.warning("email buttons are off: add to .env " + ", ".join(missing))
        return None
    return ReplyReader(settings, repo, queue, actions)


def make_matcher(
    settings: Settings,
    repo: Repo,
    queue: TaskQueue,
    llm: LLM | None,
    alerts: AlertSender | None = None,
    liveness: LivenessChecker | None = None,
    build_resumes: bool = False,
) -> Matcher | None:
    """The matcher, or None (with a warning) when matching is off or has no profile."""
    config = settings.config
    if not config.matching.enabled:
        return None
    try:
        full = load_profile(config.matching.profile_path)
        for warning in skill_warnings(full):
            log.warning("profile.yaml: " + warning)
        profile = compact_profile(full, config.filters, config.matching)
    except ConfigError as e:
        log.warning("matching is off: " + str(e))
        return None
    providers: list[MatchProvider] = []
    for name in config.matching.providers:
        if name == "llm" and llm is not None:
            providers.append(
                LLMProvider(llm, config.llm.extract_model, config.matching.branch_strict)
            )
        elif name == "skills_rule":
            providers.append(SkillsRule())
    if not providers:
        log.warning("matching is off: no provider available (add an LLM key or skills_rule)")
        return None
    # A new prompt asks different questions: match open jobs again, as for a profile edit.
    version = profile_version({**profile, "_prompt": load_prompt("match").version})
    return Matcher(
        repo, queue, providers, profile, version, config.matching,
        config.filters, config.scoring, alerts,
        alert_max_age_days=config.notify.alert_max_age_days, tz=config.notify.tz,
        old_posting_days=config.notify.old_posting_days,
        liveness=liveness, build_resumes=build_resumes,
    )  # fmt: skip


class Pipeline:
    """Everything the worker runs, plus what needs closing on shutdown."""

    def __init__(self, settings: Settings, repo: Repo, queue: TaskQueue, sink: notion.NotionSink):
        self.ingest, self.expander = make_ingest(settings, repo, queue)
        self.fetcher = Fetcher()
        handlers: dict[str, Handler] = {
            PROCESS_MESSAGE: self.ingest.process_message,
            NOTION_UPSERT: sink.handler(repo),
            NOTION_BODY: sink.body_handler(repo),
            NOTION_STATUS: sink.status_handler(repo),
        }
        llm_config = settings.config.llm
        llm = LLM(settings, repo) if llm_enabled(settings) else None  # shared: one rate limit
        alerts = make_alerts(settings, repo, queue)
        self.liveness = LivenessChecker()
        self.resumes = (
            ResumeBuilder(settings, repo, queue, llm) if settings.config.resume.enabled else None
        )
        build = self.resumes is not None
        self.matcher = make_matcher(settings, repo, queue, llm, alerts, self.liveness, build)
        extractor = Extractor(
            repo,
            queue,
            self.fetcher,
            llm,
            llm_config.extract_model,
            llm_config.extract_mode,
            match=self.matcher is not None,
        )
        handlers[FETCH], handlers[EXTRACT] = extractor.fetch, extractor.extract
        if self.matcher is not None:
            handlers[MATCH] = self.matcher.match
        if alerts is not None:
            handlers[SEND_ALERTS] = alerts.send
        self.status_sync = StatusSync(
            repo, queue, sink.client, sink.data_source_id, build_resumes=build
        )
        if self.resumes is not None:
            handlers[BUILD_RESUME] = self.resumes.build
        handlers[NOTION_POLL] = self.status_sync.poll
        boards = settings.config.sources.boards
        self.boards: BoardWatcher | None = None
        if boards.enabled and boards.companies:
            self.boards = BoardWatcher(boards, repo, queue, make_emit(repo, queue))
            handlers[BOARDS_TICK], handlers[BOARD_CHECK] = self.boards.tick, self.boards.check
        pages = settings.config.sources.pages
        self.pages: PageWatcher | None = None
        if pages.enabled and pages.watch:
            # The shared fetcher: one robots.txt cache and one per-site pace for everything.
            self.pages = PageWatcher(pages, repo, queue, make_emit(repo, queue), self.fetcher)
            handlers[PAGES_TICK], handlers[PAGE_CHECK] = self.pages.tick, self.pages.check
        self.freehire: FreehireWatcher | None = None
        if settings.config.sources.freehire.enabled:
            self.freehire = FreehireWatcher(
                settings.config.sources.freehire, repo, queue, make_emit(repo, queue)
            )
            handlers[FREEHIRE_CHECK] = self.freehire.tick
        self.digest = make_digest(settings, repo, queue)
        if self.digest is not None:
            handlers[DIGEST] = self.digest.send
        self.reminders = make_reminders(settings, repo, queue, self.liveness)
        if self.reminders is not None:
            handlers[CLOSING_CHECK] = self.reminders.tick
        self.replies = make_replies(settings, repo, queue)
        if self.replies is not None:
            handlers[REPLY_CHECK] = self.replies.tick
        self.followups = make_followups(settings, repo, queue)
        if self.followups is not None:
            handlers[FOLLOWUP_CHECK] = self.followups.tick
        self.worker = Worker(queue, handlers, concurrency=4)

    async def aclose(self) -> None:
        if self.boards is not None:
            await self.boards.aclose()
        if self.freehire is not None:
            await self.freehire.aclose()
        await self.expander.aclose()
        await self.fetcher.aclose()
        await self.liveness.aclose()


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
    if keyed := backfill_role_keys(repo):
        log.info("role keys added to earlier jobs", extra={"count": keyed})
    if dated := backfill_published(repo, datetime.now(UTC).date()):
        log.info("publish dates added to earlier jobs", extra={"count": dated})
    if pipeline.matcher is not None:
        changed, queued = pipeline.matcher.catch_up()
        if changed or queued:
            log.info("matching caught up", extra={"changed": changed, "queued": queued})
    if sink.added - notion.USER_COLUMNS:  # columns JobRadar just added: fill them on old pages
        for job_id in (pages := repo.jobs_in_notion()):
            queue.enqueue(NOTION_UPSERT, job_id)
        log.info("filling new Notion columns", extra={"columns": ", ".join(sorted(sink.added)),
                                                     "pages": len(pages)})  # fmt: skip
    pipeline.status_sync.schedule()  # then every 2 minutes
    if pipeline.boards is not None:
        pipeline.boards.schedule()  # then every minute: read the boards that are due
        log.info("watching company boards", extra={"boards": len(pipeline.boards.boards)})
    if pipeline.pages is not None:
        pipeline.pages.schedule()  # then every 5 minutes: read the pages that are due
        log.info("watching pages", extra={"pages": len(pipeline.pages.pages)})
    if pipeline.freehire is not None:
        pipeline.freehire.schedule()  # then every sources.freehire.every_minutes
        log.info("reading freehire's job search")
    if pipeline.reminders is not None:
        pipeline.reminders.schedule()  # then every hour
    if pipeline.replies is not None:
        pipeline.replies.schedule()  # then every 2 minutes
        log.info("reading your email buttons (I applied / Not interested) from the inbox")
    if pipeline.followups is not None:
        pipeline.followups.schedule(60)  # then every hour; after the first Notion read
    if pipeline.digest is not None:
        at = pipeline.digest.schedule_next()
        log.info("next digest", extra={"at": f"{at:%Y-%m-%d %H:%M}"})
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
