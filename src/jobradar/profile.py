"""config/profile.yaml, and the compact view of it that matching sends to a provider.

Only what matching needs leaves this machine: education, skills, experience and project
bullets, preferences and filters. `basics` (name, email, phone, links), `summary` and
`answers` (expected CTC, …) are never included.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from jobradar.config import ConfigError, FiltersConfig, MatchingConfig
from jobradar.pipeline.dedupe import sha1


def load_profile(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(
            f"{path} not found. Copy profile.example.yaml to {path} and fill it in; "
            "matching needs it."
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must be a mapping (basics:, education:, skills:, ...).")
    return raw


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _str(value: Any) -> str | None:
    return str(value).strip() if value not in (None, "") else None


def _bullets(item: dict[str, Any]) -> list[str]:
    return [
        t.replace("**", "")  # **bold** marks are for the resume only
        for b in _list(item.get("bullets"))
        if isinstance(b, dict) and (t := _str(b.get("text")))
    ]


def resume_problems(profile: dict[str, Any]) -> list[str]:
    """What stops profile.yaml from being used for resumes: missing name, missing or repeated
    ids on experience, projects and bullets."""
    problems = []
    basics = profile.get("basics")
    if not isinstance(basics, dict) or not _str(basics.get("name")):
        problems.append("basics.name is missing")
    seen: set[str] = set()
    for section in ("experience", "projects"):
        for n, item in enumerate(_list(profile.get(section)), 1):
            if not isinstance(item, dict):
                problems.append(f"{section} #{n} is not a mapping")
                continue
            where = f"{section} #{n}"
            ids = [(where, item.get("id"))] + [
                (f"{where} bullet #{m}", b.get("id") if isinstance(b, dict) else None)
                for m, b in enumerate(_list(item.get("bullets")), 1)
            ]
            for place, value in ids:
                if not _str(value):
                    problems.append(f"{place} needs an id")
                elif str(value) in seen:
                    problems.append(f"id {value!r} is used twice")
                else:
                    seen.add(str(value))
    return problems


def skill_names(profile: dict[str, Any]) -> list[str]:
    """Every skill listed under `skills:`, whether grouped ({languages: [...]}) or flat."""
    skills = profile.get("skills")
    groups = skills.values() if isinstance(skills, dict) else [skills]
    names: list[str] = []
    for group in groups:
        for name in _list(group):
            if (text := _str(name)) and text not in names:
                names.append(text)
    return names


def skill_warnings(profile: dict[str, Any]) -> list[str]:
    """Skills YAML cut in two: in `[a (b, c), d]` the comma inside brackets ends the item,
    so "a (b" and "c)" become two skills that match nothing."""
    return [
        f"skills: {name!r} looks cut at a comma inside brackets; put the whole item in "
        f'quotes, e.g. "Machine Learning (feature engineering, model training)"'
        for name in skill_names(profile)
        if name.count("(") != name.count(")")
    ]


def _graduation_year(education: dict[str, Any]) -> int | None:
    if isinstance(year := education.get("graduation_year"), int):
        return year
    years = re.findall(r"\b(20\d\d)\b", str(education.get("years", "")))
    return int(years[-1]) if years else None


def compact_profile(
    profile: dict[str, Any], filters: FiltersConfig, matching: MatchingConfig
) -> dict[str, Any]:
    """What a match provider sees. Keep this free of contact details."""
    education = [
        {
            "degree": _str(e.get("degree")),
            "branch": _str(e.get("branch")),
            "graduation_year": _graduation_year(e),
            "cgpa": e.get("cgpa"),
        }
        for e in _list(profile.get("education"))
        if isinstance(e, dict)
    ]
    experience = [
        {"title": _str(x.get("title")), "dates": _str(x.get("dates")), "bullets": _bullets(x)}
        for x in _list(profile.get("experience"))
        if isinstance(x, dict)
    ]
    projects = [
        {"name": _str(p.get("name")), "tags": _list(p.get("tags")), "bullets": _bullets(p)}
        for p in _list(profile.get("projects"))
        if isinstance(p, dict)
    ]
    prefs = profile.get("preferences")
    prefs = prefs if isinstance(prefs, dict) else {}
    return {
        "education": education,
        "skills": skill_names(profile),
        "experience": experience,
        "projects": projects,
        "preferences": {
            "target_roles": _list(prefs.get("target_roles")),
            "work_modes": _list(prefs.get("work_modes")),
            "locations": filters.locations or ["anywhere in India", "remote"],
        },
        "rules": {
            "batch_years": filters.batch_years,
            "max_experience_years": filters.max_experience_years,
            "branch_strict": matching.branch_strict,
        },
    }


def profile_version(compact: dict[str, Any]) -> str:
    """Changes whenever anything matching sees changes; jobs are then matched again."""
    return sha1(json.dumps(compact, sort_keys=True, ensure_ascii=False))[:16]


_ITEMS = r"(?P<items>[^\]\n]*)\](?P<tail>[ \t]*(?:#.*)?)$"
_ADDITIONAL = re.compile(r"^(?P<lead>[ \t]+additional:[ \t]*\[)" + _ITEMS, re.MULTILINE)
_FLAT = re.compile(r"^(?P<lead>skills:[ \t]*\[)" + _ITEMS, re.MULTILINE)
_PLAIN_ITEM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .+#/&()-]*$")


def add_skill(path: Path, name: str, now: str) -> str:
    """Add one skill to profile.yaml without touching anything else: to the one-line
    `additional: [...]` list under `skills:` (or a one-line flat `skills: [...]`). Keeps a
    backup first (profile.yaml.bak-<now>) and puts it back if the result doesn't read as the
    same profile plus the skill. Returns the backup's name; raises ConfigError otherwise."""
    from jobradar.providers.skills_rule import profile_skill_keys, skill_key

    name = " ".join(name.split())[:60]
    if not name:
        raise ConfigError("no skill name given")
    before = load_profile(path)
    if skill_key(name) in profile_skill_keys(before):
        raise ConfigError(f"{name} is already in your profile")
    text = path.read_text(encoding="utf-8")
    item = name if _PLAIN_ITEM.match(name) and ": " not in name else json.dumps(name)
    for pattern in (_ADDITIONAL, _FLAT):
        found = pattern.search(text)
        if found:
            items = found.group("items").rstrip()
            joined = f"{items}, {item}" if items.strip() else item
            new = (text[: found.start()] + found.group("lead") + joined + "]"
                   + found.group("tail") + text[found.end():])  # fmt: skip
            break
    else:
        raise ConfigError(
            "couldn't find a one-line `additional: [...]` list under skills: in profile.yaml; "
            "add the skill there yourself"
        )
    backup = path.with_name(f"{path.name}.bak-{now}")
    backup.write_text(text, encoding="utf-8")
    path.write_text(new, encoding="utf-8")
    try:
        after = load_profile(path)
        same = {k: v for k, v in after.items() if k != "skills"} == {
            k: v for k, v in before.items() if k != "skills"
        }
        if not same or skill_key(name) not in profile_skill_keys(after):
            raise ConfigError("the edited profile didn't read back as expected")
    except ConfigError:
        path.write_text(text, encoding="utf-8")  # put it back exactly as it was
        backup.unlink(missing_ok=True)
        raise
    return backup.name
