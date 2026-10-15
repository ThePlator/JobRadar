import json
from pathlib import Path
from typing import Any

import pytest

from jobradar.config import AppConfig, Secrets, Settings
from jobradar.db.models import JobStatus
from jobradar.db.repo import Repo
from jobradar.pipeline.task_types import BUILD_RESUME, NOTION_UPSERT
from jobradar.profile import resume_problems
from jobradar.queue.tasks import PermanentError, TaskQueue
from jobradar.resume import build as build_mod
from jobradar.resume.compile import engine
from jobradar.resume.render import (
    bold,
    content_from_profile,
    display_url,
    href,
    latex_escape,
    render,
)
from jobradar.resume.tailor import TailoredResume, candidate_facts
from jobradar.resume.validate import ground, new_claims, trim_one, vocabulary
from jobradar.storage.local import resume_path
from tests.conftest import FakeClock

PROFILE: dict[str, Any] = {
    "basics": {
        "name": "Asha Rao", "headline": "Software Engineer — Backend",
        "email": "asha@example.com", "phone": "+91-90000 00000", "location": "Pune, India",
        "links": {"github": "https://github.com/asha", "linkedin": "https://linkedin.com/in/asha"},
    },
    "summary": "Backend engineer who ships Python services. B.Tech, 2026.",
    "education": [{"degree": "B.Tech", "branch": "Electrical Engineering", "school": "PCE",
                   "location": "Bihar, India", "years": "Nov 2022 - Jun 2026", "cgpa": 7.54}],
    "skills": {
        "languages": ["Python", "SQL", "JavaScript (ES6+)"],
        "backend": ["Node.js", "Express", "REST API design", "authentication & authorization"],
        "infrastructure": ["Docker & Docker Compose", "Git/GitHub", "RabbitMQ"],
    },
    "experience": [{
        "id": "acme", "title": "Backend Intern", "company": "Acme", "location": "Remote",
        "url": "https://acme.example", "dates": "Feb 2026 - May 2026",
        "bullets": [
            {"id": "a1", "text": "Built a **REST API** in Python serving 3 clients."},
            {"id": "a2", "text": "Cut query time by **25%** with indexing."},
            {"id": "a3", "text": "Set up RabbitMQ queues for async jobs."},
        ],
    }],
    "projects": [{
        "id": "radar", "name": "Radar — Job Agent", "url": "https://github.com/asha/radar",
        "dates": "Oct 2026 - Present", "stack": "Python, SQLite", "context": "Open source (MIT)",
        "bullets": [
            {"id": "r1", "text": "Built a **durable task queue** with retries."},
            {"id": "r2", "text": "Shipped with **360+ automated tests**."},
            {"id": "r3", "text": "Reads 19 job boards."},
        ],
    }],
}  # fmt: skip


# ---- LaTeX --------------------------------------------------------------------------------


def test_latex_escape_every_special_character() -> None:
    raw = "\\ & % $ # _ { } ~ ^ | < > — – “q”"
    assert latex_escape(raw) == (
        r"\textbackslash{} \& \% \$ \# \_ \{ \} \textasciitilde{} \textasciicircum{} "
        r"\textbar{} \textless{} \textgreater{} --- -- ``q''"
    )


def test_bold_runs() -> None:
    assert bold("cut by **25%** in **R&D**") == r"cut by \textbf{25\%} in \textbf{R\&D}"
    assert bold("odd ** marker") == "odd  marker"  # unbalanced: no bold at all


def test_links() -> None:
    assert href("https://github.com/asha/") == r"\href{https://github.com/asha/}{github.com/asha}"
    assert (
        href("https://x.example/a%20b#c")
        == r"\href{https://x.example/a\%20b\#c}{x.example/a\%20b\#c}"
    )
    assert href("javascript:alert(1)") == "alert(1)"  # not a link
    assert href("https://x.example/}\\input{x}") == r"x.example/\}\textbackslash{}input\{x\}"
    assert display_url("https://www.theplator.tech/") == "theplator.tech"


