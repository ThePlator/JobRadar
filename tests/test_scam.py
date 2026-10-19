import pytest

from jobradar.pipeline.scam import check


@pytest.mark.parametrize(
    ("post", "page", "risk"),
    [
        ("Pay ₹2,000 refundable security deposit to confirm your joining", "", "high"),
        ("Training fee ₹4,999 (adjusted in first salary)", "", "high"),
        ("Registration fee: ₹499 only! Limited seats", "", "high"),  # fee + hurry
        ("Registration fee ₹499. HR: hiring.desk@gmail.com", "", "high"),  # fee + personal mail
        ("Application fee ₹100 for SC/ST", "", "medium"),  # real exams charge this too
        ("Earn ₹5,000 per day from home. Urgent hiring!", "", "medium"),
        ("Software Engineer @ Acme. Apply: https://acme.com/jobs/1", "", "low"),
        ("No registration fee. Apply now", "", "low"),
        ("Role: SDE", "Infosys never charges a registration, test or interview fee.", "low"),
        ("Role: PA", "The non-refundable fee is ₹1,500 for General candidates.", "low"),
        ("Role: SDE", "Is there any training fee? No.", "low"),
        # A warning against fees, with the fee listed after a long subject (EdgeVerve, 19 Oct).
        (
            "Role: SE",
            "Neither Infosys nor EdgeVerve asks candidates for registration fees, "
            "training charges, document-verification payments or deposits.",
            "low",
        ),
        ("Role: SE", "We don't charge any security deposit at any stage.", "low"),
        # A negation in an earlier sentence doesn't excuse a real demand.
        ("No experience needed. Pay the training fee of ₹3,000 to join.", "", "high"),
        # Job blogs list "application fee" on every page: only the post itself counts.
        ("Role: SDE", "salary details, application fee, selection process", "low"),
        ("Role: Analyst", "Analyst roles typically pay ₹14K–19K a month.", "low"),
    ],
)
def test_scam_risk(post: str, page: str, risk: str) -> None:
    assert check(post, page).risk == risk


def test_signals_explain_the_risk() -> None:
    result = check("Pay ₹500 security deposit. Contact jobs.hr@yahoo.com")
    assert result.signals == [
        "asks for a deposit or training fee",
        "contact is a personal email only",
    ]
