"""Is this posting still open? Asked of the hiring system itself, before a job is matched.

Telegram channels repost jobs for days, so a link may point at a posting that has already
closed. Greenhouse, Lever, SmartRecruiters and Workday answer per job (200 = open, 404 =
gone); Oracle answers per job too (an empty answer = no longer posted); Ashby has one list
per company, so the posting must still be on it.

Only a definite answer hides a job: a 404/410 for that posting, or its absence from an Ashby
list that was read fine. Timeouts, 429/5xx, redirects and every other site are "unknown"
and the job carries on as before.

Requests only ever go to the fixed API hosts below (for Workday, the company's own
`*.myworkdayjobs.com` host, matched by a strict pattern). The board name and posting id are
taken from the link's path with a strict pattern, so a crafted link can't pick the host.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qs, urlsplit

import httpx

from jobradar.pipeline import oracle

log = logging.getLogger(__name__)

USER_AGENT = "JobRadar/0.2 (+https://github.com/ThePlator/JobRadar)"
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_UUID = re.compile(r"^[0-9a-fA-F-]{36}$")
_DIGITS = re.compile(r"^\d{1,20}$")
_WORKDAY_HOST = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}\.wd\d{1,3}\.myworkdayjobs\.com$")
_LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
_SEGMENT = re.compile(r"^[A-Za-z0-9._~%-]{1,200}$")


class Liveness(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AtsLink:
    """A link to one posting on a hiring system: which system, whose board, which posting."""

    ats: str  # greenhouse | lever | ashby | smartrecruiters | workday | oracle
    board: str
    posting: str
    eu: bool = False  # Lever's EU instance


def ats_link(url: str | None) -> AtsLink | None:
    """Recognise a posting link by its host; None for any other site."""
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower().removeprefix("www.")
    path = [p for p in parts.path.split("/") if p]
    query = parse_qs(parts.query)
    found: AtsLink | None = None
    if _WORKDAY_HOST.match(host):
        return _workday_link(host, path)
    if (job := oracle.job_link(url)) is not None:
        return AtsLink("oracle", f"{job.host}/{job.site}", job.job_id)
    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io",
                "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io"):  # fmt: skip
        if len(path) >= 3 and path[1] == "jobs":
            found = AtsLink("greenhouse", path[0], path[2])
        elif path[:2] == ["embed", "job_app"] and query.get("for") and query.get("token"):
            found = AtsLink("greenhouse", query["for"][0], query["token"][0])
    elif host in ("jobs.lever.co", "jobs.eu.lever.co") and len(path) >= 2:
        found = AtsLink("lever", path[0], path[1], eu=host == "jobs.eu.lever.co")
    elif host == "jobs.ashbyhq.com" and len(path) >= 2:
        found = AtsLink("ashby", path[0], path[1])
    elif host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com") and len(path) >= 2:
        found = AtsLink("smartrecruiters", path[0], path[1].split("-", 1)[0])
    if found is None or not _NAME.match(found.board):
        return None
    pattern = _DIGITS if found.ats in ("greenhouse", "smartrecruiters") else _UUID
    return found if pattern.match(found.posting) else None


def _workday_link(host: str, path: list[str]) -> AtsLink | None:
    """company.wd5.myworkdayjobs.com/[en-US/]Site/job/<place>/<title>_<req id>"""
    if path and _LOCALE.match(path[0]):
        path = path[1:]
    if len(path) < 3 or path[1] != "job" or not _NAME.match(path[0]):
        return None
    rest = path[1:]
    if any(
        p in (".", "..") or not _SEGMENT.match(p) or "%2f" in p.lower() or "%2e" in p.lower()
        for p in rest
    ):
        return None
    return AtsLink("workday", f"{host}/{path[0]}", "/".join(rest))


def _api_url(link: AtsLink) -> str:
    match link.ats:
        case "oracle":
            host, site = link.board.split("/", 1)
            return oracle.detail_url(host, site, link.posting)
        case "workday":
            host, site = link.board.split("/", 1)
            return f"https://{host}/wday/cxs/{host.split('.')[0]}/{site}/{link.posting}"
        case "greenhouse":
            return f"https://boards-api.greenhouse.io/v1/boards/{link.board}/jobs/{link.posting}"
        case "lever":
            host = "api.eu.lever.co" if link.eu else "api.lever.co"
            return f"https://{host}/v0/postings/{link.board}/{link.posting}"
        case "ashby":
            return f"https://api.ashbyhq.com/posting-api/job-board/{link.board}"
        case _:
            return (
                f"https://api.smartrecruiters.com/v1/companies/{link.board}/postings/{link.posting}"
            )


class LivenessChecker:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        # Redirects are refused: a posting that moved is "unknown", and the request never
        # leaves the fixed hosts.
        self.client = client or httpx.AsyncClient(
            timeout=15, follow_redirects=False, headers={"User-Agent": USER_AGENT}
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    async def check(self, url: str | None) -> Liveness:
        link = ats_link(url)
        if link is None:
            return Liveness.UNKNOWN
        try:
            resp = await self.client.get(_api_url(link))
        except httpx.HTTPError as e:
            log.info("still-open check failed", extra={"ats": link.ats, "error": type(e).__name__})
            return Liveness.UNKNOWN
        if link.ats == "oracle":
            if resp.status_code != 200:
                return Liveness.UNKNOWN
            try:
                body = resp.json()
            except ValueError:
                return Liveness.UNKNOWN
            if not isinstance(body, dict) or not isinstance(body.get("items"), list):
                return Liveness.UNKNOWN
            return Liveness.OPEN if oracle.detail_item(body) else Liveness.CLOSED
        if link.ats != "ashby":
            if resp.status_code in (404, 410):
                return Liveness.CLOSED
            return Liveness.OPEN if resp.status_code == 200 else Liveness.UNKNOWN
        # Ashby: the company's whole list; a missing board says nothing about this posting.
        if resp.status_code != 200:
            return Liveness.UNKNOWN
        try:
            jobs = resp.json().get("jobs")
        except (ValueError, AttributeError):
            return Liveness.UNKNOWN
        if not isinstance(jobs, list) or not jobs:
            return Liveness.UNKNOWN  # an empty list is more likely a glitch than "all closed"
        listed = {str(j.get("id", "")).lower() for j in jobs if j.get("isListed", True)}
        return Liveness.OPEN if link.posting.lower() in listed else Liveness.CLOSED
