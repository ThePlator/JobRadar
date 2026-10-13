"""match -> decide: what happens to a matched job. Rules run in order; the first that applies
wins. Thresholds come from config (`matching`, `scoring`, `filters`).

| # | When | Then |
|---|------|------|
| 1 | not eligible, or an excluded company | hidden |
| 2 | scam risk high | stays in the Inbox flagged, never emailed |
| 3 | not confident, or the answers contradict each other | stays in the Inbox, never emailed |
| 4 | score below `scoring.hide_below` | hidden ("weak match") |
| 5 | strong score, clearly eligible, a real provider | email alert |
| 6 | otherwise | stays in the Inbox |
"""

from __future__ import annotations

from dataclasses import dataclass

from jobradar.config import FiltersConfig, MatchingConfig, ScoringConfig
from jobradar.providers.base import MatchAnswers

CONTRADICTION = 0.5  # score and overall verdict this far apart (on 0-1): don't trust either


def contradicts(answers: MatchAnswers, score: int | None) -> bool:
    """The weighted answers and the provider's own overall verdict disagree badly, e.g. a
    99 score with "Not a fit". Happens on things that aren't really jobs."""
    if score is None or answers.overall_fit is None:
        return False
    return abs(score / 100 - answers.overall_fit) > CONTRADICTION


def hidden_kind(reason: str | None) -> str:
    """Why a hidden job is hidden, in a few words, from its stored reason."""
    why = (reason or "").casefold()
    for prefix, kind in (
        ("not eligible", "not eligible"),
        ("weak match", "weak match"),
        ("not a job", "not a job"),
        ("hidden by you", "hidden by you"),
        ("company excluded", "excluded company"),
        ("closed on the company board", "closed on its board"),
    ):
        if why.startswith(prefix):
            return kind
    return "ad or promo link"


@dataclass(frozen=True)
class Decision:
    rule: str  # for logs and tests
    hide_because: str | None = None
    alert: bool = False


def decide(
    answers: MatchAnswers,
    score: int | None,
    company: str | None,
    scam_risk: str,
    matching: MatchingConfig,
    scoring: ScoringConfig,
    filters: FiltersConfig,
) -> Decision:
    excluded = {c.casefold() for c in filters.exclude_companies}
    if company and company.casefold() in excluded:
        return Decision("excluded", hide_because="company excluded in filters")
    if answers.eligible is not None and answers.eligible < matching.hide_if_eligible_below:
        why = answers.not_eligible_because
        return Decision(
            "not_eligible", hide_because=f"not eligible: {why}" if why else "not eligible"
        )
    if scam_risk == "high":
        return Decision("scam")
    if answers.provider != "rules" and answers.confidence < matching.min_confidence:
        return Decision("unsure")
    if contradicts(answers, score):
        return Decision("unsure")
    if score is not None and score < scoring.hide_below:
        return Decision("weak", hide_because=f"weak match ({score})")
    if (
        score is not None
        and score >= scoring.alert_above
        and (answers.eligible or 0) >= matching.alert_if_eligible_above
        and answers.provider not in ("skills_rule", "rules")
    ):
        return Decision("alert", alert=True)
    return Decision("inbox")
