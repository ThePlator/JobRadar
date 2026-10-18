import json
from datetime import date

from jobradar.pipeline.structured import find_job_posting, from_job_posting

POSTING = {
    "@context": "https://schema.org",
    "@type": "JobPosting",
    "title": "Software Engineer I",
    "hiringOrganization": {"@type": "Organization", "name": "Acme Corp"},
    "jobLocation": [
        {"@type": "Place", "address": {"addressLocality": "Bengaluru", "addressCountry": "IN"}},
        {"@type": "Place", "address": {"addressLocality": "Pune"}},
    ],
    "employmentType": ["FULL_TIME"],
    "validThrough": "2026-10-30T23:59",
    "baseSalary": {
        "@type": "MonetaryAmount",
        "currency": "INR",
        "value": {
            "@type": "QuantitativeValue",
            "minValue": 600000,
            "maxValue": 900000,
            "unitText": "YEAR",
        },
    },
    "experienceRequirements": {"monthsOfExperience": 12},
    "educationRequirements": {"credentialCategory": "B.Tech or MCA"},
    "skills": "Python, SQL, Docker",
    "description": "<p>Build things.</p>",
}


def page(*blocks: object, raw: str = "") -> str:
    scripts = "".join(
        f'<script type="application/ld+json">{json.dumps(b)}</script>' for b in blocks
    )
    return f"<html><head>{scripts}{raw}</head><body>Job</body></html>"


def test_finds_the_job_posting_among_other_objects() -> None:
    html = page({"@type": "BreadcrumbList"}, {"@graph": [{"@type": "WebPage"}, POSTING]})
    found = find_job_posting(html)
    assert found is not None and found["title"] == "Software Engineer I"


def test_type_lists_and_bad_blocks() -> None:
    raw = '<script type="application/ld+json">{not json</script>'
    html = page({**POSTING, "@type": ["JobPosting", "Thing"]}, raw=raw)
    assert find_job_posting(html) is not None
    assert find_job_posting(page({"@type": "Article"}, raw=raw)) is None


def test_maps_fields() -> None:
    fields = from_job_posting(POSTING)
    assert fields == {
        "role": "Software Engineer I",
        "company": "Acme Corp",
        "locations": ["Bengaluru", "Pune"],
        "employment_type": "full_time",
        "salary_text": "INR 600,000–900,000 per year",
        "deadline": date(2026, 10, 30),
        "experience_min": 1.0,
        "degrees": ["B.Tech", "MCA"],
        "skills_required": ["Python", "SQL", "Docker"],
    }


def test_remote_and_sparse_postings() -> None:
    fields = from_job_posting(
        {"title": "Data Intern", "hiringOrganization": "Beta", "jobLocationType": "TELECOMMUTE"}
    )
    assert fields == {
        "role": "Data Intern", "company": "Beta", "work_mode": "remote",
        "employment_type": "internship",
    }  # fmt: skip
    assert from_job_posting({"title": "", "baseSalary": {"value": 0}}) == {}


def test_experience_and_text_from_the_description() -> None:
    from jobradar.pipeline.structured import description_text

    # Accenture's Workday JobPosting (2026-10-16): experience only in the description.
    posting = {
        "@type": "JobPosting", "title": "Application Developer",
        "hiringOrganization": {"name": "8113 ASOL- Bangalore 7 SEZ Company"},
        "description": "<p>Project Role : Application Developer</p><p>Minimum 5 year(s) of "
        "experience is required</p><ul><li>Java &amp; Spring</li></ul>",
    }  # fmt: skip
    assert from_job_posting(posting)["experience_min"] == 5
    assert description_text(posting) == (
        "Project Role : Application Developer\nMinimum 5 year(s) of experience is required\n"
        "Java & Spring"
    )
    stated = {**posting, "experienceRequirements": {"monthsOfExperience": 12}}
    assert from_job_posting(stated)["experience_min"] == 1  # the stated field wins


def test_date_posted_becomes_published_on() -> None:
    base = {"@type": "JobPosting", "title": "Developer"}
    assert from_job_posting({**base, "datePosted": "2026-10-16"})["published_on"] == date(
        2026, 10, 16
    )
    assert from_job_posting({**base, "datePosted": "2026-09-02T08:00:00Z"})["published_on"] == date(
        2026, 9, 2
    )
    assert "published_on" not in from_job_posting({**base, "datePosted": "last week"})
