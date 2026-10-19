import json
from datetime import timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

from jobradar.config import AppConfig, Settings
from jobradar.db.models import JobStatus
from jobradar.db.repo import Repo
from jobradar.pipeline.skill_gaps import gap_line, job_skill_keys, skill_gaps
from jobradar.profile import skill_warnings
from jobradar.queue.tasks import TaskQueue
from jobradar.sinks import digest as digest_mod
from jobradar.sinks.digest import Digest
from tests.conftest import FakeClock
from tests.test_digest import settings as digest_settings

PROFILE: dict[str, Any] = {"skills": {"languages": ["Python", "SQL"], "infra": ["Docker"]}}


def add(repo: Repo, job_id: str, score: int, text: str = "", skills: list[str] | None = None,
        status: JobStatus = JobStatus.NEW) -> None:  # fmt: skip
    repo.get_or_create_job(job_id, f"https://acme.com/{job_id}")
    data = json.dumps({"skills_required": skills or []})
    repo.save_extraction(job_id, data, "Acme", f"Role {job_id}", None, "rules-v1",
                         JobStatus.EXTRACTED)  # fmt: skip
    repo.set_match_score(job_id, score, f"Good fit · {score}")
    if status != JobStatus.NEW:
        repo.set_job_status(job_id, status)
    if text:
        repo.save_page(job_id, f"https://acme.com/{job_id}", "Role", text)


def test_counts_each_missing_skill_once_per_job(repo: Repo, clock: FakeClock) -> None:
    add(repo, "a", 80, "Python, AWS and Kubernetes (k8s). AWS again.")
    add(repo, "b", 90, "We use AWS, Docker and C++ daily.")
    add(repo, "c", 85, skills=["Kubernetes", "Teamwork"])  # a stated list counts too
    add(repo, "weak", 40, "AWS AWS AWS C++")  # not a good match
    gaps, looked_at = skill_gaps(repo, PROFILE, clock() - timedelta(days=7), 75)
    assert looked_at == 3
    assert [(g.name, g.jobs) for g in gaps] == [("AWS", 2), ("Kubernetes", 2)]  # C++: one job
    assert gap_line(gaps) == "AWS in 2 good matches, Kubernetes in 2"


def test_everyday_words_count_only_from_a_stated_list(repo: Repo) -> None:
    repo.get_or_create_job("j", "https://acme.com/j")
    job = repo.get_job("j")
    assert job is not None
    text = "Let's go! Join us this spring to express your ideas in Excel."
    assert job_skill_keys(job, text, "") == set()
    job.data_json = json.dumps({"skills_required": ["Go", "Spring"]})
    assert job_skill_keys(job, text, "") == {"go", "spring"}


def test_synonyms_count_once(repo: Repo) -> None:
    repo.get_or_create_job("j", "https://acme.com/j")
    job = repo.get_job("j")
    assert job is not None
    assert job_skill_keys(job, "GCP or Google Cloud; LLMs and an LLM; k8s, Kubernetes", "") == {
        "googlecloud", "llm", "kubernetes"
    }  # fmt: skip


def test_a_skill_cut_at_a_comma_is_flagged() -> None:
    cut = {"skills": {"additional": ["Machine Learning (feature engineering", "evaluation)"]}}
    assert len(skill_warnings(cut)) == 2
    whole = {"skills": {"additional": ["Machine Learning (feature engineering, evaluation)"]}}
    assert skill_warnings(whole) == []


# ---- the weekly digest section --------------------------------------------------------------


def settings(tmp_path: Path, **notify: Any) -> Settings:
    (tmp_path / "profile.yaml").write_text(json.dumps(PROFILE))
    s = digest_settings(**notify)
    config = s.config.model_dump()
    config["matching"]["profile_path"] = str(tmp_path / "profile.yaml")
    return Settings(config=AppConfig.model_validate(config), secrets=s.secrets)


async def test_first_digest_of_the_week_lists_them(
    repo: Repo, queue: TaskQueue, clock: FakeClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[EmailMessage] = []
    monkeypatch.setattr(digest_mod, "smtp_send", lambda s, m: sent.append(m))
    add(repo, "a", 80, "AWS and Kubernetes")
    add(repo, "b", 90, "AWS and Kubernetes")
    d = Digest(settings(tmp_path), repo, queue, clock=clock, preview_dir=tmp_path)
    await d.send_now()
    plain = str(sent[0].get_body(("plain",)).get_content())  # type: ignore[union-attr]
    html = str(sent[0].get_body(("html",)).get_content())  # type: ignore[union-attr]
    assert "SKILLS YOU KEEP MISSING" in plain and "AWS in 2 good matches, Kubernetes in 2" in plain
    assert "Skills you keep missing" in html and "2 jobs" in html

    clock.advance(10 * 3600)  # the evening digest, same week
    add(repo, "c", 85, "AWS")
    await d.send_now()
    assert "SKILLS YOU KEEP MISSING" not in str(sent[1].get_body(("plain",)).get_content())  # type: ignore[union-attr]

    clock.advance(7 * 86400)  # next week
    add(repo, "d", 85, "AWS and Kubernetes")
    await d.send_now()
    assert "SKILLS YOU KEEP MISSING" in str(sent[2].get_body(("plain",)).get_content())  # type: ignore[union-attr]


async def test_off_switch(
    repo: Repo, queue: TaskQueue, clock: FakeClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[EmailMessage] = []
    monkeypatch.setattr(digest_mod, "smtp_send", lambda s, m: sent.append(m))
    add(repo, "a", 80, "AWS")
    add(repo, "b", 90, "AWS")
    d = Digest(settings(tmp_path, skill_gaps=False), repo, queue, clock=clock, preview_dir=tmp_path)
    await d.send_now()
    assert "SKILLS YOU KEEP MISSING" not in str(sent[0].get_body(("plain",)).get_content())  # type: ignore[union-attr]
