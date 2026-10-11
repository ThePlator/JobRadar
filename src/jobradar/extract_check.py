"""`jobradar extract --compare`: how well the free layers (page data and post labels) agree
with what the LLM extracted earlier, field by field. Run it before trusting
`llm.extract_mode: fallback` on your own channels."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from jobradar.clock import from_iso
from jobradar.db.models import JobStatus
from jobradar.db.repo import Repo
from jobradar.pipeline.extract import free_fields, own_post
from jobradar.pipeline.fetch import Fetcher
from jobradar.queue.tasks import RetryableError

FIELDS = (
    "company", "role", "locations", "work_mode", "employment_type", "experience_min",
    "experience_max", "batch_years", "degrees", "salary_text", "deadline",
)  # fmt: skip


_SAME_CITY = {
    "bangalore": "bengaluru", "gurgaon": "gurugram", "mysore": "mysuru", "bombay": "mumbai",
    "madras": "chennai", "trivandrum": "thiruvananthapuram",
}  # fmt: skip


def _norm(value: Any) -> str:
    text = re.sub(r"[^\w₹.+]+", " ", str(value).casefold()).strip()
    return " ".join(_SAME_CITY.get(word, word) for word in text.split())


def agrees(name: str, ours: Any, theirs: Any) -> bool:
    """Loose on wording (case, punctuation, "Wipro" vs "Wipro Ltd"), strict on values."""
    if theirs in (None, "", [], "unknown"):
        return False
    if name in ("company", "role", "salary_text"):
        a, b = _norm(ours), _norm(theirs)
        return a == b or (min(len(a), len(b)) >= 3 and (a in b or b in a))
    if name in ("locations", "degrees"):
        mine, other = {_norm(x) for x in ours}, {_norm(x) for x in theirs}
        return any(x in y or y in x for x in mine for y in other)
    if name == "batch_years":
        return sorted(ours) == sorted(theirs)
    if name == "deadline":
        return str(ours) == str(theirs)
    if name in ("experience_min", "experience_max"):
        return float(ours) == float(theirs)
    return bool(ours == theirs)


@dataclass
class Check:
    jobs: int = 0
    skip_llm: int = 0  # role and company found without the LLM
    skip_llm_but_discarded: list[str] = field(default_factory=list)  # LLM said: not a job
    filled: Counter[str] = field(default_factory=Counter)
    agreed: Counter[str] = field(default_factory=Counter)
    layers: Counter[str] = field(default_factory=Counter)
    differences: dict[str, list[tuple[Any, Any]]] = field(default_factory=dict)


async def refetch_pages(repo: Repo) -> int:
    """Download again the pages fetched before JobPosting data was kept. Same politeness
    and safety rules as the pipeline; JavaScript rendering is skipped for speed."""
    fetcher = Fetcher(render_js=False)
    count = 0
    try:
        for job in repo.jobs_read_by_llm():
            page = repo.get_page(job.id)
            if not job.canonical_url or (page and page.structured_json):
                continue
            try:
                fresh = await fetcher.fetch(job.canonical_url)
            except RetryableError:
                continue
            text_ = fresh.text or (page.text if page else None)  # keep text a site now hides
            repo.save_page(
                job.id, fresh.final_url, fresh.title, text_, fresh.note, fresh.structured
            )
            count += 1
    finally:
        await fetcher.aclose()
    return count


def run_check(repo: Repo) -> Check:
    check = Check()
    for job in repo.jobs_read_by_llm():
        data: dict[str, Any] = json.loads(job.data_json or "{}")
        if "_sources" in data:
            continue  # read by this version: nothing to compare against
        page = repo.get_page(job.id)
        structured = json.loads(page.structured_json) if page and page.structured_json else None
        sightings = repo.job_sightings(job.id)
        posted = from_iso(sightings[0].posted_at).date() if sightings else None
        fields, sources = free_fields(structured, own_post(repo, job.id), posted)

        check.jobs += 1
        if fields.get("role") and fields.get("company"):
            check.skip_llm += 1
            if job.status == JobStatus.DISCARDED:
                check.skip_llm_but_discarded.append(job.canonical_url or job.id)
        if job.status == JobStatus.DISCARDED:
            continue  # nothing to compare field by field
        for name in FIELDS:
            if name not in fields:
                continue
            ours = fields[name]
            ours = ours.isoformat() if hasattr(ours, "isoformat") else ours
            check.filled[name] += 1
            check.layers[sources[name]] += 1
            if agrees(name, ours, data.get(name)):
                check.agreed[name] += 1
            else:
                check.differences.setdefault(name, []).append((ours, data.get(name)))
    return check


def render(check: Check, examples: int = 0) -> str:
    if not check.jobs:
        return (
            "No jobs read by the LLM alone to compare against. "
            "(Jobs read by this version are already marked with the layer that filled them.)"
        )
    pct = 100 * check.skip_llm / check.jobs
    lines = [
        f"Jobs the LLM read earlier: {check.jobs}",
        f"Role and company found without the LLM: {check.skip_llm} ({pct:.0f}%): "
        "with extract_mode: fallback, these skip the LLM",
    ]
    if check.skip_llm_but_discarded:
        lines.append(
            f"  of these, the LLM had said 'not a job': {len(check.skip_llm_but_discarded)}"
        )
        lines += [f"    {url}" for url in check.skip_llm_but_discarded[: examples or 5]]
    lines += ["", f"{'Field':<17}{'found':>6}{'agrees':>8}"]
    for name in FIELDS:
        found = check.filled[name]
        share = f"{100 * check.agreed[name] / found:.0f}%" if found else "-"
        lines.append(f"{name:<17}{found:>6}{share:>8}")
    layers = ", ".join(f"{layer} {n}" for layer, n in check.layers.most_common())
    lines += ["", f"Fields filled by: {layers}"]
    lines.append(
        "'agrees' counts loose matches; most differences are the LLM adding detail "
        "from the page (e.g. listing every city)."
    )
    if examples:
        for name, pairs in check.differences.items():
            lines += ["", f"{name}: free layers vs LLM"]
            lines += [f"  {ours!s:<45} | {theirs}" for ours, theirs in pairs[:examples]]
    return "\n".join(lines)
