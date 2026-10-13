import json
from typing import Any

import httpx
import pytest
import respx

from jobradar.config import BoardRef, BoardsConfig
from jobradar.db.models import JobStatus, Platform
from jobradar.db.repo import Repo
from jobradar.pipeline.task_types import BOARD_CHECK, BOARDS_TICK
from jobradar.queue.tasks import TaskQueue
from jobradar.sources.base import IncomingMessage
from jobradar.sources.boards import readers
from jobradar.sources.boards.base import BoardGone, BoardUnavailable
from jobradar.sources.boards.filters import title_ok, where_ok, worth_a_look
from jobradar.sources.boards.watcher import CLOSED_REASON, BoardWatcher, post_text
from tests.conftest import FakeClock

GH = "https://boards-api.greenhouse.io/v1/boards/stripe/jobs"


def gh_job(job_id: int, title: str, location: str = "Bengaluru, India") -> dict[str, Any]:
    return {"id": job_id, "title": title, "location": {"name": location},
            "absolute_url": f"https://job-boards.greenhouse.io/stripe/jobs/{job_id}",
            "updated_at": "2026-10-13T06:00:00-04:00"}  # fmt: skip


# ---- filters --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("location", "ok"),
    [
        ("Bengaluru, India", True), ("Hyderabad · Remote", True), ("Remote", True),
        ("Remote · APAC", True), ("Remote - Worldwide", True), ("Remote - US", False),
        ("Remote, Canada", False), ("Remote · New York, NY", False), ("London, UK", False),
    ],
)  # fmt: skip
def test_where(location: str, ok: bool) -> None:
    assert where_ok(location) is ok


@pytest.mark.parametrize(
    ("title", "ok"),
    [
        ("Software Engineer, Intern", True), ("Backend Engineer (New Grad)", True),
        ("Associate Software Engineer", True), ("Data Scientist", True),
        ("Senior Software Engineer", False), ("Software Engineer II", False), ("SDE 2", False),
        ("Staff ML Engineer", False), ("Engineering Manager", False),
        ("Account Executive", False), ("Product Designer", False),
    ],
)  # fmt: skip
def test_title(title: str, ok: bool) -> None:
    assert title_ok(title) is ok


def test_seniority_levels() -> None:
    assert worth_a_look("Software Engineer", "Pune, India", "entry_level")
    assert not worth_a_look("Software Engineer", "Pune, India", "mid_senior_level")


# ---- readers --------------------------------------------------------------------------------


@respx.mock
async def test_readers_give_one_shape() -> None:
    respx.get(GH).respond(json={"jobs": [gh_job(1, "SWE Intern")]})
    respx.get("https://api.lever.co/v0/postings/meesho").respond(404)
    respx.get("https://api.eu.lever.co/v0/postings/meesho").respond(json=[{
        "id": "lv1", "text": "Backend Engineer", "hostedUrl": "https://jobs.lever.co/meesho/lv1",
        "createdAt": 1791900000000, "workplaceType": "hybrid",
        "categories": {"location": "Bangalore", "allLocations": ["Bangalore"], "commitment": "Full-time"},
    }])  # fmt: skip
    respx.get("https://api.ashbyhq.com/posting-api/job-board/sarvam").respond(json={"jobs": [
        {"id": "a1", "title": "ML Engineer", "location": "Bengaluru", "isRemote": True,
         "jobUrl": "https://jobs.ashbyhq.com/sarvam/a1", "publishedAt": "2026-10-13T05:00:00Z"},
        {"id": "a2", "title": "Hidden", "isListed": False, "jobUrl": "x"},
    ]})  # fmt: skip
    respx.get("https://api.smartrecruiters.com/v1/companies/BoschGroup/postings").respond(json={
        "totalFound": 1, "content": [{"id": "s1", "name": "Graduate Trainee",
        "location": {"fullLocation": "Bangalore, India"}, "releasedDate": "2026-10-13T06:02:58.058Z",
        "experienceLevel": {"id": "entry_level"}}],
    })  # fmt: skip
    async with httpx.AsyncClient() as client:
        (gh,) = await readers.greenhouse(client, "stripe")
        (lv,) = await readers.lever(client, "meesho")  # falls back to the EU host
        (ab,) = await readers.ashby(client, "sarvam")  # unlisted postings are skipped
        (sr,) = await readers.smartrecruiters(client, "BoschGroup")
    assert (gh.id, gh.location, gh.url) == (
        "1",
        "Bengaluru, India",
        GH.replace(
            "boards-api.greenhouse.io/v1/boards/stripe/jobs",
            "job-boards.greenhouse.io/stripe/jobs/1",
        ),
    )
    assert (lv.title, lv.location) == ("Backend Engineer", "Bangalore · Bangalore · hybrid")
    assert ab.location == "Bengaluru · Remote" and ab.posted_at is not None
    assert (sr.url, sr.level) == ("https://jobs.smartrecruiters.com/BoschGroup/s1", "entry_level")


@respx.mock
async def test_reader_errors_are_told_apart() -> None:
    respx.get(GH).respond(404)
    async with httpx.AsyncClient() as client:
        with pytest.raises(BoardGone):
            await readers.greenhouse(client, "stripe")
        respx.get(GH).respond(503)
        with pytest.raises(BoardUnavailable):
            await readers.greenhouse(client, "stripe")
        respx.get(GH).respond(200, text="<html>maintenance</html>")
        with pytest.raises(BoardUnavailable):
            await readers.greenhouse(client, "stripe")


