"""Oracle Recruiting Cloud (Oracle Fusion "Candidate Experience") career sites.

A site is `host/site`, e.g. `fa-etvl-saasfaprod1.fa.ocs.oraclecloud.com/CX_1` (Zensar), from
a careers link like https://<host>/hcmUI/CandidateExperience/en/sites/<site>/jobs. Its job
pages are JavaScript-only, but two public JSON addresses on the same host serve the data:

- the job list (`recruitingCEJobRequisitions`), searched with a `finder` clause. The paging
  offset must sit INSIDE that clause next to `limit`: Oracle ignores a top-level `offset`
  and silently re-reads page 1 (noted in freehire's oracle.go);
- one job's detail (`recruitingCEJobRequisitionDetails`): title, description, posting
  date. A job that isn't posted any more answers 200 with no items.

Links found in posts are only recognised on `*.oraclecloud.com`, with a numeric job id and a
strict site name, so a crafted link can't point a request anywhere else. A site you add to
config yourself may use the company's own domain (e.g. enterpriseplatform.dell.com).
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

_HOST = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}(?:\.[a-z0-9][a-z0-9-]{0,62})+$")
_CLOUD_HOST = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}(?:\.[a-z0-9-]{1,63})*\.oraclecloud\.com$")
_SITE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_JOB_PATH = re.compile(
    r"^/hcmUI/CandidateExperience/[a-z]{2}(?:-[A-Za-z]{2})?/sites/([A-Za-z0-9_-]{1,64})"
    r"/(?:job|requisitions/preview)/(\d{1,20})(?:/|$)"
)
# A careers link: https://<host>/hcmUI/CandidateExperience/en/sites/<site>/...
CAREERS_LINK = re.compile(
    r"^(?:https?://)?([a-z0-9][a-z0-9.-]*)/hcmUI/CandidateExperience/[a-z]{2}(?:-[A-Za-z]{2})?"
    r"/sites/([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"</?(?:p|br|li|ul|ol|div|h[1-6]|tr)\b[^>]*>", re.IGNORECASE)
API = "hcmRestApi/resources/latest"


def valid_board(board: str) -> bool:
    """`host/site` with a plausible host name and site name."""
    host, _, site = board.partition("/")
    return bool(_HOST.match(host) and _SITE.match(site))


@dataclass(frozen=True)
class OracleJob:
    """A link to one posting on an Oracle site."""

    host: str
    site: str
    job_id: str

    @property
    def detail_url(self) -> str:
        return detail_url(self.host, self.site, self.job_id)


def job_link(url: str | None) -> OracleJob | None:
    """Recognise a posting link on `*.oraclecloud.com`; None for anything else."""
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not _CLOUD_HOST.match(host):
        return None
    if m := _JOB_PATH.match(parts.path):
        return OracleJob(host, m.group(1), m.group(2))
    return None


def job_page(host: str, site: str, job_id: str) -> str:
    return f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{job_id}"


def list_url(host: str, site: str, limit: int, offset: int, extra: str = "") -> str:
    """The job list, newest first. `extra` adds finder terms, e.g. a location facet."""
    finder = (
        f"findReqs;siteNumber={site},{extra}limit={limit},offset={offset},sortBy=POSTING_DATES_DESC"
    )
    return (
        f"https://{host}/{API}/recruitingCEJobRequisitions?onlyData=true"
        f"&expand=requisitionList.secondaryLocations&finder={quote(finder, safe='=;,_')}"
    )


def detail_url(host: str, site: str, job_id: str) -> str:
    finder = f'ById;Id="{job_id}",siteNumber={site}'
    return (
        f"https://{host}/{API}/recruitingCEJobRequisitionDetails?onlyData=true&expand=all"
        f"&finder={quote(finder, safe='=;,_')}"
    )


def detail_item(body: Any) -> dict[str, Any] | None:
    """The one job in a detail answer; None when it isn't posted (or the answer is odd)."""
    items = body.get("items") if isinstance(body, dict) else None
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0]
    return None


def _plain(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        return ""
    text = _TAG.sub(" ", _BLOCK.sub("\n", html.unescape(raw)))
    lines = (" ".join(line.split()) for line in html.unescape(text).splitlines())
    return "\n".join(line for line in lines if line)


def description(item: dict[str, Any]) -> str:
    """The job's text. Some templates leave the External* fields empty ("<br>") and carry the
    body in the corporate and organisation descriptions instead (seen on Goldman Sachs'
    campus postings, per freehire)."""
    fields = ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")
    external = "\n".join(_plain(item.get(k)) for k in fields).strip()
    if external:
        return external
    return "\n".join(
        _plain(item.get(k)) for k in ("CorporateDescriptionStr", "OrganizationDescriptionStr")
    ).strip()


def as_job_posting(item: dict[str, Any]) -> dict[str, Any]:
    """The detail as schema.org JobPosting data, so the free extraction layer reads it like
    any job page's own data (title, location, published date, experience in the text)."""
    posting: dict[str, Any] = {"@type": "JobPosting", "title": item.get("Title") or ""}
    if text := description(item):
        posting["description"] = text
    if posted := item.get("ExternalPostedStartDate") or item.get("PostedDate"):
        posting["datePosted"] = str(posted)
    if where := item.get("PrimaryLocation"):
        posting["jobLocation"] = {"@type": "Place", "address": {"addressLocality": str(where)}}
    if str(item.get("WorkplaceTypeCode") or item.get("WorkplaceType") or "").upper() in (
        "ORA_REMOTE",
        "REMOTE",
    ):
        posting["jobLocationType"] = "TELECOMMUTE"
    if (schedule := str(item.get("JobSchedule") or "").casefold()) in ("full time", "part time"):
        posting["employmentType"] = schedule.upper().replace(" ", "_")
    return posting


def page_text(item: dict[str, Any]) -> str:
    """Title, place and description as plain text, for the LLM fallback and the scam check."""
    head = [str(item.get("Title") or ""), str(item.get("PrimaryLocation") or "")]
    return "\n\n".join(p for p in ("\n".join(h for h in head if h), description(item)) if p)
