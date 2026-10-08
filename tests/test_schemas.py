from datetime import date

from jobradar.pipeline.schemas import JobPosting


def test_lenient_cleanup() -> None:
    p = JobPosting.model_validate(
        {
            "company": "  Acme  ",
            "role": "null",
            "employment_type": "Full Time",
            "work_mode": "WFH",
            "locations": "Bengaluru",
            "batch_years": ["2025", 2026, "2026 batch", "soon"],
            "skills_required": ["Python", "python", " SQL ", ""],
            "deadline": "2026-10-30T23:59:00",
            "apply_url": "apply on our site",
            "summary": "x" * 900,
            "confidence": "1.7",
            "form_fields": [{"label": "Resume", "kind": "upload"}],
        }
    )
    assert p.company == "Acme" and p.role is None
    assert p.employment_type == "full_time" and p.work_mode == "remote"
    assert p.locations == ["Bengaluru"]
    assert p.batch_years == [2025, 2026]
    assert p.skills_required == ["Python", "SQL"]
    assert p.deadline == date(2026, 10, 30)
    assert p.apply_url is None
    assert len(p.summary) == 600 and p.confidence == 1.0
    assert p.form_fields[0].kind == "unknown"


def test_scalars_where_lists_belong() -> None:
    p = JobPosting.model_validate({"batch_years": 2025, "degrees": "B.Tech", "skills_required": 7})
    assert p.batch_years == [2025] and p.degrees == ["B.Tech"] and p.skills_required == ["7"]


def test_is_job() -> None:
    assert JobPosting(company="Acme", confidence=0.1).is_job
    assert not JobPosting(confidence=0.2).is_job
    assert JobPosting(confidence=0.9).is_job
