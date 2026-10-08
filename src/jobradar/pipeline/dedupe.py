"""Job identity: canonical-URL hash, and a company|role|location fingerprint (used from v0.2)."""

from __future__ import annotations

import hashlib
import re

_NOISE_WORDS = frozenset(
    {"pvt", "private", "ltd", "limited", "llp", "inc", "llc", "corp", "co", "hiring",
     "is", "for", "the", "we", "are"}
)  # fmt: skip
_NON_WORD = re.compile(r"[^a-z0-9]+")


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()


def job_id_for_url(canonical_url: str) -> str:
    return sha1(canonical_url)


def norm(text: str | None) -> str:
    """Lower-case, drop punctuation and filler words like 'pvt', 'ltd', 'hiring'."""
    words = _NON_WORD.sub(" ", (text or "").lower()).split()
    return " ".join(w for w in words if w not in _NOISE_WORDS)


def fingerprint(company: str | None, role: str | None, location: str | None) -> str | None:
    """norm(company)|norm(role)|norm(location); None unless both company and role are known."""
    c, r = norm(company), norm(role)
    if not c or not r:
        return None
    return f"{c}|{r}|{norm(location)}"
