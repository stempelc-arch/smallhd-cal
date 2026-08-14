import pytest

from smallhd_cal.analysis import estimate_gamma, summarize_measurements, xyz_to_xy
from smallhd_cal.measurement import Measurement, Patch


def test_xyz_to_xy() -> None:
    assert xyz_to_xy((1.0, 2.0, 1.0)) == (0.25, 0.5)
    assert xyz_to_xy((0.0, 0.0, 0.0)) == (0.0, 0.0)


def test_estimate_gamma_from_grayscale_measurements() -> None:
    measurements = _grayscale_measurements(gamma=2.2)

    assert estimate_gamma(measurements) == pytest.approx(2.2)


def test_summarize_measurements() -> None:
    measurements = _grayscale_measurements(gamma=2.4, white_y=120.0, black_y=0.12)

    summary = summarize_measurements(measurements)

    assert summary.total_patches == 5
    assert summary.grayscale_patches == 5
    assert summary.black_y == pytest.approx(0.12)
    assert summary.white_y == pytest.approx(120.0)
    assert summary.contrast_ratio == pytest.approx(1000.0)
    assert summary.estimated_gamma == pytest.approx(2.4)


def test_summarize_measurements_requires_measurements() -> None:
    with pytest.raises(ValueError, match="one measurement"):
        summarize_measurements([])


def _grayscale_measurements(
    gamma: float,
    white_y: float = 100.0,
    black_y: float = 0.0,
) -> list[Measurement]:
    values = [0.0, 0.25, 0.5, 0.75, 1.0]
    return [
        Measurement(
            patch=Patch(f"gray_{int(value * 255):03d}", value, value, value),
            xyz=(
                _luminance(value, gamma, white_y, black_y) * 0.95,
                _luminance(value, gamma, white_y, black_y),
                _luminance(value, gamma, white_y, black_y) * 1.08,
            ),
        )
        for value in values
    ]


def _luminance(value: float, gamma: float, white_y: float, black_y: float) -> float:
    return black_y + (white_y - black_y) * value**gamma


def test_delta_e_2000_matches_sharma_reference_pairs() -> None:
    from smallhd_cal.analysis import delta_e_2000

    pairs = [
        ((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485), 2.0425),
        ((50.0, 3.1571, -77.2803), (50.0, 0.0, -82.7485), 2.8615),
        ((50.0, 2.8361, -74.0200), (50.0, 0.0, -82.7485), 3.4412),
        ((50.0, -1.3802, -84.2814), (50.0, 0.0, -82.7485), 1.0000),
        ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
    ]
    for lab1, lab2, expected in pairs:
        assert abs(delta_e_2000(lab1, lab2) - expected) < 0.001


def test_verify_delta_e_report_scores_perfect_panel_near_zero() -> None:
    import numpy as np

    from smallhd_cal.analysis import verify_delta_e_report
    from smallhd_cal.calibration import _rgb_to_xyz_matrix, color_target
    from smallhd_cal.measurement import Measurement, Patch

    target = color_target("rec709")
    matrix = _rgb_to_xyz_matrix(target.primaries_xy, target.white_xy)
    white_y = 100.0
    scale = white_y / (matrix @ np.ones(3))[1]

    def perfect(name, r, g, b):
        xyz = matrix @ (np.array([r, g, b]) ** 2.4) * scale
        return Measurement(patch=Patch(name=name, r=r, g=g, b=b), xyz=tuple(xyz), timestamp="t")

    measurements = [
        perfect("white", 1, 1, 1),
        perfect("gray_128", 0.5, 0.5, 0.5),
        perfect("red", 1, 0, 0),
        perfect("skin_1", 0.72, 0.48, 0.36),
    ]
    report = verify_delta_e_report(measurements)
    assert report.maximum < 1e-6

    # Perturb one patch and confirm it is flagged as the worst.
    bad = perfect("skin_1", 0.72, 0.48, 0.36)
    nudged = Measurement(
        patch=bad.patch, xyz=(bad.xyz[0] * 1.08, bad.xyz[1], bad.xyz[2]), timestamp="t"
    )
    report = verify_delta_e_report(measurements[:3] + [nudged])
    assert report.worst_name == "skin_1"
    assert report.maximum > 1.0
