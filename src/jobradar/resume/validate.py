"""Grounding checks (nothing on the resume that profile.yaml doesn't say) and page count.

Tailored text is untrusted. Each check falls back to the profile's own words rather than
dropping content, and every fallback is recorded as a violation:

- a bullet must name an existing bullet of that entry (`ref_id`), else it's dropped;
- its numbers must all appear in that bullet (bold or not), and no size words ("doubled",
  "10x", "halved") unless the bullet has them;
- no technology or skill name the bullet doesn't already contain;
- no phrase from `resume.banned_phrases`;
- skills must be ones listed in that group of profile.yaml;
- the summary may only use numbers and technology names found anywhere in the profile.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from jobradar.providers.skills_rule import skill_key
from jobradar.resume.render import Item, ResumeContent, strip_marks
from jobradar.resume.tailor import TailoredItem, TailoredResume

MIN_BULLETS = 2
PRIMARY_BULLETS = 3  # a project marked `primary: true` always shows, first, with this many

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?\+?%?")
_SIZE_WORDS = re.compile(
    r"\b(?:doubl\w*|tripl\w*|quadrupl\w*|halv\w*|\w+fold|\d+(?:\.\d+)?\s?x)\b", re.IGNORECASE
)
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+#/-]*")

# Technology names a rephrase could slip in. Profile skills and project stacks are added.
COMMON_TECH = {
    "java", "go", "golang", "rust", "c++", "c#", "kotlin", "swift", "scala", "ruby", "php",
    "perl", "dart", "flutter", "typescript", "javascript", "python", "sql", "nosql", "bash",
    "aws", "azure", "gcp", "google cloud", "kubernetes", "k8s", "terraform", "ansible", "helm",
    "kafka", "spark", "hadoop", "airflow", "flink", "rabbitmq", "celery", "temporal",
    "django", "flask", "fastapi", "spring", "spring boot", "laravel", "rails", ".net", "asp.net",
    "angular", "vue", "svelte", "react native", "next.js", "nestjs", "express", "graphql", "grpc",
    "mysql", "postgresql", "sqlite", "mongodb", "dynamodb", "cassandra", "elasticsearch", "redis",
    "snowflake", "bigquery", "docker", "jenkins", "github actions", "gitlab", "linux", "nginx",
    "tensorflow", "pytorch", "keras", "scikit-learn", "pandas", "numpy", "opencv", "langchain",
    "llm", "llms", "nlp", "computer vision", "deep learning", "machine learning", "generative ai",
    "microservices", "serverless", "lambda", "s3", "ec2", "kinesis", "websockets", "oauth", "jwt",
    "tableau", "power bi", "excel", "selenium", "playwright", "jest", "pytest", "junit",
}  # fmt: skip


@dataclass
class Grounded:
    content: ResumeContent
    violations: list[str] = field(default_factory=list)


def numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUMBER.findall(strip_marks(text))}


def _ngrams(text: str, longest: int = 3) -> set[str]:
    words = [w.strip(".,;:") for w in _TOKEN.findall(strip_marks(text))]
    words = [w for w in words if w]
    grams = set()
    for n in range(1, longest + 1):
        for i in range(len(words) - n + 1):
            grams.add(" ".join(words[i : i + n]))
    return grams


def keys_in(text: str) -> set[str]:
    """Skill keys of every 1-3 word run in the text ('REST APIs' -> 'restapi')."""
    found = set()
    for gram in _ngrams(text):
        key = skill_key(gram)
        found.add(key)
        if "/" in gram:  # "Git/GitHub" mentions Git and GitHub
            found |= {skill_key(p) for p in gram.split("/")}
    return found - {""}


def vocabulary(profile: dict[str, Any]) -> set[str]:
    """Keys of every technology name the checks look out for."""
    names = set(COMMON_TECH)
    skills = profile.get("skills")
    for group in skills.values() if isinstance(skills, dict) else [skills or []]:
        names |= {str(s) for s in group if s}
    for project in profile.get("projects") or []:
        if isinstance(project, dict) and project.get("stack"):
            names |= {s.strip() for s in str(project["stack"]).split(",")}
    keys = set()
    for name in names:
        keys.add(skill_key(name))
        keys |= {skill_key(p) for p in re.split(r"/|\s&\s|\(|\)|,", name) if p.strip()}
    return keys - {""}


def new_claims(text: str, source: str, vocab: set[str], banned: list[str]) -> list[str]:
    """What `text` says that `source` doesn't: numbers, size words, technology names."""
    problems = []
    if extra := numbers(text) - numbers(source):
        problems.append(f"number not in the original: {', '.join(sorted(extra))}")
    size = {m.casefold() for m in _SIZE_WORDS.findall(strip_marks(text))}
    if extra_size := size - {m.casefold() for m in _SIZE_WORDS.findall(strip_marks(source))}:
        problems.append(f"size word not in the original: {', '.join(sorted(extra_size))}")
    if new_tech := (keys_in(text) & vocab) - keys_in(source):
        problems.append(f"technology not in the original: {', '.join(sorted(new_tech))}")
    lowered = strip_marks(text).casefold()
    for phrase in banned:
        if phrase.casefold() in lowered and phrase.casefold() not in strip_marks(source).casefold():
            problems.append(f"banned phrase: {phrase}")
    return problems


