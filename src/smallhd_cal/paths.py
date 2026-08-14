from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

APP_SUPPORT_NAME = "SmallHD Calibration"


@dataclass(frozen=True)
class AppPaths:
    resource_root: Path
    user_data_root: Path

    def ensure_user_dirs(self) -> None:
        (self.user_data_root / "measurements").mkdir(parents=True, exist_ok=True)
        (self.user_data_root / "luts").mkdir(parents=True, exist_ok=True)


def default_app_paths(source_root: Path) -> AppPaths:
    if getattr(sys, "frozen", False):
        resource_root = Path(sys._MEIPASS).resolve()
        user_data_root = Path.home() / "Documents" / APP_SUPPORT_NAME
    else:
        resource_root = source_root.resolve()
        user_data_root = source_root.resolve()

    paths = AppPaths(resource_root=resource_root, user_data_root=user_data_root)
    paths.ensure_user_dirs()
    return paths


def resolve_existing_path(value: str, *roots: Path) -> Path:
    if not roots:
        raise ValueError("resolve_existing_path() requires at least one root")
    path = Path(value).expanduser()
    if path.is_absolute():
        return path

    for root in roots:
        candidate = root / path
        if candidate.exists():
            return candidate

    return roots[0] / path


def resolve_output_path(value: str, root: Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return root / path


def relative_or_absolute(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
