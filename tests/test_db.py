import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from jobradar.db.models import Platform
from jobradar.db.repo import Repo


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
        c.execute(text("INSERT INTO source (platform, chat_id, enabled) VALUES ('bot', 'x', 1)"))
