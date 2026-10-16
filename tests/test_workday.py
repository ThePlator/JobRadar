import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from jobradar.config import BoardRef, BoardsConfig, board_from_link
from jobradar.db.models import JobStatus
from jobradar.db.repo import Repo
from jobradar.pipeline.dedupe import job_id_for_url
from jobradar.pipeline.liveness import Liveness, LivenessChecker, ats_link
from jobradar.queue.tasks import TaskQueue
from jobradar.sources.base import IncomingMessage
from jobradar.sources.boards import readers
from jobradar.sources.boards.base import BoardGone, BoardUnavailable, Partial
from jobradar.sources.boards.watcher import BoardWatcher
from tests.conftest import FakeClock

HOST = "nvidia.wd5.myworkdayjobs.com"
SITE = "NVIDIAExternalCareerSite"
BOARD = f"{HOST}/{SITE}"
API = f"https://{HOST}/wday/cxs/nvidia/{SITE}/jobs"
INDIA = "2fcb99c455831013ea52b82135ba3266"
NVIDIA = BoardRef(ats="workday", company=BOARD, name="NVIDIA")

# The shape NVIDIA's site answered with (2026-10-16): the country list is nested in a group.
FACETS = [
    {"facetParameter": "jobFamilyGroup", "descriptor": "Job Category",
     "values": [{"descriptor": "Engineering", "id": "e1", "count": 1743}]},
    {"facetParameter": "locationMainGroup", "values": [
        {"facetParameter": "locationHierarchy1", "descriptor": "Locations", "values": [
            {"descriptor": "Germany", "id": "de1", "count": 52},
            {"descriptor": "India", "id": INDIA, "count": 251},
        ]},
        {"facetParameter": "locations", "descriptor": "Sites", "values": [
            {"descriptor": "India, Pune", "id": "pune1", "count": 70},
        ]},
    ]},
]  # fmt: skip


def wd_job(
    n: int, title: str = "Software Engineer", where: str = "India, Pune", posted: str = "Today"
) -> dict[str, Any]:
    return {"title": title, "externalPath": f"/job/India-Pune/Job-{n}_JR{n:07d}-1",
            "locationsText": where, "postedOn": f"Posted {posted}", "bulletFields": [f"JR{n:07d}"]}  # fmt: skip


@pytest.fixture(autouse=True)
def fast_and_fresh(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(readers, "WD_PAGE_PAUSE", 0)
    readers._WD_INDIA.clear()
    yield
    readers._WD_INDIA.clear()


def answer(listing: list[dict[str, Any]], facets: Any = FACETS) -> Any:
    """Answers like the site: the filter lookup (limit 1, no filters), then India pages."""
    seen: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if not body["appliedFacets"]:
            return httpx.Response(200, json={"total": 2000, "jobPostings": [], "facets": facets})
        assert body["appliedFacets"] == {"locationHierarchy1": [INDIA]}
        assert body["limit"] == 20
        page = listing[body["offset"] : body["offset"] + body["limit"]]
        return httpx.Response(200, json={"total": len(listing), "jobPostings": page})

    respond.seen = seen  # type: ignore[attr-defined]
    return respond


# ---- config ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "link",
    [
        f"https://{HOST}/{SITE}",
        f"https://{HOST}/en-US/{SITE}/job/India-Pune/Senior-Designer_JR2027293-1",
        f"{HOST}/{SITE}",
        f"https://{HOST}/wday/cxs/nvidia/{SITE}/jobs",
    ],
)
def test_workday_links(link: str) -> None:
    assert board_from_link(link) == ("workday", BOARD)


def test_workday_config_section_and_label() -> None:
    config = BoardsConfig.model_validate({"workday": {"NVIDIA": f"https://{HOST}/{SITE}"}})
    assert config.companies == [NVIDIA]
    assert NVIDIA.key == f"workday:{BOARD}" and NVIDIA.label == "Workday · NVIDIA"
    assert BoardRef(ats="workday", company=BOARD).label == "Workday · nvidia"


@pytest.mark.parametrize(
    ("ats", "company"),
    [("workday", "nvidia"), ("workday", "evil.com/Site"), ("greenhouse", "a/b")],
)
def test_bad_board_names(ats: str, company: str) -> None:
    with pytest.raises(ValueError):
        BoardRef(ats=ats, company=company)  # type: ignore[arg-type]


# ---- the reader -----------------------------------------------------------------------------


@respx.mock
async def test_reads_india_pages_and_maps_postings() -> None:
    listing = [wd_job(n) for n in range(22)]
    listing[1] = wd_job(1, "Systems Engineer", "2 Locations")
    respond = answer(listing)
    respx.post(API).mock(side_effect=respond)
    async with httpx.AsyncClient() as client:
        postings = await readers.workday(client, BOARD)
        assert len(postings) == 22 and not isinstance(postings, Partial)
        first, second = postings[0], postings[1]
        assert first.id == "JR0000000" and first.location == "India, Pune"
        assert first.url == f"https://{HOST}/{SITE}/job/India-Pune/Job-0_JR0000000-1"
        assert first.posted_at is not None
        assert second.location == "India · 2 Locations"  # passes the India filter later
        # One filter lookup, then two pages; the lookup isn't repeated on the next read.
        assert [b["offset"] for b in respond.seen] == [0, 0, 20]
        await readers.workday(client, BOARD)
        assert len(respond.seen) == 5


@pytest.mark.parametrize(
    ("said", "days"),
    [("Posted Today", 0), ("Posted Yesterday", 1), ("Posted 3 Days Ago", 3),
     ("Posted 30+ Days Ago", None), ("", None)],
)  # fmt: skip
def test_posted_on(said: str, days: int | None) -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 10, 16, tzinfo=UTC)
    got = readers._posted(said, now)
    assert got == (None if days is None else now - timedelta(days=days))


