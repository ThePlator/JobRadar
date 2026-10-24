"""Skills: what your good matches ask for, and what exactly they want of one skill."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from markupsafe import Markup, escape

from jobradar.db.repo import Repo
from jobradar.pipeline.skill_detail import Detail as Needs
from jobradar.pipeline.skill_detail import detail, spelling
from jobradar.pipeline.skill_gaps import Demand, Gap, demand, skill_gaps
from jobradar.profile import skill_names, skill_warnings
from jobradar.providers.skills_rule import skill_key
from jobradar.services.cards import Card, card
from jobradar.services.context import Ctx

# ---- Skills ------------------------------------------------------------------------------------

SKILL_WINDOWS = [(7, "7 days"), (30, "30 days"), (90, "90 days")]
MAX_DOTS = 24  # per skill row; the rest show as "+n"


@dataclass(frozen=True)
class SkillRow:
    key: str
    name: str
    jobs: int  # for a skill you're missing: the jobs that need it (not "Java or C++")
    share: int  # % of the good matches looked at
    width: int  # % of the top skill, for the bar
    trend: int | None  # jobs asking for it vs the window before; None: nothing to compare
    samples: list[Card]  # the best few jobs that asked for it
    covered: int = 0  # jobs that named it but take a skill you have instead
    covered_by: str = ""  # those skills: "Java or Python"
    dots: list[str] = field(default_factory=list)  # one per job, how it asks (strictest first)
    more: int = 0  # jobs beyond the dots shown


@dataclass
class SkillsPage:
    days: int
    looked_at: int
    min_score: int
    missing: list[SkillRow]
    have: list[SkillRow]
    warnings: list[str]
    verdict: str = ""  # about the first missing skill: what its jobs really want
    counts: Counter[str] = field(default_factory=Counter)  # the first missing skill's kinds


def _skill_name(key: str, d: Demand, names: dict[str, str]) -> str:
    """Your spelling, else the usual one ("scikit-learn" stays lowercase), else capitalized."""
    if (yours := names.get(key)) and not yours.islower():
        return yours
    if (usual := d.name(key)) != key:
        return usual
    plain = yours or key
    return plain[:1].upper() + plain[1:]


def _either(words: list[str], joiner: str = "or") -> str:
    """'Java', 'Java or Python', 'Azure, GCP or IBM Cloud'."""
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} {joiner} {words[-1]}"


def _rows(
    counts: Counter[str],
    earlier: Counter[str] | None,
    d: Demand,
    repo: Repo,
    tz: ZoneInfo,
    now: datetime,
    top: int,
    names: dict[str, str],
    details: dict[str, Needs] | None = None,
) -> list[SkillRow]:
    def order(kv: tuple[str, int]) -> tuple[int, str]:
        return -kv[1], _skill_name(kv[0], d, names).casefold()

    ranked = sorted(counts.items(), key=order)[:top]
    peak = max([1, *(n for _, n in ranked)])
    rows = []
    for key, n in ranked:
        samples = [card(j, tz, now) for i in d.jobs[key][:4] if (j := repo.get_job(i))]
        trend = None if earlier is None else n - earlier.get(key, 0)
        det = (details or {}).get(key)
        by = [_skill_name(k, d, names) for k in det.covered_by[:3]] if det else []
        open_ = [x.kind for x in det.needs if x.kind != "covered"] if det else ["have"] * n
        kinds: list[str] = sorted(open_, key=lambda k: ORDER_KINDS.index(k) if det else 0)
        rows.append(SkillRow(key, _skill_name(key, d, names), n,
                             round(100 * n / max(d.looked_at, 1)), round(100 * n / peak), trend,
                             samples, det.counts["covered"] if det else 0,
                             _either(by) if by else "", kinds[:MAX_DOTS],
                             max(0, len(kinds) - MAX_DOTS)))  # fmt: skip
    return rows


def _yours(profile: dict[str, Any]) -> dict[str, str]:
    """How you wrote each skill in profile.yaml ("Next.js"), by key."""
    yours: dict[str, str] = {}
    for full in skill_names(profile):
        plain = full.split(" (")[0]
        for part in [plain, *re.split(r"/|\s&\s", plain)]:  # "Docker & Docker Compose": Docker
            yours.setdefault(skill_key(part), part.strip())
    return yours


def _details(d: Demand) -> dict[str, Needs]:
    return {k: detail(k, d.jobs[k], d.texts, d.have_keys) for k in d.missing}


def _needed(details: dict[str, Needs]) -> Counter[str]:
    return Counter({k: x.needed for k, x in details.items() if x.needed})


def skills_page(ctx: Ctx, profile: dict[str, Any] | None, days: int) -> SkillsPage:
    """The skills your good matches ask for: the ones your profile lacks (what to learn, or
    to add if you know them) and the ones it has (what to put first on a resume). A missing
    skill counts only the jobs that need it: "Python, Java or C++" doesn't count for C++."""
    repo, settings, now = ctx.repo, ctx.settings, ctx.now
    days = days if days in dict(SKILL_WINDOWS) else 30
    min_score = settings.config.scoring.alert_above
    tz = settings.config.notify.tz
    if not profile:
        return SkillsPage(days, 0, min_score, [], [], ["profile.yaml couldn't be read"])
    window = timedelta(days=days)
    d = demand(repo, profile, now - window, None, min_score)
    before = demand(repo, profile, now - 2 * window, now - window, min_score)
    yours = _yours(profile)
    details = _details(d)
    gone, had = (None, None) if not before.looked_at else (_needed(_details(before)), before.have)
    missing = _rows(_needed(details), gone, d, repo, tz, now, 20, yours, details)
    first = details[missing[0].key] if missing else None
    return SkillsPage(
        days=days,
        looked_at=d.looked_at,
        min_score=min_score,
        missing=missing,
        have=_rows(d.have, had, d, repo, tz, now, 12, yours),
        warnings=skill_warnings(profile),
        verdict=_verdict(missing[0].name, first, lambda k: _skill_name(k, d, yours))
        if first
        else "",
        counts=first.counts if first else Counter(),
    )


