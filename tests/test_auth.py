"""Signing in (the SaaS, SAASPLAN.md PR 4): Firebase tokens checked with test keys, accounts made
on first sign-in, and the dashboard locked behind a session."""

import json
import re
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from google.auth import crypt
from google.auth import jwt as google_jwt
from pydantic import ValidationError
from sqlalchemy import text

from jobradar.config import AppConfig, AuthConfig, ConfigError, Secrets, Settings
from jobradar.db.repo import Repo
from jobradar.saas.auth import Firebase, Identity, SignInError
from jobradar.services import users
from jobradar.services.context import Ctx
from jobradar.services.errors import Refused
from jobradar.web.app import create_app
from jobradar.web.signin import CSRF, SESSION
from tests.conftest import FakeClock
from tests.test_web import add

PROJECT = "jobradar-test"
OWNER = "owner@example.com"

# ---- Firebase tokens, checked with test keys ------------------------------------------------


def make_key(name: str) -> tuple[str, str]:
    """A private key and a self-signed certificate for it, like Google's signing keys."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = time.time()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime.fromtimestamp(now - 3600, UTC))
        .not_valid_after(datetime.fromtimestamp(now + 3600, UTC))
        .sign(key, hashes.SHA256())
    )
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return pem, cert.public_bytes(serialization.Encoding.PEM).decode()


GOOGLE_KEY, GOOGLE_CERT = make_key("google")
OTHER_KEY, _ = make_key("someone else")


def sign(claims: dict[str, Any], key: str = GOOGLE_KEY) -> str:
    signer = crypt.RSASigner.from_string(key, key_id="test-kid")
    return google_jwt.encode(signer, claims).decode()


def claims(kind: str = "id", **changes: Any) -> dict[str, Any]:
    now = int(time.time())
    issuer = {
        "id": "https://securetoken.google.com/",
        "cookie": "https://session.firebase.google.com/",
    }
    return {
        "iss": issuer[kind] + PROJECT, "aud": PROJECT, "sub": "uid-1", "iat": now - 10,
        "exp": now + 3600, "auth_time": now - 10, "email": OWNER, "email_verified": True,
        "firebase": {"sign_in_provider": "google.com"}, **changes,
    }  # fmt: skip


@pytest.fixture
def firebase(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Firebase:
    monkeypatch.delenv("FIREBASE_AUTH_EMULATOR_HOST", raising=False)
    monkeypatch.setattr(
        "google.oauth2.id_token._fetch_certs", lambda request, url: {"test-kid": GOOGLE_CERT}
    )
    key_file = tmp_path / "firebase-admin.json"
    key_file.write_text(json.dumps({
        "type": "service_account", "project_id": PROJECT, "private_key_id": "test",
        "private_key": GOOGLE_KEY, "client_email": f"admin@{PROJECT}.iam.gserviceaccount.com",
        "client_id": "1", "token_uri": "https://oauth2.googleapis.com/token",
    }))  # fmt: skip
    return Firebase(key_file, PROJECT)


def test_a_fresh_sign_in_is_accepted(firebase: Firebase) -> None:
    who = firebase.identity(sign(claims()))
    assert (who.uid, who.email, who.email_verified, who.way) == ("uid-1", OWNER, True, "google.com")


@pytest.mark.parametrize(
    ("token", "says"),
    [
        (sign(claims(exp=int(time.time()) - 60, iat=int(time.time()) - 3700)), "expired"),
        (sign(claims(aud="another-project")), "isn't one JobRadar accepts"),
        (sign(claims(iss="https://securetoken.google.com/another-project")), "isn't one"),
        (sign(claims(), key=OTHER_KEY), "isn't one JobRadar accepts"),  # not signed by Google
        (sign(claims("cookie")), "isn't one JobRadar accepts"),  # a session cookie, not a sign-in
        ("not-a-token", "isn't one JobRadar accepts"),
        (sign(claims(auth_time=int(time.time()) - 600)), "too old"),  # signed in 10 minutes ago
    ],
)
def test_bad_sign_ins_are_refused(firebase: Firebase, token: str, says: str) -> None:
    with pytest.raises(SignInError, match=says):
        firebase.identity(token)


def test_sessions_end_when_signed_out_or_switched_off(
    firebase: Firebase, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = SimpleNamespace(disabled=False, tokens_valid_after_timestamp=0)
    asked: list[str] = []

    def get_user(uid: str, app: object = None) -> SimpleNamespace:
        asked.append(uid)
        return record

    monkeypatch.setattr("firebase_admin.auth.get_user", get_user)
    cookie = sign(claims("cookie"))
    assert firebase.session(cookie).uid == "uid-1"
    assert firebase.session(cookie).uid == "uid-1"
    assert asked == ["uid-1"]  # asked Firebase once, not on every page
    with pytest.raises(SignInError, match="Sign in to continue"):
        firebase.session(sign(claims()))  # a sign-in token isn't a session cookie
    firebase._checked.clear()
    record.tokens_valid_after_timestamp = (int(time.time()) + 5) * 1000  # signed out everywhere
    with pytest.raises(SignInError, match="signed out"):
        firebase.session(cookie)
    record.tokens_valid_after_timestamp, record.disabled = 0, True
    with pytest.raises(SignInError, match="switched off"):
        firebase.session(cookie)


def test_the_emulator_setting_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIREBASE_AUTH_EMULATOR_HOST", "localhost:9099")
    with pytest.raises(RuntimeError, match="anyone could sign in"):
        Firebase(tmp_path / "key.json", PROJECT)


# ---- accounts --------------------------------------------------------------------------------


def who(uid: str = "uid-1", email: str | None = OWNER, verified: bool = True) -> Identity:
    now = int(time.time())
    return Identity(uid=uid, email=email, email_verified=verified, phone=None, name="Sam",
                    way="google.com", auth_time=now, issued_at=now, admin=False)  # fmt: skip


def test_the_first_sign_in_makes_an_account(repo: Repo, clock: FakeClock) -> None:
    here = Ctx(repo, auth_settings(), clock())
    owner = users.sign_in(here, who(), OWNER.upper())  # the address's case doesn't matter
    assert (owner.role, owner.plan_id, owner.email) == ("admin", "free", OWNER)
    assert users.sign_in(here, who(), OWNER).id == owner.id  # the same account next time
    stranger = users.sign_in(here, who("uid-2", OWNER, verified=False), OWNER)
    assert stranger.role == "user"  # an address nobody checked could be anyone's
    phone = users.sign_in(here, replace(who("uid-3", None), phone="+919876543210"), OWNER)
    assert (phone.role, phone.phone) == ("user", "+919876543210")
    assert not users.can_open_dashboard(phone) and users.can_open_dashboard(owner)


def test_switched_off_accounts_cant_sign_in(repo: Repo, clock: FakeClock) -> None:
    here = Ctx(repo, auth_settings(), clock())
    user = users.sign_in(here, who("uid-9", "x@example.com"), OWNER)
    with repo.engine.begin() as c:
        c.execute(
            text("UPDATE users SET deleted_at = '2026-10-25' WHERE id = :id"), {"id": user.id}
        )
    with pytest.raises(Refused, match="switched off"):
        users.sign_in(here, who("uid-9", "x@example.com"), OWNER)


def test_signing_in_needs_the_firebase_settings() -> None:
    with pytest.raises(ValidationError, match="FIREBASE_API_KEY"):
        AuthConfig(enabled=True)
    s = auth_settings(secrets={})
    with pytest.raises(ConfigError, match="FIREBASE_CREDENTIALS, JOBRADAR_ADMIN_EMAIL"):
        s.require("auth")


# ---- the dashboard behind sign-in ------------------------------------------------------------


def auth_settings(secrets: dict[str, Any] | None = None) -> Settings:
    return Settings(
        config=AppConfig.model_validate({"sources": {"telegram": {"enabled": False}}}),
        auth=AuthConfig(enabled=True, firebase_api_key="AIza-test",
                        firebase_auth_domain=f"{PROJECT}.firebaseapp.com",
                        firebase_project_id=PROJECT),
        secrets=Secrets.model_validate(
            {"firebase_credentials": "config/firebase-admin.json",
             "jobradar_admin_email": OWNER} if secrets is None else secrets
        ),
    )  # fmt: skip


EMAILS = {"uid-1": OWNER, "uid-2": "friend@example.com"}


class FakeAuth:
    """Firebase, for the pages: a token is the uid and email it stands for."""

    def __init__(self) -> None:
        self.ended: list[str] = []
        self.admins: list[str] = []
        self.refuse_sessions = False

    def identity(self, id_token: str) -> Identity:
        if id_token == "bad":  # noqa: S105
            raise SignInError("That sign-in isn't one JobRadar accepts. Sign in again.")
        return who(id_token, EMAILS.get(id_token))

    def start_session(self, id_token: str, days: int) -> str:
        return f"session-{id_token}"

    def session(self, cookie: str) -> Identity:
        if self.refuse_sessions or not cookie.startswith("session-"):
            raise SignInError("You signed out of JobRadar. Sign in again.")
        return self.identity(cookie.removeprefix("session-"))

    def end_sessions(self, uid: str) -> None:
        self.ended.append(uid)

    def make_admin(self, uid: str) -> None:
        self.admins.append(uid)


@pytest.fixture
def fake() -> FakeAuth:
    return FakeAuth()


@pytest.fixture
def site(repo: Repo, clock: FakeClock, fake: FakeAuth) -> TestClient:
    app = create_app(auth_settings(), repo, clock, auth=fake)
    return TestClient(app, base_url="http://127.0.0.1:8080", follow_redirects=False)


def sign_in_as(site: TestClient, id_token: str) -> Any:
    page = site.get("/signin")
    csrf = re.search(r'data-csrf="([^"]+)"', page.text)
    assert csrf is not None
    return site.post("/api/v1/session", json={"id_token": id_token, "next": "/jobs"},
                     headers={"x-jobradar-csrf": csrf.group(1)})  # fmt: skip


def page_token(html: str) -> str:
    found = re.search(r'name="jobradar-token" content="([^"]+)"', html)
    assert found is not None
    return found.group(1)


def test_every_page_needs_signing_in(site: TestClient) -> None:
    gone = site.get("/applications?view=board")
    assert gone.status_code == 303
    assert gone.headers["location"] == "/signin?next=%2Fapplications%3Fview%3Dboard"
    assert site.get("/api/search?q=x").status_code == 401  # scripts get an answer, not a page
    assert site.get("/api/v1/health").json() == {"ok": True}
    assert site.get("/static/app.css").status_code == 200


def test_the_sign_in_page(site: TestClient) -> None:
    page = site.get("/signin?next=//evil.example")
    assert page.status_code == 200
    assert 'data-api-key="AIza-test"' in page.text and 'data-next="/"' in page.text
    assert "Continue with Google" in page.text and "data-phone-form" in page.text
    csp = page.headers["content-security-policy"]
    assert "https://www.gstatic.com/firebasejs/" in csp
    assert f"frame-src https://{PROJECT}.firebaseapp.com" in csp
    assert "'unsafe-inline'" not in csp and "frame-ancestors 'none'" in csp
    assert CSRF in page.headers["set-cookie"] and "HttpOnly" in page.headers["set-cookie"]


def test_the_owner_signs_in_and_out(site: TestClient, fake: FakeAuth, repo: Repo) -> None:
    add(repo, "j1")
    done = sign_in_as(site, "uid-1")
    assert done.status_code == 200 and done.json() == {"next": "/jobs"}
    cookie = done.headers["set-cookie"]
    assert f"{SESSION}=session-uid-1" in cookie and "HttpOnly" in cookie
    assert "samesite=lax" in cookie.lower() and "Max-Age=1209600" in cookie  # 14 days
    assert fake.admins == ["uid-1"]  # marked as the admin in Firebase too
    today = site.get("/")
    assert today.status_code == 200 and "Sign out" in today.text

    # Buttons carry a token tied to this session: the old per-computer token no longer works.
    token = page_token(today.text)
    assert site.post("/jobs/j1/act", data={"action": "applied", "token": "old"}).status_code == 403
    moved = site.post("/jobs/j1/act", data={"action": "applied", "token": token})
    assert moved.status_code == 303

    out = site.post("/signout", data={"token": token})
    assert out.status_code == 303 and out.headers["location"] == "/signin"
    assert fake.ended == ["uid-1"]
    assert site.get("/").status_code == 303


def test_others_wait_until_their_dashboard_is_ready(site: TestClient, fake: FakeAuth) -> None:
    assert sign_in_as(site, "uid-2").status_code == 200
    assert fake.admins == []
    waiting = site.get("/jobs")
    assert waiting.status_code == 403 and "You're on the list" in waiting.text
    assert "friend@example.com" in waiting.text
    assert site.get("/api/search?q=x").status_code == 403  # no one else's jobs, by any door
    assert (
        site.post("/signout", data={"token": page_token_from_form(waiting.text)}).status_code == 303
    )


def page_token_from_form(html: str) -> str:
    found = re.search(r'name="token" value="([^"]+)"', html)
    assert found is not None
    return found.group(1)


def test_sign_in_needs_the_page_it_started_on(site: TestClient) -> None:
    site.get("/signin")
    no_csrf = site.post("/api/v1/session", json={"id_token": "uid-3"})
    assert no_csrf.status_code == 403 and "Reload" in no_csrf.json()["message"]
    page = site.get("/signin")
    csrf = re.search(r'data-csrf="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]
    elsewhere = site.post("/api/v1/session", json={"id_token": "uid-3"},
                          headers={"x-jobradar-csrf": csrf, "origin": "https://evil.example"})  # fmt: skip
    assert elsewhere.status_code == 403
    bad = sign_in_as(site, "bad")
    assert bad.status_code == 401 and "Sign in again" in bad.json()["message"]
    assert SESSION not in bad.headers.get("set-cookie", "")


def test_a_session_ended_elsewhere_signs_you_out(site: TestClient, fake: FakeAuth) -> None:
    sign_in_as(site, "uid-1")
    assert site.get("/").status_code == 200
    fake.refuse_sessions = True  # e.g. signed out on the phone
    again = site.get("/")
    assert again.status_code == 303 and again.headers["location"].startswith("/signin")
    assert f'{SESSION}=""' in again.headers["set-cookie"]


def test_local_dashboard_needs_no_sign_in(repo: Repo, clock: FakeClock) -> None:
    from tests.test_digest import settings

    local = TestClient(create_app(settings(), repo, clock), base_url="http://127.0.0.1:8080")
    assert local.get("/").status_code == 200
    assert local.get("/signin").status_code == 404


def test_sign_in_settings_come_from_the_env_file(tmp_path: Path) -> None:
    """config.yaml stays the same on main and saas/main; signing in is set in .env."""
    env = tmp_path / ".env"
    env.write_text(
        "AUTH_ENABLED=true\nAUTH_WAYS=google, phone\nFIREBASE_API_KEY=AIza-test\n"
        f"FIREBASE_AUTH_DOMAIN={PROJECT}.firebaseapp.com\nFIREBASE_PROJECT_ID={PROJECT}\n"
    )
    auth = AuthConfig(_env_file=env)  # type: ignore[call-arg]
    assert auth.enabled and auth.ways == ["google", "phone"] and auth.session_days == 14
    assert not AuthConfig(_env_file=None).enabled  # type: ignore[call-arg]  (off by default)
