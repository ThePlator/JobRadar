"""Scam signals in a job's post and page. Points add up: 3+ is high risk (never emailed),
2 is medium (labelled in Notion). Rules are deliberately few and specific: a deposit or
training fee alone is high; an application fee, which real exams charge too, is medium
unless another warning sign joins it."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Risk = Literal["low", "medium", "high"]

# Fees only fake offers ask for: count anywhere (post or page).
_SCAM_FEE = re.compile(
    r"\b(security|(?<!non-)(?<!non )refundable|training|joining|kit|laptop|interview)\s+"
    r"(fee|fees|deposit|charges?)\b"
    r"|\b(pay|deposit|transfer)\b.{0,30}\b(to|for)\s+(confirm|secure)\s+(your\s+)?"
    r"(offer|joining|job|selection)\b",
    re.IGNORECASE,
)
# Fees that real exams and government jobs charge too: count only in the post itself, where
# a channel would be telling you to pay. Job blogs mention them on every page.
_POST_FEE = re.compile(
    r"\b(registration|processing|application|exam)\s+(fee|fees|charges?)\b"
    r"|\bpay\b.{0,30}\bto\s+(confirm|book|reserve)\s+(your\s+)?(seat|slot)\b",
    re.IGNORECASE,
)
_NO_FEE = re.compile(r"\b(no|zero|free|without|nil|not|never)\b[^.\n]{0,45}$", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@([\w-]+\.)+[a-z]{2,}", re.IGNORECASE)
_FREE_MAIL = re.compile(r"@(gmail|yahoo|hotmail|outlook|rediffmail|ymail)\.", re.IGNORECASE)
_URGENT = re.compile(
    r"\b(urgent(ly)?\s+(hiring|requirement|joining)|immediate joining guaranteed|"
    r"limited seats|only\s+\d+\s+seats|apply within \d+ hours?|last \d+ seats|"
    r"100% (job|placement) guarantee(d)?|guaranteed (job|placement))\b",
    re.IGNORECASE,
)
_PAY = re.compile(
    r"(₹|rs\.?|inr)\s*([\d,]+)\s*(k\b)?\s*(per|/)\s*(day|week)"
    r"|earn\s+(up\s*to\s+)?(₹|rs\.?|inr)?\s*[\d,]+\s*(k\b)?\s*(daily|per day|weekly|per week)"
    r"|work from home.{0,40}(₹|rs\.?|inr)\s*[\d,]+\s*(k\b)?\s*(daily|per day)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ScamCheck:
    risk: Risk
    signals: list[str]


def _asks(pattern: re.Pattern[str], text: str) -> bool:
    """A fee mention that isn't negated ("no registration fee") or a question."""
    for m in pattern.finditer(text):
        before = text[max(0, m.start() - 50) : m.start()]
        after = text[m.end() : m.end() + 40].split("\n")[0]
        if (
            not _NO_FEE.search(before)
            and "?" not in after
            and not _NO_FEE.match(after.lstrip(" :-"))
        ):
            return True
    return False


def check(post: str, page: str = "") -> ScamCheck:
    text = f"{post}\n\n{page}"
    signals: list[str] = []
    points = 0
    if _asks(_SCAM_FEE, text):
        points += 3
        signals.append("asks for a deposit or training fee")
    elif _asks(_POST_FEE, post):
        points += 2
        signals.append("mentions a registration or application fee")
    emails = [m.group(0) for m in _EMAIL.finditer(text)]
    if emails and all(_FREE_MAIL.search(e) for e in emails):
        points += 1
        signals.append("contact is a personal email only")
    if _PAY.search(text):
        points += 1
        signals.append("pay quoted per day or week")
    if _URGENT.search(text):
        points += 1
        signals.append("pressure to hurry or a guaranteed job")
    risk: Risk = "high" if points >= 3 else "medium" if points == 2 else "low"
    return ScamCheck(risk, signals)
