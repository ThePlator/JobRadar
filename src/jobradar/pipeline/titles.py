"""A readable title for a job post before the LLM extractor (v0.2) knows the real role."""

from __future__ import annotations

import re

from jobradar.pipeline.links import extract_urls

TITLE_LIMIT = 120
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
