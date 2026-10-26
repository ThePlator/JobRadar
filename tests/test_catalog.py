"""The shared catalog (SaaS PR 7): config.yaml is the curated list, suggestions wait for
an admin, and a turned-down source is not read. Extraction stays one cache for everyone."""

import pytest

from jobradar.catalog import sync_config
from jobradar.config import AppConfig, BoardsConfig, PagesConfig, Settings
from jobradar.db.models import SourceApproval, SourceOrigin, User
from jobradar.db.repo import Repo
from jobradar.pipeline.fetch import Fetcher
from jobradar.pipeline.task_types import BOARD_CHECK, PAGE_CHECK
from jobradar.queue.tasks import TaskQueue
from jobradar.services import catalog as catalog
from jobradar.services.context import Ctx
from jobradar.services.errors import NotFound, Refused
from jobradar.sources.boards.watcher import BoardWatcher
from jobradar.sources.pages.watcher import PageWatcher
from tests.conftest import FakeClock
from tests.test_digest import settings as quiet


def configured() -> Settings:
    config = AppConfig.model_validate(
        {
            "sources": {
                "telegram": {"enabled": True, "chats": ["@offcampus"]},
                "boards": {"greenhouse": {"Stripe": "stripe"}},
                "pages": {"watch": {"Drives": "https://example.com/drives"}},
            }
        }
    )
    return quiet().model_copy(update={"config": config})


def here(repo: Repo, clock: FakeClock, user: str = "owner") -> Ctx:
    return Ctx(repo, configured(), clock(), user_id=user)


def stranger(repo: Repo) -> None:
    repo.add_user(
        User(
            id="u2",
            auth_uid="firebase-u2",
            role="user",
            created_at="2026-10-26T00:00:00.000000Z",
        )
    )


def test_config_becomes_the_catalog_and_leaving_it_stops_the_watch(repo: Repo) -> None:
    assert sync_config(repo, configured()) == 3
    assert sync_config(repo, configured()) == 3  # again: the same three rows
    board = repo.source_by_key("board", "greenhouse:stripe")
    page = repo.source_by_key("page", "page:https://example.com/drives")
    chat = repo.source_by_key("telegram", "@offcampus")
    assert board is not None and page is not None and chat is not None
    assert {board.origin, page.origin, chat.origin} == {SourceOrigin.CONFIG.value}
    assert board.approval == SourceApproval.APPROVED.value
    gone = quiet()  # telegram off, no boards, no pages
    assert sync_config(repo, gone) == 0
    left = repo.source_by_key("board", "greenhouse:stripe")
    assert left is not None and left.origin == SourceOrigin.SEEN.value


async def test_a_suggestion_is_read_only_after_an_admin_approves_it(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    stranger(repo)
    pending = catalog.suggest(
        here(repo, clock, "u2"), "board", "https://job-boards.greenhouse.io/notion", "Notion"
    )
    assert pending.id is not None
    assert pending.approval == SourceApproval.PENDING.value
    assert pending.suggested_by == "u2"
    empty = BoardWatcher(BoardsConfig(companies=[]), repo, queue, _emit, clock=clock)
    try:
        assert empty.queue_due() == 0  # pending: not read, and no user is involved
        with pytest.raises(Refused, match="admin"):
            catalog.review(here(repo, clock, "u2"), pending.id, "approved")
        approved = catalog.review(here(repo, clock), pending.id, "approved")
        assert approved.approval == SourceApproval.APPROVED.value
        assert empty.queue_due() == 1
        task = queue.claim([BOARD_CHECK])
        assert task is not None and task.key == "greenhouse:notion"
        again = catalog.suggest(here(repo, clock, "u2"), "board", "greenhouse:notion")
        assert again.id == approved.id  # already in the catalog
    finally:
        await empty.aclose()


async def test_turning_a_source_down_sticks_across_a_config_sync(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    sync_config(repo, configured())
    board = repo.source_by_key("board", "greenhouse:stripe")
    assert board is not None and board.id is not None
    catalog.review(here(repo, clock), board.id, "rejected")
    sync_config(repo, configured())  # still in config.yaml: the rejection wins
    kept = repo.source_by_key("board", "greenhouse:stripe")
    assert kept is not None and kept.approval == SourceApproval.REJECTED.value
    watcher = BoardWatcher(configured().config.sources.boards, repo, queue, _emit, clock=clock)
    try:
        assert watcher.queue_due() == 0
    finally:
        await watcher.aclose()
    with pytest.raises(Refused, match="turned this source down"):
        catalog.suggest(here(repo, clock), "board", "greenhouse:stripe")


def test_someone_elses_suggestion_stays_hidden_until_it_is_approved(
    repo: Repo, clock: FakeClock
) -> None:
    stranger(repo)
    mine = catalog.suggest(here(repo, clock, "u2"), "telegram", "https://t.me/asha_jobs")
    theirs = catalog.suggest(here(repo, clock), "telegram", "@secret_channel")
    visible = {s.id for s in catalog.list_sources(here(repo, clock, "u2"))}
    assert mine.id in visible and theirs.id not in visible
    assert theirs.id in {s.id for s in catalog.list_sources(here(repo, clock))}
    assert mine.chat_id == "@asha_jobs"


def test_a_suggestion_has_to_be_a_public_source(repo: Repo, clock: FakeClock) -> None:
    with pytest.raises(Refused, match="Email"):
        catalog.suggest(here(repo, clock), "email", "inbox")
    with pytest.raises(Refused, match="private"):
        catalog.suggest(here(repo, clock), "telegram", "https://t.me/+AbCdEf")
    with pytest.raises(Refused, match="https://"):
        catalog.suggest(here(repo, clock), "page", "not a url")
    with pytest.raises(NotFound):
        catalog.review(here(repo, clock), 999, "approved")
    with pytest.raises(Refused, match="Say approved"):
        catalog.review(here(repo, clock), 1, "maybe")


async def test_an_approved_page_is_watched_without_config(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    suggested = catalog.suggest(here(repo, clock), "page", "https://example.com/nqt", "NQT")
    assert suggested.id is not None
    catalog.review(here(repo, clock), suggested.id, "approved")
    watcher = PageWatcher(PagesConfig(watch={}), repo, queue, _emit, Fetcher(), clock=clock)
    try:
        assert watcher.queue_due() == 1
        task = queue.claim([PAGE_CHECK])
        assert task is not None and task.key == "https://example.com/nqt"
    finally:
        await watcher.fetcher.aclose()


async def _emit(msg: object) -> None:
    return None


def test_a_good_read_marks_the_source(repo: Repo) -> None:
    sync_config(repo, configured())
    repo.note_source_ok("board", "greenhouse:stripe", "2026-10-26T10:00:00.000000Z")
    src = repo.source_by_key("board", "greenhouse:stripe")
    assert src is not None and src.last_ok_at == "2026-10-26T10:00:00.000000Z"
