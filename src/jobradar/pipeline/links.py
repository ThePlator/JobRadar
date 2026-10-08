"""Extract, expand and canonicalise URLs so the same job link always maps to the same string."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from jobradar.net import is_public_host

log = logging.getLogger(__name__)

# Hosts that only redirect somewhere else; expanded before canonicalising.
SHORTENERS = frozenset(
    {
        "bit.ly", "lnkd.in", "t.ly", "tinyurl.com", "goo.gl", "forms.gle", "rb.gy", "cutt.ly",
        "shorturl.at", "is.gd", "ow.ly", "buff.ly", "rebrand.ly", "t.co", "surl.li", "tiny.cc",
        "shorturl.asia", "bitly.ws",
    }
)  # fmt: skip

# Links that are never a job posting (channels, social posts, videos).
DENY_HOSTS = frozenset(
    {
        "t.me", "telegram.me", "telegram.dog", "youtube.com", "youtu.be", "instagram.com",
        "whatsapp.com", "wa.me", "chat.whatsapp.com",
    }
)  # fmt: skip

_TRACKING_EXACT = frozenset(
    {"ref", "src", "source", "fbclid", "gclid", "trk", "si", "igshid", "mc_cid", "mc_eid",
     "trackingid", "refid", "lipi", "ref_src", "_hsenc", "_hsmi"}
)  # fmt: skip

_URL_RE = re.compile(r"""(?:https?://|www\.)[^\s<>"'`{}|\\^]+""", re.IGNORECASE)
_TRAILING = ".,;:!?'\"*"

Expander = Callable[[str], Awaitable[str]]


def extract_urls(text: str | None) -> list[str]:
    """URLs written in free text, in order, without duplicates or trailing punctuation."""
    found: list[str] = []
    for match in _URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(_TRAILING)
        while url.endswith(")") and url.count(")") > url.count("("):
            url = url[:-1].rstrip(_TRAILING)
        if url.lower().startswith("www."):
            url = "https://" + url
        if url not in found:
            found.append(url)
    return found


def _host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


def _matches(host: str, domains: frozenset[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def normalise(url: str) -> str | None:
    """Canonical form without network access; None if not a usable http(s) URL."""
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().removeprefix("www.")
    if scheme not in ("http", "https") or "." not in host:
        return None
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not (k.lower().startswith("utm_") or k.lower() in _TRACKING_EXACT)
    )
    path = parts.path.rstrip("/")
    return urlunsplit((scheme, host, path, urlencode(query), ""))


class ShortLinkExpander:
    """Follows redirects for known shorteners (max 5 hops, 5 s each), caching results.

    Every hop must resolve to a public IP. On any error the link is returned unexpanded,
    so a flaky shortener never loses a job; it may only dedupe less well.
    """

    def __init__(self, client: httpx.AsyncClient | None = None, max_hops: int = 5) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=5.0, follow_redirects=False, headers={"User-Agent": "Mozilla/5.0 JobRadar"}
        )
        self._max_hops = max_hops
        self._cache: dict[str, str] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __call__(self, url: str) -> str:
        if not _matches(_host(url), SHORTENERS):
            return url
        if url not in self._cache:
            self._cache[url] = await self._follow(url)
        return self._cache[url]

    async def _follow(self, url: str) -> str:
        current = url
        try:
            for _ in range(self._max_hops):
                if not await is_public_host(urlsplit(current).hostname or ""):
                    log.warning("short link points at a non-public host", extra={"url": url})
                    return url
                resp = await self._client.head(current)
                if resp.status_code in (403, 405, 400, 501):  # some shorteners refuse HEAD
                    async with self._client.stream("GET", current) as streamed:
                        resp = streamed
                location: str | None = resp.headers.get("location")
                if not resp.is_redirect or not location:
                    return current
                current = urljoin(current, location)
                if not _matches(_host(current), SHORTENERS):
                    return current
        except httpx.HTTPError as e:
            log.info("could not expand short link", extra={"url": url, "error": str(e)})
            return url
        return current


async def canonicalise(url: str, expand: Expander | None = None) -> str | None:
    """Expand (if a shortener), normalise, and drop non-job hosts. None means "not a job link"."""
    first = normalise(url)
    if first is None:
        return None
    expanded = normalise(await expand(first)) if expand else first
    if expanded is None or _matches(_host(expanded), DENY_HOSTS):
        return None
    return expanded
