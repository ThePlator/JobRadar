"""Keeping users apart (SAASPLAN.md section 9, SaaS PR 5d): user B never sees or changes user A's
scores, statuses, history, alerts, resumes, notes or profile, by any page or endpoint; and on
Postgres, row-level security holds even for a query that forgets to say whose rows it wants."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from jobradar.db.models import OWNER, JobStatus, User
from jobradar.db.repo import Repo
from jobradar.web.app import create_app
from tests.conftest import PG_URL, FakeClock
from tests.test_auth import FakeAuth, auth_settings, page_token, sign_in_as
from tests.test_web import add

SECRET = "Zyxlang"  # noqa: S105 (a skill only A's score reason names: it never reaches B)
PER_USER = ("user_jobs", "status_change", "alert", "artifact", "user_settings", "profiles",
            "preferences")  # fmt: skip


def a_has_things(repo: Repo, tmp_path: Path) -> int:
    """The owner (A) has a scored, applied job with history, an alert, a resume and a note.
    Returns the resume's id."""
    add(repo, "j", company="Acme", score=91)
    repo.set_match_score("j", 91, f"Good fit · 91 · eligible ✓ · {SECRET} ✓")
    repo.set_user_status("j", JobStatus.APPLIED)
    repo.queue_alert("j")
    repo.set_user_setting("followed_up:j", "2026-10-07")
    pdf = tmp_path / "out" / "resume.pdf"
    pdf.parent.mkdir()
    pdf.write_bytes(b"%PDF-1.4 A's resume")
    return repo.save_artifact("j", "resume", 1, str(pdf), "{}").id or 0


def site_for(repo: Repo, clock: FakeClock, tmp_path: Path) -> TestClient:
    base = auth_settings()
    profile = tmp_path / "profile.yaml"
    profile.write_text("basics:\n  name: Alice Owner\nskills: [Python]\n")
    s = base.model_copy(update={
        "auth": base.auth.model_copy(update={"waitlist": False}),
        "config": base.config.model_copy(update={
            "matching": base.config.matching.model_copy(update={"profile_path": profile}),
            "storage": base.config.storage.model_copy(update={"local_dir": tmp_path / "out"}),
        }),
    })  # fmt: skip
    app = create_app(s, repo, clock, auth=FakeAuth())
    return TestClient(app, base_url="http://127.0.0.1:8080", follow_redirects=False)


def test_b_never_sees_or_changes_as_things(repo: Repo, clock: FakeClock, tmp_path: Path) -> None:
    resume = a_has_things(repo, tmp_path)
    b = site_for(repo, clock, tmp_path)
    assert sign_in_as(b, "uid-2").status_code == 200  # B: not the admin

    pages = ["/", "/applications", "/jobs", "/jobs?show=all", "/jobs?show=all&q=Acme",
             "/digest", "/digest/email", "/jobs/j", "/skills", "/api/search?q=Acme",
             "/api/jobs/j/resumes"]  # fmt: skip
    for path in pages:
        page = b.get(path)
        assert page.status_code == 200, path
        assert SECRET not in page.text and "Alice" not in page.text, path
    assert (
        'value="applied" selected' not in b.get("/jobs/j").text
    )  # the posting is shared; A's status isn't
    assert b.get("/api/jobs/j/resumes").json()["count"] == 0
    assert b.get(f"/files/{resume}").status_code == 404  # A's resume: not found, not "forbidden"

    token = page_token(b.get("/").text)
    assert b.post("/jobs/j/act", data={"action": "shortlist", "token": token}).status_code == 303
    know = b.post("/skills/know", data={"skill": "Rust", "token": token},
                  headers={"x-jobradar-token": token})  # fmt: skip
    assert know.status_code == 409 and "profile" in know.json()["message"]

    mine = repo.get_job("j")
    assert mine is not None and mine.status == "applied" and SECRET in (mine.score_reason or "")
    assert [s for s, _ in repo.status_history("j")] == ["applied"]
    assert "Rust" not in (tmp_path / "profile.yaml").read_text()
    b_user = repo.user_by_auth_uid("uid-2")
    assert b_user is not None
    theirs = repo.for_user(b_user.id)
    assert theirs.get_job("j").status == "shortlisted"  # type: ignore[union-attr]
    assert theirs.status_history("j")[0][0] == "shortlisted"

    a = site_for(repo, clock, tmp_path)  # and A still sees all of it
    sign_in_as(a, "uid-1")
    assert SECRET in a.get("/jobs/j").text
    assert a.get(f"/files/{resume}").status_code == 200


def test_the_waitlist_keeps_others_out(repo: Repo, clock: FakeClock, fake: FakeAuth) -> None:
    site = TestClient(create_app(auth_settings(), repo, clock, auth=fake),
                      base_url="http://127.0.0.1:8080", follow_redirects=False)  # fmt: skip
    sign_in_as(site, "uid-2")
    assert site.get("/").status_code == 403  # AUTH_WAITLIST is on unless turned off


@pytest.fixture
def fake() -> FakeAuth:
    return FakeAuth()


@pytest.mark.skipif(not PG_URL, reason="needs JOBRADAR_TEST_DATABASE_URL (Postgres)")
def test_postgres_keeps_users_apart_even_without_a_filter(repo: Repo, tmp_path: Path) -> None:
    repo.add_user(User(id="u2", auth_uid="firebase-u2", created_at="2026-10-08"))
    a_has_things(repo, tmp_path)
    repo.save_profile({"basics": {"name": "Alice"}})
    repo.save_preferences({})
    other = repo.for_user("u2")
    other.set_user_status("j", JobStatus.SKIPPED)
    other.queue_alert("j")
    other.save_artifact("j", "resume", 1, "b.pdf", "{}")
    other.set_user_setting("followed_up:j", "2026-10-08")
    other.save_profile({"basics": {"name": "Bo"}})
    other.save_preferences({})

    b = repo.scoped("u2")
    with b.engine.begin() as c:  # queries with no user filter at all
        for table in PER_USER:
            whose = c.execute(text(f"SELECT DISTINCT user_id FROM {table}")).scalars().all()  # noqa: S608
            assert whose == ["u2"], table
    with pytest.raises(DBAPIError, match="row-level security"), b.engine.begin() as c:
        c.execute(text("INSERT INTO user_settings (user_id, key, value) "
                       "VALUES (:owner, 'x', 'planted')"), {"owner": OWNER})  # fmt: skip
    with pytest.raises(DBAPIError, match="permission denied"), b.engine.begin() as c:
        c.execute(text("SELECT * FROM users"))  # accounts are for sign-in code only
    with repo.engine.begin() as c:  # a session that never said who it is sees nothing
        c.execute(text("SELECT set_config('role', current_database() || '_app', true)"))
        assert c.execute(text("SELECT count(*) FROM user_jobs")).scalar() == 0
    with repo.engine.connect() as c:  # background work (the table owner) still sees all
        assert c.execute(text("SELECT count(DISTINCT user_id) FROM user_jobs")).scalar() == 2
