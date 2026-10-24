"""Alembic's entry point: runs migrations on the engine `jobradar.db.migrate` hands over."""

from __future__ import annotations

from alembic import context
from sqlmodel import SQLModel

from jobradar.db import models  # noqa: F401 (registers every table on SQLModel.metadata)
from jobradar.db.migrations.env_filter import include_object

engine = context.config.attributes["engine"]
with engine.connect() as connection:
    context.configure(
        connection=connection,
        target_metadata=SQLModel.metadata,
        include_object=include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()
    connection.commit()
