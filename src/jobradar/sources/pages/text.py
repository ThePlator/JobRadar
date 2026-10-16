"""Compare two reads of a watched page and keep only what's new.

Pages change all the time in ways that aren't news: "Posted 3 days ago", a clock, a visitor
counter, a reordered list. Lines are compared by a key with those parts removed, and lines
that are nothing but a date or a timestamp are ignored. Dates inside a real sentence are kept,
so "drive on 12 Nov" is a new line even if "drive on 5 Oct" was there before.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

MAX_TEXT = 50_000  # characters of cleaned text kept per page
MIN_ADDED = 80  # fewer added characters than this is not an announcement
MAX_LINKS = 10  # links passed on from one change
REDESIGN_SHARE = 0.6  # this much of the page new at once: a redesign, not news

_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_RELATIVE = re.compile(
    r"\b(?:posted|updated|published|added)?\s*(?:\d+|a|an|few)\s+"
    r"(?:second|sec|minute|min|hour|hr|day|week|month|year)s?\s+ago\b"
    r"|\b(?:posted|updated|published|added)\s+(?:today|yesterday|just now)\b",
    re.IGNORECASE,
)
_CLOCK = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm)?\b", re.IGNORECASE)
_COUNTER = re.compile(
    r"\b\d[\d,]*\+?\s+(?:jobs?|openings?|results?|positions?|vacanc(?:y|ies)|views?"
    r"|applicants?|applications?|visitors?)\b",
    re.IGNORECASE,
)
_DATE = re.compile(
    rf"\b\d{{1,4}}[-/.]\d{{1,2}}[-/.]\d{{1,4}}\b"
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH},?(?:\s+\d{{4}})?"
    rf"|\b{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?,?(?:\s+\d{{4}})?"
    rf"|\b{_MONTH}\s+\d{{4}}\b",
    re.IGNORECASE,
)
_META = re.compile(
    r"^\W*(?:last\s+)?(?:updated|modified|posted|published|date|copyright|©|\(c\))\b",
    re.IGNORECASE,
)
_LINK = re.compile(r"\[([^\]]*)\]\((\S+?)\)")
_BARE_URL = re.compile(r"(?<![(\w])https?://[^\s)\]]+")


def clean_lines(text: str) -> list[str]:
    """Non-empty lines with spaces collapsed, capped at MAX_TEXT characters in total."""
    lines, size = [], 0
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        size += len(line) + 1
        if size > MAX_TEXT:
            break
        lines.append(line)
    return lines


def line_key(line: str) -> str:
    """What a line says once timestamps and counters are removed; "" for a line that is
    only a date, a timestamp or page furniture."""
    if _META.match(line):
        return ""
    key = _COUNTER.sub(" ", _CLOCK.sub(" ", _RELATIVE.sub(" ", _LINK.sub(r"\1", line))))
    if len(re.sub(r"[^a-z]", "", _DATE.sub(" ", key).casefold())) < 12:
        return ""
    return " ".join(re.sub(r"[^\w\s]", " ", key.casefold()).split())


def added_lines(old: list[str], new: list[str]) -> list[str]:
    """Lines of `new` that say something no line of `old` said. A moved line is not new."""
    before = {line_key(line) for line in old}
    added, seen = [], set()
    for line in new:
        key = line_key(line)
        if key and key not in before and key not in seen:
            seen.add(key)
            added.append(line)
    return added


def is_redesign(new: list[str], added: list[str]) -> bool:
    """Most of the page changed at once: a new layout, not an announcement."""
    total = sum(len(line) for line in new)
    return total > 0 and sum(len(line) for line in added) > REDESIGN_SHARE * total


def links_in(lines: list[str], page_url: str) -> list[str]:
    """Web links in these lines (Markdown or bare), made absolute; the page itself excluded."""
    found: list[str] = []
    for line in lines:
        hrefs = [m.group(2) for m in _LINK.finditer(line)] + _BARE_URL.findall(line)
        for href in hrefs:
            url = urljoin(page_url, href.strip("<>"))
            if urlsplit(url).scheme not in ("http", "https"):
                continue
            if url.split("#")[0].rstrip("/") == page_url.split("#")[0].rstrip("/"):
                continue
            if url not in found:
                found.append(url)
    return found[:MAX_LINKS]


def readable(lines: list[str], page_url: str) -> str:
    """The added lines as plain text: "[Register](/nqt)" becomes "Register (https://…/nqt)"."""

    def link(m: re.Match[str]) -> str:
        url = urljoin(page_url, m.group(2).strip("<>"))
        return f"{m.group(1)} ({url})" if m.group(1) else url

    return "\n".join(_LINK.sub(link, line).lstrip("#*- ").strip() for line in lines)