def _ground_item(
    original: Item, tailored: TailoredItem | None, vocab: set[str], banned: list[str],
    violations: list[str],
) -> Item:  # fmt: skip
    by_id = dict(zip(original.bullet_ids, original.bullets, strict=True))
    texts: list[str] = []
    ids: list[str] = []
    for bullet in tailored.bullets if tailored else []:
        source = by_id.get(bullet.ref_id)
        if source is None:
            violations.append(f"{original.id}: unknown bullet {bullet.ref_id!r} dropped")
            continue
        if bullet.ref_id in ids:
            continue
        text = " ".join(bullet.text.split())
        if problems := new_claims(text, source, vocab, banned):
            violations.append(f"{bullet.ref_id}: {'; '.join(problems)} (original kept)")
            text = source
        texts.append(text)
        ids.append(bullet.ref_id)
    # Too few: top up with the entry's own bullets, in their order.
    for ref, source in by_id.items():
        if len(ids) >= min(MIN_BULLETS, len(by_id)):
            break
        if ref not in ids:
            texts.append(source)
            ids.append(ref)
    return replace(original, bullets=texts, bullet_ids=ids)


def ground(
    plain: ResumeContent,
    tailored: TailoredResume,
    profile: dict[str, Any],
    banned: list[str] | None = None,
) -> Grounded:
    """The tailored resume, with everything that isn't backed by profile.yaml undone."""
    banned = banned or []
    vocab = vocabulary(profile)
    violations: list[str] = []

    # Summary: facts from anywhere in the profile.
    everything = " ".join(
        [plain.summary]
        + [s for _, group in plain.skills for s in group]
        + [b for item in plain.experience + plain.projects for b in item.bullets]
    )
    summary = " ".join(tailored.summary.split())
    if not summary:
        summary = plain.summary
    elif problems := new_claims(summary, everything, vocab, banned):
        violations.append(f"summary: {'; '.join(problems)} (original kept)")
        summary = plain.summary

    # Skills: only ones listed in that group, in the model's order; groups stay as they are.
    skills = []
    wanted = {k.casefold(): v for k, v in tailored.skills.items()}
    raw_groups = profile.get("skills")
    group_keys = list(raw_groups) if isinstance(raw_groups, dict) else ["skills"]
    for key, (label, group) in zip(group_keys, plain.skills, strict=False):
        listed = {s.casefold(): s for s in group}
        chosen = []
        for name in wanted.get(str(key).casefold(), wanted.get(label.casefold(), [])):
            if (real := listed.get(name.strip().casefold())) is None:
                violations.append(f"skills: {name!r} isn't in your {label} skills; left out")
            elif real not in chosen:
                chosen.append(real)
        if len(chosen) < min(3, len(group)):
            chosen += [s for s in group if s not in chosen][: min(3, len(group)) - len(chosen)]
        skills.append((label, chosen or group))

    by_exp = {t.id: t for t in tailored.experience}
    experience = [
        _ground_item(x, by_exp.get(x.id), vocab, banned, violations) for x in plain.experience
    ]

    plain_projects = {p.id: p for p in plain.projects}
    projects: list[Item] = []
    for t in tailored.projects:
        if (original := plain_projects.get(t.id)) is None:
            violations.append(f"projects: unknown project {t.id!r} dropped")
        elif all(p.id != t.id for p in projects):
            projects.append(_ground_item(original, t, vocab, banned, violations))
    for p in reversed(plain.projects):  # primary projects first, with enough bullets
        if not _primary(profile, p.id):
            continue
        shown = next((x for x in projects if x.id == p.id), None) or replace(
            p, bullets=[], bullet_ids=[]
        )
        projects = [shown] + [x for x in projects if x.id != p.id]
        for ref, source in zip(p.bullet_ids, p.bullets, strict=True):
            if len(shown.bullet_ids) >= min(PRIMARY_BULLETS, len(p.bullet_ids)):
                break
            if ref not in shown.bullet_ids:
                shown.bullets.append(source)
                shown.bullet_ids.append(ref)
    if not projects:
        projects = list(plain.projects)

    content = replace(
        plain, summary=summary, skills=skills, experience=experience, projects=projects
    )
    return Grounded(content, violations)


def _primary(profile: dict[str, Any], project_id: str) -> bool:
    return any(
        isinstance(p, dict) and p.get("id") == project_id and p.get("primary") is True
        for p in profile.get("projects") or []
    )


def trim_one(content: ResumeContent) -> bool:
    """Drop the lowest-ranked bullet of the longest entry (keeping 2 per entry). False when
    nothing can go."""
    items = [i for i in content.experience + content.projects if len(i.bullets) > MIN_BULLETS]
    if not items:
        return False
    longest = max(items, key=lambda i: len(i.bullets))
    longest.bullets.pop()
    longest.bullet_ids.pop()
    return True


def page_count(pdf: Path) -> int:
    from pypdf import PdfReader

    return len(PdfReader(pdf).pages)
