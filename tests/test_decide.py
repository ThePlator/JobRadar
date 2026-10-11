from typing import Any

import pytest

from jobradar.config import FiltersConfig, MatchingConfig, ScoringConfig
from jobradar.pipeline.decide import decide
from jobradar.providers.base import MatchAnswers

SCORING = ScoringConfig(hide_below=40, alert_above=75, auto_resume_above=85)


def answers(**kw: Any) -> MatchAnswers:
    return MatchAnswers.model_validate(
        {"provider": "llm", "eligible": 0.95, "confidence": 0.6, **kw}
    )


@pytest.mark.parametrize(
    ("kw", "score", "company", "scam", "rule", "hidden"),
    [
        ({}, 90, "BadCo", "low", "excluded", True),
        ({"eligible": 0.05, "not_eligible_because": "CSE/IT only"}, 90, "Acme", "low",
         "not_eligible", True),
        ({}, 90, "Acme", "high", "scam", False),  # flagged, never emailed
        ({"confidence": 0.3}, 90, "Acme", "low", "unsure", False),
        ({}, 30, "Acme", "low", "weak", True),
        ({}, 80, "Acme", "low", "alert", False),
        ({"eligible": 0.5}, 80, "Acme", "low", "inbox", False),  # eligibility unclear
        ({"provider": "skills_rule", "confidence": 0.9}, 95, "Acme", "low", "inbox", False),
        ({}, 60, "Acme", "low", "inbox", False),
        ({}, None, "Acme", "low", "inbox", False),
    ],
)  # fmt: skip
def test_rules_in_order(
    kw: dict[str, Any], score: int | None, company: str, scam: str, rule: str, hidden: bool
) -> None:
    result = decide(
        answers(**kw), score, company, scam, MatchingConfig(), SCORING,
        FiltersConfig(exclude_companies=["badco"]),
    )  # fmt: skip
    assert (result.rule, result.hide_because is not None) == (rule, hidden)
    assert result.alert == (rule == "alert")


def test_reasons() -> None:
    r = decide(answers(eligible=0.0, not_eligible_because="batch 2027 only"), None, None, "low",
               MatchingConfig(), SCORING, FiltersConfig())  # fmt: skip
    assert r.hide_because == "not eligible: batch 2027 only"
    weak = decide(answers(), 31, None, "low", MatchingConfig(), SCORING, FiltersConfig())
    assert weak.hide_because == "weak match (31)"
