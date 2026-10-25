"""The read half of the REST API (SAASPLAN.md section 14, PR 6): everything the pages show, as
JSON, for whoever is signed in. Mounted at /api/v1 by the web app, behind the same sign-in
check, so a script with a session (and, from PR 10, an API token) can read it.

Ids of jobs are the catalog's, so anyone may look a job up; everything about it that is yours
(score, status, history, resumes) comes from your rows only, and someone else's resume is not
found. Errors are problem details (RFC 9457); lists page with an opaque `cursor`.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable
from datetime import timedelta
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import AfterValidator
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from jobradar.api import schemas as s
from jobradar.by_source import by_source
from jobradar.clock import to_iso
from jobradar.config import Preferences, Settings
from jobradar.db.models import OWNER
from jobradar.db.repo import Repo
from jobradar.queue.tasks import TaskQueue
from jobradar.services import applications as applications_svc
from jobradar.services import digests as digests_svc
from jobradar.services import jobs as jobs_svc
from jobradar.services import notify as notify_svc
from jobradar.services import profile as profile_svc
from jobradar.services import search as fts
from jobradar.services import skills as skills_svc
from jobradar.services.context import Ctx
from jobradar.sinks.followups import FollowUpReminder

PROBLEM = "application/problem+json"
TITLES = {400: "That request doesn't look right", 401: "Sign in first", 403: "Not allowed",
          404: "Not found", 422: "Some values aren't valid"}  # fmt: skip
NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": s.Problem, "description": "Not found (also another user's)"}
}

Limit = Annotated[int, Query(ge=1, le=100, description="Items per page, up to 100")]
Cursor = Annotated[str | None, Query(description="`next_cursor` from the page before")]


def _window(days: int) -> int:
    if days not in (7, 30, 90):
        raise ValueError("days must be 7, 30 or 90")
    return days


Days = Annotated[int, Query(description="Look back 7, 30 or 90 days"), AfterValidator(_window)]


def problem(status: int, detail: str | None = None, title: str | None = None) -> JSONResponse:
    body = s.Problem(title=title or TITLES.get(status, "Something went wrong"), status=status,
                     detail=detail)  # fmt: skip
    return JSONResponse(body.model_dump(), status_code=status, media_type=PROBLEM)


def cursor_for(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": offset}).encode()).decode().rstrip("=")


def offset_of(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        offset = int(json.loads(raw)["o"])
    except (binascii.Error, ValueError, KeyError, TypeError):
        raise HTTPException(400, "That cursor isn't one this API gave out.") from None
    if offset < 0:
        raise HTTPException(400, "That cursor isn't one this API gave out.")
    return offset


def build_api(*, settings: Settings, repo: Repo, ctx_for: Callable[[Request], Ctx]) -> FastAPI:
    api = FastAPI(
        title="JobRadar API", version="1", openapi_url="/openapi.json", docs_url=None,
        redoc_url=None,
        description="Your jobs, applications, resumes and skills, as JSON. Sign in on the site "
        "first; requests carry the session cookie. Errors are problem details (RFC 9457).",
    )  # fmt: skip

    @api.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> Response:
        return problem(exc.status_code, exc.detail if isinstance(exc.detail, str) else None)

    @api.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError) -> Response:
        where = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'][1:])}: {e['msg']}" for e in exc.errors()
        )
        return problem(422, where)

    def job_or_404(ctx: Ctx, job_id: str) -> Any:
        job = ctx.repo.get_job(job_id)
        if job is None:
            raise HTTPException(404, "No job with that id.")
        return job

    def items(ctx: Ctx, ids: list[str]) -> list[s.JobItem]:
        return [s.JobItem.of(j) for i in ids if (j := ctx.repo.get_job(i)) is not None]

    # ---- you ------------------------------------------------------------------------------

    @api.get("/me", response_model=s.Me, tags=["account"])
    def me(request: Request) -> s.Me:
        """Who you're signed in as, and your plan."""
        uid = ctx_for(request).user_id
        user = repo.get_user(uid)  # accounts are read with the app's own rights
        if user is None and uid != OWNER:
            raise HTTPException(404, "No account.")
        plan = repo.get_plan(user.plan_id if user else "free")
        return s.Me(
            id=uid, email=user.email if user else None, phone=user.phone if user else None,
            name=user.name if user else None, role=user.role if user else "admin",
            plan=s.Plan(**plan.model_dump()) if plan else None,
        )  # fmt: skip

    @api.get("/profile", response_model=dict[str, Any], responses=NOT_FOUND, tags=["account"])
    def profile(request: Request) -> dict[str, Any]:
        """Your profile, in the same shape as profile.yaml."""
        found = profile_svc.profile(ctx_for(request))
        if found is None:
            raise HTTPException(404, "You haven't added your profile yet.")
        return found

    @api.get("/preferences", response_model=Preferences, tags=["account"])
    def preferences(request: Request) -> Preferences:
        """Your filters, score thresholds, digest times and timezone."""
        return ctx_for(request).prefs

    # ---- jobs -----------------------------------------------------------------------------

    @api.get("/jobs", response_model=s.JobPage, tags=["jobs"])
    def jobs(
        request: Request,
        q: Annotated[str, Query(max_length=100, description="Words to find anywhere")] = "",
        status: Annotated[list[str] | None, Query(description="Repeat for several")] = None,
        min_score: Annotated[int | None, Query(ge=0, le=100)] = None,
        within: Annotated[int | None, Query(ge=1, le=365, description="First seen in days")] = None,
        place: Annotated[str, Query(max_length=100)] = "",
        remote: bool = False,
        source: Annotated[int | None, Query(description="An id from /sources")] = None,
        sort: Literal["new", "old", "score", "relevance"] = "new",
        cursor: Cursor = None,
        limit: Limit = 50,
    ) -> s.JobPage:
        """Jobs, newest first unless sorted otherwise. Without `status`, every status."""
        ctx = ctx_for(request)
        offset = offset_of(cursor)
        query = fts.match(q, place, remote)
        if query:
            fts.refresh(ctx.repo, ctx.now)
        elif sort == "relevance":
            sort = "new"
        since = to_iso(ctx.now - timedelta(days=within)) if within else None
        found, total = ctx.repo.find_jobs(
            status, query, min_score, since, source, sort, limit, offset
        )
        page = [s.JobItem.of(job, str(fts.snippet(raw)) if raw else None) for job, raw in found]
        more = offset + len(page) < total
        return s.JobPage(items=page, total=total,
                         next_cursor=cursor_for(offset + len(page)) if more else None)  # fmt: skip

    @api.get("/jobs/{job_id}", response_model=s.JobDetail, responses=NOT_FOUND, tags=["jobs"])
    def job(request: Request, job_id: str) -> s.JobDetail:
        """One job: the posting, your score and reasons, where it was posted, your status
        history and the resumes built for you."""
        ctx = ctx_for(request)
        found = job_or_404(ctx, job_id)
        posting = s.posting_of(found)
        artifacts = ctx.repo.artifacts_of(job_id)
        return s.JobDetail(
            **s.JobItem.of(found).model_dump(),
            reasons=found.score_reason,
            summary=posting.summary if posting else "",
            employment_type=posting.employment_type if posting else "unknown",
            experience_min=posting.experience_min if posting else None,
            experience_max=posting.experience_max if posting else None,
            batch_years=posting.batch_years if posting else [],
            degrees=posting.degrees if posting else [],
            skills_required=posting.skills_required if posting else [],
            skills_preferred=posting.skills_preferred if posting else [],
            salary=posting.salary_text if posting else None,
            scam_risk=found.scam_risk,
            posted_in=[
                s.Sighting(
                    platform=x.platform,
                    source=x.chat_title or x.chat_id,
                    posted_at=x.posted_at,
                    text=x.text,
                )
                for x in ctx.repo.job_sightings(job_id)
            ],
            resumes=[
                s.Resume.of(a, jobs_svc.artifact_file(settings, a, True) is not None)
                for a in artifacts
            ],
            history=[
                s.StatusChange(status=st, at=at) for st, at in ctx.repo.status_history(job_id)
            ],
        )

    @api.get("/jobs/{job_id}/history", response_model=list[s.StatusChange],
             responses=NOT_FOUND, tags=["jobs"])  # fmt: skip
    def history(request: Request, job_id: str) -> list[s.StatusChange]:
        """Every status you set on this job, oldest first."""
        ctx = ctx_for(request)
        job_or_404(ctx, job_id)
        return [s.StatusChange(status=st, at=at) for st, at in ctx.repo.status_history(job_id)]

    @api.get("/jobs/{job_id}/resumes", response_model=list[s.Resume], responses=NOT_FOUND,
             tags=["resumes"])  # fmt: skip
    def resumes(request: Request, job_id: str) -> list[s.Resume]:
        """Your resumes and cover letters for a job, newest first."""
        ctx = ctx_for(request)
        job_or_404(ctx, job_id)
        return [s.Resume.of(a, jobs_svc.artifact_file(settings, a, True) is not None)
                for a in ctx.repo.artifacts_of(job_id)]  # fmt: skip

    @api.get("/files/{artifact_id}", response_class=FileResponse, responses=NOT_FOUND,
             tags=["resumes"])  # fmt: skip
    def file(request: Request, artifact_id: int, cover: bool = False) -> FileResponse:
        """A resume's PDF, or with `cover=true` its cover letter. Only your own."""
        artifact = ctx_for(request).repo.get_artifact(artifact_id)
        path = jobs_svc.artifact_file(settings, artifact, cover) if artifact else None
        if path is None:
            raise HTTPException(404, "No such file.")
        return FileResponse(path, media_type="application/pdf", filename=path.name)

    @api.get("/jobs/{job_id}/followup-draft", response_model=s.Draft, responses=NOT_FOUND,
             tags=["follow-ups"])  # fmt: skip
    def followup_draft(request: Request, job_id: str) -> s.Draft:
        """The follow-up JobRadar would send for a job you applied to."""
        ctx = ctx_for(request)
        found = job_or_404(ctx, job_id)
        draft = FollowUpReminder(settings, ctx.repo, TaskQueue(ctx.repo)).draft_for(found)
        if draft is None:
            raise HTTPException(404, "No follow-up for this job: you haven't applied to it.")
        return s.Draft(subject=draft.subject, body=draft.body, to=draft.to)

    @api.get("/search", response_model=s.JobPage, tags=["jobs"])
    def search(
        request: Request,
        q: Annotated[str, Query(min_length=1, max_length=100)],
        cursor: Cursor = None,
        limit: Limit = 20,
    ) -> s.JobPage:
        """Full-text search over every job, best match first, with highlighted snippets."""
        return jobs(
            request,
            q=q,
            status=None,
            min_score=None,
            within=None,
            place="",
            remote=False,
            source=None,
            sort="relevance",
            cursor=cursor,
            limit=limit,
        )

    # ---- applications ---------------------------------------------------------------------

    @api.get("/applications", response_model=list[s.Column], tags=["applications"])
    def applications(request: Request) -> list[s.Column]:
        """Your applications board: one column per stage."""
        ctx = ctx_for(request)
        columns = []
        for col in applications_svc.board(ctx):
            items_ = []
            for bc in col.cards:
                job = ctx.repo.get_job(bc.card.id)
                if job is None:
                    continue
                t = bc.track
                items_.append(s.BoardItem(
                    **s.JobItem.of(job).model_dump(), resume_built=t.resume,
                    company_replied=t.replied, follow_ups_sent=t.reminders,
                    next_follow_up_in_days=t.nudge_in,
                ))  # fmt: skip
            columns.append(s.Column(key=col.key, title=col.title, items=items_))
        return columns

    @api.get("/applications/by-source", response_model=s.BySource, tags=["applications"])
    def applications_by_source(request: Request) -> s.BySource:
        """Where your applications came from, and which reached the next round."""
        report = by_source(ctx_for(request).repo)
        return s.BySource(
            applications=report.applications, next_round=report.next_round,
            sources=[
                s.SourceRow(name=r.name, platform=r.platform, applied=r.applied,
                            next_round=r.next_round,
                            jobs=[s.SourceApplication(job_id=a.job_id, outcome=a.outcome,
                                                      found_here_first=a.first) for a in r.jobs])
                for r in report.rows
            ],
        )  # fmt: skip

    @api.get("/followups", response_model=list[s.FollowUp], tags=["follow-ups"])
    def followups(request: Request) -> list[s.FollowUp]:
        """Every job you applied to, and where its follow-ups stand."""
        return [
            s.FollowUp(
                job=s.JobItem.of(f.job),
                applied=to_iso(f.applied),
                since=to_iso(f.since),
                sent=f.sent,
                next_at=to_iso(f.next_at),
                state=f.state,
            )
            for f in notify_svc.followups(ctx_for(request))
        ]

    # ---- skills, digests, sources ---------------------------------------------------------

    def skill(row: Any) -> s.Skill:
        return s.Skill(key=row.key, name=row.name, jobs=row.jobs, share=row.share,
                       trend=row.trend, covered=row.covered, covered_by=row.covered_by,
                       sample_job_ids=[c.id for c in row.samples])  # fmt: skip

    @api.get("/skills", response_model=s.Skills, tags=["skills"])
    def skills(request: Request, days: Days = 30) -> s.Skills:
        """The skills your good matches ask for: the ones you lack and the ones you have."""
        ctx = ctx_for(request)
        page = skills_svc.skills_page(ctx, profile_svc.profile(ctx), days)
        return s.Skills(days=page.days, looked_at=page.looked_at, min_score=page.min_score,
                        missing=[skill(r) for r in page.missing],
                        have=[skill(r) for r in page.have], warnings=page.warnings)  # fmt: skip

    @api.get("/skills/{key}", response_model=s.SkillDetail, responses=NOT_FOUND, tags=["skills"])
    def skill_detail(request: Request, key: str, days: Days = 30) -> s.SkillDetail:
        """What exactly your good matches want of one skill, in their own words."""
        ctx = ctx_for(request)
        page = skills_svc.skill_detail_page(ctx, profile_svc.profile(ctx), days, key)
        if page is None:
            raise HTTPException(404, "None of your good matches named that skill.")
        return s.SkillDetail(
            key=page.key, name=page.name, days=page.days, looked_at=page.looked_at,
            asked=page.asked, needed=page.needed, counts=dict(page.counts), verdict=page.verdict,
            start=page.start,
            topics=[s.Topic(label=t.label, jobs=t.jobs, job_ids=[c.id for c in t.cards])
                    for t in page.topics],
            quotes=[s.Quote(job_id=q.card.id, kind=q.kind, label=q.label, html=str(q.html))
                    for q in page.quotes],
            missing=page.missing,
        )  # fmt: skip

    def window(w: Any) -> s.Window:
        return s.Window(key=w.key, label=w.label, since=to_iso(w.since), until=to_iso(w.until),
                        current=w.current)  # fmt: skip

    @api.get("/digests", response_model=list[s.Window], tags=["digests"])
    def digests(request: Request) -> list[s.Window]:
        """Your digests: the one being gathered now (`now`), then each one sent, newest first."""
        ctx = ctx_for(request)
        return [window(w) for w in digests_svc.windows(ctx.now.astimezone(ctx.tz),
                                                       ctx.prefs.digest_times)]  # fmt: skip

    @api.get("/digests/{key}", response_model=s.Digest, responses=NOT_FOUND, tags=["digests"])
    def digest(request: Request, key: str) -> s.Digest:
        """One digest, by a `key` from /digests."""
        ctx = ctx_for(request)
        keys = {w.key for w in digests_svc.windows(ctx.now.astimezone(ctx.tz),
                                                   ctx.prefs.digest_times)}  # fmt: skip
        if key not in keys:
            raise HTTPException(404, "No digest with that key. GET /digests lists them.")
        page = digests_svc.digest(ctx, key, profile_svc.profile(ctx))
        return s.Digest(
            window=window(page.window), title=page.title, new=page.new, strong=page.strong,
            alerts=page.alerts, inbox=items(ctx, [c.id for c in page.inbox]),
            closing=items(ctx, [c.id for c in page.closing]),
            from_boards=items(ctx, [b.card.id for b in page.boards]),
            hidden=[s.HiddenCount(reason=r, jobs=n) for r, n in page.hidden],
            pipeline=page.pipeline, spent=page.spent, failed=page.failed,
        )  # fmt: skip

    @api.get("/sources", response_model=list[s.Source], tags=["jobs"])
    def sources(request: Request) -> list[s.Source]:
        """Every Telegram chat, company board and page that posted a job."""
        return [s.Source(id=i, platform=p, title=t) for i, p, t in ctx_for(request).repo.sources()]

    # ---- system ---------------------------------------------------------------------------

    @api.get("/ready", response_model=s.Health, tags=["system"])
    def ready() -> Response:
        """Ready to serve: the database answers. 503 when it doesn't."""
        try:
            with repo.engine.connect() as c:
                c.execute(text("SELECT 1"))
        except Exception:
            return problem(503, "The database isn't answering.", "Not ready")
        return JSONResponse({"ok": True})

    return api
