"""Changing a job's status, wherever the change comes from: a dashboard button, "Change
status", an email button, or (later) the API. The rules themselves live in
`pipeline/status_rules.py`; this is the one way in."""

from __future__ import annotations

from datetime import date

from jobradar.db.models import Job
from jobradar.pipeline import status_rules as rules
from jobradar.services.context import Ctx
from jobradar.services.errors import NotFound


def _job(ctx: Ctx, job_id: str) -> Job:
    job = ctx.repo.get_job(job_id)
    if job is None:
        raise NotFound(f"No job {job_id!r}.")
    return job


def act(
    ctx: Ctx, job_id: str, action: str, build_resumes: bool = True, day: date | None = None
) -> rules.Outcome:
    """A quick action (I applied, Shortlist, Not interested, ...): only moves a job forward.
    `day` is when it happened (an email button's send date); default today."""
    job = _job(ctx, job_id)
    return rules.act(ctx.repo, ctx.tasks, job, action, day or ctx.today, build_resumes)


def set_status(ctx: Ctx, job_id: str, status: str) -> rules.Outcome:
    """Any of your statuses ("Change status"), for fixing a mistake."""
    return rules.set_status(ctx.repo, ctx.tasks, _job(ctx, job_id), status)
