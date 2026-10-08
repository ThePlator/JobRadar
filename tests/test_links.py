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
