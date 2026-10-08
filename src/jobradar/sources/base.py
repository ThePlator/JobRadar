"""IncomingMessage model and the Source protocol every message source implements."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, Field

from jobradar.db.models import Platform


class IncomingMessage(BaseModel):
    platform: Platform
    chat_id: str
    chat_title: str | None = None
    message_id: str
    text: str | None = None
    urls: list[str] = Field(default_factory=list)
    media_path: str | None = None
    posted_at: datetime


Emit = Callable[[IncomingMessage], Awaitable[None]]


class Source(Protocol):
    name: str

    async def start(self, emit: Emit) -> None:
        """Deliver messages to `emit` until stop() is called."""
        ...

    async def stop(self) -> None: ...
