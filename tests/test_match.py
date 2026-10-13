import json
from typing import Any

import pytest

from jobradar.config import FiltersConfig, MatchingConfig
from jobradar.db.models import JobStatus, Platform
from jobradar.db.repo import Repo
from jobradar.llm import LLM
from jobradar.pipeline.match import Matcher, hard_rules, reason, score
from jobradar.pipeline.schemas import JobPosting
from jobradar.pipeline.task_types import MATCH, NOTION_BODY, NOTION_UPSERT
from jobradar.providers.base import JobView, MatchAnswers, ProviderUnavailable
from jobradar.providers.llm import LLMProvider, job_text
from jobradar.providers.skills_rule import SkillsRule, compare_skills, skill_key
from jobradar.queue.tasks import TaskQueue
from tests.conftest import FakeClock
from tests.fakes import FakeCompletion, llm_settings

PROFILE: dict[str, Any] = {
    "education": [
        {"degree": "B.Tech", "branch": "Electrical Engineering", "graduation_year": 2026}
    ],
    "skills": ["Python", "JavaScript (ES6+)", "HTML/CSS", "PostgreSQL", "REST API design"],
    "projects": [{"name": "JobRadar", "tags": ["docker"], "bullets": []}],
    "preferences": {"target_roles": ["Backend Engineer"], "work_modes": ["remote"]},
}
WEIGHTS = MatchingConfig().weights
CLOCK = FakeClock()  # the same start time as the `clock` fixture the repo uses
FILTERS = FiltersConfig(batch_years=[2025, 2026], max_experience_years=1)


def posting(**kw: Any) -> JobPosting:
    return JobPosting.model_validate({"company": "Acme", "role": "Backend Intern", **kw})


# ---- rules, score and reason ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("job", "because"),
    [
        ({"batch_years": [2023, 2024]}, "batch 2023-2024 only"),
        ({"batch_years": [2027]}, "batch 2027 only"),
        ({"experience_min": 3}, "needs 3+ years"),
        ({"batch_years": [2024, 2025]}, None),  # 2025 overlaps
        ({"experience_min": 1}, None),
        ({}, None),
    ],
)
def test_hard_rules(job: dict[str, Any], because: str | None) -> None:
    answers = hard_rules(posting(**job), FILTERS)
    assert (answers.not_eligible_because if answers else None) == because
    if answers:
        assert (answers.provider, answers.eligible, answers.confidence) == ("rules", 0.0, 1.0)


def test_score_is_a_weighted_mean_of_answered_dimensions() -> None:
    full = MatchAnswers(provider="llm", skills_coverage=1, role_alignment=1, seniority_fit=0,
                        location_ok=0)  # fmt: skip
    assert score(full, WEIGHTS) == 70  # 0.40 + 0.30
    skills_only = MatchAnswers(provider="skills_rule", skills_coverage=0.5)
    assert score(skills_only, WEIGHTS) == 50  # missing dimensions don't count as zero
    assert score(MatchAnswers(provider="llm"), WEIGHTS) is None
    assert score(full, {"seniority_fit": 1.0}) == 0  # re-weighting needs no new answers


def test_skill_names_are_compared_loosely() -> None:
    assert skill_key("JavaScript (ES6+)") == skill_key("JS") == "javascript"
    assert skill_key("Node.js") == "nodejs"
    job = ["Python", "CSS", "REST APIs", "Docker", "ES6", "Kafka", "Communication Skills"]
    have, missing = compare_skills(job, PROFILE)
    assert have == ["Python", "CSS", "REST APIs", "Docker", "ES6"]
    assert missing == ["Kafka"]  # soft skills are neither had nor missing


def test_reason_line() -> None:
    answers = MatchAnswers(provider="llm", eligible=0.95, location_ok=0.95, overall_fit=0.75,
                           confidence=0.6)  # fmt: skip
    job = posting(skills_required=["Python", "Kafka"], locations=["Pune"])
    line = reason(answers, 82, job, PROFILE, min_confidence=0.5)
    assert line == "Good fit · 82 · eligible ✓ · Python ✓ · missing Kafka · Pune ✓"

    unsure = answers.model_copy(update={"confidence": 0.3, "eligible": 0.05,
                                        "not_eligible_because": "CSE/IT only"})  # fmt: skip
    assert reason(unsure, 40, posting(), PROFILE, 0.5).startswith(
        "unsure · Good fit · 40 · not eligible: CSE/IT only"
    )
    ruled = hard_rules(posting(batch_years=[2023]), FILTERS)
    assert ruled is not None
    assert reason(ruled, None, posting(), PROFILE, 0.5) == "Not eligible: batch 2023 only"


