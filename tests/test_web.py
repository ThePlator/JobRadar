import json
import socket
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from jobradar.config import WebConfig
from jobradar.db.models import JobStatus, Platform
from jobradar.db.repo import Repo
from jobradar.pipeline.schemas import JobPosting
from jobradar.web import server, views
from jobradar.web.app import create_app
from jobradar.web.server import NoFreePort, free_port
from tests.conftest import FakeClock
from tests.test_digest import settings

TZ = settings().config.notify.tz


def add(repo: Repo, job_id: str, company: str = "Acme", role: str = "Software Engineer",
        score: int | None = 90, status: JobStatus = JobStatus.NEW, you: bool = False,
        **posting: Any) -> None:  # fmt: skip
    repo.get_or_create_job(job_id, f"https://acme.com/jobs/{job_id}")
    data = json.dumps({"company": company, "role": role, **posting})
    repo.save_extraction(job_id, data, company, role, posting.get("deadline"), "rules-v1",
                         JobStatus.EXTRACTED)  # fmt: skip
    repo.set_match_score(job_id, score, f"Good fit · {score} · eligible ✓ · Python ✓")
    if you:
        repo.set_user_status(job_id, status)
    else:
        repo.set_job_status(job_id, status)


# ---- what each page lists ------------------------------------------------------------------


def test_today_lists_recent_good_matches_you_havent_touched(repo: Repo, clock: FakeClock) -> None:
    add(repo, "old", score=95)
    clock.advance(4 * 86400)  # "old" is now 4 days old
    add(repo, "good", score=80)
    add(repo, "best", score=99)
    add(repo, "weak", score=60)
    add(repo, "applied", score=99, status=JobStatus.APPLIED, you=True)
    add(repo, "hidden", score=None, status=JobStatus.HIDDEN)
    assert [c.id for c in views.today(repo, 75, clock(), TZ)] == ["best", "good"]


def test_board_puts_each_status_in_its_column(repo: Repo, clock: FakeClock) -> None:
    add(repo, "s", status=JobStatus.SHORTLISTED, you=True)
    clock.advance(60)
    add(repo, "r", status=JobStatus.RESUME_READY)  # JobRadar built the resume, later
    add(repo, "a1", status=JobStatus.APPLIED, you=True)
    clock.advance(60)
    add(repo, "a2", status=JobStatus.APPLIED, you=True)
    add(repo, "x", status=JobStatus.REJECTED, you=True)
    add(repo, "skip", status=JobStatus.SKIPPED, you=True)
    columns = {c.title: [card.id for card in c.cards] for c in views.board(repo, TZ, clock())}
    assert columns == {
        "Shortlisted": ["r", "s"],
        "Applied": ["a2", "a1"],  # the latest change first
        "Interview": [],
        "Offer or rejected": ["x"],
    }


def test_search_and_show(repo: Repo, clock: FakeClock) -> None:
    add(repo, "r", company="Rubrik")
    add(repo, "d", company="Acme", role="Data Engineer")
    add(repo, "p", company="100% Remote Co")
    add(repo, "h", company="Rubrik", status=JobStatus.HIDDEN)
    add(repo, "n", company="Junk", status=JobStatus.DISCARDED)
    ids = lambda show="jobs", q="": {c.id for c in views.all_jobs(repo, TZ, show, q, 1).cards}  # noqa: E731
    assert ids() == {"r", "d", "p"}  # hidden and not-a-job left out
    assert ids(q="rubrik") == {"r"} and ids(q="data eng") == {"d"}
    assert ids(q="%") == {"p"}  # a literal %, not "anything"
    assert ids(show="hidden") == {"h"} and ids(show="everything") == {"r", "d", "p", "h", "n"}
    assert ids(show="nonsense") == ids()


def test_pages_of_fifty(repo: Repo, clock: FakeClock) -> None:
    for n in range(55):
        add(repo, f"j{n:02}")
        clock.advance(1)
    first = views.all_jobs(repo, TZ, "jobs", "", 1)
    assert (first.total, first.pages, len(first.cards), first.cards[0].id) == (55, 2, 50, "j54")
    last = views.all_jobs(repo, TZ, "jobs", "", 99)  # past the end: the last page
    assert (last.page, [c.id for c in last.cards][-1]) == (2, "j00")


def test_job_detail(repo: Repo, clock: FakeClock) -> None:
    add(repo, "j", company="Rubrik", locations=["Pune"], work_mode="hybrid",
        experience_min=0, experience_max=1, skills_required=["Python", "SQL"],
        deadline="2026-10-24", apply_url="https://apply.acme.com/1")  # fmt: skip
    source = repo.upsert_source(Platform.TELEGRAM, "-1001234", "Freshershunt")
    assert source.id is not None
    raw, _ = repo.save_raw_message(source.id, "77", "2026-10-08T08:30:00Z", "Rubrik hiring")
    repo.add_job_source("j", raw)
    repo.set_user_status("j", JobStatus.APPLIED)
    repo.save_artifact("j", "resume", 1, "output/Rubrik_SWE_20261008_v1.pdf", "{}")
    d = views.job_detail(repo, "j", TZ, clock())
    assert d is not None
    assert ("Experience", "0-1 years") in d.facts and ("Closes", "24 Oct") in d.facts
    assert d.card.place == "Pune · Hybrid" and d.apply_url == "https://apply.acme.com/1"
    assert [(s.where, s.at, s.link) for s in d.seen_in] == [
        ("Telegram · Freshershunt", "8 Oct, 14:00", "https://t.me/c/1234/77")
    ]
    assert d.history == [("Applied", "8 Oct, 14:30")]  # 09:00 UTC in Asia/Kolkata
    assert [(f.name, f.kind) for f in d.files] == [("Rubrik_SWE_20261008_v1.pdf", "resume")]
    assert views.job_detail(repo, "nope", TZ, clock()) is None


