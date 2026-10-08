"""Settings (pydantic-settings): loads config.yaml and .env, and fails fast with clear messages."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal
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


class SourcesConfig(_Strict):
    telegram: TelegramSourceConfig = Field(default_factory=TelegramSourceConfig)
    email_forward: EmailForwardSourceConfig = Field(default_factory=EmailForwardSourceConfig)
    whatsapp: WhatsAppSourceConfig = Field(default_factory=WhatsAppSourceConfig)


class LLMConfig(_Strict):
    extract_model: str = "gemini/gemini-flash-lite-latest"
    writer_model: str = "gemini/gemini-pro-latest"
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
    alert_above: Percent = 85
    auto_resume_above: Percent | None = 85

    @model_validator(mode="after")
    def _ordered(self) -> ScoringConfig:
        if self.alert_above < self.hide_below:
            raise ValueError("alert_above must be >= hide_below")
        if self.auto_resume_above is not None and self.auto_resume_above < self.hide_below:
            raise ValueError("auto_resume_above must be >= hide_below (or null)")
        return self


class ResumeConfig(_Strict):
    template: str = "classic"
    max_pages: int = Field(default=1, ge=1, le=3)


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
    alert_batch_minutes: int = Field(default=10, ge=0, le=120)
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


class AppConfig(_Strict):
    """Everything in config.yaml. Every section is optional and has defaults."""

    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    resume: ResumeConfig = Field(default_factory=ResumeConfig)
    links: LinksConfig = Field(default_factory=LinksConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)

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
