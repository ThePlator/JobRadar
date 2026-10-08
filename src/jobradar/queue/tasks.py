"""enqueue(), claim(), complete(), fail(): the retry policy on top of the task table."""

from __future__ import annotations

import random
from datetime import timedelta
from typing import Any

from jobradar.clock import Clock, to_iso, utcnow
from jobradar.db.repo import Repo, TaskRecord

MAX_ATTEMPTS = 5
STUCK_AFTER = timedelta(minutes=10)
_BASE_DELAY_S = 30
_MAX_DELAY_S = 3600


class RetryableError(Exception):
    """Temporary failure (timeout, 5xx, 429): retry with backoff."""

    def __init__(self, message: str = "", retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after  # seconds, e.g. from a Retry-After header


class PermanentError(Exception):
    """Retrying will not help (403, 404, invalid input): mark the task failed now."""


class Defer(Exception):
    """Postpone without counting an attempt, e.g. Telegram FloodWait."""

    def __init__(self, seconds: float) -> None:
        super().__init__(f"deferred {seconds:.0f}s")
        self.seconds = seconds


def backoff_seconds(attempt: int, rng: random.Random | None = None) -> float:
    """min(30 s x 4^(n-1), 1 h) with +-20% jitter, for attempt n >= 1."""
    base = float(min(_BASE_DELAY_S * 4 ** (max(attempt, 1) - 1), _MAX_DELAY_S))
    jitter: float = (rng or random).uniform(0.8, 1.2)
    return base * jitter


class TaskQueue:
    def __init__(
        self,
        repo: Repo,
        clock: Clock = utcnow,
        max_attempts: int = MAX_ATTEMPTS,
        rng: random.Random | None = None,
    ) -> None:
        self.repo = repo
        self._clock = clock
        self.max_attempts = max_attempts
        self._rng = rng or random.Random()  # noqa: S311  (jitter, not crypto)

    def _at(self, seconds: float = 0) -> str:
        return to_iso(self._clock() + timedelta(seconds=seconds))

    def enqueue(
        self, type_: str, key: str, payload: dict[str, Any] | None = None, delay: float = 0
    ) -> None:
        self.repo.enqueue(type_, key, payload, run_after=self._at(delay))

    def claim(self, types: list[str] | None = None) -> TaskRecord | None:
        return self.repo.claim(types)

    def complete(self, task: TaskRecord) -> None:
        self.repo.complete(task.id)

    def retry(self, task: TaskRecord, error: str, retry_after: float | None = None) -> bool:
        """Schedule another attempt, or fail the task after max_attempts. True if retried."""
        if task.attempts >= self.max_attempts:
            self.repo.fail(task.id, error, retry_at=None)
            return False
        if retry_after is not None:
            delay = retry_after  # the server said when; honour it (HTTP 429 Retry-After)
        else:
            delay = backoff_seconds(task.attempts, self._rng)
        self.repo.fail(task.id, error, retry_at=self._at(delay))
        return True

    def fail(self, task: TaskRecord, error: str) -> None:
        self.repo.fail(task.id, error, retry_at=None)

    def defer(self, task: TaskRecord, seconds: float) -> None:
        self.repo.defer(task.id, until=self._at(seconds))

    def reclaim_stuck(self) -> int:
        return self.repo.reclaim_stuck(started_before=to_iso(self._clock() - STUCK_AFTER))

    def requeue_failed(self) -> int:
        return self.repo.requeue_failed()
