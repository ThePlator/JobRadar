import json
import re
import socket
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from jobradar.config import WebConfig
from jobradar.db.models import JobStatus, Platform
from jobradar.db.repo import Repo
from jobradar.pipeline.schemas import JobPosting
from jobradar.queue.tasks import TaskQueue
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
    board = views.board(repo, settings(), clock())
    columns = {c.key: [b.card.id for b in c.cards] for c in board}
    assert columns == {
        "shortlisted": ["r", "s"],
        "applied": ["a2", "a1"],  # the latest change first
        "interview": [],
        "offer": [],
        "rejected": ["x"],
    }


def test_board_cards_track_follow_ups_and_replies(repo: Repo, clock: FakeClock) -> None:
    add(repo, "quiet", company="Rubrik", status=JobStatus.APPLIED, you=True)
    add(repo, "heard", company="Acme", status=JobStatus.APPLIED, you=True)
    repo.set_setting("company_replied:heard", json.dumps({"kind": "interview"}))
    repo.save_artifact("quiet", "resume", 1, "out.pdf", "{}")
    clock.advance(4 * 86400)
    applied = {b.card.id: b for b in views.board(repo, settings(), clock())[1].cards}
    quiet, heard = applied["quiet"].track, applied["heard"].track
    assert (quiet.resume, quiet.nudge_in, quiet.next_nudge, quiet.waited) == (True, 6, "18 Oct", 40)
    assert heard.replied == "interview" and heard.next_nudge == ""  # no nudging after a reply
    repo.set_setting("followed_up:quiet", "2026-10-11")  # you followed up: the wait restarts
    again = {b.card.id: b for b in views.board(repo, settings(), clock())[1].cards}["quiet"].track
    assert again.followed_up == "11 Oct" and again.next_nudge == "21 Oct"
    assert views.board(repo, settings(), clock())[1].note.startswith("1 replied")


def test_search_and_show(repo: Repo, clock: FakeClock) -> None:
    add(repo, "r", company="Rubrik")
    add(repo, "d", company="Acme", role="Data Engineer")
    add(repo, "p", company="100% Remote Co")
    add(repo, "h", company="Rubrik", status=JobStatus.HIDDEN)
    add(repo, "n", company="Junk", status=JobStatus.DISCARDED)
    ids = lambda show="jobs", q="": {c.id for c in views.all_jobs(repo, TZ, show, q, 1).cards}  # noqa: E731
    assert ids() == {"r", "d", "p"}  # hidden and not-a-job left out
    assert ids(q="rubrik") == {"r"} and ids(q="data eng") == {"d"}
    assert ids(q="%") == ids()  # no words in it: no search
    assert ids(q='" OR *') == set()  # operators are only words here: no error, no match
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


# ---- charts, funnel, score parts, palette ------------------------------------------------------


def test_pulse_counts_each_day(repo: Repo, clock: FakeClock) -> None:
    add(repo, "a", score=90)
    add(repo, "b", score=50)
    add(repo, "x", score=None, status=JobStatus.DISCARDED)  # not a job: not counted
    clock.advance(86400)
    add(repo, "c", score=80)
    p = views.pulse(repo, 75, clock(), TZ)
    assert len(p.days) == views.PULSE_DAYS and p.days[-1].today
    assert [(d.found, d.good) for d in p.days[-2:]] == [(2, 1), (1, 1)]
    assert (p.found, p.good, p.peak) == (3, 2, 2)


def test_sources_group_boards(repo: Repo, clock: FakeClock) -> None:
    def seen(job_id: str, platform: Platform, chat: str, title: str) -> None:
        source = repo.upsert_source(platform, chat, title)
        assert source.id is not None
        raw, _ = repo.save_raw_message(source.id, job_id, "2026-10-08T08:00:00.000000Z", "x")
        repo.add_job_source(job_id, raw)

    for n in range(3):
        add(repo, f"t{n}", score=90)
        seen(f"t{n}", Platform.TELEGRAM, "-1001", "Freshershunt")
    add(repo, "g", score=90)
    seen("g", Platform.BOARD, "greenhouse:stripe", "Greenhouse · Stripe")
    add(repo, "l", score=85)
    seen("l", Platform.BOARD, "lever:meesho", "Lever · Meesho")
    add(repo, "weak", score=40)
    seen("weak", Platform.TELEGRAM, "-1002", "Spam jobs")
    got = [(s.name, s.jobs, s.share) for s in views.sources(repo, 75, clock())]
    assert got == [("Freshershunt", 3, 100), ("Company boards", 2, 67)]


