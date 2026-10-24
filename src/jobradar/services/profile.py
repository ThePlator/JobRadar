"""Changes to your profile that the dashboard makes (the profile itself is `profile.yaml`;
in the SaaS it becomes a table, SAASPLAN.md PR 5)."""

from __future__ import annotations

from jobradar.config import ConfigError
from jobradar.profile import add_skill
from jobradar.services.context import Ctx
from jobradar.services.errors import Refused


def know_skill(ctx: Ctx, name: str) -> str:
    """ "I know this": add a skill to the profile. Returns the backup file's name."""
    stamp = ctx.now.astimezone(ctx.tz).strftime("%Y%m%d-%H%M%S")
    try:
        return add_skill(ctx.settings.config.matching.profile_path, name, stamp)
    except ConfigError as e:
        raise Refused(str(e)) from e