def test_plain_render_keeps_the_layout() -> None:
    tex = render(content_from_profile(PROFILE))
    assert tex.count(r"\resumeSubheading") == 4  # the macro + experience, project, education
    assert r"\textbf{{\Huge Asha Rao}}" in tex
    assert r"{\small Software Engineer --- Backend}" in tex
    assert (
        r"\href{mailto:asha@example.com}{asha@example.com} $|$ +91-90000 00000 $|$ Pune, India\\"
        in tex
    )
    assert r"\item \textbf{Infrastructure:} Docker \& Docker Compose, Git/GitHub, RabbitMQ" in tex
    assert "{Backend Intern --- Acme}{Feb 2026 -- May 2026}" in tex
    assert r"{Remote --- \href{https://acme.example}{acme.example}}{}" in tex
    assert r"\item Cut query time by \textbf{25\%} with indexing." in tex
    assert (
        r"{Radar --- Job Agent $|$ \normalfont \small \href{https://github.com/asha/radar}{github.com/asha/radar}}{Oct 2026 -- Present}"
        in tex
    )
    assert "{B.Tech --- Electrical Engineering (CGPA: 7.54/10)}{Bihar, India}" in tex
    assert "\\BLOCK" not in tex and "\\VAR" not in tex


def test_profile_values_cannot_inject_latex() -> None:
    evil = json.loads(json.dumps(PROFILE))
    evil["basics"]["name"] = r"\input{/etc/passwd}"
    evil["experience"][0]["bullets"][0]["text"] = r"**\write18{rm -rf}** done"
    tex = render(content_from_profile(evil))
    assert r"\input{/etc" not in tex and r"\write18{" not in tex
    assert r"\textbackslash{}input\{/etc/passwd\}" in tex


def test_profile_problems() -> None:
    assert resume_problems(PROFILE) == []
    broken = json.loads(json.dumps(PROFILE))
    broken["projects"][0]["bullets"][0]["id"] = "a1"
    del broken["experience"][0]["bullets"][1]["id"]
    assert resume_problems(broken) == [
        "experience #1 bullet #2 needs an id",
        "id 'a1' is used twice",
    ]


def test_writer_model_never_sees_contact_details() -> None:
    facts = json.dumps(candidate_facts(PROFILE))
    for private in ("Asha", "asha@example.com", "90000", "github.com/asha", "linkedin"):
        assert private not in facts
    assert '"a1"' in facts and "durable task queue" in facts


# ---- grounding ----------------------------------------------------------------------------


def tailored(**kw: Any) -> TailoredResume:
    data: dict[str, Any] = {
        "summary": "Backend engineer who ships **Python** services.",
        "skills": {"languages": ["SQL", "Python", "JavaScript (ES6+)"]},
        "experience": [{"id": "acme", "bullets": [
            {"ref_id": "a2", "text": "Reduced query time by **25%** using indexing."},
            {"ref_id": "a1", "text": "Built a **REST API** in Python for 3 clients."},
        ]}],
        "projects": [{"id": "radar", "bullets": [{"ref_id": "r1", "text": "Built a **durable task queue** with retries."},
                                                 {"ref_id": "r2", "text": "Shipped with 360+ tests."}]}],
    }  # fmt: skip
    data.update(kw)
    return TailoredResume.model_validate(data)


def grounded(t: TailoredResume, banned: list[str] | None = None) -> Any:
    return ground(content_from_profile(PROFILE), t, PROFILE, banned)


def test_clean_tailoring_is_kept() -> None:
    g = grounded(tailored())
    assert g.violations == []
    acme = g.content.experience[0]
    assert acme.bullet_ids == ["a2", "a1"]
    assert acme.bullets[0] == "Reduced query time by **25%** using indexing."
    assert g.content.skills[0] == ("Languages", ["SQL", "Python", "JavaScript (ES6+)"])
    assert g.content.skills[1][1][:3] == ["Node.js", "Express", "REST API design"]  # untouched


@pytest.mark.parametrize(
    ("ref", "text", "why"),
    [
        ("a2", "Cut query time by **40%** with indexing.", "number not in the original: 40%"),
        ("a2", "Cut query time by 25 % with indexing.", "number not in the original: 25"),
        ("a1", "Built a REST API in Python serving 30 clients.", "number not in the original: 30"),
        ("a2", "Doubled query speed (25%) with indexing.", "size word not in the original: doubled"),
        ("a2", "Cut query time 10x by 25% with indexing.", "size word not in the original: 10x"),
        ("a1", "Built a REST API in Python and Go serving 3 clients.", "technology not in the original: go"),
        ("a3", "Set up **Kafka** and RabbitMQ queues for async jobs.", "technology not in the original: kafka"),
        ("a3", "Set up RabbitMQ queues on AWS Lambda for async jobs.", "technology not in the original: aws, lambda"),
        ("a1", "Built a REST API in Python with Docker serving 3 clients.", "technology not in the original: docker"),
        ("a1", "World-class REST API in Python serving 3 clients.", "banned phrase: world-class"),
    ],
)  # fmt: skip
def test_invented_facts_fall_back_to_the_original(ref: str, text: str, why: str) -> None:
    t = tailored(experience=[{"id": "acme", "bullets": [{"ref_id": ref, "text": text}]}])
    g = grounded(t, banned=["world-class"])
    assert any(why in v for v in g.violations), g.violations
    original = {"a1": PROFILE["experience"][0]["bullets"][0]["text"],
                "a2": PROFILE["experience"][0]["bullets"][1]["text"],
                "a3": PROFILE["experience"][0]["bullets"][2]["text"]}[ref]  # fmt: skip
    assert g.content.experience[0].bullets[0] == original


