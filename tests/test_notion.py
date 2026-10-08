import json
from typing import Any

import httpx
import pytest
import respx

from jobradar.config import AppConfig, ConfigError, Secrets, Settings
from jobradar.db.models import Job, JobStatus, Platform
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


def test_text_chunks_count_emoji_as_two_units() -> None:
    content = "🔥" * 3 + "x" * 1997  # 2000 characters, 2003 UTF-16 units
    pieces = [t["text"]["content"] for t in notion._text(content)]
    assert [len(p.encode("utf-16-le")) // 2 for p in pieces] == [2000, 3]
    assert "".join(pieces) == content


def test_hidden_job_sets_status_on_update_and_never_creates() -> None:
    props = build_properties(job(status="hidden"), [], new_page=False)
    assert props["Status"] == {"select": {"name": "Hidden"}}


async def test_hidden_job_without_page_is_not_created(
    mock_api: respx.MockRouter, repo: Repo
) -> None:
    repo.get_or_create_job("ad", "https://freshershunt.in/WhatsApp")
    repo.set_job_status("ad", JobStatus.HIDDEN)
    sink = await notion.connect(settings(), http=http())
    create = mock_api.post("/pages").respond(json={"id": "nope"})
    await sink.upsert(repo, "ad")
    assert not create.called
    await sink.aclose()


POSTING = {
    "company": "Acme", "role": "SDE Intern", "locations": ["Bengaluru", "Pune"],
    "work_mode": "hybrid", "experience_min": 0, "experience_max": 1,
    "skills_required": ["Python", "SQL, NoSQL"] + [f"S{i}" for i in range(12)],
    "salary_text": "₹30,000/month", "deadline": "2026-10-30",
    "apply_url": "https://acme.com/apply/1", "batch_years": [2026], "degrees": ["B.Tech"],
    "summary": "Paid internship.", "confidence": 0.9,
}  # fmt: skip


def extracted_job(**kw: Any) -> Job:
    return job(company="Acme", role="SDE Intern", status="extracted",
               data_json=json.dumps(POSTING), **kw)  # fmt: skip


def test_properties_from_extraction() -> None:
    props = build_properties(extracted_job(), [], new_page=False)
    assert props["Role"]["title"][0]["text"]["content"] == "SDE Intern @ Acme"
    assert props["Location"]["rich_text"][0]["text"]["content"] == "Bengaluru, Pune"
    assert props["Work Mode"] == {"select": {"name": "Hybrid"}}
    assert props["Experience"]["rich_text"][0]["text"]["content"] == "0-1 years"
    skills = [s["name"] for s in props["Skills"]["multi_select"]]
    assert skills[:2] == ["Python", "SQL  NoSQL"] and len(skills) == 10  # no commas, max 10
    assert props["Deadline"] == {"date": {"start": "2026-10-30"}}
    assert props["Apply Link"] == {"url": "https://acme.com/apply/1"}
    assert "Status" not in props


def test_not_a_job_is_hidden_on_update() -> None:
    props = build_properties(job(status="discarded"), [], new_page=False)
    assert props["Status"] == {"select": {"name": "Hidden"}}


async def test_rebuild_body_replaces_only_the_agent_toggle(
    mock_api: respx.MockRouter, repo: Repo
) -> None:
    repo.get_or_create_job("abc", "https://acme.com/j/1")
    repo.save_extraction("abc", json.dumps(POSTING), "Acme", "SDE Intern", "2026-10-30",
                         "extract-v1", JobStatus.EXTRACTED)  # fmt: skip
    repo.set_notion_page("abc", "page-1")
    mock_api.get("/blocks/page-1/children").respond(json={"results": [
        {"id": "agent", "type": "toggle", "toggle": {"rich_text": [{"plain_text": "JobRadar"}]}},
        {"id": "mine", "type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "my note"}]}},
    ], "has_more": False})  # fmt: skip
    deleted = mock_api.delete("/blocks/agent").respond(json={})
    appended = mock_api.patch("/blocks/page-1/children").respond(json={})
    sink = await notion.connect(settings(), http=http())

    await sink.rebuild_body(repo, "abc")

    assert deleted.called  # the user's "my note" block is never touched (unmocked = error)
    body = json.loads(appended.calls[0].request.content)["children"][0]
    texts = [b[b["type"]]["rich_text"][0]["text"]["content"] for b in body["toggle"]["children"]]
    assert texts[0] == "Paid internship."
    assert "Eligibility" in texts and "Batch: 2026" in texts and "Apply by: 2026-10-30" in texts
    await sink.aclose()


async def test_body_waits_for_the_page(mock_api: respx.MockRouter, repo: Repo) -> None:
    repo.get_or_create_job("abc", "https://acme.com/j/1")
    sink = await notion.connect(settings(), http=http())
    with pytest.raises(RetryableError, match="not created yet"):
        await sink.rebuild_body(repo, "abc")
    await sink.aclose()
