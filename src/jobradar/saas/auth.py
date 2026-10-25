"""Signing in with Firebase Authentication (SAASPLAN.md section 10.1).

Firebase knows who someone is (Google, email and password, or a phone number); JobRadar keeps
everything else. This is the only module that talks to Firebase, so the provider can be
swapped by rewriting it (section 7.3). Tokens are checked on this server with Google's public
keys, which are cached, so most requests make no call to Firebase.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol

RECENT = 5 * 60  # a session starts only from a sign-in made in the last five minutes
RECHECK = 5 * 60  # how often a session is checked for sign-outs and switched-off accounts
SKEW = 10  # seconds of clock difference tolerated between Google and this server


class SignInError(Exception):
    """A token we don't accept. The message says what to do, for the person signing in."""


@dataclass(frozen=True)
class Identity:
    """Who a Firebase token says this is."""

    uid: str
    email: str | None
    email_verified: bool
    phone: str | None
    name: str | None
    way: str  # google.com | password | phone
    auth_time: int  # when they signed in (seconds since 1970)
    issued_at: int  # when this token was made
    admin: bool  # the "role: admin" claim JobRadar sets on the owner's account

    @classmethod
    def from_claims(cls, claims: dict[str, Any]) -> Identity:
        firebase = claims.get("firebase") or {}
        return cls(
            uid=str(claims["uid"]),
            email=claims.get("email"),
            email_verified=bool(claims.get("email_verified")),
            phone=claims.get("phone_number"),
            name=claims.get("name"),
            way=str(firebase.get("sign_in_provider") or ""),
            auth_time=int(claims.get("auth_time") or 0),
            issued_at=int(claims.get("iat") or 0),
            admin=claims.get("role") == "admin",
        )


class Auth(Protocol):
    """What the web app needs from a sign-in provider."""

    def identity(self, id_token: str) -> Identity:
        """Check a token from the sign-in page; raise SignInError if it isn't good."""
        ...

    def start_session(self, id_token: str, days: int) -> str:
        """A session cookie for that sign-in, good for `days`."""
        ...

    def session(self, cookie: str) -> Identity:
        """Check a session cookie, including that it wasn't signed out or switched off."""
        ...

    def end_sessions(self, uid: str) -> None:
        """Sign this person out everywhere."""
        ...

    def make_admin(self, uid: str) -> None:
        """Mark the owner's account as the admin in Firebase too."""
        ...


class Firebase:
    def __init__(
        self, credentials: Path, project_id: str, clock: Callable[[], float] = time.time
    ) -> None:
        if os.environ.get("FIREBASE_AUTH_EMULATOR_HOST"):
            # With it set, the Firebase library accepts tokens without checking signatures.
            raise RuntimeError(
                "FIREBASE_AUTH_EMULATOR_HOST is set: unset it, or anyone could sign in as anyone"
            )
        import firebase_admin
        from firebase_admin import credentials as firebase_credentials

        name = f"jobradar-{project_id}"
        try:
            self.app = firebase_admin.get_app(name)
        except ValueError:
            self.app = firebase_admin.initialize_app(
                firebase_credentials.Certificate(str(credentials)),
                {"projectId": project_id},
                name=name,
            )
        self.clock = clock
        self._checked: dict[tuple[str, int], float] = {}

    def identity(self, id_token: str) -> Identity:
        from firebase_admin import auth

        try:
            claims = auth.verify_id_token(id_token, app=self.app, clock_skew_seconds=SKEW)
        except auth.ExpiredIdTokenError:
            raise SignInError("That sign-in has expired. Sign in again.") from None
        except (auth.InvalidIdTokenError, ValueError):
            raise SignInError("That sign-in isn't one JobRadar accepts. Sign in again.") from None
        who = Identity.from_claims(claims)
        if self.clock() - who.auth_time > RECENT:
            raise SignInError("That sign-in is too old to start a session. Sign in again.")
        return who

    def start_session(self, id_token: str, days: int) -> str:
        from firebase_admin import auth

        try:
            cookie = auth.create_session_cookie(id_token, timedelta(days=days), app=self.app)
        except (auth.InvalidIdTokenError, ValueError):
            raise SignInError("That sign-in isn't one JobRadar accepts. Sign in again.") from None
        return cookie.decode() if isinstance(cookie, bytes) else str(cookie)

    def session(self, cookie: str) -> Identity:
        from firebase_admin import auth

        try:
            claims = auth.verify_session_cookie(cookie, app=self.app, clock_skew_seconds=SKEW)
        except auth.ExpiredSessionCookieError:
            raise SignInError("You were signed out after a while. Sign in again.") from None
        except (auth.InvalidSessionCookieError, ValueError):
            raise SignInError("Sign in to continue.") from None
        who = Identity.from_claims(claims)
        self._still_allowed(who)
        return who

    def _still_allowed(self, who: Identity) -> None:
        """Signed out everywhere, or switched off? Asked of Firebase at most every RECHECK
        seconds per session."""
        from firebase_admin import auth

        now = self.clock()
        key = (who.uid, who.issued_at)
        if now - self._checked.get(key, -RECHECK) < RECHECK:
            return
        try:
            user = auth.get_user(who.uid, app=self.app)
        except auth.UserNotFoundError:
            raise SignInError("This account no longer exists.") from None
        if user.disabled:
            raise SignInError("This account is switched off.")
        if who.issued_at * 1000 < (user.tokens_valid_after_timestamp or 0):
            raise SignInError("You signed out of JobRadar. Sign in again.")
        if len(self._checked) > 10_000:
            self._checked.clear()
        self._checked[key] = now

    def end_sessions(self, uid: str) -> None:
        from firebase_admin import auth

        auth.revoke_refresh_tokens(uid, app=self.app)
        self._checked = {k: v for k, v in self._checked.items() if k[0] != uid}

    def make_admin(self, uid: str) -> None:
        from firebase_admin import auth

        auth.set_custom_user_claims(uid, {"role": "admin"}, app=self.app)
