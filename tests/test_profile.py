import pytest

from jobradar.config import ConfigError, FiltersConfig, MatchingConfig
from jobradar.profile import compact_profile, load_profile, profile_version, skill_names

PROFILE = {
    "basics": {"name": "Asha", "email": "asha@example.com", "phone": "+91-90000-00000"},
    "summary": "About me",
    "education": [{"degree": "B.Tech", "branch": "Electrical Engineering",
                   "years": "Nov 2022 - Jun 2026", "cgpa": 7.5}],
    "skills": {"languages": ["Python", "SQL"], "backend": ["FastAPI", "Python"]},
    "experience": [{"id": "x", "title": "Intern", "company": "Acme", "dates": "2025",
                    "bullets": [{"id": "b1", "text": "Built an API", "tags": ["python"]}]}],
    "projects": [{"id": "p", "name": "JobRadar", "tags": ["llm"],
                  "bullets": [{"id": "p1", "text": "Agent"}]}],
    "preferences": {"target_roles": ["Backend Engineer"], "work_modes": ["remote"]},
    "answers": {"expected_ctc": "6 LPA"},
}  # fmt: skip


def compact(**filters: object) -> dict[str, object]:
    return compact_profile(
        PROFILE, FiltersConfig.model_validate(filters), MatchingConfig(branch_strict=True)
    )


def test_compact_profile_keeps_contact_details_out() -> None:
    view = compact(batch_years=[2025, 2026])
    text = str(view)
    for private in ("Asha", "asha@example.com", "90000", "About me", "6 LPA"):
        assert private not in text
    assert view["education"] == [
        {"degree": "B.Tech", "branch": "Electrical Engineering", "graduation_year": 2026,
         "cgpa": 7.5}
    ]  # fmt: skip
    assert view["skills"] == ["Python", "SQL", "FastAPI"]
    assert view["preferences"]["locations"] == ["anywhere in India", "remote"]
    assert view["rules"] == {
        "batch_years": [2025, 2026], "max_experience_years": None, "branch_strict": True,
    }  # fmt: skip


def test_version_follows_what_matching_sees() -> None:
    assert profile_version(compact()) == profile_version(compact())
    assert profile_version(compact()) != profile_version(compact(locations=["Pune"]))


def test_skill_names_flat_or_grouped() -> None:
    assert skill_names({"skills": ["Go", "Go", "Rust"]}) == ["Go", "Rust"]
    assert skill_names({}) == []


def test_load_profile_errors(tmp_path: object) -> None:
    from pathlib import Path

    folder = Path(str(tmp_path))
    with pytest.raises(ConfigError, match="not found"):
        load_profile(folder / "missing.yaml")
    (folder / "bad.yaml").write_text("- a list\n")
    with pytest.raises(ConfigError, match="must be a mapping"):
        load_profile(folder / "bad.yaml")
