"""Watch company job boards and pass their new postings into the normal pipeline.

A `boards_tick` task runs every minute and queues a `board_check` for each board that is
due (every `sources.boards.every_minutes`, spread out per board). One check:

1. reads the board; a 404 counts towards "gone" (3 in a row: retry after 30 days), any
   other error leaves everything as it was until the next round;
2. the **first** good check only records what is there: no alerts for old postings;
3. later checks compare with the last list: new postings that pass the filters become a
   labelled post from "Greenhouse · Stripe" and go through ingest like a Telegram post;
   postings that vanished close their job if it is still untouched in your Inbox;
4. a board that suddenly lists nothing is treated as suspicious, not as "everything closed".
"""

from __future__ import annotations

import json
import logging
import zlib
from datetime import datetime, timedelta

import httpx

from jobradar.catalog import watch_boards
from jobradar.clock import Clock, from_iso, to_iso, utcnow
from jobradar.config import BoardRef, BoardsConfig
from jobradar.db.models import JobStatus, Platform
from jobradar.db.repo import Repo, TaskRecord
from jobradar.pipeline.dedupe import job_id_for_url
from jobradar.pipeline.links import canonicalise
from jobradar.pipeline.task_types import BOARD_CHECK, BOARDS_TICK, NOTION_UPSERT
from jobradar.queue.tasks import TaskQueue
from jobradar.sources.base import Emit, IncomingMessage
from jobradar.sources.boards.base import BoardGone, BoardPosting, BoardUnavailable, Partial
from jobradar.sources.boards.filters import why_skipped
from jobradar.sources.boards.readers import READERS

log = logging.getLogger(__name__)

TICK_SECONDS = 60
GONE_AFTER = 3  # real 404s in a row
GONE_RETRY = timedelta(days=30)
CLOSED_REASON = "closed on the company board"
# Unofficial APIs are read less often than `every_minutes`, whatever it says.
MIN_EVERY_MINUTES = {"workday": 30, "oracle": 30}
# A board that lists more postings than its reader reads (Accenture: tens of thousands, in an
# order that changes between reads) can't be compared read to read: an unseen posting may just
# have been beyond the cap last time. Such reads pass nothing on; `jobradar boards` says why.
TOO_BIG = "lists more postings than JobRadar reads, in a changing order: nothing passed on"
# Readers that ask for newest first (Oracle sorts by posting date) are the exception: a new
# posting lands at the top of the list, inside the cap, so a capped read still shows it. Only
# the bottom of the list is cut off, so nothing there is closed, and the 3-day date rule
# keeps an old posting that just came into view from counting as new.
NEWEST_FIRST = ("oracle",)
TOP_ONLY = "lists more postings than JobRadar reads: new ones at the top are still passed on"
SEEN_KEEP = 5_000  # ids remembered for a board that's too big to read whole
# Workday's "Posted N Days Ago" and Oracle's PostedDate are the real publish date: an unseen
# posting older than this isn't new (it was re-listed or came into view), so it isn't passed on.
NEW_WITHIN = timedelta(days=3)
DATED_BOARDS = ("workday", "oracle")
# A safety net: more new postings than this at once is a glitch, not a hiring spree.
MAX_NEW_PER_CHECK = 10
USER_AGENT = "JobRadar/0.2 (+https://github.com/ThePlator/JobRadar)"


def post_text(board: BoardRef, posting: BoardPosting) -> str:
    """The posting written the way channels write them, so the free extraction layer reads
    company, role and location without any LLM call."""
    lines = [
        f"Company: {board.name or board.company}",
        f"Role: {posting.title}",
        f"Location: {posting.location}" if posting.location else "",
        f"Posted on {board.label}",
        posting.url,
    ]
    return "\n".join(line for line in lines if line)


