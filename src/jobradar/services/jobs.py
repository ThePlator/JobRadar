"""Jobs: Today, All jobs (search and filters), one job's page, and the Ctrl+K palette."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from markupsafe import Markup

from jobradar.clock import to_iso
from jobradar.config import Settings
from jobradar.db.models import Artifact, Job, JobStatus
from jobradar.pipeline.schemas import JobPosting
from jobradar.pipeline.task_types import BUILD_RESUME
from jobradar.providers.base import MatchAnswers
from jobradar.providers.skills_rule import compare_skills
from jobradar.queue.tasks import TaskQueue
from jobradar.services import search as fts
from jobradar.services.cards import (
    PER_PAGE,
    SHOW_CHOICES,
    SHOW_JOBS,
    TODAY_DAYS,
    Card,
    card,
    label,
    when,
)
from jobradar.services.context import Ctx
from jobradar.services.home import take
from jobradar.sinks.email_template import day_month, posting_of, split_reason
from jobradar.sinks.followups import Draft, FollowUpReminder
from jobradar.sinks.notion import experience_text, message_link, source_label

# ---- Today -----------------------------------------------------------------------------------


def today(ctx: Ctx) -> list[Card]:
    """Good matches you haven't acted on, first seen in the last few days, best first."""
    since = to_iso(ctx.now - timedelta(days=TODAY_DAYS))
    return [card(j, ctx.tz, ctx.now) for j in ctx.repo.good_new_jobs(ctx.good, since)]


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
    filters: Filters = field(default_factory=lambda: Filters())
    snippets: dict[str, Markup] = field(default_factory=dict)  # job id -> where it matched

    def query(self, page: int) -> str:
        """This search's address on another page."""
        f = self.filters
        pairs = {"q": self.words, "show": self.show, "sort": self.sort, "min": f.min_score or "",
                 "within": f.within or "", "place": f.place, "remote": "1" if f.remote else "",
                 "source": f.source or "", "page": page}  # fmt: skip
        return "/jobs?" + urlencode({k: v for k, v in pairs.items() if v not in ("", None)})


@dataclass
class Filters:
    min_score: int | None = None
    within: int | None = None  # days
    place: str = ""
    remote: bool = False
    source: int | None = None

    @property
    def active(self) -> int:
        return sum(bool(x) for x in (self.min_score, self.within, self.place, self.remote,
                                     self.source))  # fmt: skip


MIN_SCORES = [(0, "Any score"), (50, "50+"), (75, "75+"), (90, "90+")]
WITHIN = [
    (0, "Any time"),
    (1, "Last 24 h"),
    (3, "Last 3 days"),
    (7, "Last week"),
    (30, "Last month"),
]


def show_statuses(show: str) -> list[str] | None:
    """The statuses a "Show" choice lists; None for everything. Unknown → "jobs"."""
    if show == "everything":
        return None
    if show in {s.value for s in JobStatus}:
        return [show]
    return [s.value for s in SHOW_JOBS]


SORTS = [("new", "Newest first"), ("old", "Oldest first"), ("score", "Best match"),
         ("relevance", "Most relevant")]  # fmt: skip


def all_jobs(
    ctx: Ctx,
    show: str,
    words: str,
    page: int,
    sort: str = "new",
    filters: Filters | None = None,
) -> JobList:
    filters = filters or Filters()
    repo, tz, now = ctx.repo, ctx.tz, ctx.now
    if show not in dict(SHOW_CHOICES):
        show = "jobs"
    words = words.strip()[:100]
    if sort not in dict(SORTS) or (sort == "relevance" and not words):
        sort = "relevance" if words and sort not in dict(SORTS) else "new"
    query = fts.match(words, filters.place, filters.remote)
    if query:
        fts.refresh(repo, now)
    since = to_iso(now - timedelta(days=filters.within)) if filters.within else None
    args = (show_statuses(show), query, filters.min_score or None, since, filters.source, sort)
    _, total = repo.find_jobs(*args, 0, 0)
    pages = max(1, -(-total // PER_PAGE))
    page = min(max(page, 1), pages)
    found, total = repo.find_jobs(*args, PER_PAGE, (page - 1) * PER_PAGE)
    return JobList(
        [card(j, tz, now) for j, _ in found], total, page, pages, show, words, sort, filters,
        {j.id: fts.snippet(snip) for j, snip in found if snip},
    )  # fmt: skip


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


def job_detail(ctx: Ctx, job_id: str, profile: dict[str, Any] | None = None) -> Detail | None:
    """One job's page; `profile` marks which of its skills you have."""
    repo, tz, now, settings = ctx.repo, ctx.tz, ctx.now, ctx.settings
    weights = settings.config.matching.weights
    job = repo.get_job(job_id)
    if job is None:
        return None
    draft = None
    if job.status == JobStatus.APPLIED.value:
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


# ---- the command palette (Ctrl+K) -------------------------------------------------------------


def search(ctx: Ctx, words: str, limit: int = 8) -> list[dict[str, object]]:
    """Jobs whose company or role contains `words`, as plain data for the palette."""
    if not words.strip():
        return []
    jobs, _ = ctx.repo.search_jobs(None, words[:100], limit, 0)
    return [
        {"id": j.id, "role": j.role or "Role not stated", "company": j.company or "",
         "score": j.score, "status": label(j.status)}
        for j in jobs
    ]  # fmt: skip
