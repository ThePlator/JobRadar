"""Adding a job by hand: a link goes in like a post, through the same pipeline."""

from __future__ import annotations

import time

from jobradar.db.models import Platform
from jobradar.pipeline.ingest import make_emit
from jobradar.services.context import Ctx
from jobradar.sources.base import IncomingMessage


async def add_link(ctx: Ctx, url: str) -> None:
    """Queue a job link; the worker reads, scores and files it like any other."""
    msg = IncomingMessage(
        platform=Platform.MANUAL,
        chat_id="cli",
        chat_title="Added by hand",
        message_id=str(time.time_ns()),
        text=url,
        urls=[url],
        posted_at=ctx.now,
    )
    await make_emit(ctx.repo, ctx.tasks)(msg)