def test_funnel_counts_jobs_that_moved_on(repo: Repo, clock: FakeClock) -> None:
    add(repo, "a", score=90, status=JobStatus.APPLIED, you=True)
    add(repo, "b", score=95, status=JobStatus.INTERVIEW, you=True)
    repo.set_user_status("b", JobStatus.REJECTED)  # rejected after the interview
    add(repo, "c", score=80)
    add(repo, "d", score=30, status=JobStatus.HIDDEN)
    add(repo, "x", score=None, status=JobStatus.DISCARDED)
    steps = {s.name: (s.jobs, s.rate) for s in views.funnel(repo, 75)}
    assert steps["Found"] == (4, "")
    assert steps["Scored 75+"] == (3, "75% of found")
    # b is Rejected now, but it reached Interview, so it counts as applied and interviewed
    assert steps["Applied"] == (2, "100% of shortlisted")
    assert steps["Interview"] == (1, "50% of applied") and steps["Offer"] == (0, "0% of interview")


def test_breakdown_and_skills(repo: Repo) -> None:
    weights = {"skills_coverage": 0.4, "role_alignment": 0.3, "seniority_fit": 0.15,
               "location_ok": 0.15}  # fmt: skip
    answers = json.dumps({"provider": "llm", "eligible": 0.95, "skills_coverage": 0.5,
                          "role_alignment": 1.0, "confidence": 0.8})  # fmt: skip
    b = views.breakdown(answers, weights)
    assert b is not None and (b.eligible, b.confidence, b.by) == (95, 80, "AI read the whole job")
    assert [(p.name, p.value, p.weight) for p in b.parts] == [
        ("Skills you have", 50, 40), ("Role fits what you want", 100, 30)
    ]  # fmt: skip
    assert views.breakdown(None, weights) is None and views.breakdown("{bad", weights) is None
    profile = {"skills": {"languages": ["Python", "SQL"]}}
    got = [(s.name, s.have) for s in views.skills(["Python", "Kafka"], profile)]
    assert got == [("Python", True), ("Kafka", False)]
    assert views.skills(["Go"], None)[0].have is None


def test_quick_filter_tags(repo: Repo, clock: FakeClock) -> None:
    add(repo, "i", role="SDE Intern", work_mode="remote", deadline="2026-10-10")
    c = views.today(repo, 75, clock(), TZ)[0]
    assert set(c.tags.split()) == {"eligible", "remote", "internship", "closing"}
    assert c.closes_in == 2


def test_palette_search(client: TestClient, repo: Repo) -> None:
    add(repo, "r", company="Rubrik", role="SDE")
    jobs = client.get("/api/search", params={"q": "rub"}).json()["jobs"]
    assert jobs == [{"id": "r", "role": "SDE", "company": "Rubrik", "score": 90, "status": "New"}]
    assert client.get("/api/search").json() == {"jobs": []}


def test_static_files_get_a_new_address_when_they_change(client: TestClient) -> None:
    page = client.get("/").text
    js = re.search(r'src="(/static/app\.js\?v=[0-9a-f]{10})"', page)
    css = re.search(r'href="(/static/app\.css\?v=[0-9a-f]{10})"', page)
    assert js and css  # the browser can't keep running an old app.js after an update
    assert client.get(js.group(1)).headers["cache-control"] == "no-cache"


def test_today_cards_carry_what_the_toolbar_sorts_by(
    client: TestClient, repo: Repo, clock: FakeClock
) -> None:
    add(repo, "a", company="Rubrik", role="SDE Intern", deadline="2026-10-09")
    clock.advance(7 * 3600)
    add(repo, "b", company="Acme", score=80)
    c = {x.id: x for x in views.today(repo, 75, clock(), TZ)}
    assert (c["a"].fresh, c["a"].age, c["b"].fresh, c["b"].age) == (
        False,
        "7 h ago",
        True,
        "just now",
    )
    assert c["a"].url == "https://acme.com/jobs/a"
    page = client.get("/").text
    assert 'data-filter="internship"' in page and "data-sort" in page and "data-find" in page
    assert 'data-text="sde intern rubrik' in page and 'data-closes="1"' in page  # tomorrow


# ---- the digest page and sorting by time ------------------------------------------------------


