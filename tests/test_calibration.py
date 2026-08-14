import numpy as np
import pytest

from smallhd_cal.calibration import (
    D65_XY,
    DCI_WHITE_XY,
    P3_PRIMARIES_XY,
    REC709_PRIMARIES_XY,
    _rgb_to_xyz_matrix,
    build_correction_transform,
    build_grayscale_eotf_correction,
    build_rec709_matrix_correction,
    build_target_matrix_correction,
    color_target,
    make_neutral_transform,
)
from smallhd_cal.measurement import Measurement, Patch
from smallhd_cal.presets import PRESETS


def test_presets_agree_with_their_color_target_gamma() -> None:
    # Each preset hardcodes target_gamma independently of ColorTarget.default_gamma
    # (the GUI/CLI always pass target_gamma explicitly, so nothing reads
    # default_gamma at runtime); this guards against the two silently drifting
    # apart when a new preset is added, which would build a LUT with the wrong
    # EOTF gamma with no error raised.
    for preset in PRESETS.values():
        target = color_target(preset.target_name)
        assert preset.target_gamma == target.default_gamma, (
            f"preset {preset.name!r} target_gamma={preset.target_gamma} does not match "
            f"COLOR_TARGETS[{preset.target_name!r}].default_gamma={target.default_gamma}"
        )


def test_grayscale_correction_is_identity_for_matching_gamma() -> None:
    measurements = _grayscale_measurements(gamma=2.4)

    curve = build_grayscale_eotf_correction(measurements, target_gamma=2.4)

    assert curve(0.0) == pytest.approx(0.0)
    assert curve(0.5) == pytest.approx(0.5)
    assert curve(1.0) == pytest.approx(1.0)


def test_grayscale_correction_compensates_bright_midtones() -> None:
    measurements = _grayscale_measurements(gamma=1.8)

    curve = build_grayscale_eotf_correction(measurements, target_gamma=2.4)

    assert curve(0.5) < 0.5


def test_neutral_transform_applies_curve_to_each_channel() -> None:
    transform = make_neutral_transform(lambda value: value * 0.5)

    assert transform(1.0, 0.5, 0.0) == (0.5, 0.25, 0.0)


def test_grayscale_correction_requires_enough_grayscale_patches() -> None:
    measurements = [Measurement(Patch("black", 0.0, 0.0, 0.0), (0.0, 0.0, 0.0))]

    with pytest.raises(ValueError, match="grayscale"):
        build_grayscale_eotf_correction(measurements)


def _grayscale_measurements(gamma: float) -> list[Measurement]:
    values = [0.0, 0.25, 0.5, 0.75, 1.0]
    return [
        Measurement(
            patch=Patch(f"gray_{int(value * 255):03d}", value, value, value),
            xyz=(value**gamma * 0.95, value**gamma, value**gamma * 1.08),
        )
        for value in values
    ]


# Simulated display: P3-ish primaries (wider than Rec.709), off-D65 white,
# gamma 2.2 tone response, no black offset.
_NATIVE_PRIMARIES_XY = ((0.680, 0.320), (0.265, 0.690), (0.150, 0.060))
_NATIVE_WHITE_XY = (0.3050, 0.3220)
_NATIVE_GAMMA = 2.2
_NATIVE_MATRIX = _rgb_to_xyz_matrix(_NATIVE_PRIMARIES_XY, _NATIVE_WHITE_XY) * 100.0


