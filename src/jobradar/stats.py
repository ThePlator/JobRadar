"""`jobradar stats`: what came in, what became jobs, and which rows look like duplicates."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from jobradar.clock import Clock, to_iso, utcnow
from jobradar.db.models import JobStatus, TaskStatus
from jobradar.db.repo import Repo
from jobradar.pipeline.titles import title_from_post, title_key


@dataclass(frozen=True)
class JobLine:
    job_id: str
    url: str
    title: str
    sightings: int
    first_post: int | None  # jobs from the same post are siblings, never duplicates


@dataclass
class Report:
    days: int
    messages: int = 0
    no_link: int = 0
    jobs: int = 0
    sightings: int = 0
    hidden_ads: list[JobLine] = field(default_factory=list)
    per_chat: list[tuple[str, int, int]] = field(default_factory=list)
    duplicate_groups: list[list[JobLine]] = field(default_factory=list)
    tasks: dict[TaskStatus, int] = field(default_factory=dict)
    failures: list[tuple[str, str, str]] = field(default_factory=list)
    statuses: dict[str, int] = field(default_factory=dict)  # every job, by status, now
    moved: dict[str, int] = field(default_factory=dict)  # statuses you set in the window
    alerts: int = 0
    suggestion: str | None = None

    @property
    def merged(self) -> int:
        """Reposts already folded into an existing job by URL."""
        return self.sightings - self.jobs

    @property
    def extra_rows(self) -> int:
        """Rows that are probably the same job as another row: one per extra post."""
        return sum(_posts(g) - 1 for g in self.duplicate_groups)

    @property
    def duplicate_rate(self) -> float:
        return self.extra_rows / self.jobs if self.jobs else 0.0


def _posts(group: list[JobLine]) -> int:
    return len({j.first_post for j in group})


# Statuses that mean "I'd want this job": what the alert threshold should catch.
WANTED = ("shortlisted", "resume_ready", "applied", "interview", "offer")
MIN_WANTED = 5


def suggest_alert_above(scores: dict[str, list[int]], current: int | None) -> str | None:
    """Suggest `scoring.alert_above` from the Match Scores of jobs you went for."""
    wanted = sorted(s for status in WANTED for s in scores.get(status, []))
    if len(wanted) < MIN_WANTED:
        return (
            f"Mark at least {MIN_WANTED} jobs Shortlisted or Applied in Notion to get an alert "
            f"threshold suggestion ({len(wanted)} so far)."
        )
    floor = wanted[int(0.2 * (len(wanted) - 1))]  # 4 in 5 of the jobs you went for score above
    suggested = max(50, min(95, floor // 5 * 5))
    skipped = scores.get("skipped", [])
    noise = sum(1 for s in skipped if s >= suggested)
    line = (
        f"You went for {len(wanted)} scored jobs, scoring {wanted[0]} to {wanted[-1]} "
        f"(4 in 5 at {floor} or more). Suggested scoring.alert_above: {suggested}"
    )
    line += f" (now {current})." if current is not None else "."
    if noise:
        line += f" {noise} job(s) you skipped also score that high."
    return line


def build_report(
    repo: Repo, days: int = 7, clock: Clock = utcnow, alert_above: int | None = None
) -> Report:
    since = to_iso(clock() - timedelta(days=days))
    report = Report(days=days)
    report.statuses = repo.status_counts()
    report.moved = repo.status_changes_since(since)
    report.alerts = repo.alerts_sent_since(since)
    report.suggestion = suggest_alert_above(repo.scores_by_status(), alert_above)
    report.messages, report.no_link = repo.message_counts(since)
    report.per_chat = repo.per_chat(since)
    report.tasks = repo.task_counts()
    report.failures = repo.recent_failures()

    groups: dict[str, list[JobLine]] = defaultdict(list)
    for job_id, url, status, sightings, first_post, first_text in repo.jobs_since(since):
        title = title_from_post(first_text) or ""
        line = JobLine(job_id, url or "", title, sightings, first_post)
        if status == JobStatus.HIDDEN:
            report.hidden_ads.append(line)
            continue
        report.jobs += 1
        report.sightings += sightings
        if key := title_key(title):
            groups[key].append(line)
    report.duplicate_groups = sorted(
        # Same title from different posts; one post linking to several drives doesn't count.
        (g for g in groups.values() if _posts(g) > 1),
        key=len,
        reverse=True,
    )
    return report


def render(report: Report, max_groups: int = 10) -> str:
    out = [f"Last {report.days} days", ""]
    out.append(f"  Posts received        {report.messages:>6}")
    out.append(
        f"  …with no job link     {report.no_link:>6}   (text-only / images: handled in v0.2)"
    )
    out.append(f"  Jobs (Notion rows)    {report.jobs:>6}")
    out.append(f"  Reposts merged by URL {report.merged:>6}")
    out.append(f"  Ad/promo links hidden {len(report.hidden_ads):>6}")
    rate = report.duplicate_rate
    verdict = "under the 5% target" if rate < 0.05 else "over the 5% target"
    out.append(f"  Likely duplicate rows {report.extra_rows:>6}   {rate:.1%} of jobs, {verdict}")

    if report.per_chat:
        out += ["", "By chat (posts → jobs)"]
        width = min(max(len(name) for name, _, _ in report.per_chat), 40)
        for name, posts, jobs in report.per_chat:
            out.append(f"  {name[:40]:<{width}}  {posts:>5} → {jobs:<5}")

    if report.duplicate_groups:
        out += ["", "Likely duplicates (same title, different link): check these in Notion"]
        for group in report.duplicate_groups[:max_groups]:
            out.append(f"  • {group[0].title[:80]}")
            for job in group:
                out.append(f"      {job.url[:100]}")
        if len(report.duplicate_groups) > max_groups:
            out.append(f"  … and {len(report.duplicate_groups) - max_groups} more groups")

    if report.hidden_ads:
        out += ["", "Hidden as ads (link attached to posts about different jobs)"]
        for ad in sorted(report.hidden_ads, key=lambda a: -a.sightings)[:max_groups]:
            out.append(f"  {ad.sightings:>4} posts  {ad.url[:90]}")

    s = report.statuses
    out += ["", "Your applications (all time; set in Notion)"]
    funnel = [("shortlisted", "Shortlisted"), ("applied", "Applied"), ("interview", "Interview"),
              ("offer", "Offer"), ("rejected", "Rejected"), ("skipped", "Skipped")]  # fmt: skip
    for key, label in funnel:
        moved = report.moved.get(key, 0)
        recent = f"   +{moved} in the last {report.days} days" if moved else ""
        out.append(f"  {label:<21} {s.get(key, 0):>6}{recent}")
    applied = sum(s.get(k, 0) for k in ("applied", "interview", "offer", "rejected"))
    replies = s.get("interview", 0) + s.get("offer", 0)
    if applied:
        out.append(
            f"  Reply rate            {replies / applied:>6.0%}   interviews + offers / applied"
        )
    out.append(f"  Alerts sent           {report.alerts:>6}   in the last {report.days} days")
    if report.suggestion:
        out += ["", "Alert threshold", f"  {report.suggestion}"]

    pending, failed = (
        report.tasks.get(TaskStatus.PENDING, 0),
        report.tasks.get(TaskStatus.FAILED, 0),
    )
    out += ["", f"Queue: {pending} waiting, {failed} failed"]
    for type_, key, error in report.failures:
        out.append(f"  ✗ {type_} {key[:12]}: {error[:120]}")
    if failed:
        out.append("  Re-run them with: jobradar retry --failed")
    return "\n".join(out)
