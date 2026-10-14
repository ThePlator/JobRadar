from typing import Any

import httpx
import pytest
import respx

from jobradar.db.models import JobStatus
from jobradar.db.repo import Repo
from jobradar.pipeline.liveness import AtsLink, Liveness, LivenessChecker, ats_link
from jobradar.pipeline.match import CLOSED_REASON
from jobradar.pipeline.task_types import MATCH, NOTION_UPSERT
from jobradar.queue.tasks import TaskQueue
from tests.test_match import GOOD, FakeProvider, matcher, run, seed_job

LEVER_ID = "0b8e2d5c-1f3a-4e7b-9c2d-6a5f4e3d2c1b"
GH_API = "https://boards-api.greenhouse.io/v1/boards/stripe/jobs/123"


@pytest.mark.parametrize(
    ("url", "link"),
    [
        ("https://job-boards.greenhouse.io/stripe/jobs/123?gh_src=x", AtsLink("greenhouse", "stripe", "123")),
        ("https://boards.greenhouse.io/embed/job_app?for=okta&token=456", AtsLink("greenhouse", "okta", "456")),
        (f"https://jobs.lever.co/meesho/{LEVER_ID}/apply", AtsLink("lever", "meesho", LEVER_ID)),
        (f"https://jobs.eu.lever.co/zeta/{LEVER_ID}", AtsLink("lever", "zeta", LEVER_ID, eu=True)),
        (f"https://jobs.ashbyhq.com/sarvam/{LEVER_ID}", AtsLink("ashby", "sarvam", LEVER_ID)),
        ("https://jobs.smartrecruiters.com/BoschGroup/744000012345678-software-engineer",
         AtsLink("smartrecruiters", "BoschGroup", "744000012345678")),
        ("https://job-boards.greenhouse.io/stripe", None),  # the board, not a posting
        ("https://job-boards.greenhouse.io/stripe/jobs/abc", None),  # ids are digits
        ("https://jobs.lever.co/meesho/not-a-uuid", None),
        ("https://evil.example/job-boards.greenhouse.io/stripe/jobs/1", None),
        ("https://job-boards.greenhouse.io.evil.example/stripe/jobs/1", None),
        ("https://boards.greenhouse.io/embed/job_app?for=a/b&token=1", None),
        ("ftp://jobs.lever.co/meesho/" + LEVER_ID, None),
        ("https://careers.acme.com/jobs/123", None),
        (None, None),
    ],
)  # fmt: skip
def test_ats_link(url: str | None, link: AtsLink | None) -> None:
    assert ats_link(url) == link


@pytest.mark.parametrize(
    ("response", "state"),
    [
        (httpx.Response(200, json={"id": 123}), Liveness.OPEN),
        (httpx.Response(404), Liveness.CLOSED),
        (httpx.Response(410), Liveness.CLOSED),
        (httpx.Response(429), Liveness.UNKNOWN),
        (httpx.Response(503), Liveness.UNKNOWN),
        (httpx.Response(301, headers={"Location": "https://elsewhere.example/"}), Liveness.UNKNOWN),
        (httpx.TimeoutException("slow"), Liveness.UNKNOWN),
    ],
)
@respx.mock
async def test_per_posting_answers(response: Any, state: Liveness) -> None:
    if isinstance(response, Exception):
        respx.get(GH_API).mock(side_effect=response)
    else:
        respx.get(GH_API).mock(return_value=response)
    checker = LivenessChecker()
    assert await checker.check("https://job-boards.greenhouse.io/stripe/jobs/123") == state
    await checker.aclose()