def _simulate_display(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    linear = np.clip(rgb, 0.0, 1.0) ** _NATIVE_GAMMA
    return tuple(float(value) for value in _NATIVE_MATRIX @ linear)


def _synthetic_display_measurements() -> list[Measurement]:
    measurements = []
    for value in np.linspace(0.0, 1.0, 33):
        value = float(value)
        patch = Patch(f"gray_{int(value * 255):03d}", value, value, value)
        measurements.append(Measurement(patch=patch, xyz=_simulate_display((value,) * 3)))
    for name, rgb in (
        ("red", (1.0, 0.0, 0.0)),
        ("green", (0.0, 1.0, 0.0)),
        ("blue", (0.0, 0.0, 1.0)),
        ("white", (1.0, 1.0, 1.0)),
        ("black", (0.0, 0.0, 0.0)),
    ):
        measurements.append(Measurement(patch=Patch(name, *rgb), xyz=_simulate_display(rgb)))
    return measurements


def _corrected_display_xy_y(
    transform, rgb: tuple[float, float, float]
) -> tuple[tuple[float, float], float]:
    xyz = _simulate_display(transform(*rgb))
    total = sum(xyz)
    return (xyz[0] / total, xyz[1] / total), xyz[1]


def test_rec709_correction_moves_white_to_d65() -> None:
    transform = build_rec709_matrix_correction(_synthetic_display_measurements())

    white_xy, white_y = _corrected_display_xy_y(transform, (1.0, 1.0, 1.0))

    assert white_xy == pytest.approx(D65_XY, abs=2e-3)
    assert white_y > 80.0


def test_rec709_correction_tracks_gray_at_target_gamma() -> None:
    transform = build_rec709_matrix_correction(
        _synthetic_display_measurements(), target_gamma=2.4
    )
    _white_xy, white_y = _corrected_display_xy_y(transform, (1.0, 1.0, 1.0))

    for value in (0.25, 0.5, 0.75):
        gray_xy, gray_y = _corrected_display_xy_y(transform, (value, value, value))
        assert gray_xy == pytest.approx(D65_XY, abs=3e-3)
        assert gray_y / white_y == pytest.approx(value**2.4, abs=5e-3)


def test_rec709_correction_moves_primaries_to_rec709() -> None:
    transform = build_rec709_matrix_correction(_synthetic_display_measurements())

    inputs = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    for rgb, target_xy in zip(inputs, REC709_PRIMARIES_XY, strict=True):
        measured_xy, _y = _corrected_display_xy_y(transform, rgb)
        assert measured_xy == pytest.approx(target_xy, abs=3e-3)


def test_p3_d65_correction_moves_primaries_to_p3_d65() -> None:
    transform = build_target_matrix_correction(
        _synthetic_display_measurements(),
        target_name="p3-d65",
    )

    white_xy, _white_y = _corrected_display_xy_y(transform, (1.0, 1.0, 1.0))
    assert white_xy == pytest.approx(D65_XY, abs=2e-3)

    inputs = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    for rgb, target_xy in zip(inputs, P3_PRIMARIES_XY, strict=True):
        measured_xy, _y = _corrected_display_xy_y(transform, rgb)
        assert measured_xy == pytest.approx(target_xy, abs=3e-3)


def test_dci_p3_correction_targets_dci_white() -> None:
    transform = build_target_matrix_correction(
        _synthetic_display_measurements(),
        target_name="dci-p3",
        target_gamma=2.6,
    )

    white_xy, _white_y = _corrected_display_xy_y(transform, (1.0, 1.0, 1.0))

    assert white_xy == pytest.approx(DCI_WHITE_XY, abs=2e-3)


def test_color_target_rejects_unknown_name() -> None:
    with pytest.raises(ValueError, match="Unknown color target"):
        color_target("aces")


def test_rec709_correction_requires_primary_patches() -> None:
    measurements = _grayscale_measurements(gamma=2.2)

    with pytest.raises(ValueError, match="red/green/blue"):
        build_rec709_matrix_correction(measurements)


def test_build_correction_transform_dispatches_gray_mode() -> None:
    measurements = _grayscale_measurements(gamma=2.4)

    transform = build_correction_transform(measurements, mode="gray", target_gamma=2.4)

    assert transform(0.5, 0.5, 0.5) == pytest.approx((0.5, 0.5, 0.5))


def test_build_correction_transform_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="Unknown correction mode"):
        build_correction_transform(_grayscale_measurements(gamma=2.4), mode="bogus")
