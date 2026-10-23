from fastapi.testclient import TestClient

from jobradar.by_source import NEXT, REJECTED, WAITING, by_source, render
from jobradar.db.models import JobStatus, Platform
from jobradar.db.repo import Repo
from jobradar.web.app import create_app
from tests.conftest import FakeClock
from tests.test_digest import settings
from tests.test_web import add

MINUTE = 60


def seen(repo: Repo, job_id: str, chat: str, title: str, message: str,
         platform: Platform = Platform.TELEGRAM) -> None:  # fmt: skip
    source = repo.upsert_source(platform, chat, title)
    assert source.id is not None
    raw, _ = repo.save_raw_message(source.id, message, "2026-10-08T08:00:00.000000Z", "post")
    repo.add_job_source(job_id, raw)


def applied(repo: Repo, clock: FakeClock, job_id: str, *later: JobStatus) -> None:
    """Apply, then each later status an hour apart."""
    repo.set_user_status(job_id, JobStatus.APPLIED)
    for status in later:
        clock.advance(60 * MINUTE)
        repo.set_user_status(job_id, status)


def counts(repo: Repo) -> dict[str, tuple[int, int]]:
    return {r.name: (r.next_round, r.applied) for r in by_source(repo).rows}


def test_a_repost_in_the_same_chat_is_one_application(repo: Repo, clock: FakeClock) -> None:
    add(repo, "j")
    seen(repo, "j", "-1", "DotAware", "1")
    clock.advance(MINUTE)
    seen(repo, "j", "-1", "DotAware", "2")  # reposted
    applied(repo, clock, "j")
    report = by_source(repo)
    assert counts(repo) == {"DotAware": (0, 1)}
    assert report.applications == 1


def test_a_job_from_two_sources_counts_once_in_each_and_once_in_total(
    repo: Repo, clock: FakeClock
) -> None:
    add(repo, "j")
    seen(repo, "j", "-1", "DotAware", "1")
    clock.advance(MINUTE)
    seen(repo, "j", "naukri", "freehire · Naukri", "2", Platform.BOARD)
    applied(repo, clock, "j")
    report = by_source(repo)
    assert counts(repo) == {"DotAware": (0, 1), "freehire · Naukri": (0, 1)}
    assert report.applications == 1
    first = {r.name: r.jobs[0].first for r in report.rows}
    assert first == {"DotAware": True, "freehire · Naukri": False}
    assert {r.name: r.platform for r in report.rows}["freehire · Naukri"] == "Board"


def test_an_interview_undone_seconds_later_is_ignored(repo: Repo, clock: FakeClock) -> None:
    add(repo, "j")
    seen(repo, "j", "-1", "Startupjobs", "1")
    applied(repo, clock, "j", JobStatus.INTERVIEW)
    clock.advance(8)
    repo.set_user_status("j", JobStatus.APPLIED)  # the misclick, put back
    assert counts(repo) == {"Startupjobs": (0, 1)}
    assert (
        by_source(repo).summary == "1 application from 1 source. None has reached an interview yet."
    )


def test_an_applied_undone_within_ten_minutes_is_not_an_application(
    repo: Repo, clock: FakeClock
) -> None:
    add(repo, "oops")
    seen(repo, "oops", "-1", "DotAware", "1")
    repo.set_user_status("oops", JobStatus.APPLIED)
    clock.advance(2 * MINUTE)
    repo.set_user_status("oops", JobStatus.SHORTLISTED)
    assert by_source(repo).rows == []


def test_moving_forward_quickly_still_counts(repo: Repo, clock: FakeClock) -> None:
    add(repo, "j")
    seen(repo, "j", "-1", "DotAware", "1")
    repo.set_user_status("j", JobStatus.APPLIED)
    clock.advance(MINUTE)
    repo.set_user_status("j", JobStatus.INTERVIEW)  # a quick invite: Applied was no misclick
    assert counts(repo) == {"DotAware": (1, 1)}


def test_each_job_gets_one_outcome(repo: Repo, clock: FakeClock) -> None:
    for job_id in ("interviewed", "turned_down", "waiting", "dropped", "never"):
        add(repo, job_id)
        seen(repo, job_id, "-1", "DotAware", job_id)
    applied(repo, clock, "interviewed", JobStatus.INTERVIEW, JobStatus.REJECTED)
    applied(repo, clock, "turned_down", JobStatus.REJECTED)
    applied(repo, clock, "waiting")
    applied(repo, clock, "dropped", JobStatus.SKIPPED)  # you moved on, but you did apply
    repo.set_user_status("never", JobStatus.SHORTLISTED)
    clock.advance(60 * MINUTE)
    (row,) = by_source(repo).rows
    assert {a.job_id: a.outcome for a in row.jobs} == {
        "interviewed": NEXT,  # rejected after an interview still reached the next round
        "waiting": WAITING,
        "dropped": WAITING,
        "turned_down": REJECTED,
    }
    assert [a.outcome for a in row.jobs] == [NEXT, WAITING, WAITING, REJECTED]
    assert (row.next_round, row.applied) == (1, 4)


def test_sources_you_never_applied_from_are_left_out(repo: Repo, clock: FakeClock) -> None:
    add(repo, "a")
    seen(repo, "a", "-1", "DotAware", "1")
    add(repo, "b")
    seen(repo, "b", "-2", "Freshershunt", "1")
    applied(repo, clock, "a")
    assert list(counts(repo)) == ["DotAware"]


def test_rows_sort_by_next_rounds_then_applications(repo: Repo, clock: FakeClock) -> None:
    for n in range(3):
        add(repo, f"many{n}")
        seen(repo, f"many{n}", "-1", "Many", str(n))
        applied(repo, clock, f"many{n}")
    add(repo, "invite")
    seen(repo, "invite", "-2", "Invite", "1")
    applied(repo, clock, "invite", JobStatus.INTERVIEW)
    clock.advance(60 * MINUTE)
    assert list(counts(repo)) == ["Invite", "Many"]


def test_the_terminal_shows_one_line_per_source(repo: Repo, clock: FakeClock) -> None:
    assert render(by_source(repo)).startswith("No applications yet")
    add(repo, "j")
    seen(repo, "j", "-1", "DotAware", "1")
    applied(repo, clock, "j", JobStatus.REJECTED)
    text = render(by_source(repo))
    assert "1 application from 1 source." in text
    assert "DotAware" in text and "x" in text and "0 of 1" in text


def test_the_applications_page_shows_the_strip_once_you_apply(repo: Repo, clock: FakeClock) -> None:
    client = TestClient(create_app(settings(), repo, clock), base_url="http://127.0.0.1:8080")
    assert "Where your applications came from" not in client.get("/applications").text
    add(repo, "j", company="Rubrik", role="Backend Engineer")
    seen(repo, "j", "-1", "DotAware", "1")
    applied(repo, clock, "j")
    page = client.get("/applications").text
    assert "Where your applications came from" in page
    assert "1 application from 1 source. None has reached an interview yet." in page
    assert "DotAware" in page and "0 of 1" in page and "found here first" in page


def test_the_funnel_ignores_the_same_misclicks(repo: Repo, clock: FakeClock) -> None:
    from jobradar.web import views

    add(repo, "j")
    applied(repo, clock, "j", JobStatus.OFFER)
    clock.advance(4)
    repo.set_user_status("j", JobStatus.APPLIED)  # Offer put back 4 seconds later
    steps = {s.name: s.jobs for s in views.funnel(repo, 75)}
    assert (steps["Applied"], steps["Interview"], steps["Offer"]) == (1, 0, 0)
