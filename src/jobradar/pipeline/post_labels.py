"""Layer 2 of extraction: the "Company: … / Role: … / Batch: …" lines most job channels put
in their posts. Decorative Unicode (mathematical bold, full-width letters) is folded to plain text
first."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from jobradar.pipeline.values import (
    clean_company,
    clean_role,
    parse_batch_years,
    parse_deadline,
    parse_degrees,
    parse_employment,
    parse_experience,
    parse_locations,
    parse_work_mode,
)

# Label words -> what the value is. Matched case-insensitively at the start of a line.
_LABELS: dict[str, tuple[str, ...]] = {
    "company": (
        "company", "company name", "organization", "organisation", "organization name",
        "organisation name", "hiring company", "employer",
    ),
    "role": (
        "role", "job role", "post", "post name", "position", "position name", "job title",
        "designation", "profile", "job profile",
    ),
    "location": ("location", "locations", "job location", "work location", "place of posting"),
    "salary": ("salary", "expected salary", "ctc", "stipend", "package", "compensation"),
    "batch": (
        "batch", "batches", "eligible batch", "eligible batches", "passing year",
        "year of passing", "graduation year", "passout", "pass out", "passout year",
    ),
    "eligibility": (
        "eligibility", "eligibility criteria", "qualification", "qualifications", "education",
        "degree", "eligible degree", "eligible degrees",
    ),
    "experience": ("experience", "exp", "experience required", "work experience"),
    "deadline": (
        "last date", "last date to apply", "apply by", "apply before", "deadline",
        "closing date", "application deadline", "registration deadline",
    ),
    "walk_in": ("walk-in date", "walkin date", "walk in date", "walk-in", "drive date"),
    "job_type": ("job type", "jobtype", "employment type", "type of job"),
    "work_mode": ("work mode", "mode", "work type", "workplace"),
}  # fmt: skip
_KIND = {label: kind for kind, labels in _LABELS.items() for label in labels}
_LINE = re.compile(
    r"^(?P<label>"
    + "|".join(re.escape(label) for label in sorted(_KIND, key=len, reverse=True))
    + r")(?:\s*:|\s+[-\u2013\u2014])\s*(?P<value>.+)$",  # "Role: X" or "Role - X", not "Post-grad"
    re.IGNORECASE,
)
# "Opkey is hiring in Hyderabad" names a place, "Acme is hiring freshers" an audience: no role.
_NOT_A_ROLE = re.compile(
    r"^(in|at|for|from|across|with|on|now|immediately|freshers|graduates|candidates|"
    r"students|multiple|various)\b",
    re.IGNORECASE,
)
_HIRING = re.compile(r"^(?P<company>.+?)\s+is\s+hiring\s+(?:for\s+)?(?:an?\s+)?(?P<role>.+)$", re.I)


def _is_emoji(ch: str) -> bool:
    category = unicodedata.category(ch)
    return category in ("So", "Cf") or ch == "\ufe0f" or (category == "Sk" and ord(ch) > 127)


def _plain(line: str) -> str:
    """NFKC, then drop emoji, bullets and other symbols before the first letter or digit."""
    text = unicodedata.normalize("NFKC", line)
    text = "".join(ch for ch in text if not _is_emoji(ch))
    text = re.sub(r"^[\W_]+", "", text)
    return " ".join(text.split())


def labels(text: str) -> dict[str, str]:
    """{kind: value} for the first line of each kind, e.g. {"company": "Wipro"}."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        plain = _plain(line)
        if m := _LINE.match(plain):
            kind = _KIND[m["label"].lower()]
            value = m["value"].strip()
            if value and not value.lower().startswith("http"):
                found.setdefault(kind, value)
    return found


def _hiring_line(text: str) -> dict[str, str]:
    """'Epicor is hiring for Product Developer' in the first lines of the post."""
    lines = [plain for line in text.splitlines() if (plain := _plain(line))]
    for line in lines[:3]:
        if (m := _HIRING.match(line)) and "http" not in line.lower():
            company, role = clean_company(m["company"]), clean_role(m["role"].rstrip("!. "))
            if company and role and len(company) <= 50 and not _NOT_A_ROLE.match(role):
                return {"company": company, "role": role}
    return {}


def from_post(text: str, default_year: int | None = None) -> dict[str, Any]:
    """JobPosting fields (by attribute name) that the post's labels state."""
    found = labels(text)
    fields: dict[str, Any] = {}
    if (value := found.get("company")) and (company := clean_company(value)):
        fields["company"] = company
    if (value := found.get("role")) and (role := clean_role(value)):
        fields["role"] = role
    for key, value in _hiring_line(text).items():
        fields.setdefault(key, value)

    if value := found.get("location"):
        if places := parse_locations(value):
            fields["locations"] = places
        if mode := parse_work_mode(value):
            fields["work_mode"] = mode
    if (value := found.get("work_mode")) and (mode := parse_work_mode(value)):
        fields["work_mode"] = mode
    if value := found.get("salary"):
        fields["salary_text"] = value[:200]
    if (value := found.get("batch")) and (years := parse_batch_years(value)):
        fields["batch_years"] = years
    if value := found.get("eligibility"):
        if degrees := parse_degrees(value):
            fields["degrees"] = degrees
        if "batch_years" not in fields and (years := parse_batch_years(value)):
            fields["batch_years"] = years

    experience = [found[k] for k in ("experience", "job_type") if k in found]
    for value in experience:  # some channels write "JobType: 1-3 Years"
        if parse_employment(value) == "internship":  # "Internship (6 months)" is a duration
            fields["experience_min"] = 0.0
            break
        lo, hi = parse_experience(value)
        if lo is not None:
            fields["experience_min"] = lo
            if hi is not None:
                fields["experience_max"] = hi
            break

    deadline_text = found.get("deadline") or found.get("walk_in")
    if deadline_text and (deadline := parse_deadline(deadline_text, default_year)):
        fields["deadline"] = deadline
    if "walk_in" in found:
        fields.setdefault("work_mode", "onsite")

    for value in (found.get("job_type"), found.get("experience"), fields.get("role")):
        if value and (kind := parse_employment(value)):
            fields["employment_type"] = kind
            break
    return fields
