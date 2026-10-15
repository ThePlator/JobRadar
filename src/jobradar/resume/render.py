"""Resume content -> .tex, through a Jinja2 template with LaTeX-friendly delimiters.

Templates use `\\VAR{...}` for values and `\\BLOCK{...}` for loops and ifs, so LaTeX's own
braces need no escaping. Every value is LaTeX-escaped on the way in; only `Tex` values (built
here, e.g. bold runs from `**...**`) are inserted as they are.

`content_from_profile` turns profile.yaml into `ResumeContent` with nothing changed: the
plain resume, and the starting point that tailoring selects and rephrases from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import jinja2

TEMPLATES = Path(__file__).parent / "templates"

_ESCAPES = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
    "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
    "|": r"\textbar{}", "<": r"\textless{}", ">": r"\textgreater{}",
    "\u2014": "---", "\u2013": "--", "\u2018": "`", "\u2019": "'", "\u201c": "``", "\u201d": "''",
    "\u2026": r"\ldots{}", "\u00a0": "~",
}  # fmt: skip
_ESCAPE_RE = re.compile("|".join(re.escape(c) for c in _ESCAPES))


class Tex(str):
    """A string that is already LaTeX: inserted into templates without escaping."""

    __slots__ = ()


def latex_escape(text: str) -> str:
    return _ESCAPE_RE.sub(lambda m: _ESCAPES[m.group(0)], text)


def strip_marks(text: str) -> str:
    """The text without **bold** markers."""
    return text.replace("**", "")


def bold(text: str) -> Tex:
    """Escape, and print **marked** runs in bold. Unbalanced markers are dropped."""
    parts = text.split("**")
    if len(parts) % 2 == 0:  # an odd number of markers: don't guess which run was meant
        return Tex(latex_escape(strip_marks(text)))
    return Tex(
        "".join(
            rf"\textbf{{{latex_escape(p)}}}" if i % 2 else latex_escape(p)
            for i, p in enumerate(parts)
        )
    )


def href(url: str, label: str | None = None) -> Tex:
    """\\href to an http(s) link; plain escaped text for anything else."""
    shown = label if label is not None else display_url(url)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https", "mailto") or re.search(r"[\s\\{}]", url):
        return Tex(latex_escape(shown))
    safe = url.replace("%", r"\%").replace("#", r"\#")
    return Tex(rf"\href{{{safe}}}{{{latex_escape(shown)}}}")


def display_url(url: str) -> str:
    """'https://www.github.com/ThePlator/' -> 'github.com/ThePlator'."""
    text = re.sub(r"^[a-z]+:(//)?", "", url.strip(), flags=re.IGNORECASE)
    return text.removeprefix("www.").rstrip("/")


def dates(text: str | None) -> str:
    """'Feb 2026 - May 2026' -> 'Feb 2026 -- May 2026' in LaTeX (an en dash)."""
    return re.sub(r"\s+-\s+", " \u2013 ", text or "").strip()


# ---- content ---------------------------------------------------------------------------------


@dataclass
class Item:
    """One experience or project entry as printed."""

    id: str
    heading: str  # "Software Engineering Intern -- Lexipitch"
    dates: str = ""
    subtitle: str = ""  # location, or the project's stack
    side: str = ""  # right of the subtitle: the project's context
    url: str | None = None
    link_in_heading: bool = False  # projects show their link after the name
    bullets: list[str] = field(default_factory=list)  # may contain **bold**
    bullet_ids: list[str] = field(default_factory=list)


@dataclass
class ResumeContent:
    name: str
    headline: str
    contact: list[tuple[str, str | None]]  # (text, link)
    links: list[str]
    summary: str
    skills: list[tuple[str, list[str]]]
    experience: list[Item]
    projects: list[Item]
    education: list[dict[str, str]]


SKILL_LABELS = {"data": "Data", "infrastructure": "Infrastructure"}


def _str(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _items(profile: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = profile.get(key)
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def _bullets(item: dict[str, Any]) -> tuple[list[str], list[str]]:
    bullets = [b for b in item.get("bullets") or [] if isinstance(b, dict) and _str(b.get("text"))]
    return [_str(b["text"]) for b in bullets], [_str(b.get("id")) for b in bullets]


def content_from_profile(profile: dict[str, Any]) -> ResumeContent:
    basics = profile.get("basics") or {}
    links = basics.get("links") or {}
    email, phone, location = (
        _str(basics.get("email")),
        _str(basics.get("phone")),
        _str(basics.get("location")),
    )
    contact: list[tuple[str, str | None]] = []
    if email:
        contact.append((email, f"mailto:{email}"))
    if phone:
        contact.append((phone, None))
    if location:
        contact.append((location, None))
    skills_raw = profile.get("skills") or {}
    groups = skills_raw.items() if isinstance(skills_raw, dict) else [("skills", skills_raw)]
    skills = [
        (
            SKILL_LABELS.get(str(k), str(k).replace("_", " ").title()),
            [_str(s) for s in v if _str(s)],
        )
        for k, v in groups
        if isinstance(v, list)
    ]
    experience = []
    for x in _items(profile, "experience"):
        texts, ids = _bullets(x)
        company = _str(x.get("company"))
        experience.append(
            Item(
                id=_str(x.get("id")),
                heading=f"{_str(x.get('title'))} \u2014 {company}"
                if company
                else _str(x.get("title")),
                dates=dates(x.get("dates")),
                subtitle=_str(x.get("location")),
                url=_str(x.get("url")) or None,
                bullets=texts,
                bullet_ids=ids,
            )
        )
    projects = []
    for p in _items(profile, "projects"):
        texts, ids = _bullets(p)
        projects.append(
            Item(
                id=_str(p.get("id")),
                heading=_str(p.get("name")),
                dates=dates(p.get("dates")),
                subtitle=_str(p.get("stack")) or _str(p.get("role")),
                side=_str(p.get("context")),
                url=_str(p.get("url")) or None,
                link_in_heading=True,
                bullets=texts,
                bullet_ids=ids,
            )
        )
    education = []
    for e in _items(profile, "education"):
        degree = " \u2014 ".join(x for x in (_str(e.get("degree")), _str(e.get("branch"))) if x)
        if cgpa := _str(e.get("cgpa")):
            degree += f" (CGPA: {cgpa}/10)"
        education.append({"school": _str(e.get("school")), "years": dates(e.get("years")),
                          "degree": degree, "location": _str(e.get("location"))})  # fmt: skip
    return ResumeContent(
        name=_str(basics.get("name")),
        headline=_str(basics.get("headline")),
        contact=contact,
        links=[
            _str(u)
            for u in (links.get("linkedin"), links.get("github"), links.get("portfolio"))
            if _str(u)
        ],
        summary=" ".join(_str(profile.get("summary")).split()),
        skills=skills,
        experience=experience,
        projects=projects,
        education=education,
    )


# ---- rendering -------------------------------------------------------------------------------


def _finalize(value: Any) -> str:
    if isinstance(value, Tex):
        return value
    return latex_escape("" if value is None else str(value))


def environment(directory: Path = TEMPLATES) -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(directory),
        block_start_string=r"\BLOCK{",
        block_end_string="}",
        variable_start_string=r"\VAR{",
        variable_end_string="}",
        comment_start_string=r"\#{",
        comment_end_string="}",
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,  # noqa: S701 (LaTeX, not HTML: values are LaTeX-escaped in finalize)
        keep_trailing_newline=True,
        undefined=jinja2.StrictUndefined,
        finalize=_finalize,
    )
    env.globals.update(bold=bold, href=href, Tex=Tex)
    return env


def template_path(name: str) -> tuple[Path, str]:
    """A built-in template name ('classic') or a path to your own .tex.j2 file."""
    path = Path(name)
    if path.suffix == ".j2" or path.is_file():
        return path.parent, path.name
    return TEMPLATES, f"{name}.tex.j2"


def render(content: ResumeContent, template: str = "classic") -> str:
    directory, file = template_path(template)
    try:
        tpl = environment(directory).get_template(file)
    except jinja2.TemplateNotFound:
        raise ValueError(f"resume template {template!r} not found") from None
    return tpl.render(r=content)
