"""Read freehire.me's public job search and pass new postings into the normal pipeline.

freehire (github.com/strelov1/freehire, MIT) crawls ~90 hiring systems (Workday, Oracle,
iCIMS, Taleo, Phenom, …) into one catalogue with a keyless search API. One query covers
thousands of companies JobRadar has no reader for.

The search is "postings freehire first saw in the last 2 days", so a posting leaving the
results only means it got older, never that it closed: unlike a company board, nothing is
closed here. One check:

1. reads the search, newest first, up to `max_pages` pages; a filter the API ignored
   (`meta.ignored_params`) fails the read, since a dropped filter returns the whole
   catalogue and looks like a broad result;
2. the **first** good read only records what is there: no alerts for old postings;
3. later reads pass on postings not seen before, through the same India / tech / not
   senior filters as the boards (freehire's `countries=IN` also matches Indiana, USA),
   at most `MAX_NEW` per read;
4. errors, 429s and odd answers leave everything as it was until the next round.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import httpx

from jobradar.clock import Clock, to_iso, utcnow
from jobradar.config import FreehireConfig
from jobradar.db.models import Platform
from jobradar.db.repo import Repo, TaskRecord
from jobradar.pipeline.task_types import FREEHIRE_CHECK
from jobradar.queue.tasks import TaskQueue
from jobradar.sources.base import Emit, IncomingMessage
from jobradar.sources.boards.base import BoardUnavailable, parse_time
from jobradar.sources.boards.filters import why_skipped

log = logging.getLogger(__name__)

API = "https://freehire.me/api/v1/jobs/search"
USER_AGENT = "JobRadar/0.2 (+https://github.com/ThePlator/JobRadar)"
KEY = "freehire:search"  # its row in feed_board
PAGE = 100
WITHIN_DAYS = 2  # first seen by freehire in the last 2 days: overlaps reads, misses nothing
MAX_NEW = 20  # a safety net: more at once is a glitch, not a hiring spree
SEEN_KEEP = 5_000

_SOURCE_NAMES = {
    "workday": "Workday", "greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby",
    "smartrecruiters": "SmartRecruiters", "icims": "iCIMS", "oracle": "Oracle",
    "taleo": "Taleo", "successfactors": "SuccessFactors", "phenom": "Phenom",
    "eightfold": "Eightfold", "workable": "Workable", "telegram": "Telegram",
}  # fmt: skip


def source_name(source: str) -> str:
    return _SOURCE_NAMES.get(source.casefold(), source.replace("-", " ").title() or "freehire")


def post_text(job: dict[str, Any]) -> str:
    """The posting written the way channels write them, so the free extraction layer reads
    company, role and location without any LLM call."""
    lines = [
        f"Company: {job.get('company') or ''}".rstrip(" :"),
        f"Role: {job.get('title') or ''}",
        f"Location: {job['location']}" if job.get("location") else "",
        f"Found by freehire on {source_name(str(job.get('source') or ''))}",
        str(job.get("url") or ""),
    ]
    return "\n".join(line for line in lines if line and line != "Company")


def why_not(job: dict[str, Any]) -> str | None:
    """Why a freehire posting isn't worth a look; None when it is."""
    if job.get("closed_at"):
        return "closed"
    if job.get("is_tech") == "non_tech":
        return "not a tech role"
    if (job.get("reality") or {}).get("fake_freshness"):
        return "an old posting made to look new"
    return why_skipped(str(job.get("title") or ""), str(job.get("location") or ""))


