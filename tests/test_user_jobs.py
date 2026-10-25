"""Each person's view of a job (SaaS PR 5a): `job` holds the posting for everyone, `user_jobs`
one user's score and status. Existing data becomes the owner's, however it arrives."""

import json
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from jobradar.db.models import OWNER, JobStatus, User
from jobradar.db.repo import Repo, init_db, make_engine
from jobradar.db.transfer import copy, read_only, split_job
from tests.conftest import PG_URL, FakeClock
from tests.test_web import add

pg_only = pytest.mark.skipif(not PG_URL, reason="needs JOBRADAR_TEST_DATABASE_URL (Postgres)")


def other_user(repo: Repo, user_id: str = "u2") -> Repo:
    repo.add_user(User(id=user_id, auth_uid=f"firebase-{user_id}", created_at="2026-10-08"))
    return repo.for_user(user_id)


def test_two_people_see_the_same_job_their_own_way(repo: Repo) -> None:
    add(repo, "j", score=90)  # matched for the owner: new, 90
    other = other_user(repo)
    assert other.get_job("j").status == "extracted"  # type: ignore[union-attr]  (not matched yet)
    assert other.get_job("j").score is None  # type: ignore[union-attr]
    other.set_match_score("j", 40, "weak match (40)")
    other.set_user_status("j", JobStatus.APPLIED)
    mine, theirs = repo.get_job("j"), other.get_job("j")
    assert (mine.status, mine.score) == ("new", 90)  # type: ignore[union-attr]
    assert (theirs.status, theirs.score) == ("applied", 40)  # type: ignore[union-attr]
    assert theirs.company == mine.company == "Acme"  # type: ignore[union-attr]  (the posting is shared)
    assert repo.status_counts().get("applied", 0) == 0
    assert other.status_counts()["applied"] == 1
    assert [j.id for j in other.jobs_applied()] == ["j"] and repo.jobs_applied() == []
    found, total = other.find_jobs(["applied"], None, None, None, None, "new", 10, 0)
    assert total == 1 and found[0][0].score == 40
    assert repo.find_jobs(["applied"], None, None, None, None, "new", 10, 0)[1] == 0


def test_rereading_a_job_puts_it_back_for_those_who_didnt_move_it(repo: Repo) -> None:
    add(repo, "j", score=90)
    other = other_user(repo)
    other.set_user_status("j", JobStatus.SHORTLISTED)
    repo.set_job_status("j", JobStatus.DISCOVERED, "page updated")  # the page changed
    assert repo.get_job("j").status == "discovered"  # type: ignore[union-attr]
    assert other.get_job("j").status == "shortlisted"  # type: ignore[union-attr]  (yours stays)


def test_the_admin_takes_over_the_owner(repo: Repo) -> None:
    add(repo, "j", status=JobStatus.APPLIED, you=True)
    repo.add_user(User(id="early", auth_uid="firebase-me", role="admin", created_at="x"))
    assert repo.take_over_owner("firebase-me")
    owner = repo.user_by_auth_uid("firebase-me")
    assert owner is not None and owner.id == OWNER  # with every job you applied to
    assert repo.get_user("early") is None
    assert not repo.take_over_owner("firebase-someone-else")  # only once


# ---- data from before user_jobs ---------------------------------------------------------------

OLD_JOB = """CREATE TABLE job (id VARCHAR PRIMARY KEY, canonical_url VARCHAR UNIQUE,
    fingerprint VARCHAR, company VARCHAR, role VARCHAR, deadline VARCHAR, data_json VARCHAR,
    score INTEGER, score_reason VARCHAR, scam_risk VARCHAR, status VARCHAR NOT NULL,
    notion_page_id VARCHAR, notion_synced_at VARCHAR, prompt_version VARCHAR,
    user_status_at VARCHAR, created_at VARCHAR NOT NULL, updated_at VARCHAR NOT NULL)"""
