"""Telethon user client: catch-up and live messages from the configured chats (read-only).

Never sends messages, joins chats or marks anything read. Flood waits are slept out.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from telethon import TelegramClient, events, utils
from telethon.errors import FloodWaitError
from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl

from jobradar.config import ConfigError, Settings
from jobradar.db.models import Platform
from jobradar.db.repo import Repo
from jobradar.pipeline.links import extract_urls
from jobradar.sources.base import Emit, IncomingMessage

log = logging.getLogger(__name__)

SESSION_PATH = Path("data/tg")  # Telethon adds ".session"
MEDIA_DIR = Path("data/media")


def make_client(settings: Settings, session: Path = SESSION_PATH) -> TelegramClient:
    settings.require("telegram")
    api_id, api_hash = settings.secrets.tg_api_id, settings.secrets.tg_api_hash
    if api_id is None or api_hash is None:  # narrowed for mypy; require() checked it
        raise ConfigError("Missing TG_API_ID / TG_API_HASH in .env")
    session.parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(str(session), api_id, api_hash.get_secret_value())


def protect_session(session: Path = SESSION_PATH) -> None:
    """The session file grants full account access: owner read/write only."""
    path = session.with_suffix(".session")
    if path.exists():
        os.chmod(path, 0o600)


def _utf16_slice(text: str, offset: int, length: int) -> str:
    # Telegram entity offsets count UTF-16 code units, not Python characters.
    raw = text.encode("utf-16-le")
    return raw[2 * offset : 2 * (offset + length)].decode("utf-16-le", errors="ignore")


def message_urls(message: Any) -> list[str]:
    """URLs from link entities, plain-text links and inline URL buttons, in order."""
    text = message.message or ""
    urls: list[str] = []
    for entity in message.entities or []:
        if isinstance(entity, MessageEntityTextUrl):
            urls.append(entity.url)
        elif isinstance(entity, MessageEntityUrl):
            urls.append(_utf16_slice(text, entity.offset, entity.length))
    urls += extract_urls(text)  # catches links Telegram did not mark up
    markup = getattr(message, "reply_markup", None)
    for row in getattr(markup, "rows", None) or []:
        for button in row.buttons:
            # Older layers: button.url; newer layers: button.type.url (InlineButtonTypeUrl).
            url = getattr(button, "url", None) or getattr(
                getattr(button, "type", None), "url", None
            )
            if url:
                urls.append(url)
    return list(dict.fromkeys(u.strip() for u in urls if u and u.strip()))


def to_incoming(
    message: Any, chat_id: str, chat_title: str | None, media_path: str | None = None
) -> IncomingMessage:
    date: datetime = message.date
    return IncomingMessage(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        chat_title=chat_title,
        message_id=str(message.id),
        text=message.message or None,
        urls=message_urls(message),
        media_path=media_path,
        posted_at=date if date.tzinfo else date.replace(tzinfo=UTC),
    )


async def _flood_safe(it: AsyncIterator[Any]) -> AsyncIterator[Any]:
    while True:
        try:
            item = await anext(it)
        except StopAsyncIteration:
            return
        except FloodWaitError as e:
            log.warning("telegram flood wait", extra={"seconds": e.seconds})
            await asyncio.sleep(e.seconds + 1)
            continue
        yield item


class TelegramSource:
    name = "telegram"

    def __init__(self, settings: Settings, repo: Repo, client: TelegramClient | None = None):
        self._cfg = settings.config.sources.telegram
        self._repo = repo
        self._client = client or make_client(settings)
        self._stopped = asyncio.Event()

    async def _resolve_chats(self, chats: Iterable[str | int]) -> dict[str, tuple[Any, int]]:
        """peer id -> (entity, source row id). Fails listing every chat it cannot open."""
        await self._client.get_dialogs()  # fills the entity cache so numeric ids resolve
        resolved: dict[str, tuple[Any, int]] = {}
        unknown: list[str] = []
        for chat in chats:
            try:
                entity = await self._client.get_entity(chat)
            except (ValueError, TypeError):
                unknown.append(str(chat))
                continue
            peer_id = str(utils.get_peer_id(entity))
            title = getattr(entity, "title", None) or getattr(entity, "username", None)
            source = self._repo.upsert_source(Platform.TELEGRAM, peer_id, title)
            if source.id is None:
                raise RuntimeError("source row has no id")
            resolved[peer_id] = (entity, source.id)
        if unknown:
            raise ConfigError(
                "Telegram chats not found or not joined: " + ", ".join(unknown)
                + ". Run `jobradar chats` to list the ids you can use."
            )  # fmt: skip
        return resolved

    async def _deliver(self, message: Any, peer_id: str, source_id: int, emit: Emit) -> None:
        if getattr(message, "action", None) is not None:
            # Service notice ("channel created", "photo changed"): not a post.
            self._repo.set_last_msg_id(source_id, int(message.id))
            return
        media_path = None
        if getattr(message, "photo", None) and not message_urls(message):
            await asyncio.to_thread(MEDIA_DIR.mkdir, parents=True, exist_ok=True)
            target = MEDIA_DIR / f"{peer_id}_{message.id}.jpg"
            media_path = str(await message.download_media(file=str(target)))
        chat = getattr(message, "chat", None)
        title = getattr(chat, "title", None)
        await emit(to_incoming(message, peer_id, title, media_path))
        self._repo.set_last_msg_id(source_id, int(message.id))

    async def start(self, emit: Emit) -> None:
        if not self._cfg.enabled:
            return
        await self._client.connect()
        if not await self._client.is_user_authorized():
            raise ConfigError("Not logged in to Telegram. Run `jobradar init` first.")
        protect_session()
        chats = await self._resolve_chats(self._cfg.chats)

        # Register the live handler before catching up, so nothing posted meanwhile is missed;
        # duplicates are dropped by the raw_message unique key.
        async def on_new(event: Any) -> None:
            peer_id = str(event.chat_id)
            if peer_id in chats:
                await self._deliver(event.message, peer_id, chats[peer_id][1], emit)

        entities = [entity for entity, _ in chats.values()]
        self._client.add_event_handler(on_new, events.NewMessage(chats=entities))

        for peer_id, (entity, source_id) in chats.items():
            await self._catch_up(entity, peer_id, source_id, emit)
        log.info("telegram listening", extra={"chats": len(chats)})

        disconnected = asyncio.ensure_future(self._client.run_until_disconnected())
        stopped = asyncio.ensure_future(self._stopped.wait())
        await asyncio.wait({disconnected, stopped}, return_when=asyncio.FIRST_COMPLETED)
        stopped.cancel()
        if not disconnected.done():
            await self._client.disconnect()
            await asyncio.gather(disconnected, return_exceptions=True)

    async def _catch_up(self, entity: Any, peer_id: str, source_id: int, emit: Emit) -> None:
        source = self._repo.upsert_source(Platform.TELEGRAM, peer_id)
        if source.last_msg_id:
            it = self._client.iter_messages(entity, min_id=source.last_msg_id, reverse=True)
        else:
            since = datetime.now(UTC) - timedelta(days=self._cfg.backfill_days)
            it = self._client.iter_messages(entity, offset_date=since, reverse=True)
        count = 0
        async for message in _flood_safe(aiter(it)):
            if self._stopped.is_set():
                return
            await self._deliver(message, peer_id, source_id, emit)
            count += 1
        log.info("telegram catch-up done", extra={"chat": peer_id, "messages": count})

    async def stop(self) -> None:
        self._stopped.set()


async def list_chats(client: TelegramClient) -> list[tuple[str, str, str]]:
    """(peer id, kind, title) for every channel and group the account can read."""
    rows: list[tuple[str, str, str]] = []
    async for dialog in client.iter_dialogs():
        if dialog.is_channel or dialog.is_group:
            kind = "channel" if dialog.is_channel and not dialog.is_group else "group"
            rows.append((str(dialog.id), kind, dialog.name or ""))
    return rows
