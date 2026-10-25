"""What the API answers with: one Pydantic model per shape, so the OpenAPI document describes
every field. Times are UTC ISO 8601 text; days (a deadline) are YYYY-MM-DD."""

from __future__ import annotations

from pydantic import BaseModel, Field

from jobradar.db.models import Artifact, Job
from jobradar.pipeline.schemas import JobPosting


class Problem(BaseModel):
    """An error (RFC 9457 problem details)."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None


def posting_of(job: Job) -> JobPosting | None:
    if not job.data_json:
        return None
    try:
        return JobPosting.model_validate_json(job.data_json)
    except ValueError:
        return None


class JobItem(BaseModel):
    """A job as you see it: the posting, with your score and status."""

    id: str
    role: str | None
    company: str | None
    score: int | None = Field(description="Your Match Score, 0-100; null until matched")
    status: str
    status_set_at: str | None = Field(description="When you last set its status yourself")
    first_seen: str
    updated_at: str
    closes: str | None = Field(description="The deadline, YYYY-MM-DD, when the posting gives one")
    places: list[str]
    work_mode: str
    url: str | None
    apply_url: str | None
    snippet: str | None = Field(
        default=None, description="Search only: the matched text, with <mark> around the words"
    )

    @classmethod
    def of(cls, job: Job, snippet: str | None = None) -> JobItem:
        posting = posting_of(job)
        return cls(
            id=job.id, role=job.role, company=job.company, score=job.score, status=job.status,
            status_set_at=job.user_status_at, first_seen=job.created_at,
            updated_at=job.updated_at, closes=job.deadline,
            places=posting.locations if posting else [],
            work_mode=posting.work_mode if posting else "unknown",
            url=job.canonical_url, apply_url=posting.apply_url if posting else None,
            snippet=snippet,
        )  # fmt: skip


class JobPage(BaseModel):
    items: list[JobItem]
    total: int = Field(description="How many jobs match, in all pages")
    next_cursor: str | None = Field(description="Pass as `cursor` for the next page; null: done")


class Sighting(BaseModel):
    """A post or listing that carried the job."""

    platform: str
    source: str
    posted_at: str
    text: str | None


class StatusChange(BaseModel):
    status: str
    at: str


class Resume(BaseModel):
    id: int
    kind: str = Field(description="resume, or kit (form answers)")
    version: int
    created_at: str
    file: str = Field(description="Where to download the PDF (GET, same sign-in)")
    cover_letter: str | None = Field(description="The cover letter's PDF, when one was written")

    @classmethod
    def of(cls, artifact: Artifact, has_cover: bool) -> Resume:
        file = f"/api/v1/files/{artifact.id}"
        return cls(
            id=artifact.id or 0, kind=artifact.kind, version=artifact.version,
            created_at=artifact.created_at, file=file,
            cover_letter=f"{file}?cover=1" if has_cover else None,
        )  # fmt: skip


class JobDetail(JobItem):
    reasons: str | None = Field(description="Why it scored what it did")
    summary: str
    employment_type: str
    experience_min: float | None
    experience_max: float | None
    batch_years: list[int]
    degrees: list[str]
    skills_required: list[str]
    skills_preferred: list[str]
    salary: str | None
    scam_risk: str | None
    posted_in: list[Sighting]
    resumes: list[Resume]
    history: list[StatusChange]


class Plan(BaseModel):
    id: str
    llm_calls_per_day: int
    resume_builds_per_day: int
    emails_per_day: int
    followed_sources_max: int | None
    api_tokens_max: int


class Me(BaseModel):
    id: str
    email: str | None
    phone: str | None
    name: str | None
    role: str
    plan: Plan | None


class BoardItem(JobItem):
    resume_built: bool
    company_replied: str | None = Field(description="interview, test or rejected, if they wrote")
    follow_ups_sent: int
    next_follow_up_in_days: int | None


class Column(BaseModel):
    key: str
    title: str
    items: list[BoardItem]


class SourceApplication(BaseModel):
    job_id: str
    outcome: str = Field(description="next (reached an interview or offer), waiting or rejected")
    found_here_first: bool


class SourceRow(BaseModel):
    name: str
    platform: str
    applied: int
    next_round: int
    jobs: list[SourceApplication]


class BySource(BaseModel):
    applications: int
    next_round: int
    sources: list[SourceRow]


class Skill(BaseModel):
    key: str
    name: str
    jobs: int = Field(description="Good matches that need it (a choice like 'Java or C++' you "
                      "already cover doesn't count)")  # fmt: skip
    share: int = Field(description="% of the good matches looked at")
    trend: int | None = Field(description="Change against the window before")
    covered: int
    covered_by: str
    sample_job_ids: list[str]


class Skills(BaseModel):
    days: int
    looked_at: int
    min_score: int
    missing: list[Skill]
    have: list[Skill]
    warnings: list[str]


class Topic(BaseModel):
    label: str
    jobs: int
    job_ids: list[str]


class Quote(BaseModel):
    job_id: str
    kind: str
    label: str
    html: str = Field(description="The job's own words, escaped, with <mark> around the skill")


class SkillDetail(BaseModel):
    key: str
    name: str
    days: int
    looked_at: int
    asked: int
    needed: int
    counts: dict[str, int]
    verdict: str
    start: str
    topics: list[Topic]
    quotes: list[Quote]
    missing: bool


class Window(BaseModel):
    key: str
    label: str
    since: str
    until: str
    current: bool


class HiddenCount(BaseModel):
    reason: str
    jobs: int


class Digest(BaseModel):
    window: Window
    title: str
    new: int
    strong: int
    alerts: int
    inbox: list[JobItem]
    closing: list[JobItem]
    from_boards: list[JobItem]
    hidden: list[HiddenCount]
    pipeline: str | None
    spent: str
    failed: int


class Source(BaseModel):
    id: int
    platform: str
    title: str


class FollowUp(BaseModel):
    job: JobItem
    applied: str
    since: str = Field(description="When you applied, or last followed up")
    sent: int
    next_at: str
    state: str = Field(description="due, waiting, quiet (a reply came) or done")


class Draft(BaseModel):
    subject: str
    body: str
    to: str | None


class Health(BaseModel):
    ok: bool
