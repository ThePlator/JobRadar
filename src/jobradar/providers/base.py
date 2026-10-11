"""What every match provider returns, and how they are called."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from jobradar.pipeline.schemas import JobPosting

ProviderName = Literal["rules", "llm", "skills_rule"]


class MatchAnswers(BaseModel):
    """Answers on a 0-1 scale (None: not answered). Code turns them into the Match Score."""

    provider: ProviderName
    eligible: float | None = Field(default=None, ge=0, le=1)  # yes-probability
    not_eligible_because: str | None = None  # short, e.g. "batch 2023-2024 only"
    location_ok: float | None = Field(default=None, ge=0, le=1)
    role_alignment: float | None = Field(default=None, ge=0, le=1)
    skills_coverage: float | None = Field(default=None, ge=0, le=1)
    seniority_fit: float | None = Field(default=None, ge=0, le=1)
    overall_fit: float | None = Field(default=None, ge=0, le=1)  # a cross-check, not weighted
    confidence: float = Field(default=0.0, ge=0, le=1)
    raw: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class JobView:
    """What a provider may read about a job."""

    posting: JobPosting
    post_text: str
    page_text: str


class ProviderUnavailable(Exception):
    """This provider can't answer for this job; the next one is tried."""


class MatchProvider(Protocol):
    name: ProviderName

    async def match(self, job: JobView, profile: dict[str, Any]) -> MatchAnswers: ...
