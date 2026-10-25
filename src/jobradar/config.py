"""Settings (pydantic-settings): loads config.yaml and .env, and fails fast with clear messages."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from jobradar.pipeline import oracle

DEFAULT_CONFIG_PATH = Path("config/config.yaml")
DEFAULT_ENV_FILE = Path(".env")

Percent = Annotated[int, Field(ge=0, le=100)]
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ConfigError(Exception):
    """Raised when config.yaml or .env is missing or invalid. The message is user-facing."""


class _Strict(BaseModel):
    # Unknown keys are almost always typos, so reject them instead of silently ignoring them.
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---- config.yaml ----------------------------------------------------------------------------


class TelegramSourceConfig(_Strict):
    enabled: bool = True
    chats: list[str | int] = Field(default_factory=list)
    backfill_days: int = Field(default=3, ge=0, le=30)


class EmailForwardSourceConfig(_Strict):
    enabled: bool = True
    folder: str = "JobRadar"
    poll_minutes: int = Field(default=2, ge=1, le=60)


class WhatsAppSourceConfig(_Strict):
    enabled: bool = False
    port: int = Field(default=8765, ge=1024, le=65535)


BoardATS = Literal["greenhouse", "lever", "ashby", "smartrecruiters", "workday", "oracle"]
_ATS_NAMES = {
    "greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby",
    "smartrecruiters": "SmartRecruiters", "workday": "Workday", "oracle": "Oracle",
}  # fmt: skip
# A Workday career site: "<company>.wd<N>.myworkdayjobs.com/<Site>" (its board name in config).
WORKDAY_BOARD = re.compile(
    r"^([a-z0-9][a-z0-9-]{0,62})\.(wd\d{1,3})\.myworkdayjobs\.com/([A-Za-z0-9_-]{1,100})$"
)


_BOARD_LINKS = [
    (
        "greenhouse",
        r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9._-]+)",
    ),
    ("greenhouse", r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9._-]+)"),
    ("lever", r"jobs(?:\.eu)?\.lever\.co/([A-Za-z0-9._-]+)"),
    ("lever", r"api(?:\.eu)?\.lever\.co/v0/postings/([A-Za-z0-9._-]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([A-Za-z0-9._-]+)"),
    ("ashby", r"api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9._-]+)"),
    ("smartrecruiters", r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9._-]+)"),
    ("smartrecruiters", r"api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9._-]+)"),
]
# The careers link (optionally with a language part, /en-US/) or the API address.
_WORKDAY_LINK = re.compile(
    r"^(?:https?://)?([a-z0-9][a-z0-9-]*\.wd\d{1,3}\.myworkdayjobs\.com)/"
    r"(?:wday/cxs/[a-z0-9-]+/)?(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)


def board_from_link(link: str) -> tuple[str, str]:
    """('greenhouse', 'stripe') from https://job-boards.greenhouse.io/stripe (or a job link
    on that board). For Workday the board name is the site: ('workday',
    'nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite')."""
    if m := _WORKDAY_LINK.search(link.strip()):
        return "workday", f"{m.group(1).lower()}/{m.group(2)}"
    if m := oracle.CAREERS_LINK.search(link.strip()):
        return "oracle", f"{m.group(1).lower()}/{m.group(2)}"
    for ats, pattern in _BOARD_LINKS:
        if m := re.search(r"^(?:https?://)?(?:www\.)?" + pattern, link.strip(), re.IGNORECASE):
            return ats, m.group(1)
    raise ValueError(
        f"{link!r} isn't a Greenhouse, Lever, Ashby, SmartRecruiters, Workday or Oracle board link "
        "(e.g. https://job-boards.greenhouse.io/stripe)"
    )


class BoardRef(_Strict):
    """One company job board: its hiring system and the company's name in that system's
    board address (e.g. boards.greenhouse.io/**stripe**)."""

    ats: BoardATS
    company: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._/-]+$")
    name: str | None = None  # how it shows in Notion and emails; default: the company slug

    @model_validator(mode="after")
    def _board_name(self) -> BoardRef:
        if self.ats == "workday":
            if not WORKDAY_BOARD.match(self.company):
                raise ValueError(
                    f"workday: '{self.company}' should look like "
                    "company.wd5.myworkdayjobs.com/SiteName (paste the careers link)"
                )
        elif self.ats == "oracle":
            if not oracle.valid_board(self.company):
                raise ValueError(
                    f"oracle: '{self.company}' should look like host/SiteName "
                    "(paste the careers link: …/hcmUI/CandidateExperience/en/sites/CX_1/jobs)"
                )
        elif "/" in self.company or len(self.company) > 100:
            raise ValueError(f"{self.ats}: '{self.company}' isn't a board name")
        return self

    @property
    def key(self) -> str:
        return f"{self.ats}:{self.company}"

    @property
    def label(self) -> str:
        """'Greenhouse · Stripe': how the board shows in Notion and emails."""
        defaults = {"workday": self.company.split(".")[0], "oracle": self.company.split("/")[-1]}
        default = defaults.get(self.ats, self.company)
        return f"{_ATS_NAMES[self.ats]} · {self.name or default}"


class BoardsConfig(_Strict):
    """Company job boards checked directly, so new postings arrive before channels repost them."""

    enabled: bool = True
    every_minutes: int = Field(default=20, ge=5, le=240)
    # One section per hiring system: `Name: board name` (or a link to that board). A plain
    # list of board names also works, e.g. `greenhouse: [stripe, rubrik]`.
    greenhouse: dict[str, str] | list[str] = Field(default_factory=dict)
    lever: dict[str, str] | list[str] = Field(default_factory=dict)
    ashby: dict[str, str] | list[str] = Field(default_factory=dict)
    smartrecruiters: dict[str, str] | list[str] = Field(default_factory=dict)
    workday: dict[str, str] | list[str] = Field(default_factory=dict)  # India postings only
    oracle: dict[str, str] | list[str] = Field(default_factory=dict)  # India postings only
    # Every board as a flat list; filled from the sections above (or given directly).
    companies: list[BoardRef] = Field(default_factory=list)

    @classmethod
    def _from_links(cls, v: Any) -> Any:
        """`Name: board link` pairs (the readable form) become BoardRefs."""
        if not isinstance(v, dict):
            return v
        refs = []
        for name, link in v.items():
            ats, company = board_from_link(str(link))
            refs.append({"ats": ats, "company": company, "name": str(name)})
        return refs

    @model_validator(mode="before")
    @classmethod
    def _sections(cls, data: Any) -> Any:
        """Turn the per-hiring-system sections into the flat `companies` list."""
        if not isinstance(data, dict):
            return data
        listed = data.get("companies") or []
        refs: list[Any] = (
            list(cls._from_links(listed)) if isinstance(listed, dict) else list(listed)
        )
        for ats in _ATS_NAMES:
            section = data.get(ats) or {}
            if isinstance(section, dict):
                pairs = [(str(k), str(v)) for k, v in section.items()]
            elif isinstance(section, list):
                pairs = [(str(b), str(b)) for b in section]
            else:
                raise ValueError(f"{ats}: list boards as 'Name: board name' lines")
            for name, value in pairs:
                board = value.strip()
                if ats == "oracle" and oracle.valid_board(board):
                    pass  # host/SiteName as written
                elif "/" in board:  # a link to the board (or to a job on it)
                    found, board = board_from_link(board)
                    if found != ats:
                        raise ValueError(
                            f"{ats} > {name}: that's a {_ATS_NAMES[found]} link; "
                            f"move it under {found}:"
                        )
                refs.append({"ats": ats, "company": board, "name": name})
        emptied: dict[str, Any] = {
            ats: {} for ats in _ATS_NAMES if data.get(ats) is None
        }  # `lever:` alone
        return {**data, **emptied, "companies": refs}

    @field_validator("companies")
    @classmethod
    def _unique(cls, v: list[BoardRef]) -> list[BoardRef]:
        keys = [b.key for b in v]
        if dupes := sorted({k for k in keys if keys.count(k) > 1}):
            raise ValueError(f"listed twice: {', '.join(dupes)}")
        return v


class PagesConfig(_Strict):
    """Web pages watched for new announcements, such as hiring-drive pages (TCS NextStep,
    Infosys, Wipro, Cognizant) that announce drives instead of listing jobs. Empty by default:
    you choose each page, and you're responsible for following its site's terms."""

    enabled: bool = True
    every_hours: int = Field(default=2, ge=1, le=6)
    watch: dict[str, str] = Field(default_factory=dict)  # Name: https://page

    @field_validator("watch")
    @classmethod
    def _urls(cls, v: dict[str, str]) -> dict[str, str]:
        seen: dict[str, str] = {}
        for name, url in v.items():
            url = str(url).strip()
            if not re.match(r"^https?://[^\s/]+\.[^\s/]+", url):
                raise ValueError(f"{name}: '{url}' isn't a web page address (https://…)")
            if url in seen.values():
                raise ValueError(f"{name}: that page is already watched")
            seen[str(name)] = url
        return seen


FreehireSeniority = Literal["intern", "junior", "middle", "senior"]


def _juniors() -> list[FreehireSeniority]:
    return ["intern", "junior"]


class FreehireConfig(_Strict):
    """freehire.me's public job search (github.com/strelov1/freehire): one query covers
    thousands of companies on hiring systems JobRadar has no reader for. Off by default."""

    enabled: bool = False
    every_minutes: int = Field(default=30, ge=15, le=240)
    countries: list[str] = Field(default_factory=lambda: ["IN"], min_length=1)
    seniority: list[FreehireSeniority] = Field(default_factory=_juniors, min_length=1)
    categories: list[str] = Field(default_factory=list)  # e.g. [backend, fullstack]; empty: all
    max_pages: int = Field(default=5, ge=1, le=20)  # 100 postings a page


class SourcesConfig(_Strict):
    telegram: TelegramSourceConfig = Field(default_factory=TelegramSourceConfig)
    boards: BoardsConfig = Field(default_factory=BoardsConfig)
    pages: PagesConfig = Field(default_factory=PagesConfig)
    freehire: FreehireConfig = Field(default_factory=FreehireConfig)
    email_forward: EmailForwardSourceConfig = Field(default_factory=EmailForwardSourceConfig)
    whatsapp: WhatsAppSourceConfig = Field(default_factory=WhatsAppSourceConfig)


class LLMConfig(_Strict):
    extract_model: str = "gemini/gemini-flash-lite-latest"
    # fallback: only jobs whose page and post don't state role and company go to the LLM.
    # always: every job does (fills summary and skills too, at the cost of an LLM call each).
    extract_mode: Literal["fallback", "always"] = "fallback"
    writer_model: str = "gemini/gemini-flash-latest"
    daily_budget_inr: float = Field(default=15, gt=0)
    requests_per_minute: float = Field(default=10, gt=0, le=1000)  # free Gemini tier: ~10-15
    usd_to_inr: float = Field(default=88, gt=0)  # for the budget's cost estimate

    @field_validator("extract_model", "writer_model")
    @classmethod
    def _has_provider(cls, v: str) -> str:
        if "/" not in v:
            raise ValueError("use 'provider/model', e.g. 'gemini/gemini-flash-latest'")
        return v

    @property
    def providers(self) -> set[str]:
        return {m.split("/", 1)[0] for m in (self.extract_model, self.writer_model)}


class FiltersConfig(_Strict):
    batch_years: list[int] = Field(default_factory=list)
    degrees: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    max_experience_years: float | None = Field(default=None, ge=0)
    exclude_companies: list[str] = Field(default_factory=list)


class ScoringConfig(_Strict):
    hide_below: Percent = 40
    alert_above: Percent = 75
    auto_resume_above: Percent | None = 85

    @model_validator(mode="after")
    def _ordered(self) -> ScoringConfig:
        if self.alert_above < self.hide_below:
            raise ValueError("alert_above must be >= hide_below")
        if self.auto_resume_above is not None and self.auto_resume_above < self.hide_below:
            raise ValueError("auto_resume_above must be >= hide_below (or null)")
        return self


MatchProviderName = Literal["llm", "skills_rule"]


class MatchingConfig(_Strict):
    """How jobs are matched against config/profile.yaml (see docs/LLD.md, match.py)."""

    enabled: bool = True
    profile_path: Path = Path("config/profile.yaml")
    # Tried in order; the first that answers wins. skills_rule needs no key but never alerts.
    providers: list[MatchProviderName] = Field(
        default_factory=lambda: ["llm", "skills_rule"]  # type: ignore[arg-type]
    )
    # Match Score = 100 x weighted sum of the answers (re-weighting needs no new calls).
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "skills_coverage": 0.40, "role_alignment": 0.30,
            "seniority_fit": 0.15, "location_ok": 0.15,
        }
    )  # fmt: skip
    min_confidence: float = Field(default=0.5, ge=0, le=1)
    hide_if_eligible_below: float = Field(default=0.2, ge=0, le=1)
    alert_if_eligible_above: float = Field(default=0.7, ge=0, le=1)
    # true: a job limited to other branches ("CSE/IT only") counts as not eligible.
    branch_strict: bool = False

    @field_validator("weights")
    @classmethod
    def _known_weights(cls, v: dict[str, float]) -> dict[str, float]:
        known = {"skills_coverage", "role_alignment", "seniority_fit", "location_ok"}
        if unknown := set(v) - known:
            raise ValueError(f"unknown weight(s) {sorted(unknown)}; use {sorted(known)}")
        if any(w < 0 for w in v.values()) or not sum(v.values()):
            raise ValueError("weights must be >= 0 and not all zero")
        return v

    @field_validator("providers")
    @classmethod
    def _some_provider(cls, v: list[MatchProviderName]) -> list[MatchProviderName]:
        if not v:
            raise ValueError("list at least one provider")
        return list(dict.fromkeys(v))


class ResumeConfig(_Strict):
    enabled: bool = True
    template: str = "classic"  # built in, or a path to your own .tex.j2
    max_pages: int = Field(default=1, ge=1, le=3)
    tailor: bool = True  # false: every resume is your profile as written
    max_per_day: int = Field(
        default=10, ge=0, le=100
    )  # automatic builds; Shortlisted always builds
    # Phrases a tailored resume may never add (they may stay where your profile has them).
    banned_phrases: list[str] = Field(
        default_factory=lambda: [
            "world-class",
            "expert",
            "guru",
            "ninja",
            "rockstar",
            "cutting-edge",
            "passionate",
            "synergy",
        ]
    )


class LinksConfig(_Strict):
    # Extra redirect hosts to expand, e.g. a channel's own link shortener.
    extra_shorteners: list[str] = Field(default_factory=list)
    # Never treat as jobs: a host ("ads.example.com") or host/path prefix ("x.com/courses").
    deny: list[str] = Field(default_factory=list)


class StorageConfig(_Strict):
    local_dir: Path = Path("./output")


class EmailConfig(_Strict):
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = Field(default=587, ge=1, le=65535)
    imap_host: str = "imap.gmail.com"


class NotifyConfig(_Strict):
    alerts: bool = True
    # Dry run (the default): alerts are logged and saved as previews in data/alerts/, not
    # emailed. Set false once the previews look right.
    dry_run: bool = True
    alert_batch_minutes: int = Field(default=10, ge=0, le=120)
    max_alerts_per_day: int = Field(default=10, ge=1, le=100)
    # Only jobs first seen this recently can alert; older ones were in your Inbox already.
    alert_max_age_days: float = Field(default=2, gt=0, le=30)
    # A job its own site says was published longer ago than this gets no instant alert: it
    # still reaches your Inbox and digest, marked "old posting".
    old_posting_days: int = Field(default=30, ge=1, le=365)
    digest: bool = True  # a summary email at each digest time
    # An email when a job you shortlisted closes today or tomorrow (checked hourly, 8:00-22:00).
    closing_reminders: bool = True
    # An email with a follow-up draft when a job you set to Applied has had no reply for this
    # many days (at most 2 per job; set Notion's Followed Up date when you send one).
    followups: bool = True
    followup_after_days: int = Field(default=10, ge=3, le=60)
    # "I applied" / "Not interested" / "I followed up" buttons in emails: you send the ready
    # email they open, JobRadar reads it over IMAP and updates the job and Notion.
    email_actions: bool = True
    # Once a week, the digest lists the skills your good matches asked for that your profile
    # doesn't (`jobradar skills` shows them any time).
    skill_gaps: bool = True
    # Read new mail from companies you applied to (only theirs) and email you a suggested
    # status: "Rubrik replied: interview invite?" with a one-tap button. Needs email_actions.
    company_replies: bool = True
    digest_times: list[str] = Field(default_factory=lambda: ["09:00", "19:00"])
    timezone: str = "Asia/Kolkata"

    @field_validator("digest_times")
    @classmethod
    def _hhmm(cls, v: list[str]) -> list[str]:
        bad = [t for t in v if not _HHMM.match(t)]
        if bad:
            raise ValueError(f"times must be 24-hour HH:MM, got {bad}")
        return v

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown timezone {v!r}, e.g. 'Asia/Kolkata'") from None
        return v

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class WebConfig(_Strict):
    """The dashboard: your jobs and applications on a page on this computer."""

    # Start it with `jobradar run` (`jobradar web` opens it on its own either way).
    enabled: bool = True
    # 127.0.0.1: only this computer can open it. Never 0.0.0.0 (anyone on your Wi-Fi).
    host: str = "127.0.0.1"
    # If something else uses it, the next free port up to +9 is taken (the log says which).
    port: int = Field(default=8080, ge=1024, le=65535)
    # Where you open it from another device, e.g. your phone through Tailscale
    # (https://laptop.tail1234.ts.net; see docs/PHONE.md). Email links use it, and the
    # dashboard accepts requests for that name. None: this computer only.
    url: str | None = None

    @field_validator("host")
    @classmethod
    def _not_everywhere(cls, v: str) -> str:
        if v.strip() in ("0.0.0.0", "::", ""):  # noqa: S104 (refusing it, not binding)
            raise ValueError(
                "listening on every network would let anyone on your Wi-Fi open your jobs; "
                "use 127.0.0.1"
            )
        return v.strip()

    @field_validator("url")
    @classmethod
    def _a_web_address(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        parts = urlsplit(v.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("give the whole address, like https://laptop.tail1234.ts.net")
        return f"{parts.scheme}://{parts.netloc}"


class NotionConfig(_Strict):
    """Notion as a second place to track jobs. The dashboard has everything Notion shows, so
    Notion can be switched off: then no Notion key is needed and nothing is sent there."""

    enabled: bool = True


class AppConfig(_Strict):
    """Everything in config.yaml. Every section is optional and has defaults."""

    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    matching: MatchingConfig = Field(default_factory=MatchingConfig)
    resume: ResumeConfig = Field(default_factory=ResumeConfig)
    links: LinksConfig = Field(default_factory=LinksConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    notion: NotionConfig = Field(default_factory=NotionConfig)

    @model_validator(mode="after")
    def _telegram_has_chats(self) -> AppConfig:
        # Checked here, not on the section, so it also fires when the section is left out.
        tg = self.sources.telegram
        if tg.enabled and not tg.chats:
            raise ValueError(
                "sources.telegram.chats: list at least one chat, "
                "or set sources.telegram.enabled: false"
            )
        return self


# ---- .env -----------------------------------------------------------------------------------


class Secrets(BaseSettings):
    """Secrets from the environment or .env. Optional here; each feature checks what it needs."""

    model_config = SettingsConfigDict(env_file=DEFAULT_ENV_FILE, extra="ignore", frozen=True)

    tg_api_id: int | None = None
    tg_api_hash: SecretStr | None = None
    notion_token: SecretStr | None = None
    notion_database_id: str | None = None
    notion_data_source_id: str | None = None
    gemini_api_key: SecretStr | None = None
    groq_api_key: SecretStr | None = None
    email_address: str | None = None
    email_app_password: SecretStr | None = None
    notify_to: str | None = None
    gdrive_folder_id: str | None = None
    # The SaaS database (Postgres), e.g. postgresql+psycopg://user:pass@host/db?sslmode=require.
    # Not set: the local SQLite file data/jobradar.db, as in the self-hosted app.
    database_url: SecretStr | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: Any) -> Any:
        # `.env.example` leaves unused keys as `KEY=`; treat that as "not set".
        return None if isinstance(v, str) and not v.strip() else v

    @property
    def notify_address(self) -> str | None:
        return self.notify_to or self.email_address


Feature = Literal["telegram", "notion", "llm", "email", "gdrive"]

_REQUIRED: dict[Feature, tuple[str, ...]] = {
    "telegram": ("tg_api_id", "tg_api_hash"),
    "notion": ("notion_token", "notion_database_id"),
    "email": ("email_address", "email_app_password"),
    "gdrive": ("gdrive_folder_id",),
}

_PROVIDER_KEYS = {"gemini": "gemini_api_key", "groq": "groq_api_key"}


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    config: AppConfig
    secrets: Secrets

    def missing(self, *features: Feature) -> list[str]:
        """Names of the .env variables the given features need but are not set."""
        names: list[str] = []
        for feature in features:
            if feature == "llm":
                keys = [_PROVIDER_KEYS.get(p) for p in sorted(self.config.llm.providers)]
                names += [k for k in keys if k is not None]
            else:
                names += _REQUIRED[feature]
        return [n.upper() for n in dict.fromkeys(names) if getattr(self.secrets, n) is None]

    def require(self, *features: Feature) -> None:
        """Raise ConfigError naming every missing .env variable for these features."""
        missing = self.missing(*features)
        if missing:
            raise ConfigError(
                "Missing in .env: "
                + ", ".join(missing)
                + " (see .env.example for where to get them)"
            )


def _format_errors(source: str, err: ValidationError) -> str:
    lines = [f"{source} is invalid:"]
    for e in err.errors():
        where = ".".join(str(p) for p in e["loc"])
        msg = e["msg"].removeprefix("Value error, ")
        if e["type"] == "extra_forbidden":
            msg = "unknown setting (typo?)"
        lines.append(f"  - {where}: {msg}" if where else f"  - {msg}")
    return "\n".join(lines)


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    if not path.is_file():
        raise ConfigError(f"{path} not found. Copy config.example.yaml to {path} and edit it.")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must be a mapping of sections (sources:, llm:, ...).")
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(_format_errors(str(path), e)) from None


def load_settings(
    config_path: Path = DEFAULT_CONFIG_PATH, env_file: Path | None = DEFAULT_ENV_FILE
) -> Settings:
    """Load and validate config.yaml and .env. Raises ConfigError with a readable message."""
    config = load_config(config_path)
    try:
        secrets = Secrets(_env_file=env_file)
    except ValidationError as e:
        raise ConfigError(_format_errors(str(env_file or "environment"), e)) from None
    return Settings(config=config, secrets=secrets)