def test_digest_windows_follow_the_digest_times(clock: FakeClock) -> None:
    now = clock().astimezone(TZ)  # Thu 8 Oct, 14:30 IST; digests at 09:00 and 19:00
    w = views.windows(now, ["09:00", "19:00"])
    assert (w[0].key, w[0].label, w[0].current) == ("now", "So far", True)
    assert w[0].since.hour == 9 and w[0].until == now
    assert (w[1].key, w[1].label) == ("2026-10-08-0900", "Morning · 8 Oct")
    assert (w[1].since.day, w[1].since.hour, w[2].label) == (7, 19, "Evening · 7 Oct")
    assert len(w) == views.DIGEST_WINDOWS


def test_an_earlier_digest_leaves_out_what_came_after(
    client: TestClient, repo: Repo, clock: FakeClock
) -> None:
    clock.now = clock.now.replace(hour=2)  # 07:30 IST: in the window before the 09:00 digest
    add(repo, "early", company="Rubrik", score=90)
    clock.now = clock.now.replace(hour=5)  # 10:30 IST: after it
    add(repo, "late", company="Acme", score=95)
    morning = views.digest(repo, settings(), clock(), "", None)  # default: the latest sent
    assert morning.title == "Morning digest" and [c.id for c in morning.inbox] == ["early"]
    so_far = views.digest(repo, settings(), clock(), "now", None)
    assert so_far.title == "Next digest, so far" and [c.id for c in so_far.inbox] == ["late"]
    page = client.get("/digest")
    assert page.status_code == 200 and "Rubrik" in page.text and "Acme" not in page.text
    email = client.get("/digest/email", params={"w": "now"})
    assert "Acme" in email.text and "Rubrik" not in email.text
    assert email.headers["content-security-policy"].startswith("default-src 'none'")
    assert "script" not in email.headers["content-security-policy"]


def test_all_jobs_sort_by_time(repo: Repo, clock: FakeClock) -> None:
    add(repo, "first", score=50)
    clock.advance(60)
    add(repo, "second", score=99)
    order = lambda sort: [c.id for c in views.all_jobs(repo, TZ, "jobs", "", 1, sort=sort).cards]  # noqa: E731
    assert order("new") == ["second", "first"] and order("old") == ["first", "second"]
    assert order("score") == ["second", "first"] and order("nonsense") == order("new")


# ---- changing a status from the page -----------------------------------------------------------


def test_buttons_need_the_page_token(client: TestClient, repo: Repo) -> None:
    add(repo, "j", company="Rubrik")
    token = repo.get_setting("web_token")
    assert token and f'content="{token}"' in client.get("/").text
    assert client.post("/jobs/j/act", content="action=applied").status_code == 403
    bad = {"x-jobradar-token": "guess", "content-type": "application/x-www-form-urlencoded"}
    assert client.post("/jobs/j/act", content="action=applied", headers=bad).status_code == 403
    other_site = {"x-jobradar-token": token, "origin": "https://evil.example",
                  "content-type": "application/x-www-form-urlencoded"}  # fmt: skip
    assert (
        client.post("/jobs/j/act", content="action=applied", headers=other_site).status_code == 403
    )
    job = repo.get_job("j")
    assert job is not None and job.status == "new"


def test_a_button_click(client: TestClient, repo: Repo) -> None:
    add(repo, "j", company="Rubrik")
    token = repo.get_setting("web_token") or ""
    headers = {"x-jobradar-token": token, "content-type": "application/x-www-form-urlencoded"}
    out = client.post("/jobs/j/act", content="action=applied", headers=headers).json()
    assert (out["ok"], out["status"], out["undo"]) == (True, "applied", "new")
    assert 'data-id="j"' in out["row"] and "Interview" in out["row"]  # the row's next buttons
    again = client.post("/jobs/j/act", content="action=skip", headers=headers)
    assert again.status_code == 409 and "is Applied now" in again.json()["message"]
    undo = client.post("/jobs/j/status", content="status=new", headers=headers).json()
    assert undo["ok"] and undo["status"] == "new"
    assert (
        client.post("/jobs/nope/act", content="action=applied", headers=headers).status_code == 404
    )


