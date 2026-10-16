"""Task type names, in pipeline order, and task priorities."""

from datetime import datetime, timedelta

from jobradar.clock import from_iso

PROCESS_MESSAGE = "process_message"  # post -> links -> jobs
FETCH = "fetch"  # job link -> page text
EXTRACT = "extract"  # page + post -> JobPosting (page data, post labels, LLM fallback)
MATCH = "match"  # JobPosting + profile -> Match Score, reason, hide / alert decision
SEND_ALERTS = "send_alerts"  # one email with every alert queued in the batch window
NOTION_POLL = "notion_poll"  # every 2 min: copy the Status you set in Notion
BOARDS_TICK = "boards_tick"  # every minute: queue a board_check for each board that is due
BOARD_CHECK = "board_check"  # read one company job board (key: "greenhouse:stripe")
PAGES_TICK = "pages_tick"  # every 5 minutes: queue a page_check for each page that is due
PAGE_CHECK = "page_check"  # read one watched page (key: its URL)
CLOSING_CHECK = "closing_check"  # hourly: remind you of shortlisted jobs that close soon
DIGEST = "digest"  # summary email at each notify.digest_times slot (key: local slot time)
BUILD_RESUME = (
    "build_resume"  # job -> tailored one-page PDF (payload why: auto | shortlisted | manual)
)
NOTION_UPSERT = "notion_upsert"  # job -> page properties
NOTION_BODY = "notion_body"  # job -> page body (agent toggle)

# Priorities: work for a job that just arrived runs before catch-ups and backfills, so an
# alert isn't stuck behind re-matching 100 old jobs.
FRESH = 1
FRESH_FOR = timedelta(hours=1)


def priority(created_at: str, now: datetime) -> int:
    """FRESH for a job first seen within the last hour, else 0."""
    return FRESH if now - from_iso(created_at) < FRESH_FOR else 0
