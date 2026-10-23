"""Interviews by source: for each chat, board or page you applied from, how many of those
applications reached the next round. Shown above the Applications board and by
`jobradar sources`, from the same rows, so the two never disagree."""

from __future__ import annotations

from dataclasses import dataclass

from jobradar.db.models import JobStatus
from jobradar.db.repo import Repo

UNDO_MINUTES = 10  # a status changed again this soon was a misclick

NEXT, WAITING, REJECTED = "next", "waiting", "rejected"
_ORDER = {NEXT: 0, WAITING: 1, REJECTED: 2}
PLATFORMS = {"telegram": "Telegram", "board": "Board", "page": "Page", "manual": "Added by you",
             "email": "Email", "whatsapp": "WhatsApp"}  # fmt: skip


@dataclass(frozen=True)
class Application:
    job_id: str
    outcome: str  # next, waiting or rejected
    first: bool  # this source had the job before any other


@dataclass(frozen=True)
class SourceRow:
    name: str
    platform: str  # "Telegram", "Board"…
    jobs: list[Application]  # next round first, then waiting, then rejected

    @property
    def applied(self) -> int:
        return len(self.jobs)

    @property
    def next_round(self) -> int:
        return sum(a.outcome == NEXT for a in self.jobs)


@dataclass(frozen=True)
class BySource:
    rows: list[SourceRow]  # most next rounds first, then most applications
    applications: int  # each job once, however many sources carried it
    next_round: int  # likewise

    @property
    def summary(self) -> str:
        n = self.applications
        head = f"{n} application{'' if n == 1 else 's'} from {len(self.rows)} source" + (
            "" if len(self.rows) == 1 else "s"
        )
        if not self.next_round:
            return f"{head}. None has reached an interview yet."
        return f"{head}. {self.next_round} reached the next round."


def outcome(status_now: str, next_round: bool) -> str:
    if next_round:
        return NEXT  # rejected after an interview still reached the next round
    return REJECTED if status_now == JobStatus.REJECTED.value else WAITING


def by_source(repo: Repo) -> BySource:
    grouped: dict[tuple[str, str], list[Application]] = {}
    outcomes: dict[str, str] = {}
    for platform, name, job_id, status, next_round, first in repo.applications_by_source(
        UNDO_MINUTES
    ):
        kind = outcome(status, next_round)
        outcomes[job_id] = kind
        grouped.setdefault((platform, name), []).append(Application(job_id, kind, first))
    rows = [
        SourceRow(
            name,
            PLATFORMS.get(platform, platform.title()),
            sorted(jobs, key=lambda a: _ORDER[a.outcome]),
        )
        for (platform, name), jobs in grouped.items()
    ]
    rows.sort(key=lambda r: (-r.next_round, -r.applied, r.name.lower()))
    return BySource(rows, len(outcomes), sum(k == NEXT for k in outcomes.values()))


def render(report: BySource) -> str:
    """The terminal version: one line per source, ● next round, ○ no reply yet, x rejected."""
    if not report.rows:
        return "No applications yet. Mark a job Applied and its source shows up here."
    marks = {NEXT: "●", WAITING: "○", REJECTED: "x"}
    width = min(max(len(r.name) for r in report.rows), 40)
    lines = [report.summary]
    for r in report.rows:
        dots = "".join(marks[a.outcome] for a in r.jobs)
        lines.append(f"  {r.name[:width]:<{width}}  {r.platform:<9} {dots:<12} "
                     f"{r.next_round} of {r.applied}")  # fmt: skip
    lines.append("● reached the next round  ○ applied, no reply yet  x rejected")
    return "\n".join(lines)
