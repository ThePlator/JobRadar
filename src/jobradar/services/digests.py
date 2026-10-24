"""The digest on the page: the windows, and what each digest said or will say."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from jobradar.pipeline.skill_gaps import Gap, skill_gaps
from jobradar.services.cards import Card, card
from jobradar.services.context import Ctx
from jobradar.sinks.digest import gather, part_of_day, pipeline_line, previous_slot

# ---- the digest -------------------------------------------------------------------------------

DIGEST_WINDOWS = 8  # the current window and the digests before it


@dataclass(frozen=True)
class Window:
    key: str  # "now", or the digest time: "2026-10-19-1900"
    label: str  # "Now", "Evening · 19 Oct"
    since: datetime
    until: datetime
    current: bool


def windows(now: datetime, times: list[str]) -> list[Window]:
    """The window the next digest will cover, then each earlier digest's, newest first.
    `now` is in your timezone."""
    out = [Window("now", "So far", previous_slot(now - timedelta(minutes=1), times), now, True)]
    until = out[0].since
    for _ in range(DIGEST_WINDOWS - 1):
        since = previous_slot(until - timedelta(minutes=1), times)
        label = f"{part_of_day(until)} · {until.day} {until:%b}"
        out.append(Window(f"{until:%Y-%m-%d-%H%M}", label, since, until, False))
        until = since
    return out


@dataclass(frozen=True)
class Board:
    card: Card
    board: str


@dataclass
class DigestPage:
    window: Window
    windows: list[Window]
    title: str  # "Evening digest"
    span: str  # "since Mon 19 Oct, 09:00" / "19 Oct, 09:00 to 19:00"
    new: int
    strong: int
    alerts: int
    inbox: list[Card]
    hidden: list[tuple[str, int]]
    closing: list[Card]
    boards: list[Board]
    pipeline: str | None
    spent: str
    failed: int
    gaps: list[Gap]
    gap_jobs: int

    @property
    def hidden_total(self) -> int:
        return sum(n for _, n in self.hidden)


def digest(ctx: Ctx, key: str, profile: dict[str, Any] | None) -> DigestPage:
    """A digest on the page: what the next one will say ("now"), or an earlier one again."""
    repo, settings, now = ctx.repo, ctx.settings, ctx.now
    notify = settings.config.notify
    tz = notify.tz
    local = now.astimezone(tz)
    all_windows = windows(local, notify.digest_times)
    # No choice: the latest digest that went out (what's in your inbox), not the window
    # that's only just begun.
    window = next((w for w in all_windows if w.key == key), all_windows[1])
    data = gather(repo, settings, window.since, window.until, bounded=not window.current)
    min_score = settings.config.scoring.alert_above
    gaps: list[Gap] = []
    gap_jobs = 0
    if profile:
        gaps, gap_jobs = skill_gaps(repo, profile, window.until - timedelta(days=7), min_score)
    since = f"{window.since:%a} {window.since.day} {window.since:%b}, {window.since:%H:%M}"
    span = (
        f"since {since}"
        if window.current
        else f"{since} to {window.until:%H:%M}"
        if window.since.date() == window.until.date()
        else f"{since} to {window.until:%a} {window.until.day} {window.until:%b}, "
        f"{window.until:%H:%M}"
    )
    title = "Next digest, so far" if window.current else f"{part_of_day(window.until)} digest"
    return DigestPage(
        window=window,
        windows=all_windows,
        title=title,
        span=span,
        new=data.new,
        strong=sum(1 for j in data.inbox if (j.score or 0) >= min_score),
        alerts=data.alerts,
        inbox=[card(j, tz, now) for j in data.inbox[:30]],
        hidden=sorted(data.hidden.items(), key=lambda kv: -kv[1]),
        closing=[card(j, tz, now) for j in data.closing[:10]],
        boards=[Board(card(j, tz, now), b) for j, b in data.boards[:10]],
        pipeline=pipeline_line(data),
        spent=f"₹{data.spent_inr:.0f} of ₹{data.budget_inr:.0f}",
        failed=data.failed,
        gaps=gaps,
        gap_jobs=gap_jobs,
    )
