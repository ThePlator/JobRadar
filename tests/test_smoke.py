from pathlib import Path

import yaml
from typer.testing import CliRunner

from jobradar import __version__
from jobradar.__main__ import app

ROOT = Path(__file__).resolve().parents[1]
runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_unimplemented_commands_fail_loudly() -> None:
    result = runner.invoke(app, ["resume", "abc"])
    assert result.exit_code == 1


def test_example_configs_parse() -> None:
    config = yaml.safe_load((ROOT / "config.example.yaml").read_text())
    profile = yaml.safe_load((ROOT / "profile.example.yaml").read_text())
    assert config["scoring"]["auto_resume_above"] == 85
    assert config["llm"]["extract_model"].startswith("gemini/")
    assert profile["basics"]["name"]


def test_profile_ids_are_unique() -> None:
    profile = yaml.safe_load((ROOT / "profile.example.yaml").read_text())
    ids = [
        item_id
        for section in ("experience", "projects")
        for item in profile[section]
        for item_id in [item["id"], *(b["id"] for b in item["bullets"])]
    ]
    assert len(ids) == len(set(ids))
