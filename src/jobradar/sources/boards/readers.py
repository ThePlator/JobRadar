"""One reader per hiring system. Each takes the company's board name and returns its open
postings as `BoardPosting`s. Hosts are fixed here; the board name is checked against a strict
pattern in config, so a config value can't send requests anywhere else."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from jobradar.sources.boards.base import (
    BoardGone,
    BoardPosting,
    BoardUnavailable,
    Partial,
    get_json,
    parse_time,
    post_json,
)

Reader = Callable[[httpx.AsyncClient, str], Awaitable[list[BoardPosting]]]

SR_PAGE = 100
SR_MAX_PAGES = 20  # 2,000 India postings is plenty for one company
SR_PAGE_PAUSE = 1.0  # seconds between pages
WD_PAGE = 20  # Workday refuses more than 20 per request
WD_MAX_PAGES = 15  # 300 India postings per check
WD_PAGE_PAUSE = 1.5  # seconds between pages: an unofficial API, read gently


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
    else:
        return Partial(p for p in postings if p.id)  # stopped at the cap
    return [p for p in postings if p.id]


# Workday ---------------------------------------------------------------------------------------
#
# Every Workday career site (company.wd5.myworkdayjobs.com/Site) loads its jobs from a JSON
# search API on the same host. It isn't documented, so everything unexpected is "unavailable"
# (unknown this round), never an empty board. Each company names its location filter
# differently ("locationHierarchy1", "Location_Country", …), so the India filter is found once
# in the filters the site offers and kept in memory.

_WD_INDIA: dict[str, tuple[str, list[str]]] = {}
_POSTED_AGO = re.compile(r"(\d+)\+?\s+days?\s+ago", re.IGNORECASE)


def _india_filter(facets: Any) -> tuple[str, list[str]] | None:
    """(filter name, [ids]) for India: a country value named "India" anywhere in the (nested)
    filters, else every site value starting "India," (e.g. "India, Bengaluru")."""
    countries: list[tuple[str, str]] = []
    sites: list[tuple[str, str]] = []

    def walk(items: Any, parent: str | None) -> None:
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            name = item.get("facetParameter") or parent
            values = item.get("values")
            if isinstance(values, list):
                walk(values, name)
                continue
            label = str(item.get("descriptor") or "").strip().casefold()
            if name and item.get("id"):
                if label == "india":
                    countries.append((name, str(item["id"])))
                elif label.startswith("india,") or label.startswith("india -"):
                    sites.append((name, str(item["id"])))

    walk(facets, None)
    for found in (countries, sites):
        if found:
            name = found[0][0]
            return name, [i for n, i in found if n == name]
    return None


def _posted(text: Any, now: datetime) -> datetime | None:
    """'Posted Today' / 'Posted Yesterday' / 'Posted 3 Days Ago'; '30+ Days Ago' is too vague."""
    said = str(text or "").casefold()
    if "today" in said:
        return now
    if "yesterday" in said:
        return now - timedelta(days=1)
    if (m := _POSTED_AGO.search(said)) and "+" not in said:
        return now - timedelta(days=int(m.group(1)))
    return None


async def workday(client: httpx.AsyncClient, board: str) -> list[BoardPosting]:
    """India only. `board` is "company.wd5.myworkdayjobs.com/Site" (checked in config)."""
    host, site = board.split("/", 1)
    base = f"https://{host}/wday/cxs/{host.split('.')[0]}/{site}"
    india = _WD_INDIA.get(board)
    if india is None:
        first = await post_json(
            client, f"{base}/jobs", {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}
        )
        india = _india_filter(first.get("facets") if isinstance(first, dict) else None)
        if india is None:
            raise BoardUnavailable("this Workday site has no India location filter")
        _WD_INDIA[board] = india
        await asyncio.sleep(WD_PAGE_PAUSE)
    now = datetime.now(UTC)
    postings: list[BoardPosting] = []
    total = 0
    for page in range(WD_MAX_PAGES):
        body = {"appliedFacets": {india[0]: india[1]}, "limit": WD_PAGE,
                "offset": page * WD_PAGE, "searchText": ""}  # fmt: skip
        try:
            data = await post_json(client, f"{base}/jobs", body)
        except BoardUnavailable:
            _WD_INDIA.pop(board, None)  # the filter may have changed: look it up again next time
            raise
        jobs = data.get("jobPostings") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            raise BoardUnavailable("the Workday site answered in an unexpected shape")
        total = int(data.get("total") or 0) if page == 0 else total
        for j in jobs:
            path = str(j.get("externalPath") or "")
            if not path.startswith("/"):
                continue
            bullets = j.get("bulletFields") or []
            where = str(j.get("locationsText") or "")
            postings.append(
                BoardPosting(
                    id=str(bullets[0]) if bullets else path,
                    title=str(j.get("title") or ""),
                    # Every posting here has an India location; "2 Locations" doesn't say so.
                    location=where if "india" in where.casefold() else _join("India", where),
                    url=f"https://{host}/{site}{path}",
                    posted_at=_posted(j.get("postedOn"), now),
                )
            )
        if len(jobs) < WD_PAGE or len(postings) >= total:
            return postings
        await asyncio.sleep(WD_PAGE_PAUSE)
    return Partial(postings)  # more than WD_MAX_PAGES pages of India postings


READERS: dict[str, Reader] = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "smartrecruiters": smartrecruiters,
    "workday": workday,
}
