"""The dashboard's web app: pages for today's matches, your applications, every job, and one
job, read from JobRadar's database. Read-only for now: statuses still change from the email
buttons or Notion.

It runs on this computer only. A browser sends requests to 127.0.0.1 from any site you have
open, so requests naming any other host are refused (that stops a site posing as localhost,
"DNS rebinding"), and pages may not be framed or load anything from elsewhere.

For the SaaS (`auth.enabled`), every page needs signing in first: see `signin.py`.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from jobradar.clock import Clock, utcnow
from jobradar.config import Settings
from jobradar.db.repo import Repo
from jobradar.pipeline import status_rules as rules
from jobradar.queue.tasks import TaskQueue
from jobradar.saas.auth import Auth, Firebase
from jobradar.services import applications as applications_svc
from jobradar.services import cards as cards_svc
from jobradar.services import digests as digests_svc
from jobradar.services import home as home_svc
from jobradar.services import jobs as jobs_svc
from jobradar.services import profile as profile_svc
from jobradar.services import resumes as resumes_svc
from jobradar.services import skills as skills_svc
from jobradar.services import statuses as statuses_svc
from jobradar.services.context import Ctx
from jobradar.services.errors import NotFound, Refused
from jobradar.sinks.digest import build_digest, gather
from jobradar.web import signin
from jobradar.web.signin import from_another_site, safe_next

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


EMAIL_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


def asset(path: str) -> str:
    """'/static/app.js?v=3f9c1a2b7d': a new version of the file gets a new address, so the
    browser never keeps running an old one after an update."""
    digest = hashlib.sha256((HERE / "static" / path).read_bytes()).hexdigest()[:10]
    return f"/static/{path}?v={digest}"


def allowed_hosts(host: str, url: str | None = None) -> list[str]:
    """Host names a request may carry: this computer's own, the configured one, and the
    name in `web.url` (your Tailscale address, for the phone)."""
    names = {"127.0.0.1", "localhost", "[::1]", host}
    if url and (name := urlsplit(url).hostname):
        names.add(name)
    return sorted(names)


TOKEN_SETTING = "web_token"  # noqa: S105 (the setting's name, not a secret)


def page_token(repo: Repo) -> str:
    """The secret every button on the dashboard sends back. Only JobRadar's own pages carry
    it, so another site open in your browser can't change your jobs (cross-site requests)."""
    token = repo.get_setting(TOKEN_SETTING)
    if not token:
        token = secrets.token_urlsafe(24)
        repo.set_setting(TOKEN_SETTING, token)
    return token


