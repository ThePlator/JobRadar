import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from jobradar.config import AuthConfig, Secrets
from jobradar.db.repo import Repo, add_owner, connect, init_db, make_engine
from jobradar.queue.tasks import TaskQueue

# Set to a throwaway Postgres (never a real one: every table is emptied) to run the whole
# suite on Postgres, as CI does: postgresql://jobradar:jobradar@localhost:55432/jobradar
PG_URL = os.environ.get("JOBRADAR_TEST_DATABASE_URL")


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture(scope="session")
def pg_engine() -> Iterator[Engine]:
    """One Postgres for the session, built with the real migrations from an empty schema."""
    assert PG_URL
    eng = connect(PG_URL)
    with eng.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    init_db(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def engine(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Engine]:
    if PG_URL:
        eng: Engine = request.getfixturevalue("pg_engine")
        with eng.begin() as c:
            # Everything but the plan rows init_db adds once per session.
            tables = [r[0] for r in c.execute(text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename NOT IN ('alembic_version', 'plans')"))]  # fmt: skip
            c.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
        add_owner(eng)  # the self-hosted app's user, as init_db makes it
        yield eng
        return
    eng = make_engine(tmp_path / "data" / "test.db")
    init_db(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def repo(engine: Engine, clock: FakeClock) -> Repo:
    return Repo(engine, clock=clock)


@pytest.fixture
def queue(repo: Repo, clock: FakeClock) -> TaskQueue:
    import random

    return TaskQueue(repo, clock=clock, rng=random.Random(0))


@pytest.fixture(autouse=True)
def no_real_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A command run in a test (e.g. `alerts --test`) opens a throwaway database, never the
    developer's data/jobradar.db."""
    monkeypatch.setattr("jobradar.app.DEFAULT_DB_PATH", tmp_path / "default" / "jobradar.db")


@pytest.fixture(autouse=True)
def no_real_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must never pick up the developer's .env or exported API keys."""
    monkeypatch.setitem(Secrets.model_config, "env_file", None)
    for name in Secrets.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setitem(AuthConfig.model_config, "env_file", None)
    for field in AuthConfig.model_fields.values():
        monkeypatch.delenv(str(field.validation_alias), raising=False)
