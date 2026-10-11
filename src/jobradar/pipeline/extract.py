"""fetch -> extract: turn a job link plus its Telegram post into structured JobPosting data.

Cheapest layer first; each layer fills only the fields still empty:
1. the schema.org JobPosting embedded in the page (structured.py),
2. the post's own "Company: / Role:" lines (post_labels.py),
3. the LLM, only when role or company is still missing (or on every job with
   `llm.extract_mode: always`).
`data_json["_sources"]` records which layer filled each field.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Literal

from jobradar.clock import from_iso
from jobradar.db.models import FetchedPage, JobStatus
from jobradar.db.repo import Repo, TaskRecord
from jobradar.llm import SchemaError, load_prompt
from jobradar.pipeline.fetch import Fetcher
from jobradar.pipeline.post_labels import from_post
from jobradar.pipeline.schemas import JobPosting
from jobradar.pipeline.structured import from_job_posting
from jobradar.pipeline.task_types import EXTRACT, MATCH, NOTION_BODY, NOTION_UPSERT
from jobradar.queue.tasks import MAX_ATTEMPTS, RetryableError, TaskQueue

if TYPE_CHECKING:
    from jobradar.llm import LLM

log = logging.getLogger(__name__)


PAGE_TEXT_LIMIT = 12_000
RULES_VERSION = "rules-v1"  # bump when structured.py / post_labels.py change meaning
_SKIP = (JobStatus.HIDDEN, JobStatus.DISCARDED)
_NOT_LIVE = (JobStatus.HIDDEN.value, JobStatus.DISCARDED.value)
_KEY = ("role", "company")
PAGE_DEADLINE_DAYS = 45  # page deadlines further out than this are usually boilerplate

ExtractMode = Literal["fallback", "always"]


def free_fields(
    structured: dict[str, Any] | None, post: str | None, posted: date | None
) -> tuple[dict[str, Any], dict[str, str]]:
    """Fields from the free layers, and which layer gave each one."""
    from_page = from_job_posting(structured) if structured else {}
    from_labels = from_post(post, posted.year if posted else None) if post else {}
    # Job blogs fill validThrough automatically (six months on, or 31 December), so a "Last
    # date" written in the post wins, and a page deadline is kept only if it is plausible.
    page_deadline = from_page.get("deadline")
    if page_deadline and (
        "deadline" in from_labels
        or (posted and not posted <= page_deadline <= posted + timedelta(PAGE_DEADLINE_DAYS))
    ):
        del from_page["deadline"]
    fields: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for layer, found in (("structured", from_page), ("post_labels", from_labels)):
        for name, value in found.items():
            if name not in fields:
                fields[name], sources[name] = value, layer
    return fields, sources


def own_post(repo: Repo, job_id: str) -> str | None:
    """The first post about only this job. A post that also links other live jobs is
    skipped: its "Company:" line may belong to one of them."""
    for text, others in repo.job_post_contexts(job_id):
        if text and all(
            (other := repo.get_job(o)) is None or other.status in _NOT_LIVE for o in others
        ):
            return text
    return None


class Extractor:
    def __init__(
        self,
        repo: Repo,
        queue: TaskQueue,
        fetcher: Fetcher,
        llm: LLM | None,
        model: str,
        mode: ExtractMode = "fallback",
        match: bool = False,
    ) -> None:
        self.repo = repo
        self.queue = queue
        self.fetcher = fetcher
        self.llm = llm  # None: no LLM key, free layers only
        self.model = model
        self.mode = mode
        self.match = match  # queue matching after extraction
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
                self.repo.save_page(
                    job.id, page.final_url, page.title, page.text, page.note, page.structured
                )
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
        posted = from_iso(first.posted_at).date() if first else None

        structured = json.loads(page.structured_json) if page and page.structured_json else None
        fields, sources = free_fields(structured, own_post(self.repo, job.id), posted)
        has_key_fields = all(fields.get(k) for k in _KEY)
        use_llm = self.llm is not None and (self.mode == "always" or not has_key_fields)

        if use_llm:
            message = (first.text or "") if first else ""
            found = await self._llm(job.id, page, job.canonical_url, message, posted)
            if isinstance(found, str):
                if not has_key_fields:
                    self.repo.set_job_status(job.id, JobStatus.NEEDS_REVIEW, found)
                    return
                is_job = True  # the LLM failed, but the free layers found the job
            else:
                for name in JobPosting.model_fields:
                    value = getattr(found, name)
                    if name not in fields and value not in (None, "", [], "unknown"):
                        fields[name], sources[name] = value, "llm"
                is_job = found.is_job
        elif fields:
            is_job = True  # without an LLM key a partial reading still beats none
        else:
            reason = "no labels found; add an LLM key to read it"
            self.repo.set_job_status(job.id, JobStatus.NEEDS_REVIEW, reason)
            return

        fields.setdefault("confidence", 0.9 if has_key_fields else 0.5)
        posting = JobPosting.model_validate(fields)
        used_llm = "llm" in sources.values()  # includes its confidence
        version = f"{RULES_VERSION}+{self.prompt.version}" if used_llm else RULES_VERSION
        self._save(job.id, posting, sources, version, is_job)

    async def _llm(
        self,
        job_id: str,
        page: FetchedPage | None,
        url: str | None,
        message: str,
        posted: date | None,
    ) -> JobPosting | str:
        """The LLM's reading of the job, or why there is none."""
        if self.llm is None:
            return "no LLM key"
        page_text = (page.text or "")[:PAGE_TEXT_LIMIT] if page else ""
        if page and page.title and page_text:
            page_text = f"{page.title}\n\n{page_text}"
        if not page_text and not message:
            return "nothing to read"
        try:
            return await self.llm.structured(
                self.model,
                self.prompt,
                JobPosting,
                posted_on=posted.isoformat() if posted else "",
                message_text=message,
                page_url=(page.final_url if page else None) or url or "",
                page_text=page_text or "(page not available: use the message)",
            )
        except SchemaError as e:
            log.warning("extraction output unusable", extra={"job_id": job_id[:10]})
            return f"extraction failed: {e}"

    def _save(
        self, job_id: str, posting: JobPosting, sources: dict[str, str], version: str, is_job: bool
    ) -> None:
        status = JobStatus.EXTRACTED if is_job else JobStatus.DISCARDED
        data = posting.model_dump(mode="json") | {"_sources": sources}
        self.repo.save_extraction(
            job_id,
            json.dumps(data, ensure_ascii=False),
            posting.company,
            posting.role,
            posting.deadline.isoformat() if posting.deadline else None,
            version,
            status,
        )
        log.info(
            "job extracted" if is_job else "not a job posting",
            extra={
                "job_id": job_id[:10], "role": posting.role, "company": posting.company,
                "llm": "llm" in sources.values(),
            },
        )  # fmt: skip
        self.queue.enqueue(NOTION_UPSERT, job_id)
        if is_job:
            self.queue.enqueue(NOTION_BODY, job_id)
            if self.match:
                self.queue.enqueue(MATCH, job_id)
