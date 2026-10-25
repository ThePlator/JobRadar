"""SQLModel tables: source, raw_message, job, job_source, artifact, task, llm_cache; and for the
SaaS, users and plans.

Mirrors the LLD schema. Timestamps are UTC text from `jobradar.clock.to_iso`.
"""

from enum import StrEnum

from sqlalchemy import CheckConstraint, Index, UniqueConstraint
from sqlmodel import Field, SQLModel


class Platform(StrEnum):
    TELEGRAM = "telegram"
    EMAIL = "email"
    WHATSAPP = "whatsapp"
    MANUAL = "manual"  # `jobradar add <url>`
    BOARD = "board"  # a company's job board (Greenhouse, Lever, Ashby, SmartRecruiters)
    PAGE = "page"  # a watched web page, e.g. a hiring-drive page (sources.pages)


class JobStatus(StrEnum):
    DISCOVERED = "discovered"
    EXTRACTED = "extracted"
    NEEDS_REVIEW = "needs_review"
    DISCARDED = "discarded"
    NEW = "new"
    HIDDEN = "hidden"
    SHORTLISTED = "shortlisted"
    RESUME_READY = "resume_ready"
    APPLIED = "applied"
    INTERVIEW = "interview"
    OFFER = "offer"
    REJECTED = "rejected"
    SKIPPED = "skipped"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


def _in(column: str, values: type[StrEnum]) -> str:
    return f"{column} IN ({', '.join(repr(v.value) for v in values)})"


class Source(SQLModel, table=True):
    __tablename__ = "source"
    __table_args__ = (
        UniqueConstraint("platform", "chat_id"),
        CheckConstraint(_in("platform", Platform), name="source_platform"),
    )

    id: int | None = Field(default=None, primary_key=True)
    platform: str
    chat_id: str
    title: str | None = None
    enabled: bool = True
    last_msg_id: int | None = None  # catch-up point


class RawMessage(SQLModel, table=True):
    __tablename__ = "raw_message"
    __table_args__ = (UniqueConstraint("source_id", "message_id"),)

    id: int | None = Field(default=None, primary_key=True)
    source_id: int = Field(foreign_key="source.id")
    message_id: str
    text: str | None = None
    urls_json: str = "[]"
    media_path: str | None = None  # saved image for OCR
    posted_at: str
    received_at: str
    processed: bool = False


class Job(SQLModel, table=True):
    __tablename__ = "job"
    __table_args__ = (
        CheckConstraint("scam_risk IN ('low', 'medium', 'high')", name="job_scam_risk"),
        CheckConstraint(_in("status", JobStatus), name="job_status_value"),
        Index("job_fingerprint", "fingerprint"),
        Index("job_status", "status"),
    )

    id: str = Field(primary_key=True)  # sha1(canonical_url) or sha1(fingerprint)
    canonical_url: str | None = Field(default=None, unique=True)
    fingerprint: str | None = None  # norm(company)|norm(role)|norm(location)
    company: str | None = None
    role: str | None = None
    deadline: str | None = None
    data_json: str | None = None  # full JobPosting
    score: int | None = None
    score_reason: str | None = None
    scam_risk: str | None = None
    status: str = JobStatus.DISCOVERED.value
    notion_page_id: str | None = None
    notion_synced_at: str | None = None
    prompt_version: str | None = None
    user_status_at: str | None = None  # you set the Status in Notion: JobRadar never overrides it
    created_at: str
    updated_at: str


class JobSource(SQLModel, table=True):
    __tablename__ = "job_source"

    job_id: str = Field(foreign_key="job.id", primary_key=True)
    raw_message_id: int = Field(foreign_key="raw_message.id", primary_key=True)


class Artifact(SQLModel, table=True):
    __tablename__ = "artifact"
    __table_args__ = (CheckConstraint("kind IN ('resume', 'kit')", name="artifact_kind"),)

    id: int | None = Field(default=None, primary_key=True)
    job_id: str = Field(foreign_key="job.id", index=True)
    kind: str
    version: int = 1
    local_path: str | None = None
    drive_url: str | None = None  # Drive webViewLink, shown in Notion
    content_json: str | None = None  # tailored content / answers
    created_at: str


class Task(SQLModel, table=True):
    __tablename__ = "task"
    __table_args__ = (
        UniqueConstraint("type", "key"),
        CheckConstraint(_in("status", TaskStatus), name="task_status_value"),
        Index("task_ready", "status", "run_after"),
    )

    id: int | None = Field(default=None, primary_key=True)
    type: str
    key: str  # idempotency key, e.g. job id
    payload: str = "{}"
    status: str = TaskStatus.PENDING.value
    attempts: int = 0
    run_after: str
    started_at: str | None = None  # set on claim; finds tasks stuck after a crash
    rerun: bool = False  # re-enqueued while running: run again after this attempt
    priority: int | None = None  # higher runs first (FRESH: work for a job that just arrived)
    last_error: str | None = None


