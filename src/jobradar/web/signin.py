"""Signing in to the dashboard, for the SaaS (`auth.enabled`; SAASPLAN.md section 10.1).

The sign-in page uses Firebase's script to sign in with Google, an email and password, or a
phone number, then sends the token it gets to `POST /api/v1/session`. That checks the token
and answers with a session cookie (HttpOnly, so page scripts can't read it). Every other page
needs that cookie, and every form and button sends a token derived from it (CSRF), so another
site open in the browser can't act for you.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from jobradar.clock import Clock
from jobradar.config import Settings
from jobradar.db.models import User
from jobradar.db.repo import Repo
from jobradar.saas.auth import Auth, SignInError
from jobradar.services import users as users_svc
from jobradar.services.context import Ctx
from jobradar.services.errors import Refused

log = logging.getLogger(__name__)

SESSION = "jr_session"
CSRF = "jr_csrf"  # the sign-in page's own token, before there is a session
FIREBASE_JS = "https://www.gstatic.com/firebasejs/13.1.0/"
OPEN = {"/signin", "/api/v1/health"}  # pages anyone may open
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class NewSession(BaseModel):
    id_token: str = Field(min_length=1, max_length=8192)
    next: str = Field(default="/", max_length=2048)


def from_another_site(request: Request, hosts: list[str]) -> bool:
    """A browser names the page a request came from (Origin); refuse ones from elsewhere."""
    origin = request.headers.get("origin")
    return bool(origin) and urlsplit(origin).hostname not in {h.strip("[]") for h in hosts}


def safe_next(path: str | None) -> str:
    """Where to go after signing in or a form post: a path here, never another site."""
    if path and path.startswith("/") and not path.startswith("//") and "\\" not in path:
        return path
    return "/"


def page_csp(auth_domain: str) -> str:
    """The sign-in page may also run Firebase's and reCAPTCHA's scripts (phone sign-in), talk
    to Google's sign-in service, and show Google's sign-in and reCAPTCHA frames."""
    return (
        "default-src 'self'; img-src 'self' data: https://www.gstatic.com; style-src 'self'; "
        f"script-src 'self' {FIREBASE_JS} https://apis.google.com "
        "https://www.google.com/recaptcha/ https://www.gstatic.com/recaptcha/; "
        "connect-src 'self' https://identitytoolkit.googleapis.com "
        "https://securetoken.googleapis.com https://www.googleapis.com; "
        f"frame-src https://{auth_domain} https://www.google.com/recaptcha/ "
        "https://recaptcha.google.com/recaptcha/; "
        "frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
    )


def install(
    app: FastAPI,
    *,
    settings: Settings,
    repo: Repo,
    clock: Clock,
    auth: Auth,
    templates: Jinja2Templates,
    secret: str,
    hosts: list[str],
) -> Callable[[Request], str]:
    """Add the sign-in page, the session endpoints and the check every request passes. Returns
    the function giving a request's CSRF token."""
    cfg = settings.auth
    admin_email = settings.secrets.jobradar_admin_email

    def csrf(request: Request) -> str:
        cookie = request.cookies.get(SESSION, "")
        return hmac.new(secret.encode(), cookie.encode(), hashlib.sha256).hexdigest()[:32]

    def secure(request: Request) -> bool:
        # Plain http is only ever this computer; anywhere else (Tailscale, the site) is https.
        return request.url.scheme == "https" or request.url.hostname not in LOOPBACK

    def signed_out(request: Request, message: str = "Sign in first.") -> Response:
        response: Response
        if request.method == "GET" and not request.url.path.startswith("/api/"):
            back = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            response = RedirectResponse(f"/signin?next={quote(back, safe='')}", status_code=303)
        else:
            response = JSONResponse({"message": message}, status_code=401)
        response.delete_cookie(SESSION, path="/")
        return response

    def waiting(request: Request, user: User) -> Response:
        return templates.TemplateResponse(
            request, "waiting.html", {"user": user, "token": csrf(request)}, status_code=403
        )

    @app.middleware("http")
    async def _signed_in(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if path in OPEN or path.startswith("/static/"):
            return await call_next(request)
        if path == "/api/v1/session" and request.method == "POST":
            return await call_next(request)
        cookie = request.cookies.get(SESSION)
        if not cookie:
            return signed_out(request)
        try:
            who = await run_in_threadpool(auth.session, cookie)
        except SignInError as e:
            return signed_out(request, str(e))
        user = repo.user_by_auth_uid(who.uid)
        if user is None or user.deleted_at:
            return signed_out(request, "This account is switched off.")
        request.state.user = user
        if not users_svc.can_open_dashboard(user) and path not in {"/signout", "/api/v1/session"}:
            return waiting(request, user)
        return await call_next(request)

    @app.get("/signin")
    def signin_page(request: Request, next: str = "/") -> Response:
        token = secrets.token_urlsafe(24)
        response = templates.TemplateResponse(
            request, "signin.html",
            {"cfg": cfg, "csrf": token, "next": safe_next(next), "firebase_js": FIREBASE_JS},
            headers={"Content-Security-Policy": page_csp(cfg.firebase_auth_domain or "")},
        )  # fmt: skip
        response.set_cookie(
            CSRF, token, max_age=3600, path="/api/v1/session", secure=secure(request),
            httponly=True, samesite="strict",
        )  # fmt: skip
        return response

    @app.post("/api/v1/session")
    def start_session(request: Request, body: NewSession) -> Response:
        """Swap a fresh Firebase sign-in for a session cookie."""
        sent = request.headers.get("x-jobradar-csrf", "")
        kept = request.cookies.get(CSRF, "")
        if from_another_site(request, hosts) or not (
            sent and kept and hmac.compare_digest(sent.encode(), kept.encode())
        ):
            return JSONResponse(
                {"message": "This page is out of date. Reload it and sign in again."}, 403
            )
        try:
            who = auth.identity(body.id_token)
            user = users_svc.sign_in(Ctx(repo, settings, clock()), who, admin_email)
            if user.role == "admin" and not who.admin:
                auth.make_admin(who.uid)
            cookie = auth.start_session(body.id_token, cfg.session_days)
        except SignInError as e:
            return JSONResponse({"message": str(e)}, status_code=401)
        except Refused as e:
            return JSONResponse({"message": str(e)}, status_code=403)
        log.info("signed in", extra={"user": user.id[:8], "way": who.way, "role": user.role})
        response = JSONResponse({"next": safe_next(body.next)})
        response.set_cookie(
            SESSION, cookie, max_age=cfg.session_days * 86400, path="/", secure=secure(request),
            httponly=True, samesite="lax",
        )  # fmt: skip
        response.delete_cookie(CSRF, path="/api/v1/session")
        return response

    def end_session(request: Request) -> None:
        user: User = request.state.user
        auth.end_sessions(user.auth_uid)
        log.info("signed out", extra={"user": user.id[:8]})

    def sent_csrf(request: Request, form: dict[str, str] | None = None) -> bool:
        sent = request.headers.get("x-jobradar-token") or (form or {}).get("token", "")
        return not from_another_site(request, hosts) and hmac.compare_digest(
            sent.encode(), csrf(request).encode()
        )

    @app.delete("/api/v1/session")
    def delete_session(request: Request) -> Response:
        """Sign out on every device."""
        if not sent_csrf(request):
            return JSONResponse({"message": "Reload the page and try again."}, status_code=403)
        end_session(request)
        response = Response(status_code=204)
        response.delete_cookie(SESSION, path="/")
        return response

    @app.post("/signout")
    async def signout(request: Request) -> Response:
        """The sign-out button: the same, for a plain form."""
        body = (await request.body())[:4096].decode("utf-8", "replace")
        form = {k: v[0] for k, v in parse_qs(body).items()}
        if not sent_csrf(request, form):
            return JSONResponse({"message": "Reload the page and try again."}, status_code=403)
        await run_in_threadpool(end_session, request)
        response = RedirectResponse("/signin", status_code=303)
        response.delete_cookie(SESSION, path="/")
        return response

    @app.get("/api/v1/health")
    def health() -> JSONResponse:
        return JSONResponse({"ok": True})

    return csrf
