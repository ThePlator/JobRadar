"""Watch hiring-drive pages and pass what's new on them into the normal pipeline.

TCS NextStep, Infosys, Wipro Elite NTH and Cognizant GenC announce drives on pages, not in
job lists. A `pages_tick` task runs every 5 minutes and queues a `page_check` for each page
that is due (every `sources.pages.every_hours`, spread out per page). One check:

1. reads the page through the fetcher: robots.txt obeyed, one request per second per site,
   honest user agent, no logins; a page robots.txt disallows isn't read again until
   `jobradar pages resume`;
2. the **first** good read only records the text: no alerts for what was already there;
3. later reads compare line by line (timestamps and counters ignored); 80+ characters of new
   lines become one post from "TCS NextStep" that goes through ingest like a Telegram post;
4. a page that refuses (403/429) is read again after 2 h, then 4 h; the third refusal in a
   row pauses it until `jobradar pages resume`. A page that changed almost entirely is a
   redesign: its new text becomes the baseline and nothing is posted.

Ingest makes one job per link. A post carries the links found in the new lines (usually a
registration link); with none, it carries the page's own address, and that page's job is
read again so Notion shows the new details instead of the update vanishing as a duplicate.
"""

from __future__ import annotations

import hashlib
import logging
import zlib
from datetime import datetime, timedelta

from jobradar.clock import Clock, from_iso, to_iso, utcnow
from jobradar.config import PagesConfig
from jobradar.db.models import JobStatus, PageState, Platform, WatchedPage
from jobradar.db.repo import Repo, TaskRecord
from jobradar.pipeline.dedupe import job_id_for_url
from jobradar.pipeline.fetch import Fetcher
from jobradar.pipeline.links import canonicalise
from jobradar.pipeline.task_types import FETCH, FRESH, PAGE_CHECK, PAGES_TICK
from jobradar.queue.tasks import RetryableError, TaskQueue
from jobradar.sources.base import Emit, IncomingMessage
from jobradar.sources.pages.text import (
    MIN_ADDED,
    added_lines,
    clean_lines,
    is_redesign,
    links_in,
    readable,
)

log = logging.getLogger(__name__)

TICK_SECONDS = 300
REFUSED = (403, 429)
PAUSE_AFTER = 3  # refusals in a row
FIRST_WAIT = timedelta(hours=2)  # then doubled
ROBOTS = "blocked by robots.txt"


def post_text(name: str, url: str, added: str) -> str:
    return f"Source: {name} ({url})\n{added}"


def _offset(key: str, every: timedelta) -> timedelta:
    """A fixed spread per page, so pages on one site aren't all read in the same minute."""
    return timedelta(seconds=zlib.crc32(key.encode()) % max(int(every.total_seconds()), 1))


