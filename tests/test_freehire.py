from typing import Any

import httpx
import pytest
import respx

from jobradar.config import FreehireConfig, load_config
from jobradar.db.models import Platform
from jobradar.db.repo import Repo
from jobradar.pipeline.task_types import FREEHIRE_CHECK
from jobradar.queue.tasks import TaskQueue
from jobradar.sinks.email_notify import where_posted
from jobradar.sources.base import IncomingMessage
from jobradar.sources.boards import freehire
from jobradar.sources.boards.freehire import KEY, FreehireWatcher, post_text, why_not
from tests.conftest import FakeClock

API = freehire.API


def fh_job(n: int, title: str = "Software Engineer, Intern", location: str = "Pune, India",
           **kw: Any) -> dict[str, Any]:  # fmt: skip
    # The shape freehire's /jobs/search answered with on 2026-10-18.
    return {"public_slug": f"job-{n}", "source": "workday", "external_id": f"wd:{n}",
            "url": f"https://acme.wd5.myworkdayjobs.com/Ext/job/Pune/Job_{n}?utm_source=freehire.me",
            "title": title, "company": "Acme", "location": location, "countries": ["in"],
            "is_tech": "tech", "created_at": "2026-10-18T08:00:00Z", "closed_at": None,
            "reality": {"class": "fresh", "fake_freshness": False}, **kw}  # fmt: skip


def page(jobs: list[dict[str, Any]], total: int | None = None, **meta: Any) -> dict[str, Any]:
    return {"data": jobs, "meta": {"total": len(jobs) if total is None else total,
                                   "limit": 100, "offset": 0, **meta}}  # fmt: skip


class Inbox:
    def __init__(self) -> None:
        self.got: list[IncomingMessage] = []

    async def __call__(self, msg: IncomingMessage) -> None:
        self.got.append(msg)


def watcher(repo: Repo, queue: TaskQueue, clock: FakeClock, inbox: Inbox,
            **config: Any) -> FreehireWatcher:  # fmt: skip
    cfg = FreehireConfig(enabled=True, **config)
    return FreehireWatcher(cfg, repo, queue, inbox, httpx.AsyncClient(), clock=clock)


# ---- filters and text ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("job", "why"),
    [
        (fh_job(1), None),
        (fh_job(1, location="Tell City, IN"), "not in India"),  # freehire's IN includes Indiana
        (fh_job(1, title="Physical Therapist Assistant", is_tech="non_tech"), "not a tech role"),
        (fh_job(1, title="Senior Software Engineer"), "too senior"),
        (fh_job(1, closed_at="2026-10-18T09:00:00Z"), "closed"),
        (fh_job(1, reality={"fake_freshness": True}), "an old posting made to look new"),
    ],
)
def test_why_not(job: dict[str, Any], why: str | None) -> None:
    assert why_not(job) == why


def test_post_text_has_the_labels_extraction_reads() -> None:
    text = post_text(fh_job(1))
    assert text.splitlines()[:4] == [
        "Company: Acme", "Role: Software Engineer, Intern", "Location: Pune, India",
        "Found by freehire on Workday",
    ]  # fmt: skip
    assert "Company" not in post_text(fh_job(1, company=""))


def test_where_posted() -> None:
    assert where_posted("freehire · Workday", "board") == "via freehire (Workday)"
    assert where_posted("Greenhouse · Stripe", "board") == "on Stripe's Greenhouse board"


# ---- the watcher -------------------------------------------------------------------------------


@respx.mock
async def test_baseline_then_new_postings(repo: Repo, queue: TaskQueue, clock: FakeClock) -> None:
    inbox = Inbox()
    w = watcher(repo, queue, clock, inbox)
    route = respx.get(API)
    route.respond(json=page([fh_job(1), fh_job(2)]))
    assert await w.check() == 0  # first look: record, don't alert about old postings

    route.respond(json=page([
        fh_job(3),  # new, passes
        fh_job(4, location="Tell City, IN"),  # new, Indiana
        fh_job(1), fh_job(2),
    ]))  # fmt: skip
    assert await w.check() == 1
    msg = inbox.got[0]
    assert (msg.platform, msg.chat_id, msg.chat_title) == (
        Platform.BOARD,
        "freehire:workday",
        "freehire · Workday",
    )
    assert msg.message_id == "job-3" and msg.urls == [fh_job(3)["url"]]
    assert msg.posted_at.isoformat().startswith("2026-10-18T08:00")  # first seen by freehire

    # Postings that aged out of the 2-day search aren't closed, and don't come back as new.
    route.respond(json=page([fh_job(3)]))
    assert await w.check() == 0
    route.respond(json=page([fh_job(1), fh_job(3)]))
    assert await w.check() == 0
    board = repo.get_board(KEY)
    assert board.new_total == 1 and board.last_error is None


