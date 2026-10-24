"""What a service raises when it can't do what was asked. Callers turn these into an answer:
the dashboard and API into 404 / 409 with the message, the CLI into an error line."""

from __future__ import annotations


class NotFound(LookupError):
    """The thing asked for doesn't exist (or, in the SaaS, isn't this user's)."""


class Refused(Exception):
    """It exists, but this can't be done now: a feature is off, a setting is missing, or the
    request is ambiguous. The message says why and what to do."""
