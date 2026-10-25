"""Who is asking, and when: passed to every page-level service.

The self-hosted app has one user, the owner (`OWNER`). In the SaaS `user_id` comes from the
sign-in, and `repo` is scoped to that user: every score and status it reads or writes is
theirs (SAASPLAN.md section 9).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from functools import cached_property
from zoneinfo import ZoneInfo

from jobradar.config import Preferences, Settings
from jobradar.db.models import OWNER
from jobradar.db.repo import Repo
from jobradar.queue.tasks import TaskQueue

__all__ = ["OWNER", "Ctx"]


@dataclass(frozen=True)
class Ctx:
    repo: Repo
    settings: Settings
    now: datetime  # the request's moment, so one page shows one consistent time
    user_id: str = OWNER
    queue: TaskQueue | None = None  # the running app's queue; None: one on the same database

    def __post_init__(self) -> None:
        # Whatever repo was handed in, queries made through this Ctx are for its user.
        object.__setattr__(self, "repo", self.repo.for_user(self.user_id))

    @cached_property
    def prefs(self) -> Preferences:
        """The user's preferences (the owner's: config.yaml)."""
        from jobradar.profile import user_preferences

        return user_preferences(self.repo, self.settings)

    @property
    def tz(self) -> ZoneInfo:
        """The user's timezone, for showing times."""
        return self.prefs.tz

    @property
    def good(self) -> int:
        """A good match scores at least this (`scoring.alert_above`)."""
        return self.prefs.scoring.alert_above

    @property
    def tasks(self) -> TaskQueue:
        """Where changes queue their follow-up work (Notion, resume builds)."""
        return self.queue or TaskQueue(self.repo)

    @property
    def today(self) -> date:
        """The user's calendar day right now."""
        return self.now.astimezone(self.tz).date()
