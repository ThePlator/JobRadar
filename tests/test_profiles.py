"""Profiles and preferences per user (SaaS PR 5c). The owner keeps editing profile.yaml and
config.yaml, copied in whenever they change; anyone else's live only in the database."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from jobradar.config import ConfigError, Preferences, ScoringConfig, Settings
from jobradar.db.models import User
from jobradar.db.repo import Repo
from jobradar.profile import sync_owner, user_preferences, user_profile, with_skill
from jobradar.services import profile as profile_svc
from jobradar.services.context import Ctx
from jobradar.services.errors import Refused
from tests.conftest import FakeClock
from tests.test_digest import settings
from tests.test_web import PROFILE_YAML


def with_profile(path: Path) -> Settings:
    path.write_text(PROFILE_YAML)
    base = settings()
    return base.model_copy(update={"config": base.config.model_copy(update={
        "matching": base.config.matching.model_copy(update={"profile_path": path})})})  # fmt: skip


def stranger(repo: Repo) -> Repo:
    repo.add_user(User(id="u2", auth_uid="firebase-u2", created_at="2026-10-08"))
    return repo.for_user("u2")


def test_the_owners_files_are_copied_in_when_they_change(repo: Repo, tmp_path: Path) -> None:
    s = with_profile(tmp_path / "profile.yaml")
    sync_owner(repo, s)
    stored = repo.get_profile()
    assert stored is not None and stored[0] == yaml.safe_load(PROFILE_YAML)
    first = stored[1]
    sync_owner(repo, s)  # unchanged: nothing written
    assert repo.get_profile() == stored
    (tmp_path / "profile.yaml").write_text(PROFILE_YAML + "\nsummary: Changed\n")
    profile = user_profile(repo, s)  # reading it copies the new file in
    assert profile["summary"] == "Changed" and repo.get_profile()[1] != first  # type: ignore[index]
    prefs = repo.get_preferences()
    assert prefs is not None and prefs[0]["scoring"]["alert_above"] == 75


def test_someone_elses_profile_is_their_own(repo: Repo, tmp_path: Path) -> None:
    s = with_profile(tmp_path / "profile.yaml")
    other = stranger(repo)
    with pytest.raises(ConfigError, match="haven't added your profile"):
        user_profile(other, s)
    other.save_profile({"basics": {"name": "Asha"}, "skills": ["Go"]})
    assert user_profile(other, s)["basics"]["name"] == "Asha"
    assert user_profile(repo, s)["basics"]["name"] != "Asha"


def test_preferences_per_person(repo: Repo, clock: FakeClock) -> None:
    other = stranger(repo)
    assert user_preferences(other, settings()) == Preferences()  # the defaults, until set
    mine = Preferences(scoring=ScoringConfig(alert_above=60, hide_below=30), timezone="UTC")
    other.save_preferences(mine.model_dump(mode="json"))
    theirs = Ctx(repo, settings(), clock(), user_id="u2")
    assert (theirs.good, str(theirs.tz)) == (60, "UTC")
    assert Ctx(repo, settings(), clock()).good == 75  # the owner's: config.yaml


def test_preferences_are_checked() -> None:
    with pytest.raises(ValueError, match="timezone"):
        Preferences(timezone="Mars/Olympus")
    with pytest.raises(ValueError, match="HH:MM"):
        Preferences(digest_times=["9am"])


def test_i_know_this_for_the_owner_and_for_others(
    repo: Repo, clock: FakeClock, tmp_path: Path
) -> None:
    path = tmp_path / "profile.yaml"
    s = with_profile(path)
    assert profile_svc.know_skill(Ctx(repo, s, clock()), "Kafka")  # a backup name
    assert "Kafka" in path.read_text()
    assert "Kafka" in str(user_profile(repo, s)["skills"])
    other = stranger(repo)
    other.save_profile({"skills": {"languages": ["Go"]}})
    theirs = Ctx(repo, s, clock(), user_id="u2")
    assert profile_svc.know_skill(theirs, "Rust") is None  # no file: the database only
    assert user_profile(other, s)["skills"]["additional"] == ["Rust"]
    assert "Rust" not in path.read_text()
    with pytest.raises(Refused, match="already"):
        profile_svc.know_skill(theirs, "rust")


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ({"skills": ["Go"]}, {"skills": ["Go", "Rust"]}),
        (
            {"skills": {"languages": ["Go"]}},
            {"skills": {"languages": ["Go"], "additional": ["Rust"]}},
        ),
        ({}, {"skills": {"additional": ["Rust"]}}),
    ],
)
def test_with_skill(before: dict[str, Any], after: dict[str, Any]) -> None:
    assert with_skill(before, "Rust") == after
