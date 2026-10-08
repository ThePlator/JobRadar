"""Notion sync: resolve the data source, check its schema, and upsert one page per job.

Uses the 2025-09-03 API (databases contain data sources). All calls share a rate limiter
(2.5 requests/second) and map HTTP errors onto the task queue's retry semantics.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from jobradar.config import ConfigError, Settings
from jobradar.db.models import Job, JobStatus
from jobradar.db.repo import Repo, Sighting, TaskRecord
from jobradar.pipeline.titles import title_from_post
from jobradar.queue.tasks import PermanentError, RetryableError

log = logging.getLogger(__name__)

API_URL = "https://api.notion.com/v1"
API_VERSION = "2025-09-03"
TEXT_LIMIT = 2000  # characters per rich-text item

# Property name -> Notion type, as shipped in the dashboard template (see PRD / LLD).
SCHEMA: dict[str, str] = {
    "Role": "title",
    "Company": "rich_text",
    "Match Score": "number",
    "Status": "select",
    "Deadline": "date",
    "Location": "rich_text",
    "Work Mode": "select",
    "Experience": "rich_text",
    "Skills": "multi_select",
    "Salary": "rich_text",
    "Apply Link": "url",
    "Resume": "url",
    "Seen In": "number",
    "Scam Risk": "select",
    "Job ID": "rich_text",
    "Cover Letter": "checkbox",
    "Notes": "rich_text",
    "Added": "created_time",
}

# Agent-side status -> Notion Status option. Written only when the page is created; after
# that the Status column belongs to the user.
STATUS_OPTION = {
    JobStatus.DISCOVERED: "New",
    JobStatus.EXTRACTED: "New",
    JobStatus.NEW: "New",
    JobStatus.HIDDEN: "Hidden",
}


class PageGone(Exception):
    """The page was deleted or archived in Notion."""


class RateLimiter:
    def __init__(self, per_second: float) -> None:
        self._interval = 1 / per_second
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self._interval


def _text(content: str) -> list[dict[str, Any]]:
    return [
        {"type": "text", "text": {"content": content[i : i + TEXT_LIMIT]}}
        for i in range(0, len(content), TEXT_LIMIT)
    ] or [{"type": "text", "text": {"content": ""}}]


class NotionClient:
    def __init__(
        self,
        token: str,
        http: httpx.AsyncClient | None = None,
        requests_per_second: float = 2.5,
    ) -> None:
        self._http = http or httpx.AsyncClient(base_url=API_URL, timeout=30.0)
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": API_VERSION,
            "Content-Type": "application/json",
        }
        self._limiter = RateLimiter(requests_per_second)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        await self._limiter.wait()
        try:
            resp = await self._http.request(method, path, json=body, headers=self._headers)
        except httpx.TransportError as e:
            raise RetryableError(f"Notion unreachable: {e}") from e
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "1"))
            raise RetryableError("Notion rate limit", retry_after=retry_after)
        if resp.status_code >= 500 or resp.status_code == 409:
            raise RetryableError(f"Notion {resp.status_code}: {resp.text[:300]}")
        if resp.status_code >= 400:
            data = resp.json() if resp.content else {}
            message = data.get("message", resp.text[:300])
            if resp.status_code == 404 or "archived" in message.lower():
                raise PageGone(message)
            raise PermanentError(f"Notion {resp.status_code} {data.get('code', '')}: {message}")
        return resp.json()

    async def resolve_data_source(self, database_id: str) -> str:
        try:
            db = await self.request("GET", f"/databases/{database_id}")
        except PageGone:
            raise ConfigError(
                "Notion database not found. Check NOTION_DATABASE_ID and that the database "
                "page is connected to your integration (••• → Connections)."
            ) from None
        sources = db.get("data_sources") or []
        if len(sources) != 1:
            raise ConfigError(
                f"The Notion database has {len(sources)} data sources; "
                "set NOTION_DATA_SOURCE_ID in .env to the one JobRadar should use."
            )
        return str(sources[0]["id"])

    async def schema_problems(self, data_source_id: str) -> list[str]:
        ds = await self.request("GET", f"/data_sources/{data_source_id}")
        have = {name: prop["type"] for name, prop in ds.get("properties", {}).items()}
        problems = []
        for name, kind in SCHEMA.items():
            if name not in have:
                problems.append(f"missing property '{name}' ({kind})")
            elif have[name] != kind:
                problems.append(f"property '{name}' should be {kind}, is {have[name]}")
        return problems

    async def find_page(self, data_source_id: str, job_id: str) -> str | None:
        result = await self.request(
            "POST",
            f"/data_sources/{data_source_id}/query",
            {"filter": {"property": "Job ID", "rich_text": {"equals": job_id}}, "page_size": 1},
        )
        pages = result.get("results") or []
        return str(pages[0]["id"]) if pages else None

    async def create_page(
        self, data_source_id: str, properties: dict[str, Any], children: list[dict[str, Any]]
    ) -> str:
        page = await self.request(
            "POST",
            "/pages",
            {
                "parent": {"type": "data_source_id", "data_source_id": data_source_id},
                "properties": properties,
                "children": children,
            },
        )
        return str(page["id"])

    async def update_page(self, page_id: str, properties: dict[str, Any]) -> None:
        await self.request("PATCH", f"/pages/{page_id}", {"properties": properties})


async def connect(settings: Settings, http: httpx.AsyncClient | None = None) -> NotionSink:
    """Open the client, resolve the data source and verify the schema (fails fast)."""
    settings.require("notion")
    token, database_id = settings.secrets.notion_token, settings.secrets.notion_database_id
    if token is None or database_id is None:  # narrowed for mypy; require() checked it
        raise ConfigError("Missing Notion settings")
    client = NotionClient(token.get_secret_value(), http=http)
    try:
        ds = settings.secrets.notion_data_source_id or await client.resolve_data_source(database_id)
        problems = await client.schema_problems(ds)
    except BaseException:
        await client.aclose()
        raise
    if problems:
        await client.aclose()
        raise ConfigError(
            "Notion database does not match the template:\n  - " + "\n  - ".join(problems)
        )
    return NotionSink(client, ds)


# ---- job -> page ----------------------------------------------------------------------------


def page_title(job: Job, sightings: list[Sighting]) -> str:
    if job.role:
        return f"{job.role} @ {job.company}" if job.company else job.role
    for s in sightings:
        if title := title_from_post(s.text):
            return title
    return job.canonical_url or job.id


def build_properties(job: Job, sightings: list[Sighting], *, new_page: bool) -> dict[str, Any]:
    props: dict[str, Any] = {
        "Role": {"title": _text(page_title(job, sightings))},
        "Seen In": {"number": len({(s.platform, s.chat_id) for s in sightings})},
        "Job ID": {"rich_text": _text(job.id)},
    }
    if job.company:
        props["Company"] = {"rich_text": _text(job.company)}
    if job.canonical_url and len(job.canonical_url) <= TEXT_LIMIT:
        props["Apply Link"] = {"url": job.canonical_url}
    if new_page:
        option = STATUS_OPTION.get(JobStatus(job.status))
        if option:
            props["Status"] = {"select": {"name": option}}
    return props


def message_link(s: Sighting) -> str | None:
    if s.platform == "telegram" and s.chat_id.startswith("-100"):
        return f"https://t.me/c/{s.chat_id.removeprefix('-100')}/{s.message_id}"
    return None


def build_body(sightings: list[Sighting]) -> list[dict[str, Any]]:
    """Agent-owned content lives inside one "JobRadar" toggle; anything else is the user's."""
    inner: list[dict[str, Any]] = []
    if sightings and sightings[0].text:
        inner.append(_block("paragraph", _text("Original post:")))
        inner.append(_block("quote", _text(sightings[0].text)))
    for s in sightings[:20]:
        label = f"Seen in {s.chat_title or s.chat_id} · {s.posted_at[:10]}"
        rich = _text(label)
        if link := message_link(s):
            rich[0]["text"]["link"] = {"url": link}
        inner.append(_block("bulleted_list_item", rich))
    toggle = _block("toggle", _text("JobRadar"))
    toggle["toggle"]["children"] = inner
    return [toggle]