@respx.mock
async def test_each_platform_asks_its_own_api() -> None:
    respx.get(f"https://api.lever.co/v0/postings/meesho/{LEVER_ID}").respond(404)
    respx.get(f"https://api.eu.lever.co/v0/postings/zeta/{LEVER_ID}").respond(200, json={})
    respx.get("https://api.smartrecruiters.com/v1/companies/BoschGroup/postings/7440001").respond(
        410
    )
    checker = LivenessChecker()
    assert await checker.check(f"https://jobs.lever.co/meesho/{LEVER_ID}") == Liveness.CLOSED
    assert await checker.check(f"https://jobs.eu.lever.co/zeta/{LEVER_ID}") == Liveness.OPEN
    assert (
        await checker.check("https://jobs.smartrecruiters.com/BoschGroup/7440001-sde")
        == Liveness.CLOSED
    )
    assert await checker.check("https://careers.acme.com/jobs/1") == Liveness.UNKNOWN
    await checker.aclose()


@pytest.mark.parametrize(
    ("response", "state"),
    [
        (httpx.Response(200, json={"jobs": [{"id": LEVER_ID}]}), Liveness.OPEN),
        (httpx.Response(200, json={"jobs": [{"id": "other"}]}), Liveness.CLOSED),
        (httpx.Response(200, json={"jobs": [{"id": LEVER_ID, "isListed": False}]}), Liveness.CLOSED),
        (httpx.Response(200, json={"jobs": []}), Liveness.UNKNOWN),  # a glitch, not "all closed"
        (httpx.Response(200, text="<html>"), Liveness.UNKNOWN),
        (httpx.Response(404), Liveness.UNKNOWN),  # the board, not this posting
    ],
)  # fmt: skip
@respx.mock
async def test_ashby_checks_the_list(response: httpx.Response, state: Liveness) -> None:
    respx.get("https://api.ashbyhq.com/posting-api/job-board/sarvam").mock(return_value=response)
    checker = LivenessChecker()
    assert await checker.check(f"https://jobs.ashbyhq.com/sarvam/{LEVER_ID}") == state
    await checker.aclose()


# ---- in the matcher -------------------------------------------------------------------------


def seed_gh_job(repo: Repo) -> None:
    seed_job(repo, apply_url="https://job-boards.greenhouse.io/stripe/jobs/123")


@respx.mock
async def test_closed_posting_is_hidden_before_matching(repo: Repo, queue: TaskQueue) -> None:
    respx.get(GH_API).respond(404)
    seed_gh_job(repo)
    llm = FakeProvider("llm", GOOD)
    m = matcher(repo, queue, llm)
    m.liveness = LivenessChecker()
    queue.enqueue(MATCH, "job1")
    assert await run(queue, m) == [MATCH, NOTION_UPSERT]
    job = repo.get_job("job1")
    assert job is not None and job.status == JobStatus.HIDDEN
    assert job.score_reason == CLOSED_REASON
    assert llm.calls == 0  # no provider call for a closed posting
    await m.liveness.aclose()


@pytest.mark.parametrize("response", [httpx.Response(200, json={}), httpx.Response(503)])
@respx.mock
async def test_open_or_unknown_carries_on(
    repo: Repo, queue: TaskQueue, response: httpx.Response
) -> None:
    respx.get(GH_API).mock(return_value=response)
    seed_gh_job(repo)
    m = matcher(repo, queue, FakeProvider("llm", GOOD))
    m.liveness = LivenessChecker()
    queue.enqueue(MATCH, "job1")
    await run(queue, m)
    job = repo.get_job("job1")
    assert job is not None and job.status == JobStatus.NEW
    await m.liveness.aclose()


@respx.mock
async def test_only_the_first_match_asks(repo: Repo, queue: TaskQueue) -> None:
    route = respx.get(GH_API).respond(200, json={})
    seed_gh_job(repo)
    m = matcher(repo, queue, FakeProvider("llm", GOOD))
    m.liveness = LivenessChecker()
    queue.enqueue(MATCH, "job1")
    await run(queue, m)
    m.version = "v2"  # a profile edit re-matches the job
    queue.enqueue(MATCH, "job1")
    await run(queue, m)
    assert route.call_count == 1
    await m.liveness.aclose()
