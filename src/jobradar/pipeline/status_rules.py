"""What a status change may do, wherever you make it: an email button or the dashboard.

Quick actions (the buttons) only move a job forward, so an old email or a second click can't
undo a later step: "Not interested" works on a job you haven't applied to, "I applied" on one
you haven't moved past Applied. "Change status" (the dashboard's menu, like Notion's Status)
may set any status you can choose, for fixing a mistake.

Every change is recorded in the status history, and Notion is updated by a queued
`notion_status` task, so a slow or offline Notion never holds up the change itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from jobradar.db.models import Job, JobStatus
from jobradar.db.repo import Repo
from jobradar.pipeline.task_types import BUILD_RESUME, NOTION_STATUS
from jobradar.queue.tasks import TaskQueue
from jobradar.sinks.notion_sync import FOLLOWED_UP

Action = Literal["shortlist", "applied", "skip", "followed up", "interview", "offer", "rejected"]

_BEFORE_APPLYING = (JobStatus.DISCOVERED, JobStatus.EXTRACTED, JobStatus.NEW,
                    JobStatus.SHORTLISTED, JobStatus.RESUME_READY, JobStatus.HIDDEN)  # fmt: skip

# What each action may change: the job's current status must be one of these.
FROM: dict[str, set[str]] = {
    "shortlist": {s.value for s in (JobStatus.DISCOVERED, JobStatus.EXTRACTED, JobStatus.NEW,
                                    JobStatus.HIDDEN, JobStatus.SKIPPED)},
    "applied": {s.value for s in (*_BEFORE_APPLYING, JobStatus.SKIPPED)},
    "skip": {s.value for s in _BEFORE_APPLYING},
    "followed up": {JobStatus.APPLIED.value},
    "interview": {s.value for s in (JobStatus.NEW, JobStatus.SHORTLISTED,
                                    JobStatus.RESUME_READY, JobStatus.APPLIED)},
    "offer": {s.value for s in (JobStatus.APPLIED, JobStatus.INTERVIEW)},
    "rejected": {s.value for s in (JobStatus.NEW, JobStatus.SHORTLISTED, JobStatus.RESUME_READY,
                                   JobStatus.APPLIED, JobStatus.INTERVIEW)},
}  # fmt: skip
STATUS: dict[str, JobStatus] = {
    "shortlist": JobStatus.SHORTLISTED, "applied": JobStatus.APPLIED,
    "skip": JobStatus.SKIPPED, "interview": JobStatus.INTERVIEW, "offer": JobStatus.OFFER,
    "rejected": JobStatus.REJECTED,
}  # fmt: skip
# The statuses you can choose ("Change status"), in Notion's words.
YOURS: dict[JobStatus, str] = {
    JobStatus.NEW: "New", JobStatus.SHORTLISTED: "Shortlisted", JobStatus.APPLIED: "Applied",
    JobStatus.INTERVIEW: "Interview", JobStatus.OFFER: "Offer", JobStatus.REJECTED: "Rejected",
    JobStatus.SKIPPED: "Skipped", JobStatus.HIDDEN: "Hidden",
}  # fmt: skip
DONE_WORDS = {
    "shortlist": "Shortlisted", "applied": "Marked as applied", "skip": "Skipped",
    "followed up": "Follow-up noted", "interview": "Moved to Interview", "offer": "Moved to Offer",
    "rejected": "Marked as rejected",
}  # fmt: skip


def notion_option(status: JobStatus) -> str:
    """The Notion Status option for one of your statuses ("Resume ready" shows as Shortlisted)."""
    if status == JobStatus.RESUME_READY:
        return "Shortlisted"
    return YOURS.get(status, status.value.capitalize())


def can(action: str, job: Job) -> bool:
    return job.status in FROM.get(action, set())


@dataclass(frozen=True)
class Outcome:
    ok: bool
    action: str
    before: str  # the status before, so the dashboard can offer Undo
    after: str
    message: str


def _title(job: Job) -> str:
    role = (job.role or "Role not stated")[:50]
    return f"{role} @ {job.company[:40]}" if job.company else role


def act(
    repo: Repo, queue: TaskQueue, job: Job, action: str, today: date, build_resumes: bool = True
) -> Outcome:
    """Apply a quick action (a button), only if it moves the job forward."""
    if action not in FROM:
        return Outcome(False, action, job.status, job.status, f"Unknown action “{action}”.")
    if not can(action, job):
        now = YOURS.get(JobStatus(job.status), job.status) if job.status else "unknown"
        return Outcome(
            False, action, job.status, job.status,
            f"Not changed: {_title(job)} is {now} now. Use Change status on its page instead.",
        )  # fmt: skip
    if action == "followed up":
        day = today.isoformat()
        repo.set_user_setting(FOLLOWED_UP + job.id, day)
        queue.enqueue(NOTION_STATUS, job.id, {"followed_up": day})
        return Outcome(True, action, job.status, job.status, f"Follow-up noted for {_title(job)}.")
    after = STATUS[action]
    repo.set_user_status(job.id, after)
    queue.enqueue(NOTION_STATUS, job.id, {"status": notion_option(after)})
    if build_resumes and after == JobStatus.SHORTLISTED and repo.latest_artifact(job.id) is None:
        queue.enqueue(BUILD_RESUME, job.id, {"why": "shortlisted"})
    return Outcome(True, action, job.status, after.value, f"{DONE_WORDS[action]}: {_title(job)}.")


def set_status(repo: Repo, queue: TaskQueue, job: Job, status: str) -> Outcome:
    """Change status to any of yours (the dashboard's menu): for fixing a mistake."""
    target = next((st for st in YOURS if st.value == status), None)
    if target is None:
        return Outcome(False, "set", job.status, job.status, f"“{status}” isn't a status you set.")
    if job.status == target.value and job.user_status_at is not None:
        return Outcome(True, "set", job.status, job.status, f"Already {YOURS[target]}.")
    repo.set_user_status(job.id, target)
    queue.enqueue(NOTION_STATUS, job.id, {"status": notion_option(target)})
    return Outcome(True, "set", job.status, target.value, f"Now {YOURS[target]}: {_title(job)}.")
