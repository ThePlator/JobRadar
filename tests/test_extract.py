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
    assert job.prompt_version == "rules-v1+extract-v1"  # no labels in the post: LLM ran
    data = json.loads(job.data_json or "{}")
    assert data["batch_years"] == [2026]
    assert data["_sources"]["role"] == "llm"
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


LABELLED = "Company: Acme\nRole: SDE Intern\nBatch: 2026\nhttps://acme.com/j/1"


async def test_labelled_post_skips_the_llm(repo: Repo, queue: TaskQueue, clock: FakeClock) -> None:
    seed(repo, LABELLED)
    fake = FakeCompletion()  # no replies: any LLM call would fail the test
    ex = extractor(repo, queue, clock, FakeFetcher(Page("u", None, "text")), fake)
    queue.enqueue(FETCH, "job1")
    assert await run(queue, ex) == [FETCH, EXTRACT, NOTION_UPSERT, NOTION_BODY]
    assert fake.calls == []
    job = repo.get_job("job1")
    assert job is not None and job.status == JobStatus.EXTRACTED
    assert (job.company, job.role, job.prompt_version) == ("Acme", "SDE Intern", "rules-v1")
    data = json.loads(job.data_json or "{}")
    assert data["batch_years"] == [2026]
    assert data["_sources"] == {
        "company": "post_labels", "role": "post_labels", "batch_years": "post_labels",
        "employment_type": "post_labels",  # "Intern" in the role
    }  # fmt: skip