# ---- the watcher ----------------------------------------------------------------------------


STRIPE = BoardRef(ats="greenhouse", company="stripe", name="Stripe")


class Inbox:
    def __init__(self) -> None:
        self.got: list[IncomingMessage] = []

    async def __call__(self, msg: IncomingMessage) -> None:
        self.got.append(msg)


def watcher(repo: Repo, queue: TaskQueue, clock: FakeClock, inbox: Inbox) -> BoardWatcher:
    config = BoardsConfig(companies=[STRIPE], every_minutes=20)
    return BoardWatcher(config, repo, queue, inbox, httpx.AsyncClient(), clock=clock)


@respx.mock
async def test_baseline_then_new_postings_then_closed(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    inbox = Inbox()
    w = watcher(repo, queue, clock, inbox)
    route = respx.get(GH)
    route.respond(json={"jobs": [gh_job(1, "Software Engineer"), gh_job(2, "Staff Engineer")]})
    await w.check_board(STRIPE)
    assert inbox.got == []  # first look: record, don't alert about old postings

    route.respond(json={"jobs": [
        gh_job(1, "Software Engineer"),
        gh_job(3, "Software Engineer, Intern"),  # new, passes
        gh_job(4, "Senior Software Engineer"),  # new, too senior
        gh_job(5, "Software Engineer", "Dublin, Ireland"),  # new, not India
    ]})  # fmt: skip
    # Posting 2 vanished: its job closes if it's untouched in the Inbox.
    from jobradar.pipeline.dedupe import job_id_for_url

    j2 = job_id_for_url("https://job-boards.greenhouse.io/stripe/jobs/2")
    repo.get_or_create_job(j2, "https://job-boards.greenhouse.io/stripe/jobs/2")
    repo.set_job_status(j2, JobStatus.NEW)
    await w.check_board(STRIPE)

    assert [m.message_id for m in inbox.got] == ["3"]
    msg = inbox.got[0]
    assert (msg.platform, msg.chat_id, msg.chat_title) == (
        Platform.BOARD, "greenhouse:stripe", "Greenhouse · Stripe")  # fmt: skip
    assert msg.text and "Company: Stripe\nRole: Software Engineer, Intern" in msg.text
    closed = repo.get_job(j2)
    assert closed is not None and (closed.status, closed.score_reason) == (
        JobStatus.HIDDEN, CLOSED_REASON)  # fmt: skip
    board = repo.get_board("greenhouse:stripe")
    assert board.new_total == 1 and board.jobs_on_board == 4
    await w.aclose()


@respx.mock
async def test_errors_never_look_like_an_empty_board(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    inbox = Inbox()
    w = watcher(repo, queue, clock, inbox)
    route = respx.get(GH)
    route.respond(json={"jobs": [gh_job(1, "Software Engineer")]})
    await w.check_board(STRIPE)
    seen = repo.get_board("greenhouse:stripe").seen_json

    route.respond(503)
    await w.check_board(STRIPE)
    route.respond(json={"jobs": []})  # everything at once: a glitch, not mass closing
    await w.check_board(STRIPE)
    board = repo.get_board("greenhouse:stripe")
    assert board.seen_json == seen and board.misses == 0
    assert board.last_error == "listed no postings; kept the previous list"

    route.respond(404)
    for _ in range(3):
        await w.check_board(STRIPE)
    board = repo.get_board("greenhouse:stripe")
    assert board.misses == 3 and board.skip_until is not None
    assert not w.due("greenhouse:stripe", clock())  # parked for 30 days
    await w.aclose()


async def test_ticks_queue_due_boards_and_alternate(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    w = watcher(repo, queue, clock, Inbox())
    w.schedule()
    tick = queue.claim([BOARDS_TICK])
    assert tick is not None
    await w.tick(tick)
    queue.complete(tick)
    check = queue.claim([BOARD_CHECK])
    assert check is not None and check.key == "greenhouse:stripe"  # never read: due now
    board = repo.get_board("greenhouse:stripe")
    board.last_checked = "2026-10-08T09:00:00.000000Z"
    repo.save_board(board)
    assert not w.due("greenhouse:stripe", clock())
    clock.advance(20 * 60)
    assert w.due("greenhouse:stripe", clock())
    nxt = queue.claim([BOARDS_TICK])
    assert nxt is not None and nxt.key == "b"  # the next tick, under the other key
    await w.aclose()


def test_post_text_reads_like_a_channel_post() -> None:
    from jobradar.pipeline.post_labels import from_post
    from jobradar.sources.boards.base import BoardPosting

    posting = BoardPosting("9", "Backend Engineer", "Pune, India", "https://x.co/9")
    fields = from_post(post_text(STRIPE, posting))
    assert (fields["company"], fields["role"], fields["locations"]) == (
        "Stripe", "Backend Engineer", ["Pune"])  # fmt: skip


def test_board_config() -> None:
    from pydantic import ValidationError

    assert STRIPE.label == "Greenhouse · Stripe"
    bosch = BoardRef(ats="smartrecruiters", company="BoschGroup")
    assert bosch.label == "SmartRecruiters · BoschGroup"
    with pytest.raises(ValidationError):
        BoardRef(ats="greenhouse", company="../../etc")  # board names can't steer requests
    with pytest.raises(ValidationError, match="listed twice"):
        BoardsConfig(companies=[STRIPE, STRIPE])
    assert json.loads("[]") == []
