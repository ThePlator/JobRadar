import json

from jobradar.db.models import JobStatus, Platform
from jobradar.db.repo import Repo
from jobradar.extract_check import agrees, render, run_check


def test_agreement_is_loose_on_wording_strict_on_values() -> None:
    assert agrees("company", "Wipro", "Wipro Ltd.")
    assert agrees("role", "SDE Intern", "sde intern")
    assert not agrees("company", "Acme", None)
    assert agrees("locations", ["Pune"], ["Pune", "Mumbai"])
    assert not agrees("batch_years", [2025], [2025, 2026])
    assert agrees("experience_min", 0, 0.0)
    assert agrees("deadline", "2026-10-30", "2026-10-30")


def test_compares_free_layers_with_earlier_llm_answers(repo: Repo) -> None:
    src = repo.upsert_source(Platform.TELEGRAM, "-1001", "Jobs")
    assert src.id is not None
    posts = {
        "j1": ("Company: Acme\nRole: SDE Intern\nBatch: 2026", JobStatus.EXTRACTED,
               {"company": "Acme", "role": "Software Intern", "batch_years": [2026]}),
        "j2": ("Company: Promo\nRole: Course", JobStatus.DISCARDED, {"company": None}),
        "j3": ("Company: New\nRole: X", JobStatus.EXTRACTED, {"_sources": {}}),  # already new
    }  # fmt: skip
    for n, (job_id, (post, status, data)) in enumerate(posts.items()):
        raw_id, _ = repo.save_raw_message(src.id, str(n), "2026-10-07T08:00:00.000000Z", post)
        repo.get_or_create_job(job_id, f"https://acme.com/{job_id}")
        repo.add_job_source(job_id, raw_id)
        repo.save_extraction(job_id, json.dumps(data), None, None, None, "extract-v1", status)

    check = run_check(repo)
    assert (check.jobs, check.skip_llm) == (2, 2)
    assert check.skip_llm_but_discarded == ["https://acme.com/j2"]
    assert check.filled["company"] == 1 and check.agreed["company"] == 1
    assert check.agreed["role"] == 0 and check.agreed["batch_years"] == 1
    report = render(check, examples=3)
    assert "Role and company found without the LLM: 2 (100%)" in report
    assert "SDE Intern" in report


def test_city_spellings_agree() -> None:
    assert agrees("locations", ["Bengaluru"], ["Bangalore"])
    assert agrees("locations", ["India-Remote"], ["Remote"])
    assert not agrees("locations", ["Nagpur"], ["Bengaluru"])
