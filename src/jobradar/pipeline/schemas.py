"""What the extractor LLM must return. Validators are lenient: they repair small slips
(over-long summary, unknown enum value, bad URL) instead of failing the whole extraction."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

EmploymentType = Literal["full_time", "internship", "contract", "unknown"]
WorkMode = Literal["onsite", "hybrid", "remote", "unknown"]
FieldKind = Literal["text", "textarea", "choice", "file", "date", "number", "unknown"]


def _one_of(value: Any, allowed: tuple[str, ...]) -> str:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return text if text in allowed else "unknown"


def _as_list(value: Any) -> list[Any]:
    """A single value where a list belongs becomes a one-item list; None becomes []."""
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple, set)) else [value]


def _str_list(value: Any) -> list[str]:
    value = _as_list(value)
    seen: dict[str, None] = {}
    for item in value:
        text = str(item).strip()
        if text and text.lower() not in (s.lower() for s in seen):
            seen[text] = None
    return list(seen)


class FormField(BaseModel):
    label: str
    kind: FieldKind = "unknown"
    required: bool = False
    options: list[str] = Field(default_factory=list)

    @field_validator("kind", mode="before")
    @classmethod
    def _kind(cls, v: Any) -> str:
        return _one_of(v, ("text", "textarea", "choice", "file", "date", "number"))


class JobPosting(BaseModel):
    company: str | None = None
    role: str | None = None
    employment_type: EmploymentType = "unknown"
    locations: list[str] = Field(default_factory=list)
    work_mode: WorkMode = "unknown"
    experience_min: float | None = Field(default=None, ge=0, le=50)
    experience_max: float | None = Field(default=None, ge=0, le=50)
    batch_years: list[int] = Field(default_factory=list)
    degrees: list[str] = Field(default_factory=list)
    skills_required: list[str] = Field(default_factory=list)
    skills_preferred: list[str] = Field(default_factory=list)
    salary_text: str | None = None
    deadline: date | None = None
    apply_url: str | None = None
    form_fields: list[FormField] = Field(default_factory=list)
    summary: str = ""
    confidence: float = 0.0

    @field_validator("company", "role", "salary_text", mode="before")
    @classmethod
    def _blank_none(cls, v: Any) -> Any:
        if isinstance(v, str) and v.strip().lower() in ("", "null", "none", "n/a", "unknown"):
            return None
        return v.strip() if isinstance(v, str) else v

    @field_validator("employment_type", mode="before")
    @classmethod
    def _employment(cls, v: Any) -> str:
        return _one_of(v, ("full_time", "internship", "contract"))

    @field_validator("work_mode", mode="before")
    @classmethod
    def _mode(cls, v: Any) -> str:
        text = _one_of(v, ("onsite", "hybrid", "remote", "on_site", "wfh", "work_from_home"))
        return {"on_site": "onsite", "wfh": "remote", "work_from_home": "remote"}.get(text, text)

    @field_validator("locations", "degrees", "skills_required", "skills_preferred", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> list[str]:
        return _str_list(v)

    @field_validator("batch_years", mode="before")
    @classmethod
    def _years(cls, v: Any) -> list[int]:
        years: list[int] = []
        for item in _as_list(v):
            try:
                year = int(str(item).strip()[:4])
            except ValueError:
                continue
            if 2000 <= year <= 2100 and year not in years:
                years.append(year)
        return years

    @field_validator("deadline", mode="before")
    @classmethod
    def _deadline(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()[:10]
            try:
                return date.fromisoformat(v)
            except ValueError:
                return None
        return v

    @field_validator("apply_url", mode="before")
    @classmethod
    def _url(cls, v: Any) -> str | None:
        if not isinstance(v, str):
            return None
        parts = urlsplit(v.strip())
        ok = parts.scheme in ("http", "https") and "." in (parts.hostname or "")
        return v.strip() if ok else None

    @field_validator("summary", mode="before")
    @classmethod
    def _summary(cls, v: Any) -> str:
        return str(v or "").strip()[:600]

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence(cls, v: Any) -> float:
        try:
            return min(max(float(v), 0.0), 1.0)
        except (TypeError, ValueError):
            return 0.0

    @property
    def is_job(self) -> bool:
        """False when the LLM thinks this is not a real single job posting."""
        return not (self.confidence < 0.4 and not (self.company or self.role))
