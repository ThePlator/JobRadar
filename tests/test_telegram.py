from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from telethon.tl.types import (
    InlineButtonTypeUrl,
    KeyboardButton,
    MessageEntityTextUrl,
    MessageEntityUrl,
)

from jobradar.config import AppConfig, ConfigError, Secrets, Settings
from jobradar.db.models import Platform
from jobradar.db.repo import Repo
from jobradar.sources.telegram import TelegramSource, message_urls, to_incoming


def tg_message(
    text: str, entities: list[Any] | None = None, buttons: list[str] | None = None
) -> Any:
    markup = None
    if buttons:
        markup = SimpleNamespace(
            rows=[
                SimpleNamespace(
                    buttons=[
                        KeyboardButton(text="Apply", type=InlineButtonTypeUrl(url=u))
                        for u in buttons
                    ]
                    + [SimpleNamespace(text="old layer", url="https://old.example/j")]
                )
            ]
        )
    return SimpleNamespace(
        id=42, message=text, entities=entities or [], reply_markup=markup,
        date=datetime(2026, 10, 8, 8, 0, tzinfo=UTC), photo=None,
    )  # fmt: skip


def test_urls_from_entities_text_and_buttons() -> None:
    text = "🔥🔥 Apply here: acme.com/jobs and more"
    # "🔥" is 2 UTF-16 units each, so "acme.com/jobs" starts at offset 17, not 15.
    start = len("🔥🔥 Apply here: ".encode("utf-16-le")) // 2
    msg = tg_message(
        text,
        entities=[
            MessageEntityUrl(offset=start, length=len("acme.com/jobs")),
            MessageEntityTextUrl(offset=0, length=2, url="https://forms.gle/xyz"),
        ],
        buttons=["https://jobs.lever.co/acme/1"],
    )
    assert message_urls(msg) == [
        "acme.com/jobs",
        "https://forms.gle/xyz",
        "https://jobs.lever.co/acme/1",
        "https://old.example/j",
    ]


def test_to_incoming() -> None:
    incoming = to_incoming(tg_message("SDE https://acme.com/j"), "-100123", "Jobs")
    assert incoming.platform == Platform.TELEGRAM
    assert incoming.message_id == "42"
    assert incoming.urls == ["https://acme.com/j"]
    assert incoming.posted_at.tzinfo is not None


class FakeClient:
    def __init__(self, known: dict[str, Any]) -> None:
        self.known = known

    async def get_dialogs(self) -> None:
        return None

    async def get_entity(self, chat: Any) -> Any:
        if str(chat) not in self.known:
            raise ValueError("not found")
        return self.known[str(chat)]


async def test_unknown_chats_fail_with_a_hint(repo: Repo) -> None:
    settings = Settings(
        config=AppConfig.model_validate({"sources": {"telegram": {"chats": ["@ok", "@nope", 5]}}}),
        secrets=Secrets.model_validate({"tg_api_id": 1, "tg_api_hash": "h"}),
    )
    channel = SimpleNamespace(title="OK Jobs", username="ok")
    client: Any = FakeClient({"@ok": channel})
    source = TelegramSource(settings, repo, client=client)

    import jobradar.sources.telegram as tg

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tg.utils, "get_peer_id", lambda entity: -1001)
        with pytest.raises(ConfigError, match=r"not found or not joined: @nope, 5"):
            await source._resolve_chats(settings.config.sources.telegram.chats)
    assert repo.upsert_source(Platform.TELEGRAM, "-1001").title == "OK Jobs"
