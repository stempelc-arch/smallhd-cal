from pathlib import Path

from smallhd_cal.paths import (
    AppPaths,
    default_app_paths,
    relative_or_absolute,
    resolve_existing_path,
    resolve_output_path,
)


def test_default_app_paths_source_mode(tmp_path: Path) -> None:
    paths = default_app_paths(tmp_path)

    assert paths == AppPaths(resource_root=tmp_path.resolve(), user_data_root=tmp_path.resolve())
    assert (tmp_path / "measurements").exists()
    assert (tmp_path / "luts").exists()


def test_resolve_existing_path_prefers_first_existing_root(tmp_path: Path) -> None:
    resource_root = tmp_path / "resources"
    user_root = tmp_path / "user"
    resource_patch = resource_root / "measurements" / "patches.csv"
    resource_patch.parent.mkdir(parents=True)
    resource_patch.write_text("patch_name,r,g,b\nblack,0,0,0\n", encoding="utf-8")

    assert resolve_existing_path("measurements/patches.csv", resource_root, user_root) == resource_patch


def test_resolve_existing_path_falls_back_to_first_root(tmp_path: Path) -> None:
    assert resolve_existing_path("missing.csv", tmp_path, tmp_path / "user") == tmp_path / "missing.csv"


def test_resolve_output_path_uses_user_root_for_relative_paths(tmp_path: Path) -> None:
    assert resolve_output_path("measurements/session.json", tmp_path) == (
        tmp_path / "measurements" / "session.json"
    )


def test_resolve_output_path_keeps_absolute_paths(tmp_path: Path) -> None:
    absolute = Path("/tmp/session.json")

    assert resolve_output_path(str(absolute), tmp_path) == absolute


def test_relative_or_absolute_returns_relative_for_path_under_root(tmp_path: Path) -> None:
    assert relative_or_absolute(tmp_path, tmp_path / "luts" / "out.cube") == "luts/out.cube"
