"""The no-key fallback: share of the job's required skills found in the profile. It can sort
the Inbox but has no confidence, so it never triggers an alert."""

from __future__ import annotations

import re
from typing import Any

from jobradar.providers.base import JobView, MatchAnswers, ProviderName, ProviderUnavailable

_ALIASES = {
    "js": "javascript", "ts": "typescript", "node": "nodejs", "reactjs": "react",
    "nextjs": "next", "postgres": "postgresql", "psql": "postgresql", "mongo": "mongodb",
    "k8s": "kubernetes", "golang": "go", "ml": "machinelearning", "py": "python",
    "restapis": "restapi", "restfulapis": "restapi", "restful": "restapi", "rest": "restapi",
    "restapidesign": "restapi", "html5": "html", "css3": "css", "tailwind": "tailwindcss",
    "expressjs": "express", "gcp": "googlecloud", "amazonwebservices": "aws", "cicd": "cicd",
}  # fmt: skip


# Not technical skills: never shown as "missing", never counted in coverage.
_SOFT = re.compile(
    r"communication|problem[\s-]?solving|team|interpersonal|analytical|leadership|typing|"
    r"attitude|customer|basic|knowledge|learn|presentation|english|time management|"
    r"interpretation|documentation|troubleshooting|support|critical thinking|adaptab|"
    r"programming|coding|computer",
    re.IGNORECASE,
)


def technical(skills: list[str]) -> list[str]:
    return [s for s in skills if not _SOFT.search(s)]


def skill_key(name: str) -> str:
    """'JavaScript (ES6+)' -> 'javascript'; 'Node.js' -> 'nodejs'; 'REST APIs' -> 'restapi'."""
    text = re.sub(r"\([^)]*\)", "", name).casefold()
    key = re.sub(r"[^a-z0-9+#]", "", text)
    return _ALIASES.get(key, key)


def profile_skill_keys(profile: dict[str, Any]) -> set[str]:
    names = list(profile.get("skills", []))
    for project in profile.get("projects", []):
        names += [str(t) for t in project.get("tags", [])]
    keys: set[str] = set()
    for name in names:  # "HTML/CSS" is HTML and CSS too; "JavaScript (ES6+)" is ES6 too
        parts = [name, *name.split("/"), *re.findall(r"\(([^)]*)\)", name)]
        keys |= {skill_key(part) for part in parts}
        keys |= {key.rstrip("+") for key in keys if key[:-1].isalnum()}
    return keys - {""}


def compare_skills(job_skills: list[str], profile: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(the job's skills you have, the job's skills you don't), in the job's wording."""
    have = profile_skill_keys(profile)
    skills = technical(job_skills)
    matched = [s for s in skills if skill_key(s) in have]
    missing = [s for s in skills if skill_key(s) not in have]
    return matched, missing


class SkillsRule:
    name: ProviderName = "skills_rule"

    async def match(self, job: JobView, profile: dict[str, Any]) -> MatchAnswers:
        skills = technical(job.posting.skills_required)
        if not skills:
            raise ProviderUnavailable("the job lists no technical skills")
        matched, _ = compare_skills(skills, profile)
        return MatchAnswers(
            provider=self.name, skills_coverage=round(len(matched) / len(skills), 2)
        )
