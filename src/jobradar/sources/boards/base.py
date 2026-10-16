"""What every board reader returns, and how failures are told apart.

A board that answers 404 is `BoardGone` (maybe renamed or closed). Anything else that goes
wrong (timeouts, 429, 5xx, malformed JSON) is `BoardUnavailable`: the board is *unknown* this
round, never "empty". Treating an error as an empty list would close every job on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx


@dataclass(frozen=True)
class BoardPosting:
    id: str
    title: str
    location: str  # everything the board says about where, e.g. "Bengaluru, India · Remote"
    url: str
    posted_at: datetime | None = None
    level: str | None = None  # experience level, when the board states it


class Partial(list[BoardPosting]):
    """A read that stopped at the reader's page cap before the board's last posting. The
    postings it left out haven't closed, so the watcher must not close them."""


class BoardGone(Exception):
    """The board answered 404."""


class BoardUnavailable(Exception):
    """The board couldn't be read this time; try again next round."""


async def get_json(
    client: httpx.AsyncClient, url: str, params: dict[str, Any] | None = None
) -> Any:
    return await _json(client, "GET", url, params=params)


async def post_json(client: httpx.AsyncClient, url: str, body: dict[str, Any]) -> Any:
    return await _json(client, "POST", url, json=body)


async def _json(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> Any:
    try:
        resp = await client.request(method, url, headers={"Accept": "application/json"}, **kwargs)
    except httpx.HTTPError as e:
        raise BoardUnavailable(f"could not reach {httpx.URL(url).host}: {type(e).__name__}") from e
    if resp.status_code == 404:
        raise BoardGone(f"{url} answered 404")
    if resp.status_code != 200:
        raise BoardUnavailable(f"{httpx.URL(url).host} answered {resp.status_code}")
    try:
        return resp.json()
    except ValueError as e:
        raise BoardUnavailable(f"{httpx.URL(url).host} sent something that isn't JSON") from e


def parse_time(value: Any) -> datetime | None:
    """ISO strings ("2026-10-13T06:02:58.058Z") or epoch milliseconds (Lever)."""
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000).astimezone()
        if isinstance(value, str) and value:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        return None
    return None
