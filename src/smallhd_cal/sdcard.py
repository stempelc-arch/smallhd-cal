"""SD-card handling for LUT transfers.

The monitor only ever receives LUTs by SD card, so the GUI guides that transfer
instead of leaving the operator to drag files around: initialize a card once
(a marker file claims it for calibration), then whenever that card is inserted
the app copies on exactly the LUT the current step needs, removes the LUTs it
copied for earlier steps (so the monitor's file browser shows one obvious
choice), and ejects the card so it is immediately safe to move to the monitor.

Only files recorded in the card's manifest are ever deleted — anything else on
the card is the operator's and is left alone.

Everything here is parameterized by the volumes root so tests can run against a
temp directory; only :func:`eject` shells out (``diskutil``, macOS).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

MARKER_NAME = ".smallhd_cal_card.json"
VOLUMES_ROOT = Path("/Volumes")


class SDCardError(RuntimeError):
    """A card operation failed in a way the operator must resolve."""


@dataclass(frozen=True)
class Card:
    """One mounted volume, plus what our manifest says we put on it."""

    volume: Path
    initialized: bool
    managed: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return self.volume.name

    @property
    def marker_path(self) -> Path:
        return self.volume / MARKER_NAME


def default_volumes_root() -> Path:
    return VOLUMES_ROOT


def list_volumes(volumes_root: Path | None = None) -> list[Path]:
    """Writable, non-boot volumes an SD card could be mounted as."""
    root = volumes_root or default_volumes_root()
    if not root.is_dir():
        return []
    volumes: list[Path] = []
    for entry in sorted(root.iterdir()):
        if entry.name.startswith("."):
            continue
        try:
            if not entry.is_dir():
                continue
            # The boot volume shows up in /Volumes as a link to /; an SD card
            # mounts as its own directory.
            if entry.resolve() == Path("/"):
                continue
        except OSError:
            continue
        volumes.append(entry)
    return volumes


def read_card(volume: Path) -> Card:
    """Describe a volume: initialized (has our marker) or just a candidate."""
    marker = volume / MARKER_NAME
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        raw_managed = data.get("managed", [])
        if not isinstance(raw_managed, list):
            # ValueError (not TypeError): this is a malformed marker file, the
            # same category as bad JSON below, and the except clause here
            # treats both as "not initialized" — TypeError would slip past it.
            raise ValueError(  # noqa: TRY004
                f"marker 'managed' must be a list, got {type(raw_managed).__name__}"
            )
        managed = tuple(str(name) for name in raw_managed)
        return Card(volume=volume, initialized=True, managed=managed)
    except (OSError, ValueError):
        return Card(volume=volume, initialized=False)


def scan_cards(volumes_root: Path | None = None) -> list[Card]:
    """All plausible cards, initialized ones first."""
    cards = [read_card(v) for v in list_volumes(volumes_root)]
    return sorted(cards, key=lambda c: (not c.initialized, c.name))


def initialize_card(volume: Path) -> Card:
    """Claim a volume for calibration transfers by writing the marker."""
    if not volume.is_dir():
        raise SDCardError(f"{volume} is not mounted.")
    card = Card(volume=volume, initialized=True, managed=())
    _write_marker(card)
    return card


def sync_card(card: Card, files: Sequence[Path]) -> tuple[Card, str]:
    """Copy `files` onto the card and remove LUTs from earlier steps.

    Returns the updated card and a human-readable summary. Only names in the
    manifest are pruned, so operator files on the card are never touched.
    """
    if not card.volume.is_dir():
        raise SDCardError(f"{card.name} is no longer mounted.")
    missing = [f for f in files if not Path(f).is_file()]
    if missing:
        raise SDCardError(f"Missing LUT file(s): {', '.join(str(m) for m in missing)}")

    new_names = [Path(f).name for f in files]
    removed: list[str] = []
    for name in card.managed:
        if name in new_names:
            continue
        stale = card.volume / name
        try:
            if stale.is_file():
                stale.unlink()
                removed.append(name)
        except OSError as exc:
            raise SDCardError(f"Could not remove old LUT {name}: {exc}") from exc

    for f in files:
        try:
            shutil.copy2(f, card.volume / Path(f).name)
        except OSError as exc:
            raise SDCardError(f"Could not copy {Path(f).name} to {card.name}: {exc}") from exc

    updated = Card(volume=card.volume, initialized=True, managed=tuple(new_names))
    _write_marker(updated)

    summary = f"Copied {', '.join(new_names)} to {card.name}"
    if removed:
        summary += f"; removed old {', '.join(removed)}"
    return updated, summary


def eject(volume: Path) -> str:
    """Flush and eject the volume so it is safe to pull immediately."""
    if sys.platform != "darwin":
        raise SDCardError("Automatic eject is only supported on macOS.")
    result = subprocess.run(
        ["diskutil", "eject", str(volume)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise SDCardError(f"Could not eject {volume.name}: {detail or 'unknown error'}")
    return f"Ejected {volume.name} — safe to remove."


def _write_marker(card: Card) -> None:
    payload = {
        "app": "smallhd-cal",
        "initialized": datetime.now(UTC).isoformat(timespec="seconds"),
        "managed": list(card.managed),
    }
    try:
        card.marker_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise SDCardError(f"Could not write to {card.name}: {exc}") from exc
