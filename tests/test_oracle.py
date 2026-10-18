import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from jobradar.config import BoardRef, BoardsConfig, board_from_link
from jobradar.pipeline import fetch as fetch_mod
from jobradar.pipeline import oracle
from jobradar.pipeline.fetch import Fetcher
from jobradar.pipeline.liveness import Liveness, LivenessChecker, ats_link
from jobradar.pipeline.structured import from_job_posting
from jobradar.sources.boards import readers
from jobradar.sources.boards.base import BoardGone, BoardUnavailable, Partial

HOST = "fa-etvl-saasfaprod1.fa.ocs.oraclecloud.com"
SITE = "CX_1"
BOARD = f"{HOST}/{SITE}"
LIST = f"https://{HOST}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
DETAIL = f"https://{HOST}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
INDIA = "300000000435151"


def req(n: int, title: str = "Java Developer", where: str = "Pune, Maharashtra, India",
        country: str = "IN", **kw: Any) -> dict[str, Any]:  # fmt: skip
    # The shape Zensar's site answered with on 2026-10-18.
    return {"Id": str(n), "Title": title, "PostedDate": "2026-10-17", "PrimaryLocation": where,
            "PrimaryLocationCountry": country, "WorkplaceTypeCode": None,
            "secondaryLocations": [], **kw}  # fmt: skip


def answer(jobs: list[dict[str, Any]], total: int | None = None,
           facet: list[dict[str, Any]] | None = None) -> dict[str, Any]:  # fmt: skip
    return {"items": [{"TotalJobsCount": len(jobs) if total is None else total,
                       "requisitionList": jobs, "locationsFacet": facet or []}]}  # fmt: skip


def finder(request: httpx.Request) -> str:
    return request.url.params["finder"]


@pytest.fixture(autouse=True)
def fast_and_fresh(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(readers, "OR_PAGE_PAUSE", 0)
    readers._OR_INDIA.clear()
    yield
    readers._OR_INDIA.clear()


# ---- links and config -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "link",
    [
        f"https://{HOST}/hcmUI/CandidateExperience/en/sites/{SITE}/jobs",
        f"https://{HOST}/hcmUI/CandidateExperience/en/sites/{SITE}/job/152460",
        f"{HOST}/hcmUI/CandidateExperience/en-GB/sites/{SITE}/requisitions",
    ],
)
def test_careers_links(link: str) -> None:
    assert board_from_link(link) == ("oracle", BOARD)


def test_config_takes_a_link_or_host_and_site() -> None:
    config = BoardsConfig.model_validate({"oracle": {
        "Zensar": BOARD,
        "Dell": "https://enterpriseplatform.dell.com/hcmUI/CandidateExperience/en/sites/CX_1001/jobs",
    }})  # fmt: skip
    assert [(b.key, b.label) for b in config.companies] == [
        (f"oracle:{BOARD}", "Oracle · Zensar"),
        ("oracle:enterpriseplatform.dell.com/CX_1001", "Oracle · Dell"),
    ]
    assert BoardRef(ats="oracle", company=BOARD).label == "Oracle · CX_1"
    with pytest.raises(ValueError):
        BoardRef(ats="oracle", company="no-site")


@pytest.mark.parametrize(
    ("url", "found"),
    [
        (f"https://{HOST}/hcmUI/CandidateExperience/en/sites/{SITE}/job/152460", True),
        (f"https://{HOST}/hcmUI/CandidateExperience/en/sites/{SITE}/job/152460/?utm=x", True),
        (f"https://{HOST}.evil.io/hcmUI/CandidateExperience/en/sites/{SITE}/job/1", False),
        (f"https://{HOST}/hcmUI/CandidateExperience/en/sites/{SITE}/job/abc", False),
        (f"https://{HOST}/hcmUI/CandidateExperience/en/sites/../job/1", False),
        ("https://enterpriseplatform.dell.com/hcmUI/CandidateExperience/en/sites/CX/job/1", False),
    ],
)
def test_job_links_only_on_oraclecloud(url: str, found: bool) -> None:
    assert (oracle.job_link(url) is not None) is found


def test_offset_sits_inside_the_finder() -> None:
    url = httpx.URL(oracle.list_url(HOST, SITE, 100, 200, "selectedLocationsFacet=7,"))
    assert "offset" not in url.params  # a top-level offset is ignored by Oracle
    assert url.params["finder"] == (
        "findReqs;siteNumber=CX_1,selectedLocationsFacet=7,limit=100,offset=200,"
        "sortBy=POSTING_DATES_DESC"
    )


# ---- the reader -----------------------------------------------------------------------------------


@respx.mock
async def test_reads_india_with_the_facet() -> None:
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        f = finder(request)
        calls.append(f)
        if "facetsList=LOCATIONS" in f:
            facet = [{"Name": "Maharashtra, India", "Id": 1}, {"Name": "India", "Id": int(INDIA)}]
            return httpx.Response(200, json=answer([req(1)], total=223, facet=facet))
        assert f"selectedLocationsFacet={INDIA}" in f
        offset = int(f.split("offset=")[1].split(",")[0])
        jobs = [req(n) for n in range(offset, min(offset + 100, 150))]
        return httpx.Response(200, json=answer(jobs, total=150))

    respx.get(LIST).mock(side_effect=respond)
    async with httpx.AsyncClient() as client:
        postings = await readers.oracle_board(client, BOARD)
        assert len(postings) == 150 and not isinstance(postings, Partial)
        first = postings[0]
        assert first.id == "0" and first.location == "Pune, Maharashtra, India"
        assert first.url == f"https://{HOST}/hcmUI/CandidateExperience/en/sites/{SITE}/job/0"
        assert first.posted_at is not None and first.posted_at.tzinfo is not None
        assert len(calls) == 3  # one facet lookup, two pages
        await readers.oracle_board(client, BOARD)
        assert len(calls) == 5  # the India id is remembered


