from pathlib import Path

from smallhd_cal.measurement import (
    Measurement,
    Patch,
    latest_measurements_by_patch,
    load_patch_sequence,
    read_measurements_json,
    write_measurements_json,
)


def test_load_patch_sequence_accepts_normalized_patch_csv(tmp_path: Path) -> None:
    csv_path = tmp_path / "patches.csv"
    csv_path.write_text(
        "patch_name,r,g,b\n"
        "middle_gray,0.5,0.5,0.5\n"
        "red,1,0,0\n",
        encoding="utf-8",
    )

    patches = load_patch_sequence(csv_path)

    assert patches == [
        Patch("middle_gray", 0.5, 0.5, 0.5),
        Patch("red", 1.0, 0.0, 0.0),
    ]
    assert patches[0].rgb8 == (128, 128, 128)


def test_load_patch_sequence_accepts_legacy_8bit_csv(tmp_path: Path) -> None:
    csv_path = tmp_path / "patches.csv"
    csv_path.write_text("name,r,g,b\nred_bias,255,128,0\n", encoding="utf-8")

    patches = load_patch_sequence(csv_path)

    assert patches == [Patch("red_bias", 1.0, 128 / 255, 0.0)]
    assert patches[0].rgb8 == (255, 128, 0)


def test_measurements_json_round_trip(tmp_path: Path) -> None:
    out = tmp_path / "measurements.json"
    measurements = [
        Measurement(
            patch=Patch("white", 1.0, 1.0, 1.0),
            xyz=(95.0, 100.0, 108.0),
            timestamp="2026-07-02T12:00:00Z",
        )
    ]

    write_measurements_json(out, measurements)

    assert read_measurements_json(out) == measurements


def test_read_measurements_json_returns_empty_for_missing_file(tmp_path: Path) -> None:
    assert read_measurements_json(tmp_path / "missing.json") == []


def test_latest_measurements_by_patch_keeps_latest_duplicate() -> None:
    first = Measurement(Patch("white", 1.0, 1.0, 1.0), (90.0, 95.0, 100.0))
    second = Measurement(Patch("white", 1.0, 1.0, 1.0), (95.0, 100.0, 108.0))

    assert latest_measurements_by_patch([first, second]) == {"white": second}
