"""Task type names, in pipeline order."""

PROCESS_MESSAGE = "process_message"  # post -> links -> jobs
FETCH = "fetch"  # job link -> page text
EXTRACT = "extract"  # page + post -> JobPosting (page data, post labels, LLM fallback)
MATCH = "match"  # JobPosting + profile -> Match Score, reason, hide / alert decision
SEND_ALERTS = "send_alerts"  # one email with every alert queued in the batch window
DIGEST = "digest"  # summary email at each notify.digest_times slot (key: local slot time)
NOTION_UPSERT = "notion_upsert"  # job -> page properties
NOTION_BODY = "notion_body"  # job -> page body (agent toggle)