def create_app(
    settings: Settings,
    repo: Repo,
    clock: Clock = utcnow,
    queue: TaskQueue | None = None,
    wake: Callable[[], None] | None = None,
    auth: Auth | None = None,
) -> FastAPI:
    config = settings.config
    queue = queue or TaskQueue(repo, clock)
    nudge = wake or (lambda: None)  # inside `jobradar run`: start queued work now
    token = page_token(repo)
    hosts = allowed_hosts(config.web.host, config.web.url)
    app = FastAPI(title="JobRadar", docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=HERE / "templates")
    csrf: Callable[[Request], str] = lambda request: token  # noqa: E731
    if settings.auth.enabled:
        if auth is None:
            settings.require("auth")
            assert settings.secrets.firebase_credentials is not None  # noqa: S101 (required)
            auth = Firebase(
                settings.secrets.firebase_credentials, settings.auth.firebase_project_id or ""
            )
        csrf = signin.install(
            app, settings=settings, repo=repo, clock=clock, auth=auth, templates=templates,
            secret=token, hosts=hosts,
        )  # fmt: skip
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)  # checked first

    @app.middleware("http")
    async def _headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        own_csp = response.headers.get("Content-Security-Policy")  # e.g. the emails, sign-in
        response.headers.update(HEADERS)
        if own_csp:
            response.headers["Content-Security-Policy"] = own_csp
        if request.url.path.startswith("/files/") and response.status_code == 200:
            # A PDF, shown by the browser's own viewer: the page policy can stop that viewer.
            del response.headers["Content-Security-Policy"]
            del response.headers["X-Frame-Options"]
        static = request.url.path.startswith("/static/")
        # Static files are versioned by asset(): cache them, but check back before reuse.
        response.headers["Cache-Control"] = "no-cache" if static else "no-store"
        return response

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    good = config.scoring.alert_above
    templates.env.globals.update(
        good=good, fair=config.scoring.hide_below, asset=asset, token=token,
        statuses=list(rules.YOURS.items()), notion=config.notion.enabled,
    )  # fmt: skip

    def ctx_for(request: Request, now: datetime | None = None) -> Ctx:
        """Who is asking (the signed-in user; on this computer, the owner), now, with the app's
        queue: what each request hands the services."""
        user = getattr(request.state, "user", None)
        if user is None:  # this computer only: the owner, as in the self-hosted app
            return Ctx(repo, settings, now or clock(), queue=queue)
        # Signed in: a repo that can only reach their rows (row-level security on Postgres).
        return Ctx(repo.scoped(user.id), settings, now or clock(), user_id=user.id, queue=queue)

    def current_profile(request: Request) -> dict[str, Any] | None:
        """The asking user's profile (the owner's: profile.yaml, as it is now)."""
        return profile_svc.profile(ctx_for(request))

    def first_name(request: Request) -> str:
        """For Blip's greeting: the profile's name, else the name you signed in with."""
        profile = current_profile(request)
        basics = profile.get("basics") if profile else None
        if isinstance(basics, dict) and basics.get("name"):
            return str(basics["name"])
        account = getattr(request.state, "user", None)
        return str(account.name or "") if account is not None else ""

    def render(
        request: Request, page: str, nav: str, status_code: int = 200, **context: object
    ) -> HTMLResponse:
        now = clock()
        ctx = ctx_for(request, now)
        ov = home_svc.overview(ctx, first_name(request))
        account = getattr(request.state, "user", None)  # signed in (the SaaS)
        return templates.TemplateResponse(
            request, page,
            {"nav": nav, "ov": ov, "now": now, "token": csrf(request), "account": account,
             "good": ctx.good, "fair": ctx.prefs.scoring.hide_below, **context},
            status_code,
        )  # fmt: skip

    @app.exception_handler(StarletteHTTPException)
    async def _not_found(request: Request, exc: StarletteHTTPException) -> Response:
        return render(request, "error.html", "", exc.status_code, status=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _bad_request(request: Request, exc: RequestValidationError) -> Response:
        return render(request, "error.html", "", 400, status=400)

    @app.get("/", response_class=HTMLResponse)
    def today(request: Request) -> HTMLResponse:
        now = clock()
        ctx = ctx_for(request, now)
        cards = jobs_svc.today(ctx)
        return render(
            request, "today.html", "today", cards=cards, days=cards_svc.TODAY_DAYS,
            greeting=home_svc.greeting(now, ctx.tz, first_name(request)),
            says=home_svc.blip_says(cards, ctx.good),
            pulse=home_svc.pulse(ctx),
            sources=home_svc.sources(ctx),
            filters=cards_svc.QUICK_FILTERS,
        )  # fmt: skip

    @app.get("/applications", response_class=HTMLResponse)
    def applications(request: Request) -> HTMLResponse:
        ctx = ctx_for(request)
        columns = applications_svc.board(ctx)
        return render(request, "applications.html", "applications", columns=columns,
                      quiet_after=config.notify.followup_after_days,
                      funnel=applications_svc.funnel(ctx),
                      by_source=applications_svc.source_strip(ctx))  # fmt: skip

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs(
        request: Request,
        show: str = "jobs",
        q: str = "",
        page: int = 1,
        sort: str = "",
        min: int = 0,
        within: int = 0,
        place: str = "",
        remote: str = "",
        source: int = 0,
    ) -> HTMLResponse:
        filters = jobs_svc.Filters(
            min_score=min if min in dict(jobs_svc.MIN_SCORES) and min else None,
            within=within if within in dict(jobs_svc.WITHIN) and within else None,
            place=place.strip()[:60],
            remote=bool(remote),
            source=source or None,
        )
        found = jobs_svc.all_jobs(ctx_for(request), show, q, page, sort or "", filters)
        return render(request, "jobs.html", "jobs", found=found, choices=cards_svc.SHOW_CHOICES,
                      sorts=jobs_svc.SORTS, min_scores=jobs_svc.MIN_SCORES, withins=jobs_svc.WITHIN,
                      sources=repo.sources())  # fmt: skip

    @app.get("/digest", response_class=HTMLResponse)
    def digest(request: Request, w: str = "") -> HTMLResponse:
        page = digests_svc.digest(ctx_for(request), w, current_profile(request))
        return render(request, "digest.html", "digest", d=page)

    @app.get("/digest/email", response_class=HTMLResponse)
    def digest_email(request: Request, w: str = "") -> HTMLResponse:
        """The digest exactly as the email looks. Email HTML is styled inline, so this one
        page may use inline styles; it still runs no script and loads nothing."""
        ctx = ctx_for(request)
        local = clock().astimezone(ctx.tz)
        options = digests_svc.windows(local, ctx.prefs.digest_times)
        window = next((x for x in options if x.key == w), options[1])
        data = gather(repo, settings, window.since, window.until, bounded=not window.current)
        sender = settings.secrets.email_address or "jobradar@localhost"
        msg = build_digest(data, sender, sender, settings.secrets.notion_database_id,
                           notion=config.notion.enabled)  # fmt: skip
        part = msg.get_body(("html",))
        html = str(part.get_content()) if part is not None else "<p>No HTML part.</p>"
        return HTMLResponse(html, headers={"Content-Security-Policy": EMAIL_CSP})

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job(request: Request, job_id: str) -> HTMLResponse:
        detail = jobs_svc.job_detail(ctx_for(request), job_id, current_profile(request))
        if detail is None:
            raise HTTPException(status_code=404)
        return render(request, "job.html", "", job=detail)

    # ---- changing a status ----------------------------------------------------------------

    async def checked_form(request: Request) -> dict[str, str]:
        """The posted fields, after checking the request came from one of our pages."""
        if from_another_site(request, hosts):
            raise HTTPException(status_code=403)
        body = (await request.body())[:4096].decode("utf-8", "replace")
        fields = {k: v[0] for k, v in parse_qs(body).items()}
        sent = request.headers.get("x-jobradar-token") or fields.get("token", "")
        if not hmac.compare_digest(sent.encode(), csrf(request).encode()):
            raise HTTPException(status_code=403)
        return fields

    def answer(request: Request, outcome: rules.Outcome, job_id: str, back: str) -> Response:
        if request.headers.get("x-jobradar-token"):  # the page's own script: it updates itself
            undo = outcome.before if outcome.before in {s.value for s in rules.YOURS} else (
                "shortlisted" if outcome.before == "resume_ready" else "new")  # fmt: skip
            ctx = ctx_for(request)
            job = ctx.repo.get_job(job_id)
            row = ""
            if job is not None and outcome.ok:
                row = templates.get_template("_row.html").render(
                    c=cards_svc.card(job, ctx.tz, clock()),
                    with_status=True,
                    token=csrf(request),
                    good=ctx.good,
                    fair=ctx.prefs.scoring.hide_below,
                )
            return JSONResponse(
                {"ok": outcome.ok, "message": outcome.message, "status": outcome.after,
                 "label": cards_svc.label(outcome.after),
                 "undo": undo if outcome.ok and outcome.before != outcome.after else None,
                 "row": row},
                status_code=200 if outcome.ok else 409,
            )  # fmt: skip
        return RedirectResponse(safe_next(back), status_code=303)

    @app.post("/jobs/{job_id}/act")
    async def act(request: Request, job_id: str) -> Response:
        fields = await checked_form(request)
        try:
            outcome = statuses_svc.act(ctx_for(request), job_id, fields.get("action", ""))
        except NotFound:
            raise HTTPException(status_code=404) from None
        nudge()
        log.info("your dashboard action", extra={"job_id": job_id[:10], "ok": outcome.ok,
                                                 "action": outcome.action})  # fmt: skip
        return answer(request, outcome, job_id, fields.get("next", ""))

    @app.post("/jobs/{job_id}/status")
    async def set_status(request: Request, job_id: str) -> Response:
        fields = await checked_form(request)
        try:
            outcome = statuses_svc.set_status(ctx_for(request), job_id, fields.get("status", ""))
        except NotFound:
            raise HTTPException(status_code=404) from None
        nudge()
        log.info("your dashboard status", extra={"job_id": job_id[:10], "status": outcome.after})
        return answer(request, outcome, job_id, fields.get("next", ""))

    # ---- resumes and cover letters ---------------------------------------------------------

    @app.get("/files/{artifact_id}")
    def file(request: Request, artifact_id: int, cover: int = 0) -> FileResponse:
        """A resume (or the cover letter beside it) by its id in the database. The path comes
        from the database, never the address, and must be a PDF inside storage.local_dir."""
        artifact = ctx_for(request).repo.get_artifact(artifact_id)  # only your own
        path = jobs_svc.artifact_file(settings, artifact, bool(cover)) if artifact else None
        if path is None:
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type="application/pdf",
                            content_disposition_type="inline", filename=path.name)  # fmt: skip

    @app.post("/jobs/{job_id}/resume")
    async def build_resume(request: Request, job_id: str) -> Response:
        fields = await checked_form(request)
        here = ctx_for(request)
        try:
            message, ok = resumes_svc.request(here, job_id), True
            nudge()
        except NotFound:
            raise HTTPException(status_code=404) from None
        except Refused as e:
            message, ok = str(e), False
        log.info("your dashboard resume", extra={"job_id": job_id[:10], "ok": ok})
        if request.headers.get("x-jobradar-token"):
            return JSONResponse({"ok": ok, "message": message,
                                 "count": resumes_svc.progress(here, job_id).count},
                                status_code=200 if ok else 409)  # fmt: skip
        return RedirectResponse(safe_next(fields.get("next")), status_code=303)

    @app.get("/api/jobs/{job_id}/resumes")
    def resumes(request: Request, job_id: str) -> JSONResponse:
        """How many resumes a job has and whether one is being built: the page polls this."""
        p = resumes_svc.progress(ctx_for(request), job_id)
        return JSONResponse({"count": p.count, "building": p.building})

    # ---- skills ------------------------------------------------------------------------------

    @app.get("/skills", response_class=HTMLResponse)
    def skills(request: Request, days: int = 30) -> HTMLResponse:
        page = skills_svc.skills_page(ctx_for(request), current_profile(request), days)
        return render(request, "skills.html", "skills", s=page)

    @app.get("/skills/{key}", response_class=HTMLResponse)
    def skill(request: Request, key: str, days: int = 30) -> HTMLResponse:
        page = skills_svc.skill_detail_page(ctx_for(request), current_profile(request), days, key)
        if page is None:
            raise HTTPException(status_code=404)
        return render(request, "skill.html", "skills", s=page)

    @app.post("/skills/know")
    async def know(request: Request) -> Response:
        """ "I know this": add a skill to profile.yaml, so matching counts it."""
        fields = await checked_form(request)
        name = fields.get("skill", "")
        try:
            backup = profile_svc.know_skill(ctx_for(request), name)
        except Refused as e:
            ok, message = False, str(e)
        else:
            ok = True
            kept = f" (backup: {backup})" if backup else ""
            message = (f"Added {name} to your profile{kept}. Matching counts it "
                       "from the next restart of jobradar run.")  # fmt: skip
            log.info("your dashboard skill", extra={"skill": name[:60]})
        if request.headers.get("x-jobradar-token"):
            return JSONResponse({"ok": ok, "message": message}, status_code=200 if ok else 409)
        return RedirectResponse(safe_next(fields.get("next")), status_code=303)

    @app.get("/api/search")
    def api_search(request: Request, q: str = "") -> JSONResponse:
        return JSONResponse({"jobs": jobs_svc.search(ctx_for(request), q)})

    return app