def test_unknown_ids_are_dropped_and_entries_topped_up() -> None:
    t = tailored(
        experience=[{"id": "acme", "bullets": [{"ref_id": "r1", "text": "Built a durable task queue."}]}],
        projects=[{"id": "ghost", "bullets": [{"ref_id": "g1", "text": "Built Facebook."}]}],
    )  # fmt: skip
    g = grounded(t)
    assert "acme: unknown bullet 'r1' dropped" in g.violations
    assert "projects: unknown project 'ghost' dropped" in g.violations
    assert g.content.experience[0].bullet_ids == ["a1", "a2"]  # two of its own
    assert [p.id for p in g.content.projects] == ["radar"]


def test_skills_only_from_that_group() -> None:
    g = grounded(tailored(skills={"languages": ["Python", "Go", "Docker & Docker Compose"]}))
    assert "skills: 'Go' isn't in your Languages skills; left out" in g.violations
    assert (
        "skills: 'Docker & Docker Compose' isn't in your Languages skills; left out" in g.violations
    )
    assert g.content.skills[0][1] == ["Python", "SQL", "JavaScript (ES6+)"]  # topped up to 3


def test_summary_is_grounded_in_the_whole_profile() -> None:
    ok = grounded(tailored(summary="Backend engineer: REST APIs, RabbitMQ, 360+ tests."))
    assert ok.violations == []
    bad = grounded(tailored(summary="Backend engineer with 5 years of Kubernetes."))
    assert bad.violations and bad.content.summary == PROFILE["summary"]


def test_bold_and_markup_do_not_hide_numbers() -> None:
    assert new_claims("by **4**0%", "by 25%", vocabulary(PROFILE), []) == [
        "number not in the original: 40%"
    ]


def test_trim_drops_the_lowest_ranked_bullet_of_the_longest_entry() -> None:
    c = content_from_profile(PROFILE)
    assert trim_one(c) == "r3"  # a tie: the entry further down goes first
    assert trim_one(c) == "a3"
    assert trim_one(c) is None  # two each is the floor
    c = content_from_profile(PROFILE)
    assert trim_one(c, primary={"radar"}) == "a3"  # others give up bullets first
    assert trim_one(c, primary={"radar"}) is None  # a primary project keeps 3


def test_file_names() -> None:
    from datetime import date

    path = resume_path(
        Path("out"), "Stripe, Inc.", "Software Engineer (Intern)", date(2026, 10, 14), 2
    )
    assert path == Path("out/StripeInc_SoftwareEngineerIntern_20261014_v2.pdf")


# ---- building -----------------------------------------------------------------------------


def write_pdf(path: Path, pages: int) -> None:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        writer.write(f)


def settings(tmp_path: Path, **resume: Any) -> Settings:
    profile = tmp_path / "profile.yaml"
    import yaml

    profile.write_text(yaml.safe_dump(PROFILE, allow_unicode=True))
    return Settings(
        config=AppConfig.model_validate({
            "sources": {"telegram": {"enabled": False}},
            "matching": {"profile_path": str(profile)},
            "storage": {"local_dir": str(tmp_path / "out")},
            "resume": resume,
        }),
        secrets=Secrets.model_validate({}),
    )  # fmt: skip


class FakeLLM:
    def __init__(self, result: TailoredResume) -> None:
        self.result = result
        self.prompts: list[str] = []

    async def structured(self, model: str, prompt: Any, schema: Any, **values: str) -> Any:
        self.prompts.append(prompt.render(**values))
        return self.result


def seed(repo: Repo) -> None:
    repo.get_or_create_job("job1", "https://acme.example/jobs/1")
    repo.save_extraction("job1", json.dumps({"role": "Backend Intern", "skills_required": ["Python"]}),
                         "Stripe", "Backend Intern", None, "rules-v1", JobStatus.NEW)  # fmt: skip


