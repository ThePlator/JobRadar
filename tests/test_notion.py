import json
from typing import Any

import httpx
import pytest
import respx

from jobradar.config import AppConfig, ConfigError, Secrets, Settings
from jobradar.db.models import Job, Platform
from jobradar.db.repo import Repo, Sighting
from jobradar.queue.tasks import PermanentError, RetryableError
from jobradar.sinks import notion
from jobradar.sinks.notion import SCHEMA, NotionClient, build_properties, page_title

API = notion.API_URL
DS = "ds-1"


def settings() -> Settings:
    return Settings(
        config=AppConfig.model_validate({"sources": {"telegram": {"enabled": False}}}),
        secrets=Secrets.model_validate(
            {"notion_token": "ntn_test", "notion_database_id": "db-1"},
        ),
    )


def schema_response(
    overrides: dict[str, str] | None = None, drop: str | None = None
) -> dict[str, Any]:
    props = {
        name: {"type": kind} for name, kind in (SCHEMA | (overrides or {})).items() if name != drop
    }
    return {"object": "data_source", "properties": props}


@pytest.fixture
def mock_api() -> respx.MockRouter:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        router.get("/databases/db-1").respond(json={"data_sources": [{"id": DS, "name": "Jobs"}]})
        router.get(f"/data_sources/{DS}").respond(json=schema_response())
        yield router


def http() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=API)


async def test_connect_resolves_data_source_and_sends_api_version(
    mock_api: respx.MockRouter,
) -> None:
    sink = await notion.connect(settings(), http=http())
    assert sink.data_source_id == DS
    request = mock_api.calls[0].request
    assert request.headers["Notion-Version"] == "2025-09-03"
    assert request.headers["Authorization"] == "Bearer ntn_test"
    await sink.aclose()


async def test_connect_reports_schema_problems(mock_api: respx.MockRouter) -> None:
    mock_api.get(f"/data_sources/{DS}").respond(
        json=schema_response({"Status": "status"}, drop="Seen In")
    )
    with pytest.raises(ConfigError) as e:
        await notion.connect(settings(), http=http())
    assert "property 'Status' should be select, is status" in str(e.value)
    assert "missing property 'Seen In' (number)" in str(e.value)


async def test_connect_explains_unshared_database(mock_api: respx.MockRouter) -> None:
    mock_api.get("/databases/db-1").respond(404, json={"code": "object_not_found", "message": "no"})
    with pytest.raises(ConfigError, match="Connections"):
        await notion.connect(settings(), http=http())


async def test_error_mapping(mock_api: respx.MockRouter) -> None:
    client = NotionClient("t", http=http(), requests_per_second=1000)
    mock_api.patch("/pages/p1").respond(429, headers={"Retry-After": "7"})
    with pytest.raises(RetryableError) as e:
        await client.update_page("p1", {})
    assert e.value.retry_after == 7
    mock_api.patch("/pages/p2").respond(502)
    with pytest.raises(RetryableError):
        await client.update_page("p2", {})
    mock_api.patch("/pages/p3").respond(400, json={"code": "validation_error", "message": "bad"})
    with pytest.raises(PermanentError, match="validation_error"):
        await client.update_page("p3", {})
    await client.aclose()


def job(**kw: Any) -> Job:
    base = {
        "id": "abc",
        "canonical_url": "https://acme.com/j/1",
        "status": "discovered",
        "created_at": "x",
        "updated_at": "x",
    }
    return Job(**(base | kw))


def sighting(text: str | None, chat: str = "-1001234", title: str = "Jobs") -> Sighting:
    return Sighting("telegram", chat, title, "42", text, "2026-10-08T08:00:00.000000Z")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("🔥🔥 Backend Intern at Acme 🔥\nApply https://acme.com/j/1", "Backend Intern at Acme"),
        ("https://acme.com/j/1\n👉 SDE-1 | Bengaluru", "SDE-1 | Bengaluru"),
        ("https://acme.com/j/1", "https://acme.com/j/1"),
    ],
)
def test_page_title_from_message(text: str, expected: str) -> None:
    assert page_title(job(), [sighting(text)]) == expected


def test_properties_only_set_status_on_new_pages() -> None:
    sightings = [
        sighting("Role"),
        sighting("Role", chat="-100999"),
        sighting("dup", chat="-100999"),
    ]
    new = build_properties(job(), sightings, new_page=True)
    assert new["Status"] == {"select": {"name": "New"}}
    assert new["Seen In"] == {"number": 2}
    assert new["Apply Link"] == {"url": "https://acme.com/j/1"}
    assert new["Job ID"]["rich_text"][0]["text"]["content"] == "abc"
    assert "Status" not in build_properties(job(), sightings, new_page=False)


def test_long_text_is_split_into_2000_char_items() -> None:
    long = "x" * 4500
    props = build_properties(job(company=long), [], new_page=False)
    assert [len(t["text"]["content"]) for t in props["Company"]["rich_text"]] == [2000, 2000, 500]


async def test_upsert_create_update_and_deleted_page(
    mock_api: respx.MockRouter, repo: Repo
) -> None:
    src = repo.upsert_source(Platform.TELEGRAM, "-1001234", "Jobs")
    assert src.id is not None
    raw_id, _ = repo.save_raw_message(src.id, "42", "2026-10-08T08:00:00.000000Z", "Backend Intern")
    repo.get_or_create_job("abc", "https://acme.com/j/1")
    repo.add_job_source("abc", raw_id)
    sink = await notion.connect(settings(), http=http())

    query = mock_api.post(f"/data_sources/{DS}/query").respond(json={"results": []})
    create = mock_api.post("/pages").respond(json={"id": "page-1"})
    await sink.upsert(repo, "abc")
    body = json.loads(create.calls[0].request.content)
    assert query.called
    assert body["parent"] == {"type": "data_source_id", "data_source_id": DS}
    assert body["properties"]["Status"] == {"select": {"name": "New"}}
    toggle = body["children"][0]
    assert toggle["type"] == "toggle"
    link = toggle["toggle"]["children"][-1]["bulleted_list_item"]["rich_text"][0]["text"]["link"]
    assert link == {"url": "https://t.me/c/1234/42"}
    job_row = repo.get_job("abc")
    assert job_row is not None and job_row.notion_page_id == "page-1"

    update = mock_api.patch("/pages/page-1").respond(json={"id": "page-1"})
    await sink.upsert(repo, "abc")
    assert "Status" not in json.loads(update.calls[0].request.content)["properties"]

    mock_api.patch("/pages/page-1").respond(
        400, json={"message": "Can't edit block that is archived."}
    )
    await sink.upsert(repo, "abc")
    job_row = repo.get_job("abc")
    assert job_row is not None and job_row.notion_page_id is None
    await sink.aclose()
