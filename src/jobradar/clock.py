"""UTC timestamps stored as fixed-width ISO-8601 text, so string order equals time order."""

from collections.abc import Callable
from datetime import UTC, datetime

Clock = Callable[[], datetime]

_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("naive datetime; use timezone-aware UTC")
    return dt.astimezone(UTC).strftime(_FORMAT)


def from_iso(s: str) -> datetime:
    return datetime.strptime(s, _FORMAT).replace(tzinfo=UTC)
