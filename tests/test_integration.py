"""Fake source -> SQLite -> worker -> mock Notion, wired the same way `jobradar run` does."""

import asyncio
import json
from datetime import UTC, datetime

import httpx
import respx

from jobradar.config import AppConfig, Secrets, Settings
from jobradar.db.models import Platform, TaskStatus
from jobradar.db.repo import Repo
from jobradar.pipeline.ingest import Ingest, make_emit
from jobradar.queue.tasks import TaskQueue
from jobradar.queue.worker import Worker
from jobradar.sinks import notion
from jobradar.sinks.notion import SCHEMA
from jobradar.sources.base import IncomingMessage

API = notion.API_URL


def message(chat: str, mid: str, text: str) -> IncomingMessage:
    return IncomingMessage(
        platform=Platform.TELEGRAM,
        chat_id=chat,
        chat_title=f"Group {chat}",
        message_id=mid,
        text=text,
        posted_at=datetime(2026, 10, 8, 8, int(mid), tzinfo=UTC),
    )


async def test_two_groups_one_notion_page(repo: Repo, queue: TaskQueue) -> None:
    settings = Settings(
        config=AppConfig.model_validate({"sources": {"telegram": {"enabled": False}}}),
        secrets=Secrets.model_validate({"notion_token": "t", "notion_database_id": "db"}),
    )
    with respx.mock(base_url=API) as api:
        api.get("/databases/db").respond(json={"data_sources": [{"id": "ds"}]})
        api.get("/data_sources/ds").respond(
            json={"properties": {k: {"type": v} for k, v in SCHEMA.items()}}
        )
        api.post("/data_sources/ds/query").respond(json={"results": []})
        create = api.post("/pages").respond(json={"id": "page-1"})
        update = api.patch("/pages/page-1").respond(json={"id": "page-1"})

        sink = await notion.connect(settings, http=httpx.AsyncClient(base_url=API))
        ingest = Ingest(repo, queue)
        worker = Worker(
            queue,
            {"process_message": ingest.process_message, "notion_upsert": sink.handler(repo)},
        )
        worker.poll_interval = 0.01
        emit = make_emit(repo, queue, worker.wake)
        runner = asyncio.create_task(worker.run())

        await emit(
            message("-1001", "1", "🔥 SDE Intern @ Acme\nhttps://acme.com/j/1?utm_source=tg")
        )
        async with asyncio.timeout(5):
            while not create.called:
                await asyncio.sleep(0.01)
        await emit(message("-1002", "2", "Acme intern https://www.acme.com/j/1/"))
        async with asyncio.timeout(5):
            while not update.called or repo.task_counts()[TaskStatus.PENDING]:
                await asyncio.sleep(0.01)

        worker.stop()
        await runner
        await sink.aclose()

    created = json.loads(create.calls[0].request.content)["properties"]
    assert created["Role"]["title"][0]["text"]["content"] == "SDE Intern @ Acme"
    assert created["Seen In"] == {"number": 1}
    assert create.call_count == 1
    assert json.loads(update.calls[-1].request.content)["properties"]["Seen In"] == {"number": 2}
    assert repo.task_counts()[TaskStatus.FAILED] == 0