OLD_ROWS = [
    # id, data, score, reason, status, notion page, set by you
    ("applied", "{}", 88, "Good fit", "applied", "page-1", "2026-10-02"),
    ("hidden", "{}", 30, "weak match (30)", "hidden", None, None),
    ("ad", None, None, "an ad", "hidden", None, None),  # hidden before it was read
    ("review", "{}", None, "several jobs in one post", "needs_review", None, None),
    ("fresh", None, None, None, "discovered", None, None),
]


def old_database(path: Path) -> Engine:
    """The self-hosted app's layout: score and status on `job`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = make_engine(path)
    with engine.begin() as c:
        c.execute(text(OLD_JOB))
        for sql in OLD_OTHERS + OLD_OTHER_ROWS:
            c.execute(text(sql))
        for id_, data, score, reason, status, page, yours in OLD_ROWS:
            c.execute(
                text("INSERT INTO job (id, data_json, score, score_reason, status, notion_page_id,"
                     " user_status_at, created_at, updated_at) VALUES (:id, :data, :score,"
                     " :reason, :status, :page, :yours, '2026-10-01', '2026-10-01')"),
                {"id": id_, "data": data, "score": score, "reason": reason, "status": status,
                 "page": page, "yours": yours},
            )  # fmt: skip
    return engine


OLD_OTHERS = [
    "CREATE TABLE IF NOT EXISTS alert (job_id VARCHAR PRIMARY KEY, queued_at VARCHAR NOT NULL,"
    " sent_at VARCHAR, outcome VARCHAR)",
    "CREATE TABLE IF NOT EXISTS status_change (id INTEGER PRIMARY KEY, job_id VARCHAR NOT NULL,"
    " status VARCHAR NOT NULL, changed_at VARCHAR NOT NULL)",
    "CREATE TABLE IF NOT EXISTS setting (key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL)",
]
OLD_OTHER_ROWS = [
    "INSERT INTO alert (job_id, queued_at, sent_at, outcome) VALUES ('applied', 't', 't', 'sent')",
    "INSERT INTO status_change (job_id, status, changed_at) VALUES ('applied', 'applied', 't')",
    "INSERT INTO setting (key, value) VALUES ('followed_up:applied', '2026-10-05')",
    "INSERT INTO setting (key, value) VALUES ('web_token', 'app-wide')",
]


def check_owned(repo: Repo) -> None:
    """Alerts, status history and per-job notes from before user_id are the owner's."""
    assert repo.status_history("applied") == [("applied", "t")]
    assert repo.alerts_sent_since("s") == 1 and not repo.queue_alert("applied")
    assert repo.get_user_setting("followed_up:applied") == "2026-10-05"
    assert repo.get_setting("followed_up:applied") is None  # moved, not copied
    assert repo.get_setting("web_token") == "app-wide"  # the app's own stay
    other = repo.for_user("u2")
    assert other.status_history("applied") == [] and other.alerts_sent_since("s") == 0


def check_split(repo: Repo) -> None:
    applied, hidden, ad = repo.get_job("applied"), repo.get_job("hidden"), repo.get_job("ad")
    review, fresh = repo.get_job("review"), repo.get_job("fresh")
    assert applied and hidden and ad and review and fresh
    assert (applied.status, applied.score, applied.score_reason) == ("applied", 88, "Good fit")
    assert (applied.notion_page_id, applied.user_status_at) == ("page-1", "2026-10-02")
    assert (hidden.status, hidden.score_reason) == ("hidden", "weak match (30)")
    assert (ad.status, ad.score_reason) == ("hidden", "an ad")
    assert (review.status, review.score_reason) == ("needs_review", "several jobs in one post")
    assert (fresh.status, fresh.score) == ("discovered", None)
    with repo.engine.connect() as c:
        posting = dict(c.execute(text("SELECT id, status FROM job")).all())
    assert posting == {"applied": "extracted", "hidden": "extracted", "ad": "discovered",
                       "review": "needs_review", "fresh": "discovered"}  # fmt: skip