def test_without_javascript_a_form_post_comes_back(client: TestClient, repo: Repo) -> None:
    add(repo, "j")
    token = repo.get_setting("web_token") or ""
    form = {"content-type": "application/x-www-form-urlencoded"}
    back = client.post("/jobs/j/act", content=f"action=shortlist&token={token}&next=/jobs%3Fq%3Dx",
                       headers=form, follow_redirects=False)  # fmt: skip
    assert back.status_code == 303 and back.headers["location"] == "/jobs?q=x"
    away = client.post("/jobs/j/status", content=f"status=new&token={token}&next=//evil.com",
                       headers=form, follow_redirects=False)  # fmt: skip
    assert away.headers["location"] == "/"  # never to another site
    assert [st for st, _ in repo.status_history("j")] == ["shortlisted", "new"]


def test_cards_show_the_next_steps(repo: Repo, clock: FakeClock) -> None:
    add(repo, "n")
    add(repo, "a", status=JobStatus.APPLIED, you=True)
    add(repo, "i", status=JobStatus.INTERVIEW, you=True)
    add(repo, "o", status=JobStatus.OFFER, you=True)
    acts = lambda job_id: [a for a, _ in views.card(repo.get_job(job_id), TZ).actions]  # type: ignore[arg-type]  # noqa: E731
    assert acts("n") == ["applied", "shortlist", "skip"]
    assert acts("a") == ["interview", "followed up", "rejected"]
    assert acts("i") == ["offer", "rejected"] and acts("o") == []


# ---- step 3: inside `jobradar run`, resumes, follow-up drafts ---------------------------------


def with_storage(tmp_path: Any, **web: Any) -> Any:
    from jobradar.config import AppConfig, Settings

    base = settings()
    config = base.config.model_dump()
    config["storage"]["local_dir"] = str(tmp_path / "output")
    config["web"].update(web)
    return Settings(config=AppConfig.model_validate(config), secrets=base.secrets)


async def test_the_dashboard_runs_inside_jobradar_run(
    repo: Repo, queue: TaskQueue, tmp_path: Path
) -> None:
    import asyncio
    import signal

    import httpx

    from jobradar.app import start_dashboard

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    before = signal.getsignal(signal.SIGINT)
    woke: list[int] = []
    server = start_dashboard(with_storage(tmp_path, port=port), repo, queue,
                             lambda: woke.append(1))  # fmt: skip
    assert server is not None
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    async with httpx.AsyncClient() as http:
        page = await http.get(f"http://127.0.0.1:{port}/")
    assert page.status_code == 200 and "JobRadar" in page.text
    assert signal.getsignal(signal.SIGINT) is before  # Ctrl+C stays JobRadar's
    server.should_exit = True
    await asyncio.wait_for(task, 5)
    assert start_dashboard(with_storage(tmp_path, enabled=False), repo, queue, print) is None


def test_resume_files_only_from_the_output_folder(
    repo: Repo, tmp_path: Path, clock: FakeClock
) -> None:
    s = with_storage(tmp_path)
    out = tmp_path / "output"
    out.mkdir()
    (out / "Acme_SWE_20261020_v1.pdf").write_bytes(b"%PDF-1.4 resume")
    (out / "Acme_SWE_20261020_cover_letter.pdf").write_bytes(b"%PDF-1.4 letter")
    (tmp_path / "secret.pdf").write_bytes(b"%PDF-1.4 not yours to serve")
    add(repo, "j", company="Acme")
    ats = {"ats": {"problems": [], "skills_on_resume": ["Python"], "skills_not_on_resume": ["SQL"]},
           "tailored": True}  # fmt: skip
    good = repo.save_artifact(
        "j", "resume", 1, str(out / "Acme_SWE_20261020_v1.pdf"), json.dumps(ats)
    )
    evil = repo.save_artifact("j", "resume", 2, str(out / ".." / "secret.pdf"), "{}")
    client = TestClient(create_app(s, repo, clock), base_url="http://127.0.0.1:8080")
    pdf = client.get(f"/files/{good.id}")
    assert pdf.status_code == 200 and pdf.content == b"%PDF-1.4 resume"
    assert (
        pdf.headers["content-type"] == "application/pdf"
        and "inline" in pdf.headers["content-disposition"]
    )
    assert "content-security-policy" not in pdf.headers  # the browser's PDF viewer may show it
    assert client.get(f"/files/{good.id}?cover=1").content == b"%PDF-1.4 letter"
    assert client.get(f"/files/{evil.id}").status_code == 404  # outside storage.local_dir
    assert client.get("/files/12345").status_code == 404
    page = client.get("/jobs/j").text
    assert f'href="/files/{good.id}"' in page and f'href="/files/{good.id}?cover=1"' in page
    assert "✓ Python" in page and "Version 2" in page and "File not found" in page