# ---- providers ------------------------------------------------------------------------------


def view(**kw: Any) -> JobView:
    return JobView(posting(**kw), "Role: Backend Intern", "We build APIs.")


async def test_llm_provider_maps_levels(repo: Repo, clock: FakeClock) -> None:
    fake = FakeCompletion(
        {"eligible": "no", "not_eligible_because": "CSE/IT branches only", "location_ok": "yes",
         "role_alignment": 3, "skills_coverage": "2", "seniority_fit": 2, "overall_fit": 9,
         "confidence": 0.9}
    )  # fmt: skip
    llm = LLM(llm_settings(), repo, clock=clock, completion=fake)
    provider = LLMProvider(llm, "gemini/gemini-flash-lite-latest", branch_strict=True)
    answers = await provider.match(view(), PROFILE)
    assert answers.eligible == 0.05 and answers.not_eligible_because == "CSE/IT branches only"
    assert (answers.role_alignment, answers.seniority_fit, answers.overall_fit) == (1, 1, 1)
    assert answers.skills_coverage == pytest.approx(2 / 3)
    assert answers.confidence == 0.6  # capped: LLM-only matches alert less readily
    prompt = fake.calls[0]["messages"][0]["content"]
    assert "CSE/IT only' for an Electrical candidate" in prompt
    assert "Electrical with Electronics (EEE/ECE)" in prompt
    assert "Electrical Engineering" in prompt and "We build APIs." in prompt


async def test_llm_provider_unusable_output_falls_through(repo: Repo, clock: FakeClock) -> None:
    llm = LLM(llm_settings(), repo, clock=clock, completion=FakeCompletion("no", "still no"))
    provider = LLMProvider(llm, "gemini/gemini-flash-lite-latest", branch_strict=False)
    with pytest.raises(ProviderUnavailable):
        await provider.match(view(), PROFILE)


def test_job_text_skips_empty_parts_and_private_fields() -> None:
    text = job_text(JobView(posting(apply_url="https://acme.com/apply"), "", "Page body"))
    assert "Post:" not in text and "Page:\nPage body" in text
    assert "apply_url" not in text


async def test_skills_rule() -> None:
    answers = await SkillsRule().match(view(skills_required=["Python", "Kafka"]), PROFILE)
    assert (answers.skills_coverage, answers.confidence) == (0.5, 0.0)
    with pytest.raises(ProviderUnavailable):
        await SkillsRule().match(view(), PROFILE)


# ---- the match task -------------------------------------------------------------------------


class FakeProvider:
    def __init__(self, name: str, answers: MatchAnswers | None) -> None:
        self.name = name
        self.answers = answers
        self.calls = 0

    async def match(self, job: JobView, profile: dict[str, Any]) -> MatchAnswers:
        self.calls += 1
        if self.answers is None:
            raise ProviderUnavailable("down")
        return self.answers


GOOD = MatchAnswers(provider="llm", eligible=0.95, location_ok=1.0, role_alignment=1,
                    skills_coverage=1, seniority_fit=1, overall_fit=1, confidence=0.6)  # fmt: skip


def seed_job(repo: Repo, job_id: str = "job1", **data: Any) -> None:
    src = repo.upsert_source(Platform.TELEGRAM, "-1001", "Jobs")
    assert src.id is not None
    raw_id, _ = repo.save_raw_message(src.id, job_id, "2026-10-07T08:00:00.000000Z", "post")
    repo.get_or_create_job(job_id, f"https://acme.com/{job_id}")
    repo.add_job_source(job_id, raw_id)
    body = posting(**data).model_dump(mode="json")
    repo.save_extraction(job_id, json.dumps(body), "Acme", "Backend Intern", None, "rules-v1",
                         JobStatus.EXTRACTED)  # fmt: skip


def matcher(repo: Repo, queue: TaskQueue, *providers: Any, **matching: Any) -> Matcher:
    return Matcher(repo, queue, list(providers), PROFILE, "v1",
                   MatchingConfig(**matching), FILTERS)  # fmt: skip