class FreehireWatcher:
    def __init__(
        self,
        config: FreehireConfig,
        repo: Repo,
        queue: TaskQueue,
        emit: Emit,
        client: httpx.AsyncClient | None = None,
        clock: Clock = utcnow,
    ) -> None:
        self.config = config
        self.repo = repo
        self.queue = queue
        self.emit = emit
        self.client = client or httpx.AsyncClient(
            timeout=30, follow_redirects=False, headers={"User-Agent": USER_AGENT}
        )
        self.clock = clock

    async def aclose(self) -> None:
        await self.client.aclose()

    # ---- scheduling ---------------------------------------------------------------------------

    def schedule(self, delay: float = 0, key: str = "a") -> None:
        self.queue.enqueue(FREEHIRE_CHECK, key, delay=delay)

    async def tick(self, task: TaskRecord) -> None:
        try:
            await self.check()
        finally:
            # The other key: re-queueing the running task would run it again at once.
            self.schedule(self.config.every_minutes * 60, key="b" if task.key == "a" else "a")

    # ---- reading ------------------------------------------------------------------------------

    def _params(self, offset: int) -> dict[str, str]:
        params = {
            "countries": ",".join(self.config.countries),
            "seniority": ",".join(self.config.seniority),
            "open_within_days": str(WITHIN_DAYS),
            "reality_exclude": "likely-evergreen",
            "sort": "created_at",
            "order": "desc",
            "limit": str(PAGE),
            "offset": str(offset),
        }
        if self.config.categories:
            params["category"] = ",".join(self.config.categories)
        return params

    async def read(self) -> list[dict[str, Any]]:
        """Every posting in the search, newest first. Raises BoardUnavailable."""
        jobs: list[dict[str, Any]] = []
        for page in range(self.config.max_pages):
            try:
                resp = await self.client.get(API, params=self._params(page * PAGE))
            except httpx.HTTPError as e:
                raise BoardUnavailable(f"could not reach freehire: {type(e).__name__}") from e
            if resp.status_code != 200:
                raise BoardUnavailable(f"freehire answered {resp.status_code}")
            try:
                body = resp.json()
            except ValueError as e:
                raise BoardUnavailable("freehire sent something that isn't JSON") from e
            data, meta = body.get("data"), body.get("meta") or {}
            if not isinstance(data, list):
                raise BoardUnavailable("freehire answered in an unexpected shape")
            if ignored := meta.get("ignored_params"):
                # A dropped filter returns the whole catalogue: never treat that as a result.
                raise BoardUnavailable(f"freehire ignored the filters {ignored}")
            jobs.extend(j for j in data if isinstance(j, dict) and j.get("url"))
            if len(data) < PAGE or len(jobs) >= int(meta.get("total") or 0):
                break
        return jobs

    async def check(self) -> int:
        """One read. Returns how many postings were passed on."""
        board = self.repo.get_board(KEY)
        now = self.clock()
        board.last_checked = to_iso(now)
        try:
            jobs = await self.read()
        except BoardUnavailable as e:
            board.last_error = str(e)
            self.repo.save_board(board)
            log.info("freehire unavailable", extra={"error": str(e)[:200]})
            return 0

        current = {self._id(j): str(j["url"]) for j in jobs}
        seen: dict[str, str] | None = json.loads(board.seen_json) if board.seen_json else None
        board.last_error, board.last_ok = None, to_iso(now)
        passed: list[dict[str, Any]] = []
        if seen is None:
            log.info("freehire baseline: existing postings recorded, no alerts",
                     extra={"postings": len(current)})  # fmt: skip
        else:
            fresh = [j for j in jobs if self._id(j) not in seen]
            for job in fresh:
                why = why_not(job)
                about = {"title": str(job.get("title"))[:80],
                         "company": str(job.get("company"))[:40],
                         "where": str(job.get("location"))[:60]}  # fmt: skip
                if why is None:
                    passed.append(job)
                    log.info("freehire posting passed", extra=about)
                else:
                    log.info("freehire posting skipped", extra=about | {"why": why})
            if fresh:
                board.latest_json = self._latest(fresh[0], now)
            if len(passed) > MAX_NEW:
                log.warning("freehire passed on too many at once; keeping the newest 20",
                            extra={"new": len(passed)})  # fmt: skip
                passed = passed[:MAX_NEW]
            for job in passed:
                await self.emit(self._message(job, now))
            board.new_total += len(passed)
        # Older postings leave the search as they age: keep them in memory so they never
        # come back as new, but not forever.
        merged = {**(seen or {}), **current}
        board.seen_json = json.dumps(dict(list(merged.items())[-SEEN_KEEP:]))
        board.jobs_on_board = len(jobs)
        self.repo.save_board(board)
        return len(passed)

    @staticmethod
    def _id(job: dict[str, Any]) -> str:
        return str(job.get("public_slug") or job.get("external_id") or job.get("url"))

    def _message(self, job: dict[str, Any], now: datetime) -> IncomingMessage:
        source = str(job.get("source") or "freehire")
        return IncomingMessage(
            platform=Platform.BOARD,
            chat_id=f"freehire:{source}",
            chat_title=f"freehire · {source_name(source)}",
            message_id=self._id(job),
            text=post_text(job),
            urls=[str(job["url"])],
            # When freehire first saw it: a source's own date can be restated on every crawl.
            posted_at=parse_time(job.get("created_at")) or now,
        )

    @staticmethod
    def _latest(job: dict[str, Any], now: datetime) -> str:
        why = why_not(job)
        return json.dumps({
            "title": str(job.get("title") or ""), "where": str(job.get("location") or ""),
            "verdict": "passed" if why is None else why, "seen": to_iso(now),
        })  # fmt: skip