class PageWatcher:
    def __init__(
        self,
        config: PagesConfig,
        repo: Repo,
        queue: TaskQueue,
        emit: Emit,
        fetcher: Fetcher,
        clock: Clock = utcnow,
    ) -> None:
        self.config = config
        self.pages = {url: name for name, url in config.watch.items()}
        self.repo = repo
        self.queue = queue
        self.emit = emit
        self.fetcher = fetcher
        self.clock = clock

    # ---- scheduling ---------------------------------------------------------------------------

    def schedule(self, delay: float = 0, key: str = "a") -> None:
        self.queue.enqueue(PAGES_TICK, key, delay=delay)

    async def tick(self, task: TaskRecord) -> None:
        try:
            self.queue_due()
        finally:
            # The other key: re-queueing the running task would run it again at once.
            self.schedule(TICK_SECONDS, key="b" if task.key == "a" else "a")

    def due(self, url: str, now: datetime) -> bool:
        page = self.repo.get_watched_page(url, self.pages[url])
        if page.state in (PageState.PAUSED, PageState.BLOCKED):
            return False
        if page.wait_until and from_iso(page.wait_until) > now:
            return False
        if page.last_checked is None:
            return True
        every = timedelta(hours=self.config.every_hours)
        return now - from_iso(page.last_checked) >= every - _offset(url, every) / 4

    def queue_due(self) -> int:
        now = self.clock()
        due = [url for url in self.pages if self.due(url, now)]
        for url in due:
            self.queue.enqueue(PAGE_CHECK, url)
        return len(due)

    # ---- one check ----------------------------------------------------------------------------

    async def check(self, task: TaskRecord) -> None:
        if (name := self.pages.get(task.key)) is not None:
            await self.check_page(name, task.key)

    async def check_page(self, name: str, url: str) -> int:
        """Read one page. Returns how many posts it made (0 or 1)."""
        page = self.repo.get_watched_page(url, name)
        page.name = name
        now = self.clock()
        page.last_checked = to_iso(now)
        try:
            read = await self.fetcher.fetch(url, links=True)
        except RetryableError as e:
            if e.status in REFUSED:
                self._refused(page, str(e), now)
            else:
                page.last_error = str(e)[:200]  # unknown this round: nothing changes
                log.info("page unavailable", extra={"page": name, "error": page.last_error})
            self.repo.save_watched_page(page)
            return 0

        if read.text is None:
            note = read.note or "no readable text"
            if note == ROBOTS:
                page.state, page.last_error = PageState.BLOCKED.value, note
                log.warning("page will not be read", extra={"page": name, "why": note})
            elif note == "HTTP 403":
                self._refused(page, note, now)
            elif note == "login required":
                page.state = PageState.PAUSED.value
                page.last_error = "needs a login; JobRadar doesn't log in"
                log.warning("page needs a login; paused", extra={"page": name})
            else:  # incl. "not a public address", which a failed DNS lookup also gives
                page.last_error = note
                log.info("page not usable", extra={"page": name, "why": note})
            self.repo.save_watched_page(page)
            return 0

        lines = clean_lines(read.text)
        page.state, page.refusals, page.wait_until = PageState.WATCHING.value, 0, None
        page.last_error, page.last_ok = None, to_iso(now)
        posted = 0
        if page.text is None:
            log.info("page baseline: text recorded, no alerts", extra={"page": name})
        else:
            added = added_lines(page.text.splitlines(), lines)
            size = sum(len(line) for line in added)
            if added and is_redesign(lines, added):
                page.last_error = "most of the page changed at once; took a new baseline"
                log.warning("page redesigned; new baseline, nothing posted", extra={"page": name})
            elif size >= MIN_ADDED:
                await self._post(name, url, added, now)
                page.last_changed = to_iso(now)
                page.posts_total += 1
                posted = 1
            elif added:
                log.info("page changed a little; not an announcement",
                         extra={"page": name, "chars": size})  # fmt: skip
        page.text = "\n".join(lines)
        self.repo.save_watched_page(page)
        return posted

    def _refused(self, page: WatchedPage, why: str, now: datetime) -> None:
        page.refusals += 1
        page.last_error = why[:200]
        if page.refusals >= PAUSE_AFTER:
            page.state, page.wait_until = PageState.PAUSED.value, None
            log.warning("page refused 3 times in a row; paused (jobradar pages resume)",
                        extra={"page": page.name})  # fmt: skip
            return
        wait = FIRST_WAIT * 2 ** (page.refusals - 1)
        page.state, page.wait_until = PageState.BACKING_OFF.value, to_iso(now + wait)
        hours = wait / timedelta(hours=1)
        log.info("page refused; waiting", extra={"page": page.name, "hours": hours})

    async def _post(self, name: str, url: str, added: list[str], now: datetime) -> None:
        text = readable(added, url)
        links = links_in(added, url)
        digest = hashlib.sha1(f"{url}\n{text}".encode(), usedforsecurity=False).hexdigest()
        await self.emit(
            IncomingMessage(
                platform=Platform.PAGE,
                chat_id=f"page:{url}",
                chat_title=name,
                message_id=digest[:16],
                text=post_text(name, url, text),
                urls=links or [url],
                posted_at=now,
            )
        )
        log.info("page changed; posted the new part",
                 extra={"page": name, "lines": len(added), "links": len(links)})  # fmt: skip
        if not links:
            await self._reread(url)

    async def _reread(self, url: str) -> None:
        """The page itself is the job: read it again so Notion shows what changed. A job you
        moved in Notion is left alone."""
        canonical = await canonicalise(url)
        job = self.repo.get_job(job_id_for_url(canonical)) if canonical else None
        if job is None or job.user_status_at is not None:
            return  # a new job is fetched by ingest anyway
        if job.status in (JobStatus.HIDDEN, JobStatus.DISCARDED):
            self.repo.set_job_status(job.id, JobStatus.DISCOVERED, "page updated")
        self.queue.enqueue(FETCH, job.id, priority=FRESH)

    def resume(self, url: str) -> bool:
        """Watch a paused or blocked page again from its next turn (robots.txt is still
        checked on every read). False if it wasn't stopped."""
        page = self.repo.get_watched_page(url, self.pages.get(url, url))
        if page.state not in (PageState.PAUSED, PageState.BLOCKED):
            return False
        page.state, page.refusals, page.wait_until = PageState.WATCHING.value, 0, None
        page.last_error = None
        self.repo.save_watched_page(page)
        return True
