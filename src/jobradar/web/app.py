"""The dashboard's web app: pages for today's matches, your applications, every job, and one
job, read from JobRadar's database. Read-only for now: statuses still change from the email
buttons or Notion.

It runs on this computer only. A browser sends requests to 127.0.0.1 from any site you have
open, so requests naming any other host are refused (that stops a site posing as localhost,
"DNS rebinding"), and pages may not be framed or load anything from elsewhere.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from jobradar.clock import Clock, utcnow
from jobradar.config import ConfigError, Settings
from jobradar.db.repo import Repo
from jobradar.profile import load_profile
from jobradar.web import views

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


def allowed_hosts(host: str) -> list[str]:
    """Host names a request may carry: this computer's own, and the configured one."""
    return sorted({"127.0.0.1", "localhost", "[::1]", host})


def create_app(settings: Settings, repo: Repo, clock: Clock = utcnow) -> FastAPI:
    config = settings.config
    tz = config.notify.tz
    app = FastAPI(title="JobRadar", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts(config.web.host))

    @app.middleware("http")
    async def _headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.update(HEADERS)
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    good = config.scoring.alert_above
    templates.env.globals.update(good=good, fair=config.scoring.hide_below)
    profile = your_profile(settings)
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
        columns = views.board(repo, tz, clock())
        return render(request, "applications.html", "applications", columns=columns,
                      quiet_after=config.notify.followup_after_days,
                      funnel=views.funnel(repo, good))  # fmt: skip

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs(request: Request, show: str = "jobs", q: str = "", page: int = 1) -> HTMLResponse:
        found = views.all_jobs(repo, tz, show, q, page, clock())
        return render(request, "jobs.html", "jobs", found=found, choices=views.SHOW_CHOICES)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job(request: Request, job_id: str) -> HTMLResponse:
        detail = views.job_detail(repo, job_id, tz, clock(), config.matching.weights, profile)
        if detail is None:
            raise HTTPException(status_code=404)
        return render(request, "job.html", "", job=detail)

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
