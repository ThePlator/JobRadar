# ruff: noqa: E501  (inline-styled email HTML reads better one element per line)
"""Shared look of JobRadar's emails: alerts, digests and the test email.

Email clients ignore stylesheets and most modern CSS, so this is the conservative dialect:
tables for layout, inline styles, system fonts, explicit colours on every element (so
Gmail's dark mode has something sensible to invert), and a plain-text twin of everything.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from jobradar.db.models import Job
from jobradar.pipeline.schemas import JobPosting

# Unquoted on purpose: styles sit inside single-quoted attributes.
FONT = "-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica Neue,Arial,sans-serif"
INK = "#1f2328"
MUTED = "#59636e"
LINE = "#e3e6ea"
PAGE = "#f1f3f5"
CARD = "#ffffff"
ACCENT = "#1a56db"
GOOD = ("#e6f4ea", "#137333")  # background, text
FAIR = ("#fef3e0", "#9a5b00")
POOR = ("#f1f3f4", "#5f6368")
BAD = ("#fce8e6", "#b3261e")

_FIT_WORDS = ("Not a fit", "Weak fit", "Partial fit", "Good fit", "Strong fit", "skills only")


def esc(text: str | None) -> str:
    return html.escape(text or "", quote=True)


def posting_of(job: Job) -> JobPosting | None:
    try:
        return JobPosting.model_validate_json(job.data_json) if job.data_json else None
    except ValueError:
        return None


def day_month(iso_date: str | None) -> str:
    try:
        day = datetime.fromisoformat(iso_date or "")
    except ValueError:
        return ""
    return f"{day.day} {day:%b}"


OLD_POSTING_DAYS = 30  # the default of notify.old_posting_days


def posting_age(posting: JobPosting | None, today: date | None = None) -> str:
    """'Posted 2 Sep' (the company's own date), 'Old posting · 2 Sep' past 30 days, or ''."""
    if posting is None or posting.published_on is None:
        return ""
    when = day_month(posting.published_on.isoformat())
    today = today or date.today()
    if posting.published_on < today - timedelta(days=OLD_POSTING_DAYS):
        return f"Old posting · {when}"
    return f"Posted {when}"


def score_colors(score: int | None) -> tuple[str, str]:
    if score is None:
        return POOR
    return GOOD if score >= 75 else FAIR if score >= 50 else POOR


@dataclass
class Reason:
    """The matcher's reason line, taken apart for display."""

    fit: str | None = None
    eligibility: str | None = None  # "Eligible", "Not eligible: …", "Eligibility unclear"
    eligible: bool | None = None
    have: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    place: str | None = None
    place_ok: bool | None = None
    unsure: bool = False


def split_reason(reason: str | None, posting: JobPosting | None) -> Reason:
    """'Good fit · 86 · eligible ✓ · Python, SQL ✓ · missing Kafka · Pune ✓' → parts."""
    parts = Reason()
    places = {p.casefold() for p in (posting.locations if posting else [])}
    if posting and posting.work_mode != "unknown":
        places.add(posting.work_mode)
    ticks: list[str] = []
    for seg in (reason or "").split(" · "):
        seg = seg.strip()
        low = seg.casefold()
        if not seg or seg.isdigit():
            continue
        if low == "unsure":
            parts.unsure = True
        elif seg in _FIT_WORDS:
            parts.fit = seg
        elif low.startswith("eligible"):
            parts.eligibility, parts.eligible = "Eligible", True
        elif low.startswith("not eligible"):
            parts.eligibility, parts.eligible = "Not eligible" + seg[len("not eligible") :], False
        elif low.startswith("eligibility unclear"):
            parts.eligibility = "Eligibility unclear"
        elif low.startswith("missing "):
            parts.missing = [s.strip() for s in seg[len("missing ") :].split(",") if s.strip()]
        elif low == "location ✗":
            parts.place_ok = False
        elif seg.endswith(" ✓"):
            ticks.append(seg[:-2].strip())
    for tick in ticks:
        if tick.casefold() in places and parts.place is None:
            parts.place, parts.place_ok = tick, True
        else:
            parts.have = [s.strip() for s in tick.split(",") if s.strip()]
    return parts


# ---- building blocks ------------------------------------------------------------------------


def chip(text: str, colors: tuple[str, str]) -> str:
    bg, fg = colors
    return (
        f"<span style='display:inline-block;background:{bg};color:{fg};font-size:12px;"
        f"font-weight:600;line-height:18px;padding:3px 9px;border-radius:999px;"
        f"margin:0 6px 6px 0;white-space:nowrap'>{esc(text)}</span>"
    )


def button(label: str, url: str, primary: bool = True) -> str:
    style = (
        f"background:{ACCENT};color:#ffffff;border:1px solid {ACCENT}"
        if primary
        else f"background:{CARD};color:{ACCENT};border:1px solid #c6d4f5"
    )
    return (
        f"<a href='{esc(url)}' style='display:inline-block;{style};font-size:14px;"
        f"font-weight:600;text-decoration:none;padding:9px 18px;border-radius:8px;"
        f"margin:0 8px 0 0'>{esc(label)}</a>"
    )


def heading(text: str) -> str:
    return (
        f"<tr><td style='padding:22px 0 10px;font-size:13px;font-weight:700;letter-spacing:.06em;"
        f"text-transform:uppercase;color:{MUTED}'>{esc(text)}</td></tr>"
    )


def job_card(
    job: Job, seen: str = "", notion: str | None = None, compact: bool = False
) -> tuple[str, str]:
    """(html, text) for one job."""
    posting = posting_of(job)
    reason = split_reason(job.score_reason, posting)
    apply = (posting.apply_url if posting else None) or job.canonical_url
    role = job.role or "Role not stated"
    where = [
        job.company or "",
        reason.place or (posting.locations[0] if posting and posting.locations else ""),
    ]
    sub = " · ".join(w for w in where if w)
    bg, fg = score_colors(job.score)
    score = "\u2013" if job.score is None else str(job.score)

    chips = []
    if reason.unsure:
        chips.append(chip("Unsure", POOR))
    if reason.fit:
        fit_colors = (
            GOOD
            if reason.fit in ("Good fit", "Strong fit")
            else FAIR
            if reason.fit == "Partial fit"
            else POOR
        )
        chips.append(chip(reason.fit, fit_colors))
    if reason.eligibility:
        chips.append(
            chip(
                reason.eligibility,
                GOOD if reason.eligible else BAD if reason.eligible is False else POOR,
            )
        )
    if reason.place_ok is False:
        chips.append(chip("Location doesn't fit", BAD))
    if seen:
        chips.append(chip(seen[0].upper() + seen[1:], ("#e8f0fe", ACCENT)))
    if closes := day_month(job.deadline):
        chips.append(chip(f"Closes {closes}", FAIR))
    if age := posting_age(posting):
        chips.append(chip(age, POOR if age.startswith("Old") else ("#eef1f5", MUTED)))

    skill_rows = ""
    if reason.have:
        skill_rows += (
            f"<div style='font-size:13px;color:{INK};margin:4px 0 0'>"
            f"<span style='color:{GOOD[1]};font-weight:600'>You have</span>&nbsp; "
            f"{esc(', '.join(reason.have))}</div>"
        )
    if reason.missing:
        skill_rows += (
            f"<div style='font-size:13px;color:{INK};margin:4px 0 0'>"
            f"<span style='color:{FAIR[1]};font-weight:600'>Missing</span>&nbsp; "
            f"{esc(', '.join(reason.missing))}</div>"
        )
    links = []
    if apply:
        links.append(button("Apply" if compact else "Apply now", apply))
    if notion:
        links.append(button("Notion" if compact else "Open in Notion", notion, primary=False))
    buttons = f"<div style='margin:14px 0 0'>{''.join(links)}</div>" if links else ""

    tile = 52 if not compact else 44
    card = f"""
<tr><td style='padding:0 0 12px'>
<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='background:{CARD};border:1px solid {LINE};border-radius:12px'>
<tr>
<td width='{tile + 20}' valign='top' style='padding:16px 0 16px 16px'>
<div style='width:{tile}px;height:{tile}px;line-height:{tile}px;border-radius:10px;background:{bg};color:{fg};font-size:{20 if compact else 22}px;font-weight:700;text-align:center'>{score}</div>
</td>
<td valign='top' style='padding:16px 16px 16px 14px'>
<div style='font-size:{16 if compact else 17}px;font-weight:700;color:{INK};line-height:1.3'>{esc(role)}</div>
<div style='font-size:14px;color:{MUTED};margin:2px 0 10px'>{esc(sub)}</div>
<div>{"".join(chips)}</div>
{skill_rows}
{buttons}
</td></tr></table>
</td></tr>"""

    text = [f"{score}  {role}" + (f" — {sub}" if sub else "")]
    meta = [
        m
        for m in (reason.fit, reason.eligibility, seen, f"closes {closes}" if closes else "", age)
        if m
    ]
    if meta:
        text.append("    " + " · ".join(meta))
    if reason.have:
        text.append("    You have: " + ", ".join(reason.have))
    if reason.missing:
        text.append("    Missing: " + ", ".join(reason.missing))
    if apply:
        text.append(f"    Apply: {apply}")
    if notion:
        text.append(f"    Notion: {notion}")
    return card, "\n".join(text)


def stats_row(items: list[tuple[str, str]]) -> str:
    cells = "".join(
        f"<td width='{100 // len(items)}%' style='padding:0 4px'>"
        f"<div style='background:{CARD};border:1px solid {LINE};border-radius:10px;padding:12px 8px;text-align:center'>"
        f"<div style='font-size:22px;font-weight:700;color:{INK}'>{esc(value)}</div>"
        f"<div style='font-size:12px;color:{MUTED};margin-top:2px'>{esc(label)}</div></div></td>"
        for value, label in items
    )
    return (
        "<tr><td style='padding:4px 0 0'><table role='presentation' width='100%' cellpadding='0' "
        f"cellspacing='0' style='margin:0 -4px'><tr>{cells}</tr></table></td></tr>"
    )


def layout(eyebrow: str, title: str, subtitle: str, preheader: str, rows: str, footer: str) -> str:
    """The page every email sits in: grey ground, a 600px column, header and footer."""
    return f"""<!doctype html>
<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<meta name='color-scheme' content='light'><meta name='supported-color-schemes' content='light'></head>
<body style='margin:0;padding:0;background:{PAGE}'>
<div style='display:none;max-height:0;overflow:hidden;opacity:0'>{esc(preheader)}</div>
<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='background:{PAGE}'>
<tr><td align='center' style='padding:24px 12px'>
<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='max-width:600px;font-family:{FONT};color:{INK}'>
<tr><td style='padding:0 0 4px;font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:{ACCENT}'>{esc(eyebrow)}</td></tr>
<tr><td style='padding:0 0 4px;font-size:24px;font-weight:700;color:{INK};line-height:1.25'>{esc(title)}</td></tr>
<tr><td style='padding:0 0 18px;font-size:14px;color:{MUTED}'>{esc(subtitle)}</td></tr>
{rows}
<tr><td style='padding:22px 0 0;border-top:1px solid {LINE};font-size:12px;color:{MUTED};line-height:1.6'>{footer}</td></tr>
</table></td></tr></table></body></html>"""
