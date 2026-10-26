"""Suggest a public source, and approve it. Reading and extracting the jobs is shared:
no user's id is on the source, the job or the extraction cache (SAASPLAN.md PR 7).

Follow and unfollow, which source each person wants, is the next PR.
"""

from __future__ import annotations

from jobradar.catalog import parse_suggestion, sync_config
from jobradar.db.models import Role, Source, SourceApproval, SourceOrigin
from jobradar.services.context import Ctx
from jobradar.services.errors import NotFound, Refused

__all__ = ["list_sources", "review", "suggest", "sync_config"]


def suggest(ctx: Ctx, platform: str, key: str, title: str | None = None) -> Source:
    """Propose a public channel, board or page. It is not read until an admin approves it.

    Suggesting one that is already approved returns it. One an admin turned down stays down.
    """
    kind, chat_id, name, detail = parse_suggestion(platform, key, title)
    existing = ctx.repo.source_by_key(kind, chat_id)
    if existing is not None:
        if existing.approval == SourceApproval.REJECTED.value:
            raise Refused("An admin turned this source down.")
        return existing
    return ctx.repo.put_catalog_source(
        kind,
        chat_id,
        title=name,
        approval=SourceApproval.PENDING.value,
        origin=SourceOrigin.SUGGESTION.value,
        detail_json=detail,
        suggested_by=ctx.user_id,
    )


def review(ctx: Ctx, source_id: int, decision: str) -> Source:
    """An admin approves or turns down a source. Approved suggestions are read on the next tick."""
    who = ctx.repo.account()
    if who is None or who.role != Role.ADMIN.value:
        raise Refused("Only an admin can approve or turn down a source.")
    try:
        approval = SourceApproval(decision)
    except ValueError:
        raise Refused("Say approved, rejected or pending.") from None
    src = ctx.repo.get_source(source_id)
    if src is None:
        raise NotFound("No source with that id.")
    return ctx.repo.put_catalog_source(
        src.platform,
        src.chat_id,
        title=src.title,
        approval=approval.value,
        origin=src.origin,
        detail_json=src.detail_json,
        suggested_by=src.suggested_by,
    )


def list_sources(ctx: Ctx) -> list[Source]:
    """An admin sees every source. Anyone else sees the approved ones and their own suggestions."""
    rows = ctx.repo.catalog_sources()
    who = ctx.repo.account()
    if who is not None and who.role == Role.ADMIN.value:
        return rows
    return [
        s
        for s in rows
        if s.approval == SourceApproval.APPROVED.value or s.suggested_by == ctx.user_id
    ]
