"""The shared catalog: public sources, read once for every user (SAASPLAN.md PR 7).

`config.yaml` is the operator's curated list. On start those boards, pages and Telegram
chats are written as approved sources (`origin` config). A user can suggest another public
source; the platform reads it only after an admin approves it. A source an admin has
turned down stays off, even if it is still listed in config, until an admin approves it
again. Removing one from config stops watching it; messages already saved stay.

Jobs, pages and the LLM cache have no user on them. Matching a job to a person is later
(fan-out). The platform's Telegram login is the one account in `.env`, never a user's.
"""

from __future__ import annotations

import json
import re
from typing import cast

from jobradar.config import BoardATS, BoardRef, Settings, board_from_link
from jobradar.db.models import Platform, Source, SourceApproval, SourceOrigin
from jobradar.db.repo import Repo
from jobradar.services.errors import Refused

PUBLIC = (Platform.TELEGRAM, Platform.BOARD, Platform.PAGE)
_PAGE = "page:"


def page_key(url: str) -> str:
    """The catalog key for a watched page, the same key ingest stores on the message."""
    return f"{_PAGE}{url}"


def sync_config(repo: Repo, settings: Settings) -> int:
    """Write the operator's config.yaml into the catalog. Returns how many sources that is.

    A source an admin has rejected is not switched back on by being listed here. One that
    left config is kept (`origin` seen) so its old messages still have a source, and it is
    no longer read.
    """
    wanted = _from_config(settings)
    for src in repo.catalog_sources(origin=SourceOrigin.CONFIG.value):
        if (src.platform, src.chat_id) not in wanted:
            repo.put_catalog_source(
                src.platform,
                src.chat_id,
                title=src.title,
                approval=src.approval,
                origin=SourceOrigin.SEEN.value,
                detail_json=src.detail_json,
            )
    for (platform, chat_id), (title, detail) in wanted.items():
        existing = repo.source_by_key(platform, chat_id)
        approval = (
            SourceApproval.REJECTED.value
            if existing is not None and existing.approval == SourceApproval.REJECTED.value
            else SourceApproval.APPROVED.value
        )
        repo.put_catalog_source(
            platform,
            chat_id,
            title=title,
            approval=approval,
            origin=SourceOrigin.CONFIG.value,
            detail_json=detail,
        )
    return len(wanted)


def has_approved_suggestion(repo: Repo, platform: str) -> bool:
    """True when an admin has approved a source that isn't in config.yaml."""
    return bool(
        repo.catalog_sources(
            platform=platform,
            approval=SourceApproval.APPROVED.value,
            origin=SourceOrigin.SUGGESTION.value,
        )
    )


def watch_boards(configured: dict[str, BoardRef], repo: Repo) -> dict[str, BoardRef]:
    """Boards to read this tick: config, plus approved suggestions, minus anything rejected."""
    boards = dict(configured)
    for src in repo.catalog_sources(
        platform=Platform.BOARD.value,
        approval=SourceApproval.APPROVED.value,
        origin=SourceOrigin.SUGGESTION.value,
    ):
        if (ref := board_ref(src)) is not None:
            boards[ref.key] = ref
    for src in repo.catalog_sources(
        platform=Platform.BOARD.value, approval=SourceApproval.REJECTED.value
    ):
        boards.pop(src.chat_id, None)
    return boards


def watch_pages(configured: dict[str, str], repo: Repo) -> dict[str, str]:
    """Pages to read this tick: url to name. Same rules as boards."""
    pages = dict(configured)
    for src in repo.catalog_sources(
        platform=Platform.PAGE.value,
        approval=SourceApproval.APPROVED.value,
        origin=SourceOrigin.SUGGESTION.value,
    ):
        if (found := page_of(src)) is not None:
            pages[found[0]] = found[1]
    for src in repo.catalog_sources(
        platform=Platform.PAGE.value, approval=SourceApproval.REJECTED.value
    ):
        if src.chat_id.startswith(_PAGE):
            pages.pop(src.chat_id.removeprefix(_PAGE), None)
    return pages


def watch_chats(configured: list[str | int], repo: Repo) -> list[str | int]:
    """Telegram chats to read: config, plus approved suggestions, minus rejected keys."""
    rejected = {
        s.chat_id
        for s in repo.catalog_sources(
            platform=Platform.TELEGRAM.value, approval=SourceApproval.REJECTED.value
        )
    }
    extra = [
        s.chat_id
        for s in repo.catalog_sources(
            platform=Platform.TELEGRAM.value,
            approval=SourceApproval.APPROVED.value,
            origin=SourceOrigin.SUGGESTION.value,
        )
    ]
    seen: set[str] = set()
    chats: list[str | int] = []
    for chat in [*configured, *extra]:
        if str(chat) in rejected or str(chat) in seen:
            continue
        seen.add(str(chat))
        chats.append(chat)
    return chats


