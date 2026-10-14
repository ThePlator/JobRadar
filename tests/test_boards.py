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


@pytest.mark.parametrize(
    ("link", "ats", "company"),
    [
        ("https://job-boards.greenhouse.io/stripe", "greenhouse", "stripe"),
        ("https://boards.greenhouse.io/razorpaysoftwareprivatelimited/jobs/12?gh_src=x",
         "greenhouse", "razorpaysoftwareprivatelimited"),
        ("job-boards.eu.greenhouse.io/acme", "greenhouse", "acme"),
        ("https://jobs.lever.co/meesho", "lever", "meesho"),
        ("https://jobs.eu.lever.co/zeta/0a1b2c", "lever", "zeta"),
        ("https://jobs.ashbyhq.com/sarvam", "ashby", "sarvam"),
        ("https://jobs.smartrecruiters.com/BoschGroup", "smartrecruiters", "BoschGroup"),
        ("https://careers.smartrecruiters.com/Freshworks", "smartrecruiters", "Freshworks"),
    ],
)  # fmt: skip
def test_board_links(link: str, ats: str, company: str) -> None:
    from jobradar.config import board_from_link

    assert board_from_link(link) == (ats, company)


def test_boards_grouped_by_hiring_system() -> None:
    from pydantic import ValidationError

    config = BoardsConfig.model_validate({
        "greenhouse": {"Stripe": "stripe", "Rubrik": "https://job-boards.greenhouse.io/rubrik"},
        "lever": ["meesho"],  # a plain list: the board name is the display name
        "smartrecruiters": {"Bosch": "BoschGroup"},
    })  # fmt: skip
    assert [(b.key, b.label) for b in config.companies] == [
        ("greenhouse:stripe", "Greenhouse · Stripe"),
        ("greenhouse:rubrik", "Greenhouse · Rubrik"),
        ("lever:meesho", "Lever · meesho"),
        ("smartrecruiters:BoschGroup", "SmartRecruiters · Bosch"),
    ]
    with pytest.raises(ValidationError, match="that's a Lever link; move it under lever"):
        BoardsConfig.model_validate({"greenhouse": {"Meesho": "https://jobs.lever.co/meesho"}})
    with pytest.raises(ValidationError):
        BoardsConfig.model_validate({"lever": {"X": "bad name!"}})
    with pytest.raises(ValidationError, match="listed twice"):
        BoardsConfig.model_validate({"greenhouse": ["stripe", "stripe"]})


def test_readable_board_list() -> None:
    from pydantic import ValidationError

    config = BoardsConfig.model_validate({"companies": {
        "Stripe": "https://job-boards.greenhouse.io/stripe",
        "Bosch": "https://jobs.smartrecruiters.com/BoschGroup",
    }})  # fmt: skip
    assert [b.label for b in config.companies] == ["Greenhouse · Stripe", "SmartRecruiters · Bosch"]
    with pytest.raises(ValidationError, match="isn't a Greenhouse, Lever, Ashby"):
        BoardsConfig.model_validate({"companies": {"Google": "https://careers.google.com/jobs"}})
    assert json.loads("[]") == []


@pytest.mark.parametrize(
    ("title", "location", "level", "why"),
    [
        ("Software Engineer, Intern", "Bengaluru, India", None, None),
        ("Software Engineer", "Tokyo, Japan", None, "not in India"),
        ("Software Engineer", "Remote - US", None, "remote, but not open to India"),
        ("Global Partner Program Operations Specialist", "Bengaluru, India", None,
         "not a tech role"),
        ("Territory Manager, Bank Retail", "Kolkata", None, "not a tech role"),
        ("Lead Software Development Engineer", "Bengaluru", None, "too senior"),
        ("Software Engineer", "Pune, India", "mid_senior_level", "too senior"),
    ],
)  # fmt: skip
def test_why_skipped(title: str, location: str, level: str | None, why: str | None) -> None:
    from jobradar.sources.boards.filters import why_skipped

    assert why_skipped(title, location, level) == why


