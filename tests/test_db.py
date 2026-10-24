from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from jobradar.db.models import Platform
from jobradar.db.repo import Repo, init_db, make_engine
from tests.conftest import PG_URL

sqlite_only = pytest.mark.skipif(bool(PG_URL), reason="SQLite settings and repairs")


@sqlite_only
def test_pragmas(engine: Engine) -> None:
    with engine.connect() as c:
        assert c.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert c.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_upsert_source_is_idempotent(repo: Repo) -> None:
    a = repo.upsert_source(Platform.TELEGRAM, "-100123", "Off Campus Jobs")
    b = repo.upsert_source(Platform.TELEGRAM, "-100123", "Off Campus Jobs (renamed)")
    assert a.id == b.id
    assert b.title == "Off Campus Jobs (renamed)"
    other = repo.upsert_source(Platform.EMAIL, "-100123")
    assert other.id != a.id


def test_last_msg_id_never_moves_backwards(repo: Repo) -> None:
    src = repo.upsert_source(Platform.TELEGRAM, "@jobs")
    assert src.id is not None
    repo.set_last_msg_id(src.id, 50)
    repo.set_last_msg_id(src.id, 40)
    assert repo.upsert_source(Platform.TELEGRAM, "@jobs").last_msg_id == 50


def test_raw_message_saved_once(repo: Repo) -> None:
    src = repo.upsert_source(Platform.TELEGRAM, "@jobs")
    assert src.id is not None
    first = repo.save_raw_message(src.id, "7", "2026-10-08T08:00:00.000000Z", "hi", ["https://a.b"])
    again = repo.save_raw_message(src.id, "7", "2026-10-08T08:00:00.000000Z", "hi")
    assert first == (first[0], True)
    assert again == (first[0], False)
    msg = repo.get_raw_message(first[0])
    assert msg is not None and msg.urls_json == '["https://a.b"]' and not msg.processed


def test_foreign_keys_enforced(repo: Repo) -> None:
    with pytest.raises(IntegrityError):
        repo.save_raw_message(999, "1", "2026-10-08T08:00:00.000000Z")


def test_check_constraints(engine: Engine) -> None:
    with pytest.raises(IntegrityError), engine.begin() as c:
        c.execute(text("INSERT INTO source (platform, chat_id, enabled) VALUES ('bot', 'x', TRUE)"))


def test_new_columns_are_added_to_an_old_database(tmp_path: Path) -> None:
    eng = make_engine(tmp_path / "old.db")
    init_db(eng)
    with eng.begin() as c:  # make it look like a v0.2 A database, with a saved page
        c.execute(text("ALTER TABLE fetched_page DROP COLUMN structured_json"))
        c.execute(
            text(
                "INSERT INTO job (id, canonical_url, status, created_at, updated_at) "
                "VALUES ('j1', 'https://a.co', 'discovered', 'x', 'x')"
            )
        )
        c.execute(text("INSERT INTO fetched_page (job_id, fetched_at) VALUES ('j1', 'x')"))
    init_db(eng)
    init_db(eng)  # idempotent
    with eng.connect() as c:
        columns = {row[1] for row in c.execute(text("PRAGMA table_info('fetched_page')"))}
        assert "structured_json" in columns
        assert c.execute(text("SELECT count(*) FROM fetched_page")).scalar() == 1
    eng.dispose()


def test_source_platform_list_is_upgraded_in_place(tmp_path: Path) -> None:
    import sqlite3

    from jobradar.db.models import Platform

    path = tmp_path / "old.db"
    eng = make_engine(path)
    init_db(eng)
    with eng.begin() as c:  # make `source` look like it did before boards and pages existed
        c.execute(text("PRAGMA foreign_keys = OFF"))
        sql = c.execute(text("SELECT sql FROM sqlite_master WHERE name = 'source'")).scalar()
        assert sql is not None
        c.execute(text("PRAGMA writable_schema = ON"))
        c.execute(
            text("UPDATE sqlite_master SET sql = :sql WHERE name = 'source'"),
            {"sql": sql.replace(", 'board'", "").replace(", 'page'", "")},
        )
        c.execute(text("PRAGMA writable_schema = OFF"))
    eng.dispose()
    eng = make_engine(path)
    repo = Repo(eng)
    src = repo.upsert_source(Platform.TELEGRAM, "-1001", "Jobs")
    assert src.id is not None
    raw_id, _ = repo.save_raw_message(src.id, "1", "2026-10-08T08:00:00.000000Z", "post")
    with pytest.raises(IntegrityError):  # the old list refuses boards
        repo.upsert_source(Platform.BOARD, "greenhouse:stripe", "Greenhouse · Stripe")
    eng.dispose()

    eng = make_engine(path)
    init_db(eng)  # upgrades `source`, keeping rows and links
    init_db(eng)  # and is a no-op the second time
    repo = Repo(eng)
    board = repo.upsert_source(Platform.BOARD, "greenhouse:stripe", "Greenhouse · Stripe")
    assert board.id is not None
    assert repo.get_raw_message(raw_id) is not None
    assert repo.upsert_source(Platform.PAGE, "page:https://x.com/d", "Drives").id is not None
    assert (tmp_path / "old.db.before-board-page-sources.bak").exists()
    with eng.connect() as c:
        assert c.execute(text("PRAGMA foreign_key_check")).all() == []
    eng.dispose()
    sqlite3.connect(path).close()


def test_jobs_from_source(repo: Repo) -> None:
    from jobradar.db.models import JobStatus, Platform

    src = repo.upsert_source(Platform.BOARD, "workday:x", "Workday · Accenture")
    other = repo.upsert_source(Platform.TELEGRAM, "-1001", "Jobs")
    assert src.id is not None and other.id is not None
    for n, source_id in enumerate([src.id, src.id, other.id]):
        raw, _ = repo.save_raw_message(source_id, str(n), "2026-10-16T08:00:00.000000Z", "post")
        repo.get_or_create_job(f"j{n}", f"https://x.com/{n}")
        repo.add_job_source(f"j{n}", raw)
    repo.set_user_status("j1", JobStatus.APPLIED)  # you moved it in Notion: left alone
    assert repo.jobs_from_source("workday · accenture") == ["j0"]


@pytest.mark.skipif(not PG_URL, reason="needs JOBRADAR_TEST_DATABASE_URL (Postgres)")
def test_migrations_match_the_models(engine: Engine) -> None:
    """Every model change needs a migration: compare the migrated database with the models."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlmodel import SQLModel

    from jobradar.db.migrations.env_filter import include_object

    with engine.connect() as c:
        ctx = MigrationContext.configure(
            c, opts={"compare_type": True, "include_object": include_object}
        )
        assert compare_metadata(ctx, SQLModel.metadata) == []


@pytest.mark.skipif(not PG_URL, reason="needs JOBRADAR_TEST_DATABASE_URL (Postgres)")
def test_claims_skip_rows_another_worker_holds(engine: Engine) -> None:
    """Two workers claiming at once never get the same task (FOR UPDATE SKIP LOCKED)."""
    repo = Repo(engine)
    repo.enqueue("fetch", "a")
    repo.enqueue("fetch", "b")
    with engine.connect() as held:
        held.execute(text("BEGIN"))
        row = held.execute(text("SELECT id, key FROM task WHERE key = 'a' FOR UPDATE")).one()
        other = repo.claim()
        assert other is not None and other.key == "b"  # 'a' is locked: skipped, not waited on
        held.execute(text("ROLLBACK"))
    assert row.key == "a"
