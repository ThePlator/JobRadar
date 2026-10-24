"""Resumes and cover letters: asking for one, following a build, and building one now."""

from __future__ import annotations

from dataclasses import dataclass

from jobradar.db.models import Artifact, Job
from jobradar.pipeline.task_types import BUILD_RESUME
from jobradar.services.context import Ctx
from jobradar.services.errors import NotFound, Refused


@dataclass(frozen=True)
class Progress:
    count: int  # resumes built for the job so far
    building: bool  # one is queued or being built


def request(ctx: Ctx, job_id: str) -> str:
    """Queue a new resume for a job; returns what to tell the user."""
    job = ctx.repo.get_job(job_id)
    if job is None:
        raise NotFound(f"No job {job_id!r}.")
    if not ctx.settings.config.resume.enabled:
        raise Refused("Resumes are off (resume.enabled in config.yaml).")
    ctx.tasks.enqueue(BUILD_RESUME, job.id, {"why": "manual"})
    return f"Building a resume for {job.role or 'this job'}…"


def progress(ctx: Ctx, job_id: str) -> Progress:
    return Progress(len(ctx.repo.artifacts_of(job_id)), ctx.repo.task_waiting(BUILD_RESUME, job_id))


def find_job(ctx: Ctx, prefix: str) -> Job:
    """The one job whose id starts with `prefix` (the first 8 characters are enough)."""
    matches = ctx.repo.jobs_with_prefix(prefix.strip().lower())
    if not matches:
        raise NotFound(f"No job with ID {prefix!r}.")
    if len(matches) > 1:
        raise Refused(f"{len(matches)} jobs start with {prefix!r}; give more characters.")
    job = ctx.repo.get_job(matches[0])
    if job is None:
        raise NotFound(f"No job with ID {prefix!r}.")
    return job


async def build_now(
    ctx: Ctx, job: Job, tailored: bool = True, template: str | None = None
) -> Artifact:
    """Build a new version right away (the CLI's `jobradar resume <id>`). Tailoring needs an
    LLM; without one, or with `tailored=False`, the profile is used as written."""
    from jobradar import app as application
    from jobradar.llm import LLM
    from jobradar.resume.build import ResumeBuilder

    use_llm = tailored and application.llm_enabled(ctx.settings)
    llm = LLM(ctx.settings, ctx.repo) if use_llm else None
    builder = ResumeBuilder(ctx.settings, ctx.repo, ctx.tasks, llm)
    return await builder.build_job(job.id, tailored=tailored, template=template)
