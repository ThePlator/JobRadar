"""extract -> match: answers about job vs. profile, turned into a Match Score and a reason.

0. A posting on Greenhouse, Lever, Ashby or SmartRecruiters that the site says has closed is
   hidden right away (`pipeline/liveness.py`).
1. Hard rules in code (batch years, maximum experience): a job that fails them needs no
   provider call.
2. Otherwise providers are tried in `matching.providers` order; the first that answers wins.
3. Code combines the answers with `matching.weights` into a 0-100 score and writes a reason
   line. Answers are stored per profile version, so new weights re-score without new calls,
   and editing profile.yaml re-matches open jobs.
"""

from __future__ import annotations

import logging
from datetime import timedelta, tzinfo
from typing import Any, Protocol

from jobradar.clock import Clock, from_iso, utcnow
from jobradar.config import FiltersConfig, MatchingConfig, ScoringConfig
from jobradar.db.models import Job, JobStatus
from jobradar.db.repo import Repo, TaskRecord
from jobradar.pipeline import scam
from jobradar.pipeline.decide import Decision, contradicts, decide
from jobradar.pipeline.extract import PAGE_TEXT_LIMIT, own_post
from jobradar.pipeline.liveness import Liveness, LivenessChecker
from jobradar.pipeline.schemas import JobPosting
from jobradar.pipeline.task_types import MATCH, NOTION_BODY, NOTION_UPSERT
from jobradar.providers.base import JobView, MatchAnswers, MatchProvider, ProviderUnavailable
from jobradar.providers.skills_rule import compare_skills
from jobradar.queue.tasks import TaskQueue

log = logging.getLogger(__name__)

_OPEN = (JobStatus.EXTRACTED.value, JobStatus.NEW.value)
CLOSED_REASON = "posting closed on the hiring site"
_FIT_WORDS = ["Not a fit", "Weak fit", "Partial fit", "Good fit", "Strong fit"]


def hard_rules(posting: JobPosting, filters: FiltersConfig) -> MatchAnswers | None:
    """Not eligible by the job's own stated batch years or experience, or None."""
    wanted = set(filters.batch_years)
    if posting.batch_years and wanted and not wanted & set(posting.batch_years):
        years = sorted(posting.batch_years)
        span = str(years[0]) if len(years) == 1 else f"{years[0]}-{years[-1]}"
        because = f"batch {span} only"
    elif (
        filters.max_experience_years is not None
        and posting.experience_min is not None
        and posting.experience_min > filters.max_experience_years
    ):
        because = f"needs {posting.experience_min:g}+ years"
    else:
        return None
    return MatchAnswers(
        provider="rules", eligible=0.0, not_eligible_because=because, confidence=1.0
    )


def score(answers: MatchAnswers, weights: dict[str, float]) -> int | None:
    """100 x weighted mean of the answered dimensions; None when none was answered."""
    parts = [(w, getattr(answers, name)) for name, w in weights.items()]
    parts = [(w, v) for w, v in parts if v is not None and w > 0]
    total = sum(w for w, _ in parts)
    return round(100 * sum(w * v for w, v in parts) / total) if total else None


def reason(
    answers: MatchAnswers,
    value: int | None,
    posting: JobPosting,
    profile: dict[str, Any],
    min_confidence: float,
) -> str:
    """One line for Notion, built by code: 'Good fit · 82 · eligible ✓ · Python, SQL ✓ · …'."""
    if answers.provider == "rules":
        return f"Not eligible: {answers.not_eligible_because}"
    bits: list[str] = []
    if (answers.provider == "llm" and answers.confidence < min_confidence) or contradicts(
        answers, value
    ):
        bits.append("unsure")
    if answers.overall_fit is not None:
        bits.append(_FIT_WORDS[round(answers.overall_fit * 4)])
    elif answers.provider == "skills_rule":
        bits.append("skills only")
    if value is not None:
        bits.append(str(value))
    if answers.eligible is not None:
        if answers.eligible >= 0.7:
            bits.append("eligible ✓")
        elif answers.eligible <= 0.3:
            why = answers.not_eligible_because
            bits.append(f"not eligible: {why}" if why else "not eligible")
        else:
            bits.append("eligibility unclear")
    if posting.skills_required:
        have, missing = compare_skills(posting.skills_required, profile)
        if have:
            bits.append(", ".join(have[:4]) + " ✓")
        if missing:
            bits.append("missing " + ", ".join(missing[:3]))
    if answers.location_ok is not None and answers.location_ok <= 0.3:
        bits.append("location ✗")
    elif answers.location_ok is not None and answers.location_ok >= 0.7:
        place = posting.locations[0] if posting.locations else posting.work_mode
        if place and place != "unknown":
            bits.append(f"{place} ✓")
    return " · ".join(bits)


class AlertQueue(Protocol):
    def queue_alert(self, job_id: str) -> None: ...