async def run(queue: TaskQueue, m: Matcher) -> list[str]:
    done = []
    while (task := queue.claim()) is not None:
        done.append(task.type)
        if task.type == MATCH:
            await m.match(task)
        queue.complete(task)
    return done


async def test_match_scores_the_job_and_syncs_notion(repo: Repo, queue: TaskQueue) -> None:
    seed_job(repo)
    down, llm = FakeProvider("llm", None), FakeProvider("llm", GOOD)
    m = matcher(repo, queue, down, llm)
    queue.enqueue(MATCH, "job1")
    assert await run(queue, m) == [MATCH, NOTION_UPSERT, NOTION_BODY]
    assert (down.calls, llm.calls) == (1, 1)  # the first provider failed, the next answered
    job = repo.get_job("job1")
    assert job is not None and (job.score, job.status) == (100, JobStatus.NEW)
    assert job.score_reason == "Strong fit · 100 · eligible ✓"
    assert repo.match_answers("v1")[0][0] == "job1"


async def test_hard_rules_need_no_provider(repo: Repo, queue: TaskQueue) -> None:
    seed_job(repo, batch_years=[2023])
    provider = FakeProvider("llm", GOOD)
    m = matcher(repo, queue, provider)
    queue.enqueue(MATCH, "job1")
    await run(queue, m)
    job = repo.get_job("job1")
    assert provider.calls == 0
    assert job is not None and (job.status, job.score_reason) == (
        JobStatus.HIDDEN,
        "not eligible: batch 2023 only",
    )


async def test_no_provider_answers(repo: Repo, queue: TaskQueue) -> None:
    seed_job(repo)
    m = matcher(repo, queue, FakeProvider("llm", None))
    queue.enqueue(MATCH, "job1")
    await run(queue, m)
    job = repo.get_job("job1")
    assert job is not None and job.score is None and job.score_reason == "not matched"
    assert repo.match_answers("v1") == []


async def test_catch_up_rescores_and_queues(repo: Repo, queue: TaskQueue) -> None:
    seed_job(repo, "job1")
    seed_job(repo, "job2")
    provider = FakeProvider("llm", GOOD.model_copy(update={"seniority_fit": 0.0}))
    m = matcher(repo, queue, provider)
    queue.enqueue(MATCH, "job1")
    await run(queue, m)
    first = repo.get_job("job1")
    assert first is not None and first.score == 85

    # New weights: job1 is re-scored from its stored answers; job2 was never matched.
    m2 = matcher(repo, queue, provider, weights={"seniority_fit": 1.0, "skills_coverage": 1.0})
    assert m2.catch_up() == (1, 1)
    assert provider.calls == 1  # no new call for job1
    rescored = repo.get_job("job1")
    assert rescored is not None and rescored.score == 50
    assert m2.catch_up() == (0, 1)  # nothing changed; job2 still waiting for its task


async def test_closed_jobs_are_left_alone(repo: Repo, queue: TaskQueue) -> None:
    seed_job(repo)
    repo.set_job_status("job1", JobStatus.SHORTLISTED)
    provider = FakeProvider("llm", GOOD)
    queue.enqueue(MATCH, "job1")
    await run(queue, matcher(repo, queue, provider))
    assert provider.calls == 0


class Alerts:
    def __init__(self) -> None:
        self.queued: list[str] = []

    def queue_alert(self, job_id: str) -> None:
        self.queued.append(job_id)


async def test_strong_match_alerts_weak_one_hides(repo: Repo, queue: TaskQueue) -> None:
    from jobradar.config import ScoringConfig

    seed_job(repo, "strong")
    seed_job(repo, "weak")
    alerts = Alerts()
    weak = GOOD.model_copy(
        update={"skills_coverage": 0.0, "role_alignment": 0.0, "overall_fit": 0.25}
    )
    answers = {"strong": GOOD, "weak": weak}

    class ByJob:
        name = "llm"

        async def match(self, job: JobView, profile: dict[str, Any]) -> MatchAnswers:
            return answers["strong" if job.posting.role == "Strong" else "weak"]

    repo.save_extraction("strong", posting(role="Strong").model_dump_json(), "Acme", "Strong",
                         None, "rules-v1", JobStatus.EXTRACTED)  # fmt: skip
    m = Matcher(repo, queue, [ByJob()], PROFILE, "v1", MatchingConfig(), FILTERS,
                ScoringConfig(alert_above=75), alerts, clock=CLOCK)  # fmt: skip
    for job_id in ("strong", "weak"):
        queue.enqueue(MATCH, job_id)
    await run(queue, m)
    assert alerts.queued == ["strong"]
    weak_job = repo.get_job("weak")
    assert weak_job is not None and weak_job.status == JobStatus.HIDDEN
    assert weak_job.score_reason == "weak match (30)"
    strong_job = repo.get_job("strong")
    assert strong_job is not None and strong_job.scam_risk == "low"

    # Startup catch-up re-decides but never alerts: old jobs can't flood your inbox.
    alerts.queued.clear()
    repo.set_match_score("strong", 0, "x")  # force a change
    assert m.catch_up()[0] == 1
    assert alerts.queued == []


