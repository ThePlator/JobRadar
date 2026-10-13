"""Which board postings are worth a look, decided before any Gemini call.

Big boards list hundreds of jobs worldwide. Only postings in India (or remote and open to
India), with a tech title, and not clearly senior go on to matching. Matching still decides
fit; these rules only skip what can't fit, cheaply.
"""

from __future__ import annotations

import re

_INDIA = re.compile(
    r"\b(india|bengaluru|bangalore|hyderabad|pune|mumbai|delhi|ncr|gurgaon|gurugram|noida|"
    r"chennai|kolkata|ahmedabad|jaipur|kochi|trivandrum|thiruvananthapuram|coimbatore|"
    r"chandigarh|indore|mysore|mysuru|apac|asia)\b",
    re.IGNORECASE,
)
_REMOTE = re.compile(r"\b(remote|anywhere|worldwide|work from home)\b", re.IGNORECASE)
# Words a remote location may carry without naming a place.
_FILLER = {
    "remote", "anywhere", "worldwide", "global", "globally", "work", "from", "home",
    "first", "friendly", "fully", "only", "position", "role", "the", "in", "or", "and",
}  # fmt: skip

_TECH = re.compile(
    r"software|engineer|developer|\bsde\b|\bswe\b|backend|back-end|frontend|front-end|"
    r"full.?stack|machine learning|\bml\b|\bai\b|data scien|data engineer|platform|devops|"
    r"\bsre\b|cloud|mobile|android|ios|security engineer|qa|test",
    re.IGNORECASE,
)
_NOT_TECH = re.compile(
    r"sales|account (executive|manager)|recruit|talent|marketing|finance|legal|counsel|"
    r"business develop|customer success|\bhr\b|people partner|payroll|procurement|designer",
    re.IGNORECASE,
)
_SENIOR = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|manager|director|head|architect|vp|chief|"
    r"distinguished|fellow|ii|iii|iv|sde ?-?[23]|engineer [23]|l[5-9])\b",
    re.IGNORECASE,
)
# SmartRecruiters' experience levels that can't be a fresher job.
_SENIOR_LEVELS = {"mid_senior_level", "director", "executive"}


def where_ok(location: str) -> bool:
    if _INDIA.search(location):
        return True
    if _REMOTE.search(location):
        # "Remote" alone is fine; "Remote - US" or "Remote · New York, NY" names somewhere else.
        words = re.findall(r"[^\W\d_]+", location.casefold())
        return all(w in _FILLER for w in words)
    return False


def title_ok(title: str) -> bool:
    return bool(_TECH.search(title)) and not _NOT_TECH.search(title) and not _SENIOR.search(title)


def level_ok(level: str | None) -> bool:
    return (level or "").casefold() not in _SENIOR_LEVELS


def worth_a_look(title: str, location: str, level: str | None = None) -> bool:
    return where_ok(location) and title_ok(title) and level_ok(level)
