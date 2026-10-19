"""Read a built resume the way hiring software reads it: the PDF's text layer, not the page.

An applicant tracking system (Workday, Oracle, SuccessFactors) imports the text it can pull
out of the PDF. A resume that looks right can still import with the email missing or the
dates dropped, so after each build the text is extracted with pypdf and checked:

- the email and the phone number are there as plain text (not only an icon or a link);
- date ranges use a plain hyphen or "to": Workday drops dates written with a long dash
  (found by MadsLorentzen/ai-job-search);
- no replacement characters, private-use glyphs or "(cid:12)" codes where letters should be;
- words don't run together ("pipelineinPython"): a PDF with no real spaces between a bold
  run and its neighbours reads that way in some parsers;
- each entry's heading comes before its first bullet (two-column layouts can scramble this).

A problem is the template's, not the content's: the resume is kept and the problem logged
with the text that shows it. The job's skills that you have are also looked for, so you can
see which of them the page actually carries; nothing is ever added to raise that number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobradar.providers.skills_rule import compare_skills, skill_key
from jobradar.resume.render import ResumeContent, strip_marks
from jobradar.resume.validate import keys_in

_LONG_DASH = "\u2012-\u2015\u2212"  # figure, en, em and horizontal-bar dashes; minus
_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b\.?"
)
# A year or month, a long dash, then a year, month or "present": a date range.
_WHEN = rf"(?:\b{_MONTH}\s*)?(?:\b(?:19|20)\d{{2}}\b)|\b{_MONTH}"
_DASH_RANGE = re.compile(
    rf"(?:{_WHEN})\s*[{_LONG_DASH}]\s*(?:{_WHEN}|present\b|current\b|now\b|today\b)",
    re.IGNORECASE,
)
_GARBLED = re.compile(r"[�-]|\(cid:\d+\)")
_WORD = re.compile(r"[A-Za-z]+")
_URLISH = re.compile(r"\S+\.(?:com|in|io|dev|org|net)\S*|\S+@\S+", re.IGNORECASE)


@dataclass
class AtsReport:
    problems: list[str] = field(default_factory=list)
    skills_on_resume: list[str] = field(default_factory=list)  # the job's, that you have
    skills_not_on_resume: list[str] = field(default_factory=list)  # you have them; not printed

    def as_record(self) -> dict[str, Any]:
        return {
            "problems": self.problems,
            "skills_on_resume": self.skills_on_resume,
            "skills_not_on_resume": self.skills_not_on_resume,
        }


def pdf_text(pdf: Path) -> str:
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(pdf).pages)


def _squash(text: str) -> str:
    """Letters and digits only, lowercased: survives any spacing the extractor invents."""
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def _vocabulary(content: ResumeContent) -> set[str]:
    parts = [content.name, content.headline, content.summary]
    parts += [s for _, group in content.skills for s in group] + [g for g, _ in content.skills]
    for item in content.experience + content.projects:
        parts += [item.heading, item.subtitle, item.side, item.dates, *item.bullets]
    for e in content.education:
        parts += list(e.values())
    text = _URLISH.sub(" ", strip_marks(" ".join(parts)))
    return {w.casefold() for w in _WORD.findall(text)}


def _splits(word: str, vocab: set[str]) -> bool:
    """True when `word` is two or more vocabulary words with the spaces lost."""
    ok = [False] * (len(word) + 1)  # ok[i]: word[:i] is a run of vocabulary words
    ok[0] = True
    for end in range(1, len(word) + 1):
        ok[end] = any(
            ok[start] and word[start:end] in vocab and (start, end) != (0, len(word))
            for start in range(end)
        )
    return ok[len(word)]


def run_together(text: str, content: ResumeContent) -> list[str]:
    """Words in the extracted text that are resume words joined with no space."""
    vocab = {w for w in _vocabulary(content) if len(w) >= 2} | {"a"}
    found: list[str] = []
    for token in _WORD.findall(_URLISH.sub(" ", text)):
        low = token.casefold()
        if len(low) >= 6 and low not in vocab and _splits(low, vocab) and token not in found:
            found.append(token)
    return found


def _order(text: str, content: ResumeContent) -> list[str]:
    flat, problems = _squash(text), []
    for item in content.experience + content.projects:
        head = _squash(strip_marks(item.heading))[:24]
        first = _squash(strip_marks(item.bullets[0]))[:24] if item.bullets else ""
        if not head or not first:
            continue
        h, b = flat.find(head), flat.find(first)
        if h != -1 and b != -1 and b < h:
            problems.append(f"'{strip_marks(item.heading)[:40]}' reads after its own bullets")
    return problems


def check(
    pdf: Path, content: ResumeContent, job_skills: list[str], profile: dict[str, Any]
) -> AtsReport:
    text = pdf_text(pdf)
    report = AtsReport()
    flat = _squash(text)
    email = next((t for t, link in content.contact if link and link.startswith("mailto:")), "")
    if email and _squash(email) not in flat:
        report.problems.append(f"the email {email} isn't in the PDF's text")
    phone = next((t for t, link in content.contact if not link and sum(map(str.isdigit, t)) >= 7),
                 "")  # fmt: skip
    if phone and re.sub(r"\D", "", phone) not in re.sub(r"\D", "", text):
        report.problems.append(f"the phone number {phone} isn't in the PDF's text")
    if dashes := sorted({m.group(0) for m in _DASH_RANGE.finditer(text)}):
        report.problems.append(
            "dates written with a long dash, which Workday drops: " + ", ".join(dashes[:3])
        )
    if garbled := sorted(set(_GARBLED.findall(text))):
        report.problems.append("unreadable characters in the text: " + " ".join(garbled[:5]))
    if joined := run_together(text, content):
        report.problems.append(f"{len(joined)} word(s) run together, e.g. " + ", ".join(joined[:3]))
    report.problems += _order(text, content)

    have, _ = compare_skills(job_skills, profile)
    printed = keys_in(text)
    for skill in dict.fromkeys(have):
        on_page = skill_key(skill) in printed
        (report.skills_on_resume if on_page else report.skills_not_on_resume).append(skill)
    return report
