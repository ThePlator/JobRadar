from pathlib import Path
from typing import Any

import pytest

from jobradar.resume import ats
from jobradar.resume.compile import engine
from jobradar.resume.render import content_from_profile, dates, render
from tests.test_resume import PROFILE

CONTENT = content_from_profile(PROFILE)
EMAIL = PROFILE["basics"]["email"]
PHONE = PROFILE["basics"]["phone"]


def clean_text() -> str:
    """Roughly what a well-built PDF gives back: every printed word, spaced."""
    lines = [CONTENT.name, f"{EMAIL} | {PHONE}", CONTENT.summary]
    for item in CONTENT.experience + CONTENT.projects:
        lines += [f"{item.heading} {item.dates}", *(b.replace("**", "") for b in item.bullets)]
    return "\n".join(lines)


def report(monkeypatch: pytest.MonkeyPatch, text: str, skills: list[str] | None = None) -> Any:
    monkeypatch.setattr(ats, "pdf_text", lambda pdf: text)
    return ats.check(Path("r.pdf"), CONTENT, skills or [], PROFILE)


@pytest.mark.parametrize(
    ("raw", "printed"),
    [
        ("Feb 2026 - May 2026", "Feb 2026 - May 2026"),
        ("Feb 2026 – May 2026", "Feb 2026 - May 2026"),
        ("2022—2026", "2022 - 2026"),
        ("Jun 2025 -- Present", "Jun 2025 - Present"),
        ("Mar-2024", "Mar-2024"),
        (None, ""),
    ],
)
def test_dates_are_printed_with_a_plain_hyphen(raw: str | None, printed: str) -> None:
    assert dates(raw) == printed


def test_a_clean_resume_has_no_problems(monkeypatch: pytest.MonkeyPatch) -> None:
    assert report(monkeypatch, clean_text()).problems == []


def test_long_dash_dates_are_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    text = clean_text() + "\nIntern | Acme Feb 2026 – May 2026\nB.Tech 2022—2026"
    (problem,) = report(monkeypatch, text).problems
    assert "long dash" in problem and "Feb 2026 – May 2026" in problem and "2022—2026" in problem


def test_a_bullet_dash_after_a_date_is_not_a_date(monkeypatch: pytest.MonkeyPatch) -> None:
    text = clean_text() + "\nFeb 2026\n– Decreased latency by caching"
    assert report(monkeypatch, text).problems == []


def test_missing_contact_details(monkeypatch: pytest.MonkeyPatch) -> None:
    text = clean_text().replace(EMAIL, "Envelope").replace(PHONE, "MOBILE-ALT")
    problems = report(monkeypatch, text).problems
    assert any(EMAIL in p for p in problems) and any(PHONE in p for p in problems)
    # Spacing the extractor invents doesn't count as missing.
    spaced = clean_text().replace(EMAIL, " ".join(EMAIL))
    assert report(monkeypatch, spaced).problems == []


def test_words_run_together_are_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    item = CONTENT.experience[0]
    words = item.bullets[0].replace("**", "").split()
    glued = clean_text() + "\n" + "".join(words[:3])
    (problem,) = report(monkeypatch, glued).problems
    assert "run together" in problem and "".join(words[:3]) in problem


def test_split_letters_are_not_run_together(monkeypatch: pytest.MonkeyPatch) -> None:
    # pypdf splits some kerned capitals ("F rontend"); that's not a missing space.
    assert ats.run_together("F rontend T emporal", CONTENT) == []


def test_unreadable_characters(monkeypatch: pytest.MonkeyPatch) -> None:
    (problem,) = report(monkeypatch, clean_text() + "\n (cid:12) �").problems
    assert "unreadable" in problem and "(cid:12)" in problem


def test_heading_after_its_bullets(monkeypatch: pytest.MonkeyPatch) -> None:
    item = CONTENT.experience[0]
    text = clean_text().replace(item.heading, "")
    text = text.replace(item.bullets[0].replace("**", ""), item.bullets[0].replace("**", "") +
                        "\n" + item.heading, 1)  # fmt: skip
    assert any("reads after its own bullets" in p for p in report(monkeypatch, text).problems)


def test_job_skills_you_have_on_the_page(monkeypatch: pytest.MonkeyPatch) -> None:
    text = clean_text().replace("Docker", "containers")
    r = report(monkeypatch, text, ["Python", "Docker", "Kubernetes"])
    assert r.skills_on_resume == ["Python"]
    assert r.skills_not_on_resume == ["Docker"]  # in the profile, not printed
    assert "Kubernetes" not in r.skills_on_resume + r.skills_not_on_resume  # you don't have it


def test_grouped_profile_skills_count_for_matching() -> None:
    from jobradar.providers.skills_rule import compare_skills

    grouped = {**PROFILE, "skills": {"languages": ["Python"], "infrastructure": ["Docker"]}}
    assert compare_skills(["Python", "Docker", "Rust"], grouped) == (["Python", "Docker"], ["Rust"])


@pytest.mark.skipif(engine() is None, reason="no LaTeX engine installed")
async def test_classic_template_reads_cleanly(tmp_path: Path) -> None:
    from jobradar.profile import load_profile
    from jobradar.resume.compile import compile_pdf

    for profile in (PROFILE, load_profile(Path("profile.example.yaml"))):
        content = content_from_profile(profile)
        pdf = await compile_pdf(render(content), tmp_path / "r.pdf")
        assert ats.check(pdf, content, [], profile).problems == []