def latest(postings: list[BoardPosting], seen_at: datetime) -> str:
    """The newest of these postings, with what the filters made of it, for `jobradar boards`."""
    newest = max(postings, key=lambda p: p.posted_at.timestamp() if p.posted_at else 0.0)
    why = why_skipped(newest.title, newest.location, newest.level)
    return json.dumps({
        "title": newest.title, "where": newest.location,
        "verdict": "passed" if why is None else why, "seen": to_iso(seen_at),
    })  # fmt: skip


def _offset(key: str, every: timedelta) -> timedelta:
    """A fixed spread per board, so 20 boards aren't all read in the same minute."""
    return timedelta(seconds=zlib.crc32(key.encode()) % max(int(every.total_seconds()), 1))


class BoardWatcher:
    def __init__(
        self,
        config: BoardsConfig,
        repo: Repo,
        queue: TaskQueue,
        emit: Emit,
        client: httpx.AsyncClient | None = None,
        clock: Clock = utcnow,
    ) -> None:
        self.config = config
        self.boards = {b.key: b for b in config.companies}
        self.repo = repo
        self.queue = queue
        self.emit = emit
        self.client = client or httpx.AsyncClient(
            timeout=20, follow_redirects=False, headers={"User-Agent": USER_AGENT}
        )
        self.clock = clock

    async def aclose(self) -> None:
        await self.client.aclose()

    # ---- scheduling ---------------------------------------------------------------------------

    def schedule(self, delay: float = 0, key: str = "a") -> None:
        self.queue.enqueue(BOARDS_TICK, key, delay=delay)

    async def tick(self, task: TaskRecord) -> None:
        try:
            self.queue_due()
        finally:
            # The other key: re-queueing the running task would run it again at once.
            self.schedule(TICK_SECONDS, key="b" if task.key == "a" else "a")

    def watching(self) -> dict[str, BoardRef]:
        """Config boards plus approved suggestions, minus any an admin has turned down."""
        return watch_boards(self.boards, self.repo)

    def due(self, key: str, now: datetime) -> bool:
        ref = self.boards.get(key) or self.watching().get(key)
        if ref is None:
            return False
        board = self.repo.get_board(key)
        if board.skip_until and from_iso(board.skip_until) > now:
            return False
        if board.last_checked is None:
            return True
        ats = ref.ats
        every = timedelta(minutes=max(self.config.every_minutes, MIN_EVERY_MINUTES.get(ats, 0)))
        # Spread checks out: each board's turn comes at its own offset within the interval.
        return now - from_iso(board.last_checked) >= every - _offset(key, every) / 4

    def queue_due(self) -> int:
        now = self.clock()
        due = [key for key in self.watching() if self.due(key, now)]
        for key in due:
            self.queue.enqueue(BOARD_CHECK, key)
        return len(due)

    # ---- one check ----------------------------------------------------------------------------

    async def check(self, task: TaskRecord) -> None:
        if (ref := self.watching().get(task.key)) is not None:
            await self.check_board(ref)

    async def check_board(self, ref: BoardRef) -> None:
        board = self.repo.get_board(ref.key)
        now = self.clock()
        board.last_checked = to_iso(now)
        try:
            postings = await READERS[ref.ats](self.client, ref.company)
        except BoardGone as e:
            board.misses += 1
            board.last_error = str(e)
            if board.misses >= GONE_AFTER:
                board.skip_until = to_iso(now + GONE_RETRY)
                log.warning("board not found 3 times; retrying in 30 days: check its name",
                            extra={"board": ref.key})  # fmt: skip
            self.repo.save_board(board)
            return
        except BoardUnavailable as e:
            board.last_error = str(e)  # unknown this round: nothing changes
            self.repo.save_board(board)
            log.info("board unavailable", extra={"board": ref.key, "error": str(e)[:200]})
            return

        current = {p.id: p.url for p in postings}
        # A read that stopped at the reader's page cap: nothing in it is known to be new, and
        # postings it left out haven't closed.
        complete = not isinstance(postings, Partial)
        seen: dict[str, str] | None = json.loads(board.seen_json) if board.seen_json else None
        board.misses, board.skip_until, board.last_error = 0, None, None
        board.last_ok = to_iso(now)
        self.repo.note_source_ok(Platform.BOARD.value, ref.key, board.last_ok)
        if seen is None:
            if postings:
                board.latest_json = latest(postings, now)
            log.info("board baseline: existing postings recorded, no alerts",
                     extra={"board": ref.key, "postings": len(current)})  # fmt: skip
        elif seen and not current:
            # From some postings to none at once is far more often a glitch than a company
            # closing every opening: keep the old list.
            board.last_error = "listed no postings; kept the previous list"
            self.repo.save_board(board)
            log.warning("board listed nothing; ignoring this round", extra={"board": ref.key})
            return
        else:
            readable = complete or ref.ats in NEWEST_FIRST
            fresh = [p for p in postings if p.id not in seen] if readable else []
            if ref.ats in DATED_BOARDS:
                fresh = [p for p in fresh if p.posted_at and p.posted_at >= now - NEW_WITHIN]
            passed = []
            for posting in fresh:
                why = why_skipped(posting.title, posting.location, posting.level)
                about = {"board": ref.key, "title": posting.title[:80],
                         "where": posting.location[:60]}  # fmt: skip
                if why is None:
                    passed.append(posting)
                    log.info("board posting passed", extra=about)
                else:
                    log.info("board posting skipped", extra=about | {"why": why})
            if fresh:
                board.latest_json = latest(fresh, now)
            if len(passed) > MAX_NEW_PER_CHECK:
                log.warning("board passed on too many at once; keeping the first 10",
                            extra={"board": ref.key, "new": len(passed)})  # fmt: skip
                passed = passed[:MAX_NEW_PER_CHECK]
            for posting in passed:
                await self.emit(
                    IncomingMessage(
                        platform=Platform.BOARD,
                        chat_id=ref.key,
                        chat_title=ref.label,
                        message_id=posting.id,
                        text=post_text(ref, posting),
                        urls=[posting.url],
                        posted_at=posting.posted_at or now,
                    )
                )
            vanished = [url for pid, url in seen.items() if pid not in current] if complete else []
            closed = [await self._close(url) for url in vanished]
            board.new_total += len(passed)
            if fresh or any(closed):
                log.info("board checked", extra={"board": ref.key, "new": len(fresh),
                         "passed_filters": len(passed), "closed": sum(closed)})  # fmt: skip
        if board.latest_json is None and postings:  # boards read before this was kept
            board.latest_json = latest(postings, now)
        if not complete:
            board.last_error = TOP_ONLY if ref.ats in NEWEST_FIRST else TOO_BIG
            if seen is not None and ref.ats not in NEWEST_FIRST:
                log.info("board too big to track; nothing passed on", extra={"board": ref.key})
            # Ids still listed go last, so trimming to SEEN_KEEP drops ones that left first.
            kept = {pid: url for pid, url in (seen or {}).items() if pid not in current}
            current = dict(list({**kept, **current}.items())[-SEEN_KEEP:])
        board.seen_json = json.dumps(current)
        board.jobs_on_board = len(postings)
        self.repo.save_board(board)

    async def _close(self, url: str) -> bool:
        """Hide the job for a posting that left its board, if you haven't touched it."""
        canonical = await canonicalise(url)
        if not canonical:
            return False
        job = self.repo.get_job(job_id_for_url(canonical))
        if job is None or job.user_status_at is not None:
            return False
        if job.status not in (JobStatus.DISCOVERED, JobStatus.EXTRACTED, JobStatus.NEW):
            return False
        self.repo.set_job_status(job.id, JobStatus.HIDDEN, CLOSED_REASON)
        self.queue.enqueue(NOTION_UPSERT, job.id)
        return True
