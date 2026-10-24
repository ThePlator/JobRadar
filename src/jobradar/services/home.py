"""The sidebar and Today's header: counts, Blip's state and lines, and the two charts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from jobradar.clock import to_iso
from jobradar.db.models import JobStatus
from jobradar.services.cards import SHOW_JOBS, TODAY_DAYS, Card, ago, as_utc
from jobradar.services.context import Ctx
from jobradar.sinks.email_template import Reason

# ---- Blip ------------------------------------------------------------------------------------

AWAKE_MINUTES = 20  # read something this recently: JobRadar is running


@dataclass(frozen=True)
class Overview:
    """The numbers in the sidebar and on Today, and what Blip is up to."""

    today: int  # good matches you haven't acted on
    active: int  # on the Applications board, still open
    jobs: int  # All jobs (not hidden)
    applied: int
    interviews: int
    closing: int  # jobs you may still apply to, closing in the next 2 days
    awake: bool
    last_read: str  # "3 min ago", or ""
    name: str  # your first name, for the greeting


def overview(ctx: Ctx, name: str) -> Overview:
    """The sidebar's counts and Blip's state; `name` is the user's name, for the greeting."""
    repo, min_score, now, tz = ctx.repo, ctx.good, ctx.now, ctx.tz
    counts = repo.status_counts()

    def n(*statuses: JobStatus) -> int:
        return sum(counts.get(st.value, 0) for st in statuses)

    since = to_iso(now - timedelta(days=TODAY_DAYS))
    day = now.astimezone(tz).date()
    closing = repo.jobs_closing(day.isoformat(), (day + timedelta(days=2)).isoformat())
    last = repo.last_activity()
    awake = bool(last) and (now - as_utc(last or "")) < timedelta(minutes=AWAKE_MINUTES)
    return Overview(
        today=len(repo.good_new_jobs(min_score, since)),
        active=n(
            JobStatus.SHORTLISTED, JobStatus.RESUME_READY, JobStatus.APPLIED, JobStatus.INTERVIEW
        ),
        jobs=n(*SHOW_JOBS),
        applied=n(JobStatus.APPLIED, JobStatus.INTERVIEW, JobStatus.OFFER, JobStatus.REJECTED),
        interviews=n(JobStatus.INTERVIEW, JobStatus.OFFER),
        closing=len(closing),
        awake=awake,
        last_read=ago(last, now),
        name=name.split()[0] if name.strip() and name != "Your Name" else "",
    )


def greeting(now: datetime, tz: ZoneInfo, name: str) -> str:
    hour = now.astimezone(tz).hour
    part = "morning" if 5 <= hour < 12 else "afternoon" if hour < 17 else "evening"
    return f"Good {part}, {name}." if name else f"Good {part}."


def blip_says(cards: list[Card], min_score: int) -> str:
    """Blip's speech bubble on Today."""
    if not cards:
        return f"Nothing new scored {min_score}+ in the last {TODAY_DAYS} days. I'll keep looking."
    best = cards[0]
    at = f" at {best.company}" if best.company else ""
    many = f"{len(cards)} good matches" if len(cards) > 1 else "One good match"
    return f"{many} waiting. The best is {best.role}{at}, a {best.score}."


def take(r: Reason) -> str:
    """'Strong fit. You're eligible, Pune works, and you have Python, SQL. Missing: Kafka.'"""
    bits: list[str] = []
    if r.eligible is True:
        bits.append("you're eligible")
    elif r.eligible is False and r.eligibility:
        bits.append(r.eligibility[0].lower() + r.eligibility[1:])
    elif r.eligibility:
        bits.append("eligibility is unclear")
    if r.place and r.place_ok:
        bits.append(f"{r.place} works")
    elif r.place_ok is False:
        bits.append("the location doesn't fit")
    if r.have:
        bits.append("you have " + ", ".join(r.have))
    text = (r.fit + ". ") if r.fit else ""
    if bits:
        joined = bits[0] if len(bits) == 1 else ", ".join(bits[:-1]) + " and " + bits[-1]
        text += joined[0].upper() + joined[1:] + "."
    if r.missing:
        text += " Missing: " + ", ".join(r.missing) + "."
    if r.unsure:
        text += " I'm not sure about this score."
    return text.strip() or "I couldn't read enough of this job to say."


# ---- charts ----------------------------------------------------------------------------------

PULSE_DAYS = 14


@dataclass(frozen=True)
class Day:
    day: str  # "19"
    weekday: str  # "Mon"
    found: int
    good: int
    today: bool


@dataclass(frozen=True)
class Pulse:
    days: list[Day]
    found: int
    good: int
    peak: int  # the tallest bar, for the scale


def pulse(ctx: Ctx) -> Pulse:
    """Jobs first seen each day for the last two weeks, and how many were good matches."""
    repo, min_score, now, tz = ctx.repo, ctx.good, ctx.now, ctx.tz
    today = now.astimezone(tz).date()
    first = today - timedelta(days=PULSE_DAYS - 1)
    start = datetime.combine(first, time(0), tz)
    found = dict.fromkeys(range(PULSE_DAYS), 0)
    good = dict.fromkeys(range(PULSE_DAYS), 0)
    for job in repo.jobs_created_since(to_iso(start)):
        if job.status == JobStatus.DISCARDED.value:
            continue
        i = (as_utc(job.created_at).astimezone(tz).date() - first).days
        if 0 <= i < PULSE_DAYS:
            found[i] += 1
            good[i] += (job.score or 0) >= min_score
    days = [
        Day(str(d.day), f"{d:%a}", found[i], good[i], d == today)
        for i in range(PULSE_DAYS)
        if (d := first + timedelta(days=i))
    ]
    return Pulse(days, sum(found.values()), sum(good.values()), max([1, *found.values()]))


@dataclass(frozen=True)
class Source:
    name: str
    jobs: int
    share: int  # % of the biggest source, for the bar


GROUPS = {"board": "Company boards", "page": "Watched pages", "manual": "Added by you",
          "email": "Email"}  # fmt: skip


def sources(ctx: Ctx, top: int = 5) -> list[Source]:
    """Where your good matches of the last two weeks came from: each Telegram chat, and
    company boards, pages and the rest as one line each."""
    repo, min_score, now = ctx.repo, ctx.good, ctx.now
    since = to_iso(now - timedelta(days=PULSE_DAYS))
    counts: dict[str, int] = {}
    for platform, title, n in repo.good_job_sources(min_score, since):
        name = GROUPS.get(platform, title)
        counts[name] = counts.get(name, 0) + n
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
    peak = max([1, *(n for _, n in ranked)])
    return [Source(name, n, round(100 * n / peak)) for name, n in ranked]
