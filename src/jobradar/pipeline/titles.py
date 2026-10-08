"""A readable title for a job post before the LLM extractor (v0.2) knows the real role."""

from __future__ import annotations

import re

from jobradar.pipeline.dedupe import norm
from jobradar.pipeline.links import extract_urls

TITLE_LIMIT = 120
# Words that say nothing about which job it is; removed before comparing titles.
_FILLER = frozenset(
    {"apply", "now", "link", "urgent", "opening", "openings", "job", "jobs", "vacancy",
     "freshers", "fresher", "off", "campus", "drive", "batch", "new", "role", "position"}
)  # fmt: skip
MIN_KEY_WORDS = 3  # shorter titles ("Apply now") are too generic to compare
_DECORATION = " -\u2013\u2014:|*•🔥📢✅👉📌"


def title_from_post(text: str | None) -> str | None:
    """First line of the post with links and decoration removed, if it says anything."""
    for line in (text or "").splitlines():
        for url in extract_urls(line):
            line = line.replace(url, "")
        line = re.sub(r"\s+", " ", line).strip(_DECORATION)
        if len(line) >= 4:
            return line[:TITLE_LIMIT]
    return None


def title_key(title: str) -> str | None:
    """Comparable form of a title, or None when it is too generic to compare."""
    words = [w for w in norm(title).split() if w not in _FILLER]
    return " ".join(words) if len(words) >= MIN_KEY_WORDS else None
