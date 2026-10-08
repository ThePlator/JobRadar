from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine

from jobradar.config import Secrets
from jobradar.db.repo import Repo, init_db, make_engine
from jobradar.queue.tasks import TaskQueue


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


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
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
def no_real_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must never pick up the developer's .env or exported API keys."""
    monkeypatch.setitem(Secrets.model_config, "env_file", None)
    for name in Secrets.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
