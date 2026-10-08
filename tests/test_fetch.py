import httpx
import pytest
import respx

from jobradar.pipeline import fetch as fetch_mod
from jobradar.pipeline.fetch import Fetcher
from jobradar.queue.tasks import RetryableError

ARTICLE = (
    "<html><head><title>SDE Intern - Acme Careers</title></head><body><main><h1>SDE Intern</h1>"
    + (
        "<p>Acme is hiring software engineering interns in Bengaluru. "
        "Required skills: Python, SQL. "
        "Eligible batches: 2025 and 2026. Stipend 30,000 per month. Apply before 30 October.</p>"
        * 6
    )
    + "</main></body></html>"
)


@pytest.fixture(autouse=True)
def public_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    async def check(host: str) -> bool:
        return not host.startswith("10.")

    monkeypatch.setattr(fetch_mod, "is_public_host", check)


def fetcher() -> Fetcher:
    return Fetcher(httpx.AsyncClient(), per_site_interval=0, render_js=False)


@respx.mock
async def test_reads_main_text_and_title() -> None:
    respx.get("https://acme.com/robots.txt").respond(404)
    respx.get("https://acme.com/j/1").respond(301, headers={"Location": "/careers/1"})
    respx.get("https://acme.com/careers/1").respond(200, html=ARTICLE)
    page = await fetcher().fetch("https://acme.com/j/1")
    assert page.final_url == "https://acme.com/careers/1"
    assert page.title == "SDE Intern - Acme Careers"
    assert page.text and "Python, SQL" in page.text


@respx.mock
@pytest.mark.parametrize(
    ("status", "headers", "body", "note"),
    [
        (404, {}, "", "HTTP 404"),
        (200, {"content-type": "application/pdf"}, "%PDF", "not an HTML page"),
        (200, {"content-type": "text/html"}, "<input type='password' name='p'>", "login required"),
    ],
)
async def test_unusable_pages(status: int, headers: dict[str, str], body: str, note: str) -> None:
    respx.get("https://acme.com/robots.txt").respond(404)
    respx.get("https://acme.com/j").respond(status, headers=headers, text=body)
    page = await fetcher().fetch("https://acme.com/j")
    assert page.text is None and page.note == note


@respx.mock
async def test_login_redirect_robots_and_private_targets() -> None:
    respx.get("https://linkedin.com/robots.txt").respond(404)
    respx.get("https://linkedin.com/jobs/view/1").respond(
        302, headers={"Location": "/authwall?x=1"}
    )
    respx.get("https://linkedin.com/authwall?x=1").respond(200, html="<p>Sign in</p>")
    assert (await fetcher().fetch("https://linkedin.com/jobs/view/1")).note == "login required"

    respx.get("https://blocked.com/robots.txt").respond(200, text="User-agent: *\nDisallow: /jobs")
    assert (await fetcher().fetch("https://blocked.com/jobs/9")).note == "blocked by robots.txt"

    respx.get("https://evil.com/robots.txt").respond(404)
    respx.get("https://evil.com/j").respond(302, headers={"Location": "http://10.0.0.1/admin"})
    page = await fetcher().fetch("https://evil.com/j")
    assert page.note == "not a public address"
    assert not any(c.request.url.host == "10.0.0.1" for c in respx.calls)


@respx.mock
async def test_temporary_failures_are_retryable() -> None:
    respx.get("https://acme.com/robots.txt").respond(404)
    respx.get("https://acme.com/busy").respond(503)
    respx.get("https://acme.com/slow").mock(side_effect=httpx.ReadTimeout("slow"))
    respx.get("https://acme.com/limit").respond(429, headers={"Retry-After": "30"})
    f = fetcher()
    with pytest.raises(RetryableError):
        await f.fetch("https://acme.com/busy")
    with pytest.raises(RetryableError):
        await f.fetch("https://acme.com/slow")
    with pytest.raises(RetryableError) as e:
        await f.fetch("https://acme.com/limit")
    assert e.value.retry_after == 30
