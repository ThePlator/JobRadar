"""From incoming message to job rows: store first, then extract links and dedupe."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable

from jobradar.clock import to_iso
from jobradar.db.repo import Repo, TaskRecord
from jobradar.pipeline.dedupe import job_id_for_url
from jobradar.pipeline.links import Expander, canonicalise, extract_urls
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
        for url in links:
            job_id = job_id_for_url(url)
            _, created = self.repo.get_or_create_job(job_id, url)
            seen_here_first = self.repo.add_job_source(job_id, raw.id)
            if created:
                log.info("new job", extra={"job_id": job_id[:10], "url": url})
            elif seen_here_first:
                log.info("duplicate job, added sighting", extra={"job_id": job_id[:10]})
            if created or seen_here_first:
                self.queue.enqueue(NOTION_UPSERT, job_id)
        self.repo.mark_processed(raw.id)