async def test_page_data_wins_and_llm_fills_the_rest(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    seed(repo, "Role: Intern\nhttps://acme.com/j/1")  # no company in the post
    structured = {"@type": "JobPosting", "title": "SDE Intern", "validThrough": "2026-11-01"}
    fake = FakeCompletion(
        {"company": "Acme", "role": "Software Intern", "summary": "Intern.", "confidence": 0.9}
    )
    page = Page("u", None, "text", structured=structured)
    ex = extractor(repo, queue, clock, FakeFetcher(page), fake)
    queue.enqueue(FETCH, "job1")
    await run(queue, ex)
    assert len(fake.calls) == 1
    job = repo.get_job("job1")
    assert job is not None and (job.company, job.role) == ("Acme", "SDE Intern")
    data = json.loads(job.data_json or "{}")
    assert data["_sources"]["role"] == "structured"
    assert data["_sources"]["company"] == "llm"
    assert data["deadline"] == "2026-11-01"
    assert data["summary"] == "Intern."


async def test_post_with_several_jobs_does_not_lend_its_labels(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    seed(repo, LABELLED)
    repo.get_or_create_job("job2", "https://beta.com/j/2")  # a second live job in the same post
    raw_id = repo.job_sightings("job1")[0].message_id
    message = repo.get_raw_message(1)
    assert message is not None and message.id is not None and raw_id == "1"
    repo.add_job_source("job2", message.id)

    fake = FakeCompletion({"company": "Acme", "role": "SDE Intern", "confidence": 0.9})
    ex = extractor(repo, queue, clock, FakeFetcher(Page("u", None, "text")), fake)
    queue.enqueue(FETCH, "job1")
    await run(queue, ex)
    assert len(fake.calls) == 1  # the labels might describe job2, so the LLM reads it

    repo.set_job_status("job2", JobStatus.HIDDEN)  # job2 turned out to be an ad
    ex.llm = LLM(llm_settings(), repo, clock=clock, completion=FakeCompletion())
    queue.enqueue(EXTRACT, "job1")
    await run(queue, ex)  # now the labels are this job's own: no LLM call needed
    job = repo.get_job("job1")
    assert job is not None and job.prompt_version == "rules-v1"


async def test_always_mode_asks_the_llm_but_labels_win(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    seed(repo, LABELLED)
    fake = FakeCompletion(
        {"company": "ACME Inc", "role": "Intern", "summary": "Intern.", "confidence": 0.9}
    )
    ex = extractor(repo, queue, clock, FakeFetcher(Page("u", None, "text")), fake)
    ex.mode = "always"
    queue.enqueue(FETCH, "job1")
    await run(queue, ex)
    job = repo.get_job("job1")
    assert job is not None and (job.company, job.role) == ("Acme", "SDE Intern")
    assert json.loads(job.data_json or "{}")["summary"] == "Intern."


async def test_llm_failure_keeps_what_labels_found(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    seed(repo, LABELLED)
    ex = extractor(repo, queue, clock, FakeFetcher(Page("u", None, "text")), FakeCompletion())

    class Broken:
        async def structured(self, *a: Any, **k: Any) -> Any:
            raise SchemaError("nope")

    ex.llm = Broken()  # type: ignore[assignment]
    ex.mode = "always"
    queue.enqueue(FETCH, "job1")
    await run(queue, ex)
    job = repo.get_job("job1")
    assert job is not None and job.status == JobStatus.EXTRACTED
    assert job.prompt_version == "rules-v1"


async def test_without_an_llm_key(repo: Repo, queue: TaskQueue, clock: FakeClock) -> None:
    seed(repo, "Role: SDE Intern\nhttps://acme.com/j/1")  # partial: no company
    ex = extractor(repo, queue, clock, FakeFetcher(Page("u", None, "text")), FakeCompletion())
    ex.llm = None
    queue.enqueue(FETCH, "job1")
    assert await run(queue, ex) == [FETCH, EXTRACT, NOTION_UPSERT, NOTION_BODY]
    job = repo.get_job("job1")
    assert job is not None and (job.status, job.role) == (JobStatus.EXTRACTED, "SDE Intern")

    seed_other = "🔥 Great opening at Acme\nhttps://acme.com/j/3"  # nothing labelled
    src = repo.upsert_source(Platform.TELEGRAM, "-1001", "Jobs")
    assert src.id is not None
    repo.get_or_create_job("job3", "https://acme.com/j/3")
    raw_id, _ = repo.save_raw_message(src.id, "3", "2026-10-07T09:00:00.000000Z", seed_other)
    repo.add_job_source("job3", raw_id)
    queue.enqueue(EXTRACT, "job3")
    await run(queue, ex)
    job3 = repo.get_job("job3")
    assert job3 is not None and job3.status == JobStatus.NEEDS_REVIEW


def test_post_deadline_beats_boilerplate_page_deadline() -> None:
    from datetime import date

    from jobradar.pipeline.extract import free_fields

    page = {"@type": "JobPosting", "title": "SDE", "validThrough": "2026-10-31"}
    posted = date(2026, 10, 7)
    fields, sources = free_fields(page, "Last date: 20 October 2026", posted)
    assert (fields["deadline"], sources["deadline"]) == (date(2026, 10, 20), "post_labels")
    expired = {**page, "validThrough": "2026-07-31"}  # already past when posted
    assert "deadline" not in free_fields(expired, "Role: SDE", posted)[0]
    year_end = {**page, "validThrough": "2026-12-31"}  # months away: a blog's default
    assert "deadline" not in free_fields(year_end, None, posted)[0]
    assert free_fields(page, None, posted)[0]["deadline"] == date(2026, 10, 31)


async def test_matching_is_queued_after_extraction(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    from jobradar.pipeline.task_types import MATCH

    seed(repo, LABELLED)
    ex = extractor(repo, queue, clock, FakeFetcher(Page("u", None, "text")), FakeCompletion())
    ex.match = True
    queue.enqueue(FETCH, "job1")
    assert await run(queue, ex) == [FETCH, EXTRACT, NOTION_UPSERT, NOTION_BODY, MATCH]


def test_board_posts_keep_the_boards_company() -> None:
    from jobradar.pipeline.extract import free_fields

    page = {"@type": "JobPosting", "title": "Application Developer",
            "hiringOrganization": {"name": "8113 ASOL- Bangalore 7 SEZ Company"}}  # fmt: skip
    post = "Company: Accenture\nRole: Application Developer\nLocation: India"
    assert free_fields(page, post, None, from_board=True)[0]["company"] == "Accenture"
    assert free_fields(page, post, None)[0]["company"] != "Accenture"  # other posts: page first
