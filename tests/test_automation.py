from pathlib import Path

from smallhd_cal.automation import probe_command
from smallhd_cal.paths import AppPaths
from smallhd_cal.probe import SPOTREAD_ARGS


def test_probe_command_prefers_resource_root_argyll(
    monkeypatch,
    tmp_path: Path,
) -> None:
    resource_spotread = tmp_path / "resources" / "Argyll" / "bin" / "spotread"
    user_spotread = tmp_path / "user" / "Argyll" / "bin" / "spotread"

    def fake_find(root: Path) -> Path | None:
        if root == tmp_path / "resources":
            return resource_spotread
        if root == tmp_path / "user":
            return user_spotread
        return None

    monkeypatch.setattr("smallhd_cal.automation.find_bundled_spotread", fake_find)
    paths = AppPaths(resource_root=tmp_path / "resources", user_data_root=tmp_path / "user")

    assert probe_command(paths) == [str(resource_spotread), *SPOTREAD_ARGS]


def test_probe_command_falls_back_to_user_root_argyll(
    monkeypatch,
    tmp_path: Path,
) -> None:
    user_spotread = tmp_path / "user" / "Argyll" / "bin" / "spotread"

    def fake_find(root: Path) -> Path | None:
        if root == tmp_path / "user":
            return user_spotread
        return None

    monkeypatch.setattr("smallhd_cal.automation.find_bundled_spotread", fake_find)
    paths = AppPaths(resource_root=tmp_path / "resources", user_data_root=tmp_path / "user")

    assert probe_command(paths) == [str(user_spotread), *SPOTREAD_ARGS]


def test_probe_command_falls_back_to_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("smallhd_cal.automation.find_bundled_spotread", lambda _root: None)
    paths = AppPaths(resource_root=tmp_path / "resources", user_data_root=tmp_path / "user")

    assert probe_command(paths) == ["spotread", *SPOTREAD_ARGS]
