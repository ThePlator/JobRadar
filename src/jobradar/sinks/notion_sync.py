"""Notion read path: notice the Status you set on a job and copy it into JobRadar.

Every `POLL_SECONDS` a `notion_poll` task asks Notion for pages edited since the last poll
(minus a minute of overlap) and compares each page's Status with what JobRadar itself would
show. A difference is yours: the job takes that status for good, the change is written to
`status_change` (your application funnel), and JobRadar never hides, re-decides or alerts on
that job again. JobRadar's own edits (score, details) also touch pages, which is why only a
*different* Status counts.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from jobradar.clock import Clock, to_iso, utcnow
from jobradar.db.models import JobStatus
from jobradar.db.repo import Repo, TaskRecord
from jobradar.pipeline.task_types import NOTION_POLL
from jobradar.queue.tasks import TaskQueue
from jobradar.sinks.notion import STATUS_OPTION, NotionClient

log = logging.getLogger(__name__)

POLL_SECONDS = 120
OVERLAP = timedelta(minutes=1)
CURSOR_KEY = "notion_poll_since"
FIRST_SINCE = "2000-01-01T00:00:00.000000Z"  # the first poll reads every page once

# Notion Status option -> JobRadar status. Case doesn't matter.
FROM_NOTION: dict[str, JobStatus] = {
    "new": JobStatus.NEW,
    "shortlisted": JobStatus.SHORTLISTED,
    "applied": JobStatus.APPLIED,
    "interview": JobStatus.INTERVIEW,
    "offer": JobStatus.OFFER,
    "rejected": JobStatus.REJECTED,
    "skipped": JobStatus.SKIPPED,
    "hidden": JobStatus.HIDDEN,
}


def _plain(prop: dict[str, Any] | None) -> str:
    if not prop:
        return ""
    parts = prop.get(prop.get("type", ""), [])
    if isinstance(parts, list):
        return "".join(p.get("plain_text", "") for p in parts).strip()
    return ""


def page_status(page: dict[str, Any]) -> tuple[str, str | None]:
    """(Job ID, Status option or None) of one Notion page."""
    props = page.get("properties", {})
    select = (props.get("Status") or {}).get("select") or {}
    name = select.get("name")
    return _plain(props.get("Job ID")), (str(name) if name else None)


def shown_by_jobradar(status: str) -> str | None:
    """The Status option JobRadar itself writes for one of its own statuses."""
    try:
        return STATUS_OPTION.get(JobStatus(status))
    except ValueError:
        return None


class StatusSync:
    def __init__(
        self,
        repo: Repo,
        queue: TaskQueue,
        client: NotionClient,
        data_source_id: str,
        clock: Clock = utcnow,
    ) -> None:
        self.repo = repo
        self.queue = queue
        self.client = client
        self.data_source_id = data_source_id
        self.clock = clock
        self._unknown: set[str] = set()

    def schedule(self, delay: float = 0, key: str = "a") -> None:
        self.queue.enqueue(NOTION_POLL, key, delay=delay)

    async def poll(self, task: TaskRecord) -> None:
        try:
            await self.poll_now()
        finally:
            # Queue the next poll under the other key: re-queueing the task that is running
            # would make it run again at once instead of in two minutes.
            self.schedule(POLL_SECONDS, key="b" if task.key == "a" else "a")

    async def poll_now(self) -> int:
        """Read changed pages once; returns how many statuses you changed."""
        started = self.clock()
        since = self.repo.get_setting(CURSOR_KEY) or FIRST_SINCE
        pages = await self.client.changed_pages(self.data_source_id, since)
        changed = sum(self._apply(page) for page in pages)
        # Notion timestamps are minute-rounded and edits can land mid-poll: overlap a bit.
        self.repo.set_setting(CURSOR_KEY, to_iso(started - OVERLAP))
        if changed:
            log.info("statuses from Notion", extra={"changed": changed, "pages": len(pages)})
        return changed

    def _apply(self, page: dict[str, Any]) -> bool:
        job_id, option = page_status(page)
        if not job_id or not option:
            return False  # a page you made yourself, or no Status set
        status = FROM_NOTION.get(option.casefold())
        if status is None:
            if option not in self._unknown:
                self._unknown.add(option)
                log.warning("unknown Notion Status option; ignored", extra={"option": option})
            return False
        job = self.repo.get_job(job_id)
        if job is None:
            return False
        if job.user_status_at is None and shown_by_jobradar(job.status) == option:
            return False  # JobRadar's own value, e.g. "New" on a job it scored
        if job.user_status_at is not None and job.status == status.value:
            return False
        if self.repo.set_user_status(job_id, status):
            log.info("your status", extra={"job_id": job_id[:10], "status": status.value})
            return True
        return False
