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

from jobradar.clock import Clock, from_iso, to_iso, utcnow
from jobradar.config import BoardRef, BoardsConfig
from jobradar.db.models import JobStatus, Platform
from jobradar.db.repo import Repo, TaskRecord
from jobradar.pipeline.dedupe import job_id_for_url
from jobradar.pipeline.links import canonicalise
from jobradar.pipeline.task_types import BOARD_CHECK, BOARDS_TICK, NOTION_UPSERT
from jobradar.queue.tasks import TaskQueue
from jobradar.sources.base import Emit, IncomingMessage
from jobradar.sources.boards.base import BoardGone, BoardPosting, BoardUnavailable
from jobradar.sources.boards.filters import worth_a_look
from jobradar.sources.boards.readers import READERS

log = logging.getLogger(__name__)

TICK_SECONDS = 60
GONE_AFTER = 3  # real 404s in a row
GONE_RETRY = timedelta(days=30)
CLOSED_REASON = "closed on the company board"
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

    def due(self, key: str, now: datetime) -> bool:
        board = self.repo.get_board(key)
        if board.skip_until and from_iso(board.skip_until) > now:
            return False
        if board.last_checked is None:
            return True
        every = timedelta(minutes=self.config.every_minutes)
        # Spread checks out: each board's turn comes at its own offset within the interval.
        return now - from_iso(board.last_checked) >= every - _offset(key, every) / 4

    def queue_due(self) -> int:
        now = self.clock()
        due = [key for key in self.boards if self.due(key, now)]
        for key in due:
            self.queue.enqueue(BOARD_CHECK, key)
        return len(due)

    # ---- one check ----------------------------------------------------------------------------

    async def check(self, task: TaskRecord) -> None:
        if (ref := self.boards.get(task.key)) is not None:
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
        seen: dict[str, str] | None = json.loads(board.seen_json) if board.seen_json else None
        board.misses, board.skip_until, board.last_error = 0, None, None
        board.last_ok = to_iso(now)
        if seen is None:
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
            fresh = [p for p in postings if p.id not in seen]
            passed = [p for p in fresh if worth_a_look(p.title, p.location, p.level)]
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
            closed = [await self._close(url) for pid, url in seen.items() if pid not in current]
            board.new_total += len(passed)
            if fresh or any(closed):
                log.info("board checked", extra={"board": ref.key, "new": len(fresh),
                         "passed_filters": len(passed), "closed": sum(closed)})  # fmt: skip
        board.seen_json = json.dumps(current)
        board.jobs_on_board = len(current)
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
