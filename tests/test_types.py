"""Times and JSON (SaaS PR 5e): `timestamptz` and `jsonb` on Postgres, text on SQLite, and the
same strings to JobRadar either way."""

import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DataError

from jobradar.db.repo import Repo
from tests.conftest import PG_URL
from tests.test_web import add

pg_only = pytest.mark.skipif(not PG_URL, reason="needs JOBRADAR_TEST_DATABASE_URL (Postgres)")


def test_times_and_json_read_back_as_text(repo: Repo) -> None:
    add(repo, "j", skills_required=["Python"], locations=["Pune"])
    job = repo.get_job("j")  # through SQL written by hand
    assert job is not None and isinstance(job.created_at, str) and isinstance(job.data_json, str)
    assert job.created_at == "2026-10-08T09:00:00.000000Z"  # the clock's moment, as stored
    assert json.loads(job.data_json)["skills_required"] == ["Python"]
    repo.enqueue("fetch", "j", {"why": "test"})
    task = repo.claim()  # through the ORM-free queue, JSON payload included
    assert task is not None and task.payload == {"why": "test"}
    page = repo.get_page("j")  # and through the ORM
    assert page is None


@pg_only
def test_postgres_stores_real_times_and_json(repo: Repo) -> None:
    add(repo, "j")
    with repo.engine.begin() as c:
        kinds = dict(c.execute(text(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'job' AND column_name IN ('created_at', 'data_json')")).all())  # fmt: skip
        assert kinds == {"created_at": "timestamp with time zone", "data_json": "jsonb"}
        # Another offset, written by hand: read back in UTC, in the usual format.
        c.execute(text("UPDATE job SET updated_at = '2026-10-08T14:30:00+05:30' WHERE id = 'j'"))
    job = repo.get_job("j")
    assert job is not None and job.updated_at == "2026-10-08T09:00:00.000000Z"
    with pytest.raises(DataError), repo.engine.begin() as c:
        c.execute(text("UPDATE job SET updated_at = 'yesterday-ish' WHERE id = 'j'"))
    with pytest.raises(DataError), repo.engine.begin() as c:
        c.execute(text("UPDATE job SET data_json = '{not json' WHERE id = 'j'"))
