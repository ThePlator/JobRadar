"""The task queue, as the user sees it."""

from __future__ import annotations

from jobradar.services.context import Ctx


def retry_failed(ctx: Ctx) -> int:
    """Put failed tasks back in the queue; returns how many."""
    return ctx.tasks.requeue_failed()
