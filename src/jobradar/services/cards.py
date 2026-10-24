"""How one job reads on a list or a card: labels, times, places, buttons."""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from jobradar.db.models import Job, JobStatus
from jobradar.pipeline import status_rules as rules
from jobradar.pipeline.schemas import JobPosting
from jobradar.sinks.email_template import Reason, day_month, posting_of, split_reason

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


def as_utc(iso: str) -> datetime:
    at = datetime.fromisoformat(iso)
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def ago(iso: str | None, now: datetime) -> str:
    """'just now', '12 min ago', '3 h ago', '2 days ago', or ''."""
    if not iso:
        return ""
    try:
        minutes = max(int((now - as_utc(iso)).total_seconds() // 60), 0)
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
        at = as_utc(iso)
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
        days = max((now - as_utc(job.user_status_at)).days, 0)
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
        fresh=now is not None and now - as_utc(job.created_at) < timedelta(hours=FRESH_HOURS),
        actions=buttons(job),
    )
