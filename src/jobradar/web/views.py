"""What each dashboard page shows, read from JobRadar's own database.

No web code here: each function turns database rows into plain data a template prints, so a
page's content is tested without a server. Times are shown in `notify.timezone`.
"""

from __future__ import annotations

import json
import re
import zlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jobradar.clock import to_iso
from jobradar.config import Settings
from jobradar.db.models import Artifact, Job, JobStatus
from jobradar.db.repo import Repo
from jobradar.pipeline import status_rules as rules
from jobradar.pipeline.schemas import JobPosting
from jobradar.pipeline.skill_gaps import Gap, skill_gaps
from jobradar.pipeline.task_types import BUILD_RESUME
from jobradar.providers.base import MatchAnswers
from jobradar.providers.skills_rule import compare_skills
from jobradar.queue.tasks import TaskQueue
from jobradar.sinks.digest import gather, part_of_day, pipeline_line, previous_slot
from jobradar.sinks.email_actions import REPLIED
from jobradar.sinks.email_template import Reason, day_month, posting_of, split_reason
from jobradar.sinks.followups import MAX_REMINDERS, Draft, FollowUpReminder
from jobradar.sinks.notion import experience_text, message_link, source_label
from jobradar.sinks.notion_sync import FOLLOWED_UP

LABELS = {
    JobStatus.NEW: "New",
    JobStatus.SHORTLISTED: "Shortlisted",
    JobStatus.RESUME_READY: "Resume ready",
    JobStatus.APPLIED: "Applied",
    JobStatus.INTERVIEW: "Interview",
    JobStatus.OFFER: "Offer",
    JobStatus.REJECTED: "Rejected",
    JobStatus.SKIPPED: "Skipped",
    JobStatus.HIDDEN: "Hidden",
    JobStatus.DISCARDED: "Not a job",
    JobStatus.NEEDS_REVIEW: "Needs a look",
    JobStatus.DISCOVERED: "Just found",
    JobStatus.EXTRACTED: "Being scored",
}
TODAY_DAYS = 3  # Today lists good matches first seen this recently
FRESH_HOURS = 6  # a job first seen this recently is marked "new"
PER_PAGE = 50

# The Applications board: (column, the statuses in it).
# The Applications board: (column, the status a card dropped there gets, the statuses in it).
COLUMNS: list[tuple[str, JobStatus, list[JobStatus]]] = [
    ("Shortlisted", JobStatus.SHORTLISTED, [JobStatus.SHORTLISTED, JobStatus.RESUME_READY]),
    ("Applied", JobStatus.APPLIED, [JobStatus.APPLIED]),
    ("Interview", JobStatus.INTERVIEW, [JobStatus.INTERVIEW]),
    ("Offer", JobStatus.OFFER, [JobStatus.OFFER]),
    ("Rejected", JobStatus.REJECTED, [JobStatus.REJECTED]),
]

# All jobs: the "Show" choices. "jobs" leaves out what JobRadar hid or found wasn't a job.
SHOW_JOBS = [
    JobStatus.NEW, JobStatus.NEEDS_REVIEW, JobStatus.SHORTLISTED, JobStatus.RESUME_READY,
    JobStatus.APPLIED, JobStatus.INTERVIEW, JobStatus.OFFER, JobStatus.REJECTED,
    JobStatus.SKIPPED,
]  # fmt: skip
SHOW_CHOICES: list[tuple[str, str]] = [
    ("jobs", "Jobs (not hidden)"),
    *[(s.value, LABELS[s]) for s in SHOW_JOBS],
    (JobStatus.HIDDEN.value, LABELS[JobStatus.HIDDEN]),
    ("everything", "Everything"),
]


def label(status: str) -> str:
    try:
        return LABELS[JobStatus(status)]
    except ValueError:
        return status.replace("_", " ").capitalize()