# ---- One skill: what exactly they want --------------------------------------------------------

KIND_WORDS = {
    "must": "Must have", "listed": "In its skill list", "either": "One of",
    "nice": "Nice to have", "covered": "You're covered",
}  # fmt: skip
ORDER_KINDS = ["must", "listed", "either", "nice", "covered"]  # strictest first
# How the quotes are grouped: "In its skill list" reads as a must have.
GROUP_OF = {"must": "must", "listed": "must", "either": "either", "nice": "nice",
            "covered": "covered"}  # fmt: skip
GROUP_WORDS = [("must", "Must have"), ("either", "Any one of a few"), ("nice", "Nice to have"),
               ("covered", "Covered by your skills")]  # fmt: skip
QUOTE_CHARS = 240


@dataclass(frozen=True)
class TopicRow:
    label: str
    jobs: int
    width: int
    cards: list[Card]
    kinds: list[str] = field(default_factory=list)  # each job's verdict, strictest first


@dataclass(frozen=True)
class Tile:
    """One job in the roll call: how it asks for the skill."""

    kind: str
    job_id: str
    title: str  # "Rubrik: must have", for the tooltip


@dataclass(frozen=True)
class QuoteGroup:
    kind: str
    title: str
    quotes: list[Quote]


@dataclass(frozen=True)
class Quote:
    card: Card
    kind: str
    label: str
    html: Markup
    note: str  # "or Azure, Google Cloud" / "you have Java"


@dataclass
class SkillDetailPage:
    key: str
    name: str
    days: int
    looked_at: int
    asked: int  # jobs that named it
    needed: int
    counts: Counter[str]
    verdict: str
    start: str  # what to learn first, from the topics
    topics: list[TopicRow]
    alongside: list[tuple[str, int, bool]]  # (name, jobs, you have it)
    quotes: list[Quote]
    missing: bool  # not in your profile (so "I know this" applies)
    tiles: list[Tile] = field(default_factory=list)
    groups: list[QuoteGroup] = field(default_factory=list)
    legend: list[tuple[str, str, int]] = field(default_factory=list)  # (kind, words, jobs)
    mood: str = ""  # Blip's: "happy" when your skills already cover most of these jobs


