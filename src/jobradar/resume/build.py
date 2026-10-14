"""job -> tailored, grounded, one-page PDF in `storage.local_dir`, linked from Notion.

A `build_resume` task (key: job id) is queued when a job first scores at or above
`scoring.auto_resume_above` (at most `resume.max_per_day` a day), when you set a job to
Shortlisted in Notion and it has no resume yet, and by `jobradar resume <job>`.

1. profile.yaml is read fresh (edits apply to the next resume) and checked for ids.
2. The writer model selects and rephrases (`resume/tailor.py`); `resume/validate.py` undoes
   anything not backed by the profile. With no LLM key, or `resume.tailor: false`, the
   profile is used as written.
3. Rendered with your template and compiled. Over `resume.max_pages`: the lowest-ranked
   bullet goes and it compiles again, up to 3 times. If the tailored version won't compile,
   the plain one is tried once.
4. The PDF and its .tex are saved as {Company}_{Role}_{yyyymmdd}_v{n}; Notion's Resume
   column links to the newest version. The job's Status stays as it is.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

from jobradar.clock import Clock, to_iso, utcnow
from jobradar.config import ConfigError, Settings
from jobradar.db.models import Artifact
from jobradar.db.repo import Repo, TaskRecord
from jobradar.llm import LLM, SchemaError
from jobradar.pipeline.extract import PAGE_TEXT_LIMIT, own_post
from jobradar.pipeline.schemas import JobPosting
from jobradar.pipeline.task_types import NOTION_UPSERT
from jobradar.profile import load_profile, resume_problems
from jobradar.providers.base import JobView
from jobradar.queue.tasks import PermanentError, TaskQueue
from jobradar.resume.compile import CompileError, compile_pdf
from jobradar.resume.render import ResumeContent, content_from_profile, render
from jobradar.resume.tailor import tailor
from jobradar.resume.validate import ground, page_count, trim_one
from jobradar.storage.local import resume_path

log = logging.getLogger(__name__)

MAX_TRIMS = 3


def _copy(content: ResumeContent) -> ResumeContent:
    """Deep enough to trim bullets without touching the original."""
    return replace(
        content,
        experience=[replace(i, bullets=list(i.bullets), bullet_ids=list(i.bullet_ids))
                    for i in content.experience],
        projects=[replace(i, bullets=list(i.bullets), bullet_ids=list(i.bullet_ids))
                  for i in content.projects],
    )  # fmt: skip


class ResumeBuilder:
    def __init__(
        self,
        settings: Settings,
        repo: Repo,
        queue: TaskQueue,
        llm: LLM | None,
        clock: Clock = utcnow,
    ) -> None:
        self.settings = settings
        self.config = settings.config.resume
        self.repo = repo
        self.queue = queue
        self.llm = llm
        self.clock = clock

    def room_today(self) -> bool:
        """Automatic builds left today (local day)."""
        local = self.clock().astimezone(self.settings.config.notify.tz)
        midnight = to_iso(local.replace(hour=0, minute=0, second=0, microsecond=0))
        return self.repo.artifacts_since(midnight) < self.config.max_per_day

    async def build(self, task: TaskRecord) -> None:
        why = str(task.payload.get("why", "auto"))
        if why == "auto" and not self.room_today():
            log.info("resume skipped: daily limit reached", extra={"job_id": task.key[:10]})
            return
        if why != "manual" and self.repo.latest_artifact(task.key) is not None:
            return  # built already; `jobradar resume <job>` makes a new version
        await self.build_job(task.key)

    async def build_job(
        self, job_id: str, tailored: bool | None = None, template: str | None = None
    ) -> Artifact:
        job = self.repo.get_job(job_id)
        if job is None:
            raise PermanentError(f"no job {job_id}")
        path = self.settings.config.matching.profile_path
        try:
            profile = load_profile(path)
        except ConfigError as e:
            raise PermanentError(str(e)) from e
        if problems := resume_problems(profile):
            raise PermanentError(f"{path}: " + "; ".join(problems))
        plain = content_from_profile(profile)
        content, violations, used_llm = plain, [], False
        if (self.config.tailor if tailored is None else tailored) and self.llm is not None:
            try:
                result = await tailor(
                    self.llm, self.settings.config.llm.writer_model, profile, self._view(job_id)
                )
                grounded = ground(plain, result, profile, self.config.banned_phrases)
                content, violations, used_llm = grounded.content, grounded.violations, True
            except SchemaError as e:
                violations = [f"tailoring failed, profile used as written: {e}"[:300]]
        for v in violations:
            log.info("resume check", extra={"job_id": job_id[:10], "fixed": v[:200]})

        version = self.repo.next_version(job_id)
        pdf = resume_path(
            self.settings.config.storage.local_dir, job.company, job.role,
            self.clock().astimezone(self.settings.config.notify.tz).date(), version,
        )  # fmt: skip
        pages, content = await self._compile(content, plain, pdf, template, used_llm)
        record = self._record(content, violations, used_llm, pages)
        artifact = self.repo.save_artifact(job_id, "resume", version, str(pdf), json.dumps(record))
        self.queue.enqueue(NOTION_UPSERT, job_id)
        log.info(
            "resume built",
            extra={"job_id": job_id[:10], "path": str(pdf), "pages": pages,
                   "tailored": used_llm, "fixed": len(violations)},
        )  # fmt: skip
        return artifact

    async def _compile(
        self, content: ResumeContent, plain: ResumeContent, pdf: Path, template: str | None,
        tailored: bool,
    ) -> tuple[int, ResumeContent]:  # fmt: skip
        template = template or self.config.template
        content = _copy(content)
        try:
            render(content, template)
        except ValueError as e:
            raise PermanentError(str(e)) from e
        for attempt in range(MAX_TRIMS + 1):
            tex = render(content, template)
            try:
                await compile_pdf(tex, pdf)
            except CompileError as e:
                if tailored and attempt == 0:
                    log.warning("tailored resume didn't compile; trying your profile as written")
                    content, tailored = _copy(plain), False
                    continue
                raise PermanentError(str(e)) from e
            pdf.with_suffix(".tex").write_text(tex, encoding="utf-8")
            pages = page_count(pdf)
            if pages <= self.config.max_pages:
                return pages, content
            if attempt == MAX_TRIMS or not trim_one(content):
                log.warning("resume is longer than resume.max_pages",
                            extra={"pages": pages, "path": str(pdf)})  # fmt: skip
                return pages, content
        return page_count(pdf), content

    def _view(self, job_id: str) -> JobView:
        job = self.repo.get_job(job_id)
        posting = (
            JobPosting.model_validate_json(job.data_json)
            if job is not None and job.data_json
            else JobPosting()
        )
        page = self.repo.get_page(job_id)
        page_text = ((page.text or "") if page else "")[:PAGE_TEXT_LIMIT]
        return JobView(posting=posting, post_text=own_post(self.repo, job_id) or "",
                       page_text=page_text)  # fmt: skip

    @staticmethod
    def _record(
        content: ResumeContent, violations: list[str], tailored: bool, pages: int
    ) -> dict[str, Any]:
        return {
            "tailored": tailored,
            "pages": pages,
            "fixed": violations,
            "bullets": {i.id: i.bullet_ids for i in content.experience + content.projects},
            "skills": {label: skills for label, skills in content.skills},
            "summary": content.summary,
        }
