"""People who sign in to the SaaS: their first sign-in creates their account.

The admin's first sign-in takes over the `owner` account, which holds every score and status
from before the SaaS. Until each user gets their own matches (SAASPLAN.md, PR 5), only the admin
may open the dashboard; everyone else signs in and is told it opens soon.
"""

from __future__ import annotations

from uuid import uuid4

from jobradar.clock import to_iso
from jobradar.db.models import Role, User
from jobradar.saas.auth import Identity
from jobradar.services.context import Ctx
from jobradar.services.errors import Refused


def is_admin_email(who: Identity, admin_email: str | None) -> bool:
    """The owner's address, checked by the sign-in provider (an unchecked address could be
    anyone's)."""
    return bool(
        admin_email
        and who.email
        and who.email_verified
        and who.email.strip().casefold() == admin_email.strip().casefold()
    )


def sign_in(ctx: Ctx, who: Identity, admin_email: str | None) -> User:
    """The account for this sign-in, created the first time. Raises Refused if the admin has
    switched it off."""
    role = Role.ADMIN if is_admin_email(who, admin_email) else Role.USER
    if role == Role.ADMIN:
        ctx.repo.take_over_owner(who.uid)  # your jobs so far are the owner's
    user = ctx.repo.user_by_auth_uid(who.uid)
    if user is None:
        now = to_iso(ctx.now)
        return ctx.repo.add_user(
            User(
                id=uuid4().hex, auth_uid=who.uid, email=who.email, phone=who.phone,
                name=who.name, role=role.value, created_at=now, last_seen_at=now,
            )
        )  # fmt: skip
    if user.deleted_at:
        raise Refused("This account is switched off. Write to the JobRadar team if that's wrong.")
    if user.role != Role.ADMIN:  # the admin role is never taken away by a sign-in
        user.role = role.value
    ctx.repo.saw_user(user.id, who.email, who.phone, who.name, user.role)
    user.email, user.phone, user.name = who.email, who.phone, who.name
    return user


def can_open_dashboard(user: User) -> bool:
    """Only the admin, until the dashboard shows each user their own jobs (PR 5)."""
    return user.role == Role.ADMIN