def test_build_resume_button(repo: Repo, tmp_path: Path, clock: FakeClock) -> None:
    add(repo, "j")
    client = TestClient(
        create_app(with_storage(tmp_path), repo, clock), base_url="http://127.0.0.1:8080"
    )
    token = repo.get_setting("web_token") or ""
    headers = {"x-jobradar-token": token, "content-type": "application/x-www-form-urlencoded"}
    assert (
        client.post(
            "/jobs/j/resume", content="", headers={"content-type": headers["content-type"]}
        ).status_code
        == 403
    )
    out = client.post("/jobs/j/resume", content="", headers=headers).json()
    assert out["ok"] and out["count"] == 0
    assert client.get("/api/jobs/j/resumes").json() == {"count": 0, "building": True}
    assert "Building…" in client.get("/jobs/j").text


def test_follow_up_draft_on_an_applied_job(repo: Repo, tmp_path: Path, clock: FakeClock) -> None:
    add(repo, "j", company="Rubrik", role="SDE", status=JobStatus.APPLIED, you=True)
    add(repo, "n", company="Acme")
    client = TestClient(
        create_app(with_storage(tmp_path), repo, clock), base_url="http://127.0.0.1:8080"
    )
    page = client.get("/jobs/j").text
    assert "Follow-up email" in page and "SDE role at Rubrik" in page and "mailto:" in page
    assert "Follow-up email" not in client.get("/jobs/n").text


def test_alert_emails_link_to_the_dashboard(repo: Repo, tmp_path: Path) -> None:
    from jobradar.sinks.email_notify import build_email, dashboard_link

    add(repo, "j", company="Rubrik")
    job = repo.get_job("j")
    assert job is not None
    msg = build_email([job], "me@x.com", "me@x.com", dashboard=dashboard_link(settings()))
    assert "http://127.0.0.1:8080/jobs/j" in str(msg.get_body(("plain",)).get_content())  # type: ignore[union-attr]
    off = with_storage(tmp_path, enabled=False)
    assert dashboard_link(off) is None


# ---- full-text search and filters -----------------------------------------------------------


def test_search_reads_the_whole_job(repo: Repo, clock: FakeClock) -> None:
    add(repo, "k", company="Acme", role="Backend Engineer", locations=["Pune"], work_mode="hybrid",
        skills_required=["Kafka"])  # fmt: skip
    repo.save_page("k", "https://acme.com/k", "Backend", "You will build event pipelines on Kafka.")
    add(repo, "g", company="Globex", role="Frontend Developer", locations=["Bangalore"])
    repo.save_page("g", "https://acme.com/g", "Frontend", "React and TypeScript, onsite.")
    found = lambda q="", **f: views.all_jobs(repo, TZ, "jobs", q, 1, clock(),  # noqa: E731
                                             filters=views.Filters(**f))  # fmt: skip
    hit = found("kafka pipelines")
    assert [c.id for c in hit.cards] == ["k"]
    assert "<mark>pipelines</mark>" in str(hit.snippets["k"])
    assert [c.id for c in found("typescr").cards] == ["g"]  # a prefix finds the word
    assert [c.id for c in found(place="pune").cards] == ["k"]
    assert [c.id for c in found(remote=True).cards] == ["k"]  # hybrid counts
    assert {c.id for c in found().cards} == {"k", "g"}
    # A page fetched later gets into the index on the next search.
    clock.advance(60)
    repo.save_page("g", "https://acme.com/g", "Frontend", "React, TypeScript and some Kafka.")
    assert {c.id for c in found("kafka").cards} == {"k", "g"}


def test_filters_by_score_age_and_source(repo: Repo, clock: FakeClock) -> None:
    source = repo.upsert_source(Platform.TELEGRAM, "-1009", "Freshershunt")
    assert source.id is not None
    add(repo, "old", score=95)
    clock.advance(5 * 86400)
    add(repo, "weak", score=55)
    add(repo, "tg", score=92)
    raw, _ = repo.save_raw_message(source.id, "5", "2026-10-13T08:00:00.000000Z", "post")
    repo.add_job_source("tg", raw)
    ids = lambda **f: {c.id for c in views.all_jobs(repo, TZ, "jobs", "", 1, clock(),  # noqa: E731
                                                    filters=views.Filters(**f)).cards}  # fmt: skip
    assert ids(min_score=90) == {"old", "tg"} and ids(within=3) == {"weak", "tg"}
    assert ids(source=source.id) == {"tg"} and ids(min_score=90, within=3) == {"tg"}
    assert repo.sources() == [(source.id, "telegram", "Freshershunt")]


