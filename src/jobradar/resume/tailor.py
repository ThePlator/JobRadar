"""Ask the writer model to select, order and lightly rephrase profile facts for one job.

The model sees the job and the candidate's facts with ids, never contact details. What it
returns is untrusted: `resume/validate.py` checks every bullet against the one it claims to
rephrase and falls back to the original wording when anything new appears.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, field_validator

from jobradar.llm import load_prompt
from jobradar.providers.base import JobView
from jobradar.providers.llm import job_text

if TYPE_CHECKING:
    from jobradar.llm import LLM


class TailoredBullet(BaseModel):
    ref_id: str
    text: str = Field(max_length=600)


class TailoredItem(BaseModel):
    id: str
    bullets: list[TailoredBullet] = Field(default_factory=list)


class TailoredResume(BaseModel):
    """What prompts/tailor.md returns."""

    summary: str = Field(default="", max_length=1200)
    skills: dict[str, list[str]] = Field(default_factory=dict)
    experience: list[TailoredItem] = Field(default_factory=list)
    projects: list[TailoredItem] = Field(default_factory=list)

    @field_validator("skills", mode="before")
    @classmethod
    def _groups(cls, v: Any) -> Any:
        if not isinstance(v, dict):
            return {}
        return {
            str(k): [str(s) for s in items] for k, items in v.items() if isinstance(items, list)
        }


def _bullets(item: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"id": str(b.get("id")), "text": str(b.get("text"))}
        for b in item.get("bullets") or []
        if isinstance(b, dict) and b.get("id") and b.get("text")
    ]


def candidate_facts(profile: dict[str, Any]) -> dict[str, Any]:
    """The profile as the writer model sees it: facts with ids, no name or contact details."""
    skills = profile.get("skills")
    return {
        "summary": " ".join(str(profile.get("summary") or "").split()),
        "skills": skills if isinstance(skills, dict) else {"skills": skills or []},
        "experience": [
            {
                "id": x.get("id"),
                "title": x.get("title"),
                "company": x.get("company"),
                "dates": x.get("dates"),
                "bullets": _bullets(x),
            }
            for x in profile.get("experience") or []
            if isinstance(x, dict)
        ],
        "projects": [
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "stack": p.get("stack"),
                "primary": bool(p.get("primary")),
                "tags": p.get("tags"),
                "bullets": _bullets(p),
            }
            for p in profile.get("projects") or []
            if isinstance(p, dict)
        ],
        "education": [
            {"degree": e.get("degree"), "branch": e.get("branch"), "years": e.get("years")}
            for e in profile.get("education") or []
            if isinstance(e, dict)
        ],
    }


async def tailor(llm: LLM, model: str, profile: dict[str, Any], job: JobView) -> TailoredResume:
    prompt = load_prompt("tailor")
    return await llm.structured(
        model,
        prompt,
        TailoredResume,
        profile=json.dumps(candidate_facts(profile), ensure_ascii=False, indent=1),
        job=job_text(job),
    )
