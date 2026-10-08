import re
from pathlib import Path

import pytest

from jobradar.config import ConfigError, load_config, load_settings

ROOT = Path(__file__).resolve().parents[1]


def write(tmp_path: Path, text: str, name: str = "config.yaml") -> Path:
    p = tmp_path / name
    p.write_text(text)
    return p


def test_example_config_loads() -> None:
    cfg = load_config(ROOT / "config.example.yaml")
    assert cfg.scoring.auto_resume_above == 85
    assert cfg.llm.providers == {"gemini"}
    assert cfg.notify.tz.key == "Asia/Kolkata"
    assert cfg.sources.email_forward.folder == "JobRadar"


def test_empty_file_uses_defaults_but_telegram_needs_chats(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"sources\.telegram\.chats: list at least one chat"):
        load_config(write(tmp_path, ""))
    cfg = load_config(write(tmp_path, "sources: {telegram: {enabled: false}}"))
    assert cfg.scoring.hide_below == 40


def test_missing_file_explains_how_to_create_it(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=re.escape("Copy config.example.yaml")):
        load_config(tmp_path / "nope.yaml")


BASE = "sources: {telegram: {chats: ['@x']}}\n"


@pytest.mark.parametrize(
    ("yaml_text", "expected"),
    [
        ("scoring: {hide_beloww: 40}", r"scoring\.hide_beloww: unknown setting \(typo\?\)"),
        ("scoring: {hide_below: 150}", r"scoring\.hide_below: Input should be less than or equal"),
        ("scoring: {hide_below: 60, alert_above: 50}", "alert_above must be >= hide_below"),
        ("notify: {timezone: Mars/Base}", "unknown timezone 'Mars/Base'"),
        ("notify: {digest_times: ['9am']}", "24-hour HH:MM"),
        ("llm: {extract_model: gemini-flash}", "use 'provider/model'"),
    ],
)
def test_bad_values_give_readable_errors(tmp_path: Path, yaml_text: str, expected: str) -> None:
    with pytest.raises(ConfigError, match=expected):
        load_config(write(tmp_path, BASE + yaml_text))


@pytest.mark.parametrize(
    ("yaml_text", "expected"),
    [("- just\n- a list", "must be a mapping"), ("scoring: [", "not valid YAML")],
)
def test_malformed_file(tmp_path: Path, yaml_text: str, expected: str) -> None:
    with pytest.raises(ConfigError, match=expected):
        load_config(write(tmp_path, yaml_text))


def test_secrets_from_env_file_and_require(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "TG_API_ID",
        "TG_API_HASH",
        "NOTION_TOKEN",
        "NOTION_DATABASE_ID",
        "GEMINI_API_KEY",
        "EMAIL_ADDRESS",
        "EMAIL_APP_PASSWORD",
        "NOTIFY_TO",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = write(tmp_path, "sources: {telegram: {chats: ['@x']}}")
    env = write(
        tmp_path, "TG_API_ID=123\nTG_API_HASH=abc\nNOTION_TOKEN=\nEMAIL_ADDRESS=bot@x.com\n", ".env"
    )

    settings = load_settings(cfg, env)

    assert settings.secrets.tg_api_id == 123
    assert settings.secrets.tg_api_hash is not None
    assert "abc" not in repr(settings.secrets)  # secrets never show up in logs/reprs
    assert settings.secrets.notion_token is None  # blank means unset
    assert settings.secrets.notify_address == "bot@x.com"  # NOTIFY_TO falls back
    settings.require("telegram")
    assert settings.missing("notion", "llm") == [
        "NOTION_TOKEN",
        "NOTION_DATABASE_ID",
        "GEMINI_API_KEY",
    ]
    with pytest.raises(ConfigError, match=re.escape("Missing in .env: EMAIL_APP_PASSWORD")):
        settings.require("email")


def test_bad_secret_type_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TG_API_ID", raising=False)
    cfg = write(tmp_path, "sources: {telegram: {chats: ['@x']}}")
    env = write(tmp_path, "TG_API_ID=not-a-number\n", ".env")
    with pytest.raises(ConfigError, match="tg_api_id"):
        load_settings(cfg, env)
