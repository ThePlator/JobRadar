from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx

from jobradar.clock import from_iso
from jobradar.config import PagesConfig, load_config
from jobradar.db.models import JobStatus, PageState, Platform
from jobradar.db.repo import Repo
from jobradar.pipeline import fetch as fetch_mod
from jobradar.pipeline.dedupe import job_id_for_url
from jobradar.pipeline.fetch import Fetcher, Page
from jobradar.pipeline.task_types import FETCH, PAGE_CHECK
from jobradar.queue.tasks import RetryableError, TaskQueue
from jobradar.sources.base import IncomingMessage
from jobradar.sources.boards.config_edit import EditError
from jobradar.sources.pages.config_edit import add_page, remove_page
from jobradar.sources.pages.text import added_lines, is_redesign, line_key, links_in, readable
from jobradar.sources.pages.watcher import PageWatcher
from tests.conftest import FakeClock

URL = "https://nextstep.example.com/drives"
NAME = "TCS NextStep"

BASE = """# Hiring drives
Welcome to the careers portal for graduates across India.
Posted 2 days ago
Current openings for experienced professionals are listed under Jobs.
1,204 jobs found"""

NQT = (
    "TCS NQT 2026 batch: registration is open for engineering graduates till 30 Oct. "
    "[Register here](/apply/nqt-2026)"
)


class Inbox:
    def __init__(self) -> None:
        self.got: list[IncomingMessage] = []

    async def __call__(self, msg: IncomingMessage) -> None:
        self.got.append(msg)


class FakeFetcher:
    """Answers each fetch with the next item: a text, a Page, or an exception."""

    def __init__(self) -> None:
        self.answers: list[str | Page | Exception] = []
        self.calls = 0

    async def fetch(self, url: str, links: bool = False) -> Page:
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, Page):
            return answer
        return Page(url, None, answer)

    async def aclose(self) -> None:
        pass


def make(
    repo: Repo, queue: TaskQueue, clock: FakeClock, inbox: Inbox, fetcher: FakeFetcher
) -> PageWatcher:
    config = PagesConfig(watch={NAME: URL}, every_hours=2)
    return PageWatcher(config, repo, queue, inbox, fetcher, clock=clock)  # type: ignore[arg-type]


# ---- comparing two reads --------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "Posted 3 days ago", "Updated: 16 Oct 2026 10:30", "Last updated on 16/10/2026",
        "© 2026 Example Ltd", "1,204 jobs found", "12 Oct 2026",
    ],
)  # fmt: skip
def test_furniture_lines_are_ignored(line: str) -> None:
    assert line_key(line) == ""


def test_dates_inside_sentences_still_count() -> None:
    old = "Off campus drive for 2025 batch on 5 Oct in Chennai"
    new = "Off campus drive for 2025 batch on 12 Nov in Chennai"
    assert line_key(old) != line_key(new)
    # …but a ticking counter in the same sentence doesn't.
    assert line_key("Drive results: 120 applications so far, registration open") == line_key(
        "Drive results: 135 applications so far, registration open"
    )


def test_moved_lines_are_not_new() -> None:
    old = ["Alpha drive for graduates is open", "Beta drive for graduates is open"]
    new = ["Beta drive for graduates is open", "Alpha drive for graduates is open"]
    assert added_lines(old, new) == []


def test_links_made_absolute_page_and_mail_excluded() -> None:
    lines = [
        "[Register](/apply/1) or [mail us](mailto:hr@x.com)",
        f"[This page]({URL}) and https://forms.example.com/nqt",
    ]
    assert links_in(lines, URL) == [
        "https://nextstep.example.com/apply/1",
        "https://forms.example.com/nqt",
    ]
    assert readable(["## [Register](/apply/1) now"], URL) == (
        "Register (https://nextstep.example.com/apply/1) now"
    )


def test_redesign() -> None:
    new = ["a" * 100, "b" * 100]
    assert is_redesign(new, new)
    assert not is_redesign([*new, "c" * 400], new[:1])


# ---- the watcher ----------------------------------------------------------------------------