@respx.mock
async def test_skipped_postings_are_explained(
    repo: Repo, queue: TaskQueue, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    w = watcher(repo, queue, clock, Inbox())
    route = respx.get(GH)
    route.respond(json={"jobs": [gh_job(1, "Software Engineer")]})
    await w.check_board(STRIPE)
    latest = json.loads(repo.get_board("greenhouse:stripe").latest_json or "{}")
    assert (latest["title"], latest["verdict"]) == ("Software Engineer", "passed")

    route.respond(json={"jobs": [gh_job(1, "Software Engineer"),
                                 gh_job(2, "Lead Software Development Engineer")]})  # fmt: skip
    with caplog.at_level(logging.INFO, logger="jobradar.sources.boards.watcher"):
        await w.check_board(STRIPE)
    skipped = [r for r in caplog.records if r.getMessage() == "board posting skipped"]
    assert len(skipped) == 1
    assert (skipped[0].title, skipped[0].why) == (
        "Lead Software Development Engineer",
        "too senior",
    )  # type: ignore[attr-defined]
    latest = json.loads(repo.get_board("greenhouse:stripe").latest_json or "{}")
    assert latest["verdict"] == "too senior"
    await w.aclose()


# ---- boards add / remove --------------------------------------------------------------------

CONFIG = """\
sources:
  telegram:
    enabled: false
  boards:                 # company boards
    every_minutes: 20
    greenhouse:
      Stripe:     stripe
      Razorpay:   razorpaysoftwareprivatelimited
    lever:
      Meesho: https://jobs.lever.co/meesho
  email_forward:
    enabled: false
llm:
  daily_budget_inr: 40
"""


def test_add_board_lines_up_with_the_section() -> None:
    from jobradar.sources.boards.config_edit import add_board

    out = add_board(CONFIG, "greenhouse", "postman", "Postman")
    assert (
        "      Razorpay:   razorpaysoftwareprivatelimited\n      Postman:    postman\n    lever:"
        in out
    )
    out = add_board(out, "ashby", "sarvam", "Sarvam AI")
    assert (
        "      Meesho: https://jobs.lever.co/meesho\n    ashby:\n      Sarvam AI: sarvam\n  email_forward:"
        in out
    )
    out = add_board(out, "lever", "x", "A: B")
    assert '      "A: B": x\n' in out
    assert BoardsConfig.model_validate(yaml_boards(out)).companies[-1].name == "Sarvam AI"


def yaml_boards(text: str) -> dict[str, Any]:
    import yaml

    return yaml.safe_load(text)["sources"]["boards"]  # type: ignore[no-any-return]


@pytest.mark.parametrize(
    ("ats", "board", "name", "error"),
    [
        ("greenhouse", "STRIPE", "Stripe 2", r"already in config.yaml \(as Stripe\)"),
        ("lever", "meesho", "M", "already in config.yaml"),  # listed as a link
        ("greenhouse", "other", "razorpay", "already a greenhouse board named"),
    ],
)
def test_add_board_refuses_duplicates(ats: str, board: str, name: str, error: str) -> None:
    from jobradar.sources.boards.config_edit import EditError, add_board

    with pytest.raises(EditError, match=error):
        add_board(CONFIG, ats, board, name)


def test_add_board_needs_a_boards_section() -> None:
    from jobradar.sources.boards.config_edit import EditError, add_board

    with pytest.raises(EditError, match="no `sources: boards:`"):
        add_board("sources:\n  telegram:\n    enabled: false\n", "lever", "x", "X")
    with pytest.raises(EditError, match="one line"):
        add_board(CONFIG.replace("    lever:\n      Meesho: https://jobs.lever.co/meesho\n",
                                 "    lever: [meesho]\n"), "lever", "x", "X")  # fmt: skip


@pytest.mark.parametrize("which", ["stripe", "Stripe", "https://job-boards.greenhouse.io/stripe"])
def test_remove_board(which: str) -> None:
    from jobradar.sources.boards.config_edit import remove_board

    out, ats, name = remove_board(CONFIG, which)
    assert (ats, name) == ("greenhouse", "Stripe")
    assert "stripe" not in out and "Razorpay:" in out
    out, ats, name = remove_board(out, "meesho")  # listed as a link
    assert (ats, name) == ("lever", "Meesho")
    assert [b.key for b in BoardsConfig.model_validate(yaml_boards(out)).companies] == [
        "greenhouse:razorpaysoftwareprivatelimited"
    ]


def test_remove_unknown_board() -> None:
    from jobradar.sources.boards.config_edit import EditError, remove_board

    with pytest.raises(EditError, match="no board 'acme'"):
        remove_board(CONFIG, "acme")


@respx.mock
def test_boards_add_command(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from jobradar import __main__ as cli

    path = tmp_path / "config.yaml"
    path.write_text(CONFIG)
    monkeypatch.setattr(cli, "CONFIG_PATH", path)
    respx.get("https://boards-api.greenhouse.io/v1/boards/postman/jobs").respond(
        json={"jobs": [gh_job(1, "Software Engineer Intern"), gh_job(2, "Account Executive")]}
    )
    respx.get("https://boards-api.greenhouse.io/v1/boards/gone/jobs").respond(404)
    runner = CliRunner()

    result = runner.invoke(cli.app, ["boards", "add", "greenhouse", "postman", "--name", "Postman"])
    assert result.exit_code == 0, result.output
    assert "2 postings now, 1 pass your filters" in result.output
    assert "Postman:    postman" in path.read_text()

    result = runner.invoke(cli.app, ["boards", "add", "https://job-boards.greenhouse.io/gone"])
    assert result.exit_code == 2 and "answered 404" in result.output
    assert "gone" not in path.read_text()

    result = runner.invoke(cli.app, ["boards", "remove", "postman"])
    assert result.exit_code == 0, result.output
    assert path.read_text() == CONFIG
