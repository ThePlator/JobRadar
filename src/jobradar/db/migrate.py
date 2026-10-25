"""Database migrations (Alembic) for Postgres, run on start-up by `init_db`.

Every schema change is one file in `migrations/versions/`. SQLite (tests, and the self-hosted
app on `main`) still uses `create_all`; a test checks the migrations and the models agree.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine

HERE = Path(__file__).parent


def config(engine: Engine) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(HERE / "migrations"))
    cfg.attributes["engine"] = engine
    return cfg


def upgrade(engine: Engine, revision: str = "head") -> None:
    """Apply every migration not yet applied. Safe to run on each start."""
    command.upgrade(config(engine), revision)
