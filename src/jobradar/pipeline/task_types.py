"""Task type names, in pipeline order."""

PROCESS_MESSAGE = "process_message"  # post -> links -> jobs
FETCH = "fetch"  # job link -> page text
EXTRACT = "extract"  # page + post -> JobPosting (LLM)
NOTION_UPSERT = "notion_upsert"  # job -> page properties
NOTION_BODY = "notion_body"  # job -> page body (agent toggle)
