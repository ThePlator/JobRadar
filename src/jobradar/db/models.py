"""SQLModel tables: source, raw_message, job, job_source, artifact, task, llm_cache.

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
    last_error: str | None = None


class FetchedPage(SQLModel, table=True):
    """Main text of a job's page, kept between the fetch and extract tasks."""

    __tablename__ = "fetched_page"

    job_id: str = Field(foreign_key="job.id", primary_key=True)
    final_url: str | None = None
    title: str | None = None
    text: str | None = None  # None: login wall, blocked, or not HTML; use the post text
    note: str | None = None  # why there is no text
    fetched_at: str


class LLMCache(SQLModel, table=True):
    __tablename__ = "llm_cache"

    key: str = Field(primary_key=True)  # sha1(model|prompt_version|input)
    output: str
    cost_inr: float | None = None
    created_at: str
