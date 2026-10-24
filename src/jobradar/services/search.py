"""All jobs' full-text search: what goes into the index, and turning what you type into a
query that can't break it.

The index (an SQLite FTS5 table) holds each job's role, company, places, skills and text (its
page and the posts about it). It's brought up to date just before a search, from whatever
changed since the last one, so it never needs a rebuild.
"""

from __future__ import annotations

import re
from datetime import datetime

from markupsafe import Markup, escape

from jobradar.clock import to_iso
from jobradar.db.models import Job
from jobradar.db.repo import Repo
from jobradar.sinks.email_template import posting_of

INDEXED = "search_indexed_at"  # setting: when the index was last brought up to date
POSTS = 3  # posts per job that go into the index
_WORD = re.compile(r"[\w][\w.+#-]*", re.UNICODE)


def row(repo: Repo, job: Job) -> tuple[str, str, str, str, str, str]:
    posting = posting_of(job)
    places = list(posting.locations) if posting else []
    if posting and posting.work_mode != "unknown":
        places.append(posting.work_mode)
    skills = (posting.skills_required + posting.skills_preferred) if posting else []
    page = repo.get_page(job.id)
    posts = [t for t, _ in repo.job_post_contexts(job.id, limit=POSTS) if t]
    parts = [posting.summary if posting else "", page.text if page and page.text else "", *posts]
    body = "\n".join(p for p in parts if p)
    return (job.id, job.role or "", job.company or "", " ".join(places), ", ".join(skills),
            body[:20000])  # fmt: skip


def refresh(repo: Repo, now: datetime) -> int:
    """Bring the index up to date; returns how many jobs were (re)indexed."""
    stamp = to_iso(now)  # taken first: anything changing while this runs is caught next time
    ids = repo.jobs_to_index(repo.get_setting(INDEXED))
    rows = [row(repo, job) for i in ids if (job := repo.get_job(i))]
    repo.index_jobs(rows)
    repo.set_setting(INDEXED, stamp)
    return len(rows)


def _terms(text: str) -> list[str]:
    return [f'"{w.replace(chr(34), "")}"*' for w in _WORD.findall(text)[:12]]


def match(words: str, place: str = "", remote: bool = False) -> str | None:
    """An FTS5 query: every word you typed (as a prefix, so "kube" finds Kubernetes), in any
    field; a place only in the places field; remote or hybrid as the work mode."""
    parts = _terms(words)
    if place_terms := _terms(place):
        parts.append("places : (" + " ".join(place_terms) + ")")
    if remote:
        parts.append('places : ("remote" OR "hybrid")')
    return " AND ".join(parts) or None


def snippet(raw: str | None) -> Markup:
    """The index's snippet (matches between \\x02 and \\x03) as HTML with <mark>."""
    if not raw:
        return Markup("")
    safe = str(escape(" ".join(raw.split())))
    # Escaped above; only our own two markers become tags.
    return Markup(safe.replace("\x02", "<mark>").replace("\x03", "</mark>"))  # noqa: S704
