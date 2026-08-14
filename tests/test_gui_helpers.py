from pathlib import Path

from smallhd_cal.gui import _probe_command
from smallhd_cal.paths import AppPaths, relative_or_absolute
from smallhd_cal.probe import SPOTREAD_ARGS


def test_relative_or_absolute_returns_relative_path_inside_root(tmp_path: Path) -> None:
    path = tmp_path / "measurements" / "session.json"

    assert relative_or_absolute(tmp_path, path) == "measurements/session.json"


def test_relative_or_absolute_keeps_absolute_path_outside_root(tmp_path: Path) -> None:
    outside = Path("/tmp/session.json")

    assert relative_or_absolute(tmp_path, outside) == str(outside)


def test_probe_command_uses_bundled_spotread(
    tmp_path: Path,
    monkeypatch,
) -> None:
    spotread = tmp_path / "Argyll_V3.5.0" / "bin" / "spotread"
    monkeypatch.setattr("smallhd_cal.gui.find_bundled_spotread", lambda _root: spotread)

    paths = AppPaths(resource_root=tmp_path, user_data_root=tmp_path / "user")

    assert _probe_command(paths) == [str(spotread), *SPOTREAD_ARGS]


def test_probe_command_falls_back_to_path(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("smallhd_cal.gui.find_bundled_spotread", lambda _root: None)

    paths = AppPaths(resource_root=tmp_path, user_data_root=tmp_path / "user")

    assert _probe_command(paths) == ["spotread", *SPOTREAD_ARGS]


def test_single_instance_lock_excludes_second_holder(tmp_path) -> None:
    from smallhd_cal.gui import acquire_single_instance_lock

    first = acquire_single_instance_lock(tmp_path)
    assert first is not None
    second = acquire_single_instance_lock(tmp_path)
    assert second is None
    first.close()
    third = acquire_single_instance_lock(tmp_path)
    assert third is not None
    third.close()
