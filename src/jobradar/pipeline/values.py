"""Turn short label values ("0-3 Years", "8th - 10th October 2026") into JobPosting
fields. Shared by the free extraction layers (structured data and post labels).

Every parser returns None / [] when unsure: an empty field can still be filled by a later
layer, a wrong one cannot.
"""

from __future__ import annotations

import re
from datetime import date

_MONTH_NAMES = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_DAY = r"(\d{1,2})(?:st|nd|rd|th)?"
_DAY_MONTH = re.compile(rf"\b{_DAY}[\s\-]+{_MON},?(?:[\s\-]+(\d{{4}}))?\b", re.IGNORECASE)
_MONTH_DAY = re.compile(rf"\b{_MON}\s+{_DAY},?(?:\s+(\d{{4}}))?\b", re.IGNORECASE)
_NUMERIC = re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})\b")  # Indian order: day first
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})(?!\d)")  # also "2026-10-30T23:59"
_YEAR = re.compile(r"\b(20\d\d)\b")


def _date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_deadline(value: str, default_year: int | None = None) -> date | None:
    """The latest date written in `value`; a date without a year takes the year written
    elsewhere in the value, else `default_year`."""
    years = [int(y) for y in _YEAR.findall(value)]
    fallback: int | None = max(years) if years else default_year
    found: list[date | None] = []
    for m in _ISO.finditer(value):
        found.append(_date(int(m[1]), int(m[2]), int(m[3])))
    for m in _NUMERIC.finditer(value):
        found.append(_date(int(m[3]), int(m[2]), int(m[1])))
    for day, mon, year in _DAY_MONTH.findall(value):
        if year or fallback:
            month = _MONTH_NAMES.index(mon.lower()[:3]) + 1
            found.append(_date(int(year) if year else fallback or 0, month, int(day)))
    for mon, day, year in _MONTH_DAY.findall(value):
        if year or fallback:
            month = _MONTH_NAMES.index(mon.lower()[:3]) + 1
            found.append(_date(int(year) if year else fallback or 0, month, int(day)))
    dates = [d for d in found if d is not None]
    return max(dates) if dates else None


_RANGE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:-|\u2013|\u2014|to)\s*(\d+(?:\.\d+)?)\s*\+?\s*(years?|yrs?|months?)",
    re.I,
)
_SINGLE = re.compile(r"(\d+(?:\.\d+)?)\s*\+?\s*(years?|yrs?|months?)", re.IGNORECASE)
_FRESHER = re.compile(
    r"\b(freshers?|entry[\s-]level|no experience|students?|graduates?|trainees?)\b", re.I
)


def parse_experience(value: str) -> tuple[float | None, float | None]:
    """(min, max) years. "0-3 Years" -> (0, 3); "2+ years" -> (2, None); "Freshers" -> (0, None)."""

    def years(number: str, unit: str) -> float:
        n = float(number)
        return round(n / 12, 2) if unit.lower().startswith("month") else n

    if m := _RANGE.search(value):
        lo, hi = years(m[1], m[3]), years(m[2], m[3])
        if lo <= hi <= 50:
            return lo, hi
    if m := _SINGLE.search(value):
        n = years(m[1], m[2])
        if n <= 50:
            return n, None
    if _FRESHER.search(value):
        return 0.0, None
    return None, None


def parse_batch_years(value: str) -> list[int]:
    """Graduation years: "2023/2024/2025" and "2023 - 2025" both give [2023, 2024, 2025]."""
    years: list[int] = []
    for m in re.finditer(r"\b(20\d\d)\s*(?:-|\u2013|\u2014|to)\s*(20\d\d)\b", value):
        lo, hi = int(m[1]), int(m[2])
        if 0 < hi - lo <= 6:
            years.extend(range(lo, hi + 1))
    for m in re.finditer(
        r"\b(20(\d\d))\s*(?:-|\u2013|\u2014|to)\s*(\d\d)\b(?!\d)", value
    ):  # "2024-26"
        lo, hi = int(m[1]), 2000 + int(m[3])
        if 0 < hi - lo <= 6:
            years.extend(range(lo, hi + 1))
    years.extend(int(y) for y in _YEAR.findall(value))
    return sorted(set(years))