@respx.mock
async def test_without_an_india_facet_reads_all_and_keeps_india() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if "facetsList=LOCATIONS" in finder(request):
            return httpx.Response(200, json=answer([], total=3, facet=[{"Name": "Texas", "Id": 9}]))
        assert "selectedLocationsFacet" not in finder(request)
        return httpx.Response(200, json=answer([
            req(1),
            req(2, where="Austin, TX", country="US"),
            req(3, where="Austin, TX", country="US",
                secondaryLocations=[{"Name": "Bangalore", "CountryCode": "IN"}]),
            req(4, where="Bangalore, India", WorkplaceTypeCode="ORA_REMOTE"),
        ]))  # fmt: skip

    dell = LIST.replace(HOST, "enterpriseplatform.dell.com")
    respx.get(dell).mock(side_effect=respond)
    async with httpx.AsyncClient() as client:
        postings = await readers.oracle_board(client, "enterpriseplatform.dell.com/CX_1001")
    assert [p.id for p in postings] == ["1", "3", "4"]
    assert postings[1].location == "India · Austin, TX"
    assert postings[2].location == "Bangalore, India · Remote"


@respx.mock
async def test_page_cap_and_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(readers, "OR_MAX_PAGES", 1)
    readers._OR_INDIA[BOARD] = INDIA
    route = respx.get(LIST)
    route.respond(json=answer([req(n) for n in range(100)], total=400))
    async with httpx.AsyncClient() as client:
        assert isinstance(await readers.oracle_board(client, BOARD), Partial)
        route.respond(json={"items": []})
        with pytest.raises(BoardUnavailable, match="unexpected shape"):
            await readers.oracle_board(client, BOARD)
        route.respond(429)
        with pytest.raises(BoardUnavailable):
            await readers.oracle_board(client, BOARD)
        assert BOARD not in readers._OR_INDIA  # looked up again next time
        route.respond(404)
        with pytest.raises(BoardGone):
            await readers.oracle_board(client, BOARD)


# ---- job pages and the still-open check -------------------------------------------------------

JOB = f"https://{HOST}/hcmUI/CandidateExperience/en/sites/{SITE}/job/152460"
ITEM = {
    "Title": "Java Developer", "PrimaryLocation": "Pune, Maharashtra, India",
    "ExternalPostedStartDate": "2026-10-17T07:28:44+00:00", "JobSchedule": "Full time",
    "ExternalDescriptionStr": "<p>Build services in Java.</p><p>Minimum 2 years of experience.</p>",
    "ExternalQualificationsStr": "<br>",
}  # fmt: skip


@respx.mock
async def test_job_page_reads_the_detail_address(monkeypatch: pytest.MonkeyPatch) -> None:
    async def public(host: str) -> bool:
        return True

    monkeypatch.setattr(fetch_mod, "is_public_host", public)
    respx.get(f"https://{HOST}/robots.txt").respond(404)
    route = respx.get(DETAIL).respond(json={"items": [ITEM]})
    fetcher = Fetcher(httpx.AsyncClient(), per_site_interval=0, render_js=False)
    page = await fetcher.fetch(JOB)
    assert route.calls.last.request.url.params["finder"] == 'ById;Id="152460",siteNumber=CX_1'
    assert page.title == "Java Developer" and page.note is None
    assert page.text and "Minimum 2 years of experience." in page.text
    fields = from_job_posting(page.structured or {})
    assert fields["experience_min"] == 2 and str(fields["published_on"]) == "2026-10-17"
    assert fields["employment_type"] == "full_time"

    route.respond(json={"items": []})
    gone = await fetcher.fetch(JOB)
    assert gone.text is None and gone.note == "no longer posted"


def test_description_falls_back_to_corporate_fields() -> None:
    item = {"ExternalDescriptionStr": "<br>", "CorporateDescriptionStr": "<p>Campus role.</p>"}
    assert oracle.description(item) == "Campus role."


@respx.mock
@pytest.mark.parametrize(
    ("response", "state"),
    [
        (httpx.Response(200, json={"items": [ITEM]}), Liveness.OPEN),
        (httpx.Response(200, json={"items": []}), Liveness.CLOSED),
        (httpx.Response(200, json={"oops": 1}), Liveness.UNKNOWN),
        (httpx.Response(200, text="<html>"), Liveness.UNKNOWN),
        (httpx.Response(429), Liveness.UNKNOWN),
        (httpx.Response(404), Liveness.UNKNOWN),
    ],
)
async def test_still_open(response: httpx.Response, state: Liveness) -> None:
    respx.get(DETAIL).mock(return_value=response)
    link = ats_link(JOB)
    assert link is not None and link.ats == "oracle"
    assert await LivenessChecker(httpx.AsyncClient()).check(JOB) == state


def test_as_job_posting_shape() -> None:
    posting = oracle.as_job_posting({**ITEM, "WorkplaceTypeCode": "ORA_REMOTE"})
    assert posting["jobLocationType"] == "TELECOMMUTE"
    assert json.dumps(posting)  # plain data, stored as the page's JobPosting