async def test_baseline_then_change_with_link_posts_once(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    inbox, fetcher = Inbox(), FakeFetcher()
    w = make(repo, queue, clock, inbox, fetcher)
    fetcher.answers = [
        BASE,
        BASE.replace("2 days", "3 days").replace("1,204", "1,250"),  # only furniture moved
        BASE + "\n" + NQT,
        BASE + "\n" + NQT,
    ]
    assert await w.check_page(NAME, URL) == 0  # baseline: nothing posted
    assert await w.check_page(NAME, URL) == 0
    assert await w.check_page(NAME, URL) == 1
    assert await w.check_page(NAME, URL) == 0
    assert len(inbox.got) == 1
    msg = inbox.got[0]
    assert msg.platform == Platform.PAGE and msg.chat_title == NAME
    assert msg.urls == ["https://nextstep.example.com/apply/nqt-2026"]
    assert msg.text and msg.text.startswith(f"Source: {NAME} ({URL})")
    assert "Register here (https://nextstep.example.com/apply/nqt-2026)" in msg.text
    page = repo.get_watched_page(URL, NAME)
    assert page.state == PageState.WATCHING and page.posts_total == 1


async def test_same_change_after_restart_has_the_same_message_id(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    # The text is saved after posting, so a restart reads it as unchanged; and if a crash
    # lost that save, the post's id is the same, so ingest stores it once.
    inbox, fetcher = Inbox(), FakeFetcher()
    fetcher.answers = [BASE, BASE + "\n" + NQT]
    w = make(repo, queue, clock, inbox, fetcher)
    await w.check_page(NAME, URL)
    await w.check_page(NAME, URL)
    repo.save_watched_page(repo.get_watched_page(URL, NAME).model_copy(update={"text": BASE}))
    fetcher.answers = [BASE + "\n" + NQT]
    await make(repo, queue, clock, inbox, fetcher).check_page(NAME, URL)
    assert len(inbox.got) == 2 and inbox.got[0].message_id == inbox.got[1].message_id


async def test_small_change_is_not_an_announcement(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    inbox, fetcher = Inbox(), FakeFetcher()
    fetcher.answers = [BASE, BASE + "\nNew FAQ section added today"]
    w = make(repo, queue, clock, inbox, fetcher)
    await w.check_page(NAME, URL)
    assert await w.check_page(NAME, URL) == 0 and inbox.got == []


async def test_change_without_link_rereads_the_page_job(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    job_id = job_id_for_url(URL)
    repo.get_or_create_job(job_id, URL)
    repo.set_job_status(job_id, JobStatus.HIDDEN, "no role found")
    inbox, fetcher = Inbox(), FakeFetcher()
    notice = (
        "Wipro Elite NTH 2026: online assessment dates for registered candidates will be "
        "shared by email next week."
    )
    fetcher.answers = [BASE, BASE + "\n" + notice]
    w = make(repo, queue, clock, inbox, fetcher)
    await w.check_page(NAME, URL)
    await w.check_page(NAME, URL)
    assert inbox.got[0].urls == [URL]
    job = repo.get_job(job_id)
    assert job is not None and job.status == JobStatus.DISCOVERED
    task = queue.claim([FETCH])
    assert task is not None and task.key == job_id


async def test_redesign_takes_a_new_baseline(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    inbox, fetcher = Inbox(), FakeFetcher()
    redesigned = "\n".join(f"Brand new section number {i} about careers here" for i in range(9))
    fetcher.answers = [BASE, redesigned, redesigned + "\n" + NQT]
    w = make(repo, queue, clock, inbox, fetcher)
    await w.check_page(NAME, URL)
    assert await w.check_page(NAME, URL) == 0
    assert "new baseline" in (repo.get_watched_page(URL, NAME).last_error or "")
    assert await w.check_page(NAME, URL) == 1  # compared with the new layout from then on


async def test_refusals_back_off_then_pause_then_resume(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    inbox, fetcher = Inbox(), FakeFetcher()
    w = make(repo, queue, clock, inbox, fetcher)
    fetcher.answers = [
        BASE,
        RetryableError("answered 429", status=429),
        Page(URL, None, None, "HTTP 403"),
        RetryableError("answered 429", status=429),
    ]
    await w.check_page(NAME, URL)
    await w.check_page(NAME, URL)
    page = repo.get_watched_page(URL, NAME)
    assert page.state == PageState.BACKING_OFF and page.refusals == 1
    clock.advance(timedelta(hours=1, minutes=59).total_seconds())
    assert not w.due(URL, clock())
    clock.advance(120)
    assert w.due(URL, clock())
    await w.check_page(NAME, URL)
    page = repo.get_watched_page(URL, NAME)
    assert page.refusals == 2 and page.wait_until is not None
    assert from_iso(page.wait_until) == clock() + timedelta(hours=4)
    await w.check_page(NAME, URL)
    page = repo.get_watched_page(URL, NAME)
    assert page.state == PageState.PAUSED
    clock.advance(timedelta(days=2).total_seconds())
    assert not w.due(URL, clock())
    assert w.resume(URL)
    assert w.due(URL, clock())
    assert repo.get_watched_page(URL, NAME).text == "\n".join(BASE.splitlines())  # kept


async def test_other_errors_change_nothing(repo: Repo, queue: TaskQueue, clock: FakeClock) -> None:
    inbox, fetcher = Inbox(), FakeFetcher()
    fetcher.answers = [
        BASE,
        Page(URL, None, None, "not a public address"),  # also what a failed DNS lookup gives
        RetryableError("timeout"),
        RetryableError("answered 503", status=503),
    ]
    w = make(repo, queue, clock, inbox, fetcher)
    for _ in range(4):
        await w.check_page(NAME, URL)
    page = repo.get_watched_page(URL, NAME)
    assert page.state == PageState.WATCHING and page.refusals == 0
    assert page.last_error == "answered 503"


@pytest.mark.parametrize(
    ("note", "state"),
    [
        ("blocked by robots.txt", PageState.BLOCKED),
        ("login required", PageState.PAUSED),
    ],
)
async def test_pages_never_read_again(
    repo: Repo, queue: TaskQueue, clock: FakeClock, note: str, state: PageState
) -> None:
    inbox, fetcher = Inbox(), FakeFetcher()
    fetcher.answers = [Page(URL, None, None, note)]
    w = make(repo, queue, clock, inbox, fetcher)
    assert w.queue_due() == 1
    await w.check_page(NAME, URL)
    assert repo.get_watched_page(URL, NAME).state == state
    clock.advance(timedelta(days=1).total_seconds())
    assert w.queue_due() == 0 and fetcher.calls == 1


async def test_check_task_and_schedule(repo: Repo, queue: TaskQueue, clock: FakeClock) -> None:
    inbox, fetcher = Inbox(), FakeFetcher()
    fetcher.answers = [BASE]
    w = make(repo, queue, clock, inbox, fetcher)
    assert w.queue_due() == 1
    task = queue.claim([PAGE_CHECK])
    assert task is not None
    await w.check(task)
    assert w.queue_due() == 0  # read just now
    clock.advance(timedelta(hours=2).total_seconds())
    assert w.queue_due() == 1


# ---- fetching with links ----------------------------------------------------------------------


@respx.mock
async def test_fetch_keeps_links(monkeypatch: pytest.MonkeyPatch) -> None:
    async def public(host: str) -> bool:
        return True

    monkeypatch.setattr(fetch_mod, "is_public_host", public)
    body = (
        "<html><body><main><h1>Drives</h1>"
        + "<p>TCS NQT 2026 batch registration is open for graduates this month. "
        "<a href='/apply/nqt'>Register here</a></p>" * 4 + "</main></body></html>"
    )
    respx.get("https://ex.com/robots.txt").respond(404)
    respx.get("https://ex.com/drives").respond(200, html=body)
    fetcher = Fetcher(httpx.AsyncClient(), per_site_interval=0, render_js=False)
    page = await fetcher.fetch("https://ex.com/drives", links=True)
    assert page.text and "[Register here](https://ex.com/apply/nqt)" in page.text


# ---- config -----------------------------------------------------------------------------------


def test_config_rejects_bad_addresses() -> None:
    with pytest.raises(ValueError, match="isn't a web page address"):
        PagesConfig(watch={"X": "nextstep.tcs.com"})
    with pytest.raises(ValueError, match="already watched"):
        PagesConfig(watch={"A": URL, "B": URL})
    with pytest.raises(ValueError):
        PagesConfig(every_hours=0)


SHIPPED = """sources:
  boards:
    enabled: true
  pages:                    # drive pages
    enabled: true
    watch: {}
  email_forward:
    enabled: true
"""


def test_add_and_remove_page_keep_the_rest() -> None:
    text = add_page(SHIPPED, NAME, URL)
    assert f"    watch:\n      TCS NextStep: {URL}\n  email_forward:" in text
    text = add_page(text, "Wipro Elite", "https://careers.example.com/elite")
    assert "      Wipro Elite:  https://careers.example.com/elite" in text
    with pytest.raises(EditError, match="already watched"):
        add_page(text, "Again", URL + "/")
    text, removed = remove_page(text, URL)
    assert removed == NAME and URL not in text and "Wipro Elite" in text
    with pytest.raises(EditError, match="no page"):
        remove_page(text, "Nope")


def test_add_page_creates_the_section() -> None:
    text = add_page("sources:\n  boards:\n    enabled: true\nllm:\n  extract_model: x\n", NAME, URL)
    assert text == (
        "sources:\n  boards:\n    enabled: true\n  pages:\n    watch:\n"
        f"      TCS NextStep: {URL}\nllm:\n  extract_model: x\n"
    )


def test_example_config_loads_with_pages() -> None:
    config = load_config(Path("config.example.yaml"))
    assert config.sources.pages.watch == {} and config.sources.pages.every_hours == 2
