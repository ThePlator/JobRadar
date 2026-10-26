"""The read API, /api/v1 (SaaS PR 6): the pages' data as JSON, problem details for errors,
cursor paging, and an OpenAPI document that only changes on purpose."""

import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jobradar.db.models import JobStatus
from jobradar.db.repo import Repo
from jobradar.web.app import create_app
from tests.conftest import FakeClock
from tests.test_auth import FakeAuth, auth_settings, sign_in_as
from tests.test_web import add, with_storage

SNAPSHOT = Path(__file__).parent / "snapshots" / "openapi.json"


@pytest.fixture
def api(repo: Repo, clock: FakeClock, tmp_path: Path) -> TestClient:
    """The API on this computer (no sign-in): the owner's data."""
    return TestClient(create_app(with_storage(tmp_path), repo, clock),
                      base_url="http://127.0.0.1:8080")  # fmt: skip


def is_problem(r: Any, status: int) -> dict[str, Any]:
    assert r.status_code == status, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    body: dict[str, Any] = r.json()
    assert body["status"] == status and body["title"]
    return body


def test_jobs_page_by_cursor(api: TestClient, repo: Repo, clock: FakeClock) -> None:
    for n in range(5):
        add(repo, f"j{n}", role=f"Engineer {n}", score=60 + n, locations=["Pune"],
            work_mode="remote", apply_url=f"https://acme.com/apply/{n}")  # fmt: skip
        clock.advance(60)
    first = api.get("/api/v1/jobs?limit=2").json()
    assert [j["id"] for j in first["items"]] == ["j4", "j3"] and first["total"] == 5
    item = first["items"][0]
    assert item["places"] == ["Pune"] and item["work_mode"] == "remote" and item["score"] == 64
    assert item["first_seen"].endswith("Z") and item["apply_url"] == "https://acme.com/apply/4"
    second = api.get(f"/api/v1/jobs?limit=2&cursor={first['next_cursor']}").json()
    third = api.get(f"/api/v1/jobs?limit=2&cursor={second['next_cursor']}").json()
    assert [j["id"] for j in second["items"] + third["items"]] == ["j2", "j1", "j0"]
    assert third["next_cursor"] is None
    best = api.get("/api/v1/jobs?sort=score&min_score=63").json()
    assert [j["id"] for j in best["items"]] == ["j4", "j3"]


def test_filters_and_search(api: TestClient, repo: Repo) -> None:
    add(repo, "a", role="Backend Engineer", company="Rubrik")
    add(repo, "b", role="Data Analyst", company="Acme", status=JobStatus.APPLIED, you=True)
    applied = api.get("/api/v1/jobs?status=applied").json()
    assert [j["id"] for j in applied["items"]] == ["b"]
    two = api.get("/api/v1/jobs?status=applied&status=new").json()
    assert {j["id"] for j in two["items"]} == {"a", "b"}
    found = api.get("/api/v1/search?q=rubrik").json()
    assert [j["id"] for j in found["items"]] == ["a"]
    assert "<mark>" in (found["items"][0]["snippet"] or "")


def test_one_job_with_everything_about_it(api: TestClient, repo: Repo, tmp_path: Path) -> None:
    add(repo, "j", company="Acme", skills_required=["Python"], summary="Build APIs.")
    repo.set_user_status("j", JobStatus.APPLIED)
    out = tmp_path / "output"
    out.mkdir()
    (out / "Acme_SWE_v1.pdf").write_bytes(b"%PDF-1.4 resume")
    resume = repo.save_artifact("j", "resume", 1, str(out / "Acme_SWE_v1.pdf"), "{}")
    job = api.get("/api/v1/jobs/j").json()
    assert job["status"] == "applied" and job["skills_required"] == ["Python"]
    assert job["summary"] == "Build APIs." and job["reasons"].startswith("Good fit")
    assert [h["status"] for h in job["history"]] == ["applied"]
    assert job["resumes"][0]["file"] == f"/api/v1/files/{resume.id}"
    assert api.get(job["resumes"][0]["file"]).content == b"%PDF-1.4 resume"
    assert api.get("/api/v1/jobs/j/history").json()[0]["status"] == "applied"
    assert api.get("/api/v1/jobs/j/resumes").json()[0]["version"] == 1
    draft = api.get("/api/v1/jobs/j/followup-draft").json()
    assert "Acme" in draft["subject"] + draft["body"]
    assert [f["job"]["id"] for f in api.get("/api/v1/followups").json()] == ["j"]


def test_errors_are_problem_details(api: TestClient, repo: Repo) -> None:
    add(repo, "j")
    assert "No job" in is_problem(api.get("/api/v1/jobs/nope"), 404)["detail"]
    is_problem(api.get("/api/v1/files/999"), 404)
    is_problem(api.get("/api/v1/jobs/j/followup-draft"), 404)  # not applied to
    is_problem(api.get("/api/v1/jobs?cursor=not-ours"), 400)
    assert "limit" in is_problem(api.get("/api/v1/jobs?limit=500"), 422)["detail"]
    is_problem(api.get("/api/v1/skills/no-such-skill"), 404)
    is_problem(api.get("/api/v1/skills?days=12"), 422)
    is_problem(api.get("/api/v1/digests/1999-01-01-0900"), 404)


def test_the_rest_of_the_pages(api: TestClient, repo: Repo) -> None:
    add(repo, "j", status=JobStatus.APPLIED, you=True)
    me = api.get("/api/v1/me").json()
    assert me["id"] == "owner" and me["plan"]["id"] == "free"
    assert api.get("/api/v1/preferences").json()["scoring"]["alert_above"] == 75
    columns = api.get("/api/v1/applications").json()
    applied = next(c for c in columns if c["key"] == "applied")
    assert [i["id"] for i in applied["items"]] == ["j"]
    assert applied["items"][0]["resume_built"] is False
    assert api.get("/api/v1/applications/by-source").json()["applications"] == 0  # no posts
    assert api.get("/api/v1/skills?days=7").json()["days"] == 7
    windows = api.get("/api/v1/digests").json()
    assert windows[0]["key"] == "now" and windows[0]["current"]
    assert api.get("/api/v1/digests/now").json()["window"]["key"] == "now"
    assert api.get("/api/v1/sources").json() == []
    assert api.get("/api/v1/ready").json() == {"ok": True}
    assert "GET" in api.get("/api").text and "/api/v1/jobs" in api.get("/api").text


def test_scripts_must_sign_in(repo: Repo, clock: FakeClock) -> None:
    site = TestClient(create_app(auth_settings(), repo, clock, auth=FakeAuth()),
                      base_url="http://127.0.0.1:8080", follow_redirects=False)  # fmt: skip
    is_problem(site.get("/api/v1/jobs"), 401)
    assert site.get("/api/v1/ready").status_code == 200  # no sign-in needed
    sign_in_as(site, "uid-2")  # not the admin: on the waitlist
    assert "on the list" in is_problem(site.get("/api/v1/jobs"), 403)["detail"]


def test_the_openapi_document_only_changes_on_purpose(api: TestClient) -> None:
    """A change to the API changes tests/snapshots/openapi.json; review it, then update it with
    JOBRADAR_UPDATE_SNAPSHOTS=1 uv run pytest tests/test_api.py."""
    doc = api.get("/api/v1/openapi.json").json()
    text = json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if os.environ.get("JOBRADAR_UPDATE_SNAPSHOTS"):
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(text, encoding="utf-8")
    assert SNAPSHOT.read_text(encoding="utf-8") == text, (
        "The API changed: review the difference, then run with JOBRADAR_UPDATE_SNAPSHOTS=1"
    )
