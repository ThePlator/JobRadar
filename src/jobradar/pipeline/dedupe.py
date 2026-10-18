"""Job identity: a canonical-URL hash, and a company|role key that joins one role posted for
several cities (or on several boards) into one job."""

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


def role_key(company: str | None, role: str | None) -> str | None:
    """norm(company)|norm(role), the location left out: one role posted for ten cities is one
    job (freehire's jobdedup does the same). None unless both company and role are known."""
    c, r = norm(company), norm(role)
    if not c or not r:
        return None
    return f"{c}|{r}"
