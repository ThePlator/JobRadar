"""Your profile: reading it, and the changes the dashboard makes to it. The owner edits
profile.yaml (copied into the database when it changes); anyone else's lives only in the
database (SAASPLAN.md, PR 5c)."""

from __future__ import annotations

from typing import Any

from jobradar.config import ConfigError
from jobradar.db.models import OWNER
from jobradar.profile import add_skill, user_profile, with_skill
from jobradar.services.context import Ctx
from jobradar.services.errors import Refused


def profile(ctx: Ctx) -> dict[str, Any] | None:
    """The user's profile, or None when there's none (or it can't be read)."""
    try:
        return user_profile(ctx.repo, ctx.settings)
    except ConfigError:
        return None


def know_skill(ctx: Ctx, name: str) -> str | None:
    """ "I know this": add a skill to the profile. The owner's profile.yaml gets it too (with a
    backup first, whose name is returned); anyone else's is changed in the database only."""
    if ctx.user_id == OWNER and ctx.settings.config.matching.profile_path.is_file():
        stamp = ctx.now.astimezone(ctx.tz).strftime("%Y%m%d-%H%M%S")
        try:
            backup = add_skill(ctx.settings.config.matching.profile_path, name, stamp)
        except ConfigError as e:
            raise Refused(str(e)) from e
        user_profile(ctx.repo, ctx.settings)  # copy the changed file in now
        return backup
    current = profile(ctx)
    if current is None:
        raise Refused("Add your profile first, then the skills you know.")
    try:
        ctx.repo.save_profile(with_skill(current, name))
    except ConfigError as e:
        raise Refused(str(e)) from e
    return None