def test_blip(repo: Repo, clock: FakeClock) -> None:
    add(repo, "a", company="Rubrik", role="Backend Engineer", score=92)
    add(repo, "b", status=JobStatus.APPLIED, you=True)
    cards = views.today(repo, 75, clock(), TZ)
    assert (
        views.blip_says(cards, 75)
        == "One good match waiting. The best is Backend Engineer at Rubrik, a 92."
    )
    assert views.blip_says([], 75).startswith("Nothing new scored 75+")
    assert views.greeting(clock(), TZ, "Sameer") == "Good afternoon, Sameer."  # 14:30 IST

    ov = views.overview(repo, 75, clock(), TZ, "Sameer Kumar")
    assert (ov.today, ov.active, ov.applied, ov.name, ov.awake) == (1, 1, 1, "Sameer", False)
    source = repo.upsert_source(Platform.TELEGRAM, "-1001", "Chat")
    assert source.id is not None
    repo.save_raw_message(source.id, "1", "2026-10-08T08:59:00.000000Z", "hi")
    ov = views.overview(repo, 75, clock(), TZ, "Your Name")  # the example profile's name
    assert ov.awake and ov.last_read == "just now" and ov.name == ""
    clock.advance(3600)
    assert not views.overview(repo, 75, clock(), TZ, "").awake  # an hour with nothing read


def test_blips_take() -> None:
    r = views.split_reason("Strong fit · 96 · eligible ✓ · Python, SQL ✓ · missing Kafka · Pune ✓",
                           JobPosting(locations=["Pune"]))  # fmt: skip
    assert views.take(r) == (
        "Strong fit. You're eligible, Pune works and you have Python, SQL. Missing: Kafka."
    )
    assert (
        views.take(views.split_reason(None, None)) == "I couldn't read enough of this job to say."
    )
    assert views.initials("Thermo Fisher Scientific") == "TF" and views.initials("rubrik") == "RU"
    assert views.hue("Rubrik") == views.hue("rubrik")


def test_when() -> None:
    assert views.when("2026-10-19T17:41:43.000000Z", TZ) == "19 Oct, 23:11"
    assert views.when("2026-10-19T00:00:00.000000Z", TZ) == "19 Oct"  # a board's bare date
    assert views.when("not a time", TZ) == "" and views.when(None, TZ) == ""


# ---- the web app ------------------------------------------------------------------------------


@pytest.fixture
def client(repo: Repo, clock: FakeClock) -> TestClient:
    return TestClient(create_app(settings(), repo, clock), base_url="http://127.0.0.1:8080")


def test_pages_render(client: TestClient, repo: Repo) -> None:
    add(repo, "a", company="Rubrik")
    add(repo, "b", company="Acme", status=JobStatus.APPLIED, you=True)
    today = client.get("/")
    assert today.status_code == 200 and "Rubrik" in today.text and "Acme" not in today.text
    assert "Acme" in client.get("/applications").text
    assert "Rubrik" in client.get("/jobs?q=rub").text
    job = client.get("/jobs/a")
    assert job.status_code == 200 and "https://acme.com/jobs/a" in job.text
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert '<symbol id="blip"' in today.text and "Good afternoon" in today.text


def test_errors_are_pages(client: TestClient) -> None:
    missing = client.get("/jobs/nope")
    assert missing.status_code == 404 and "Not found" in missing.text
    bad = client.get("/jobs?page=abc")
    assert bad.status_code == 400 and "doesn't look right" in bad.text


def test_job_text_is_escaped(client: TestClient, repo: Repo) -> None:
    add(repo, "x", company="<script>alert(1)</script>")
    body = client.get("/jobs/x").text
    assert "<script>alert(1)" not in body and "&lt;script&gt;" in body


def test_only_this_computer(client: TestClient, repo: Repo) -> None:
    # A site that points its own name at 127.0.0.1 ("DNS rebinding") gets nothing.
    assert client.get("/", headers={"host": "evil.example:8080"}).status_code == 400
    assert client.get("/", headers={"host": "localhost:8080"}).status_code == 200
    headers = client.get("/").headers
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["cache-control"] == "no-store" and "server" not in headers


def test_never_every_network() -> None:
    with pytest.raises(ValidationError, match="Wi-Fi"):
        WebConfig(host="0.0.0.0")  # noqa: S104


def test_busy_port_takes_the_next(monkeypatch: pytest.MonkeyPatch) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        assert free_port("127.0.0.1", port) != port
        monkeypatch.setattr(server, "PORT_TRIES", 1)
        with pytest.raises(NoFreePort, match="in use"):
            free_port("127.0.0.1", port)
