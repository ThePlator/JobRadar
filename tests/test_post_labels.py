from datetime import date

from jobradar.pipeline.post_labels import from_post, labels

BOLD_POST = """🚨 𝗛𝗔𝗥𝗠𝗔𝗡 — 𝗦𝘁𝘂𝗱𝗲𝗻𝘁𝘀 𝗜𝗻𝘁𝗲𝗿𝗻𝘀𝗵𝗶𝗽! 🎯

🏢 𝗖𝗼𝗺𝗽𝗮𝗻𝘆: HARMAN (a Samsung company)
💼 Role: Students / Trainees – Software Intern | R-56145-2026
📍 Location: Bangalore (Hybrid)
💰 Salary: Not disclosed
🎓 Eligibility: BE/B.Tech CS or Electronics
💼 Experience: Students / Recent Graduates

🔗 https://example.com/harman-internship-2026/
"""

WALK_IN_POST = """Company Name:  Wipro
Post Name:  US HR Operations – Associate
Salary:  ₹3.5-4.5 LPA*
Experience:  0-3 Years
Job Location:  Navi Mumbai
Walk-in date:  12th October 2026

✅Apply Link: https://example.com/wipro-walk-in/
"""


def test_reads_decorated_labels() -> None:
    fields = from_post(BOLD_POST, default_year=2026)
    assert fields["company"] == "HARMAN"
    assert fields["role"] == "Students / Trainees – Software Intern"
    assert fields["locations"] == ["Bangalore"]
    assert fields["work_mode"] == "hybrid"
    assert fields["degrees"] == ["B.Tech", "BE"]
    assert fields["experience_min"] == 0.0
    assert fields["employment_type"] == "internship"  # from the role


def test_walk_in_post() -> None:
    fields = from_post(WALK_IN_POST, default_year=2026)
    assert (fields["company"], fields["role"]) == ("Wipro", "US HR Operations – Associate")
    assert fields["salary_text"] == "₹3.5-4.5 LPA*"
    assert (fields["experience_min"], fields["experience_max"]) == (0.0, 3.0)
    assert fields["deadline"] == date(2026, 10, 12)
    assert fields["work_mode"] == "onsite"  # walk-in interviews happen in person


def test_dash_labels_and_batches() -> None:
    post = (
        "Company - Commvault\nRole - Software Engineer (AI)\nBatch - 2023/2024/2025\nCTC - 25 LPA"
    )
    fields = from_post(post)
    assert (fields["company"], fields["role"]) == ("Commvault", "Software Engineer (AI)")
    assert fields["batch_years"] == [2023, 2024, 2025]
    assert fields["salary_text"] == "25 LPA"


def test_hiring_line() -> None:
    fields = from_post("📌Epicor is hiring for Product Developer\nBatch: 2021, 2022")
    assert (fields["company"], fields["role"]) == ("Epicor", "Product Developer")
    assert fields["batch_years"] == [2021, 2022]


def test_labels_win_over_the_hiring_line() -> None:
    fields = from_post("Acme is hiring interns\nRole: SDE Intern")
    assert (fields["company"], fields["role"]) == ("Acme", "SDE Intern")


def test_ignores_lookalikes() -> None:
    # A hyphenated word is not a label, a link is not a value, and only the first line counts.
    assert labels("Post-graduates may apply\nCompany: https://x.co\nRole: A\nRole: B") == {
        "role": "A"
    }


def test_internship_duration_is_not_experience() -> None:
    fields = from_post("Company: Valeo\nRole: Intern\nExperience: Internship (6–9 months)")
    assert fields["experience_min"] == 0.0
    assert "experience_max" not in fields
    assert fields["employment_type"] == "internship"


def test_nothing_to_read() -> None:
    assert from_post("Big news! Join our WhatsApp channel https://example.com") == {}


def test_hiring_line_without_a_role() -> None:
    assert from_post("Opkey is hiring in Hyderabad\n2023 - 2025 Graduates") == {}
    assert "role" not in from_post("Acme is hiring freshers")
