"""Applications: the board, where each application stands, the funnel, and
interviews by source."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from jobradar.by_source import UNDO_MINUTES, Application, BySource, SourceRow, by_source
from jobradar.db.models import Job, JobStatus
from jobradar.db.repo import Repo
from jobradar.queue.tasks import TaskQueue
from jobradar.services.cards import COLUMNS, Card, card
from jobradar.services.context import Ctx
from jobradar.sinks.email_actions import REPLIED
from jobradar.sinks.email_template import day_month
from jobradar.sinks.followups import MAX_REMINDERS, FollowUpReminder
from jobradar.sinks.notion_sync import FOLLOWED_UP

# ---- Applications ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Track:
    """Where an application stands, beyond its status."""

    resume: bool  # a resume was built for it
    replied: str | None  # the company wrote back: "interview", "test", "rejected"
    followed_up: str  # the day you last followed up, "21 Oct", or ""
    next_nudge: str  # when JobRadar will draft a follow-up, "29 Oct", or ""
    nudge_in: int | None  # days until then (0: due now)
    waited: int  # % of the wait to the next follow-up, for the meter
    reminders: int  # follow-up drafts sent so far


@dataclass(frozen=True)
class BoardCard:
    card: Card
    track: Track


@dataclass(frozen=True)
class Column:
    key: str  # the status a card dropped here gets
    title: str
    cards: list[BoardCard]
    note: str  # one line under the title: "2 due for a follow-up"


REPLY_WORDS = {"interview": "Interview invite", "test": "Online test", "rejected": "Rejection"}


def track(repo: Repo, job: Job, tz: ZoneInfo, now: datetime, nudges: FollowUpReminder) -> Track:
    raw = repo.get_user_setting(REPLIED + job.id)
    replied = None
    if raw:
        try:
            replied = str(json.loads(raw).get("kind") or "") or None
        except ValueError:
            replied = None
    followed = repo.get_user_setting(FOLLOWED_UP + job.id) or ""
    next_nudge, nudge_in, waited, reminders = "", None, 0, 0
    if job.status == JobStatus.APPLIED.value and job.user_status_at and replied is None:
        _, since, reminders = nudges.status(job)
        if reminders < MAX_REMINDERS:
            due = since + nudges.after
            nudge_in = max((due.astimezone(tz).date() - now.astimezone(tz).date()).days, 0)
            next_nudge = f"{due.astimezone(tz).day} {due.astimezone(tz):%b}"
            total = nudges.after.total_seconds()
            waited = min(100, max(0, round(100 * (now - since).total_seconds() / total)))
    return Track(
        resume=repo.latest_artifact(job.id) is not None,
        replied=replied,
        followed_up=day_month(followed) if followed else "",
        next_nudge=next_nudge,
        nudge_in=nudge_in,
        waited=waited,
        reminders=reminders,
    )


def _note(key: JobStatus, cards: list[BoardCard]) -> str:
    if not cards:
        return ""
    if key == JobStatus.SHORTLISTED:
        ready = sum(1 for c in cards if c.track.resume)
        return f"{ready} with a resume ready" if ready else "No resumes built yet"
    if key == JobStatus.APPLIED:
        due = sum(1 for c in cards if c.track.nudge_in == 0)
        replied = sum(1 for c in cards if c.track.replied)
        bits = [f"{due} due for a follow-up"] if due else []
        bits += [f"{replied} replied"] if replied else []
        days = [c.card.days for c in cards if c.card.days is not None]
        if days and (average := round(sum(days) / len(days))) >= 1:
            bits.append(f"waiting {average} day{'s' if average > 1 else ''} on average")
        return " · ".join(bits)
    if key == JobStatus.INTERVIEW:
        return "Prepare, then set Offer or Rejected"
    return ""


def board(ctx: Ctx) -> list[Column]:
    """The Applications board: one column per stage, each card with where it stands."""
    repo, settings, now, tz = ctx.repo, ctx.settings, ctx.now, ctx.tz
    nudges = FollowUpReminder(settings, repo, TaskQueue(repo))
    columns = []
    for title, key, statuses in COLUMNS:
        cards = [BoardCard(card(j, tz, now), track(repo, j, tz, now, nudges))
                 for j in repo.jobs_in(statuses)]  # fmt: skip
        columns.append(Column(key.value, title, cards, _note(key, cards)))
    return columns


@dataclass(frozen=True)
class Step:
    name: str
    jobs: int
    width: int  # % of the first step, for the bar
    rate: str  # "12% of the step before", or ""


def funnel(ctx: Ctx) -> list[Step]:
    """From every job found to offers: how far your applications get."""
    repo, min_score = ctx.repo, ctx.good
    counts = repo.status_counts()
    found = sum(n for st, n in counts.items() if st != JobStatus.DISCARDED.value)
    with_good = repo.search_good_count(min_score)
    after_shortlist = [JobStatus.SHORTLISTED, JobStatus.RESUME_READY, JobStatus.APPLIED,
                       JobStatus.INTERVIEW, JobStatus.OFFER]  # fmt: skip
    steps = [
        ("Found", found),
        (f"Scored {min_score}+", with_good),
        ("Shortlisted", repo.jobs_ever_in(after_shortlist, UNDO_MINUTES)),
        ("Applied", repo.jobs_ever_in(after_shortlist[2:], UNDO_MINUTES)),
        ("Interview", repo.jobs_ever_in([JobStatus.INTERVIEW, JobStatus.OFFER], UNDO_MINUTES)),
        ("Offer", repo.jobs_ever_in([JobStatus.OFFER], UNDO_MINUTES)),
    ]
    top = max(steps[0][1], 1)
    out: list[Step] = []
    for i, (name, n) in enumerate(steps):
        before = steps[i - 1][1] if i else 0
        rate = f"{round(100 * n / before)}% of {steps[i - 1][0].lower()}" if i and before else ""
        out.append(Step(name, n, max(round(100 * n / top), 1 if n else 0), rate))
    return out


@dataclass(frozen=True)
class SourceStrip:
    """Interviews by source, above the Applications board."""

    summary: str
    rows: list[tuple[SourceRow, list[tuple[Card, Application]]]]  # each row with its jobs


def source_strip(ctx: Ctx) -> SourceStrip | None:
    """None until you've applied to something."""
    repo, tz = ctx.repo, ctx.tz
    report = by_source(repo)
    if not report.rows:
        return None
    cards: dict[str, Card] = {}
    rows = []
    for row in report.rows:
        jobs = []
        for a in row.jobs:
            if a.job_id not in cards and (job := repo.get_job(a.job_id)):
                cards[a.job_id] = card(job, tz)
            if a.job_id in cards:
                jobs.append((cards[a.job_id], a))
        rows.append((row, jobs))
    return SourceStrip(report.summary, rows)


def sources_report(ctx: Ctx) -> BySource:
    """Each source you applied from and how many reached the next round: `jobradar sources`."""
    return by_source(ctx.repo)