# (canonical name, pattern). Short names are matched case-sensitively so that the words
# "be", "me" and "a" in a sentence don't count as degrees.
_DEGREES: list[tuple[str, re.Pattern[str]]] = [
    (name, re.compile(pattern, flags))
    for name, pattern, flags in [
        ("B.Tech", r"\bB\.?\s?Tech\b", re.I),
        ("M.Tech", r"\bM\.?\s?Tech\b", re.I),
        ("BE", r"\bB\.?E\b\.?", 0),
        ("ME", r"\bM\.?E\b\.?", 0),
        ("MCA", r"\bMCA\b", re.I),
        ("BCA", r"\bBCA\b", re.I),
        ("B.Sc", r"\bB\.?\s?Sc\b", re.I),
        ("M.Sc", r"\bM\.?\s?Sc\b", re.I),
        ("BBA", r"\bBBA\b", re.I),
        ("MBA", r"\bMBA\b", re.I),
        ("B.Com", r"\bB\.?\s?Com\b", re.I),
        ("M.Com", r"\bM\.?\s?Com\b", re.I),
        ("BA", r"\bB\.?A\b\.?", 0),
        ("MA", r"\bM\.?A\b\.?", 0),
        ("PGDM", r"\bPGDM\b", re.I),
        ("B.Pharm", r"\bB\.?\s?Pharm(?:a|acy)?\b", re.I),
        ("D.Pharm", r"\bD\.?\s?Pharm(?:a|acy)?\b", re.I),
        ("M.Pharm", r"\bM\.?\s?Pharm(?:a|acy)?\b", re.I),
        ("Diploma", r"\bDiploma\b", re.I),
        ("PhD", r"\bPh\.?\s?D\b", re.I),
        ("Any Degree", r"\bany\s+(?:degree|graduate|graduation|stream)\b", re.I),
    ]
]


def parse_degrees(value: str) -> list[str]:
    return [name for name, pattern in _DEGREES if pattern.search(value)]


def parse_employment(value: str) -> str | None:
    text = value.lower()
    if re.search(r"\bintern(ship)?s?\b", text):
        return "internship"
    if re.search(r"\b(contract(ual)?|freelance|temporary)\b", text):
        return "contract"
    if re.search(r"\b(full[\s-]?time|permanent)\b", text):
        return "full_time"
    return None


def parse_work_mode(value: str) -> str | None:
    text = value.lower()
    if "hybrid" in text:
        return "hybrid"
    if re.search(r"\b(remote|wfh|work from home)\b", text):
        return "remote"
    if re.search(r"\b(on[\s-]?site|in[\s-]office|wfo|walk[\s-]?in)\b", text):
        return "onsite"
    return None


_NOT_PLACES = {
    "more", "others", "etc", "multiple", "various", "multiple locations", "any", "hybrid",
    "onsite", "on-site", "wfo", "not disclosed",
}  # fmt: skip
_PLACEHOLDER_ROLES = re.compile(
    r"^(multiple|various|many|several|different)\s+(roles?|positions?|openings?|posts?|profiles?)$",
    re.I,
)


def parse_locations(value: str) -> list[str]:
    """Split "Bangalore (Hybrid), Pune / Remote" into places; the brackets are dropped
    (read them with parse_work_mode)."""
    text = re.sub(r"\([^)]*\)", " ", value)
    places: list[str] = []
    for part in re.split(r",|/|\||;|&|\band\b|\s[\u2013\u2014-]\s", text):
        place = " ".join(part.split()).strip(" .-*")
        known = place.lower() in _NOT_PLACES or place.lower() in (p.lower() for p in places)
        if place and len(place) <= 60 and not known:
            places.append(place)
    return places[:10]


def clean_company(value: str) -> str | None:
    """'HARMAN (a Samsung company)' -> 'HARMAN'."""
    text = " ".join(re.sub(r"\([^)]*\)", " ", value).split()).strip(" .-*|")
    return text if 1 < len(text) <= 80 else None


def clean_role(value: str) -> str | None:
    """Drops a trailing id or batch: 'Software Intern | R-56145-2026' -> 'Software Intern'."""
    text = re.sub(r"\s+\|[^|]*\d.*$", "", value)
    text = " ".join(text.split()).strip(" .-*|")
    if _PLACEHOLDER_ROLES.match(text):
        return None  # "Multiple Roles": leave it to a later layer
    return text if 1 < len(text) <= 120 else None