def test_india_filter_falls_back_to_sites() -> None:
    sites_only = [{"facetParameter": "locations", "values": [
        {"descriptor": "India, Pune", "id": "p"}, {"descriptor": "India, Bengaluru", "id": "b"},
        {"descriptor": "Indiana, US", "id": "x"},
    ]}]  # fmt: skip
    assert readers._india_filter(sites_only) == ("locations", ["p", "b"])
    assert readers._india_filter([{"facetParameter": "x", "values": []}]) is None


@respx.mock
async def test_errors_are_unknown_never_empty() -> None:
    async with httpx.AsyncClient() as client:
        respx.post(API).respond(json={"total": 0, "jobPostings": [], "facets": []})
        with pytest.raises(BoardUnavailable, match="no India location filter"):
            await readers.workday(client, BOARD)

        respx.post(API).respond(404)
        with pytest.raises(BoardGone):
            await readers.workday(client, BOARD)

        readers._WD_INDIA[BOARD] = ("locationHierarchy1", ["stale-id"])
        respx.post(API).respond(422, json={"errorCode": "HTTP_422"})
        with pytest.raises(BoardUnavailable, match="422"):
            await readers.workday(client, BOARD)
        assert BOARD not in readers._WD_INDIA  # looked up again next time

        readers._WD_INDIA[BOARD] = ("locationHierarchy1", [INDIA])
        respx.post(API).respond(json={"total": 3, "jobs": []})  # a changed response shape
        with pytest.raises(BoardUnavailable, match="unexpected shape"):
            await readers.workday(client, BOARD)


@respx.mock
async def test_page_cap_gives_a_partial_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(readers, "WD_MAX_PAGES", 2)
    respx.post(API).mock(side_effect=answer([wd_job(n) for n in range(50)]))
    async with httpx.AsyncClient() as client:
        postings = await readers.workday(client, BOARD)
    assert isinstance(postings, Partial) and len(postings) == 40


# ---- the watcher with a Workday board -----------------------------------------------------


class Inbox:
    def __init__(self) -> None:
        self.got: list[IncomingMessage] = []

    async def __call__(self, msg: IncomingMessage) -> None:
        self.got.append(msg)


@respx.mock
async def test_partial_reads_close_nothing_and_old_postings_never_return_as_new(
    repo: Repo, queue: TaskQueue, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(readers, "WD_MAX_PAGES", 1)
    inbox = Inbox()
    config = BoardsConfig(companies=[NVIDIA], every_minutes=20)
    w = BoardWatcher(config, repo, queue, inbox, httpx.AsyncClient(), clock=clock)
    old = [wd_job(n, posted="30+ Days Ago") for n in range(100, 125)]  # 25: over one page
    route = respx.post(API)
    route.mock(side_effect=answer(old))
    await w.check_board(NVIDIA)  # baseline: the newest 20 of 25

    # A job JobRadar knows from the left-out part must not be closed by a capped read.
    known = old[22]
    url = f"https://{HOST}/{SITE}{known['externalPath']}"
    repo.get_or_create_job(job_id_for_url(url), url)
    fresh = wd_job(1, "Software Engineer, New Grad")
    route.mock(side_effect=answer([fresh, *old]))
    await w.check_board(NVIDIA)
    assert [m.urls for m in inbox.got] == [[f"https://{HOST}/{SITE}{fresh['externalPath']}"]]
    job = repo.get_job(job_id_for_url(url))
    assert job is not None and job.status != JobStatus.HIDDEN

    # Postings that had been beyond the cap come into view: they're not new.
    route.mock(side_effect=answer(old[5:]))
    await w.check_board(NVIDIA)
    assert len(inbox.got) == 1


def test_workday_is_read_at_most_every_30_minutes(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    config = BoardsConfig(companies=[NVIDIA], every_minutes=5)
    w = BoardWatcher(config, repo, queue, Inbox(), httpx.AsyncClient(), clock=clock)
    board = repo.get_board(NVIDIA.key)
    board.last_checked = clock().strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    repo.save_board(board)
    clock.advance(20 * 60)
    assert not w.due(NVIDIA.key, clock())
    clock.advance(10 * 60)
    assert w.due(NVIDIA.key, clock())


# ---- still-open check -------------------------------------------------------------------------

JOB = f"https://{HOST}/{SITE}/job/India-Pune/Senior-Designer_JR2027293-1"
JOB_API = f"https://{HOST}/wday/cxs/nvidia/{SITE}/job/India-Pune/Senior-Designer_JR2027293-1"


@pytest.mark.parametrize(
    "url",
    [
        f"https://{HOST}.attacker.io/{SITE}/job/a/b",
        f"https://{HOST}/{SITE}/job/../../x",
        f"https://{HOST}/{SITE}/job/a%2F..%2Fb",
        f"https://{HOST}/{SITE}/details/a",
        f"https://{HOST}/{SITE}",
    ],
)
def test_crafted_workday_links_are_not_recognised(url: str) -> None:
    assert ats_link(url) is None


@respx.mock
@pytest.mark.parametrize(
    ("status", "state"),
    [(200, Liveness.OPEN), (404, Liveness.CLOSED), (410, Liveness.CLOSED),
     (429, Liveness.UNKNOWN), (500, Liveness.UNKNOWN)],
)  # fmt: skip
async def test_workday_still_open(status: int, state: Liveness) -> None:
    respx.get(JOB_API).respond(status, json={})
    checker = LivenessChecker(httpx.AsyncClient())
    assert await checker.check(JOB) == state
    assert await checker.check(JOB.replace(f"/{SITE}/", f"/en-US/{SITE}/")) == state