def _at(iso: str) -> datetime:
    at = datetime.fromisoformat(iso)
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def ago(iso: str | None, now: datetime) -> str:
    """'just now', '12 min ago', '3 h ago', '2 days ago', or ''."""
    if not iso:
        return ""
    try:
        minutes = max(int((now - _at(iso)).total_seconds() // 60), 0)
    except ValueError:
        return ""
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h ago"
    return f"{minutes // (24 * 60)} days ago"


def when(iso: str | None, tz: ZoneInfo) -> str:
    """'19 Oct, 21:15' in your timezone, or ''. A bare date (boards often give only the day,
    stored as midnight UTC) shows as '19 Oct'."""
    if not iso:
        return ""
    try:
        at = _at(iso)
    except ValueError:
        return ""
    if at.astimezone(UTC).time() == time(0):
        return f"{at.day} {at:%b}"
    local = at.astimezone(tz)
    return f"{local.day} {local:%b}, {local:%H:%M}"


def _place(posting: JobPosting | None) -> str:
    if posting is None:
        return ""
    places = posting.locations[:3]
    mode = posting.work_mode.capitalize()
    if posting.work_mode not in ("unknown", "onsite") and mode.casefold() not in {
        p.casefold() for p in places
    }:
        places.append(mode)
    more = len(posting.locations) - 3
    return " · ".join(places) + (f" +{more}" if more > 0 else "")


@dataclass(frozen=True)
class Card:
    """One job as a line on a list or a card on the board."""

    id: str
    role: str
    company: str
    score: int | None
    status: str
    status_label: str
    place: str
    closes: str  # "24 Oct", or ""
    seen: str  # first seen, "19 Oct, 21:15"
    moved: str  # when you last set its status, or ""
    reason: Reason
    initials: str  # the company's avatar: "RU"
    hue: int  # and its colour, 0-7, stable per company
    days: int | None  # days since you last set its status
    tags: str  # quick-filter tags on Today: "eligible remote internship closing"
    closes_in: int | None  # days until the deadline (negative: closed), None: no deadline
    url: str | None  # the job's own page
    seen_at: str  # first seen, ISO, for sorting
    age: str  # "3 h ago"
    fresh: bool  # first seen in the last few hours
    actions: list[tuple[str, str]]  # the buttons for its status: (action, label)


def initials(name: str) -> str:
    words = [w for w in name.replace("&", " ").split() if w[:1].isalnum()]
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    return (words[0][:2] if words else "?").upper()


def hue(name: str) -> int:
    return zlib.crc32(name.casefold().encode()) % 8


def _tags(job: Job, posting: JobPosting | None, reason: Reason, closes_in: int | None) -> list[str]:
    tags: list[str] = []
    if reason.eligible:
        tags.append("eligible")
    if posting and posting.work_mode in ("remote", "hybrid"):
        tags.append("remote")
    if (posting and posting.employment_type == "internship") or "intern" in (
        job.role or ""
    ).casefold():
        tags.append("internship")
    if closes_in is not None and 0 <= closes_in <= 7:
        tags.append("closing")
    return tags


QUICK_FILTERS = [
    ("all", "All"), ("eligible", "Eligible"), ("remote", "Remote or hybrid"),
    ("internship", "Internships"), ("closing", "Closing soon"),
]  # fmt: skip


BUTTON_LABELS = {
    "applied": "I applied", "shortlist": "Shortlist", "skip": "Not interested",
    "interview": "Interview", "followed up": "I followed up", "offer": "Offer",
    "rejected": "Rejected",
}  # fmt: skip
# The buttons each status shows, most likely first. (The rules allow a little more, e.g.
# Interview straight from New; that's in Change status.)
_BUTTONS: dict[str, list[str]] = {
    JobStatus.NEW.value: ["applied", "shortlist", "skip"],
    JobStatus.HIDDEN.value: ["shortlist", "applied"],
    JobStatus.SHORTLISTED.value: ["applied", "skip"],
    JobStatus.RESUME_READY.value: ["applied", "skip"],
    JobStatus.SKIPPED.value: ["shortlist", "applied"],
    JobStatus.APPLIED.value: ["interview", "followed up", "rejected"],
    JobStatus.INTERVIEW.value: ["offer", "rejected"],
}


def buttons(job: Job) -> list[tuple[str, str]]:
    return [(a, BUTTON_LABELS[a]) for a in _BUTTONS.get(job.status, []) if rules.can(a, job)]


def card(job: Job, tz: ZoneInfo, now: datetime | None = None) -> Card:
    posting = posting_of(job)
    company = job.company or ""
    reason = split_reason(job.score_reason, posting)
    closes_in = None
    if job.deadline and now is not None:
        try:
            closes_in = (date.fromisoformat(job.deadline[:10]) - now.astimezone(tz).date()).days
        except ValueError:
            closes_in = None
    days = None
    if job.user_status_at and now is not None:
        days = max((now - _at(job.user_status_at)).days, 0)
    return Card(
        id=job.id,
        role=job.role or "Role not stated",
        company=job.company or "",
        score=job.score,
        status=job.status,
        status_label=label(job.status),
        place=_place(posting),
        closes=day_month(job.deadline),
        seen=when(job.created_at, tz),
        moved=when(job.user_status_at, tz),
        reason=reason,
        initials=initials(company or job.role or "?"),
        hue=hue(company or job.role or "?"),
        days=days,
        tags=" ".join(_tags(job, posting, reason, closes_in)),
        closes_in=closes_in,
        url=job.canonical_url,
        seen_at=job.created_at,
        age=ago(job.created_at, now) if now is not None else "",
        fresh=now is not None and now - _at(job.created_at) < timedelta(hours=FRESH_HOURS),
        actions=buttons(job),
    )


# ---- Today -----------------------------------------------------------------------------------


def today(repo: Repo, min_score: int, now: datetime, tz: ZoneInfo) -> list[Card]:
    """Good matches you haven't acted on, first seen in the last few days, best first."""
    since = to_iso(now - timedelta(days=TODAY_DAYS))
    return [card(j, tz, now) for j in repo.good_new_jobs(min_score, since)]


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
    raw = repo.get_setting(REPLIED + job.id)
    replied = None
    if raw:
        try:
            replied = str(json.loads(raw).get("kind") or "") or None
        except ValueError:
            replied = None
    followed = repo.get_setting(FOLLOWED_UP + job.id) or ""
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


def board(repo: Repo, settings: Settings, now: datetime) -> list[Column]:
    tz = settings.config.notify.tz
    nudges = FollowUpReminder(settings, repo, TaskQueue(repo))
    columns = []
    for title, key, statuses in COLUMNS:
        cards = [BoardCard(card(j, tz, now), track(repo, j, tz, now, nudges))
                 for j in repo.jobs_in(statuses)]  # fmt: skip
        columns.append(Column(key.value, title, cards, _note(key, cards)))
    return columns


# ---- All jobs --------------------------------------------------------------------------------


@dataclass(frozen=True)
class JobList:
    cards: list[Card]
    total: int
    page: int
    pages: int
    show: str
    words: str
    sort: str = "new"


def show_statuses(show: str) -> list[str] | None:
    """The statuses a "Show" choice lists; None for everything. Unknown → "jobs"."""
    if show == "everything":
        return None
    if show in {s.value for s in JobStatus}:
        return [show]
    return [s.value for s in SHOW_JOBS]


SORTS = [("new", "Newest first"), ("old", "Oldest first"), ("score", "Best match")]


def all_jobs(
    repo: Repo,
    tz: ZoneInfo,
    show: str,
    words: str,
    page: int,
    now: datetime | None = None,
    sort: str = "new",
) -> JobList:
    if show not in dict(SHOW_CHOICES):
        show = "jobs"
    words = words.strip()[:100]
    _, total = repo.search_jobs(show_statuses(show), words, 0, 0)
    pages = max(1, -(-total // PER_PAGE))
    page = min(max(page, 1), pages)
    if sort not in dict(SORTS):
        sort = "new"
    offset = (page - 1) * PER_PAGE
    jobs, total = repo.search_jobs(show_statuses(show), words, PER_PAGE, offset, sort)
    return JobList([card(j, tz, now) for j in jobs], total, page, pages, show, words, sort)


# ---- One job ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Seen:
    where: str
    at: str
    link: str | None


@dataclass(frozen=True)
class File:
    id: int | None
    name: str
    kind: str
    at: str
    version: int = 1
    url: str | None = None  # opens the PDF, when it's there
    cover_url: str | None = None  # the cover letter written next to it, when there is one
    tailored: bool = False
    problems: list[str] = field(default_factory=list)  # the resume check's findings
    skills_on: list[str] = field(default_factory=list)  # the job's skills on the page
    skills_off: list[str] = field(default_factory=list)  # the job's skills you have, not on it


@dataclass
class Detail:
    card: Card
    take: str  # Blip's one-line reading of the score
    breakdown: Breakdown | None
    url: str | None  # the job's own page
    apply_url: str | None  # where the job says to apply, when it's a different address
    score_reason: str | None
    facts: list[tuple[str, str]] = field(default_factory=list)
    skills_required: list[Skill] = field(default_factory=list)
    skills_preferred: list[Skill] = field(default_factory=list)
    summary: str = ""
    seen_in: list[Seen] = field(default_factory=list)
    history: list[tuple[str, str]] = field(default_factory=list)
    files: list[File] = field(default_factory=list)
    building: bool = False  # a resume is queued or being built
    draft: Draft | None = None  # the follow-up JobRadar would send (Applied jobs)
    page_text: str | None = None


def _facts(job: Job, posting: JobPosting | None) -> list[tuple[str, str]]:
    facts: list[tuple[str, str]] = []
    if posting is None:
        return facts
    if posting.locations:
        facts.append(("Location", ", ".join(posting.locations)))
    if posting.work_mode != "unknown":
        facts.append(("Work mode", posting.work_mode.capitalize()))
    if posting.employment_type != "unknown":
        facts.append(("Type", posting.employment_type.replace("_", " ").capitalize()))
    if experience := experience_text(posting):
        facts.append(("Experience", experience))
    if posting.batch_years:
        facts.append(("Batch", ", ".join(str(y) for y in posting.batch_years)))
    if posting.degrees:
        facts.append(("Degree", ", ".join(posting.degrees)))
    if posting.salary_text:
        facts.append(("Salary", posting.salary_text))
    if closes := day_month(job.deadline):
        facts.append(("Closes", closes))
    if posting.published_on:
        facts.append(("Posted", day_month(posting.published_on.isoformat())))
    return facts


_VERSION = re.compile(r"_v\d+\.pdf$")


def artifact_file(
    settings: Settings | None, artifact: Artifact, cover: bool = False
) -> Path | None:
    """The PDF for this resume, or the cover letter beside it ({…}_cover_letter.pdf), if it's
    a file inside storage.local_dir. Never a path from anywhere else."""
    if not artifact.local_path:
        return None
    base = (settings.config.storage.local_dir if settings else Path("output")).resolve()
    path = Path(artifact.local_path)
    path = (path if path.is_absolute() else Path.cwd() / path).resolve()
    if cover:
        if not _VERSION.search(path.name):
            return None
        path = path.with_name(_VERSION.sub("_cover_letter.pdf", path.name))
    if path.suffix.lower() != ".pdf" or not path.is_relative_to(base) or not path.is_file():
        return None
    return path


def _file(artifact: Artifact, tz: ZoneInfo, settings: Settings | None = None) -> File:
    path = artifact.local_path
    name = Path(path).name if path else f"{artifact.kind} v{artifact.version}"
    record: dict[str, Any] = {}
    try:
        record = json.loads(artifact.content_json or "{}")
    except ValueError:
        record = {}
    ats_report = record.get("ats") or {}
    has_file = artifact_file(settings, artifact) is not None
    has_cover = artifact_file(settings, artifact, cover=True) is not None
    return File(
        id=artifact.id,
        name=name,
        kind=artifact.kind.replace("_", " "),
        at=when(artifact.created_at, tz),
        version=artifact.version,
        url=f"/files/{artifact.id}" if has_file else None,
        cover_url=f"/files/{artifact.id}?cover=1" if has_cover else None,
        tailored=bool(record.get("tailored")),
        problems=[str(p) for p in ats_report.get("problems") or []],
        skills_on=[str(k) for k in ats_report.get("skills_on_resume") or []],
        skills_off=[str(k) for k in ats_report.get("skills_not_on_resume") or []],
    )


@dataclass(frozen=True)
class Skill:
    name: str
    have: bool | None  # on your profile; None when there's no profile to compare


def skills(names: list[str], profile: dict[str, Any] | None) -> list[Skill]:
    if not profile:
        return [Skill(n, None) for n in names]
    have, _ = compare_skills(names, profile)
    return [Skill(n, n in have) for n in names]


@dataclass(frozen=True)
class Part:
    """One weighted part of the score."""

    name: str
    value: int  # 0-100
    weight: int  # % of the score


@dataclass(frozen=True)
class Breakdown:
    parts: list[Part]
    eligible: int | None  # 0-100: how sure the job is open to you (a gate, not weighted)
    confidence: int  # 0-100
    by: str  # "AI read the job" / "Rules" / "Skills only"


PART_NAMES = {
    "skills_coverage": "Skills you have",
    "role_alignment": "Role fits what you want",
    "seniority_fit": "Right level for you",
    "location_ok": "Location works",
}
BY = {"llm": "AI read the whole job", "rules": "Rules (a hard filter)",
      "skills_rule": "Skills only (no AI)"}  # fmt: skip


def breakdown(answers_json: str | None, weights: dict[str, float]) -> Breakdown | None:
    if not answers_json:
        return None
    try:
        answers = MatchAnswers.model_validate_json(answers_json)
    except ValueError:
        return None
    total = sum(weights.values()) or 1
    parts = [
        Part(PART_NAMES.get(name, name), round(100 * value), round(100 * w / total))
        for name, w in weights.items()
        if (value := getattr(answers, name, None)) is not None and w > 0
    ]
    eligible = None if answers.eligible is None else round(100 * answers.eligible)
    if not parts and eligible is None:
        return None
    by = BY.get(answers.provider, answers.provider)
    return Breakdown(parts, eligible, round(100 * answers.confidence), by)


def job_detail(
    repo: Repo,
    job_id: str,
    tz: ZoneInfo,
    now: datetime,
    weights: dict[str, float] | None = None,
    profile: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> Detail | None:
    job = repo.get_job(job_id)
    if job is None:
        return None
    draft = None
    if settings is not None and job.status == JobStatus.APPLIED.value:
        draft = FollowUpReminder(settings, repo, TaskQueue(repo)).draft_for(job)
    posting = posting_of(job)
    page = repo.get_page(job.id)
    apply_url = posting.apply_url if posting else None
    return Detail(
        card=card(job, tz, now),
        take=take(split_reason(job.score_reason, posting)),
        breakdown=breakdown(repo.latest_match(job.id), weights or {}),
        url=job.canonical_url,
        apply_url=apply_url if apply_url and apply_url != job.canonical_url else None,
        score_reason=job.score_reason,
        facts=_facts(job, posting),
        skills_required=skills(posting.skills_required, profile) if posting else [],
        skills_preferred=skills(posting.skills_preferred, profile) if posting else [],
        summary=posting.summary if posting else "",
        seen_in=[
            Seen(source_label(s), when(s.posted_at, tz), message_link(s))
            for s in repo.job_sightings(job.id)
        ],
        history=[(label(st), when(at, tz)) for st, at in repo.status_history(job.id)],
        files=[_file(a, tz, settings) for a in repo.artifacts_of(job.id)],
        building=repo.task_waiting(BUILD_RESUME, job.id),
        draft=draft,
        page_text=page.text if page and page.text else None,
    )


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


def overview(repo: Repo, min_score: int, now: datetime, tz: ZoneInfo, name: str) -> Overview:
    counts = repo.status_counts()

    def n(*statuses: JobStatus) -> int:
        return sum(counts.get(st.value, 0) for st in statuses)

    since = to_iso(now - timedelta(days=TODAY_DAYS))
    day = now.astimezone(tz).date()
    closing = repo.jobs_closing(day.isoformat(), (day + timedelta(days=2)).isoformat())
    last = repo.last_activity()
    awake = bool(last) and (now - _at(last or "")) < timedelta(minutes=AWAKE_MINUTES)
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


def pulse(repo: Repo, min_score: int, now: datetime, tz: ZoneInfo) -> Pulse:
    """Jobs first seen each day for the last two weeks, and how many were good matches."""
    today = now.astimezone(tz).date()
    first = today - timedelta(days=PULSE_DAYS - 1)
    start = datetime.combine(first, time(0), tz)
    found = dict.fromkeys(range(PULSE_DAYS), 0)
    good = dict.fromkeys(range(PULSE_DAYS), 0)
    for job in repo.jobs_created_since(to_iso(start)):
        if job.status == JobStatus.DISCARDED.value:
            continue
        i = (_at(job.created_at).astimezone(tz).date() - first).days
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


def sources(repo: Repo, min_score: int, now: datetime, top: int = 5) -> list[Source]:
    """Where your good matches of the last two weeks came from: each Telegram chat, and
    company boards, pages and the rest as one line each."""
    since = to_iso(now - timedelta(days=PULSE_DAYS))
    counts: dict[str, int] = {}
    for platform, title, n in repo.good_job_sources(min_score, since):
        name = GROUPS.get(platform, title)
        counts[name] = counts.get(name, 0) + n
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
    peak = max([1, *(n for _, n in ranked)])
    return [Source(name, n, round(100 * n / peak)) for name, n in ranked]


@dataclass(frozen=True)
class Step:
    name: str
    jobs: int
    width: int  # % of the first step, for the bar
    rate: str  # "12% of the step before", or ""


def funnel(repo: Repo, min_score: int) -> list[Step]:
    """From every job found to offers: how far your applications get."""
    counts = repo.status_counts()
    found = sum(n for st, n in counts.items() if st != JobStatus.DISCARDED.value)
    with_good = repo.search_good_count(min_score)
    after_shortlist = [JobStatus.SHORTLISTED, JobStatus.RESUME_READY, JobStatus.APPLIED,
                       JobStatus.INTERVIEW, JobStatus.OFFER]  # fmt: skip
    steps = [
        ("Found", found),
        (f"Scored {min_score}+", with_good),
        ("Shortlisted", repo.jobs_ever_in(after_shortlist)),
        ("Applied", repo.jobs_ever_in(after_shortlist[2:])),
        ("Interview", repo.jobs_ever_in([JobStatus.INTERVIEW, JobStatus.OFFER])),
        ("Offer", repo.jobs_ever_in([JobStatus.OFFER])),
    ]
    top = max(steps[0][1], 1)
    out: list[Step] = []
    for i, (name, n) in enumerate(steps):
        before = steps[i - 1][1] if i else 0
        rate = f"{round(100 * n / before)}% of {steps[i - 1][0].lower()}" if i and before else ""
        out.append(Step(name, n, max(round(100 * n / top), 1 if n else 0), rate))
    return out


# ---- the command palette (Ctrl+K) -------------------------------------------------------------


def search(repo: Repo, words: str, limit: int = 8) -> list[dict[str, object]]:
    """Jobs whose company or role contains `words`, as plain data for the palette."""
    if not words.strip():
        return []
    jobs, _ = repo.search_jobs(None, words[:100], limit, 0)
    return [
        {"id": j.id, "role": j.role or "Role not stated", "company": j.company or "",
         "score": j.score, "status": label(j.status)}
        for j in jobs
    ]  # fmt: skip


# ---- the digest -------------------------------------------------------------------------------

DIGEST_WINDOWS = 8  # the current window and the digests before it


@dataclass(frozen=True)
class Window:
    key: str  # "now", or the digest time: "2026-10-19-1900"
    label: str  # "Now", "Evening · 19 Oct"
    since: datetime
    until: datetime
    current: bool


def windows(now: datetime, times: list[str]) -> list[Window]:
    """The window the next digest will cover, then each earlier digest's, newest first.
    `now` is in your timezone."""
    out = [Window("now", "So far", previous_slot(now - timedelta(minutes=1), times), now, True)]
    until = out[0].since
    for _ in range(DIGEST_WINDOWS - 1):
        since = previous_slot(until - timedelta(minutes=1), times)
        label = f"{part_of_day(until)} · {until.day} {until:%b}"
        out.append(Window(f"{until:%Y-%m-%d-%H%M}", label, since, until, False))
        until = since
    return out


@dataclass(frozen=True)
class Board:
    card: Card
    board: str


@dataclass
class DigestPage:
    window: Window
    windows: list[Window]
    title: str  # "Evening digest"
    span: str  # "since Mon 19 Oct, 09:00" / "19 Oct, 09:00 to 19:00"
    new: int
    strong: int
    alerts: int
    inbox: list[Card]
    hidden: list[tuple[str, int]]
    closing: list[Card]
    boards: list[Board]
    pipeline: str | None
    spent: str
    failed: int
    gaps: list[Gap]
    gap_jobs: int

    @property
    def hidden_total(self) -> int:
        return sum(n for _, n in self.hidden)


def digest(
    repo: Repo, settings: Settings, now: datetime, key: str, profile: dict[str, Any] | None
) -> DigestPage:
    """A digest on the page: what the next one will say ("now"), or an earlier one again."""
    notify = settings.config.notify
    tz = notify.tz
    local = now.astimezone(tz)
    all_windows = windows(local, notify.digest_times)
    # No choice: the latest digest that went out (what's in your inbox), not the window
    # that's only just begun.
    window = next((w for w in all_windows if w.key == key), all_windows[1])
    data = gather(repo, settings, window.since, window.until, bounded=not window.current)
    min_score = settings.config.scoring.alert_above
    gaps: list[Gap] = []
    gap_jobs = 0
    if profile:
        gaps, gap_jobs = skill_gaps(repo, profile, window.until - timedelta(days=7), min_score)
    since = f"{window.since:%a} {window.since.day} {window.since:%b}, {window.since:%H:%M}"
    span = (
        f"since {since}"
        if window.current
        else f"{since} to {window.until:%H:%M}"
        if window.since.date() == window.until.date()
        else f"{since} to {window.until:%a} {window.until.day} {window.until:%b}, "
        f"{window.until:%H:%M}"
    )
    title = "Next digest, so far" if window.current else f"{part_of_day(window.until)} digest"
    return DigestPage(
        window=window,
        windows=all_windows,
        title=title,
        span=span,
        new=data.new,
        strong=sum(1 for j in data.inbox if (j.score or 0) >= min_score),
        alerts=data.alerts,
        inbox=[card(j, tz, now) for j in data.inbox[:30]],
        hidden=sorted(data.hidden.items(), key=lambda kv: -kv[1]),
        closing=[card(j, tz, now) for j in data.closing[:10]],
        boards=[Board(card(j, tz, now), b) for j, b in data.boards[:10]],
        pipeline=pipeline_line(data),
        spent=f"₹{data.spent_inr:.0f} of ₹{data.budget_inr:.0f}",
        failed=data.failed,
        gaps=gaps,
        gap_jobs=gap_jobs,
    )
