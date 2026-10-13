"""One reader per hiring system. Each takes the company's board name and returns its open
postings as `BoardPosting`s. Hosts are fixed here; the board name is checked against a strict
pattern in config, so a config value can't send requests anywhere else."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from jobradar.sources.boards.base import BoardGone, BoardPosting, get_json, parse_time

Reader = Callable[[httpx.AsyncClient, str], Awaitable[list[BoardPosting]]]

SR_PAGE = 100
SR_MAX_PAGES = 20  # 2,000 India postings is plenty for one company
SR_PAGE_PAUSE = 1.0  # seconds between pages


def _join(*parts: Any) -> str:
    return " · ".join(str(p).strip() for p in parts if p and str(p).strip())


async def greenhouse(client: httpx.AsyncClient, board: str) -> list[BoardPosting]:
    data = await get_json(client, f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs")
    return [
        BoardPosting(
            id=str(j["id"]),
            title=str(j.get("title") or ""),
            location=_join((j.get("location") or {}).get("name")),
            url=str(j.get("absolute_url") or ""),
            posted_at=parse_time(j.get("first_published") or j.get("updated_at")),
        )
        for j in data.get("jobs", [])
        if j.get("id") and j.get("absolute_url")
    ]


async def lever(client: httpx.AsyncClient, board: str) -> list[BoardPosting]:
    try:
        data = await get_json(client, f"https://api.lever.co/v0/postings/{board}", {"mode": "json"})
    except BoardGone:  # companies on Lever's EU instance live on a different host
        data = await get_json(
            client, f"https://api.eu.lever.co/v0/postings/{board}", {"mode": "json"}
        )
    postings = []
    for j in data if isinstance(data, list) else []:
        cats = j.get("categories") or {}
        postings.append(
            BoardPosting(
                id=str(j.get("id") or ""),
                title=str(j.get("text") or ""),
                location=_join(
                    cats.get("location"),
                    ", ".join(cats.get("allLocations") or []),
                    j.get("workplaceType"),
                ),
                url=str(j.get("hostedUrl") or ""),
                posted_at=parse_time(j.get("createdAt")),
                level=cats.get("commitment"),
            )
        )
    return [p for p in postings if p.id and p.url]


async def ashby(client: httpx.AsyncClient, board: str) -> list[BoardPosting]:
    data = await get_json(client, f"https://api.ashbyhq.com/posting-api/job-board/{board}")
    postings = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        others = ", ".join(s.get("location", "") for s in j.get("secondaryLocations") or [])
        postings.append(
            BoardPosting(
                id=str(j.get("id") or ""),
                title=str(j.get("title") or ""),
                location=_join(j.get("location"), others, "Remote" if j.get("isRemote") else None),
                url=str(j.get("jobUrl") or ""),
                posted_at=parse_time(j.get("publishedAt")),
                level=j.get("employmentType"),
            )
        )
    return [p for p in postings if p.id and p.url]


async def smartrecruiters(client: httpx.AsyncClient, board: str) -> list[BoardPosting]:
    """India only (`country=in`): SmartRecruiters boards of big companies list thousands of
    jobs worldwide."""
    postings: list[BoardPosting] = []
    for page in range(SR_MAX_PAGES):
        data = await get_json(
            client,
            f"https://api.smartrecruiters.com/v1/companies/{board}/postings",
            {"country": "in", "limit": SR_PAGE, "offset": page * SR_PAGE},
        )
        content = data.get("content") or []
        for j in content:
            loc = j.get("location") or {}
            postings.append(
                BoardPosting(
                    id=str(j.get("id") or ""),
                    title=str(j.get("name") or ""),
                    location=_join(
                        loc.get("fullLocation") or loc.get("city"),
                        "Remote" if loc.get("remote") else None,
                    ),
                    url=f"https://jobs.smartrecruiters.com/{board}/{j.get('id')}",
                    posted_at=parse_time(j.get("releasedDate")),
                    level=(j.get("experienceLevel") or {}).get("id"),
                )
            )
        if len(content) < SR_PAGE or len(postings) >= int(data.get("totalFound") or 0):
            break
        await asyncio.sleep(SR_PAGE_PAUSE)  # SmartRecruiters answers 400 to quick paging
    return [p for p in postings if p.id]


READERS: dict[str, Reader] = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "smartrecruiters": smartrecruiters,
}