def test_all_jobs_page_with_filters(client: TestClient, repo: Repo) -> None:
    add(repo, "k", company="Acme", role="Kafka Engineer", score=91)
    page = client.get("/jobs", params={"q": "kafka", "min": 90, "remote": "", "sort": "relevance"})
    assert page.status_code == 200 and "Kafka Engineer" in page.text and "1 filter" in page.text
    assert client.get("/jobs", params={"min": 37}).status_code == 200  # an odd value: ignored


# ---- Skills ------------------------------------------------------------------------------------

PROFILE_YAML = """basics:
  name: Sameer Kumar   # me
skills:
  languages: [Python, SQL]
  additional: ["Machine Learning (feature engineering, model training)", Agile]  # keep this
experience: []
"""


def test_add_skill_touches_one_line(tmp_path: Path) -> None:
    from jobradar.config import ConfigError
    from jobradar.profile import add_skill, load_profile

    path = tmp_path / "profile.yaml"
    path.write_text(PROFILE_YAML)
    backup = add_skill(path, "AWS", "20261020-1300")
    assert (
        backup == "profile.yaml.bak-20261020-1300"
        and (tmp_path / backup).read_text() == PROFILE_YAML
    )
    after = path.read_text()
    assert "Agile, AWS]  # keep this" in after and "# me" in after  # comments stay
    assert after.count("\n") == PROFILE_YAML.count("\n")
    assert "AWS" in load_profile(path)["skills"]["additional"]
    add_skill(path, "Node.js: backend", "x")  # needs quotes in YAML
    assert load_profile(path)["skills"]["additional"][-1] == "Node.js: backend"
    with pytest.raises(ConfigError, match="already"):
        add_skill(path, "aws", "y")
    flat = tmp_path / "flat.yaml"
    flat.write_text("skills:\n  - Python\n")
    with pytest.raises(ConfigError, match="yourself"):
        add_skill(flat, "AWS", "z")
    assert flat.read_text() == "skills:\n  - Python\n"


def test_skills_page_and_i_know_this(repo: Repo, tmp_path: Path, clock: FakeClock) -> None:
    from jobradar.config import AppConfig, Settings

    path = tmp_path / "profile.yaml"
    path.write_text(PROFILE_YAML)
    base = settings().config.model_dump()
    base["matching"]["profile_path"] = str(path)
    s = Settings(config=AppConfig.model_validate(base), secrets=settings().secrets)
    for n in range(3):
        add(repo, f"j{n}", company=f"Co{n}", score=90)
        repo.save_page(f"j{n}", f"https://acme.com/j{n}", "Role", "Python, AWS and Kubernetes.")
    page = views.skills_page(repo, s, views_profile(path), clock(), 30)
    assert page.looked_at == 3
    assert [(r.name, r.jobs, r.trend) for r in page.missing] == [
        ("AWS", 3, None),
        ("Kubernetes", 3, None),
    ]
    assert [r.name for r in page.have] == ["Python"] and len(page.missing[0].samples) == 3
    client = TestClient(create_app(s, repo, clock), base_url="http://127.0.0.1:8080")
    assert "Skills you keep missing" in client.get("/skills").text
    form = {"content-type": "application/x-www-form-urlencoded"}
    assert client.post("/skills/know", content="skill=AWS", headers=form).status_code == 403
    token = repo.get_setting("web_token") or ""
    out = client.post(
        "/skills/know", content="skill=AWS", headers=form | {"x-jobradar-token": token}
    )
    assert out.json()["ok"] and "AWS" in path.read_text()
    assert "AWS" not in [
        r.name for r in views.skills_page(repo, s, views_profile(path), clock(), 30).missing
    ]
    again = client.post(
        "/skills/know", content="skill=AWS", headers=form | {"x-jobradar-token": token}
    )
    assert again.status_code == 409 and "already" in again.json()["message"]


def views_profile(path: Path) -> dict[str, Any]:
    from jobradar.profile import load_profile

    return load_profile(path)