def _block(kind: str, rich_text: list[dict[str, Any]]) -> dict[str, Any]:
    return {"object": "block", "type": kind, kind: {"rich_text": rich_text}}


class NotionSink:
    name = "notion"

    def __init__(self, client: NotionClient, data_source_id: str) -> None:
        self.client = client
        self.data_source_id = data_source_id

    async def aclose(self) -> None:
        await self.client.aclose()

    async def upsert(self, repo: Repo, job_id: str) -> None:
        job = repo.get_job(job_id)
        if job is None or job.status == JobStatus.DISCARDED:
            return
        sightings = repo.job_sightings(job_id)
        page_id = job.notion_page_id or await self.client.find_page(self.data_source_id, job_id)
        if page_id:
            try:
                await self.client.update_page(
                    page_id, build_properties(job, sightings, new_page=False)
                )
            except PageGone:
                # Deleted by the user: forget it; a later change to the job creates it again.
                log.info("notion page gone", extra={"job_id": job_id[:10]})
                repo.set_notion_page(job_id, None)
                return
            log.info("notion page updated", extra={"job_id": job_id[:10]})
        else:
            page_id = await self.client.create_page(
                self.data_source_id,
                build_properties(job, sightings, new_page=True),
                build_body(sightings),
            )
            log.info("notion page created", extra={"job_id": job_id[:10]})
        repo.set_notion_page(job_id, page_id)

    def handler(self, repo: Repo) -> Any:
        async def notion_upsert(task: TaskRecord) -> None:
            try:
                await self.upsert(repo, task.key)
            except PageGone as e:  # e.g. the data source itself vanished
                raise PermanentError(str(e)) from e

        return notion_upsert