@pytest.fixture
def fake_compile(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Compiles to 2 pages while the resume has more than 5 bullets, else 1."""
    texs: list[str] = []

    async def compile_pdf(tex: str, pdf: Path) -> Path:
        texs.append(tex)
        write_pdf(pdf, 2 if tex.count(r"\item ") - 3 > 5 else 1)  # minus the 3 skill lines
        return pdf

    monkeypatch.setattr(build_mod, "compile_pdf", compile_pdf)
    return texs


async def test_build_tailors_trims_and_links(
    repo: Repo, queue: TaskQueue, clock: FakeClock, tmp_path: Path, fake_compile: list[str]
) -> None:
    seed(repo)
    full = tailored(experience=[{"id": "acme", "bullets": [
        {"ref_id": "a1", "text": "Built a **REST API** in Python serving 3 clients."},
        {"ref_id": "a2", "text": "Cut query time by **25%** with indexing."},
        {"ref_id": "a3", "text": "Set up RabbitMQ queues for async jobs."}]}],
        projects=[{"id": "radar", "bullets": [{"ref_id": "r1", "text": "Built a durable task queue with retries."},
                                              {"ref_id": "r2", "text": "Shipped with **360+ automated tests**."},
                                              {"ref_id": "r3", "text": "Reads 19 job boards."}]}])  # fmt: skip
    llm = FakeLLM(full)
    builder = build_mod.ResumeBuilder(settings(tmp_path), repo, queue, llm, clock=clock)  # type: ignore[arg-type]
    artifact = await builder.build_job("job1")

    assert artifact.version == 1
    pdf = Path(artifact.local_path or "")
    assert pdf.name == "Stripe_BackendIntern_20261008_v1.pdf" and pdf.is_file()  # noqa: ASYNC240 (test files only)
    assert pdf.with_suffix(".tex").is_file()
    record = json.loads(artifact.content_json or "{}")
    assert record["tailored"] and record["pages"] == 1 and record["fixed"] == []
    assert record["bullets"] == {"acme": ["a1", "a2", "a3"], "radar": ["r1", "r2"]}  # trimmed once
    assert record["dropped_to_fit"] == ["r3"]
    assert len(fake_compile) == 2
    assert "asha@example.com" not in llm.prompts[0] and "Asha" not in llm.prompts[0]
    task = queue.claim([NOTION_UPSERT])
    assert task is not None and task.key == "job1"

    again = await builder.build_job("job1", tailored=False)
    assert again.version == 2 and Path(again.local_path or "").name.endswith("_v2.pdf")


async def test_build_task_rules(
    repo: Repo, queue: TaskQueue, clock: FakeClock, tmp_path: Path, fake_compile: list[str]
) -> None:
    seed(repo)
    builder = build_mod.ResumeBuilder(
        settings(tmp_path, max_per_day=1), repo, queue, None, clock=clock
    )
    queue.enqueue(BUILD_RESUME, "job1", {"why": "auto"})
    task = queue.claim([BUILD_RESUME])
    assert task is not None
    await builder.build(task)
    await builder.build(task)  # built already: not again
    assert repo.next_version("job1") == 2
    assert not builder.room_today()
    record = json.loads(repo.latest_artifact("job1").content_json or "{}")  # type: ignore[union-attr]
    assert record["tailored"] is False  # no LLM: the profile as written


async def test_unusable_profile_fails_clearly(
    repo: Repo, queue: TaskQueue, clock: FakeClock, tmp_path: Path
) -> None:
    seed(repo)
    s = settings(tmp_path)
    s.config.matching.profile_path.write_text("basics: {}\n")
    builder = build_mod.ResumeBuilder(s, repo, queue, None, clock=clock)
    with pytest.raises(PermanentError, match=r"basics\.name is missing"):
        await builder.build_job("job1")


@pytest.mark.skipif(engine() is None, reason="no LaTeX engine installed")
async def test_example_profile_compiles_to_one_page(tmp_path: Path) -> None:
    from jobradar.profile import load_profile
    from jobradar.resume.compile import compile_pdf
    from jobradar.resume.validate import page_count

    for profile in (PROFILE, load_profile(Path("profile.example.yaml"))):
        pdf = await compile_pdf(render(content_from_profile(profile)), tmp_path / "r.pdf")
        assert page_count(pdf) == 1


# ---- triggers -----------------------------------------------------------------------------


async def test_strong_first_match_queues_a_resume(repo: Repo, queue: TaskQueue) -> None:
    from jobradar.config import MatchingConfig, ScoringConfig
    from jobradar.pipeline.match import Matcher
    from jobradar.pipeline.task_types import MATCH
    from tests.test_match import (
        FILTERS,
        GOOD,
        FakeProvider,
        run,
        seed_job,
    )
    from tests.test_match import (
        PROFILE as MATCH_PROFILE,
    )

    seed_job(repo)
    m = Matcher(repo, queue, [FakeProvider("llm", GOOD)], MATCH_PROFILE, "v1", MatchingConfig(),
                FILTERS, ScoringConfig(auto_resume_above=85), build_resumes=True)  # fmt: skip
    queue.enqueue(MATCH, "job1")
    assert BUILD_RESUME in await run(queue, m)


async def test_shortlisting_queues_a_resume(repo: Repo, queue: TaskQueue) -> None:
    from jobradar.sinks.notion_sync import StatusSync
    from tests.test_notion_sync import add, page

    add(repo, "job1")
    sync = StatusSync(repo, queue, None, "ds", build_resumes=True)  # type: ignore[arg-type]
    assert sync._apply(page("job1", "Shortlisted"))
    task = queue.claim([BUILD_RESUME])
    assert task is not None and task.payload == {"why": "shortlisted"}


def test_notion_links_the_newest_resume(repo: Repo, tmp_path: Path) -> None:
    from jobradar.db.models import Job
    from jobradar.sinks.notion import build_properties, resume_link

    seed(repo)
    assert resume_link(repo, "job1") is None
    repo.save_artifact("job1", "resume", 1, str(tmp_path / "a_v1.pdf"), "{}")
    repo.save_artifact("job1", "resume", 2, str(tmp_path / "a_v2.pdf"), "{}")
    link = resume_link(repo, "job1")
    assert link == (tmp_path / "a_v2.pdf").as_uri()
    job = Job(id="j", created_at="", updated_at="", status=JobStatus.NEW)
    assert build_properties(job, [], new_page=True, resume=link)["Resume"] == {"url": link}


def test_primary_project_always_shows_first_with_three_bullets() -> None:
    profile = json.loads(json.dumps(PROFILE))
    profile["projects"][0]["primary"] = True
    profile["projects"].append({"id": "side", "name": "Side", "bullets": [
        {"id": "s1", "text": "A side project."}, {"id": "s2", "text": "Another bit."}]})  # fmt: skip
    t = tailored(
        projects=[{"id": "side", "bullets": [{"ref_id": "s1", "text": "A side project."}]}]
    )
    g = ground(content_from_profile(profile), t, profile)
    assert [p.id for p in g.content.projects] == ["radar", "side"]
    assert g.content.projects[0].bullet_ids == ["r1", "r2", "r3"]


async def test_busy_writer_model_falls_back_to_the_extraction_model(
    repo: Repo, queue: TaskQueue, clock: FakeClock, tmp_path: Path, fake_compile: list[str]
) -> None:
    from jobradar.queue.tasks import RetryableError

    class Busy(FakeLLM):
        def __init__(self, result: TailoredResume) -> None:
            super().__init__(result)
            self.models: list[str] = []

        async def structured(self, model: str, prompt: Any, schema: Any, **values: str) -> Any:
            self.models.append(model)
            if model == "gemini/gemini-flash-latest":
                raise RetryableError("503 overloaded")
            return self.result

    seed(repo)
    llm = Busy(tailored())
    builder = build_mod.ResumeBuilder(settings(tmp_path), repo, queue, llm, clock=clock)  # type: ignore[arg-type]
    artifact = await builder.build_job("job1")
    assert llm.models == ["gemini/gemini-flash-latest", "gemini/gemini-flash-lite-latest"]
    assert json.loads(artifact.content_json or "{}")["tailored"] is True


async def test_long_profiles_are_cut_to_one_page(tmp_path: Path, fake_compile: list[str]) -> None:
    profile = json.loads(json.dumps(PROFILE))
    profile["experience"][0]["bullets"] += [
        {"id": f"x{n}", "text": f"Extra {n}."} for n in range(4)
    ]
    pages, content, dropped = await build_mod.fit_to_pages(
        content_from_profile(profile), "classic", tmp_path / "p.pdf", 1, {"radar"}
    )
    assert pages == 1 and dropped == ["x3", "x2", "x1", "x0", "a3"]
    assert content.projects[0].bullet_ids == ["r1", "r2", "r3"]  # primary untouched