class Matcher:
    def __init__(
        self,
        repo: Repo,
        queue: TaskQueue,
        providers: list[MatchProvider],
        profile: dict[str, Any],
        version: str,
        matching: MatchingConfig,
        filters: FiltersConfig,
        scoring: ScoringConfig | None = None,
        alerts: AlertQueue | None = None,
        alert_max_age_days: float = 2,
        tz: tzinfo | None = None,
        clock: Clock = utcnow,
        liveness: LivenessChecker | None = None,
    ) -> None:
        self.repo = repo
        self.queue = queue
        self.providers = providers
        self.profile = profile
        self.version = version
        self.matching = matching
        self.filters = filters
        self.scoring = scoring or ScoringConfig()
        self.alerts = alerts  # None: alerts off
        self.alert_max_age_days = alert_max_age_days
        self.tz = tz
        self.clock = clock
        self.liveness = liveness  # None: don't ask hiring sites whether postings are open

    async def match(self, task: TaskRecord) -> None:
        job = self.repo.get_job(task.key)
        if job is None or job.status not in _OPEN or not job.data_json:
            return
        posting = JobPosting.model_validate_json(job.data_json)
        # Only a job's first match may alert: re-matching after a profile or prompt change
        # must not email you about jobs you've already seen.
        first = not self.repo.ever_matched(job.id)
        if first and await self._closed(job, posting):
            return
        answers = hard_rules(posting, self.filters) or await self._ask(job.id, posting)
        if answers is None:
            log.info("no match provider could answer", extra={"job_id": job.id[:10]})
            self.repo.set_match_score(job.id, None, "not matched")
            return
        self.repo.save_match(job.id, self.version, answers.provider, answers.model_dump_json())
        self._apply(job.id, posting, answers, alert_ok=first)

    async def _closed(self, job: Job, posting: JobPosting) -> bool:
        """Ask the hiring site whether the posting is still open (Greenhouse, Lever, Ashby,
        SmartRecruiters links only). A closed one is hidden before any provider call."""
        if self.liveness is None or job.user_status_at is not None:
            return False
        if any(s.platform == "board" for s in self.repo.job_sightings(job.id)):
            return False  # just read off the company's board: open
        for url in dict.fromkeys(u for u in (posting.apply_url, job.canonical_url) if u):
            state = await self.liveness.check(url)
            if state == Liveness.UNKNOWN:
                continue
            if state == Liveness.OPEN:
                return False
            self.repo.set_job_status(job.id, JobStatus.HIDDEN, CLOSED_REASON)
            log.info("job hidden", extra={"job_id": job.id[:10], "why": CLOSED_REASON})
            self.queue.enqueue(NOTION_UPSERT, job.id)
            return True
        return False

    def _texts(self, job_id: str) -> tuple[str, str]:
        page = self.repo.get_page(job_id)
        page_text = ((page.text or "") if page else "")[:PAGE_TEXT_LIMIT]
        return own_post(self.repo, job_id) or "", page_text

    async def _ask(self, job_id: str, posting: JobPosting) -> MatchAnswers | None:
        post_text, page_text = self._texts(job_id)
        view = JobView(posting=posting, post_text=post_text, page_text=page_text)
        for provider in self.providers:
            try:
                return await provider.match(view, self.profile)
            except ProviderUnavailable as e:
                log.info(
                    "match provider skipped",
                    extra={"job_id": job_id[:10], "provider": provider.name, "why": str(e)[:200]},
                )
        return None

    def _apply(
        self, job_id: str, posting: JobPosting, answers: MatchAnswers, alert_ok: bool
    ) -> bool:
        """Score, reason and decision; sync Notion when something changed. True if changed."""
        job = self.repo.get_job(job_id)
        if job is None:
            return False
        risk = scam.check(*self._texts(job_id)).risk
        if risk != job.scam_risk:
            self.repo.set_scam_risk(job_id, risk)
        value = score(answers, self.matching.weights)
        line = reason(answers, value, posting, self.profile, self.matching.min_confidence)
        changed = self.repo.set_match_score(job_id, value, line) or risk != job.scam_risk
        decision = decide(
            answers, value, job.company, risk, self.matching, self.scoring, self.filters
        )
        if not posting.is_job:  # an ad, course or event that got through extraction
            decision = Decision("not_a_job", hide_because="not a job posting (ad or event)")
        if job.user_status_at is not None:
            # You set this job's Status in Notion: keep the score fresh, decide nothing.
            decision = Decision("yours")
        if decision.hide_because:
            self.repo.set_job_status(job_id, JobStatus.HIDDEN, decision.hide_because)
            log.info("job hidden", extra={"job_id": job_id[:10], "why": decision.hide_because})
            self.queue.enqueue(NOTION_UPSERT, job_id)
            return True
        if changed:
            log.info(
                "job matched",
                extra={"job_id": job_id[:10], "score": value, "provider": answers.provider,
                       "decision": decision.rule},
            )  # fmt: skip
            self.queue.enqueue(NOTION_UPSERT, job_id)
            self.queue.enqueue(NOTION_BODY, job_id)
        if decision.alert and alert_ok and self.alerts is not None:
            if why_not := self._stale(job, posting):
                log.info("alert skipped", extra={"job_id": job_id[:10], "why": why_not})
            else:
                self.alerts.queue_alert(job_id)
        return changed

    def _stale(self, job: Job, posting: JobPosting) -> str | None:
        """Why this job is too old to email about, or None."""
        now = self.clock()
        if from_iso(job.created_at) < now - timedelta(days=self.alert_max_age_days):
            return f"first seen more than {self.alert_max_age_days:g} days ago"
        today = now.astimezone(self.tz).date() if self.tz else now.date()
        if posting.deadline and posting.deadline < today:
            return f"deadline {posting.deadline.isoformat()} has passed"
        return None

    def catch_up(self) -> tuple[int, int]:
        """At startup: re-score and re-decide stored answers (weights or thresholds may have
        changed; this never sends alerts, so old jobs can't flood your inbox) and queue jobs
        not yet matched against this profile version. Returns (changed, queued)."""
        changed = 0
        for job_id, answers_json in self.repo.match_answers(self.version):
            job = self.repo.get_job(job_id)
            if job is None or not job.data_json:
                continue
            posting = JobPosting.model_validate_json(job.data_json)
            answers = MatchAnswers.model_validate_json(answers_json)
            changed += self._apply(job_id, posting, answers, alert_ok=False)
        queued = self.repo.jobs_to_match(self.version)
        for job_id in queued:
            self.queue.enqueue(MATCH, job_id)
        return changed, len(queued)
