"""fetch -> extract: turn a job link plus its Telegram post into structured JobPosting data."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from jobradar.clock import from_iso
from jobradar.db.models import JobStatus
from jobradar.db.repo import Repo, TaskRecord
from jobradar.llm import SchemaError, load_prompt
from jobradar.pipeline.fetch import Fetcher
from jobradar.pipeline.schemas import JobPosting
from jobradar.pipeline.task_types import EXTRACT, NOTION_BODY, NOTION_UPSERT
from jobradar.queue.tasks import MAX_ATTEMPTS, RetryableError, TaskQueue

if TYPE_CHECKING:
    from jobradar.llm import LLM

log = logging.getLogger(__name__)


PAGE_TEXT_LIMIT = 12_000
_SKIP = (JobStatus.HIDDEN, JobStatus.DISCARDED)


class Extractor:
    def __init__(
        self, repo: Repo, queue: TaskQueue, fetcher: Fetcher, llm: LLM, model: str
    ) -> None:
        self.repo = repo
        self.queue = queue
        self.fetcher = fetcher
        self.llm = llm
        self.model = model
        self.prompt = load_prompt("extract")

    async def fetch(self, task: TaskRecord) -> None:
        job = self.repo.get_job(task.key)
        if job is None or job.status in _SKIP:
            return
        if job.canonical_url:
            try:
                page = await self.fetcher.fetch(job.canonical_url)
            except RetryableError as e:
                if task.attempts < MAX_ATTEMPTS - 2:
                    raise
                # The site keeps failing: extract from the Telegram post alone.
                log.info("page kept failing; using the post text", extra={"job_id": job.id[:10]})
                self.repo.save_page(job.id, job.canonical_url, None, None, str(e)[:200])
            else:
                self.repo.save_page(job.id, page.final_url, page.title, page.text, page.note)
                if page.text is None:
                    log.info("page not usable", extra={"job_id": job.id[:10], "why": page.note})
        self.queue.enqueue(EXTRACT, job.id)

    async def extract(self, task: TaskRecord) -> None:
        job = self.repo.get_job(task.key)
        if job is None or job.status in _SKIP:
            return
        sightings = self.repo.job_sightings(job.id)
        first = sightings[0] if sightings else None
        page = self.repo.get_page(job.id)
        page_text = (page.text or "")[:PAGE_TEXT_LIMIT] if page else ""
        if page and page.title and page_text:
            page_text = f"{page.title}\n\n{page_text}"
        message = (first.text or "") if first else ""
        if not page_text and not message:
            self.repo.set_job_status(job.id, JobStatus.NEEDS_REVIEW, "nothing to read")
            return

        posted_on = from_iso(first.posted_at).date().isoformat() if first else ""
        try:
            posting = await self.llm.structured(
                self.model,
                self.prompt,
                JobPosting,
                posted_on=posted_on,
                message_text=message,
                page_url=(page.final_url if page else None) or job.canonical_url or "",
                page_text=page_text or "(page not available: use the message)",
            )
        except SchemaError as e:
            log.warning("extraction output unusable", extra={"job_id": job.id[:10]})
            self.repo.set_job_status(job.id, JobStatus.NEEDS_REVIEW, f"extraction failed: {e}")
            return

        status = JobStatus.EXTRACTED if posting.is_job else JobStatus.DISCARDED
        self.repo.save_extraction(
            job.id,
            posting.model_dump_json(),
            posting.company,
            posting.role,
            posting.deadline.isoformat() if posting.deadline else None,
            self.prompt.version,
            status,
        )
        log.info(
            "job extracted" if posting.is_job else "not a job posting",
            extra={"job_id": job.id[:10], "role": posting.role, "company": posting.company},
        )
        self.queue.enqueue(NOTION_UPSERT, job.id)
        if posting.is_job:
            self.queue.enqueue(NOTION_BODY, job.id)
