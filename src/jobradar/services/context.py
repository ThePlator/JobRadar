"""Who is asking, and when: passed to every page-level service.

Today JobRadar has one user, the owner, so `user_id` is always `OWNER`. The SaaS (SAASPLAN.md,
PR 5) fills it from the sign-in and scopes every query by it; the services keep these
signatures.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from jobradar.config import Settings
from jobradar.db.repo import Repo
from jobradar.queue.tasks import TaskQueue

OWNER = "owner"


@dataclass(frozen=True)
class Ctx:
    repo: Repo
    settings: Settings
    now: datetime  # the request's moment, so one page shows one consistent time
    user_id: str = OWNER
    queue: TaskQueue | None = None  # the running app's queue; None: one on the same database

    @property
    def tz(self) -> ZoneInfo:
        """The user's timezone, for showing times."""
        return self.settings.config.notify.tz

    @property
    def good(self) -> int:
        """A good match scores at least this (`scoring.alert_above`)."""
        return self.settings.config.scoring.alert_above

    @property
    def tasks(self) -> TaskQueue:
        """Where changes queue their follow-up work (Notion, resume builds)."""
        return self.queue or TaskQueue(self.repo)

    @property
    def today(self) -> date:
        """The user's calendar day right now."""
        return self.now.astimezone(self.tz).date()