@respx.mock
async def test_query_and_paging(repo: Repo, queue: TaskQueue, clock: FakeClock) -> None:
    seen: list[httpx.QueryParams] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params)
        offset = int(request.url.params["offset"])
        jobs = [fh_job(n) for n in range(offset, min(offset + 100, 230))]
        return httpx.Response(200, json=page(jobs, total=230))

    respx.get(API).mock(side_effect=respond)
    w = watcher(repo, queue, clock, Inbox(), categories=["backend", "fullstack"])
    assert len(await w.read()) == 230
    assert [p["offset"] for p in seen] == ["0", "100", "200"]
    first = seen[0]
    assert first["countries"] == "IN" and first["seniority"] == "intern,junior"
    assert first["open_within_days"] == "2" and first["category"] == "backend,fullstack"
    assert first["reality_exclude"] == "likely-evergreen" and first["sort"] == "created_at"


@respx.mock
async def test_max_pages(repo: Repo, queue: TaskQueue, clock: FakeClock) -> None:
    route = respx.get(API).respond(json=page([fh_job(n) for n in range(100)], total=900))
    assert len(await watcher(repo, queue, clock, Inbox(), max_pages=2).read()) == 200
    assert route.call_count == 2


@respx.mock
@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(429), "answered 429"),
        (httpx.Response(503), "answered 503"),
        (httpx.Response(200, text="<html>"), "isn't JSON"),
        (httpx.Response(200, json={"jobs": []}), "unexpected shape"),
        # A filter the API dropped returns the whole catalogue: never a result.
        (httpx.Response(200, json=page([fh_job(1)], ignored_params=["reality_exclude"])),
         "ignored the filters"),
    ],
)  # fmt: skip
async def test_errors_change_nothing(
    repo: Repo, queue: TaskQueue, clock: FakeClock, response: httpx.Response, error: str
) -> None:
    inbox = Inbox()
    w = watcher(repo, queue, clock, inbox)
    route = respx.get(API)
    route.respond(json=page([fh_job(1)]))
    await w.check()
    route.mock(return_value=response)
    assert await w.check() == 0
    board = repo.get_board(KEY)
    assert board.last_error is not None and error in board.last_error
    assert '"job-1"' in (board.seen_json or "")  # the last good list is kept


@respx.mock
async def test_at_most_twenty_new_per_read(repo: Repo, queue: TaskQueue, clock: FakeClock) -> None:
    inbox = Inbox()
    w = watcher(repo, queue, clock, inbox)
    route = respx.get(API)
    route.respond(json=page([fh_job(0)]))
    await w.check()
    route.respond(json=page([fh_job(n) for n in range(1, 31)]))
    assert await w.check() == 20
    assert await w.check() == 0  # the other ten are remembered, not passed on later


@respx.mock
async def test_tick_reschedules(repo: Repo, queue: TaskQueue, clock: FakeClock) -> None:
    respx.get(API).respond(json=page([]))
    w = watcher(repo, queue, clock, Inbox(), every_minutes=30)
    w.schedule()
    task = queue.claim([FREEHIRE_CHECK])
    assert task is not None
    await w.tick(task)
    queue.complete(task)
    assert queue.claim([FREEHIRE_CHECK]) is None  # next one is 30 min away
    clock.advance(30 * 60)
    nxt = queue.claim([FREEHIRE_CHECK])
    assert nxt is not None and nxt.key == "b"


# ---- config ---------------------------------------------------------------------------------------


def test_config() -> None:
    from pathlib import Path

    assert load_config(Path("config.example.yaml")).sources.freehire.enabled is False
    with pytest.raises(ValueError):
        FreehireConfig(seniority=["wizard"])  # type: ignore[list-item]
    with pytest.raises(ValueError):
        FreehireConfig(every_minutes=5)  # gentle on someone else's service
