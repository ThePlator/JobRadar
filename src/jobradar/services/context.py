"""Who is asking, and when: passed to every page-level service.

Today JobRadar has one user, the owner, so `user_id` is always `OWNER`. The SaaS (SAASPLAN.md,
PR 5) fills it from the sign-in and scopes every query by it; the services keep these
signatures.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from jobradar.config import Settings
from jobradar.db.repo import Repo

OWNER = "owner"


@dataclass(frozen=True)
class Ctx:
    repo: Repo
    settings: Settings
    now: datetime  # the request's moment, so one page shows one consistent time
    user_id: str = OWNER

    @property
    def tz(self) -> ZoneInfo:
        """The user's timezone, for showing times."""
        return self.settings.config.notify.tz

    @property
    def good(self) -> int:
        """A good match scores at least this (`scoring.alert_above`)."""
        return self.settings.config.scoring.alert_above
