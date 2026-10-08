import httpx
import pytest
import respx

from jobradar.pipeline import links
from jobradar.pipeline.links import ShortLinkExpander, canonicalise, extract_urls, normalise


def test_extract_urls_from_text() -> None:
    text = (
        "🔥 Hiring! Apply: https://careers.acme.com/jobs/123?utm_source=tg). "
        "More (www.example.org/jobs), dup https://careers.acme.com/jobs/123?utm_source=tg."
    )
    assert extract_urls(text) == [
        "https://careers.acme.com/jobs/123?utm_source=tg",
        "https://www.example.org/jobs",
    ]
    assert extract_urls(None) == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "HTTPS://WWW.Acme.com/Jobs/42/?utm_source=x&b=2&a=1#apply",
            "https://acme.com/Jobs/42?a=1&b=2",
        ),
        ("https://acme.com/?fbclid=1&gclid=2&ref=tg&trk=3&si=4", "https://acme.com"),
        ("http://acme.com:80/x", "http://acme.com/x"),
        ("https://acme.com:8443/x", "https://acme.com:8443/x"),
        ("acme.com/careers", "https://acme.com/careers"),
        ("ftp://acme.com/x", None),
        ("https://localhost/x", None),
        ("https://[bad", None),
    ],
)
def test_normalise(raw: str, expected: str | None) -> None:
    assert normalise(raw) == expected


async def test_canonicalise_drops_non_job_hosts() -> None:
    assert await canonicalise("https://t.me/somechannel/12") is None
    assert await canonicalise("https://m.youtube.com/watch?v=1") is None
    assert await canonicalise("https://chat.whatsapp.com/abc") is None
    assert await canonicalise("https://jobs.lever.co/acme/1?lever-source=tg") == (
        "https://jobs.lever.co/acme/1?lever-source=tg"
    )


@pytest.fixture
def public_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    async def yes(host: str) -> bool:
        return host != "10.0.0.5"

    monkeypatch.setattr(links, "is_public_host", yes)


@respx.mock
async def test_expander_follows_redirect_chain(public_hosts: None) -> None:
    respx.head("https://bit.ly/abc").respond(301, headers={"Location": "https://lnkd.in/x"})
    respx.head("https://lnkd.in/x").respond(
        302, headers={"Location": "https://careers.acme.com/j/1?utm_medium=social"}
    )
    expand = ShortLinkExpander(httpx.AsyncClient())
    assert await canonicalise("https://bit.ly/abc", expand) == "https://careers.acme.com/j/1"
    assert await expand("https://bit.ly/abc")  # cached: no new request
    assert respx.calls.call_count == 2
    await expand.aclose()


@respx.mock
async def test_expander_falls_back_to_get_and_handles_errors(public_hosts: None) -> None:
    respx.head("https://t.ly/a").respond(405)
    respx.get("https://t.ly/a").respond(302, headers={"Location": "/final"})
    respx.head("https://t.ly/final").respond(200)
    respx.head("https://tinyurl.com/down").mock(side_effect=httpx.ConnectTimeout("slow"))
    expand = ShortLinkExpander(httpx.AsyncClient())
    assert await expand("https://t.ly/a") == "https://t.ly/final"
    assert await expand("https://tinyurl.com/down") == "https://tinyurl.com/down"
    assert await expand("https://acme.com/not-short") == "https://acme.com/not-short"
    await expand.aclose()


@respx.mock
async def test_expander_refuses_private_targets(public_hosts: None) -> None:
    respx.head("https://bit.ly/evil").respond(302, headers={"Location": "https://10.0.0.5/admin"})
    respx.head("https://10.0.0.5/admin").respond(200)
    expand = ShortLinkExpander(httpx.AsyncClient())
    # Redirect leaves the shortener, so expansion stops before touching the private host.
    assert await expand("https://bit.ly/evil") == "https://10.0.0.5/admin"
    assert not any(c.request.url.host == "10.0.0.5" for c in respx.calls)
    await expand.aclose()


@pytest.mark.parametrize(
    ("url", "rules", "denied"),
    [
        (
            "https://acciojob.com/full-stack-development-courses",
            ["acciojob.com/full-stack-development-courses"],
            True,
        ),
        (
            "https://placement.acciojob.com/job-drive-details?jobDriveId=1",
            ["acciojob.com/full-stack-development-courses"],
            False,
        ),
        ("https://ads.example.com/x", ["example.com"], True),
        ("https://example.org/x", ["example.com"], False),
        ("https://acme.com/Courses/sde", ["https://www.ACME.com/courses/"], True),
        ("https://t.me/channel", [], True),
    ],
)
def test_deny_rules(url: str, rules: list[str], denied: bool) -> None:
    assert links.is_denied(url, rules) is denied


@respx.mock
async def test_channel_shortener_expands_to_an_ad_that_is_denied(public_hosts: None) -> None:
    respx.head("https://go.acciojob.com/ad").respond(
        301,
        headers={"Location": "https://acciojob.com/full-stack-development-courses?utm_source=x"},
    )
    respx.head("https://go.mysite.in/j").respond(301, headers={"Location": "https://acme.com/j/9"})
    expand = ShortLinkExpander(httpx.AsyncClient(), extra_shorteners=["go.mysite.in"])
    deny = ["acciojob.com/full-stack-development-courses"]
    assert await canonicalise("https://go.acciojob.com/ad", expand, deny) is None
    assert await canonicalise("https://go.mysite.in/j", expand) == "https://acme.com/j/9"
    await expand.aclose()


@pytest.mark.parametrize(
    ("url", "denied"),
    [
        ("https://x.com/ajsinghrawat", True),
        ("https://topmate.io/sdejobsandinternships", True),
        ("https://play.google.com/store/apps/details?id=x", True),
        ("https://linkedin.com/in/someone", True),
        ("https://linkedin.com/company/acme", True),
        ("https://linkedin.com/jobs/view/4475551081", False),
        ("https://linkedin.com/posts/recruiter_hiring-123", False),
    ],
)
def test_builtin_non_job_links(url: str, denied: bool) -> None:
    assert links.is_denied(url) is denied
