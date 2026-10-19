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

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from jobradar.clock import Clock, utcnow
from jobradar.config import Settings
from jobradar.db.repo import Repo
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
    templates.env.globals.update(good=config.scoring.alert_above, fair=config.scoring.hide_below)

    def render(request: Request, name: str, nav: str, **context: object) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"nav": nav, **context})

    @app.exception_handler(StarletteHTTPException)
    async def _not_found(request: Request, exc: StarletteHTTPException) -> Response:
        return templates.TemplateResponse(
            request, "error.html", {"nav": "", "status": exc.status_code}, exc.status_code
        )

    @app.exception_handler(RequestValidationError)
    async def _bad_request(request: Request, exc: RequestValidationError) -> Response:
        return templates.TemplateResponse(request, "error.html", {"nav": "", "status": 400}, 400)

    @app.get("/", response_class=HTMLResponse)
    def today(request: Request) -> HTMLResponse:
        cards = views.today(repo, config.scoring.alert_above, clock(), tz)
        return render(request, "today.html", "today", cards=cards, days=views.TODAY_DAYS)

    @app.get("/applications", response_class=HTMLResponse)
    def applications(request: Request) -> HTMLResponse:
        return render(request, "applications.html", "applications", columns=views.board(repo, tz))

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs(request: Request, show: str = "jobs", q: str = "", page: int = 1) -> HTMLResponse:
        found = views.all_jobs(repo, tz, show, q, page)
        return render(request, "jobs.html", "jobs", found=found, choices=views.SHOW_CHOICES)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job(request: Request, job_id: str) -> HTMLResponse:
        detail = views.job_detail(repo, job_id, tz)
        if detail is None:
            raise HTTPException(status_code=404)
        return render(request, "job.html", "", job=detail)

    return app