def test_an_old_sqlite_database_is_split_on_start(tmp_path: Path, clock: FakeClock) -> None:
    engine = old_database(tmp_path / "old.db")
    init_db(engine)
    repo = Repo(engine, clock)
    other_user(repo)
    check_split(repo)
    check_owned(repo)
    init_db(engine)  # again: nothing moves twice
    check_split(repo)
    check_owned(repo)


def test_split_job() -> None:
    row = {"id": "a", "data_json": "{}", "score": 70, "score_reason": "ok", "status": "new",
           "user_status_at": None, "notion_page_id": None, "notion_synced_at": None,
           "created_at": "t", "updated_at": "t"}  # fmt: skip
    posting, view = split_job(row)
    assert posting["status"] == "extracted" and "score" not in posting
    assert view is not None and (view["user_id"], view["status"], view["score"]) == (
        OWNER,
        "new",
        70,
    )
    assert split_job(row | {"status": "discovered", "score": None, "score_reason": None})[1] is None


@pg_only
def test_saas_import_splits_old_jobs(tmp_path: Path, engine: Engine, clock: FakeClock) -> None:
    source = tmp_path / "data" / "jobradar.db"
    old_database(source).dispose()
    report = copy(read_only(source), engine)
    counts = {c.table: c for c in report}
    assert counts["job"].in_postgres == 5 and counts["user_jobs"].in_postgres == 4
    assert all(c.ok for c in report)
    repo = Repo(engine, clock)
    other_user(repo)
    check_split(repo)
    check_owned(repo)


@pg_only
def test_migration_0003_moves_scores_to_the_owner(pg_engine: Engine, clock: FakeClock) -> None:
    from jobradar.db.migrate import upgrade

    with pg_engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    upgrade(pg_engine, "0002")  # the layout before user_jobs
    with pg_engine.begin() as c:
        for id_, data, score, reason, status, page, yours in OLD_ROWS:
            c.execute(
                text("INSERT INTO job (id, data_json, score, score_reason, status, notion_page_id,"
                     " user_status_at, created_at, updated_at) VALUES (:id, :data, :score,"
                     " :reason, :status, :page, :yours, '2026-10-01', '2026-10-01')"),
                {"id": id_, "data": data, "score": score, "reason": reason, "status": status,
                 "page": page, "yours": yours},
            )  # fmt: skip
        for sql in OLD_OTHER_ROWS:
            c.execute(text(sql))
    try:
        init_db(pg_engine)
        repo = Repo(pg_engine, clock)
        other_user(repo)
        check_split(repo)
        check_owned(repo)
    finally:  # leave the schema as the other tests expect it
        with pg_engine.begin() as c:
            c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
        init_db(pg_engine)


def test_job_data_stays_text(repo: Repo) -> None:
    add(repo, "j", skills_required=["Python"])
    job = repo.get_job("j")
    assert job is not None and json.loads(job.data_json or "{}")["skills_required"] == ["Python"]


def test_each_persons_records_are_their_own(repo: Repo, tmp_path: Path) -> None:
    add(repo, "j")
    other = other_user(repo)
    mine = repo.save_artifact("j", "resume", 1, str(tmp_path / "r.pdf"), "{}")
    theirs = other.save_artifact("j", "resume", 1, str(tmp_path / "t.pdf"), "{}")
    assert repo.get_artifact(theirs.id or 0) is None  # someone else's resume: not found
    assert other.get_artifact(mine.id or 0) is None
    assert [a.id for a in repo.artifacts_of("j")] == [mine.id]
    assert repo.next_version("j") == other.next_version("j") == 2
    assert repo.queue_alert("j") and other.queue_alert("j")  # one alert each
    assert repo.pending_alerts() == other.pending_alerts() == ["j"]
    other.finish_alerts(["j"], "sent")
    assert repo.pending_alerts() == ["j"] and other.pending_alerts() == []
    other.set_user_status("j", JobStatus.APPLIED)
    assert repo.status_history("j") == [] and len(other.status_history("j")) == 1
    assert repo.status_changes_since("2000") == {}
    other.set_user_setting("followed_up:j", "2026-10-09")
    assert repo.get_user_setting("followed_up:j") is None
