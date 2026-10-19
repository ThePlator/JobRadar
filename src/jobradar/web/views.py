"""What each dashboard page shows, read from JobRadar's own database.

No web code here: each function turns database rows into plain data a template prints, so a
page's content is tested without a server. Times are shown in `notify.timezone`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from jobradar.clock import to_iso
from jobradar.db.models import Artifact, Job, JobStatus
from jobradar.db.repo import Repo
from jobradar.pipeline.schemas import JobPosting
from jobradar.sinks.email_template import Reason, day_month, posting_of, split_reason
from jobradar.sinks.notion import experience_text, message_link, source_label

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
PER_PAGE = 50

# The Applications board: (column, the statuses in it).
COLUMNS: list[tuple[str, list[JobStatus]]] = [
    ("Shortlisted", [JobStatus.SHORTLISTED, JobStatus.RESUME_READY]),
    ("Applied", [JobStatus.APPLIED]),
    ("Interview", [JobStatus.INTERVIEW]),
    ("Offer or rejected", [JobStatus.OFFER, JobStatus.REJECTED]),
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


def when(iso: str | None, tz: ZoneInfo) -> str:
    """'19 Oct, 21:15' in your timezone, or ''. A bare date (boards often give only the day,
    stored as midnight UTC) shows as '19 Oct'."""
    if not iso:
        return ""
    try:
        at = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    at = at if at.tzinfo else at.replace(tzinfo=UTC)
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


def card(job: Job, tz: ZoneInfo) -> Card:
    posting = posting_of(job)
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
        reason=split_reason(job.score_reason, posting),
    )


# ---- Today -----------------------------------------------------------------------------------


def today(repo: Repo, min_score: int, now: datetime, tz: ZoneInfo) -> list[Card]:
    """Good matches you haven't acted on, first seen in the last few days, best first."""
    since = to_iso(now - timedelta(days=TODAY_DAYS))
    return [card(j, tz) for j in repo.good_new_jobs(min_score, since)]


# ---- Applications ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Column:
    title: str
    cards: list[Card]


def board(repo: Repo, tz: ZoneInfo) -> list[Column]:
    return [Column(title, [card(j, tz) for j in repo.jobs_in(sts)]) for title, sts in COLUMNS]


# ---- All jobs --------------------------------------------------------------------------------


@dataclass(frozen=True)
class JobList:
    cards: list[Card]
    total: int
    page: int
    pages: int
    show: str
    words: str


def show_statuses(show: str) -> list[str] | None:
    """The statuses a "Show" choice lists; None for everything. Unknown → "jobs"."""
    if show == "everything":
        return None
    if show in {s.value for s in JobStatus}:
        return [show]
    return [s.value for s in SHOW_JOBS]


def all_jobs(repo: Repo, tz: ZoneInfo, show: str, words: str, page: int) -> JobList:
    if show not in dict(SHOW_CHOICES):
        show = "jobs"
    words = words.strip()[:100]
    _, total = repo.search_jobs(show_statuses(show), words, 0, 0)
    pages = max(1, -(-total // PER_PAGE))
    page = min(max(page, 1), pages)
    jobs, total = repo.search_jobs(show_statuses(show), words, PER_PAGE, (page - 1) * PER_PAGE)
    return JobList([card(j, tz) for j in jobs], total, page, pages, show, words)


# ---- One job ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Seen:
    where: str
    at: str
    link: str | None


@dataclass(frozen=True)
class File:
    name: str
    kind: str
    at: str


@dataclass
class Detail:
    card: Card
    url: str | None  # the job's own page
    apply_url: str | None  # where the job says to apply, when it's a different address
    score_reason: str | None
    facts: list[tuple[str, str]] = field(default_factory=list)
    skills_required: list[str] = field(default_factory=list)
    skills_preferred: list[str] = field(default_factory=list)
    summary: str = ""
    seen_in: list[Seen] = field(default_factory=list)
    history: list[tuple[str, str]] = field(default_factory=list)
    files: list[File] = field(default_factory=list)
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


def _file(artifact: Artifact, tz: ZoneInfo) -> File:
    path = artifact.local_path
    name = Path(path).name if path else f"{artifact.kind} v{artifact.version}"
    return File(name, artifact.kind.replace("_", " "), when(artifact.created_at, tz))


def job_detail(repo: Repo, job_id: str, tz: ZoneInfo) -> Detail | None:
    job = repo.get_job(job_id)
    if job is None:
        return None
    posting = posting_of(job)
    page = repo.get_page(job.id)
    apply_url = posting.apply_url if posting else None
    return Detail(
        card=card(job, tz),
        url=job.canonical_url,
        apply_url=apply_url if apply_url and apply_url != job.canonical_url else None,
        score_reason=job.score_reason,
        facts=_facts(job, posting),
        skills_required=posting.skills_required if posting else [],
        skills_preferred=posting.skills_preferred if posting else [],
        summary=posting.summary if posting else "",
        seen_in=[
            Seen(source_label(s), when(s.posted_at, tz), message_link(s))
            for s in repo.job_sightings(job.id)
        ],
        history=[(label(st), when(at, tz)) for st, at in repo.status_history(job.id)],
        files=[_file(a, tz) for a in repo.artifacts_of(job.id)],
        page_text=page.text if page and page.text else None,
    )
