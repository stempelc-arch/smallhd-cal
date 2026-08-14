import json
from pathlib import Path

import pytest

from smallhd_cal import sdcard
from smallhd_cal.lut import read_bmd_cube
from smallhd_cal.sdcard import (
    MARKER_NAME,
    SDCardError,
    initialize_card,
    list_volumes,
    read_card,
    scan_cards,
    sync_card,
)
from smallhd_cal.steps import IDENTITY_CUBE_NAME, ensure_identity_lut


def _make_volume(root: Path, name: str) -> Path:
    volume = root / name
    volume.mkdir()
    return volume


def test_list_volumes_skips_hidden_files_and_boot_link(tmp_path: Path) -> None:
    card = _make_volume(tmp_path, "NO NAME")
    (tmp_path / ".Spotlight-V100").mkdir()
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / "Macintosh HD").symlink_to("/")

    assert list_volumes(tmp_path) == [card]


def test_list_volumes_empty_when_root_missing(tmp_path: Path) -> None:
    assert list_volumes(tmp_path / "nope") == []


def test_read_card_uninitialized_then_initialized(tmp_path: Path) -> None:
    volume = _make_volume(tmp_path, "CARD")

    assert read_card(volume).initialized is False

    card = initialize_card(volume)

    assert card.initialized is True
    assert card.managed == ()
    assert read_card(volume).initialized is True


def test_read_card_treats_corrupt_marker_as_uninitialized(tmp_path: Path) -> None:
    volume = _make_volume(tmp_path, "CARD")
    (volume / MARKER_NAME).write_text("not json")

    assert read_card(volume).initialized is False


def test_initialize_card_requires_mounted_volume(tmp_path: Path) -> None:
    with pytest.raises(SDCardError, match="not mounted"):
        initialize_card(tmp_path / "GONE")


def test_scan_cards_puts_initialized_first(tmp_path: Path) -> None:
    _make_volume(tmp_path, "AAA")
    ours = _make_volume(tmp_path, "ZZZ")
    initialize_card(ours)

    cards = scan_cards(tmp_path)

    assert [c.name for c in cards] == ["ZZZ", "AAA"]
    assert cards[0].initialized and not cards[1].initialized


def test_sync_card_copies_and_prunes_only_managed_files(tmp_path: Path) -> None:
    volume = _make_volume(tmp_path, "CARD")
    card = initialize_card(volume)
    operators_own = volume / "my_look.cube"
    operators_own.write_text("KEEP")

    first = tmp_path / "identity.cube"
    first.write_text("LUT_3D_SIZE 17\n")
    card, message = sync_card(card, [first])

    assert (volume / "identity.cube").read_text() == first.read_text()
    assert "identity.cube" in message

    second = tmp_path / "correction_v3.cube"
    second.write_text("LUT_3D_SIZE 17 v3\n")
    card, message = sync_card(card, [second])

    # The LUT from the earlier step is gone; the operator's file is untouched.
    assert not (volume / "identity.cube").exists()
    assert (volume / "correction_v3.cube").exists()
    assert operators_own.read_text() == "KEEP"
    assert "removed old identity.cube" in message

    manifest = json.loads((volume / MARKER_NAME).read_text())
    assert manifest["managed"] == ["correction_v3.cube"]


def test_sync_card_rejects_missing_source_and_unmounted_volume(tmp_path: Path) -> None:
    volume = _make_volume(tmp_path, "CARD")
    card = initialize_card(volume)

    with pytest.raises(SDCardError, match="Missing LUT"):
        sync_card(card, [tmp_path / "ghost.cube"])

    lut = tmp_path / "ok.cube"
    lut.write_text("x")
    gone = sdcard.Card(volume=tmp_path / "GONE", initialized=True)
    with pytest.raises(SDCardError, match="no longer mounted"):
        sync_card(gone, [lut])


def test_eject_runs_diskutil_and_reports_failure(tmp_path: Path, monkeypatch) -> None:
    calls = []

    class Result:
        def __init__(self, code: int) -> None:
            self.returncode = code
            self.stdout = ""
            self.stderr = "in use" if code else ""

    def fake_run(cmd, **_kw):
        calls.append(cmd)
        return Result(0 if len(calls) == 1 else 1)

    monkeypatch.setattr(sdcard.sys, "platform", "darwin")
    monkeypatch.setattr(sdcard.subprocess, "run", fake_run)

    message = sdcard.eject(tmp_path / "CARD")

    assert calls[0] == ["diskutil", "eject", str(tmp_path / "CARD")]
    assert "safe to remove" in message

    with pytest.raises(SDCardError, match="in use"):
        sdcard.eject(tmp_path / "CARD")


def test_ensure_identity_lut_writes_bmd_format_once(tmp_path: Path) -> None:
    path = ensure_identity_lut(tmp_path)

    assert path.name == IDENTITY_CUBE_NAME
    header = path.read_text().splitlines()[:3]
    assert header[1] == "LUT_3D_SIZE 17"

    # Identity: the cube passes values through unchanged.
    cube = read_bmd_cube(path)
    assert cube.lookup(0.0, 0.0, 0.0) == pytest.approx((0.0, 0.0, 0.0))
    assert cube.lookup(1.0, 0.5, 0.25) == pytest.approx((1.0, 0.5, 0.25), abs=1e-5)

    before = path.stat().st_mtime_ns
    assert ensure_identity_lut(tmp_path) == path
    assert path.stat().st_mtime_ns == before