def parse_suggestion(
    platform: str, key: str, title: str | None
) -> tuple[str, str, str | None, str]:
    """Check a suggestion and return (platform, catalog key, title, detail json).

    Raises Refused when it isn't a public Telegram channel, company board or page.
    """
    kind = next((item for item in PUBLIC if item.value == platform), None)
    if kind is None:
        raise Refused(
            "Suggest a public Telegram channel, a company board or a page. "
            "Email and WhatsApp stay on your own computer."
        )
    key = key.strip()
    if kind is Platform.TELEGRAM:
        return (kind.value, *_telegram(key, title))
    if kind is Platform.BOARD:
        return (kind.value, *_board(key, title))
    return (kind.value, *_page(key, title))


def board_ref(src: Source) -> BoardRef | None:
    """The board a suggestion describes, or None when the detail isn't a board we can read."""
    data = _detail(src)
    ats, company = data.get("ats"), data.get("company")
    if not isinstance(ats, str) or not isinstance(company, str) or ats == "freehire":
        return None
    raw_name = data.get("name")
    name = raw_name if isinstance(raw_name, str) else src.title
    try:
        return BoardRef(ats=cast(BoardATS, ats), company=company, name=name)
    except ValueError:
        return None


def page_of(src: Source) -> tuple[str, str] | None:
    """(url, name) for a page suggestion."""
    data = _detail(src)
    url = data.get("url")
    if not isinstance(url, str) or page_key(url) != src.chat_id:
        return None
    return url, src.title or url


def _from_config(settings: Settings) -> dict[tuple[str, str], tuple[str | None, str]]:
    out: dict[tuple[str, str], tuple[str | None, str]] = {}
    telegram = settings.config.sources.telegram
    if telegram.enabled:
        for chat in telegram.chats:
            key = str(chat).strip()
            if key:
                out[(Platform.TELEGRAM.value, key)] = (key, json.dumps({"chat": key}))
    boards = settings.config.sources.boards
    if boards.enabled:
        for ref in boards.companies:
            detail = {"ats": ref.ats, "company": ref.company, "name": ref.name}
            out[(Platform.BOARD.value, ref.key)] = (ref.label, json.dumps(detail))
    pages = settings.config.sources.pages
    if pages.enabled:
        for name, url in pages.watch.items():
            out[(Platform.PAGE.value, page_key(url))] = (name, json.dumps({"url": url}))
    return out


def _detail(src: Source) -> dict[str, object]:
    if not src.detail_json:
        return {}
    loaded = json.loads(src.detail_json)
    return loaded if isinstance(loaded, dict) else {}


def _telegram(key: str, title: str | None) -> tuple[str, str | None, str]:
    if key.startswith("https://t.me/") or key.startswith("https://telegram.me/"):
        key = "@" + key.rstrip("/").rsplit("/", 1)[-1]
    if key.startswith("@+") or "joinchat" in key or not key:
        raise Refused("Only public channels. JobRadar doesn't join private groups.")
    if not (key.startswith("@") and len(key) > 1) and re.fullmatch(r"-?\d+", key) is None:
        raise Refused("Give a public channel as @name, t.me/name, or its numeric id.")
    return key, title or key, json.dumps({"chat": key})


def _board(key: str, title: str | None) -> tuple[str, str | None, str]:
    try:
        if re.match(r"^https?://", key):
            found, company = board_from_link(key)
            ref = BoardRef(ats=cast(BoardATS, found), company=company, name=title)
        elif ":" in key:
            found, company = key.split(":", 1)
            ref = BoardRef(ats=cast(BoardATS, found), company=company, name=title)
        else:
            raise ValueError(key)
    except ValueError as e:
        raise Refused(
            "That isn't a Greenhouse, Lever, Ashby, SmartRecruiters, Workday or Oracle board. "
            "Paste the board link, or write greenhouse:stripe."
        ) from e
    detail = {"ats": ref.ats, "company": ref.company, "name": ref.name}
    return ref.key, ref.label, json.dumps(detail)


def _page(key: str, title: str | None) -> tuple[str, str | None, str]:
    if not re.match(r"^https?://[^\s/]+\.[^\s/]+", key):
        raise Refused("Give the page's address, starting with https://.")
    return page_key(key), title or key, json.dumps({"url": key})
