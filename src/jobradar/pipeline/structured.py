"""Layer 1 of extraction: the schema.org JobPosting that many job pages embed for search
engines (`<script type="application/ld+json">`). Free, exact, and written by the employer
or job board itself, so it wins over every other layer."""

from __future__ import annotations

import html as html_lib
import json
import re
from typing import Any

from jobradar.pipeline.values import (
    clean_company,
    clean_role,
    experience_in_text,
    parse_deadline,
    parse_degrees,
    parse_employment,
    parse_experience,
)

_LD_JSON = re.compile(
    r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL
)
MAX_DESCRIPTION = 20_000

_EMPLOYMENT = {
    "FULL_TIME": "full_time", "INTERN": "internship", "INTERNSHIP": "internship",
    "CONTRACTOR": "contract", "CONTRACT": "contract", "TEMPORARY": "contract",
}  # fmt: skip


def _items(node: Any) -> list[Any]:
    """Every object in a JSON-LD document, including those inside lists and @graph."""
    if isinstance(node, list):
        return [item for child in node for item in _items(child)]
    if isinstance(node, dict):
        return [node, *_items(node.get("@graph", []))]
    return []


def _is_job_posting(item: dict[str, Any]) -> bool:
    kind = item.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return "JobPosting" in (str(k) for k in kinds)


def _load(block: str) -> Any:
    try:
        return json.loads(block.strip(), strict=False)  # raw newlines inside strings happen
    except ValueError:
        return None


def find_job_posting(html: str) -> dict[str, Any] | None:
    """The first JobPosting object in the page, or None. Long descriptions are trimmed."""
    for block in _LD_JSON.findall(html):
        data = _load(block) or _load(html_lib.unescape(block))  # some sites escape the block
        for item in _items(data):
            if _is_job_posting(item):
                if isinstance(item.get("description"), str):
                    item["description"] = item["description"][:MAX_DESCRIPTION]
                return dict(item)
    return None


def _text(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("name")
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, str | int | float):
        text = " ".join(html_lib.unescape(str(value)).split())
        return text or None
    return None


_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"</?(?:p|br|li|ul|ol|div|h[1-6]|tr)\b[^>]*>", re.IGNORECASE)


def description_text(posting: dict[str, Any]) -> str | None:
    """The JobPosting description as plain text (HTML removed), or None."""
    raw = posting.get("description")
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = _TAG.sub(" ", _BLOCK.sub("\n", html_lib.unescape(raw)))
    lines = (" ".join(line.split()) for line in html_lib.unescape(text).splitlines())
    return "\n".join(line for line in lines if line) or None


def _locations(posting: dict[str, Any]) -> list[str]:
    raw = posting.get("jobLocation") or []
    places: list[str] = []
    for loc in raw if isinstance(raw, list) else [raw]:
        address = loc.get("address", {}) if isinstance(loc, dict) else {}
        place: str | None
        if isinstance(address, str):
            place = address
        elif isinstance(address, dict):
            place = _text(address.get("addressLocality")) or _text(address.get("addressRegion"))
        else:
            place = None
        if place and place not in places:
            places.append(place)
    return places[:10]


def _salary(value: Any) -> str | None:
    if not isinstance(value, dict):
        return _text(value)
    currency = _text(value.get("currency")) or ""
    amount = value.get("value")
    unit = ""
    if isinstance(amount, dict):
        unit = (_text(amount.get("unitText")) or "").lower()
        lo, hi, one = amount.get("minValue"), amount.get("maxValue"), amount.get("value")
        if lo is not None and hi is not None and lo != hi:
            amount = (
                f"{lo:,}\u2013{hi:,}"
                if isinstance(lo, int) and isinstance(hi, int)
                else f"{lo}\u2013{hi}"
            )
        else:
            amount = one if one is not None else lo if lo is not None else hi
    if amount in (None, "", 0, "0"):
        return None
    text = " ".join(str(p) for p in (currency, amount) if p)
    return f"{text} per {unit}" if unit else text


def _experience(value: Any) -> tuple[float | None, float | None]:
    if isinstance(value, dict):
        months = value.get("monthsOfExperience")
        try:
            return (round(float(months) / 12, 2), None) if months is not None else (None, None)
        except (TypeError, ValueError):
            return None, None
    text = _text(value)
    return parse_experience(text) if text else (None, None)


def _skills(value: Any) -> list[str]:
    if isinstance(value, list):
        names = [_text(v) for v in value]
    elif isinstance(value, str):
        names = [part.strip() for part in re.split(r",|;|\n", value)]
    else:
        names = []
    return [n for n in names if n and len(n) <= 60][:30]


def from_job_posting(posting: dict[str, Any]) -> dict[str, Any]:
    """JobPosting fields (by JobPosting attribute name) that this object states."""
    fields: dict[str, Any] = {}
    if (title := _text(posting.get("title"))) and (role := clean_role(title)):
        fields["role"] = role
    if (org := _text(posting.get("hiringOrganization"))) and (company := clean_company(org)):
        fields["company"] = company
    if locations := _locations(posting):
        fields["locations"] = locations
    if str(posting.get("jobLocationType", "")).upper() == "TELECOMMUTE":
        fields["work_mode"] = "remote"
    kinds = posting.get("employmentType")
    for kind in kinds if isinstance(kinds, list) else [kinds]:
        if mapped := _EMPLOYMENT.get(str(kind).upper().replace("-", "_")):
            fields["employment_type"] = mapped
            break
    else:
        if (title := fields.get("role")) and (mapped := parse_employment(title)):
            fields["employment_type"] = mapped
    if salary := _salary(posting.get("baseSalary")):
        fields["salary_text"] = salary
    if (valid := _text(posting.get("validThrough"))) and (deadline := parse_deadline(valid)):
        fields["deadline"] = deadline
    lo, hi = _experience(posting.get("experienceRequirements"))
    if lo is None and hi is None and (about := description_text(posting)):
        # Many sites (Workday among them) state it only in the description:
        # "Minimum 5 year(s) of experience is required".
        lo, hi = experience_in_text(about)
    if lo is not None:
        fields["experience_min"] = lo
    if hi is not None:
        fields["experience_max"] = hi
    education = posting.get("educationRequirements")
    if isinstance(education, dict):
        education = education.get("credentialCategory")
    if (text := _text(education)) and (degrees := parse_degrees(text)):
        fields["degrees"] = degrees
    if skills := _skills(posting.get("skills")):
        fields["skills_required"] = skills
    return fields