def _quote(text: str, pattern: re.Pattern[str]) -> Markup:
    """The sentence, cut to about QUOTE_CHARS around the skill, with the skill marked."""
    if len(text) > QUOTE_CHARS:
        m = pattern.search(text)
        a = max(0, (m.start() if m else 0) - QUOTE_CHARS // 3)
        b = min(len(text), a + QUOTE_CHARS)
        text = ("… " if a else "") + text[a:b].strip() + (" …" if b < len(text) else "")
    parts: list[Markup] = []
    last = 0
    for m in pattern.finditer(text):
        parts += [escape(text[last : m.start()]), Markup("<mark>%s</mark>") % m.group(0)]
        last = m.end()
    parts.append(escape(text[last:]))
    return Markup("").join(parts)


def _verdict(nm: str, det: Needs, names: Callable[[str], str]) -> str:
    c, total = det.counts, len(det.needs)
    if c["covered"] and c["covered"] * 2 >= total:
        by = _either([names(k) for k in det.covered_by[:3]])
        if not det.needed:
            if total == 1:
                return f"The one job naming {nm} takes {by} instead, which you have."
            return f"None of these {total} jobs needs {nm}: each takes {by}, which you have."
        return (f"{c['covered']} of {total} jobs take {by} instead, which you already have. "
                f"Only {det.needed} really need{'s' if det.needed == 1 else ''} {nm}.")  # fmt: skip
    if c["either"] * 2 >= total:
        alts = Counter(a for n in det.needs if n.kind == "either" for a in n.alternatives)
        choices = _either([nm, *(names(k) for k, _ in alts.most_common(3))])
        return (f"{c['either']} of {total} jobs take any one of {choices}. Get good at one "
                "and these are covered; the rest ask for it outright or as a plus.")  # fmt: skip
    outright = c["must"] + c["listed"]
    more = f", and {c['nice']} more call it a plus" if c["nice"] else ""
    return f"{outright} of {total} jobs ask for {nm} outright{more}."


def skill_detail_page(
    ctx: Ctx, profile: dict[str, Any] | None, days: int, key: str
) -> SkillDetailPage | None:
    """What your good matches want of one skill; None when none of them named it."""
    repo, settings, now = ctx.repo, ctx.settings, ctx.now
    days = days if days in dict(SKILL_WINDOWS) else 30
    if not profile:
        return None
    tz = settings.config.notify.tz
    d = demand(repo, profile, now - timedelta(days=days), None, settings.config.scoring.alert_above)
    if key not in d.jobs:
        return None
    yours = _yours(profile)

    def names(k: str) -> str:
        return _skill_name(k, d, yours)

    det = detail(key, d.jobs[key], d.texts, d.have_keys)
    cards = {i: card(j, tz, now) for i in d.jobs[key] if (j := repo.get_job(i))}
    peak = max([1, *(len(t.jobs) for t in det.topics)])
    kind_of = {n.job_id: n.kind for n in det.needs}

    def strictest(ids: list[str]) -> list[str]:
        return sorted((kind_of[i] for i in ids), key=ORDER_KINDS.index)

    topics = [
        TopicRow(t.label, len(t.jobs), round(100 * len(t.jobs) / peak),
                 [cards[i] for i in t.jobs[:3] if i in cards], strictest(t.jobs))
        for t in det.topics
    ]  # fmt: skip
    pattern = spelling(key)
    quotes = []
    for n in sorted(det.needs, key=lambda n: ORDER_KINDS.index(n.kind)):
        if n.job_id not in cards:
            continue
        alts = [names(a) for a in n.alternatives]
        note = (_either([names(key), *alts]) if n.kind == "either" and alts else
                f"you have {_either(alts)}" if n.kind == "covered" and alts else "")  # fmt: skip
        html = _quote(n.quote.text, pattern) if n.quote else Markup("")
        quotes.append(Quote(cards[n.job_id], n.kind, KIND_WORDS[n.kind], html, note))
    often = [f"{t.label} ({len(t.jobs)} jobs)" for t in det.topics[:4] if len(t.jobs) >= 2]
    once = [t.label for t in det.topics if len(t.jobs) == 1][: max(0, 4 - len(often))]
    start = " ".join(
        [f"Named most often with it: {', '.join(often)}." if often else "",
         f"{'Also' if often else 'Named'} once each: {', '.join(once)}." if once else ""]
    ).strip()  # fmt: skip
    ordered = sorted((n for n in det.needs if n.job_id in cards),
                     key=lambda n: ORDER_KINDS.index(n.kind))  # fmt: skip
    tiles = [
        Tile(n.kind, n.job_id,
             f"{cards[n.job_id].company or cards[n.job_id].role}: {KIND_WORDS[n.kind].lower()}")
        for n in ordered
    ]  # fmt: skip
    groups = []
    for kind, title in GROUP_WORDS:
        members = [q for q in quotes if GROUP_OF[q.kind] == kind]
        if members:
            groups.append(QuoteGroup(kind, title, members))
    legend = [(g.kind, g.title, len(g.quotes)) for g in groups]
    return SkillDetailPage(
        tiles=tiles,
        groups=groups,
        legend=legend,
        mood="happy" if det.needed * 2 < len(det.needs) else "",
        key=key,
        name=names(key),
        days=days,
        looked_at=d.looked_at,
        asked=len(det.needs),
        needed=det.needed,
        counts=det.counts,
        verdict=_verdict(names(key), det, names),
        start=start,
        topics=topics,
        alongside=[(names(k), n, k in d.have_keys) for k, n in det.alongside],
        quotes=quotes,
        missing=key not in d.have_keys,
    )


def gaps(ctx: Ctx, profile: dict[str, Any], days: int, top: int = 15) -> tuple[list[Gap], int]:
    """(the skills your good matches of the last `days` days need that your profile doesn't
    list, how many good matches were looked at): `jobradar skills`."""
    return skill_gaps(ctx.repo, profile, ctx.now - timedelta(days=days), ctx.good, top=top)
