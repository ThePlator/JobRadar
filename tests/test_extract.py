import json
from typing import Any

from jobradar.db.models import JobStatus, Platform
from jobradar.db.repo import Repo
from jobradar.llm import LLM, SchemaError
from jobradar.pipeline.extract import Extractor
from jobradar.pipeline.fetch import Page
from jobradar.pipeline.task_types import EXTRACT, FETCH, NOTION_BODY, NOTION_UPSERT
from jobradar.queue.tasks import RetryableError, TaskQueue
from tests.conftest import FakeClock
from tests.fakes import FakeCompletion, llm_settings

MODEL = "gemini/gemini-flash-lite-latest"


class FakeFetcher:
    def __init__(self, result: Page | Exception) -> None:
        self.result = result
        self.urls: list[str] = []

    async def fetch(self, url: str) -> Page:
        self.urls.append(url)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def seed(repo: Repo, text: str = "🔥 SDE Intern at Acme\nhttps://acme.com/j/1") -> None:
    src = repo.upsert_source(Platform.TELEGRAM, "-1001", "Jobs")
    assert src.id is not None
    raw_id, _ = repo.save_raw_message(src.id, "1", "2026-10-07T08:00:00.000000Z", text)
    repo.get_or_create_job("job1", "https://acme.com/j/1")
    repo.add_job_source("job1", raw_id)


def extractor(repo: Repo, queue: TaskQueue, clock: FakeClock, fetcher: Any, fake: Any) -> Extractor:
    llm = LLM(llm_settings(), repo, clock=clock, completion=fake)
    return Extractor(repo, queue, fetcher, llm, MODEL)


async def run(queue: TaskQueue, ex: Extractor) -> list[str]:
    done = []
    while (task := queue.claim()) is not None:
        done.append(task.type)
        if task.type == FETCH:
            await ex.fetch(task)
        elif task.type == EXTRACT:
            await ex.extract(task)
        queue.complete(task)
    return done


async def test_fetch_then_extract_fills_the_job(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    seed(repo)
    page = Page("https://acme.com/careers/1", "SDE Intern | Acme", "Python and SQL. Batch 2026.")
    fake = FakeCompletion(
        {"company": "Acme", "role": "SDE Intern", "skills_required": ["Python", "SQL"],
         "batch_years": [2026], "deadline": "2026-10-30", "summary": "Intern.", "confidence": 0.95}
    )  # fmt: skip
    ex = extractor(repo, queue, clock, FakeFetcher(page), fake)
    queue.enqueue(FETCH, "job1")

    assert await run(queue, ex) == [FETCH, EXTRACT, NOTION_UPSERT, NOTION_BODY]

    job = repo.get_job("job1")
    assert job is not None
    assert (job.company, job.role, job.deadline, job.status) == (
        "Acme", "SDE Intern", "2026-10-30", JobStatus.EXTRACTED.value,
    )  # fmt: skip
    assert job.prompt_version == "extract-v1"
    assert json.loads(job.data_json or "{}")["batch_years"] == [2026]
    prompt = fake.calls[0]["messages"][0]["content"]
    assert "<posted_on>2026-10-07</posted_on>" in prompt
    assert "SDE Intern | Acme\n\nPython and SQL" in prompt
    assert "🔥 SDE Intern at Acme" in prompt


async def test_unusable_page_falls_back_to_the_post(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    seed(repo)
    fake = FakeCompletion({"company": "Acme", "role": "SDE Intern", "confidence": 0.8})
    ex = extractor(
        repo, queue, clock, FakeFetcher(Page("https://x", None, None, "login required")), fake
    )
    queue.enqueue(FETCH, "job1")
    await run(queue, ex)
    assert "(page not available: use the message)" in fake.calls[0]["messages"][0]["content"]
    page = repo.get_page("job1")
    assert page is not None and page.note == "login required"


async def test_site_that_keeps_failing_is_skipped_late(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    seed(repo)
    ex = extractor(repo, queue, clock, FakeFetcher(RetryableError("503")), FakeCompletion())
    queue.enqueue(FETCH, "job1")
    task = queue.claim()
    assert task is not None
    try:
        await ex.fetch(task)  # early attempt: retry later
    except RetryableError:
        pass
    else:
        raise AssertionError("expected a retry")
    late = task.__class__(task.id, task.type, task.key, task.payload, attempts=4)
    await ex.fetch(late)  # late attempt: give up on the page, extract from the post
    page = repo.get_page("job1")
    assert page is not None and page.text is None
    assert queue.claim([EXTRACT]) is not None


async def test_not_a_job_and_bad_output(repo: Repo, queue: TaskQueue, clock: FakeClock) -> None:
    seed(repo)
    ex = extractor(repo, queue, clock, FakeFetcher(Page("u", None, "course ad")),
                   FakeCompletion({"summary": "A paid course.", "confidence": 0.1}))  # fmt: skip
    queue.enqueue(FETCH, "job1")
    assert await run(queue, ex) == [FETCH, EXTRACT, NOTION_UPSERT]  # no body for non-jobs
    job = repo.get_job("job1")
    assert job is not None and job.status == JobStatus.DISCARDED

    repo.set_job_status("job1", JobStatus.DISCOVERED)

    class Broken:
        async def structured(self, *a: Any, **k: Any) -> Any:
            raise SchemaError("nope")

    ex.llm = Broken()  # type: ignore[assignment]
    queue.enqueue(EXTRACT, "job1")
    await run(queue, ex)
    job = repo.get_job("job1")
    assert job is not None and job.status == JobStatus.NEEDS_REVIEW


async def test_hidden_jobs_are_never_fetched(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    seed(repo)
    repo.set_job_status("job1", JobStatus.HIDDEN)
    fetcher = FakeFetcher(Page("u", None, "x"))
    ex = extractor(repo, queue, clock, fetcher, FakeCompletion())
    queue.enqueue(FETCH, "job1")
    assert await run(queue, ex) == [FETCH]
    assert fetcher.urls == []
