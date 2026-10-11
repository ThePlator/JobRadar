"""Match with the configured LLM (prompts/match.md). Its answers are levels, not free text;
confidence is capped at 0.6 so that LLM-only matches alert less readily than Jev's."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, field_validator

from jobradar.llm import SchemaError, load_prompt
from jobradar.providers.base import JobView, MatchAnswers, ProviderName, ProviderUnavailable
from jobradar.queue.tasks import PermanentError

if TYPE_CHECKING:
    from jobradar.llm import LLM

MAX_CONFIDENCE = 0.6
JOB_TEXT_LIMIT = 6_000
_YES_NO = {"yes": 0.95, "no": 0.05, "unclear": 0.5}


def _level(value: Any, top: int) -> int:
    try:
        return min(max(round(float(value)), 0), top)
    except (TypeError, ValueError):
        return 0


class LLMMatch(BaseModel):
    """What prompts/match.md returns. Lenient: an odd value becomes "unclear" / level 0."""

    eligible: str = "unclear"
    not_eligible_because: str | None = None
    location_ok: str = "unclear"
    role_alignment: int = 0
    skills_coverage: int = 0
    seniority_fit: int = 0
    overall_fit: int = 0
    confidence: float = 0.0

    @field_validator("eligible", "location_ok", mode="before")
    @classmethod
    def _yes_no(cls, v: Any) -> str:
        text = str(v).strip().lower()
        return text if text in _YES_NO else "unclear"

    @field_validator("not_eligible_because", mode="before")
    @classmethod
    def _short(cls, v: Any) -> str | None:
        text = " ".join(str(v or "").split())
        return None if text.lower() in ("", "null", "none") else text[:60]

    @field_validator("role_alignment", "skills_coverage", mode="before")
    @classmethod
    def _four(cls, v: Any) -> int:
        return _level(v, 3)

    @field_validator("seniority_fit", mode="before")
    @classmethod
    def _three(cls, v: Any) -> int:
        return _level(v, 2)

    @field_validator("overall_fit", mode="before")
    @classmethod
    def _five(cls, v: Any) -> int:
        return _level(v, 4)

    @field_validator("confidence", mode="before")
    @classmethod
    def _unit(cls, v: Any) -> float:
        try:
            return min(max(float(v), 0.0), 1.0)
        except (TypeError, ValueError):
            return 0.0


def job_text(job: JobView) -> str:
    """Extracted details first, then the post and the page, within the size limit."""
    details = job.posting.model_dump(
        mode="json", exclude={"form_fields", "confidence", "apply_url"}, exclude_defaults=True
    )
    parts = [
        "Details: " + json.dumps(details, ensure_ascii=False),
        "Post:\n" + job.post_text.strip(),
        "Page:\n" + job.page_text.strip(),
    ]
    return "\n\n".join(p for p in parts if not p.endswith(":\n"))[:JOB_TEXT_LIMIT]


class LLMProvider:
    name: ProviderName = "llm"

    def __init__(self, llm: LLM, model: str, branch_strict: bool) -> None:
        self.llm = llm
        self.model = model
        self.prompt = load_prompt("match")
        self.branch_rule = (
            "if the job is limited to branches or streams that do not include the "
            "candidate's branch (e.g. 'CSE/IT only' for an Electrical candidate), answer \"no\"."
            if branch_strict
            else "ignore branch or stream limits; judge degree, batch and CGPA only."
        )

    async def match(self, job: JobView, profile: dict[str, Any]) -> MatchAnswers:
        try:
            found = await self.llm.structured(
                self.model,
                self.prompt,
                LLMMatch,
                branch_rule=self.branch_rule,
                profile=json.dumps(profile, ensure_ascii=False),
                job=job_text(job),
            )
        except (SchemaError, PermanentError) as e:
            raise ProviderUnavailable(str(e)) from e
        return MatchAnswers(
            provider=self.name,
            eligible=_YES_NO[found.eligible],
            not_eligible_because=found.not_eligible_because if found.eligible == "no" else None,
            location_ok=_YES_NO[found.location_ok],
            role_alignment=found.role_alignment / 3,
            skills_coverage=found.skills_coverage / 3,
            seniority_fit=found.seniority_fit / 2,
            overall_fit=found.overall_fit / 4,
            confidence=min(found.confidence, MAX_CONFIDENCE),
            raw=found.model_dump(),
        )