async def test_rematching_never_alerts(repo: Repo, queue: TaskQueue) -> None:
    from jobradar.config import ScoringConfig

    seed_job(repo)
    alerts = Alerts()
    first = Matcher(repo, queue, [FakeProvider("llm", GOOD)], PROFILE, "v1", MatchingConfig(),
                    FILTERS, ScoringConfig(alert_above=75), alerts, clock=CLOCK)  # fmt: skip
    queue.enqueue(MATCH, "job1")
    await run(queue, first)
    assert alerts.queued == ["job1"]

    # The profile (or prompt) changed: the job is matched again, but you've seen it already.
    second = Matcher(repo, queue, [FakeProvider("llm", GOOD)], PROFILE, "v2", MatchingConfig(),
                     FILTERS, ScoringConfig(alert_above=75), alerts, clock=CLOCK)  # fmt: skip
    assert second.catch_up() == (0, 1)
    await run(queue, second)
    assert alerts.queued == ["job1"]


@pytest.mark.parametrize(
    ("days_later", "deadline", "alerted"),
    [
        (0, None, True),
        (1, "2026-10-20", True),
        (3, None, False),  # first seen 3 days ago: you've had it in your Inbox
        (0, "2026-10-07", False),  # the deadline passed before the job was matched
    ],
)
async def test_only_fresh_open_jobs_alert(
    repo: Repo, queue: TaskQueue, days_later: int, deadline: str | None, alerted: bool
) -> None:
    from jobradar.config import ScoringConfig

    seed_job(repo, deadline=deadline)  # created at the fixture clock's 8 Oct 09:00
    later = FakeClock()
    later.advance(days_later * 86400)
    alerts = Alerts()
    m = Matcher(repo, queue, [FakeProvider("llm", GOOD)], PROFILE, "v1", MatchingConfig(),
                FILTERS, ScoringConfig(alert_above=75), alerts, clock=later)  # fmt: skip
    queue.enqueue(MATCH, "job1")
    await run(queue, m)
    assert (alerts.queued == ["job1"]) == alerted
    job = repo.get_job("job1")
    assert job is not None and job.status == JobStatus.NEW  # stays in the Inbox either way


async def test_ads_that_slipped_through_extraction_are_hidden(repo: Repo, queue: TaskQueue) -> None:
    seed_job(repo, role=None, confidence=0.05, summary="A free webinar, not a job.")
    alerts = Alerts()
    m = Matcher(repo, queue, [FakeProvider("llm", GOOD)], PROFILE, "v1", MatchingConfig(),
                FILTERS, alerts=alerts, clock=CLOCK)  # fmt: skip
    queue.enqueue(MATCH, "job1")
    await run(queue, m)
    job = repo.get_job("job1")
    assert job is not None and job.status == JobStatus.HIDDEN
    assert job.score_reason == "not a job posting (ad or event)"
    assert alerts.queued == []


async def test_your_status_is_never_overridden(repo: Repo, queue: TaskQueue) -> None:
    seed_job(repo)
    repo.set_user_status("job1", JobStatus.NEW)  # you un-hid it in Notion
    weak = GOOD.model_copy(
        update={"skills_coverage": 0.0, "role_alignment": 0.0, "overall_fit": 0.25}
    )
    alerts = Alerts()
    m = Matcher(repo, queue, [FakeProvider("llm", weak)], PROFILE, "v1", MatchingConfig(),
                FILTERS, alerts=alerts, clock=CLOCK)  # fmt: skip
    queue.enqueue(MATCH, "job1")
    await run(queue, m)
    job = repo.get_job("job1")
    assert job is not None and job.status == JobStatus.NEW  # weak, but you chose to keep it
    assert job.score == 30 and alerts.queued == []
