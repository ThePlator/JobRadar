"""The dashboard's web app: pages for today's matches, your applications, every job, and one
job, read from JobRadar's database. Read-only for now: statuses still change from the email
buttons or Notion.

It runs on this computer only. A browser sends requests to 127.0.0.1 from any site you have
open, so requests naming any other host are refused (that stops a site posing as localhost,
"DNS rebinding"), and pages may not be framed or load anything from elsewhere.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from collections.abc import Awaitable, Callable
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
from jobradar.config import ConfigError, Settings
from jobradar.db.repo import Repo
from jobradar.pipeline import status_rules as rules
from jobradar.pipeline.task_types import BUILD_RESUME
from jobradar.profile import add_skill, load_profile
from jobradar.queue.tasks import TaskQueue
from jobradar.sinks.digest import build_digest, gather
from jobradar.web import views

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


def safe_next(path: str | None) -> str:
    """Where to go back to after a form post: a path on this dashboard, never another site."""
    if path and path.startswith("/") and not path.startswith("//") and "\\" not in path:
        return path
    return "/"


def create_app(
    settings: Settings,
    repo: Repo,
    clock: Clock = utcnow,
    queue: TaskQueue | None = None,
    wake: Callable[[], None] | None = None,
) -> FastAPI:
    config = settings.config
    tz = config.notify.tz
    queue = queue or TaskQueue(repo, clock)
    nudge = wake or (lambda: None)  # inside `jobradar run`: start queued work now
    token = page_token(repo)
    hosts = allowed_hosts(config.web.host, config.web.url)
    app = FastAPI(title="JobRadar", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    @app.middleware("http")
    async def _headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        email_csp = response.headers.get("Content-Security-Policy") == EMAIL_CSP
        response.headers.update(HEADERS)
        if email_csp:
            response.headers["Content-Security-Policy"] = EMAIL_CSP
        if request.url.path.startswith("/files/") and response.status_code == 200:
            # A PDF, shown by the browser's own viewer: the page policy can stop that viewer.
            del response.headers["Content-Security-Policy"]
            del response.headers["X-Frame-Options"]
        static = request.url.path.startswith("/static/")
        # Static files are versioned by asset(): cache them, but check back before reuse.
        response.headers["Cache-Control"] = "no-cache" if static else "no-store"
        return response

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    good = config.scoring.alert_above
    templates.env.globals.update(
        good=good, fair=config.scoring.hide_below, asset=asset, token=token,
        statuses=list(rules.YOURS.items()), notion=config.notion.enabled,
    )  # fmt: skip
    cache: dict[str, Any] = {"mtime": None, "profile": None}

    def current_profile() -> dict[str, Any] | None:
        """profile.yaml, read again whenever the file changes (e.g. "I know this")."""
        path = config.matching.profile_path
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        if cache["mtime"] != mtime:
            cache["mtime"], cache["profile"] = mtime, your_profile(settings)
        return cache["profile"]  # type: ignore[no-any-return]

    profile = current_profile()
    basics = profile.get("basics") if profile else None
    name = str(basics.get("name") or "") if isinstance(basics, dict) else ""

    def render(
        request: Request, page: str, nav: str, status_code: int = 200, **context: object
    ) -> HTMLResponse:
        now = clock()
        ov = views.overview(repo, good, now, tz, name)
        return templates.TemplateResponse(
            request, page, {"nav": nav, "ov": ov, "now": now, **context}, status_code
        )

    @app.exception_handler(StarletteHTTPException)
    async def _not_found(request: Request, exc: StarletteHTTPException) -> Response:
        return render(request, "error.html", "", exc.status_code, status=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _bad_request(request: Request, exc: RequestValidationError) -> Response:
        return render(request, "error.html", "", 400, status=400)

    @app.get("/", response_class=HTMLResponse)
    def today(request: Request) -> HTMLResponse:
        now = clock()
        cards = views.today(repo, good, now, tz)
        return render(
            request, "today.html", "today", cards=cards, days=views.TODAY_DAYS,
            greeting=views.greeting(now, tz, name),
            says=views.blip_says(cards, good),
            pulse=views.pulse(repo, good, now, tz),
            sources=views.sources(repo, good, now),
            filters=views.QUICK_FILTERS,
        )  # fmt: skip

    @app.get("/applications", response_class=HTMLResponse)
    def applications(request: Request) -> HTMLResponse:
        columns = views.board(repo, settings, clock())
        return render(request, "applications.html", "applications", columns=columns,
                      quiet_after=config.notify.followup_after_days,
                      funnel=views.funnel(repo, good))  # fmt: skip

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
        filters = views.Filters(
            min_score=min if min in dict(views.MIN_SCORES) and min else None,
            within=within if within in dict(views.WITHIN) and within else None,
            place=place.strip()[:60],
            remote=bool(remote),
            source=source or None,
        )
        found = views.all_jobs(repo, tz, show, q, page, clock(), sort or "", filters)
        return render(request, "jobs.html", "jobs", found=found, choices=views.SHOW_CHOICES,
                      sorts=views.SORTS, min_scores=views.MIN_SCORES, withins=views.WITHIN,
                      sources=repo.sources())  # fmt: skip

    @app.get("/digest", response_class=HTMLResponse)
    def digest(request: Request, w: str = "") -> HTMLResponse:
        page = views.digest(repo, settings, clock(), w, current_profile())
        return render(request, "digest.html", "digest", d=page)

    @app.get("/digest/email", response_class=HTMLResponse)
    def digest_email(w: str = "") -> HTMLResponse:
        """The digest exactly as the email looks. Email HTML is styled inline, so this one
        page may use inline styles; it still runs no script and loads nothing."""
        notify = config.notify
        local = clock().astimezone(tz)
        options = views.windows(local, notify.digest_times)
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
        detail = views.job_detail(
            repo, job_id, tz, clock(), config.matching.weights, current_profile(), settings
        )
        if detail is None:
            raise HTTPException(status_code=404)
        return render(request, "job.html", "", job=detail)

    # ---- changing a status ----------------------------------------------------------------

    async def checked_form(request: Request) -> dict[str, str]:
        """The posted fields, after checking the request came from one of our pages."""
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).hostname not in {h.strip("[]") for h in hosts}:
            raise HTTPException(status_code=403)
        body = (await request.body())[:4096].decode("utf-8", "replace")
        fields = {k: v[0] for k, v in parse_qs(body).items()}
        sent = request.headers.get("x-jobradar-token") or fields.get("token", "")
        if not hmac.compare_digest(sent.encode(), token.encode()):
            raise HTTPException(status_code=403)
        return fields

    def answer(request: Request, outcome: rules.Outcome, job_id: str, back: str) -> Response:
        if request.headers.get("x-jobradar-token"):  # the page's own script: it updates itself
            undo = outcome.before if outcome.before in {s.value for s in rules.YOURS} else (
                "shortlisted" if outcome.before == "resume_ready" else "new")  # fmt: skip
            job = repo.get_job(job_id)
            row = ""
            if job is not None and outcome.ok:
                row = templates.get_template("_row.html").render(
                    c=views.card(job, tz, clock()), with_status=True
                )
            return JSONResponse(
                {"ok": outcome.ok, "message": outcome.message, "status": outcome.after,
                 "label": views.label(outcome.after),
                 "undo": undo if outcome.ok and outcome.before != outcome.after else None,
                 "row": row},
                status_code=200 if outcome.ok else 409,
            )  # fmt: skip
        return RedirectResponse(safe_next(back), status_code=303)

    @app.post("/jobs/{job_id}/act")
    async def act(request: Request, job_id: str) -> Response:
        fields = await checked_form(request)
        job = repo.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404)
        today = clock().astimezone(tz).date()
        outcome = rules.act(repo, queue, job, fields.get("action", ""), today)
        nudge()
        log.info("your dashboard action", extra={"job_id": job_id[:10], "ok": outcome.ok,
                                                 "action": outcome.action})  # fmt: skip
        return answer(request, outcome, job_id, fields.get("next", ""))

    @app.post("/jobs/{job_id}/status")
    async def set_status(request: Request, job_id: str) -> Response:
        fields = await checked_form(request)
        job = repo.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404)
        outcome = rules.set_status(repo, queue, job, fields.get("status", ""))
        nudge()
        log.info("your dashboard status", extra={"job_id": job_id[:10], "status": outcome.after})
        return answer(request, outcome, job_id, fields.get("next", ""))

    # ---- resumes and cover letters ---------------------------------------------------------

    @app.get("/files/{artifact_id}")
    def file(artifact_id: int, cover: int = 0) -> FileResponse:
        """A resume (or the cover letter beside it) by its id in the database. The path comes
        from the database, never the address, and must be a PDF inside storage.local_dir."""
        artifact = repo.get_artifact(artifact_id)
        path = views.artifact_file(settings, artifact, bool(cover)) if artifact else None
        if path is None:
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type="application/pdf",
                            content_disposition_type="inline", filename=path.name)  # fmt: skip

    @app.post("/jobs/{job_id}/resume")
    async def build_resume(request: Request, job_id: str) -> Response:
        fields = await checked_form(request)
        job = repo.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404)
        if not config.resume.enabled:
            message, ok = "Resumes are off (resume.enabled in config.yaml).", False
        else:
            queue.enqueue(BUILD_RESUME, job.id, {"why": "manual"})
            nudge()
            message, ok = f"Building a resume for {views.card(job, tz).role}…", True
        log.info("your dashboard resume", extra={"job_id": job_id[:10], "ok": ok})
        if request.headers.get("x-jobradar-token"):
            return JSONResponse({"ok": ok, "message": message,
                                 "count": len(repo.artifacts_of(job.id))},
                                status_code=200 if ok else 409)  # fmt: skip
        return RedirectResponse(safe_next(fields.get("next")), status_code=303)

    @app.get("/api/jobs/{job_id}/resumes")
    def resumes(job_id: str) -> JSONResponse:
        """How many resumes a job has and whether one is being built: the page polls this."""
        return JSONResponse({
            "count": len(repo.artifacts_of(job_id)),
            "building": repo.task_waiting(BUILD_RESUME, job_id),
        })  # fmt: skip

    # ---- skills ------------------------------------------------------------------------------

    @app.get("/skills", response_class=HTMLResponse)
    def skills(request: Request, days: int = 30) -> HTMLResponse:
        page = views.skills_page(repo, settings, current_profile(), clock(), days)
        return render(request, "skills.html", "skills", s=page)

    @app.get("/skills/{key}", response_class=HTMLResponse)
    def skill(request: Request, key: str, days: int = 30) -> HTMLResponse:
        page = views.skill_detail_page(repo, settings, current_profile(), clock(), days, key)
        if page is None:
            raise HTTPException(status_code=404)
        return render(request, "skill.html", "skills", s=page)

    @app.post("/skills/know")
    async def know(request: Request) -> Response:
        """ "I know this": add a skill to profile.yaml, so matching counts it."""
        fields = await checked_form(request)
        name = fields.get("skill", "")
        stamp = clock().astimezone(tz).strftime("%Y%m%d-%H%M%S")
        try:
            backup = add_skill(config.matching.profile_path, name, stamp)
        except ConfigError as e:
            ok, message = False, str(e)
        else:
            ok = True
            message = (f"Added {name} to your profile (backup: {backup}). Matching counts it "
                       "from the next restart of jobradar run.")  # fmt: skip
            log.info("your dashboard skill", extra={"skill": name[:60]})
        if request.headers.get("x-jobradar-token"):
            return JSONResponse({"ok": ok, "message": message}, status_code=200 if ok else 409)
        return RedirectResponse(safe_next(fields.get("next")), status_code=303)

    @app.get("/api/search")
    def api_search(q: str = "") -> JSONResponse:
        return JSONResponse({"jobs": views.search(repo, q)})

    return app


def your_profile(settings: Settings) -> dict[str, Any] | None:
    """profile.yaml, for Blip's greeting and the skills you have; None when it can't be read."""
    try:
        return load_profile(settings.config.matching.profile_path)
    except ConfigError:
        return None
