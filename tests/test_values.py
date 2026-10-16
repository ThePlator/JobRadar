from datetime import date

import pytest

from jobradar.pipeline.values import (
    clean_company,
    clean_role,
    parse_batch_years,
    parse_deadline,
    parse_degrees,
    parse_employment,
    parse_experience,
    parse_locations,
    parse_work_mode,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("5 October 2026", date(2026, 10, 5)),
        ("12th October 2026", date(2026, 10, 12)),
        ("8th October – 10th October 2026", date(2026, 10, 10)),  # a range: the last day
        ("9th- 10th October 2026", date(2026, 10, 10)),
        ("Oct 30, 2026", date(2026, 10, 30)),
        ("30/10/2026", date(2026, 10, 30)),  # day first
        ("2026-10-30T23:59:00Z", date(2026, 10, 30)),
        ("7 Oct", date(2026, 10, 7)),  # year from the post date
        ("ASAP", None),
        ("31 February 2026", None),
    ],
)
def test_deadline(text: str, expected: date | None) -> None:
    assert parse_deadline(text, default_year=2026) == expected


def test_deadline_without_any_year_is_unknown() -> None:
    assert parse_deadline("7 Oct") is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0-3 Years", (0.0, 3.0)),
        ("0–2 Years", (0.0, 2.0)),
        ("1 to 3 yrs", (1.0, 3.0)),
        ("2+ years", (2.0, None)),
        ("6 months", (0.5, None)),
        ("Freshers", (0.0, None)),
        ("Freshers (0-3 Years)", (0.0, 3.0)),
        ("Students / Recent Graduates", (0.0, None)),
        ("Not mentioned", (None, None)),
    ],
)
def test_experience(text: str, expected: tuple[float | None, float | None]) -> None:
    assert parse_experience(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2023/2024/2025/2026", [2023, 2024, 2025, 2026]),
        ("2025 and 2026", [2025, 2026]),
        ("2023 - 2025", [2023, 2024, 2025]),
        ("2024-26", [2024, 2025, 2026]),
        ("2027 Batch", [2027]),
        ("Any", []),
    ],
)
def test_batch_years(text: str, expected: list[int]) -> None:
    assert parse_batch_years(text) == expected


def test_degrees() -> None:
    assert parse_degrees("BE/B.Tech CS or Electronics") == ["B.Tech", "BE"]
    assert parse_degrees("B.Sc, BCA, MCA") == ["MCA", "BCA", "B.Sc"]
    assert parse_degrees("Any Graduate") == ["Any Degree"]
    # Lowercase "be" / "me" / "a" in a sentence are words, not degrees.
    assert parse_degrees("Candidates must be able to join me as a team") == []


def test_employment_and_work_mode() -> None:
    assert parse_employment("Software Intern") == "internship"
    assert parse_employment("Internship (6 months)") == "internship"
    assert parse_employment("Full Time") == "full_time"
    assert parse_employment("Contractual") == "contract"
    assert parse_employment("Engineer") is None
    assert parse_work_mode("Bangalore (Hybrid)") == "hybrid"
    assert parse_work_mode("Work from home") == "remote"
    assert parse_work_mode("Walk-in") == "onsite"
    assert parse_work_mode("Pune") is None


def test_locations() -> None:
    assert parse_locations("Bangalore (Hybrid)") == ["Bangalore"]
    assert parse_locations("Hyderabad, Bengaluru / Pune & Chennai") == [
        "Hyderabad", "Bengaluru", "Pune", "Chennai",
    ]  # fmt: skip
    assert parse_locations("Pan India – Bengaluru, Pune") == ["Pan India", "Bengaluru", "Pune"]
    assert parse_locations("Chennai, Hybrid") == ["Chennai"]
    assert parse_locations("Multiple") == []


def test_company_and_role_cleanup() -> None:
    assert clean_company("HARMAN (a Samsung company)") == "HARMAN"
    assert clean_role("Software Intern | R-56145-2026") == "Software Intern"
    assert clean_role("Systems Engineer (SE) | 2027 Batch") == "Systems Engineer (SE)"
    assert clean_role("SDE | Backend") == "SDE | Backend"  # no id: kept
    assert clean_role("Multiple Roles") is None


@pytest.mark.parametrize(
    ("text", "years"),
    [
        ("Must have skills : Java Minimum 7.5 year(s) of experience is required Educational "
         "Qualification : 15 years full time education", (7.5, None)),
        ("We need 3+ years of relevant industry experience in Java", (3, None)),
        ("2-4 yrs exp in Python", (2, 4)),
        ("Experience: 0-2 Years", (0, 2)),
        ("Experience required - 1 year", (1, None)),
        ("Educational Qualification : 15 years full time education", (None, None)),
        ("A company with 25 years of history, great experience", (None, None)),
        ("Freshers welcome", (None, None)),
    ],
)  # fmt: skip
def test_experience_in_description(text: str, years: tuple[float | None, float | None]) -> None:
    from jobradar.pipeline.values import experience_in_text

    assert experience_in_text(text) == years
