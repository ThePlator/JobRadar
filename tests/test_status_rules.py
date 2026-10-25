from datetime import date

from jobradar.db.models import JobStatus
from jobradar.db.repo import Repo
from jobradar.pipeline import status_rules as rules
from jobradar.pipeline.task_types import BUILD_RESUME, NOTION_STATUS
from jobradar.queue.tasks import TaskQueue
from jobradar.sinks.notion_sync import FOLLOWED_UP
from tests.test_web import add

TODAY = date(2026, 10, 20)


def job(repo: Repo, job_id: str = "j") -> object:
    found = repo.get_job(job_id)
    assert found is not None
    return found


def queued(queue: TaskQueue, kind: str) -> list[dict[str, object]]:
    out = []
    while (task := queue.claim([kind])) is not None:
        out.append(task.payload)
        queue.complete(task)
    return out


def test_buttons_move_forward_only(repo: Repo, queue: TaskQueue) -> None:
    add(repo, "j")
    done = rules.act(repo, queue, job(repo), "applied", TODAY)  # type: ignore[arg-type]
    assert (done.ok, done.before, done.after) == (True, "new", "applied")
    assert done.message == "Marked as applied: Software Engineer @ Acme."
    assert queued(queue, NOTION_STATUS) == [{"status": "Applied"}]
    late = rules.act(repo, queue, job(repo), "skip", TODAY)  # type: ignore[arg-type]
    assert not late.ok and "is Applied now" in late.message and "Change status" in late.message
    assert job(repo).status == "applied"  # type: ignore[attr-defined]
    assert not rules.act(repo, queue, job(repo), "dance", TODAY).ok  # type: ignore[arg-type]


def test_shortlist_builds_a_resume_once(repo: Repo, queue: TaskQueue) -> None:
    add(repo, "j")
    rules.act(repo, queue, job(repo), "shortlist", TODAY)  # type: ignore[arg-type]
    assert queued(queue, BUILD_RESUME) == [{"why": "shortlisted"}]
    assert queued(queue, NOTION_STATUS) == [{"status": "Shortlisted"}]
    add(repo, "k")
    repo.save_artifact("k", "resume", 1, "out.pdf", "{}")
    rules.act(repo, queue, job(repo, "k"), "shortlist", TODAY)  # type: ignore[arg-type]
    assert queued(queue, BUILD_RESUME) == []  # it has one already
    add(repo, "m")
    rules.act(repo, queue, job(repo, "m"), "shortlist", TODAY, build_resumes=False)  # type: ignore[arg-type]
    assert queued(queue, BUILD_RESUME) == []


def test_followed_up_keeps_the_status(repo: Repo, queue: TaskQueue) -> None:
    add(repo, "j", status=JobStatus.APPLIED, you=True)
    done = rules.act(repo, queue, job(repo), "followed up", TODAY)  # type: ignore[arg-type]
    assert done.ok and done.after == "applied"
    assert repo.get_user_setting(FOLLOWED_UP + "j") == "2026-10-20"
    assert queued(queue, NOTION_STATUS) == [{"followed_up": "2026-10-20"}]


def test_change_status_goes_anywhere_of_yours(repo: Repo, queue: TaskQueue) -> None:
    add(repo, "j", status=JobStatus.APPLIED, you=True)
    back = rules.set_status(repo, queue, job(repo), "skipped")  # type: ignore[arg-type]
    assert (back.ok, back.after, back.message) == (
        True,
        "skipped",
        "Now Skipped: Software Engineer @ Acme.",
    )
    assert queued(queue, NOTION_STATUS) == [{"status": "Skipped"}]
    assert repo.status_history("j")[-1][0] == "skipped"
    assert not rules.set_status(repo, queue, job(repo), "discarded").ok  # JobRadar's, not yours
    assert rules.set_status(repo, queue, job(repo), "skipped").message == "Already Skipped."
    assert rules.notion_option(JobStatus.RESUME_READY) == "Shortlisted"
