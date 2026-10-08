"""From incoming message to job rows: store first, then extract links and dedupe."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable

from jobradar.clock import to_iso
from jobradar.db.models import JobStatus
from jobradar.db.repo import Repo, TaskRecord
from jobradar.pipeline.dedupe import job_id_for_url
from jobradar.pipeline.links import Expander, canonicalise, extract_urls, is_denied
from jobradar.pipeline.promo import (
    MIN_DIFFERENT_JOBS,
    PROMO_REASON,
    PostContext,
    looks_like_promo,
)
from jobradar.queue.tasks import TaskQueue
from jobradar.sources.base import Emit, IncomingMessage

log = logging.getLogger(__name__)

PROCESS_MESSAGE = "process_message"
NOTION_UPSERT = "notion_upsert"


def make_emit(repo: Repo, queue: TaskQueue, on_enqueue: Callable[[], None] = lambda: None) -> Emit:
    """Persist each message before any processing (nothing is lost on a crash), then queue it."""
    source_ids: dict[tuple[str, str], int] = {}

    async def emit(msg: IncomingMessage) -> None:
        key = (msg.platform.value, msg.chat_id)
        if key not in source_ids:
            source = repo.upsert_source(msg.platform, msg.chat_id, msg.chat_title)
            if source.id is None:
                raise RuntimeError("source row has no id")
            source_ids[key] = source.id
        raw_id, created = repo.save_raw_message(
            source_ids[key],
            msg.message_id,
            to_iso(msg.posted_at),
            msg.text,
            msg.urls,
            msg.media_path,
        )
        if created:
            queue.enqueue(PROCESS_MESSAGE, str(raw_id))
            on_enqueue()

    return emit


class Ingest:
    def __init__(
        self,
        repo: Repo,
        queue: TaskQueue,
        expand: Expander | None = None,
        deny: Iterable[str] = (),
    ) -> None:
        self.repo = repo
        self.queue = queue
        self.expand = expand
        self.deny = tuple(deny)

    async def process_message(self, task: TaskRecord) -> None:
        raw = self.repo.get_raw_message(int(task.key))
        if raw is None or raw.id is None:
            return
        candidates = list(dict.fromkeys(json.loads(raw.urls_json) + extract_urls(raw.text)))
        links: list[str] = []
        for url in candidates:
            canonical = await canonicalise(url, self.expand, self.deny)
            if canonical and canonical not in links:
                links.append(canonical)

        if not links:
            # Text-only posts and poster images are handled by the LLM extractor (v0.2).
            log.info("message has no job link", extra={"raw_message_id": raw.id})
        # Record every link of the post first: the ad check looks at which links share a post.
        recorded = []
        for url in links:
            job_id = job_id_for_url(url)
            job, created = self.repo.get_or_create_job(job_id, url)
            seen_here_first = self.repo.add_job_source(job_id, raw.id)
            recorded.append((url, job_id, job.status, created, seen_here_first))
        for url, job_id, status, created, seen_here_first in recorded:
            if status == JobStatus.HIDDEN:
                continue  # already known to be an ad (or filtered out): nothing to sync
            if seen_here_first and not created and self._hide_if_promo(job_id):
                continue
            if created:
                log.info("new job", extra={"job_id": job_id[:10], "url": url})
            elif seen_here_first:
                log.info("duplicate job, added sighting", extra={"job_id": job_id[:10]})
            if created or seen_here_first:
                self.queue.enqueue(NOTION_UPSERT, job_id)
        self.repo.mark_processed(raw.id)

    def _hide(self, job_id: str, reason: str) -> None:
        self.repo.set_job_status(job_id, JobStatus.HIDDEN, reason)
        self.queue.enqueue(NOTION_UPSERT, job_id)  # flips an existing page to Hidden
        log.info("link hidden", extra={"job_id": job_id[:10], "reason": reason})

    def _hide_if_promo(self, job_id: str) -> bool:
        posts = [
            PostContext(text_, frozenset(others))
            for text_, others in self.repo.job_post_contexts(job_id)
        ]
        if not looks_like_promo(posts):
            return False
        self._hide(job_id, PROMO_REASON)
        return True

    def sweep_promos(self) -> int:
        """Apply today's link rules to jobs stored earlier: deny rules, then the ad check."""
        hidden = 0
        for job_id, url in self.repo.job_urls(JobStatus.DISCOVERED):
            if is_denied(url, self.deny):
                self._hide(job_id, "not a job link (deny rule)")
                hidden += 1
        candidates = self.repo.jobs_with_sightings(MIN_DIFFERENT_JOBS, JobStatus.DISCOVERED)
        return hidden + sum(self._hide_if_promo(job_id) for job_id in candidates)
