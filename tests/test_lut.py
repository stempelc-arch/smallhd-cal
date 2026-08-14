from pathlib import Path

import pytest

from smallhd_cal.lut import expand_legal, squeeze_legal, wrap_legal_range, write_smallhd_cube


def _data_rows(text: str) -> list[list[float]]:
    return [
        [float(part) for part in line.split()]
        for line in text.splitlines()
        if line and not line.startswith("#") and not line.startswith("LUT_SIZE")
    ]


def test_write_smallhd_cube_identity(tmp_path: Path) -> None:
    out = tmp_path / "identity_2.cube"
    write_smallhd_cube(out, 2)
    text = out.read_text(encoding="utf-8")
    assert "# SmallHD Exported LUT." in text
    assert "LUT_SIZE 2" in text
    # 2^3 data rows plus 5 header/comment rows and size row.
    assert len(_data_rows(text)) == 8


def test_write_smallhd_cube_defaults_to_blue_fastest(tmp_path: Path) -> None:
    out = tmp_path / "identity_2.cube"
    write_smallhd_cube(out, 2)
    text = out.read_text(encoding="utf-8")
    assert "# Blue changes fastest" in text
    # Second entry: blue advanced first, red and green still 0.
    assert _data_rows(text)[1] == [0.0, 0.0, 1.0]


def test_write_smallhd_cube_red_fastest_order(tmp_path: Path) -> None:
    out = tmp_path / "identity_2.cube"
    write_smallhd_cube(out, 2, index_order="red-fastest")
    text = out.read_text(encoding="utf-8")
    assert "# Red changes fastest" in text
    assert _data_rows(text)[1] == [1.0, 0.0, 0.0]


def test_write_smallhd_cube_rejects_unknown_index_order(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="index order"):
        write_smallhd_cube(tmp_path / "bad.cube", 2, index_order="green-fastest")


def test_legal_range_round_trip() -> None:
    assert expand_legal(squeeze_legal(0.5)) == pytest.approx(0.5)
    assert squeeze_legal(0.0) == pytest.approx(16.0 / 255.0)
    assert squeeze_legal(1.0) == pytest.approx(235.0 / 255.0)
    assert expand_legal(0.0) == 0.0
    assert expand_legal(1.0) == 1.0


def test_wrap_legal_range_survives_monitor_pipeline() -> None:
    # Monitor pipeline (PageOS 6): index by raw byte, expand stored value to
    # full range, drive panel. Baseline calibration measured drives through a
    # legal-range feed, so intended drive for signal x is squeeze(transform(x)).
    identity = wrap_legal_range(lambda r, g, b: (r, g, b))

    for signal in (0.0, 0.25, 0.5, 0.75, 1.0):
        index = squeeze_legal(signal)
        stored = identity(index, index, index)[0]
        drive = expand_legal(stored)
        assert drive == pytest.approx(squeeze_legal(signal), abs=1e-9)


def test_write_bmd_cube_standard_header_and_order(tmp_path: Path) -> None:
    from smallhd_cal.lut import write_bmd_cube

    out = tmp_path / "bmd_identity.cube"
    write_bmd_cube(out, 2, title="Diag")
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == 'TITLE "Diag"'
    assert lines[1] == "LUT_3D_SIZE 2"
    assert lines[2] == "LUT_3D_INPUT_RANGE 0.0 1.0"
    rows = [[float(p) for p in line.split()] for line in lines[3:]]
    assert len(rows) == 8
    # Standard cubes are red-fastest: second row is the red=1 corner.
    assert rows[0] == [0.0, 0.0, 0.0]
    assert rows[1] == [1.0, 0.0, 0.0]
    assert rows[-1] == [1.0, 1.0, 1.0]


def test_read_bmd_cube_round_trips_transform(tmp_path: Path) -> None:
    from smallhd_cal.lut import read_bmd_cube, write_bmd_cube

    out = tmp_path / "bmd_rot.cube"
    write_bmd_cube(out, 5, lambda r, g, b: (g, b, r))
    lut = read_bmd_cube(out)
    assert lut.lookup(1.0, 0.0, 0.0) == pytest.approx((0.0, 0.0, 1.0))
    assert lut.lookup(0.25, 0.5, 0.75) == pytest.approx((0.5, 0.75, 0.25))
