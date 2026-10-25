"""What Alembic leaves alone when comparing the database with the models: tables Postgres has
that the models don't describe (the search index, created by migration 0001)."""

from __future__ import annotations

NOT_IN_MODELS = {"job_search", "alembic_version"}


def include_object(
    obj: object, name: str | None, kind: str, reflected: bool, other: object
) -> bool:
    return not (kind == "table" and name in NOT_IN_MODELS)