class FetchedPage(SQLModel, table=True):
    """Main text of a job's page (and its JobPosting data, if any), kept between the fetch
    and extract tasks."""

    __tablename__ = "fetched_page"

    job_id: str = Field(foreign_key="job.id", primary_key=True)
    final_url: str | None = None
    title: str | None = None
    text: str | None = None  # None: login wall, blocked, or not HTML; use the post text
    note: str | None = None  # why there is no text
    structured_json: str | None = None  # schema.org JobPosting embedded in the page
    fetched_at: str


class MatchResult(SQLModel, table=True):
    """A provider's answers for one job against one version of the profile. Scores are
    computed from these by code, so new weights need no new provider calls."""

    __tablename__ = "match_result"

    job_id: str = Field(foreign_key="job.id", primary_key=True)
    profile_version: str = Field(primary_key=True)
    provider: str
    answers_json: str  # MatchAnswers
    created_at: str


class StatusChange(SQLModel, table=True):
    """Every Status you set in Notion, with when JobRadar noticed: your application funnel."""

    __tablename__ = "status_change"

    id: int | None = Field(default=None, primary_key=True)
    job_id: str = Field(foreign_key="job.id", index=True)
    status: str
    changed_at: str


class FeedBoard(SQLModel, table=True):
    """A company job board JobRadar watches, and what it saw there last time."""

    __tablename__ = "feed_board"

    key: str = Field(primary_key=True)  # "greenhouse:stripe"
    seen_json: str | None = None  # posting ids at the last good check; None: never checked
    jobs_on_board: int = 0  # all postings at the last good check, before filters
    new_total: int = 0  # postings JobRadar passed on since it started watching
    last_checked: str | None = None
    last_ok: str | None = None
    last_error: str | None = None
    misses: int = 0  # real 404s in a row; other errors never count
    skip_until: str | None = None  # a board that 404s 3 times is retried after 30 days
    latest_json: str | None = None  # newest posting seen: title, location, verdict, when


class PageState(StrEnum):
    NEW = "new"  # never read
    WATCHING = "watching"
    BACKING_OFF = "backing_off"  # the site refused (403/429); next read after wait_until
    PAUSED = "paused"  # refused 3 times in a row, or needs a login: `jobradar pages resume`
    BLOCKED = "blocked"  # robots.txt disallows it: not read until `jobradar pages resume`


class WatchedPage(SQLModel, table=True):
    """A web page JobRadar watches for new announcements, and its text at the last read."""

    __tablename__ = "watch_page"

    url: str = Field(primary_key=True)
    name: str
    state: str = PageState.NEW.value
    text: str | None = None  # cleaned main text at the last good read; None: no baseline yet
    last_checked: str | None = None
    last_ok: str | None = None
    last_changed: str | None = None  # last read that posted something
    last_error: str | None = None
    refusals: int = 0  # 403/429 in a row; other errors never count
    wait_until: str | None = None
    posts_total: int = 0


class Setting(SQLModel, table=True):
    """Small bits of state that outlive a run, e.g. the Notion poll cursor."""

    __tablename__ = "setting"

    key: str = Field(primary_key=True)
    value: str


class Alert(SQLModel, table=True):
    """At most one email alert per job, ever."""

    __tablename__ = "alert"

    job_id: str = Field(foreign_key="job.id", primary_key=True)
    queued_at: str
    sent_at: str | None = None
    outcome: str | None = None  # sent | dry_run | capped (daily limit reached)


class LLMCache(SQLModel, table=True):
    __tablename__ = "llm_cache"

    key: str = Field(primary_key=True)  # sha1(model|prompt_version|input)
    output: str
    cost_inr: float | None = None
    created_at: str


class Role(StrEnum):
    USER = "user"
    ADMIN = "admin"


class Plan(SQLModel, table=True):
    """What a plan allows per day (SAASPLAN.md section 17). Rows, so numbers change without a
    release."""

    __tablename__ = "plans"

    id: str = Field(primary_key=True)  # free | pro
    llm_calls_per_day: int
    resume_builds_per_day: int
    emails_per_day: int
    followed_sources_max: int | None = None  # None: no limit
    api_tokens_max: int


PLANS = (
    Plan(id="free", llm_calls_per_day=100, resume_builds_per_day=5, emails_per_day=20,
         followed_sources_max=50, api_tokens_max=2),
    Plan(id="pro", llm_calls_per_day=1000, resume_builds_per_day=50, emails_per_day=100,
         followed_sources_max=None, api_tokens_max=10),
)  # fmt: skip


class User(SQLModel, table=True):
    """Someone who signs in to the SaaS. Firebase knows who they are (`auth_uid`); everything
    else in JobRadar refers to `id`, so the sign-in provider can change."""

    __tablename__ = "users"
    __table_args__ = (CheckConstraint(_in("role", Role), name="users_role"),)

    id: str = Field(primary_key=True)  # random uuid, hex
    auth_uid: str = Field(unique=True)
    email: str | None = None  # None when they sign in with a phone number
    phone: str | None = None
    name: str | None = None
    role: str = Role.USER.value
    plan_id: str = Field(foreign_key="plans.id", default="free")
    created_at: str
    last_seen_at: str | None = None
    deleted_at: str | None = None  # switched off by the admin: sign-in is refused
