"""Spot channel ads: one link attached to posts about several different jobs.

A channel's WhatsApp group, app, course or YouTube link rides along next to a *different*
job link in every post. A real job that gets reposted sits next to the *same* links (the
channel's promos) each time. So each post is identified by the other job links it carries,
falling back to its title words when it has none, and a link whose posts add up to three or
more different identities is treated as an ad.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from jobradar.pipeline.dedupe import norm
from jobradar.pipeline.titles import title_from_post, title_key

MIN_DIFFERENT_JOBS = 3
SAME_JOB_OVERLAP = 0.6  # share of links / words two posts need in common to be the same job
ROUNDUP_LINKS = 5  # a post with this many other job links is a digest ("Today's Job Updates")

PROMO_REASON = "promo link: attached to posts about several different jobs"


@dataclass(frozen=True)
class PostContext:
    """One post that carried the link under test."""

    text: str | None
    other_jobs: frozenset[str]  # ids of the other job links in the same post


def _overlap(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a | b else 1.0


def _identity(post: PostContext, constant: frozenset[str]) -> frozenset[str] | None:
    others = post.other_jobs - constant
    if others:
        return frozenset("link:" + j for j in others)
    title = title_from_post(post.text)
    key = title and (title_key(title) or norm(title))
    return frozenset(key.split()) if key else None


def different_jobs(posts: list[PostContext]) -> int:
    """How many distinct jobs the posts carrying one link are about.

    Links that travel with this one in more than half of its posts (a channel's other
    promos) say nothing about which job a post is about, so they are ignored.
    """
    # Digests list many jobs at once; every job in them would look like an ad.
    posts = [p for p in posts if len(p.other_jobs) < ROUNDUP_LINKS]
    if not posts:
        return 0
    counts: Counter[str] = Counter(j for post in posts for j in post.other_jobs)
    constant = frozenset(j for j, n in counts.items() if n / len(posts) > 0.5)
    seen: list[frozenset[str]] = []
    for post in posts:
        ident = _identity(post, constant)
        if ident is None:
            continue
        if not any(_overlap(ident, other) >= SAME_JOB_OVERLAP for other in seen):
            seen.append(ident)
    return len(seen)


def looks_like_promo(posts: list[PostContext]) -> bool:
    return different_jobs(posts) >= MIN_DIFFERENT_JOBS
